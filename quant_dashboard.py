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
    initial_sidebar_state="expanded"
)

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
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════
# 데이터 함수
# ══════════════════════════════════════════

@st.cache_data(ttl=300)
def fetch_ohlcv(ticker, lookback=80):
    from pykrx import stock
    end   = datetime.today().strftime('%Y%m%d')
    start = (datetime.today() - timedelta(days=lookback*2)).strftime('%Y%m%d')
    try:
        df = stock.get_market_ohlcv(start, end, ticker)
        rename = {}
        for c in df.columns:
            if '시가'  in c: rename[c] = '시가'
            elif '고가' in c: rename[c] = '고가'
            elif '저가' in c: rename[c] = '저가'
            elif '종가' in c: rename[c] = '종가'
            elif '거래량' in c: rename[c] = '거래량'
        df = df.rename(columns=rename)[['시가','고가','저가','종가','거래량']]
        df = df[df['거래량'] > 0].tail(lookback)
        return df
    except:
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

def make_chart(df, name):
    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[0.5, 0.15, 0.18, 0.17],
        vertical_spacing=0.02
    )
    # 캔들
    fig.add_trace(go.Candlestick(
        x=df.index, open=df['시가'], high=df['고가'],
        low=df['저가'], close=df['종가'],
        increasing_line_color='#ff4d6d', decreasing_line_color='#4da6ff',
        increasing_fillcolor='#ff4d6d', decreasing_fillcolor='#4da6ff',
        name='캔들', showlegend=False
    ), row=1, col=1)

    # 이평선
    colors = {'MA5':'#ffd166','MA20':'#06d6a0','MA60':'#a78bfa','MA120':'#38bdf8'}
    for ma, c in colors.items():
        if ma in df.columns:
            fig.add_trace(go.Scatter(x=df.index, y=df[ma], line=dict(color=c, width=1.2),
                                     name=ma, opacity=0.85), row=1, col=1)
    # BB
    fig.add_trace(go.Scatter(x=df.index, y=df['BB_upper'], line=dict(color='#475569', width=0.8, dash='dash'),
                             name='BB상단', showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['BB_lower'], line=dict(color='#475569', width=0.8, dash='dash'),
                             fill='tonexty', fillcolor='rgba(71,85,105,0.08)',
                             name='BB하단', showlegend=False), row=1, col=1)

    # 거래량
    colors_vol = ['#ff4d6d' if df['종가'].iloc[i] >= df['시가'].iloc[i] else '#4da6ff'
                  for i in range(len(df))]
    fig.add_trace(go.Bar(x=df.index, y=df['거래량'], marker_color=colors_vol,
                         opacity=0.7, name='거래량', showlegend=False), row=2, col=1)

    # MACD
    hist_colors = ['#ff4d6d' if v >= 0 else '#4da6ff' for v in df['MACD_hist']]
    fig.add_trace(go.Bar(x=df.index, y=df['MACD_hist'], marker_color=hist_colors,
                         opacity=0.6, name='히스토그램', showlegend=False), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['MACD'], line=dict(color='#38bdf8', width=1.2),
                             name='MACD'), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df['Signal'], line=dict(color='#f472b6', width=1.2),
                             name='Signal'), row=3, col=1)

    # RSI
    fig.add_trace(go.Scatter(x=df.index, y=df['RSI'], line=dict(color='#a78bfa', width=1.5),
                             name='RSI', showlegend=False), row=4, col=1)
    fig.add_hline(y=70, line_dash='dash', line_color='#ff4d6d', line_width=0.8, row=4, col=1)
    fig.add_hline(y=30, line_dash='dash', line_color='#4da6ff', line_width=0.8, row=4, col=1)
    fig.add_hrect(y0=70, y1=100, fillcolor='rgba(255,77,109,0.05)', line_width=0, row=4, col=1)
    fig.add_hrect(y0=0,  y1=30,  fillcolor='rgba(77,166,255,0.05)', line_width=0, row=4, col=1)

    fig.update_layout(
        title=dict(text=f'<b>{name}</b>', font=dict(size=16, color='#e0e6f0'), x=0.01),
        paper_bgcolor='#0a0e1a', plot_bgcolor='#0f1726',
        font=dict(color='#8899bb', size=11),
        xaxis_rangeslider_visible=False,
        height=680,
        legend=dict(orientation='h', y=1.02, x=0, font=dict(size=10),
                    bgcolor='rgba(0,0,0,0)', bordercolor='rgba(0,0,0,0)'),
        margin=dict(l=10, r=10, t=50, b=10),
    )
    for i in range(1, 5):
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
    default_tickers = "042700,한미반도체\n005930,삼성전자\n000660,SK하이닉스\n012450,한화에어로스페이스\n329180,HD현대중공업"
    ticker_input = st.text_area("종목코드,종목명 (한 줄에 하나)", value=default_tickers, height=160)

    lookback = st.slider("분석 기간 (거래일)", 30, 120, 60)

    model_name = st.selectbox("Gemini 모델", [
        "models/gemini-2.5-flash",
        "models/gemini-2.5-pro",
        "models/gemini-2.0-flash",
        "models/gemini-3.1-pro-preview",
    ])

    refresh = st.button("🔄 데이터 새로고침", use_container_width=True)
    if refresh:
        st.cache_data.clear()
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

