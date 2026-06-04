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
import json
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
# ══════════════════════════════════════════
# 페이퍼 트레이딩 백엔드
# ══════════════════════════════════════════

def get_trading_sheet():
    """거래 일지 시트 (Sheet2)"""
    try:
        creds_dict = dict(st.secrets["gcp_service_account"])
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc    = gspread.authorize(creds)
        sh    = gc.open_by_key(st.secrets["SHEET_ID"])
        # Sheet2 없으면 생성
        try:
            ws = sh.worksheet("trading_log")
        except:
            ws = sh.add_worksheet("trading_log", rows=1000, cols=20)
            ws.append_row(["날짜","시간","종목코드","종목명","매매","수량",
                           "체결단가","수수료","슬리피지","순체결가",
                           "잔고","평가금액","5AI점수","ADX","Z-Score","메모"])
        return ws
    except Exception as e:
        return None

def get_account_sheet():
    """가상 계좌 시트 (Sheet3)"""
    try:
        creds_dict = dict(st.secrets["gcp_service_account"])
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive"
        ]
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc    = gspread.authorize(creds)
        sh    = gc.open_by_key(st.secrets["SHEET_ID"])
        try:
            ws = sh.worksheet("account")
        except:
            ws = sh.add_worksheet("account", rows=100, cols=10)
            ws.append_row(["초기자본","현금잔고","보유종목JSON","최고자산","최저자산"])
            ws.append_row([10000000, 10000000, "[]", 10000000, 10000000])
        return ws
    except:
        return None

def load_account():
    """가상 계좌 로드"""
    if 'paper_account' in st.session_state:
        return st.session_state.paper_account
    try:
        ws   = get_account_sheet()
        data = ws.get_all_values()
        if len(data) >= 2:
            row = data[1]
            acc = {
                'initial':    float(row[0]),
                'cash':       float(row[1]),
                'positions':  json.loads(row[2]) if row[2] else [],
                'peak':       float(row[3]),
                'trough':     float(row[4]),
            }
            st.session_state.paper_account = acc
            return acc
    except:
        pass
    default = {'initial':10000000,'cash':10000000,'positions':[],'peak':10000000,'trough':10000000}
    st.session_state.paper_account = default
    return default

def save_account(acc):
    """가상 계좌 저장"""
    st.session_state.paper_account = acc
    try:
        ws = get_account_sheet()
        ws.update("A2:E2", [[
            acc['initial'], acc['cash'],
            json.dumps(acc['positions'], ensure_ascii=False),
            acc['peak'], acc['trough']
        ]])
    except:
        pass

def calc_slippage(price, is_buy, is_korean=True):
    """슬리피지 + 수수료 + 세금 계산"""
    commission = 0.00015   # 증권사 수수료 0.015%
    slippage   = 0.001     # 슬리피지 0.1%
    tax        = 0.0018 if (not is_buy and is_korean) else 0  # 매도세 0.18% (한국)
    total_cost = commission + slippage + tax
    if is_buy:
        return round(price * (1 + total_cost))   # 매수: 단가 올라감
    else:
        return round(price * (1 - total_cost))   # 매도: 단가 내려감

def log_trade(ticker, name, action, qty, price, net_price, cash_after,
              eval_total, ai_score=0, adx=0, zscore=0, memo=""):
    """거래 일지 기록"""
    try:
        from datetime import datetime as _dt
        now  = _dt.now()
        ws   = get_trading_sheet()
        if ws:
            ws.append_row([
                now.strftime('%Y-%m-%d'),
                now.strftime('%H:%M:%S'),
                ticker, name, action, qty,
                price, round(price*0.00015), round(price*0.001),
                net_price, cash_after, eval_total,
                ai_score, adx, zscore, memo
            ])
    except:
        pass

def get_position(acc, ticker):
    """보유 포지션 조회"""
    for p in acc['positions']:
        if p['ticker'] == ticker:
            return p
    return None

def calc_portfolio_value(acc):
    """총 평가금액 계산"""
    total = acc['cash']
    for pos in acc['positions']:
        try:
            df = fetch_ohlcv(pos['ticker'], 5)
            if df is not None and not df.empty:
                cur_price = df['종가'].iloc[-1]
                total += cur_price * pos['qty']
            else:
                total += pos['avg_price'] * pos['qty']
        except:
            total += pos['avg_price'] * pos['qty']
    return total

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

def clean_sheet_duplicates():
    """Sheets 중복 데이터 제거"""
    try:
        ws   = get_gsheet()
        data = ws.get_all_values()
        seen = set()
        clean = []
        for row in data:
            if row and row[0].strip() not in seen:
                seen.add(row[0].strip())
                clean.append(row)
        ws.clear()
        if clean:
            ws.update("A1", clean)
        result = "\n".join([",".join(r) for r in clean if len(r)>=2])
        st.session_state.watchlist_data = result
        load_watchlist.clear()
        return result
    except Exception as e:
        return None

def save_watchlist(text):
    """관심종목 전체 저장 (삭제 시 사용)"""
    st.session_state.watchlist_data = text
    load_watchlist.clear()
    try:
        ws = get_gsheet()
        ws.clear()
        rows = []
        for line in text.strip().split("\n"):
            parts = line.strip().split(",", 1)
            if len(parts) == 2:
                rows.append(parts)
        if rows:
            ws.update("A1", rows)
    except Exception as e:
        st.warning(f"Sheets 저장 오류: {e}")

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
if '_keep_passed' not in st.session_state:
    st.session_state._keep_passed = False
