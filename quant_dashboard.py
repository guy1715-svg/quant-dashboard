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

# ══════════════════════════════════════════
# KIS API 연동 (한국투자증권)
# ══════════════════════════════════════════
import requests as _requests

# ── KIS API 엔드포인트 상수 ──
_KIS_BASE = "https://openapi.koreainvestment.com:9443"
_KIS_URL_TOKEN    = f"{_KIS_BASE}/oauth2/tokenP"
_KIS_URL_PRICE    = f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
_KIS_URL_BALANCE  = f"{_KIS_BASE}/uapi/domestic-stock/v1/trading/inquire-balance"
_KIS_URL_INVESTOR = f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-investor"

import time as _time_kis

def kis_get_token():
    """KIS API 접근 토큰 발급 — 6시간 TTL 자동 갱신"""
    _now = _time_kis.time()
    # 유효한 토큰이 있으면 바로 반환
    if (st.session_state.get('kis_token') and
            _now - st.session_state.get('kis_token_time', 0) < 21600):
        return st.session_state.kis_token
    # 토큰 없거나 만료 → 재발급
    try:
        _key    = st.secrets["KIS_APP_KEY"]
        _secret = st.secrets["KIS_APP_SECRET"]
        _url    = _KIS_URL_TOKEN
        _res    = _requests.post(_url, json={
            "grant_type": "client_credentials",
            "appkey":     _key,
            "appsecret":  _secret
        }, timeout=10)
        _token = _res.json().get("access_token")
        if _token:
            st.session_state.kis_token      = _token
            st.session_state.kis_token_time = _now
            return _token
    except Exception:
        pass
    return None

def kis_get_price(ticker):
    """KIS API 실시간 현재가 조회"""
    try:
        _token  = kis_get_token()
        if not _token: return None
        _key    = st.secrets["KIS_APP_KEY"]
        _secret = st.secrets["KIS_APP_SECRET"]
        _url    = _KIS_URL_PRICE
        _res    = _requests.get(_url, headers={
            "authorization": f"Bearer {_token}",
            "appkey":        _key,
            "appsecret":     _secret,
            "tr_id":         "FHKST01010100",
        }, params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker}, timeout=5)
        _data = _res.json().get("output", {})
        if _data:
            return {
                "현재가":    int(_data.get("stck_prpr", 0)),
                "전일대비":  int(_data.get("prdy_vrss", 0)),
                "등락률":    float(_data.get("prdy_ctrt", 0)),
                "거래량":    int(_data.get("acml_vol", 0)),
                "고가":      int(_data.get("stck_hgpr", 0)),
                "저가":      int(_data.get("stck_lwpr", 0)),
                "시가":      int(_data.get("stck_oprc", 0)),
                "52주고가":  int(_data.get("d250_hgpr", 0)),
                "52주저가":  int(_data.get("d250_lwpr", 0)),
                "PER":       float(_data.get("per", 0)),
                "PBR":       float(_data.get("pbr", 0)),
            }
    except Exception as _e:
        pass
    return None

def kis_get_balance():
    """KIS API 실제 잔고 조회"""
    try:
        _token  = kis_get_token()
        if not _token: return None
        _key    = st.secrets["KIS_APP_KEY"]
        _secret = st.secrets["KIS_APP_SECRET"]
        _acc_no = st.secrets["KIS_ACCOUNT_NO"]
        _acc_pd = st.secrets.get("KIS_ACCOUNT_PD", "01")
        _url    = _KIS_URL_BALANCE
        _res    = _requests.get(_url, headers={
            "authorization": f"Bearer {_token}",
            "appkey":        _key,
            "appsecret":     _secret,
            "tr_id":         "TTTC8434R",
        }, params={
            "CANO":            _acc_no,
            "ACNT_PRDT_CD":    _acc_pd,
            "AFHR_FLPR_YN":    "N",
            "OFL_YN":          "",
            "INQR_DVSN":       "02",
            "UNPR_DVSN":       "01",
            "FUND_STTL_ICLD_YN":"N",
            "FNCG_AMT_AUTO_RDPT_YN":"N",
            "PRCS_DVSN":       "01",
            "CTX_AREA_FK100":  "",
            "CTX_AREA_NK100":  ""
        }, timeout=10)
        _d = _res.json()
        _holdings = []
        for _h in _d.get("output1", []):
            if int(_h.get("hldg_qty", 0)) > 0:
                _holdings.append({
                    "종목코드": _h.get("pdno"),
                    "종목명":   _h.get("prdt_name"),
                    "수량":     int(_h.get("hldg_qty", 0)),
                    "평단가":   int(float(_h.get("pchs_avg_pric", 0))),
                    "현재가":   int(_h.get("prpr", 0)),
                    "평가손익": int(_h.get("evlu_pfls_amt", 0)),
                    "수익률":   float(_h.get("evlu_pfls_rt", 0)),
                    "평가금액": int(_h.get("evlu_amt", 0)),
                })
        _summary = _d.get("output2", [{}])[0] if _d.get("output2") else {}
        return {
            "holdings": _holdings,
            "현금":      int(float(_summary.get("dnca_tot_amt", 0))),
            "총평가":    int(float(_summary.get("tot_evlu_amt", 0))),
            "총손익":    int(float(_summary.get("evlu_pfls_smtl_amt", 0))),
            "수익률":    float(_summary.get("tot_evlu_pfls_rt", 0)),
        }
    except Exception as _e:
        return None

def kis_get_investor(ticker):
    """외인/기관 순매수 조회"""
    try:
        _token  = kis_get_token()
        if not _token: return None
        _key    = st.secrets["KIS_APP_KEY"]
        _secret = st.secrets["KIS_APP_SECRET"]
        _url    = _KIS_URL_INVESTOR
        _res    = _requests.get(_url, headers={
            "authorization": f"Bearer {_token}",
            "appkey":        _key,
            "appsecret":     _secret,
            "tr_id":         "FHKST01010900",
        }, params={
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd":         ticker
        }, timeout=5)
        _out = _res.json().get("output", [])
        if _out:
            _latest = _out[0]
            return {
                "외인순매수":  int(_latest.get("frgn_ntby_qty", 0)),
                "기관순매수":  int(_latest.get("orgn_ntby_qty", 0)),
                "개인순매수":  int(_latest.get("prsn_ntby_qty", 0)),
            }
    except:
        pass
    return None

def kis_available():
    """KIS API 사용 가능 여부 확인"""
    try:
        _keys = ["KIS_APP_KEY","KIS_APP_SECRET","KIS_ACCOUNT_NO"]
        return all(k in st.secrets for k in _keys)
    except:
        return False

def kis_debug_info():
    """KIS 키 등록 현황 확인"""
    try:
        _found = [k for k in ["KIS_APP_KEY","KIS_APP_SECRET","KIS_ACCOUNT_NO","KIS_ACCOUNT_PD","KIS_MODE"] if k in st.secrets]
        _missing = [k for k in ["KIS_APP_KEY","KIS_APP_SECRET","KIS_ACCOUNT_NO"] if k not in st.secrets]
        return _found, _missing
    except Exception as _e:
        return [], [str(_e)]

# ══════════════════════════════════════════
# V8.9.1 하드 서킷 브레이커 & 방어 모듈
# ══════════════════════════════════════════

MACRO_EVENTS_1TIER = [
    # "2026-06-18",  # FOMC 예시 (대시보드 홈탭에서 UI로 관리)
]

def get_macro_events():
    """session_state + 하드코딩 이벤트 통합"""
    _ui_events = st.session_state.get('macro_events', [])
    return MACRO_EVENTS_1TIER + _ui_events

def check_macro_blackout():
    from datetime import datetime
    _now    = datetime.now()
    _events = get_macro_events()
    for _ev_item in _events:
        try:
            # {"date": "2026-06-18", "name": "FOMC"} 또는 "2026-06-18" 형식 지원
            if isinstance(_ev_item, dict):
                _ev_date = _ev_item.get('date','')
                _ev_name = _ev_item.get('name','이벤트')
            else:
                _ev_date = str(_ev_item)
                _ev_name = '이벤트'
            _ev_dt = datetime.strptime(_ev_date, "%Y-%m-%d")
            _diff  = abs((_now - _ev_dt).total_seconds() / 3600)
            if _diff <= 48:
                return True, f"🚫 매크로 블랙아웃 — {_ev_name}({_ev_date}) {_diff:.0f}시간 이내 (OBSERVE_ONLY)"
        except:
            pass
    return False, ""

def check_index_shutdown():
    try:
        import yfinance as yf
        _results = {}
        for _name, _sym in [("코스피","^KS11"), ("코스닥","^KQ11")]:
            _h = yf.Ticker(_sym).history(period="2d", interval="1d")
            if len(_h) >= 2:
                _chg = (_h['Close'].iloc[-1] / _h['Close'].iloc[-2] - 1) * 100
                _results[_name] = round(_chg, 2)
        _kospi_chg  = _results.get("코스피", 0)
        _kosdaq_chg = _results.get("코스닥", 0)
        if _kospi_chg <= -2.0 or _kosdaq_chg <= -2.0:
            _reason = (
                f"🚨 지수 셧다운 — 코스피 {_kospi_chg:+.2f}% / 코스닥 {_kosdaq_chg:+.2f}% "
                f"(-2.0% 급락) | 개별 지지선 무효 / 신규 매수 차단"
            )
            return True, _reason, _kospi_chg, _kosdaq_chg
        return False, "", _kospi_chg, _kosdaq_chg
    except Exception as _e:
        return False, f"지수 조회 오류: {_e}", 0, 0

def check_smart_killswitch(ticker, entry_price, current_price):
    if entry_price <= 0:
        return 'SAFE', ""
    _chg_pct = (current_price - entry_price) / entry_price * 100
    if _chg_pct <= -10.0:
        return 'EXECUTE_MARKET_SELL', (
            f"🚨 하드 서킷 브레이커! 진입가 {entry_price:,.0f} 대비 {_chg_pct:.2f}% (-10%) → EXECUTE_MARKET_SELL"
        )
    if _chg_pct <= -7.0:
        try:
            import yfinance as yf
            _is_korean = ticker.isdigit() and len(ticker) == 6
            _sym = f"{ticker}.KS" if _is_korean else ticker
            _df  = yf.Ticker(_sym).history(period="10d", interval="1d")
            if _df is not None and len(_df) >= 6:
                _vol_today = _df['Volume'].iloc[-1]
                _vol_5d    = _df['Volume'].iloc[-6:-1].mean()
                _vol_ratio = _vol_today / _vol_5d if _vol_5d > 0 else 1.0
                if _vol_ratio < 0.5:
                    return 'HOLD_AND_VERIFY_1HR', (
                        f"⚠️ 스마트 킬스위치 — {_chg_pct:.2f}% (거래량 {_vol_ratio*100:.0f}% — 투매 아님) → HOLD_AND_VERIFY_1HR"
                    )
                else:
                    return 'EXECUTE_MARKET_SELL', (
                        f"🚨 킬스위치 — {_chg_pct:.2f}% (거래량 {_vol_ratio*100:.0f}% — 실제 투매) → EXECUTE_MARKET_SELL"
                    )
        except:
            pass
        return 'EXECUTE_MARKET_SELL', f"🚨 킬스위치 — {_chg_pct:.2f}% → EXECUTE_MARKET_SELL"
    return 'SAFE', ""

def run_v891_system_check(ticker="", entry_price=0, current_price=0):
    _alerts = []; _can_enter = True; _killswitch = 'SAFE'
    _bo, _bo_msg = check_macro_blackout()
    if _bo:
        _can_enter = False
        _alerts.append(_bo_msg)
    _sd, _sd_msg, _kospi_chg, _kosdaq_chg = check_index_shutdown()
    if _sd:
        _can_enter = False
        _alerts.append(_sd_msg)
    if ticker and entry_price > 0 and current_price > 0:
        _ks_action, _ks_msg = check_smart_killswitch(ticker, entry_price, current_price)
        _killswitch = _ks_action
        if _ks_action != 'SAFE':
            _alerts.append(_ks_msg)
    return {
        'can_enter':  _can_enter,
        'killswitch': _killswitch,
        'alerts':     _alerts,
        'blackout':   _bo,
        'shutdown':   _sd,
        'kospi_chg':  _kospi_chg,
        'kosdaq_chg': _kosdaq_chg,
    }

# ══════════════════════════════════════════
# Google Sheets 공통 인증 헬퍼
# ══════════════════════════════════════════

_GS_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

@st.cache_resource(show_spinner=False)
@st.cache_resource
def _get_gspread_workbook():
    """인증 + 워크북 연결 — 앱 전체에서 1회만 실행"""
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=_GS_SCOPES
    )
    return gspread.authorize(creds).open_by_key(st.secrets["SHEET_ID"])

def get_gsheet():
    """관심종목 시트 (sheet1)"""
    return _get_gspread_workbook().sheet1

# ══════════════════════════════════════════
# 페이퍼 트레이딩 백엔드
# ══════════════════════════════════════════

def get_trading_sheet():
    """거래 일지 시트 (trading_log) — 없으면 자동 생성"""
    try:
        sh = _get_gspread_workbook()
        try:
            return sh.worksheet("trading_log")
        except Exception:
            ws = sh.add_worksheet("trading_log", rows=1000, cols=20)
            ws.append_row(["날짜","시간","종목코드","종목명","매매","수량",
                           "체결단가","수수료","슬리피지","순체결가",
                           "잔고","평가금액","5AI점수","ADX","Z-Score","메모"])
            return ws
    except Exception:
        return None

def get_account_sheet():
    """가상 계좌 시트 (account) — 없으면 자동 생성"""
    try:
        sh = _get_gspread_workbook()
        try:
            return sh.worksheet("account")
        except Exception:
            ws = sh.add_worksheet("account", rows=100, cols=10)
            ws.append_row(["초기자본","현금잔고","보유종목JSON","최고자산","최저자산"])
            ws.append_row([10000000, 10000000, "[]", 10000000, 10000000])
            return ws
    except Exception:
        return None

def _safe_json(s, default=None):
    """JSON 파싱 실패 시 default 반환"""
    if default is None:
        default = []
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

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
                'positions':  _safe_json(row[2]),
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

def _parse_watchlist(wl):
    """watchlist 문자열 → [(ticker, name), ...] 파싱"""
    result = []
    for line in wl.strip().split("\n"):
        parts = line.strip().split(",", 1)
        if len(parts) == 2 and parts[0].strip():
            result.append((parts[0].strip(), parts[1].strip()))
    return result

def _pairs_to_text(pairs):
    return "\n".join(f"{t},{n}" for t, n in pairs)

def load_watchlist():
    """Google Sheets에서 관심종목 로드"""
    try:
        ws = get_gsheet()
        data = ws.get_all_values()
        if data:
            return "\n".join([",".join(row[:2]) for row in data if len(row) >= 2 and row[0].strip()])
    except Exception:
        pass
    return DEFAULT_WATCHLIST

def get_watchlist():
    """★ 관심종목 표준 로드 함수"""
    _wl = st.session_state.get('watchlist_data', '')
    if _wl:
        return _wl
    _wl = load_watchlist()
    st.session_state.watchlist_data = _wl
    return _wl

def safe_clear_cache():
    """watchlist session_state 초기화 → 다음 호출 시 Sheets에서 재로드"""
    st.session_state.pop('watchlist_data', None)

def save_watchlist(text):
    """관심종목 전체 저장 — session_state + Sheets 동시 저장"""
    st.session_state.watchlist_data = text
    try:
        ws = get_gsheet()
        rows = [[p.strip() for p in l.split(",", 1)]
                for l in text.strip().split("\n")
                if "," in l and l.strip()]
        ws.clear()
        if rows:
            ws.update("A1", rows)
    except Exception as _e:
        st.warning(f"⚠️ Sheets 저장 오류 (앱은 정상): {_e}")

def get_watchlist_tickers():
    return _parse_watchlist(get_watchlist())

def add_ticker(ticker, name):
    """관심종목 1개 추가 — append_row로 안전하게"""
    wl = get_watchlist()
    existing = [t for t, _ in _parse_watchlist(wl)]
    if ticker in existing:
        return False
    # session_state 즉시 반영
    st.session_state.watchlist_data = wl.strip() + f"\n{ticker},{name}"
    # Sheets에 한 줄 추가 (clear 없이 안전)
    try:
        get_gsheet().append_row([ticker, name], value_input_option="RAW")
    except Exception as _e:
        st.warning(f"⚠️ Sheets 저장 오류 (앱은 정상): {_e}")
    return True

def remove_ticker_from_sheets(text):
    """삭제 후 Sheets 전체 갱신"""
    try:
        ws = get_gsheet()
        rows = [[p.strip() for p in l.split(",", 1)]
                for l in text.strip().split("\n")
                if "," in l and l.strip()]
        ws.clear()
        if rows:
            ws.update("A1", rows)
    except Exception as _e:
        st.warning(f"⚠️ Sheets 저장 오류 (앱은 정상): {_e}")

def clean_sheet_duplicates():
    """중복 제거"""
    wl = get_watchlist()
    seen = set(); clean = []
    for t, n in _parse_watchlist(wl):
        if t not in seen:
            seen.add(t); clean.append((t, n))
    result = _pairs_to_text(clean)
    save_watchlist(result)
    return result

def remove_ticker(ticker):
    pairs = _parse_watchlist(get_watchlist())
    new_text = "\n".join(f"{t},{n}" for t, n in pairs if t != ticker)
    st.session_state.watchlist_data = new_text
    remove_ticker_from_sheets(new_text)

# session_state 초기화
if 'passed' not in st.session_state:
    st.session_state.passed = None
