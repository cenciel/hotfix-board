import json
import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dateutil.relativedelta import relativedelta

st.set_page_config(
    page_title="SOOP 핫픽스 대시보드",
    page_icon="🔧",
    layout="wide",
)

DATA_FILE = Path(__file__).parent / "data" / "releases.json"


# ════════════════════════════════════════════════════════
#  데이터 로드 및 처리
# ════════════════════════════════════════════════════════
def is_hotfix(version: str) -> bool:
    parts = re.split(r"[.\-]", str(version))
    try:
        return int(parts[-1]) > 0
    except (ValueError, IndexError):
        return False


@st.cache_data
def load_data() -> pd.DataFrame:
    with open(DATA_FILE, encoding="utf-8") as f:
        records = json.load(f)
    df = pd.DataFrame(records)
    df["date"]       = pd.to_datetime(df["date"])
    df["year"]       = df["date"].dt.year
    df["month"]      = df["date"].dt.month
    df["year_month"] = df["date"].dt.to_period("M")
    if "hotfix" not in df.columns:
        df["hotfix"] = df["version"].apply(is_hotfix)
    return df.sort_values("date", ascending=False).reset_index(drop=True)


# ════════════════════════════════════════════════════════
#  예측 로직
# ════════════════════════════════════════════════════════
def predict_this_month(df: pd.DataFrame, today: date) -> dict:
    this_year, this_month = today.year, today.month
    this_mask   = (df["year"] == this_year) & (df["month"] == this_month)
    same_month  = df[(df["month"] == this_month) & ~this_mask]
    rate_hist   = same_month["hotfix"].mean() if len(same_month) > 0 else 0.0
    cutoff_3m   = pd.Timestamp(today - relativedelta(months=3))
    recent      = df[(df["date"] >= cutoff_3m) & ~this_mask]
    rate_recent = recent["hotfix"].mean() if len(recent) > 0 else 0.0
    combined    = rate_hist * 0.6 + rate_recent * 0.4
    already     = df[this_mask & df["hotfix"]]
    return {
        "pct":              round(combined * 100),
        "rate_hist":        round(rate_hist * 100),
        "rate_recent":      round(rate_recent * 100),
        "same_month_total": len(same_month),
        "same_month_hf":    int(same_month["hotfix"].sum()),
        "already_hf":       len(already),
        "already_versions": already["version"].tolist(),
    }