if 'scan_done' not in st.session_state:
    st.session_state.scan_done = False
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
    end   = datetime.today()
    start = end - timedelta(days=lookback*2)

    # 한국 종목 여부 판단 (숫자 6자리)
    is_korean = ticker.isdigit() and len(ticker) == 6

    if is_korean:
        # 한국 종목 — KS, KQ 순으로 시도
        for suffix in ['.KS', '.KQ']:
            try:
                yt = yf.Ticker(ticker + suffix)
                df = yt.history(start=start, end=end, interval='1d')
                if df is None or df.empty:
                    continue
                df = df.rename(columns={
                    'Open':'시가','High':'고가','Low':'저가',
                    'Close':'종가','Volume':'거래량'
                })[['시가','고가','저가','종가','거래량']]
                df = df[df['거래량'] > 0].tail(lookback)
                if len(df) >= 5:
                    return df
            except:
                continue
    else:
        # 미국 종목 — suffix 없이 직접 조회
        try:
            yt = yf.Ticker(ticker)
            df = yt.history(start=start, end=end, interval='1d')
            if df is not None and not df.empty:
                df = df.rename(columns={
                    'Open':'시가','High':'고가','Low':'저가',
                    'Close':'종가','Volume':'거래량'
                })[['시가','고가','저가','종가','거래량']]
                df = df[df['거래량'] > 0].tail(lookback)
                if len(df) >= 5:
                    return df
        except:
            pass
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

    # 사이드바는 항상 Sheets에서 직접 읽기 (캐시 무시)
    try:
        _ws_sb = get_gsheet()
        _raw   = _ws_sb.get_all_values()
        if _raw:
            _sb_wl = "\n".join([",".join(r) for r in _raw if len(r)>=2])
            st.session_state.watchlist_data = _sb_wl
        else:
            _sb_wl = st.session_state.get('watchlist_data') or DEFAULT_WATCHLIST
    except:
        _sb_wl = st.session_state.get('watchlist_data') or DEFAULT_WATCHLIST
    _sb_lines = [l.strip() for l in _sb_wl.split("\n") if "," in l.strip()]
    _sb_pairs = [l.split(",", 1) for l in _sb_lines if len(l.split(",", 1)) == 2]

    for _t, _n in _sb_pairs:
        _t = _t.strip(); _n = _n.strip()
        _sc1, _sc2 = st.columns([3, 1])
        _sc1.markdown(f"<div style='font-size:12px; padding:4px 0'><b>{_n}</b><br><span style='color:#475569; font-size:10px'>{_t}</span></div>", unsafe_allow_html=True)
        if _sc2.button("✕", key=f"sb_del_{_t}"):
            _new_lines = [l for l in _sb_lines if not l.startswith(_t + ",")]
            _new_wl = "\n".join(_new_lines)
            st.session_state.watchlist_data = _new_wl
            try:
                _ws = get_gsheet()
                _ws.clear()
                _rows = [[p.strip() for p in l.split(",",1)] for l in _new_lines]
                if _rows:
                    _ws.update("A1", _rows)
                load_watchlist.clear()
            except Exception as _e:
                st.warning(f"저장 오류: {_e}")
            st.rerun()

    st.markdown("---")
    st.markdown("**➕ 종목 추가**")
    _sb_code = st.text_input("종목코드", placeholder="005930", key="sb_code")
    _sb_name = st.text_input("종목명",   placeholder="삼성전자", key="sb_name")
    if st.button("추가", key="sb_add", use_container_width=True):
        if _sb_code and _sb_name:
            _cur_ids = [p[0].strip() for p in _sb_pairs]
            if _sb_code.strip() not in _cur_ids:
                try:
                    _ws = get_gsheet()
                    _ws.append_row([_sb_code.strip(), _sb_name.strip()])
                    _new_wl = _sb_wl.strip() + f"\n{_sb_code.strip()},{_sb_name.strip()}"
                    st.session_state.watchlist_data = _new_wl
                    load_watchlist.clear()
                    st.rerun()
                except Exception as _e:
                    st.error(f"오류: {_e}")
            else:
                st.warning("이미 있는 종목")
        else:
            st.warning("코드와 이름 입력")

    n = len(_sb_pairs)
    st.markdown(f"<div style='font-size:11px; color:#4dff91'>✅ 총 {n}개 종목</div>", unsafe_allow_html=True)

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
def is_korean_ticker(ticker):
    """한국 종목 여부 (숫자 6자리)"""
    return ticker.isdigit() and len(ticker) == 6

def get_currency(ticker):
    """종목 통화 단위"""
    return "원" if is_korean_ticker(ticker) else "$"

def format_price(price, ticker):
    """가격 포맷 (한국: 원, 미국: 달러)"""
    if is_korean_ticker(ticker):
        return f"{price:,.0f}원"
    else:
        return f"${price:,.2f}"

def get_watchlist_tickers():
    wl = st.session_state.get('watchlist_data', None) or load_watchlist()
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
tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(["🌏 시장 지수", "📊 현황판", "📈 차트 분석", "🤖 Gemini 분석", "🔍 추천 스캐너", "⭐ 관심종목 관리", "🔄 ETF 로테이션", "📝 페이퍼 트레이딩"])



