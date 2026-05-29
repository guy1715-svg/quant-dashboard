# ============================================================
# 퀀트 대시보드 — Streamlit + pykrx + Gemini
# 실행: streamlit run quant_dashboard.py
# 설치: pip install streamlit pykrx plotly google-generativeai pandas numpy
# ============================================================

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# ── 페이지 설정 ──
st.set_page_config(
    page_title="퀀트 관제탑",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ── Google Sheets 기반 관심종목 저장 ──
import os
import gspread
from google.oauth2.service_account import Credentials

DEFAULT_WATCHLIST = "042700,한미반도체\n005930,삼성전자\n000660,SK하이닉스\n012450,한화에어로스페이스\n329180,HD현대중공업"

@st.cache_resource
def get_gsheet():
    """Google Sheets 연결 (캐시 — 연결 1회만)"""
    creds_dict = dict(st.secrets["gcp_service_account"])
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(st.secrets["SHEET_ID"])
    return sh.sheet1

@st.cache_data(ttl=30, show_spinner=False)
def load_watchlist():
    """Google Sheets에서 관심종목 로드 (30초 캐시)"""
    try:
        ws = get_gsheet()
        data = ws.get_all_values()
        if data:
            result = "\n".join([",".join(row) for row in data if len(row) >= 2])
            return result
        else:
            # Sheets 비어있으면 DEFAULT 반환 후 Sheets에 저장
            return DEFAULT_WATCHLIST
    except Exception as e:
        pass
    if 'watchlist_data' in st.session_state:
        return st.session_state.watchlist_data
    return DEFAULT_WATCHLIST

def get_watchlist_fast():
    """session_state 우선 반환 (Sheets 호출 최소화)"""
    if 'watchlist_data' in st.session_state:
        return st.session_state.watchlist_data
    return load_watchlist()

def save_watchlist(text):
    """관심종목 저장"""
    # 1. session_state 즉시 업데이트
    st.session_state.watchlist_data = text
    # 2. 캐시 클리어
    load_watchlist.clear()
    # 3. Google Sheets 저장 — 오류 노출
    ws = get_gsheet()
    ws.clear()
    rows = []
    for line in text.strip().split("\n"):
        parts = line.strip().split(",", 1)
        if len(parts) == 2:
            rows.append(parts)
    if rows:
        ws.update(rows, "A1")

def get_watchlist_tickers():
    wl = load_watchlist()
    result = []
    for line in wl.strip().split("\n"):
        parts = line.strip().split(",", 1)
        if len(parts) == 2:
            result.append((parts[0].strip(), parts[1].strip()))
    return result

def add_ticker(ticker, name):
    wl = load_watchlist()
    tickers = [l.split(",")[0].strip() for l in wl.split("\n") if "," in l]
    if ticker not in tickers:
        save_watchlist(wl.strip() + f"\n{ticker},{name}")
        return True
    return False

def remove_ticker(ticker):
    wl = load_watchlist()
    lines = [l for l in wl.split("\n")
             if l.strip() and l.split(",")[0].strip() != ticker]
    save_watchlist("\n".join(lines))

# session_state 초기화
if 'passed' not in st.session_state:
    st.session_state.passed = []
if 'watchlist_data' not in st.session_state:
    st.session_state.watchlist_data = DEFAULT_WATCHLIST

# ── 스타일 ──
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Noto+Sans+KR:wght@400;700&display=swap');

html, body, [class*="css"] {
    font-family: 'Noto Sans KR', sans-serif;
    background-color: #0a0e1a;
    color: #e0e6f0;
}
.stApp { background-color: #0a0e1a; }

/* 카드 */
.metric-card {
    background: linear-gradient(135deg, #111827 0%, #1a2235 100%);
    border: 1px solid #1e3a5f;
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 12px;
}
.metric-card .label {
    font-size: 11px;
    color: #6b7fa3;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    font-family: 'IBM Plex Mono', monospace;
}
.metric-card .value {
    font-size: 24px;
    font-weight: 700;
    font-family: 'IBM Plex Mono', monospace;
    margin-top: 4px;
}
.up   { color: #ff4d6d; }
.down { color: #4da6ff; }
.flat { color: #a0b0c8; }

/* 신호 뱃지 */
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
    margin: 2px;
}
.badge-buy    { background: #1a3a2a; color: #4dff91; border: 1px solid #2d6644; }
.badge-sell   { background: #3a1a1a; color: #ff6b6b; border: 1px solid #6b2d2d; }
.badge-watch  { background: #1a2a3a; color: #6bbfff; border: 1px solid #2d4a6b; }
.badge-neutral{ background: #1e2535; color: #8899bb; border: 1px solid #2d3a55; }

/* Gemini 결과 박스 */
.gemini-box {
    background: #0f1726;
    border-left: 3px solid #4da6ff;
    border-radius: 0 8px 8px 0;
    padding: 16px 20px;
    font-size: 14px;
    line-height: 1.7;
    white-space: pre-wrap;
    font-family: 'Noto Sans KR', sans-serif;
}

/* 사이드바 */
[data-testid="stSidebar"] {
    background-color: #080c18;
    border-right: 1px solid #1e3a5f;
}

/* 구분선 */
hr { border-color: #1e3a5f; }

/* 테이블 */
.watchlist-row {
    display: flex;
    align-items: center;
    padding: 10px 0;
    border-bottom: 1px solid #1a2535;
    gap: 12px;
}

/* ── 모바일 반응형 ── */
@media (max-width: 768px) {
    .metric-card { padding: 10px 12px; }
    .metric-card .value { font-size: 18px; }
    .metric-card .label { font-size: 10px; }
    .badge { font-size: 10px; padding: 2px 7px; }
    .gemini-box { font-size: 13px; padding: 12px 14px; }
    .stTabs [data-baseweb="tab"] { font-size: 11px; padding: 5px 6px; }
    h1, h2, h3 { font-size: 16px !important; }
}
/* 탭 스타일 */
.stTabs [data-baseweb="tab-list"] {
    gap: 4px; background: #080c18; padding: 4px; border-radius: 8px;
}
.stTabs [data-baseweb="tab"] {
    background: transparent; border-radius: 6px; color: #6b7fa3; font-weight: 600;
}
.stTabs [aria-selected="true"] {
    background: #1e3a5f !important; color: #e0e6f0 !important;
}
/* 버튼 */
.stButton > button {
    background: linear-gradient(135deg, #1e3a5f, #2d5a8f);
    color: #e0e6f0; border: 1px solid #2d5a8f;
    border-radius: 8px; font-weight: 600;
}
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════
# 데이터 함수
# ══════════════════════════════════════════

@st.cache_data(ttl=60, show_spinner=False)
def fetch_ohlcv(ticker, lookback=80):
    import yfinance as yf
    # 한국 종목: 종목코드 + .KS (코스피) 또는 .KQ (코스닥)
    end   = datetime.today()
    start = end - timedelta(days=lookback*2)
    # KS 먼저 시도, 실패 시 KQ
    for suffix in ['.KS', '.KQ']:
        try:
            yt = yf.Ticker(ticker + suffix)
            df = yt.history(start=start, end=end, interval='1d')
            if df is None or df.empty:
                continue
            df = df.rename(columns={
                'Open':'시가', 'High':'고가', 'Low':'저가',
                'Close':'종가', 'Volume':'거래량'
            })
            df = df[['시가','고가','저가','종가','거래량']]
            df = df[df['거래량'] > 0].tail(lookback)
            if len(df) >= 5:
                return df
        except Exception:
            continue
    return None

def calc_indicators(df):
    for n in [5, 20, 60, 120]:
        df[f'MA{n}'] = df['종가'].rolling(n).mean().round(0)
    df['BB_mid']   = df['종가'].rolling(20).mean()
    std            = df['종가'].rolling(20).std()
    df['BB_upper'] = (df['BB_mid'] + 2*std).round(0)
    df['BB_lower'] = (df['BB_mid'] - 2*std).round(0)
    df['BB_mid']   = df['BB_mid'].round(0)
    delta = df['종가'].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df['RSI'] = (100 - 100/(1 + gain/loss.replace(0, np.nan))).round(1)
    ema12 = df['종가'].ewm(span=12, adjust=False).mean()
    ema26 = df['종가'].ewm(span=26, adjust=False).mean()
    df['MACD']      = (ema12 - ema26).round(0)
    df['Signal']    = df['MACD'].ewm(span=9, adjust=False).mean().round(0)
    df['MACD_hist'] = (df['MACD'] - df['Signal']).round(0)
    low10  = df['저가'].rolling(10).min()
    high10 = df['고가'].rolling(10).max()
    df['Sto_K'] = (100*(df['종가']-low10)/(high10-low10).replace(0,np.nan)).round(1)
    df['Sto_D'] = df['Sto_K'].rolling(5).mean().round(1)
    df['거래량_비율'] = (df['거래량']/df['거래량'].shift(1)*100).round(1)
    # 52주 고저
    df['52W_high'] = df['고가'].rolling(min(252, len(df))).max()
    df['52W_low']  = df['저가'].rolling(min(252, len(df))).min()
    # OBV (On Balance Volume)
    obv = [0]
    for i in range(1, len(df)):
        if df['종가'].iloc[i] > df['종가'].iloc[i-1]:
            obv.append(obv[-1] + df['거래량'].iloc[i])
        elif df['종가'].iloc[i] < df['종가'].iloc[i-1]:
            obv.append(obv[-1] - df['거래량'].iloc[i])
        else:
            obv.append(obv[-1])
    df['OBV'] = obv
    df['OBV_MA'] = df['OBV'].rolling(9).mean()
    # 지지/저항선 자동 감지 (최근 20일 고저점)
    df['지지선'] = df['저가'].rolling(20).min()
    df['저항선'] = df['고가'].rolling(20).max()
    return df

def get_signal(df):
    l = df.iloc[-1]
    signals = []
    if l['RSI'] <= 30:               signals.append(('📉 과매도', 'watch'))
    if l['RSI'] >= 70:               signals.append(('📈 과매수', 'sell'))
    if l['거래량_비율'] >= 200:       signals.append(('🔥 거래량폭발', 'buy'))
    if l['종가'] > l['MA5'] > l['MA20']: signals.append(('✅ 정배열', 'buy'))
    if l['종가'] < l['MA5'] < l['MA20']: signals.append(('❌ 역배열', 'sell'))
    if l['MACD'] > l['Signal'] and df.iloc[-2]['MACD'] <= df.iloc[-2]['Signal']:
        signals.append(('⚡ 골든크로스', 'buy'))
    if l['MACD'] < l['Signal'] and df.iloc[-2]['MACD'] >= df.iloc[-2]['Signal']:
        signals.append(('💀 데드크로스', 'sell'))
    if not signals: signals.append(('➖ 중립', 'neutral'))
    return signals

def build_prompt(df, name, ticker):
    l = df.iloc[-1]; p = df.iloc[-2]
    w = df.iloc[-6] if len(df)>=6 else df.iloc[0]
    macd_sig = ('골든크로스' if l['MACD']>l['Signal'] and p['MACD']<=p['Signal'] else
                '데드크로스' if l['MACD']<l['Signal'] and p['MACD']>=p['Signal'] else
                'MACD>Signal' if l['MACD']>l['Signal'] else 'MACD<Signal')
    rsi_s = '과매수' if l['RSI']>=70 else '과매도' if l['RSI']<=30 else '중립'
    bb_r  = l['BB_upper']-l['BB_lower']
    bb_p  = round((l['종가']-l['BB_lower'])/bb_r*100,1) if bb_r>0 else 50
    cur   = l['종가']
    lines = [
        f'종목: {name} ({ticker}) | 분석일: {str(df.index[-1])[:10]}',
        f'현재가: {cur:,.0f}원 | 전일대비: {round((cur/p["종가"]-1)*100,2)}% | 1주일대비: {round((cur/w["종가"]-1)*100,2)}%',
        f'시가: {l["시가"]:,.0f} | 고가: {l["고가"]:,.0f} | 저가: {l["저가"]:,.0f}',
        f'MA5: {l["MA5"]:,.0f} | MA20: {l["MA20"]:,.0f} | MA60: {l["MA60"]:,.0f} | MA120: {l["MA120"]:,.0f}',
        f'BB 상단: {l["BB_upper"]:,.0f} | 중단: {l["BB_mid"]:,.0f} | 하단: {l["BB_lower"]:,.0f} | 위치: {bb_p}%',
        f'MACD: {l["MACD"]:,.0f} / Signal: {l["Signal"]:,.0f} -> {macd_sig}',
        f'RSI(14): {l["RSI"]} -> {rsi_s} | Sto K: {l["Sto_K"]} D: {l["Sto_D"]}',
        f'거래량: {l["거래량"]:,}주 | 전일대비: {l["거래량_비율"]:.0f}% | 20일평균: {df["거래량"].tail(20).mean():,.0f}주',
        f'52주 고가: {l["52W_high"]:,.0f} | 52주 저가: {l["52W_low"]:,.0f}',
        '',
        '분석 요청 (R:R 2.0이상 / 손절 -7% 적용):',
        '1.추세판정  2.지지/저항  3.매수조건  4.손절가  5.목표가(R:R포함)  6.리스크  7.최종판정[매수검토/관망/매수불가]',
    ]
    return '\n'.join(lines)


# ══════════════════════════════════════════
# 차트 함수
# ══════════════════════════════════════════

def make_chart(df, name, entry=None, stoploss=None, target1=None, target2=None):
    fig = make_subplots(
        rows=5, cols=1,
        shared_xaxes=True,
        row_heights=[0.42, 0.12, 0.15, 0.15, 0.16],
        vertical_spacing=0.02,
        subplot_titles=('', '거래량', 'MACD', 'RSI', 'OBV')
    )
    # ── 캔들 ──
    fig.add_trace(go.Candlestick(
        x=df.index, open=df['시가'], high=df['고가'],
        low=df['저가'], close=df['종가'],
        increasing_line_color='#ff4d6d', decreasing_line_color='#4da6ff',
        increasing_fillcolor='#ff4d6d', decreasing_fillcolor='#4da6ff',
        name='캔들', showlegend=False
    ), row=1, col=1)

    # ── 이평선 ──
    ma_colors = {'MA5':'#ffd166','MA20':'#06d6a0','MA60':'#a78bfa','MA120':'#38bdf8'}
    for ma, c in ma_colors.items():
        if ma in df.columns:
            fig.add_trace(go.Scatter(x=df.index, y=df[ma], line=dict(color=c, width=1.2),
                                     name=ma, opacity=0.85), row=1, col=1)
    # ── 볼린저밴드 ──
    fig.add_trace(go.Scatter(x=df.index, y=df['BB_upper'],
                             line=dict(color='#475569', width=0.8, dash='dash'),
                             name='BB상단', showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['BB_lower'],
                             line=dict(color='#475569', width=0.8, dash='dash'),
                             fill='tonexty', fillcolor='rgba(71,85,105,0.08)',
                             name='BB하단', showlegend=False), row=1, col=1)

    # ── 지지/저항선 ──
    if '지지선' in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df['지지선'],
                                 line=dict(color='#4dff91', width=0.8, dash='dot'),
                                 name='지지선', opacity=0.6), row=1, col=1)
    if '저항선' in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df['저항선'],
                                 line=dict(color='#ff6b6b', width=0.8, dash='dot'),
                                 name='저항선', opacity=0.6), row=1, col=1)

    # ── 매수/손절/목표가 라인 ──
    last_x = df.index[-1]
    if entry:
        fig.add_hline(y=entry, line_color='#ffd166', line_width=1.5,
                      annotation_text=f'매수 {entry:,.0f}', annotation_position='right',
                      annotation_font_color='#ffd166', row=1, col=1)
    if stoploss:
        fig.add_hline(y=stoploss, line_color='#ff4d6d', line_width=1.5, line_dash='dash',
                      annotation_text=f'손절 {stoploss:,.0f} (-7%)', annotation_position='right',
                      annotation_font_color='#ff4d6d', row=1, col=1)
    if target1:
        fig.add_hline(y=target1, line_color='#4dff91', line_width=1.5,
                      annotation_text=f'1차목표 {target1:,.0f}', annotation_position='right',
                      annotation_font_color='#4dff91', row=1, col=1)
    if target2:
        fig.add_hline(y=target2, line_color='#06d6a0', line_width=1.2, line_dash='dash',
                      annotation_text=f'2차목표 {target2:,.0f}', annotation_position='right',
                      annotation_font_color='#06d6a0', row=1, col=1)

    # ── 거래량 ──
    colors_vol = ['#ff4d6d' if df['종가'].iloc[i] >= df['시가'].iloc[i] else '#4da6ff'
                  for i in range(len(df))]
    fig.add_trace(go.Bar(x=df.index, y=df['거래량'], marker_color=colors_vol,
                         opacity=0.7, name='거래량', showlegend=False), row=2, col=1)

    # ── MACD ──
    hist_colors = ['#ff4d6d' if v >= 0 else '#4da6ff' for v in df['MACD_hist']]
    fig.add_trace(go.Bar(x=df.index, y=df['MACD_hist'], marker_color=hist_colors,
                         opacity=0.6, name='히스토그램', showlegend=False), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['MACD'],
                             line=dict(color='#38bdf8', width=1.2), name='MACD'), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['Signal'],
                             line=dict(color='#f472b6', width=1.2), name='Signal'), row=3, col=1)
    fig.add_hline(y=0, line_color='#2d3a55', line_width=0.5, row=3, col=1)

    # ── RSI ──
    fig.add_trace(go.Scatter(x=df.index, y=df['RSI'],
                             line=dict(color='#a78bfa', width=1.5),
                             name='RSI', showlegend=False), row=4, col=1)
    fig.add_hline(y=70, line_dash='dash', line_color='#ff4d6d', line_width=0.8, row=4, col=1)
    fig.add_hline(y=30, line_dash='dash', line_color='#4da6ff', line_width=0.8, row=4, col=1)
    fig.add_hrect(y0=70, y1=100, fillcolor='rgba(255,77,109,0.05)', line_width=0, row=4, col=1)
    fig.add_hrect(y0=0,  y1=30,  fillcolor='rgba(77,166,255,0.05)', line_width=0, row=4, col=1)

    # ── OBV ──
    if 'OBV' in df.columns:
        obv_color = '#4dff91' if df['OBV'].iloc[-1] > df['OBV'].iloc[-2] else '#ff6b6b'
        fig.add_trace(go.Scatter(x=df.index, y=df['OBV'],
                                 line=dict(color=obv_color, width=1.3),
                                 name='OBV', showlegend=False), row=5, col=1)
        if 'OBV_MA' in df.columns:
            fig.add_trace(go.Scatter(x=df.index, y=df['OBV_MA'],
                                     line=dict(color='#ffd166', width=1.0, dash='dash'),
                                     name='OBV MA9', showlegend=False), row=5, col=1)

    fig.update_layout(
        title=dict(text=f'<b>{name}</b>', font=dict(size=16, color='#e0e6f0'), x=0.01),
        paper_bgcolor='#0a0e1a', plot_bgcolor='#0f1726',
        font=dict(color='#8899bb', size=11),
        xaxis_rangeslider_visible=False,
        height=820,
        legend=dict(orientation='h', y=1.02, x=0, font=dict(size=10),
                    bgcolor='rgba(0,0,0,0)', bordercolor='rgba(0,0,0,0)'),
        margin=dict(l=10, r=80, t=50, b=10),
    )
    for i in range(1, 6):
        fig.update_xaxes(gridcolor='#1a2535', row=i, col=1, showgrid=True)
        fig.update_yaxes(gridcolor='#1a2535', row=i, col=1, showgrid=True,
                         tickfont=dict(family='IBM Plex Mono', size=10))
    fig.update_yaxes(range=[0,100], row=4, col=1)
    return fig


# ══════════════════════════════════════════
# 사이드바
# ══════════════════════════════════════════

with st.sidebar:
    st.markdown("## ⚙️ 설정")
    st.markdown("---")

    gemini_key = st.text_input("🔑 Gemini API 키", type="password",
                                help="aistudio.google.com에서 발급")

    st.markdown("### 📋 관심 종목")
    wl_now = load_watchlist()
    ticker_input = st.text_area(
        "종목코드,종목명 (한 줄에 하나)",
        value=wl_now,
        height=160,
        key="ticker_textarea"
    )
    # 사용자가 직접 수정한 경우 파일에 저장
    if ticker_input != wl_now:
        save_watchlist(ticker_input)

    n = len([l for l in load_watchlist().split('\n') if ',' in l.strip()])
    st.markdown(f"<div style='font-size:11px; color:#4dff91'>✅ 총 {n}개 종목 등록됨</div>", unsafe_allow_html=True)

    lookback = st.slider("분석 기간 (거래일)", 30, 120, 60)

    model_name = st.selectbox("Gemini 모델", [
        "models/gemini-2.5-flash",
        "models/gemini-2.5-pro",
        "models/gemini-2.0-flash",
        "models/gemini-3.1-pro-preview",
    ])

    st.markdown(f"<div style='font-size:10px; color:#475569; text-align:center'>마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}</div>", unsafe_allow_html=True)
    refresh = st.button("🔄 강제 새로고침", use_container_width=True)
    if refresh:
        st.cache_data.clear()
        st.success("캐시 초기화 완료!")
        import time; time.sleep(0.5)
        st.rerun()

    st.markdown("---")
    st.markdown("""
    <div style='font-size:11px; color:#475569; line-height:1.8'>
    📌 <b>보완 규칙 적용 중</b><br>
    • R:R 2.0 미만 기각<br>
    • 손절 -7% 킬스위치<br>
    • 09:00~09:30 진입 금지<br>
    • 물타기 절대 금지<br>
    • 현금 20% 유지
    </div>
    """, unsafe_allow_html=True)

# ── 종목 파싱 — session_state 우선 ──
def get_watchlist_tickers():
    wl = get_watchlist_fast()
    result = []
    for line in wl.strip().split("\n"):
        parts = line.strip().split(",", 1)
        if len(parts) == 2:
            result.append((parts[0].strip(), parts[1].strip()))
    return result

TICKERS = get_watchlist_tickers()


# ══════════════════════════════════════════
# 메인
# ══════════════════════════════════════════

st.markdown("""
<div style='display:flex; align-items:center; gap:12px; margin-bottom:8px'>
    <span style='font-size:28px; font-weight:800; font-family:"IBM Plex Mono",monospace;
                 background:linear-gradient(90deg,#4da6ff,#a78bfa); -webkit-background-clip:text;
                 -webkit-text-fill-color:transparent'>퀀트 관제탑</span>
    <span style='font-size:12px; color:#475569; font-family:"IBM Plex Mono",monospace'>V8.9</span>
</div>
""", unsafe_allow_html=True)

now = datetime.now().strftime('%Y.%m.%d %H:%M KST')
st.markdown(f"<div style='font-size:12px; color:#475569; font-family:\"IBM Plex Mono\",monospace; margin-bottom:20px'>⏱ {now}</div>", unsafe_allow_html=True)

# ── 탭 ──
tab0, tab1, tab2, tab3, tab4, tab5 = st.tabs(["🌏 시장 지수", "📊 현황판", "📈 차트 분석", "🤖 Gemini 분석", "🔍 추천 스캐너", "⭐ 관심종목 관리"])



# ══════════════════════════════════════════
# 탭 0: 시장 지수
# ══════════════════════════════════════════
with tab0:
    st.markdown("### 🌏 시장 지수 & 투자자 동향")

    @st.cache_data(ttl=60, show_spinner=False)
    def fetch_index_data():
        import yfinance as yf
        indices = {
            "코스피": "^KS11",
            "코스닥": "^KQ11",
            "코스피200선물": "KSF24.KS",
            "S&P500": "^GSPC",
            "나스닥": "^IXIC",
            "달러/원": "KRW=X",
            "공포탐욕(VIX)": "^VIX",
        }
        result = {}
        for name, symbol in indices.items():
            try:
                t = yf.Ticker(symbol)
                hist = t.history(period="5d", interval="1d")
                if hist.empty: continue
                cur  = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2] if len(hist)>=2 else cur
                chg  = (cur/prev-1)*100
                result[name] = {'현재': cur, '등락': chg, '심볼': symbol}
            except:
                continue
        return result

    @st.cache_data(ttl=60, show_spinner=False)
    def fetch_investor_data():
        """외인/기관/개인 투자자 동향 — pykrx"""
        try:
            from pykrx import stock
            today = datetime.today().strftime('%Y%m%d')
            start = (datetime.today() - timedelta(days=10)).strftime('%Y%m%d')
            # 코스피 투자자별 거래대금
            df = stock.get_market_trading_value_by_date(start, today, "KOSPI")
            if df.empty:
                return None
            df.index = pd.to_datetime(df.index)
            return df.tail(5)
        except:
            return None

    with st.spinner("지수 데이터 로딩 중..."):
        idx_data    = fetch_index_data()
        inv_data    = fetch_investor_data()

    # ── 지수 카드 ──
    st.markdown("#### 📈 주요 지수")
    if idx_data:
        # 1행: 국내
        domestic = ["코스피","코스닥","코스피200선물"]
        cols_d = st.columns(3)
        for i, name in enumerate(domestic):
            if name in idx_data:
                d = idx_data[name]
                chg_c = '#ff4d6d' if d['등락']>0 else '#4da6ff'
                arrow = '▲' if d['등락']>0 else '▼'
                # 지수/환율 포맷
                if name == "달러/원":
                    val_str = f"{d['현재']:,.2f}"
                elif name in ["공포탐욕(VIX)"]:
                    val_str = f"{d['현재']:.2f}"
                else:
                    val_str = f"{d['현재']:,.2f}"
                cols_d[i].markdown(
                    f"<div class='metric-card'>"
                    f"<div class='label'>{name}</div>"
                    f"<div class='value flat' style='font-size:20px'>{val_str}</div>"
                    f"<div style='color:{chg_c}; font-size:14px; font-family:IBM Plex Mono; margin-top:4px'>"
                    f"{arrow} {abs(d['등락']):.2f}%</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )

        # 2행: 해외 + 매크로
        global_names = ["S&P500","나스닥","달러/원","공포탐욕(VIX)"]
        cols_g = st.columns(4)
        for i, name in enumerate(global_names):
            if name in idx_data:
                d = idx_data[name]
                chg_c = '#ff4d6d' if d['등락']>0 else '#4da6ff'
                # VIX는 오를수록 위험 — 색상 반전
                if name == "공포탐욕(VIX)":
                    chg_c = '#ff4d6d' if d['등락']>0 else '#4dff91'
                arrow = '▲' if d['등락']>0 else '▼'
                val_str = f"{d['현재']:,.2f}"
                cols_g[i].markdown(
                    f"<div class='metric-card'>"
                    f"<div class='label'>{name}</div>"
                    f"<div class='value flat' style='font-size:20px'>{val_str}</div>"
                    f"<div style='color:{chg_c}; font-size:14px; font-family:IBM Plex Mono; margin-top:4px'>"
                    f"{arrow} {abs(d['등락']):.2f}%</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )
    else:
        st.warning("지수 데이터 로딩 실패")

    st.markdown("---")

    # ── 투자자 동향 ──
    st.markdown("#### 👥 코스피 투자자별 순매수 (최근 5거래일)")
    if inv_data is not None and not inv_data.empty:
        try:
            # 컬럼 정리
            inv_show = inv_data.copy()
            col_map = {}
            for c in inv_show.columns:
                if '외국인' in c or '외인' in c: col_map[c] = '외국인'
                elif '기관' in c and '합계' in c: col_map[c] = '기관합계'
                elif '개인' in c: col_map[c] = '개인'
                elif '기관' in c and '계' not in c: col_map[c] = c
            inv_show = inv_show.rename(columns=col_map)
            keep_cols = [c for c in ['외국인','기관합계','개인'] if c in inv_show.columns]
            if keep_cols:
                inv_show = inv_show[keep_cols]
                inv_show.index = inv_show.index.strftime('%m/%d')

                # 차트
                fig_inv = go.Figure()
                colors_inv = {'외국인':'#4da6ff','기관합계':'#ffd166','개인':'#ff4d6d'}
                for col in inv_show.columns:
                    c = colors_inv.get(col, '#a78bfa')
                    fig_inv.add_trace(go.Bar(
                        name=col,
                        x=inv_show.index,
                        y=inv_show[col]/1e8,  # 억원 단위
                        marker_color=c,
                        opacity=0.85
                    ))
                fig_inv.update_layout(
                    barmode='group',
                    paper_bgcolor='#0a0e1a',
                    plot_bgcolor='#0f1726',
                    font=dict(color='#8899bb', size=11),
                    height=300,
                    margin=dict(l=10,r=10,t=20,b=10),
                    legend=dict(orientation='h', y=1.1),
                    yaxis=dict(title='억원', gridcolor='#1a2535'),
                    xaxis=dict(gridcolor='#1a2535'),
                )
                fig_inv.add_hline(y=0, line_color='#475569', line_width=0.8)
                st.plotly_chart(fig_inv, use_container_width=True)

                # 수치 테이블
                st.markdown("**수치 (억원)**")
                inv_disp = (inv_show/1e8).round(0).astype(int)
                st.dataframe(
                    inv_disp.style.applymap(
                        lambda v: 'color: #ff4d6d' if v > 0 else 'color: #4da6ff'
                    ),
                    use_container_width=True
                )
        except Exception as e:
            st.warning(f"투자자 데이터 표시 오류: {e}")
    else:
        st.info("💡 투자자 순매수 데이터는 장 마감 후 업데이트됩니다.")

    # ── 코스피 지수 차트 ──
    st.markdown("---")
    st.markdown("#### 📊 코스피 최근 60일 차트")
    try:
        import yfinance as yf
        kospi_hist = yf.Ticker("^KS11").history(period="3mo", interval="1d")
        if not kospi_hist.empty:
            fig_k = go.Figure()
            colors_k = ['#ff4d6d' if kospi_hist['Close'].iloc[i] >= kospi_hist['Open'].iloc[i]
                        else '#4da6ff' for i in range(len(kospi_hist))]
            fig_k.add_trace(go.Candlestick(
                x=kospi_hist.index,
                open=kospi_hist['Open'], high=kospi_hist['High'],
                low=kospi_hist['Low'], close=kospi_hist['Close'],
                increasing_line_color='#ff4d6d', decreasing_line_color='#4da6ff',
                increasing_fillcolor='#ff4d6d', decreasing_fillcolor='#4da6ff',
                name='코스피', showlegend=False
            ))
            # 20일 이평선
            ma20 = kospi_hist['Close'].rolling(20).mean()
            fig_k.add_trace(go.Scatter(
                x=kospi_hist.index, y=ma20,
                line=dict(color='#06d6a0', width=1.5), name='MA20'
            ))
            fig_k.update_layout(
                paper_bgcolor='#0a0e1a', plot_bgcolor='#0f1726',
                font=dict(color='#8899bb', size=11),
                xaxis_rangeslider_visible=False,
                height=350,
                margin=dict(l=10,r=10,t=20,b=10),
                xaxis=dict(gridcolor='#1a2535'),
                yaxis=dict(gridcolor='#1a2535'),
            )
            st.plotly_chart(fig_k, use_container_width=True)
    except Exception as e:
        st.warning(f"코스피 차트 오류: {e}")

# ══════════════════════════════════════════
# 탭 1: 현황판
# ══════════════════════════════════════════
with tab1:
    st.markdown("### 관심 종목 현황")

    all_data = {}
    is_mobile = st.toggle("📱 모바일 뷰", value=False)
    if is_mobile:
        cols_header = st.columns([2, 1.5, 1, 2])
        headers = ['종목', '현재가/등락', 'RSI', '신호']
    else:
        cols_header = st.columns([2, 1.2, 1, 0.8, 1, 1, 1, 2.5])
        headers = ['종목', '현재가', '등락', 'RSI', 'MA5', 'MA20', '거래량비율', '신호']
    for col, h in zip(cols_header, headers):
        col.markdown(f"<div style='font-size:10px; color:#475569; text-transform:uppercase; letter-spacing:1px'>{h}</div>", unsafe_allow_html=True)
    st.markdown("<hr style='margin:6px 0; border-color:#1a2535'>", unsafe_allow_html=True)

    total = len(TICKERS)
    prog_bar = st.progress(0, text="데이터 로딩 중...")
    for idx, (ticker, name) in enumerate(TICKERS):
        prog_bar.progress((idx)/max(total,1), text=f"📡 {name} 수집 중... ({idx+1}/{total})")
        df = fetch_ohlcv(ticker, lookback)
        if df is None or len(df) < 20:
            st.warning(f"⚠️ {name} 데이터 없음 (장 마감 또는 오류)")
            continue
        df = calc_indicators(df)
        all_data[ticker] = {'name': name, 'df': df}

        l = df.iloc[-1]; p = df.iloc[-2]
        chg  = (l['종가']/p['종가']-1)*100
        volr = l['거래량']/df['거래량'].tail(20).mean()*100
        sigs = get_signal(df)
        chg_color = 'up' if chg > 0 else 'down' if chg < 0 else 'flat'

        rsi_color = '#ff4d6d' if l['RSI']>=70 else '#4da6ff' if l['RSI']<=30 else '#a0b0c8'
        vol_color = '#ff4d6d' if volr >= 200 else '#8899bb'
        badge_html = ''
        for sig_text, sig_type in sigs:
            badge_html += f'<span class="badge badge-{sig_type}">{sig_text}</span>'

        if is_mobile:
            cols = st.columns([2, 1.5, 1, 2])
            cols[0].markdown(
                f"<b style='font-size:13px'>{name}</b><br>"
                f"<span style='font-size:10px; color:#475569'>{ticker}</span>",
                unsafe_allow_html=True)
            cols[1].markdown(
                f"<span style='font-family:IBM Plex Mono; font-size:14px; font-weight:700'>{l['종가']:,.0f}</span><br>"
                f"<span class='{chg_color}' style='font-size:12px'>{chg:+.2f}%</span>",
                unsafe_allow_html=True)
            cols[2].markdown(
                f"<span style='color:{rsi_color}; font-family:IBM Plex Mono; font-size:15px; font-weight:700'>{l['RSI']:.1f}</span>",
                unsafe_allow_html=True)
            cols[3].markdown(badge_html, unsafe_allow_html=True)
        else:
            cols = st.columns([2, 1.2, 1, 0.8, 1, 1, 1, 2.5])
            cols[0].markdown(f"<b style='font-size:13px'>{name}</b><br><span style='font-size:10px; color:#475569; font-family:IBM Plex Mono'>{ticker}</span>", unsafe_allow_html=True)
            cols[1].markdown(f"<span style='font-family:IBM Plex Mono; font-size:13px; font-weight:600'>{l['종가']:,.0f}</span>", unsafe_allow_html=True)
            cols[2].markdown(f"<span class='{chg_color}' style='font-family:IBM Plex Mono; font-size:13px'>{chg:+.2f}%</span>", unsafe_allow_html=True)
            cols[3].markdown(f"<span style='color:{rsi_color}; font-family:IBM Plex Mono; font-size:13px'>{l['RSI']:.1f}</span>", unsafe_allow_html=True)
            cols[4].markdown(f"<span style='font-family:IBM Plex Mono; font-size:12px; color:#8899bb'>{l['MA5']:,.0f}</span>", unsafe_allow_html=True)
            cols[5].markdown(f"<span style='font-family:IBM Plex Mono; font-size:12px; color:#8899bb'>{l['MA20']:,.0f}</span>", unsafe_allow_html=True)
            cols[6].markdown(f"<span style='color:{vol_color}; font-family:IBM Plex Mono; font-size:12px'>{volr:.0f}%</span>", unsafe_allow_html=True)
            cols[7].markdown(badge_html, unsafe_allow_html=True)

        st.markdown("<hr style='margin:4px 0; border-color:#0f1726'>", unsafe_allow_html=True)

    prog_bar.progress(1.0, text="✅ 로딩 완료!")
    prog_bar.empty()

    # 요약 통계
    if all_data:
        st.markdown("### 📊 요약 통계")
        c1, c2, c3, c4 = st.columns(4)
        buy_cnt  = sum(1 for t,_ in TICKERS if t in all_data and
                       any(s[1]=='buy' for s in get_signal(all_data[t]['df'])))
        sell_cnt = sum(1 for t,_ in TICKERS if t in all_data and
                       any(s[1]=='sell' for s in get_signal(all_data[t]['df'])))
        oversold = sum(1 for t,_ in TICKERS if t in all_data and
                       all_data[t]['df'].iloc[-1]['RSI'] <= 35)
        overbought = sum(1 for t,_ in TICKERS if t in all_data and
                         all_data[t]['df'].iloc[-1]['RSI'] >= 65)

        c1.markdown(f"<div class='metric-card'><div class='label'>매수신호</div><div class='value up'>{buy_cnt}종목</div></div>", unsafe_allow_html=True)
        c2.markdown(f"<div class='metric-card'><div class='label'>매도신호</div><div class='value down'>{sell_cnt}종목</div></div>", unsafe_allow_html=True)
        c3.markdown(f"<div class='metric-card'><div class='label'>과매도(RSI≤35)</div><div class='value' style='color:#4da6ff'>{oversold}종목</div></div>", unsafe_allow_html=True)
        c4.markdown(f"<div class='metric-card'><div class='label'>과매수(RSI≥65)</div><div class='value' style='color:#ff4d6d'>{overbought}종목</div></div>", unsafe_allow_html=True)


# ══════════════════════════════════════════
# 탭 2: 차트 분석
# ══════════════════════════════════════════
with tab2:
    if not all_data:
        st.info("현황판 탭을 먼저 열어서 데이터를 로드해주세요.")
    else:
        selected = st.selectbox("종목 선택", [f"{name} ({ticker})" for ticker, name in TICKERS if ticker in all_data])
        sel_ticker = selected.split('(')[-1].replace(')', '').strip()
        sel_name   = all_data[sel_ticker]['name']
        sel_df     = all_data[sel_ticker]['df']

        # 핵심 지표 카드
        l = sel_df.iloc[-1]; p = sel_df.iloc[-2]
        chg = (l['종가']/p['종가']-1)*100
        bb_r = l['BB_upper']-l['BB_lower']
        bb_p = round((l['종가']-l['BB_lower'])/bb_r*100,1) if bb_r>0 else 50
        w52_pos = round((l['종가']-l['52W_low'])/(l['52W_high']-l['52W_low'])*100,1) if (l['52W_high']-l['52W_low'])>0 else 50

        m1,m2,m3,m4,m5,m6 = st.columns(6)
        chg_color = 'up' if chg>0 else 'down'
        m1.markdown(f"<div class='metric-card'><div class='label'>현재가</div><div class='value flat'>{l['종가']:,.0f}</div></div>", unsafe_allow_html=True)
        m2.markdown(f"<div class='metric-card'><div class='label'>등락</div><div class='value {chg_color}'>{chg:+.2f}%</div></div>", unsafe_allow_html=True)
        rsi_c = 'up' if l['RSI']>=70 else 'down' if l['RSI']<=30 else 'flat'
        m3.markdown(f"<div class='metric-card'><div class='label'>RSI(14)</div><div class='value {rsi_c}'>{l['RSI']:.1f}</div></div>", unsafe_allow_html=True)
        m4.markdown(f"<div class='metric-card'><div class='label'>BB 위치</div><div class='value flat'>{bb_p}%</div></div>", unsafe_allow_html=True)
        m5.markdown(f"<div class='metric-card'><div class='label'>52주 위치</div><div class='value flat'>{w52_pos}%</div></div>", unsafe_allow_html=True)
        vol_c = 'up' if l['거래량_비율']>=200 else 'flat'
        m6.markdown(f"<div class='metric-card'><div class='label'>거래량비율</div><div class='value {vol_c}'>{l['거래량_비율']:.0f}%</div></div>", unsafe_allow_html=True)

        # ── 매수/손절/목표가 입력 ──
        st.markdown("### 🎯 매수 전략 라인")
        lc1, lc2, lc3, lc4 = st.columns(4)
        entry_price   = lc1.number_input("매수가 (원)", value=0, step=100,
                                          help="입력하면 차트에 황금색 선으로 표시")
        stop_price    = lc2.number_input("손절가 (원, -7% 자동계산)",
                                          value=int(l['종가']*0.93) if entry_price==0 else int(entry_price*0.93),
                                          step=100)
        target1_price = lc3.number_input("1차 목표가 (원)", value=0, step=100)
        target2_price = lc4.number_input("2차 목표가 (원)", value=0, step=100)

        # R:R 자동 계산
        if entry_price > 0 and stop_price > 0 and target1_price > 0:
            risk   = entry_price - stop_price
            reward = target1_price - entry_price
            rr     = round(reward/risk, 2) if risk > 0 else 0
            rr_color = '#4dff91' if rr >= 2.0 else '#ff4d6d'
            rr_text  = '✅ 진입 가능' if rr >= 2.0 else '❌ R:R 부족 (2.0 미만 기각)'
            st.markdown(
                f"<div class='metric-card' style='display:inline-block; padding:10px 20px'>"
                f"<span style='color:#6b7fa3; font-size:11px'>R:R 비율</span> "
                f"<span style='color:{rr_color}; font-size:20px; font-weight:700; font-family:IBM Plex Mono'> {rr}</span> "
                f"<span style='color:{rr_color}; font-size:13px'>{rr_text}</span>"
                f"</div>",
                unsafe_allow_html=True
            )

        # 차트
        fig = make_chart(
            sel_df, sel_name,
            entry    = entry_price   if entry_price   > 0 else None,
            stoploss = stop_price    if stop_price     > 0 else None,
            target1  = target1_price if target1_price  > 0 else None,
            target2  = target2_price if target2_price  > 0 else None,
        )
        st.plotly_chart(fig, use_container_width=True)

        # 이평선 상태 테이블
        st.markdown("### 📐 이평선 현황")
        ma_cols = st.columns(4)
        for i, (ma, label) in enumerate([('MA5','5일'),('MA20','20일'),('MA60','60일'),('MA120','120일')]):
            val = l[ma]
            diff = round((l['종가']/val-1)*100, 2) if val > 0 else 0
            status = '위' if l['종가'] > val else '아래'
            c = 'up' if l['종가'] > val else 'down'
            ma_cols[i].markdown(
                f"<div class='metric-card'><div class='label'>{label}선</div>"
                f"<div class='value flat' style='font-size:16px'>{val:,.0f}</div>"
                f"<div style='font-size:12px; margin-top:4px' class='{c}'>현재가 {status} ({diff:+.1f}%)</div></div>",
                unsafe_allow_html=True)


# ══════════════════════════════════════════
# 탭 3: Gemini 분석
# ══════════════════════════════════════════
with tab3:
    if not gemini_key:
        st.warning("👈 사이드바에 Gemini API 키를 입력해주세요.")
    elif not all_data:
        st.info("현황판 탭을 먼저 열어서 데이터를 로드해주세요.")
    else:
        analyze_all = st.button("🤖 전체 종목 Gemini 분석 시작", use_container_width=True)
        st.markdown("또는 종목별로 개별 분석:")

        for ticker, name in TICKERS:
            if ticker not in all_data:
                continue
            with st.expander(f"📊 {name} ({ticker}) 분석", expanded=False):
                btn = st.button(f"{name} 분석", key=f"btn_{ticker}")
                if btn or analyze_all:
                    import google.generativeai as genai
                    genai.configure(api_key=gemini_key)
                    model = genai.GenerativeModel(model_name)
                    SYSTEM = (
                        'You are a Korean stock quantitative analysis AI. '
                        'Always respond in Korean. '
                        'Rules: Reject R:R below 2.0 / Stop-loss -7% / '
                        'No entry 09:00-09:30 KST / No averaging down'
                    )
                    prompt = build_prompt(all_data[ticker]['df'], name, ticker)
                    with st.spinner(f'{name} 분석 중...'):
                        try:
                            res = model.generate_content(SYSTEM + '\n\n' + prompt)
                            st.markdown(f"<div class='gemini-box'>{res.text}</div>",
                                        unsafe_allow_html=True)
                        except Exception as e:
                            st.error(f"오류: {e}")


# ══════════════════════════════════════════
# 탭 4: 추천 스캐너
# ══════════════════════════════════════════
with tab4:
    st.markdown("### 🔍 주도 종목 자동 스캐너")
    st.markdown("<div style='font-size:13px; color:#6b7fa3; margin-bottom:16px'>거래대금 상위 종목 중 조건 충족 종목을 자동 발굴합니다.</div>", unsafe_allow_html=True)

    # 스캔 조건 설정
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("**📋 스캔 대상**")
        market_type = st.selectbox("시장", ["KOSPI", "KOSDAQ", "KOSPI+KOSDAQ"])
        top_n = st.slider("거래대금 상위 N종목", 20, 200, 50)
    with col_b:
        st.markdown("**🎯 필터 조건**")
        use_rsi     = st.checkbox("RSI 과매도 (≤35)", value=True)
        use_vol     = st.checkbox("거래량 폭발 (≥150%)", value=True)
        use_macd    = st.checkbox("MACD 골든크로스", value=False)
        use_bb      = st.checkbox("BB 하단 근접 (≤25%)", value=False)
        use_align   = st.checkbox("정배열 (MA5>MA20>MA60)", value=False)
    with col_c:
        st.markdown("**⚙️ 추가 설정**")
        min_price   = st.number_input("최소 주가 (원)", value=5000, step=1000)
        max_price   = st.number_input("최대 주가 (원)", value=2000000, step=10000)
        use_gemini_scan = st.checkbox("Gemini 최종 분석 포함", value=False)

    scan_btn = st.button("🚀 스캔 시작", use_container_width=True)

    if scan_btn:
        # 스캐너: 사용자가 직접 입력한 종목 + 기본 주요 종목 스캔
        # (Streamlit Cloud 환경에서는 pykrx 전체 종목 스캔 불가)
        DEFAULT_SCAN = [
            ("005930","삼성전자"),("000660","SK하이닉스"),("042700","한미반도체"),
            ("035420","NAVER"),("035720","카카오"),("012450","한화에어로스페이스"),
            ("329180","HD현대중공업"),("015760","한국전력"),("034730","SK"),
            ("051910","LG화학"),("028260","삼성물산"),("003670","포스코퓨처엠"),
            ("247540","에코프로비엠"),("086520","에코프로"),("006400","삼성SDI"),
            ("207940","삼성바이오로직스"),("068270","셀트리온"),("096770","SK이노베이션"),
            ("011200","HMM"),("010130","고려아연"),("000270","기아"),("005380","현대차"),
            ("066570","LG전자"),("316140","우리금융지주"),("055550","신한지주"),
            ("105560","KB금융"),("032830","삼성생명"),("017670","SK텔레콤"),
            ("030200","KT"),("018260","삼성에스디에스"),
        ]
        # 사용자 관심종목도 스캔에 포함
        extra = [(t,n) for t,n in TICKERS if (t,n) not in DEFAULT_SCAN]
        scan_list = DEFAULT_SCAN + extra
        # 시장 필터
        if market_type == "KOSPI":
            scan_list = scan_list[:20]
        elif market_type == "KOSDAQ":
            scan_list = [("042700","한미반도체"),("086520","에코프로"),
                         ("247540","에코프로비엠"),("003670","포스코퓨처엠")] + extra
        scan_list = scan_list[:top_n]
        scan_tickers = [t for t,n in scan_list]
        name_map = {t:n for t,n in scan_list}

        with st.spinner(f'종목 스캔 준비 중... ({len(scan_tickers)}종목)'):
            st.info(f"📋 스캔 대상: {len(scan_tickers)}종목")

        if scan_tickers:
            pass  # 아래에서 처리

            passed = []
            progress = st.progress(0)
            status_text = st.empty()

            for idx, ticker in enumerate(scan_tickers):
                progress.progress((idx+1)/len(scan_tickers))
                name = name_map.get(ticker, ticker)
                status_text.markdown(f"<span style='font-size:12px; color:#6b7fa3'>분석 중: {name} ({idx+1}/{len(scan_tickers)})</span>", unsafe_allow_html=True)

                try:
                    df = fetch_ohlcv(ticker, 60)
                    if df is None or len(df) < 20: continue

                    l = df.iloc[-1]
                    # 가격 필터
                    if l['종가'] < min_price or l['종가'] > max_price: continue

                    df = calc_indicators(df)
                    l  = df.iloc[-1]
                    p  = df.iloc[-2]

                    volr = l['거래량']/df['거래량'].tail(20).mean()*100
                    bb_r = l['BB_upper']-l['BB_lower']
                    bb_p = round((l['종가']-l['BB_lower'])/bb_r*100,1) if bb_r>0 else 50

                    score = 0
                    reasons = []

                    if use_rsi and l['RSI'] <= 35:
                        score += 2; reasons.append(f"📉RSI {l['RSI']:.1f}")
                    if use_vol and volr >= 150:
                        score += 2; reasons.append(f"🔥거래량{volr:.0f}%")
                    if use_macd and l['MACD'] > l['Signal'] and p['MACD'] <= p['Signal']:
                        score += 3; reasons.append("⚡골든크로스")
                    if use_bb and bb_p <= 25:
                        score += 2; reasons.append(f"📊BB하단{bb_p}%")
                    if use_align and l['종가'] > l['MA5'] > l['MA20'] > l['MA60']:
                        score += 2; reasons.append("✅정배열")

                    if score > 0:
                        chg = (l['종가']/p['종가']-1)*100
                        passed.append({
                            'ticker': ticker,
                            'name': name,
                            '현재가': l['종가'],
                            '등락(%)': chg,
                            'RSI': l['RSI'],
                            '거래량비율': volr,
                            'BB위치': bb_p,
                            'score': score,
                            'reasons': reasons,
                            'df': df
                        })
                except:
                    continue

            progress.empty()
            status_text.empty()

            # 점수순 정렬
            passed = sorted(passed, key=lambda x: x['score'], reverse=True)
            # session_state에 저장 (df 제외)
            st.session_state.passed = [{k:v for k,v in p.items() if k != 'df'} for p in passed]

            if not passed:
                st.warning("⚠️ 조건을 충족하는 종목이 없습니다. 조건을 완화해보세요.")
            else:
                st.success(f"✅ {len(passed)}개 종목 발굴 완료!")
                st.markdown("---")

                # 결과 출력
                for item in passed:
                    chg_color = '#ff4d6d' if item['등락(%)'] > 0 else '#4da6ff'
                    badge_html = ' '.join([f"<span class='badge badge-buy'>{r}</span>" for r in item['reasons']])

                    with st.expander(
                        f"⭐{'★'*min(item['score'],5)}  {item['name']} ({item['ticker']})  "
                        f"{'▲' if item['등락(%)']>0 else '▼'} {abs(item['등락(%)']):+.2f}%",
                        expanded=(item == passed[0])
                    ):
                        c1, c2, c3, c4, c5 = st.columns(5)
                        c1.markdown(f"<div class='metric-card'><div class='label'>현재가</div><div class='value flat'>{item['현재가']:,.0f}</div></div>", unsafe_allow_html=True)
                        c2.markdown(f"<div class='metric-card'><div class='label'>등락</div><div class='value' style='color:{chg_color}'>{item['등락(%)']:+.2f}%</div></div>", unsafe_allow_html=True)
                        rsi_c = '#4da6ff' if item['RSI']<=35 else '#ff4d6d' if item['RSI']>=70 else '#a0b0c8'
                        c3.markdown(f"<div class='metric-card'><div class='label'>RSI</div><div class='value' style='color:{rsi_c}'>{item['RSI']:.1f}</div></div>", unsafe_allow_html=True)
                        c4.markdown(f"<div class='metric-card'><div class='label'>거래량비율</div><div class='value flat'>{item['거래량비율']:.0f}%</div></div>", unsafe_allow_html=True)
                        c5.markdown(f"<div class='metric-card'><div class='label'>선정 점수</div><div class='value up'>{item['score']}점</div></div>", unsafe_allow_html=True)

                        st.markdown(f"**선정 이유:** {badge_html}", unsafe_allow_html=True)

                        # 미니 차트
                        mini_df = item['df']
                        fig = make_chart(mini_df, item['name'])
                        st.plotly_chart(fig, use_container_width=True)

                        # Gemini 분석
                        if use_gemini_scan and gemini_key:
                            if st.button(f"🤖 {item['name']} Gemini 분석", key=f"scan_gem_{item['ticker']}"):
                                import google.generativeai as genai
                                genai.configure(api_key=gemini_key)
                                gmodel = genai.GenerativeModel(model_name)
                                SYSTEM = (
                                    'You are a Korean stock quantitative analysis AI. '
                                    'Always respond in Korean. '
                                    'Rules: Reject R:R below 2.0 / Stop-loss -7% / '
                                    'No entry 09:00-09:30 KST / No averaging down'
                                )
                                prompt = build_prompt(item['df'], item['name'], item['ticker'])
                                with st.spinner('분석 중...'):
                                    try:
                                        res = gmodel.generate_content(SYSTEM + '\n\n' + prompt)
                                        st.markdown(f"<div class='gemini-box'>{res.text}</div>", unsafe_allow_html=True)
                                    except Exception as e:
                                        st.error(f"오류: {e}")

                        # ── 관심종목 추가 (파일 기반) ──
                        wl_cur = load_watchlist()
                        cur_tickers = [l.split(',')[0].strip() for l in wl_cur.split('\n') if ',' in l]
                        if item['ticker'] in cur_tickers:
                            st.markdown("<div style='color:#4dff91; font-size:13px'>✅ 관심종목 관리 탭에서 확인</div>", unsafe_allow_html=True)
                        else:
                            if st.button(f"⭐ 관심종목 추가", key=f"tab4_add_{item['ticker']}", use_container_width=True):
                                save_watchlist(wl_cur.strip() + f"\n{item['ticker']},{item['name']}")
                                st.success(f"✅ {item['name']} 추가! 관심종목 관리 탭에서 확인하세요.")
                                st.rerun()

# ══════════════════════════════════════════
# 탭 5: 관심종목 관리
# ══════════════════════════════════════════
with tab5:
    st.markdown("### ⭐ 관심종목 관리")
    st.caption("추가/삭제 후 현황판 탭으로 이동하면 즉시 반영됩니다.")

    # ── 연결 상태 디버그 ──
    with st.expander("🔧 연결 상태 확인", expanded=True):
        try:
            _sheet_id = st.secrets["SHEET_ID"]
            st.success(f"✅ SHEET_ID 확인: {_sheet_id[:20]}...")
        except Exception as e:
            st.error(f"❌ SHEET_ID 없음: {e}")

        try:
            _sa = st.secrets["gcp_service_account"]
            st.success(f"✅ 서비스 계정 확인: {_sa['client_email']}")
        except Exception as e:
            st.error(f"❌ 서비스 계정 없음: {e}")

        try:
            _ws = get_gsheet()
            st.success("✅ Google Sheets 연결 성공!")
            _test = _ws.get_all_values()
            st.write(f"현재 데이터: {_test}")
        except Exception as e:
            st.error(f"❌ Sheets 연결 오류 상세: {e}")
            import traceback
            st.code(traceback.format_exc())

    # 항상 최신 데이터 로드
    _wl    = load_watchlist()
    _lines = [l.strip() for l in _wl.split("\n") if "," in l.strip()]
    _pairs = []
    for _l in _lines:
        _p = _l.split(",", 1)
        if len(_p) == 2:
            _pairs.append((_p[0].strip(), _p[1].strip()))
    _tids = [t for t, n in _pairs]

    # ── 현재 종목 목록 + 삭제 콜백 ──
    def _do_delete(tk):
        remove_ticker(tk)

    st.markdown(f"#### 📋 현재 종목 ({len(_pairs)}개)")
    for _tk, _nm in _pairs:
        _ca, _cb = st.columns([5, 1])
        _ca.markdown(
            f"<div style='padding:10px; background:#111827; border-radius:8px;"
            f"border:1px solid #1e3a5f; margin-bottom:6px'>"
            f"<b>{_nm}</b>&nbsp;&nbsp;"
            f"<code style='color:#475569;font-size:11px'>{_tk}</code></div>",
            unsafe_allow_html=True
        )
        _cb.button(
            "삭제", key=f"D_{_tk}",
            on_click=_do_delete, args=(_tk,)
        )

    st.divider()

    # ── 직접 추가 ──
    st.markdown("#### ➕ 직접 추가")

    with st.form("add_ticker_form", clear_on_submit=True):
        _fc, _fn = st.columns(2)
        _f_code = _fc.text_input("종목코드", placeholder="005930")
        _f_name = _fn.text_input("종목명",   placeholder="삼성전자")
        _submitted = st.form_submit_button("✅ 추가", use_container_width=True)
        if _submitted:
            st.write(f"DEBUG: 코드={_f_code}, 이름={_f_name}")
            if _f_code and _f_name:
                _cur_wl = load_watchlist()
                _cur_ids = [l.split(",")[0].strip() for l in _cur_wl.split("\n") if "," in l]
                st.write(f"DEBUG: 현재종목={_cur_ids}")
                if _f_code.strip() not in _cur_ids:
                    try:
                        _new_wl = _cur_wl.strip() + f"\n{_f_code.strip()},{_f_name.strip()}"
                        st.write(f"DEBUG: 저장시도={_new_wl}")
                        save_watchlist(_new_wl)
                        st.write("DEBUG: 저장완료")
                        st.session_state.watchlist_data = _new_wl
                    except Exception as _e:
                        st.error(f"저장 오류: {_e}")
                else:
                    st.warning("이미 등록된 종목입니다.")
            else:
                st.warning("종목코드와 종목명을 모두 입력해주세요.")

    # form 밖 rerun
    if 'watchlist_data' in st.session_state:
        _chk = [l.split(",")[0].strip() for l in st.session_state.watchlist_data.split("\n") if "," in l]
        if set(_chk) != set(_tids):
            st.rerun()

    st.divider()

    # ── 스캐너 추천 종목 — 콜백 방식 ──
    st.markdown("#### 🔍 스캐너 추천 종목")

    def _do_add(tk, nm):
        add_ticker(tk, nm)

    if st.session_state.passed:
        for _item in st.session_state.passed:
            _tk2  = _item["ticker"]
            _nm2  = _item["name"]
            _chg  = _item["등락(%)"]
            _cc   = "#ff4d6d" if _chg > 0 else "#4da6ff"
            _done = _tk2 in _tids
            _ra, _rb = st.columns([5, 1])
            _ra.markdown(
                f"<div style='padding:10px;"
                f"background:{'#0a1a0a' if _done else '#111827'};"
                f"border-radius:8px;"
                f"border:1px solid {'#2d6644' if _done else '#1e3a5f'};"
                f"margin-bottom:6px'>"
                f"<b>{_nm2}</b>&nbsp;"
                f"<code style='color:#475569;font-size:11px'>{_tk2}</code>&nbsp;"
                f"<span style='color:{_cc}'>{_chg:+.2f}%</span>"
                f"{'&nbsp;✅' if _done else ''}</div>",
                unsafe_allow_html=True
            )
            if not _done:
                _rb.button(
                    "추가", key=f"A_{_tk2}",
                    on_click=_do_add, args=(_tk2, _nm2)
                )
    else:
        st.info("💡 추천 스캐너 탭에서 먼저 스캔을 실행해주세요.")

st.markdown("---")
st.markdown("<div style='text-align:center; font-size:11px; color:#2d3a55; font-family:IBM Plex Mono'>퀀트 관제탑 V8.9 | 투자 자문 아님 — 모든 손익의 책임은 본인에게 있습니다</div>", unsafe_allow_html=True)