if 'watchlist_data' not in st.session_state:
    st.session_state.watchlist_data = DEFAULT_WATCHLIST

# ── 스타일 (반응형 — Desktop / Mobile) ──
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap');

/* ══════════════════════════════════════
   CSS 변수 (테마 토큰)
══════════════════════════════════════ */
:root {
    --bg-base:    #0d1117;
    --bg-card:    rgba(255,255,255,0.04);
    --bg-sidebar: #090d18;
    --border:     rgba(255,255,255,0.07);
    --accent:     #6366f1;
    --accent2:    #8b5cf6;
    --text-pri:   #e2e8f0;
    --text-sec:   #94a3b8;
    --text-dim:   #475569;
    --up:         #f43f5e;
    --down:       #38bdf8;
    --green:      #34d399;
    --font-body:  'Noto Sans KR', sans-serif;
    --font-mono:  'IBM Plex Mono', monospace;
    /* 데스크톱 기본값 */
    --fs-xs:   11px;
    --fs-sm:   13px;
    --fs-md:   15px;
    --fs-lg:   18px;
    --fs-xl:   24px;
    --fs-2xl:  32px;
    --card-pad: 18px 22px;
    --radius:   14px;
}

/* ══════════════════════════════════════
   전역
══════════════════════════════════════ */
html, body, [class*="css"] {
    font-family: var(--font-body);
    background-color: var(--bg-base);
    color: var(--text-pri);
    font-size: var(--fs-md);
    line-height: 1.6;
}
.stApp {
    background: linear-gradient(160deg, #0d1117 0%, #0f172a 60%, #0d1117 100%);
}

/* ══════════════════════════════════════
   사이드바
══════════════════════════════════════ */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, var(--bg-sidebar) 0%, var(--bg-base) 100%);
    border-right: 1px solid var(--border);
}
[data-testid="stSidebar"] * { font-size: var(--fs-sm) !important; }
[data-testid="stSidebar"] h2 { font-size: var(--fs-md) !important; }

/* ══════════════════════════════════════
   탭
══════════════════════════════════════ */
.stTabs [data-baseweb="tab-list"] {
    background: rgba(255,255,255,0.03);
    border-radius: var(--radius);
    padding: 4px;
    border: 1px solid var(--border);
    gap: 3px;
    flex-wrap: wrap;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 10px;
    color: var(--text-sec);
    font-weight: 600;
    font-size: var(--fs-sm);
    padding: 8px 18px;
    transition: all 0.2s;
    white-space: nowrap;
}
.stTabs [aria-selected="true"] {
    background: linear-gradient(135deg, var(--accent), var(--accent2)) !important;
    color: #fff !important;
    box-shadow: 0 3px 12px rgba(99,102,241,0.4);
}

/* ══════════════════════════════════════
   메트릭 카드
══════════════════════════════════════ */
.metric-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: var(--card-pad);
    margin-bottom: 10px;
    backdrop-filter: blur(8px);
    transition: border-color 0.2s, transform 0.15s;
}
.metric-card:hover {
    border-color: rgba(99,102,241,0.35);
    transform: translateY(-1px);
}
.metric-card .label {
    font-size: var(--fs-xs);
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 1.2px;
    font-family: var(--font-mono);
}
.metric-card .value {
    font-size: var(--fs-xl);
    font-weight: 700;
    font-family: var(--font-mono);
    margin-top: 5px;
    letter-spacing: -0.5px;
    color: var(--text-pri);
}
.metric-card .delta {
    font-size: var(--fs-sm);
    font-family: var(--font-mono);
    margin-top: 2px;
}

/* ══════════════════════════════════════
   색상 유틸
══════════════════════════════════════ */
.up   { color: var(--up); }
.down { color: var(--down); }
.flat { color: var(--text-sec); }

/* ══════════════════════════════════════
   뱃지
══════════════════════════════════════ */
.badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.3px;
    margin: 2px;
    font-family: var(--font-mono);
}
.badge-buy     { background: rgba(52,211,153,0.12); color: var(--green);  border: 1px solid rgba(52,211,153,0.25); }
.badge-sell    { background: rgba(244,63,94,0.12);  color: var(--up);     border: 1px solid rgba(244,63,94,0.25); }
.badge-watch   { background: rgba(56,189,248,0.12); color: var(--down);   border: 1px solid rgba(56,189,248,0.25); }
.badge-neutral { background: rgba(148,163,184,0.08); color: var(--text-sec); border: 1px solid rgba(148,163,184,0.18); }

/* ══════════════════════════════════════
   Gemini 결과 박스
══════════════════════════════════════ */
.gemini-box {
    background: linear-gradient(135deg, rgba(99,102,241,0.07), rgba(139,92,246,0.04));
    border-left: 3px solid var(--accent);
    border-top: 1px solid rgba(99,102,241,0.18);
    border-right: 1px solid rgba(99,102,241,0.08);
    border-bottom: 1px solid rgba(99,102,241,0.08);
    border-radius: 0 var(--radius) var(--radius) 0;
    padding: 16px 20px;
    font-size: var(--fs-md);
    line-height: 1.85;
    white-space: pre-wrap;
}

/* ══════════════════════════════════════
   버튼
══════════════════════════════════════ */
.stButton > button {
    border-radius: 10px !important;
    font-weight: 600 !important;
    font-size: var(--fs-sm) !important;
    padding: 8px 16px !important;
    transition: all 0.2s !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, var(--accent), var(--accent2)) !important;
    border: none !important;
    box-shadow: 0 3px 12px rgba(99,102,241,0.3) !important;
    color: #fff !important;
}
.stButton > button[kind="primary"]:hover {
    box-shadow: 0 5px 18px rgba(99,102,241,0.5) !important;
    transform: translateY(-1px) !important;
}
.stButton > button[kind="secondary"] {
    background: rgba(255,255,255,0.05) !important;
    border: 1px solid var(--border) !important;
    color: var(--text-sec) !important;
}
.stButton > button[kind="secondary"]:hover {
    background: rgba(255,255,255,0.09) !important;
    color: var(--text-pri) !important;
}

/* ══════════════════════════════════════
   입력 필드
══════════════════════════════════════ */
.stTextInput input, .stNumberInput input,
.stSelectbox select, textarea {
    background: rgba(255,255,255,0.04) !important;
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    color: var(--text-pri) !important;
    font-size: var(--fs-sm) !important;
}
.stTextInput input:focus, .stNumberInput input:focus, textarea:focus {
    border-color: rgba(99,102,241,0.5) !important;
    box-shadow: 0 0 0 2px rgba(99,102,241,0.15) !important;
}

/* ══════════════════════════════════════
   Expander
══════════════════════════════════════ */
.streamlit-expanderHeader {
    background: rgba(255,255,255,0.03) !important;
    border-radius: 10px !important;
    border: 1px solid var(--border) !important;
    color: var(--text-pri) !important;
    font-size: var(--fs-sm) !important;
}