# ══════════════════════════════════════════
# 탭 0: 시장 지수
# ══════════════════════════════════════════
with tab0:
    st.markdown("### 🌏 시장 지수 & 투자자 동향")

    # ── 환율 경고 배너 ──
    try:
        import yfinance as yf
        _krw = yf.Ticker("KRW=X").history(period="1d")['Close'].iloc[-1]
        if _krw >= 1500:
            st.error(f"🚨 환차손 헷지 경고! 원/달러 환율 {_krw:,.1f}원 — 1,500원 돌파! 미국 주식 신규 진입 자제 및 환헷지 검토 필요")
        elif _krw >= 1450:
            st.warning(f"⚠️ 환율 주의 — 원/달러 {_krw:,.1f}원 (1,500원 경계 접근 중)")
    except:
        pass

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

    # ── 5. 10:30 룰 서킷 브레이커 ──
    from datetime import datetime as _dt
    _now_kst = _dt.utcnow()  # UTC 기준 (KST = UTC+9)
    _kst_hour = (_now_kst.hour + 9) % 24
    _kst_min  = _now_kst.minute
    _in_window = (9 <= _kst_hour < 10) or (_kst_hour == 10 and _kst_min <= 30)

    _circuit_breaker = False
    _cb_reason = ""

    if _in_window:
        try:
            import yfinance as yf
            _kospi = yf.Ticker("^KS11").history(period="2d", interval="1d")
            if len(_kospi) >= 2:
                _chg_pct = abs((_kospi['Close'].iloc[-1] / _kospi['Close'].iloc[-2] - 1) * 100)
                if _chg_pct >= 1.5:
                    _circuit_breaker = True
                    _cb_reason = f"코스피 변동성 {_chg_pct:.2f}% (±1.5% 초과)"
        except:
            pass

    if _circuit_breaker:
        st.error(f"🚫 10:30 룰 무효화 — 서킷 브레이커 발동! | 사유: {_cb_reason} | 오늘은 전면 관망. 신규 진입 금지.")
    elif _in_window:
        st.warning("⏰ 09:00~10:30 진입 금지 구간 — 변곡점 대기 중")
    else:
        st.success("✅ 10:30 변곡점 통과 — 진입 가능 구간")

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
            continue  # 데이터 없는 종목 조용히 스킵
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

        # NXT 거래소 가용성 (코스피/코스닥 종목만)
        _is_kr = ticker.isdigit() and len(ticker) == 6
        _nxt_flag = "✅ NXT가능" if _is_kr else "❌ 해당없음"
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
        def _display_name(ticker, name):
            """영문 종목은 티커(이름) 형식으로 표시"""
            if is_korean_ticker(ticker):
                return f"{name} ({ticker})"
            else:
                return f"{ticker} ({name})"

        selected = st.selectbox("종목 선택",
            [_display_name(ticker, name) for ticker, name in TICKERS if ticker in all_data])
        # 티커 추출
        sel_ticker = selected.split('(')[-1].replace(')', '').strip()
        if not is_korean_ticker(sel_ticker):
            # 미국 종목: "AVGO (Broadcom)" → ticker=AVGO
            sel_ticker = selected.split(' ')[0].strip()
        sel_name = all_data[sel_ticker]['name']
        sel_df     = all_data[sel_ticker]['df']

        # 핵심 지표 카드
        l = sel_df.iloc[-1]; p = sel_df.iloc[-2]
        chg = (l['종가']/p['종가']-1)*100
        bb_r = l['BB_upper']-l['BB_lower']
        bb_p = round((l['종가']-l['BB_lower'])/bb_r*100,1) if bb_r>0 else 50
        w52_pos = round((l['종가']-l['52W_low'])/(l['52W_high']-l['52W_low'])*100,1) if (l['52W_high']-l['52W_low'])>0 else 50

        m1,m2,m3,m4,m5,m6 = st.columns(6)
        chg_color = 'up' if chg>0 else 'down'
        _cur_unit = get_currency(sel_ticker)
        _cur_fmt  = format_price(l['종가'], sel_ticker)
        m1.markdown(f"<div class='metric-card'><div class='label'>현재가</div><div class='value flat'>{_cur_fmt}</div></div>", unsafe_allow_html=True)
        m2.markdown(f"<div class='metric-card'><div class='label'>등락</div><div class='value {chg_color}'>{chg:+.2f}%</div></div>", unsafe_allow_html=True)
        rsi_c = 'up' if l['RSI']>=70 else 'down' if l['RSI']<=30 else 'flat'
        m3.markdown(f"<div class='metric-card'><div class='label'>RSI(14)</div><div class='value {rsi_c}'>{l['RSI']:.1f}</div></div>", unsafe_allow_html=True)
        m4.markdown(f"<div class='metric-card'><div class='label'>BB 위치</div><div class='value flat'>{bb_p}%</div></div>", unsafe_allow_html=True)
        m5.markdown(f"<div class='metric-card'><div class='label'>52주 위치</div><div class='value flat'>{w52_pos}%</div></div>", unsafe_allow_html=True)
        vol_c = 'up' if l['거래량_비율']>=200 else 'flat'
        m6.markdown(f"<div class='metric-card'><div class='label'>거래량비율</div><div class='value {vol_c}'>{l['거래량_비율']:.0f}%</div></div>", unsafe_allow_html=True)

        # ── 매수/손절/목표가 입력 ──
        st.markdown("### 🎯 매수 전략 라인")
        _unit    = get_currency(sel_ticker)
        _step    = 100 if is_korean_ticker(sel_ticker) else 1
        _fmt     = "%.0f" if is_korean_ticker(sel_ticker) else "%.2f"
        lc1, lc2, lc3, lc4 = st.columns(4)
        entry_price   = lc1.number_input(f"매수가 ({_unit})", value=0, step=_step,
                                          help="입력하면 차트에 황금색 선으로 표시")
        _stop_default = int(l['종가']*0.93) if entry_price==0 else int(entry_price*0.93)
        stop_price    = lc2.number_input(f"손절가 ({_unit}, -7% 자동계산)",
                                          value=_stop_default, step=_step)
        target1_price = lc3.number_input(f"1차 목표가 ({_unit})", value=0, step=_step)
        target2_price = lc4.number_input(f"2차 목표가 ({_unit})", value=0, step=_step)

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
            _val_fmt = format_price(val, sel_ticker)
            ma_cols[i].markdown(
                f"<div class='metric-card'><div class='label'>{label}선</div>"
                f"<div class='value flat' style='font-size:16px'>{_val_fmt}</div>"
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
    st.markdown("<div style='font-size:13px; color:#6b7fa3; margin-bottom:12px'>거래대금 상위 종목 중 조건 충족 종목을 자동 발굴합니다.</div>", unsafe_allow_html=True)

    # 상황별 추천 조건 가이드
    with st.expander("📖 상황별 추천 필터 조건 가이드", expanded=False):
        gc1, gc2, gc3 = st.columns(3)
        gc1.markdown("""
<div class='metric-card'>
<div class='label'>📉 반등 매매</div>
<div style='font-size:13px; margin-top:8px; line-height:2'>
많이 빠진 종목의 반등을 노릴 때<br>
✅ RSI 과매도<br>
✅ 거래량 폭발<br>
□ MACD 골든크로스<br>
□ BB 하단 근접<br>
□ 정배열
</div>
<div style='font-size:11px; color:#4da6ff; margin-top:8px'>
💡 낙폭 과대 종목 중 거래량으로<br>세력 진입 확인
</div>
</div>""", unsafe_allow_html=True)

        gc2.markdown("""
<div class='metric-card'>
<div class='label'>📈 추세 매매</div>
<div style='font-size:13px; margin-top:8px; line-height:2'>
이미 상승 중인 종목을 탈 때<br>
□ RSI 과매도<br>
✅ 거래량 폭발<br>
✅ MACD 골든크로스<br>
□ BB 하단 근접<br>
✅ 정배열
</div>
<div style='font-size:11px; color:#4dff91; margin-top:8px'>
💡 상승 추세 확인 후 눌림목<br>진입 타이밍 포착
</div>
</div>""", unsafe_allow_html=True)

        gc3.markdown("""
<div class='metric-card'>
<div class='label'>🎯 바닥 확인 매수</div>
<div style='font-size:13px; margin-top:8px; line-height:2'>
바닥 다지고 전환 신호 시<br>
□ RSI 과매도<br>
✅ 거래량 폭발<br>
✅ MACD 골든크로스<br>
✅ BB 하단 근접<br>
□ 정배열
</div>
<div style='font-size:11px; color:#ffd166; margin-top:8px'>
💡 BB 하단 + MACD 전환 =<br>가장 강력한 바닥 신호
</div>
</div>""", unsafe_allow_html=True)

        st.markdown("""
<div style='background:#0f1726; border-left:3px solid #ff4d6d; padding:10px 14px; border-radius:0 8px 8px 0; font-size:12px; margin-top:8px'>
⚠️ <b>주의</b>: 조건을 많이 체크할수록 AND 조건이라 결과가 줄어듭니다.<br>
정배열(상승중) + RSI 과매도(많이 빠짐)는 서로 반대 의미라 동시 체크 시 결과가 거의 없습니다.
</div>""", unsafe_allow_html=True)

    # 스캔 조건 설정
    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("**📋 스캔 대상**")
        market_type = st.selectbox("시장", ["KOSPI", "KOSDAQ", "KOSPI+KOSDAQ", "미국(S&P500)"])
        top_n = st.slider("상위 N종목", 20, 200, 50)
    with col_b:
        st.markdown("**🎯 필터 조건**")
        use_rsi     = st.checkbox("RSI 과매도 (≤35)", value=True)
        use_vol     = st.checkbox("거래량 폭발 (≥150%)", value=True)
        use_macd    = st.checkbox("MACD 골든크로스", value=False)
        use_bb      = st.checkbox("BB 하단 근접 (≤25%)", value=False)
        use_align   = st.checkbox("정배열 (MA5>MA20>MA60)", value=False)
    with col_c:
        st.markdown("**⚙️ 추가 설정**")
        _is_us = market_type == "미국(S&P500)"
        min_price = st.number_input(
            "최소 주가 (원/$)",
            value=1 if _is_us else 5000,
            step=1 if _is_us else 1000
        )
        max_price = st.number_input(
            "최대 주가 (원/$)",
            value=100000 if _is_us else 2000000,
            step=100 if _is_us else 10000
        )
        use_gemini_scan = st.checkbox("Gemini 최종 분석 포함", value=False)
        min_score = st.slider("최소 선정 점수", 1, 10, 4,
                              help="높을수록 조건 많이 충족한 종목만 표시")

    scan_btn = st.button("🚀 스캔 시작", use_container_width=True)

    if st.session_state._keep_passed:
        st.session_state._keep_passed = False

    if scan_btn:
        st.session_state.passed = []
        st.session_state.scan_done = False

        # ── 종목 리스트 구성 ──
        KOSPI_200 = [
            ("005930","삼성전자"),("000660","SK하이닉스"),("005380","현대차"),
            ("000270","기아"),("051910","LG화학"),("006400","삼성SDI"),
            ("035420","NAVER"),("035720","카카오"),("012450","한화에어로스페이스"),
            ("329180","HD현대중공업"),("015760","한국전력"),("034730","SK"),
            ("028260","삼성물산"),("003670","포스코퓨처엠"),("247540","에코프로비엠"),
            ("086520","에코프로"),("207940","삼성바이오로직스"),("068270","셀트리온"),
            ("096770","SK이노베이션"),("011200","HMM"),("010130","고려아연"),
            ("066570","LG전자"),("316140","우리금융지주"),("055550","신한지주"),
            ("105560","KB금융"),("032830","삼성생명"),("017670","SK텔레콤"),
            ("030200","KT"),("018260","삼성에스디에스"),("042700","한미반도체"),
            ("009150","삼성전기"),("010950","S-Oil"),("011070","LG이노텍"),
            ("034220","LG디스플레이"),("024110","기업은행"),("000810","삼성화재"),
            ("088350","한화생명"),("139480","이마트"),("097950","CJ제일제당"),
            ("011780","금호석유"),("009540","HD한국조선해양"),("000100","유한양행"),
            ("032640","LG유플러스"),("003550","LG"),("011170","롯데케미칼"),
            ("004020","현대제철"),("010140","삼성중공업"),("005490","POSCO홀딩스"),
            ("028670","팬오션"),("001040","CJ"),
        ]
        KOSDAQ_100 = [
            ("042700","한미반도체"),("086520","에코프로"),("247540","에코프로비엠"),
            ("003670","포스코퓨처엠"),("196170","알테오젠"),("091990","셀트리온헬스케어"),
            ("263750","펄어비스"),("112040","위메이드"),("357780","솔브레인"),
            ("058470","리노공업"),("095340","ISC"),("122870","와이지엔터테인먼트"),
            ("036930","주성엔지니어링"),("039030","이오테크닉스"),("240810","원익IPS"),
            ("035900","JYP엔터테인먼트"),("041510","에스엠"),("067160","아프리카TV"),
            ("064350","현대로템"),("214150","클래시스"),
        ]
        SP500_TOP = [
            # 기술
            ("AAPL","Apple"),("MSFT","Microsoft"),("NVDA","NVIDIA"),
            ("GOOGL","Alphabet"),("AMZN","Amazon"),("META","Meta"),
            ("TSLA","Tesla"),("AVGO","Broadcom"),("AMD","AMD"),
            ("INTC","Intel"),("QCOM","Qualcomm"),("MU","Micron"),
            ("NOW","ServiceNow"),("CRM","Salesforce"),("PLTR","Palantir"),
            ("ARM","ARM"),("SMCI","Super Micro"),("ORCL","Oracle"),
            ("CSCO","Cisco"),("IBM","IBM"),("TXN","Texas Instruments"),
            ("AMAT","Applied Materials"),("LRCX","Lam Research"),
            ("KLAC","KLA Corp"),("ADI","Analog Devices"),
            ("MRVL","Marvell"),("MPWR","Monolithic Power"),
            ("ON","ON Semiconductor"),("NXPI","NXP Semi"),
            ("STX","Seagate"),("WDC","Western Digital"),
            ("HPQ","HP Inc"),("HPE","HP Enterprise"),("DELL","Dell"),
            ("ACN","Accenture"),("INTU","Intuit"),("ADP","ADP"),
            ("ADBE","Adobe"),("ANSS","Ansys"),("CDNS","Cadence"),
            ("SNPS","Synopsys"),("FTNT","Fortinet"),("PANW","Palo Alto"),
            ("CRWD","CrowdStrike"),("ZS","Zscaler"),("OKTA","Okta"),
            ("SNOW","Snowflake"),("DDOG","Datadog"),("MDB","MongoDB"),
            ("NET","Cloudflare"),("TEAM","Atlassian"),("HUBS","HubSpot"),
            # 금융
            ("JPM","JPMorgan"),("BAC","Bank of America"),("WFC","Wells Fargo"),
            ("GS","Goldman Sachs"),("MS","Morgan Stanley"),("C","Citigroup"),
            ("BLK","BlackRock"),("SCHW","Charles Schwab"),("AXP","Amex"),
            ("V","Visa"),("MA","Mastercard"),("PYPL","PayPal"),
            ("COF","Capital One"),("USB","US Bancorp"),("TFC","Truist"),
            ("PNC","PNC Financial"),("MTB","M&T Bank"),("FITB","Fifth Third"),
            ("KEY","KeyCorp"),("RF","Regions Financial"),
            # 헬스케어
            ("UNH","UnitedHealth"),("LLY","Eli Lilly"),("JNJ","J&J"),
            ("PFE","Pfizer"),("MRK","Merck"),("ABBV","AbbVie"),
            ("ABT","Abbott"),("TMO","Thermo Fisher"),("DHR","Danaher"),
            ("BMY","Bristol Myers"),("AMGN","Amgen"),("GILD","Gilead"),
            ("BIIB","Biogen"),("VRTX","Vertex"),("REGN","Regeneron"),
            ("ISRG","Intuitive Surgical"),("SYK","Stryker"),("MDT","Medtronic"),
            ("BSX","Boston Scientific"),("EW","Edwards Life"),
            # 소비재
            ("WMT","Walmart"),("COST","Costco"),("HD","Home Depot"),
            ("LOW","Lowe's"),("TGT","Target"),("AMZN","Amazon"),
            ("MCD","McDonald's"),("SBUX","Starbucks"),("YUM","Yum Brands"),
            ("NKE","Nike"),("PG","P&G"),("KO","Coca-Cola"),
            ("PEP","PepsiCo"),("PM","Philip Morris"),("MO","Altria"),
            ("CL","Colgate"),("EL","Estee Lauder"),("ULTA","Ulta Beauty"),
            # 에너지
            ("XOM","ExxonMobil"),("CVX","Chevron"),("COP","ConocoPhillips"),
            ("SLB","SLB"),("EOG","EOG Resources"),("PXD","Pioneer Natural"),
            ("MPC","Marathon Petroleum"),("VLO","Valero"),("PSX","Phillips 66"),
            # 산업
            ("BA","Boeing"),("CAT","Caterpillar"),("DE","John Deere"),
            ("HON","Honeywell"),("GE","GE"),("MMM","3M"),
            ("RTX","Raytheon"),("LMT","Lockheed Martin"),("NOC","Northrop"),
            ("GD","General Dynamics"),("UPS","UPS"),("FDX","FedEx"),
            ("WM","Waste Management"),("RSG","Republic Services"),
            # 통신/미디어
            ("NFLX","Netflix"),("DIS","Disney"),("CMCSA","Comcast"),
            ("T","AT&T"),("VZ","Verizon"),("TMUS","T-Mobile"),
            ("CHTR","Charter"),("PARA","Paramount"),("WBD","Warner Bros"),
            # 부동산/유틸리티
            ("NEE","NextEra Energy"),("DUK","Duke Energy"),("SO","Southern"),
            ("D","Dominion"),("AEP","AEP"),("EXC","Exelon"),
            ("PLD","Prologis"),("AMT","American Tower"),("CCI","Crown Castle"),
            ("EQIX","Equinix"),("PSA","Public Storage"),("AVB","AvalonBay"),
            # 기타 주목 종목
            ("MSTR","MicroStrategy"),("COIN","Coinbase"),("MELI","MercadoLibre"),
            ("SE","Sea Limited"),("BABA","Alibaba"),("JD","JD.com"),
            ("SHOP","Shopify"),("SQ","Block"),("AFRM","Affirm"),
            ("RBLX","Roblox"),("UBER","Uber"),("LYFT","Lyft"),
            ("ABNB","Airbnb"),("DASH","DoorDash"),("PINS","Pinterest"),
            ("SNAP","Snap"),("ROKU","Roku"),("TTD","Trade Desk"),
        ]

        extra = [(t,n) for t,n in TICKERS]

        if market_type == "KOSPI":
            scan_list = KOSPI_200 + [x for x in extra if x not in KOSPI_200]
        elif market_type == "KOSDAQ":
            scan_list = KOSDAQ_100 + [x for x in extra if x not in KOSDAQ_100]
        elif market_type == "KOSPI+KOSDAQ":
            scan_list = KOSPI_200 + [x for x in KOSDAQ_100 if x not in KOSPI_200]
            scan_list += [x for x in extra if x not in scan_list]
        else:  # 미국 S&P500
            scan_list = SP500_TOP + [x for x in extra if x not in SP500_TOP]

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
                    # 미국 종목은 suffix 없이 직접 조회
                    if market_type == "미국(S&P500)":
                        try:
                            import yfinance as yf
                            _yt   = yf.Ticker(ticker)
                            _hist = _yt.history(period="6mo", interval="1d")
                            if _hist is None or _hist.empty:
                                continue
                            df = _hist.rename(columns={
                                'Open':'시가','High':'고가','Low':'저가',
                                'Close':'종가','Volume':'거래량'
                            })[['시가','고가','저가','종가','거래량']].tail(60)
                            df = df[df['거래량'] > 0]
                        except:
                            continue
                    else:
                        df = fetch_ohlcv(ticker, 60)
                    if df is None or len(df) < 20: continue

                    l = df.iloc[-1]
                    # 가격 필터 (미국은 달러 기준)
                    _price = l['종가']
                    if _price < min_price or _price > max_price: continue

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

                    if score >= min_score:
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
            # df 포함 전체 저장 — rerun 후에도 차트 유지
            st.session_state.passed = passed
            st.session_state.scan_done = True

            if not passed:
                st.warning("⚠️ 조건을 충족하는 종목이 없습니다. 조건을 완화해보세요.")

        # ── 스캔 결과 항상 표시 ──
        if st.session_state.passed:
            _sc_wl  = st.session_state.get('watchlist_data', None) or load_watchlist()
            _sc_ids = [l.split(',')[0].strip() for l in _sc_wl.split('\n') if ',' in l]
            _new_items = [i for i in st.session_state.passed if i['ticker'] not in _sc_ids]

            st.success(f"✅ {len(st.session_state.passed)}개 종목 발굴!")

            # ── 전체 일괄 추가 버튼 ──
            if _new_items:
                _col_add, _col_info = st.columns([2, 3])
                if _col_add.button(
                    f"⭐ 전체 {len(_new_items)}개 사이드바 추가",
                    key="bulk_add_btn",
                    use_container_width=True,
                    type="primary"
                ):
                    try:
                        _ws = get_gsheet()
                        # Sheets 현재 데이터 다시 읽어서 중복 방지
                        _sheet_data = _ws.get_all_values()
                        _sheet_ids  = [r[0].strip() for r in _sheet_data if r]
                        _cur = "\n".join([",".join(r) for r in _sheet_data if len(r)>=2])
                        _added = 0
                        for _it in _new_items:
                            if _it['ticker'] not in _sheet_ids:
                                _ws.append_row([_it['ticker'], _it['name']])
                                _cur = _cur.strip() + f"\n{_it['ticker']},{_it['name']}"
                                _sheet_ids.append(_it['ticker'])
                                _added += 1
                        # session_state 즉시 업데이트
                        st.session_state.watchlist_data = _cur
                        load_watchlist.clear()
                        st.session_state._keep_passed = True
                        st.success(f"✅ {_added}개 추가 완료! 사이드바가 업데이트됩니다.")
                        st.rerun()
                    except Exception as _e:
                        import traceback
                        st.error(f"추가 오류: {_e}")
                        st.code(traceback.format_exc())
                _col_info.markdown(
                    "<div style='padding:8px; font-size:12px; color:#6b7fa3'>"
                    "사이드바에 없는 종목만 추가됩니다.</div>",
                    unsafe_allow_html=True
                )
            else:
                st.info("✅ 발굴된 종목이 모두 이미 사이드바에 있습니다.")

            st.markdown("---")

            # ── 종목 카드 (읽기 전용 — 버튼 없음) ──
            for _si, item in enumerate(st.session_state.passed):
                _is_added  = item['ticker'] in _sc_ids
                chg_color  = '#ff4d6d' if item['등락(%)'] > 0 else '#4da6ff'
                badge_html = ' '.join([f"<span class='badge badge-buy'>{r}</span>" for r in item['reasons']])

                st.markdown(
                    f"<div style='background:#111827; border:1px solid {'#2d6644' if _is_added else '#1e3a5f'}; "
                    f"border-radius:10px; padding:14px 18px; margin-bottom:12px'>"
                    f"<div style='display:flex; justify-content:space-between; align-items:center; margin-bottom:8px'>"
                    f"<span style='font-size:15px; font-weight:700'>{'✅' if _is_added else '⭐'} {item['name']} "
                    f"<span style='color:#475569; font-size:12px; font-family:IBM Plex Mono'>({item['ticker']})</span></span>"
                    f"<span style='color:{chg_color}; font-family:IBM Plex Mono; font-weight:700'>"
                    f"{'▲' if item['등락(%)']>0 else '▼'} {abs(item['등락(%)']):+.2f}%</span>"
                    f"</div>"
                    f"<div style='display:flex; gap:16px; margin-bottom:8px'>"
                    f"<span style='font-size:13px; color:#a0b0c8'>현재가 <b style='color:#e0e6f0'>{item['현재가']:,.0f}</b></span>"
                    f"<span style='font-size:13px; color:#a0b0c8'>RSI <b style='color:#e0e6f0'>{item['RSI']:.1f}</b></span>"
                    f"<span style='font-size:13px; color:#a0b0c8'>거래량 <b style='color:#e0e6f0'>{item['거래량비율']:.0f}%</b></span>"
                    f"<span style='font-size:13px; color:#ffd166'>점수 <b>{item['score']}점</b></span>"
                    f"</div>"
                    f"<div>{badge_html} "
                    f"{'<span style="color:#4dff91; font-size:12px">✅ 사이드바 추가됨</span>' if _is_added else ''}"
                    f"</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )


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
    for _idx, (_tk, _nm) in enumerate(_pairs):
        _ca, _cb = st.columns([5, 1])
        _ca.markdown(
            f"<div style='padding:10px; background:#111827; border-radius:8px;"
            f"border:1px solid #1e3a5f; margin-bottom:6px'>"
            f"<b>{_nm}</b>&nbsp;&nbsp;"
            f"<code style='color:#475569;font-size:11px'>{_tk}</code></div>",
            unsafe_allow_html=True
        )
        _cb.button(
            "삭제", key=f"t5_del_{_idx}_{_tk}",
            on_click=_do_delete, args=(_tk,)
        )

    st.divider()

    # ── 직접 추가 ──
    st.markdown("#### ➕ 직접 추가")

    # session_state로 입력값 보존
    if 'form_code' not in st.session_state:
        st.session_state.form_code = ''
    if 'form_name' not in st.session_state:
        st.session_state.form_name = ''
    if 'form_msg' not in st.session_state:
        st.session_state.form_msg = None

    with st.form("add_ticker_form", clear_on_submit=True):
        _fc, _fn = st.columns(2)
        _f_code = _fc.text_input("종목코드", placeholder="005930")
        _f_name = _fn.text_input("종목명",   placeholder="삼성전자")
        _submitted = st.form_submit_button("✅ 추가", use_container_width=True)
        if _submitted:
            st.session_state.form_code = _f_code
            st.session_state.form_name = _f_name

    # form 밖에서 처리
    if st.session_state.form_code and st.session_state.form_name:
        _code = st.session_state.form_code.strip()
        _name = st.session_state.form_name.strip()
        st.session_state.form_code = ''
        st.session_state.form_name = ''
        try:
            _cur_wl  = load_watchlist()
            _cur_ids = [l.split(",")[0].strip() for l in _cur_wl.split("\n") if "," in l]
            if _code not in _cur_ids:
                # append_rows 방식으로 변경 (clear 없이 한 줄만 추가)
                ws = get_gsheet()
                ws.append_row([_code, _name])
                # session_state 업데이트
                _new_wl = _cur_wl.strip() + f"\n{_code},{_name}"
                st.session_state.watchlist_data = _new_wl
                load_watchlist.clear()
                st.success(f"✅ {_name} 추가 완료!")
                st.rerun()
            else:
                st.warning("이미 등록된 종목입니다.")
        except Exception as _e:
            import traceback
            st.error(f"오류: {_e}")
            st.code(traceback.format_exc())

    st.divider()

    # ── 스캐너 추천 종목 — 콜백 방식 ──
    st.markdown("#### 🔍 스캐너 추천 종목")

    def _do_add(tk, nm):
        add_ticker(tk, nm)

    if st.session_state.passed:
        for _idx2, _item in enumerate(st.session_state.passed):
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

# ══════════════════════════════════════════
# 탭 6: ETF 로테이션 랭킹판
# ══════════════════════════════════════════
with tab6:
    st.markdown("### 🔄 ADX/Z-Score 기반 7대 ETF 로테이션 랭킹판")
    st.caption("ADX 25 미만은 추세 없음으로 탈락. 1위 ETF에 100% 스위칭 권고.")

    ETF_LIST = [
        ("069500",  "KODEX 200",        "KS"),
        ("133690",  "TIGER 나스닥100",  "KS"),
        ("091160",  "KODEX 반도체",     "KS"),
        ("464690",  "KODEX 조선",       "KS"),
        ("455050",  "PLUS K방산",       "KS"),
        ("459580",  "KODEX AI전력핵심", "KS"),
        ("411060",  "ACE KRX금현물",    "KS"),
    ]

    @st.cache_data(ttl=3600, show_spinner=False)
    def fetch_etf_data():
        import yfinance as yf
        import numpy as np
        results = []
        for ticker, name, mkt in ETF_LIST:
            try:
                _sym = f"{ticker}.KS"
                _df  = yf.Ticker(_sym).history(period="6mo", interval="1d")
                if _df is None or len(_df) < 30:
                    results.append({'종목코드':ticker,'ETF명':name,'현재가':0,'등락(%)':0,'ADX':0,'Z-Score':0,'상태':'데이터없음'})
                    continue
                _df = _df.rename(columns={'High':'고가','Low':'저가','Close':'종가'})
                _hi = _df['고가']; _lo = _df['저가']; _cl = _df['종가']
                _tr  = pd.DataFrame({'hl':_hi-_lo,'hc':(_hi-_cl.shift()).abs(),'lc':(_lo-_cl.shift()).abs()}).max(axis=1)
                _atr = _tr.rolling(14).mean()
                _pdm = _hi.diff().clip(lower=0)
                _ndm = (-_lo.diff()).clip(lower=0)
                _pdi = 100*_pdm.rolling(14).mean()/_atr.replace(0,np.nan)
                _ndi = 100*_ndm.rolling(14).mean()/_atr.replace(0,np.nan)
                _dx  = 100*(_pdi-_ndi).abs()/(_pdi+_ndi).replace(0,np.nan)
                _adx = _dx.rolling(14).mean().iloc[-1]
                _ret = _cl.pct_change()
                _zs  = (_ret.iloc[-1]-_ret.rolling(20).mean().iloc[-1])/_ret.rolling(20).std().iloc[-1] if _ret.rolling(20).std().iloc[-1]>0 else 0
                _chg = (_cl.iloc[-1]/_cl.iloc[-2]-1)*100
                results.append({'종목코드':ticker,'ETF명':name,'현재가':round(_cl.iloc[-1],0),
                                 '등락(%)':round(_chg,2),'ADX':round(_adx,1),
                                 'Z-Score':round(_zs,2),'상태':'활성' if _adx>=25 else '탈락'})
            except Exception as _e:
                results.append({'종목코드':ticker,'ETF명':name,'현재가':0,'등락(%)':0,'ADX':0,'Z-Score':0,'상태':'오류'})
        return results

    with st.spinner("ETF 데이터 로딩 중..."):
        _etf_data = fetch_etf_data()

    if _etf_data:
        _df_etf  = pd.DataFrame(_etf_data)
        _active  = _df_etf[_df_etf['상태']=='활성'].sort_values('Z-Score', ascending=False)
        _passive = _df_etf[_df_etf['상태']!='활성']
        _ranked  = pd.concat([_active, _passive]).reset_index(drop=True)

        # 현재 관심종목 목록
        _etf_wl_now  = st.session_state.get('watchlist_data') or load_watchlist()
        _etf_wl_ids  = [l.split(',')[0].strip() for l in _etf_wl_now.split('\n') if ',' in l]

        for _i, row in _ranked.iterrows():
            _is_top  = (_i == 0 and row['상태'] == '활성')
            _is_dead = (row['상태'] != '활성')
            _bg      = '#1a1400' if _is_top else '#0d0d0d' if _is_dead else '#111827'
            _border  = '#ffd166' if _is_top else '#2d3a55' if _is_dead else '#1e3a5f'
            _op      = '0.4' if _is_dead else '1.0'
            _cc      = '#ff4d6d' if row['등락(%)']>0 else '#4da6ff'
            _ac      = '#4dff91' if row['ADX']>=25 else '#ff4d6d'
            _rank    = '🥇' if _is_top else f"{_i+1}위"
            _tag     = ' <span style="background:#ffd166;color:#000;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">100% 스위칭 타겟</span>' if _is_top else ''
            _dead_tag= ' <span style="color:#475569;font-size:11px">ADX 25미만 탈락</span>' if _is_dead else ''
            _already = row['종목코드'] in _etf_wl_ids

            st.markdown(
                f"<div style='background:{_bg};border:1px solid {_border};border-radius:10px;"
                f"padding:14px 18px;margin-bottom:4px;opacity:{_op}'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<div><b style='font-size:15px'>{_rank} {row['ETF명']}</b>"
                f"<span style='color:#475569;font-size:11px'> ({row['종목코드']})</span>"
                f"{_tag}{_dead_tag}</div>"
                f"<span style='color:{_cc};font-family:IBM Plex Mono'>{'▲' if row['등락(%)']>0 else '▼'}{abs(row['등락(%)']):+.2f}%</span>"
                f"</div>"
                f"<div style='display:flex;gap:20px;margin-top:8px'>"
                f"<span style='font-size:13px;color:#a0b0c8'>현재가 <b style='color:#e0e6f0'>{row['현재가']:,.0f}</b></span>"
                f"<span style='font-size:13px;color:#a0b0c8'>ADX <b style='color:{_ac}'>{row['ADX']}</b></span>"
                f"<span style='font-size:13px;color:#a0b0c8'>Z-Score <b style='color:#e0e6f0'>{row['Z-Score']:+.2f}</b></span>"
                f"</div></div>",
                unsafe_allow_html=True
            )

            # 관심종목 추가 버튼
            _eb1, _eb2 = st.columns([1, 4])
            if _already:
                _eb1.markdown("<div style='color:#4dff91;font-size:12px;padding:4px 0'>✅ 추가됨</div>", unsafe_allow_html=True)
            else:
                if _eb1.button("⭐ 추가", key=f"etf_add_{_i}_{row['종목코드']}"):
                    try:
                        _ws_etf = get_gsheet()
                        _ws_etf.append_row([row['종목코드'], row['ETF명']])
                        _new_wl = _etf_wl_now.strip() + f"\n{row['종목코드']},{row['ETF명']}"
                        st.session_state.watchlist_data = _new_wl
                        try:
                            load_watchlist.clear()
                        except:
                            pass
                        st.success(f"✅ {row['ETF명']} 관심종목 추가!")
                        st.rerun()
                    except Exception as _e:
                        st.error(f"추가 오류: {_e}")
            st.markdown("<div style='margin-bottom:8px'></div>", unsafe_allow_html=True)

        st.markdown("---")
        st.caption("ADX ≥ 25: 강한 추세 / Z-Score 높을수록 상대 강도 우위 / 1위 ETF 집중 스위칭 권고")
        if st.button("🔄 ETF 새로고침", key="etf_refresh2"):
            fetch_etf_data.clear()
            st.rerun()

# ══════════════════════════════════════════
# 탭 7: 페이퍼 트레이딩
# ══════════════════════════════════════════
with tab7:
    st.markdown("### 📝 페이퍼 트레이딩 (모의투자)")
    st.caption("실제 자금 없이 V8.9 전략을 검증합니다. 슬리피지·수수료·세금 자동 반영.")

    _acc       = load_account()
    _total_val = calc_portfolio_value(_acc)
    _pnl       = _total_val - _acc['initial']
    _pnl_pct   = (_pnl / _acc['initial'] * 100) if _acc['initial'] > 0 else 0
    _mdd       = ((_acc['trough'] - _acc['peak']) / _acc['peak'] * 100) if _acc['peak'] > 0 else 0

    # ── 1. 계좌 현황 ──
    st.markdown("#### 💰 가상 계좌 현황")
    _pa1, _pa2, _pa3, _pa4, _pa5 = st.columns(5)
    _pa1.markdown(f"<div class='metric-card'><div class='label'>초기자본</div><div class='value flat'>{_acc['initial']:,.0f}원</div></div>", unsafe_allow_html=True)
    _pa2.markdown(f"<div class='metric-card'><div class='label'>현금잔고</div><div class='value flat'>{_acc['cash']:,.0f}원</div></div>", unsafe_allow_html=True)
    _pa3.markdown(f"<div class='metric-card'><div class='label'>총평가금액</div><div class='value flat'>{_total_val:,.0f}원</div></div>", unsafe_allow_html=True)
    _pnl_c = 'up' if _pnl >= 0 else 'down'
    _pa4.markdown(f"<div class='metric-card'><div class='label'>총손익</div><div class='value {_pnl_c}'>{_pnl:+,.0f}원<br>({_pnl_pct:+.2f}%)</div></div>", unsafe_allow_html=True)
    _mdd_c = 'down' if _mdd < -5 else 'flat'
    _pa5.markdown(f"<div class='metric-card'><div class='label'>MDD</div><div class='value {_mdd_c}'>{_mdd:.2f}%</div></div>", unsafe_allow_html=True)

    if _mdd < -10:
        st.error(f"🚨 MDD 경고! {_mdd:.2f}% — 포지션 즉시 점검 필요")
    elif _mdd < -5:
        st.warning(f"⚠️ MDD 주의 {_mdd:.2f}%")

    # 초기화
    with st.expander("⚙️ 가상 계좌 설정"):
        _new_cap = st.number_input("초기자본 (원)", value=int(_acc['initial']), step=1000000, min_value=1000000)
        if st.button("🔄 계좌 초기화 (전체 리셋)", key="reset_account"):
            _new_acc = {'initial':_new_cap,'cash':_new_cap,'positions':[],'peak':_new_cap,'trough':_new_cap}
            save_account(_new_acc)
            st.success(f"✅ {_new_cap:,.0f}원으로 초기화!")
            st.rerun()

    st.divider()

    # ── 2. 보유 포지션 ──
    st.markdown("#### 📊 보유 포지션")
    if not _acc['positions']:
        st.info("💡 보유 포지션 없음. 아래 가상 매수를 실행해보세요.")
    else:
        for _pi, _pos in enumerate(_acc['positions']):
            try:
                _cur_df = fetch_ohlcv(_pos['ticker'], 5)
                _cur_p  = float(_cur_df['종가'].iloc[-1]) if _cur_df is not None and not _cur_df.empty else float(_pos['avg_price'])
            except:
                _cur_p = float(_pos['avg_price'])

            _pos_val = _cur_p * _pos['qty']
            _pos_pnl = (_cur_p - _pos['avg_price']) * _pos['qty']
            _pos_pct = (_cur_p / _pos['avg_price'] - 1) * 100
            _pc      = 'up' if _pos_pnl >= 0 else 'down'
            _kill    = _pos['avg_price'] * 0.93
            _kill_alert = _cur_p <= _kill

            st.markdown(
                f"<div style='background:#111827;border:2px solid {'#ff4d6d' if _kill_alert else '#1e3a5f'};border-radius:10px;padding:14px;margin-bottom:8px'>"
                f"<div style='display:flex;justify-content:space-between'>"
                f"<b style='font-size:15px'>{_pos['name']} <span style='color:#475569;font-size:12px'>({_pos['ticker']})</span></b>"
                f"<span class='{_pc}' style='font-size:16px;font-weight:700'>{_pos_pct:+.2f}%</span></div>"
                f"<div style='display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:10px'>"
                f"<div style='text-align:center'><div style='font-size:10px;color:#6b7fa3'>수량</div><div style='font-weight:700'>{_pos['qty']:,}주</div></div>"
                f"<div style='text-align:center'><div style='font-size:10px;color:#6b7fa3'>평단가</div><div style='font-weight:700'>{_pos['avg_price']:,.0f}원</div></div>"
                f"<div style='text-align:center'><div style='font-size:10px;color:#6b7fa3'>현재가</div><div style='font-weight:700'>{_cur_p:,.0f}원</div></div>"
                f"<div style='text-align:center'><div style='font-size:10px;color:#6b7fa3'>평가금액</div><div style='font-weight:700'>{_pos_val:,.0f}원</div></div>"
                f"<div style='text-align:center'><div style='font-size:10px;color:#6b7fa3'>평가손익</div><div class='{_pc}' style='font-weight:700'>{_pos_pnl:+,.0f}원</div></div>"
                f"</div>"
                f"<div style='margin-top:8px;font-size:12px;color:#ff4d6d'>킬스위치 기준: {_kill:,.0f}원 (-7%)"
                f"{'  🚨 킬스위치 발동! 즉각 매도 검토' if _kill_alert else ''}</div>"
                f"</div>",
                unsafe_allow_html=True
            )

            _sc1, _sc2, _sc3 = st.columns([1, 1, 3])
            _sell_qty = _sc1.number_input("매도수량", min_value=1, max_value=_pos['qty'],
                                           value=_pos['qty'], key=f"sq_{_pi}_{_pos['ticker']}")
            if _sc2.button("📤 가상 매도", key=f"sell_{_pi}_{_pos['ticker']}", use_container_width=True):
                _net_p    = calc_slippage(_cur_p, False, is_korean_ticker(_pos['ticker']))
                _proceeds = _net_p * _sell_qty
                _acc['cash'] += _proceeds
                if _sell_qty >= _pos['qty']:
                    _acc['positions'] = [p for p in _acc['positions'] if p['ticker'] != _pos['ticker']]
                else:
                    _pos['qty'] -= _sell_qty
                _tv_now = calc_portfolio_value(_acc)
                _acc['peak']   = max(_acc['peak'], _tv_now)
                _acc['trough'] = min(_acc['trough'], _tv_now)
                save_account(_acc)
                log_trade(_pos['ticker'], _pos['name'], "매도", _sell_qty,
                          _cur_p, _net_p, _acc['cash'], _tv_now)
                st.success(f"✅ {_pos['name']} {_sell_qty}주 매도 @ {_net_p:,.0f}원 (세금+수수료 차감)")
                st.rerun()

    st.divider()

    # ── 3. 가상 매수 ──
    st.markdown("#### 📥 가상 매수 실행")

    _bc1, _bc2 = st.columns([2, 3])
    _buy_ticker_sel = _bc1.selectbox("종목 선택",
        [f"{n} ({t})" for t,n in TICKERS], key="buy_ticker_sel")
    _bt = _buy_ticker_sel.split('(')[-1].replace(')','').strip()
    if not is_korean_ticker(_bt):
        _bt = _buy_ticker_sel.split(' ')[0].strip()
    _bn = dict([(t,n) for t,n in TICKERS]).get(_bt, _bt)

    # 현재가 자동 로드
    try:
        _buy_df  = fetch_ohlcv(_bt, 5)
        _buy_cur = float(_buy_df['종가'].iloc[-1]) if _buy_df is not None and not _buy_df.empty else 0
    except:
        _buy_cur = 0

    _bc2.markdown(
        f"<div style='background:#0f1726;border:1px solid #1e3a5f;border-radius:8px;padding:12px;margin-top:28px'>"
        f"현재가: <b style='font-size:18px;color:#ffd166'>{_buy_cur:,.0f}원</b> | "
        f"현금잔고: <b style='color:#4dff91'>{_acc['cash']:,.0f}원</b></div>",
        unsafe_allow_html=True
    )

    _brow1, _brow2, _brow3, _brow4 = st.columns(4)
    _buy_price = _brow1.number_input("매수가 (원)", value=int(_buy_cur) if _buy_cur > 0 else 1,
                                      step=100, min_value=1, key="buy_price_inp")
    _buy_qty   = _brow2.number_input("수량 (주)", min_value=1, value=1, key="buy_qty_inp")
    _ai_score  = _brow3.number_input("5AI 점수", min_value=-5, max_value=5, value=0, key="buy_ai")
    _buy_total = _buy_price * _buy_qty
    _net_buy_preview = calc_slippage(_buy_price, True, is_korean_ticker(_bt))
    _brow4.markdown(
        f"<div style='padding-top:28px'>"
        f"필요금액: <b>{_buy_total:,.0f}원</b><br>"
        f"<span style='font-size:11px;color:#6b7fa3'>슬리피지 반영: {_net_buy_preview:,.0f}원/주</span></div>",
        unsafe_allow_html=True
    )

    _buy_memo = st.text_input("매수 근거 (Why)", placeholder="예: BB하단 반등, 골든크로스 확인, 5AI +3점", key="buy_memo")

    _cash_ok = _acc['cash'] >= _buy_total
    if not _cash_ok:
        st.warning(f"⚠️ 현금 부족 — 필요: {_buy_total:,.0f}원 / 보유: {_acc['cash']:,.0f}원")

    if st.button("📥 가상 매수 실행", key="exec_buy", use_container_width=True,
                 type="primary", disabled=not _cash_ok):
        _net_b = calc_slippage(_buy_price, True, is_korean_ticker(_bt))
        _cost  = _net_b * _buy_qty
        _acc['cash'] -= _cost
        _pos_exist = get_position(_acc, _bt)
        if _pos_exist:
            _old_v = _pos_exist['avg_price'] * _pos_exist['qty']
            _new_v = _net_b * _buy_qty
            _pos_exist['qty']      += _buy_qty
            _pos_exist['avg_price'] = round((_old_v + _new_v) / _pos_exist['qty'])
        else:
            _acc['positions'].append({
                'ticker': _bt, 'name': _bn,
                'qty': _buy_qty, 'avg_price': _net_b,
                'entry_date': str(pd.Timestamp.now())[:10]
            })
        _tv_now = calc_portfolio_value(_acc)
        _acc['peak']   = max(_acc['peak'], _tv_now)
        _acc['trough'] = min(_acc['trough'], _tv_now)
        save_account(_acc)
        log_trade(_bt, _bn, "매수", _buy_qty, _buy_price, _net_b,
                  _acc['cash'], _tv_now, ai_score=_ai_score, memo=_buy_memo)
        st.success(f"✅ {_bn} {_buy_qty}주 @ {_net_b:,.0f}원 체결! (슬리피지+수수료 반영)")
        st.rerun()

    st.divider()

    # ── 4. 성과 분석 ──
    st.markdown("#### 📈 성과 분석 (vs 벤치마크)")
    try:
        _log_ws   = get_trading_sheet()
        _log_data = _log_ws.get_all_values() if _log_ws else []

        if len(_log_data) > 1:
            _log_df = pd.DataFrame(_log_data[1:], columns=_log_data[0])
            _log_df['날짜']     = pd.to_datetime(_log_df['날짜'], errors='coerce')
            _log_df['평가금액'] = pd.to_numeric(_log_df['평가금액'], errors='coerce')
            _log_df = _log_df.dropna(subset=['날짜','평가금액']).sort_values('날짜')

            if not _log_df.empty:
                _log_df['수익률(%)'] = (_log_df['평가금액'] / _acc['initial'] - 1) * 100

                # 벤치마크 비교
                import yfinance as yf
                _start_bm = _log_df['날짜'].min()
                try:
                    _bm   = yf.Ticker("^KS11").history(start=_start_bm, interval="1d")
                    _bm_r = (_bm['Close'] / _bm['Close'].iloc[0] - 1) * 100
                    _bm_r.index = pd.to_datetime(_bm_r.index).tz_localize(None)
                    _port = _log_df.set_index('날짜')['수익률(%)']
                    _port.index = pd.to_datetime(_port.index).tz_localize(None)
                    _cmp  = pd.DataFrame({'내 포트폴리오(%)': _port, '코스피(%)': _bm_r})
                    _cmp  = _cmp.fillna(method='ffill').dropna()
                    st.line_chart(_cmp)
                except:
                    st.line_chart(_log_df.set_index('날짜')['수익률(%)'])

                # MDD
                _cm  = _log_df['평가금액'].cummax()
                _dd  = (_log_df['평가금액'] - _cm) / _cm * 100
                _mdd_v = _dd.min()
                _mc1, _mc2, _mc3 = st.columns(3)
                _mc1.metric("최대낙폭(MDD)", f"{_mdd_v:.2f}%")
                _mc2.metric("총 거래 횟수", f"{len(_log_df)}회")
                _mc3.metric("최종 수익률", f"{_log_df['수익률(%)'].iloc[-1]:+.2f}%")

                # 거래 일지
                st.markdown("##### 📋 거래 일지")
                _show_cols = [c for c in ['날짜','종목명','매매','수량','순체결가','평가금액','5AI점수','메모'] if c in _log_df.columns]
                st.dataframe(_log_df[_show_cols].tail(20), use_container_width=True)
        else:
            st.info("아직 거래 기록이 없습니다. 가상 매수를 실행해보세요!")
            # 샘플 차트
            _sample = pd.DataFrame({'내 포트폴리오(%)': [0,1.2,0.8,2.1,1.5,3.2,2.8],
                                     '코스피(%)':       [0,0.5,0.3,1.1,0.9,1.8,1.5]})
            st.markdown("*(샘플 차트 — 거래 실행 후 실제 데이터로 교체됩니다)*")
            st.line_chart(_sample)
    except Exception as _e:
        st.warning(f"성과 분석 로드 오류: {_e}")

st.markdown("---")
st.markdown("<div style='text-align:center;font-size:11px;color:#2d3a55;font-family:IBM Plex Mono'>퀀트 관제탑 V8.9 | 투자 자문 아님 — 모든 손익의 책임은 본인에게 있습니다</div>", unsafe_allow_html=True)
