import json
import os
import re
import tomllib
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

st.set_page_config(
    page_title="SOOP 핫픽스 대시보드",
    page_icon="🔧",
    layout="wide",
)

BASE_DIR  = Path(__file__).parent

_env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=_env_path, override=True)
DATA_FILE = BASE_DIR / "data" / "releases.json"
CFG_FILE  = BASE_DIR / "config.toml"


# ════════════════════════════════════════════════════════
#  설정
# ════════════════════════════════════════════════════════
def load_config() -> dict:
    with open(CFG_FILE, "rb") as f:
        cfg = tomllib.load(f)

    token = ""
    try:
        token = st.secrets["JIRA_API_TOKEN"].strip()
    except Exception:
        pass

    if not token:
        token = os.environ.get("JIRA_API_TOKEN", "").strip()

    if token:
        cfg["jira"]["api_token"] = token

    if not cfg["jira"].get("api_token"):
        cfg["jira"]["api_token"] = ""

    return cfg


# ════════════════════════════════════════════════════════
#  유틸
# ════════════════════════════════════════════════════════
def is_hotfix(version: str) -> bool:
    """버전 마지막 세그먼트 > 0 이면 핫픽스"""
    parts = re.split(r"[.\-]", str(version).strip())
    try:
        return int(parts[-1]) > 0
    except (ValueError, IndexError):
        return False


# ════════════════════════════════════════════════════════
#  파일 I/O
# ════════════════════════════════════════════════════════
def file_load() -> list[dict]:
    """releases.json → list. 없거나 비어있으면 빈 리스트."""
    if not DATA_FILE.exists():
        return []
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = f.read().strip()
            if not data:
                return []
            return json.loads(data)
    except json.JSONDecodeError:
        return []