/* ══════════════════════════════════════
   제목 / 구분선 / 테이블
══════════════════════════════════════ */
h1 {
    background: linear-gradient(135deg, #f0f4ff, #a5b4fc);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    font-weight: 800; font-size: var(--fs-2xl) !important;
}
h2 { color: var(--text-pri); font-weight: 700; font-size: var(--fs-xl) !important; }
h3 { color: #e2e8f0; font-weight: 700; font-size: var(--fs-lg) !important; }
h4 { color: var(--text-sec); font-weight: 600; font-size: var(--fs-md) !important; }
hr { border-color: var(--border); margin: 12px 0; }
.stDataFrame { border-radius: var(--radius) !important; border: 1px solid var(--border) !important; }
.stAlert { border-radius: 10px !important; font-size: var(--fs-sm) !important; }
p, li { font-size: var(--fs-md); }
caption, .stCaption { font-size: var(--fs-xs) !important; color: var(--text-dim) !important; }

/* ══════════════════════════════════════
   모바일 반응형  ≤ 768px
══════════════════════════════════════ */
@media (max-width: 768px) {
    :root {
        --fs-xs:  10px;
        --fs-sm:  12px;
        --fs-md:  13px;
        --fs-lg:  15px;
        --fs-xl:  19px;
        --fs-2xl: 22px;
        --card-pad: 12px 14px;
        --radius: 10px;
    }
    /* 탭 — 아이콘만 보이도록 축소 */
    .stTabs [data-baseweb="tab"] {
        padding: 7px 10px !important;
        font-size: 11px !important;
    }
    /* 사이드바 숨김 처리 (접을 수 있음) */
    [data-testid="stSidebar"] * { font-size: 11px !important; }
    /* 버튼 풀너비 패딩 줄임 */
    .stButton > button { padding: 7px 10px !important; font-size: 11px !important; }
    /* 데이터프레임 스크롤 */
    .stDataFrame { font-size: 11px !important; }
    /* 카드 수치 크기 */
    .metric-card .value { font-size: var(--fs-xl); }
}

/* ══════════════════════════════════════
   태블릿  769px ~ 1024px
══════════════════════════════════════ */
@media (min-width: 769px) and (max-width: 1024px) {
    :root {
        --fs-sm:  12px;
        --fs-md:  14px;
        --fs-lg:  16px;
        --fs-xl:  21px;
        --card-pad: 14px 18px;
    }
}

/* ══════════════════════════════════════
   대형 모니터  ≥ 1440px
══════════════════════════════════════ */
@media (min-width: 1440px) {
    :root {
        --fs-sm:  14px;
        --fs-md:  16px;
        --fs-lg:  20px;
        --fs-xl:  26px;
        --fs-2xl: 36px;
        --card-pad: 22px 28px;
    }
}
</style>""", unsafe_allow_html=True)


# ══════════════════════════════════════════
# 데이터 함수
# ══════════════════════════════════════════

@st.cache_data(ttl=300, show_spinner=False)
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

def calc_entry_point(df, preset=None):
    """
    프리셋별 진입 타점 자동 계산
    규칙: entry < cur (매수 타점은 항상 현재가 아래)
          stoploss < entry (손절가는 항상 매수가 아래)
          target1 > entry (목표가는 항상 매수가 위)
    """
    import numpy as np
    l   = df.iloc[-1]
    cur = float(l['종가'])

    ma5   = float(l['MA5'])
    ma20  = float(l['MA20'])
    ma60  = float(l['MA60'])
    bb_lo = float(l['BB_lower'])
    bb_mi = float((l['BB_upper'] + l['BB_lower']) / 2)
    bb_hi = float(l['BB_upper'])

    # 지지선 후보 — 반드시 현재가 아래
    _sup_cands = sorted(
        [v for v in [ma20, ma60, bb_lo,
                     float(df['저가'].tail(20).nsmallest(3).mean())]
         if v < cur * 0.999],
        reverse=True
    )
    support = _sup_cands[0] if _sup_cands else cur * 0.93

    # 저항선 후보 — 반드시 현재가 위
    _res_cands = sorted(
        [v for v in [bb_hi,
                     float(df['고가'].tail(20).nlargest(3).mean())]
         if v > cur * 1.001]
    )
    resist = _res_cands[0] if _res_cands else cur * 1.10

    if preset == 'bounce':
        _cands = [v for v in [bb_lo, ma20, support] if v < cur * 0.998]
        entry   = round(max(_cands) * 1.003) if _cands else round(cur * 0.96)
        reason  = f"BB하단({bb_lo:,.0f}) 반등 눌림목 대기"
        target1 = round(max(bb_mi, entry * 1.07))
        target2 = round(max(resist, entry * 1.14))

    elif preset == 'trend':
        _cands = [v for v in [ma5, ma20] if v < cur * 0.998]
        entry   = round(max(_cands) * 1.003) if _cands else round(cur * 0.97)
        reason  = f"MA20({ma20:,.0f}) 눌림목 대기"
        target1 = round(max(resist, entry * 1.08))
        target2 = round(max(resist * 1.08, entry * 1.15))

    elif preset == 'bottom':
        entry   = round(bb_lo * 1.005)
        reason  = f"BB하단({bb_lo:,.0f}) 바닥 확인 진입"
        target1 = round(max(bb_mi, entry * 1.07))
        target2 = round(max(bb_hi, entry * 1.14))

    else:
        entry   = round(support * 1.005)
        reason  = f"지지선({support:,.0f}) 기준"
        target1 = round(max(resist, entry * 1.08))
        target2 = round(max(resist * 1.08, entry * 1.15))

    # ── 안전 검증 ──
    # 1. entry가 현재가 이상이면 강제로 낮춤
    if entry >= cur:
        entry  = round(cur * 0.97)
        reason += " (현재가 근접 → 3% 눌림 대기)"

    # 2. stoploss = entry × 0.93 (항상 entry 아래)
    stoploss = round(entry * 0.93)

    # 3. target1이 entry 이하면 강제로 높임
    if target1 <= entry:
        target1 = round(entry * 1.08)
    if target2 <= target1:
        target2 = round(target1 * 1.07)

    # 4. 최종 안전 클램프 (엣지케이스 방어)
    if not (stoploss < entry < cur):
        entry    = round(cur * 0.97)
        stoploss = round(entry * 0.93)
        reason  += " (안전클램프 적용)"
    if target1 <= entry:
        target1 = round(entry * 1.08)
    if target2 <= target1:
        target2 = round(target1 * 1.07)

    risk   = entry - stoploss
    reward = target1 - entry
    rr     = round(reward / risk, 2) if risk > 0 else 0

    return {
        'cur':      round(cur),
        'entry':    entry,
        'stoploss': stoploss,
        'target1':  target1,
        'target2':  target2,
        'reason':   reason,
        'rr':       rr,
        'gap_pct':  round((entry - cur) / cur * 100, 1),  # 현재가 대비 진입가 차이
    }

def make_chart(df, name, entry=None, stoploss=None, target1=None, target2=None):
    import pandas as pd

    fig = make_subplots(
        rows=5, cols=1,
        shared_xaxes=True,
        row_heights=[0.42, 0.12, 0.15, 0.15, 0.16],
        vertical_spacing=0.02,
        subplot_titles=('', '거래량', 'MACD', 'RSI', 'OBV')
    )

    # ── 날짜 포맷 (1월, 2월 표기) ──
    _idx = df.index
    if hasattr(_idx, 'strftime'):
        try:
            _tick_vals = []
            _tick_text = []
            _seen = set()
            for _d in _idx:
                _ym = (_d.year, _d.month)
                if _ym not in _seen:
                    _seen.add(_ym)
                    _tick_vals.append(_d)
                    _tick_text.append(f"{_d.month}월")
        except:
            _tick_vals = None
            _tick_text = None
    else:
        _tick_vals = None
        _tick_text = None

    # ── 캔들 ──
    fig.add_trace(go.Candlestick(
        x=_idx, open=df['시가'], high=df['고가'],
        low=df['저가'], close=df['종가'],
        increasing_line_color='#ff4d6d', decreasing_line_color='#4da6ff',
        increasing_fillcolor='#ff4d6d', decreasing_fillcolor='#4da6ff',
        name='캔들', showlegend=False
    ), row=1, col=1)

    # ── 현재가 라인 ──
    _cur_price = df['종가'].iloc[-1]
    _prev_price = df['종가'].iloc[-2]
    _cur_color = '#ff4d6d' if _cur_price >= _prev_price else '#4da6ff'
    # 현재가 — scatter로 마지막 점에 표시
    fig.add_trace(go.Scatter(
        x=[_idx[-1]], y=[_cur_price],
        mode='markers+text',
        marker=dict(color=_cur_color, size=8, symbol='circle'),
        text=[f' ◀ 현재가 {_cur_price:,.0f}'],
        textposition='middle right',
        textfont=dict(color=_cur_color, size=11),
        name='현재가', showlegend=False
    ), row=1, col=1)

    # ── 이평선 ──
    ma_colors = {'MA5':'#ffd166','MA20':'#06d6a0','MA60':'#a78bfa','MA120':'#38bdf8'}
    for ma, c in ma_colors.items():
        if ma in df.columns:
            fig.add_trace(go.Scatter(x=_idx, y=df[ma], line=dict(color=c, width=1.2),
                                     name=ma, opacity=0.85), row=1, col=1)

    # ── 볼린저밴드 ──
    fig.add_trace(go.Scatter(x=_idx, y=df['BB_upper'],
                             line=dict(color='#475569', width=0.8, dash='dash'),
                             name='BB상단', showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=_idx, y=df['BB_lower'],
                             line=dict(color='#475569', width=0.8, dash='dash'),
                             fill='tonexty', fillcolor='rgba(71,85,105,0.08)',
                             name='BB하단', showlegend=False), row=1, col=1)

    # ── 지지/저항선 ──
    if '지지선' in df.columns:
        fig.add_trace(go.Scatter(x=_idx, y=df['지지선'],
                                 line=dict(color='#4dff91', width=0.8, dash='dot'),
                                 name='지지선', opacity=0.6), row=1, col=1)
    if '저항선' in df.columns:
        fig.add_trace(go.Scatter(x=_idx, y=df['저항선'],
                                 line=dict(color='#ff6b6b', width=0.8, dash='dot'),
                                 name='저항선', opacity=0.6), row=1, col=1)

    # ── 매수/손절/목표가 라인 ──
    if entry:
        fig.add_hline(y=entry, line_color='#ffd166', line_width=2.0,
                      annotation_text=f'🎯매수 {entry:,.0f}',
                      annotation_position='right',
                      annotation_font_color='#ffd166',
                      annotation_font_size=12,
                      row=1, col=1)
    if stoploss:
        fig.add_hline(y=stoploss, line_color='#ff4d6d', line_width=2.0, line_dash='dash',
                      annotation_text=f'🛑손절 {stoploss:,.0f}(-7%)',
                      annotation_position='right',
                      annotation_font_color='#ff4d6d',
                      annotation_font_size=12,
                      row=1, col=1)
    if target1:
        fig.add_hline(y=target1, line_color='#4dff91', line_width=2.0,
                      annotation_text=f'🎯1차목표 {target1:,.0f}',
                      annotation_position='right',
                      annotation_font_color='#4dff91',
                      annotation_font_size=12,
                      row=1, col=1)
    if target2:
        fig.add_hline(y=target2, line_color='#ffd166', line_width=1.8, line_dash='dot',
                      annotation_text=f'✨2차목표 {target2:,.0f}(+20%)',
                      annotation_position='right',
                      annotation_font_color='#ffd166',
                      annotation_font_size=12,
                      row=1, col=1)

    # ── 거래량 ──
    colors_vol = ['#ff4d6d' if df['종가'].iloc[i] >= df['시가'].iloc[i] else '#4da6ff'
                  for i in range(len(df))]
    fig.add_trace(go.Bar(x=_idx, y=df['거래량'], marker_color=colors_vol,
                         opacity=0.7, name='거래량', showlegend=False), row=2, col=1)

    # ── MACD ──
    hist_colors = ['#ff4d6d' if v >= 0 else '#4da6ff' for v in df['MACD_hist']]
    fig.add_trace(go.Bar(x=_idx, y=df['MACD_hist'], marker_color=hist_colors,
                         opacity=0.6, name='히스토그램', showlegend=False), row=3, col=1)
    fig.add_trace(go.Scatter(x=_idx, y=df['MACD'],
                             line=dict(color='#38bdf8', width=1.2), name='MACD'), row=3, col=1)
    fig.add_trace(go.Scatter(x=_idx, y=df['Signal'],
                             line=dict(color='#f472b6', width=1.2), name='Signal'), row=3, col=1)
    fig.add_hline(y=0, line_color='#2d3a55', line_width=0.5, row=3, col=1)

    # ── RSI ──
    fig.add_trace(go.Scatter(x=_idx, y=df['RSI'],
                             line=dict(color='#a78bfa', width=1.5),
                             name='RSI', showlegend=False), row=4, col=1)
    fig.add_hline(y=70, line_dash='dash', line_color='#ff4d6d', line_width=0.8, row=4, col=1)
    fig.add_hline(y=30, line_dash='dash', line_color='#4da6ff', line_width=0.8, row=4, col=1)
    fig.add_hrect(y0=70, y1=100, fillcolor='rgba(255,77,109,0.05)', line_width=0, row=4, col=1)
    fig.add_hrect(y0=0,  y1=30,  fillcolor='rgba(77,166,255,0.05)', line_width=0, row=4, col=1)

    # ── OBV ──
    if 'OBV' in df.columns:
        obv_color = '#4dff91' if df['OBV'].iloc[-1] > df['OBV'].iloc[-2] else '#ff6b6b'
        fig.add_trace(go.Scatter(x=_idx, y=df['OBV'],
                                 line=dict(color=obv_color, width=1.3),
                                 name='OBV', showlegend=False), row=5, col=1)
        if 'OBV_MA' in df.columns:
            fig.add_trace(go.Scatter(x=_idx, y=df['OBV_MA'],
                                     line=dict(color='#ffd166', width=1.0, dash='dash'),
                                     name='OBV MA9', showlegend=False), row=5, col=1)

    # ── 레이아웃 ──
    fig.update_layout(
        title=dict(text=f'<b>{name}</b>', font=dict(size=16, color='#e0e6f0'), x=0.01),
        paper_bgcolor='#0d1117', plot_bgcolor='#0f1723',
        font=dict(color='#8899bb', size=11),
        xaxis_rangeslider_visible=False,
        height=820,
        legend=dict(orientation='h', y=1.02, x=0, font=dict(size=10),
                    bgcolor='rgba(0,0,0,0)', bordercolor='rgba(0,0,0,0)'),
        margin=dict(l=10, r=160, t=50, b=30),
    )

    # ── Y축 오른쪽 + X축 날짜 포맷 ──
    for i in range(1, 6):
        fig.update_xaxes(
            gridcolor='#1a2535', row=i, col=1, showgrid=True,
            tickvals=_tick_vals if _tick_vals else None,
            ticktext=_tick_text if _tick_text else None,
            tickfont=dict(size=10),
        )
        fig.update_yaxes(
            gridcolor='#1a2535', row=i, col=1, showgrid=True,
            tickfont=dict(family='IBM Plex Mono', size=10),
            side='right',  # Y축 오른쪽
        )
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

    # 사이드바 — session_state 우선
    _sb_wl = get_watchlist()
    _sb_lines = [l.strip() for l in _sb_wl.split("\n") if "," in l.strip()]
    _sb_pairs = [l.split(",", 1) for l in _sb_lines if len(l.split(",", 1)) == 2]

    for _t, _n in _sb_pairs:
        _t = _t.strip(); _n = _n.strip()
        _sc1, _sc2 = st.columns([3, 1])
        _sc1.markdown(f"<div style='font-size:12px; padding:4px 0'><b>{_n}</b><br><span style='color:#64748b; font-size:10px'>{_t}</span></div>", unsafe_allow_html=True)
        if _sc2.button("✕", key=f"sb_del_{_t}"):
            _new_lines = [l for l in _sb_lines if not l.startswith(_t + ",")]
            _new_text = "\n".join(_new_lines)
            st.session_state.watchlist_data = _new_text
            remove_ticker_from_sheets(_new_text)
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
                    safe_clear_cache()
                    st.rerun()
                except Exception as _e:
                    st.error(f"오류: {_e}")
            else:
                st.warning("이미 있는 종목")
        else:
            st.warning("코드와 이름 입력")

    n = len(_sb_pairs)
    st.markdown(f"<div style='font-size:11px; color:#34d399'>✅ 총 {n}개 종목</div>", unsafe_allow_html=True)

    lookback = st.slider("분석 기간 (거래일)", 30, 120, 60)

    model_name = st.selectbox("Gemini 모델", [
        "models/gemini-2.5-flash",
        "models/gemini-2.5-pro",
        "models/gemini-2.0-flash",
        "models/gemini-3.1-pro-preview",
    ])

    st.markdown(f"<div style='font-size:10px; color:#64748b; text-align:center'>마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}</div>", unsafe_allow_html=True)
    refresh = st.button("🔄 강제 새로고침", use_container_width=True)
    if refresh:
        st.cache_data.clear()
        st.success("캐시 초기화 완료!")
        import time; time.sleep(0.5)
        st.rerun()

    st.markdown("---")
    st.markdown("""
    <div style='font-size:11px; color:#64748b; line-height:1.8'>
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

TICKERS = get_watchlist_tickers()


# ══════════════════════════════════════════
# 메인
# ══════════════════════════════════════════

st.markdown("""
<div style='display:flex; align-items:center; gap:12px; margin-bottom:8px'>
    <span style='font-size:28px; font-weight:800; font-family:"IBM Plex Mono",monospace;
                 background:linear-gradient(90deg,#4da6ff,#a78bfa); -webkit-background-clip:text;
                 -webkit-text-fill-color:transparent'>퀀트 관제탑</span>
    <span style='font-size:12px; color:#64748b; font-family:"IBM Plex Mono",monospace'>V8.9</span>
</div>
""", unsafe_allow_html=True)

now = datetime.now().strftime('%Y.%m.%d %H:%M KST')
st.markdown(f"<div style='font-size:12px; color:#64748b; font-family:\"IBM Plex Mono\",monospace; margin-bottom:20px'>⏱ {now}</div>", unsafe_allow_html=True)

# ── 탭 ──
# ── 전역 데이터 초기화 (5분 캐시) ──
import time as _time
if 'all_data_cache' not in st.session_state:
    st.session_state.all_data_cache = {}
if 'all_data_time' not in st.session_state:
    st.session_state.all_data_time = 0

# 5분(300초) 지나면 캐시 초기화
if _time.time() - st.session_state.all_data_time > 300:
    if st.session_state.all_data_cache:
        st.session_state.all_data_cache = {}

all_data = st.session_state.all_data_cache

tab_a, tab_b, tab_c, tab_d, tab_e = st.tabs(["🏠 홈", "🔍 분석", "📡 스캐너", "🔄 전략", "⚙️ 관리"])


with tab_a:
    st.markdown("### 🏠 오늘의 퀀트 관제탑")

    # 시장 요약
    import yfinance as _yf2
    @st.cache_data(ttl=300, show_spinner=False)
    def _get_market():
        _r = {}
        for _n, _s in [("코스피","^KS11"),("코스닥","^KQ11"),("나스닥","^IXIC"),("달러/원","KRW=X"),("VIX","^VIX")]:
            try:
                _h = _yf2.Ticker(_s).history(period="2d",interval="1d")
                if len(_h)>=2:
                    _c = _h['Close'].iloc[-1]; _p = _h['Close'].iloc[-2]
                    _r[_n] = {'현재':_c,'등락':(_c/_p-1)*100}
            except: pass
        return _r

    _mkt2 = _get_market()
    if _mkt2:
        _cols_m = st.columns(len(_mkt2))
        for _i_m, (_nm_m, _d_m) in enumerate(_mkt2.items()):
            _up = _d_m['등락'] > 0
            _cc_m = 'up' if (_up and _nm_m != 'VIX') or (not _up and _nm_m == 'VIX') else 'down'
            _cols_m[_i_m].markdown(
                f"<div class='metric-card'><div class='label'>{_nm_m}</div>"
                f"<div class='value flat' style='font-size:16px'>{_d_m['현재']:,.2f}</div>"
                f"<div class='{_cc_m}'>{'▲' if _up else '▼'}{abs(_d_m['등락']):.2f}%</div></div>",
                unsafe_allow_html=True)

    # 환율 경고
    if _mkt2.get('달러/원',{}).get('현재',0) >= 1500:
        st.error("🚨 환율 1,500원 돌파! 미국 주식 신규 진입 자제")
    elif _mkt2.get('달러/원',{}).get('현재',0) >= 1450:
        st.warning("⚠️ 환율 1,450원 이상 — 환차손 주의")

    st.divider()

    # ── V8.9.1 시스템 상태 요약 카드 ──
    st.markdown("#### 🛡️ V8.9.1 방어 시스템 상태")
    _v891_home = run_v891_system_check()
    _h_bo  = _v891_home['blackout']
    _h_sd  = _v891_home['shutdown']
    _h_kp  = _v891_home['kospi_chg']
    _h_kq  = _v891_home['kosdaq_chg']
    _h_ok  = _v891_home['can_enter']

    _sc1, _sc2, _sc3, _sc4 = st.columns(4)

    # 진입 가능 여부
    _sc1.markdown(
        f"<div class='metric-card' style='border-color:{'rgba(52,211,153,0.4)' if _h_ok else 'rgba(244,63,94,0.4)'}'>"
        f"<div class='label'>진입 가능 여부</div>"
        f"<div class='value' style='color:{'#34d399' if _h_ok else '#f43f5e'};font-size:18px'>"
        f"{'✅ 진입 가능' if _h_ok else '🚫 진입 차단'}</div></div>",
        unsafe_allow_html=True)

    # 블랙아웃
    _sc2.markdown(
        f"<div class='metric-card' style='border-color:{'rgba(244,63,94,0.4)' if _h_bo else 'rgba(255,255,255,0.08)'}'>"
        f"<div class='label'>매크로 블랙아웃</div>"
        f"<div class='value' style='color:{'#f43f5e' if _h_bo else '#34d399'};font-size:18px'>"
        f"{'🚫 차단 중' if _h_bo else '✅ 정상'}</div></div>",
        unsafe_allow_html=True)

    # 지수 셧다운
    _sc3.markdown(
        f"<div class='metric-card' style='border-color:{'rgba(244,63,94,0.4)' if _h_sd else 'rgba(255,255,255,0.08)'}'>"
        f"<div class='label'>지수 셧다운</div>"
        f"<div class='value' style='color:{'#f43f5e' if _h_sd else '#34d399'};font-size:18px'>"
        f"{'🚨 발동' if _h_sd else '✅ 정상'}</div></div>",
        unsafe_allow_html=True)

    # 지수 현황
    _kp_c = '#f43f5e' if _h_kp < 0 else '#34d399'
    _kq_c = '#f43f5e' if _h_kq < 0 else '#34d399'
    _sc4.markdown(
        f"<div class='metric-card'>"
        f"<div class='label'>지수 등락</div>"
        f"<div style='font-size:13px;margin-top:6px'>"
        f"코스피 <b style='color:{_kp_c}'>{_h_kp:+.2f}%</b><br>"
        f"코스닥 <b style='color:{_kq_c}'>{_h_kq:+.2f}%</b></div></div>",
        unsafe_allow_html=True)

    # 경고 메시지
    for _alert in _v891_home['alerts']:
        st.error(_alert)

    st.divider()

    # 10:30 매매 가능 여부
    from datetime import datetime as _dt_h
    _kh = (_dt_h.utcnow().hour + 9) % 24
    _km = _dt_h.utcnow().minute
    if (9 <= _kh < 10) or (_kh == 10 and _km <= 30):
        st.error("🔒 09:00~10:30 진입 금지 구간")
    elif 9 <= _kh < 16:
        st.success("✅ 진입 가능 구간 (장중)")
    else:
        st.info("💤 장 외 시간")

    st.divider()

    # 관심종목 현황 요약
    st.markdown("#### 📊 관심종목 현황")
    _wl_home = get_watchlist()
    _tickers_home = [(l.split(',')[0].strip(), l.split(',')[1].strip())
                     for l in _wl_home.split('\n') if ',' in l]

    for _th, _nh in _tickers_home:
        try:
            _df_h = fetch_ohlcv(_th, 30)
            if _df_h is None or len(_df_h) < 5: continue
            _df_h = calc_indicators(_df_h)
            _lh = _df_h.iloc[-1]; _ph = _df_h.iloc[-2]
            _chgh = (_lh['종가']/_ph['종가']-1)*100
            _cch  = '#ff4d6d' if _chgh>0 else '#4da6ff'
            _sigh = get_signal(_df_h)
            _bdgh = ' '.join([f"<span class='badge badge-{s[1]}'>{s[0]}</span>" for s in _sigh])
            _rsi_ch = '#ff4d6d' if _lh['RSI']>=70 else '#4da6ff' if _lh['RSI']<=30 else '#6b7fa3'
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"padding:8px 12px;background:rgba(255,255,255,0.04);border-radius:8px;margin-bottom:4px;border:1px solid rgba(255,255,255,0.08)'>"
                f"<span><b>{_nh}</b> <span style='color:#64748b;font-size:11px'>({_th})</span></span>"
                f"<span style='display:flex;gap:10px;align-items:center'>"
                f"<span style='font-family:IBM Plex Mono'>{format_price(_lh['종가'],_th)}</span>"
                f"<span style='color:{_cch}'>{_chgh:+.2f}%</span>"
                f"<span style='color:{_rsi_ch};font-size:12px'>RSI {_lh['RSI']:.0f}</span>"
                f"{_bdgh}</span></div>",
                unsafe_allow_html=True)
        except: pass

    st.divider()

    # ── 🗓️ 매크로 이벤트 관리 ──
    st.markdown("#### 🗓️ 매크로 이벤트 관리")
    st.caption("등록된 이벤트 ±48시간 이내 신규 진입 자동 차단 (V8.9.1 블랙아웃)")

    # session_state 초기화
    if 'macro_events' not in st.session_state:
        st.session_state.macro_events = []

    # 주요 이벤트 빠른 추가 버튼
    st.markdown("**⚡ 빠른 추가**")
    _qe_cols = st.columns(6)
    _quick_events = ["FOMC", "CPI", "GDP", "금리발표", "실업지표", "PPI"]
    for _qi, _qe in enumerate(_quick_events):
        if _qe_cols[_qi].button(_qe, key=f"qe_{_qi}", use_container_width=True):
            from datetime import datetime as _dtt
            st.session_state['_qe_name'] = _qe

    # 이벤트 추가 폼
    with st.form("macro_add_form", clear_on_submit=True):
        _fa1, _fa2, _fa3 = st.columns([2, 3, 1])
        _ev_date = _fa1.date_input("날짜", key="ev_date_input")
        _ev_name_default = st.session_state.pop('_qe_name', '')
        _ev_name = _fa2.text_input("이벤트명", value=_ev_name_default,
                                    placeholder="예: FOMC, CPI 발표")
        _fa3.markdown("<div style='padding-top:28px'>", unsafe_allow_html=True)
        _ev_submit = st.form_submit_button("➕ 추가", use_container_width=True)
        if _ev_submit and _ev_name:
            _new_ev = {"date": str(_ev_date), "name": _ev_name.strip()}
            # 중복 체크
            _existing = [e['date'] for e in st.session_state.macro_events]
            if str(_ev_date) not in _existing:
                st.session_state.macro_events.append(_new_ev)
                st.session_state.pop('v891_cache', None)  # V8.9.1 캐시 초기화
                st.success(f"✅ {_ev_name} ({_ev_date}) 추가!")
                st.rerun()
            else:
                st.warning("이미 등록된 날짜입니다.")

    # 등록된 이벤트 목록
    if st.session_state.macro_events:
        from datetime import datetime as _dtt2
        _now_str = _dtt2.now()
        st.markdown("**📋 등록된 이벤트**")
        for _ei, _ev in enumerate(sorted(st.session_state.macro_events,
                                          key=lambda x: x['date'])):
            try:
                _ev_dt2  = _dtt2.strptime(_ev['date'], "%Y-%m-%d")
                _diff_h  = (_ev_dt2 - _now_str).total_seconds() / 3600
                _is_active = abs(_diff_h) <= 48
                _status  = "🔴 블랙아웃 중" if _is_active else (
                            "⏰ 임박" if 0 < _diff_h <= 72 else
                            "✅ 종료" if _diff_h < 0 else "📅 예정")
                _bg = "rgba(244,63,94,0.1)" if _is_active else "rgba(255,255,255,0.03)"
                _border = "rgba(244,63,94,0.4)" if _is_active else "rgba(255,255,255,0.08)"
            except:
                _status = "📅 예정"; _bg = "rgba(255,255,255,0.03)"; _border = "rgba(255,255,255,0.08)"
                _diff_h = 999

            _el1, _el2 = st.columns([5, 1])
            _el1.markdown(
                f"<div style='background:{_bg};border:1px solid {_border};"
                f"border-radius:10px;padding:10px 14px;margin-bottom:4px'>"
                f"<b>{_ev['name']}</b> "
                f"<span style='color:#64748b;font-size:12px'>{_ev['date']}</span> "
                f"<span style='font-size:12px'>{_status}</span>"
                f"{'  <span style="color:#f43f5e;font-size:11px">신규진입 차단중</span>' if _is_active else ''}"
                f"</div>",
                unsafe_allow_html=True
            )
            if _el2.button("🗑️", key=f"del_ev_{_ei}", use_container_width=True):
                st.session_state.macro_events.pop(_ei)
                st.session_state.pop('v891_cache', None)
                st.rerun()
    else:
        st.info("💡 등록된 이벤트 없음 — 위에서 FOMC, CPI 등 주요 이벤트를 추가하세요.")

    st.divider()
    if st.button("🔄 새로고침", key="home_refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


with tab_b:
    st.markdown("### 🔍 분석")
    _sub_b1, _sub_b2 = st.tabs(["📈 차트+지표", "🤖 Gemini 분석"])

    with _sub_b1:
        def _display_name(ticker, name):
            if is_korean_ticker(ticker):
                return f"{name} ({ticker})"
            else:
                return f"{ticker} ({name})"

        _b1_tickers = get_watchlist_tickers()
        if not _b1_tickers:
            st.info("👈 사이드바에서 관심종목을 추가해주세요.")
        else:
            # all_data에 없는 종목 즉시 로드 (관리 탭을 안 열어도 동작)
            _b1_missing = [(_bt, _bn) for _bt, _bn in _b1_tickers if _bt not in all_data]
            if _b1_missing:
                with st.spinner(f"📡 {len(_b1_missing)}개 종목 데이터 로딩 중..."):
                    for _bt, _bn in _b1_missing:
                        _bdf = fetch_ohlcv(_bt, 80)
                        if _bdf is not None and len(_bdf) >= 20:
                            all_data[_bt] = {'name': _bn, 'df': calc_indicators(_bdf)}
                    st.session_state.all_data_cache = all_data

            _b1_opts = [_display_name(t, n) for t, n in _b1_tickers if t in all_data]
            if not _b1_opts:
                st.warning("데이터를 불러올 수 없습니다. 잠시 후 다시 시도해주세요.")
                st.stop()
            selected = st.selectbox("종목 선택", _b1_opts)
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
            # KIS 실시간 현재가 우선 사용
            _kis_price = None
            if kis_available() and is_korean_ticker(sel_ticker):
                _kis_price = kis_get_price(sel_ticker)
            _display_price = _kis_price['현재가'] if _kis_price else l['종가']
            _display_chg   = _kis_price['등락률'] if _kis_price else chg
            _kis_badge     = " <span style='font-size:10px;color:#34d399'>● 실시간</span>" if _kis_price else " <span style='font-size:10px;color:#64748b'>● 지연</span>"
            _cur_fmt  = format_price(_display_price, sel_ticker)
            m1.markdown(f"<div class='metric-card'><div class='label'>현재가{_kis_badge}</div><div class='value flat'>{_cur_fmt}</div></div>", unsafe_allow_html=True)
            m2.markdown(f"<div class='metric-card'><div class='label'>등락</div><div class='value {chg_color}'>{chg:+.2f}%</div></div>", unsafe_allow_html=True)
            rsi_c = 'up' if l['RSI']>=70 else 'down' if l['RSI']<=30 else 'flat'
            m3.markdown(f"<div class='metric-card'><div class='label'>RSI(14)</div><div class='value {rsi_c}'>{l['RSI']:.1f}</div></div>", unsafe_allow_html=True)
            m4.markdown(f"<div class='metric-card'><div class='label'>BB 위치</div><div class='value flat'>{bb_p}%</div></div>", unsafe_allow_html=True)
            m5.markdown(f"<div class='metric-card'><div class='label'>52주 위치</div><div class='value flat'>{w52_pos}%</div></div>", unsafe_allow_html=True)
            vol_c = 'up' if l['거래량_비율']>=200 else 'flat'
            m6.markdown(f"<div class='metric-card'><div class='label'>거래량비율</div><div class='value {vol_c}'>{l['거래량_비율']:.0f}%</div></div>", unsafe_allow_html=True)

            # ── 🎯 자동 전략 분석 ──
            st.markdown("### 🎯 자동 전략 분석")

            # 프리셋 선택
            _an_pr1, _an_pr2, _an_pr3 = st.columns(3)
            if 'analysis_preset' not in st.session_state:
                st.session_state.analysis_preset = 'bounce'
            if _an_pr1.button("📉 반등매매", key="an_bounce",
                              type="primary" if st.session_state.analysis_preset=="bounce" else "secondary",
                              use_container_width=True):
                st.session_state.analysis_preset = "bounce"
                st.rerun()
            if _an_pr2.button("📈 추세매매", key="an_trend",
                              type="primary" if st.session_state.analysis_preset=="trend" else "secondary",
                              use_container_width=True):
                st.session_state.analysis_preset = "trend"
                st.rerun()
            if _an_pr3.button("🎯 바닥확인", key="an_bottom",
                              type="primary" if st.session_state.analysis_preset=="bottom" else "secondary",
                              use_container_width=True):
                st.session_state.analysis_preset = "bottom"
                st.rerun()

            # 타점 자동 계산
            try:
                _ep = calc_entry_point(sel_df, st.session_state.analysis_preset)
                _rr_c = '#34d399' if _ep['rr'] >= 2 else '#fbbf24' if _ep['rr'] >= 1 else '#f43f5e'
                _gap_c = '#34d399' if _ep['gap_pct'] < 0 else '#fbbf24'

                # 진입 종합 판정
                _sigs = get_signal(sel_df)
                _buy_cnt  = sum(1 for _, t in _sigs if t == 'buy')
                _sell_cnt = sum(1 for _, t in _sigs if t == 'sell')
                _v891     = run_v891_system_check()

                if not _v891['can_enter']:
                    _verdict = "🚫 진입 차단"
                    _verdict_color = "#f43f5e"
                    _verdict_bg    = "rgba(244,63,94,0.1)"
                    _verdict_border= "rgba(244,63,94,0.4)"
                    _verdict_detail = " / ".join(_v891['alerts'])
                elif _ep['rr'] < 2.0:
                    _verdict = "❌ 진입 불가"
                    _verdict_color = "#f43f5e"
                    _verdict_bg    = "rgba(244,63,94,0.1)"
                    _verdict_border= "rgba(244,63,94,0.4)"
                    _verdict_detail = f"R:R {_ep['rr']} — 2.0 미만 기각"
                elif _buy_cnt >= 2 and _ep['rr'] >= 2.0:
                    _verdict = "✅ 매수 검토"
                    _verdict_color = "#34d399"
                    _verdict_bg    = "rgba(52,211,153,0.1)"
                    _verdict_border= "rgba(52,211,153,0.4)"
                    _verdict_detail = f"매수신호 {_buy_cnt}개 / R:R {_ep['rr']}"
                else:
                    _verdict = "⚠️ 관망"
                    _verdict_color = "#fbbf24"
                    _verdict_bg    = "rgba(251,191,36,0.1)"
                    _verdict_border= "rgba(251,191,36,0.4)"
                    _verdict_detail = f"신호 약함 (매수 {_buy_cnt} / 매도 {_sell_cnt})"

                # 판정 배너
                st.markdown(
                    f"<div style='background:{_verdict_bg};border:2px solid {_verdict_border};"
                    f"border-radius:14px;padding:14px 20px;margin-bottom:12px;"
                    f"display:flex;justify-content:space-between;align-items:center'>"
                    f"<span style='font-size:22px;font-weight:800;color:{_verdict_color}'>{_verdict}</span>"
                    f"<span style='font-size:13px;color:#94a3b8'>{_verdict_detail}</span>"
                    f"<span style='font-size:12px;color:#64748b'>{_ep['reason']}</span>"
                    f"</div>",
                    unsafe_allow_html=True
                )

                # 전략 카드 (현재가 / 매수타점 / 손절가 / 1차목표 / R:R)
                st.markdown(
                    f"<div style='display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px'>"
                    f"<div style='background:rgba(255,255,255,0.05);border-radius:12px;padding:14px;text-align:center'>"
                    f"<div style='font-size:10px;color:#64748b;margin-bottom:6px'>현재가</div>"
                    f"<div style='font-size:18px;font-weight:700;color:#94a3b8'>{_ep['cur']:,.0f}</div></div>"

                    f"<div style='background:rgba(251,191,36,0.1);border:1px solid rgba(251,191,36,0.3);border-radius:12px;padding:14px;text-align:center'>"
                    f"<div style='font-size:10px;color:#64748b;margin-bottom:6px'>🎯 매수 타점</div>"
                    f"<div style='font-size:18px;font-weight:700;color:#fbbf24'>{_ep['entry']:,.0f}</div>"
                    f"<div style='font-size:11px;color:{_gap_c}'>{_ep['gap_pct']:+.1f}% 대기</div></div>"

                    f"<div style='background:rgba(244,63,94,0.1);border:1px solid rgba(244,63,94,0.3);border-radius:12px;padding:14px;text-align:center'>"
                    f"<div style='font-size:10px;color:#64748b;margin-bottom:6px'>🛑 손절가</div>"
                    f"<div style='font-size:18px;font-weight:700;color:#f43f5e'>{_ep['stoploss']:,.0f}</div>"
                    f"<div style='font-size:11px;color:#64748b'>-7%</div></div>"

                    f"<div style='background:rgba(52,211,153,0.1);border:1px solid rgba(52,211,153,0.3);border-radius:12px;padding:14px;text-align:center'>"
                    f"<div style='font-size:10px;color:#64748b;margin-bottom:6px'>🎯 1차 목표</div>"
                    f"<div style='font-size:18px;font-weight:700;color:#34d399'>{_ep['target1']:,.0f}</div></div>"

                    f"<div style='background:rgba(167,139,250,0.1);border:1px solid rgba(167,139,250,0.3);border-radius:12px;padding:14px;text-align:center'>"
                    f"<div style='font-size:10px;color:#64748b;margin-bottom:6px'>✨ 2차 목표</div>"
                    f"<div style='font-size:18px;font-weight:700;color:#a78bfa'>{_ep['target2']:,.0f}</div></div>"
                    f"</div>",
                    unsafe_allow_html=True
                )

                # R:R 바
                st.markdown(
                    f"<div style='background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);"
                    f"border-radius:12px;padding:12px 20px;display:flex;align-items:center;gap:20px;margin-bottom:8px'>"
                    f"<span style='color:#64748b;font-size:12px'>R:R</span>"
                    f"<span style='font-size:28px;font-weight:800;color:{_rr_c};font-family:IBM Plex Mono'>{_ep['rr']}</span>"
                    f"<span style='font-size:13px;color:{_rr_c}'>{'✅ 진입 가능 (2.0 이상)' if _ep['rr']>=2 else '⚠️ 소량만 (1.0~2.0)' if _ep['rr']>=1 else '❌ 진입 불가 (2.0 미만)'}</span>"
                    f"<span style='margin-left:auto;font-size:11px;color:#64748b'>신호: {' '.join(s for s,_ in _sigs)}</span>"
                    f"</div>",
                    unsafe_allow_html=True
                )

                entry_price   = _ep['entry']
                stop_price    = _ep['stoploss']
                target1_price = _ep['target1']
                target2_price = _ep['target2']

            except Exception as _ep_err:
                st.warning(f"타점 계산 오류: {_ep_err}")
                entry_price = stop_price = target1_price = target2_price = 0

            st.divider()

            # ── 수동 조정 (선택) ──
            with st.expander("✏️ 수동 조정", expanded=False):
                _unit  = get_currency(sel_ticker)
                _step  = 100 if is_korean_ticker(sel_ticker) else 1
                lc1, lc2, lc3, lc4 = st.columns(4)
                entry_price   = lc1.number_input(f"매수가 ({_unit})", value=int(entry_price) if entry_price else 0, step=_step)
                stop_price    = lc2.number_input(f"손절가 ({_unit})", value=int(stop_price)  if stop_price  else 0, step=_step)
                target1_price = lc3.number_input(f"1차 목표 ({_unit})", value=int(target1_price) if target1_price else 0, step=_step)
                target2_price = lc4.number_input(f"2차 목표 ({_unit})", value=int(target2_price) if target2_price else 0, step=_step)

            # 차트
            fig = make_chart(
                sel_df, sel_name,
                entry    = entry_price   if entry_price   > 0 else None,
                stoploss = stop_price    if stop_price    > 0 else None,
                target1  = target1_price if target1_price > 0 else None,
                target2  = target2_price if target2_price > 0 else None,
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

    with _sub_b2:
        if not gemini_key:
            st.warning("👈 사이드바에 Gemini API 키를 입력해주세요.")
        else:
            st.caption("💡 종목별로 개별 분석 버튼을 클릭하세요. (Free tier: 하루 20회 제한)")

            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            _b2_model = genai.GenerativeModel(model_name)
            _B2_SYSTEM = (
                'You are a Korean stock quantitative analysis AI. '
                'Always respond in Korean. '
                'Rules: Reject R:R below 2.0 / Stop-loss -7% / '
                'No entry 09:00-09:30 KST / No averaging down'
            )

            def _gemini_safe_call(mdl, prompt_text, max_retries=2):
                """429 rate-limit 에러 시 retry_delay 만큼 대기 후 재시도"""
                import time as _time
                for attempt in range(max_retries):
                    try:
                        return mdl.generate_content(prompt_text)
                    except Exception as _e:
                        err_str = str(_e)
                        if '429' in err_str:
                            import re as _re
                            m = _re.search(r'seconds:\s*(\d+)', err_str)
                            wait = int(m.group(1)) + 2 if m else 20
                            st.warning(f"⏳ API 한도 초과 — {wait}초 대기 후 재시도 ({attempt+1}/{max_retries})")
                            _time.sleep(wait)
                        else:
                            raise
                raise Exception("최대 재시도 횟수 초과 (429 rate limit). 내일 다시 시도하거나 유료 플랜을 확인하세요.")

            _b2_tickers = get_watchlist_tickers()
            # all_data에 없는 종목 즉시 로드
            _b2_missing = [(_bt, _bn) for _bt, _bn in _b2_tickers if _bt not in all_data]
            if _b2_missing:
                with st.spinner(f"📡 {len(_b2_missing)}개 종목 데이터 로딩 중..."):
                    for _bt, _bn in _b2_missing:
                        _bdf = fetch_ohlcv(_bt, 80)
                        if _bdf is not None and len(_bdf) >= 20:
                            all_data[_bt] = {'name': _bn, 'df': calc_indicators(_bdf)}
                    st.session_state.all_data_cache = all_data

            for ticker, name in _b2_tickers:
                if ticker not in all_data:
                    continue
                with st.expander(f"📊 {name} ({ticker}) 분석", expanded=False):
                    btn = st.button(f"{name} 분석", key=f"btn_{ticker}")
                    if btn:
                        prompt = build_prompt(all_data[ticker]['df'], name, ticker)
                        with st.spinner(f'{name} 분석 중...'):
                            try:
                                res = _gemini_safe_call(_b2_model, _B2_SYSTEM + '\n\n' + prompt)
                                st.markdown(f"<div class='gemini-box'>{res.text}</div>",
                                            unsafe_allow_html=True)
                            except Exception as e:
                                st.error(f"오류: {e}")


    # ══════════════════════════════════════════
    # 탭 4: 추천 스캐너
    # ══════════════════════════════════════════

with tab_c:
    st.markdown("### 🔍 주도 종목 자동 스캐너")

    # ── 프리셋 버튼 ──
    st.markdown("#### ⚡ 전략 프리셋")
    _pr1, _pr2, _pr3, _pr4 = st.columns(4)

    if 'scan_preset' not in st.session_state:
        st.session_state.scan_preset = None

    if _pr1.button("📉 반등매매", key="preset_bounce", use_container_width=True,
                   type="primary" if st.session_state.scan_preset=="bounce" else "secondary"):
        st.session_state.scan_preset = "bounce"
        st.rerun()
    if _pr2.button("📈 추세매매", key="preset_trend", use_container_width=True,
                   type="primary" if st.session_state.scan_preset=="trend" else "secondary"):
        st.session_state.scan_preset = "trend"
        st.rerun()
    if _pr3.button("🎯 바닥확인", key="preset_bottom", use_container_width=True,
                   type="primary" if st.session_state.scan_preset=="bottom" else "secondary"):
        st.session_state.scan_preset = "bottom"
        st.rerun()
    if _pr4.button("⚙️ 직접설정", key="preset_custom", use_container_width=True,
                   type="primary" if st.session_state.scan_preset=="custom" else "secondary"):
        st.session_state.scan_preset = "custom"
        st.rerun()

    # 프리셋 설명
    _preset_desc = {
        "bounce": "📉 반등매매 — RSI 과매도 + 거래량 폭발 (많이 빠진 종목의 반등)",
        "trend":  "📈 추세매매 — 거래량 폭발 + MACD 골든크로스 + 정배열 (상승 추세 탑승)",
        "bottom": "🎯 바닥확인 — 거래량 폭발 + MACD 골든크로스 + BB 하단 (바닥 전환)",
        "custom": "⚙️ 직접설정 — 조건을 직접 선택",
    }
    if st.session_state.scan_preset:
        st.info(_preset_desc[st.session_state.scan_preset])

    st.divider()

    # ── 스캔 설정 ──
    _sc_col1, _sc_col2, _sc_col3 = st.columns(3)
    with _sc_col1:
        st.markdown("**📋 스캔 대상**")
        market_type = st.selectbox("시장", ["KOSPI", "KOSDAQ", "KOSPI+KOSDAQ", "미국(S&P500)"], key="scanner_market")
        top_n = st.slider("상위 N종목", 20, 200, 50, key="scanner_topn")
        min_score = st.slider("최소 점수", 1, 10, 4, key="scanner_minscore",
                              help="높을수록 조건 많이 충족한 종목만")

    with _sc_col2:
        st.markdown("**🎯 필터 조건**")
        _preset = st.session_state.scan_preset

        # 프리셋에 따라 기본값 설정
        _def_rsi   = _preset in ["bounce", "bottom"] if _preset else True
        _def_vol   = True
        _def_macd  = _preset in ["trend", "bottom"] if _preset else False
        _def_bb    = _preset == "bottom" if _preset else False
        _def_align = _preset == "trend" if _preset else False

        _disabled = _preset != "custom" and _preset is not None

        use_rsi   = st.checkbox("RSI 과매도 (≤35)", value=_def_rsi,   disabled=_disabled, key="f_rsi")
        use_vol   = st.checkbox("거래량 폭발 (≥150%)", value=_def_vol, disabled=_disabled, key="f_vol")
        use_macd  = st.checkbox("MACD 골든크로스", value=_def_macd,   disabled=_disabled, key="f_macd")
        use_bb    = st.checkbox("BB 하단 근접 (≤25%)", value=_def_bb, disabled=_disabled, key="f_bb")
        use_align = st.checkbox("정배열 (MA5>MA20>MA60)", value=_def_align, disabled=_disabled, key="f_align")

    with _sc_col3:
        st.markdown("**⚙️ 추가 설정**")
        _is_us = market_type == "미국(S&P500)"
        # 시장 전환 시 가격 필터 자동 리셋
        _prev_market = st.session_state.get('_scanner_prev_market', '')
        if _prev_market != market_type:
            st.session_state['f_minp'] = 1 if _is_us else 5000
            st.session_state['f_maxp'] = 100000 if _is_us else 2000000
            st.session_state['_scanner_prev_market'] = market_type
        st.caption("💡 미국 선택 시 달러 기준 자동 적용")
        min_price = st.number_input(
            f"최소 주가({'$' if _is_us else '원'})",
            value=1 if _is_us else 5000,
            step=1 if _is_us else 1000, key="f_minp")
        max_price = st.number_input(
            f"최대 주가({'$' if _is_us else '원'})",
            value=100000 if _is_us else 2000000,
            step=100 if _is_us else 10000, key="f_maxp")
        use_gemini_scan = st.checkbox("Gemini 분석 포함", value=False, key="f_gemini")

    scan_btn = st.button("🚀 스캔 시작", use_container_width=True, type="primary", key="scan_start_btn")

    if scan_btn:
        st.session_state.passed = None

        # 종목 리스트 — scanner_tickers.json 로드
        try:
            import os as _os
            _json_path = _os.path.join(_os.path.dirname(__file__), 'scanner_tickers.json')
            with open(_json_path, 'r', encoding='utf-8') as _f:
                _tickers_json = json.load(_f)
            KOSPI_LIST  = [tuple(x) for x in _tickers_json.get('KOSPI',  [])]
            KOSDAQ_LIST = [tuple(x) for x in _tickers_json.get('KOSDAQ', [])]
            SP500_LIST  = [tuple(x) for x in _tickers_json.get('SP500',  [])]
        except Exception as _je:
            st.warning(f"⚠️ scanner_tickers.json 로드 실패: {_je} — 내장 리스트 사용")
            KOSPI_LIST = [
            # 대형주
            ("005930","삼성전자"),("000660","SK하이닉스"),("005380","현대차"),
            ("000270","기아"),("051910","LG화학"),("006400","삼성SDI"),
            ("035420","NAVER"),("035720","카카오"),("012450","한화에어로스페이스"),
            ("329180","HD현대중공업"),("015760","한국전력"),("034730","SK"),
            ("028260","삼성물산"),("003670","포스코퓨처엠"),("247540","에코프로비엠"),
            ("086520","에코프로"),("207940","삼성바이오로직스"),("068270","셀트리온"),
            ("096770","SK이노베이션"),("011200","HMM"),("010130","고려아연"),
            ("066570","LG전자"),("055550","신한지주"),("105560","KB금융"),
            ("042700","한미반도체"),("009150","삼성전기"),("034220","LG디스플레이"),
            ("024110","기업은행"),("032640","LG유플러스"),("003550","LG"),
            ("004020","현대제철"),("010140","삼성중공업"),("005490","POSCO홀딩스"),
            ("001040","CJ"),("017670","SK텔레콤"),("030200","KT"),
            ("316140","우리금융지주"),("032830","삼성생명"),("011780","금호석유"),
            ("009540","HD한국조선해양"),("000100","유한양행"),("028670","팬오션"),
            ("018260","삼성에스디에스"),("064350","현대로템"),("000810","삼성화재"),
            ("088350","한화생명"),("139480","이마트"),("097950","CJ제일제당"),
            ("011070","LG이노텍"),("010950","S-Oil"),
            # 중형주 추가
            ("323410","카카오뱅크"),("035250","강원랜드"),("047050","포스코인터내셔널"),
            ("069960","현대백화점"),("071050","한국금융지주"),("030000","제일기획"),
            ("004170","신세계"),("069620","대웅제약"),("003490","대한항공"),
            ("020150","롯데에너지머티리얼즈"),("010620","현대미포조선"),("002380","KCC"),
            ("006360","GS건설"),("000720","현대건설"),("047810","한국항공우주"),
            ("267250","HD현대"),("009830","한화솔루션"),("008930","한미사이언스"),
            ("000990","DB하이텍"),("033780","KT&G"),("079550","LIG넥스원"),
            ("377300","카카오페이"),("293490","카카오게임즈"),("259960","크래프톤"),
            ("352820","하이브"),("122630","KODEX레버리지"),("114800","KODEX인버스"),
            ("091180","티씨케이"),("036460","한국가스공사"),("138930","BNK금융지주"),
            ("001270","부국증권"),("005070","코스모신소재"),("006650","대한유화"),
            ("012330","현대모비스"),("161390","한국타이어앤테크놀로지"),
            ("004000","롯데정밀화학"),("007070","GS리테일"),("021240","코웨이"),
            ("086280","현대글로비스"),("042660","한화오션"),("000880","한화"),
            ("010060","OCI홀딩스"),("002790","아모레퍼시픽"),("090430","아모레G"),
            ("000120","CJ대한통운"),("006800","미래에셋증권"),("016360","삼성증권"),
            ("071970","STX중공업"),("003380","하림지주"),("004830","덕성"),
        ]

        # ── KOSDAQ 100대 ──
        KOSDAQ_LIST = [
            ("042700","한미반도체"),("086520","에코프로"),("247540","에코프로비엠"),
            ("003670","포스코퓨처엠"),("196170","알테오젠"),("263750","펄어비스"),
            ("357780","솔브레인"),("058470","리노공업"),("095340","ISC"),
            ("036930","주성엔지니어링"),("039030","이오테크닉스"),("240810","원익IPS"),
            ("035900","JYP엔터테인먼트"),("041510","에스엠"),("067160","아프리카TV"),
            ("064350","현대로템"),("214150","클래시스"),("112040","위메이드"),
            ("122870","와이지엔터테인먼트"),("091990","셀트리온헬스케어"),
            # 추가 종목
            ("145020","휴젤"),("066970","엘앤에프"),("373220","LG에너지솔루션"),
            ("278280","천보"),("207940","삼성바이오로직스"),("000660","SK하이닉스"),
            ("018290","레이"),("039980","리켐"),("950130","코오롱티슈진"),
            ("054540","삼양옵틱스"),("084370","유진테크"),("115390","락앤락"),
            ("058610","에스씨엔지니어링"),("078340","컴투스"),("060310","3S"),
            ("089790","제이씨케미칼"),("043370","피에이치에이"),("094840","슈프리마"),
            ("053980","에이스테크"),("060250","NHN KCP"),("041960","블리자드"),
            ("108860","셀바스AI"),("950200","파나시아"),("192820","코스맥스"),
            ("131970","두산테스나"),("054080","큐렉소"),("096530","씨젠"),
            ("145720","덴티움"),("253450","스튜디오드래곤"),("950160","코오롱티슈진"),
            ("060560","홈캐스트"),("215600","신라젠"),("043650","국일제지"),
            ("238170","엔에스"),("161890","한국콜마"),("089850","유비쿼스"),
            ("060310","3S"),("023760","한국캐피탈"),("145995","삼양사우"),
            ("049830","이노메트리"),("078590","EMW"),("119860","트루윈"),
        ]

        # ── S&P500 150대 ──
        SP500_LIST = [
            # 기술 대형주
            ("AAPL","Apple"),("MSFT","Microsoft"),("NVDA","NVIDIA"),
            ("GOOGL","Alphabet"),("AMZN","Amazon"),("META","Meta"),
            ("TSLA","Tesla"),("AVGO","Broadcom"),("AMD","AMD"),
            ("INTC","Intel"),("QCOM","Qualcomm"),("MU","Micron"),
            ("NOW","ServiceNow"),("CRM","Salesforce"),("PLTR","Palantir"),
            ("ORCL","Oracle"),("CSCO","Cisco"),("AMAT","Applied Materials"),
            ("LRCX","Lam Research"),("KLAC","KLA Corp"),("ADI","Analog Devices"),
            ("MRVL","Marvell"),("ARM","ARM Holdings"),("SMCI","Super Micro"),
            ("DELL","Dell"),("HPE","HP Enterprise"),("WDC","Western Digital"),
            ("STX","Seagate"),("NXPI","NXP Semi"),("ON","ON Semi"),
            ("TXN","Texas Instruments"),("MPWR","Monolithic Power"),
            ("ADBE","Adobe"),("INTU","Intuit"),("ANSS","Ansys"),
            ("CDNS","Cadence"),("SNPS","Synopsys"),("ACN","Accenture"),
            ("IBM","IBM"),("HPQ","HP Inc"),("ADP","ADP"),
            # 사이버보안/클라우드
            ("FTNT","Fortinet"),("PANW","Palo Alto"),("CRWD","CrowdStrike"),
            ("ZS","Zscaler"),("OKTA","Okta"),("SNOW","Snowflake"),
            ("DDOG","Datadog"),("MDB","MongoDB"),("NET","Cloudflare"),
            ("TEAM","Atlassian"),("HUBS","HubSpot"),
            # 금융
            ("JPM","JPMorgan"),("BAC","Bank of America"),("WFC","Wells Fargo"),
            ("GS","Goldman Sachs"),("MS","Morgan Stanley"),("C","Citigroup"),
            ("BLK","BlackRock"),("SCHW","Charles Schwab"),
            ("V","Visa"),("MA","Mastercard"),("PYPL","PayPal"),
            ("AXP","AmericanExpress"),("COF","Capital One"),
            # 헬스케어
            ("UNH","UnitedHealth"),("LLY","Eli Lilly"),("JNJ","J&J"),
            ("PFE","Pfizer"),("MRK","Merck"),("ABBV","AbbVie"),
            ("ABT","Abbott"),("TMO","Thermo Fisher"),("DHR","Danaher"),
            ("AMGN","Amgen"),("GILD","Gilead"),("VRTX","Vertex"),
            ("REGN","Regeneron"),("ISRG","Intuitive Surgical"),("BSX","Boston Sci"),
            # 소비재
            ("WMT","Walmart"),("COST","Costco"),("HD","Home Depot"),
            ("LOW","Lowes"),("TGT","Target"),("MCD","McDonalds"),
            ("SBUX","Starbucks"),("NKE","Nike"),("PG","P&G"),
            ("KO","Coca-Cola"),("PEP","PepsiCo"),("PM","Philip Morris"),
            # 에너지
            ("XOM","ExxonMobil"),("CVX","Chevron"),("COP","ConocoPhillips"),
            ("SLB","SLB"),("EOG","EOG Resources"),
            # 산업/방산
            ("BA","Boeing"),("CAT","Caterpillar"),("LMT","Lockheed Martin"),
            ("RTX","Raytheon"),("NOC","Northrop"),("GD","General Dynamics"),
            ("GE","GE"),("HON","Honeywell"),("UPS","UPS"),("FDX","FedEx"),
            # 미디어/통신
            ("NFLX","Netflix"),("DIS","Disney"),("T","AT&T"),("VZ","Verizon"),
            ("TMUS","T-Mobile"),("CMCSA","Comcast"),
            # 핫 종목
            ("COIN","Coinbase"),("MSTR","MicroStrategy"),("UBER","Uber"),
            ("ABNB","Airbnb"),("SHOP","Shopify"),("MELI","MercadoLibre"),
            ("SE","Sea Limited"),("DASH","DoorDash"),("RBLX","Roblox"),
            ("HOOD","Robinhood"),("SOFI","SoFi"),("AFRM","Affirm"),
            ("RIVN","Rivian"),("LCID","Lucid"),("NIO","NIO"),
            ("BABA","Alibaba"),("JD","JD.com"),("PDD","PDD Holdings"),
            ]

        extra = [(t,n) for t,n in TICKERS]
        if market_type == "KOSPI":
            scan_list = KOSPI_LIST + [x for x in extra if x not in KOSPI_LIST]
        elif market_type == "KOSDAQ":
            scan_list = KOSDAQ_LIST + [x for x in extra if x not in KOSDAQ_LIST]
        elif market_type == "KOSPI+KOSDAQ":
            scan_list = KOSPI_LIST + [x for x in KOSDAQ_LIST if x not in KOSPI_LIST]
            scan_list += [x for x in extra if x not in scan_list]
        else:
            scan_list = SP500_LIST + [x for x in extra if x not in SP500_LIST]

        scan_list   = scan_list[:top_n]
        scan_tickers = [t for t,n in scan_list]
        name_map     = {t:n for t,n in scan_list}

        st.info(f"📋 스캔 대상: {len(scan_tickers)}종목")

        passed = []
        prog   = st.progress(0)
        status = st.empty()

        for idx, ticker in enumerate(scan_tickers):
            prog.progress((idx+1)/len(scan_tickers))
            name = name_map.get(ticker, ticker)
            status.markdown(f"<span style='font-size:12px;color:#64748b'>분석 중: {name} ({idx+1}/{len(scan_tickers)})</span>", unsafe_allow_html=True)

            try:
                if market_type == "미국(S&P500)":
                    import yfinance as yf
                    _yt   = yf.Ticker(ticker)
                    _hist = _yt.history(period="6mo", interval="1d")
                    if _hist is None or _hist.empty: continue
                    df = _hist.rename(columns={'Open':'시가','High':'고가','Low':'저가','Close':'종가','Volume':'거래량'})[['시가','고가','저가','종가','거래량']].tail(60)
                    df = df[df['거래량']>0]
                else:
                    df = fetch_ohlcv(ticker, 60)
                if df is None or len(df) < 20: continue

                _price = df['종가'].iloc[-1]
                if _price < min_price or _price > max_price: continue

                df = calc_indicators(df)
                l = df.iloc[-1]; p = df.iloc[-2]
                volr  = l['거래량']/df['거래량'].tail(20).mean()*100
                bb_r  = l['BB_upper']-l['BB_lower']
                bb_p  = round((l['종가']-l['BB_lower'])/bb_r*100,1) if bb_r>0 else 50

                score = 0; reasons = []
                if use_rsi and l['RSI']<=35:
                    score+=2; reasons.append(f"📉RSI {l['RSI']:.0f}")
                if use_vol and volr>=150:
                    score+=2; reasons.append(f"🔥거래량{volr:.0f}%")
                if use_macd and l['MACD']>l['Signal'] and p['MACD']<=p['Signal']:
                    score+=3; reasons.append("⚡골든크로스")
                if use_bb and bb_p<=25:
                    score+=2; reasons.append(f"📊BB{bb_p}%")
                if use_align and l['종가']>l['MA5']>l['MA20']>l['MA60']:
                    score+=2; reasons.append("✅정배열")

                if score >= min_score:
                    chg = (l['종가']/p['종가']-1)*100
                    passed.append({
                        'ticker':   ticker,
                        'name':     name,
                        '현재가':   l['종가'],
                        '등락(%)':  round(chg,2),
                        'RSI':      l['RSI'],
                        'MACD':     '골든크로스' if (l['MACD']>l['Signal'] and p['MACD']<=p['Signal']) else ('▲' if l['MACD']>l['Signal'] else '▼'),
                        'BB위치':   f"{bb_p}%",
                        '거래량비율': round(volr,0),
                        'score':    score,
                        'reasons':  reasons,
                        'df':       df,
                    })
            except: continue

        prog.empty(); status.empty()
        passed = sorted(passed, key=lambda x: x['score'], reverse=True)
        st.session_state.passed = passed

        if not passed:
            st.warning(f"⚠️ 조건 충족 종목 없음 — 최소점수({min_score}점)를 낮추거나 조건을 완화하세요.")
        else:
            st.success(f"✅ {len(passed)}개 종목 발굴!")

    # ── 결과 표시 ──
    if st.session_state.passed is not None and st.session_state.passed:
        _sc_wl  = get_watchlist()
        _sc_ids = [l.split(',')[0].strip() for l in _sc_wl.split('\n') if ',' in l]
        _p_list = st.session_state.passed

        st.success(f"✅ {len(_p_list)}개 종목 발굴!")

        # 전체 추가 버튼
        _new_items = [i for i in _p_list if i['ticker'] not in _sc_ids]
        if _new_items:
            if st.button(f"⭐ 전체 {len(_new_items)}개 사이드바 추가", key="bulk_add_btn",
                         use_container_width=True, type="primary"):
                _added_cnt = sum(1 for _it in _new_items if add_ticker(_it['ticker'], _it['name']))
                if _added_cnt:
                    st.success(f"✅ {_added_cnt}개 추가 완료!")
                    st.rerun()
                else:
                    st.warning("모두 이미 등록된 종목입니다.")

        st.divider()

        # ── 결과 테이블 ──
        st.markdown("#### 📋 발굴 종목 테이블")
        import pandas as pd
        _rows = []
        for item in _p_list:
            _added = "✅" if item['ticker'] in _sc_ids else "➕"
            _chg_arrow = "▲" if item['등락(%)']>0 else "▼"
            _rows.append({
                "추가": _added,
                "종목명": item['name'],
                "코드": item['ticker'],
                "현재가": f"{item['현재가']:,.0f}",
                "등락(%)": f"{_chg_arrow}{abs(item['등락(%)']):+.2f}%",
                "RSI": f"{item['RSI']:.1f}",
                "MACD": item['MACD'],
                "BB위치": item['BB위치'],
                "거래량%": f"{item['거래량비율']:.0f}%",
                "점수": f"{item['score']}점",
                "신호": " ".join(item['reasons']),
            })

        _result_df = pd.DataFrame(_rows)

        # 스타일 적용
        def _color_row(row):
            styles = []
            for col in row.index:
                if col == '등락(%)':
                    styles.append('color: #ff4d6d' if '▲' in str(row[col]) else 'color: #4da6ff')
                elif col == '점수':
                    val = int(str(row[col]).replace('점',''))
                    styles.append('color: #ffd166; font-weight: bold' if val >= 6 else 'color: #e0e6f0')
                elif col == '추가':
                    styles.append('color: #4dff91' if row[col]=='✅' else 'color: #ffd166')
                else:
                    styles.append('')
            return styles

        st.dataframe(
            _result_df.style.apply(_color_row, axis=1),
            use_container_width=True,
            height=min(400, 35 + len(_rows)*35),
            hide_index=True,
        )

        st.divider()

        # ── 종목 선택 → 상세 분석 ──
        st.markdown("#### 🔍 종목 선택 → 상세 분석")
        _sel_names = [f"{item['name']} ({item['ticker']}) | {item['score']}점" for item in _p_list]
        _sel_scan  = st.selectbox("분석할 종목", _sel_names, key="scan_detail_sel")
        _sel_scan_idx = _sel_names.index(_sel_scan)
        _sel_scan_item = _p_list[_sel_scan_idx]

        # 액션 버튼
        _ab1, _ab2, _ab3 = st.columns(3)
        _is_added_scan = _sel_scan_item['ticker'] in _sc_ids

        if _is_added_scan:
            _ab1.markdown("<div style='color:#34d399;padding:8px 0'>✅ 이미 추가됨</div>", unsafe_allow_html=True)
        else:
            if _ab1.button("⭐ 관심종목 추가", key="scan_ind_add", use_container_width=True):
                try:
                    _ws2 = get_gsheet()
                    _ws2.append_row([_sel_scan_item['ticker'], _sel_scan_item['name']])
                    _cur2 = (get_watchlist()).strip()
                    st.session_state.watchlist_data = _cur2 + f"\n{_sel_scan_item['ticker']},{_sel_scan_item['name']}"
                    safe_clear_cache()
                    st.success(f"✅ {_sel_scan_item['name']} 추가!")
                    st.rerun()
                except Exception as _e:
                    st.error(f"오류: {_e}")

        _chart_key_s = f"scan_chart_{_sel_scan_item['ticker']}"
        if _chart_key_s not in st.session_state:
            st.session_state[_chart_key_s] = False

        def _toggle_chart():
            st.session_state[_chart_key_s] = not st.session_state.get(_chart_key_s, False)

        _ab2.button(
            "📈 차트 닫기" if st.session_state.get(_chart_key_s, False) else "📈 차트",
            key="scan_chart_toggle",
            on_click=_toggle_chart,
            use_container_width=True
        )

        _gem_key_s = f"scan_gem_{_sel_scan_item['ticker']}"
        if _gem_key_s not in st.session_state:
            st.session_state[_gem_key_s] = False

        def _toggle_gem():
            if not gemini_key:
                return
            st.session_state[_gem_key_s] = not st.session_state.get(_gem_key_s, False)

        _ab3.button(
            "🤖 분석 닫기" if st.session_state.get(_gem_key_s, False) else "🤖 Gemini 정밀분석",
            key="scan_gem_toggle",
            on_click=_toggle_gem,
            use_container_width=True,
            disabled=not gemini_key
        )

        # 차트
        if st.session_state.get(_chart_key_s, False):
            _df_s_tmp = _sel_scan_item.get('df')
            _df_s = _df_s_tmp if (_df_s_tmp is not None and not _df_s_tmp.empty) else fetch_ohlcv(_sel_scan_item['ticker'], 60)
            if _df_s is not None and not _df_s.empty:
                try:
                    _df_s  = calc_indicators(_df_s)
                    _preset_s = st.session_state.get('scan_preset')
                    _ep_s  = calc_entry_point(_df_s, _preset_s)
                    _cur_s = _ep_s['cur']
                    _rr_c_s = '#34d399' if _ep_s['rr']>=2 else '#fbbf24' if _ep_s['rr']>=1 else '#f43f5e'
                    _gap_c  = '#34d399' if _ep_s['gap_pct'] < 0 else '#fbbf24'

                    # 전략 요약 박스
                    st.markdown(
                        f"<div style='background:linear-gradient(135deg,rgba(99,102,241,0.1),rgba(139,92,246,0.05));"
                        f"border:1px solid rgba(99,102,241,0.3);border-radius:14px;padding:16px;margin-bottom:12px'>"
                        f"<div style='font-size:11px;color:#64748b;margin-bottom:10px'>"
                        f"📐 {_ep_s['reason']} &nbsp;|&nbsp; "
                        f"현재가 <b style='color:#f0f4ff'>{_cur_s:,.0f}원</b> &nbsp;|&nbsp; "
                        f"진입 대기 <b style='color:{_gap_c}'>{_ep_s['gap_pct']:+.1f}%</b>"
                        f"</div>"
                        f"<div style='display:grid;grid-template-columns:repeat(5,1fr);gap:10px;text-align:center'>"
                        f"<div style='background:rgba(255,255,255,0.05);border-radius:10px;padding:10px'>"
                        f"<div style='font-size:10px;color:#64748b'>현재가</div>"
                        f"<div style='font-size:16px;font-weight:700;color:#94a3b8'>{_cur_s:,.0f}</div></div>"
                        f"<div style='background:rgba(251,191,36,0.1);border:1px solid rgba(251,191,36,0.3);border-radius:10px;padding:10px'>"
                        f"<div style='font-size:10px;color:#64748b'>🎯 매수 타점</div>"
                        f"<div style='font-size:16px;font-weight:700;color:#fbbf24'>{_ep_s['entry']:,.0f}</div></div>"
                        f"<div style='background:rgba(244,63,94,0.1);border:1px solid rgba(244,63,94,0.3);border-radius:10px;padding:10px'>"
                        f"<div style='font-size:10px;color:#64748b'>🛑 손절가</div>"
                        f"<div style='font-size:16px;font-weight:700;color:#f43f5e'>{_ep_s['stoploss']:,.0f}</div>"
                        f"<div style='font-size:10px;color:#64748b'>-7%</div></div>"
                        f"<div style='background:rgba(52,211,153,0.1);border:1px solid rgba(52,211,153,0.3);border-radius:10px;padding:10px'>"
                        f"<div style='font-size:10px;color:#64748b'>🎯 1차목표</div>"
                        f"<div style='font-size:16px;font-weight:700;color:#34d399'>{_ep_s['target1']:,.0f}</div></div>"
                        f"<div style='background:rgba({_rr_c_s.replace('#','').replace('34d399','52,211,153').replace('fbbf24','251,191,36').replace('f43f5e','244,63,94')},0.15);border-radius:10px;padding:10px'>"
                        f"<div style='font-size:10px;color:#64748b'>📊 R:R</div>"
                        f"<div style='font-size:22px;font-weight:700;color:{_rr_c_s}'>{_ep_s['rr']}</div>"
                        f"<div style='font-size:11px;color:{_rr_c_s}'>{'✅ 진입가능' if _ep_s['rr']>=2 else '⚠️ 소량' if _ep_s['rr']>=1 else '❌ 불가'}</div>"
                        f"</div></div></div>",
                        unsafe_allow_html=True
                    )

                    st.plotly_chart(
                        make_chart(
                            _df_s, _sel_scan_item['name'],
                            entry    = _ep_s['entry'],
                            stoploss = _ep_s['stoploss'],
                            target1  = _ep_s['target1'],
                            target2  = _ep_s['target2'],
                        ),
                        use_container_width=True
                    )
                except Exception as _e:
                    st.warning(f"차트 오류: {_e}")

        # Gemini 정밀분석
        if st.session_state.get(_gem_key_s, False) and gemini_key:
            _gcache = f"gem_cache_{_sel_scan_item['ticker']}"
            st.markdown("#### 🤖 Gemini 정밀분석")
            st.markdown(
                f"<div style='background:rgba(99,102,241,0.06);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:10px;margin-bottom:8px;font-size:12px;color:#64748b'>"
                f"분석 대상: <b style='color:#f0f4ff'>{_sel_scan_item['name']}</b> | "
                f"현재가: <b style='color:#fbbf24'>{_sel_scan_item['현재가']:,.0f}원</b> | "
                f"RSI: <b>{_sel_scan_item['RSI']:.1f}</b> | "
                f"점수: <b style='color:#fbbf24'>{_sel_scan_item['score']}점</b>"
                f"</div>",
                unsafe_allow_html=True
            )
            if _gcache not in st.session_state:
                import google.generativeai as genai
                genai.configure(api_key=gemini_key)
                _gm = genai.GenerativeModel(model_name)

                _sys  = (
                    'You are a Korean stock quantitative analysis AI. Always respond in Korean. '
                    'CRITICAL: Start your response with a clear verdict box in this EXACT format:\n'
                    '```\n'
                    '【최종 판정】\n'
                    '결론: [✅ 매수 가능 / ⚠️ 관망 / ❌ 회피] ← 반드시 셋 중 하나\n'
                    '신뢰도: [상 / 중 / 하]\n'
                    '매수 타점: [가격]원 (현재가 대비 [%])\n'
                    '손절가: [가격]원 (-7%)\n'
                    '1차 목표: [가격]원\n'
                    'R:R: [수치]\n'
                    '```\n'
                    'Then provide: 1)근거(기술적/수급) 2)리스크 요인 3)진입 타이밍 조건\n'
                    'Rules: R:R>2.0 / Stop-loss -7% / No entry 09-09:30 / No averaging down / No averaging down ever.'
                )
                _df_g_raw = _sel_scan_item.get('df')
                _df_g = _df_g_raw if (_df_g_raw is not None and not _df_g_raw.empty) else fetch_ohlcv(_sel_scan_item['ticker'], 60)
                if _df_g is not None:
                    with st.spinner(f"🤖 {_sel_scan_item['name']} 정밀분석 중..."):
                        try:
                            _res_g = _gm.generate_content(
                                _sys + '\n\n' + build_prompt(_df_g, _sel_scan_item['name'], _sel_scan_item['ticker'])
                            )
                            st.session_state[_gcache] = _res_g.text
                        except Exception as _eg:
                            st.session_state[_gcache] = f"분석 오류: {_eg}"

            if _gcache in st.session_state:
                _gem_text = st.session_state[_gcache]

                # 판정 박스 추출 및 강조 표시
                import re as _re
                _verdict_match = _re.search(r'【최종 판정】.*?```', _gem_text, _re.DOTALL)
                if _verdict_match:
                    _verdict = _verdict_match.group(0)
                    _rest    = _gem_text[_verdict_match.end():]
                    _v_color = '#34d399' if '✅' in _verdict else '#fbbf24' if '⚠️' in _verdict else '#f43f5e'
                    _v_bg    = 'rgba(52,211,153,0.1)' if '✅' in _verdict else 'rgba(251,191,36,0.1)' if '⚠️' in _verdict else 'rgba(244,63,94,0.1)'
                    st.markdown(
                        f"<div style='background:{_v_bg};border:2px solid {_v_color};"
                        f"border-radius:14px;padding:16px;margin-bottom:12px;"
                        f"font-family:monospace;font-size:14px;line-height:1.8;white-space:pre-wrap'>"
                        f"{_verdict.replace('```','').strip()}"
                        f"</div>",
                        unsafe_allow_html=True
                    )
                    st.markdown(
                        f"<div class='gemini-box'>{_rest.strip()}</div>",
                        unsafe_allow_html=True
                    )
                else:
                    st.markdown(
                        f"<div class='gemini-box'>{_gem_text}</div>",
                        unsafe_allow_html=True
                    )

                _rr1, _rr2 = st.columns(2)
                if _rr1.button("🔄 재분석", key="scan_gem_rerun"):
                    del st.session_state[_gcache]
                    st.rerun()
                if _rr2.button("📝 페이퍼 매수로 이동", key="scan_to_paper"):
                    st.session_state['paper_prefill'] = _sel_scan_item['ticker']
                    st.info("💡 페이퍼 트레이딩 탭에서 매수하세요!")


with tab_d:
    st.markdown("### 🔄 ETF 로테이션 종합 랭킹판")
    st.caption("ADX·RSI·MACD·Z-Score·모멘텀·거래량 6개 지표 종합 점수로 랭킹 산출. ADX 25 미만 탈락.")

    ETF_LIST = [
        # 삼성증권 HTS 기준 ETF 종목코드
        ("069500",  "KODEX 200",              "KS"),  # 코스피200
        ("133690",  "TIGER 나스닥100",        "KS"),  # 나스닥100
        ("091160",  "KODEX 반도체",           "KS"),  # 반도체 섹터
        ("395160",  "KODEX AI반도체TOP2+",    "KS"),  # AI반도체
        ("463250",  "TIGER K방산&우주",       "KS"),  # K방산
        ("459580",  "KODEX AI전력핵심설비",   "KS"),  # AI전력 (2026 수익률 1위)
        ("411060",  "ACE KRX금현물",          "KS"),  # 금현물
        ("364980",  "TIGER 조선TOP10",        "KS"),  # 조선 ETF
        ("305720",  "KODEX 2차전지산업",      "KS"),  # 2차전지
        ("140710",  "TIGER 원자력테마",       "KS"),  # 원자력
    ]

    @st.cache_data(ttl=3600, show_spinner=False)
    def fetch_etf_data():
        import yfinance as yf
        import numpy as np
        results = []
        for ticker, name, mkt in ETF_LIST:
            try:
                _sym = f"{ticker}.KS"
                _df  = yf.Ticker(_sym).history(period="1y", interval="1d")
                if _df is None or len(_df) < 60:
                    results.append({'종목코드':ticker,'ETF명':name,'현재가':0,'등락(%)':0,
                                    'ADX':0,'RSI':0,'MACD신호':'','Z-Score':0,
                                    '모멘텀(20일)':0,'거래량비율':0,'종합점수':0,'상태':'데이터없음'})
                    continue

                _df  = _df.rename(columns={'Open':'시가','High':'고가','Low':'저가','Close':'종가','Volume':'거래량'})
                _hi  = _df['고가']; _lo = _df['저가']; _cl = _df['종가']; _vol = _df['거래량']

                # ── ADX(14) ──
                _tr   = pd.DataFrame({'hl':_hi-_lo,'hc':(_hi-_cl.shift()).abs(),'lc':(_lo-_cl.shift()).abs()}).max(axis=1)
                _atr  = _tr.rolling(14).mean()
                _pdm  = _hi.diff().clip(lower=0)
                _ndm  = (-_lo.diff()).clip(lower=0)
                _pdi  = 100*_pdm.rolling(14).mean()/_atr.replace(0,np.nan)
                _ndi  = 100*_ndm.rolling(14).mean()/_atr.replace(0,np.nan)
                _dx   = 100*(_pdi-_ndi).abs()/(_pdi+_ndi).replace(0,np.nan)
                _adx  = round(_dx.rolling(14).mean().iloc[-1], 1)

                # ── RSI(14) ──
                _delta = _cl.diff()
                _gain  = _delta.clip(lower=0).rolling(14).mean()
                _loss  = (-_delta.clip(upper=0)).rolling(14).mean()
                _rs    = _gain / _loss.replace(0, np.nan)
                _rsi   = round((100 - 100/(1+_rs)).iloc[-1], 1)

                # ── MACD(12,26,9) ──
                _ema12  = _cl.ewm(span=12).mean()
                _ema26  = _cl.ewm(span=26).mean()
                _macd   = _ema12 - _ema26
                _signal = _macd.ewm(span=9).mean()
                _macd_v = _macd.iloc[-1]; _sig_v = _signal.iloc[-1]
                _macd_p = _macd.iloc[-2]; _sig_p = _signal.iloc[-2]
                if _macd_v > _sig_v and _macd_p <= _sig_p:
                    _macd_sig = '🟢골든크로스'
                elif _macd_v > _sig_v:
                    _macd_sig = '▲상승'
                elif _macd_v < _sig_v and _macd_p >= _sig_p:
                    _macd_sig = '🔴데드크로스'
                else:
                    _macd_sig = '▼하락'

                # ── Z-Score(20일) ──
                _ret = _cl.pct_change()
                _zs  = round((_ret.iloc[-1]-_ret.rolling(20).mean().iloc[-1])/_ret.rolling(20).std().iloc[-1]
                             if _ret.rolling(20).std().iloc[-1] > 0 else 0, 2)

                # ── 모멘텀(20일 수익률) ──
                _mom = round((_cl.iloc[-1]/_cl.iloc[-20]-1)*100, 2) if len(_cl)>=20 else 0

                # ── 거래량 비율(5일 평균 대비) ──
                _vol_r = round(_vol.iloc[-1]/_vol.tail(20).mean()*100, 0) if _vol.tail(20).mean() > 0 else 100

                # ── 정배열 여부 ──
                _ma5  = _cl.rolling(5).mean().iloc[-1]
                _ma20 = _cl.rolling(20).mean().iloc[-1]
                _ma60 = _cl.rolling(60).mean().iloc[-1]
                _aligned = _cl.iloc[-1] > _ma5 > _ma20 > _ma60

                # ── 볼린저 밴드 위치 ──
                _bb_mid = _cl.rolling(20).mean().iloc[-1]
                _bb_std = _cl.rolling(20).std().iloc[-1]
                _bb_up  = _bb_mid + 2*_bb_std
                _bb_lo  = _bb_mid - 2*_bb_std
                _bb_pos = round((_cl.iloc[-1]-_bb_lo)/(_bb_up-_bb_lo)*100, 1) if (_bb_up-_bb_lo) > 0 else 50

                # ── 52주 위치 ──
                _52h = _cl.tail(252).max()
                _52l = _cl.tail(252).min()
                _52pos = round((_cl.iloc[-1]-_52l)/(_52h-_52l)*100, 1) if (_52h-_52l) > 0 else 50

                # ── 종합 점수 계산 (0~100) ──
                _score = 0
                # ADX (추세 강도) — 최대 25점
                if _adx >= 40:   _score += 25
                elif _adx >= 30: _score += 18
                elif _adx >= 25: _score += 12
                # RSI (과매수/과매도) — 최대 15점
                if 40 <= _rsi <= 60:   _score += 15  # 중립 = 좋음
                elif 30 <= _rsi < 40:  _score += 10  # 반등 기대
                elif 60 < _rsi <= 70:  _score += 8   # 강세지만 주의
                elif _rsi < 30:        _score += 5   # 과매도
                # MACD — 최대 20점
                if '골든크로스' in _macd_sig: _score += 20
                elif '상승' in _macd_sig:     _score += 12
                elif '데드크로스' in _macd_sig: _score += 0
                else:                          _score += 4
                # Z-Score (상대강도) — 최대 15점
                if _zs >= 1.5:    _score += 15
                elif _zs >= 0.5:  _score += 10
                elif _zs >= -0.5: _score += 6
                elif _zs >= -1.5: _score += 2
                # 모멘텀(20일) — 최대 15점
                if _mom >= 10:    _score += 15
                elif _mom >= 5:   _score += 10
                elif _mom >= 0:   _score += 6
                elif _mom >= -5:  _score += 2
                # 정배열 — 최대 10점
                if _aligned: _score += 10
                # 거래량 비율 — 최대 10점 (150% 이상이면 관심)
                if _vol_r >= 200:   _score += 10
                elif _vol_r >= 150: _score += 7
                elif _vol_r >= 100: _score += 4

                _chg = round((_cl.iloc[-1]/_cl.iloc[-2]-1)*100, 2)

                results.append({
                    '종목코드':    ticker,
                    'ETF명':      name,
                    '현재가':     round(_cl.iloc[-1], 0),
                    '등락(%)':    _chg,
                    'ADX':        _adx,
                    'RSI':        _rsi,
                    'MACD':       _macd_sig,
                    'Z-Score':    _zs,
                    '모멘텀(%)':  _mom,
                    '거래량%':    _vol_r,
                    'BB위치':     _bb_pos,
                    '52주위치':   _52pos,
                    '정배열':     '✅' if _aligned else '❌',
                    '종합점수':   _score,
                    '상태':       '활성' if _adx >= 25 else '탈락',
                })
            except Exception as _e:
                results.append({'종목코드':ticker,'ETF명':name,'현재가':0,'등락(%)':0,
                                'ADX':0,'RSI':0,'MACD':'','Z-Score':0,
                                '모멘텀(%)':0,'거래량%':0,'BB위치':0,'52주위치':0,
                                '정배열':'❌','종합점수':0,'상태':'오류'})
        return results

    with st.spinner("ETF 데이터 로딩 중..."):
        _etf_data = fetch_etf_data()

    if _etf_data:
        _df_etf  = pd.DataFrame(_etf_data)
        _active  = _df_etf[_df_etf['상태']=='활성'].sort_values('종합점수', ascending=False)
        _passive = _df_etf[_df_etf['상태']!='활성']
        _ranked  = pd.concat([_active, _passive]).reset_index(drop=True)

        # 현재 관심종목 목록
        _etf_wl_now  = get_watchlist()
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
            _dead_tag= ' <span style="color:#64748b;font-size:11px">ADX 25미만 탈락</span>' if _is_dead else ''
            _already = row['종목코드'] in _etf_wl_ids

            st.markdown(
                f"<div style='background:{_bg};border:1px solid {_border};border-radius:10px;"
                f"padding:14px 18px;margin-bottom:4px;opacity:{_op}'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<div><b style='font-size:15px'>{_rank} {row['ETF명']}</b>"
                f"<span style='color:#64748b;font-size:11px'> ({row['종목코드']})</span>"
                f"{_tag}{_dead_tag}</div>"
                f"<span style='color:{_cc};font-family:IBM Plex Mono'>{'▲' if row['등락(%)']>0 else '▼'}{abs(row['등락(%)']):+.2f}%</span>"
                f"</div>"
                f"<div style='display:flex;gap:20px;margin-top:8px'>"
                f"<span style='font-size:12px;color:#94a3b8'>현재가 <b style='color:#f0f4ff'>{row['현재가']:,.0f}</b></span>"
                f"<span style='font-size:12px;color:#94a3b8'>ADX <b style='color:{_ac}'>{row.get('ADX',0)}</b></span>"
                f"<span style='font-size:12px;color:#94a3b8'>RSI <b style='color:#f0f4ff'>{row.get('RSI',0)}</b></span>"
                f"<span style='font-size:12px;color:#94a3b8'>MACD <b style='color:#f0f4ff'>{row.get('MACD','')}</b></span>"
                f"<span style='font-size:12px;color:#94a3b8'>Z <b style='color:#f0f4ff'>{row.get('Z-Score',0):+.2f}</b></span>"
                f"<span style='font-size:12px;color:#94a3b8'>모멘텀 <b style='color:#f0f4ff'>{row.get('모멘텀(%)',0):+.1f}%</b></span>"
                f"<span style='font-size:12px;color:#94a3b8'>정배열 <b>{row.get('정배열','')}</b></span>"
                f"<span style='font-size:12px;color:#fbbf24'>종합 <b style='font-size:15px'>{row.get('종합점수',0)}점</b></span>"
                f"</div></div>",
                unsafe_allow_html=True
            )

            # 관심종목 추가 버튼
            _eb1, _eb2 = st.columns([1, 4])
            if _already:
                _eb1.markdown("<div style='color:#34d399;font-size:12px;padding:4px 0'>✅ 추가됨</div>", unsafe_allow_html=True)
            else:
                if _eb1.button("⭐ 추가", key=f"etf_add_{_i}_{row['종목코드']}"):
                    if add_ticker(row['종목코드'], row['ETF명']):
                        st.success(f"✅ {row['ETF명']} 관심종목 추가!")
                        st.rerun()
                    else:
                        st.warning("이미 등록된 종목입니다.")
            st.markdown("<div style='margin-bottom:8px'></div>", unsafe_allow_html=True)

        st.markdown("---")
        st.caption("종합점수 = ADX(25) + RSI(15) + MACD(20) + Z-Score(15) + 모멘텀(15) + 정배열(10) + 거래량(10) | ADX 25미만 자동 탈락")

        _r1, _r2 = st.columns(2)
        if _r1.button("🔄 ETF 새로고침", key="etf_refresh2", use_container_width=True):
            fetch_etf_data.clear()
            st.rerun()

        st.divider()

        # ── 백테스팅 ──
        st.markdown("### 📊 ETF 로테이션 백테스팅")
        st.caption("1위 ETF에 매월 스위칭 전략 vs 코스피 수익률 비교")

        # 수수료/세금 설정 UI
        st.markdown("#### ⚙️ 백테스팅 비용 설정")
        _bt_c1, _bt_c2, _bt_c3 = st.columns(3)
        _fee_buy  = _bt_c1.number_input("매수 수수료(%)", value=0.015, step=0.005,
                                         format="%.3f", key="bt_fee_buy",
                                         help="증권사 수수료 (보통 0.015%)")
        _fee_sell = _bt_c2.number_input("매도 수수료+세금(%)", value=0.33, step=0.01,
                                         format="%.3f", key="bt_fee_sell",
                                         help="수수료 0.015% + 거래세 0.18% + 농특세 0.15% ≈ 0.33%")
        _slip     = _bt_c3.number_input("슬리피지(%)", value=0.1, step=0.05,
                                         format="%.2f", key="bt_slip",
                                         help="호가 공백 오차 (보통 0.05~0.2%)")

        # 총 거래비용 (매수+매도 합산)
        _total_cost = (_fee_buy + _fee_sell + _slip * 2) / 100
        st.caption(f"💡 스위칭 1회당 총 비용: 약 {(_fee_buy + _fee_sell + _slip*2):.3f}% "
                   f"(매수 {_fee_buy+_slip:.3f}% + 매도 {_fee_sell+_slip:.3f}%)")

        @st.cache_data(ttl=86400, show_spinner=False)
        def run_etf_backtest():
            import yfinance as yf
            import numpy as np

            # 수수료 설정 (session_state에서)
            _buy_cost  = (st.session_state.get('bt_fee_buy',  0.015) +
                          st.session_state.get('bt_slip', 0.1)) / 100
            _sell_cost = (st.session_state.get('bt_fee_sell', 0.33) +
                          st.session_state.get('bt_slip', 0.1)) / 100

            # 각 ETF 월별 수익률 계산
            _monthly = {}
            for ticker, name, _ in ETF_LIST:
                try:
                    _sym = f"{ticker}.KS"
                    _df  = yf.Ticker(_sym).history(period="2y", interval="1mo")
                    if _df is None or len(_df) < 6: continue
                    _cl  = _df['Close']
                    _ret = _cl.pct_change().dropna()
                    _monthly[ticker] = {'name': name, 'returns': _ret}
                except:
                    pass

            # 벤치마크 (코스피)
            try:
                _bm_df  = yf.Ticker("^KS11").history(period="2y", interval="1mo")
                _bm_ret = _bm_df['Close'].pct_change().dropna()
            except:
                _bm_ret = None

            if not _monthly: return None

            _all_tickers = list(_monthly.keys())

            # 공통 날짜
            _dates = None
            for t in _all_tickers:
                _idx = _monthly[t]['returns'].index
                _dates = set(_idx) if _dates is None else _dates & set(_idx)
            _dates = sorted(_dates)
            if len(_dates) < 4: return None

            # 로테이션 전략 (수수료 반영)
            _portfolio     = [1.0]  # 수수료 반영
            _portfolio_raw = [1.0]  # 수수료 미반영 (비교용)
            _chosen        = []
            _bench         = [1.0]
            _switch_count  = 0
            _prev_best     = None
            _total_fee     = 0.0

            for _i, _dt in enumerate(_dates[3:], 3):
                # 직전 3개월 모멘텀
                _scores = {}
                for t in _all_tickers:
                    _rets = _monthly[t]['returns']
                    _rd   = dict(zip(_rets.index, _rets))
                    _past = [_rd.get(d, 0) for d in _dates[_i-3:_i]]
                    if _past:
                        _scores[t] = sum(_past)

                if not _scores:
                    _portfolio.append(_portfolio[-1])
                    _portfolio_raw.append(_portfolio_raw[-1])
                    _chosen.append(_prev_best or '없음')
                    continue

                _best_t = max(_scores, key=_scores.get)
                _best_n = _monthly[_best_t]['name']

                # 해당 월 수익률
                _rets_t    = _monthly[_best_t]['returns']
                _month_ret = dict(zip(_rets_t.index, _rets_t)).get(_dt, 0)

                # 수수료 적용 (스위칭 발생 시에만)
                _fee_this = 0.0
                if _prev_best is not None and _best_t != _prev_best:
                    # 매도(이전) + 매수(신규) 비용
                    _fee_this   = _sell_cost + _buy_cost
                    _switch_count += 1
                    _total_fee  += _fee_this
                elif _prev_best is None:
                    # 최초 매수
                    _fee_this  = _buy_cost
                    _total_fee += _fee_this

                _portfolio.append(_portfolio[-1] * (1 + _month_ret - _fee_this))
                _portfolio_raw.append(_portfolio_raw[-1] * (1 + _month_ret))
                _chosen.append(_best_n)
                _prev_best = _best_t

                # 최종 매도세 (마지막 달)
                if _i == len(_dates) - 1:
                    _portfolio[-1] *= (1 - _sell_cost)
                    _total_fee += _sell_cost

                # 벤치마크
                if _bm_ret is not None:
                    _bm_m = dict(zip(_bm_ret.index, _bm_ret)).get(_dt, 0)
                    _bench.append(_bench[-1] * (1 + _bm_m))
                else:
                    _bench.append(_bench[-1])

            # 성과 지표
            _port_arr  = np.array(_portfolio)
            _raw_arr   = np.array(_portfolio_raw)
            _bench_arr = np.array(_bench)

            _port_ret  = (_port_arr[-1]  - 1) * 100
            _raw_ret   = (_raw_arr[-1]   - 1) * 100
            _bench_ret = (_bench_arr[-1] - 1) * 100

            # MDD
            _peak = np.maximum.accumulate(_port_arr)
            _mdd  = (((_port_arr - _peak) / _peak) * 100).min()

            # 샤프
            _m_rets = np.diff(_port_arr) / _port_arr[:-1]
            _sharpe = round(_m_rets.mean() / _m_rets.std() * np.sqrt(12)
                            if _m_rets.std() > 0 else 0, 2)

            # 승률
            _win_months = sum(1 for r in _m_rets if r > 0)
            _win_rate   = round(_win_months / len(_m_rets) * 100, 1) if _m_rets.size > 0 else 0

            return {
                'dates':        _dates[3:],
                'portfolio':    [round((v-1)*100, 2) for v in _portfolio[1:]],
                'portfolio_raw':[round((v-1)*100, 2) for v in _portfolio_raw[1:]],
                'benchmark':    [round((v-1)*100, 2) for v in _bench[1:]],
                'chosen':       _chosen,
                'total_ret':    round(_port_ret, 2),
                'raw_ret':      round(_raw_ret, 2),
                'fee_drag':     round(_raw_ret - _port_ret, 2),
                'bench_ret':    round(_bench_ret, 2),
                'mdd':          round(_mdd, 2),
                'sharpe':       _sharpe,
                'win_rate':     _win_rate,
                'switch_count': _switch_count,
                'total_fee_pct':round(_total_fee * 100, 3),
            }

        with st.spinner("백테스팅 계산 중... (최초 1회)"):
            _bt = run_etf_backtest()

        if _bt:
            # 성과 요약
            # 1행 — 핵심 수익률
            _bt1, _bt2, _bt3, _bt4 = st.columns(4)
            _ret_c = 'up' if _bt['total_ret'] > 0 else 'down'
            _alpha = _bt['total_ret'] - _bt['bench_ret']
            _ac    = 'up' if _alpha > 0 else 'down'

            _bt1.markdown(
                f"<div class='metric-card'><div class='label'>수수료 반영 수익률</div>"
                f"<div class='value {_ret_c}'>{_bt['total_ret']:+.2f}%</div>"
                f"<div style='font-size:11px;color:#64748b'>수수료 전: {_bt.get('raw_ret',0):+.2f}%</div></div>",
                unsafe_allow_html=True)
            _bt2.markdown(
                f"<div class='metric-card'><div class='label'>코스피 수익률</div>"
                f"<div class='value {'up' if _bt['bench_ret']>0 else 'down'}'>{_bt['bench_ret']:+.2f}%</div></div>",
                unsafe_allow_html=True)
            _bt3.markdown(
                f"<div class='metric-card'><div class='label'>알파(초과수익)</div>"
                f"<div class='value {_ac}'>{_alpha:+.2f}%</div></div>",
                unsafe_allow_html=True)
            _bt4.markdown(
                f"<div class='metric-card'><div class='label'>MDD / 샤프</div>"
                f"<div class='value flat'>{_bt['mdd']:.1f}% / {_bt['sharpe']}</div></div>",
                unsafe_allow_html=True)

            # 2행 — 비용 분석
            _bt5, _bt6, _bt7, _bt8 = st.columns(4)
            _bt5.markdown(
                f"<div class='metric-card'><div class='label'>수수료 비용 합계</div>"
                f"<div class='value down'>-{_bt.get('fee_drag',0):.2f}%</div>"
                f"<div style='font-size:11px;color:#64748b'>총 {_bt.get('total_fee_pct',0):.3f}%</div></div>",
                unsafe_allow_html=True)
            _bt6.markdown(
                f"<div class='metric-card'><div class='label'>스위칭 횟수</div>"
                f"<div class='value flat'>{_bt.get('switch_count',0)}회</div>"
                f"<div style='font-size:11px;color:#64748b'>월평균 {_bt.get('switch_count',0)/max(len(_bt['dates']),1):.1f}회</div></div>",
                unsafe_allow_html=True)
            _bt7.markdown(
                f"<div class='metric-card'><div class='label'>월간 승률</div>"
                f"<div class='value {'up' if _bt.get('win_rate',0)>50 else 'down'}'>{_bt.get('win_rate',0):.1f}%</div></div>",
                unsafe_allow_html=True)
            _bt8.markdown(
                f"<div class='metric-card'><div class='label'>수수료 최적화 팁</div>"
                f"<div class='value flat' style='font-size:13px'>{'✅ 효율적' if _bt.get('switch_count',0) < 12 else '⚠️ 과도한 교체'}</div>"
                f"<div style='font-size:11px;color:#64748b'>연 {_bt.get('switch_count',0)/2:.0f}회 교체</div></div>",
                unsafe_allow_html=True)

            # 수익률 차트
            import plotly.graph_objects as go
            _fig_bt = go.Figure()
            _fig_bt.add_trace(go.Scatter(
                x=list(range(len(_bt['portfolio']))),
                y=_bt['portfolio'],
                name='전략 (수수료 반영)',
                line=dict(color='#34d399', width=2.5),
                fill='tozeroy',
                fillcolor='rgba(52,211,153,0.08)'
            ))
            if 'portfolio_raw' in _bt:
                _fig_bt.add_trace(go.Scatter(
                    x=list(range(len(_bt['portfolio_raw']))),
                    y=_bt['portfolio_raw'],
                    name='전략 (수수료 전)',
                    line=dict(color='#34d399', width=1.2, dash='dot'),
                    opacity=0.5
                ))
            _fig_bt.add_trace(go.Scatter(
                x=list(range(len(_bt['benchmark']))),
                y=_bt['benchmark'],
                name='코스피',
                line=dict(color='#38bdf8', width=1.5, dash='dash')
            ))
            _fig_bt.add_hline(y=0, line_color='#2d3a55', line_width=0.8)
            _fig_bt.update_layout(
                paper_bgcolor='#0a0e1a', plot_bgcolor='#0f1726',
                font=dict(color='#8899bb', size=11),
                height=300,
                legend=dict(orientation='h', y=1.02),
                margin=dict(l=10, r=40, t=30, b=10),
                yaxis=dict(gridcolor='#1a2535', ticksuffix='%', side='right'),
                xaxis=dict(gridcolor='#1a2535', title='개월'),
            )
            st.plotly_chart(_fig_bt, use_container_width=True)

            # 월별 선택 ETF 히스토리
            with st.expander("📋 월별 선택 ETF 히스토리"):
                _hist_rows = []
                for _d, _c, _p, _b in zip(
                    _bt['dates'], _bt['chosen'],
                    _bt['portfolio'], _bt['benchmark']
                ):
                    try:
                        _d_str = str(_d)[:7]
                    except:
                        _d_str = str(_d)
                    _hist_rows.append({
                        '월': _d_str,
                        '선택 ETF': _c,
                        '전략 누적(%)': f"{_p:+.2f}%",
                        '코스피 누적(%)': f"{_b:+.2f}%",
                    })
                st.dataframe(pd.DataFrame(_hist_rows), use_container_width=True, hide_index=True)

            if st.button("🔄 백테스팅 재실행", key="bt_rerun"):
                run_etf_backtest.clear()
                st.rerun()
        else:
            st.warning("백테스팅 데이터 부족 (2년 데이터 필요)")

# ══════════════════════════════════════════
# 탭 7: 페이퍼 트레이딩
# ══════════════════════════════════════════

with tab_e:
    _sub_e1, _sub_e2, _sub_e3, _sub_e4 = st.tabs(["⭐ 관심종목", "📝 페이퍼", "🌏 시장지수", "📊 현황판"])

    with _sub_e1:
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
        _wl    = get_watchlist()
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
                f"<div style='padding:10px; background:rgba(255,255,255,0.04); border-radius:8px;"
                f"border:1px solid rgba(255,255,255,0.08); margin-bottom:6px'>"
                f"<b>{_nm}</b>&nbsp;&nbsp;"
                f"<code style='color:#64748b;font-size:11px'>{_tk}</code></div>",
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
                _cur_wl  = get_watchlist()
                _cur_ids = [l.split(",")[0].strip() for l in _cur_wl.split("\n") if "," in l]
                if _code not in _cur_ids:
                    # append_rows 방식으로 변경 (clear 없이 한 줄만 추가)
                    ws = get_gsheet()
                    ws.append_row([_code, _name])
                    # session_state 업데이트
                    _new_wl = _cur_wl.strip() + f"\n{_code},{_name}"
                    st.session_state.watchlist_data = _new_wl
                    safe_clear_cache()
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
                    f"<code style='color:#64748b;font-size:11px'>{_tk2}</code>&nbsp;"
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

    with _sub_e2:
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

                # V8.9.1 스마트 킬스위치 체크
                _ks_result = run_v891_system_check(
                    ticker=_pos['ticker'],
                    entry_price=float(_pos['avg_price']),
                    current_price=float(_cur_p)
                )
                _ks_action = _ks_result['killswitch']

                st.markdown(
                    f"<div style='background:rgba(255,255,255,0.04);border:2px solid {'#ff4d6d' if _kill_alert else '#1e3a5f'};border-radius:10px;padding:14px;margin-bottom:8px'>"
                    f"<div style='display:flex;justify-content:space-between'>"
                    f"<b style='font-size:15px'>{_pos['name']} <span style='color:#64748b;font-size:12px'>({_pos['ticker']})</span></b>"
                    f"<span class='{_pc}' style='font-size:16px;font-weight:700'>{_pos_pct:+.2f}%</span></div>"
                    f"<div style='display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:10px'>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>수량</div><div style='font-weight:700'>{_pos['qty']:,}주</div></div>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>평단가</div><div style='font-weight:700'>{_pos['avg_price']:,.0f}원</div></div>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>현재가</div><div style='font-weight:700'>{_cur_p:,.0f}원</div></div>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>평가금액</div><div style='font-weight:700'>{_pos_val:,.0f}원</div></div>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>평가손익</div><div class='{_pc}' style='font-weight:700'>{_pos_pnl:+,.0f}원</div></div>"
                    f"</div>"
                    f"<div style='margin-top:8px;font-size:12px;color:#f43f5e'>킬스위치 기준: {_kill:,.0f}원 (-7%)"
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
            f"<div style='background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:12px;margin-top:28px'>"
            f"현재가: <b style='font-size:18px;color:#fbbf24'>{_buy_cur:,.0f}원</b> | "
            f"현금잔고: <b style='color:#34d399'>{_acc['cash']:,.0f}원</b></div>",
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
            f"<span style='font-size:11px;color:#64748b'>슬리피지 반영: {_net_buy_preview:,.0f}원/주</span></div>",
            unsafe_allow_html=True
        )

        _buy_memo = st.text_input("매수 근거 (Why)", placeholder="예: BB하단 반등, 골든크로스 확인, 5AI +3점", key="buy_memo")

        _cash_ok = _acc['cash'] >= _buy_total
        if not _cash_ok:
            st.warning(f"⚠️ 현금 부족 — 필요: {_buy_total:,.0f}원 / 보유: {_acc['cash']:,.0f}원")

        # V8.9.1 진입 가능 여부 확인
        _v891_check = run_v891_system_check()
        if not _v891_check['can_enter']:
            for _a in _v891_check['alerts']:
                st.error(_a)
            st.warning("⚠️ V8.9.1 방어 시스템 — 현재 신규 진입 차단 상태입니다.")

        if st.button("📥 가상 매수 실행", key="exec_buy", use_container_width=True,
                     type="primary", disabled=(not _cash_ok or not _v891_check['can_enter'])):
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
                        _cmp  = _cmp.ffill().dropna()
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

                    _csv = _log_df[_show_cols].to_csv(index=False, encoding='utf-8-sig')
                    st.download_button(
                        label="📥 거래일지 CSV 다운로드",
                        data=_csv,
                        file_name=f"trading_log_{datetime.now().strftime('%Y%m%d')}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
            else:
                st.info("아직 거래 기록이 없습니다. 가상 매수를 실행해보세요!")
                # 샘플 차트
                _sample = pd.DataFrame({'내 포트폴리오(%)': [0,1.2,0.8,2.1,1.5,3.2,2.8],
                                         '코스피(%)':       [0,0.5,0.3,1.1,0.9,1.8,1.5]})
                st.markdown("*(샘플 차트 — 거래 실행 후 실제 데이터로 교체됩니다)*")
                st.line_chart(_sample)
        except Exception as _e:
            st.warning(f"성과 분석 로드 오류: {_e}")


    with _sub_e3:
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

    with _sub_e4:
        st.markdown("### 관심 종목 현황")
        # 데이터 로드
        _cur_tickers = get_watchlist_tickers()
        # 새로 추가된 종목이 all_data에 없으면 즉시 로드
        _missing = [(t, n) for t, n in _cur_tickers if t not in all_data]
        if _missing:
            for _mt, _mn in _missing:
                _mdf = fetch_ohlcv(_mt, 80)
                if _mdf is not None and len(_mdf) >= 20:
                    all_data[_mt] = {'name': _mn, 'df': calc_indicators(_mdf)}
            st.session_state.all_data_cache = all_data

        if not all_data:
            _lookback = 80
            all_data = {}
            total = len(_cur_tickers)
            prog_bar = st.progress(0, text="데이터 로딩 중...")
            for idx, (ticker, name) in enumerate(_cur_tickers):
                prog_bar.progress((idx+1)/max(total,1), text=f"📡 {name} ({idx+1}/{total})")
                df = fetch_ohlcv(ticker, _lookback)
                if df is None or len(df) < 20:
                    import yfinance as yf
                    try:
                        _yt = yf.Ticker(ticker)
                        _h  = _yt.history(period="3mo", interval="1d")
                        if not _h.empty:
                            df = _h.rename(columns={'Open':'시가','High':'고가','Low':'저가','Close':'종가','Volume':'거래량'})[['시가','고가','저가','종가','거래량']].tail(_lookback)
                    except: pass
                if df is None or len(df) < 20: continue
                df = calc_indicators(df)
                all_data[ticker] = {'name': name, 'df': df}
            prog_bar.empty()
            st.session_state.all_data_cache = all_data
            import time
            st.session_state.all_data_time = time.time()

        # ── KIS 실시간 연동 ──
        # V8.9.1 지수 셧다운 체크 (현황판)
        _sd_check, _sd_msg, _kp, _kq = check_index_shutdown()
        if _sd_check:
            st.error(_sd_msg)
        elif _kp <= -1.0 or _kq <= -1.0:
            st.warning(f"⚠️ 지수 주의 — 코스피 {_kp:+.2f}% / 코스닥 {_kq:+.2f}%")

        if kis_available():
            with st.expander("📡 KIS 실시간 계좌 현황", expanded=True):
                _kis_col1, _kis_col2 = st.columns([1, 1])

                with _kis_col1:
                    st.markdown("**💰 실제 계좌 잔고**")
                    with st.spinner("잔고 조회 중..."):
                        _bal = kis_get_balance()
                    if _bal:
                        _bc1, _bc2, _bc3 = st.columns(3)
                        _bc1.markdown(f"<div class='metric-card'><div class='label'>현금</div><div class='value flat'>{_bal['현금']:,.0f}원</div></div>", unsafe_allow_html=True)
                        _bc2.markdown(f"<div class='metric-card'><div class='label'>총평가</div><div class='value flat'>{_bal['총평가']:,.0f}원</div></div>", unsafe_allow_html=True)
                        _pnl_c2 = 'up' if _bal['총손익'] >= 0 else 'down'
                        _bc3.markdown(f"<div class='metric-card'><div class='label'>총손익</div><div class='value {_pnl_c2}'>{_bal['총손익']:+,.0f}원<br>({_bal['수익률']:+.2f}%)</div></div>", unsafe_allow_html=True)

                        if _bal['holdings']:
                            st.markdown("**보유 종목**")
                            for _h in _bal['holdings']:
                                _hc = 'up' if _h['수익률'] >= 0 else 'down'
                                _kill_warn = _h['수익률'] <= -6.5
                                st.markdown(
                                    f"<div style='background:rgba(255,255,255,0.04);border:1px solid {'#ff4d6d' if _kill_warn else '#1e3a5f'};border-radius:8px;padding:10px;margin-bottom:6px'>"
                                    f"<b>{_h['종목명']}</b> <span style='color:#64748b;font-size:11px'>({_h['종목코드']})</span>"
                                    f"{'  🚨 킬스위치 임박!' if _kill_warn else ''}<br>"
                                    f"<span style='font-size:12px;color:#94a3b8'>"
                                    f"수량 {_h['수량']:,}주 | 평단 {_h['평단가']:,.0f} | 현재 {_h['현재가']:,.0f} | "
                                    f"<span class='{_hc}'>{_h['수익률']:+.2f}% ({_h['평가손익']:+,.0f}원)</span>"
                                    f"</span></div>",
                                    unsafe_allow_html=True
                                )
                    else:
                        _tok = st.session_state.get('kis_token')
                        _tok_age = _time_kis.time() - st.session_state.get('kis_token_time', 0)
                        if _tok and _tok_age > 21600:
                            st.warning("⏰ KIS 토큰 만료 (6시간) — 페이지를 새로고침하면 자동 갱신됩니다.")
                        elif not _tok:
                            st.error("❌ KIS 토큰 없음 — API 키(KIS_APP_KEY / KIS_APP_SECRET)를 secrets에 등록해주세요.")
                        else:
                            st.warning("⚠️ 잔고 조회 실패 — KIS API 응답 오류. 잠시 후 새로고침해주세요.")

                with _kis_col2:
                    st.markdown("**📡 관심종목 실시간 현재가**")
                    for _t, _n in TICKERS[:5]:  # 상위 5개만
                        if is_korean_ticker(_t):
                            _price_data = kis_get_price(_t)
                            if _price_data:
                                _pc = 'up' if _price_data['등락률'] >= 0 else 'down'
                                st.markdown(
                                    f"<div style='display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1a2535'>"
                                    f"<span><b>{_n}</b> <span style='color:#64748b;font-size:11px'>({_t})</span></span>"
                                    f"<span class='{_pc}' style='font-family:IBM Plex Mono'>"
                                    f"{_price_data['현재가']:,.0f}원 ({_price_data['등락률']:+.2f}%)</span>"
                                    f"</div>",
                                    unsafe_allow_html=True
                                )

                if st.button("🔄 실시간 갱신", key="kis_refresh"):
                    st.rerun()

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
            col.markdown(f"<div style='font-size:10px; color:#64748b; text-transform:uppercase; letter-spacing:1px'>{h}</div>", unsafe_allow_html=True)
        st.markdown("<hr style='margin:6px 0; border-color:rgba(255,255,255,0.06)'>", unsafe_allow_html=True)

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
                    f"<span style='font-size:10px; color:#64748b'>{ticker}</span>",
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
                cols[0].markdown(f"<b style='font-size:13px'>{name}</b><br><span style='font-size:10px; color:#64748b; font-family:IBM Plex Mono'>{ticker}</span>", unsafe_allow_html=True)
                cols[1].markdown(f"<span style='font-family:IBM Plex Mono; font-size:13px; font-weight:600'>{l['종가']:,.0f}</span>", unsafe_allow_html=True)
                cols[2].markdown(f"<span class='{chg_color}' style='font-family:IBM Plex Mono; font-size:13px'>{chg:+.2f}%</span>", unsafe_allow_html=True)
                cols[3].markdown(f"<span style='color:{rsi_color}; font-family:IBM Plex Mono; font-size:13px'>{l['RSI']:.1f}</span>", unsafe_allow_html=True)
                cols[4].markdown(f"<span style='font-family:IBM Plex Mono; font-size:12px; color:#94a3b8'>{l['MA5']:,.0f}</span>", unsafe_allow_html=True)
                cols[5].markdown(f"<span style='font-family:IBM Plex Mono; font-size:12px; color:#94a3b8'>{l['MA20']:,.0f}</span>", unsafe_allow_html=True)
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
            c3.markdown(f"<div class='metric-card'><div class='label'>과매도(RSI≤35)</div><div class='value' style='color:#38bdf8'>{oversold}종목</div></div>", unsafe_allow_html=True)
            c4.markdown(f"<div class='metric-card'><div class='label'>과매수(RSI≥65)</div><div class='value' style='color:#f43f5e'>{overbought}종목</div></div>", unsafe_allow_html=True)


    # ══════════════════════════════════════════
    # 탭 2: 차트 분석
    # ══════════════════════════════════════════

st.markdown("---")
st.markdown("<div style='text-align:center;font-size:11px;color:rgba(255,255,255,0.1);font-family:IBM Plex Mono'>퀀트 관제탑 V8.9 | 투자 자문 아님 — 모든 손익의 책임은 본인에게 있습니다</div>", unsafe_allow_html=True)