# ════════════════════════════════════════════════════════
#  메인 UI
# ════════════════════════════════════════════════════════
def main():
    today  = date.today()
    df_all = load_data()

    # ── 사이드바 ──────────────────────────────────────
    with st.sidebar:
        st.title("⚙️ 설정")
        year_range = st.selectbox(
            "분석 기간", options=[1, 2, 3, 5], index=2,
            format_func=lambda x: f"최근 {x}년",
        )
        st.divider()
        mtime = datetime.fromtimestamp(DATA_FILE.stat().st_mtime)
        st.caption(f"🕐 데이터 기준: **{mtime:%Y-%m-%d}**")
        st.caption(f"📦 전체 {len(df_all)}건")

    # ── 기간 필터링 ──────────────────────────────────
    cutoff = pd.Timestamp(today - relativedelta(years=year_range))
    df     = df_all[df_all["date"] >= cutoff].copy()

    st.title("🔧 SOOP 핫픽스 대시보드")
    st.caption(
        f"APKMirror 릴리즈 데이터  ·  "
        f"{cutoff.strftime('%Y.%m')} – {today.strftime('%Y.%m')}  ·  "
        f"데이터 기준: **{mtime:%Y-%m-%d}**"
    )

    # ── 지표 카드 ─────────────────────────────────────
    total     = len(df)
    hf_count  = int(df["hotfix"].sum())
    rel_count = total - hf_count
    months_n  = max(year_range * 12, 1)
    hf_rate   = hf_count / total * 100 if total > 0 else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("전체 릴리즈",     f"{total}건",         f"{year_range}년간")
    c2.metric("핫픽스 총 횟수", f"{hf_count}건",      f"월평균 {hf_count/months_n:.1f}회")
    c3.metric("핫픽스 비율",     f"{hf_rate:.1f}%",    "전체 대비")
    c4.metric("정식 릴리즈",     f"{rel_count}건",     f"월평균 {rel_count/months_n:.1f}회")
    c5.metric("분석 기간",       f"{year_range}년",    f"{months_n}개월")

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
                st.success(
                    f"이번달 핫픽스 **{pred['already_hf']}건** 배포됨\n\n"
                    + "  \n".join(f"`{v}`" for v in pred["already_versions"])
                )
            else:
                st.info("이번달 아직 핫픽스 없음")

    st.divider()

    # ── 월별 차트 (X축 정렬 및 상단 여백 문제 해결) ─────────────────
    st.subheader("📅 월별 릴리즈 현황")

    monthly = df.groupby(["year_month", "hotfix"]).size().reset_index(name="count")
    monthly["type"] = monthly["hotfix"].map({True: "핫픽스", False: "정식 릴리즈"})
    
    # 1. 시계열 순서로 정렬 및 카테고리 순서 추출
    monthly = monthly.sort_values("year_month")
    monthly["ym_str"] = monthly["year_month"].dt.strftime("%y/%m")
    category_order = monthly["ym_str"].unique().tolist()

    chart_type = st.radio("차트 유형", ["누적 막대", "선형"], horizontal=True)
    color_map  = {"핫픽스": "#E24B4A", "정식 릴리즈": "#378ADD"}

    if chart_type == "누적 막대":
        fig = px.bar(
            monthly, x="ym_str", y="count", color="type",
            color_discrete_map=color_map, barmode="stack",
            category_orders={"ym_str": category_order},
            labels={"ym_str": "연월", "count": "릴리즈 수", "type": "유형"}
        )
    else:
        fig = px.line(
            monthly, x="ym_str", y="count", color="type",
            color_discrete_map=color_map, markers=True,
            category_orders={"ym_str": category_order},
            labels={"ym_str": "연월", "count": "릴리즈 수", "type": "유형"}
        )

    fig.update_layout(
        height=400,
        margin=dict(l=10, r=10, t=60, b=10), # t 마진 충분히 확보
        legend=dict(
            orientation="h", yanchor="bottom", y=1.05, xanchor="left", x=0
        ),
        xaxis_tickangle=-45,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)

    # ── 연도별 차트 (숫자 라벨 잘림 문제 해결) ─────────────────────────
    st.subheader("📈 연도별 월평균 핫픽스")
    yearly_data = df[df["hotfix"]].copy()
    
    if not yearly_data.empty:
        yearly = (
            yearly_data.groupby(["year", "year_month"]).size().reset_index(name="n")
            .groupby("year")["n"].mean().reset_index().rename(columns={"n": "avg"})
        )
        yearly["year"] = yearly["year"].astype(str)
        max_avg = yearly["avg"].max()

        fig2 = px.bar(
            yearly, x="year", y="avg", color="year",
            color_discrete_sequence=["#534AB7", "#0F6E56", "#D95A30", "#378ADD", "#E24B4A"],
            text=yearly["avg"].round(2),
            labels={"year": "연도", "avg": "월평균 핫픽스 수"},
        )
        fig2.update_traces(
            textposition="outside",
            cliponaxis=False # 라벨이 축 밖으로 나가도 표시
        )
        fig2.update_layout(
            height=350, 
            showlegend=False, 
            margin=dict(l=10, r=10, t=50, b=10),
            yaxis=dict(range=[0, max_avg * 1.25]), # 상단 여백 25% 강제 확보
            plot_bgcolor="rgba(0,0,0,0)", 
            paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("해당 기간 내 핫픽스 데이터가 없습니다.")

    # ── 로그 테이블 ───────────────────────────────────
    st.subheader("🗂️ 릴리즈 로그")
    col_f1, col_f2 = st.columns([1, 3])
    with col_f1:
        log_filter = st.selectbox("필터", ["전체", "핫픽스만", "정식만"])
    with col_f2:
        search = st.text_input("버전 검색", placeholder="예: 8.24")

    df_log = df.copy()
    if log_filter == "핫픽스만": df_log = df_log[df_log["hotfix"]]
    elif log_filter == "정식만":  df_log = df_log[~df_log["hotfix"]]
    if search: df_log = df_log[df_log["version"].str.contains(search, na=False)]

    df_disp = df_log[["version", "date", "hotfix"]].copy()
    df_disp["date"]   = df_disp["date"].dt.strftime("%Y.%m.%d")
    df_disp["hotfix"] = df_disp["hotfix"].map({True: "🔴 핫픽스", False: "🟢 정식"})
    df_disp.columns   = ["버전", "날짜", "유형"]

    st.dataframe(
        df_disp, use_container_width=True, hide_index=True,
        height=min(400, (len(df_disp) + 1) * 35 + 3),
    )
    st.caption(f"총 {len(df_disp)}건 표시")


if __name__ == "__main__":
    main()