# ── 종목 파싱 ──
TICKERS = []
for line in ticker_input.strip().split('\n'):
    parts = line.strip().split(',')
    if len(parts) == 2:
        TICKERS.append((parts[0].strip(), parts[1].strip()))


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
tab1, tab2, tab3 = st.tabs(["📊 현황판", "📈 차트 분석", "🤖 Gemini 분석"])


# ══════════════════════════════════════════
# 탭 1: 현황판
# ══════════════════════════════════════════
with tab1:
    st.markdown("### 관심 종목 현황")

    all_data = {}
    cols_header = st.columns([2, 1.2, 1, 0.8, 1, 1, 1, 2.5])
    headers = ['종목', '현재가', '등락', 'RSI', 'MA5', 'MA20', '거래량비율', '신호']
    for col, h in zip(cols_header, headers):
        col.markdown(f"<div style='font-size:10px; color:#475569; text-transform:uppercase; letter-spacing:1px'>{h}</div>", unsafe_allow_html=True)
    st.markdown("<hr style='margin:6px 0; border-color:#1a2535'>", unsafe_allow_html=True)

    for ticker, name in TICKERS:
        with st.spinner(f'{name} 로딩...'):
            df = fetch_ohlcv(ticker, lookback)
        if df is None or len(df) < 20:
            st.error(f"❌ {name} 데이터 없음")
            continue
        df = calc_indicators(df)
        all_data[ticker] = {'name': name, 'df': df}

        l = df.iloc[-1]; p = df.iloc[-2]
        chg  = (l['종가']/p['종가']-1)*100
        volr = l['거래량']/df['거래량'].tail(20).mean()*100
        sigs = get_signal(df)
        chg_color = 'up' if chg > 0 else 'down' if chg < 0 else 'flat'

        cols = st.columns([2, 1.2, 1, 0.8, 1, 1, 1, 2.5])
        cols[0].markdown(f"<b style='font-size:13px'>{name}</b><br><span style='font-size:10px; color:#475569; font-family:IBM Plex Mono'>{ticker}</span>", unsafe_allow_html=True)
        cols[1].markdown(f"<span style='font-family:IBM Plex Mono; font-size:13px; font-weight:600'>{l['종가']:,.0f}</span>", unsafe_allow_html=True)
        cols[2].markdown(f"<span class='{chg_color}' style='font-family:IBM Plex Mono; font-size:13px'>{chg:+.2f}%</span>", unsafe_allow_html=True)

        rsi_color = '#ff4d6d' if l['RSI']>=70 else '#4da6ff' if l['RSI']<=30 else '#a0b0c8'
        cols[3].markdown(f"<span style='color:{rsi_color}; font-family:IBM Plex Mono; font-size:13px'>{l['RSI']:.1f}</span>", unsafe_allow_html=True)
        cols[4].markdown(f"<span style='font-family:IBM Plex Mono; font-size:12px; color:#8899bb'>{l['MA5']:,.0f}</span>", unsafe_allow_html=True)
        cols[5].markdown(f"<span style='font-family:IBM Plex Mono; font-size:12px; color:#8899bb'>{l['MA20']:,.0f}</span>", unsafe_allow_html=True)

        vol_color = '#ff4d6d' if volr >= 200 else '#8899bb'
        cols[6].markdown(f"<span style='color:{vol_color}; font-family:IBM Plex Mono; font-size:12px'>{volr:.0f}%</span>", unsafe_allow_html=True)

        badge_html = ''
        for sig_text, sig_type in sigs:
            badge_html += f'<span class="badge badge-{sig_type}">{sig_text}</span>'
        cols[7].markdown(badge_html, unsafe_allow_html=True)

        st.markdown("<hr style='margin:4px 0; border-color:#0f1726'>", unsafe_allow_html=True)

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

        # 차트
        fig = make_chart(sel_df, sel_name)
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

st.markdown("---")
st.markdown("<div style='text-align:center; font-size:11px; color:#2d3a55; font-family:IBM Plex Mono'>퀀트 관제탑 V8.9 | 투자 자문 아님 — 모든 손익의 책임은 본인에게 있습니다</div>", unsafe_allow_html=True)