def file_save(records: list[dict]):
    """날짜 내림차순 정렬 후 저장."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    sorted_list = sorted(
        records,
        key=lambda r: r.get("date") or "0000-00-00",
        reverse=True,
    )
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted_list, f, ensure_ascii=False, indent=2)


def version_sort_key(ver: str):
    """버전 문자열을 정수 튜플로 변환 (정렬용). 예: "8.22.2" → (8, 22, 2)"""
    try:
        return tuple(int(x) for x in str(ver).split("."))
    except Exception:
        return (0, 0, 0)


def file_load_sorted() -> list[dict]:
    """file_load 후 날짜 내림차순, 같은 날짜는 버전 내림차순 정렬."""
    records = file_load()
    return sorted(
        records,
        key=lambda r: (r.get("date") or "0000-00-00", version_sort_key(r.get("version", "0"))),
        reverse=True,
    )


# ════════════════════════════════════════════════════════
#  Jira API
# ════════════════════════════════════════════════════════
def jira_auth_headers(cfg: dict) -> dict:
    import base64
    jira  = cfg["jira"]
    token = base64.b64encode(
        f"{jira['email']}:{jira['api_token']}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json"}


def fetch_and_merge(cfg: dict) -> tuple[int, int]:
    """
    Jira Android 버전 목록 가져와서 기존 데이터에 병합.
    Returns: (신규 추가 건수, 전체 건수)
    """
    jira     = cfg["jira"]
    base_url = jira["base_url"].rstrip("/")
    proj_key = jira["project_key"]
    headers  = jira_auth_headers(cfg)

    resp = requests.get(
        f"{base_url}/rest/api/3/project/{proj_key}/versions",
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()

    # ── 1. 기존 파일 통째로 읽기 ─────────────────────
    existing_list = file_load()        
    existing = {r["version"]: r for r in existing_list} 
    original_count = len(existing)
    added = 0

    # ── 2. Jira 데이터 파싱 ──────────────────────────
    for v in resp.json():
        name     = str(v.get("name", "")).strip()
        rel_date = v.get("releaseDate", "")
        ver_id   = str(v.get("id", ""))
        released = v.get("released", False)
        desc     = str(v.get("description", "")).lower()

        if not released:
            continue

        name_lower = name.lower()
        if "ios" in name_lower:
            continue
        if "androidtv" in name_lower:
            continue
        if "-" in name and "android" not in name_lower:
            continue

        if not rel_date:
            continue

        m = re.search(r"(\d+\.\d+\.\d+)", name)
        if not m:
            continue
        ver = m.group(1)

        jira_url = f"{base_url}/projects/{proj_key}/versions/{ver_id}/tab/release-report-all-issues"

        hf = is_hotfix(ver) or "hotfix" in desc

        if ver not in existing:
            existing[ver] = {
                "version":   ver,
                "date":      rel_date[:10],
                "hotfix":    hf,
                "jira_url":  jira_url,
                "jira_id":   ver_id,
                "jira_name": name,
            }
            added += 1
        else:
            existing[ver]["jira_url"]  = jira_url
            existing[ver]["jira_id"]   = ver_id
            existing[ver]["jira_name"] = name
            existing[ver]["hotfix"]    = hf

    # ── 3. 기존 항목 hotfix/jira_url 필드 보정 ───────
    for r in existing.values():
        if "hotfix" not in r or r["hotfix"] is None:
            r["hotfix"] = is_hotfix(r["version"])
        if "jira_url" not in r:
            r["jira_url"] = ""

    # ── 4. 저장 (기존 건수보다 적으면 저장 거부) ──────
    if len(existing) < original_count:
        raise RuntimeError(
            f"데이터 손실 방지: 기존 {original_count}건 → {len(existing)}건으로 줄어듦, 저장 중단"
        )

    file_save(list(existing.values()))
    return added, len(existing)


# ════════════════════════════════════════════════════════
#  데이터프레임 빌드
# ════════════════════════════════════════════════════════
def build_df() -> pd.DataFrame:
    records = file_load_sorted() 
    if not records:
        return pd.DataFrame(
            columns=["version","date","hotfix","year","month",
                     "year_month","ym_str","jira_url"]
        )
    df = pd.DataFrame(records)
    df["date"]    = pd.to_datetime(df["date"], errors="coerce")
    df            = df.dropna(subset=["date"])  
    df["hotfix"]  = df.apply(
        lambda r: bool(r["hotfix"]) if r.get("hotfix") is not None and not pd.isna(r.get("hotfix"))
        else is_hotfix(r["version"]),
        axis=1,
    )
    if "jira_url" not in df.columns:
        df["jira_url"] = ""
    df["jira_url"]   = df["jira_url"].fillna("").astype(str)
    df["_ver_key"] = df["version"].apply(version_sort_key)
    df = df.sort_values(
        ["date", "_ver_key"], ascending=[False, False]
    ).drop(columns=["_ver_key"]).reset_index(drop=True)
    df["year"]       = df["date"].dt.year
    df["month"]      = df["date"].dt.month
    df["year_month"] = df["date"].dt.to_period("M")
    df["ym_str"]     = df["date"].dt.strftime("%y/%m")
    return df



# ════════════════════════════════════════════════════════
#  핫픽스 예측
# ════════════════════════════════════════════════════════
def predict_this_month(df: pd.DataFrame, today: date) -> dict:
    this_year, this_month = today.year, today.month
    this_mask = (df["year"] == this_year) & (df["month"] == this_month)

    same_month  = df[(df["month"] == this_month) & ~this_mask]
    rate_hist   = float(same_month["hotfix"].mean()) if len(same_month) > 0 else 0.0
    rate_hist   = 0.0 if pd.isna(rate_hist) else rate_hist

    cutoff_3m   = pd.Timestamp(today - relativedelta(months=3))
    recent      = df[(df["date"] >= cutoff_3m) & ~this_mask]
    rate_recent = float(recent["hotfix"].mean()) if len(recent) > 0 else 0.0
    rate_recent = 0.0 if pd.isna(rate_recent) else rate_recent

    combined = rate_hist * 0.6 + rate_recent * 0.4
    already  = df[this_mask & (df["hotfix"] == True)]

    return {
        "pct":              round(combined * 100),
        "rate_hist":        round(rate_hist * 100),
        "rate_recent":      round(rate_recent * 100),
        "same_month_total": len(same_month),
        "same_month_hf":    int(same_month["hotfix"].sum()),
        "already_hf":       len(already),
        "already_versions": already["version"].tolist(),
        "already_urls":     already["jira_url"].tolist(),
    }


# ════════════════════════════════════════════════════════
#  메인 UI
# ════════════════════════════════════════════════════════
def main():
    today = date.today()
    cfg   = load_config()
    jira  = cfg["jira"]

    # ── 사이드바 ──────────────────────────────────────
    with st.sidebar:
        st.title("⚙️ 설정")
        year_range = st.selectbox(
            "분석 기간", options=[1, 2, 3, 5], index=2,
            format_func=lambda x: f"최근 {x}년",
        )
        st.divider()
        st.markdown("#### 🔄 데이터 로드")
        st.caption(f"Jira **{jira['project_key']}** Android 버전을 가져와 병합합니다.")
        load_clicked = st.button("📥 데이터 로드", type="primary", use_container_width=True)

    # ── 버튼 처리 ─────────────────────────────────────
    if load_clicked:
        with st.spinner("Jira에서 데이터 가져오는 중..."):
            try:
                added, total = fetch_and_merge(cfg)
                if added > 0:
                    st.sidebar.success(f"✅ 신규 {added}건 추가 (전체 {total}건)")
                else:
                    st.sidebar.info(f"ℹ️ 신규 없음 (전체 {total}건, 링크 업데이트 완료)")
            except requests.exceptions.HTTPError as e:
                st.sidebar.error(f"❌ HTTP {e.response.status_code if e.response else '?'}: {e}")
                st.stop()
            except Exception as e:
                st.sidebar.error(f"❌ 오류: {e}")
                st.stop()

    # ── 데이터 빌드 ───────────────────────────────────
    df_all = build_df()

    with st.sidebar:
        st.divider()
        if len(df_all) > 0 and DATA_FILE.exists():
            mtime  = datetime.fromtimestamp(DATA_FILE.stat().st_mtime)
            linked = int((df_all["jira_url"] != "").sum())
            hf_cnt = int(df_all["hotfix"].sum())
            st.caption(f"🕐 마지막 로드: **{mtime:%Y-%m-%d %H:%M}**")
            st.caption(f"📦 전체 {len(df_all)}건  |  🔴 핫픽스 {hf_cnt}건")
            st.caption(f"🔗 Jira 링크: {linked}건")
        else:
            st.caption("📭 데이터 없음 — 데이터 로드를 눌러주세요")

    if len(df_all) == 0:
        st.title("🔧 SOOP 핫픽스 대시보드")
        st.info("👈 사이드바에서 **데이터 로드** 버튼을 눌러주세요.")
        st.stop()

    # ── 기간 필터 적용 ────────────────────────────────
    cutoff = pd.Timestamp(today - relativedelta(years=year_range))
    df     = df_all[df_all["date"] >= cutoff].copy()
    mtime  = datetime.fromtimestamp(DATA_FILE.stat().st_mtime)

    # ── 헤더 ──────────────────────────────────────────
    st.title("🔧 SOOP 핫픽스 대시보드")
    st.caption(
        f"Jira **{jira['project_key']}** Android 릴리즈  ·  "
        f"{cutoff.strftime('%Y.%m')} – {today.strftime('%Y.%m')}  ·  "
        f"마지막 로드: **{mtime:%Y-%m-%d %H:%M}**"
    )

    # ── 지표 카드 ─────────────────────────────────────
    total     = len(df)
    hf_count  = int(df["hotfix"].sum())
    rel_count = total - hf_count
    months_n  = max(year_range * 12, 1)
    hf_rate   = hf_count / total * 100 if total > 0 else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("전체 릴리즈",    f"{total}건",        f"{year_range}년간")
    c2.metric("핫픽스 총 횟수", f"{hf_count}건",     f"월평균 {hf_count/months_n:.1f}회")
    c3.metric("핫픽스 비율",    f"{hf_rate:.1f}%",   "전체 대비")
    c4.metric("정식 릴리즈",    f"{rel_count}건",    f"월평균 {rel_count/months_n:.1f}회")
    c5.metric("분석 기간",      f"{year_range}년",   f"{months_n}개월")

    st.divider()

    # ── 이번달 예측 ───────────────────────────────────
    pred          = predict_this_month(df, today)
    pct           = pred["pct"]
    verdict_emoji = "🔴" if pct >= 60 else "🟡" if pct >= 35 else "🟢"
    verdict_text  = "핫픽스 가능성 높음" if pct >= 60 else "보통 수준" if pct >= 35 else "핫픽스 가능성 낮음"

    with st.container(border=True):
        st.subheader(f"📊 {today.strftime('%Y년 %m월')} 핫픽스 전망")
        col_l, col_r = st.columns([2, 1])
        with col_l:
            st.markdown(f"**핫픽스 발생 확률: {pct}%**")
            st.progress(pct / 100)
            st.caption(
                f"동월 과거 이력 {pred['same_month_total']}건 중 "
                f"핫픽스 {pred['same_month_hf']}건 ({pred['rate_hist']}%)  ·  "
                f"최근 3개월 {pred['rate_recent']}%  →  가중합산 6:4"
            )
        with col_r:
            st.markdown(f"### {verdict_emoji} {verdict_text}")
            if pred["already_hf"] > 0:
                lines = [
                    f"[`{v}`]({u})" if u else f"`{v}`"
                    for v, u in zip(pred["already_versions"], pred["already_urls"])
                ]
                st.success(
                    f"이번달 핫픽스 **{pred['already_hf']}건** 배포됨\n\n"
                    + "  \n".join(lines)
                )
            else:
                st.info("이번달 아직 핫픽스 없음")

    st.divider()

    # ── 월별 차트 ─────────────────────────────────────
    st.subheader("📅 월별 릴리즈 현황")
    monthly = (
        df.groupby(["ym_str", "year_month", "hotfix"])
        .size().reset_index(name="count")
    )
    monthly["type"] = monthly["hotfix"].map({True: "핫픽스", False: "정식 릴리즈"})
    monthly = monthly.sort_values("year_month")
    month_order = monthly.sort_values("year_month")["ym_str"].unique().tolist()

    chart_type = st.radio("차트 유형", ["누적 막대", "선형"], horizontal=True)
    color_map  = {"핫픽스": "#E24B4A", "정식 릴리즈": "#378ADD"}

    fig = px.bar(
        monthly, x="ym_str", y="count", color="type",
        color_discrete_map=color_map, barmode="stack",
        category_orders={"ym_str": month_order},
        labels={"ym_str": "연월", "count": "릴리즈 수", "type": "유형"},
    ) if chart_type == "누적 막대" else px.line(
        monthly, x="ym_str", y="count", color="type",
        color_discrete_map=color_map, markers=True,
        category_orders={"ym_str": month_order},
        labels={"ym_str": "연월", "count": "릴리즈 수", "type": "유형"},
    )
    fig.update_layout(
        height=340,
        margin=dict(l=0, r=0, t=60, b=0),
        legend=dict(orientation="h", y=1.15, x=0),
        xaxis_tickangle=-45,
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── 연도별 차트 ───────────────────────────────────
    st.subheader("📈 연도별 월평균 핫픽스")
    df_hf  = df[df["hotfix"] == True].copy()
    if len(df_hf) > 0:
        yearly = (
            df_hf.groupby(["year", "year_month"]).size().reset_index(name="n")
            .groupby("year")["n"].mean().reset_index().rename(columns={"n": "avg"})
        )
        yearly["year"] = yearly["year"].astype(str)
        fig2 = px.bar(
            yearly, x="year", y="avg", color="year",
            color_discrete_sequence=["#534AB7","#0F6E56","#D95A30","#378ADD","#E24B4A"],
            text=yearly["avg"].round(2),
            labels={"year": "연도", "avg": "월평균 핫픽스 수"},
        )
        fig2.update_traces(textposition="inside", textfont_color="white")
        fig2.update_layout(
            height=260, showlegend=False,
            margin=dict(l=0, r=0, t=10, b=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            xaxis=dict(type="category"),
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("핫픽스 데이터가 없습니다.")

    st.divider()

    # ── 릴리즈 로그 ───────────────────────────────────
    st.subheader("🗂️ 릴리즈 로그")

    col_f1, col_f2 = st.columns([1, 3])
    with col_f1:
        log_filter = st.selectbox("필터", ["전체", "핫픽스만", "정식만"])
    with col_f2:
        search = st.text_input("버전 검색", placeholder="예: 8.24")

    df["_ver_key"] = df["version"].apply(version_sort_key)
    df_log = df.sort_values(
        ["date", "_ver_key"], ascending=[False, False]
    ).drop(columns=["_ver_key"]).reset_index(drop=True).copy()

    if log_filter == "핫픽스만":
        df_log = df_log[df_log["hotfix"] == True]
    elif log_filter == "정식만":
        df_log = df_log[df_log["hotfix"] == False]
    if search:
        df_log = df_log[df_log["version"].str.contains(search, na=False)]

    df_disp = pd.DataFrame({
        "버전": df_log["version"].values,
        "날짜": df_log["date"].dt.strftime("%Y.%m.%d").values,
        "유형": ["🔴 핫픽스" if h else "🟢 정식" for h in df_log["hotfix"].values],
        "Jira": [u if u else None for u in df_log["jira_url"].values],
    })

    row_h    = 35
    header_h = 36
    max_rows = 15
    tbl_h    = header_h + row_h * min(len(df_disp), max_rows)

    st.dataframe(
        df_disp,
        use_container_width=True,
        hide_index=True,
        height=tbl_h,
        column_config={
            "버전": st.column_config.TextColumn("버전", width="small"),
            "날짜": st.column_config.TextColumn("날짜", width="small"),
            "유형": st.column_config.TextColumn("유형", width="small"),
            "Jira": st.column_config.LinkColumn(
                "Jira 링크", width="small", display_text="🔗 열기"
            ),
        },
    )
    st.caption(f"총 {len(df_disp)}건 표시")


if __name__ == "__main__":
    main()