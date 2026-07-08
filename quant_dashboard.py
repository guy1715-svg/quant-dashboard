# ============================================================
# 퀀트 대시보드 — Streamlit + pykrx + Gemini + KIS API (V8.9)
# 실행: streamlit run quant_dashboard.py
# 설치: pip install -r requirements.txt
# ============================================================

# ── 통합 캐시 TTL 상수 ──────────────────────────────────────
GLOBAL_CACHE_TTL    = 300     # 기본 5분 (호가·기술지표)
FINANCIAL_CACHE_TTL = 86400   # 재무·시총 1일
REALTIME_CACHE_TTL  = 30      # 수급·체결 30초

# ── KIS API 활성화 여부 (환경변수 KIS_APP_KEY가 있으면 자동 활성화) ──
import os as _os_init
KIS_ENABLED = bool(_os_init.environ.get("KIS_APP_KEY", ""))

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import json
import json as _json
import warnings
warnings.filterwarnings('ignore')

# ── 페이지 설정 ──
st.set_page_config(
    page_title="퀀트 관제탑",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# ══════════════════════════════════════════
# 🔐 다중 사용자 인증 (사용자별 독립 데이터)
#
# secrets.toml 설정 예시:
#   [users.guy]
#   password = "내비밀번호"
#
#   [users.friend]
#   password = "친구비밀번호"
#
# ※ 구버전 호환: [auth] password = "..." 도 계속 지원
#    → 단일 사용자 "default" 로 처리
# ══════════════════════════════════════════

def _get_user_db() -> dict:
    """secrets에서 사용자 목록 반환. {username: password}"""
    try:
        _users_cfg = dict(st.secrets.get("users", {}))
        if _users_cfg:
            return {u: dict(v).get("password", "") for u, v in _users_cfg.items()}
    except Exception:
        pass
    # 구버전 호환: [auth] password
    try:
        _pw = st.secrets.get("auth", {}).get("password", "")
        if _pw:
            return {"default": _pw}
    except Exception:
        pass
    return {}

_AUTH_TOKEN_DAYS = 14   # 자동 로그인 유지 기간

def _make_auth_token(uid: str, pw: str) -> str:
    """uid.expiry.sig 형태 서명 토큰 — 비밀번호를 키로 HMAC 서명(위조 불가)."""
    import hmac, hashlib, base64, time
    _exp = int(time.time()) + _AUTH_TOKEN_DAYS * 86400
    _msg = f"{uid}.{_exp}"
    _sig = hmac.new(pw.encode(), _msg.encode(), hashlib.sha256).hexdigest()[:32]
    _raw = f"{_msg}.{_sig}"
    return base64.urlsafe_b64encode(_raw.encode()).decode()

def _verify_auth_token(token: str, user_db: dict):
    """토큰 검증 → 유효 시 uid 반환, 아니면 None. 만료/위조/사용자변경 시 무효."""
    import hmac, hashlib, base64, time
    try:
        _raw = base64.urlsafe_b64decode(token.encode()).decode()
        _uid, _exp, _sig = _raw.rsplit(".", 2)
        if int(_exp) < int(time.time()):
            return None                       # 만료
        _pw = user_db.get(_uid, "")
        if not _pw:
            return None                       # 사용자 없음
        _expect = hmac.new(_pw.encode(), f"{_uid}.{_exp}".encode(),
                           hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(_sig, _expect):
            return _uid
    except Exception:
        pass
    return None


def _check_auth() -> bool:
    """다중 사용자 세션 인증. 미로그인 시 로그인 폼 렌더링 후 st.stop().
    새로고침 유지: URL 쿼리파라미터의 서명 토큰으로 자동 로그인."""
    if st.session_state.get('_auth_ok'):
        return True

    _user_db = _get_user_db()

    # secrets에 사용자 없으면 인증 생략 (개발/로컬 환경)
    if not _user_db:
        st.session_state['_auth_ok'] = True
        st.session_state['_username'] = 'default'
        return True

    # ── 자동 로그인: URL 토큰 검증 (새로고침 유지) ──
    try:
        _tok = st.query_params.get("t", "")
    except Exception:
        _tok = ""
    if _tok:
        _uid_ok = _verify_auth_token(_tok, _user_db)
        if _uid_ok:
            st.session_state['_auth_ok']   = True
            st.session_state['_username']  = _uid_ok
            st.session_state['_auth_time'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            return True

    _is_multi = len(_user_db) > 1  # 사용자가 2명 이상이면 ID 입력 필드 표시

    # ── 로그인 화면 (단일 라인 HTML — 들여쓰기 시 마크다운이 코드블록 처리함) ──
    st.markdown(
        "<div style='text-align:center;margin:40px 0 8px'>"
        "<div style='font-size:48px;margin-bottom:12px'>📊</div>"
        "<div style='font-size:24px;font-weight:900;color:#f0f4ff;margin-bottom:6px'>퀀트 관제탑</div>"
        "<div style='font-size:13px;color:#64748b;margin-bottom:20px'>접근 권한이 필요합니다</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    if _is_multi:
        _inp_user = st.text_input("사용자 ID", placeholder="아이디를 입력하세요",
                                   label_visibility="collapsed", key="_auth_user_input")
    else:
        _inp_user = list(_user_db.keys())[0]  # 단일 사용자는 자동 선택

    _inp_pw = st.text_input("비밀번호", type="password",
                             placeholder="비밀번호를 입력하세요",
                             label_visibility="collapsed",
                             key="_auth_pw_input")
    _login_btn = st.button("🔓 입장", use_container_width=True,
                            type="primary", key="_auth_login_btn")

    if _login_btn:
        _uid = _inp_user.strip().lower() if _inp_user else ""
        _expected_pw = _user_db.get(_uid, "")
        if _uid and _expected_pw and _inp_pw == _expected_pw:
            st.session_state['_auth_ok']   = True
            st.session_state['_username']  = _uid
            st.session_state['_auth_time'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            # 새로고침 유지용 서명 토큰을 URL에 저장 (14일)
            try:
                st.query_params["t"] = _make_auth_token(_uid, _expected_pw)
            except Exception:
                pass
            st.rerun()
        elif not _uid:
            st.error("❌ 사용자 ID를 입력하세요.")
        else:
            st.error("❌ ID 또는 비밀번호가 틀렸습니다.")

    st.stop()
    return False

_check_auth()


import os
import os as _os
import gspread
from google.oauth2.service_account import Credentials

DEFAULT_WATCHLIST = "042700,한미반도체\n005930,삼성전자\n000660,SK하이닉스\n012450,한화에어로스페이스\n329180,HD현대중공업"

# ══════════════════════════════════════════
# Firebase Realtime Database 헬퍼
# ══════════════════════════════════════════
import firebase_admin
from firebase_admin import credentials as fb_credentials, db as fb_db

@st.cache_resource(show_spinner=False)
def _get_firebase_app():
    """Firebase Admin SDK 초기화 — 앱 전체 1회"""
    try:
        if not firebase_admin._apps:
            _fb_cfg = dict(st.secrets["firebase"])
            _fb_cred = fb_credentials.Certificate(_fb_cfg)
            _db_url  = st.secrets["firebase_config"]["database_url"]
            firebase_admin.initialize_app(_fb_cred, {"databaseURL": _db_url})
        return firebase_admin.get_app()
    except Exception as _e:
        # 에러 상세 미노출 — 내부 로그만 기록
        import logging as _logging
        _logging.error("Firebase 초기화 오류: %s", type(_e).__name__)
        return None

class _NullRef:
    """Firebase 미연결 시 get/set/push 호출이 조용히 실패하도록 하는 더미 레퍼런스"""
    def get(self): return None
    def set(self, v):
        st.toast("⚠️ DB 저장 지연: 세션에 임시 보관됩니다.", icon="🚨")
    def push(self, v):
        st.toast("⚠️ DB 저장 지연: 세션에 임시 보관됩니다.", icon="🚨")
    def update(self, v):
        st.toast("⚠️ DB 저장 지연: 세션에 임시 보관됩니다.", icon="🚨")

def _current_username() -> str:
    """현재 로그인된 사용자명 반환 (미로그인/단일사용자 시 'default')"""
    return st.session_state.get('_username', 'default') or 'default'

def _fb_ref(path: str):
    """
    Firebase DB 레퍼런스 반환 — 사용자별 경로 자동 분리.
    모든 경로는 /users/{username}{path} 로 저장됨.
    앱 미초기화 시 NullRef 반환(AttributeError 방지).
    """
    _app = _get_firebase_app()
    if _app is None:
        return _NullRef()
    try:
        _uid  = _current_username()
        _full = f"/users/{_uid}{path}"
        return fb_db.reference(_full)
    except Exception:
        return _NullRef()


# ══════════════════════════════════════════
# 3거래일 연속 1위 추적기 (Whipsaw 방지)
# ══════════════════════════════════════════
import json as _json_tracker
import os as _os_tracker

_LOCAL_TRACKER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "rotation_3day_tracker.json"
)


def _local_tracker_read() -> dict:
    """로컬 JSON 백업에서 추적 데이터 읽기. 파일 없거나 파싱 실패 시 {} 반환."""
    try:
        with open(_LOCAL_TRACKER_PATH, "r", encoding="utf-8") as _f:
            return _json_tracker.load(_f)
    except (FileNotFoundError, _json_tracker.JSONDecodeError, OSError):
        return {}


def _local_tracker_write(data: dict) -> None:
    """로컬 JSON 백업에 추적 데이터 덮어쓰기. 쓰기 실패는 조용히 무시."""
    try:
        with open(_LOCAL_TRACKER_PATH, "w", encoding="utf-8") as _f:
            _json_tracker.dump(data, _f, ensure_ascii=False)
    except OSError:
        pass


def _get_rotation_day_count(top1_ticker: str) -> dict:
    """
    오늘의 1위 ETF 티커를 Firebase(1순위) + 로컬 JSON(폴백)에 날짜 단위(1일 1회 Lock)로
    누적 기록하고, 연속 1위 일차(1~3)를 계산해 반환합니다.

    저장소 우선순위:
      1) Firebase /rotation_3day_tracker  (설정된 경우)
      2) rotation_3day_tracker.json       (Firebase 미설정 or 오류 시 자동 폴백)

    쓰기는 양쪽에 항상 동시 수행(듀얼 쓰기) — Firebase 장애 시에도 로컬 JSON이
    카운트를 보존하므로 휩쏘 방어 로직이 마비되지 않음.

    반환 dict:
      count     (int)  : 현재 연속 1위 일차 (1 / 2 / 3)
      ticker    (str)  : 기록된 1위 티커
      last_date (str)  : 마지막 기록일 (YYYY-MM-DD, KST)
      is_locked (bool) : True = 오늘 이미 기록 완료 (날짜 Lock 적용 중)

    날짜 Lock 원칙:
      - last_date == 오늘 이면 쓰기 없이 저장값 그대로 반환
        → 장중 버튼 수십 번 눌러도 count 가 올라가지 않음
      - last_date 가 오늘이 아니고 ticker 동일 + 5 캘린더일 이내 → count + 1 (최대 3)
      - ticker 가 바뀌었거나 5일 초과 공백 → count = 1 (강제 리셋)
    """
    import datetime as _dt
    _kst_today = (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y-%m-%d")

    # ── [하이브리드 READ] Firebase 우선, 실패/비설정 시 로컬 JSON 폴백 ──
    _ref        = _fb_ref("/rotation_3day_tracker")
    _fb_data    = _ref.get()          # NullRef.get() → None
    _use_local  = (_fb_data is None)  # Firebase 미설정 or 네트워크 오류
    _data       = _fb_data if not _use_local else _local_tracker_read()

    _stored_ticker = str(_data.get("ticker", ""))
    _stored_count  = int(_data.get("count", 0))
    _stored_date   = str(_data.get("last_date", ""))

    # ── 날짜 Lock: 오늘 이미 기록됐으면 쓰기 없이 반환 ──
    if _stored_date == _kst_today:
        return {
            "count": _stored_count,
            "ticker": _stored_ticker,
            "last_date": _stored_date,
            "is_locked": True,
        }

    # ── 연속성 판단 ──
    _is_consecutive = False
    if _stored_date and _stored_ticker == top1_ticker:
        try:
            _prev_d  = _dt.datetime.strptime(_stored_date, "%Y-%m-%d").date()
            _today_d = _dt.datetime.strptime(_kst_today,   "%Y-%m-%d").date()
            _gap     = (_today_d - _prev_d).days
            # 1~5 캘린더일 허용 (주말 2일 + 공휴일 최대 3일 포함)
            _is_consecutive = (1 <= _gap <= 5)
        except Exception:
            pass

    _new_count = min(_stored_count + 1, 3) if _is_consecutive else 1
    _new_data  = {"ticker": top1_ticker, "count": _new_count, "last_date": _kst_today}

    # ── [듀얼 WRITE] Firebase + 로컬 JSON 동시 저장 ──
    _ref.set(_new_data)               # NullRef.set() → 조용히 무시
    _local_tracker_write(_new_data)   # 항상 로컬 JSON에도 덮어씀

    return {
        "count": _new_count,
        "ticker": top1_ticker,
        "last_date": _kst_today,
        "is_locked": False,
    }


def _get_pension_scan_streak(today_tickers: list) -> tuple:
    """
    연기금 스캐너 결과를 Firebase에 날짜별로 저장하고,
    각 티커가 연속 몇 일째 리스트에 등장했는지 반환합니다.

    반환:
      streak_map  (dict) : {ticker: 연속등장일수(int)}
      is_locked   (bool) : 오늘 이미 기록 완료 여부 (날짜 Lock)

    날짜 Lock 원칙:
      - 오늘 날짜 데이터가 이미 있으면 Firebase 쓰기 없이 그대로 사용
      - 최근 5일치만 Firebase에 유지 (자동 정리)
    """
    import datetime as _dt
    _kst_today = (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y-%m-%d")

    _ref     = _fb_ref("/pension_scan_history")
    _history = _ref.get() or {}

    _is_locked = _kst_today in _history

    if not _is_locked:
        _history[_kst_today] = today_tickers
        # 최근 5일치만 유지
        _keep = sorted(_history.keys(), reverse=True)[:5]
        _history = {d: _history[d] for d in _keep}
        _ref.set(_history)

    # 날짜 내림차순 정렬
    _sorted_dates = sorted(_history.keys(), reverse=True)

    _streak_map: dict = {}
    for _tk in today_tickers:
        _cnt = 0
        for _d in _sorted_dates:
            if _tk in (_history.get(_d) or []):
                _cnt += 1
            else:
                break  # 연속성 끊김 → 카운트 종료
        _streak_map[_tk] = _cnt

    return _streak_map, _is_locked

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

@st.cache_resource(ttl=21600, show_spinner=False)  # 6시간 (금요일 발급 → 월요일 장전 만료 방지)
def _get_kis_token_cached():
    """KIS API 접근 토큰 발급 — cache_resource로 격리 (6시간 TTL)"""
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
            return _token
    except Exception:
        pass
    return None

def kis_get_token():
    """KIS API 접근 토큰 발급 — 6시간 TTL 자동 갱신"""
    return _get_kis_token_cached()

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
        _price = int(_data.get("stck_prpr", 0)) if _data else 0
        if _data and _price > 0:
            return {
                "현재가":    _price,
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
    except Exception:
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
        if _out and isinstance(_out, list) and len(_out) > 0:
            _latest = _out[0]
            if isinstance(_latest, dict):
                return {
                    "외인순매수":  int(_latest.get("frgn_ntby_qty", 0)),
                    "기관순매수":  int(_latest.get("orgn_ntby_qty", 0)),
                    "개인순매수":  int(_latest.get("prsn_ntby_qty", 0)),
                }
    except Exception:
        pass
    return None

def kis_get_org_net_daily(ticker, days=10):
    """종목별 일별 '기관 순매수 수량' 리스트 — KIS FHKST01010900(외인기관 추정).
    반환: (org_list_oldest_first, foreign_total) 또는 (None, 0).
    org_list: 최근 days일의 기관 순매수(오래된→최신 순). 연기금은 기관에 포함됨."""
    try:
        _token = kis_get_token()
        if not _token:
            return None, 0
        _key    = st.secrets["KIS_APP_KEY"]
        _secret = st.secrets["KIS_APP_SECRET"]
        _res = _requests.get(_KIS_URL_INVESTOR, headers={
            "authorization": f"Bearer {_token}",
            "appkey":        _key,
            "appsecret":     _secret,
            "tr_id":         "FHKST01010900",
        }, params={
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd":         ticker,
        }, timeout=5)
        _out = _res.json().get("output", [])
        if not (_out and isinstance(_out, list)):
            return None, 0
        _org, _for_tot = [], 0.0
        for _row in _out[:days]:          # 최신 → 과거 순
            if not isinstance(_row, dict):
                continue
            try:
                _org.append(float(str(_row.get("orgn_ntby_qty", 0)).replace(",", "") or 0))
            except (TypeError, ValueError):
                _org.append(0.0)
            try:
                _for_tot += float(str(_row.get("frgn_ntby_qty", 0)).replace(",", "") or 0)
            except (TypeError, ValueError):
                pass
        if not _org:
            return None, 0
        _org.reverse()                    # 오래된 → 최신 (연속일 계산용)
        return _org, _for_tot
    except Exception:
        return None, 0


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
                return True, f"{_ev_name} {_diff:.0f}시간 이내"
        except:
            pass
    return False, ""

@st.cache_data(ttl=120, show_spinner=False)
def get_index_quotes():
    """★ 지수/매크로 단일 소스(Single Source of Truth).
    코스피·코스닥 = FinanceDataReader(KRX 정확), 나스닥·환율·유가·VIX = yfinance.
    반환: {name: {'현재': float, '등락': float(%)}}. 모든 화면(헤더·경고·브리핑)이 이걸 참조."""
    _r = {}
    try:
        import FinanceDataReader as _fdr
        _end = datetime.now().strftime('%Y-%m-%d')
        _start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        for _n, _s in [("코스피", "KS11"), ("코스닥", "KQ11")]:
            try:
                _h = _fdr.DataReader(_s, _start, _end).dropna(subset=['Close'])
                if len(_h) >= 2:
                    _c = float(_h['Close'].iloc[-1]); _p = float(_h['Close'].iloc[-2])
                    if _c > 0 and _p > 0:
                        _r[_n] = {'현재': _c, '등락': (_c/_p-1)*100}
            except Exception:
                pass
    except ImportError:
        pass
    try:
        import yfinance as _yf2
        for _n, _s in [("나스닥", "^IXIC"), ("달러/원", "KRW=X"), ("VIX", "^VIX"), ("WTI유가", "CL=F")]:
            try:
                _h = _yf2.Ticker(_s).history(period="5d", interval="1d").dropna(subset=['Close'])
                if len(_h) >= 2:
                    _c = float(_h['Close'].iloc[-1]); _p = float(_h['Close'].iloc[-2])
                    if _c > 0 and _p > 0:
                        _r[_n] = {'현재': _c, '등락': (_c/_p-1)*100}
            except Exception:
                pass
    except Exception:
        pass
    return _r


@st.cache_data(ttl=300, show_spinner=False)
def check_index_shutdown():
    try:
        # 단일 소스(get_index_quotes)에서 코스피/코스닥 등락 참조 (헤더와 값 일치)
        _q = get_index_quotes()
        _kospi_chg  = round(_q.get("코스피", {}).get("등락", 0), 2)
        _kosdaq_chg = round(_q.get("코스닥", {}).get("등락", 0), 2)
        if _kospi_chg <= -2.0 or _kosdaq_chg <= -2.0:
            _reason = (
                f"🚨 지수 셧다운 — 코스피 {_kospi_chg:+.2f}% / 코스닥 {_kosdaq_chg:+.2f}% "
                f"(-2.0% 급락) | 개별 지지선 무효 / 신규 매수 차단"
            )
            return True, _reason, _kospi_chg, _kosdaq_chg
        return False, "", _kospi_chg, _kosdaq_chg
    except Exception as _e:
        return False, f"지수 조회 오류: {_e}", 0, 0

# ── 전역 손절 비율 상수 ──────────────────────────────────────────────────────
# 이 두 값만 바꾸면 전체 손절가 로직에 일괄 반영됨
_STOP_LOSS_PCT  = 0.07   # 기본 손절: entry × (1 - 0.07) = -7%
_STOP_LOSS_HARD = 0.10   # 하드 서킷: entry × (1 - 0.10) = -10%

def fetch_realtime_price(ticker: str) -> float:
    """캐시 없이 실시간 현재가 조회 — 킬스위치/평가 전용 (TTL=0)"""
    try:
        import yfinance as _yf_rt
        _sym = f"{ticker}.KS" if (ticker.isdigit() and len(ticker) == 6) else ticker
        _fi  = _yf_rt.Ticker(_sym).fast_info
        _p   = getattr(_fi, 'last_price', None)
        if _p and float(_p) > 0:
            return float(_p)
        _h = _yf_rt.Ticker(_sym).history(period="1d", interval="1m")
        if _h is not None and not _h.empty:
            return float(_h['Close'].iloc[-1])
    except Exception:
        pass
    return 0.0

def check_global_drawdown_killswitch(current_total: float, prev_total: float) -> tuple:
    """
    전역 자산 낙폭 킬스위치 — 당일 총평가액이 전일 대비 -5% 이상이면 매수 전면 차단.
    Returns: (is_safe: bool, message: str)
    """
    if prev_total <= 0:
        return True, ""
    _dd = (current_total - prev_total) / prev_total * 100
    if _dd <= -5.0:
        return False, (
            f"🚨 [전역 낙폭 킬스위치] 총평가액 {_dd:+.2f}% (임계 -5%) — "
            f"모든 신규 매수 차단. 수동 확인 후 재개하십시오."
        )
    return True, ""

def check_data_heartbeat(df, max_stale_seconds: int = 60) -> tuple:
    """
    시세 데이터 신선도 체크 — 마지막 타임스탬프가 max_stale_seconds 초 초과 시 매매 차단.
    Returns: (is_fresh: bool, message: str)
    """
    try:
        from datetime import datetime, timezone
        if df is None or df.empty:
            return False, "⚠️ 데이터 없음 — 시세 서버 연결 확인 필요"
        _last_ts = df.index[-1]
        if hasattr(_last_ts, 'tzinfo') and _last_ts.tzinfo is not None:
            _now = datetime.now(timezone.utc)
            _last_ts_cmp = _last_ts.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
        else:
            _now = datetime.now()
            _last_ts_cmp = _last_ts.to_pydatetime() if hasattr(_last_ts, 'to_pydatetime') else _last_ts
        _stale_sec = (_now - _last_ts_cmp).total_seconds()
        if _stale_sec > max_stale_seconds:
            return False, f"⚠️ 시세 {int(_stale_sec)}초 경과 — 실시간 연결 끊김 가능성"
    except Exception:
        return True, ""
    return True, ""

def check_smart_killswitch(ticker, entry_price, current_price):
    if entry_price <= 0:
        return 'SAFE', ""
    _chg_pct = (current_price - entry_price) / entry_price * 100
    if _chg_pct <= -(_STOP_LOSS_HARD * 100):
        return 'EXECUTE_MARKET_SELL', (
            f"🚨 하드 서킷 브레이커! 진입가 {entry_price:,.0f} 대비 {_chg_pct:.2f}% "
            f"(-{_STOP_LOSS_HARD*100:.0f}%) → EXECUTE_MARKET_SELL"
        )
    if _chg_pct <= -(_STOP_LOSS_PCT * 100):
        try:
            import yfinance as yf
            _is_korean = ticker.isdigit() and len(ticker) == 6
            _sym = f"{ticker}.KS" if _is_korean else ticker
            _df  = yf.Ticker(_sym).history(period="10d", interval="1d")
            if _df is not None and len(_df) >= 6:
                _vol_today = float(_df['Volume'].iloc[-1])
                _vol_5d    = float(_df['Volume'].iloc[-6:-1].mean())
                _vol_ratio = _vol_today / _vol_5d if _vol_5d > 0 else 1.0
                if _vol_ratio < 0.5:
                    return 'HOLD_AND_VERIFY_1HR', (
                        f"⚠️ 스마트 킬스위치 — {_chg_pct:.2f}% (거래량 {_vol_ratio*100:.0f}% — 투매 아님) → HOLD_AND_VERIFY_1HR"
                    )
                else:
                    return 'EXECUTE_MARKET_SELL', (
                        f"🚨 킬스위치 — {_chg_pct:.2f}% (거래량 {_vol_ratio*100:.0f}% — 실제 투매) → EXECUTE_MARKET_SELL"
                    )
        except Exception as _kse:
            return 'EXECUTE_MARKET_SELL', (
                f"🚨 킬스위치 {_chg_pct:.2f}% — 거래량 조회 실패({_kse}), 보수적 매도 권고"
            )
        return 'EXECUTE_MARKET_SELL', f"🚨 킬스위치 — {_chg_pct:.2f}% → EXECUTE_MARKET_SELL"
    return 'SAFE', ""

def check_reentry_allowed(ticker, kill_date_str, df=None):
    """
    손절 후 재진입 가능 여부 — Gemini T2 모범 답안 3단계 필터
    1. 쿨링오프: 손절일로부터 3 거래일 경과
    2. 조건 회복: 종가 > MA20 & 거래량 실린 돌파
    3. 지표 복원: RSI 40 상향 돌파 또는 이전 저점 위 지지 확인
    Returns: (can_reenter: bool, reason: str)
    """
    from datetime import datetime as _dt_re, timedelta as _td_re
    import numpy as np
    try:
        _kill_dt = _dt_re.strptime(kill_date_str, '%Y-%m-%d')
        _elapsed = (_dt_re.now() - _kill_dt).days
        if _elapsed < 3:
            return False, f"쿨링오프 중 ({_elapsed}일 경과 / 최소 3거래일 필요)"

        if df is None or len(df) < 20:
            return False, "데이터 부족 — 조건 확인 불가"

        _cl   = df['종가'] if '종가' in df.columns else df['Close']
        _vol  = df['거래량'] if '거래량' in df.columns else df['Volume']
        _ma20 = _cl.rolling(20).mean()
        _cur  = float(_cl.iloc[-1])
        _m20  = float(_ma20.iloc[-1]) if not np.isnan(_ma20.iloc[-1]) else _cur

        # 조건 회복: 종가 > MA20 + 거래량 > 5일 평균 120%
        _vol_ratio = float(_vol.iloc[-1]) / float(_vol.tail(5).mean()) if float(_vol.tail(5).mean()) > 0 else 0
        _above_ma20 = _cur > _m20
        _vol_ok     = _vol_ratio >= 1.2

        # RSI 복원
        _d = _cl.diff()
        _g = _d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        _l = (-_d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        _rsi_now  = float(100 - 100 / (1 + _g.iloc[-1] / max(_l.iloc[-1], 1e-9)))
        _rsi_prev = float(100 - 100 / (1 + _g.iloc[-2] / max(_l.iloc[-2], 1e-9))) if len(_cl) >= 2 else _rsi_now
        _rsi_cross40 = _rsi_now >= 40 and _rsi_prev < 40

        if _above_ma20 and _vol_ok and (_rsi_cross40 or _rsi_now >= 45):
            return True, f"재진입 허용 — MA20 돌파 + 거래량 {_vol_ratio*100:.0f}% + RSI {_rsi_now:.1f}"
        elif _above_ma20 and not _vol_ok:
            return False, f"MA20 위이나 거래량 부족 ({_vol_ratio*100:.0f}%) — 돌파 확인 대기"
        elif not _above_ma20:
            return False, f"MA20({_m20:,.0f}) 미돌파 — 현재가 {_cur:,.0f}"
        else:
            return False, f"RSI {_rsi_now:.1f} — 40 상향 돌파 대기"
    except Exception as _e:
        return False, f"조건 확인 오류: {_e}"

def run_v891_system_check(ticker="", entry_price=0, current_price=0):
    # 무인수 호출(진입 여부만 체크)은 5분 캐시 재사용
    _cache_key = '_v891_base_cache'
    import time as _t
    _cached = st.session_state.get(_cache_key)
    if _cached and _t.time() - _cached.get('_ts', 0) < 300 and not ticker:
        return _cached

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
    _result = {
        'can_enter':  _can_enter,
        'killswitch': _killswitch,
        'alerts':     _alerts,
        'blackout':   _bo,
        'shutdown':   _sd,
        'kospi_chg':  _kospi_chg,
        'kosdaq_chg': _kosdaq_chg,
        '_ts':        _t.time(),
    }
    if not ticker:
        st.session_state[_cache_key] = _result
    return _result

# ══════════════════════════════════════════
# Google Sheets — 관심종목용 (호환성 유지)
# ══════════════════════════════════════════

_GS_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

@st.cache_resource(show_spinner=False)
def _get_gspread_workbook():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=_GS_SCOPES
    )
    return gspread.authorize(creds).open_by_key(st.secrets["SHEET_ID"])

def get_gsheet():
    return _get_gspread_workbook().sheet1

# ══════════════════════════════════════════
# 페이퍼 트레이딩 백엔드 (Firebase 기반)
# ══════════════════════════════════════════

def _safe_json(s, default=None):
    """JSON 파싱 실패 시 default 반환"""
    if default is None:
        default = []
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

def load_account():
    """가상 계좌 로드 — Firebase 우선 (세션 캐시는 5분 TTL)"""
    import time as _t_acc
    _now_acc = _t_acc.time()
    # 5분 이내 캐시면 바로 반환
    if ('paper_account' in st.session_state and
            _now_acc - st.session_state.get('_paper_account_ts', 0) < 300):
        return st.session_state.paper_account
    try:
        data = _fb_ref("/quant_account").get()
        if data:
            # Firebase stores lists as {"0":{...},"1":{...}} dicts — convert back
            _pos_raw = data.get('positions', [])
            if isinstance(_pos_raw, dict):
                _pos_raw = list(_pos_raw.values())
            acc = {
                'initial':   float(data.get('initial', 10000000)),
                'cash':      float(data.get('cash', 10000000)),
                'positions': _pos_raw,
                'peak':      float(data.get('peak', 10000000)),
                'trough':    float(data.get('trough', 10000000)),
            }
            st.session_state.paper_account = acc
            st.session_state['_paper_account_ts'] = _now_acc
            return acc
    except Exception:
        pass
    # Firebase 실패 시 기존 세션 데이터라도 반환
    if 'paper_account' in st.session_state:
        return st.session_state.paper_account
    default = {'initial':10000000,'cash':10000000,'positions':[],'peak':10000000,'trough':10000000}
    st.session_state.paper_account = default
    st.session_state['_paper_account_ts'] = _now_acc
    return default

def save_account(acc):
    """가상 계좌 저장 — Firebase + session_state 이중 저장"""
    st.session_state.paper_account = acc
    # 직렬화 가능한 형태로 정제 (datetime 등 제거)
    try:
        import json as _json_sa
        _clean = _json_sa.loads(_json_sa.dumps(acc, default=str))
    except Exception:
        _clean = acc
    # Firebase 저장
    _fb_ok = False
    try:
        _fb_ref("/quant_account").set(_clean)
        _fb_ok = True
        st.session_state['_paper_account_ts'] = 0  # 다음 load_account에서 Firebase 재읽기 강제
    except Exception as _e:
        st.session_state['_save_account_err'] = str(_e)
    # Firebase 미설정/실패 시 경고 표시
    if not _fb_ok:
        _err = st.session_state.get('_save_account_err', 'Firebase 미설정')
        st.warning(f"⚠️ Firebase 저장 실패: {_err} — 새로고침 시 데이터가 사라질 수 있습니다. (Streamlit Secrets → firebase_credentials 확인)")

def save_op_positions(positions: list):
    """실전 운용 포지션 저장 — Firebase + session_state"""
    st.session_state['op_positions'] = positions
    try:
        import json as _json_op
        _fb_ref("/op_positions").set({"data": _json_op.dumps(positions, default=str)})
    except Exception as _e:
        pass  # Firebase 미설정 시 session_state만 유지

def load_op_positions() -> list:
    """실전 운용 포지션 로드 — Firebase 우선, 없으면 session_state"""
    # 이미 세션에 있으면 바로 반환
    if st.session_state.get('op_positions'):
        return st.session_state['op_positions']
    try:
        import json as _json_op
        _raw = _fb_ref("/op_positions").get()
        if _raw and isinstance(_raw, dict) and _raw.get("data"):
            _loaded = _json_op.loads(_raw["data"])
            if isinstance(_loaded, list) and _loaded:
                st.session_state['op_positions'] = _loaded
                return _loaded
    except Exception:
        pass
    return []


# 국내 ETF 과세 구분 — 국내 주식형(비과세) vs 해외/원자재형(15.4% 배당소득세)
# 코드가 isdigit() == True → KR ETF 판단, 추가로 해외형 여부 판별
_OVERSEAS_ETF_TAX_CODES = {
    # 해외지수/원자재 추종 → 매매차익 15.4% 배당소득세 과세
    "133690","379800","360750","161490","299030","381170","438330",
    "465580","469670","472640","487690","487710",
}

def is_overseas_tax_etf(ticker: str) -> bool:
    """해외형 ETF 여부 (매매차익 15.4% 과세 대상)"""
    return str(ticker).strip() in _OVERSEAS_ETF_TAX_CODES

def calc_slippage(price, is_buy, is_korean=True, ticker: str = ""):
    """슬리피지 + 수수료 + 세금 계산
    - 한국 주식/주식형 ETF: 매도 시 증권거래세 0.18%
    - 해외형/원자재 ETF: 매도 시 배당소득세 15.4% (수익 구간에만 적용 — 근사치로 0.154 반영)
    - 미국 주식: 세금 없음 (양도세는 연간 250만원 초과분만, 개별 거래 반영 안 함)
    """
    commission = 0.00015   # 증권사 수수료 0.015%
    slippage   = 0.001     # 슬리피지 0.1%
    if not is_buy:
        if is_korean and is_overseas_tax_etf(ticker):
            tax = 0.0          # 과세는 수익 구간에만 — 개별 거래 반영 생략 (연말 정산)
        elif is_korean:
            tax = 0.0018       # 국내 주식/주식형 ETF 거래세 0.18%
        else:
            tax = 0.0          # 미국 주식: 거래세 없음
    else:
        tax = 0.0
    total_cost = commission + slippage + tax
    if is_buy:
        return round(price * (1 + total_cost))
    else:
        return round(price * (1 - total_cost))

def log_trade(ticker, name, action, qty, price, net_price, cash_after,
              eval_total, ai_score=0, adx=0, zscore=0, memo=""):
    """거래 일지 기록 — Firebase (영구) + session_state"""
    from datetime import datetime as _dt
    import time as _t
    now = _dt.now()
    _row = {
        '날짜':     now.strftime('%Y-%m-%d'),
        '시간':     now.strftime('%H:%M:%S'),
        '종목코드': ticker,
        '종목명':   name,
        '매매':     action,
        '수량':     int(qty),
        '체결단가': float(price),
        '수수료':   round(float(price) * 0.00015),
        '슬리피지': round(float(price) * 0.001),
        '순체결가': float(net_price),
        '잔고':     float(cash_after),
        '평가금액': float(eval_total),
        '5AI점수':  ai_score,
        'ADX':      adx,
        'Z-Score':  zscore,
        '메모':     memo,
    }
    # Firebase 저장 성공 시 session_state에는 저장 안 함 (중복 표시 방지)
    _fb_ok = False
    try:
        _key = now.strftime('%Y%m%d_%H%M%S_') + ticker
        _fb_ref(f"/quant_trades/{_key}").set(_row)
        _fb_ok = True
    except Exception as _e:
        st.session_state['_trade_log_err'] = str(_e)

    # Firebase 실패 시에만 session_state에 임시 저장 (폴백)
    if not _fb_ok:
        if 'local_trade_log' not in st.session_state:
            st.session_state.local_trade_log = []
        st.session_state.local_trade_log.append(_row)

def _load_trade_log_firebase():
    """Firebase에서 거래기록 전체 로드"""
    try:
        data = _fb_ref("/quant_trades").get()
        if data:
            return sorted(data.values(), key=lambda x: x.get('날짜','') + x.get('시간',''))
    except Exception:
        pass
    return []

def save_analysis_log(ticker, name, verdict, rr, entry, stop, target1, target2, preset="", score=0, source="분석탭"):
    """분석 기록을 Firebase에 저장 (중복 방지: 같은 종목·분·판정은 1회만)."""
    from datetime import datetime as _dt
    now = _dt.now()
    # ── 중복 방지: 종목코드 + 분(minute) + 판정 + 출처 동일하면 스킵 ──
    _dedup_key = f"{ticker}_{now.strftime('%Y%m%d_%H%M')}_{verdict}_{source}"
    _seen = st.session_state.setdefault('_analysis_saved_keys', set())
    if _dedup_key in _seen:
        return   # 직전 저장과 동일 → 스킵 (rerun 중복 방지)
    if len(_seen) > 500:          # 세션 무한 증가 방지 — 오래된 키 비움
        _seen.clear()
    _seen.add(_dedup_key)

    def _f(x):                    # None/NaN 안전 float
        try:
            v = float(x)
            return v if v == v else 0.0
        except (TypeError, ValueError):
            return 0.0

    _row = {
        '날짜':   now.strftime('%Y-%m-%d'),
        '시간':   now.strftime('%H:%M:%S'),
        '종목코드': ticker,
        '종목명':   name,
        '판정':     verdict,
        'R:R':      _f(rr),
        '진입가':   _f(entry),
        '손절가':   _f(stop),
        '목표1':    _f(target1),
        '목표2':    _f(target2),
        '프리셋':   preset,
        '점수':     int(_f(score)),
        '출처':     source,
    }
    try:
        _key = now.strftime('%Y%m%d_%H%M%S_') + ticker
        _fb_ref(f"/quant_analysis/{_key}").set(_row)
    except Exception:
        if 'local_analysis_log' not in st.session_state:
            st.session_state.local_analysis_log = []
        st.session_state.local_analysis_log.append(_row)

def load_analysis_log(limit=50):
    """Firebase에서 분석 기록 로드"""
    rows = []
    try:
        data = _fb_ref("/quant_analysis").get()
        if data:
            rows = sorted(data.values(), key=lambda x: x.get('날짜','') + x.get('시간',''), reverse=True)
    except Exception:
        pass
    rows += st.session_state.get('local_analysis_log', [])
    return rows[:limit]

def get_position(acc, ticker):
    """보유 포지션 조회"""
    for p in acc['positions']:
        if p['ticker'] == ticker:
            return p
    return None

def calc_portfolio_value(acc):
    """총 평가금액 계산 (원화 기준, 미국주식 USD→KRW 환산)"""
    # 환율 조회 (미국 포지션 있을 때만)
    _has_us = any(not is_korean_ticker(p['ticker']) for p in acc.get('positions', []))
    _usd_krw = get_usd_krw() if _has_us else 1350.0
    total = acc['cash']
    for pos in acc['positions']:
        _is_kr = is_korean_ticker(pos['ticker'])
        _fx = 1.0 if _is_kr else _usd_krw
        try:
            df = fetch_ohlcv(pos['ticker'], 5)
            cur_price = float(df['종가'].iloc[-1]) if (df is not None and not df.empty) else float('nan')
            # NaN/0/음수 가격이면 평단가로 대체 (총액 NaN 오염 방지)
            if not (cur_price == cur_price) or cur_price <= 0:
                cur_price = pos['avg_price']
            total += cur_price * pos['qty'] * _fx
        except Exception:
            total += pos['avg_price'] * pos['qty'] * _fx
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
    """Firebase에서 관심종목 로드 — Sheets 폴백"""
    # 1) Firebase 우선
    try:
        data = _fb_ref("/quant_watchlist").get()
        if data:
            lines = [f"{v['ticker']},{v['name']}" for v in data.values()
                     if isinstance(v, dict) and v.get('ticker')]
            if lines:
                return "\n".join(lines)
    except Exception:
        pass
    # 2) Google Sheets 폴백
    try:
        ws = get_gsheet()
        rows = ws.get_all_values()
        if rows:
            parsed = "\n".join([",".join(r[:2]) for r in rows if len(r) >= 2 and r[0].strip()])
            if parsed.strip():
                return parsed
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
    """관심종목 전체 저장 — session_state + Firebase + Sheets 동시 저장"""
    st.session_state.watchlist_data = text
    # Firebase 저장 (주 저장소)
    try:
        pairs = [l.strip().split(",", 1) for l in text.strip().split("\n")
                 if "," in l.strip()]
        _fb_ref("/quant_watchlist").set(
            {p[0].strip(): {"ticker": p[0].strip(), "name": p[1].strip()}
             for p in pairs if len(p) == 2}
        )
    except Exception as _fe:
        st.warning(f"⚠️ Firebase 저장 오류: {_fe}")
    # Sheets 폴백 저장
    try:
        ws = get_gsheet()
        rows = [[p.strip() for p in l.split(",", 1)]
                for l in text.strip().split("\n")
                if "," in l and l.strip()]
        ws.clear()
        if rows:
            ws.update("A1", rows)
    except Exception:
        pass

def get_watchlist_tickers():
    return _parse_watchlist(get_watchlist())

def _validate_ticker(ticker: str) -> bool:
    """종목코드 형식 검증 — 한국(6자리 숫자) 또는 미국(1~6자 영문+숫자, 특수문자 불허)"""
    import re as _re_v
    if not ticker or len(ticker) > 10:
        return False
    return bool(_re_v.match(r'^[A-Za-z0-9]{1,10}$', ticker))

def add_ticker(ticker, name):
    """관심종목 1개 추가 — Firebase 저장"""
    if not _validate_ticker(ticker):
        return False
    name = str(name)[:30]  # 종목명 최대 30자 제한
    wl = get_watchlist()
    existing = [t for t, _ in _parse_watchlist(wl)]
    if ticker in existing:
        return False
    new_wl = wl.strip() + f"\n{ticker},{name}"
    # session_state 즉시 반영
    st.session_state.watchlist_data = new_wl
    # all_data 캐시 무효화 (새 종목은 다음 로드 시 신규 데이터 취득)
    st.session_state.get('all_data_cache', {}).pop(ticker, None)
    # Firebase 저장
    try:
        _fb_ref(f"/quant_watchlist/{ticker}").set({"ticker": ticker, "name": name})
    except Exception as _e:
        st.error(f"⚠️ Firebase 저장 실패: {_e}")
    return True

def remove_ticker_from_firebase(ticker):
    """Firebase에서 종목 삭제"""
    try:
        _fb_ref(f"/quant_watchlist/{ticker}").delete()
    except Exception:
        pass

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
    # 캐시에서도 즉시 제거
    st.session_state.get('all_data_cache', {}).pop(ticker, None)
    remove_ticker_from_sheets(new_text)

# session_state 초기화
if 'passed' not in st.session_state:
    st.session_state.passed = []
# watchlist_data는 get_watchlist() 첫 호출 시 Firebase에서 자동 로드

# ── 스타일 (반응형 — Desktop / Mobile) ──
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&family=IBM+Plex+Mono:wght@400;600&display=swap');

/* ══════════════════════════════════════
   CSS 변수 (테마 토큰)
══════════════════════════════════════ */
:root {
    --bg-base:    #f5f7fa;
    --bg-card:    #ffffff;
    --bg-sidebar: #eef2f7;
    --border:     #e2e8f0;
    --accent:     #3b82f6;
    --accent2:    #6366f1;
    --text-pri:   #0f172a;
    --text-sec:   #475569;
    --text-dim:   #94a3b8;
    --up:         #dc2626;
    --down:       #2563eb;
    --green:      #16a34a;
    --shadow-sm:  0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);
    --shadow-md:  0 4px 12px rgba(0,0,0,0.08), 0 2px 4px rgba(0,0,0,0.04);
    --font-body:  'Noto Sans KR', sans-serif;
    --font-mono:  'IBM Plex Mono', monospace;
    --fs-xs:   11px;
    --fs-sm:   13px;
    --fs-md:   15px;
    --fs-lg:   17px;
    --fs-xl:   22px;
    --fs-2xl:  30px;
    --card-pad: 16px 20px;
    --radius:   12px;
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
    background: #f5f7fa;
}
/* 섹션 헤더 스타일 */
h1, h2, h3 { color: #0f172a !important; font-weight: 700 !important; }
h4 { color: #1e293b !important; font-weight: 600 !important; }
/* 구분선 */
hr { border-color: #e2e8f0 !important; }

/* ══════════════════════════════════════
   사이드바
══════════════════════════════════════ */
[data-testid="stSidebar"] {
    background: #ffffff;
    border-right: 1px solid var(--border);
    box-shadow: 2px 0 8px rgba(0,0,0,0.04);
}
[data-testid="stSidebar"] * { font-size: var(--fs-sm) !important; }
[data-testid="stSidebar"] h2 { font-size: var(--fs-md) !important; }

/* ══════════════════════════════════════
   탭
══════════════════════════════════════ */
.stTabs [data-baseweb="tab-list"] {
    background: #ffffff;
    border-radius: var(--radius);
    padding: 4px;
    border: 1px solid var(--border);
    box-shadow: var(--shadow-sm);
    gap: 2px;
    flex-wrap: wrap;
    position: sticky;
    top: 0;
    z-index: 100;
}
.stTabs [data-baseweb="tab"] {
    border-radius: 8px;
    color: var(--text-sec);
    font-weight: 600;
    font-size: var(--fs-sm);
    padding: 7px 16px;
    transition: all 0.18s;
    white-space: nowrap;
}
.stTabs [data-baseweb="tab"]:hover {
    background: #f1f5f9;
    color: var(--text-pri);
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
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: var(--card-pad);
    margin-bottom: 10px;
    box-shadow: var(--shadow-sm);
    transition: box-shadow 0.2s, transform 0.15s;
}
.metric-card:hover {
    box-shadow: var(--shadow-md);
    transform: translateY(-1px);
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
    padding: 10px 20px !important;
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
    background: #ffffff !important;
    border: 1px solid var(--border) !important;
    color: var(--text-sec) !important;
    box-shadow: var(--shadow-sm) !important;
}
.stButton > button[kind="secondary"]:hover {
    background: #f8fafc !important;
    border-color: #94a3b8 !important;
    color: var(--text-pri) !important;
}
/* Streamlit 기본 메트릭 */
[data-testid="stMetric"] {
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 18px;
    box-shadow: var(--shadow-sm);
}
[data-testid="stMetricLabel"] { color: var(--text-sec) !important; font-size: 12px !important; }
[data-testid="stMetricValue"] { color: var(--text-pri) !important; font-weight: 700 !important; }
/* 데이터프레임 */
[data-testid="stDataFrame"] { border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow-sm); }
/* expander */
[data-testid="stExpander"] {
    border: 1px solid var(--border) !important;
    border-radius: var(--radius) !important;
    background: #ffffff !important;
    box-shadow: var(--shadow-sm) !important;
}
/* selectbox */
[data-baseweb="select"] > div {
    background: #ffffff !important;
    border-color: var(--border) !important;
    border-radius: 8px !important;
    color: var(--text-pri) !important;
}

/* ══════════════════════════════════════
   입력 필드
══════════════════════════════════════ */
.stTextInput input, .stNumberInput input,
.stSelectbox select, textarea {
    background: #ffffff !important;
    border: 1px solid rgba(0,0,0,0.15) !important;
    border-radius: 10px !important;
    color: #1e293b !important;
    font-size: var(--fs-sm) !important;
}
.stTextInput input::placeholder, textarea::placeholder {
    color: #94a3b8 !important;
}
.stTextInput input:focus, .stNumberInput input:focus, textarea:focus {
    border-color: rgba(99,102,241,0.6) !important;
    box-shadow: 0 0 0 2px rgba(99,102,241,0.15) !important;
}
[data-testid="stSidebar"] .stTextInput input,
[data-testid="stSidebar"] .stNumberInput input {
    color: #1e293b !important;
    background: #ffffff !important;
}
[data-testid="stSidebar"] .stTextInput input::placeholder {
    color: #94a3b8 !important;
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
# 데이터 무결성 검증 계층 (Validation Layer)
# 정확성 > 속도 — 자산 운용 시스템의 신뢰성 기반
# ══════════════════════════════════════════

# 마스터 ETF DB: KRX 공식 코드 기준 (코드 → 공식명)
# 외부 데이터소스(yfinance 등)가 이 DB와 충돌할 경우 이 DB를 우선함
_MASTER_ETF_DB: dict = {
    # 국내 지수
    "069500": "KODEX 200",
    "102110": "TIGER 200",
    "229200": "KODEX 코스닥150",
    "233740": "KODEX 코스닥150레버리지",
    "153130": "KODEX 단기채권PLUS",
    # 미국 지수 추종 (국내상장)
    "133690": "TIGER 나스닥100",
    "379800": "KODEX 미국S&P500TR",
    "360750": "TIGER 미국S&P500",
    "161490": "TIGER 미국나스닥100",
    "299030": "KODEX 미국나스닥100TR",
    "381170": "TIGER 미국테크TOP10 INDXX",
    # 반도체 / IT
    "091160": "KODEX 반도체",
    "395160": "KODEX AI반도체TOP2+",
    "396500": "TIGER Fn반도체TOP10",   # ← 수정: 441680은 오매핑이었음
    "457450": "KODEX AI테크TOP10",
    # 방산 / 중공업
    "463250": "TIGER K방산&우주",
    "364980": "TIGER 조선TOP10",
    # 에너지 / 전력
    "487240": "KODEX AI전력핵심설비",
    "140710": "TIGER 원자력테마",
    "455890": "KODEX 원자력",
    # 채권 / 금리형
    "459580": "KODEX CD금리액티브(합성)",
    # 2차전지
    "305720": "KODEX 2차전지산업",
    # 금 / 원자재
    "411060": "ACE KRX금현물",
    "132030": "KODEX 골드선물(H)",
    # 채권
    "308620": "KODEX 미국10년국채선물",   # Naver 팩트체크로 수정 확인
    # 배당
    "266160": "KODEX 코스피고배당",
    "161510": "TIGER 배당성장",
    # 헬스케어
    "143460": "TIGER 헬스케어",
    "143850": "TIGER 미국S&P500선물",
    # 미국 ETF
    "SPY":  "SPDR S&P500",
    "QQQ":  "Invesco 나스닥100",
    "IWM":  "iShares 러셀2000",
    "DIA":  "SPDR 다우존스",
    "VTI":  "Vanguard 전체주식시장",
    "VOO":  "Vanguard S&P500",
    "XLK":  "Technology Select",
    "SOXX": "iShares 반도체",
    "SMH":  "VanEck 반도체",
    "ARKK": "ARK 혁신",
    "GLD":  "SPDR 금",
    "TLT":  "iShares 미국채20년",
    "JEPQ": "JPMorgan Nasdaq Equity Premium Income",
    "JEPI": "JPMorgan Equity Premium Income",
    "SCHD": "Schwab US Dividend Equity",
}


def check_ticker_integrity(ticker: str, name: str) -> tuple:
    """
    티커-종목명 정합성 검증. 내부 MASTER_ETF_DB를 우선 신뢰.
    Returns: (is_ok: bool, canonical_name: str | None, error_msg: str | None)
    - is_ok=True: 검증 통과 (DB에 없거나 일치)
    - is_ok=False: 불일치 감지 → 화면에 노출 차단 권고
    """
    canonical = _MASTER_ETF_DB.get(str(ticker).strip())
    if canonical is None:
        return True, None, None  # DB 미등록 종목 → 패스 (신규/비ETF)
    _dash = name.strip().replace(' ', '')
    _canon = canonical.strip().replace(' ', '')
    if _dash == _canon:
        return True, canonical, None
    # 불일치
    _msg = (
        f"데이터 정합성 오류: [{ticker}] 입력명칭 '{name}' ≠ "
        f"DB공식명칭 '{canonical}'. "
        "종목 정보 재설정 필요 — 진입 금지."
    )
    return False, canonical, _msg


def resolve_korean_name(ticker: str, fallback: str = "") -> str:
    """
    종목명을 한글 우선으로 해석 (삼성증권 표기 기준 내부 DB 우선).
    1순위: _MASTER_ETF_DB 한글명
    2순위: pykrx 한글명 (로컬 환경)
    3순위: fallback (yfinance 영어명 등)
    """
    _code = str(ticker).strip()
    # 1순위: 내부 마스터 DB (한글, 삼성증권 기준으로 관리)
    _db_name = _MASTER_ETF_DB.get(_code)
    if _db_name:
        return _db_name
    # 2순위: 한국 6자리 코드는 pykrx 한글명 시도
    if _code.isdigit() and len(_code) == 6:
        try:
            from pykrx import stock as _pk_rn
            _pk_name = _pk_rn.get_market_ticker_name(_code)
            if _pk_name and _pk_name.strip():
                return _pk_name.strip()
        except Exception:
            pass
    # 3순위: fallback (없으면 코드 그대로)
    return fallback.strip() if fallback and fallback.strip() else _code


# ══════════════════════════════════════════
# 데이터 함수
# ══════════════════════════════════════════

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_ohlcv(ticker, lookback=80):
    # import를 함수 외부(모듈 레벨)에서 이미 했을 경우를 대비해 lazy하게 처리
    try:
        import FinanceDataReader as _fdr
    except ImportError:
        _fdr = None
    try:
        import yfinance as _yf_fetch
    except ImportError:
        _yf_fetch = None
    import time as _time_fetch

    end   = datetime.today()
    start = end - timedelta(days=lookback*2)
    _start_str = start.strftime('%Y-%m-%d')
    _end_str   = end.strftime('%Y-%m-%d')

    is_korean = ticker.isdigit() and len(ticker) == 6

    if is_korean:
        # 1순위: FinanceDataReader
        if _fdr is not None:
            try:
                _df = _fdr.DataReader(ticker, _start_str, _end_str)
                if _df is not None and not _df.empty and len(_df) >= 5:
                    _col_map = {}
                    for _c in _df.columns:
                        _cl = _c.lower()
                        if _cl in ('open', '시가'): _col_map[_c] = '시가'
                        elif _cl in ('high', '고가'): _col_map[_c] = '고가'
                        elif _cl in ('low', '저가'): _col_map[_c] = '저가'
                        elif _cl in ('close', '종가'): _col_map[_c] = '종가'
                        elif _cl in ('volume', '거래량'): _col_map[_c] = '거래량'
                    _df = _df.rename(columns=_col_map)
                    _needed = ['시가','고가','저가','종가','거래량']
                    if all(c in _df.columns for c in _needed):
                        _df = _df[_needed]
                        _df = _df[_df['거래량'] > 0].tail(lookback)
                        if len(_df) >= 5:
                            return _df
            except Exception:
                pass

        # 2순위: yfinance fallback
        if _yf_fetch is not None:
            for suffix in ['.KS', '.KQ']:
                for _attempt in range(2):
                    try:
                        _yt = _yf_fetch.Ticker(ticker + suffix)
                        _df = _yt.history(start=start, end=end, interval='1d')
                        if _df is None or _df.empty:
                            break
                        _df = _df.rename(columns={
                            'Open':'시가','High':'고가','Low':'저가',
                            'Close':'종가','Volume':'거래량'
                        })[['시가','고가','저가','종가','거래량']]
                        _df = _df[_df['거래량'] > 0].dropna().tail(lookback)
                        if len(_df) >= 5:
                            return _df
                        break
                    except Exception:
                        if _attempt == 0:
                            _time_fetch.sleep(1)
                        continue
    else:
        # 미국 종목 — yfinance
        if _yf_fetch is not None:
            for _attempt in range(2):
                try:
                    _yt = _yf_fetch.Ticker(ticker)
                    _df = _yt.history(start=start, end=end, interval='1d')
                    if _df is not None and not _df.empty:
                        _df = _df.rename(columns={
                            'Open':'시가','High':'고가','Low':'저가',
                            'Close':'종가','Volume':'거래량'
                        })[['시가','고가','저가','종가','거래량']]
                        _df = _df[_df['거래량'] > 0].dropna().tail(lookback)
                        if len(_df) >= 5:
                            return _df
                    break
                except Exception:
                    if _attempt == 0:
                        _time_fetch.sleep(1)
                    continue
    return None

@st.cache_data(ttl=300, show_spinner=False)
def check_profit_recycling(current_krw_usd_rate, target_rate=1450):
    """환율 기반 수익 순환 판단 — 국장 수익금 → 미장 배당 자산 이동 시점 결정"""
    if current_krw_usd_rate <= target_rate:
        urgency = "BUY_THE_DIP" if current_krw_usd_rate <= 1400 else "ACTION_REQUIRED"
        return {
            "status": urgency,
            "color":  "#166534" if urgency == "BUY_THE_DIP" else "#1E40AF",
            "icon":   "🟢" if urgency == "BUY_THE_DIP" else "🔵",
            "message": f"환율 {current_krw_usd_rate:,.0f}원 — {'1,400원 이하: 추가 매수(Buy the Dip)' if urgency=='BUY_THE_DIP' else '1,450원 이하: 미장 자산 이동 최적기'}",
            "action":  "삼성증권 수익금 → 토스 이체 후 JEPQ / SCHD / MAIN 즉시 매수"
        }
    else:
        return {
            "status": "HOLD",
            "color":  "#92400E",
            "icon":   "🟡",
            "message": f"현재 환율 {current_krw_usd_rate:,.0f}원 — 환차손 위험 구간 (기준: 1,450원)",
            "action":  "국장 파킹형 자산(단기채 ETF) 또는 현금으로 유지"
        }

@st.cache_data(ttl=300, show_spinner=False)
def get_usd_krw():
    """USD/KRW 환율 — 5분 캐시. 실패 시 마지막값(없으면 1350) 폴백, 절대 예외 전파 안 함."""
    try:
        import yfinance as _yf_fx
        _h = _yf_fx.Ticker("USDKRW=X").history(period="5d")
        if _h is None or _h.empty or 'Close' not in _h.columns:
            return st.session_state.get('_last_usd_krw', 1350.0)
        _ser = _h['Close'].dropna()
        if _ser.empty:
            return st.session_state.get('_last_usd_krw', 1350.0)
        _val = float(_ser.iloc[-1])
        if not (_val == _val) or _val <= 0:        # NaN / 비정상 차단
            return st.session_state.get('_last_usd_krw', 1350.0)
        st.session_state['_last_usd_krw'] = _val
        return _val
    except (KeyError, IndexError, ValueError, ConnectionError, OSError, Exception):
        return st.session_state.get('_last_usd_krw', 1350.0)


@st.cache_data(ttl=300, show_spinner=False)
def get_wti_oil():
    """WTI 유가($/배럴) — 5분 캐시. 실패 시 마지막값(없으면 None) 폴백, 예외 전파 안 함."""
    try:
        import yfinance as _yf_oil
        _h = _yf_oil.Ticker("CL=F").history(period="5d")
        if _h is None or _h.empty or 'Close' not in _h.columns:
            return st.session_state.get('_last_wti', None)
        _ser = _h['Close'].dropna()
        if _ser.empty:
            return st.session_state.get('_last_wti', None)
        _val = float(_ser.iloc[-1])
        if not (_val == _val) or _val <= 0:
            return st.session_state.get('_last_wti', None)
        st.session_state['_last_wti'] = _val
        return _val
    except Exception:
        return st.session_state.get('_last_wti', None)


def compute_macro_regime_gate(krw=None, oil=None, foreign_net_krw=None):
    """매크로 레짐 게이트 — 환율·유가·외국인수급 종합 신호등.
    모든 입력 None/NaN 허용(부분판정). 절대 예외 없이 dict 반환.
    반환: light('green'|'amber'|'red'), verdict, risk(int), krw/oil/flow 상태, reasons[]"""
    def _num(x):
        return isinstance(x, (int, float)) and (x == x)   # not None, not NaN

    reasons, risk = [], 0

    krw_state = "unknown"
    if _num(krw) and krw > 0:
        if krw >= 1520:
            risk += 2; krw_state = "danger"; reasons.append(f"환율 {krw:,.0f}원 ≥1,520 (리스크오프)")
        elif krw >= 1480:
            risk += 1; krw_state = "warn"; reasons.append(f"환율 {krw:,.0f}원 경계")
        else:
            krw_state = "safe"; reasons.append(f"환율 {krw:,.0f}원 안정")

    oil_state = "unknown"
    if _num(oil) and oil > 0:
        if oil >= 100:
            risk += 2; oil_state = "danger"; reasons.append(f"WTI ${oil:.0f} ≥$100 (인플레 압력)")
        elif oil >= 90:
            risk += 1; oil_state = "warn"; reasons.append(f"WTI ${oil:.0f} 경계")
        else:
            oil_state = "safe"; reasons.append(f"WTI ${oil:.0f} 안정")

    flow_state = "unknown"
    if _num(foreign_net_krw):
        if foreign_net_krw <= -1_000_000_000_000:      # -1조 이하 = 패닉셀
            risk += 2; flow_state = "danger"; reasons.append("외국인 -1조↑ 순매도 (패닉셀)")
        elif foreign_net_krw < 0:
            risk += 1; flow_state = "warn"; reasons.append("외국인 순매도 진행")
        else:
            flow_state = "safe"; reasons.append("외국인 순매수")

    if risk >= 3:
        light, verdict = "red", "🔴 리스크오프 — 신규진입 금지 / 방어 우선"
    elif risk >= 1:
        light, verdict = "amber", "🟡 경계 — 분할·관망, 추격 금지"
    else:
        light, verdict = "green", "🟢 정상 — 전략 정상 가동"

    return {"light": light, "verdict": verdict, "risk": risk,
            "krw": krw_state, "oil": oil_state, "flow": flow_state, "reasons": reasons}


def macro_allows_scale_in(krw=None, foreign_net_krw=None):
    """추가매집(scale-in) 승격 게이트 — 시나리오 B 전용.
    환율 1,520 이하 안착 AND 외국인 순매수 전환 둘 다 충족할 때만 True.
    데이터 결측 시 보수적으로 False(엣지케이스: 좋은 환율이어도 수급 미확인이면 보류)."""
    def _num(x):
        return isinstance(x, (int, float)) and (x == x)
    krw_ok  = _num(krw) and 0 < krw <= 1520
    flow_ok = _num(foreign_net_krw) and foreign_net_krw > 0
    return bool(krw_ok and flow_ok), {"krw_ok": krw_ok, "flow_ok": flow_ok}


def parse_motie_export_text(text):
    """산자부 보도자료/뉴스 텍스트(붙여넣기)에서 수출 수치 정규식 추출.
    반환: dict(total, semi, semi_yoy) — 추출 실패 항목은 None. 예외 없이 반환."""
    import re as _re_me
    out = {"total": None, "semi": None, "semi_yoy": None}
    if not text or not isinstance(text, str):
        return out
    _t = text.replace(",", "").replace(" ", "")
    try:
        # 총 수출액: "수출 568억달러", "총수출 5,688천만달러" 등
        _m = _re_me.search(r"(?:총?수출(?:액|은|이|)?)\D{0,6}([\d.]+)\s*(억달러|억\$|십억달러|조원|억원)", _t)
        if _m:
            out["total"] = f"{_m.group(1)}{_m.group(2)}"
        # 반도체 수출액: "반도체 138억달러"
        _ms = _re_me.search(r"반도체\D{0,10}?([\d.]+)\s*(억달러|억\$|십억달러)", _t)
        if _ms:
            out["semi"] = f"{_ms.group(1)}{_ms.group(2)}"
        # 반도체 전년동월비 증감률: "반도체...+27.6%", "반도체 수출 27.6% 증가"
        _my = _re_me.search(r"반도체[^%]{0,40}?([+\-]?\d+\.?\d*)\s*%", _t)
        if _my:
            _v = float(_my.group(1))
            if "감소" in _t[_my.start():_my.end() + 6] and _v > 0:
                _v = -_v
            out["semi_yoy"] = _v
    except Exception:
        pass
    return out


def render_pension_results(pg_df, streak_map, streak_locked, mode_label, top_n, n_results):
    """연기금 스캔 결과 표시 + 관심종목 버튼 — 세션 캐시 기반으로 스캔 없이도 렌더.
    ⚠️ 반드시 try/except 밖에서 호출 (버튼 st.rerun 예외가 삼켜지지 않도록)."""
    if pg_df is None or len(pg_df) == 0:
        return

    def _pg_highlight(row):
        _s = row.get('연속등장(일)', 0)
        if _s >= 3: return ['background-color:#0d2a0d'] * len(row)
        if _s == 2: return ['background-color:#1a1a06'] * len(row)
        return [''] * len(row)

    _three = pg_df[pg_df['연속등장(일)'] >= 3]
    if not _three.empty:
        _ns = ", ".join(f"{r['종목명']}({r['종목코드']})" for _, r in _three.iterrows())
        st.success(f"🟢 **3일 연속 등장 → 매수 검토 대상:** {_ns}")
    elif not pg_df[pg_df['연속등장(일)'] == 2].empty:
        _tn = ", ".join(f"{r['종목명']}({r['종목코드']})" for _, r in pg_df[pg_df['연속등장(일)'] == 2].iterrows())
        st.warning(f"🟡 **2일 연속 등장 → 내일 재확인:** {_tn}")

    if streak_locked:
        st.caption("🔒 오늘 스캔 기록 확정 (날짜 Lock — 재스캔해도 연속일 카운트 고정)")

    st.markdown(f"#### {mode_label} TOP {min(top_n, n_results)}")
    st.caption("종합점수 = 연속일×10 + 순매수강도×2 + 외인쌍끌이 20점 (KRX모드) | "
               "연속상승×10 + 거래량비율×5 (프록시모드)  |  🟢배경=3일연속 🟡배경=2일연속")

    _disp = ['연속등장(일)'] + [c for c in pg_df.columns if c != '연속등장(일)']
    st.dataframe(pg_df[_disp].style.apply(_pg_highlight, axis=1),
                 use_container_width=True, hide_index=True)

    st.markdown("##### 📡 관심종목 즉시 추가")
    st.caption("버튼을 누르면 해당 종목이 관심종목에 추가되고 개별종목 분석탭에 자동 입력됩니다.")
    _cols = st.columns(min(len(pg_df), 3))
    for _bi, (_, _row) in enumerate(pg_df.head(6).iterrows()):
        _s = int(streak_map.get(str(_row['종목코드']), 1))
        _ic = "🟢" if _s >= 3 else "🟡" if _s == 2 else "⚪"
        with _cols[_bi % 3]:
            if st.button(f"{_ic} {_row['종목명']}\n연속{_s}일 · {_row['종목코드']}",
                         key=f"pg_wl_{_row['종목코드']}", use_container_width=True):
                _tc, _tnm = str(_row['종목코드']), str(_row['종목명'])
                _added = add_ticker(_tc, _tnm)
                st.session_state['analysis_ticker'] = _tc
                st.session_state['snipe_ticker_input'] = _tc
                if _added:
                    st.toast(f"✅ {_tnm}({_tc}) 관심종목 추가 완료!", icon="✅")
                else:
                    st.toast(f"ℹ️ {_tnm}({_tc}) 이미 관심종목에 있습니다.", icon="ℹ️")
                st.rerun()


def save_motie_manual(data: dict):
    """산자부 수동 입력값을 Firebase에 영구 저장(세션 소실 대비). 예외 무시."""
    try:
        _fb_ref("/motie_manual").set(data)
    except Exception:
        pass


def load_motie_manual() -> dict:
    """Firebase에서 산자부 수동 입력값 복원. 없으면 {}."""
    try:
        _d = _fb_ref("/motie_manual").get()
        return _d if isinstance(_d, dict) else {}
    except Exception:
        return {}


def render_motie_manual_widget(key_prefix="sb_motie"):
    """산자부 수출 수치 수동 입력 위젯 (사이드바용, 세로 배치 + 검증)."""
    import re as _re_mv
    _in_total = st.text_input("총 수출액", key=f"{key_prefix}_total", placeholder="예: 568억달러")
    _in_semi  = st.text_input("반도체 수출액", key=f"{key_prefix}_semi", placeholder="예: 138억달러")
    _in_yoy   = st.text_input("반도체 전년동월비(%)", key=f"{key_prefix}_yoy", placeholder="예: 27.6")
    if st.button("💾 산자부 수치 적용", key=f"{key_prefix}_apply", use_container_width=True):
        _errs = []
        _yoy_v = None
        _yoy_raw = _in_yoy.strip().replace("%", "").replace("+", "")
        if _yoy_raw:
            try:
                _yoy_v = float(_yoy_raw)
                if not (-100.0 <= _yoy_v <= 1000.0):
                    _errs.append("증감률은 -100 ~ +1000% 범위여야 합니다"); _yoy_v = None
            except ValueError:
                _errs.append("증감률은 % 단위 숫자여야 합니다 (예: 27.6)")

        def _valid_amount(_label, _raw):
            _raw = _raw.strip()
            if not _raw:
                return None
            if _raw.lstrip().startswith("-"):
                _errs.append(f"{_label}은 음수 불가"); return None
            if not _re_mv.search(r"\d", _raw):
                _errs.append(f"{_label}에 숫자 필요 (예: 568억달러)"); return None
            return _raw

        _total_v = _valid_amount("총 수출액", _in_total)
        _semi_v  = _valid_amount("반도체 수출액", _in_semi)
        if not any([_total_v, _semi_v, _yoy_v is not None]) and not _errs:
            _errs.append("최소 한 개 이상 입력하세요")
        if _errs:
            st.error("🚨 " + " / ".join(_errs))
        else:
            _payload = {"total": _total_v, "semi": _semi_v, "semi_yoy": _yoy_v,
                        "date": datetime.now().strftime("%Y-%m-%d")}
            st.session_state["_motie_manual"] = _payload
            save_motie_manual(_payload)
            fetch_motie_exports.clear()
            st.success("✅ 산자부 수치 적용 완료")
            st.rerun()


@st.cache_data(ttl=1800, show_spinner=False)
def get_short_selling_pressure(ticker):
    """개별 종목 하방 압력 지표 — pykrx 공매도/대차잔고. 절대 예외 전파 안 함.
    반환: dict(short_ratio, borrow_trend, net, ok) 또는 결측 시 ok=False.
      short_ratio  : 최근 3일 평균 공매도 거래대금 비중(%)
      borrow_trend : 대차잔고 증감 추세('증가'/'감소'/'중립'/None)
      net          : 최근 기간 외국인+기관 합산 순매수액(원, +매수 -매도) 또는 None
    한국 6자리 종목만 대상. 미국은 ok=False 반환."""
    _fail = {"short_ratio": None, "borrow_trend": None, "net": None, "ok": False}
    if not (isinstance(ticker, str) and ticker.isdigit() and len(ticker) == 6):
        return _fail
    try:
        from pykrx import stock as _pk_ss
        import datetime as _dt_ss
        _today = _dt_ss.datetime.utcnow() + _dt_ss.timedelta(hours=9)   # KST
        _end   = _today.strftime("%Y%m%d")
        _start = (_today - _dt_ss.timedelta(days=12)).strftime("%Y%m%d")

        # ── 공매도 거래 비중(%) — 최근 3영업일 평균 ──
        _short_ratio = None
        try:
            _sdf = _pk_ss.get_shorting_volume_by_date(_start, _end, ticker)
            if _sdf is not None and not _sdf.empty:
                # 비중 컬럼 탐색 (버전별 명칭 대응): '비중' 또는 공매도/거래량 직접 계산
                _rcol = next((c for c in _sdf.columns if "비중" in str(c)), None)
                if _rcol is not None:
                    _vals = _sdf[_rcol].dropna().tail(3)
                    if len(_vals) > 0:
                        _short_ratio = round(float(_vals.mean()), 2)
                else:
                    _scol = next((c for c in _sdf.columns if "공매도" in str(c)), None)
                    _vcol = next((c for c in _sdf.columns if "거래량" in str(c) and "공매도" not in str(c)), None)
                    if _scol is not None and _vcol is not None:
                        _tail = _sdf.tail(3)
                        _tot = float(_tail[_vcol].sum())
                        if _tot > 0:
                            _short_ratio = round(float(_tail[_scol].sum()) / _tot * 100, 2)
        except Exception:
            _short_ratio = None

        # ── 대차잔고 증감 추세 ──
        _borrow_trend = None
        try:
            _bdf = _pk_ss.get_shorting_balance_by_date(_start, _end, ticker)
            if _bdf is not None and not _bdf.empty:
                _bcol = next((c for c in _bdf.columns if "잔고" in str(c) and ("수량" in str(c) or "주" in str(c))), None)
                if _bcol is None:
                    _bcol = next((c for c in _bdf.columns if "잔고" in str(c)), None)
                if _bcol is not None:
                    _bvals = _bdf[_bcol].dropna()
                    if len(_bvals) >= 2:
                        _delta = float(_bvals.iloc[-1]) - float(_bvals.iloc[0])
                        _base  = abs(float(_bvals.iloc[0])) or 1.0
                        _pct   = _delta / _base
                        _borrow_trend = "증가" if _pct > 0.05 else "감소" if _pct < -0.05 else "중립"
        except Exception:
            _borrow_trend = None

        # ── 외국인 + 기관 합산 순매수(원) — 하방 Kill Switch 판정용 ──
        _net = None
        try:
            _ndf = _pk_ss.get_market_trading_value_by_investor(_start, _end, ticker)
            if _ndf is not None and not _ndf.empty:
                _ncol = "순매수" if "순매수" in _ndf.columns else _ndf.columns[-1]
                _sum = 0.0
                _found = False
                for _key in ("외국인", "외국인합계", "기관합계", "기관계", "기관"):
                    if _key in _ndf.index:
                        _v = float(_ndf.loc[_key, _ncol])
                        if _v == _v:
                            _sum += _v
                            _found = True
                _net = _sum if _found else None
        except Exception:
            _net = None

        _ok = (_short_ratio is not None) or (_borrow_trend is not None) or (_net is not None)
        return {"short_ratio": _short_ratio, "borrow_trend": _borrow_trend, "net": _net, "ok": _ok}
    except Exception:
        return _fail


def evaluate_downside_pressure(short_ratio, foreign_inst_net):
    """하방 압력 Kill Switch 판정.
    [공매도 비중 > 10% AND 외국인/기관 순매도] → 진입 기각(위험).
    반환: (is_blocked: bool, level: str, reason: str)
      level: 'safe'|'watch'|'danger'."""
    def _num(x):
        return isinstance(x, (int, float)) and (x == x)
    _short_hi = _num(short_ratio) and short_ratio > 10.0
    _net_sell = _num(foreign_inst_net) and foreign_inst_net < 0
    if _short_hi and _net_sell:
        return True, "danger", f"공매도 {short_ratio:.1f}% + 수급 순매도 → 하방 압력 위험"
    if _short_hi:
        return False, "watch", f"공매도 비중 {short_ratio:.1f}% 과다(단, 수급 방어 중)"
    if _num(short_ratio):
        return False, "safe", f"공매도 {short_ratio:.1f}% 정상"
    return False, "safe", "공매도 데이터 없음"


@st.cache_data(ttl=600, show_spinner=False)
def get_foreign_net_kospi():
    """코스피 외국인 순매수액(원) — pykrx 자동 조회. 실패 시 None(→수동 폴백).
    최근 영업일을 최대 8일 역추적(주말/휴일 대비). 절대 예외 전파 안 함."""
    try:
        from pykrx import stock as _pk_fn
        import datetime as _dt_fn
        _today = _dt_fn.datetime.utcnow() + _dt_fn.timedelta(hours=9)   # KST
        for _back in range(0, 8):
            _d = (_today - _dt_fn.timedelta(days=_back)).strftime("%Y%m%d")
            try:
                _df = _pk_fn.get_market_trading_value_by_investor(_d, _d, "KOSPI")
            except Exception:
                _df = None
            if _df is None or _df.empty:
                continue
            # 외국인 행 탐색 (버전별 명칭 차이 대응)
            _idx = None
            for _key in ("외국인", "외국인합계", "외국인투자자"):
                if _key in _df.index:
                    _idx = _key
                    break
            if _idx is None:
                continue
            _col = "순매수" if "순매수" in _df.columns else _df.columns[-1]
            _val = float(_df.loc[_idx, _col])
            if _val == _val:        # NaN 차단
                return _val
        return None
    except Exception:
        return None


@st.cache_data(ttl=600, show_spinner=False)
def get_foreign_net_kospi_kis_estimate():
    """KIS 폴백 — 주요 코스피 대형주 외국인 순매수 '수량' 합산으로 방향+개략 규모 추정.
    KRX/pykrx 차단 시 자동 대체용. 반환: (원-근사값 or None, hit수).
    ⚠️ 정확한 시장 총액이 아닌 대형주 기반 추정치(방향은 신뢰, 규모는 근사)."""
    if not kis_available():
        return None, 0
    # 코스피 외국인 수급을 대표하는 시총 상위 대형주
    _TOP = ["005930","000660","373220","207940","005380","000270","005490",
            "035420","051910","006400","035720","105560","055550","012330",
            "066570","028260","011200","009150","096770","034730"]
    _qty_sum, _hit = 0.0, 0
    for _tk in _TOP:
        _inv = kis_get_investor(_tk)
        if _inv and isinstance(_inv, dict):
            try:
                _qty_sum += float(_inv.get("외인순매수", 0))
                _hit += 1
            except (TypeError, ValueError):
                pass
    if _hit == 0:
        return None, 0
    # 대형주 평균 주가(≈7만원)로 원 단위 개략 환산 (방향 정확, 규모 근사)
    _won_est = _qty_sum * 70_000
    return _won_est, _hit


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_motie_exports():
    """산자부 6월 수출 데이터 — 우선순위 폴백 체인. 절대 예외 전파 안 함.
    1) 세션 수동입력(_motie_manual)  2) 보도자료/뉴스 크롤링(뼈대)  3) 실패 → None
    반환: dict(total, semi, semi_yoy, date, source) 또는 None."""
    # ── 1) 수동 입력 우선 (세션 → 없으면 Firebase 복원) ──
    _man = st.session_state.get("_motie_manual")
    if not (isinstance(_man, dict) and any(_man.get(k) is not None for k in ("total", "semi", "semi_yoy"))):
        _man = load_motie_manual()   # 세션 소실 시 Firebase에서 복원
        if isinstance(_man, dict) and any(_man.get(k) is not None for k in ("total", "semi", "semi_yoy")):
            st.session_state["_motie_manual"] = _man
    if isinstance(_man, dict) and any(_man.get(k) is not None for k in ("total", "semi", "semi_yoy")):
        return {**{"total": None, "semi": None, "semi_yoy": None, "date": ""}, **_man, "source": "수동입력"}

    # ── 2) 크롤링 시도 (BeautifulSoup 뼈대) ──
    try:
        import requests
        from bs4 import BeautifulSoup
        # 예시 소스: 네이버 뉴스 '산업통상자원부 수출' 검색 최신 기사 본문
        _url = "https://search.naver.com/search.naver?where=news&query=산업통상자원부+수출+반도체"
        _resp = requests.get(_url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        _resp.raise_for_status()
        _soup = BeautifulSoup(_resp.text, "html.parser")
        # 뉴스 요약 텍스트 수집 후 정규식 파싱에 위임
        _blocks = _soup.select("div.news_dsc") or _soup.find_all("a")
        _joined = " ".join(b.get_text(" ", strip=True) for b in _blocks[:20])
        _parsed = parse_motie_export_text(_joined)
        if any(_parsed.get(k) is not None for k in ("total", "semi", "semi_yoy")):
            return {**_parsed, "date": "", "source": "뉴스크롤링"}
        return None   # 수치 미발견 → 안전하게 None (발표 전이거나 셀렉터 변경)
    except Exception:
        return None   # 네트워크/파싱 실패 → None (패널은 '대기 중' 출력)


@st.cache_data(ttl=300, show_spinner=False)
def detect_market_regime_for_strategy():
    """코스피 지수로 시장 레짐 판정 → 추천 스캔 전략 매핑. 절대 예외 없이 dict 반환.
    반환: {regime, preset, label, reason}
      regime: 'crash'(폭락/셧다운) | 'bull'(대세상승) | 'range'(박스권)
      preset: 'bottom' | 'trend' | 'bounce'  (스캐너 프리셋 키)"""
    try:
        import yfinance as _yf_rg
        _df = _yf_rg.Ticker("^KS11").history(period="3mo", interval="1d")
        if _df is None or len(_df) < 20:
            raise ValueError("data")
        _cl = _df['Close'].dropna()
        _cur = float(_cl.iloc[-1])
        _ma20 = float(_cl.tail(20).mean())
        _ma5  = float(_cl.tail(5).mean())
        _chg1 = (_cur / float(_cl.iloc[-2]) - 1) * 100 if len(_cl) >= 2 else 0.0
        _disp = (_cur / _ma20 - 1) * 100 if _ma20 > 0 else 0.0   # 20일선 이격도(%)
        # 폭락장: 20일선 -3% 이상 하회 OR 당일 -2.5% 이상 급락
        if _disp <= -3.0 or _chg1 <= -2.5:
            return {"regime": "crash", "preset": "bottom",
                    "label": "지수 셧다운/폭락장",
                    "reason": f"코스피 20일선 {_disp:+.1f}% 이격 (하락 압력)"}
        # 대세 상승장: 20일선 위 + 5일선>20일선(정배열 초입)
        if _disp >= 1.0 and _ma5 > _ma20:
            return {"regime": "bull", "preset": "trend",
                    "label": "대세 상승장",
                    "reason": f"코스피 20일선 상단({_disp:+.1f}%) · 정배열"}
        # 그 외: 박스권/단기조정
        return {"regime": "range", "preset": "bounce",
                "label": "박스권 횡보/단기조정",
                "reason": f"코스피 20일선 근처({_disp:+.1f}%)"}
    except Exception:
        return {"regime": "range", "preset": "bounce",
                "label": "판정 보류(데이터 지연)", "reason": "지수 조회 실패 → 기본값"}


def generate_ai_briefing(krw=None, foreign_net_krw=None, top1=None):
    """5AI Top-Down 레짐 브리핑 — 3줄 자동 생성.
    krw: 원/달러 환율(float) / foreign_net_krw: 코스피 외국인 순매수액(원, +매수 -매도)
    top1: 1위 종목 dict 또는 (score, is_aligned) — 절대조건(점수≥70 AND 정배열) 판정용
    반환: {'lines': [str,str,str], 'verdict': str, 'light': 'green'|'amber'|'red'}
    절대 예외 없이 반환(결측은 '데이터 확인 필요'로 처리)."""
    def _num(x):
        return isinstance(x, (int, float)) and (x == x)

    # ── 1줄: 환율(리스크오프 레짐) ──
    if _num(krw) and krw > 0:
        if krw <= 1480:
            l1 = f"1. 환율이 {krw:,.0f}원으로 안정권에 머물며 리스크 오프 압력이 낮습니다."
            s1 = 1
        elif krw <= 1520:
            l1 = f"1. 환율이 {krw:,.0f}원으로 1,520원 아래에서 진정되며 리스크 오프 레짐이 완화 중입니다."
            s1 = 1
        else:
            l1 = f"1. 환율이 {krw:,.0f}원으로 1,520원을 넘어 리스크 오프(외국인 환차손) 압력이 지속됩니다."
            s1 = 0
    else:
        l1 = "1. 환율 데이터 확인 필요 — 레짐 판정 보류."
        s1 = -1

    # ── 2줄: 외국인 수급(매크로 게이트) ──
    if _num(foreign_net_krw):
        if foreign_net_krw > 0:
            l2 = "2. 외국인 수급이 순매수로 전환되어 매크로 레짐 게이트가 개방되었습니다."
            s2 = 1
        elif foreign_net_krw <= -1_000_000_000_000:
            l2 = "2. 외국인이 1조원 이상 순매도하며 레짐 게이트가 굳게 닫혀 있습니다."
            s2 = 0
        else:
            l2 = "2. 외국인 순매도가 이어져 매크로 레짐 게이트가 닫혀 있습니다."
            s2 = 0
    else:
        l2 = "2. 외국인 수급 데이터 미수신 — 게이트 상태 미확인(보수적 보류)."
        s2 = -1

    # ── 3줄: 1위 종목 절대조건(점수≥70 AND 정배열) → 매수 승인 여부 ──
    _score, _aligned = None, None
    if isinstance(top1, dict):
        try:
            _score = float(top1.get('종합점수', 0))
        except (TypeError, ValueError):
            _score = None
        _aligned = (str(top1.get('정배열', '')) == '✅')
    elif isinstance(top1, (tuple, list)) and len(top1) >= 2:
        try:
            _score = float(top1[0])
        except (TypeError, ValueError):
            _score = None
        _aligned = bool(top1[1])

    if _score is None:
        l3 = "3. 1위 종목 데이터 확인 필요 — 신규 진입 판정 보류."
        s3 = -1
    elif _score >= 70 and _aligned:
        l3 = f"3. 신규 진입 1위 종목의 절대 조건(점수 {int(_score)}·정배열)이 충족되어 매수를 승인합니다."
        s3 = 1
    else:
        _why = []
        if _score < 70: _why.append(f"점수 {int(_score)}<70")
        if not _aligned: _why.append("역배열")
        l3 = f"3. 1위 종목 절대 조건 미달({' · '.join(_why)}) — 신규 진입 보류."
        s3 = 0

    # ── 종합 신호등: 셋 다 양호=green / 하나라도 결측=amber / 위험=red ──
    _pos = [s for s in (s1, s2, s3)]
    if all(s == 1 for s in _pos):
        light, verdict = "green", "🟢 오늘은 신규 진입·추가 매집 승인 (3대 조건 충족)"
    elif any(s == 0 for s in _pos):
        light, verdict = "red", "🔴 오늘은 신규 진입 보류 (조건 미충족)"
    else:
        light, verdict = "amber", "🟡 데이터 일부 미확인 — 신규 진입 신중 검토"

    return {"lines": [l1, l2, l3], "verdict": verdict, "light": light}

def calc_indicators(df):
    """V8.9.2 — indicators.py 위임 (Wilder RSI, CMF20, ATR14)."""
    try:
        from indicators import calc_indicators as _calc
        result = _calc(df)
        # 하위 호환: Sto_K/D, 지지/저항선 유지
        low10  = df['저가'].rolling(10).min()
        high10 = df['고가'].rolling(10).max()
        denom = (high10 - low10).replace(0, np.nan)
        result['Sto_K']  = (100*(df['종가']-low10)/denom).round(1)
        result['Sto_D']  = result['Sto_K'].rolling(5).mean().round(1)
        result['지지선'] = df['저가'].rolling(20).min()
        result['저항선'] = df['고가'].rolling(20).max()
        return result
    except Exception:
        # indicators.py 로드 실패 시 기존 로직 폴백
        for n in [5, 20, 60, 120]:
            df[f'MA{n}'] = df['종가'].rolling(n).mean().round(0)
        df['BB_mid']   = df['종가'].rolling(20).mean()
        std            = df['종가'].rolling(20).std()
        df['BB_upper'] = (df['BB_mid'] + 2*std).round(0)
        df['BB_lower'] = (df['BB_mid'] - 2*std).round(0)
        df['BB_mid']   = df['BB_mid'].round(0)
        delta = df['종가'].diff()
        gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        df['RSI'] = (100 - 100/(1 + gain/loss.replace(0, np.nan))).round(1)
        ema12 = df['종가'].ewm(span=12, adjust=False).mean()
        ema26 = df['종가'].ewm(span=26, adjust=False).mean()
        df['MACD']      = (ema12 - ema26).round(4)
        df['Signal']    = df['MACD'].ewm(span=9, adjust=False).mean().round(4)
        df['MACD_hist'] = (df['MACD'] - df['Signal']).round(4)
        low10  = df['저가'].rolling(10).min()
        high10 = df['고가'].rolling(10).max()
        df['Sto_K'] = (100*(df['종가']-low10)/(high10-low10).replace(0,np.nan)).round(1)
        df['Sto_D'] = df['Sto_K'].rolling(5).mean().round(1)
        df['거래량_비율'] = (df['거래량']/df['거래량'].shift(1)*100).round(1)
        df['52W_high'] = df['고가'].rolling(min(252, len(df))).max()
        df['52W_low']  = df['저가'].rolling(min(252, len(df))).min()
        df['ATR14']    = np.maximum(df['고가']-df['저가'],
                         np.maximum(abs(df['고가']-df['종가'].shift(1)),
                                    abs(df['저가']-df['종가'].shift(1)))
                         ).rolling(14).mean()
        # CMF/OBV: 거래량 컬럼 있을 때만 계산, 없으면 NaN으로 skip
        if '거래량' in df.columns and df['거래량'].sum() > 0:
            mfm = ((df['종가'] - df['저가']) - (df['고가'] - df['종가'])) / \
                  (df['고가'] - df['저가']).replace(0, np.nan)
            df['CMF20'] = (mfm * df['거래량']).rolling(20).sum() / \
                          df['거래량'].rolling(20).sum().replace(0, np.nan)
            df['OBV']   = (np.where(df['종가'] > df['종가'].shift(1),
                                    df['거래량'],
                                    np.where(df['종가'] < df['종가'].shift(1),
                                             -df['거래량'], 0))).cumsum()
        else:
            df['CMF20'] = np.nan
            df['OBV']   = np.nan
        df['지지선']    = df['저가'].rolling(20).min()
        df['저항선']    = df['고가'].rolling(20).max()
        return df

def get_signal(df):
    l = df.iloc[-1]
    signals = []
    if l.get('RSI', 50) <= 30:               signals.append(('📉 과매도', 'watch'))
    if l.get('RSI', 50) >= 70:               signals.append(('📈 과매수', 'sell'))
    if l.get('거래량_비율', 0) >= 200:       signals.append(('🔥 거래량폭발', 'buy'))
    if l['종가'] > l.get('MA5', 0) > l.get('MA20', 0) > 0: signals.append(('✅ 정배열', 'buy'))
    if 0 < l['종가'] < l.get('MA5', 0) < l.get('MA20', 0): signals.append(('❌ 역배열', 'sell'))
    _macd  = l.get('MACD', None)
    _sig   = l.get('Signal', None)
    _macd2 = df.iloc[-2].get('MACD', None) if len(df) >= 2 else None
    _sig2  = df.iloc[-2].get('Signal', None) if len(df) >= 2 else None
    if _macd is not None and _sig is not None and _macd2 is not None and _sig2 is not None:
        if _macd > _sig and _macd2 <= _sig2:
            signals.append(('⚡ 골든크로스', 'buy'))
        if _macd < _sig and _macd2 >= _sig2:
            signals.append(('💀 데드크로스', 'sell'))
    if not signals: signals.append(('➖ 중립', 'neutral'))
    return signals

def build_prompt(df, name, ticker):
    if df is None or len(df) < 2:
        return f"{name}({ticker}) 데이터 부족으로 프롬프트 생성 불가"
    l = df.iloc[-1]
    p = df.iloc[-2] if len(df) >= 2 else df.iloc[-1]
    w = df.iloc[-6] if len(df) >= 6 else df.iloc[0]

    def _g(row, key, default=0):
        v = row.get(key, default) if hasattr(row, 'get') else getattr(row, key, default)
        return v if (v is not None and not (isinstance(v, float) and np.isnan(v))) else default

    macd_v  = _g(l, 'MACD', 0); sig_v = _g(l, 'Signal', 0)
    macd_p  = _g(p, 'MACD', 0); sig_p = _g(p, 'Signal', 0)
    macd_sig = ('골든크로스' if macd_v > sig_v and macd_p <= sig_p else
                '데드크로스' if macd_v < sig_v and macd_p >= sig_p else
                'MACD>Signal' if macd_v > sig_v else 'MACD<Signal')

    rsi_v = _g(l, 'RSI', 50)
    rsi_s = '과매수' if rsi_v >= 70 else '과매도' if rsi_v <= 30 else '중립'
    bb_u  = _g(l, 'BB_upper', 0); bb_lo = _g(l, 'BB_lower', 0); bb_mi = _g(l, 'BB_mid', 0)
    bb_r  = bb_u - bb_lo
    bb_p  = round((l['종가'] - bb_lo) / bb_r * 100, 1) if bb_r > 0 else 50
    cur   = l['종가']
    lines = [
        f'종목: {name} ({ticker}) | 분석일: {str(df.index[-1])[:10]}',
        f'현재가: {cur:,.0f}원 | 전일대비: {round((cur/p["종가"]-1)*100,2)}% | 1주일대비: {round((cur/w["종가"]-1)*100,2)}%',
        f'시가: {l["시가"]:,.0f} | 고가: {l["고가"]:,.0f} | 저가: {l["저가"]:,.0f}',
        f'MA5: {_g(l,"MA5"):,.0f} | MA20: {_g(l,"MA20"):,.0f} | MA60: {_g(l,"MA60"):,.0f} | MA120: {_g(l,"MA120"):,.0f}',
        f'BB 상단: {bb_u:,.0f} | 중단: {bb_mi:,.0f} | 하단: {bb_lo:,.0f} | 위치: {bb_p}%',
        f'MACD: {macd_v:,.2f} / Signal: {sig_v:,.2f} -> {macd_sig}',
        f'RSI(14): {rsi_v} -> {rsi_s} | Sto K: {_g(l,"Sto_K","N/A")} D: {_g(l,"Sto_D","N/A")}',
        f'거래량: {l["거래량"]:,}주 | 전일대비: {_g(l,"거래량_비율",0):.0f}% | 20일평균: {df["거래량"].tail(20).mean():,.0f}주',
        f'52주 고가: {_g(l,"52W_high",0):,.0f} | 52주 저가: {_g(l,"52W_low",0):,.0f}',
        f'ATR14: {_g(l,"ATR14",0):,.0f} | CMF20: {_g(l,"CMF20",0):.3f}',
        '',
        '분석 요청 (R:R 2.0이상 / ATR 동적 손절 적용):',
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
    if df is None or len(df) < 2:
        return {'cur':0,'entry':0,'stoploss':0,'target1':0,'target2':0,
                'reason':'데이터 부족','rr':0,'gap_pct':0}
    l   = df.iloc[-1]
    cur = float(l['종가']) if float(l.get('종가', 0)) > 0 else 1.0

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

    # 2. stoploss = entry × (1 - _STOP_LOSS_PCT) — 전역 상수 사용 (기본 7%)
    stoploss = round(entry * (1 - _STOP_LOSS_PCT))

    # 3. target1이 entry 이하면 강제로 높임
    if target1 <= entry:
        target1 = round(entry * 1.08)
    if target2 <= target1:
        target2 = round(target1 * 1.07)

    # 4. 최종 안전 클램프 (엣지케이스 방어)
    if not (stoploss < entry < cur):
        entry    = round(cur * 0.97)
        stoploss = round(entry * (1 - _STOP_LOSS_PCT))
        reason  += " (안전클램프 적용)"
    if target1 <= entry:
        target1 = round(entry * 1.08)
    if target2 <= target1:
        target2 = round(target1 * 1.07)

    risk   = entry - stoploss
    # R:R은 '최종 목표(target2)' 기준 — 손절 7% 대비 목표 ~14%면 2.0 달성.
    # (1차 목표만 쓰면 R:R이 구조적으로 ~1.1로 고정돼 '진입 불가'만 나옴)
    reward = target2 - entry
    rr     = round(reward / risk, 2) if risk > 0 else 0
    # cur == 0 방어 (ZeroDivision)
    gap_pct = round((entry - cur) / cur * 100, 1) if cur > 0 else 0.0

    return {
        'cur':      round(cur),
        'entry':    entry,
        'stoploss': stoploss,
        'target1':  target1,
        'target2':  target2,
        'reason':   reason,
        'rr':       rr,
        'gap_pct':  gap_pct,
    }

def make_chart(df, name, entry=None, stoploss=None, target1=None, target2=None):
    _dark = st.session_state.get('ui_dark', True)

    BG   = '#0d1117' if _dark else '#ffffff'
    BG2  = '#161b22' if _dark else '#f8fafc'
    GRID = 'rgba(255,255,255,0.04)' if _dark else 'rgba(0,0,0,0.05)'
    AXIS = 'rgba(255,255,255,0.08)' if _dark else 'rgba(0,0,0,0.12)'
    TXT  = '#8b949e' if _dark else '#57606a'
    TXT2 = '#e6edf3' if _dark else '#24292f'

    UP   = '#ef4444'   # 상승: 빨강 (한국 증권사 기본)
    DOWN = '#3b82f6'   # 하락: 파랑

    # ── 서브플롯: 캔들 / 거래량 / MACD / RSI / CMF ──
    fig = make_subplots(
        rows=5, cols=1,
        shared_xaxes=True,
        row_heights=[0.52, 0.12, 0.13, 0.12, 0.11],
        vertical_spacing=0.0,
    )

    idx    = df.index
    closes = df['종가'].astype(float)
    opens  = df['시가'].astype(float)
    highs  = df['고가'].astype(float)
    lows   = df['저가'].astype(float)
    cur    = float(closes.iloc[-1])
    prev   = float(closes.iloc[-2]) if len(closes) >= 2 else cur
    cur_c  = UP if cur >= prev else DOWN
    chg_p  = (cur / prev - 1) * 100 if prev > 0 else 0

    # ── Y축 범위: 최근 60봉 고저 기준 ± 여유분 5% ──
    _n    = min(60, len(df))
    _hi   = float(highs.iloc[-_n:].max())
    _lo   = float(lows.iloc[-_n:].min())
    _pad  = (_hi - _lo) * 0.08
    _ymin = _lo - _pad
    _ymax = _hi + _pad * 1.5   # 위쪽 여유 더 줌 (현재가 레이블 공간)

    # ── 볼린저 밴드 ──
    bb_c = 'rgba(100,116,139,0.30)'
    bb_f = 'rgba(100,116,139,0.05)'
    if 'BB_upper' in df.columns and 'BB_lower' in df.columns:
        fig.add_trace(go.Scatter(x=idx, y=df['BB_upper'],
            line=dict(color=bb_c, width=0.8, dash='dot'),
            name='BB상단', showlegend=False, hoverinfo='skip'), row=1, col=1)
        fig.add_trace(go.Scatter(x=idx, y=df['BB_lower'],
            line=dict(color=bb_c, width=0.8, dash='dot'),
            fill='tonexty', fillcolor=bb_f,
            name='BB밴드', showlegend=False, hoverinfo='skip'), row=1, col=1)

    # ── 이동평균선 ──
    for ma, c, w, d in [
        ('MA5',  '#f59e0b', 1.4, 'solid'),
        ('MA20', '#22c55e', 1.4, 'solid'),
        ('MA60', '#a855f7', 1.2, 'solid'),
        ('MA120','#38bdf8', 1.0, 'dot'),
    ]:
        if ma in df.columns:
            fig.add_trace(go.Scatter(x=idx, y=df[ma],
                line=dict(color=c, width=w, dash=d),
                name=ma, hovertemplate=f'{ma}: %{{y:,.0f}}<extra></extra>'), row=1, col=1)

    # ── 캔들스틱 ──
    fig.add_trace(go.Candlestick(
        x=idx,
        open=opens, high=highs, low=lows, close=closes,
        increasing=dict(line=dict(color=UP,   width=1), fillcolor=UP),
        decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN),
        name='캔들', showlegend=False,
        hovertext=[
            f"<b>{str(d)[:10]}</b><br>"
            f"시가 {o:,.0f} &nbsp; 고가 {h:,.0f}<br>"
            f"저가 {l:,.0f} &nbsp; 종가 {c:,.0f}<br>"
            f"등락 {(c/o-1)*100:+.2f}%"
            for d, o, h, l, c in zip(idx, opens, highs, lows, closes)
        ],
        hoverinfo='text',
        whiskerwidth=0,
    ), row=1, col=1)

    # ── 현재가 점선 ──
    fig.add_shape(type='line', x0=0, x1=1, xref='x domain',
        y0=cur, y1=cur, yref='y',
        line=dict(color=cur_c, width=1.0, dash='dot'), row=1, col=1)
    fig.add_annotation(
        x=1.002, y=cur, xref='x domain', yref='y',
        text=f"<b>{cur:,.0f}</b>",
        showarrow=False, xanchor='left',
        font=dict(color='#ffffff', size=11, family='D2Coding, monospace'),
        bgcolor=cur_c, borderpad=3, bordercolor=cur_c, row=1, col=1)

    # ── 매수·손절·목표가 라인 (Y축 범위 밖으로 밀지 않도록 주석만 표기) ──
    _strategy_lines = []
    if entry:    _strategy_lines.append((entry,    '#f59e0b', 'solid', f'매수 {entry:,.0f}'))
    if stoploss: _strategy_lines.append((stoploss, UP,        'dash',  f'손절 {stoploss:,.0f}'))
    if target1:  _strategy_lines.append((target1,  '#22c55e', 'solid', f'1차목표 {target1:,.0f}'))
    if target2:  _strategy_lines.append((target2,  '#a855f7', 'dot',   f'2차목표 {target2:,.0f}'))

    for val, color, dash, lbl in _strategy_lines:
        # Y축 범위 동적 확장 (라인이 범위 안에 들어오도록 최소 조정만)
        if val < _ymin: _ymin = val - _pad * 0.5
        if val > _ymax: _ymax = val + _pad * 0.5
        fig.add_shape(type='line', x0=0, x1=1, xref='x domain',
            y0=val, y1=val, yref='y',
            line=dict(color=color, dash=dash, width=1.2), row=1, col=1)
        fig.add_annotation(
            x=0.97, y=val, xref='x domain', yref='y',
            text=f'<b>{lbl}</b>', showarrow=False, xanchor='right',
            font=dict(color=color, size=10, family='D2Coding, monospace'),
            bgcolor=f'rgba(13,17,23,0.75)' if _dark else 'rgba(255,255,255,0.85)',
            borderpad=2, row=1, col=1)

    # ── 거래량 ──
    vol_max = float(df['거래량'].max()) or 1
    vol_colors = []
    for i in range(len(df)):
        ratio = float(df['거래량'].iloc[i]) / vol_max
        is_up = float(closes.iloc[i]) >= float(opens.iloc[i])
        r, g, b = (239, 68, 68) if is_up else (59, 130, 246)
        vol_colors.append(f'rgba({r},{g},{b},{0.30 + ratio * 0.60:.2f})')
    fig.add_trace(go.Bar(x=idx, y=df['거래량'],
        marker=dict(color=vol_colors, line=dict(width=0)),
        name='거래량', showlegend=False,
        hovertemplate='거래량: %{y:,.0f}<extra></extra>'), row=2, col=1)
    if len(df) >= 20:
        fig.add_trace(go.Scatter(x=idx, y=df['거래량'].rolling(20).mean(),
            line=dict(color='#f59e0b', width=1.0, dash='dot'),
            name='거래량MA20', showlegend=False, hoverinfo='skip'), row=2, col=1)

    # ── MACD ──
    if 'MACD_hist' in df.columns and 'MACD' in df.columns:
        macd_max = float(df['MACD_hist'].abs().max()) or 1
        hist_colors = [
            f'rgba(239,68,68,{0.4 + abs(v)/macd_max*0.5:.2f})' if v >= 0
            else f'rgba(59,130,246,{0.4 + abs(v)/macd_max*0.5:.2f})'
            for v in df['MACD_hist']
        ]
        fig.add_trace(go.Bar(x=idx, y=df['MACD_hist'],
            marker=dict(color=hist_colors, line=dict(width=0)),
            name='히스토', showlegend=False,
            hovertemplate='MACD히스토: %{y:.2f}<extra></extra>'), row=3, col=1)
        fig.add_trace(go.Scatter(x=idx, y=df['MACD'],
            line=dict(color='#38bdf8', width=1.5), name='MACD',
            hovertemplate='MACD: %{y:.2f}<extra></extra>'), row=3, col=1)
        fig.add_trace(go.Scatter(x=idx, y=df['Signal'],
            line=dict(color='#f472b6', width=1.5), name='Signal',
            hovertemplate='Signal: %{y:.2f}<extra></extra>'), row=3, col=1)
        fig.add_shape(type='line', x0=0, x1=1, xref='x domain',
            y0=0, y1=0, yref='y3', line=dict(color=AXIS, width=0.8))

    # ── RSI ──
    if 'RSI' in df.columns:
        rsi_cur = float(df['RSI'].iloc[-1])
        rsi_c   = UP if rsi_cur >= 70 else (DOWN if rsi_cur <= 30 else '#a855f7')
        fig.add_hrect(y0=70, y1=100, fillcolor='rgba(239,68,68,0.06)',  line_width=0, row=4, col=1)
        fig.add_hrect(y0=0,  y1=30,  fillcolor='rgba(59,130,246,0.06)', line_width=0, row=4, col=1)
        for lvl, clr in [(70, UP), (30, DOWN), (50, AXIS)]:
            fig.add_shape(type='line', x0=0, x1=1, xref='x domain',
                y0=lvl, y1=lvl, yref='y4',
                line=dict(color=clr, width=0.7, dash='dot'))
        fig.add_trace(go.Scatter(x=idx, y=df['RSI'],
            line=dict(color='#a855f7', width=1.6),
            fill='tozeroy', fillcolor='rgba(168,85,247,0.07)',
            name='RSI', showlegend=False,
            hovertemplate='RSI: %{y:.1f}<extra></extra>'), row=4, col=1)
        fig.add_annotation(
            x=1.002, y=rsi_cur, xref='x domain', yref='y4',
            text=f'<b>{rsi_cur:.0f}</b>', showarrow=False, xanchor='left',
            font=dict(color=rsi_c, size=10, family='D2Coding, monospace'),
            bgcolor=BG, row=4, col=1)

    # ── CMF20 (OBV 대신) ──
    _cmf_col = 'CMF20' if 'CMF20' in df.columns else None
    if _cmf_col:
        cmf_ser = df[_cmf_col].astype(float)
        cmf_cur = float(cmf_ser.iloc[-1])
        cmf_colors = [
            f'rgba(34,197,94,{min(0.9, 0.3+abs(v)*3):.2f})' if v >= 0
            else f'rgba(239,68,68,{min(0.9, 0.3+abs(v)*3):.2f})'
            for v in cmf_ser
        ]
        fig.add_trace(go.Bar(x=idx, y=cmf_ser,
            marker=dict(color=cmf_colors, line=dict(width=0)),
            name='CMF20', showlegend=False,
            hovertemplate='CMF: %{y:.3f}<extra></extra>'), row=5, col=1)
        fig.add_shape(type='line', x0=0, x1=1, xref='x domain',
            y0=0, y1=0, yref='y5', line=dict(color=AXIS, width=0.8))
        fig.add_shape(type='line', x0=0, x1=1, xref='x domain',
            y0=0.05, y1=0.05, yref='y5',
            line=dict(color='rgba(34,197,94,0.4)', width=0.7, dash='dot'))
        fig.add_shape(type='line', x0=0, x1=1, xref='x domain',
            y0=-0.05, y1=-0.05, yref='y5',
            line=dict(color='rgba(239,68,68,0.4)', width=0.7, dash='dot'))
        cmf_c = '#22c55e' if cmf_cur >= 0.05 else ('#ef4444' if cmf_cur <= -0.05 else TXT)
        fig.add_annotation(
            x=1.002, y=cmf_cur, xref='x domain', yref='y5',
            text=f'<b>{cmf_cur:+.3f}</b>', showarrow=False, xanchor='left',
            font=dict(color=cmf_c, size=10, family='D2Coding, monospace'),
            bgcolor=BG, row=5, col=1)

    # ── 레이아웃 ──
    fig.update_layout(
        title=dict(
            text=(f'<b style="font-size:15px;color:{TXT2}">{name}</b>'
                  f'&nbsp;&nbsp;<b style="font-size:16px;color:{cur_c}">{cur:,.0f}</b>'
                  f'&nbsp;<span style="font-size:13px;color:{cur_c}">{chg_p:+.2f}%</span>'),
            x=0.01, y=0.99, xanchor='left', yanchor='top',
        ),
        paper_bgcolor=BG,
        plot_bgcolor=BG,
        font=dict(color=TXT, size=11, family='D2Coding, monospace'),
        xaxis_rangeslider_visible=False,
        height=960,
        legend=dict(
            orientation='h', y=1.042, x=0.30,
            font=dict(size=10, color=TXT2), bgcolor='rgba(0,0,0,0)',
            traceorder='normal',
        ),
        margin=dict(l=0, r=80, t=55, b=10),
        hovermode='x unified',
        hoverlabel=dict(
            bgcolor='#1c2128' if _dark else '#ffffff',
            bordercolor='rgba(255,255,255,0.12)' if _dark else '#d0d7de',
            font=dict(color='#e6edf3' if _dark else '#24292f',
                      size=11, family='D2Coding, monospace'),
            namelength=-1,
        ),
        modebar=dict(
            bgcolor='rgba(0,0,0,0)', color=TXT, activecolor='#3b82f6',
            remove=['toImage','sendDataToCloud','editInChartStudio','lasso2d','select2d'],
        ),
        modebar_add=['autoScale2d', 'resetScale2d'],
    )

    # ── 레인지 셀렉터 ──
    fig.update_xaxes(row=1, col=1,
        rangeselector=dict(
            buttons=[
                dict(count=1,  label='1M',  step='month', stepmode='backward'),
                dict(count=3,  label='3M',  step='month', stepmode='backward'),
                dict(count=6,  label='6M',  step='month', stepmode='backward'),
                dict(step='all', label='ALL'),
            ],
            bgcolor='rgba(22,27,34,0.9)' if _dark else 'rgba(246,248,250,0.95)',
            activecolor='#1f6feb',
            bordercolor='rgba(255,255,255,0.1)' if _dark else '#d0d7de',
            borderwidth=1,
            font=dict(color=TXT2, size=10),
            x=0.0, y=1.0,
        ),
    )

    # ── 크로스헤어 스파이크 ──
    _spike = dict(
        showspikes=True, spikecolor='rgba(139,148,158,0.5)',
        spikemode='across', spikesnap='cursor',
        spikedash='solid', spikethickness=1,
    )

    # ── X축 공통 설정 ──
    for row in range(1, 6):
        fig.update_xaxes(row=row, col=1,
            showgrid=True, gridcolor=GRID, gridwidth=1,
            zeroline=False, linecolor=AXIS, showline=True,
            showticklabels=(row == 5),
            tickfont=dict(size=10, color=TXT),
            **_spike,
        )

    # ── Y축 — 캔들(row1): 범위 고정으로 찌그러짐 방지 ──
    fig.update_yaxes(row=1, col=1,
        showgrid=True, gridcolor=GRID, gridwidth=1,
        zeroline=False, linecolor=AXIS, showline=True,
        side='right', tickformat=',.0f',
        tickfont=dict(size=11, color=TXT2),
        range=[_ymin, _ymax],
        showspikes=True, spikecolor='rgba(139,148,158,0.3)', spikethickness=1,
        automargin=True,
        fixedrange=False,
    )

    # ── Y축 — 거래량(row2) ──
    fig.update_yaxes(row=2, col=1,
        showgrid=False, zeroline=False, linecolor=AXIS, showline=True,
        side='right', tickformat=',.0s',
        tickfont=dict(size=9, color=TXT), automargin=True,
    )

    # ── Y축 — MACD(row3) ──
    fig.update_yaxes(row=3, col=1,
        showgrid=True, gridcolor=GRID, zeroline=False,
        linecolor=AXIS, showline=True, side='right',
        tickfont=dict(size=9, color=TXT), automargin=True,
    )

    # ── Y축 — RSI(row4): 0~100 고정 ──
    fig.update_yaxes(row=4, col=1,
        showgrid=True, gridcolor=GRID, zeroline=False,
        linecolor=AXIS, showline=True, side='right',
        range=[0, 100], tickvals=[30, 50, 70],
        tickfont=dict(size=9, color=TXT), automargin=True,
    )

    # ── Y축 — CMF(row5) ──
    fig.update_yaxes(row=5, col=1,
        showgrid=True, gridcolor=GRID, zeroline=False,
        linecolor=AXIS, showline=True, side='right',
        tickfont=dict(size=9, color=TXT), automargin=True,
    )

    # ── 패널 레이블 ──
    for row, lbl in [(2,'Vol'), (3,'MACD'), (4,'RSI'), (5,'CMF20')]:
        fig.add_annotation(xref='x domain', yref='y domain',
            x=0.008, y=0.98, xanchor='left', yanchor='top',
            text=f'<b style="font-size:9px;color:{TXT}">{lbl}</b>',
            showarrow=False, bgcolor='rgba(0,0,0,0)', row=row, col=1)

    return fig



_KR_BUILTIN_MODULE = {
    "005930":"삼성전자","000660":"SK하이닉스","005380":"현대차","000270":"기아",
    "005490":"POSCO홀딩스","051910":"LG화학","006400":"삼성SDI","035720":"카카오",
    "035420":"NAVER","012330":"현대모비스","028260":"삼성물산","066570":"LG전자",
    "207940":"삼성바이오로직스","068270":"셀트리온","105560":"KB금융","055550":"신한지주",
    "003550":"LG","032830":"삼성생명","086790":"하나금융지주","015760":"한국전력",
    "017670":"SK텔레콤","030200":"KT","011200":"HMM","018880":"한온시스템",
    "009150":"삼성전기","010130":"고려아연","011070":"LG이노텍","000810":"삼성화재",
    "024110":"기업은행","000100":"유한양행","096770":"SK이노베이션","034730":"SK",
    "002380":"KCC","004020":"현대제철","042700":"한미반도체","042660":"한화오션",
    "009540":"한국조선해양","329180":"HD현대중공업","267250":"HD현대","012450":"한화에어로스페이스",
    "047810":"한국항공우주","000120":"CJ대한통운","097950":"CJ제일제당","001040":"CJ",
    "282330":"BGF리테일","139480":"이마트","023530":"롯데쇼핑","004170":"신세계",
    "011780":"금호석유","010950":"S-Oil","078930":"GS","001270":"부국증권",
    "086280":"현대글로비스","064350":"현대로템","022100":"포스코DX","402340":"SK스퀘어",
    "373220":"LG에너지솔루션","247540":"에코프로비엠","086520":"에코프로","003670":"포스코퓨처엠",
    "051900":"LG생활건강","090430":"아모레퍼시픽","161390":"한국타이어앤테크놀로지",
    "018260":"삼성에스디에스","034020":"두산에너빌리티","336260":"두산밥캣",
    "241560":"두산퓨얼셀","039130":"하나투어","035250":"강원랜드","000080":"하이트진로",
    "002790":"아모레G","007070":"GS리테일","036460":"한국가스공사","015020":"이랜텍",
    "089030":"테크윙","000990":"DB하이텍","045180":"파이오링크","036800":"나이스정보통신",
    "079550":"LIG넥스원","010140":"삼성중공업","009830":"한화솔루션","011790":"SKC",
    "002960":"한국쉘석유","000830":"삼성공조","032640":"LG유플러스","017800":"현대엘리베이터",
    "003490":"대한항공","020560":"아시아나항공","006360":"GS건설","000720":"현대건설",
    "028050":"삼성엔지니어링","047050":"포스코인터내셔널","001450":"현대해상",
    "000100":"유한양행","128940":"한미약품","069620":"대웅제약","185750":"종근당",
    "008770":"호텔신라","011170":"롯데케미칼","009110":"오씨아이","014820":"동원시스템즈",
    "139130":"DGB금융지주","138930":"BNK금융지주","175330":"JB금융지주",
    "088980":"맥쿼리인프라","139290":"코드네이처","259960":"크래프톤","263750":"펄어비스",
    "036570":"엔씨소프트","251270":"넷마블","112040":"위메이드","095660":"네오위즈",
    "293490":"카카오게임즈","352820":"하이브","041510":"에스엠","035900":"JYP",
    "122870":"와이지엔터테인먼트","058970":"엠씨넥스","091990":"셀트리온헬스케어",
    "196170":"알테오젠","326030":"SK바이오팜","302440":"SK바이오사이언스",
    "145020":"휴젤","214150":"클래시스","013360":"일진머티리얼즈","011000":"삼양홀딩스",
    "010060":"OCI","004990":"롯데지주","004000":"롯데정밀화학","002790":"아모레G",
    "271560":"오리온","097130":"이씨에스","071970":"STX중공업","010620":"현대미포조선",
    "006280":"녹십자","000670":"영풍","005870":"휴니드","090460":"비에이치",
    "357780":"솔브레인","408620":"새빗켐","336370":"솔루스첨단소재","121600":"나노신소재",
    "036490":"SK머티리얼즈","278280":"천보","166090":"하나머티리얼즈","005290":"동진쎄미켐",
    "049830":"세아제강지주","004140":"동양","012630":"HDC","294870":"HDC현대산업개발",
    "042670":"두산인프라코어","017960":"한국카본","009450":"경동나비엔","071840":"하이록코리아",
    "064960":"S&T모티브","025900":"동화기업","025820":"이구산업","003300":"한일홀딩스",
    "016360":"삼성증권","071050":"한국금융지주","003540":"대신증권","001500":"현대차증권",
    "039490":"키움증권","005940":"NH투자증권","006800":"미래에셋증권",
}

@st.cache_data(ttl=86400, show_spinner=False)
def _load_kr_stock_list():
    # 1순위: pykrx 실시간 (로컬 환경)
    try:
        from pykrx import stock as _pykrx
        _today = datetime.today().strftime("%Y%m%d")
        _tickers = _pykrx.get_market_ticker_list(market="ALL")
        _result = {t: _pykrx.get_market_ticker_name(t) for t in _tickers}
        if len(_result) > 100:
            return _result
    except Exception:
        pass
    # 2순위: 내장 DB (Streamlit Cloud)
    return _KR_BUILTIN_MODULE


# ══════════════════════════════════════════
# ⏱ 단일 KST 시간 소스 (모든 시간 표시가 공통 참조 — 파편화 방지)
# ══════════════════════════════════════════
_NOW_KST = datetime.utcnow() + timedelta(hours=9)   # 서버 UTC → KST
st.session_state['_now_kst']     = _NOW_KST
st.session_state['_now_kst_str'] = _NOW_KST.strftime('%Y.%m.%d %H:%M:%S KST')

# ══════════════════════════════════════════
# 사이드바
# ══════════════════════════════════════════

with st.sidebar:
    # ══════════════════════════════════════════════════════════════════
    # 📌 STICKY 관제 상태 패널 — 모든 탭에서 항상 표시 (사이드바 최상단 고정)
    # ══════════════════════════════════════════════════════════════════
    try:
        _sbv = run_v891_system_check()
        _sb_black = not _sbv.get('can_enter', True)
        _sb_krw   = get_usd_krw()
        _sb_oil   = get_wti_oil()
        _sb_flow  = st.session_state.get('_foreign_net_krw', None)
        _sb_gate  = compute_macro_regime_gate(_sb_krw, _sb_oil, _sb_flow)
        if _sb_black:
            _sbt, _sbc, _sbi = "진입 금지", "#ef4444", "🚫"
        elif _sb_gate["light"] == "red":
            _sbt, _sbc, _sbi = "진입 금지", "#ef4444", "🔴"
        elif _sb_gate["light"] == "amber":
            _sbt, _sbc, _sbi = "관망", "#f59e0b", "🟡"
        else:
            _sbt, _sbc, _sbi = "진입 가능", "#16a34a", "🟢"
        st.markdown(
            f"<div style='background:{_sbc}20;border:2px solid {_sbc};border-radius:12px;"
            f"padding:10px 12px;margin-bottom:8px;text-align:center'>"
            f"<div style='font-size:26px;line-height:1'>{_sbi}</div>"
            f"<div style='font-size:17px;font-weight:900;color:{_sbc};margin-top:2px'>{_sbt}</div>"
            f"</div>", unsafe_allow_html=True)
        if _sb_black:
            _al = _sbv.get('alerts', ['이벤트 48시간 이내'])
            st.error(f"🚨 매크로 블랙아웃: {_al[0] if _al else '이벤트 임박'}")
        # 핵심 수치 — 좁은 사이드바에서 2컬럼 대신 수직 배열(위아래로 넓게)
        st.metric("💱 원/달러 환율", f"{_sb_krw:,.0f}원" if isinstance(_sb_krw,(int,float)) else "—",
                  delta=("⚠️ 1,450 경계" if isinstance(_sb_krw,(int,float)) and _sb_krw>=1450 else "안정"),
                  delta_color="inverse")
        if isinstance(_sb_flow,(int,float)):
            st.metric("🌍 외국인 수급", f"{_sb_flow/1e8:+,.0f}억원",
                      delta=("순매수" if _sb_flow>0 else "순매도"),
                      delta_color=("normal" if _sb_flow>0 else "inverse"))
        else:
            st.metric("🌍 외국인 수급", "—")
    except Exception:
        st.caption("⚠️ 상태 패널 일시 비활성 (데이터 지연)")

    st.markdown("---")
    st.markdown("## ⚙️ 설정")

    # ── 수동 입력 위젯 (설정 하단으로 이동 — 상단은 생존지표 전용) ──
    # 외국인 수급 수동 입력 (사이드바 고정)
    with st.expander("✍️ 외인 수급 수동입력", expanded=False):
        _sbfn = st.number_input("코스피 외국인 순매수 (억원, 매도는 음수)",
                                value=0.0, step=100.0, key="sb_fn_in")
        _sbb1, _sbb2 = st.columns(2)
        if _sbb1.button("적용", key="sb_fn_apply", use_container_width=True):
            _v = float(_sbfn) * 100_000_000
            st.session_state['_foreign_net_krw'] = _v
            st.session_state['_foreign_net_src'] = 'manual'
            try:
                _fb_ref("/foreign_net_manual").set(
                    {'krw': _v, 'date': datetime.now().strftime("%Y-%m-%d %H:%M")})
            except Exception:
                pass
            st.rerun()
        if _sbb2.button("자동복귀", key="sb_fn_auto", use_container_width=True):
            st.session_state.pop('_foreign_net_src', None)
            st.session_state.pop('_foreign_net_krw', None)
            try:
                _fb_ref("/foreign_net_manual").delete()
            except Exception:
                pass
            st.rerun()
    # 산자부 수출 수치 수동 입력 (사이드바 고정)
    with st.expander("📦 산자부 수출 수동입력", expanded=False):
        render_motie_manual_widget()

    # ── 세션 정보 + 로그아웃 ──
    _auth_time = st.session_state.get('_auth_time', '')
    _auth_user = st.session_state.get('_username', '')
    if _auth_user and _auth_user != 'default':
        st.caption(f"👤 **{_auth_user}** · {_auth_time}")
    elif _auth_time:
        st.caption(f"🔐 로그인: {_auth_time}")
    if st.button("🚪 로그아웃", key="sidebar_logout", use_container_width=True):
        _keys_to_clear = ['_auth_ok', '_auth_time', '_username',
                          'paper_account', '_paper_account_ts',
                          'op_positions', 'watchlist_data']
        for _k in _keys_to_clear:
            st.session_state.pop(_k, None)
        # 자동 로그인 토큰 제거 (로그아웃 확실히 유지)
        try:
            st.query_params.clear()
        except Exception:
            pass
        st.rerun()

    st.markdown("---")

    gemini_key = st.text_input("🔑 Gemini API 키", type="password",
                                help="aistudio.google.com에서 발급")

    st.markdown("### 📋 관심 종목")

    # 사이드바 — session_state 우선
    _sb_wl = get_watchlist()
    _sb_lines = [l.strip() for l in _sb_wl.split("\n") if "," in l.strip()]
    _sb_pairs = [l.split(",", 1) for l in _sb_lines if len(l.split(",", 1)) == 2]

    # 표시명 한글 정정: 내부 DB에 한글명이 있으면 영어 저장값 대신 한글 표기
    _name_fixed = False
    _sb_fixed_lines = []
    for _t, _n in _sb_pairs:
        _t_s = _t.strip(); _n_s = _n.strip()
        _kr_disp = _MASTER_ETF_DB.get(_t_s)
        if _kr_disp and _kr_disp != _n_s:
            _n = _kr_disp; _name_fixed = True
            _sb_fixed_lines.append(f"{_t_s},{_kr_disp}")
        else:
            _sb_fixed_lines.append(f"{_t_s},{_n_s}")
    # 정정된 이름을 watchlist에 영속화 (1회)
    if _name_fixed:
        st.session_state.watchlist_data = "\n".join(_sb_fixed_lines)
        _sb_pairs = [l.split(",", 1) for l in _sb_fixed_lines]

    for _t, _n in _sb_pairs:
        _t = _t.strip(); _n = _n.strip()
        _sc1, _sc2 = st.columns([3, 1])
        _sc1.markdown(f"<div style='font-size:12px; padding:4px 0'><b>{_n}</b><br><span style='color:#64748b; font-size:10px'>{_t}</span></div>", unsafe_allow_html=True)
        if _sc2.button("✕", key=f"sb_del_{_t}"):
            _new_lines = [l for l in _sb_lines if not l.startswith(_t + ",")]
            _new_text = "\n".join(_new_lines)
            st.session_state.watchlist_data = _new_text
            remove_ticker_from_firebase(_t)
            st.rerun()

    st.markdown("---")
    st.markdown("**➕ 종목 추가**")

    _sb_mkt = st.radio("시장", ["🇰🇷 국내", "🇺🇸 미국"], horizontal=True, key="sb_mkt")

    # ── 미국 인기 종목 목록 ──
    _US_POPULAR = {
        "AAPL":"Apple","MSFT":"Microsoft","NVDA":"NVIDIA","AMZN":"Amazon",
        "GOOGL":"Alphabet","META":"Meta","TSLA":"Tesla","AVGO":"Broadcom",
        "BRK-B":"Berkshire Hathaway","JPM":"JPMorgan","V":"Visa","MA":"Mastercard",
        "UNH":"UnitedHealth","JNJ":"Johnson & Johnson","XOM":"Exxon Mobil",
        "WMT":"Walmart","PG":"P&G","HD":"Home Depot","CVX":"Chevron",
        "MRK":"Merck","ABBV":"AbbVie","LLY":"Eli Lilly","PFE":"Pfizer",
        "BAC":"Bank of America","KO":"Coca-Cola","PEP":"PepsiCo",
        "ORCL":"Oracle","CRM":"Salesforce","ADBE":"Adobe","AMD":"AMD",
        "INTC":"Intel","QCOM":"Qualcomm","TXN":"Texas Instruments",
        "NFLX":"Netflix","DIS":"Disney","PYPL":"PayPal","SQ":"Block",
        "SHOP":"Shopify","UBER":"Uber","LYFT":"Lyft","ABNB":"Airbnb",
        "COIN":"Coinbase","HOOD":"Robinhood","PLTR":"Palantir",
        "RIVN":"Rivian","NIO":"NIO","BIDU":"Baidu","BABA":"Alibaba",
        "TSM":"TSMC","ASML":"ASML","ARM":"ARM Holdings",
        "SPY":"S&P500 ETF","QQQ":"나스닥100 ETF","IWM":"러셀2000 ETF",
        "GLD":"금 ETF","TLT":"장기국채 ETF","TQQQ":"나스닥3x","SQQQ":"나스닥-3x",
        "SOXX":"반도체 ETF","SMH":"반도체 ETF2","ARKK":"ARK 혁신",
        "JEPI":"JPM 배당","SCHD":"Schwab 배당","EWY":"한국 ETF",
    }

    if _sb_mkt == "🇰🇷 국내":
        _sb_query = st.text_input("종목명 또는 코드 검색", placeholder="삼성전자 또는 005930", key="sb_kr_query")
        _sb_sel_code = ""; _sb_sel_name = ""

        if _sb_query:
            _kr_map = _load_kr_stock_list()
            _q = _sb_query.strip()
            # 코드 or 이름으로 필터
            _matches = [
                (c, n) for c, n in _kr_map.items()
                if _q in n or _q in c
            ][:10]

            if _matches:
                _opts = [f"{n} ({c})" for c, n in _matches]
                _chosen = st.selectbox("검색결과", _opts, key="sb_kr_sel")
                if _chosen:
                    _sb_sel_name = _chosen.split(" (")[0]
                    _sb_sel_code = _chosen.split("(")[-1].replace(")", "")
            else:
                # DB 조회 실패 시 직접 입력 fallback
                _q_strip = _sb_query.strip()
                if _q_strip.isdigit() and len(_q_strip) == 6:
                    # 6자리 코드 직접 입력 → 한글 우선 해석 (내부 DB → pykrx → yfinance)
                    _fb_name = ""
                    # 내부 DB/pykrx에 한글명 있으면 yfinance 호출 생략
                    _kr_resolved = resolve_korean_name(_q_strip, "")
                    if _kr_resolved and _kr_resolved != _q_strip:
                        _fb_name = _kr_resolved
                    else:
                        # 최후 폴백: yfinance 영어명
                        try:
                            import yfinance as _yf_sb
                            for _sfx in [".KS", ".KQ"]:
                                _info_sb = _yf_sb.Ticker(_q_strip + _sfx).info
                                if _info_sb and _info_sb.get("shortName"):
                                    _fb_name = _info_sb["shortName"].replace(" Ordinary Shares", "").strip()
                                    break
                        except Exception:
                            pass
                        if not _fb_name:
                            _fb_name = _q_strip
                    _sb_sel_code = _q_strip
                    _sb_sel_name = _fb_name
                    st.info(f"✅ 코드 직접 입력: {_fb_name} ({_q_strip})")
                else:
                    st.caption("검색 결과 없음 — 6자리 종목코드를 직접 입력해보세요 (예: 005930)")

        if st.button("➕ 추가", key="sb_add", use_container_width=True, disabled=not _sb_sel_code):
            if add_ticker(_sb_sel_code.strip(), _sb_sel_name.strip()):
                st.success(f"✅ {_sb_sel_name} 추가됨")
                st.rerun()
            else:
                st.warning("이미 있는 종목")

    else:  # 미국
        _sb_query_us = st.text_input("티커 또는 종목명 검색", placeholder="AAPL 또는 Apple", key="sb_us_query")
        _sb_sel_code = ""; _sb_sel_name = ""

        if _sb_query_us:
            _q_us = _sb_query_us.strip().upper()
            # 인기 목록에서 필터
            _matches_us = [
                (t, n) for t, n in _US_POPULAR.items()
                if _q_us in t or _q_us in n.upper()
            ][:8]

            # 인기 목록 없으면 yfinance로 직접 조회 시도
            if not _matches_us:
                try:
                    import yfinance as yf
                    _info = yf.Ticker(_q_us).fast_info
                    _price = getattr(_info, 'last_price', None)
                    if _price:
                        _full = yf.Ticker(_q_us).info
                        _auto = _full.get("shortName") or _full.get("longName") or _q_us
                        _matches_us = [(_q_us, _auto)]
                except Exception:
                    pass

            if _matches_us:
                _opts_us = [f"{t} — {n}" for t, n in _matches_us]
                _chosen_us = st.selectbox("검색결과", _opts_us, key="sb_us_sel")
                if _chosen_us:
                    _sb_sel_code = _chosen_us.split(" — ")[0].strip()
                    _sb_sel_name = _chosen_us.split(" — ")[1].strip()
            else:
                st.caption("목록에 없는 종목이면 정확한 티커를 입력 후 추가")
                # 직접 입력 fallback
                _sb_sel_code = _sb_query_us.strip().upper()
                _sb_sel_name = _sb_query_us.strip().upper()

        if st.button("➕ 추가", key="sb_add_us", use_container_width=True, disabled=not _sb_sel_code):
            _final_code = _sb_sel_code.strip()
            _final_name = _sb_sel_name.strip()
            # 이름이 티커와 같으면 yfinance로 이름 보완
            if _final_name == _final_code:
                try:
                    import yfinance as yf
                    _full = yf.Ticker(_final_code).info
                    _final_name = _full.get("shortName") or _full.get("longName") or _final_code
                except Exception:
                    pass
            if add_ticker(_final_code, _final_name):
                st.success(f"✅ {_final_name} 추가됨")
                st.rerun()
            else:
                st.warning("이미 있는 종목")

    n = len(_sb_pairs)
    st.markdown(f"<div style='font-size:11px; color:#34d399'>✅ 총 {n}개 종목</div>", unsafe_allow_html=True)

    lookback = st.slider("분석 기간 (거래일)", 30, 120, 60)

    model_name = st.selectbox("Gemini 모델", [
        "models/gemini-2.5-flash",
        "models/gemini-2.5-pro",
        "models/gemini-2.0-flash",
    ], help="Flash: 빠름·하루 500회 무료 / Pro: 정밀분석·하루 25회 무료")

    st.markdown(f"<div style='font-size:10px; color:#64748b; text-align:center'>마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}</div>", unsafe_allow_html=True)
    refresh = st.button("🔄 강제 새로고침", use_container_width=True)
    if refresh:
        st.cache_data.clear()
        st.success("캐시 초기화 완료!")
        import time; time.sleep(0.5)
        st.rerun()

    st.markdown("---")
    st.markdown(
        "<div style='font-size:11px; color:#64748b; line-height:1.8'>"
        "📌 <b>보완 규칙 적용 중</b><br>"
        "• R:R 2.0 미만 기각<br>"
        "• 손절 -7% 킬스위치<br>"
        "• 09:00~09:30 진입 금지<br>"
        "• 물타기 절대 금지<br>"
        "• 현금 20% 유지"
        "</div>",
        unsafe_allow_html=True,
    )

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

# ── UI 설정 초기화 ──
if 'ui_dark' not in st.session_state:
    st.session_state.ui_dark = True
if 'ui_mobile' not in st.session_state:
    st.session_state.ui_mobile = False

# ── 다크/라이트 + 모바일/데스크탑 CSS 동적 적용 ──
if st.session_state.ui_dark:
    _theme_css = """
:root {
    --bg-base: #0a0f1e; --bg-card: #0f1726; --bg-sidebar: #0d1424;
    --border: rgba(255,255,255,0.08); --text-pri: #e2e8f0;
    --text-sec: #94a3b8; --text-dim: #64748b;
}
html, body, [class*="css"] { background-color: #0a0f1e !important; color: #e2e8f0 !important; }
.stApp { background: #0a0f1e !important; }
h1,h2,h3,h4 { color: #e2e8f0 !important; }
hr { border-color: rgba(255,255,255,0.08) !important; }
[data-testid="stSidebar"] { background: #0d1424 !important; border-right: 1px solid rgba(255,255,255,0.06) !important; }
.stTabs [data-baseweb="tab-list"] { background: rgba(255,255,255,0.04) !important; border-color: rgba(255,255,255,0.08) !important; }
.stTabs [data-baseweb="tab"] { color: #94a3b8 !important; }
.metric-card { background: #0f1726 !important; border-color: rgba(255,255,255,0.08) !important; }
.metric-card .value { color: #e2e8f0 !important; }
.stButton > button[kind="secondary"] { background: rgba(255,255,255,0.05) !important; border-color: rgba(255,255,255,0.12) !important; color: #94a3b8 !important; }
[data-testid="stExpander"] { background: rgba(255,255,255,0.03) !important; border-color: rgba(255,255,255,0.08) !important; }
.streamlit-expanderHeader { background: rgba(255,255,255,0.03) !important; border-color: rgba(255,255,255,0.08) !important; color: #e2e8f0 !important; }
[data-baseweb="select"] > div { background: #0f1726 !important; border-color: rgba(255,255,255,0.12) !important; color: #e2e8f0 !important; }
.stTextInput input, .stNumberInput input, textarea { background: #0f1726 !important; border-color: rgba(255,255,255,0.12) !important; color: #e2e8f0 !important; }
[data-testid="stMetric"] { background: #0f1726 !important; border-color: rgba(255,255,255,0.08) !important; }
[data-testid="stMetricValue"] { color: #e2e8f0 !important; }
"""
else:
    _theme_css = """
/* ══════════════════════════════════════════════════════
   라이트 모드 UI — 퀀트 관제탑 V9.7
   원칙: 눈부심 제거 · 정보 위계 유지 · 대비 10:1 이상
══════════════════════════════════════════════════════ */

/* ① 색상 토큰 */
:root {
    --bg-base:     #F8FAFC;   /* 쿨 그레이 오프화이트 — 쨍한 화이트 대신 */
    --bg-card:     #FFFFFF;   /* 카드만 순백 */
    --bg-sidebar:  #F1F5F9;   /* 사이드바 약간 어둡게 */
    --bg-hover:    #EFF6FF;   /* 호버: 아이스 블루 */
    --border:      #E2E8F0;   /* 미세 보더 */
    --border-focus:#3B82F6;
    --text-pri:    #1E293B;   /* 슬레이트 블루 — 순검정보다 부드러움 */
    --text-sec:    #475569;
    --text-dim:    #94A3B8;
    /* ② 금융 강조 색: 톤 다운 + 볼드로 대체 */
    --color-up:    #991B1B;   /* 크림슨 레드 — 상승/손실 */
    --color-down:  #1E40AF;   /* 딥 블루 — 하락 */
    --color-profit:#166534;   /* 포레스트 그린 — 수익 */
    --color-warn:  #92400E;   /* 앰버 브라운 — 경고 */
    /* ④ 그림자 */
    --shadow-sm:   0 1px 3px rgba(15,23,42,0.06), 0 1px 2px rgba(15,23,42,0.04);
    --shadow-md:   0 4px 12px rgba(15,23,42,0.08), 0 2px 4px rgba(15,23,42,0.05);
    --shadow-lg:   0 8px 24px rgba(15,23,42,0.10), 0 4px 8px rgba(15,23,42,0.06);
    --shadow-card: 0 2px 8px rgba(15,23,42,0.07), 0 1px 3px rgba(15,23,42,0.05);
}

/* ── 앱 기반 배경 ── */
html, body, [class*="css"] {
    background-color: var(--bg-base) !important;
    color: var(--text-pri) !important;
}
.stApp {
    background: linear-gradient(160deg, #F8FAFC 0%, #EEF2F8 100%) !important;
}

/* ── 헤더 텍스트 ── */
h1 {
    background: linear-gradient(135deg, #1D4ED8, #7C3AED) !important;
    -webkit-background-clip: text !important;
    -webkit-text-fill-color: transparent !important;
    font-weight: 800 !important;
}
h2, h3 { color: #1E293B !important; font-weight: 700 !important; }
h4      { color: #334155 !important; font-weight: 600 !important; }
p, li   { color: var(--text-pri) !important; line-height: 1.7; }
/* ③ 캡션/보조 텍스트 */
.stCaption, caption, small { color: var(--text-dim) !important; }
hr { border-color: var(--border) !important; }

/* ── 사이드바 ── */
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #FFFFFF 0%, #F1F5F9 100%) !important;
    border-right: 1px solid var(--border) !important;
    box-shadow: 2px 0 16px rgba(15,23,42,0.07) !important;
}
[data-testid="stSidebar"] * { color: var(--text-sec) !important; }
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 { color: var(--text-pri) !important; }
[data-testid="stSidebar"] label { color: var(--text-sec) !important; font-weight: 600 !important; }

/* ── 탭 바 ── */
.stTabs [data-baseweb="tab-list"] {
    background: #FFFFFF !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    box-shadow: var(--shadow-sm) !important;
    padding: 3px !important;
}
.stTabs [data-baseweb="tab"] {
    color: var(--text-sec) !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
    transition: all 0.15s ease !important;
}
.stTabs [data-baseweb="tab"]:hover {
    background: var(--bg-hover) !important;
    color: #1D4ED8 !important;
}
.stTabs [aria-selected="true"] {
    background: linear-gradient(135deg, #2563EB, #7C3AED) !important;
    color: #FFFFFF !important;
    font-weight: 700 !important;
    box-shadow: 0 3px 10px rgba(37,99,235,0.30) !important;
}

/* ── 메트릭 카드 (④ 소프트 섀도우) ── */
.metric-card {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: 14px !important;
    box-shadow: var(--shadow-card) !important;
    transition: box-shadow 0.2s ease, border-color 0.2s ease !important;
}
.metric-card:hover {
    border-color: #93C5FD !important;
    box-shadow: var(--shadow-lg) !important;
}
.metric-card .label { color: var(--text-dim) !important; font-size: 11px !important; }
.metric-card .value { color: var(--text-pri) !important; font-weight: 700 !important; }

/* ② 수익/손실 숫자 강조 (폰트 굵기로 대체) */
.metric-card .value.up   { color: var(--color-up)     !important; font-weight: 800 !important; }
.metric-card .value.down { color: var(--color-down)   !important; font-weight: 800 !important; }
.metric-card .value.flat { color: var(--text-pri)      !important; }

/* ── 버튼 ── */
.stButton > button {
    border-radius: 10px !important;
    font-weight: 600 !important;
    transition: all 0.15s ease !important;
}
.stButton > button[kind="secondary"] {
    background: #FFFFFF !important;
    border: 1px solid var(--border) !important;
    color: var(--text-sec) !important;
    box-shadow: var(--shadow-sm) !important;
}
.stButton > button[kind="secondary"]:hover {
    background: var(--bg-hover) !important;
    border-color: #93C5FD !important;
    color: #1D4ED8 !important;
    box-shadow: var(--shadow-md) !important;
}
.stButton > button[kind="primary"] {
    box-shadow: 0 3px 10px rgba(37,99,235,0.25) !important;
}

/* ── 입력 필드 ── */
.stTextInput input, .stNumberInput input, textarea {
    background: #FFFFFF !important;
    border: 1px solid #CBD5E1 !important;
    color: var(--text-pri) !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
}
.stTextInput input:focus, .stNumberInput input:focus, textarea:focus {
    border-color: var(--border-focus) !important;
    box-shadow: 0 0 0 3px rgba(59,130,246,0.12) !important;
    outline: none !important;
}
[data-baseweb="select"] > div {
    background: #FFFFFF !important;
    border-color: #CBD5E1 !important;
    color: var(--text-pri) !important;
    border-radius: 8px !important;
}

/* ── Expander / 카드 컨테이너 (④ 그림자) ── */
[data-testid="stExpander"] {
    background: #FFFFFF !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    box-shadow: var(--shadow-sm) !important;
    overflow: hidden !important;
}
.streamlit-expanderHeader {
    background: #F8FAFC !important;
    border-color: var(--border) !important;
    color: var(--text-pri) !important;
    font-weight: 600 !important;
}

/* ── Metric 위젯 ── */
[data-testid="stMetric"] {
    background: #FFFFFF !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    box-shadow: var(--shadow-card) !important;
}
[data-testid="stMetricLabel"] { color: var(--text-dim) !important; font-weight: 500 !important; }
[data-testid="stMetricValue"] { color: var(--text-pri) !important; font-weight: 700 !important; }
[data-testid="stMetricDelta"] svg { filter: none !important; }

/* ── 뱃지 (라이트 전용 색상) ── */
.badge-buy     { background: rgba(22,101,52,0.10)  !important; color: #166534 !important; border-color: rgba(22,101,52,0.25) !important; font-weight: 700 !important; }
.badge-sell    { background: rgba(153,27,27,0.08)  !important; color: #991B1B !important; border-color: rgba(153,27,27,0.20) !important; font-weight: 700 !important; }
.badge-watch   { background: rgba(30,64,175,0.08)  !important; color: #1E40AF !important; border-color: rgba(30,64,175,0.20) !important; font-weight: 700 !important; }
.badge-neutral { background: rgba(71,85,105,0.07)  !important; color: #475569 !important; border-color: rgba(71,85,105,0.15) !important; }

/* ── Gemini 분석 박스 ── */
.gemini-box {
    background: linear-gradient(135deg, rgba(37,99,235,0.05), rgba(99,102,241,0.03)) !important;
    border-left: 3px solid #2563EB !important;
    border-top: 1px solid rgba(37,99,235,0.15) !important;
    border-right: 1px solid rgba(37,99,235,0.08) !important;
    border-bottom: 1px solid rgba(37,99,235,0.08) !important;
    border-radius: 0 10px 10px 0 !important;
    color: var(--text-pri) !important;
    box-shadow: var(--shadow-sm) !important;
}

/* ── 알림/경고 ── */
.stAlert { border-radius: 10px !important; font-weight: 500 !important; }
[data-baseweb="notification"] { border-radius: 10px !important; }

/* ── 데이터프레임 ── */
[data-testid="stDataFrame"] {
    border: 1px solid var(--border) !important;
    border-radius: 10px !important;
    box-shadow: var(--shadow-sm) !important;
    overflow: hidden !important;
}
[data-testid="stDataFrame"] th {
    background: #F1F5F9 !important;
    color: var(--text-sec) !important;
    font-weight: 700 !important;
    border-bottom: 2px solid var(--border) !important;
}
[data-testid="stDataFrame"] td { color: var(--text-pri) !important; }

/* ── 구분선 / divider ── */
[data-testid="stDivider"] { border-color: var(--border) !important; }

/* ── 일반 텍스트 컬러 정규화 ── */
span, div { color: inherit; }
.stCaption { color: var(--text-dim) !important; }

/* ③ 라이트 전용: 인라인 수익/손실 색상 재정의 */
/* HTML 카드 내 색상은 inline style로 직접 입히므로
   아래 클래스로 오버라이드 제공 */
.lm-profit { color: #166534 !important; font-weight: 800 !important; }
.lm-loss   { color: #991B1B !important; font-weight: 800 !important; }
.lm-warn   { color: #92400E !important; font-weight: 700 !important; }
"""

if st.session_state.ui_mobile:
    _mobile_css = """
:root { --fs-xs:10px; --fs-sm:12px; --fs-md:13px; --fs-lg:15px; --fs-xl:19px; --fs-2xl:22px; --card-pad:12px 14px; --radius:10px; }
.stTabs [data-baseweb="tab"] { padding: 7px 10px !important; font-size: 11px !important; }
.stButton > button { padding: 7px 10px !important; font-size: 11px !important; }
.stDataFrame { font-size: 11px !important; }
"""
else:
    _mobile_css = ""

if _theme_css or _mobile_css:
    st.markdown(f"<style>{_theme_css}{_mobile_css}</style>", unsafe_allow_html=True)

# ── 헤더 + UI 토글 버튼 ──
_h1, _h2, _h3 = st.columns([4, 1, 1])
_h1.markdown("""
<div style='display:flex; align-items:center; gap:12px; margin-bottom:8px'>
    <span style='font-size:28px; font-weight:800; font-family:"IBM Plex Mono",monospace;
                 background:linear-gradient(90deg,#4da6ff,#a78bfa); -webkit-background-clip:text;
                 -webkit-text-fill-color:transparent'>퀀트 관제탑</span>
    <span style='font-size:12px; color:#64748b; font-family:"IBM Plex Mono",monospace'>V9.1</span>
</div>
""", unsafe_allow_html=True)

_dark_label  = "☀️ 라이트" if st.session_state.ui_dark  else "🌙 다크"
_mobile_label = "🖥 데스크탑" if st.session_state.ui_mobile else "📱 모바일"
if _h2.button(_dark_label,   key="toggle_dark",   use_container_width=True):
    st.session_state.ui_dark = not st.session_state.ui_dark
    st.rerun()
if _h3.button(_mobile_label, key="toggle_mobile", use_container_width=True):
    st.session_state.ui_mobile = not st.session_state.ui_mobile
    st.rerun()

now = st.session_state.get('_now_kst_str', '')   # 단일 KST 소스 참조
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

# ── Session State 핵심 변수 사전 초기화 ──
for _ss_key, _ss_default in [
    ('passed', []),
    ('all_data_cache', {}),
    ('ui_dark', True),
    ('opt_best_cond5', 0.08),
    ('opt_best_cond6', 0.50),
    ('paper_account', {'initial':10000000,'cash':10000000,'positions':[],'peak':10000000,'trough':10000000}),
    ('watchlist_data', None),
    ('gemini_model_global', 'gemini-1.5-flash'),
    ('etf_market_sel', '🇰🇷 국장 ETF'),
]:
    if _ss_key not in st.session_state:
        st.session_state[_ss_key] = _ss_default

# ══════════════════════════════════════════

# ── ETF 구성종목 DB (상위 보유 종목 하드코딩 — yfinance holdings API 불안정 대응) ──
_ETF_HOLDINGS_DB = {
    # 국장 ETF
    "069500": [("005930","삼성전자"),("000660","SK하이닉스"),("005490","POSCO홀딩스"),("005380","현대차"),("035420","NAVER"),("000270","기아"),("051910","LG화학"),("006400","삼성SDI"),("035720","카카오"),("055550","신한지주")],
    "102110": [("005930","삼성전자"),("000660","SK하이닉스"),("005490","POSCO홀딩스"),("005380","현대차"),("035420","NAVER"),("000270","기아"),("051910","LG화학"),("006400","삼성SDI"),("035720","카카오"),("055550","신한지주")],
    "114800": [("069500","KODEX200"),("005930","삼성전자"),("000660","SK하이닉스")],
    "122630": [("005930","삼성전자"),("000660","SK하이닉스"),("005490","POSCO홀딩스"),("005380","현대차"),("035420","NAVER")],
    "229200": [("005930","삼성전자"),("000660","SK하이닉스"),("005490","POSCO홀딩스"),("005380","현대차"),("035420","NAVER"),("000270","기아"),("051910","LG화학"),("006400","삼성SDI"),("035720","카카오"),("055550","신한지주")],
    "233740": [("005930","삼성전자"),("000660","SK하이닉스"),("005490","POSCO홀딩스"),("005380","현대차"),("035420","NAVER")],
    "091160": [("005930","삼성전자"),("000660","SK하이닉스"),("042700","한미반도체"),("066570","LG전자"),("009150","삼성전기"),("030200","KT"),("032830","삼성생명"),("017670","SK텔레콤"),("011200","HMM"),("010130","고려아연")],
    # KODEX AI반도체TOP2+ — 삼성전자·SK하이닉스 2배 레버리지 비중 + 관련 부품주
    "395160": [("005930","삼성전자"),("000660","SK하이닉스"),("042700","한미반도체"),("009150","삼성전기"),("011070","LG이노텍"),("036830","솔브레인홀딩스"),("357780","솔브레인"),("058470","리노공업"),("095340","ISC"),("039030","이오테크닉스")],
    # TIGER Fn반도체TOP10 — 반도체 밸류체인 상위 10종목
    "396500": [("005930","삼성전자"),("000660","SK하이닉스"),("042700","한미반도체"),("009150","삼성전기"),("011070","LG이노텍"),("036830","솔브레인홀딩스"),("357780","솔브레인"),("058470","리노공업"),("095340","ISC"),("240810","원익IPS")],
    # KODEX AI테크TOP10
    "457450": [("005930","삼성전자"),("000660","SK하이닉스"),("035420","NAVER"),("035720","카카오"),("042700","한미반도체"),("259960","크래프톤"),("036570","엔씨소프트"),("112040","위메이드"),("293490","카카오게임즈"),("251270","넷마블")],
    # TIGER 미국테크TOP10 INDXX
    "381170": [("MSFT","Microsoft"),("AAPL","Apple"),("NVDA","NVIDIA"),("AMZN","Amazon"),("META","Meta"),("GOOGL","Alphabet A"),("TSLA","Tesla"),("AVGO","Broadcom"),("NFLX","Netflix"),("CRM","Salesforce")],
    # KODEX 미국S&P500TR
    "379800": [("AAPL","Apple"),("MSFT","Microsoft"),("NVDA","NVIDIA"),("AMZN","Amazon"),("META","Meta"),("GOOGL","Alphabet A"),("BRK.B","Berkshire"),("LLY","Eli Lilly"),("AVGO","Broadcom"),("JPM","JPMorgan")],
    # TIGER 나스닥100
    "133690": [("MSFT","Microsoft"),("AAPL","Apple"),("NVDA","NVIDIA"),("AMZN","Amazon"),("META","Meta"),("GOOGL","Alphabet A"),("TSLA","Tesla"),("AVGO","Broadcom"),("GOOG","Alphabet C"),("COST","Costco")],
    # TIGER K방산&우주
    "463250": [("012450","한화에어로스페이스"),("329180","HD현대중공업"),("047810","한국항공우주"),("064350","현대로템"),("042660","한화오션"),("267250","HD현대"),("009540","HD한국조선해양"),("000720","현대건설"),("082740","HSD엔진"),("272210","한화시스템")],
    # KODEX AI전력핵심설비
    "487240": [("012450","한화에어로스페이스"),("267250","HD현대"),("042660","한화오션"),("082740","HSD엔진"),("298040","효성중공업"),("009560","현대중공업지주"),("001440","대한전선"),("272210","한화시스템"),("214430","아모텍"),("093240","이구산업")],
    "098560": [("005930","삼성전자"),("000660","SK하이닉스"),("042700","한미반도체"),("012450","한화에어로스페이스"),("329180","HD현대중공업"),("267250","HD현대"),("009540","HD한국조선해양")],
    "139220": [("006400","삼성SDI"),("051910","LG화학"),("247540","에코프로비엠"),("373220","LG에너지솔루션"),("096770","SK이노베이션"),("011070","LG이노텍"),("003670","포스코퓨처엠")],
    "305720": [("006400","삼성SDI"),("051910","LG화학"),("247540","에코프로비엠"),("373220","LG에너지솔루션"),("003670","포스코퓨처엠"),("096770","SK이노베이션"),("011070","LG이노텍")],
    "012450": [("012450","한화에어로스페이스"),("329180","HD현대중공업"),("000720","현대건설"),("267250","HD현대"),("047810","한국항공우주"),("064350","현대로템"),("042660","한화오션")],
    # 미장 ETF
    "SPY":  [("AAPL","Apple"),("MSFT","Microsoft"),("NVDA","NVIDIA"),("AMZN","Amazon"),("META","Meta"),("GOOGL","Alphabet A"),("BRK.B","Berkshire"),("LLY","Eli Lilly"),("AVGO","Broadcom"),("JPM","JPMorgan")],
    "QQQ":  [("MSFT","Microsoft"),("AAPL","Apple"),("NVDA","NVIDIA"),("AMZN","Amazon"),("META","Meta"),("GOOGL","Alphabet A"),("TSLA","Tesla"),("AVGO","Broadcom"),("GOOG","Alphabet C"),("COST","Costco")],
    "SOXX": [("NVDA","NVIDIA"),("AVGO","Broadcom"),("AMD","AMD"),("INTC","Intel"),("QCOM","Qualcomm"),("AMAT","Applied Materials"),("LRCX","Lam Research"),("MU","Micron"),("KLAC","KLA Corp"),("TXN","Texas Instruments")],
    "SOXL": [("NVDA","NVIDIA"),("AVGO","Broadcom"),("AMD","AMD"),("INTC","Intel"),("QCOM","Qualcomm"),("AMAT","Applied Materials"),("LRCX","Lam Research"),("MU","Micron"),("KLAC","KLA Corp"),("TXN","Texas Instruments")],
    "XLK":  [("MSFT","Microsoft"),("AAPL","Apple"),("NVDA","NVIDIA"),("AVGO","Broadcom"),("CRM","Salesforce"),("ORCL","Oracle"),("ACN","Accenture"),("AMD","AMD"),("NOW","ServiceNow"),("CSCO","Cisco")],
    "SMH":  [("NVDA","NVIDIA"),("TSM","TSMC"),("AVGO","Broadcom"),("ASML","ASML"),("TXN","Texas Instruments"),("QCOM","Qualcomm"),("AMAT","Applied Materials"),("MU","Micron"),("AMD","AMD"),("LRCX","Lam Research")],
    "TQQQ": [("MSFT","Microsoft"),("AAPL","Apple"),("NVDA","NVIDIA"),("AMZN","Amazon"),("META","Meta"),("GOOGL","Alphabet A"),("TSLA","Tesla"),("AVGO","Broadcom"),("GOOG","Alphabet C"),("COST","Costco")],
    "IWM":  [("SMCI","Super Micro"),("MSTR","MicroStrategy"),("CELH","Celsius"),("WTFC","Wintrust Financial"),("PLTR","Palantir"),("NTRA","Natera"),("APP","Applovin"),("PTON","Peloton"),("RH","RH"),("SAIA","Saia Inc")],
    "XLE":  [("XOM","Exxon Mobil"),("CVX","Chevron"),("COP","ConocoPhillips"),("EOG","EOG Resources"),("SLB","SLB"),("MPC","Marathon Petroleum"),("PSX","Phillips 66"),("PXD","Pioneer Natural"),("VLO","Valero Energy"),("DVN","Devon Energy")],
    "GLD":  [],  # 금 ETF — 개별종목 없음
    "TLT":  [],  # 채권 ETF — 개별종목 없음
    "ARKK": [("TSLA","Tesla"),("ROKU","Roku"),("COIN","Coinbase"),("PATH","UiPath"),("TWLO","Twilio"),("EXAS","Exact Sciences"),("CRSP","CRISPR Therapeutics"),("BEAM","Beam Therapeutics"),("TDOC","Teladoc"),("SHOP","Shopify")],
    "ARKG": [("RXRX","Recursion Pharma"),("CRSP","CRISPR Therapeutics"),("TWST","Twist Bioscience"),("PACB","Pacific Biosciences"),("CDNA","CareDx"),("ACMR","ACM Research"),("NVTA","Invitae"),("BEAM","Beam Therapeutics"),("NTLA","Intellia Therapeutics"),("VERV","Verve Therapeutics")],
    "ARKW": [("TSLA","Tesla"),("COIN","Coinbase"),("ROKU","Roku"),("MSTR","MicroStrategy"),("TWLO","Twilio"),("PATH","UiPath"),("TDOC","Teladoc"),("SHOP","Shopify"),("OPEN","Opendoor"),("DKNG","DraftKings")],
    "BOTZ": [("NVDA","NVIDIA"),("ISRG","Intuitive Surgical"),("ABB","ABB Ltd"),("FANUY","Fanuc"),("IRBT","iRobot"),("BRKS","Brooks Automation"),("KEYB","Keyence"),("OMRNY","Omron"),("AZPN","Aspen Tech"),("NNDM","Nano Dimension")],
    "CIBR": [("PANW","Palo Alto Networks"),("CRWD","CrowdStrike"),("FTNT","Fortinet"),("ZS","Zscaler"),("OKTA","Okta"),("S","SentinelOne"),("CYBR","CyberArk"),("QLYS","Qualys"),("VRNS","Varonis"),("TENB","Tenable")],
}

# ── 구성종목 DB 보유 ETF의 한글명 보충 매핑 ──────────────────────────────────
#   _MASTER_ETF_DB에 없는 코드(스나이핑 드롭다운이 '코드 (코드)'로 표기되던 원인)를
#   보강. 드롭다운 표기 전용 — 조회 우선순위: _MASTER_ETF_DB → 이 딕셔너리 → 코드.
_HOLDINGS_ETF_NAMES = {
    "114800": "KODEX 인버스",
    "122630": "KODEX 레버리지",
    "139220": "TIGER 2차전지테마",
    "012450": "한화에어로스페이스",
    "098560": "반도체·방산 혼합 바스켓",
}

@st.cache_data(ttl=300, show_spinner=False)
def _scan_etf_holdings(etf_code: str, is_korean: bool = True) -> list[dict]:
    """ETF 구성종목 개별 스캐닝 — Z-Score/RSI/ATR 기반 타점 산출"""
    import yfinance as yf
    holdings = _ETF_HOLDINGS_DB.get(etf_code, [])
    # DB에 없으면 yfinance로 구성종목 자동 조회 (미국 ETF만)
    if not holdings and not is_korean:
        try:
            _tk_obj = yf.Ticker(etf_code)
            _fund_data = _tk_obj.funds_data
            if _fund_data is not None:
                _top = getattr(_fund_data, 'top_holdings', None)
                if _top is not None and not _top.empty:
                    holdings = [(row.get('Symbol', sym), row.get('Name', sym))
                                for sym, row in _top.head(10).iterrows()]
        except Exception:
            pass
    if not holdings:
        return []
    results = []
    for code, name in holdings[:8]:  # 상위 8개만
        try:
            sym = f"{code}.KS" if is_korean else code
            df  = yf.Ticker(sym).history(period="3mo", interval="1d")
            if df is None or len(df) < 20:
                continue

            cl  = df["Close"]; hi = df["High"]; lo = df["Low"]; vo = df["Volume"]
            cur = float(cl.iloc[-1])
            if cur <= 0:
                continue

            # ATR14
            tr   = pd.concat([hi-lo, (hi-cl.shift()).abs(), (lo-cl.shift()).abs()], axis=1).max(axis=1)
            atr  = float(tr.rolling(14).mean().iloc[-1])
            atr_r = atr / cur

            # RSI14
            d = cl.diff(); g = d.clip(lower=0).rolling(14).mean(); l_ = (-d).clip(lower=0).rolling(14).mean()
            rsi = float(100 - 100 / (1 + g.iloc[-1] / (l_.iloc[-1] + 1e-9)))

            # Z-Score20
            mu = cl.rolling(20).mean().iloc[-1]; sd = cl.rolling(20).std().iloc[-1]
            zscore = float((cur - mu) / (sd + 1e-9))

            # MA5 이격
            ma5     = float(cl.rolling(5).mean().iloc[-1])
            ma5_diff = (cur - ma5) / ma5 * 100

            # 거래대금 (당일)
            turnover = cur * float(vo.iloc[-1])

            # 전일 저가 지지선
            prev_low = float(lo.iloc[-2]) if len(lo) >= 2 else cur * 0.95

            # 타점 판단
            if zscore <= -0.5 and rsi <= 45 and abs(ma5_diff) <= 3:
                signal = "🎯 눌림목 타점"
                signal_color = "#089981"
            elif zscore <= 0 and rsi <= 55:
                signal = "⏳ 대기"
                signal_color = "#f0b90b"
            else:
                signal = "⚠️ 과열"
                signal_color = "#f23645"

            # 손절: 전일저가 또는 -5% (더 타이트한 쪽)
            stop  = max(prev_low, cur * 0.95)
            target = cur * (1 + atr_r * 2)  # ATR 2배 목표
            rr    = (target - cur) / (cur - stop + 1e-9)

            results.append({
                "종목코드": code, "종목명": name,
                "현재가": round(cur, 0 if is_korean else 2),
                "RSI": round(rsi, 1), "Z-Score": round(zscore, 2),
                "ATR%": round(atr_r * 100, 2), "MA5이격": round(ma5_diff, 2),
                "거래대금": turnover,
                "타점": signal, "타점색": signal_color,
                "목표가": round(target, 0 if is_korean else 2),
                "손절가": round(stop, 0 if is_korean else 2),
                "R:R": round(rr, 2),
            })
        except Exception:
            continue

    # 거래대금 + Z-Score 낮은 순 정렬 (대장주 + 눌림목 우선)
    results.sort(key=lambda x: (-x["거래대금"], x["Z-Score"]))
    return results


def _batch_download_ohlcv(symbols):
    """여러 티커를 한 번의 HTTP 요청으로 받아 {symbol: DataFrame} 반환.
    Rate-limit 회피용 — 56개 개별 호출 대신 1회 batch download."""
    import yfinance as yf
    out = {}
    if not symbols:
        return out
    try:
        _data = yf.download(symbols, period="1y", interval="1d",
                            group_by='ticker', auto_adjust=True,
                            threads=True, progress=False)
    except Exception:
        return out
    for _s in symbols:
        try:
            if len(symbols) == 1:
                _sub = _data
            else:
                _sub = _data[_s] if _s in _data.columns.get_level_values(0) else None
            if _sub is not None and not _sub.empty:
                out[_s] = _sub.dropna(subset=['Open', 'High', 'Low', 'Close'])
        except Exception:
            continue
    return out


def _calc_etf_indicators(ticker_sym, prefetch_df=None):
    """yfinance ticker symbol로 ETF 지표 계산. 실패시 None 반환.
    prefetch_df: batch download으로 미리 받은 DataFrame (선택)."""
    import yfinance as yf
    import numpy as np
    import time as _t_etf
    try:
        # prefetch_df: batch download으로 미리 받은 DataFrame (rate-limit 회피).
        # 주어지면 개별 호출을 생략한다.
        _df = prefetch_df
        if _df is None:
            # Rate-limit 대응: 빈 응답 시 짧은 백오프로 최대 3회 재시도
            for _try in range(3):
                try:
                    _df = yf.Ticker(ticker_sym).history(period="1y", interval="1d")
                except Exception:
                    _df = None
                if _df is not None and len(_df) >= 60:
                    break
                _t_etf.sleep(0.4 * (_try + 1))
        if _df is None or len(_df) < 60:
            return None
        # ⚠️ Rate-limit 시 yfinance가 OHLC에 NaN 섞인 행을 반환 → NaN이 가격필터를
        #    통과(NaN<1=False)하고 ADX가 NaN→0이 되는 버그 차단. NaN 행 전부 제거.
        _df = _df.dropna(subset=['Open', 'High', 'Low', 'Close'])
        if len(_df) < 60:
            return None
        _cl  = _df['Close']; _hi = _df['High']; _lo = _df['Low']; _vol = _df['Volume']

        # 가격 이상값 감지: 통화별 범위 자동 분기 (지수값/오류값 혼입 방지)
        # .KS/.KQ 접미사 = 한국 ETF(원화) / 접미사 없음 = 미국 ETF(달러)
        _last_price = float(_cl.iloc[-1])
        if not np.isfinite(_last_price):   # NaN/inf 가격 → 데이터 불량
            return None
        _is_kr_sym = ticker_sym.endswith('.KS') or ticker_sym.endswith('.KQ')
        if _is_kr_sym:
            if _last_price < 500 or _last_price > 2_000_000:   # 원화: 500원~200만원
                return None
        else:
            if _last_price < 1 or _last_price > 10_000:        # 달러: $1~$10,000
                return None

        _tr   = pd.DataFrame({'hl':_hi-_lo,'hc':(_hi-_cl.shift()).abs(),'lc':(_lo-_cl.shift()).abs()}).max(axis=1)
        _atr  = _tr.rolling(14).mean()
        _pdm  = _hi.diff().clip(lower=0); _ndm = (-_lo.diff()).clip(lower=0)
        _pdi  = 100*_pdm.rolling(14).mean()/_atr.replace(0,np.nan)
        _ndi  = 100*_ndm.rolling(14).mean()/_atr.replace(0,np.nan)
        _dx   = 100*(_pdi-_ndi).abs()/(_pdi+_ndi).replace(0,np.nan)
        _adx_raw = _dx.rolling(14).mean().iloc[-1]
        # ADX가 NaN = 데이터 불량(throttle). 가짜 '탈락'(ADX0) 대신 실패 처리.
        if not np.isfinite(float(_adx_raw)):
            return None
        _adx  = round(float(_adx_raw), 1)
        _adx  = min(100.0, max(0.0, _adx))

        _delta = _cl.diff(); _gain = _delta.clip(lower=0).rolling(14).mean()
        _loss  = (-_delta.clip(upper=0)).rolling(14).mean()
        # 순수 상승장(_loss=0)이면 RSI=100 (NaN 방지: 1e-9 하한)
        _rsi_raw = (100 - 100/(1 + _gain.iloc[-1] / max(float(_loss.iloc[-1]), 1e-9)))
        _rsi = round(float(_rsi_raw), 1) if _rsi_raw == _rsi_raw else 50.0

        _ema12 = _cl.ewm(span=12).mean(); _ema26 = _cl.ewm(span=26).mean()
        _macd  = _ema12 - _ema26; _signal = _macd.ewm(span=9).mean()
        _mv = _macd.iloc[-1]; _sv = _signal.iloc[-1]; _mp = _macd.iloc[-2]; _sp = _signal.iloc[-2]
        if _mv > _sv and _mp <= _sp:   _macd_sig = '🟢골든크로스'
        elif _mv > _sv:                _macd_sig = '▲상승'
        elif _mv < _sv and _mp >= _sp: _macd_sig = '🔴데드크로스'
        else:                          _macd_sig = '▼하락'

        _ret = _cl.pct_change()
        _zs  = round((_ret.iloc[-1]-_ret.rolling(20).mean().iloc[-1])/_ret.rolling(20).std().iloc[-1]
                     if _ret.rolling(20).std().iloc[-1] > 0 else 0, 2)
        _mom = round((_cl.iloc[-1]/_cl.iloc[-20]-1)*100, 2) if len(_cl)>=20 else 0
        _vol_r = round(_vol.iloc[-1]/_vol.tail(20).mean()*100, 0) if _vol.tail(20).mean() > 0 else 100

        _ma5 = _cl.rolling(5).mean().iloc[-1]; _ma20 = _cl.rolling(20).mean().iloc[-1]; _ma60 = _cl.rolling(60).mean().iloc[-1]
        _aligned = bool(_cl.iloc[-1] > _ma5 > _ma20 > _ma60)

        _score = 0
        if _adx >= 40: _score += 25
        elif _adx >= 30: _score += 18
        elif _adx >= 25: _score += 12
        if 40 <= _rsi <= 60: _score += 15
        elif 30 <= _rsi < 40: _score += 10
        elif 60 < _rsi <= 70: _score += 8
        elif _rsi < 30: _score += 5
        if '골든크로스' in _macd_sig: _score += 20
        elif '상승' in _macd_sig: _score += 12
        elif '하락' in _macd_sig: _score += 4
        if _zs >= 1.5: _score += 15
        elif _zs >= 0.5: _score += 10
        elif _zs >= -0.5: _score += 6
        elif _zs >= -1.5: _score += 2
        if _mom >= 10: _score += 15
        elif _mom >= 5: _score += 10
        elif _mom >= 0: _score += 6
        elif _mom >= -5: _score += 2
        if _aligned: _score += 10
        if _vol_r >= 200: _score += 10
        elif _vol_r >= 150: _score += 7
        elif _vol_r >= 100: _score += 4

        _chg = round((_cl.iloc[-1]/_cl.iloc[-2]-1)*100, 2)
        # 갭상승 뇌동매매 차단용 데이터
        _open_today    = float(_df['Open'].iloc[-1])
        _prev_close    = float(_cl.iloc[-2])
        _gap_pct       = (_open_today - _prev_close) / _prev_close if _prev_close > 0 else 0
        _cur_vs_ma5    = (float(_cl.iloc[-1]) - _ma5) / _ma5 if _ma5 > 0 else 0
        return {
            'ADX': _adx, 'RSI': _rsi, 'MACD': _macd_sig,
            'Z-Score': _zs, '모멘텀(%)': _mom, '거래량%': _vol_r,
            '정배열': '✅' if _aligned else '❌',
            '종합점수': _score, '등락(%)': _chg,
            '현재가': round(_cl.iloc[-1], 2),
            '상태': '활성' if _adx >= 25 else '탈락',
            '갭(%)': round(_gap_pct * 100, 2),
            'MA5이격(%)': round(_cur_vs_ma5 * 100, 2),
            'MA5가격': round(_ma5, 2),
            '전일종가': round(_prev_close, 2),
        }
    except Exception:
        return None

@st.cache_data(ttl=1800, show_spinner=False)
def _get_home_etf_top(n=6):
    """홈탭 관제판용 — 국장+미장 ETF 상위 N개 빠른 조회 (점수≥60 필터)"""
    _QUICK_KR = [("395160","KODEX AI반도체TOP2+"),("091160","KODEX 반도체"),
                 ("069500","KODEX 200"),("463250","TIGER K방산&우주"),
                 ("487240","KODEX AI전력핵심설비"),("133690","TIGER 나스닥100"),
                 ("364980","TIGER 조선TOP10"),("305720","KODEX 2차전지산업")]
    _QUICK_US = [("QQQ","나스닥100"),("SOXX","iShares 반도체"),("SMH","VanEck 반도체"),
                 ("ARKK","ARK 혁신"),("ARKG","ARK 유전체"),("XLK","Technology"),
                 ("TQQQ","나스닥3x"),("SPY","S&P500")]
    rows = []
    for code, name in _QUICK_KR:
        ind = _calc_etf_indicators(f"{code}.KS")
        if ind and ind.get('종합점수', 0) >= 60:
            rows.append({'코드': code, 'ETF명': name, '시장': '🇰🇷', **ind})
    for code, name in _QUICK_US:
        ind = _calc_etf_indicators(code)
        if ind and ind.get('종합점수', 0) >= 60:
            rows.append({'코드': code, 'ETF명': name, '시장': '🇺🇸', **ind})
    rows.sort(key=lambda r: r.get('종합점수', 0), reverse=True)
    return rows[:n]


tab_a, tab_b, tab_c, tab_d, tab_e = st.tabs(["🏠 홈", "🔍 분석", "📡 스캐너", "🔄 전략", "⚙️ 관리"])


with tab_a:
    # ──────────────────────────────────────────────────────────────────────
    # V9.0 4-Panel Command Center
    # ──────────────────────────────────────────────────────────────────────
    def _get_market():
        # 단일 소스(get_index_quotes)로 통합 — 헤더/사이드바/브리핑 지수값 완전 일치
        return get_index_quotes()

    from datetime import datetime as _dt_cc
    _kst_h = (_dt_cc.utcnow().hour + 9) % 24
    _kst_m = _dt_cc.utcnow().minute
    _is_market_open = (9 <= _kst_h < 16) and not (_kst_h == 9 and _kst_m < 30)
    _blackout_48 = False
    _v891_home = run_v891_system_check()
    if not _v891_home['can_enter']:
        _blackout_48 = True

    # ── 모의투자 모드 배너 ──
    st.markdown("""
<div style='background:linear-gradient(90deg,#1e1b4b,#312e81);border:1px solid #4f46e5;
border-radius:8px;padding:8px 16px;display:flex;justify-content:space-between;align-items:center;margin-bottom:8px'>
  <div style='display:flex;align-items:center;gap:10px'>
    <span style='background:#4f46e5;color:#fff;font-size:10px;font-weight:800;
    padding:2px 8px;border-radius:4px'>📋 모의투자 모드</span>
    <span style='color:#a5b4fc;font-size:12px'>실전 자금 미사용 — 모든 거래는 페이퍼 트레이딩으로 기록됩니다</span>
  </div>
  <span style='color:#6366f1;font-size:11px'>실전 로직 검증 중 ✓</span>
</div>""", unsafe_allow_html=True)

    # ── CSS: 글로우/점멸 애니메이션 ──
    st.markdown("""
<style>
@keyframes redBlink {
  0%,100%{box-shadow:0 0 8px 2px #ef4444;}
  50%{box-shadow:0 0 0 0 transparent;}
}
@keyframes greenGlow {
  0%,100%{box-shadow:0 0 12px 3px #16a34a;}
  50%{box-shadow:0 0 20px 6px #22c55e;}
}
/* 깜빡임 제거 → 정적 글로우로 강조 (눈 피로 방지) */
.card-stop-warn {box-shadow:0 0 12px 2px rgba(239,68,68,0.6);}
.card-profit-high {box-shadow:0 0 12px 2px rgba(34,197,94,0.5);}

/* 2차 다이어트: 여백 확보 + 지표 폰트 대형화 (배경은 테마 CSS에 위임 — 라이트 모드 깨짐 방지) */
div[data-testid="stMetric"] {
  border:1px solid rgba(128,128,128,0.25); border-radius:12px; padding:10px 14px;
}
div[data-testid="stMetricValue"] { font-size:1.55rem; font-weight:800; }
div[data-testid="stMetricLabel"] { font-size:0.78rem; }
/* 사이드바 Sticky 상태 패널 — 스크롤해도 상단 고정 */
section[data-testid="stSidebar"] > div:first-child { padding-top:8px; }
/* 긴급 경고(st.error) 강조 — 큰 폰트·굵게 */
div[data-testid="stAlert"] { font-size:0.95rem; font-weight:700; border-radius:10px; }
/* 블록 간 간격 살짝 넓혀 가독성 확보 */
div[data-testid="stVerticalBlock"] { gap:0.55rem; }
</style>""", unsafe_allow_html=True)

    # ── 상단 상태 바 (지수 배지와 세로 중앙 정렬) ──
    try:
        _sb_cols = st.columns([3, 1, 1, 1, 1], vertical_alignment="center")
    except TypeError:
        _sb_cols = st.columns([3, 1, 1, 1, 1])   # 구버전 폴백
    # H2(##) 대신 여백 없는 인라인 타이틀 → 배지와 같은 수평선 정렬
    _sb_cols[0].markdown(
        "<div style='font-size:23px;font-weight:900;color:#f0f4ff;line-height:1.2;margin:0'>"
        "🎯 V9.1 <span style='background:linear-gradient(90deg,#4da6ff,#a78bfa);"
        "-webkit-background-clip:text;-webkit-text-fill-color:transparent'>Quant Command Center</span></div>",
        unsafe_allow_html=True)
    _market_badge = (
        "<span style='background:#16a34a;color:#fff;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:700'>● 장중</span>"
        if _is_market_open else
        "<span style='background:#374151;color:#9ca3af;padding:3px 10px;border-radius:20px;font-size:12px'>○ 장외</span>"
    )
    _sb_cols[1].markdown(_market_badge, unsafe_allow_html=True)
    _mkt_home = _get_market()
    for _i_sb, (_nm_sb, _key_sb) in enumerate([("코스피","코스피"),("코스닥","코스닥"),("나스닥","나스닥")]):
        _d_sb = _mkt_home.get(_key_sb, {})
        if _d_sb:
            _up_sb = _d_sb.get('등락', 0) > 0
            _c_sb = "#f63d68" if _up_sb else "#3b82f6"
            _sb_cols[2+_i_sb].markdown(
                f"<div style='font-size:11px;color:#64748b'>{_nm_sb}</div>"
                f"<div style='font-size:13px;font-weight:700;color:{_c_sb}'>{'▲' if _up_sb else '▼'}{abs(_d_sb.get('등락',0)):.2f}%</div>",
                unsafe_allow_html=True)

    # ── 지수 새로고침 (Streamlit은 상호작용 없으면 자동 갱신 안 됨 → 수동 갱신) ──
    _rf1, _rf2 = st.columns([1, 6])
    if _rf1.button("🔄 지수 갱신", key="refresh_index", use_container_width=True):
        get_index_quotes.clear()          # 단일 소스 캐시 비움 → 헤더·사이드바 동시 갱신
        try:
            check_index_shutdown.clear()
        except Exception:
            pass
        st.rerun()
    _rf2.caption(f"🕒 현재: {st.session_state.get('_now_kst', datetime.utcnow()+timedelta(hours=9)).strftime('%H:%M:%S')} KST "
                 f"· 자동 캐시 120초 (실시간 반영하려면 🔄)")

    # (전략 방향 · 블랙아웃 경고 · 수동 입력은 모두 사이드바 Sticky 패널로 이전 —
    #  본문은 데이터 모니터링에만 집중. 여기서는 아무것도 렌더링하지 않음.)

    # ══════════════════════════════════════════════════════════════════════
    # 🌐 외국인 수급 자동 연동 (pykrx) — 실패 시 수동 입력 폴백
    # ══════════════════════════════════════════════════════════════════════
    # 수동 입력이 있으면 자동/추정으로 덮어쓰지 않음 (사용자 우선). Firebase 복원 포함.
    if st.session_state.get('_foreign_net_src') != 'manual':
        try:
            _fn_saved = _fb_ref("/foreign_net_manual").get()
        except Exception:
            _fn_saved = None
        if isinstance(_fn_saved, dict) and _fn_saved.get('krw') is not None:
            st.session_state['_foreign_net_krw'] = float(_fn_saved['krw'])
            st.session_state['_foreign_net_src'] = 'manual'

    if st.session_state.get('_foreign_net_src') != 'manual':
        _fn_auto = get_foreign_net_kospi()
        if _fn_auto is not None:
            st.session_state['_foreign_net_krw'] = _fn_auto
            st.session_state['_foreign_net_src'] = 'auto'
        else:
            # pykrx 실패 → KIS 대형주 합산 추정 시도
            _fn_kis, _fn_hit = get_foreign_net_kospi_kis_estimate()
            if _fn_kis is not None:
                st.session_state['_foreign_net_krw'] = _fn_kis
                st.session_state['_foreign_net_src'] = 'kis_est'
                st.session_state['_foreign_net_hit'] = _fn_hit
            elif st.session_state.get('_foreign_net_krw') is None:
                st.session_state['_foreign_net_src'] = 'none'

    # KIS 추정 출처 안내 (수동 입력창은 사이드바 Sticky 패널로 이동됨)
    if st.session_state.get('_foreign_net_src') == 'kis_est':
        st.caption(f"🏦 외국인 수급: KIS 대형주 {st.session_state.get('_foreign_net_hit',0)}종목 합산 추정 "
                   f"(방향 신뢰·규모 근사) — 정확값은 사이드바 '외인 수급 수동입력'에서 덮어쓰기")

    # ══════════════════════════════════════════════════════════════════════
    # 🤖 5AI Top-Down 레짐 브리핑 패널 (오늘의 AI 코멘트 — 3줄 요약)
    # ══════════════════════════════════════════════════════════════════════
    try:
        _ai_krw  = get_usd_krw()
        _ai_flow = st.session_state.get('_foreign_net_krw', None)
        try:
            _ai_tops = _get_home_etf_top(1)
            _ai_top1 = _ai_tops[0] if _ai_tops else None
        except Exception:
            _ai_top1 = None
        _brief = generate_ai_briefing(_ai_krw, _ai_flow, _ai_top1)
        # 테마(라이트/다크) 자동 대응 — 네이티브 컴포넌트 사용(강제 다크배경 제거)
        _render_fn = {"green": st.success, "amber": st.warning, "red": st.error}.get(
            _brief["light"], st.info)
        _brief_md = (f"**🤖 오늘의 5AI 브리핑 — {_brief['verdict']}**\n\n"
                     + "\n".join(f"- {_ln[3:].strip()}" for _ln in _brief["lines"]))
        _render_fn(_brief_md)
    except Exception:
        st.caption("⚠️ 5AI 브리핑 일시 비활성 (데이터 지연)")

    # ══════════════════════════════════════════════════════════════════════
    # 📇 매크로 핵심 지표 카드 행 — 환율·유가·외국인수급·반도체수출 (대형 폰트)
    # ══════════════════════════════════════════════════════════════════════
    _me = fetch_motie_exports()
    _card_krw  = get_usd_krw()
    _card_oil  = get_wti_oil()
    _card_flow = st.session_state.get('_foreign_net_krw', None)
    _mc1, _mc2, _mc3, _mc4 = st.columns(4)

    # 환율 — 1,450/1,500 임계 대비 색상
    if isinstance(_card_krw, (int, float)):
        _krw_delta = ("🚨 1,500 돌파" if _card_krw >= 1500 else
                      "⚠️ 경계" if _card_krw >= 1450 else "안정")
        _mc1.metric("💱 원/달러 환율", f"{_card_krw:,.0f}원",
                    delta=_krw_delta,
                    delta_color=("inverse" if _card_krw >= 1450 else "off"))
    else:
        _mc1.metric("💱 원/달러 환율", "조회실패")

    # WTI 유가 — $90/$100 임계
    if isinstance(_card_oil, (int, float)):
        _oil_delta = ("🚨 $100 돌파" if _card_oil >= 100 else
                      "⚠️ 경계" if _card_oil >= 90 else "안정")
        _mc2.metric("🛢️ WTI 유가", f"${_card_oil:.1f}",
                    delta=_oil_delta,
                    delta_color=("inverse" if _card_oil >= 90 else "off"))
    else:
        _mc2.metric("🛢️ WTI 유가", "조회실패")

    # 외국인 수급 — 순매수/순매도 색상
    if isinstance(_card_flow, (int, float)):
        _flow_ok = _card_flow > 0
        _mc3.metric("🌍 외국인 수급", f"{_card_flow/1e8:+,.0f}억",
                    delta=("순매수 (게이트 개방)" if _flow_ok else "순매도 (게이트 폐쇄)"),
                    delta_color=("normal" if _flow_ok else "inverse"))
    else:
        _mc3.metric("🌍 외국인 수급", "데이터 없음")

    # 반도체 수출 전년동월비
    _yoy = _me.get("semi_yoy") if _me else None
    if isinstance(_yoy, (int, float)):
        _mc4.metric("💾 반도체 수출 YoY", f"{_yoy:+.1f}%",
                    delta=("서프라이즈" if _yoy >= 20 else "둔화" if _yoy < 0 else "보통"),
                    delta_color=("normal" if _yoy >= 0 else "inverse"))
    else:
        _mc4.metric("💾 반도체 수출 YoY", "대기 중")

    # (산자부 총/반도체 상세 + 수동 입력은 사이드바로 이동 — 본문은 카드만 표시)

    st.markdown("<hr style='margin:6px 0;border-color:#1e2a3a'>", unsafe_allow_html=True)

    # ── 2행 레이아웃 (압착 방지) ──
    # 1행: 계좌 요약 + 통합 랭킹 / 2행: 포트폴리오 관제 + 차트
    _p1, _p2 = st.columns([1, 1.6])

    # ══════════════════════════════════════════════
    # PANEL 1 — Account Summary + Live Signal Stream
    # ══════════════════════════════════════════════
    with _p1:
        _acc_cc = load_account()
        _pos_list_cc = _acc_cc.get('positions', [])
        _total_eval = _acc_cc['cash']
        _pos_pnl_pct = 0.0

        # 포지션 현재 평가금액 계산 (캐시 활용)
        for _pcc in _pos_list_cc:
            try:
                _sym_cc = _pcc['ticker']
                if is_korean_ticker(_sym_cc):
                    _sym_cc_yf = f"{_pcc['ticker']}.KS"
                else:
                    _sym_cc_yf = _pcc['ticker']
                _cur_cc = float(_pcc.get('avg_price', 0))  # fallback
                if _sym_cc in all_data:
                    _v_cc = all_data[_sym_cc]['df']['종가'].iloc[-1]
                    if _v_cc and not pd.isna(_v_cc):
                        _cur_cc = float(_v_cc)
                else:
                    import yfinance as _yf_cc
                    _h_cc = _yf_cc.Ticker(_sym_cc_yf).history(period="5d")
                    if isinstance(_h_cc.columns, pd.MultiIndex):
                        _h_cc.columns = _h_cc.columns.get_level_values(0)
                    if not _h_cc.empty:
                        _v_cc2 = _h_cc['Close'].dropna()
                        if not _v_cc2.empty and not pd.isna(_v_cc2.iloc[-1]):
                            _cur_cc = float(_v_cc2.iloc[-1])
                _eval_cc = _cur_cc * _pcc['qty']
                _total_eval += _eval_cc
            except Exception:
                _total_eval += _pcc.get('avg_price', 0) * _pcc.get('qty', 0)

        _ret_pct = (_total_eval / _acc_cc['initial'] - 1) * 100 if _acc_cc['initial'] > 0 else 0
        _ret_color = "#16a34a" if _ret_pct >= 0 else "#ef4444"

        st.markdown(f"""
<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:12px;padding:14px 16px;margin-bottom:10px'>
  <div style='font-size:11px;color:#64748b;margin-bottom:2px'>ACCOUNT SUMMARY</div>
  <div style='font-size:22px;font-weight:800;color:#f0f4ff'>{f"{_total_eval/1e6:.1f}" if not pd.isna(_total_eval) else "?"}M <span style='font-size:13px;color:#64748b'>KRW</span></div>
  <div style='display:flex;gap:14px;margin-top:8px'>
    <div>
      <div style='font-size:10px;color:#64748b'>Portfolio Return</div>
      <div style='font-size:16px;font-weight:700;color:{_ret_color}'>{_ret_pct:+.2f}%</div>
    </div>
    <div>
      <div style='font-size:10px;color:#64748b'>보유종목</div>
      <div style='font-size:16px;font-weight:700;color:#f0f4ff'>{len(_pos_list_cc)}개</div>
    </div>
    <div>
      <div style='font-size:10px;color:#64748b'>가용현금</div>
      <div style='font-size:14px;font-weight:600;color:#94a3b8'>{_acc_cc['cash']/1e6:.1f}M</div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)


        # 매크로 이벤트 다음 일정
        _future_cc = sorted(
            [e for e in st.session_state.get('macro_events', [])
             if e['date'] >= _dt_cc.now().strftime("%Y-%m-%d")],
            key=lambda x: x['date']
        )
        if _future_cc:
            _ne = _future_cc[0]
            _ne_dt = _dt_cc.strptime(_ne['date'], "%Y-%m-%d")
            _ne_days = (_ne_dt - _dt_cc.now()).days
            _ne_c = "#ef4444" if _ne_days <= 2 else "#f97316" if _ne_days <= 7 else "#64748b"
            st.markdown(
                f"<div style='margin-top:8px;background:#0d1117;border-radius:6px;padding:7px 10px;font-size:11px'>"
                f"<span style='color:#64748b'>다음 이벤트</span> "
                f"<span style='color:{_ne_c};font-weight:700'>{_ne['name']}</span> "
                f"<span style='color:#64748b'>D-{_ne_days}</span></div>",
                unsafe_allow_html=True
            )

        if st.button("🔄 새로고침", key="home_refresh_cc", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    # ══════════════════════════════════════════════
    # PANEL 2 — Global Integrated Rankings
    # ══════════════════════════════════════════════
    with _p2:
        st.markdown("""<div style='font-size:11px;color:#64748b;font-weight:700;margin-bottom:6px'>
        GLOBAL INTEGRATED RANKINGS <span style='color:#374151'>(Score ≥ 60)</span></div>""",
        unsafe_allow_html=True)

        _rank_tab = st.radio("", ["국장 ETFs", "미장 ETFs", "관심종목"], horizontal=True, key="home_rank_tab", label_visibility="collapsed")

        if _rank_tab in ("국장 ETFs", "미장 ETFs"):
            with st.spinner("랭킹 로딩 중..."):
                _home_etfs = _get_home_etf_top(8)

            _filtered_etfs = [r for r in _home_etfs if
                (r['시장'] == '🇰🇷' and _rank_tab == "국장 ETFs") or
                (r['시장'] == '🇺🇸' and _rank_tab == "미장 ETFs")]

            if not _filtered_etfs:
                # V9.1 Item 3: 장외 시간 — AI 전략 시나리오
                import datetime as _dt_p2
                _kst_h = (_dt_p2.datetime.utcnow().hour + 9) % 24
                _is_offhours = not (9 <= _kst_h < 16)
                _offhours_label = "🌙 장 마감 후" if _kst_h >= 16 else "🌅 개장 전"
                # 전략 섹터 TOP3 시나리오 (랭킹 캐시 기반)
                try:
                    _all_etfs_sc = _get_home_etf_top(20)
                    _sc_kr = [r for r in _all_etfs_sc if r['시장'] == '🇰🇷']
                    _sc_us = [r for r in _all_etfs_sc if r['시장'] == '🇺🇸']
                    _sc_pool = _sc_kr if _rank_tab == "국장 ETFs" else _sc_us
                except Exception:
                    _sc_pool = []
                if _sc_pool:
                    st.markdown(
                        "<div style='background:linear-gradient(135deg,#0f172a,#1e1b4b);"
                        "border:1px solid #4f46e5;border-radius:10px;padding:10px 14px;margin-bottom:8px;"
                        "font-size:11px;font-weight:700;color:#818cf8'>"
                        f"{_offhours_label} · 내일 공략 AI 시나리오</div>",
                        unsafe_allow_html=True)
                    for _sci, _scr in enumerate(_sc_pool[:3]):
                        _sc_adx = _scr.get('ADX', 0)
                        _sc_mom = _scr.get('모멘텀(%)', 0)
                        _sc_rsi = _scr.get('RSI', 50)
                        _sc_score = _scr.get('종합점수', 0)
                        _sc_action = "매수 대기" if _sc_rsi < 55 else "모멘텀 추종" if _sc_adx >= 25 else "관망"
                        _sc_ac = "#16a34a" if _sc_action == "매수 대기" else "#f59e0b" if _sc_action == "모멘텀 추종" else "#64748b"
                        st.markdown(f"""
<div style='background:#0d1117;border-left:3px solid {_sc_ac};border-radius:6px;
padding:8px 12px;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center'>
  <div>
    <span style='font-weight:700;font-size:12px;color:#f0f4ff'>{_sci+1}. {_scr["ETF명"]}</span>
    <span style='color:#64748b;font-size:10px;margin-left:6px'>점수 {_sc_score}</span>
  </div>
  <div style='text-align:right'>
    <div style='font-size:11px;color:{_sc_ac};font-weight:700'>{_sc_action}</div>
    <div style='font-size:10px;color:#64748b'>RSI {_sc_rsi} · ADX {_sc_adx}</div>
  </div>
</div>""", unsafe_allow_html=True)
                else:
                    st.info("점수 60 이상 ETF 없음 (장 외 시간이거나 데이터 로딩 중)")
            else:
                for _ri, _re in enumerate(_filtered_etfs[:5]):
                    _is_top_r = (_ri == 0)
                    _rc = "#ffd166" if _is_top_r else "#3b82f6" if _re.get('ADX', 0) >= 30 else "#374151"
                    _macd_r = _re.get('MACD', '')
                    _border_r = "#ffd166" if _is_top_r else ("#d4a017" if "골든" in _macd_r else "#1e3a5f")
                    _bg_r = "#1a1400" if _is_top_r else "#0d1117"
                    _score_r = _re.get('종합점수', 0)
                    _mom_r = _re.get('모멘텀(%)', 0)
                    _adx_r = _re.get('ADX', 0)
                    _rsi_r = _re.get('RSI', 0)
                    _chg_r = _re.get('등락(%)', 0)
                    _chg_c_r = "#16a34a" if _chg_r > 0 else "#ef4444"
                    _cur_r = _re.get('현재가', 0)
                    _is_kr_r = _re['시장'] == '🇰🇷'
                    _price_r = f"{_cur_r:,.0f}원" if _is_kr_r else f"${_cur_r:,.2f}"

                    st.markdown(f"""
<div style='background:{_bg_r};border:1px solid {_border_r};border-radius:8px;padding:10px 12px;margin-bottom:4px'>
  <div style='display:flex;justify-content:space-between;align-items:center'>
    <div style='display:flex;align-items:center;gap:6px'>
      <span style='color:{_rc};font-weight:800;font-size:13px'>{'🥇' if _is_top_r else f'{_ri+1}위'}</span>
      <span style='font-weight:700;font-size:13px'>{_re['ETF명']}</span>
      <span style='color:#64748b;font-size:10px'>({_re['코드']})</span>
    </div>
    <span style='background:#1e293b;color:#fbbf24;font-size:13px;font-weight:800;padding:2px 8px;border-radius:6px'>{_score_r}</span>
  </div>
  <div style='display:flex;gap:10px;margin-top:6px;flex-wrap:wrap'>
    <span style='font-size:11px;color:#64748b'>현재가 <b style='color:#f0f4ff'>{_price_r}</b></span>
    <span style='font-size:11px;color:#64748b'>ADX <b style='color:{"#16a34a" if _adx_r>=25 else "#ef4444"}'>{_adx_r}</b></span>
    <span style='font-size:11px;color:#64748b'>RSI <b style='color:#f0f4ff'>{_rsi_r}</b></span>
    <span style='font-size:11px;color:#64748b'>모멘텀 <b style='color:{_chg_c_r}'>{_mom_r:+.1f}%</b></span>
    <span style='font-size:11px;color:{_chg_c_r}'>{'▲' if _chg_r>0 else '▼'}{abs(_chg_r):.2f}%</span>
  </div>
</div>""", unsafe_allow_html=True)

                    # 1위 ETF: Top Holdings 버튼
                    if _is_top_r:
                        _top_key = f"home_show_holdings_{_re['코드']}"
                        if st.button(f"🔫 Scan Top Holdings — {_re['ETF명']}", key=f"home_holdings_btn_{_re['코드']}", use_container_width=True):
                            st.session_state[_top_key] = not st.session_state.get(_top_key, False)

                        if st.session_state.get(_top_key, False):
                            with st.spinner("구성종목 스캔 중..."):
                                _home_snipe = _scan_etf_holdings(_re['코드'], is_korean=_is_kr_r)
                            if _home_snipe:
                                st.markdown("<div style='font-size:11px;color:#64748b;margin:4px 0 2px'>▶ 구성종목 타점</div>", unsafe_allow_html=True)
                                for _hs in _home_snipe[:5]:
                                    _fmt_hs = lambda p: f"{int(p):,}원" if (_is_kr_r and p >= 100) else f"${p:,.2f}"
                                    st.markdown(
                                        f"<div style='background:#0d1117;border-left:3px solid {_hs['타점색']};"
                                        f"border-radius:4px;padding:5px 10px;margin:2px 0;font-size:11px;"
                                        f"display:flex;justify-content:space-between'>"
                                        f"<span><b>{_hs['종목명']}</b> <span style='color:#64748b'>{_hs['종목코드']}</span></span>"
                                        f"<span style='color:{_hs['타점색']};font-weight:700'>{_hs['타점']}</span>"
                                        f"<span style='color:#64748b'>R:R {_hs['R:R']:.1f}</span>"
                                        f"</div>",
                                        unsafe_allow_html=True
                                    )

        else:  # 관심종목
            _wl_cc2 = get_watchlist_tickers()
            if not _wl_cc2:
                st.info("관심종목을 추가하세요")
            else:
                import yfinance as _yf_wl
                _wl_scored = []
                for _wt, _wn in _wl_cc2:
                    try:
                        # 한국 6자리: .KS(코스피) → 실패 시 .KQ(코스닥) 폴백
                        if _wt.isdigit() and len(_wt) == 6:
                            _wdf = None
                            for _sfx in ('.KS', '.KQ'):
                                _tmp = _yf_wl.Ticker(_wt + _sfx).history(period="5d", interval="1d")
                                if _tmp is not None and len(_tmp) >= 2:
                                    _wdf = _tmp
                                    break
                        else:
                            _wdf = _yf_wl.Ticker(_wt).history(period="5d", interval="1d")
                        if _wdf is None or len(_wdf) < 2:
                            continue
                        _wcl = _wdf['Close']
                        _wchg = float((_wcl.iloc[-1] / _wcl.iloc[-2] - 1) * 100)
                        _wprice = float(_wcl.iloc[-1])
                        # RSI14 간이 계산 (5일치라 근사값)
                        _wd = _wcl.diff()
                        _wg = _wd.clip(lower=0).mean()
                        _wl_ = (-_wd).clip(lower=0).mean()
                        _wrsi = float(100 - 100 / (1 + _wg / (_wl_ + 1e-9)))
                        _wl_scored.append((_wt, _wn, _wchg, _wrsi, _wprice))
                    except Exception:
                        pass
                if not _wl_scored:
                    st.info("관심종목 시세 조회 실패 — 네트워크 또는 종목코드를 확인하세요")
                else:
                    _wl_scored.sort(key=lambda x: x[2], reverse=True)
                    for _wt, _wn, _wchg, _wrsi, _wp in _wl_scored[:6]:
                        _wc = "#16a34a" if _wchg > 0 else "#ef4444"
                        _wr_c = "#ef4444" if _wrsi >= 70 else "#3b82f6" if _wrsi <= 30 else "#64748b"
                        _wis_kr = _wt.isdigit() and len(_wt) == 6
                        _wp_fmt = f"{int(_wp):,}원" if _wis_kr else f"${_wp:,.2f}"
                        st.markdown(
                            f"<div style='background:#0d1117;border-radius:6px;padding:7px 12px;margin-bottom:3px;"
                            f"display:flex;justify-content:space-between;align-items:center'>"
                            f"<div><span style='font-weight:600;font-size:13px'>{_wn}</span> "
                            f"<span style='color:#64748b;font-size:10px'>{_wt}</span></div>"
                            f"<div style='text-align:right'>"
                            f"<span style='color:#94a3b8;font-size:11px'>{_wp_fmt}</span> "
                            f"<span style='color:{_wc};font-weight:700;margin-left:6px'>{_wchg:+.2f}%</span> "
                            f"<span style='color:{_wr_c};font-size:11px;margin-left:4px'>RSI {_wrsi:.0f}</span>"
                            f"</div></div>",
                            unsafe_allow_html=True
                        )

    # ══════════════════════════════════════════════
    # 2행 — PANEL 3(관제) + PANEL 4(차트) : 40% / 60%
    # ══════════════════════════════════════════════
    st.markdown("<div style='margin-top:6px'></div>", unsafe_allow_html=True)
    # ⚡ LIVE SIGNAL STREAM — 계좌 카드에서 분리해 전체폭 독립 컨테이너
    with st.expander("⚡ LIVE SIGNAL STREAM (관심종목 실시간 신호)", expanded=True):
        # Live Signal Stream

        # 신호 피드 조합: 관심종목 신호 + 최근 거래
        _signal_feed = []
        _tickers_cc = get_watchlist_tickers()
        for _t_cc, _n_cc in _tickers_cc[:5]:
            try:
                _df_cc2 = all_data.get(_t_cc, {}).get('df')
                if _df_cc2 is None:
                    # 홈에서 all_data 캐시가 비어있으면 즉석 로드 (시그널 피드 빈칸 방지)
                    _raw_cc = fetch_ohlcv(_t_cc, 80)
                    if _raw_cc is not None and len(_raw_cc) >= 20:
                        _df_cc2 = calc_indicators(_raw_cc)
                        st.session_state.all_data_cache[_t_cc] = {'name': _n_cc, 'df': _df_cc2}
                if _df_cc2 is None or len(_df_cc2) < 2:
                    continue
                _sig_cc = get_signal(_df_cc2)
                _chg_cc = (_df_cc2['종가'].iloc[-1] / _df_cc2['종가'].iloc[-2] - 1) * 100
                _chg_c2 = "#16a34a" if _chg_cc > 0 else "#ef4444"
                for _s, _stype in _sig_cc[:1]:
                    _signal_feed.append((_n_cc, _s, _chg_cc, _chg_c2))
            except Exception:
                pass

        if _signal_feed:
            for _sn, _ss, _sc, _scc in _signal_feed:
                st.markdown(
                    f"<div style='background:#0d1117;border-left:2px solid {_scc};border-radius:4px;"
                    f"padding:5px 10px;margin-bottom:3px;font-size:11px'>"
                    f"<span style='color:#f0f4ff;font-weight:600'>{_sn}</span> "
                    f"<span style='color:#64748b'>{_ss}</span> "
                    f"<span style='color:{_scc};float:right'>{_sc:+.1f}%</span>"
                    f"</div>",
                    unsafe_allow_html=True
                )
        else:
            st.markdown("<div style='color:#374151;font-size:11px;padding:6px'>관심종목 신호 없음</div>", unsafe_allow_html=True)

    _p3, _p4 = st.columns([4, 6])

    # ══════════════════════════════════════════════
    # PANEL 3 — Active Portfolio 관제
    # ══════════════════════════════════════════════
    with _p3:
        st.markdown("<div style='font-size:11px;color:#64748b;font-weight:700;margin-bottom:6px'>ACTIVE PORTFOLIO 관제</div>", unsafe_allow_html=True)

        # ── 전역 자산 낙폭 킬스위치 (Gemini 방어벽 #1) ──
        _p3_cur_total  = st.session_state.get('portfolio_total_today', 0)
        _p3_prev_total = st.session_state.get('portfolio_total_prev', 0)
        if _p3_cur_total > 0 and _p3_prev_total > 0:
            _gd_safe, _gd_msg = check_global_drawdown_killswitch(_p3_cur_total, _p3_prev_total)
            if not _gd_safe:
                st.error(_gd_msg)
                st.session_state['_global_buy_blocked'] = True
            else:
                st.session_state['_global_buy_blocked'] = False

        _acc_p3 = load_account()
        _pos_p3 = _acc_p3.get('positions', [])

        if not _pos_p3:
            st.markdown("""
<div style='background:#0d1117;border:1px solid #1e3a5f;border-radius:10px;padding:20px;text-align:center'>
  <div style='color:#374151;font-size:28px;margin-bottom:8px'>📭</div>
  <div style='color:#64748b;font-size:12px'>보유 포지션 없음</div>
  <div style='color:#374151;font-size:11px;margin-top:4px'>관리 탭에서 페이퍼 트레이딩 실행</div>
</div>""", unsafe_allow_html=True)
        else:
            for _pos_p3i in _pos_p3:
                with st.container(border=True):
                    try:
                        _tk_p3 = _pos_p3i['ticker']
                        _nm_p3 = _pos_p3i.get('name', _tk_p3)
                        _qty_p3 = _pos_p3i.get('qty', 0)
                        _avg_p3 = float(_pos_p3i.get('avg_price', 0))
                        _is_kr_p3 = is_korean_ticker(_tk_p3)

                        # 현재가 조회
                        _cur_p3 = _avg_p3  # fallback
                        if _tk_p3 in all_data:
                            _df_p3_raw = all_data[_tk_p3]['df']
                            _v = _df_p3_raw['종가'].iloc[-1]
                            if _v and not pd.isna(_v):
                                _cur_p3 = float(_v)
                        else:
                            try:
                                import yfinance as _yf_p3
                                _sym_p3 = f"{_tk_p3}.KS" if _is_kr_p3 else _tk_p3
                                _h_p3 = _yf_p3.Ticker(_sym_p3).history(period="5d")
                                if isinstance(_h_p3.columns, pd.MultiIndex):
                                    _h_p3.columns = _h_p3.columns.get_level_values(0)
                                if not _h_p3.empty:
                                    _v3 = _h_p3['Close'].dropna().iloc[-1]
                                    if _v3 and not pd.isna(_v3):
                                        _cur_p3 = float(_v3)
                            except Exception:
                                pass

                        _pnl_pct_p3 = (_cur_p3 / _avg_p3 - 1) * 100 if _avg_p3 > 0 else 0
                        _pnl_abs_p3 = (_cur_p3 - _avg_p3) * _qty_p3
                        _stop_p3    = _avg_p3 * (1 - _STOP_LOSS_PCT)
                        _target_p3  = _avg_p3 * 1.08
                        _t2_p3      = _avg_p3 * 1.15
                        _eval_p3    = _cur_p3 * _qty_p3
                        _sym_p3str  = "원" if _is_kr_p3 else "$"
                        _fmt_p3     = lambda v: f"{int(v):,}{_sym_p3str}" if _is_kr_p3 else f"{_sym_p3str}{v:,.2f}"

                        # 손절/목표 사이 진행률 바 (0%=손절, 100%=1차목표)
                        _range_p3   = _target_p3 - _stop_p3
                        _prog_p3    = max(0, min(100, (_cur_p3 - _stop_p3) / _range_p3 * 100)) if _range_p3 > 0 else 0
                        _stop_breached = _cur_p3 <= _stop_p3          # 손절가 하향 이탈(치명)
                        _stop_warn  = _cur_p3 <= _stop_p3 * 1.03      # 손절 근접(경계)
                        _target_hit = _cur_p3 >= _target_p3
                        # 상태 배지 문구: 이탈 > 근접 > 목표달성 우선순위
                        _status_msg = ("🚨 손절 이탈 — 즉시 매도!" if _stop_breached
                                       else "⚠️ 손절 근접!" if _stop_warn
                                       else "✅ 목표 달성!" if _target_hit else "")
                        # 라이트/다크 모드에 따라 색상 분기
                        _is_light = not st.session_state.get('ui_dark', True)
                        if _is_light:
                            # 라이트: 포레스트 그린 / 크림슨 레드 (형광 대신 차분한 톤)
                            _pnl_color = "#166534" if _pnl_pct_p3 >= 0 else ("#991B1B" if _stop_warn else "#B91C1C")
                        else:
                            # 다크: 형광 그린/레드
                            _pnl_color = "#39ff14" if _pnl_pct_p3 >= 0 else ("#ff003c" if _stop_warn else "#ef4444")
                        if _is_light:
                            _card_border_p3 = "#991B1B" if _stop_warn else ("#166534" if _target_hit else "#CBD5E1")
                        else:
                            _card_border_p3 = "#ff003c" if _stop_warn else ("#39ff14" if _target_hit else "#1e3a5f")

                        # 트레일링 스탑 상태
                        _ts_key = f"trailing_stop_{_tk_p3}"
                        if _ts_key not in st.session_state:
                            st.session_state[_ts_key] = False
                        _ts_active = st.session_state[_ts_key]
                        # 평균가 돌파 시 자동 트레일링 스탑 '최초 1회'만 제안
                        # (사용자가 수동으로 끄면 다시 강제 ON 하지 않음)
                        _ts_sug_key = f"{_ts_key}_suggested"
                        if _pnl_pct_p3 > 0 and not st.session_state.get(_ts_sug_key):
                            st.session_state[_ts_key] = True
                            st.session_state[_ts_sug_key] = True
                            _ts_active = True

                        # 카드 렌더링 — V9.1: 퀵 액션 바 상단 배치
                        _ts_badge = "<span style='background:#7c3aed;color:#fff;font-size:9px;padding:1px 6px;border-radius:10px'>🔒 트레일링스탑</span>" if _ts_active else ""

                        # ── 퀵 액션 바 (카드 위쪽) ──
                        _qa1, _qa2, _qa3 = st.columns(3)
                        with _qa1:
                            if st.button(f"📉 절반 매도", key=f"half_sell_{_tk_p3}", use_container_width=True):
                                _half_qty = max(1, _qty_p3 // 2)
                                _net_sell = calc_slippage(_cur_p3, is_buy=False, is_korean=_is_kr_p3)
                                _acc_p3_act = load_account()
                                _pos_idx = next((i for i, p in enumerate(_acc_p3_act['positions']) if p['ticker'] == _tk_p3), None)
                                if _pos_idx is not None:
                                    _acc_p3_act['positions'][_pos_idx]['qty'] -= _half_qty
                                    if _acc_p3_act['positions'][_pos_idx]['qty'] <= 0:
                                        _acc_p3_act['positions'].pop(_pos_idx)
                                    _acc_p3_act['cash'] += _net_sell * _half_qty
                                    save_account(_acc_p3_act)
                                    log_trade(_tk_p3, _nm_p3, 'SELL', _half_qty, _cur_p3, _net_sell,
                                              _acc_p3_act['cash'], _acc_p3_act['cash'], memo="홈탭 절반매도")
                                    st.success(f"✅ {_half_qty}주 절반 매도 완료")
                                    st.rerun()
                        with _qa2:
                            if st.button(f"🚨 전량 매도", key=f"full_sell_{_tk_p3}", use_container_width=True,
                                         type="primary" if _stop_warn else "secondary"):
                                _net_sell2 = calc_slippage(_cur_p3, is_buy=False, is_korean=_is_kr_p3)
                                _acc_p3_act2 = load_account()
                                _acc_p3_act2['positions'] = [p for p in _acc_p3_act2['positions'] if p['ticker'] != _tk_p3]
                                _acc_p3_act2['cash'] += _net_sell2 * _qty_p3
                                save_account(_acc_p3_act2)
                                log_trade(_tk_p3, _nm_p3, 'SELL', _qty_p3, _cur_p3, _net_sell2,
                                          _acc_p3_act2['cash'], _acc_p3_act2['cash'], memo="홈탭 전량매도")
                                st.success(f"✅ {_qty_p3}주 전량 매도 완료")
                                st.rerun()
                        with _qa3:
                            _ts_label = "🔒 트레일링ON" if _ts_active else "🔓 트레일링OFF"
                            if st.button(_ts_label, key=f"ts_toggle_{_tk_p3}", use_container_width=True):
                                st.session_state[_ts_key] = not _ts_active
                                st.rerun()

                        # ── V9.1 Item 1: 카드 글로우 클래스 ──
                        _glow_class = "card-profit-high" if _pnl_pct_p3 >= 10 else ("card-stop-warn" if _stop_warn else "")
                        st.markdown(f"""<div class='{_glow_class}' style='background:#0d1117;border:2px solid {_card_border_p3};border-radius:12px;padding:14px 16px;margin-bottom:8px'><div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px'><div><div style='font-weight:800;font-size:14px;color:#f0f4ff'>{_nm_p3} {_ts_badge}</div><div style='color:#64748b;font-size:11px;margin-top:2px'>{_tk_p3} · {_qty_p3:,}주 · 평균 {_fmt_p3(_avg_p3)} · 평가 {_fmt_p3(_eval_p3)}</div></div><div style='text-align:right'><div style='font-size:22px;font-weight:900;color:{_pnl_color};line-height:1'>{_pnl_pct_p3:+.2f}%</div><div style='font-size:12px;color:{_pnl_color}'>{"+" if _pnl_abs_p3>=0 else "-"}{_fmt_p3(abs(_pnl_abs_p3))}</div></div></div><div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px'><div style='background:#111827;border-radius:8px;padding:8px;text-align:center'><div style='font-size:10px;color:#64748b'>현재가</div><div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_fmt_p3(_cur_p3)}</div></div><div style='background:#1a0a0a;border-radius:8px;padding:8px;text-align:center;border:1px solid {"#ef4444" if _stop_warn else "#3f1515"}'><div style='font-size:10px;color:#ef4444'>🛑 손절 -7%</div><div style='font-size:14px;font-weight:700;color:#ef4444'>{_fmt_p3(_stop_p3)}</div></div><div style='background:#0a1a0d;border-radius:8px;padding:8px;text-align:center;border:1px solid {"#16a34a" if _target_hit else "#14532d"}'><div style='font-size:10px;color:#16a34a'>🎯 1차 +8%</div><div style='font-size:14px;font-weight:700;color:#16a34a'>{_fmt_p3(_target_p3)}</div></div></div><div style='background:#111827;border-radius:6px;padding:4px 8px;margin-bottom:8px'><div style='display:flex;justify-content:space-between;font-size:9px;color:#64748b;margin-bottom:3px'><span>손절 {_fmt_p3(_stop_p3)}</span><span>현재 {_fmt_p3(_cur_p3)}</span><span>목표 {_fmt_p3(_target_p3)}</span></div><div style='background:#1e293b;border-radius:4px;height:6px;overflow:hidden'><div style='background:{"#ef4444" if _prog_p3<25 else "#f97316" if _prog_p3<60 else "#16a34a"};height:100%;width:{_prog_p3:.0f}%;border-radius:4px;transition:width 0.3s'></div></div></div><div style='display:flex;justify-content:space-between;font-size:11px;color:#64748b'><span>R:R <b style='color:#f0f4ff'>1:{(_target_p3-_avg_p3)/max(_avg_p3-_stop_p3,1):.1f}</b></span><span>2차목표 <b style='color:#22d3ee'>{_fmt_p3(_t2_p3)}</b></span><span style='font-weight:{"800" if _stop_breached else "400"};color:{"#ef4444" if _stop_breached else "#64748b"}'>{_status_msg}</span></div></div>""", unsafe_allow_html=True)


                    except Exception as _ep3:
                        _ename = _pos_p3i.get('name', _pos_p3i.get('ticker', '?'))
                        st.markdown(
                            f"<div style='background:#1a0a0a;border:1px solid #3f1515;border-radius:8px;"
                            f"padding:10px 14px;margin-bottom:6px;font-size:12px'>"
                            f"<b>{_ename}</b> — 현재가 조회 실패 (장외시간 또는 네트워크)<br>"
                            f"<span style='color:#64748b'>평균가 기준: {float(_pos_p3i.get('avg_price',0)):,.0f} · {_pos_p3i.get('qty',0)}주</span>"
                            f"</div>",
                            unsafe_allow_html=True
                        )

    # ══════════════════════════════════════════════
    # PANEL 4 — Performance & Chart
    # ══════════════════════════════════════════════
    with _p4:
        st.markdown("<div style='font-size:11px;color:#64748b;font-weight:700;margin-bottom:6px'>PERFORMANCE & CHART</div>", unsafe_allow_html=True)

        _acc_p4 = load_account()
        _pos_p4 = _acc_p4.get('positions', [])

        if _pos_p4:
            # 첫 번째 포지션의 Z-Score + RSI 오버레이 차트
            _focus = _pos_p4[0]
            _tk_p4 = _focus['ticker']
            _nm_p4 = _focus.get('name', _tk_p4)
            _avg_p4 = float(_focus.get('avg_price', 0))
            try:
                if _tk_p4 in all_data:
                    _df_p4 = all_data[_tk_p4]['df']
                else:
                    import yfinance as _yf_p4
                    _sym_p4 = f"{_tk_p4}.KS" if is_korean_ticker(_tk_p4) else _tk_p4
                    _raw_p4 = _yf_p4.Ticker(_sym_p4).history(period="3mo")
                    if isinstance(_raw_p4.columns, pd.MultiIndex):
                        _raw_p4.columns = _raw_p4.columns.get_level_values(0)
                    _raw_p4 = _raw_p4.rename(columns={'Open':'시가','High':'고가','Low':'저가','Close':'종가','Volume':'거래량'})
                    _df_p4 = calc_indicators(_raw_p4)

                _cl_p4 = _df_p4['종가'].tail(30)
                _idx_p4 = list(range(len(_cl_p4)))
                _mu4 = _cl_p4.rolling(20).mean()
                _sd4 = _cl_p4.rolling(20).std()
                _zs4 = ((_cl_p4 - _mu4) / (_sd4 + 1e-9)).round(2)

                _fig_p4 = go.Figure()
                _fig_p4.add_trace(go.Scatter(
                    x=_idx_p4, y=_cl_p4.values,
                    name='종가', line=dict(color='#3b82f6', width=1.5),
                    hovertemplate='%{y:,.0f}원<extra></extra>'
                ))
                if _avg_p4 > 0:
                    _fig_p4.add_hline(y=_avg_p4, line=dict(color='#fbbf24', dash='dash', width=1),
                                      annotation_text=f"평균 {_avg_p4:,.0f}", annotation_font_size=9,
                                      annotation_font_color='#fbbf24')
                    _fig_p4.add_hline(y=_avg_p4 * 0.93, line=dict(color='#ef4444', dash='dot', width=1),
                                      annotation_text="손절", annotation_font_size=9,
                                      annotation_font_color='#ef4444')
                    _fig_p4.add_hline(y=_avg_p4 * 1.08, line=dict(color='#16a34a', dash='dot', width=1),
                                      annotation_text="목표", annotation_font_size=9,
                                      annotation_font_color='#16a34a')
                _fig_p4.update_layout(
                    height=140, margin=dict(l=0, r=40, t=20, b=0),
                    paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                    showlegend=False, font_color='#64748b',
                    xaxis=dict(visible=False),
                    yaxis=dict(showgrid=False, tickfont=dict(size=9, color='#64748b')),
                    title=dict(text=f"<b>{_nm_p4}</b> 30일", font=dict(size=11, color='#94a3b8'), x=0)
                )
                st.plotly_chart(_fig_p4, use_container_width=True)

                # V9.1 Item 4: 목표/손절 거리 오버레이
                if _avg_p4 > 0:
                    _cur_p4_price = float(_cl_p4.iloc[-1]) if len(_cl_p4) else _avg_p4
                    _stop_p4 = _avg_p4 * (1 - _STOP_LOSS_PCT)
                    _tgt_p4 = _avg_p4 * 1.08
                    _dist_stop_p4 = (_cur_p4_price - _stop_p4) / _cur_p4_price * 100
                    _dist_tgt_p4 = (_tgt_p4 - _cur_p4_price) / _cur_p4_price * 100
                    _breached_p4 = _cur_p4_price <= _stop_p4     # 손절가 하향 이탈
                    _dc_stop_p4 = "#ef4444" if _dist_stop_p4 < 3 else "#f97316" if _dist_stop_p4 < 5 else "#64748b"
                    _stop_txt_p4 = "🚨 이탈!" if _breached_p4 else f"-{_dist_stop_p4:.1f}%"
                    st.markdown(f"""
<div style='display:flex;gap:6px;margin-bottom:6px'>
  <div style='flex:1;background:#0d1117;border-radius:6px;padding:7px;text-align:center;border:1px solid #ef444440'>
    <div style='font-size:10px;color:#ef4444'>{'🛑 손절 이탈' if _breached_p4 else '🛑 손절까지'}</div>
    <div style='font-size:16px;font-weight:800;color:{"#ef4444" if _breached_p4 else _dc_stop_p4}'>{_stop_txt_p4}</div>
  </div>
  <div style='flex:1;background:#0d1117;border-radius:6px;padding:7px;text-align:center;border:1px solid #16a34a40'>
    <div style='font-size:10px;color:#16a34a'>🎯 목표까지</div>
    <div style='font-size:16px;font-weight:800;color:#16a34a'>+{_dist_tgt_p4:.1f}%</div>
  </div>
</div>""", unsafe_allow_html=True)

                # Z-Score 바
                _cur_z4 = float(_zs4.dropna().iloc[-1]) if not _zs4.dropna().empty else 0.0
                _zc4 = "#16a34a" if _cur_z4 < -0.5 else "#ef4444" if _cur_z4 > 1.5 else "#64748b"
                _rsi_p4 = float(_df_p4['RSI'].iloc[-1]) if 'RSI' in _df_p4.columns else 50
                _rsi_c4 = "#ef4444" if _rsi_p4 >= 70 else "#3b82f6" if _rsi_p4 <= 30 else "#64748b"

                st.markdown(f"""
<div style='display:flex;gap:8px;margin-bottom:8px'>
  <div style='flex:1;background:#0d1117;border-radius:6px;padding:7px;text-align:center'>
    <div style='font-size:10px;color:#64748b'>Z-Score</div>
    <div style='font-size:15px;font-weight:700;color:{_zc4}'>{_cur_z4:+.2f}</div>
  </div>
  <div style='flex:1;background:#0d1117;border-radius:6px;padding:7px;text-align:center'>
    <div style='font-size:10px;color:#64748b'>RSI</div>
    <div style='font-size:15px;font-weight:700;color:{_rsi_c4}'>{_rsi_p4:.0f}</div>
  </div>
  <div style='flex:1;background:#0d1117;border-radius:6px;padding:7px;text-align:center'>
    <div style='font-size:10px;color:#64748b'>MDD</div>
    <div style='font-size:15px;font-weight:700;color:#ef4444'>{((_acc_p4.get("trough",_acc_p4["initial"])/_acc_p4["peak"])-1)*100:.1f}%</div>
  </div>
</div>""", unsafe_allow_html=True)

            except Exception:
                st.caption("차트 로드 실패")

        else:
            st.markdown("""
<div style='background:#0d1117;border-radius:8px;padding:16px;text-align:center;color:#374151;font-size:12px'>
포지션 없음 — 전략 탭에서 ETF 랭킹 확인 후 관리 탭에서 페이퍼 트레이딩 실행
</div>""", unsafe_allow_html=True)

        # 최근 거래 Order Book
        st.markdown("<div style='font-size:11px;color:#64748b;font-weight:700;margin-top:4px;margin-bottom:4px'>ACTIVE TRADES & ORDER BOOK</div>", unsafe_allow_html=True)
        _fb_trades_p4 = _load_trade_log_firebase()
        if _fb_trades_p4:
            for _tr4 in reversed(_fb_trades_p4[-4:]):
                _act4 = _tr4.get('매매', '')
                _tc4 = "#16a34a" if _act4 in ('BUY','매수') else "#ef4444"
                st.markdown(
                    f"<div style='background:#0d1117;border-left:2px solid {_tc4};border-radius:4px;"
                    f"padding:4px 8px;margin-bottom:2px;font-size:11px;display:flex;justify-content:space-between'>"
                    f"<span><b style='color:{_tc4}'>{_act4}</b> {_tr4.get('종목명','?')}</span>"
                    f"<span style='color:#64748b'>{_tr4.get('수량',0)}주 @ {_tr4.get('순체결가',0):,.0f}</span>"
                    f"<span style='color:#374151'>{_tr4.get('날짜','')}</span>"
                    f"</div>",
                    unsafe_allow_html=True
                )
        else:
            st.markdown("<div style='color:#374151;font-size:11px;padding:4px'>거래 기록 없음</div>", unsafe_allow_html=True)

    # ── 하단: 가이드 + 매크로 이벤트 (접힘) ──
    st.markdown("<hr style='margin:12px 0;border-color:#1e2a3a'>", unsafe_allow_html=True)
    _bot1, _bot2 = st.columns(2)
    with _bot1:
        with st.expander("📖 대시보드 사용 가이드", expanded=False):
            st.markdown("""
### 🗺️ 탭별 역할 한눈에 보기

| 탭 | 역할 | 언제 쓰나 |
|---|---|---|
| 🏠 **홈** | 시장 현황 + 관심종목 빠른 확인 | 매일 장 시작 전 |
| 🔍 **분석** | 개별 종목 차트 + Gemini AI 분석 | 종목 검토 시 |
| 📡 **스캐너** | 오늘의 매수 후보 자동 발굴 | **장 마감 후** |
| 🔄 **전략** | ETF 로테이션 랭킹 + 매도 신호 | 주 1~2회 |
| ⚙️ **관리** | 관심종목 추가/삭제 + 페이퍼 트레이딩 | 필요할 때 |

---

### 📅 추천 일일 루틴

**🌅 장 시작 전 (08:50~09:00)**
1. **홈 탭** → 코스피/코스닥 지수, 관심종목 등락 확인
2. 매크로 이벤트(FOMC 등) 블랙아웃 여부 확인
3. 09:00~10:30은 **진입 금지 구간** — 차트만 모니터링

**☀️ 장 중 (10:30~15:20)**
1. **분석 탭** → 관심종목 차트 확인, Gemini AI 분석
2. 조건 맞으면 **관리 탭 → 페이퍼 트레이딩**으로 가상 매수

**🌆 장 마감 후 (16:00~)**
1. **📡 스캐너 탭** → 스캔 실행 → 내일 매수 후보 발굴
2. 🏆 A-Grade(90점↑) 종목 우선 확인
3. **분석 탭**에서 후보 종목 차트 + AI 분석
4. 마음에 들면 **사이드바**에서 관심종목 추가

---

### 📡 스캐너 점수 읽는 법

| 등급 | 점수 | 의미 |
|---|---|---|
| 🏆 **A-Grade 주도주** | 90점↑ | 변동성·모멘텀·수급 모두 최상 → 우선 매수 타겟 |
| 🎯 **Target_Locked** | 70~89점 | 핵심 조건 충족 → 분석 후 진입 검토 |
| ❌ **Filtered** | 70점 미만 | 조건 미달 → 제외 |

> **⚠️ 주의**: 스캐너는 "후보 발굴" 도구입니다. 나온 종목은 반드시 **분석 탭에서 차트를 직접 확인**하고 진입하세요.

---

### 🔄 ETF 로테이션 전략 사용법
1. **전략 탭** → 국장/미장 선택 → 1위 ETF 확인
2. 보유 ETF + 매수가 입력 → HOLD/SWITCH 신호 확인
3. **매도 신호** 종류:
   - 🔴 ADX < 25 → 추세 소멸 → 전량 매도 검토
   - 🟠 RSI ≥ 78 → 과매수 → 부분 익절
   - 🟡 MACD 데드크로스 → 다음날 재확인
   - ⚫ 손절 -7% 도달 → 즉시 매도
4. **스위칭 규칙**: 보유 ETF가 4위 이하로 밀리면 1위 ETF로 교체
   - 단, 1위가 3거래일 연속 유지 중인 ETF로만 이동 (잦은 스위칭 금지)
""")

    with _bot2:
        with st.expander("🗓️ 매크로 이벤트 관리", expanded=False):
            _DEFAULT_MACRO_EVENTS = [
                {"date": "2026-06-18", "name": "🇺🇸 FOMC"},
                {"date": "2026-07-03", "name": "🇺🇸 NFP"},
                {"date": "2026-07-15", "name": "🇺🇸 CPI"},
                {"date": "2026-07-17", "name": "🇰🇷 금통위"},
                {"date": "2026-07-30", "name": "🇺🇸 FOMC"},
                {"date": "2026-08-07", "name": "🇺🇸 NFP"},
                {"date": "2026-08-12", "name": "🇺🇸 CPI"},
                {"date": "2026-08-28", "name": "🇰🇷 금통위"},
                {"date": "2026-09-04", "name": "🇺🇸 NFP"},
                {"date": "2026-09-11", "name": "🇺🇸 CPI"},
                {"date": "2026-09-17", "name": "🇺🇸 FOMC"},
                {"date": "2026-10-02", "name": "🇺🇸 NFP"},
                {"date": "2026-10-15", "name": "🇺🇸 CPI"},
                {"date": "2026-10-16", "name": "🇰🇷 금통위"},
                {"date": "2026-10-29", "name": "🇺🇸 FOMC"},
                {"date": "2026-11-06", "name": "🇺🇸 NFP"},
                {"date": "2026-11-13", "name": "🇺🇸 CPI"},
                {"date": "2026-11-27", "name": "🇰🇷 금통위"},
                {"date": "2026-12-04", "name": "🇺🇸 NFP"},
                {"date": "2026-12-10", "name": "🇺🇸 FOMC"},
                {"date": "2026-12-11", "name": "🇺🇸 CPI"},
            ]
            if 'macro_events' not in st.session_state:
                st.session_state.macro_events = _DEFAULT_MACRO_EVENTS.copy()
            from datetime import datetime as _dtt2
            _now_dt = _dtt2.now()
            _today_str2 = _now_dt.strftime("%Y-%m-%d")
            with st.form("macro_add_form", clear_on_submit=True):
                _fa1, _fa2, _fa3 = st.columns([2, 3, 1])
                _ev_date = _fa1.date_input("날짜")
                _ev_name = _fa2.text_input("이벤트명", placeholder="예: FOMC, CPI")
                _fa3.markdown("<div style='padding-top:28px'>", unsafe_allow_html=True)
                if st.form_submit_button("➕", use_container_width=True) and _ev_name:
                    _new_ev = {"date": str(_ev_date), "name": _ev_name.strip()}
                    _dup = [(e['date'], e['name']) for e in st.session_state.macro_events]
                    if (str(_ev_date), _ev_name.strip()) not in _dup:
                        st.session_state.macro_events.append(_new_ev)
                        st.rerun()
            _mc1b, _mc2b = st.columns([3, 1])
            if _mc2b.button("🔄 초기화", key="reset_macro_b", use_container_width=True):
                _existing_pairs2 = [(e['date'], e['name']) for e in st.session_state.macro_events]
                for _de in _DEFAULT_MACRO_EVENTS:
                    if _de['date'] >= _today_str2 and (_de['date'], _de['name']) not in _existing_pairs2:
                        st.session_state.macro_events.append(_de)
                st.rerun()
            _future_evs2 = sorted(
                [e for e in st.session_state.macro_events if e['date'] >= _today_str2],
                key=lambda x: x['date']
            )[:8]
            _ev_type_color2 = {"FOMC": "#ef4444", "CPI": "#f97316", "NFP": "#eab308", "금통위": "#3b82f6"}
            for _ev2 in _future_evs2:
                try:
                    _ev_dt2 = _dtt2.strptime(_ev2['date'], "%Y-%m-%d")
                    _diff_h2 = (_ev_dt2 - _now_dt).total_seconds() / 3600
                    _blackout2 = abs(_diff_h2) <= 48
                    _day_str2 = _ev2['date'][5:]
                except Exception:
                    _blackout2 = False; _day_str2 = _ev2['date'][5:]
                _tc2 = "#64748b"
                for _kw2, _c2 in _ev_type_color2.items():
                    if _kw2 in _ev2['name']:
                        _tc2 = _c2; break
                _bb2 = ("<span style='background:#ef4444;color:#fff;font-size:10px;font-weight:800;"
                        "padding:1px 7px;border-radius:8px;margin-left:6px'>🚨 블랙아웃</span>") if _blackout2 else ""
                _row_bg = "background:rgba(239,68,68,0.10);" if _blackout2 else ""
                st.markdown(
                    f"<div style='font-size:11px;padding:4px 6px;border-bottom:1px solid #1e2a3a;{_row_bg}border-radius:6px'>"
                    f"<span style='color:#64748b;font-family:monospace'>{_day_str2}</span> "
                    f"<span style='color:{_tc2};font-weight:{'800' if _blackout2 else '400'}'>{_ev2['name']}</span>"
                    f"{_bb2}"
                    f"</div>",
                    unsafe_allow_html=True
                )


with tab_b:
    st.markdown("### 🔍 분석")
    # ── 진입 금지 / 매크로 블랙아웃 대형 배너 ──
    _v891_b = run_v891_system_check()
    from datetime import datetime as _dt_tb
    _kh_b = (_dt_tb.utcnow().hour + 9) % 24
    _km_b = _dt_tb.utcnow().minute
    _time_block_b = (9 <= _kh_b < 10) or (_kh_b == 10 and _km_b <= 30)
    if not _v891_b['can_enter'] or _time_block_b:
        _ban_msg  = _v891_b['alerts'][0] if not _v891_b['can_enter'] else "09:00~10:30 변동성 과다 구간"
        _ban_title = "현재 매매 불가: " + ("FOMC 대기 모드" if _v891_b.get('blackout') else "진입 금지 구간")
        st.markdown(f"""
<div style='background:linear-gradient(135deg,#1a0000,#2d0a0a);border:2px solid #ef4444;
border-radius:16px;padding:24px 28px;margin-bottom:16px;text-align:center'>
  <div style='font-size:40px;margin-bottom:8px'>🚫</div>
  <div style='font-size:22px;font-weight:900;color:#ef4444;margin-bottom:8px'>{_ban_title}</div>
  <div style='font-size:14px;color:#fca5a5;margin-bottom:6px'>{_ban_msg}</div>
  <div style='font-size:12px;color:#7f1d1d;margin-top:8px;border-top:1px solid #7f1d1d30;padding-top:8px'>
    차트 분석 · 타점 계산은 가능 — 실제 주문은 금지 구간 해제 후 실행하세요
  </div>
</div>""", unsafe_allow_html=True)
        # ── 시장 레짐 + 해제 카운트다운 ──
        from datetime import datetime as _dt_reg, timedelta as _td_reg
        _now_utc = _dt_reg.utcnow()
        _now_kst = _now_utc + _td_reg(hours=9)
        _kh_now  = _now_kst.hour
        _km_now  = _now_kst.minute
        if _v891_b.get('blackout'):
            _regime_label = "FOMC 블랙아웃 모드 (매파적 리스크)"
            _regime_icon  = "🦅"
            _regime_color = "#f97316"
        elif _time_block_b:
            _regime_label = "장 초반 변동성 구간 (관망 필수)"
            _regime_icon  = "⏰"
            _regime_color = "#fbbf24"
        else:
            _regime_label = "일반 진입 금지 (시스템 알림)"
            _regime_icon  = "🔒"
            _regime_color = "#ef4444"
        # 다음 09:00 KST까지 남은 시간
        _next_open = _now_kst.replace(hour=9, minute=0, second=0, microsecond=0)
        if _now_kst >= _next_open:
            _next_open += _td_reg(days=1)
        _remaining = _next_open - _now_kst
        _rem_h  = int(_remaining.total_seconds() // 3600)
        _rem_m  = int((_remaining.total_seconds() % 3600) // 60)
        st.markdown(
            f"<div style='background:#0d1117;border:1px solid {_regime_color}40;border-radius:10px;"
            f"padding:10px 16px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center'>"
            f"<div><span style='font-size:16px'>{_regime_icon}</span>"
            f"<span style='color:{_regime_color};font-weight:700;font-size:13px;margin-left:8px'>금일 시장 레짐: {_regime_label}</span></div>"
            f"<div style='text-align:right'>"
            f"<div style='font-size:10px;color:#64748b'>다음 진입 가능 해제까지</div>"
            f"<div style='font-size:16px;font-weight:900;color:#fbbf24;font-family:monospace'>{_rem_h:02d}:{_rem_m:02d}</div>"
            f"<div style='font-size:10px;color:#64748b'>내일 09:00 KST 해제 예정</div>"
            f"</div></div>",
            unsafe_allow_html=True
        )
    # ── 빠른 결론 헤드라인 (탭 선택 전) ──
    _b_tickers = get_watchlist_tickers()
    # quick select 전에 all_data 미수록 종목 즉시 로드
    _b_missing_pre = [(_bt, _bn) for _bt, _bn in _b_tickers if _bt not in all_data]
    if _b_missing_pre:
        for _bt, _bn in _b_missing_pre:
            _bdf = fetch_ohlcv(_bt, 80)
            if _bdf is not None and len(_bdf) >= 20:
                # M1: 루프 안에서 즉시 캐시 반영 — 부분 실패 시 이전 성공분 보존
                st.session_state.all_data_cache[_bt] = {'name': _bn, 'df': calc_indicators(_bdf)}
    if _b_tickers:
        _b_quick_sel = st.selectbox(
            "▶ 분석 종목 선택 (결론 우선 표시)",
            [f"{n} ({t})" for t, n in _b_tickers if t in all_data],
            key="b_quick_sel"
        )
        if _b_quick_sel:
            _bq_tk = _b_quick_sel.split('(')[-1].replace(')','').strip()
            if not is_korean_ticker(_bq_tk):
                _bq_tk = _b_quick_sel.split(' ')[0].strip()
            if _bq_tk in all_data:
                try:
                    _bq_df = all_data[_bq_tk]['df']
                    _bq_ep = calc_entry_point(_bq_df, st.session_state.get('analysis_preset','bounce'))
                    _bq_sigs = get_signal(_bq_df)
                    _bq_buy  = sum(1 for _, t in _bq_sigs if t == 'buy')
                    _bq_v891 = run_v891_system_check()
                    if not _bq_v891['can_enter']:
                        _bq_vd = "🚫 진입 차단"; _bq_vc = "#f43f5e"; _bq_vb = "rgba(244,63,94,0.12)"
                    elif _bq_ep['rr'] < 2.0:
                        _bq_vd = "❌ 진입 불가"; _bq_vc = "#f43f5e"; _bq_vb = "rgba(244,63,94,0.10)"
                    elif _bq_buy >= 2:
                        _bq_vd = "✅ 매수 권장"; _bq_vc = "#34d399"; _bq_vb = "rgba(52,211,153,0.12)"
                    else:
                        _bq_vd = "⚠️ 관망"; _bq_vc = "#fbbf24"; _bq_vb = "rgba(251,191,36,0.10)"
                    st.markdown(f"""
<div style='background:{_bq_vb};border:2px solid {_bq_vc}60;border-radius:12px;
padding:12px 20px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center'>
  <div>
    <span style='font-size:20px;font-weight:900;color:{_bq_vc}'>{_bq_vd}</span>
    <span style='font-size:11px;color:#64748b;margin-left:12px'>
      진입 {_bq_ep["entry"]:,.0f} | 손절 {_bq_ep["stoploss"]:,.0f} | 목표 {_bq_ep["target1"]:,.0f}
    </span>
  </div>
  <span style='font-size:28px;font-weight:900;color:{_bq_vc};font-family:IBM Plex Mono'>R:R {_bq_ep["rr"]}</span>
</div>""", unsafe_allow_html=True)
                except Exception:
                    pass

    _sub_b1, _sub_b2, _sub_b3 = st.tabs(["📈 차트+지표", "🤖 Gemini 분석", "📋 분석 기록"])

    with _sub_b1:
        def _display_name(ticker, name):
            return f"{name} ({ticker})" if is_korean_ticker(ticker) else f"{ticker} ({name})"

        _b1_tickers = get_watchlist_tickers()
        if not _b1_tickers:
            st.info("👈 사이드바에서 관심종목을 추가해주세요.")
        else:
            # all_data에 없는 종목 즉시 로드
            _b1_missing = [(_bt, _bn) for _bt, _bn in _b1_tickers if _bt not in all_data]
            if _b1_missing:
                _load_failed = []
                with st.spinner(f"📡 {len(_b1_missing)}개 종목 데이터 로딩 중..."):
                    for _bt, _bn in _b1_missing:
                        _bdf = fetch_ohlcv(_bt, 80)
                        if _bdf is not None and len(_bdf) >= 20:
                            st.session_state.all_data_cache[_bt] = {'name': _bn, 'df': calc_indicators(_bdf)}
                        else:
                            _load_failed.append(f"{_bn}({_bt})")
                    import time as _time_ad
                    st.session_state.all_data_time = _time_ad.time()
                if _load_failed:
                    _fail_col1, _fail_col2, _fail_col3 = st.columns([3.5, 1, 1])
                    _fail_col1.warning(
                        f"⚠️ 데이터 로드 실패: {', '.join(_load_failed)}\n\n"
                        "상장폐지 또는 잘못된 티커일 수 있습니다. 관심종목에서 제거하거나 재시도하세요."
                    )
                    if _fail_col2.button("🔄 재시도", key="retry_load_fail", use_container_width=True):
                        st.session_state.all_data_cache = {}
                        st.session_state.all_data_time = 0
                        st.rerun()
                    # 실패한 티커를 관심종목에서 일괄 제거
                    _fail_tickers = [f.split('(')[-1].rstrip(')') for f in _load_failed]
                    def _remove_failed():
                        for _ft in _fail_tickers:
                            try:
                                remove_ticker(_ft)
                            except Exception:
                                pass
                    if _fail_col3.button("🗑️ 목록 제거", key="remove_failed_tickers",
                                         use_container_width=True,
                                         help=f"{', '.join(_fail_tickers)} 관심종목에서 제거"):
                        _remove_failed()
                        st.toast(f"🗑️ {', '.join(_fail_tickers)} 제거 완료", icon="✅")
                        st.rerun()

            _b1_opts = [_display_name(t, n) for t, n in _b1_tickers if t in all_data]
            if not _b1_opts:
                st.warning("데이터를 불러올 수 없습니다. 잠시 후 다시 시도해주세요.")
                st.stop()

            # ── 종목 선택 + 프리셋 ──
            _sel_col_b, _pre_col_b = st.columns([2, 1])
            with _sel_col_b:
                selected = st.selectbox("종목 선택", _b1_opts)
            sel_ticker = selected.split('(')[-1].replace(')', '').strip()
            if not is_korean_ticker(sel_ticker):
                sel_ticker = selected.split(' ')[0].strip()
            sel_name = all_data[sel_ticker]['name']
            sel_df   = all_data[sel_ticker]['df']

            with _pre_col_b:
                if 'analysis_preset' not in st.session_state:
                    st.session_state.analysis_preset = 'bounce'
                _pr_map = {"📉 반등": "bounce", "📈 추세": "trend", "🎯 바닥": "bottom"}
                _pr_sel = st.radio("전략", list(_pr_map.keys()), horizontal=True,
                                   index=list(_pr_map.values()).index(st.session_state.analysis_preset),
                                   key="preset_radio_b1")
                if _pr_map[_pr_sel] != st.session_state.analysis_preset:
                    st.session_state.analysis_preset = _pr_map[_pr_sel]
                    st.rerun()

            # ── 핵심 지표 계산 ──
            l = sel_df.iloc[-1]; p = sel_df.iloc[-2]
            chg = (l['종가'] / p['종가'] - 1) * 100
            bb_r = l['BB_upper'] - l['BB_lower']
            bb_p = round((l['종가'] - l['BB_lower']) / bb_r * 100, 1) if bb_r > 0 else 50
            _sigs     = get_signal(sel_df)
            _buy_cnt  = sum(1 for _, t in _sigs if t == 'buy')
            _sell_cnt = sum(1 for _, t in _sigs if t == 'sell')
            _v891     = run_v891_system_check()

            _kis_price = None
            if kis_available() and is_korean_ticker(sel_ticker):
                _kis_price = kis_get_price(sel_ticker)
            _display_price = _kis_price['현재가'] if _kis_price else l['종가']
            _kis_badge = " <span style='font-size:10px;color:#34d399'>● 실시간</span>" if _kis_price else " <span style='font-size:10px;color:#64748b'>● 지연</span>"

            # ── 타점 계산 ──
            try:
                _ep = calc_entry_point(sel_df, st.session_state.analysis_preset)
                entry_price   = _ep['entry']
                stop_price    = _ep['stoploss']
                target1_price = _ep['target1']
                target2_price = _ep['target2']
            except Exception as _ep_err:
                st.error(f"타점 계산 오류: {_ep_err}")
                entry_price = stop_price = target1_price = target2_price = 0
                _ep = {'rr': 0, 'gap_pct': 0, 'reason': '계산 실패', 'cur': l['종가'],
                       'entry': 0, 'stoploss': 0, 'target1': 0, 'target2': 0}

            # ══════════════════════════════════════════
            # 1. AI VERDICT CARD
            # ══════════════════════════════════════════
            if not _v891['can_enter']:
                _vd_icon = "🔴"; _vd_color = "#f43f5e"
                _vd_bg = "rgba(244,63,94,0.12)"; _vd_border = "#f43f5e80"
                _vd_label = "🚫 진입 차단"
                _vd_lines = [
                    _v891['alerts'][0] if _v891['alerts'] else "시스템 차단 상태입니다.",
                    "매크로/시간 필터에 의해 진입이 제한됩니다.",
                    "차트 분석 및 대기 모드를 유지하세요."
                ]
            elif _ep['rr'] < 2.0:
                _vd_icon = "🔴"; _vd_color = "#f43f5e"
                _vd_bg = "rgba(244,63,94,0.10)"; _vd_border = "#f43f5e80"
                _vd_label = "❌ 진입 불가"
                _vd_lines = [
                    f"R:R {_ep['rr']} — 최소 기준 2.0 미달로 기각합니다.",
                    "손절 대비 수익 기대값이 불충분한 구간입니다.",
                    "다음 타점을 기다리거나 전략 프리셋을 변경하세요."
                ]
            elif _buy_cnt >= 2 and _ep['rr'] >= 2.0:
                _vd_icon = "🟢"; _vd_color = "#34d399"
                _vd_bg = "rgba(52,211,153,0.12)"; _vd_border = "#34d39980"
                _vd_label = "✅ 매수 권장"
                _vd_lines = [
                    f"퀀트 신호 {_buy_cnt}개 동시 발현, 기술적 조건 충족.",
                    f"눌림목 달성 후 반등 흐름 확인 (R:R {_ep['rr']}).",
                    "손실 소멸가 + 익절가 안전 구간 — 진입 검토하세요."
                ]
            else:
                _vd_icon = "🟡"; _vd_color = "#fbbf24"
                _vd_bg = "rgba(251,191,36,0.10)"; _vd_border = "#fbbf2480"
                _vd_label = "⚠️ 관망"
                _vd_lines = [
                    f"매수 신호 {_buy_cnt}개 — 기준 2개 미달, 확신도 부족.",
                    "현재 가격대는 추가 확인이 필요한 구간입니다.",
                    "신호 강화 또는 지지선 근접 시 재진입 검토하세요."
                ]

            # ── 분석 기록 저장 (사용자가 명시적으로 누를 때만 — 자동 남발 방지) ──
            if st.button("💾 이 분석 기록에 저장", key=f"save_log_{sel_ticker}", use_container_width=True):
                save_analysis_log(
                    sel_ticker, sel_name, _vd_label, _ep['rr'],
                    _ep['entry'], _ep['stoploss'], _ep['target1'], _ep['target2'],
                    preset=st.session_state.analysis_preset, score=_buy_cnt, source="분석탭"
                )
                st.toast(f"✅ {sel_name} 분석 기록 저장됨", icon="💾")

            _vd_check = "✅" if _vd_icon == "🟢" else "⚠️" if _vd_icon == "🟡" else "❌"
            st.markdown(f"""
<div style='background:{_vd_bg};border:2px solid {_vd_border};border-radius:16px;
padding:20px 24px;margin-bottom:14px;display:flex;align-items:center;gap:20px'>
  <div style='font-size:56px;line-height:1'>{_vd_icon}</div>
  <div style='flex:1'>
    <div style='font-size:24px;font-weight:900;color:{_vd_color};margin-bottom:8px'>
      VERDICT: {_vd_label}
    </div>
    {''.join(f"<div style='font-size:12px;color:#94a3b8;margin-bottom:2px'>{_vd_check} {ln}</div>" for ln in _vd_lines)}
  </div>
  <div style='text-align:right;min-width:90px'>
    <div style='font-size:10px;color:#64748b'>R:R Ratio</div>
    <div style='font-size:36px;font-weight:900;color:{_vd_color};font-family:IBM Plex Mono;line-height:1.1'>{_ep["rr"]}</div>
    <div style='font-size:10px;color:#64748b;margin-top:4px'>{sel_name[:12]}</div>
    <div style='font-size:10px;color:#64748b'>신호 {_buy_cnt}매수/{_sell_cnt}매도</div>
  </div>
</div>""", unsafe_allow_html=True)

            # ══════════════════════════════════════════
            # 2. CHECKLIST CARD — 대형 스테이터스 배지
            # ══════════════════════════════════════════
            _rr_ok   = _ep['rr'] >= 2.0
            _sig_ok  = _buy_cnt >= 2
            _sys_ok  = _v891['can_enter']
            _vol_ok  = l.get('거래량_비율', 100) >= 120
            _rsi_ok  = 30 <= l['RSI'] <= 65
            _ma_ok   = l['종가'] > l.get('MA20', l['종가'])

            def _ck_badge(label, ok, detail=""):
                c  = "#16a34a" if ok else "#dc2626"
                bg = "rgba(22,163,74,0.12)" if ok else "rgba(220,38,38,0.12)"
                bd = "#16a34a50" if ok else "#dc262650"
                ic = "✅" if ok else "❌"
                glow = f"box-shadow:0 0 10px 2px {'#16a34a' if ok else '#dc2626'}50;" if ok else ""
                return (
                    f"<div style='background:{bg};border:1px solid {bd};border-radius:10px;"
                    f"padding:10px;text-align:center;{glow}'>"
                    f"<div style='font-size:20px'>{ic}</div>"
                    f"<div style='font-size:11px;font-weight:700;color:{c};margin-top:4px'>{label}</div>"
                    f"<div style='font-size:10px;color:#64748b;margin-top:2px'>{detail}</div>"
                    f"</div>"
                )

            st.markdown(f"""
<div style='background:#0d1117;border:1px solid #1e293b;border-radius:12px;padding:14px 16px;margin-bottom:14px'>
  <div style='font-size:11px;color:#64748b;font-weight:700;margin-bottom:10px'>
    CHECKLIST CARD — {sel_name} ({sel_ticker})
    <span style='float:right;color:#64748b'>현재가 <b style='color:#f0f4ff'>{format_price(_display_price, sel_ticker)}</b>{_kis_badge}</span>
  </div>
  <div style='display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:10px'>
    {_ck_badge("R:R 2.0+", _rr_ok, str(_ep["rr"]))}
    {_ck_badge("매수신호 2+", _sig_ok, f"{_buy_cnt}개")}
    {_ck_badge("시스템 OK", _sys_ok, "매크로")}
    {_ck_badge("거래량 폭발", _vol_ok, f"{l.get('거래량_비율',100):.0f}%")}
    {_ck_badge("RSI 30-65", _rsi_ok, f"{l['RSI']:.0f}")}
    {_ck_badge("MA20 위", _ma_ok, f"{l.get('MA20',0):,.0f}")}
  </div>
  <div style='display:grid;grid-template-columns:repeat(4,1fr);gap:8px'>
    <div style='background:rgba(251,191,36,0.1);border:1px solid rgba(251,191,36,0.3);border-radius:10px;padding:10px;text-align:center'>
      <div style='font-size:10px;color:#64748b'>🎯 진입</div>
      <div style='font-size:17px;font-weight:800;color:#fbbf24'>{_ep["entry"]:,.0f}</div>
      <div style='font-size:10px;color:#64748b'>{_ep["gap_pct"]:+.1f}% 대기</div>
    </div>
    <div style='background:rgba(244,63,94,0.1);border:1px solid rgba(244,63,94,0.3);border-radius:10px;padding:10px;text-align:center'>
      <div style='font-size:10px;color:#64748b'>🛑 손절가</div>
      <div style='font-size:17px;font-weight:800;color:#f43f5e'>{_ep["stoploss"]:,.0f}</div>
      <div style='font-size:10px;color:#64748b'>-7%</div>
    </div>
    <div style='background:rgba(52,211,153,0.1);border:1px solid rgba(52,211,153,0.3);border-radius:10px;padding:10px;text-align:center'>
      <div style='font-size:10px;color:#64748b'>🎯 익절 1차</div>
      <div style='font-size:17px;font-weight:800;color:#34d399'>{_ep["target1"]:,.0f}</div>
      <div style='font-size:10px;color:#64748b'>+8%</div>
    </div>
    <div style='background:rgba(167,139,250,0.1);border:1px solid rgba(167,139,250,0.3);border-radius:10px;padding:10px;text-align:center'>
      <div style='font-size:10px;color:#64748b'>✨ 익절 2차</div>
      <div style='font-size:17px;font-weight:800;color:#a78bfa'>{_ep["target2"]:,.0f}</div>
      <div style='font-size:10px;color:#64748b'>+15%</div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)

            # ══════════════════════════════════════════
            # 3. MULTI-PANE CHART + TOGGLE + VALUATION BAND
            # ══════════════════════════════════════════
            st.markdown("<div style='margin-top:14px;font-size:11px;color:#64748b;font-weight:700;margin-bottom:8px'>MULTI-PANE CHART</div>", unsafe_allow_html=True)

            _mp_tc1, _mp_tc2, _mp_tc3, _mp_tc4 = st.columns(4)
            _mp_rsi  = _mp_tc1.toggle("RSI",    value=True,  key="mp_rsi")
            _mp_vol  = _mp_tc2.toggle("Volume", value=True,  key="mp_vol")
            _mp_macd = _mp_tc3.toggle("MACD",   value=False, key="mp_macd")
            _mp_band = _mp_tc4.toggle("밸류에이션 Band", value=True, key="mp_band")

            _mp_nrows = 1 + int(_mp_rsi) + int(_mp_vol) + int(_mp_macd)
            _mp_hts   = [0.55] + [0.15] * (_mp_nrows - 1)
            _ht_s = sum(_mp_hts); _mp_hts = [h / _ht_s for h in _mp_hts]

            from plotly.subplots import make_subplots as _ms_b
            _mp_fig = _ms_b(rows=_mp_nrows, cols=1, shared_xaxes=True,
                            row_heights=_mp_hts, vertical_spacing=0.02)

            _mpdf = sel_df.tail(60).copy()
            _x_mp = list(range(len(_mpdf)))
            _cl_mp = _mpdf['종가']
            _op_mp = _mpdf.get('시가', _cl_mp)
            _hi_mp = _mpdf.get('고가', _cl_mp)
            _lo_mp = _mpdf.get('저가', _cl_mp)

            # 밸류에이션 Band
            if _mp_band and 'BB_upper' in _mpdf.columns and 'RSI' in _mpdf.columns:
                _bb_lo_mp = _mpdf['BB_lower']
                _bb_hi_mp = _mpdf['BB_upper']
                _rsi_mp   = _mpdf['RSI']
                for _xi in range(len(_mpdf)):
                    _bb_rng = float(_bb_hi_mp.iloc[_xi] - _bb_lo_mp.iloc[_xi])
                    _bp_v = (float(_cl_mp.iloc[_xi]) - float(_bb_lo_mp.iloc[_xi])) / (_bb_rng + 1e-9) * 100
                    _rv = float(_rsi_mp.iloc[_xi])
                    if _rv < 40 or _bp_v < 25:
                        _mp_fig.add_vrect(x0=_xi - 0.5, x1=_xi + 0.5,
                                          fillcolor="rgba(52,211,153,0.08)", line_width=0, row=1, col=1)
                    elif _rv > 65 or _bp_v > 75:
                        _mp_fig.add_vrect(x0=_xi - 0.5, x1=_xi + 0.5,
                                          fillcolor="rgba(244,63,94,0.08)", line_width=0, row=1, col=1)

            # 캔들스틱
            _mp_fig.add_trace(go.Candlestick(
                x=_x_mp, open=_op_mp, high=_hi_mp, low=_lo_mp, close=_cl_mp,
                increasing_line_color='#ef4444', decreasing_line_color='#3b82f6',
                name='가격', showlegend=False
            ), row=1, col=1)

            # MA선
            for _ma_col, _ma_c in [('MA5', '#fbbf24'), ('MA20', '#34d399'), ('MA60', '#a78bfa')]:
                if _ma_col in _mpdf.columns:
                    _mp_fig.add_trace(go.Scatter(
                        x=_x_mp, y=_mpdf[_ma_col], name=_ma_col,
                        line=dict(color=_ma_c, width=1), showlegend=False
                    ), row=1, col=1)

            # 전략 라인
            for _sl_v, _sl_c, _sl_d, _sl_lbl in [
                (entry_price,   '#fbbf24', 'dash',  '진입'),
                (stop_price,    '#f43f5e', 'dot',   '손절'),
                (target1_price, '#34d399', 'solid', '목표1'),
                (target2_price, '#a78bfa', 'dot',   '목표2'),
            ]:
                if _sl_v and _sl_v > 0:
                    _mp_fig.add_hline(y=_sl_v, line=dict(color=_sl_c, dash=_sl_d, width=2),
                                      annotation_text=f"<b>{_sl_lbl} {_sl_v:,.0f}</b>",
                                      annotation_font=dict(color=_sl_c, size=12, family='IBM Plex Mono'),
                                      annotation_position="right", row=1, col=1)

            _mp_ri = 2

            if _mp_rsi and 'RSI' in _mpdf.columns:
                _mp_fig.add_trace(go.Scatter(x=_x_mp, y=_mpdf['RSI'], name='RSI',
                    line=dict(color='#a78bfa', width=1.2), showlegend=False), row=_mp_ri, col=1)
                _mp_fig.add_hline(y=70, line=dict(color='#f43f5e', dash='dot', width=0.8), row=_mp_ri, col=1)
                _mp_fig.add_hline(y=30, line=dict(color='#34d399', dash='dot', width=0.8), row=_mp_ri, col=1)
                _mp_fig.update_yaxes(title_text="RSI", title_font_size=9, tickfont_size=9, row=_mp_ri, col=1)
                _mp_ri += 1

            if _mp_vol and '거래량' in _mpdf.columns:
                _v_clrs = ['#ef4444' if c >= o else '#3b82f6'
                           for c, o in zip(_cl_mp.values, _op_mp.values)]
                _mp_fig.add_trace(go.Bar(x=_x_mp, y=_mpdf['거래량'], name='거래량',
                    marker_color=_v_clrs, showlegend=False), row=_mp_ri, col=1)
                _mp_fig.update_yaxes(title_text="Vol", title_font_size=9, tickfont_size=9, row=_mp_ri, col=1)
                _mp_ri += 1

            if _mp_macd and 'MACD' in _mpdf.columns:
                _mp_fig.add_trace(go.Scatter(x=_x_mp, y=_mpdf['MACD'], name='MACD',
                    line=dict(color='#fbbf24', width=1.2), showlegend=False), row=_mp_ri, col=1)
                if 'Signal' in _mpdf.columns:
                    _mp_fig.add_trace(go.Scatter(x=_x_mp, y=_mpdf['Signal'], name='Signal',
                        line=dict(color='#f43f5e', width=1, dash='dot'), showlegend=False), row=_mp_ri, col=1)
                if 'MACD_hist' in _mpdf.columns:
                    _hist_c = ['#34d399' if v >= 0 else '#f43f5e' for v in _mpdf['MACD_hist']]
                    _mp_fig.add_trace(go.Bar(x=_x_mp, y=_mpdf['MACD_hist'], name='Hist',
                        marker_color=_hist_c, showlegend=False), row=_mp_ri, col=1)
                _mp_fig.update_yaxes(title_text="MACD", title_font_size=9, tickfont_size=9, row=_mp_ri, col=1)

            _mp_fig.update_layout(
                height=500 if _mp_nrows > 1 else 300,
                paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                margin=dict(l=0, r=70, t=8, b=0),
                font=dict(color='#64748b', size=10),
                showlegend=False,
                xaxis_rangeslider_visible=False,
            )
            for _ri_u in range(1, _mp_nrows + 1):
                _mp_fig.update_xaxes(showgrid=False, row=_ri_u, col=1)
                _mp_fig.update_yaxes(showgrid=True, gridcolor='rgba(255,255,255,0.05)',
                                     zeroline=False, row=_ri_u, col=1)

            st.plotly_chart(_mp_fig, use_container_width=True)

            if _mp_band:
                st.markdown("""
<div style='display:flex;gap:20px;font-size:11px;color:#64748b;margin-top:-8px;margin-bottom:10px'>
  <span><span style='color:#34d399'>■</span> 저평가 구간 (RSI&lt;40 또는 BB하단25%)</span>
  <span><span style='color:#64748b'>■</span> 적정 구간</span>
  <span><span style='color:#ef4444'>■</span> 과열 구간 (RSI&gt;65 또는 BB상단75%)</span>
</div>""", unsafe_allow_html=True)

            # ── 수동 조정 ──
            with st.expander("✏️ 수동 조정", expanded=False):
                _unit   = get_currency(sel_ticker)
                _is_kr_m = is_korean_ticker(sel_ticker)
                _step   = 100.0 if _is_kr_m else 0.01     # US는 센트 단위
                # 종목별 key → 종목 전환 시 이전 값이 남지 않고 새 타점이 반영됨
                lc1, lc2, lc3, lc4 = st.columns(4)
                entry_price   = lc1.number_input(f"매수가 ({_unit})",   value=float(entry_price or 0),   step=_step, key=f"madj_entry_{sel_ticker}")
                stop_price    = lc2.number_input(f"손절가 ({_unit})",   value=float(stop_price or 0),    step=_step, key=f"madj_stop_{sel_ticker}")
                target1_price = lc3.number_input(f"1차 목표 ({_unit})", value=float(target1_price or 0), step=_step, key=f"madj_t1_{sel_ticker}")
                target2_price = lc4.number_input(f"2차 목표 ({_unit})", value=float(target2_price or 0), step=_step, key=f"madj_t2_{sel_ticker}")

            # ══════════════════════════════════════════
            # 4. PERFORMANCE PROJECTION CARD
            # ══════════════════════════════════════════
            if entry_price > 0 and stop_price > 0:
                _pp_e = entry_price
                _pp_s = stop_price
                _pp_t1 = target1_price if target1_price > 0 else _pp_e * 1.08
                _pp_t2 = target2_price if target2_price > 0 else _pp_e * 1.15
                _pp_loss = (_pp_s - _pp_e) / _pp_e * 100
                _pp_base = (_pp_t1 - _pp_e) / _pp_e * 100
                _pp_best = (_pp_t2 - _pp_e) / _pp_e * 100
                _pp_rr   = abs(_pp_base / _pp_loss) if _pp_loss != 0 else 0
                _pp_mx   = max(abs(_pp_best), abs(_pp_base), abs(_pp_loss), 1)
                _pp_bw_best = abs(_pp_best) / _pp_mx * 100
                _pp_bw_base = abs(_pp_base) / _pp_mx * 100
                _pp_bw_loss = abs(_pp_loss) / _pp_mx * 100

                st.markdown(f"""
<div style='background:#0d1117;border:1px solid #1e293b;border-radius:12px;padding:16px 20px;margin-top:6px'>
  <div style='font-size:11px;color:#64748b;font-weight:700;margin-bottom:12px'>
    PERFORMANCE PROJECTION
    <span style='float:right;font-size:12px;color:#94a3b8'>Risk/Reward
      <b style='color:#fbbf24;font-size:18px;margin-left:6px'>{_pp_rr:.2f}</b>
      &nbsp;<span style='font-size:11px;color:#64748b'>(승률 기준 63.9%)</span>
    </span>
  </div>
  <div style='display:flex;flex-direction:column;gap:10px'>
    <div style='display:flex;align-items:center;gap:10px'>
      <span style='color:#a78bfa;font-size:11px;font-weight:700;min-width:40px'>Best</span>
      <div style='flex:1;background:#1e293b;border-radius:4px;height:16px'>
        <div style='width:{_pp_bw_best:.0f}%;background:linear-gradient(90deg,#7c3aed,#a78bfa);height:100%;border-radius:4px'></div>
      </div>
      <span style='color:#a78bfa;font-size:14px;font-weight:800;min-width:50px;text-align:right'>{_pp_best:+.1f}%</span>
    </div>
    <div style='display:flex;align-items:center;gap:10px'>
      <span style='color:#34d399;font-size:11px;font-weight:700;min-width:40px'>Base</span>
      <div style='flex:1;background:#1e293b;border-radius:4px;height:16px'>
        <div style='width:{_pp_bw_base:.0f}%;background:linear-gradient(90deg,#16a34a,#34d399);height:100%;border-radius:4px'></div>
      </div>
      <span style='color:#34d399;font-size:14px;font-weight:800;min-width:50px;text-align:right'>{_pp_base:+.1f}%</span>
    </div>
    <div style='display:flex;align-items:center;gap:10px'>
      <span style='color:#f43f5e;font-size:11px;font-weight:700;min-width:40px'>Worst</span>
      <div style='flex:1;background:#1e293b;border-radius:4px;height:16px'>
        <div style='width:{_pp_bw_loss:.0f}%;background:linear-gradient(90deg,#991b1b,#f43f5e);height:100%;border-radius:4px'></div>
      </div>
      <span style='color:#f43f5e;font-size:14px;font-weight:800;min-width:50px;text-align:right'>{_pp_loss:+.1f}%</span>
    </div>
  </div>
  <div style='margin-top:12px;display:flex;gap:16px;font-size:11px;color:#64748b;
  border-top:1px solid #1e293b;padding-top:10px;flex-wrap:wrap'>
    <span>진입 <b style='color:#fbbf24'>{_pp_e:,.0f}</b></span>
    <span>손절 <b style='color:#f43f5e'>{_pp_s:,.0f}</b></span>
    <span>1차목표 <b style='color:#34d399'>{_pp_t1:,.0f}</b></span>
    <span>2차목표 <b style='color:#a78bfa'>{_pp_t2:,.0f}</b></span>
  </div>
</div>""", unsafe_allow_html=True)

            # 이평선 현황 — 컬러 바 형태
            st.markdown("<div style='margin-top:12px'></div>", unsafe_allow_html=True)
            with st.expander("📐 이평선 현황 — 현재가 대비 거리", expanded=True):
                _ma_items = [('MA5','5일선','#fbbf24'),('MA20','20일선','#34d399'),
                             ('MA60','60일선','#a78bfa'),('MA120','120일선','#f472b6')]
                _ma_html = "<div style='display:flex;flex-direction:column;gap:8px;padding:4px 0'>"
                for _mak, _mal, _mac in _ma_items:
                    _mav = float(l.get(_mak, 0))
                    if _mav <= 0:
                        continue
                    _diff = (l['종가'] / _mav - 1) * 100
                    _abs  = abs(_diff)
                    # 바 너비: 최대 ±10% = 100% 폭
                    _bar_w = min(_abs / 10 * 100, 100)
                    _above = _diff > 0
                    _bar_c = "#16a34a" if _above else "#dc2626"
                    _txt_c = "#34d399" if _above else "#f43f5e"
                    _dir   = f"현재가 위 +{_diff:.2f}%" if _above else f"현재가 아래 {_diff:.2f}%"
                    _ma_html += (
                        f"<div style='display:flex;align-items:center;gap:10px'>"
                        f"<span style='color:{_mac};font-size:11px;font-weight:700;min-width:52px'>{_mal}</span>"
                        f"<span style='color:#64748b;font-size:11px;min-width:80px'>{format_price(_mav, sel_ticker)}</span>"
                        f"<div style='flex:1;background:#1e293b;border-radius:4px;height:12px;position:relative'>"
                        f"<div style='width:{_bar_w:.0f}%;background:{_bar_c};height:100%;border-radius:4px;"
                        f"{'margin-left:auto;' if not _above else ''}'></div>"
                        f"</div>"
                        f"<span style='color:{_txt_c};font-size:12px;font-weight:700;min-width:80px;text-align:right'>{_dir}</span>"
                        f"</div>"
                    )
                _ma_html += "</div>"
                st.markdown(_ma_html, unsafe_allow_html=True)


    # ══════════════════════════════════════════
    # 탭 3: Gemini 분석
    # ══════════════════════════════════════════

    with _sub_b2:
        if not gemini_key:
            st.warning("👈 사이드바에 Gemini API 키를 입력해주세요.")
        else:
            st.caption("💡 종목별로 개별 분석 버튼을 클릭하세요. (Free tier — Flash: 하루 500회 / Pro: 하루 25회)")

            import google.generativeai as genai
            genai.configure(api_key=gemini_key)
            _b2_model = genai.GenerativeModel(model_name)
            _B2_SYSTEM = (
                'You are a Korean stock quantitative analysis AI. '
                'Always respond in Korean. '
                'Rules: Reject R:R below 2.0 / Stop-loss -7% / '
                'No entry 09:00-09:30 KST / No averaging down'
            )

            def _gemini_safe_call(mdl, prompt_text, max_retries=4):
                """429 rate-limit 에러 시 지수 백오프로 재시도"""
                import time as _time, random as _random, re as _re
                for attempt in range(max_retries):
                    try:
                        return mdl.generate_content(prompt_text)
                    except Exception as _e:
                        err_str = str(_e)
                        if '429' in err_str:
                            # API가 명시한 대기 시간 우선, 없으면 지수 백오프
                            m = _re.search(r'seconds:\s*(\d+)', err_str)
                            base_wait = int(m.group(1)) + 2 if m else (10 * (2 ** attempt))
                            jitter = _random.uniform(0, 3)
                            wait = min(int(base_wait + jitter), 120)
                            st.warning(f"⏳ API 한도 초과 — {wait}초 대기 후 재시도 ({attempt+1}/{max_retries})")
                            _time.sleep(wait)
                        else:
                            raise
                raise Exception("최대 재시도 횟수 초과 (429 rate limit). 잠시 후 다시 시도하세요.")

            _b2_tickers = get_watchlist_tickers()
            # all_data에 없는 종목 즉시 로드
            _b2_missing = [(_bt, _bn) for _bt, _bn in _b2_tickers if _bt not in all_data]
            if _b2_missing:
                with st.spinner(f"📡 {len(_b2_missing)}개 종목 데이터 로딩 중..."):
                    for _bt, _bn in _b2_missing:
                        _bdf = fetch_ohlcv(_bt, 80)
                        if _bdf is not None and len(_bdf) >= 20:
                            st.session_state.all_data_cache[_bt] = {'name': _bn, 'df': calc_indicators(_bdf)}

            for ticker, name in _b2_tickers:
                if ticker not in all_data:
                    continue
                with st.expander(f"📊 {name} ({ticker}) 분석", expanded=False):
                    _ai_cache_key = f"_ai_result_{ticker}"
                    if st.button(f"{name} 분석", key=f"btn_{ticker}"):
                        prompt = build_prompt(all_data[ticker]['df'], name, ticker)
                        with st.spinner(f'{name} 분석 중...'):
                            try:
                                res = _gemini_safe_call(_b2_model, _B2_SYSTEM + '\n\n' + prompt)
                                st.session_state[_ai_cache_key] = res.text   # 결과 캐싱(rerun 유지)
                            except Exception as e:
                                st.error(f"오류: {e}")
                    # 캐시된 결과 렌더 (다운로드 버튼 클릭=rerun 에도 유지)
                    _ai_txt = st.session_state.get(_ai_cache_key)
                    if _ai_txt:
                        st.markdown(f"<div class='gemini-box'>{_ai_txt}</div>", unsafe_allow_html=True)
                        st.download_button(
                            "📋 분석 결과 저장", data=_ai_txt,
                            file_name=f"AI분석_{name}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
                            mime="text/plain", key=f"dl_ai_{ticker}", use_container_width=True,
                        )


    # ══════════════════════════════════════════
    # 탭 3: 분석 기록
    # ══════════════════════════════════════════
    with _sub_b3:
        st.markdown("<div style='font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:12px'>📋 분석 기록 — 최근 50건</div>", unsafe_allow_html=True)

        _col_hist_f, _col_hist_del = st.columns([5, 1])
        # 출처 필터 (분석탭 / 스캐너 / 스캐너드로어 구분해서 보기)
        _src_filter = _col_hist_f.radio(
            "출처 필터", ["전체", "분석탭", "스캐너드로어"],
            horizontal=True, key="analysis_src_filter", label_visibility="collapsed")
        if _col_hist_del.button("🗑️ 기록 초기화", key="clear_analysis_log", use_container_width=True):
            try:
                _fb_ref("/quant_analysis").delete()
            except Exception:
                pass
            st.session_state.pop('local_analysis_log', None)
            st.session_state.pop('_last_analysis_key', None)
            st.session_state.pop('_analysis_saved_keys', None)   # dedup 집합도 리셋
            st.rerun()

        _hist_rows = load_analysis_log(200)
        if _src_filter != "전체":
            _hist_rows = [r for r in _hist_rows if r.get('출처', '') == _src_filter]
        _hist_rows = _hist_rows[:50]
        if not _hist_rows:
            st.info("아직 분석 기록이 없습니다. 종목을 선택하면 자동으로 저장됩니다.")
        else:
            # 요약 통계
            _h_buy  = sum(1 for r in _hist_rows if '매수' in r.get('판정',''))
            _h_wait = sum(1 for r in _hist_rows if '관망' in r.get('판정',''))
            _h_no   = sum(1 for r in _hist_rows if '불가' in r.get('판정','') or '차단' in r.get('판정',''))
            st.markdown(f"""
<div style='display:flex;gap:12px;margin-bottom:12px'>
  <div style='background:rgba(52,211,153,0.12);border:1px solid #34d39940;border-radius:8px;padding:8px 14px;text-align:center;min-width:70px'>
    <div style='font-size:11px;color:#64748b'>매수권장</div>
    <div style='font-size:20px;font-weight:800;color:#34d399'>{_h_buy}</div>
  </div>
  <div style='background:rgba(251,191,36,0.10);border:1px solid #fbbf2440;border-radius:8px;padding:8px 14px;text-align:center;min-width:70px'>
    <div style='font-size:11px;color:#64748b'>관망</div>
    <div style='font-size:20px;font-weight:800;color:#fbbf24'>{_h_wait}</div>
  </div>
  <div style='background:rgba(244,63,94,0.10);border:1px solid #f43f5e40;border-radius:8px;padding:8px 14px;text-align:center;min-width:70px'>
    <div style='font-size:11px;color:#64748b'>진입불가</div>
    <div style='font-size:20px;font-weight:800;color:#f43f5e'>{_h_no}</div>
  </div>
  <div style='background:#0d1117;border:1px solid #1e293b;border-radius:8px;padding:8px 14px;text-align:center;min-width:70px'>
    <div style='font-size:11px;color:#64748b'>총 기록</div>
    <div style='font-size:20px;font-weight:800;color:#f0f4ff'>{len(_hist_rows)}</div>
  </div>
</div>""", unsafe_allow_html=True)

            for _hr in _hist_rows:
                _hv = _hr.get('판정', '')
                _hvc = "#34d399" if '매수' in _hv else "#fbbf24" if '관망' in _hv else "#f43f5e"
                _hvb = "rgba(52,211,153,0.08)" if '매수' in _hv else "rgba(251,191,36,0.06)" if '관망' in _hv else "rgba(244,63,94,0.06)"
                _hrr = _hr.get('R:R', 0)
                _hentry = _hr.get('진입가', 0)
                _hstop = _hr.get('손절가', 0)
                _ht1 = _hr.get('목표1', 0)
                _hsrc = _hr.get('출처', '')
                _hpre = _hr.get('프리셋', '')
                _hsc  = _hr.get('점수', 0)
                # 상세(진입/손절/목표/점수) 조합 — 값 있는 것만
                _det = f'{_hr.get("날짜","")} {_hr.get("시간","")[:5]}'
                if _hentry > 0: _det += f'&nbsp;·&nbsp;진입 <b style="color:#fbbf24">{_hentry:,.0f}</b>'
                if _hstop  > 0: _det += f'&nbsp;·&nbsp;손절 <b style="color:#f43f5e">{_hstop:,.0f}</b>'
                if _ht1    > 0: _det += f'&nbsp;·&nbsp;목표 <b style="color:#34d399">{_ht1:,.0f}</b>'
                if _hsc    > 0: _det += f'&nbsp;·&nbsp;점수 <b style="color:#fbbf24">{_hsc}</b>'
                # ⚠️ 단일 라인 HTML — 줄바꿈/들여쓰기 시 st.markdown이 </div>를 텍스트로 출력함
                _card = (
                    f"<div style='background:{_hvb};border:1px solid {_hvc}30;border-radius:10px;"
                    f"padding:10px 14px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center'>"
                    f"<div>"
                    f"<span style='font-weight:700;font-size:13px;color:#f0f4ff'>{_hr.get('종목명','?')}</span>"
                    f"<span style='color:#64748b;font-size:11px;margin-left:6px'>{_hr.get('종목코드','')}</span>"
                    f"<span style='background:#1e293b;color:#64748b;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px'>{_hsrc} · {_hpre}</span>"
                    f"<div style='font-size:11px;color:#64748b;margin-top:3px'>{_det}</div>"
                    f"</div>"
                    f"<div style='text-align:right'>"
                    f"<div style='font-size:13px;font-weight:800;color:{_hvc}'>{_hv}</div>"
                    f"<div style='font-size:12px;color:#64748b'>R:R <b style=\"color:{_hvc}\">{_hrr}</b></div>"
                    f"</div>"
                    f"</div>"
                )
                st.markdown(_card, unsafe_allow_html=True)

    # ══════════════════════════════════════════
    # 탭 4: 추천 스캐너
    # ══════════════════════════════════════════

with tab_c:
    st.markdown("### 📡 V9.1 단기 스윙 스캐너")

    # ── 📖 실전 매뉴얼 (기본 닫힘) ──────────────────────────────────────
    with st.expander("📖 V9.1 스캐너 실전 매뉴얼 (필독)", expanded=False):
        st.markdown("""
### 🦅 [V9.1 스나이퍼 스캐너 운용 수칙]

**STEP 1. 타격 전장(Universe) 선택**
- 🇰🇷 **국장 통합:** 당일 거래대금 상위 200개 주도주 (메인 타깃)
- 🇺🇸 **미장 핵심:** 나스닥 100 우량 기술주
- 🏦 **국내/미국 ETF:** 하락장 방어용 (※ 주의: ETF 모드 시 프리셋 및 AI 최적화는 안전을 위해 자동 차단됩니다)

**STEP 2. 🚀 스캔 가동 및 1초 브리핑 확인**
- 복잡한 설정은 잊고 **[스캔 시작]**을 누르십시오.
- 최상단 **'🔥 오늘 사격 가능'** 패널의 숫자부터 확인합니다. 0개라면 미련 없이 HTS를 끄고, 숫자가 1 이상일 때만 하단의 결과 테이블을 봅니다.

**STEP 3. 🚨 휩쏘 방어 배지(Badge) 행동 강령 (절대 원칙)**

테이블의 '연속등장' 배지에 따라 기계적으로 매매를 통제하십시오.
- ⚪ **1일 (신규):** 가짜 반등(휩쏘)일 수 있습니다. ➔ **관망**
- 🟡 **2일연속:** 추세가 굳어지고 있습니다. ➔ **관심종목 추가 후 사격 준비**
- 🟢 **3일연속:** 3일간의 가혹한 검증을 통과했습니다. ➔ **S/A등급 확인 후 1차 매수 격발**

**💡 [관제탑 실전 필승 루틴]**
1. **스캔 타이밍:** 장 마감 직전(15:10~15:20) 종가 베팅 또는 장 마감 후 복기 시간에 가동합니다.
2. **사전 준비:** 🟡 2일 연속 배지가 뜬 종목을 '관심종목'으로 넘겨 차트를 점검합니다.
3. **최종 타격:** 다음 날 장중 스캔 시 **🟢 3일 연속** 배지가 점등되면 09:30 이후 방아쇠를 당깁니다.
""")

    # ── 진입 금지 대형 배너 ──────────────────────────────────────────────
    _v891_c = run_v891_system_check()
    from datetime import datetime as _dt_tc
    _kh_c = (_dt_tc.utcnow().hour + 9) % 24
    _km_c = _dt_tc.utcnow().minute
    _tblock_c = (9 <= _kh_c < 10) or (_kh_c == 10 and _km_c <= 30)
    if not _v891_c['can_enter'] or _tblock_c:
        _bc_msg   = _v891_c['alerts'][0] if not _v891_c['can_enter'] else "09:00~10:30 변동성 과다"
        _bc_title = "현재 매매 불가: " + ("FOMC 대기 모드" if _v891_c.get('blackout') else "진입 금지 구간")
        st.markdown(f"""
<div style='background:linear-gradient(135deg,#1a0000,#2d0a0a);border:2px solid #ef4444;
border-radius:16px;padding:20px 24px;margin-bottom:14px;text-align:center'>
  <div style='font-size:36px;margin-bottom:6px'>🚫</div>
  <div style='font-size:20px;font-weight:900;color:#ef4444;margin-bottom:6px'>{_bc_title}</div>
  <div style='font-size:13px;color:#fca5a5'>{_bc_msg}</div>
  <div style='font-size:11px;color:#7f1d1d;margin-top:8px'>스캔 결과 확인은 가능 — 실제 주문은 금지 구간 해제 후</div>
</div>""", unsafe_allow_html=True)
    st.caption("하드필터(시총·ATR) + 스코어링(재무·수급·모멘텀·눌림목) — 70점 이상 종목만 포착")
    # 진입 금지 배너
    _v891_c = run_v891_system_check()
    if not _v891_c['can_enter']:
        for _ca in _v891_c['alerts']:
            st.warning(f"⚠️ {_ca} — 스캔은 가능하나 결과 종목 오늘 진입 불가")
    else:
        from datetime import datetime as _dt_tc
        _kh_c = (_dt_tc.utcnow().hour + 9) % 24
        _km_c = _dt_tc.utcnow().minute
        if (9 <= _kh_c < 10) or (_kh_c == 10 and _km_c <= 30):
            st.warning("🔒 09:00~10:30 진입 금지 구간 — 스캔 결과는 내일 진입 검토용으로 활용하세요")


    # ══════════════════════════════════════════
    # 🏛️ 연기금 추종 스캐너 (Gemini V2 설계)
    # ══════════════════════════════════════════
    with st.expander("🏛️ 연기금 추종 종목 스캐너", expanded=False):
        st.markdown("""
**연기금(국민연금 등)이 연속 순매수 중인 종목**을 탐지합니다. (Gemini V2 설계)

| 항목 | 내용 |
|---|---|
| 유니버스 | 시가총액 상위 300종목 (전종목 순회 제거) |
| 연기금 컬럼 | `연기금` → `연기금등` 한정 (기관합계 폴백 폐기) |
| 종합 점수 | 연속일×10 + **순매수 강도%**×2 + 외인쌍끌이 보너스 20점 |
| 기술 필터 | RSI≤70 (과매수 회피) + 종가≥MA60 (역배열 늪지대 제외) |
        """)

        _pg_c1, _pg_c2, _pg_c3 = st.columns([1, 1, 1])
        with _pg_c1:
            _pg_market = st.selectbox("대상 시장", ["KOSPI", "KOSDAQ", "KOSPI+KOSDAQ"],
                                       key="pg_market")
        with _pg_c2:
            _pg_days = st.slider("분석 기간 (거래일)", 3, 20, 10, key="pg_days",
                                  help="최근 N 거래일의 연기금 순매수 데이터를 분석합니다")
        with _pg_c3:
            _pg_min_streak = st.slider("연속 순매수 최소 일수", 1, 5, 2, key="pg_streak",
                                        help="N일 연속 순매수인 종목만 필터링")

        _pg_top_n = st.slider("결과 표시 종목 수", 5, 50, 20, key="pg_topn")
        _run_pg = st.button("🏛️ 연기금 추종 스캔 시작", type="primary",
                             use_container_width=True, key="run_pension_scan")

        # ── 진단 버튼: pykrx 실제 컬럼 확인 ──
        if st.button("🔍 pykrx 진단 (삼성전자 005930 기준)", key="pg_diag"):
            try:
                import pykrx as _pykrx_pkg
                from pykrx import stock as _pykrx_diag
                _d_end   = datetime.today().strftime('%Y%m%d')
                _d_prev  = (datetime.today() - timedelta(days=5)).strftime('%Y%m%d')
                _d_start = (datetime.today() - timedelta(days=30)).strftime('%Y%m%d')

                st.markdown(f"**pykrx 버전: `{getattr(_pykrx_pkg,'__version__','알 수 없음')}`**")

                # 사용 가능한 함수 중 investor 관련
                _inv_funcs = [f for f in dir(_pykrx_diag) if 'invest' in f.lower() or 'institution' in f.lower() or 'purchases' in f.lower()]
                st.write("investor 관련 함수:", _inv_funcs)

                st.markdown("**① `get_market_cap_by_ticker` 컬럼:**")
                try:
                    _dc = _pykrx_diag.get_market_cap_by_ticker(_d_end, market="KOSPI")
                    st.write(f"행 수: {len(_dc)}, 컬럼: {list(_dc.columns)}")
                    st.dataframe(_dc.head(3))
                except Exception as _e:
                    st.error(f"실패: {_e}")

                st.markdown("**② `get_market_trading_value_by_date` (삼성전자, detail=False):**")
                try:
                    _dv0 = _pykrx_diag.get_market_trading_value_by_date(_d_start, _d_end, "005930", detail=False)
                    st.write(f"shape: {_dv0.shape}, 컬럼: {list(_dv0.columns)}, index: {list(_dv0.index[-2:])}")
                    st.dataframe(_dv0.tail(3))
                except Exception as _e:
                    st.error(f"실패: {_e}")

                st.markdown("**③ `get_market_trading_value_by_date` (detail=True):**")
                try:
                    _dv1 = _pykrx_diag.get_market_trading_value_by_date(_d_start, _d_end, "005930", detail=True)
                    st.write(f"shape: {_dv1.shape}, 컬럼: {list(_dv1.columns)}")
                    st.dataframe(_dv1.tail(3))
                except Exception as _e:
                    st.error(f"실패: {_e}")

                st.markdown("**④ `get_market_net_purchases_of_equities_by_ticker` (KOSPI, 최근 5일):**")
                try:
                    _dv3 = _pykrx_diag.get_market_net_purchases_of_equities_by_ticker(
                        _d_prev, _d_end, market="KOSPI"
                    )
                    st.write(f"shape: {_dv3.shape}, 컬럼: {list(_dv3.columns)}")
                    st.dataframe(_dv3.head(5))
                except Exception as _e:
                    st.error(f"실패: {_e}")

                # 구버전 유령 함수 생존 여부 확인 (diagnostic only)
                st.markdown("**④-구버전 `get_market_net_purchases_of_institutional_investors_by_ticker` (존재 여부만 확인):**")
                _ghost_exists = hasattr(_pykrx_diag, "get_market_net_purchases_of_institutional_investors_by_ticker")
                st.write(f"함수 존재: {_ghost_exists} → {'❌ 유령 함수 (호출 금지)' if not _ghost_exists else '⚠️ 구버전 잔존'}")

                st.markdown("**⑤ `get_market_trading_value_by_investor` (시장 전체):**")
                try:
                    _dv4 = _pykrx_diag.get_market_trading_value_by_investor(_d_start, _d_end, "KOSPI")
                    st.write(f"shape: {_dv4.shape}, 컬럼: {list(_dv4.columns) if hasattr(_dv4,'columns') else type(_dv4)}")
                    st.dataframe(_dv4.tail(3) if hasattr(_dv4,'tail') else str(_dv4))
                except Exception as _e:
                    st.error(f"실패: {_e}")

            except Exception as _pg_diag_err:
                import traceback
                st.error(f"진단 오류: {_pg_diag_err}")
                st.code(traceback.format_exc())

        if _run_pg:
            try:
                import pandas as _pd_pg
                import yfinance as _yf_pg
                import requests as _req_pg
                import io as _io_pg

                _pg_prog   = st.progress(0)
                _pg_status = st.empty()

                # ══ KRX 직접 API로 연기금 실제 데이터 수집 ══
                # pykrx가 내부적으로 쓰는 KRX 엔드포인트를 직접 호출 (파싱 버그 우회)
                _KRX_OTP  = "http://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd"
                _KRX_DOWN = "http://data.krx.co.kr/comm/fileDn/download_csv.cmd"
                _KRX_HDR  = {
                    "User-Agent": "Mozilla/5.0",
                    "Referer":    "http://data.krx.co.kr/",
                }

                def _krx_investor_by_ticker(date_str: str, mkt: str) -> "_pd_pg.DataFrame | None":
                    """KRX 투자자별 거래실적 (종목별) — 날짜 1일치 전체 종목"""
                    _mkt_id = "STK" if mkt == "KOSPI" else "KSQ"
                    try:
                        _otp_r = _req_pg.post(_KRX_OTP, data={
                            "bld":    "dbms/MDC/STAT/standard/MDCSTAT02302",
                            "mktId":  _mkt_id,
                            "trdDd":  date_str,
                            "share":  "1",
                            "money":  "1",
                            "csvxls_isNo": "false",
                        }, headers=_KRX_HDR, timeout=15)
                        _otp = _otp_r.text.strip()
                        if not _otp:
                            return None
                        _csv_r = _req_pg.post(_KRX_DOWN, data={"code": _otp},
                                              headers=_KRX_HDR, timeout=15)
                        _csv_r.encoding = "euc-kr"
                        _df = _pd_pg.read_csv(_io_pg.StringIO(_csv_r.text),
                                              thousands=",", encoding="utf-8")
                        return _df
                    except Exception:
                        return None

                # ── 유니버스: 내장 주요 종목 리스트 ──
                _KS_UNI = [
                    ("005930","삼성전자","KOSPI"),("000660","SK하이닉스","KOSPI"),
                    ("005490","POSCO홀딩스","KOSPI"),("005380","현대차","KOSPI"),
                    ("035420","NAVER","KOSPI"),("000270","기아","KOSPI"),
                    ("051910","LG화학","KOSPI"),("006400","삼성SDI","KOSPI"),
                    ("055550","신한지주","KOSPI"),("105560","KB금융","KOSPI"),
                    ("086790","하나금융지주","KOSPI"),("003550","LG","KOSPI"),
                    ("017670","SK텔레콤","KOSPI"),("030200","KT","KOSPI"),
                    ("066570","LG전자","KOSPI"),("009150","삼성전기","KOSPI"),
                    ("042700","한미반도체","KOSPI"),("012450","한화에어로스페이스","KOSPI"),
                    ("329180","HD현대중공업","KOSPI"),("009540","HD한국조선해양","KOSPI"),
                    ("042660","한화오션","KOSPI"),("064350","현대로템","KOSPI"),
                    ("047810","한국항공우주","KOSPI"),("298040","효성중공업","KOSPI"),
                    ("011070","LG이노텍","KOSPI"),("373220","LG에너지솔루션","KOSPI"),
                    ("010130","고려아연","KOSPI"),("058470","리노공업","KOSPI"),
                    ("068270","셀트리온","KOSPI"),("207940","삼성바이오로직스","KOSPI"),
                    ("000100","유한양행","KOSPI"),("128940","한미약품","KOSPI"),
                    ("272210","한화시스템","KOSPI"),("357780","솔브레인","KOSPI"),
                    ("095340","ISC","KOSPI"),("001440","대한전선","KOSPI"),
                    ("034730","SK","KOSPI"),("096770","SK이노베이션","KOSPI"),
                    ("271560","오리온","KOSPI"),("097950","CJ제일제당","KOSPI"),
                ]
                _KQ_UNI = [
                    ("086520","에코프로","KOSDAQ"),("196170","알테오젠","KOSDAQ"),
                    ("214150","클래시스","KOSDAQ"),("145020","휴젤","KOSDAQ"),
                    ("259960","크래프톤","KOSDAQ"),("293490","카카오게임즈","KOSDAQ"),
                    ("039030","이오테크닉스","KOSDAQ"),("240810","원익IPS","KOSDAQ"),
                    ("036830","솔브레인홀딩스","KOSDAQ"),("046890","서울반도체","KOSDAQ"),
                    ("035900","JYP Ent.","KOSDAQ"),("041510","에스엠","KOSDAQ"),
                    ("263750","펄어비스","KOSDAQ"),("007660","이수페타시스","KOSDAQ"),
                    ("079550","LIG넥스원","KOSDAQ"),
                ]
                _universe = (_KS_UNI if _pg_market == "KOSPI"
                             else _KQ_UNI if _pg_market == "KOSDAQ"
                             else _KS_UNI + _KQ_UNI)

                _pg_prog.progress(0.05)
                _pg_status.caption(f"유니버스: {len(_universe)}종목")

                # ── ① KRX 직접 API로 최근 N일 연기금 순매수 수집 ──
                _pg_status.caption("① KRX 직접 API — 연기금 순매수 수집 중...")
                _today_pg = datetime.utcnow() + timedelta(hours=9)   # KST (서버 UTC 대비)
                _krx_dates = []
                for _dd in range(_pg_days * 2 + 5):
                    _cand = (_today_pg - timedelta(days=_dd)).strftime('%Y%m%d')
                    _krx_dates.append(_cand)

                # ticker → [daily_pension_net list]
                _pension_daily: dict = {}   # tk → [n1, n2, ...]
                _foreigner_daily: dict = {} # tk → total
                _ticker_name_map: dict = {tk: nm for tk, nm, _ in _universe}
                _krx_col_names = ['연기금', '연기금등']
                _for_col_names = ['외국인', '외국인합계']
                _code_col_names = ['종목코드', '티커', 'ISU_CD', 'ISU_SRT_CD']

                _days_collected = 0
                for _mkt_pg in (["KOSPI","KOSDAQ"] if _pg_market == "KOSPI+KOSDAQ"
                                 else [_pg_market]):
                    for _date_str in _krx_dates:
                        if _days_collected >= _pg_days:
                            break
                        _df_krx = _krx_investor_by_ticker(_date_str, _mkt_pg)
                        if _df_krx is None or _df_krx.empty:
                            continue

                        # 컬럼명 정규화
                        _df_krx.columns = [c.strip() for c in _df_krx.columns]
                        _code_col = next((c for c in _code_col_names if c in _df_krx.columns), None)
                        _pen_col  = next((c for c in _krx_col_names  if c in _df_krx.columns), None)
                        _for_col  = next((c for c in _for_col_names  if c in _df_krx.columns), None)

                        if _code_col is None or _pen_col is None:
                            continue  # 이 날 데이터 구조가 다름

                        def _parse_num_signed(_x):
                            """콤마 제거 후 부호 보존 파싱. 빈칸/'-'만 있으면 0. (음수 부호 파괴 금지)"""
                            _s = str(_x).replace(',', '').strip()
                            if _s in ('', '-', 'nan', 'None'):
                                return 0.0
                            try:
                                return float(_s)
                            except (ValueError, TypeError):
                                return 0.0

                        for _, _rw in _df_krx.iterrows():
                            _tk = str(_rw[_code_col]).strip().zfill(6)
                            _pv = _parse_num_signed(_rw[_pen_col])
                            _fv = _parse_num_signed(_rw.get(_for_col, 0)) if _for_col else 0.0
                            _pension_daily.setdefault(_tk, []).append(_pv)
                            _foreigner_daily[_tk] = _foreigner_daily.get(_tk, 0.0) + _fv

                        _days_collected += 1

                # KRX raw는 today→past(내림차순) 수집 → 오름차순으로 뒤집어야
                # 연속일 계산(reversed 최신부터)이 정확 (pykrx 티어와 동일 기준)
                for _tk6 in _pension_daily:
                    _pension_daily[_tk6].reverse()
                _krx_ok = bool(_pension_daily)

                # ── ①-b KRX raw 실패 시 pykrx 폴백 (실제 연기금 데이터 재시도) ──
                if not _krx_ok:
                    _pg_status.caption("①-b pykrx 폴백 — 연기금 순매수 재수집 중...")
                    try:
                        from pykrx import stock as _pk_pg
                        _pk_days = 0
                        for _mkt_pg2 in (["KOSPI","KOSDAQ"] if _pg_market == "KOSPI+KOSDAQ" else [_pg_market]):
                            for _date_str in _krx_dates:
                                if _pk_days >= _pg_days:
                                    break
                                try:
                                    _npdf = _pk_pg.get_market_net_purchases_of_equities(
                                        _date_str, _date_str, _mkt_pg2, "연기금")
                                except Exception:
                                    _npdf = None
                                if _npdf is None or _npdf.empty:
                                    continue
                                _ncol = next((c for c in _npdf.columns if "순매수" in str(c) and "대금" in str(c)), None)
                                if _ncol is None:
                                    _ncol = next((c for c in _npdf.columns if "순매수" in str(c)), None)
                                if _ncol is None:
                                    continue
                                # 외국인 순매수(같은 날, 보너스 판정용)
                                try:
                                    _fdf = _pk_pg.get_market_net_purchases_of_equities(
                                        _date_str, _date_str, _mkt_pg2, "외국인")
                                    _fcol = next((c for c in _fdf.columns if "순매수" in str(c) and "대금" in str(c)), None)
                                except Exception:
                                    _fdf, _fcol = None, None
                                for _tk_idx in _npdf.index:
                                    _tk6 = str(_tk_idx).strip().zfill(6)
                                    try:
                                        _pv = float(_npdf.loc[_tk_idx, _ncol])
                                    except Exception:
                                        _pv = 0.0
                                    if _pv == _pv:
                                        _pension_daily.setdefault(_tk6, []).append(_pv)
                                    if _fdf is not None and _fcol is not None and _tk_idx in _fdf.index:
                                        try:
                                            _foreigner_daily[_tk6] = _foreigner_daily.get(_tk6, 0.0) + float(_fdf.loc[_tk_idx, _fcol])
                                        except Exception:
                                            pass
                                _pk_days += 1
                        # pykrx는 최신→과거 역순 수집 → 날짜 오름차순 정렬(연속일 계산 정확성)
                        for _tk6 in _pension_daily:
                            _pension_daily[_tk6].reverse()
                        _krx_ok = bool(_pension_daily)
                    except ImportError:
                        pass

                # ── ①-c KRX·pykrx 모두 실패 시 KIS '기관 순매수' 폴백 ──
                # 연기금 ⊂ 기관. KIS는 순수 연기금 분리를 못 하므로 기관 전체 순매수로 대체.
                _kis_mode = False
                if not _krx_ok and kis_available():
                    _pg_status.caption("①-c KIS 폴백 — 기관 순매수 수집 중...")
                    _kis_hit = 0
                    for _utk, _unm, _umkt in _universe:
                        _org_list, _for_tot = kis_get_org_net_daily(str(_utk), _pg_days)
                        if _org_list:
                            _pension_daily[str(_utk).zfill(6)] = _org_list
                            _foreigner_daily[str(_utk).zfill(6)] = _for_tot
                            _kis_hit += 1
                    if _kis_hit > 0:
                        _krx_ok = True
                        _kis_mode = True

                if _krx_ok and _kis_mode:
                    st.success(f"✅ KIS 기관 순매수 데이터 수집 완료 ({len(_pension_daily)}종목) "
                               "— ※ 순수 연기금이 아닌 '기관 전체'(연기금 포함) 기준입니다.")
                elif _krx_ok:
                    st.success(f"✅ 연기금 실제 데이터 수집 완료 ({len(_pension_daily)}종목)")
                else:
                    st.warning("⚠️ KRX·pykrx·KIS 모두 응답 없음 → 기술적 프록시 모드로 전환합니다. "
                               "(실제 수급 데이터가 아닌 기술적 근사치이니 참고용으로만 활용하세요)")

                _pg_prog.progress(0.4)

                # ── ② yfinance 배치 다운로드 (기술 필터용) ──
                _pg_status.caption("② yfinance 주가 데이터 배치 수집 중...")
                _sym_map = {
                    (f"{tk}.KS" if mkt == "KOSPI" else f"{tk}.KQ"): (tk, nm, mkt)
                    for tk, nm, mkt in _universe
                }
                _all_syms = list(_sym_map.keys())
                try:
                    _batch = _yf_pg.download(
                        _all_syms, period="6mo", interval="1d",
                        group_by="ticker", progress=False, threads=True, timeout=60
                    )
                except Exception as _be:
                    st.error(f"❌ yfinance 실패: {_be}")
                    st.stop()

                _pg_prog.progress(0.65)
                _pg_status.caption("③ 분석 중...")

                _pg_results = []
                _fail_counts: dict = {}

                for _i, (_sym, (_tk, _nm, _mkt)) in enumerate(_sym_map.items()):
                    try:
                        _df = _batch if len(_all_syms) == 1 else (
                            _batch.get(_sym) if hasattr(_batch,'get') else _batch[_sym])
                        if _df is None or len(_df) < 14:
                            continue
                        _cl = _df['Close'].dropna()
                        _vl = _df['Volume'].dropna()
                        if len(_cl) < 14:
                            continue

                        _cur   = float(_cl.iloc[-1])
                        _ma20  = float(_cl.rolling(20).mean().iloc[-1])
                        _ma60  = float(_cl.rolling(min(len(_cl),60)).mean().iloc[-1])
                        _vol20 = float(_vl.rolling(20).mean().iloc[-1])
                        _vol_r = float(_vl.iloc[-1]) / (_vol20 + 1e-9)
                        _dg = _cl.diff(); _g = _dg.clip(lower=0).rolling(14).mean().iloc[-1]
                        _l  = (-_dg).clip(lower=0).rolling(14).mean().iloc[-1]
                        _rsi = float(100 - 100 / (1 + _g / (_l + 1e-9)))

                        # 연속 상승 일수
                        _streak = 0
                        for _v in reversed(_cl.diff().dropna().values):
                            if _v > 0: _streak += 1
                            else: break

                        if _krx_ok:
                            # ── KRX 실제 데이터 모드 ──
                            _daily_vals  = _pension_daily.get(_tk, [])
                            _pen_streak  = 0
                            for _v in reversed(_daily_vals):
                                if _v > 0: _pen_streak += 1
                                else: break
                            if _pen_streak < _pg_min_streak:
                                _fail_counts['연기금연속'] = _fail_counts.get('연기금연속',0)+1
                                continue
                            if _rsi > 75:
                                _fail_counts['rsi'] = _fail_counts.get('rsi',0)+1; continue
                            if _cur < _ma60 * 0.97:
                                _fail_counts['ma60'] = _fail_counts.get('ma60',0)+1; continue

                            _pen_net   = sum(_daily_vals)
                            _pen_abs   = sum(abs(v) for v in _daily_vals)
                            _intensity = (_pen_net / _pen_abs * 100) if _pen_abs > 0 else 0.0
                            _for_bonus = 20 if _foreigner_daily.get(_tk,0) > 0 else 0
                            _score = _pen_streak*10 + max(_intensity,0)*2 + _for_bonus

                            _pg_results.append({
                                '종목코드':       _tk, '종목명': _nm, '시장': _mkt,
                                '연기금연속(일)':  _pen_streak,
                                '순매수강도(%)':   round(_intensity, 2),
                                '외인쌍끌이':      "✅" if _for_bonus else "-",
                                '현재가':          f"{int(_cur):,}원",
                                'RSI':            round(_rsi, 1),
                                'MA60대비(%)':    round((_cur/_ma60-1)*100,1),
                                '종합점수':        round(_score, 1),
                            })
                        else:
                            # ── 기술적 프록시 모드 ──
                            if _streak < _pg_min_streak:
                                _fail_counts['streak'] = _fail_counts.get('streak',0)+1; continue
                            if _rsi > 75:
                                _fail_counts['rsi'] = _fail_counts.get('rsi',0)+1; continue
                            if _cur < _ma60 * 0.97:
                                _fail_counts['ma60'] = _fail_counts.get('ma60',0)+1; continue
                            _aligned = _ma20 > _ma60
                            _score = (_streak*10 + min(_vol_r,3.0)*5
                                      + (75-_rsi)*0.5 + (10 if _aligned else 0))
                            _pg_results.append({
                                '종목코드':     _tk, '종목명': _nm, '시장': _mkt,
                                '연속상승(일)':  _streak,
                                '거래량비율':    round(_vol_r, 2),
                                '현재가':        f"{int(_cur):,}원",
                                'RSI':          round(_rsi, 1),
                                'MA60대비(%)':  round((_cur/_ma60-1)*100,1),
                                '정배열':        "✅" if _ma20>_ma60 else "-",
                                '종합점수':      round(_score, 1),
                            })

                    except Exception:
                        pass

                    if _i % 15 == 0:
                        _pg_prog.progress(min(0.65 + 0.34*_i/max(len(_sym_map),1), 0.99))

                _pg_prog.progress(1.0)
                _pg_status.caption(f"✅ 스캔 완료 — {len(_pg_results)}종목 탐지")
                _mode_label = ("🏦 KIS 기관 순매수(연기금 포함)" if _kis_mode
                               else "🏛️ 연기금 실제순매수" if _krx_ok
                               else "📊 기술적 기관매집 프록시")

                if not _pg_results:
                    _reason = " | ".join(f"{k} {v}개" for k, v in _fail_counts.items())
                    st.info(f"📭 조건 만족 종목 없음 | 탈락: {_reason or '데이터 없음'}\n"
                            f"💡 '연속 최소 일수' 슬라이더를 1로 낮춰보세요.")
                else:
                    _pg_df = (_pd_pg.DataFrame(_pg_results)
                              .sort_values('종합점수', ascending=False)
                              .head(_pg_top_n).reset_index(drop=True))

                    # ── 3일 연속 등장 추적 (Firebase) ──
                    _today_tk_list = _pg_df['종목코드'].astype(str).tolist()
                    try:
                        _streak_map, _streak_locked = _get_pension_scan_streak(_today_tk_list)
                    except Exception:
                        _streak_map, _streak_locked = {}, False

                    # 연속등장일 컬럼 추가
                    _pg_df['연속등장(일)'] = _pg_df['종목코드'].astype(str).map(
                        lambda _t: _streak_map.get(_t, 1)
                    )

                    # 결과를 세션에 캐시 → rerun/버튼 클릭에도 표시 유지
                    # (표시·버튼 렌더는 try 밖에서 수행 → st.rerun 예외가 삼켜지지 않음)
                    st.session_state['_pg_cache'] = {
                        'df': _pg_df,
                        'streak': _streak_map,
                        'locked': _streak_locked,
                        'mode': _mode_label,
                        'topn': _pg_top_n,
                        'nres': len(_pg_results),
                    }

            except Exception as _pg_err:
                st.error(f"연기금 스캔 오류: {_pg_err}")
                import traceback; st.code(traceback.format_exc())

        # ── 결과 렌더 (스캔 없이도 세션 캐시로 표시 · try 밖 → 버튼 rerun 정상) ──
        _pgc = st.session_state.get('_pg_cache')
        if _pgc:
            render_pension_results(_pgc['df'], _pgc['streak'], _pgc['locked'],
                                   _pgc['mode'], _pgc['topn'], _pgc['nres'])

    # ══════════════════════════════════════════
    # ⚙️ 고급 스캔 설정 (Progressive Disclosure — 기본 닫힘)
    # ══════════════════════════════════════════
    with st.expander("⚙️ 고급 스캔 설정 (프리셋 · 필터 · AI 최적화)", expanded=False):

        # (빈 '전략 프리셋' 헤더/컬럼 제거 — 실제 프리셋 라디오는 아래 별도 블록)
        _opt_col1, _opt_col2, _opt_col3 = st.columns([2, 1, 1])
        with _opt_col1:
            _opt_months = st.slider("백테스트 기간 (개월)", 3, 12, 6, key="opt_months")
            _opt_topn   = st.slider("최적화 대상 종목 수", 10, 50, 20, key="opt_topn",
                                     help="종목이 많을수록 정확하지만 시간이 오래 걸립니다")
        with _opt_col2:
            _opt_market = st.selectbox("대상 시장", ["KOSPI", "KOSDAQ", "KOSPI+KOSDAQ", "미국(S&P500)", "미국 ETF(VTI+)"], key="opt_market")
        with _opt_col3:
            st.markdown("<br>", unsafe_allow_html=True)
            _run_opt = st.button("🔥 최적화 시작", use_container_width=True,
                                  type="primary", key="run_optimizer")

        # ── 현재 적용된 파라미터 표시 ──
        _cur_c5 = st.session_state.get("opt_best_cond5", 0.08)
        _cur_c6 = st.session_state.get("opt_best_cond6", 0.50)
        st.info(f"📌 현재 스캐너 적용 파라미터 — cond5: **{_cur_c5*100:.0f}%** | cond6: **{_cur_c6*100:.0f}%**")

        if _run_opt:
            try:
                from optimizer import run_walk_forward, fetch_ohlcv_for_optimization

                # 종목 리스트 로드 (기존 스캐너와 동일 로직)
                try:
                    _oj = _os.path.join(_os.path.dirname(__file__), 'scanner_tickers.json')
                    with open(_oj, 'r', encoding='utf-8') as _f:
                        _tj = json.load(_f)
                    _opt_kospi  = [tuple(x) for x in _tj.get('KOSPI',  [])]
                    _opt_kosdaq = [tuple(x) for x in _tj.get('KOSDAQ', [])]
                    _opt_sp500  = [tuple(x) for x in _tj.get('SP500',  [])]
                except Exception:
                    _opt_kospi  = [("005930","삼성전자"),("000660","SK하이닉스"),
                                   ("042700","한미반도체"),("012450","한화에어로스페이스"),
                                   ("329180","HD현대중공업"),("005380","현대차"),
                                   ("000270","기아"),("035420","NAVER"),
                                   ("051910","LG화학"),("006400","삼성SDI")]
                    _opt_kosdaq = [("086520","에코프로"),("247540","에코프로비엠"),
                                   ("196170","알테오젠"),("357780","솔브레인"),
                                   ("058470","리노공업"),("095340","ISC"),
                                   ("036930","주성엔지니어링"),("039030","이오테크닉스"),
                                   ("240810","원익IPS"),("035900","JYP엔터테인먼트")]
                    _opt_sp500  = [("AAPL","Apple"),("MSFT","Microsoft"),
                                   ("NVDA","NVIDIA"),("GOOGL","Alphabet"),
                                   ("AMZN","Amazon"),("META","Meta"),
                                   ("TSLA","Tesla"),("AVGO","Broadcom"),
                                   ("AMD","AMD"),("NFLX","Netflix"),
                                   ("CRM","Salesforce"),("ORCL","Oracle"),
                                   ("ADBE","Adobe"),("QCOM","Qualcomm"),
                                   ("MU","Micron"),("INTC","Intel"),
                                   ("COIN","Coinbase"),("SHOP","Shopify"),
                                   ("UBER","Uber"),("SNOW","Snowflake")]

                _opt_etf_uni_pairs = [
                    ("VTI","Vanguard 전체주식시장"),("SPY","SPDR S&P500"),
                    ("QQQ","Invesco 나스닥100"),("DIA","SPDR 다우존스"),
                    ("IWM","iShares 러셀2000"),("JEPQ","JPMorgan Nasdaq Income"),
                    ("SCHD","Schwab 배당주"),("TLT","iShares 미국채20년"),
                    ("IEF","iShares 미국채7-10년"),("SOXX","iShares 반도체"),
                    ("SMH","VanEck 반도체"),("ARKK","ARK 혁신"),
                    ("TQQQ","ProShares 나스닥3X"),("SOXL","Direxion 반도체3X"),
                    ("ITA","iShares 방산항공"),("GLD","SPDR 금"),
                    ("SLV","iShares 은"),("XLE","Energy Select"),
                    ("XLI","Industrials Select"),("BOTZ","글로벌 로보틱스AI"),
                    ("PPA","Invesco 방산"),("EEM","iShares 이머징"),
                ][:_opt_topn]

                if _opt_market == "KOSPI":
                    _opt_tickers = _opt_kospi[:_opt_topn]
                elif _opt_market == "KOSDAQ":
                    _opt_tickers = _opt_kosdaq[:_opt_topn]
                elif _opt_market == "미국(S&P500)":
                    _opt_tickers = _opt_sp500[:_opt_topn]
                elif _opt_market == "미국 ETF(VTI+)":
                    _opt_tickers = _opt_etf_uni_pairs
                else:
                    _half = _opt_topn // 2
                    _opt_tickers = _opt_kospi[:_half] + _opt_kosdaq[:_half]

                # ── Step 1: 데이터 다운로드 ──
                st.markdown("**① 데이터 다운로드 중...**")
                _dl_prog  = st.progress(0)
                _dl_status = st.empty()

                def _dl_cb(cur, tot):
                    _dl_prog.progress(cur / tot)
                    _dl_status.caption(f"{cur}/{tot} 종목 다운로드 중...")

                _ticker_dfs = fetch_ohlcv_for_optimization(
                    _opt_tickers, months=_opt_months, progress_cb=_dl_cb
                )
                _dl_prog.progress(1.0)
                _dl_status.caption(f"✅ {len(_ticker_dfs)}/{len(_opt_tickers)} 종목 데이터 로드 완료")

                if len(_ticker_dfs) < 3:
                    st.error("데이터를 충분히 가져오지 못했습니다. 네트워크를 확인하거나 종목 수를 줄여주세요.")
                    st.stop()

                # ── Step 2: Walk-Forward 최적화 ──
                st.markdown("**② Walk-Forward Grid Search 실행 중...**")
                _wf_prog   = st.progress(0)
                _wf_status = st.empty()

                def _wf_cb(cur, tot):
                    _wf_prog.progress(cur / tot)
                    _wf_status.caption(f"그리드 탐색: {cur}/{tot}")

                _report = run_walk_forward(
                    _ticker_dfs,
                    in_months=4,
                    out_months=2,
                    progress_cb=_wf_cb,
                )
                _wf_prog.progress(1.0)
                _wf_status.caption("✅ 최적화 완료!")

                # ── Step 3: 결과 저장 ──
                st.session_state["opt_best_cond5"]  = _report.best_cond5
                st.session_state["opt_best_cond6"]  = _report.best_cond6
                st.session_state["opt_report"]      = _report
                st.session_state["opt_applied"]     = True

                st.success(
                    f"🎯 최적 파라미터 도출 — "
                    f"**cond5: {_report.best_cond5*100:.0f}%** | "
                    f"**cond6: {_report.best_cond6*100:.0f}%** — "
                    f"스캐너에 즉시 반영됩니다!"
                )

            except Exception as _oe:
                st.error(f"최적화 오류: {_oe}")
                import traceback; st.code(traceback.format_exc())

        # ── 최적화 결과 표시 ──
        if "opt_report" in st.session_state:
            _rep = st.session_state["opt_report"]
            st.divider()
            st.markdown(f"#### 📊 최적화 결과 ({_rep.timestamp})")

            _res_c1, _res_c2, _res_c3, _res_c4, _res_c5 = st.columns(5)
            _res_c1.metric("최적 cond5", f"{_rep.best_cond5*100:.0f}%")
            _res_c2.metric("최적 cond6", f"{_rep.best_cond6*100:.0f}%")
            _res_c3.metric("OOS 승률",   f"{_rep.oos_win_rate:.1f}%")
            _res_c4.metric("OOS 샤프",   f"{_rep.oos_sharpe:.2f}")
            _res_c5.metric("OOS MDD",    f"{_rep.oos_mdd:.1f}%")

            _mc1, _mc2 = st.columns(2)

            with _mc1:
                st.markdown("**윈도우별 Walk-Forward 결과**")
                if _rep.window_results:
                    _wf_df = pd.DataFrame(_rep.window_results).rename(columns={
                        "window": "기간", "best_cond5": "cond5", "best_cond6": "cond6",
                        "is_score": "IS 점수", "oos_win_rate": "OOS 승률(%)",
                        "oos_sharpe": "OOS 샤프", "oos_mdd": "OOS MDD(%)",
                        "oos_trades": "OOS 신호수",
                    })
                    _wf_df["cond5"] = (_wf_df["cond5"] * 100).astype(int).astype(str) + "%"
                    _wf_df["cond6"] = (_wf_df["cond6"] * 100).astype(int).astype(str) + "%"
                    st.dataframe(_wf_df, use_container_width=True, hide_index=True)

            with _mc2:
                st.markdown("**그리드 서치 히트맵 (마지막 윈도우)**")
                if not _rep.grid_summary.empty:
                    import plotly.graph_objects as _go_opt
                    _gs = _rep.grid_summary.copy()
                    _c5_labels = [f"{v*100:.0f}%" for v in sorted(_gs["cond5"].unique())]
                    _c6_labels = [f"{v*100:.0f}%" for v in sorted(_gs["cond6"].unique())]
                    _pivot = _gs.pivot_table(index="cond6", columns="cond5", values="score")
                    _fig_hm = _go_opt.Figure(_go_opt.Heatmap(
                        z=_pivot.values.tolist(),
                        x=[f"{v*100:.0f}%" for v in _pivot.columns],
                        y=[f"{v*100:.0f}%" for v in _pivot.index],
                        colorscale="RdYlGn",
                        colorbar_title="점수",
                        hovertemplate="cond5=%{x}<br>cond6=%{y}<br>점수=%{z:.3f}<extra></extra>",
                    ))
                    _fig_hm.update_layout(
                        title="Sharpe×승률 스코어 히트맵",
                        xaxis_title="cond5 (5일 누적 수익률 하한)",
                        yaxis_title="cond6 (거래량 비율 상한)",
                        height=350, margin=dict(l=50, r=20, t=40, b=40),
                    )
                    st.plotly_chart(_fig_hm, use_container_width=True)

    st.divider()

    with st.expander("⚙️ 스캐너 설정 (프리셋 · 필터 · AI 최적화)", expanded=False):
        # ── 프리셋: 시장 레짐 기반 자동 추천 라디오 ──
        st.markdown("#### ⚡ 전략 프리셋")

        if 'scan_preset' not in st.session_state:
            st.session_state.scan_preset = None

        def _apply_preset(name):
            """프리셋 선택 시 체크박스 session_state 동시 업데이트"""
            st.session_state.scan_preset = name
            _map = {
                # (rsi, vol, macd, bb, align)
                "bounce": (True,  True,  False, False, False),
                "trend":  (False, True,  True,  False, True),
                "bottom": (True,  True,  True,  True,  False),
                "custom": (st.session_state.get('f_rsi', True),
                           st.session_state.get('f_vol', True),
                           st.session_state.get('f_macd', False),
                           st.session_state.get('f_bb', False),
                           st.session_state.get('f_align', False)),
            }
            r, v, m, b, a = _map[name]
            st.session_state['f_rsi']   = r
            st.session_state['f_vol']   = v
            st.session_state['f_macd']  = m
            st.session_state['f_bb']    = b
            st.session_state['f_align'] = a

        # ── 고급 설정 expander 내부 UI ──────────────────────────────────────
        _preset_etf_lock = ("국내 ETF" in st.session_state.get("scanner_market", "")
                            or "미국 ETF" in st.session_state.get("scanner_market", ""))
        if _preset_etf_lock:
            st.caption("🔒 ETF 모드: 프리셋은 ETF 스캔에 적용되지 않습니다 (스캔 시 자동 무시)")

        # ── 5AI 레짐 판정 → 추천 전략 ──
        _rg = detect_market_regime_for_strategy()
        _rec_preset = _rg["preset"]   # 'bottom' | 'trend' | 'bounce'
        _rec_extra = " (실매수 금지 · 관망/정찰용)" if _rg["regime"] == "crash" else ""

        # 프리셋 키 (라디오 값은 '키'로 안정화 — 배지는 captions로 분리해 라벨 불일치 방지)
        _preset_keys = ["bounce", "trend", "bottom", "custom"]
        _base_lbl = {"bounce": "📉 반등매매", "trend": "📈 추세매매",
                     "bottom": "🎯 바닥확인", "custom": "⚙️ 직접설정"}
        _caps = ["✨ 추천" if k == _rec_preset else " " for k in _preset_keys]

        # 추천 알림 메시지
        st.info(f"**5AI 판정: 현재 [{_rg['label']}] 장세입니다.** "
                f"→ **[{_base_lbl[_rec_preset]}]** 전략을 권장합니다{_rec_extra}.  "
                f"\n\n📊 {_rg['reason']}", icon="🧭")

        # 기본 index: 기존 선택값 있으면 유지, 없으면 추천 전략
        _cur_preset = st.session_state.get('scan_preset')
        _default_key = _cur_preset if _cur_preset in _preset_keys else _rec_preset
        _default_idx = _preset_keys.index(_default_key)

        try:
            _sel_key = st.radio("전략 선택", _preset_keys, index=_default_idx,
                                format_func=lambda k: _base_lbl.get(k, k),
                                captions=_caps, key="scan_preset_radio",
                                horizontal=True, label_visibility="collapsed")
        except TypeError:   # 구버전(captions 미지원) 폴백
            _sel_key = st.radio("전략 선택", _preset_keys, index=_default_idx,
                                format_func=lambda k: _base_lbl.get(k, k),
                                key="scan_preset_radio", horizontal=True,
                                label_visibility="collapsed")
        # 선택이 바뀌면 프리셋 적용
        if _sel_key != st.session_state.get('scan_preset'):
            _apply_preset(_sel_key)
            st.rerun()

        _preset_desc = {
            "bounce": "📉 반등매매 — RSI 과매도 + 거래량 폭발",
            "trend":  "📈 추세매매 — MACD 골든크로스 + 정배열 + 거래량",
            "bottom": "🎯 바닥확인 — RSI + MACD + BB 하단 + 거래량",
            "custom": "⚙️ 직접설정 — 아래 체크박스로 조건 선택",
        }
        if st.session_state.scan_preset and not _preset_etf_lock:
            st.caption(_preset_desc[st.session_state.scan_preset])

        st.divider()
        # ── 필터 체크박스 (직접설정 시 활성) ────────────────────────────────
        st.markdown("##### 🎯 상세 필터 조건")
        _preset = st.session_state.scan_preset
        if 'f_rsi'   not in st.session_state: st.session_state['f_rsi']   = True
        if 'f_vol'   not in st.session_state: st.session_state['f_vol']   = True
        if 'f_macd'  not in st.session_state: st.session_state['f_macd']  = False
        if 'f_bb'    not in st.session_state: st.session_state['f_bb']    = False
        if 'f_align' not in st.session_state: st.session_state['f_align'] = False
        _disabled = _preset != "custom" and _preset is not None
        _fx1, _fx2 = st.columns(2)
        with _fx1:
            st.checkbox("RSI 과매도 (≤35)",      disabled=_disabled, key="f_rsi")
            st.checkbox("거래량 폭발 (≥150%)",   disabled=_disabled, key="f_vol")
            st.checkbox("MACD 골든크로스",        disabled=_disabled, key="f_macd")
        with _fx2:
            st.checkbox("BB 하단 근접 (≤25%)",   disabled=_disabled, key="f_bb")
            st.checkbox("정배열 (MA5>MA20>MA60)", disabled=_disabled, key="f_align")

        st.divider()
        # ── AI 파라미터 자동 최적화 ─────────────────────────────────────────
        st.markdown("##### 🔥 AI 파라미터 자동 최적화 (Walk-Forward)")
        _etf_mode_now = ("국내 ETF" in st.session_state.get("scanner_market", "")
                         or "미국 ETF" in st.session_state.get("scanner_market", ""))
        if _etf_mode_now:
            st.info("ℹ️ ETF 모드에서는 AI 최적화가 적용되지 않습니다. "
                    "개별주(국장 통합/미장 핵심) 선택 시 활성화됩니다.")

    # ── 스캔 설정 — 메인 화면에 3가지만 노출 (Progressive Disclosure) ──
    _SC_OPTS = [
        "🇰🇷 국장 통합 (거래대금 상위 200)",
        "🇺🇸 미장 핵심 (S&P500+나스닥)",
        "🏦 국내 ETF (핵심 테마)",
        "🌐 미국 ETF (글로벌 섹터)",
    ]
    # 이전 8-옵션 값이 session_state에 남아있으면 초기값으로 리셋
    if st.session_state.get("scanner_market") not in _SC_OPTS:
        st.session_state["scanner_market"] = _SC_OPTS[0]

    # ─ 메인 조작부: 시장 선택 + 종목 수 + 스캔 버튼만 ─
    _mc1, _mc2 = st.columns([3, 1])
    with _mc1:
        market_type = st.selectbox(
            "🌏 스캔 대상 시장",
            _SC_OPTS,
            key="scanner_market",
        )
    with _mc2:
        top_n = st.slider("종목 수", 20, 300, 100, key="scanner_topn")

    # ── UI 동기화: 시장 선택에 따라 스캔 모드 자동 고정 ──────────────────
    _market_forces_etf = ("국내 ETF" in market_type or "미국 ETF" in market_type)
    _market_forces_stock = ("국장 통합" in market_type or "미장 핵심" in market_type)
    if _market_forces_etf and st.session_state.get("scan_mode") != "🏦 ETF":
        st.session_state["scan_mode"] = "🏦 ETF"
    elif _market_forces_stock and st.session_state.get("scan_mode") == "🏦 ETF":
        st.session_state["scan_mode"] = "📈 개별주"

    scan_mode = st.radio(
        "스캔 모드",
        ["📈 개별주", "🏦 ETF", "🔀 통합"],
        horizontal=True,
        key="scan_mode",
        disabled=(_market_forces_etf or _market_forces_stock),
        help="시장 선택에 따라 자동 고정됩니다" if (_market_forces_etf or _market_forces_stock) else None,
    )

    # session_state에서 필터값 읽기 (고급 설정 expander가 닫혀있어도 유지됨)
    use_rsi   = st.session_state.get('f_rsi',   True)
    use_vol   = st.session_state.get('f_vol',   True)
    use_macd  = st.session_state.get('f_macd',  False)
    use_bb    = st.session_state.get('f_bb',    False)
    use_align = st.session_state.get('f_align', False)

    _is_us = "미장" in market_type or "미국 ETF" in market_type
    # 시장 전환 시 가격 필터 자동 리셋
    _prev_market = st.session_state.get('_scanner_prev_market', '')
    if _prev_market != market_type:
        st.session_state['f_minp'] = 1 if _is_us else 5000
        st.session_state['f_maxp'] = 100000 if _is_us else 2000000
        st.session_state['_scanner_prev_market'] = market_type
    min_price = st.session_state.get('f_minp', 1 if _is_us else 5000)
    max_price = st.session_state.get('f_maxp', 100000 if _is_us else 2000000)
    use_gemini_scan = st.session_state.get('f_gemini', False)

    # 가격 필터 — key 유지용 (label 숨김, 고급 설정 expander 안에서 관리)
    _hidden_mp = st.number_input(
        f"최소 주가({'$' if _is_us else '원'})",
        value=st.session_state.get('f_minp', 1 if _is_us else 5000),
        step=1 if _is_us else 1000, key="f_minp", label_visibility="collapsed")
    _hidden_mx = st.number_input(
        f"최대 주가({'$' if _is_us else '원'})",
        value=st.session_state.get('f_maxp', 100000 if _is_us else 2000000),
        step=100 if _is_us else 10000, key="f_maxp", label_visibility="collapsed")
    min_price = float(_hidden_mp)
    max_price = float(_hidden_mx)
    use_gemini_scan = st.session_state.get('f_gemini', False)

    # ── 선택 즉시 표시되는 스캔 대상 안내 ──
    _SC_META = {
        "🇰🇷 국장 통합 (거래대금 상위 200)": {
            "cnt": "최대 200종목 (당일 거래대금 상위 동적 추출)",
            "src": "KIS API / pykrx → 내장 KOSPI+KOSDAQ 폴백",
            "eta": "5~8분",
            "color": "#1e40af",
        },
        "🇺🇸 미장 핵심 (S&P500+나스닥)": {
            "cnt": "S&P500 + 나스닥100 병합 (~180종목, 섹터 다양)",
            "src": "yfinance 직접 조회",
            "eta": "5~9분",
            "color": "#065f46",
        },
        "🏦 국내 ETF (핵심 테마)": {
            "cnt": "30개 ETF (반도체·방산·조선·원전·2차전지·헬스케어)",
            "src": "yfinance .KS 경로",
            "eta": "1~2분",
            "color": "#7c2d12",
        },
        "🌐 미국 ETF (글로벌 섹터)": {
            "cnt": "35개 ETF (지수·섹터·채권·방산·원자재)",
            "src": "yfinance 직접 조회",
            "eta": "1~2분",
            "color": "#4a044e",
        },
    }
    _sm = _SC_META.get(market_type, {})
    st.markdown(
        f"<div style='background:rgba(30,64,175,0.07);border-left:4px solid {_sm.get('color','#334155')};"
        f"border-radius:6px;padding:10px 16px;margin:6px 0 10px 0'>"
        f"<span style='font-size:13px;font-weight:700;color:#e2e8f0'>현재 스캔 대상: {_sm.get('cnt','—')}</span>"
        f"<span style='font-size:11px;color:#64748b;margin-left:12px'>데이터: {_sm.get('src','—')} · 예상 시간: {_sm.get('eta','—')}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )
    scan_btn = st.button("🚀 스캔 시작", use_container_width=True, type="primary", key="scan_start_btn")

    if scan_btn:
        st.session_state.passed = []

        # ══════════════════════════════════════════════════════════════════
        # 🛡️ GUARDRAIL: ETF 모드 — AI 최적화 & 프리셋 자동 무력화
        # ETF는 _etf_scorer() 고정 로직으로만 채점. opt_best_cond와
        # 프리셋 필터(f_rsi/f_vol/…)는 _v89_scanner(개별주 전용)에서만
        # 의미가 있으므로, ETF 유니버스 선택 시 강제 바이패스.
        # ══════════════════════════════════════════════════════════════════
        _IS_ETF_UNIVERSE = ("국내 ETF" in market_type or "미국 ETF" in market_type)

        if _IS_ETF_UNIVERSE:
            # 방어 로직 A: AI 최적화 적용 여부 경고 + 무력화
            _opt_applied = st.session_state.get("opt_applied", False)
            _preset_on   = st.session_state.get("scan_preset") not in (None, "custom")
            if _opt_applied or _preset_on:
                st.warning(
                    "⚠️ **ETF 모드 Guardrail 작동** — "
                    "AI 파라미터 최적화(cond5/cond6) 및 전략 프리셋이 "
                    "ETF 스캔에서 자동 무시됩니다. "
                    "ETF는 전용 스코어링(MA200 · RSI 40~65 · 거래량 안정성)으로만 평가됩니다."
                )
            # 방어 로직 A: 해당 세션의 opt/프리셋 변수를 로컬 레벨에서 바이패스
            use_rsi   = False
            use_vol   = False
            use_macd  = False
            use_bb    = False
            use_align = False
            # scan_preset을 None으로 덮어써서 프리셋 게이트가 동작 안 하게 함
            st.session_state['scan_preset'] = None

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
            ("086520","에코프로"),("247540","에코프로비엠"),
            ("196170","알테오젠"),("263750","펄어비스"),
            ("357780","솔브레인"),("058470","리노공업"),("095340","ISC"),
            ("036930","주성엔지니어링"),("039030","이오테크닉스"),("240810","원익IPS"),
            ("035900","JYP엔터테인먼트"),("041510","에스엠"),("067160","아프리카TV"),
            ("214150","클래시스"),("112040","위메이드"),
            ("122870","와이지엔터테인먼트"),("091990","셀트리온헬스케어"),
            # 추가 종목 (KOSPI 상장 종목은 제외 — SK하이닉스·삼성바이오·LG엔솔·현대로템·한미반도체·포스코퓨처엠)
            ("145020","휴젤"),("066970","엘앤에프"),
            ("278280","천보"),
            ("018290","레이"),("039980","폴라리스AI"),
            ("054540","삼영엠텍"),("084370","유진테크"),("115390","락앤락"),
            ("058610","에스씨엔지니어링"),("078340","컴투스"),("060310","3S"),
            ("089790","제이씨케미칼"),("043370","피에이치에이"),("094840","슈프리마"),
            ("053980","에이스테크"),("060250","NHN KCP"),("041960","코미팜"),
            ("108860","셀바스AI"),("950200","소마젠"),("192820","코스맥스"),
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

        # ── NASDAQ 100 내장 리스트 ──
        NASDAQ100_LIST = [
            ("MSFT","Microsoft"),("AAPL","Apple"),("NVDA","NVIDIA"),
            ("AMZN","Amazon"),("META","Meta"),("GOOGL","Alphabet A"),
            ("GOOG","Alphabet C"),("TSLA","Tesla"),("AVGO","Broadcom"),
            ("COST","Costco"),("NFLX","Netflix"),("ASML","ASML"),
            ("AZN","AstraZeneca"),("AMD","AMD"),("CSCO","Cisco"),
            ("ADBE","Adobe"),("QCOM","Qualcomm"),("INTU","Intuit"),
            ("TXN","Texas Instruments"),("AMGN","Amgen"),
            ("ISRG","Intuitive Surgical"),("HON","Honeywell"),
            ("BKNG","Booking Holdings"),("VRTX","Vertex"),("REGN","Regeneron"),
            ("PANW","Palo Alto"),("GILD","Gilead"),("SBUX","Starbucks"),
            ("MU","Micron"),("LRCX","Lam Research"),("KLAC","KLA Corp"),
            ("AMAT","Applied Materials"),("ADI","Analog Devices"),
            ("MRVL","Marvell"),("CDNS","Cadence"),("SNPS","Synopsys"),
            ("CRWD","CrowdStrike"),("FTNT","Fortinet"),("ABNB","Airbnb"),
            ("CEG","Constellation Energy"),("CTAS","Cintas"),
            ("PCAR","Paccar"),("ORLY","O'Reilly Auto"),("FAST","Fastenal"),
            ("ON","ON Semiconductor"),("MELI","MercadoLibre"),("TTD","Trade Desk"),
            ("ZS","Zscaler"),("DXCM","Dexcom"),("FANG","Diamondback Energy"),
            ("KDP","Keurig Dr Pepper"),("MNST","Monster Beverage"),
            ("PAYX","Paychex"),("ODFL","Old Dominion"),("TEAM","Atlassian"),
            ("DASH","DoorDash"),("WDAY","Workday"),("ROP","Roper Tech"),
            ("IDXX","IDEXX"),("GFS","GlobalFoundries"),("ARM","Arm Holdings"),
            ("SHOP","Shopify"),
            ("APP","AppLovin"),("PLTR","Palantir"),("SNOW","Snowflake"),
            ("UBER","Uber"),("COIN","Coinbase"),("NET","Cloudflare"),
            ("DDOG","Datadog"),("HUBS","HubSpot"),("RBLX","Roblox"),
            # 제거: ZM·DOCU·BILL·U·SOFI·AFRM·MSTR·HOOD·IONQ·QBTS·RGTI
            #  (양자컴퓨팅 마이크로캡·비(非)나스닥100 하이프주 — 투기성 과다)
        ]

        # ── 국내 ETF 핵심 테마 리스트 ──
        KR_SECTOR_ETF_LIST = [
            ("091160","KODEX 반도체"),("395160","KODEX AI반도체TOP2+"),
            ("396500","TIGER Fn반도체TOP10"),("457450","KODEX AI테크TOP10"),
            ("381170","TIGER 미국테크TOP10 INDXX"),
            ("463250","TIGER K방산&우주"),("329200","TIGER 방산"),
            ("364980","TIGER 조선TOP10"),("453810","KODEX 조선해양"),
            ("487240","KODEX AI전력핵심설비"),("455890","KODEX 원자력"),
            ("140710","TIGER 원자력테마"),("411060","ACE KRX금현물"),
            ("305720","KODEX 2차전지산업"),("371460","TIGER 2차전지테마"),
            ("143460","TIGER 헬스케어"),("266410","KODEX 바이오"),
            ("227550","TIGER 200 산업재"),
            ("266360","KODEX 200생활소비재"),("157490","TIGER 소비재"),
            ("069500","KODEX 200"),("102110","TIGER 200"),
            ("229200","KODEX 코스닥150"),("261220","KODEX WTI유선물(H)"),
            ("140550","TIGER 금융"),("102970","KODEX 은행"),
            ("357870","TIGER 리츠부동산인프라"),("329750","KODEX 한국부동산리츠인프라"),
        ]

        # ── 국장 통합: 거래대금 상위 200 동적 로드 ──
        KR_TVL200_LIST = []
        if "국장 통합" in market_type:
            _tvl_ph = st.empty()
            _tvl_ph.caption("🔄 코스피+코스닥 거래대금 상위 200 추출 중...")
            try:
                from pykrx import stock as _pk_tvl
                _tvl_end   = datetime.today().strftime('%Y%m%d')
                _tvl_start = (datetime.today() - timedelta(days=5)).strftime('%Y%m%d')
                _tvl_rows  = []
                for _tvl_mkt in ("KOSPI", "KOSDAQ"):
                    _tvl_df = _pk_tvl.get_market_trading_value_by_ticker(
                        _tvl_start, _tvl_end, market=_tvl_mkt
                    )
                    if _tvl_df is None or _tvl_df.empty or _tvl_df.shape[1] == 0:
                        continue
                    _col_map = {c: c.replace(" ", "") for c in _tvl_df.columns}
                    _tvl_df  = _tvl_df.rename(columns=_col_map)
                    _val_col = next((c for c in _tvl_df.columns if "거래대금" in c), None)
                    if _val_col is None:
                        continue
                    for _tk in _tvl_df.index:
                        _val = float(_tvl_df.at[_tk, _val_col])
                        _nm  = _pk_tvl.get_market_ticker_name(str(_tk)) or str(_tk)
                        _tvl_rows.append((str(_tk).zfill(6), _nm, _val))
                if _tvl_rows:
                    _tvl_rows.sort(key=lambda x: x[2], reverse=True)
                    KR_TVL200_LIST = [(t, n) for t, n, _ in _tvl_rows[:200]]
            except Exception:
                pass

            if not KR_TVL200_LIST:
                # pykrx 실패 → 내장 KOSPI+KOSDAQ 폴백
                KR_TVL200_LIST = (KOSPI_LIST + [x for x in KOSDAQ_LIST if x not in KOSPI_LIST])[:200]
                _tvl_ph.warning("⚠️ pykrx 거래대금 조회 실패 → 내장 KOSPI+KOSDAQ 200종목 사용")
            else:
                _tvl_ph.success(f"✅ 거래대금 상위 {len(KR_TVL200_LIST)}종목 추출 완료")

        # ── ETF 유니버스 ──────────────────────────────────────────────────────
        _ETF_UNIVERSE = [
            # 지수 ETF (벤치마크)
            ("VTI",  "Vanguard 전체주식시장"),
            ("SPY",  "SPDR S&P500"),
            ("QQQ",  "Invesco 나스닥100"),
            ("IVV",  "iShares S&P500"),
            ("VOO",  "Vanguard S&P500"),
            ("DIA",  "SPDR 다우존스"),
            ("IWM",  "iShares 러셀2000"),
            # 배당 ETF
            ("JEPQ", "JPMorgan Nasdaq Income"),
            ("JEPI", "JPMorgan Premium Income"),
            ("SCHD", "Schwab 배당주"),
            ("MAIN", "Main Street Capital"),
            ("DIVO", "Amplify 배당성장"),
            ("HDV",  "iShares 고배당"),
            ("VYM",  "Vanguard 고배당수익률"),
            # 채권 ETF
            ("AGG",  "iShares 미국채종합"),
            ("TLT",  "iShares 미국채20년"),
            ("BND",  "Vanguard 채권시장"),
            ("IEF",  "iShares 미국채7-10년"),
            # 섹터 ETF
            ("XLK",  "Technology Select"),
            ("XLV",  "Healthcare Select"),
            ("XLF",  "Financial Select"),
            ("XLE",  "Energy Select"),
            ("XLI",  "Industrials Select"),
            ("SOXX", "iShares 반도체"),
            ("SMH",  "VanEck 반도체"),
            ("ARKK", "ARK 혁신"),
            ("BOTZ", "글로벌 로보틱스AI"),
            # 방산
            ("ITA",  "iShares 방산항공"),
            ("PPA",  "Invesco 방산"),
            # 원자재/금
            ("GLD",  "SPDR 금"),
            ("IAU",  "iShares 금"),
            ("SLV",  "iShares 은"),
            # 레버리지/인버스
            ("TQQQ", "ProShares 나스닥3X"),
            ("SOXL", "Direxion 반도체3X"),
            # 해외 ETF
            ("VEA",  "Vanguard 선진국"),
            ("VWO",  "Vanguard 이머징"),
            ("EEM",  "iShares 이머징"),
        ]
        _ETF_TICKERS_SET = {t for t,_ in _ETF_UNIVERSE}

        # ── ETF 전용 스코어링 함수 ────────────────────────────────────────────
        def _etf_scorer(df_e, ticker_e):
            """안정성(MA200) + 추세(RSI 40~65) + 거래량 안정성 3축 평가"""
            if df_e is None or len(df_e) < 30:
                return False, {}
            _ce = df_e['종가'].astype(float)
            _ve = df_e['거래량'].astype(float)
            _cur_e = float(_ce.iloc[-1])
            _sc_e = 0; _det_e = []

            # 안정성: MA200 상단 위치 (40점)
            _ma200_e = float(_ce.tail(200).mean()) if len(_ce) >= 200 else float(_ce.mean())
            if _cur_e > _ma200_e: _sc_e += 40; _det_e.append(f"MA200상단+40")

            # 추세: RSI 40~65 적정 구간 (30점)
            _dv = _ce.diff(); _gu = _dv.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            _lu = (-_dv.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            _rsi_e = float(100 - 100 / (1 + _gu.iloc[-1] / max(_lu.iloc[-1], 1e-9)))
            if 40 <= _rsi_e <= 65: _sc_e += 30; _det_e.append(f"RSI적정({_rsi_e:.0f})+30")

            # 거래량 안정성 (30점)
            _vol5_e  = float(_ve.tail(5).mean())
            _vol20_e = float(_ve.tail(20).mean())
            _vstab_e = _vol5_e > 0 and (_vol5_e / max(_vol20_e, 1)) >= 0.5
            if _vstab_e: _sc_e += 30; _det_e.append("거래량안정+30")

            # 보너스: 5일 수익률 양수 (+10)
            _cum5_e = ((_cur_e - float(_ce.iloc[-6])) / float(_ce.iloc[-6])) if len(_ce) >= 6 else 0
            if _cum5_e > 0: _sc_e += 10; _det_e.append(f"추세양수+10")

            if _sc_e >= 90:   _grade_e = "🥇 S등급"
            elif _sc_e >= 70: _grade_e = "🎯 A등급"
            elif _sc_e >= 50: _grade_e = "🔎 B등급"
            else:             _grade_e = "Filtered"

            _pass_e = _grade_e in ("🥇 S등급", "🎯 A등급", "🔎 B등급")
            _e_ok = lambda b: "✅" if b else "❌"
            return _pass_e, {
                '등급': _grade_e, '점수': _sc_e,
                'RSI': round(_rsi_e, 1), '5일수익률': round(_cum5_e * 100, 2),
                '거래량비율': round(_vol5_e / max(_vol20_e, 1) * 100, 1),
                '시총(억)': '?', 'CMF': 0, 'ATR비율': 0,
                '조건': (f"[ETF] MA200{_e_ok(_cur_e>_ma200_e)} "
                         f"RSI{_e_ok(40<=_rsi_e<=65)}({_rsi_e:.0f}) "
                         f"Vol{_e_ok(_vstab_e)} [{_sc_e}점] {_grade_e}"),
            }

        # ── 스캔 리스트 구성 (모드 연동) ──────────────────────────────────────
        _scan_mode = st.session_state.get('scan_mode', '📈 개별주')

        # ── 4개 옵션 → 스캔 리스트 매핑 ──
        if "국장 통합" in market_type:
            scan_list = KR_TVL200_LIST[:]
        elif "미장 핵심" in market_type:
            # S&P500 + 나스닥100 병합·중복제거 → 섹터 다양성 확보(산업/에너지/헬스케어/금융 포함)
            _us_seen = set(); scan_list = []
            for _ut, _un in (SP500_LIST + NASDAQ100_LIST):
                if _ut not in _us_seen:
                    _us_seen.add(_ut); scan_list.append((_ut, _un))
        elif "국내 ETF" in market_type:
            scan_list = KR_SECTOR_ETF_LIST[:]
        else:  # 미국 ETF (글로벌 섹터)
            scan_list = _ETF_UNIVERSE[:]

        scan_list    = scan_list[:top_n]
        scan_tickers = [t for t,n in scan_list]
        name_map     = {t:n for t,n in scan_list}

        _mode_label = {"📈 개별주": "개별주", "🏦 ETF": "ETF 전용", "🔀 통합": "개별주+ETF"}.get(_scan_mode, "개별주")
        st.info(f"📋 {_mode_label} {len(scan_tickers)}종목 | 엔진: {'🔥 KIS API (실시간)' if KIS_ENABLED else '📡 yfinance (지연)'}")

        passed = []
        prog   = st.progress(0)
        status = st.empty()

        # ── KIS API 모드 (환경변수 KIS_APP_KEY 설정 시) ──────────────────────
        if KIS_ENABLED and "미장" not in market_type and "미국 ETF" not in market_type:
            try:
                from scanner import run_v89_scan, results_to_df
                status.markdown("<span style='color:#34d399'>🔥 KIS API 비동기 스캔 중...</span>", unsafe_allow_html=True)
                _kis_results = run_v89_scan(
                    tickers   = scan_list,
                    min_price = min_price,
                    max_price = max_price,
                    concurrency = 10,
                )
                prog.empty(); status.empty()
                for _kr in _kis_results:
                    passed.append({
                        'ticker':      _kr.ticker,
                        'name':        _kr.name,
                        '현재가':      _kr.price,
                        '등락(%)':     round(_kr.change_pct, 2),
                        'RSI':         _kr.rsi,
                        'MACD':        '골든크로스' if _kr.macd_cross else '—',
                        'BB위치':      '—',
                        '거래량비율':  _kr.vol_ratio,
                        'ATR비율':     _kr.atr_ratio,
                        '5일수익률':   _kr.cum5_ret,
                        'OBV상승':     '✅' if _kr.foreign_net > 0 else '❌',
                        '시총(억)':    _kr.market_cap_bil,
                        'NXT':         '✅' if _kr.tradable_nxt else '⚠️',
                        '조건':        _kr.cond_detail,
                        'score':       _kr.score if hasattr(_kr, 'score') else 70,
                        '점수':        _kr.score if hasattr(_kr, 'score') else 70,
                        '등급':        _kr.grade  if hasattr(_kr, 'grade')  else '🎯 A등급',
                        'reasons':     _kr.reasons,
                    })
                # KIS 모드에서는 아래 yfinance 루프 건너뜀
                prog.empty(); status.empty()
                passed = sorted(passed, key=lambda x: x['5일수익률'], reverse=True)
                st.session_state.passed = passed
                if not passed:
                    st.warning("⚠️ B등급(50점↑) 이상 종목 없음. (KIS 실시간)")
                else:
                    _ks_s = sum(1 for p in passed if 'S등급' in str(p.get('등급','')))
                    _ks_a = sum(1 for p in passed if 'A등급' in str(p.get('등급','')))
                    _ks_b = sum(1 for p in passed if 'B등급' in str(p.get('등급','')))
                    st.success(f"✅ {len(passed)}개 발굴! 🥇S {_ks_s} · 🎯A {_ks_a} · 🔎B {_ks_b} (KIS 실시간)")
            except Exception as _kis_err:
                st.warning(f"⚠️ KIS API 오류 ({_kis_err}) — yfinance 폴백으로 전환")
                KIS_ENABLED_FALLBACK = False
            else:
                KIS_ENABLED_FALLBACK = True
        else:
            KIS_ENABLED_FALLBACK = False

        # ── yfinance 폴백 스캐너 ──────────────────────────────────────────────
        import yfinance as _yf_scan

        # 하드 필터 상수
        _ETF_KEYWORDS = [
            "KODEX","TIGER","KBSTAR","HANARO","ARIRANG","KOSEF",
            "RISE","ACE","SOL","PLUS","ETF","레버리지","인버스",
            "스팩","SPAC","리츠","REITS","우선주",
        ]
        _BLOCKED_SECTORS = [
            "유통","은행","금융","보험","전력","유틸리티","통신","지주",
            "Banks","Insurance","Financial Services","Electric Utilities",
            "Utilities","Telecom","Telecommunication","Communication Services",
            "Retail","Food & Staples Retailing","Conglomerates","Holding Companies",
        ]
        # 종목명 기반 섹터 블랙리스트 (yfinance sector 누락 보완)
        _BLOCKED_NAME_KEYWORDS = [
            # 지주사
            "지주","홀딩스","홀딩","holding","holdings",
            # 유통
            "리테일","마트","쇼핑","유통","편의점","홈쇼핑","백화점","면세",
            # 은행/금융/보험
            "은행","뱅크","증권","보험","캐피탈","카드","저축","투자","자산운용","신탁",
            # 통신
            "텔레콤","통신","SKT","KT","LGU",
            # 전력/유틸리티
            "한전","발전","전력","가스",
        ]

        def _hard_filter(ticker, name, yf_info, is_us=False):
            """ETF/SPAC/우선주/저변동성 섹터 즉시 차단. True=통과.
            is_us=True면 한국시장 전용 섹터/이름 차단(통신·유통·금융·지주 등)은
            건너뜀 — 미국은 GOOGL/AMZN/META 등이 핵심 타깃이라 섹터 차단 부적절."""
            _name_up = name.upper()
            # 필터1: 종목명 ETF/SPAC 키워드 (양 시장 공통)
            _etf_kw_us = ("ETF","SPAC","ETN","TRUST","FUND")  # 미장은 영문 키워드만
            _kw_list = _etf_kw_us if is_us else _ETF_KEYWORDS
            for kw in _kw_list:
                if kw.upper() in _name_up:
                    return False, f"ETF/SPAC: {kw}"
            if not is_us:
                # 필터1-B: 종목명 섹터 키워드 (한국 전용)
                for kw in _BLOCKED_NAME_KEYWORDS:
                    if kw.upper() in _name_up:
                        return False, f"종목명 섹터차단: {kw}"
                # 필터2: 한국 우선주 코드 패턴 (5번째 자리 = 5)
                if ticker.isdigit() and len(ticker) == 6 and ticker[4] == "5":
                    return False, "우선주 코드 패턴"
            # 필터3: quoteType ETF (양 시장 공통)
            qt = str(yf_info.get("quoteType","") or "").upper()
            if qt in ("ETF","MUTUALFUND","FUTURE","INDEX"):
                return False, f"quoteType={qt}"
            # 필터4: 시총 0/None (yfinance .info 누락 잦음 → 미장은 통과시킴)
            mktcap = yf_info.get("marketCap", None)
            if not is_us and (mktcap is None or mktcap == 0):
                return False, "시총 0/None"
            # 필터5: 금지 섹터 — 한국 전용 (미장은 섹터 차단 안 함)
            if not is_us:
                combined = (str(yf_info.get("sector","") or "") + " " +
                            str(yf_info.get("industry","") or ""))
                for blk in _BLOCKED_SECTORS:
                    if blk.lower() in combined.lower():
                        return False, f"금지섹터: {blk}"
            return True, ""

        def _v89_scanner(df, ticker):
            """
            V8.9.4 하이브리드 스코어링 스캐너
            하드필터: C1(시총) + C2(ATR) — 필수 AND
            스코어링: C3(재무 20점) + C4(수급 30점) + C5(모멘텀 25점) + C6(눌림목 25점)
            판정: 70점↑ Target_Locked / 90점↑ A-Grade 주도주
            """
            if df is None or len(df) < 22:
                return False, {}

            c  = df['종가'].astype(float)
            h  = df['고가'].astype(float)
            l  = df['저가'].astype(float)
            v  = df['거래량'].astype(float)

            cur   = float(c.iloc[-1])
            vol_t = float(v.iloc[-1])

            # ATR14 (Wilder EWM)
            tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
            atr14 = float(tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1])

            # 최근 20일 최대 거래량 (당일 제외)
            max_vol_20 = float(v.iloc[:-1].rolling(20).max().iloc[-1]) if len(v) >= 21 else float(v.rolling(20).max().iloc[-1])

            # 5거래일 누적 수익률
            cum5 = (cur - float(c.iloc[-6])) / float(c.iloc[-6]) if len(c) >= 6 else 0

            # CMF20 (수급 대체 지표 — KIS 없을 때)
            hl_range = (h - l).replace(0, np.nan)
            mfm  = ((c - l) - (h - c)) / hl_range
            cmf20 = float((mfm * v).rolling(20).sum().iloc[-1] / v.rolling(20).sum().iloc[-1]) if v.rolling(20).sum().iloc[-1] > 0 else 0.0

            # yfinance 시총·재무 조회
            mktcap_b = None; _mktcap_usd = None; op_income = None; rev_g = None
            _is_kr   = is_korean_ticker(ticker)
            _yf_info = {}
            try:
                for _sfx in ([".KS", ".KQ"] if _is_kr else [""]):
                    try:
                        _tmp = _yf_scan.Ticker(ticker + _sfx).info
                        if _tmp and _tmp.get("regularMarketPrice"):
                            _yf_info = _tmp; break
                    except Exception:
                        continue
                _mc_raw   = _yf_info.get('marketCap', 0)
                mktcap_b  = _mc_raw / 1e8 if _mc_raw else None   # 한국: 억원 단위
                _mktcap_usd = float(_mc_raw) if _mc_raw else None  # 미국: USD 원값
                op_income = _yf_info.get('operatingIncome', None)
                rev_g     = _yf_info.get('revenueGrowth', None)
            except Exception:
                pass

            # ── 블랙리스트: 영구 배제 종목 ──
            _BLACKLIST = ['002790']  # 아모레퍼시픽(지주사 - API 오분류)
            if ticker in _BLACKLIST:
                return False, {'조건': f'블랙리스트: {ticker}', '점수': 0, '등급': 'Filtered'}

            # ── 하드 필터: ETF/SPAC/섹터 즉시 차단 ──
            _hf_ok, _hf_reason = _hard_filter(ticker, name, _yf_info, is_us=(not _is_kr))
            if not _hf_ok:
                return False, {'조건': f'하드필터: {_hf_reason}', '점수': 0, '등급': 'Filtered'}

            # ── 하드 필터: C1 시총 / C2 ATR ──
            if _is_kr:
                # 한국: 5,000억 ~ 3조원 (중형~대형)
                c1_pass = (5000 <= mktcap_b <= 30000) if mktcap_b is not None else True
            else:
                # 미국: USD 기준 — 초소형만 배제(≥$2B), 상한 없음(메가캡도 통과)
                c1_pass = (_mktcap_usd >= 2e9) if _mktcap_usd is not None else True
            c2_pass = (atr14 / cur) >= 0.035 if cur > 0 else False
            hard_pass = c1_pass and c2_pass

            # ── 대형주 여부 판정 (시총 1조=10,000억 이상 or KOSPI200 편입) ──
            _KOSPI200 = {
                '005930','000660','005380','005490','035420','000270','105560','055550',
                '012330','051910','006400','207940','068270','035720','003550','323410',
                '034730','086790','028260','011200','009830','010130','032830','017670',
                '066570','011070','003490','024110','018260','030200','090430','096770',
                '010950','011780','009150','000810','033780','329180','012450','247540',
                '373220','003670','091990','316140','267250','042700','000100','402340',
            }
            if _is_kr:
                _is_large_cap = (
                    (mktcap_b is not None and mktcap_b >= 10_000)   # 1조원↑
                    or (ticker in _KOSPI200)
                )
            else:
                _is_large_cap = (_mktcap_usd is not None and _mktcap_usd >= 5e10)  # $50B↑


            # ── 갭/이격 계산 (과열 방지용) ──
            _open_t   = float(df['시가'].iloc[-1])
            _prev_cl  = float(df['종가'].iloc[-2]) if len(df) >= 2 else _open_t
            _gap_pct  = (_open_t - _prev_cl) / _prev_cl * 100 if _prev_cl > 0 else 0
            _ma5_val  = df['종가'].iloc[-5:].mean() if len(df) >= 5 else cur
            _ma5_diff = (cur - _ma5_val) / _ma5_val * 100 if _ma5_val > 0 else 0
            _overheat = (_gap_pct >= 3.0) or (abs(_ma5_diff) >= 3.0)

            # ── 기술 지표 (항상 계산 — 보너스 스코어링 + 프리셋 게이트 공용) ──
            _cl = df['종가'].astype(float)
            _d_rsi = _cl.diff()
            _g_rsi = _d_rsi.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            _l_rsi = (-_d_rsi.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            _rsi_val = float(100 - 100 / (1 + _g_rsi.iloc[-1] / max(_l_rsi.iloc[-1], 1e-9)))
            _vr_pct  = vol_t / max_vol_20 * 100 if max_vol_20 > 0 else 0
            _m12 = _cl.ewm(span=12, adjust=False).mean()
            _m26 = _cl.ewm(span=26, adjust=False).mean()
            _mc  = _m12 - _m26; _sg = _mc.ewm(span=9, adjust=False).mean()
            _macd_gc = bool(len(_mc) >= 2 and _mc.iloc[-1] > _sg.iloc[-1] and _mc.iloc[-2] <= _sg.iloc[-2])
            _bb_m = _cl.rolling(20).mean().iloc[-1]; _bb_s = _cl.rolling(20).std().iloc[-1]
            _bb_pos = (cur - (_bb_m - 2*_bb_s)) / (4*_bb_s + 1e-9) * 100
            _ma5_g  = float(_cl.tail(5).mean())
            _ma20_g = float(_cl.tail(20).mean())
            _ma60_g = float(_cl.tail(60).mean()) if len(_cl) >= 60 else _ma20_g
            _ma_align = _ma5_g > _ma20_g > _ma60_g

            # ── 스코어링 ──
            score = 0; score_detail = []

            # C3: 재무 20점
            c3_ok = False
            if op_income is not None or rev_g is not None:
                c3_ok = ((op_income is not None and op_income > 0) or
                         (rev_g is not None and rev_g >= 0.20))
            if c3_ok: score += 20; score_detail.append("재무+20")

            # C4: 수급 30점 — KIS 없으면 CMF20으로 대체
            c4_ok = (cmf20 > 0)
            if c4_ok: score += 30; score_detail.append("수급+30")

            # C5: 모멘텀 25점
            _p_c5 = st.session_state.get("opt_best_cond5", 0.08)
            c5_ok = cum5 >= _p_c5
            if c5_ok: score += 25; score_detail.append("모멘텀+25")

            # C6: 눌림목 25점
            _p_c6 = st.session_state.get("opt_best_cond6", 0.50)
            c6_ok = (vol_t < max_vol_20 * _p_c6) if max_vol_20 > 0 else False
            if c6_ok: score += 25; score_detail.append("눌림목+25")

            # ── 보너스 점수 (OR 로직 보강: RSI/거래량/MACD 중 해당 시 가점) ──
            _bonus = 0; _bonus_tags = []
            if _rsi_val <= 35:  _bonus += 10; _bonus_tags.append(f"RSI과매도({_rsi_val:.0f}↓)+10")
            if _vr_pct >= 200:  _bonus += 10; _bonus_tags.append(f"거래량폭증({_vr_pct:.0f}%)+10")
            if _macd_gc:        _bonus += 10; _bonus_tags.append("MACD골든크로스+10")

            # ── 레짐 기반 보너스 (약세장=방어수급 가점 / 강세장=추세 가점) ──
            _regime = st.session_state.get('_market_regime', 'neutral')
            if _regime == 'bear' and c4_ok:   _bonus += 5; _bonus_tags.append("방어수급+5(약세장)")
            if _regime == 'bull' and c5_ok:   _bonus += 5; _bonus_tags.append("추세모멘텀+5(강세장)")

            score = min(score + _bonus, 110)

            # ── 프리셋 게이트: OR 로직 (선택 조건 중 1개 이상 충족하면 통과) ──
            _active_preset = st.session_state.get('scan_preset')
            if _active_preset and _active_preset != 'custom':
                _preset_checks = []
                if use_rsi:   _preset_checks.append(_rsi_val <= 35)
                if use_vol:   _preset_checks.append(_vr_pct >= 150)
                if use_macd:  _preset_checks.append(_macd_gc)
                if use_bb:    _preset_checks.append(_bb_pos <= 25)
                if use_align: _preset_checks.append(_ma_align)
                if _preset_checks and not any(_preset_checks):
                    return False, {'조건': '프리셋 조건 미충족(OR)', '점수': score, '등급': 'Filtered'}

            # ── S/A/B 3단계 등급 판정 ──
            all6_pass = c1_pass and c2_pass and c3_ok and c4_ok and c5_ok and c6_ok
            _large_cap_pass = (
                _is_large_cap and c1_pass and c3_ok and c4_ok and not _overheat
            )

            if _overheat:
                grade = "🔥 과열차단"
            elif (all6_pass or _large_cap_pass) and score >= 90:
                grade = "🥇 S등급"        # 확신도 높음 — 핵심 조건 100% + 90점↑
            elif (all6_pass or _large_cap_pass or hard_pass) and score >= 70:
                grade = "🎯 A등급"        # 관심 종목 — 주요 지표 2개↑ 충족
            elif score >= 50:
                grade = "🔎 B등급"        # 정찰병 — 추세 전환 가능성 포착
            else:
                grade = "Filtered"

            passed = grade in ("🥇 S등급", "🎯 A등급", "🔎 B등급")

            def _e(b): return "✅" if b else "❌"
            _lc_tag = " 🏦대형주특례" if (_large_cap_pass and not all6_pass and not _overheat) else ""
            _oh_tag = " 🔥과열" if _overheat else ""
            _rg_tag = f" [{_regime.upper()}장]" if _regime != 'neutral' else ""
            _bonus_str = (" | " + " ".join(_bonus_tags)) if _bonus_tags else ""
            meta = {
                'ATR비율':    round(atr14 / cur * 100, 2) if cur > 0 else 0,
                '5일수익률':  round(cum5 * 100, 2),
                '거래량비율': round(_vr_pct, 1),
                '시총(억)':   round(mktcap_b) if mktcap_b else '?',
                'CMF':        round(cmf20, 3),
                '갭(%)':      round(_gap_pct, 2),
                'MA5이격(%)': round(_ma5_diff, 2),
                'RSI':        round(_rsi_val, 1),
                '점수':       score,
                '등급':       grade,
                '조건': (f"C1{_e(c1_pass)} C2{_e(c2_pass)} "
                         f"C3{_e(c3_ok)} C4{_e(c4_ok)} C5{_e(c5_ok)} C6{_e(c6_ok)} "
                         f"[{score}점] {grade}{_lc_tag}{_oh_tag}{_rg_tag}{_bonus_str}"),
            }
            return passed, meta

        # ── 시장 레짐 감지 — 스캔 시장에 맞는 지수 사용 ───────────────────────
        # 미장 스캔이면 나스닥(^IXIC), 그 외(국장)는 코스피(^KS11)
        _reg_idx = "^IXIC" if ("미장" in market_type) else "^KS11"
        try:
            import yfinance as _yf_reg
            _reg_df = _yf_reg.Ticker(_reg_idx).history(period="2mo", interval="1d")
            if _reg_df is not None and len(_reg_df) >= 20:
                _reg_c = _reg_df['Close']
                _reg_ma5  = float(_reg_c.tail(5).mean())
                _reg_ma20 = float(_reg_c.tail(20).mean())
                _reg_slope = (_reg_c.iloc[-1] - _reg_c.iloc[-5]) / max(_reg_c.iloc[-5], 1)
                if _reg_ma5 > _reg_ma20 * 1.005 and _reg_slope > 0:
                    st.session_state['_market_regime'] = 'bull'
                elif _reg_ma5 < _reg_ma20 * 0.995 and _reg_slope < 0:
                    st.session_state['_market_regime'] = 'bear'
                else:
                    st.session_state['_market_regime'] = 'neutral'
        except Exception:
            st.session_state.setdefault('_market_regime', 'neutral')
        _regime_now = st.session_state.get('_market_regime', 'neutral')
        _regime_labels = {'bull': '📈 강세장', 'bear': '📉 약세장', 'neutral': '➡️ 중립'}
        _regime_colors = {'bull': '#166534', 'bear': '#991B1B', 'neutral': '#64748b'}
        _rc = _regime_colors.get(_regime_now, '#64748b')
        _rl = _regime_labels.get(_regime_now, '중립')
        status.markdown(
            f"<span style='font-size:11px;color:{_rc}'>시장 레짐: {_rl} — 스캐너 자동 조정 완료</span>",
            unsafe_allow_html=True
        )

        # ── Rate Limit 방어 상수 ──
        # yfinance는 60초당 ~2,000 req 허용, 100종목 이상 시 미세 슬립으로 Ban 방지
        import time as _rl_time
        _IS_US_MARKET    = "미장" in market_type or "미국 ETF" in market_type
        _IS_KR_ETF_SCAN  = "국내 ETF" in market_type
        _RL_SLEEP_BASE   = 0.08   # 기본 80ms (국내 KIS/pykrx 세션)
        _RL_SLEEP_US     = 0.15   # 미국 yfinance 직접 호출 시 150ms
        _RL_BURST_EVERY  = 25     # 25종목마다 추가 슬립
        _RL_BURST_SLEEP  = 1.5    # 추가 1.5초 (yfinance 버스트 리셋)
        _rl_err_streak   = 0      # 연속 에러 카운터

        _scan_fatal = None
        try:
            for idx, ticker in enumerate(scan_tickers):
                prog.progress((idx+1)/len(scan_tickers))
                name = name_map.get(ticker, ticker)
                status.markdown(f"<span style='font-size:12px;color:#64748b'>V9.1 스캔 중: {name} ({idx+1}/{len(scan_tickers)})</span>", unsafe_allow_html=True)

                # ── Rate Limit 방어 슬립 ──
                if _IS_US_MARKET or _IS_KR_ETF_SCAN:
                    _rl_time.sleep(_RL_SLEEP_US)
                else:
                    _rl_time.sleep(_RL_SLEEP_BASE)
                if idx > 0 and idx % _RL_BURST_EVERY == 0:
                    _rl_time.sleep(_RL_BURST_SLEEP)
                # 연속 에러 5회 → 3초 강제 휴식 (Ban 직전 쿨다운)
                if _rl_err_streak >= 5:
                    _rl_time.sleep(3.0)
                    _rl_err_streak = 0

                try:
                    if "미장" in market_type:
                        import yfinance as yf
                        _yt   = yf.Ticker(ticker)
                        _hist = _yt.history(period="6mo", interval="1d")
                        if _hist is None or _hist.empty:
                            _rl_err_streak += 1; continue
                        df = _hist.rename(columns={'Open':'시가','High':'고가','Low':'저가','Close':'종가','Volume':'거래량'})[['시가','고가','저가','종가','거래량']].tail(60)
                        df = df[df['거래량']>0]
                    elif _IS_KR_ETF_SCAN:
                        # 국내 ETF는 yfinance .KS 경로로 조회
                        import yfinance as yf
                        _yt   = yf.Ticker(f"{ticker}.KS")
                        _hist = _yt.history(period="6mo", interval="1d")
                        if _hist is None or _hist.empty:
                            _rl_err_streak += 1; continue
                        df = _hist.rename(columns={'Open':'시가','High':'고가','Low':'저가','Close':'종가','Volume':'거래량'})[['시가','고가','저가','종가','거래량']].tail(60)
                        df = df[df['거래량']>0]
                    else:
                        df = fetch_ohlcv(ticker, 60)
                    if df is None or len(df) < 22: continue

                    _price = float(df['종가'].iloc[-1])
                    if _price < min_price or _price > max_price: continue

                    # 이중 Guardrail: UI 라디오보다 실제 티커 소속이 우선
                    # _IS_ETF_UNIVERSE(시장 드롭다운) 또는 ETF_TICKERS_SET 소속일 때만 ETF 엔진 사용
                    # 개별주 유니버스에서 '🏦 ETF' 라디오를 선택해도 실제 ETF 티커가 아니면 차단
                    _ticker_is_real_etf = ticker in _ETF_TICKERS_SET
                    _is_etf = _IS_ETF_UNIVERSE or _ticker_is_real_etf
                    # 라디오가 ETF인데 실제 ETF 티커가 아닌 경우 → 개별주 엔진으로 강제 전환
                    if '🏦 ETF' in _scan_mode and not _ticker_is_real_etf and not _IS_ETF_UNIVERSE:
                        _is_etf = False  # 개별주 엔진으로 분기
                    if _is_etf:
                        _ok, _meta = _etf_scorer(df, ticker)
                    else:
                        _ok, _meta = _v89_scanner(df, ticker)
                    if not _ok:
                        continue

                    df = calc_indicators(df)
                    l = df.iloc[-1]; p = df.iloc[-2]
                    chg = (l['종가']/p['종가']-1)*100

                    passed.append({
                        'ticker':    ticker,
                        'name':      name,
                        '현재가':    l['종가'],
                        '등락(%)':   round(chg, 2),
                        'RSI':       l['RSI'],
                        'MACD':      '골든크로스' if (l['MACD']>l['Signal'] and p['MACD']<=p['Signal']) else ('▲' if l['MACD']>l['Signal'] else '▼'),
                        'BB위치':    f"{round((l['종가']-l['BB_lower'])/(l['BB_upper']-l['BB_lower'])*100,1) if (l['BB_upper']-l['BB_lower'])>0 else 50}%",
                        '거래량비율': _meta['거래량비율'],
                        'ATR비율':   _meta['ATR비율'],
                        '5일수익률': _meta['5일수익률'],
                        'CMF':       _meta.get('CMF', 0),
                        '시총(억)':  _meta['시총(억)'],
                        '점수':      _meta.get('점수', 0),
                        '등급':      _meta.get('등급', ''),
                        '조건':      _meta['조건'],
                        'score':     _meta.get('점수', 0),
                        'reasons':   [f"📐ATR {_meta['ATR비율']}%", f"📈5일 {_meta['5일수익률']}%",
                                      f"📉거래량 {_meta['거래량비율']}%", f"CMF {_meta.get('CMF', 0):.3f}"],
                    })
                    _rl_err_streak = 0  # 성공 시 에러 streak 리셋
                except Exception as _scan_e:
                    _rl_err_streak += 1
                    st.session_state.setdefault('_scan_errors', []).append(f"{ticker}: {_scan_e}")
                    continue
        except Exception as _fatal_e:
            _scan_fatal = _fatal_e

        # yfinance 폴백 결과 저장
        prog.empty(); status.empty()
        if _scan_fatal is not None:
            st.error(
                f"🚨 실시간 데이터 조회 실패: 장외 시간 혹은 서버 지연\n\n"
                f"오류 내용: `{type(_scan_fatal).__name__}: {_scan_fatal}`\n\n"
                "장중(09:00~15:30 KST / 미국 시장 시간) 이후 다시 시도하거나, "
                "잠시 후 [🚀 스캔 시작] 버튼을 다시 눌러주세요."
            )
            st.stop()
        # 약세장에서는 ETF를 결과 상단으로 배치 (방어 포트폴리오 유도)
        _regime_sort = st.session_state.get('_market_regime', 'neutral')
        if _regime_sort == 'bear':
            _etf_res  = [x for x in passed if x['ticker'] in _ETF_TICKERS_SET]
            _stk_res  = [x for x in passed if x['ticker'] not in _ETF_TICKERS_SET]
            _etf_res  = sorted(_etf_res, key=lambda x: x.get('점수', 0), reverse=True)
            _stk_res  = sorted(_stk_res, key=lambda x: x.get('점수', 0), reverse=True)
            passed = _etf_res + _stk_res
        else:
            passed = sorted(passed, key=lambda x: x.get('점수', 0), reverse=True)
        st.session_state.passed = passed
        # (스캔 결과 자동 저장 폐지 — 스캔할 때마다 상위 10종목이 분석기록에 쌓여
        #  '검색 안 한 기록'이 생기던 문제. 기록은 사용자가 명시적으로 저장할 때만.)
        _errs = st.session_state.pop('_scan_errors', [])
        if _errs:
            with st.expander(f"⚠️ 스캔 중 오류 {len(_errs)}건 (데이터 없음 / API 오류)", expanded=False):
                for _em in _errs[:20]:
                    st.caption(_em)
        _s_cnt  = sum(1 for p in passed if 'S등급' in str(p.get('등급','')))
        _a_cnt  = sum(1 for p in passed if 'A등급' in str(p.get('등급','')))
        _b_cnt  = sum(1 for p in passed if 'B등급' in str(p.get('등급','')))
        if not passed:
            _errs_empty = st.session_state.get('_scan_errors', [])
            if _errs_empty:
                st.error(
                    "📡 실시간 데이터 조회 실패: 장외 시간이거나 서버 지연이 발생했습니다.\n\n"
                    f"오류 {len(_errs_empty)}건 발생 — 잠시 후 다시 스캔하거나, 장중(09:00~15:30) 시간에 시도해 주세요."
                )
                with st.expander("🔍 오류 상세 보기", expanded=False):
                    for _em in _errs_empty[:10]:
                        st.caption(_em)
            else:
                st.warning(
                    "🔍 조건을 충족하는 종목이 없습니다.\n\n"
                    "필터 조건을 완화하거나 다른 프리셋을 선택해 보세요."
                )
        else:
            _sc1, _sc2 = st.columns([4, 1])
            _sc1.success(f"✅ {len(passed)}개 발굴! 🥇S등급 {_s_cnt}개 / 🎯A등급 {_a_cnt}개 / 🔎B등급 {_b_cnt}개")
            try:
                _dl_df = pd.DataFrame([{k: v for k, v in p.items() if k not in ('reasons',)} for p in passed])
                _sc2.download_button("📥 CSV", _dl_df.to_csv(index=False, encoding='utf-8-sig'),
                                     file_name=f"scan_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                                     mime="text/csv", use_container_width=True)
            except Exception:
                pass

    # ── VTI 벤치마크 배너 ─────────────────────────────────────────────────────
    try:
        import yfinance as _yf_vti
        _vti_h = _yf_vti.Ticker("VTI").history(period="1mo", interval="1d")
        if _vti_h is not None and len(_vti_h) >= 2:
            _vti_now  = float(_vti_h['Close'].iloc[-1])
            _vti_prev = float(_vti_h['Close'].iloc[0])
            _vti_ret  = (_vti_now / _vti_prev - 1) * 100
            _vti_5d   = (_vti_now / float(_vti_h['Close'].iloc[-6]) - 1) * 100 if len(_vti_h) >= 6 else 0
            _vti_c    = "#166534" if _vti_ret >= 0 else "#991B1B"
            _vti_arr  = "▲" if _vti_ret >= 0 else "▼"
            st.markdown(
                f"<div style='background:#0f172a;border:1px solid #1e3a5f;border-radius:10px;"
                f"padding:10px 16px;display:flex;align-items:center;gap:16px;margin-bottom:8px'>"
                f"<span style='font-size:11px;color:#64748b;font-weight:600'>📊 VTI 벤치마크</span>"
                f"<span style='font-size:14px;font-weight:800;color:#f0f4ff'>${_vti_now:.2f}</span>"
                f"<span style='color:{_vti_c};font-size:12px'>{_vti_arr}{abs(_vti_ret):.2f}% (1개월)</span>"
                f"<span style='color:{('#166534' if _vti_5d>=0 else '#991B1B')};font-size:12px'>"
                f"{('▲' if _vti_5d>=0 else '▼')}{abs(_vti_5d):.2f}% (5일)</span>"
                f"<span style='font-size:10px;color:#475569;margin-left:auto'>"
                f"{'📈 시장 강세 — 알파 전략 유효' if _vti_ret >= 0 else '📉 시장 약세 — ETF 방어 고려'}</span>"
                f"</div>",
                unsafe_allow_html=True
            )
    except Exception:
        pass

    # ── 결과 표시 ──
    if not st.session_state.get('passed'):
        st.info("💡 스캔 버튼을 눌러 오늘의 매수 후보를 발굴하세요.")
    elif not st.session_state.passed:
        st.warning("⚠️ B등급(50점↑) 이상 종목 없음. 시장 레짐 확인 후 다른 시장대를 시도하세요.")
    if st.session_state.get('passed'):
        _sc_ids = [t for t, _ in get_watchlist_tickers()]
        _p_list = st.session_state.passed

        # CSV 다운로드 (영구 패널 — rerun 후에도 유지)
        try:
            _dl_all = pd.DataFrame([{k: v for k, v in p.items() if k != 'reasons'} for p in _p_list])
            st.download_button("📥 스캔 결과 CSV 다운로드",
                               _dl_all.to_csv(index=False, encoding='utf-8-sig'),
                               file_name="scan_result.csv", mime="text/csv",
                               key="scan_csv_persist", use_container_width=True)
        except Exception:
            pass

        _s_c = sum(1 for _x in _p_list if 'S등급' in str(_x.get('등급','')))
        _a_c = sum(1 for _x in _p_list if 'A등급' in str(_x.get('등급','')))
        _b_c = sum(1 for _x in _p_list if 'B등급' in str(_x.get('등급','')))

        # ══════════════════════════════════════════════════════
        # 🎯 액션 브리핑 패널 — 매수 가능 종목 즉각 표시
        # ══════════════════════════════════════════════════════
        # '3일 연속 & S/A등급' 교집합 — 스캐너 streak_map 참조
        _streak_now = st.session_state.get('pension_streak_map', {})
        _action_cnt = sum(
            1 for _x in _p_list
            if _streak_now.get(str(_x['ticker']), 1) >= 3
            and ('S등급' in str(_x.get('등급','')) or 'A등급' in str(_x.get('등급','')))
        )
        _sa_cnt = _s_c + _a_c

        if _action_cnt > 0:
            _brief_bg    = "rgba(52,211,153,0.10)"
            _brief_border = "rgba(52,211,153,0.50)"
            _brief_color  = "#34d399"
            _brief_icon   = "🔥"
            _brief_msg    = f"오늘 사격 가능 (🟢 3일연속 & S/A등급): <b style='font-size:22px;color:#34d399'>{_action_cnt}개</b>"
        else:
            _brief_bg    = "rgba(148,163,184,0.06)"
            _brief_border = "rgba(148,163,184,0.25)"
            _brief_color  = "#64748b"
            _brief_icon   = "📋"
            _brief_msg    = f"사격 대기 중 (3일연속 & S/A등급 0개) — S/A합계 {_sa_cnt}개, 내일 재확인"

        st.markdown(
            f"<div style='background:{_brief_bg};border:2px solid {_brief_border};"
            f"border-radius:14px;padding:16px 22px;margin:0 0 14px 0'>"
            f"<div style='font-size:13px;font-weight:800;color:{_brief_color}'>"
            f"{_brief_icon} {_brief_msg}</div>"
            f"<div style='font-size:11px;color:#64748b;margin-top:6px'>"
            f"총 발굴: {len(_p_list)}개 &nbsp;|&nbsp; "
            f"🥇S등급 {_s_c}개 &nbsp;·&nbsp; 🎯A등급 {_a_c}개 &nbsp;·&nbsp; 🔎B등급 {_b_c}개</div>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # 전체 추가 버튼
        _new_items = [i for i in _p_list if i['ticker'] not in _sc_ids]
        if _new_items:
            if st.button(f"⭐ 전체 {len(_new_items)}개 관심종목 추가", key="bulk_add_btn",
                         use_container_width=True, type="primary"):
                _added_cnt = sum(1 for _it in _new_items if add_ticker(_it['ticker'], _it['name']))
                if _added_cnt:
                    st.success(f"✅ {_added_cnt}개 추가 완료!")
                    st.rerun()
                else:
                    st.warning("모두 이미 등록된 종목입니다.")

        st.divider()

        # ══════════════════════════════════════════════════════
        # 📊 핵심 5컬럼 압축 메인 테이블 (Pandas Styler 적용)
        # ══════════════════════════════════════════════════════
        _streak_now = st.session_state.get('pension_streak_map', {})

        def _streak_badge(tk):
            s = _streak_now.get(str(tk), 1)
            return "🟢 3일연속" if s >= 3 else "🟡 2일연속" if s == 2 else "⚪ 1일"

        _display_rows = []
        for _x in _p_list:
            _tk = _x['ticker']
            _chg = _x.get('등락(%)', 0)
            _chg_str = f"{'▲' if _chg>0 else '▼'}{abs(_chg):.1f}%"
            _display_rows.append({
                '종목명':   f"{_x['name']} ({_tk})",
                '현재가':   f"{_x.get('현재가',0):,.0f}",
                '등락률':   _chg_str,
                '연속등장': _streak_badge(_tk),
                '등급':     _x.get('등급', ''),
                # 스타일 판단용 내부 키 (표시 안 됨)
                '_grade':   _x.get('등급', ''),
                '_streak':  _streak_now.get(str(_tk), 1),
                '_chg':     _chg,
            })

        _disp_df = pd.DataFrame(_display_rows)

        def _row_style(row):
            g  = row.get('_grade', '')
            sk = row.get('_streak', 1)
            # 강조: S등급 or 3일연속 → 연한 형광 녹색
            if 'S등급' in str(g) or sk >= 3:
                return ['background-color:rgba(52,211,153,0.10);color:#d1fae5']*len(row)
            # 축소: B등급 or 1일 → 회색 dim
            if 'B등급' in str(g) or sk == 1:
                return ['color:#475569']*len(row)
            return ['']*len(row)

        _visible_cols = ['종목명', '현재가', '등락률', '연속등장', '등급']
        # ── 좌: 종목 테이블 / 우: 선택 종목 상세 카드 + 퀵 매매 ──
        _scan_black = not run_v891_system_check().get('can_enter', True)
        _tbl_col, _det_col = st.columns([4, 6])   # 좌 테이블 : 우 상세+차트 = 4:6
        _sel_idx = None
        with _tbl_col:
            _styled = _disp_df[_visible_cols + ['_grade', '_streak', '_chg']].style.apply(_row_style, axis=1)
            # on_select 지원 버전이면 행 클릭, 아니면 selectbox 폴백
            _click_ok = False   # 사전 초기화 (TypeError 외 예외에도 NameError 방지)
            try:
                _evt = st.dataframe(
                    _styled, use_container_width=True, hide_index=True,
                    column_order=_visible_cols, key="scan_result_tbl",
                    on_select="rerun", selection_mode="single-row",
                )
                _rows = getattr(getattr(_evt, "selection", None), "rows", None) or \
                        (_evt.get("selection", {}).get("rows", []) if isinstance(_evt, dict) else [])
                if _rows:
                    _sel_idx = _rows[0]
                _click_ok = True
            except Exception:
                # on_select 미지원(TypeError) 또는 기타 예외 → 정적 테이블 + selectbox 폴백
                st.dataframe(_styled, use_container_width=True, hide_index=True, column_order=_visible_cols)
                _click_ok = False
        with _det_col:
            _det_opts = {f"{_x['name']} ({_x['ticker']})": _x for _x in _p_list}
            if _click_ok:
                st.caption("👈 좌측 테이블에서 종목을 **클릭**하면 상세가 갱신됩니다.")
                if _sel_idx is not None and 0 <= _sel_idx < len(_p_list):
                    _sx = _p_list[_sel_idx]
                else:
                    _sx = _p_list[0]   # 미선택 시 1위 종목
            else:
                _det_lbl = st.selectbox("🎯 상세 볼 종목", list(_det_opts.keys()), key="scan_detail_sel")
                _sx = _det_opts.get(_det_lbl, {})
            _sx_tk = _sx.get('ticker', '')
            _sx_kr = is_korean_ticker(_sx_tk) if _sx_tk else True
            _u = '원' if _sx_kr else '$'
            _cur = float(_sx.get('현재가', 0) or 0)
            # 블랙아웃 오버레이 (신규 진입 실수 방지)
            if _scan_black:
                st.markdown(
                    "<div style='background:#2a0505;border:2px solid #ef4444;border-radius:10px;"
                    "padding:10px 14px;margin-bottom:8px;text-align:center;font-weight:900;"
                    "color:#ef4444;font-size:14px'>🚫 시장 셧다운 — 신규 진입 불가</div>",
                    unsafe_allow_html=True)
            # 상세 지표 카드 (핵심만)
            _d1, _d2 = st.columns(2)
            _d1.metric("현재가", f"{_cur:,.0f}{_u}", delta=f"{_sx.get('등락(%)',0):+.1f}%",
                       delta_color=("normal" if _sx.get('등락(%)',0) >= 0 else "inverse"))
            _d2.metric("종합점수", f"{_sx.get('점수', _sx.get('score',0))}점", delta=_sx.get('등급',''))
            _d3, _d4 = st.columns(2)
            _d3.metric("RSI", f"{_sx.get('RSI','-')}")
            _d4.metric("거래량비율", f"{_sx.get('거래량비율','-')}%")
            _d5, _d6 = st.columns(2)
            _d5.metric("수급(CMF)", f"{_sx.get('CMF','-')}")
            _d6.metric("5일수익률", f"{_sx.get('5일수익률','-')}%")
            # ── 5AI 정밀 타점 (지지/저항 기반 calc_entry_point) ──
            _ep_sx = None
            try:
                _sdf = st.session_state.get('all_data_cache', {}).get(_sx_tk, {}).get('df')
                if _sdf is None and _sx_tk:
                    _raw_sx = fetch_ohlcv(_sx_tk, 80)
                    if _raw_sx is not None and len(_raw_sx) >= 20:
                        _sdf = calc_indicators(_raw_sx)
                        st.session_state.setdefault('all_data_cache', {})[_sx_tk] = {'name': _sx.get('name',''), 'df': _sdf}
                if _sdf is not None:
                    _ep_sx = calc_entry_point(_sdf, st.session_state.get('analysis_preset'))
            except Exception:
                _ep_sx = None
            if _ep_sx and _ep_sx.get('entry'):
                _e1, _e2, _e3 = st.columns(3)
                _e1.metric("진입", f"{_ep_sx['entry']:,.0f}{_u}")
                _e2.metric("손절", f"{_ep_sx['stoploss']:,.0f}{_u}",
                           delta=f"{(_ep_sx['stoploss']/_ep_sx['entry']-1)*100:+.1f}%", delta_color="inverse")
                _e3.metric("목표", f"{_ep_sx['target1']:,.0f}{_u}",
                           delta=f"{(_ep_sx['target1']/_ep_sx['entry']-1)*100:+.1f}%", delta_color="normal")
                st.caption(f"🎯 R:R **1:{_ep_sx.get('rr',0)}** · {_ep_sx.get('reason','지지/저항 기반')}")
            else:
                st.caption("🎯 정밀 타점 계산 불가 (데이터 부족) — 종목을 다시 선택하세요.")
            # 퀵 매매
            _qty_s = st.number_input("수량(주)", min_value=1, value=10, step=1, key="scan_quick_qty")
            _qb1, _qb2 = st.columns(2)
            if _qb1.button("🟢 가상 매수", key="scan_quick_buy", use_container_width=True,
                           type="primary", disabled=(_scan_black or _cur <= 0)):
                _acc_q = load_account()
                _fx_q = 1.0 if _sx_kr else get_usd_krw()
                _net_q = calc_slippage(_cur, True, _sx_kr)
                _acc_q['cash'] -= _net_q * _qty_s * _fx_q
                _pex = get_position(_acc_q, _sx_tk)
                _nd = 0 if _sx_kr else 2
                if _pex:
                    _ov = _pex['avg_price'] * _pex['qty']; _nv = _net_q * _qty_s
                    _pex['qty'] += _qty_s
                    _pex['avg_price'] = round((_ov + _nv) / _pex['qty'], _nd)
                else:
                    _acc_q['positions'].append({'ticker': _sx_tk, 'name': _sx.get('name', _sx_tk),
                        'qty': _qty_s, 'avg_price': _net_q, 'entry_date': str(pd.Timestamp.now())[:10]})
                save_account(_acc_q)
                st.toast(f"✅ {_sx.get('name','')} {_qty_s}주 가상 매수", icon="🟢")
                st.rerun()
            if _qb2.button("🔴 가상 매도", key="scan_quick_sell", use_container_width=True,
                           disabled=(_cur <= 0)):
                _acc_q = load_account()
                _pex = get_position(_acc_q, _sx_tk)
                if not _pex:
                    st.toast("보유 포지션이 없습니다.", icon="⚠️")
                else:
                    _fx_q = 1.0 if _sx_kr else get_usd_krw()
                    _net_q = calc_slippage(_cur, False, _sx_kr)
                    _sell_q = min(_qty_s, _pex['qty'])
                    _acc_q['cash'] += _net_q * _sell_q * _fx_q
                    _pex['qty'] -= _sell_q
                    if _pex['qty'] <= 0:
                        _acc_q['positions'] = [p for p in _acc_q['positions'] if p['ticker'] != _sx_tk]
                    save_account(_acc_q)
                    st.toast(f"✅ {_sx.get('name','')} {_sell_q}주 가상 매도", icon="🔴")
                    st.rerun()

            # ── 선택 종목 미니 차트 (한 세트로 우측에 묶임) ──
            try:
                _cdf = st.session_state.get('all_data_cache', {}).get(_sx_tk, {}).get('df')
                if _cdf is None and _sx_tk:
                    _rawc = fetch_ohlcv(_sx_tk, 60)
                    if _rawc is not None and len(_rawc) >= 5:
                        _cdf = calc_indicators(_rawc)
                if _cdf is not None and len(_cdf) >= 5:
                    import plotly.graph_objects as _go_s
                    _cl_s = _cdf['종가'].tail(40)
                    _figs = _go_s.Figure(_go_s.Scatter(
                        y=_cl_s.values, mode='lines', line=dict(color='#4da6ff', width=1.6),
                        fill='tozeroy', fillcolor='rgba(77,166,255,0.08)'))
                    # 손절/목표 라인 (타점 있으면)
                    if _ep_sx and _ep_sx.get('entry'):
                        _figs.add_hline(y=_ep_sx['stoploss'], line=dict(color='#ef4444', dash='dot', width=1))
                        _figs.add_hline(y=_ep_sx['target1'], line=dict(color='#16a34a', dash='dot', width=1))
                    _figs.update_layout(height=190, margin=dict(l=0, r=0, t=6, b=0),
                        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                        xaxis=dict(visible=False), yaxis=dict(showgrid=False, tickfont=dict(size=9)))
                    st.caption(f"📈 {_sx.get('name','')} 최근 40일 (점선=손절/목표)")
                    st.plotly_chart(_figs, use_container_width=True, key=f"scan_mini_chart_{_sx_tk}")
            except Exception:
                pass

        # ══════════════════════════════════════════════════════
        # 🔎 종목별 상세 스코어 — expander로 은닉
        # ══════════════════════════════════════════════════════
        with st.expander("🔎 종목별 상세 스코어 데이터 (C1~C6 · RSI · CMF · ATR)", expanded=False):
            _grid_html = "<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;margin-bottom:16px'>"
            for _gi, _gitem in enumerate(_p_list[:20]):
                _gcond = _gitem.get('조건', '')
                _ggrd  = _gitem.get('등급', '')
                _gsc   = _gitem.get('score', 0)
                _gchg  = _gitem.get('등락(%)', 0)
                _gchg_c = "#ef4444" if _gchg > 0 else "#3b82f6"
                _gg_c  = "#ffd166" if 'S등급' in _ggrd else "#3b82f6" if 'A등급' in _ggrd else "#10b981" if 'B등급' in _ggrd else "#64748b"
                def _cx(cond_str, cx): return "✅" if f"C{cx}✅" in cond_str else "❌"
                _is_etf_card = _gitem['ticker'] in _ETF_TICKERS_SET if '_ETF_TICKERS_SET' in dir() else False
                _is_wl_g = _gitem['ticker'] in _sc_ids
                if _is_etf_card:
                    _etf_badge = "<span style='background:#1e3a5f;color:#60a5fa;font-size:9px;padding:1px 6px;border-radius:8px;margin-left:4px'>ETF</span>"
                    _cond_grid = f"<div style='font-size:9px;color:#64748b;margin-top:4px'>{_gitem.get('조건','')[:60]}</div>"
                else:
                    _etf_badge = ""
                    _c1=_cx(_gcond,1);_c2=_cx(_gcond,2);_c3=_cx(_gcond,3)
                    _c4=_cx(_gcond,4);_c5=_cx(_gcond,5);_c6=_cx(_gcond,6)
                    _cond_grid = (
                        f"<div style='display:grid;grid-template-columns:repeat(6,1fr);gap:2px;font-size:10px;text-align:center'>"
                        f"<div style='color:#64748b'>C1<br>{_c1}</div>"
                        f"<div style='color:#64748b'>C2<br>{_c2}</div>"
                        f"<div style='color:#64748b'>C3<br>{_c3}</div>"
                        f"<div style='color:#64748b'>C4<br>{_c4}</div>"
                        f"<div style='color:#64748b'>C5<br>{_c5}</div>"
                        f"<div style='color:#64748b'>C6<br>{_c6}</div>"
                        f"</div>"
                    )
                _grid_html += (
                    f"<div style='background:#0d1117;border:1px solid {_gg_c}30;border-radius:10px;padding:10px 12px'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>"
                    f"<span style='font-weight:700;font-size:12px;color:#f0f4ff'>{_gitem['name'][:10]}{_etf_badge}</span>"
                    f"<span style='background:#1e293b;color:#fbbf24;font-size:11px;padding:1px 8px;border-radius:12px'>{_gsc}점</span>"
                    f"</div>"
                    f"<div style='font-size:10px;color:#64748b;margin-bottom:6px'>{_gitem['ticker']} | "
                    f"<span style='color:{_gchg_c}'>{'▲' if _gchg>0 else '▼'}{abs(_gchg):.1f}%</span>"
                    f" | 5일 {_gitem.get('5일수익률',0):+.1f}%</div>"
                    + _cond_grid +
                    f"<div style='font-size:10px;color:{_gg_c};margin-top:6px'>{_ggrd}"
                    + ("&nbsp;<span style='color:#34d399'>★ 관심</span>" if _is_wl_g else "") +
                    "</div></div>"
                )
            _grid_html += "</div>"
            st.markdown(_grid_html, unsafe_allow_html=True)

        # ── V9.7 사이드 패널 Drawer — 좌: 목록 / 우: 상세 분석 ──
        st.markdown("<div style='font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:10px'>⚡ 종목 선택 → 우측 패널에서 즉시 분석</div>", unsafe_allow_html=True)

        # 초기 선택값 1회만 설정 (rerun 없이 콜백으로 즉시 반영)
        if 'scan_drawer_sel' not in st.session_state:
            st.session_state['scan_drawer_sel'] = _p_list[0]['ticker'] if _p_list else None

        def _set_drawer(tk):
            st.session_state['scan_drawer_sel'] = tk

        _drawer_left, _drawer_right = st.columns([2, 3])

        with _drawer_left:
            st.markdown("<div style='font-size:11px;color:#64748b;margin-bottom:6px'>📋 발굴 종목 목록</div>", unsafe_allow_html=True)
            for _si, item in enumerate(_p_list):
                _stk = item['ticker']; _snm = item['name']
                _schg = item.get('등락(%)', 0); _ssc = item.get('score', 0)
                _sgrd = item.get('등급', '')
                _is_sel = st.session_state.get('scan_drawer_sel') == _stk
                _btn_style = "primary" if _is_sel else "secondary"
                _btn_lbl = f"{'🏆' if '🏆' in _sgrd else '🎯'} {_snm[:8]} | {_ssc}점 | {'▲' if _schg>0 else '▼'}{abs(_schg):.1f}%"
                # on_click 콜백 — st.rerun() 없이 session_state만 갱신 → 딜레이 제거
                st.button(_btn_lbl, key=f"drawer_btn_{_stk}",
                          use_container_width=True, type=_btn_style,
                          on_click=_set_drawer, args=(_stk,))

        with _drawer_right:
            _sel_tk = st.session_state.get('scan_drawer_sel')
            _sel_item = next((i for i in _p_list if i['ticker'] == _sel_tk), None)
            if _sel_item:
                _stk = _sel_item['ticker']; _snm = _sel_item['name']
                _ssc = _sel_item.get('score', 0)
                _sgrd = _sel_item.get('등급', '')
                _schg = _sel_item.get('등락(%)', 0)
                _scc  = "#ffd166" if '🏆' in _sgrd else "#3b82f6"
                _schg_c = "#39ff14" if _schg > 0 else "#3b82f6"
                _is_in_wl = _stk in _sc_ids

                # 메타 칩
                _smeta_html = (
                    f"<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px'>"
                    f"<span style='background:#1e293b;color:#fbbf24;font-size:11px;padding:3px 10px;border-radius:20px'>점수 {_ssc}</span>"
                    f"<span style='background:#1e293b;color:{_schg_c};font-size:11px;padding:3px 10px;border-radius:20px'>{'▲' if _schg>0 else '▼'}{abs(_schg):.2f}%</span>"
                    f"<span style='background:#1e293b;color:#94a3b8;font-size:11px;padding:3px 10px;border-radius:20px'>거래량 {_sel_item.get('거래량비율',0):.0f}%</span>"
                    f"<span style='background:#1e293b;color:#94a3b8;font-size:11px;padding:3px 10px;border-radius:20px'>CMF {_sel_item.get('CMF',0):.3f}</span>"
                    f"<span style='background:#1e293b;color:#94a3b8;font-size:11px;padding:3px 10px;border-radius:20px'>5일 {_sel_item.get('5일수익률',0):+.1f}%</span>"
                    f"</div>"
                )
                st.markdown(_smeta_html, unsafe_allow_html=True)

                # ⚡ Verdict 분석
                try:
                    _df_ov = fetch_ohlcv(_stk, 60)
                    if _df_ov is not None and len(_df_ov) >= 20:
                        _df_ov = calc_indicators(_df_ov)
                        _ep_ov = calc_entry_point(_df_ov, st.session_state.get('scan_preset', 'bounce'))
                        _sigs_ov = get_signal(_df_ov)
                        _buy_ov  = sum(1 for _, t in _sigs_ov if t == 'buy')
                        _v891_ov = run_v891_system_check()

                        if not _v891_ov['can_enter']:
                            _vd_ov = "🚫 진입 차단"; _vc_ov = "#f43f5e"; _vb_ov = "rgba(244,63,94,0.10)"
                        elif _ep_ov['rr'] < 2.0:
                            _vd_ov = "❌ 진입 불가"; _vc_ov = "#f43f5e"; _vb_ov = "rgba(244,63,94,0.08)"
                        elif _buy_ov >= 2 and _ep_ov['rr'] >= 2.0:
                            _vd_ov = "✅ 매수 권장"; _vc_ov = "#34d399"; _vb_ov = "rgba(52,211,153,0.10)"
                        else:
                            _vd_ov = "⚠️ 관망"; _vc_ov = "#fbbf24"; _vb_ov = "rgba(251,191,36,0.08)"

                        # 지지선/손절가 계산
                        _ep_sup = _ep_ov['stoploss']
                        _ep_ent = _ep_ov['entry']
                        _ep_tgt = _ep_ov['target1']
                        _ep_cur = float(_df_ov['Close'].iloc[-1]) if 'Close' in _df_ov.columns else _ep_ent

                        st.markdown(
                            f"<div style='background:{_vb_ov};border:2px solid {_vc_ov}50;border-radius:12px;"
                            f"padding:12px 16px;margin-bottom:8px'>"
                            f"<div style='font-size:18px;font-weight:900;color:{_vc_ov};margin-bottom:6px'>{_vd_ov}</div>"
                            f"<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:4px;font-size:11px'>"
                            f"<div style='color:#64748b'>현재가<br><span style='color:#f0f4ff;font-weight:700'>{_ep_cur:,.0f}</span></div>"
                            f"<div style='color:#64748b'>진입가<br><span style='color:#fbbf24;font-weight:700'>{_ep_ent:,.0f}</span></div>"
                            f"<div style='color:#64748b'>지지/손절<br><span style='color:#ff003c;font-weight:700'>{_ep_sup:,.0f}</span></div>"
                            f"<div style='color:#64748b'>목표가<br><span style='color:#39ff14;font-weight:700'>{_ep_tgt:,.0f}</span></div>"
                            f"</div>"
                            f"<div style='margin-top:8px;font-size:11px;color:#64748b'>수급점수 {_sel_item.get('CMF',0):.3f} &nbsp;|&nbsp; R:R <span style='color:{_vc_ov};font-weight:700'>" + str(_ep_ov['rr']) + "</span></div>"
                            f"</div>",
                            unsafe_allow_html=True
                        )
                        # (분석 기록 저장은 조회만으로 실행하지 않음 — 무한 중복 방지.
                        #  저장은 명시적 액션 시에만: 아래 '📌 이 종목 기록 저장' 버튼)
                        if st.button("📌 이 종목 기록 저장", key=f"ov_save_{_stk}",
                                     use_container_width=True):
                            save_analysis_log(_stk, _snm, _vd_ov, _ep_ov['rr'],
                                              _ep_ov['entry'], _ep_ov['stoploss'],
                                              _ep_ov['target1'], _ep_ov['target2'],
                                              preset=st.session_state.get('scan_preset',''),
                                              score=_ssc, source="스캐너드로어")
                            st.toast(f"✅ {_snm} 분석 기록 저장", icon="📌")

                        # 차트 토글
                        _chart_key_ov = f"ov_chart_{_stk}"
                        if st.button(
                            "📈 차트 닫기" if st.session_state.get(_chart_key_ov) else "📈 차트 보기",
                            key=f"ov_chart_btn_{_stk}", use_container_width=True
                        ):
                            st.session_state[_chart_key_ov] = not st.session_state.get(_chart_key_ov, False)
                            st.rerun()
                        if st.session_state.get(_chart_key_ov):
                            try:
                                _df_ch = fetch_ohlcv(_stk, 60)
                                if _df_ch is not None:
                                    _df_ch = calc_indicators(_df_ch)
                                    _ep_ch = calc_entry_point(_df_ch, st.session_state.get('scan_preset', 'bounce'))
                                    st.plotly_chart(make_chart(_df_ch, _snm,
                                        entry=_ep_ch['entry'], stoploss=_ep_ch['stoploss'],
                                        target1=_ep_ch['target1'], target2=_ep_ch['target2']),
                                        use_container_width=True)
                            except Exception:
                                st.caption("차트 로드 실패")

                        # 관심종목 추가
                        if _is_in_wl:
                            st.markdown("<div style='color:#34d399;font-size:12px;margin-top:6px'>✅ 관심종목 등록됨</div>", unsafe_allow_html=True)
                        else:
                            if st.button("⭐ 관심종목 추가", key=f"ov_add_{_stk}", use_container_width=True):
                                if add_ticker(_stk, _snm):
                                    st.success(f"✅ {_snm} 추가!"); st.rerun()

                        # ➕ 실전 운용 연동 버튼
                        def _bind_to_op(_t=_stk):
                            st.session_state['scanner_selection'] = _t
                        st.button("➕ 포트폴리오 추가", key=f"ov_op_add_{_stk}",
                                  use_container_width=True, type="primary",
                                  on_click=_bind_to_op)
                    else:
                        st.caption("데이터 부족 — 분석 불가")
                except Exception as _ov_e:
                    st.caption(f"분석 오류: {_ov_e}")

        st.divider()

        # ── 종목 선택 → Gemini 정밀분석 ──
        st.markdown("#### 🤖 Gemini 정밀분석 (선택)")
        _sel_names = [f"{item['name']} ({item['ticker']}) | {item['score']}점" for item in _p_list]
        _sel_scan  = st.selectbox("Gemini 분석할 종목", _sel_names, key="gemini_scan_sel")
        _sel_scan_idx = _sel_names.index(_sel_scan)
        _sel_scan_item = _p_list[_sel_scan_idx]

        _ab1, _ab2, _ab3 = st.columns(3)
        _is_added_scan = _sel_scan_item['ticker'] in _sc_ids

        if _is_added_scan:
            _ab1.markdown("<div style='color:#34d399;padding:8px 0'>✅ 이미 추가됨</div>", unsafe_allow_html=True)
        else:
            if _ab1.button("⭐ 관심종목 추가", key="scan_ind_add", use_container_width=True):
                try:
                    if add_ticker(_sel_scan_item['ticker'], _sel_scan_item['name']):
                        st.success(f"✅ {_sel_scan_item['name']} 추가!")
                        st.rerun()
                    else:
                        st.warning("이미 등록된 종목입니다.")
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


# ══════════════════════════════════════════
# 랭킹 히스토리 헬퍼
def _update_rank_history(df_ranked, history_key: str, max_days: int = 7) -> dict:
    """
    오늘의 순위를 히스토리에 기록하고 반환.
    history_key: st.session_state 저장 키 (e.g. '_rh_kr', '_rh_us')
    반환: {ticker: [rank_today, rank_d-1, rank_d-2, ...]} (index 0 = 최신)
    """
    from datetime import date as _date_rh
    _today = str(_date_rh.today())
    _rh_store = st.session_state.setdefault(history_key, {'dates': [], 'snapshots': {}})
    # 오늘 날짜가 없으면 오늘 순위 기록
    if not _rh_store['dates'] or _rh_store['dates'][-1] != _today:
        _today_snapshot = {}
        for _idx, _row in df_ranked.iterrows():
            if _row.get('상태') == '활성':
                _today_snapshot[str(_row['코드'])] = _idx + 1
        _rh_store['dates'].append(_today)
        _rh_store['snapshots'][_today] = _today_snapshot
        # max_days 초과분 정리
        if len(_rh_store['dates']) > max_days:
            _old_date = _rh_store['dates'].pop(0)
            _rh_store['snapshots'].pop(_old_date, None)
    # {ticker: [rank_newest, ..., rank_oldest]} 형태로 변환
    _result = {}
    for _d in reversed(_rh_store['dates']):  # 최신 → 오래된 순
        for _tk, _r in _rh_store['snapshots'][_d].items():
            _result.setdefault(_tk, []).append(_r)
    return _result

# 국장ETF / 미장ETF 공용 지표 계산 함수
def calculate_trade_levels(cur_price, ma5_price, prev_close, gap_pct, ma5_disp, is_kr=True):
    """★ ETF 가격 전략 단일 산출 함수(Single Source) — 랭킹 카드·타점 위젯이 공통 참조.
    갭/과열/눌림목 상황에 따라 매수 타점을 정하고 손절(-7%)·목표(+8%/+15%)·R:R을 계산.
    반환 dict: entry, stop, target1, target2, rr, status, status_c, comment, in_zone."""
    cur  = float(cur_price or 0)
    ma5  = float(ma5_price) if ma5_price else cur
    prev = float(prev_close) if prev_close else cur
    _gp  = float(gap_pct or 0)
    _md  = float(ma5_disp or 0)
    _is_gap  = _gp >= 3.0
    _is_hot  = _md >= 3.0
    _is_cool = -1.0 <= _md <= 1.0
    if _is_gap and _is_hot:
        entry = round(ma5 * 0.99, 2); status = "⛔ 매수 차단"; sc = "#f43f5e"
        cm = "갭상승+과열 — MA5 -1% 눌림목 대기"; zone = False
    elif _is_gap:
        entry = round(prev * 1.001, 2); status = "⛔ 갭상승 차단"; sc = "#f97316"
        cm = f"갭상승 +{_gp:.1f}% — 전일종가 복귀 시 진입"; zone = False
    elif _is_hot:
        entry = round(ma5 * 0.99, 2); status = "⚠️ 과열 대기"; sc = "#f97316"
        cm = f"MA5 이격 +{_md:.1f}% 과열 — MA5 -1% 눌림목 대기"; zone = False
    elif _is_cool:
        entry = round(cur, 2); status = "✅ 진입 타점"; sc = "#22c55e"
        cm = f"MA5 이격 {_md:+.1f}% — 현재가가 타점권"; zone = True
    else:
        entry = round(ma5, 2); status = "⏳ 눌림목 대기"; sc = "#60a5fa"
        cm = "MA5 도달(-1%~+1%) 시 진입"; zone = False
    stop    = round(entry * (1 - _STOP_LOSS_PCT), 2)
    target1 = round(entry * 1.08, 2)
    target2 = round(entry * 1.15, 2)
    _risk   = entry - stop
    rr      = round((target1 - entry) / _risk, 1) if _risk > 0 else 0
    return {'entry': entry, 'stop': stop, 'target1': target1, 'target2': target2,
            'rr': rr, 'status': status, 'status_c': sc, 'comment': cm, 'in_zone': zone}


def _render_etf_ranking(df_ranked, currency_symbol='원', key_prefix='etf', show_add_btn=False, rank_history=None):
    """ETF 랭킹 카드 렌더링 공용 함수."""
    _rh = rank_history or {}  # {ticker: [rank_d0(today), rank_d1, rank_d2, ...]}

    # P1: O(1) 이름 조회를 위한 dict — iterrows() 대신 set_index 활용
    _code_to_name = df_ranked.set_index('코드')['ETF명'].to_dict()

    # ── On-Deck: 최근 5일간 순위 가장 많이 상승한 TOP3 ──────────────────────
    _ondeck_candidates = []
    for _tk, _history in _rh.items():
        if len(_history) >= 2:
            _oldest = _history[-1]; _newest = _history[0]
            _rise = _oldest - _newest
            if _rise > 0 and _newest > 1:
                _ondeck_candidates.append((_tk, _newest, _rise, _history))
    _ondeck_candidates.sort(key=lambda x: x[2], reverse=True)
    if _ondeck_candidates[:3]:
        _od_html = (
            "<div style='background:#0d1117;border:1px solid #7c3aed40;border-radius:12px;"
            "padding:12px 16px;margin-bottom:10px'>"
            "<div style='font-size:11px;font-weight:700;color:#7c3aed;margin-bottom:8px'>"
            "🎯 스위칭 대기 (On-Deck) — 최근 순위 급상승 종목</div>"
            "<div style='display:flex;gap:10px;flex-wrap:wrap'>"
        )
        for _od_tk, _od_rank, _od_rise, _od_hist in _ondeck_candidates[:3]:
            _od_name = _code_to_name.get(_od_tk, _od_tk)  # P1: O(1) 조회
            _od_hist_str = " ".join(["●" if r <= 2 else "◑" if r <= 4 else "○" for r in _od_hist[:5]])
            _od_html += (
                f"<div style='background:#1e1040;border:1px solid #7c3aed60;border-radius:8px;"
                f"padding:8px 12px;flex:1;min-width:120px'>"
                f"<div style='font-size:12px;font-weight:700;color:#a78bfa'>{_od_tk}</div>"
                f"<div style='font-size:10px;color:#64748b'>{_od_name[:10]}</div>"
                f"<div style='font-size:11px;color:#7c3aed;margin-top:4px'>현재 {_od_rank}위 "
                f"<span style='color:#34d399'>+{_od_rise}계단 ↑</span></div>"
                f"<div style='font-size:10px;color:#475569;letter-spacing:2px;margin-top:2px'>{_od_hist_str}</div>"
                f"</div>"
            )
        _od_html += "</div></div>"
        st.markdown(_od_html, unsafe_allow_html=True)

    # ── 단일 카드 렌더러 (Top 3는 메인, 4위 이하는 Expander 격리용으로 재사용) ──
    #    in_expander=True → 이미 '더 보기' Expander 안이므로 상세 지표를 중첩
    #    Expander 대신 container로 인라인 표시(Streamlit 중첩 Expander 금지 회피).
    def _render_card(_i, row, in_expander=False):
        _is_top  = (_i == 0 and row['상태'] == '활성')
        _is_dead = (row['상태'] != '활성')
        _rank   = '🥇' if _is_top else f"{_i+1}위"
        _tk_code = str(row['코드'])

        # ── 탈락 종목: 컴팩트 한 줄 표시 ──
        if _is_dead:
            # '오류'(데이터 조회 실패)와 '탈락'(ADX<25 추세미달) 구분 표시
            _is_err = (row.get('상태') == '오류')
            _dead_msg = "⚠️ 데이터 조회 실패 (시세 못 불러옴)" if _is_err else f"ADX {row.get('ADX',0)} 탈락 (추세 약함)"
            _dead_col = "#f59e0b" if _is_err else "#64748b"
            st.markdown(
                f"<div style='background:#0d0d0d;border-radius:6px;padding:5px 14px;margin-bottom:2px;opacity:0.55;"
                f"font-size:12px;color:{_dead_col}'>"
                f"{_rank} {row['ETF명']} ({row['코드']}) — {_dead_msg}"
                f"</div>",
                unsafe_allow_html=True
            )
            return

        # ── 랭킹 히스토리: 크라운 배지 + 도트 바 ──
        _hist_ranks = _rh.get(_tk_code, [])
        _consec_1   = sum(1 for r in _hist_ranks if r == 1)
        _crown_badge = ""
        if _is_top and _consec_1 >= 3:
            _crown_badge = (
                f" <span style='background:#ffd16620;color:#ffd166;padding:2px 8px;"
                f"border-radius:8px;font-size:10px;font-weight:700'>"
                f"👑 {_consec_1}일 연속 1위</span>"
            )
        elif _is_top and _consec_1 >= 2:
            _crown_badge = (
                f" <span style='background:#fbbf2420;color:#fbbf24;padding:2px 8px;"
                f"border-radius:8px;font-size:10px'>🔥 {_consec_1}일 연속</span>"
            )

        # 도트 바: 최근 5일 순위 (● = 1위, ◕ = 2위, ◑ = 3위, ◔ = 4위, ○ = 5위↓)
        # M3: 순위별 색상 그라데이션 — 상위권 밝은 톤 / 하위권 무채색
        def _rank_dot(r):
            if r == 1:   return ("●", "#ffd166")  # 금
            if r == 2:   return ("●", "#34d399")  # 초록
            if r == 3:   return ("●", "#60a5fa")  # 파랑
            if r <= 5:   return ("◕", "#94a3b8")  # 연회색
            if r <= 10:  return ("◑", "#475569")  # 중간회색
            return           ("○", "#1e293b")     # 어두운 회색 (탈락 직전)
        _dot_bar = ""
        if len(_hist_ranks) >= 2:
            _trend  = _hist_ranks[0] - _hist_ranks[-1]  # 음수 = 상승
            _t_icon = "▲" if _trend < 0 else ("▼" if _trend > 0 else "─")
            _t_c    = "#34d399" if _trend < 0 else ("#ef4444" if _trend > 0 else "#64748b")
            _dots = "".join(
                "<span style='color:" + _rank_dot(r)[1] + "'>" + _rank_dot(r)[0] + "</span>"
                for r in list(_hist_ranks)[:5]
            )
            _dot_bar = (
                f"<span style='font-size:11px;letter-spacing:2px;margin-left:10px'>{_dots}</span>"
                f"<span style='font-size:10px;color:{_t_c};margin-left:4px'>{_t_icon}</span>"
            )

        # 순위 변동 화살표 (직전 vs 현재)
        _rank_change_html = ""
        if len(_hist_ranks) >= 2:
            _prev_r = _hist_ranks[1]; _cur_r = _i + 1
            if _cur_r < _prev_r:
                _rank_change_html = f"<span style='color:#34d399;font-size:10px;margin-left:4px'>▲{_prev_r-_cur_r}</span>"
            elif _cur_r > _prev_r:
                _rank_change_html = f"<span style='color:#ef4444;font-size:10px;margin-left:4px'>▼{_cur_r-_prev_r}</span>"

        _bg     = '#1a1400' if _is_top else '#111827'
        _macd   = row.get('MACD', '')
        _border_color = '#ffd166' if _is_top else ('#d4a017' if _macd == '골든크로스' else '#c0392b' if _macd == '데드크로스' else '#1e3a5f')
        _cc     = '#ff4d6d' if row['등락(%)'] > 0 else '#4da6ff'
        _ac     = '#4dff91' if row.get('ADX', 0) >= 25 else '#ff4d6d'
        _tag    = ' <span style="background:#ffd166;color:#000;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">🏆 1위</span>' if _is_top else ''
        _price_str = f"{row['현재가']:,.2f}{currency_symbol}" if currency_symbol == '$' else f"{row['현재가']:,.0f}{currency_symbol}"

        # ── 검증 배지 (내부 DB 우선: check_ticker_integrity 결과) ──
        _validated = row.get('_validated', True)
        _integrity_ok, _canon_name, _integrity_msg = check_ticker_integrity(
            str(row['코드']), str(row['ETF명'])
        )
        # 내부 DB가 불일치를 감지한 경우 _validated를 강제 override
        if not _integrity_ok:
            _validated = False
        _val_badge = (
            "<span style='background:#16a34a20;color:#4ade80;font-size:9px;"
            "padding:2px 6px;border-radius:6px;margin-left:6px;border:1px solid #16a34a40'>"
            "✅ 검증완료</span>"
            if _validated else
            "<span style='background:#f9731620;color:#fb923c;font-size:9px;"
            "padding:2px 6px;border-radius:6px;margin-left:6px;border:1px solid #f9731640'>"
            "⚠️ 명칭불일치</span>"
        )

        # ── ⓘ 툴팁 HTML (title 속성) ──
        from datetime import datetime as _dt_tip
        _tip_time = _dt_tip.now().strftime('%Y-%m-%d %H:%M')
        _tip_exp  = row.get('_expected_name', '')
        _tip_text = f"티커: {row['코드']} | 명칭: {row['ETF명']}"
        if _tip_exp:
            _tip_text += f" | DB기준: {_tip_exp}"
        _tip_text += f" | 업데이트: {_tip_time}"
        _info_icon = (
            f"<span title='{_tip_text}' style='color:#64748b;font-size:11px;"
            f"cursor:help;margin-left:6px;background:#1e293b;padding:1px 5px;"
            f"border-radius:4px'>ⓘ</span>"
        )

        if show_add_btn:
            # 버튼 컬럼 폭 확대(10%→약 18%) + 세로 중앙 정렬 (버튼 찢어짐/덜컹 방지)
            try:
                _card_col, _btn_col = st.columns([5, 1], vertical_alignment="center")
            except TypeError:
                _card_col, _btn_col = st.columns([5, 1])
        else:
            _card_col = st.container()
        with _card_col:
            # ── 5-컬럼 압축 카드 (메인 뷰) ──
            _rank_score = row.get('종합점수', 0)
            _adx_val    = row.get('ADX', 0)
            # 랭킹 기반 4-state 레이블
            if _adx_val < 25:
                _rank_state = "🚨 정리검토"
            elif _i == 0:
                _rank_state = "📈 추가매집 검토" if _consec_1 >= 3 else "🛡️ 보유유지"
            elif _i <= 2:
                _rank_state = "🛡️ 보유유지"
            elif _i == 3:
                _rank_state = "✂️ 일부축소"
            else:
                _rank_state = "🚨 정리검토"

            _state_c = (
                "#ef4444" if "정리검토" in _rank_state else
                "#f97316" if "일부축소" in _rank_state else
                "#34d399" if "추가매집" in _rank_state else
                "#64748b"
            )
            # ── 🎯 실전 가격 타점 (단일 함수 calculate_trade_levels 참조 — 위젯과 완전 일치) ──
            _cur_r  = float(row.get('현재가', 0) or 0)
            _u_r    = currency_symbol
            _fmt_r  = (lambda v: f"{v:,.0f}{_u_r}") if _u_r == '원' else (lambda v: f"{_u_r}{v:,.2f}")
            _lv_r = calculate_trade_levels(_cur_r, row.get('MA5가격'), row.get('전일종가'),
                                           row.get('갭(%)', 0), row.get('MA5이격(%)', 0),
                                           str(row['코드']).isdigit())
            _in_zone = _lv_r['in_zone']
            _entry_badge = (
                "<span style='background:#16a34a25;color:#34d399;font-size:9px;font-weight:700;"
                "padding:2px 7px;border-radius:8px;margin-left:6px'>🎯 진입 가능</span>" if _in_zone else
                "<span style='background:#f59e0b20;color:#fbbf24;font-size:9px;font-weight:700;"
                "padding:2px 7px;border-radius:8px;margin-left:6px'>⏳ 눌림목 대기</span>"
            ) if _cur_r > 0 else ""
            _price_line = (
                f"<div style='margin-top:6px;font-size:11px;color:#94a3b8;letter-spacing:0.2px'>"
                f"🎯 타점 <b style='color:#fbbf24'>{_fmt_r(_lv_r['entry'])}</b> &nbsp;|&nbsp; "
                f"🛑 손절 <b style='color:#ef4444'>{_fmt_r(_lv_r['stop'])}</b> &nbsp;|&nbsp; "
                f"🚀 목표 <b style='color:#34d399'>{_fmt_r(_lv_r['target1'])}</b> &nbsp;|&nbsp; "
                f"⚖️ R:R <b style='color:#f0f4ff'>1:{_lv_r['rr']:.1f}</b></div>"
            ) if _cur_r > 0 else ""
            st.markdown(
                f"<div style='background:{_bg};border:1px solid {_border_color};border-radius:10px;"
                f"padding:12px 18px;margin-bottom:4px'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'>"
                f"<div>"
                f"<b style='font-size:15px'>{_rank}{_rank_change_html} {row['ETF명']}</b>"
                f"{_entry_badge}"
                f"<span style='color:#64748b;font-size:11px'> ({row['코드']})</span>"
                f"{_info_icon}{_val_badge}{_tag}{_crown_badge}{_dot_bar}"
                f"</div>"
                f"<span style='color:{_cc};font-family:IBM Plex Mono'>{'▲' if row['등락(%)']>0 else '▼'}{abs(row['등락(%)']):+.2f}%</span>"
                f"</div>"
                f"<div style='display:flex;gap:20px;margin-top:8px;flex-wrap:wrap;align-items:center'>"
                f"<span style='font-size:12px;color:#94a3b8'>현재가 <b style='color:#f0f4ff'>{_price_str}</b></span>"
                f"<span style='font-size:12px;color:#fbbf24'>종합 <b style='font-size:15px'>{_rank_score}점</b></span>"
                f"<span style='background:{_state_c}20;color:{_state_c};padding:3px 10px;"
                f"border-radius:12px;font-size:11px;font-weight:700;border:1px solid {_state_c}50'>"
                f"{_rank_state}</span>"
                f"</div>"
                f"{_price_line}"
                f"</div>",
                unsafe_allow_html=True
            )
            # ── 백엔드 퀀트 지표 (Progressive Disclosure) ──
            # 4위 이하는 이미 '더 보기' Expander 안 → 중첩 금지 회피 위해 container로 인라인
            _detail_cm = st.container() if in_expander else st.expander(f"🔎 {row['ETF명']} 상세 지표", expanded=False)
            with _detail_cm:
                if in_expander:
                    st.markdown(f"**🔎 {row['ETF명']} 상세 지표**")
                _dc1, _dc2, _dc3 = st.columns(3)
                _dc1.metric("ADX(14)", f"{row.get('ADX',0)}", help="25 미만 탈락")
                _dc2.metric("RSI(14)", f"{row.get('RSI',0)}")
                _dc3.metric("종합점수", f"{row.get('종합점수',0)}점")
                _dc4, _dc5, _dc6 = st.columns(3)
                _dc4.metric("MACD", row.get('MACD',''))
                _dc5.metric("모멘텀(20일)", f"{row.get('모멘텀(%)',0):+.1f}%")
                _dc6.metric("MA 정배열", row.get('정배열',''))
                st.caption(f"Z-Score: {row.get('Z-Score',0)} | 거래량%: {row.get('거래량%',0)}")

                # ── 🎯 실전 가격 타점 (1위 종목 한정 — 상세 지표 안으로 이관, 메인 중복 제거) ──
                if _is_top:
                    _gap_v      = row.get('갭(%)', 0)
                    _ma5_v      = row.get('MA5이격(%)', 0)
                    _ma5_price  = float(row.get('MA5가격', row['현재가']))
                    _prev_close = float(row.get('전일종가', row['현재가']))
                    _cur_price  = float(row['현재가'])
                    _is_kr_etf  = str(row['코드']).isdigit()
                    _sym        = '원' if _is_kr_etf else '$'
                    _fmt        = (lambda v: f"{v:,.0f}{_sym}") if _is_kr_etf else (lambda v: f"{_sym}{v:,.2f}")
                    # 단일 함수 참조 — 상단 황금색 요약선과 1원도 안 어긋나게 통일
                    _lv = calculate_trade_levels(_cur_price, _ma5_price, _prev_close,
                                                 _gap_v, _ma5_v, _is_kr_etf)
                    st.markdown("---")
                    st.markdown(f"""
<div style='background:rgba(30,30,50,0.7);border:2px solid {_lv['status_c']};border-radius:12px;padding:16px 20px;margin:4px 0'>
  <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px'>
    <span style='font-size:16px;font-weight:800;color:{_lv['status_c']}'>{_lv['status']}</span>
    <span style='font-size:12px;color:#94a3b8'>{_lv['comment']}</span>
  </div>
  <div style='display:grid;grid-template-columns:repeat(5,1fr);gap:10px;text-align:center'>
    <div style='background:rgba(255,255,255,0.05);border-radius:8px;padding:10px'>
      <div style='font-size:10px;color:#64748b'>🎯 매수 타점</div>
      <div style='font-size:16px;font-weight:700;color:#fbbf24'>{_fmt(_lv['entry'])}</div>
    </div>
    <div style='background:rgba(255,255,255,0.05);border-radius:8px;padding:10px'>
      <div style='font-size:10px;color:#64748b'>🛑 손절가 (-7%)</div>
      <div style='font-size:16px;font-weight:700;color:#f43f5e'>{_fmt(_lv['stop'])}</div>
    </div>
    <div style='background:rgba(255,255,255,0.05);border-radius:8px;padding:10px'>
      <div style='font-size:10px;color:#64748b'>🎯 1차 목표 (+8%)</div>
      <div style='font-size:16px;font-weight:700;color:#22c55e'>{_fmt(_lv['target1'])}</div>
    </div>
    <div style='background:rgba(255,255,255,0.05);border-radius:8px;padding:10px'>
      <div style='font-size:10px;color:#64748b'>🚀 2차 목표 (+15%)</div>
      <div style='font-size:16px;font-weight:700;color:#34d399'>{_fmt(_lv['target2'])}</div>
    </div>
    <div style='background:rgba(255,255,255,0.05);border-radius:8px;padding:10px'>
      <div style='font-size:10px;color:#64748b'>⚖️ R:R</div>
      <div style='font-size:16px;font-weight:700;color:{"#22c55e" if _lv['rr'] >= 2 else "#f97316"}'>{_lv['rr']:.1f}</div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)
                    st.caption(f"📐 MA5 이격도: **{_ma5_v:+.1f}%** · 갭: {_gap_v:+.1f}% "
                               f"(이격 -1%~+1% = 타점권 / +3%↑ = 과열 눌림목 대기)")

            # ── 명칭 불일치 → st.error + 진입 차단 경고 ──
            if not _validated and _integrity_msg:
                st.error(
                    f"🚨 **데이터 정합성 오류: 종목 정보 재설정 필요**\n\n"
                    f"{_integrity_msg}\n\n"
                    "⛔ **이 종목은 진입 금지** — 증권사 앱에서 코드 직접 확인 후 시스템 관리자에게 보고하세요."
                )

            # ── 팩트체크 버튼 (국장=Naver, 미장=yfinance) ──
            _fc_key = f"{key_prefix}_fc_{row['코드']}_{_i}"
            _fc_result_key = f"_fc_result_{row['코드']}"
            _is_kr_fc = str(row['코드']).isdigit() and len(str(row['코드'])) == 6
            if st.button("🔍 데이터 검증", key=_fc_key,
                         help="외부 소스(Naver/yfinance)와 종목명 일치 여부 대조"):
                with st.spinner("검증 중..."):
                    try:
                        if _is_kr_fc:
                            import urllib.request as _ur
                            _naver_url = f"https://finance.naver.com/item/main.naver?code={row['코드']}"
                            _req = _ur.Request(_naver_url, headers={'User-Agent': 'Mozilla/5.0'})
                            _raw = _ur.urlopen(_req, timeout=5).read()
                            # 인코딩 자동 감지: charset 힌트 추출 후 시도, 없으면 UTF-8 → EUC-KR 순
                            import re as _re_fc
                            _charset_m = _re_fc.search(rb'charset=["\']?([A-Za-z0-9_-]+)', _raw)
                            _enc_hint = _charset_m.group(1).decode('ascii').lower() if _charset_m else 'utf-8'
                            for _enc in ([_enc_hint] if _enc_hint else []) + ['utf-8', 'euc-kr']:
                                try:
                                    _html = _raw.decode(_enc, errors='strict')
                                    break
                                except (UnicodeDecodeError, LookupError):
                                    continue
                            else:
                                _html = _raw.decode('utf-8', errors='replace')
                            _m = _re_fc.search(r'<title>([^:<]+)', _html)
                            _naver_name = _m.group(1).strip() if _m else None
                            # 인코딩 깨짐 감지: 한글 비율이 너무 낮으면 신뢰 불가
                            if _naver_name:
                                _kor_ratio = sum(1 for c in _naver_name if '가' <= c <= '힣') / max(len(_naver_name), 1)
                                _has_garbage = any(ord(c) > 0xD7A3 and not c.isascii() for c in _naver_name)
                                if _has_garbage or (_kor_ratio < 0.1 and len(_naver_name) > 5):
                                    _naver_name = None  # 인코딩 깨짐 → 검증 포기
                        else:
                            import yfinance as _yf_fc
                            _info_fc = _yf_fc.Ticker(str(row['코드'])).fast_info
                            _naver_name = getattr(_info_fc, 'long_name', None) or getattr(_info_fc, 'short_name', None)

                        if _naver_name:
                            def _norm(s):
                                # Naver는 + → 플러스, & → 앤드 로 표기 → 동일 취급
                                return (s.replace(' ', '').replace('+', '플러스')
                                         .replace('&', '앤드').upper())
                            _dash_n = _norm(str(row['ETF명']))
                            _src_n  = _norm(_naver_name)
                            _db_n   = _norm(_MASTER_ETF_DB.get(str(row['코드']), ''))
                            # 매치 조건: 대시보드명 OR 내부 DB 공식명과 외부명이 겹치면 통과
                            _match = (
                                (_dash_n in _src_n) or (_src_n in _dash_n) or
                                (_db_n and ((_db_n in _src_n) or (_src_n in _db_n)))
                            )
                            st.session_state[_fc_result_key] = (True, _naver_name, _match)
                        else:
                            # 외부 소스 응답 불가 → 내부 DB 검증 결과로 대체
                            _db_ok, _db_cn, _ = check_ticker_integrity(str(row['코드']), str(row['ETF명']))
                            st.session_state[_fc_result_key] = (True, f"외부 응답 없음 (내부DB: {'일치' if _db_ok else '불일치'})", _db_ok)
                    except Exception as _fc_e:
                        st.session_state[_fc_result_key] = (False, str(_fc_e), False)

            if _fc_result_key in st.session_state:
                _fc_ok, _fc_nm, _fc_match = st.session_state[_fc_result_key]
                if not _fc_ok:
                    st.caption(f"⚠️ 검증 불가: {_fc_nm or '응답 없음'}")
                elif _fc_match:
                    _src_label = "Naver 금융" if _is_kr_fc else "yfinance"
                    st.success(f"✅ 정합성 확인 — {_src_label}: **{_fc_nm}**")
                else:
                    st.error(f"⚠️ 불일치 보고 — 외부소스: **{_fc_nm}** / 대시보드: **{row['ETF명']}** — 진입 전 재확인 필수!")

        if show_add_btn:
            with _btn_col:
                st.markdown("<div style='margin-top:12px'></div>", unsafe_allow_html=True)
                _ticker_key = row['코드']
                _name_key   = row['ETF명']
                if st.button("➕ 추가", key=f"{key_prefix}_add_{_ticker_key}_{_i}", help=f"{_name_key} 관심종목 추가"):
                    _ok = add_ticker(_ticker_key, _name_key)
                    if _ok:
                        st.success(f"✅ {_name_key} 추가됨")
                    else:
                        st.info("이미 추가된 종목입니다")
        # (1위 매수 타점 카드는 🔎 상세 지표 Expander 내부로 이관됨 — 메인 중복 노출 제거)
        st.markdown("<div style='margin-bottom:6px'></div>", unsafe_allow_html=True)

    # ── Top 3만 메인 노출 · 4위 이하(활성+탈락 종목)는 Expander로 강제 격리 ──
    _n_total = len(df_ranked)
    for _i, row in df_ranked.iloc[:3].iterrows():
        _render_card(_i, row, in_expander=False)
    if _n_total > 3:
        with st.expander("🔽 4위 이하 종목 더 보기 (클릭하여 펼치기)", expanded=False):
            for _i, row in df_ranked.iloc[3:].iterrows():
                _render_card(_i, row, in_expander=True)


with tab_d:
    _tab_d1, _tab_d2 = st.tabs(["🔄 전략 로테이션", "⚔️ 실전 운용"])

    with _tab_d2:
        st.markdown("### ⚔️ 실전 운용 관제 센터")
        st.caption("현재 보유 종목의 손절·익절 기준선을 실시간으로 모니터링합니다.")

        # ── 보유 종목 입력 ──────────────────────────────────────────────────
        _op_key = 'op_positions'
        if _op_key not in st.session_state:
            st.session_state[_op_key] = []

        # ── Firebase에서 포지션 복원 (새로고침/재접속 대비) ──
        if not st.session_state[_op_key]:
            st.session_state[_op_key] = load_op_positions()

        def _save_positions_to_ls():
            """Firebase + session_state에 포지션 저장."""
            save_op_positions(st.session_state[_op_key])

        with st.expander("➕ 보유 종목 등록 / 수정", expanded=not bool(st.session_state[_op_key])):
            _op_c1, _op_c2, _op_c3, _op_c4 = st.columns([2, 1.5, 1.5, 1])
            with _op_c1:
                _op_ticker_default = st.session_state.pop('scanner_selection', '')
                _op_ticker = st.text_input("종목코드 / 티커", placeholder="005930 / JEPQ",
                                           value=_op_ticker_default, key="op_inp_ticker").strip().upper()
            with _op_c2:
                _op_qty    = st.number_input("보유수량", min_value=1, value=10, key="op_inp_qty")
            with _op_c3:
                _op_avg    = st.number_input("평단가", min_value=0.01, value=50000.0, format="%.2f", key="op_inp_avg")
            with _op_c4:
                _op_t1_pct = st.number_input("1차 익절(%)", min_value=1.0, value=8.0, step=0.5, key="op_inp_t1")
            _op_c5, _op_c6 = st.columns([1, 1])
            with _op_c5:
                _op_stop_pct = st.number_input("손절 기준(%)", min_value=1.0, value=7.0, step=0.5, key="op_inp_stop")
            with _op_c6:
                _op_t2_pct = st.number_input("2차 익절(%)", min_value=1.0, value=15.0, step=0.5, key="op_inp_t2")
            if st.button("✅ 종목 추가", type="primary", use_container_width=True, key="op_add_btn"):
                if _op_ticker:
                    import uuid as _uuid_op
                    import yfinance as _yf_cur
                    # 통화 자동 감지
                    _is_kr_new = _op_ticker.isdigit() and len(_op_ticker) == 6
                    if _is_kr_new:
                        _detected_currency = 'KRW'
                    else:
                        try:
                            _cur_info = _yf_cur.Ticker(_op_ticker).fast_info
                            _detected_currency = getattr(_cur_info, 'currency', None) or 'USD'
                        except Exception:
                            _detected_currency = 'USD'
                    # C1: 기존 ticker가 있으면 업데이트, 없으면 신규 uuid 부여
                    _exist = next((p for p in st.session_state[_op_key] if p['ticker'] == _op_ticker), None)
                    _new_pos = {
                        'id':        str(_exist['id'] if _exist else _uuid_op.uuid4()),
                        'ticker':    _op_ticker,
                        'qty':       _op_qty,
                        'avg':       _op_avg,
                        'stop_pct':  _op_stop_pct,
                        't1_pct':    _op_t1_pct,
                        't2_pct':    _op_t2_pct,
                        't1_done':   _exist.get('t1_done', False) if _exist else False,
                        'currency':  _detected_currency,
                    }
                    if _exist:
                        st.session_state[_op_key] = [_new_pos if p['ticker'] == _op_ticker else p
                                                      for p in st.session_state[_op_key]]
                        st.success(f"✅ {_op_ticker} 업데이트 완료")
                        # 수정 시 거래일지에 메모 기록
                        _op_name = resolve_korean_name(_op_ticker, _op_ticker)
                        log_trade(_op_ticker, _op_name, "수정", _op_qty, _op_avg, _op_avg,
                                  0, 0, memo=f"실전운용 포지션 수정 — 손절{_op_stop_pct}% / 1차익절{_op_t1_pct}% / 2차익절{_op_t2_pct}%")
                    else:
                        st.session_state[_op_key].append(_new_pos)
                        st.success(f"✅ {_op_ticker} 등록 완료")
                        # 신규 등록 시 거래일지에 매수 기록
                        _op_name = resolve_korean_name(_op_ticker, _op_ticker)
                        log_trade(_op_ticker, _op_name, "매수", _op_qty, _op_avg, _op_avg,
                                  0, 0, memo=f"실전운용 포지션 등록 — 손절{_op_stop_pct}% / 1차익절{_op_t1_pct}% / 2차익절{_op_t2_pct}%")
                    _save_positions_to_ls()
                    st.rerun()

        if not st.session_state[_op_key]:
            st.info("💡 위에서 보유 종목을 등록하면 손절/익절 기준선이 자동 계산됩니다.")
        else:
            # ── H1: 현재가 조회 (한국/미국 자동 구분 + 실패 알림) ────────────
            import yfinance as _yf_op

            _LKG_KEY = '_live_price_lkg'   # Last-Known-Good 시세 캐시 {ticker: (cur, prev)}
            st.session_state.setdefault(_LKG_KEY, {})

            def _get_live_price(tk: str):
                """한국(6자리)=.KS→.KQ, 미국=suffix 없음.
                반환: (cur, prev, status) — status: 'live'(정상) / 'cache'(직전값) / 'fail'(없음).
                단 1원 누락·타임아웃에도 멈추지 않고 Last-Known-Good을 우선 반환."""
                _is_kr_tk = tk.isdigit() and len(tk) == 6
                _suffixes = [".KS", ".KQ"] if _is_kr_tk else [""]
                for _sfx in _suffixes:
                    try:
                        _h = _yf_op.Ticker(tk + _sfx).history(period="2d", interval="1d")
                        if _h is None or _h.empty or 'Close' not in _h.columns:
                            continue
                        _ser = _h['Close'].dropna()
                        if _ser.empty:
                            continue
                        _cur = float(_ser.iloc[-1])
                        _prev = float(_ser.iloc[-2]) if len(_ser) >= 2 else _cur
                        # 유효성: 양수·유한수만 채택 (1원 누락/NaN 차단)
                        if not (_cur == _cur) or _cur <= 0:
                            continue
                        if not (_prev == _prev) or _prev <= 0:
                            _prev = _cur
                        st.session_state[_LKG_KEY][tk] = (_cur, _prev)   # LKG 갱신
                        return _cur, _prev, 'live'
                    except Exception:
                        continue
                # 조회 실패 → Last-Known-Good 폴백 (있으면)
                _cached = st.session_state.get(_LKG_KEY, {}).get(tk)
                if _cached:
                    return _cached[0], _cached[1], 'cache'
                return None, None, 'fail'

            _has_danger = False

            def _get_adx_rsi_pos(tk, is_kr):
                """포지션 카드용 ADX(14) + RSI(14) 경량 산출."""
                try:
                    import numpy as _np_pos
                    _sfxs = [".KS", ".KQ"] if is_kr else [""]
                    for _sfx in _sfxs:
                        _dfp = _yf_op.Ticker(tk + _sfx).history(period="6mo", interval="1d")
                        if _dfp is None or len(_dfp) < 30:
                            continue
                        _clp = _dfp['Close']; _hip = _dfp['High']; _lop = _dfp['Low']
                        _trp = pd.DataFrame({'hl':_hip-_lop,'hc':(_hip-_clp.shift()).abs(),'lc':(_lop-_clp.shift()).abs()}).max(axis=1)
                        _atp = _trp.rolling(14).mean()
                        _pdp = _hip.diff().clip(lower=0); _ndp = (-_lop.diff()).clip(lower=0)
                        _pip = 100*_pdp.rolling(14).mean()/_atp.replace(0,_np_pos.nan)
                        _nip = 100*_ndp.rolling(14).mean()/_atp.replace(0,_np_pos.nan)
                        _dxp = 100*(_pip-_nip).abs()/(_pip+_nip).replace(0,_np_pos.nan)
                        _adxp = float(_dxp.rolling(14).mean().iloc[-1])
                        _dvp  = _clp.diff()
                        _gup  = _dvp.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
                        _lup  = (-_dvp.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
                        _rsip = float(100 - 100/(1 + _gup.iloc[-1]/max(_lup.iloc[-1], 1e-9)))
                        return round(_adxp, 1), round(_rsip, 1)
                except Exception:
                    pass
                return None, None

            for _pos in list(st.session_state[_op_key]):  # C1: uuid 기반 — list copy로 안전 순회
                _pos_id = _pos.get('id', _pos['ticker'])  # 구버전 호환
                _tk    = _pos['ticker']

                # ── 잔고 데이터 무결성 검증 (삼성증권 실측 평단/수량) ──
                # 오타·문자열·0이하 값이 연산(수익률·비중)에 유입되는 것을 원천 차단.
                try:
                    _avg = float(_pos['avg'])
                    _qty = float(_pos['qty'])
                    assert _avg > 0, "평균단가는 0보다 커야 합니다"
                    assert _qty > 0, "보유수량은 0보다 커야 합니다"
                    assert _avg == _avg and _qty == _qty, "NaN 불가"   # NaN 차단
                except (KeyError, TypeError, ValueError, AssertionError) as _berr:
                    st.error(f"🚨 {_tk} 잔고 데이터 오류 — 평단/수량 재등록 필요 ({_berr}). 이 종목은 연산에서 제외됩니다.")
                    continue
                _is_kr = _tk.isdigit() and len(_tk) == 6

                # H1: 현재가 조회 (Last-Known-Good 폴백 내장)
                _cur_p, _prev_p, _price_st = _get_live_price(_tk)
                if _price_st == 'cache':
                    st.caption(f"📡 {_tk} 실시간 조회 지연 — 직전 캐싱 시세(Last Known Good)로 표시 중")
                elif _price_st == 'fail':
                    st.warning(f"⚠️ {_tk} 현재가 조회 실패(캐시 없음) — 평단가로 대체 표시 중. 티커를 확인하세요.")
                    _cur_p  = _avg
                    _prev_p = _avg

                # 핵심 계산
                _stop_p   = round(_avg * (1 - _pos['stop_pct'] / 100), 2)
                _t1_p     = round(_avg * (1 + _pos['t1_pct']  / 100), 2)
                _t2_p     = round(_avg * (1 + _pos['t2_pct']  / 100), 2)
                _pnl_pct  = (_cur_p / _avg - 1) * 100
                _pnl_amt  = (_cur_p - _avg) * _qty
                _chg_pct  = (_cur_p / _prev_p - 1) * 100 if _prev_p and _prev_p > 0 else 0

                # 거리 계산
                _dist_stop = (_cur_p - _stop_p) / _avg * 100
                _dist_t1   = (_t1_p  - _cur_p)  / _avg * 100
                _dist_t2   = (_t2_p  - _cur_p)  / _avg * 100

                # 상태 판정
                _danger  = _cur_p <= _stop_p * 1.03
                _t2_hit  = _cur_p >= _t2_p
                _t1_hit  = _cur_p >= _t1_p

                if _danger: _has_danger = True

                # ADX + RSI 실시간 산출 (4-state 판정용)
                _adx_pos, _rsi_pos = _get_adx_rsi_pos(_tk, _is_kr)
                _adx_weak = (_adx_pos is not None and _adx_pos < 25)
                _rsi_hot  = (_rsi_pos is not None and _rsi_pos >= 78)

                # ── 4-State 상태 레이블 (매매 원칙 5대 원칙 준수) ──
                # 우선순위: 정리검토 > 일부축소 > 추가매집 검토 > 보유유지
                # 매크로 게이트: 추가매집은 환율≤1,520 + 외인 순매수 둘 다 충족 시에만 승격.
                #   엣지케이스 — 가격이 +20% 도달(_t2_hit)해도 리스크오프면 '보유유지'로 강등.
                _macro_ok, _macro_dbg = macro_allows_scale_in(
                    st.session_state.get('_last_usd_krw', get_usd_krw()),
                    st.session_state.get('_foreign_net_krw', None),
                )
                if _danger or _adx_weak:
                    _brd = "#ef4444"; _bg = "#1a0505"; _status_label = "🚨 정리검토"
                elif _t2_hit and _macro_ok:
                    _brd = "#34d399"; _bg = "#051a10"; _status_label = "📈 추가매집 검토"
                elif _t1_hit or _rsi_hot:
                    _brd = "#f97316"; _bg = "#1a0800"; _status_label = "✂️ 일부축소"
                elif _t2_hit and not _macro_ok:
                    # 가격 조건은 충족했으나 매크로 미충족 → 추격 보류, 보유유지로 강등
                    _brd = "#1e3a5f"; _bg = "#0d1117"; _status_label = "🛡️ 보유유지"
                else:
                    _brd = "#1e3a5f"; _bg = "#0d1117"; _status_label = "🛡️ 보유유지"

                # 브리핑 패널용 상태 캐시 저장 (tab_d1에서 읽음)
                st.session_state.setdefault('_live_pos_states', {})[_tk] = _status_label
                st.session_state.setdefault('_live_pos_summary', {})[_tk] = {
                    'name': _tk, 'cur': _cur_p, 'pnl': round(_pnl_pct, 2),
                    'stop': _stop_p, 't1': _t1_p, 't2': _t2_p,
                    'unit': '원' if _is_kr else '$', 'state': _status_label,
                }

                _pnl_c   = "#39ff14" if _pnl_pct >= 0 else "#ff003c"
                _chg_c   = "#39ff14" if _chg_pct >= 0 else "#ff003c"
                _currency = _pos.get('currency', 'KRW' if _is_kr else 'USD')
                _unit    = "원" if _currency == 'KRW' else "$"
                _fmt_p   = (lambda v: f"{v:,.0f}") if _currency == 'KRW' else (lambda v: f"{v:.2f}")
                _cur_badge_color = "#34d399" if _currency == 'USD' else "#64748b"
                _cur_badge = (
                    f"<span style='background:{_cur_badge_color}25;color:{_cur_badge_color};"
                    f"font-size:9px;padding:2px 7px;border-radius:8px;border:1px solid {_cur_badge_color}60;"
                    f"margin-left:6px'>{_currency}</span>"
                )
                _price_warn = {"live": "", "cache": " 📡캐시", "fail": " ⚠️조회실패"}.get(_price_st, "")

                # ── 카드 렌더링 ── (깜빡임 제거 → 정적 붉은 글로우로 강조)
                _danger_anim = "box-shadow:0 0 12px 2px rgba(239,68,68,0.55);" if (_danger or _adx_weak) else ""
                _trail_badge = (
                    "<span style='background:#34d39930;color:#34d399;font-size:10px;"
                    "padding:2px 8px;border-radius:10px;margin-left:8px'>📈 추가매집 구간 진입</span>"
                ) if _t2_hit else (
                    "<span style='background:#f9731630;color:#f97316;font-size:10px;"
                    "padding:2px 8px;border-radius:10px;margin-left:8px'>✂️ 익절 구간</span>"
                ) if (_t1_hit or _rsi_hot) else ""

                _total_range = _t2_p - _stop_p
                _cur_pos_pct = max(0, min(100, (_cur_p - _stop_p) / _total_range * 100)) if _total_range > 0 else 50

                _cur_left_brd = "#34d399" if _currency == 'USD' else "#64748b"
                st.markdown(
                    f"<div style='background:{_bg};border:2px solid {_brd};"
                    f"border-left:4px solid {_cur_left_brd};border-radius:14px;"
                    f"padding:16px 20px;margin-bottom:12px;{_danger_anim}'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px'>"
                    f"<div>"
                    f"<span style='font-size:16px;font-weight:900;color:#f0f4ff'>{_tk}{_price_warn}</span>"
                    f"{_cur_badge}"
                    f"<span style='font-size:11px;color:#64748b;margin-left:10px'>{_qty}주 @ {_unit}{_fmt_p(_avg)}</span>"
                    f"{_trail_badge}"
                    f"</div>"
                    f"<div style='text-align:right'>"
                    f"<div style='font-size:20px;font-weight:900;color:#f0f4ff'>{_unit}{_fmt_p(_cur_p)}</div>"
                    f"<div style='font-size:11px;color:{_chg_c}'>당일 {'▲' if _chg_pct>=0 else '▼'}{abs(_chg_pct):.2f}%</div>"
                    f"</div>"
                    f"</div>"
                    f"<div style='display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap'>"
                    f"<span style='background:#1e293b;padding:4px 12px;border-radius:20px;font-size:12px;"
                    f"color:{_pnl_c};font-weight:700'>"
                    f"{'▲' if _pnl_pct>=0 else '▼'}{abs(_pnl_pct):.2f}% &nbsp; ({'+' if _pnl_amt>=0 else ''}{_fmt_p(_pnl_amt)}{_unit})</span>"
                    f"<span style='background:{_brd}20;color:{_brd};padding:4px 12px;border-radius:20px;"
                    f"font-size:12px;font-weight:700;border:1px solid {_brd}60'>{_status_label}</span>"
                    f"</div>"
                    f"<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px'>"
                    f"<div style='background:#2a0a0a;border:1px solid #ef444440;border-radius:8px;padding:8px 10px;text-align:center'>"
                    f"<div style='font-size:9px;color:#ef4444;font-weight:700;margin-bottom:4px'>🛑 손절가 (생존 마지노선)</div>"
                    f"<div style='font-size:15px;font-weight:900;color:#ef4444'>{_unit}{_fmt_p(_stop_p)}</div>"
                    f"<div style='font-size:9px;color:#64748b;margin-top:2px'>-{_pos['stop_pct']:.1f}% | 현재까지 {_dist_stop:+.2f}%</div>"
                    f"</div>"
                    f"<div style='background:#0a1a0a;border:1px solid #34d39940;border-radius:8px;padding:8px 10px;text-align:center'>"
                    f"<div style='font-size:9px;color:#34d399;font-weight:700;margin-bottom:4px'>🎯 1차 익절 ({'+' if _pos['t1_pct']>=0 else ''}{_pos['t1_pct']:.1f}%)</div>"
                    f"<div style='font-size:15px;font-weight:900;color:#34d399'>{_unit}{_fmt_p(_t1_p)}</div>"
                    f"<div style='font-size:9px;color:#64748b;margin-top:2px'>{'✅ 완료' if _pos.get('t1_done') else ('남은거리 ' + str(round(_dist_t1, 2)) + '%')}</div>"
                    f"</div>"
                    f"<div style='background:#1a1200;border:1px solid #fbbf2440;border-radius:8px;padding:8px 10px;text-align:center'>"
                    f"<div style='font-size:9px;color:#fbbf24;font-weight:700;margin-bottom:4px'>🚀 2차 익절 / 추격모드 ({'+' if _pos['t2_pct']>=0 else ''}{_pos['t2_pct']:.1f}%)</div>"
                    f"<div style='font-size:15px;font-weight:900;color:#fbbf24'>{_unit}{_fmt_p(_t2_p)}</div>"
                    f"<div style='font-size:9px;color:#64748b;margin-top:2px'>{'📈 추가매집 검토' if _t2_hit else ('남은거리 ' + str(round(_dist_t2, 2)) + '%')}</div>"
                    f"</div>"
                    f"</div>"
                    f"<div style='margin-bottom:4px'>"
                    f"<div style='display:flex;justify-content:space-between;font-size:9px;color:#475569;margin-bottom:2px'>"
                    f"<span>손절 {_unit}{_fmt_p(_stop_p)}</span><span>현재가</span><span>2차목표 {_unit}{_fmt_p(_t2_p)}</span>"
                    f"</div>"
                    f"<div style='background:#1e293b;border-radius:4px;height:8px;position:relative'>"
                    f"<div style='position:absolute;left:0;top:0;height:100%;width:{_cur_pos_pct:.1f}%;"
                    f"background:linear-gradient(90deg,#ef4444,#fbbf24,#34d399);border-radius:4px'></div>"
                    f"<div style='position:absolute;top:-4px;height:16px;width:3px;background:#f0f4ff;"
                    f"border-radius:2px;left:calc({_cur_pos_pct:.1f}% - 1px)'></div>"
                    f"</div>"
                    f"</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )

                # C1: uuid 기반 버튼 key + id 필터 삭제
                _btn_c1, _btn_c3 = st.columns([3, 1])   # 📝 수정(무동작) 제거
                with _btn_c1:
                    _t1_label = "✅ 1차 익절 완료 표시" if not _pos.get('t1_done') else "↩️ 1차 익절 취소"
                    def _toggle_t1(_pid=_pos_id):
                        for _pp in st.session_state[_op_key]:
                            if _pp.get('id', _pp['ticker']) == _pid:
                                _pp['t1_done'] = not _pp.get('t1_done', False)
                                break
                        _save_positions_to_ls()
                    st.button(_t1_label, key=f"op_t1_{_pos_id}", use_container_width=True,
                              on_click=_toggle_t1)
                with _btn_c3:
                    def _del_pos(_pid=_pos_id, _ptk=_tk, _pavg=_avg, _pqty=_qty):
                        # 청산 시 거래일지에 매도 기록
                        try:
                            _pname = resolve_korean_name(_ptk, _ptk)
                            _pcur, _, _ = _get_live_price(_ptk)
                            _sell_p = _pcur if _pcur else _pavg
                            log_trade(_ptk, _pname, "매도", _pqty, _sell_p, _sell_p,
                                      0, 0, memo="실전운용 청산")
                        except Exception:
                            pass
                        st.session_state[_op_key] = [p for p in st.session_state[_op_key]
                                                      if p.get('id', p['ticker']) != _pid]
                        _save_positions_to_ls()
                    st.button("🗑️ 청산", key=f"op_del_{_pos_id}", use_container_width=True,
                              type="secondary", on_click=_del_pos)

            # ── 핵심 원칙 고정 배너 ───────────────────────────────────────────
            _danger_html = (
                "<div style='background:#1a0505;border:2px solid #ef4444;border-radius:10px;"
                "padding:12px 18px;margin-top:8px;box-shadow:0 0 12px 2px rgba(239,68,68,0.5)'>"
                "<span style='color:#ef4444;font-size:14px;font-weight:900'>🚨 손절가 도달 종목 감지 — 즉각 매도 실행</span>"
                "</div>"
            ) if _has_danger else ""

            st.markdown(
                (_danger_html if _has_danger else "") +
                "<div style='background:#0d1117;border:2px solid #ef444460;border-radius:12px;"
                "padding:14px 20px;margin-top:16px'>"
                "<div style='font-size:13px;font-weight:900;color:#ef4444;margin-bottom:6px'>"
                "⚠️ 시스템 운영 핵심 원칙 — 항시 준수</div>"
                "<div style='font-size:12px;color:#94a3b8;line-height:2'>"
                "🛑 <b style='color:#f0f4ff'>손절가 도달 시, 전략 로테이션 순위와 무관하게 즉시 전량 매도</b><br>"
                "🎯 1차 익절 후 잔량은 <b style='color:#fbbf24'>2차 목표가까지 추격 보유</b><br>"
                "🔄 스위칭 전에 반드시 <b style='color:#34d399'>손절가 여유 3% 이상</b> 확인 후 실행<br>"
                "📊 순위 1위라도 손절가 근접 시 <b style='color:#ef4444'>빨간 경고등 점등 = 즉시 행동</b>"
                "</div>"
                "</div>",
                unsafe_allow_html=True
            )

with _tab_d1:
    # ── 시장 레짐 기반 전략 자동 추천 알림 (블랙아웃/폭락장 = 정찰 모드) ──
    try:
        _rg_d = detect_market_regime_for_strategy()
        _sb_black_d = not run_v891_system_check().get('can_enter', True)
        _rec_lbl_d = {"bounce": "📉 반등매매", "trend": "📈 추세매매", "bottom": "🎯 바닥확인"}.get(_rg_d["preset"], "🎯 바닥확인")
        if _sb_black_d or _rg_d["regime"] == "crash":
            st.warning(f"🚨 현재 시장 날씨는 **[{_rg_d['label']}]** — **[{_rec_lbl_d}]** 정찰 전략이 자동 추천/세팅되었습니다. "
                       f"(실매수 금지 · 관망/정찰 우선)")
        elif _rg_d["regime"] == "bull":
            st.info(f"📈 현재 시장 날씨는 **[{_rg_d['label']}]** — **[{_rec_lbl_d}]** 전략을 권장합니다.")
    except Exception:
        pass

    # ══════════════════════════════════════════════════════════════════════
    # [영역 1] 액션 브리핑 — st.columns(3) 메트릭 3개
    # ══════════════════════════════════════════════════════════════════════
    _ps = st.session_state.get('_live_pos_summary', {})
    _state_order = {"🚨 정리검토": 0, "✂️ 일부축소": 1, "📈 추가매집 검토": 2, "🛡️ 보유유지": 3}
    _sum_rows = sorted(_ps.values(), key=lambda r: _state_order.get(r['state'], 9))

    _total_pnl_pct = (
        sum(r['pnl'] for r in _sum_rows) / len(_sum_rows) if _sum_rows else 0.0
    )
    _cnt_clear = sum(1 for r in _sum_rows if '정리검토' in r.get('state', ''))
    _cnt_trim  = sum(1 for r in _sum_rows if '일부축소' in r.get('state', ''))

    _bc1, _bc2, _bc3 = st.columns(3)
    _bc1.metric(
        "총 평균 수익률",
        f"{_total_pnl_pct:+.2f}%",
        delta=f"{len(_sum_rows)}종목 보유",
    )
    _bc2.metric(
        "🚨 정리검토",
        f"{_cnt_clear}종목",
        delta="즉각 매도 검토" if _cnt_clear else "이상 없음",
        delta_color="inverse" if _cnt_clear else "off",
    )
    _bc3.metric(
        "✂️ 일부축소",
        f"{_cnt_trim}종목",
        delta="절반 익절 검토" if _cnt_trim else "이상 없음",
        delta_color="inverse" if _cnt_trim else "off",
    )

    # ══════════════════════════════════════════════════════════════════════
    # [영역 2] 메인 행동 테이블 — 5컬럼 단일 Styler
    # ══════════════════════════════════════════════════════════════════════
    if _sum_rows:
        _sum_df = pd.DataFrame([{
            '종목명':      r['name'],
            '현재가':      f"{r['cur']:,.0f}{r['unit']}",
            '수익률(%)':   r['pnl'],
            '🚦 현재 상태': r['state'],
            '🎯 기준가':   f"손절 {r['stop']:,.0f} / 목표 {r['t1']:,.0f}{r['unit']}",
        } for r in _sum_rows])

        def _tbl_row_style(row):
            s = row.get('🚦 현재 상태', '')
            if '정리검토' in s:
                return ['background-color:rgba(239,68,68,0.13);color:#fca5a5'] * len(row)
            if '일부축소' in s:
                return ['background-color:rgba(249,115,22,0.10);color:#fdba74'] * len(row)
            if '추가매집' in s:
                return ['background-color:rgba(52,211,153,0.08);color:#6ee7b7'] * len(row)
            return ['color:#475569'] * len(row)

        st.dataframe(
            _sum_df.style.apply(_tbl_row_style, axis=1),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("⚔️ 실전운용 탭에서 보유 종목을 등록하면 여기에 현황이 표시됩니다.")

    # ── 🔄 실시간 시세 강제 갱신 (Kill Switch) ──
    def _force_refresh_etf():
        """전체 데이터 캐시 초기화 — on_click 콜백 (렌더링 前 실행)."""
        try:
            st.cache_data.clear()
            st.session_state['_etf_refresh_ts'] = datetime.now().strftime('%H:%M:%S')
            st.session_state['_etf_refresh_ok'] = True
        except Exception as _ce:
            st.session_state['_etf_refresh_ok'] = False
            st.session_state['_etf_refresh_err'] = str(_ce)

    _rf_c1, _rf_c2 = st.columns([1.4, 4])
    with _rf_c1:
        st.button("🔄 실시간 시세 강제 갱신", key="etf_force_refresh_btn",
                  type="primary", use_container_width=True,
                  on_click=_force_refresh_etf,
                  help="전체 캐시를 비우고 최신 호가를 다시 불러옵니다")
    with _rf_c2:
        _last_rf = st.session_state.get('_etf_refresh_ts')
        if st.session_state.get('_etf_refresh_ok') is False:
            st.warning(f"⏳ API 호출 지연 중 — {st.session_state.get('_etf_refresh_err','')[:60]}")
        elif _last_rf:
            st.caption(f"🟢 마지막 강제 갱신: {_last_rf} · 시세 캐시 TTL 60초")
        else:
            st.caption("💡 시세가 멈춘 것 같으면 좌측 버튼으로 강제 갱신하세요 (캐시 TTL 60초)")

    # ── 투트랙(국장/미장) 라디오 — '전체 통합' 제거(15차 UI 다이어트) ──
    # 🔥 현재 우위 뱃지: 직전 렌더에서 저장한 국장/미장 1위 종합점수를 비교해
    #    더 높은 시장 옵션에만 표시. format_func로 '표시'만 바꾸므로 위젯 저장값은
    #    항상 안정("🇰🇷 국장 ETF"/"🇺🇸 미장 ETF") → 옵션 변화로 인한 선택 리셋 없음.
    _kr_sc = st.session_state.get('_kr_top_score')
    _us_sc = st.session_state.get('_us_top_score')
    def _fmt_etf_market(_opt):
        if _kr_sc is None or _us_sc is None:
            return _opt
        if _opt == "🇰🇷 국장 ETF" and _kr_sc > _us_sc:
            return f"{_opt}  (🔥 현재 우위 {int(_kr_sc)}점)"
        if _opt == "🇺🇸 미장 ETF" and _us_sc > _kr_sc:
            return f"{_opt}  (🔥 현재 우위 {int(_us_sc)}점)"
        return _opt
    _etf_market = st.radio("", ["🇰🇷 국장 ETF", "🇺🇸 미장 ETF"],
                           format_func=_fmt_etf_market, horizontal=True, key="etf_market_sel")

    # ── 탭 전환 시 데이터 클렌징(State Reset) ──
    # 국장↔미장 전환 즉시 순위 히스토리/통합 잔상을 비워, 이전 시장 데이터가
    # 하단 관제판에 섞이지 않도록 락(Lock). fetch_etf_data는 etf_list를 캐시키로
    # 쓰므로 시세 자체는 시장별로 분리되지만, 순위 히스토리는 명시적으로 초기화.
    if st.session_state.get('_etf_market_prev') != _etf_market:
        for _stale_k in ('_rh_kr', '_rh_us', '_rh_all'):
            st.session_state.pop(_stale_k, None)
        st.session_state['_etf_market_prev'] = _etf_market

    # ── ETF 리스트 정의 (국장 / 미장) ──
    # ⚠️ 매핑 정확성 최우선 — KRX 공식 종목코드 기준 (2024년 검증)
    _KR_ETF_LIST = [
        # ── 국내 지수 ──
        ("069500", "KODEX 200"),
        ("102110", "TIGER 200"),
        ("229200", "KODEX 코스닥150"),
        ("233740", "KODEX 코스닥150레버리지"),
        ("153130", "KODEX 단기채권PLUS"),
        # ── 미국 지수 추종 (국내상장) ──
        ("133690", "TIGER 나스닥100"),
        ("379800", "KODEX 미국S&P500TR"),
        ("360750", "TIGER 미국S&P500"),
        ("161490", "TIGER 미국나스닥100"),
        ("299030", "KODEX 미국나스닥100TR"),
        # ── 반도체 / IT ──
        ("091160", "KODEX 반도체"),
        ("395160", "KODEX AI반도체TOP2+"),
        ("396500", "TIGER Fn반도체TOP10"),   # ✅ KRX 공식 코드 396500 (수정: 441680은 오매핑)
        ("457450", "KODEX AI테크TOP10"),
        # ── 방산 / 중공업 ──
        ("463250", "TIGER K방산&우주"),
        ("364980", "TIGER 조선TOP10"),
        # ── 에너지 / 전력 ──
        ("487240", "KODEX AI전력핵심설비"),
        ("140710", "TIGER 원자력테마"),
        ("455890", "KODEX 원자력"),
        # ── 2차전지 ──
        ("305720", "KODEX 2차전지산업"),
        # ── 금 / 원자재 ──
        ("411060", "ACE KRX금현물"),
        ("132030", "KODEX 골드선물(H)"),
        # ── 채권 ──
        # 385560: TIGER 미국채10년선물 (Naver 검증 필요)
        # 308620: Naver 검증 결과 "KODEX 미국10년국채선물" → DB명 수정
        ("308620", "KODEX 미국10년국채선물"),
        # KODEX 미국채울트라30년선물(H) 코드 미확인 → 제거 (오매핑 방지)
        # ── 배당 ──
        ("266160", "KODEX 코스피고배당"),
        ("161510", "TIGER 배당성장"),
        # ── 헬스케어 / 바이오 ──
        ("143460", "TIGER 헬스케어"),
        ("143850", "TIGER 미국S&P500선물"),
    ]
    _KR_ETF_LIST = [(c, n) for c, n in _KR_ETF_LIST if c.isdigit() and len(c) == 6]

    _US_ETF_LIST = [
        # ── 주요 지수 ──
        ("SPY",  "SPDR S&P500"),
        ("QQQ",  "Invesco 나스닥100"),
        ("IWM",  "iShares 러셀2000"),
        ("DIA",  "SPDR 다우존스"),
        ("VTI",  "Vanguard 전체주식시장"),
        ("VOO",  "Vanguard S&P500"),
        # ── 섹터 ──
        ("XLK",  "Technology Select"),
        ("XLF",  "Financial Select"),
        ("XLE",  "Energy Select"),
        ("XLV",  "Health Care Select"),
        ("XLI",  "Industrials Select"),
        ("XLC",  "Communication Services"),
        ("XLY",  "Consumer Discretionary"),
        ("XLP",  "Consumer Staples"),
        ("XLU",  "Utilities Select"),
        ("XLB",  "Materials Select"),
        ("XLRE", "Real Estate Select"),
        # ── 테마 / 성장 ──
        ("SOXX", "iShares 반도체"),
        ("SMH",  "VanEck 반도체"),
        ("ARKK", "ARK 혁신"),
        ("ARKG", "ARK 유전체혁명"),
        ("BOTZ", "글로벌 로보틱스AI"),
        ("CIBR", "사이버보안"),
        ("HACK", "ETFMG 사이버보안"),
        ("CLOU", "글로벌 클라우드"),
        ("AIQ",  "글로벌 AI&테크"),
        ("ROBO", "Robo Global 로보틱스"),
        # ── 방산 ──
        ("ITA",  "iShares 방산항공"),
        ("PPA",  "Invesco 방산"),
        ("XAR",  "SPDR 방산항공"),
        # ── 에너지 / 원자재 ──
        ("GLD",  "SPDR 금"),
        ("SLV",  "iShares 은"),
        ("USO",  "미국 원유"),
        ("UNG",  "US 천연가스"),
        ("PDBC", "원자재 선물"),
        # ── 채권 ──
        ("TLT",  "iShares 장기국채 20+Y"),
        ("IEF",  "iShares 중기국채 7-10Y"),
        ("SHY",  "iShares 단기국채 1-3Y"),
        ("BND",  "Vanguard 총채권"),
        ("HYG",  "iShares 하이일드"),
        ("LQD",  "iShares 투자등급"),
        # ── 레버리지 / 인버스 ──
        ("TQQQ", "ProShares 나스닥100 3x"),
        ("SQQQ", "ProShares 나스닥100 -3x"),
        ("SPXL", "Direxion S&P500 3x"),
        ("SPXS", "Direxion S&P500 -3x"),
        ("SOXL", "Direxion 반도체 3x"),
        ("SOXS", "Direxion 반도체 -3x"),
        # ── 배당 ──
        ("JEPI", "JPMorgan 배당성장"),
        ("SCHD", "Schwab 배당"),
        ("VYM",  "Vanguard 고배당"),
        ("DVY",  "iShares 고배당"),
        # ── 국제 ──
        ("EWY",  "iShares MSCI 한국"),
        ("FXI",  "iShares MSCI 중국"),
        ("EWJ",  "iShares MSCI 일본"),
        ("VGK",  "Vanguard 유럽"),
        ("EEM",  "iShares 이머징"),
    ]

    # ── ETF 데이터 fetch 함수 (호출 전에 반드시 정의) ──

    # ETF 마스터 DB: 코드 → 공식명칭 매핑 (입력 데이터 무결성 검증용)
    # 전략탭 검증은 모듈 상단의 _MASTER_ETF_DB + check_ticker_integrity() 사용
    # 내부 DB가 외부 소스보다 항상 우선 (신뢰성 > 편의성)

    @st.cache_data(ttl=60, show_spinner=False)  # 실전 타점용 60초 단축
    def fetch_kr_etf_data():
        results = []
        _mismatch_log = []
        # batch download (rate-limit 회피) — 실패 시 개별 호출로 자동 폴백
        _kr_syms = [f"{t}.KS" for t, _ in _KR_ETF_LIST]
        _kr_batch = _batch_download_ohlcv(_kr_syms)
        for ticker, name in _KR_ETF_LIST:
            _sym = f"{ticker}.KS"
            # 마스터 DB 검증
            _v_ok, _v_exp, _v_msg = check_ticker_integrity(ticker, name)
            if not _v_ok:
                _mismatch_log.append((ticker, name, _v_exp))
            _ind = _calc_etf_indicators(_sym, prefetch_df=_kr_batch.get(_sym))
            if _ind:
                results.append({'코드': ticker, 'ETF명': name, '_validated': _v_ok,
                                '_expected_name': _v_exp, **_ind})
            else:
                results.append({'코드': ticker, 'ETF명': name, '_validated': _v_ok,
                                '_expected_name': _v_exp,
                                '현재가': 0, '등락(%)': 0, 'ADX': 0, 'RSI': 0, 'MACD': '',
                                'Z-Score': 0, '모멘텀(%)': 0, '거래량%': 0,
                                '정배열': '❌', '종합점수': 0, '상태': '오류'})
        if _mismatch_log:
            import logging as _lg
            for _mc, _mn, _me in _mismatch_log:
                _lg.warning("ETF 마스터 불일치: %s ('%s' ≠ '%s')", _mc, _mn, _me)
        return results

    @st.cache_data(ttl=60, show_spinner=False)  # 실전 타점용 60초 단축
    def fetch_us_etf_data():
        results = []
        # batch download (rate-limit 회피) — 56개 1회 요청, 실패 시 개별 폴백
        _us_syms = [t for t, _ in _US_ETF_LIST]
        _us_batch = _batch_download_ohlcv(_us_syms)
        for ticker, name in _US_ETF_LIST:
            _v_ok, _v_exp, _v_msg = check_ticker_integrity(ticker, name)
            _ind = _calc_etf_indicators(ticker, prefetch_df=_us_batch.get(ticker))
            if _ind:
                results.append({'코드': ticker, 'ETF명': name, '_validated': _v_ok,
                                '_expected_name': _v_exp, **_ind})
            else:
                results.append({'코드': ticker, 'ETF명': name, '_validated': _v_ok,
                                '_expected_name': _v_exp,
                                '현재가': 0, '등락(%)': 0, 'ADX': 0, 'RSI': 0, 'MACD': '',
                                'Z-Score': 0, '모멘텀(%)': 0, '거래량%': 0,
                                '정배열': '❌', '종합점수': 0, '상태': '오류'})
        return results

    # ── 시장별 분기: 라디오 토글에 따라 국장/미장/전체 랭킹판 표시 ──
    if _etf_market == "🇰🇷 국장 ETF":
        _cc1, _cc2 = st.columns([4, 1])
        with _cc2:
            if st.button("🔄 새로고침", key="kr_etf_refresh"):
                fetch_kr_etf_data.clear()
                st.rerun()

        with st.spinner("국장ETF 데이터 로딩 중..."):
            try:
                _kr_data = fetch_kr_etf_data()
            except Exception as _fe:
                st.warning(f"⏳ API 호출 지연 중 (Rate Limit 가능성) — 잠시 후 다시 시도하세요. [{type(_fe).__name__}]")
                st.toast("⏳ API 호출 지연 중", icon="⚠️")
                _kr_data = []

        if not _kr_data:
            # st.stop() 제거 — 랭킹만 건너뛰고 아래 백테스트/다른 섹션은 계속 렌더
            st.warning("⚠️ 국장 ETF 랭킹 로드 실패 (네트워크/지연) — 🔄 새로고침 후 재시도. 아래 섹션은 정상입니다.")
        if _kr_data:
            _df_kr = pd.DataFrame(_kr_data)
            _kr_active  = _df_kr[_df_kr['상태'] == '활성'].sort_values('종합점수', ascending=False)
            _kr_passive = _df_kr[_df_kr['상태'] != '활성']
            _kr_ranked  = pd.concat([_kr_active, _kr_passive]).reset_index(drop=True)
            # ── 하단 관제판 동기화: 카테고리 필터 전(全) 랭킹을 세션에 저장 ──
            #    (관제판/신규 진입 추천이 상단 스캐너 1위를 그대로 이어받도록)
            st.session_state['_scanner_ranked_kr'] = _kr_ranked.copy()
            st.session_state['_scanner_ranked_active'] = '_scanner_ranked_kr'

            _kr_cat = st.selectbox("카테고리 필터", ["전체", "국내지수", "미국지수추종", "반도체/IT", "방산/중공업", "에너지/전력", "2차전지", "금/원자재", "채권", "배당", "헬스케어"], key="kr_etf_cat")

            _cat_map = {
                "국내지수":    ["069500","102110","229200","233740","153130"],
                "미국지수추종":["133690","379800","360750","161490","299030"],
                "반도체/IT":   ["091160","395160","396500","457450"],
                "방산/중공업": ["463250","364980"],
                "에너지/전력": ["487240","140710","455890"],
                "2차전지":     ["305720"],
                "금/원자재":   ["411060","132030"],
                "채권":        ["308620"],
                "배당":        ["266160","161510"],
                "헬스케어":    ["143460"],
            }

            if _kr_cat != "전체":
                _filter_codes = _cat_map.get(_kr_cat, [])
                _kr_ranked = _kr_ranked[_kr_ranked['코드'].isin(_filter_codes)].reset_index(drop=True)

            _kr_m1, _kr_m2, _kr_m3, _kr_m4 = st.columns(4)
            _kr_m1.metric("전체 종목", len(_df_kr))
            _kr_m2.metric("활성 (ADX≥25)", len(_kr_active))
            _kr_top = _kr_active.iloc[0] if not _kr_active.empty else None
            if _kr_top is not None:
                _kr_m3.metric("1위 ETF", _kr_top['ETF명'])
                _kr_m4.metric("1위 점수", f"{int(_kr_top['종합점수'])}점")
                st.session_state['_kr_top_score'] = float(_kr_top['종합점수'])  # 🔥 우위 뱃지용

            if not _kr_active.empty:
                with st.expander("📊 TOP10 히트맵 보기", expanded=False):
                    _kr_top10 = _kr_active.head(10)
                    _kr_hm_fig = go.Figure(go.Bar(
                        x=_kr_top10['종합점수'],
                        y=[f"{r['ETF명']} ({r['코드']})" for _, r in _kr_top10.iterrows()],
                        orientation='h',
                        marker_color=['#ffd166' if i==0 else '#4da6ff' for i in range(len(_kr_top10))],
                        text=[f"{v}점" for v in _kr_top10['종합점수']],
                        textposition='inside',
                    ))
                    _kr_hm_fig.update_layout(
                        height=320, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                        font_color='#f0f4ff', xaxis_title='종합점수', yaxis_autorange='reversed',
                        margin=dict(l=0,r=0,t=10,b=0)
                    )
                    st.plotly_chart(_kr_hm_fig, use_container_width=True)

            _kr_rh = _update_rank_history(_kr_ranked, '_rh_kr')
            _render_etf_ranking(_kr_ranked, currency_symbol='원', key_prefix='kr_etf', show_add_btn=True, rank_history=_kr_rh)
            st.caption("종합점수 = ADX(25) + RSI(15) + MACD(20) + Z-Score(15) + 모멘텀(15) + 정배열(10) + 거래량(10) | ADX 25미만 자동 탈락")

            # ── 🔻 하방 압력 스캐너 (공매도 비중 · 대차잔고 · 순매도) ──────────
            with st.expander("🔻 하방 압력 스캐너 (공매도/대차잔고 — 숏 타겟 회피)", expanded=False):
                st.caption("상위 활성 ETF의 최근 3일 공매도 비중·대차잔고·수급을 추적. "
                           "공매도>10% AND 순매도 = 🔴 하방 위험 (진입 기각 대상)")
                _ds_rows = []
                for _, _dr in _kr_ranked[_kr_ranked['상태'] == '활성'].head(12).iterrows():
                    _dcode = str(_dr['종목코드']) if '종목코드' in _dr else str(_dr.get('코드', ''))
                    _ssd = get_short_selling_pressure(_dcode)
                    _blk, _lvl, _rsn = evaluate_downside_pressure(_ssd.get('short_ratio'), _ssd.get('net'))
                    _risk_lbl = {"danger": "🔴 위험", "watch": "🟡 주의", "safe": "🟢 안전"}.get(_lvl, "⚪ N/A")
                    _ds_rows.append({
                        '종목명': _dr['ETF명'],
                        '종합점수': int(_dr.get('종합점수', 0)),
                        '공매도 비중(%)': _ssd.get('short_ratio') if _ssd.get('short_ratio') is not None else '—',
                        '대차잔고': _ssd.get('borrow_trend') or '—',
                        '하방 위험도': _risk_lbl,
                        '_lvl': _lvl,
                    })
                if _ds_rows:
                    _ds_df = pd.DataFrame(_ds_rows)

                    def _ds_style(row):
                        if row.get('_lvl') == 'danger':
                            return ['background-color:rgba(239,68,68,0.14);color:#fca5a5'] * len(row)
                        if row.get('_lvl') == 'watch':
                            return ['background-color:rgba(251,191,36,0.10);color:#fde68a'] * len(row)
                        return [''] * len(row)

                    st.dataframe(
                        _ds_df.style.apply(_ds_style, axis=1),
                        use_container_width=True, hide_index=True,
                        column_config={"_lvl": None},   # 내부 판정 키 숨김
                    )
                else:
                    st.info("공매도 데이터를 불러오지 못했습니다 (KRX 지연 또는 비영업일).")

            # ── 🎯 개별종목 스나이핑 리스트 (ETF 선택 가능) ──
            if _kr_top is not None:
                st.markdown(f"---")
                st.markdown("### 🔫 개별종목 스나이핑 — 구성종목 타점 추적")

                # 국장 ETF 선택 — _ETF_HOLDINGS_DB 한국 코드 목록 + 랭킹 ETF 포함
                _kr_db_codes = [k for k in _ETF_HOLDINGS_DB if k.isdigit() and _ETF_HOLDINGS_DB[k]]
                _kr_snipe_opts = {}
                for _kc in _kr_db_codes:
                    # 조회 우선순위: 마스터 DB → 보충 매핑 → 코드 (숫자만 표기되던 버그 방지)
                    _kn = _MASTER_ETF_DB.get(_kc) or _HOLDINGS_ETF_NAMES.get(_kc) or _kc
                    _kr_snipe_opts[f"{_kn} ({_kc})"] = _kc
                # 랭킹에 있는 ETF도 추가 (DB에 없을 수 있음)
                for _, _rrow in _kr_ranked.iterrows():
                    _rc = str(_rrow['코드'])
                    if _rc.isdigit():
                        _rn = _rrow['ETF명']
                        _rlabel = f"{_rn} ({_rc})"
                        if _rlabel not in _kr_snipe_opts:
                            _kr_snipe_opts[_rlabel] = _rc
                _kr_snipe_labels = sorted(_kr_snipe_opts.keys())
                # 기본 선택: 현재 1위 ETF
                _default_label = f"{_kr_top['ETF명']} ({_kr_top['코드']})"
                _default_idx = _kr_snipe_labels.index(_default_label) if _default_label in _kr_snipe_labels else 0
                _sel_label = st.selectbox("📦 스캔할 ETF 선택 (구성종목 DB 보유 ETF)",
                                          _kr_snipe_labels, index=_default_idx,
                                          key="kr_snipe_etf_sel",
                                          help="구성종목 DB가 있는 ETF만 표시됩니다. 1위 ETF가 기본 선택.")
                _top_code = _kr_snipe_opts[_sel_label]
                _top_name = _sel_label.rsplit(" (", 1)[0]

                st.caption(f"{_top_name} 상위 구성종목 실시간 스캔 | 손절: 전일저가 or -5% (더 타이트한 쪽 자동 적용)")

                with st.spinner("구성종목 스캔 중..."):
                    _snipe_list = _scan_etf_holdings(_top_code, is_korean=True)

                if not _snipe_list:
                    st.info("구성종목 DB 없음 또는 데이터 로드 실패")
                else:
                    _fmt_p = lambda p: f"{int(p):,}원" if p >= 100 else f"{p:,.2f}"
                    for _h in _snipe_list:
                        st.markdown(
                            f"<div style='display:flex;justify-content:space-between;align-items:center;"
                            f"background:#111827;border-left:3px solid {_h['타점색']};border-radius:6px;"
                            f"padding:10px 14px;margin:4px 0'>"
                            f"<div>"
                            f"<b>{_h['종목명']}</b> <span style='color:#64748b;font-size:11px'>{_h['종목코드']}</span>"
                            f"<div style='font-size:11px;color:#94a3b8;margin-top:2px'>현재가 {_fmt_p(_h['현재가'])} · MA5이격 {_h['MA5이격']:+.1f}%</div>"
                            f"</div>"
                            f"<div style='text-align:center'>"
                            f"<div style='color:{_h['타점색']};font-weight:700;font-size:13px'>{_h['타점']}</div>"
                            f"<div style='font-size:11px;color:#64748b'>RSI {_h['RSI']} · Z {_h['Z-Score']:+.2f}</div>"
                            f"</div>"
                            f"<div style='text-align:right'>"
                            f"<div style='font-size:13px;font-weight:700'>R:R {_h['R:R']:.1f}</div>"
                            f"<div style='font-size:11px;color:#f43f5e'>손절 {_fmt_p(_h['손절가'])}</div>"
                            f"</div>"
                            f"</div>",
                            unsafe_allow_html=True
                        )

    elif _etf_market == "🇺🇸 미장 ETF":
        _uc1, _uc2 = st.columns([4, 1])
        with _uc2:
            if st.button("🔄 새로고침", key="us_etf_refresh"):
                fetch_us_etf_data.clear()
                st.rerun()

        _us_cat_options = ["전체", "주요지수", "섹터", "테마/성장", "방산", "에너지/원자재", "채권", "레버리지/인버스", "배당", "국제"]
        _us_cat = st.selectbox("카테고리 필터", _us_cat_options, key="us_etf_cat")

        _us_cat_map = {
            "주요지수":      ["SPY","QQQ","IWM","DIA","VTI","VOO"],
            "섹터":          ["XLK","XLF","XLE","XLV","XLI","XLC","XLY","XLP","XLU","XLB","XLRE"],
            "테마/성장":     ["SOXX","SMH","ARKK","ARKG","BOTZ","CIBR","HACK","CLOU","AIQ","ROBO"],
            "방산":          ["ITA","PPA","XAR"],
            "에너지/원자재": ["GLD","SLV","USO","UNG","PDBC"],
            "채권":          ["TLT","IEF","SHY","BND","HYG","LQD"],
            "레버리지/인버스":["TQQQ","SQQQ","SPXL","SPXS","SOXL","SOXS"],
            "배당":          ["JEPI","SCHD","VYM","DVY"],
            "국제":          ["EWY","FXI","EWJ","VGK","EEM"],
        }

        with st.spinner("미장ETF 데이터 로딩 중... (최대 30초)"):
            try:
                _us_data = fetch_us_etf_data()
            except Exception as _fe:
                st.warning(f"⏳ API 호출 지연 중 (Rate Limit 가능성) — 잠시 후 다시 시도하세요. [{type(_fe).__name__}]")
                st.toast("⏳ API 호출 지연 중", icon="⚠️")
                _us_data = []

        if not _us_data:
            st.warning("⚠️ 미장 ETF 랭킹 로드 실패 (네트워크/지연) — 🔄 새로고침 후 재시도. 아래 섹션은 정상입니다.")
        if _us_data:
            _df_us = pd.DataFrame(_us_data)
            _us_active  = _df_us[_df_us['상태'] == '활성'].sort_values('종합점수', ascending=False)
            _us_passive = _df_us[_df_us['상태'] != '활성']
            _us_ranked  = pd.concat([_us_active, _us_passive]).reset_index(drop=True)
            # ── 하단 관제판 동기화: 카테고리 필터 전(全) 랭킹을 세션에 저장 ──
            st.session_state['_scanner_ranked_us'] = _us_ranked.copy()
            st.session_state['_scanner_ranked_active'] = '_scanner_ranked_us'

            if _us_cat != "전체":
                _us_filter = _us_cat_map.get(_us_cat, [])
                _us_ranked = _us_ranked[_us_ranked['코드'].isin(_us_filter)].reset_index(drop=True)

            _us_m1, _us_m2, _us_m3, _us_m4 = st.columns(4)
            _us_m1.metric("전체 종목", len(_df_us))
            _us_m2.metric("활성 (ADX≥25)", len(_us_active))
            _us_top = _us_active.iloc[0] if not _us_active.empty else None
            if _us_top is not None:
                _us_m3.metric("1위 ETF", f"{_us_top['ETF명']} ({_us_top['코드']})")
                _us_m4.metric("1위 점수", f"{int(_us_top['종합점수'])}점")
                st.session_state['_us_top_score'] = float(_us_top['종합점수'])  # 🔥 우위 뱃지용

            if not _us_active.empty:
                with st.expander("📊 TOP10 히트맵 보기", expanded=False):
                    _top10 = _us_active.head(10)
                    _hm_fig = go.Figure(go.Bar(
                        x=_top10['종합점수'],
                        y=[f"{r['ETF명']} ({r['코드']})" for _, r in _top10.iterrows()],
                        orientation='h',
                        marker_color=['#ffd166' if i==0 else '#4da6ff' for i in range(len(_top10))],
                        text=[f"{v}점" for v in _top10['종합점수']],
                        textposition='inside',
                    ))
                    _hm_fig.update_layout(
                        height=320, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
                        font_color='#f0f4ff', xaxis_title='종합점수', yaxis_autorange='reversed',
                        margin=dict(l=0,r=0,t=10,b=0)
                    )
                    st.plotly_chart(_hm_fig, use_container_width=True)

            _us_rh = _update_rank_history(_us_ranked, '_rh_us')
            _render_etf_ranking(_us_ranked, currency_symbol='$', key_prefix='us_etf', show_add_btn=True, rank_history=_us_rh)
            st.caption("종합점수 = ADX(25) + RSI(15) + MACD(20) + Z-Score(15) + 모멘텀(15) + 정배열(10) + 거래량(10) | ADX 25미만 자동 탈락")

            # ── 🎯 개별종목 스나이핑 리스트 (미장 ETF 1위 구성종목) ──
            _us_top = _us_active.iloc[0] if not _us_active.empty else None
            if _us_top is not None:
                _us_top_code = str(_us_top['코드'])
                _us_top_name = _us_top['ETF명']
                st.markdown("---")
                st.markdown(f"### 🔫 개별종목 스나이핑 — `{_us_top_name}` 구성종목 타점 추적")
                st.caption(f"ETF 1위({_us_top_name}) 상위 구성종목 실시간 스캔 | 손절: 전일저가 or -5%")

                with st.spinner("구성종목 스캔 중..."):
                    _us_snipe = _scan_etf_holdings(_us_top_code, is_korean=False)

                if not _us_snipe:
                    st.info("구성종목 DB 없음 또는 데이터 로드 실패")
                else:
                    for _h in _us_snipe:
                        st.markdown(
                            f"<div style='display:flex;justify-content:space-between;align-items:center;"
                            f"background:#111827;border-left:3px solid {_h['타점색']};border-radius:6px;"
                            f"padding:10px 14px;margin:4px 0'>"
                            f"<div>"
                            f"<b>{_h['종목명']}</b> <span style='color:#64748b;font-size:11px'>{_h['종목코드']}</span>"
                            f"<div style='font-size:11px;color:#94a3b8;margin-top:2px'>현재가 ${_h['현재가']:,.2f} · MA5이격 {_h['MA5이격']:+.1f}%</div>"
                            f"</div>"
                            f"<div style='text-align:center'>"
                            f"<div style='color:{_h['타점색']};font-weight:700;font-size:13px'>{_h['타점']}</div>"
                            f"<div style='font-size:11px;color:#64748b'>RSI {_h['RSI']} · Z {_h['Z-Score']:+.2f}</div>"
                            f"</div>"
                            f"<div style='text-align:right'>"
                            f"<div style='font-size:13px;font-weight:700'>R:R {_h['R:R']:.1f}</div>"
                            f"<div style='font-size:11px;color:#f43f5e'>손절 ${_h['손절가']:,.2f}</div>"
                            f"</div>"
                            f"</div>",
                            unsafe_allow_html=True
                        )

    else:  # 🌐 전체 통합 (15차 UI 다이어트로 라디오에서 제거 — 도달 불가 레거시)
        st.markdown("### 🌐 국장+미장 ETF 통합 랭킹판")
        st.caption("국장ETF(원화) + 미장ETF(USD) 전체 통합 랭킹. 동일한 스코어링 엔진 적용.")

        _all_col1, _all_col2 = st.columns([4, 1])
        with _all_col2:
            if st.button("🔄 전체 새로고침", key="all_etf_refresh"):
                fetch_kr_etf_data.clear()
                fetch_us_etf_data.clear()
                st.rerun()

        with st.spinner("국장+미장 ETF 데이터 로딩 중... (최대 60초)"):
            _kr_data_all, _us_data_all = [], []
            try:
                _kr_data_all = fetch_kr_etf_data()
                _us_data_all = fetch_us_etf_data()
            except Exception as _fe:
                st.warning(f"⏳ API 호출 지연 중 (Rate Limit 가능성) — 일부 데이터만 표시될 수 있습니다. [{type(_fe).__name__}]")
                st.toast("⏳ API 호출 지연 중", icon="⚠️")

        _all_rows = []
        for r in (_kr_data_all or []):
            _all_rows.append({**r, '시장': '🇰🇷 국장'})
        for r in (_us_data_all or []):
            _all_rows.append({**r, '시장': '🇺🇸 미장'})

        if _all_rows:
            _df_all = pd.DataFrame(_all_rows)
            _all_active  = _df_all[_df_all['상태'] == '활성'].sort_values('종합점수', ascending=False)
            _all_passive = _df_all[_df_all['상태'] != '활성']
            _all_ranked  = pd.concat([_all_active, _all_passive]).reset_index(drop=True)

            _am1, _am2, _am3, _am4 = st.columns(4)
            _am1.metric("전체 종목", len(_df_all))
            _am2.metric("활성 (ADX≥25)", len(_all_active))
            _all_top = _all_active.iloc[0] if not _all_active.empty else None
            if _all_top is not None:
                _am3.metric("1위 ETF", f"{_all_top['ETF명']} ({_all_top['코드']})")
                _am4.metric("1위 점수", f"{int(_all_top['종합점수'])}점")

            _mkt_filter = st.selectbox("시장 필터", ["전체", "🇰🇷 국장", "🇺🇸 미장"], key="all_etf_mkt_filter")
            if _mkt_filter != "전체":
                _all_ranked = _all_ranked[_all_ranked['시장'] == _mkt_filter].reset_index(drop=True)

            # 1위 ETF 시장에 따라 통화 단위 결정
            _all_top_row = _all_ranked.iloc[0] if not _all_ranked.empty else None
            _all_top_sym = '$' if (_all_top_row is not None and _all_top_row.get('시장') == '🇺🇸 미장') else '원'
            _all_rh = _update_rank_history(_all_ranked, '_rh_all')
            _render_etf_ranking(_all_ranked, currency_symbol=_all_top_sym, key_prefix='all_etf', show_add_btn=True, rank_history=_all_rh)
            st.caption("종합점수 = ADX(25) + RSI(15) + MACD(20) + Z-Score(15) + 모멘텀(15) + 정배열(10) + 거래량(10) | ADX 25미만 자동 탈락")

    # 관제판 대상 = 상단 라디오(_etf_market) 선택에 따라 동적 스위칭
    _ETF_LIST_KR = [
        ("069500", "KODEX 200", "KS"), ("133690", "TIGER 나스닥100", "KS"),
        ("091160", "KODEX 반도체", "KS"), ("395160", "KODEX AI반도체TOP2+", "KS"),
        ("463250", "TIGER K방산&우주", "KS"), ("487240", "KODEX AI전력핵심설비", "KS"),
        ("411060", "ACE KRX금현물", "KS"), ("364980", "TIGER 조선TOP10", "KS"),
        ("305720", "KODEX 2차전지산업", "KS"), ("140710", "TIGER 원자력테마", "KS"),
    ]
    _ETF_LIST_US = [
        ("SPY", "SPDR S&P500", "US"), ("QQQ", "Invesco 나스닥100", "US"),
        ("DIA", "SPDR 다우존스", "US"), ("IWM", "iShares 러셀2000", "US"),
        ("XLK", "Technology Select", "US"), ("XLF", "Financial Select", "US"),
        ("XLE", "Energy Select", "US"), ("XLV", "Health Care Select", "US"),
        ("XLI", "Industrials Select", "US"), ("XLY", "Consumer Discretionary", "US"),
        ("XLP", "Consumer Staples", "US"), ("XLU", "Utilities Select", "US"),
        ("XLB", "Materials Select", "US"), ("SOXX", "iShares 반도체", "US"),
        ("SMH", "VanEck 반도체", "US"), ("GLD", "SPDR 금", "US"),
        ("TLT", "iShares 장기국채", "US"), ("ARKK", "ARK 혁신", "US"),
    ]
    if _etf_market == "🇺🇸 미장 ETF":
        ETF_LIST = _ETF_LIST_US
    elif _etf_market == "🌐 전체 통합":
        ETF_LIST = _ETF_LIST_KR + _ETF_LIST_US
    else:
        ETF_LIST = _ETF_LIST_KR

    @st.cache_data(ttl=60, show_spinner=False)  # 실전 타점용 60초 단축
    def fetch_etf_data(etf_list):
        import yfinance as yf
        import numpy as np
        results = []
        for ticker, name, mkt in etf_list:
            try:
                # 한국 6자리=.KS, 미국 티커=접미사 없음 (관제판 시장 동기화)
                _sym = f"{ticker}.KS" if (str(ticker).isdigit() and len(str(ticker)) == 6) else ticker
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
                _adx_raw = _dx.rolling(14).mean().iloc[-1]
                _adx  = round(float(np.nan_to_num(float(_adx_raw), nan=0.0)), 1)
                _adx  = min(100.0, max(0.0, _adx))

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

                # ── 거래량 비율(직전 20일 평균 대비, 당일 제외) ──
                _vol_avg20 = _vol.iloc[-21:-1].mean() if len(_vol) >= 21 else _vol.iloc[:-1].mean()
                _vol_r = round(_vol.iloc[-1] / _vol_avg20 * 100, 0) if _vol_avg20 > 0 else 100

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

                # 통화 인식(한국 6자리=원, 그 외=달러) — 소수점/타점 계산
                _is_kr_etf = str(ticker).isdigit() and len(str(ticker)) == 6
                _cur_e   = float(_cl.iloc[-1])
                _ma20_e  = float(_cl.tail(20).mean())
                _low5_e  = float(_cl.tail(5).min())
                # 눌림목 매수 타점: MA20·최근5일저가 중 낮은 값(현재가 아래). 지지선이
                # 현재가보다 높으면 현재가 -2.3% 눌림 대기 타점으로 대체.
                _entry_e = min(_ma20_e, _low5_e)
                if _entry_e >= _cur_e:
                    _entry_e = _cur_e * 0.977
                _nd_e = 0 if _is_kr_etf else 2

                results.append({
                    '종목코드':    ticker,
                    'ETF명':      name,
                    '현재가':     round(_cur_e, _nd_e),
                    '타점':       round(_entry_e, _nd_e),
                    '_원화':      _is_kr_etf,
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
                results.append({'종목코드':ticker,'ETF명':name,'현재가':0,'타점':0,'_원화':True,'등락(%)':0,
                                'ADX':0,'RSI':0,'MACD':'','Z-Score':0,
                                '모멘텀(%)':0,'거래량%':0,'BB위치':0,'52주위치':0,
                                '정배열':'❌','종합점수':0,'상태':'오류'})
        return results

    with st.spinner("ETF 데이터 로딩 중..."):
        try:
            _etf_data = fetch_etf_data(tuple(ETF_LIST))
        except Exception as _fe:
            st.warning(f"⏳ API 호출 지연 중 (Rate Limit 가능성) — 잠시 후 다시 시도하세요. [{type(_fe).__name__}]")
            st.toast("⏳ API 호출 지연 중", icon="⚠️")
            _etf_data = []

    if _etf_data:
        _df_etf  = pd.DataFrame(_etf_data)
        _active  = _df_etf[_df_etf['상태']=='활성'].sort_values('종합점수', ascending=False)
        _passive = _df_etf[_df_etf['상태']!='활성']
        _ranked  = pd.concat([_active, _passive]).reset_index(drop=True)

        # ══════════════════════════════════════════════════════════════════
        # 🔗 상단 스캐너 ↔ 하단 관제판 데이터 바인딩 일치 (고스트 버그 저격)
        #    관제판/신규 진입 추천이 '자체 fetch_etf_data(작은 유니버스)' 대신
        #    상단 메인 스캐너에서 최종 정렬된 1위 랭킹을 그대로 이어받도록 교체.
        #    → 상단 1위 KODEX 원자력이면 하단도 KODEX 원자력 (ACE금현물 잔상 제거)
        # ══════════════════════════════════════════════════════════════════
        _scan_key = '_scanner_ranked_kr' if _etf_market == "🇰🇷 국장 ETF" else '_scanner_ranked_us'
        _scan_df  = st.session_state.get(_scan_key)
        if _scan_df is not None and not _scan_df.empty:
            _is_kr_scan = (_etf_market == "🇰🇷 국장 ETF")
            _norm = _scan_df.copy()
            # 스키마 정규화: 코드→종목코드, _원화/타점 보강 (관제판 필드 요구사항)
            if '종목코드' not in _norm.columns and '코드' in _norm.columns:
                _norm['종목코드'] = _norm['코드'].astype(str)
            _norm['_원화'] = _is_kr_scan
            if '타점' not in _norm.columns:
                def _scan_entry(_r):
                    try:
                        _lv = calculate_trade_levels(
                            _r.get('현재가'), _r.get('MA5가격'), _r.get('전일종가'),
                            _r.get('갭(%)', 0), _r.get('MA5이격(%)', 0), _is_kr_scan)
                        return _lv['entry']
                    except Exception:
                        return _r.get('현재가', 0)
                _norm['타점'] = _norm.apply(_scan_entry, axis=1)
            _df_etf  = _norm
            _active  = _norm[_norm['상태']=='활성'].sort_values('종합점수', ascending=False).reset_index(drop=True)
            _passive = _norm[_norm['상태']!='활성']
            _ranked  = pd.concat([_active, _passive]).reset_index(drop=True)

        # ══════════════════════════════════════════
        # 🎯 실전 매매 관제판
        # ══════════════════════════════════════════
        st.markdown("### 🎯 실전 매매 관제판")
        st.caption("보유 중인 ETF와 매수가를 입력하면 지금 당장 홀드/스위칭 여부를 판단합니다.")

        # 현재 1위 ETF 정보
        _top1 = _active.iloc[0] if not _active.empty else None

        # ── 🗓️ 3거래일 연속 1위 룰 배지 (Whipsaw 방지) ──
        if _top1 is not None:
            try:
                _day_info = _get_rotation_day_count(str(_top1['종목코드']))
                _dc = _day_info["count"]

                # ── 🛡️ 신규 진입 절대 방어 조건 (시장 폭락 순위 왜곡 차단) ──
                # ⚠️ V6.1 FINAL CUT — 영구 동결(LOCK-IN). 임계값 변경 금지.
                #    [종합점수≥70 AND 정배열 AND MACD상승 AND 모멘텀>0]
                #    "폭락장에선 기회를 놓치더라도 잃지 않는 것이 최우선" — 사령관 지시.
                # 3일 연속 1위라도 아래 3조건 모두 충족해야 매수 신호 점등.
                _sw_score   = float(_top1.get('종합점수', 0))
                _sw_aligned = (str(_top1.get('정배열', '')) == '✅')          # 정배열 O
                _sw_macd    = str(_top1.get('MACD', ''))
                _sw_macd_up = ('상승' in _sw_macd) or ('골든크로스' in _sw_macd)  # MACD 상승
                _sw_mom     = float(_top1.get('모멘텀(%)', 0))

                _cond1_score = _sw_score >= 70                  # [1] 절대 점수 70점 이상
                _cond2_align = _sw_aligned                      # [2] 정배열 필수
                _cond3_trend = _sw_macd_up and _sw_mom > 0      # [3] MACD 상승 AND 모멘텀 양수

                # [4] 하방 압력 Kill Switch — 공매도 비중>10% AND 외인/기관 순매도 → 강제 기각
                _ss = get_short_selling_pressure(str(_top1['종목코드']))
                _ds_blocked, _ds_level, _ds_reason = evaluate_downside_pressure(
                    _ss.get("short_ratio"), _ss.get("net"))
                _cond4_downside = not _ds_blocked               # 하방 위험 아니어야 통과

                _switch_ok = _cond1_score and _cond2_align and _cond3_trend and _cond4_downside

                # 미충족 사유 수집 (경고 메시지용)
                _sw_fail = []
                if not _cond1_score:   _sw_fail.append(f"종합점수 {int(_sw_score)}점<70")
                if not _cond2_align:   _sw_fail.append("역배열(정배열 X)")
                if not _sw_macd_up:    _sw_fail.append(f"MACD {_sw_macd or '하락'}")
                if _sw_mom <= 0:       _sw_fail.append(f"모멘텀 {_sw_mom:+.1f}%≤0")
                if not _cond4_downside: _sw_fail.append(f"🔴 하방 압력 위험({_ds_reason})")

                # 일차별 배지 스타일 결정
                if _dc >= 3 and _switch_ok:
                    _db_bg     = "rgba(52,211,153,0.12)"
                    _db_border = "rgba(52,211,153,0.5)"
                    _db_icon   = "🟢"
                    _db_label  = f"연속 1위: {_dc}일차"
                    _db_msg    = "✨ 스위칭 조건 충족! 오늘 09:30 매수 집행"
                    _db_color  = "#34d399"
                elif _dc >= 3 and not _switch_ok:
                    # 3일차 도달했으나 방어 조건 미충족 → 매수 기각(Block)
                    _db_bg     = "rgba(239,68,68,0.10)"
                    _db_border = "rgba(239,68,68,0.5)"
                    _db_icon   = "🚫"
                    _db_label  = f"연속 1위: {_dc}일차 (신호 기각)"
                    _db_msg    = "⚠️ 시장 전체 폭락으로 인한 순위 왜곡 방지: 신규 매수 보류 — " + " / ".join(_sw_fail)
                    _db_color  = "#ef4444"
                elif _dc == 2:
                    _db_bg     = "rgba(251,191,36,0.09)"
                    _db_border = "rgba(251,191,36,0.45)"
                    _db_icon   = "🟡"
                    _db_label  = f"연속 1위: {_dc}일차"
                    _db_msg    = "검증 진행중 / 매수 보류"
                    _db_color  = "#fbbf24"
                else:
                    _db_bg     = "rgba(148,163,184,0.07)"
                    _db_border = "rgba(148,163,184,0.3)"
                    _db_icon   = "🟡"
                    _db_label  = "연속 1위: 1일차"
                    _db_msg    = "검증 대기중 / 매수 보류"
                    _db_color  = "#94a3b8"

                # 날짜 Lock 메모 (오늘 이미 기록 완료)
                _db_lock = " <span style='font-size:10px;color:#475569'>〔오늘 기록 확정〕</span>" if _day_info["is_locked"] else ""

                # 진행 바 (1칸=33%) — 3일차 완성 시각화
                _bar_filled = "".join(
                    [f"<div style='flex:1;height:6px;border-radius:3px;background:{_db_color}'></div>"
                     for _ in range(_dc)]
                )
                _bar_empty  = "".join(
                    [f"<div style='flex:1;height:6px;border-radius:3px;background:#1e293b'></div>"
                     for _ in range(3 - _dc)]
                )

                st.markdown(
                    f"<div style='background:{_db_bg};border:1px solid {_db_border};"
                    f"border-radius:12px;padding:14px 20px;margin:6px 0 14px 0'>"
                    f"<div style='display:flex;align-items:center;gap:12px;flex-wrap:wrap'>"
                    f"<div style='font-size:24px;line-height:1'>{_db_icon}</div>"
                    f"<div style='flex:1;min-width:200px'>"
                    f"<div style='font-size:15px;font-weight:800;color:{_db_color}'>"
                    f"[{_db_label}]{_db_lock} — {_db_msg}</div>"
                    f"<div style='font-size:12px;color:#64748b;margin-top:3px'>"
                    f"현재 1위: <b style='color:#e2e8f0'>{_top1['ETF명']} ({_top1['종목코드']})</b> "
                    f"· 종합점수 {int(_top1['종합점수'])}점 · 기준일 {_day_info['last_date']}</div>"
                    f"</div>"
                    f"<div style='display:flex;gap:4px;width:90px;align-self:center'>"
                    f"{_bar_filled}{_bar_empty}"
                    f"</div>"
                    f"</div></div>",
                    unsafe_allow_html=True
                )
            except Exception as _dce:
                pass  # 배지 렌더 실패는 조용히 무시

        # 보유 ETF 선택 — ETF 리스트에서 고르기
        _etf_names = [f"{r['ETF명']} ({r['종목코드']})" for _, r in _ranked.iterrows() if r['상태']=='활성']
        _etf_code_map = {f"{r['ETF명']} ({r['종목코드']})": r for _, r in _ranked.iterrows() if r['상태']=='활성'}

        _pc1, _pc2, _pc3 = st.columns(3)
        with _pc1:
            _hold_sel = st.selectbox("📦 보유 ETF", ["(없음 / 신규진입)"] + _etf_names, key="etf_hold_sel")
        with _pc2:
            _buy_price = st.number_input("💰 매수 평단가 (원)", min_value=0, value=0, step=100, key="etf_buy_price")
        with _pc3:
            _hold_qty  = st.number_input("📊 보유 수량", min_value=0, value=0, step=1, key="etf_hold_qty")

        if _hold_sel != "(없음 / 신규진입)" and _top1 is not None:
            _hold_row   = _etf_code_map[_hold_sel]
            _hold_code  = _hold_row['종목코드']
            _hold_name  = _hold_row['ETF명']
            _hold_price = float(_hold_row['현재가'])
            _hold_score = int(_hold_row['종합점수'])

            # 현재 보유 종목의 순위
            _active_list = _active.reset_index(drop=True)
            _hold_rank_list = _active_list[_active_list['종목코드']==_hold_code].index.tolist()
            _hold_rank = _hold_rank_list[0] + 1 if _hold_rank_list else 99

            _top1_score = int(_top1['종합점수'])
            _score_gap  = _top1_score - _hold_score

            # 손익 계산
            _pnl_pct  = (_hold_price / _buy_price - 1) * 100 if _buy_price > 0 else 0
            _pnl_amt  = (_hold_price - _buy_price) * _hold_qty if _buy_price > 0 and _hold_qty > 0 else 0

            # ── 판단 로직 ──
            # 우선순위: 손절 > 스위칭 > 주의 > 홀드
            if _buy_price > 0 and _pnl_pct <= -7:
                _signal = "STOP"
            elif _hold_rank >= 4:
                _signal = "SWITCH"
            elif _score_gap >= 20 and _hold_rank >= 3:
                _signal = "SWITCH"
            elif _hold_rank == 3 or _score_gap >= 15:
                _signal = "WATCH"
            else:
                _signal = "HOLD"

            _sig_cfg = {
                "HOLD":   ("🟢 홀드",    "#064e3b", "#34d399", "현재 1~2위 유지 중. 계속 보유하세요."),
                "WATCH":  ("🟡 주의",    "#422006", "#fbbf24", "3위권 진입 또는 1위와 점수 차이가 벌어지고 있습니다."),
                "SWITCH": ("🔴 스위칭",  "#450a0a", "#f87171", "보유 ETF 경쟁력 하락. 1위 ETF로 교체를 검토하세요."),
                "STOP":   ("⚫ 손절",    "#1c1c1c", "#94a3b8", "-7% 손절 라인 도달. 즉시 매도 후 재판단하세요."),
            }
            _sig_label, _sig_bg, _sig_color, _sig_msg = _sig_cfg[_signal]

            # 판단 카드 — 조건부 HTML을 사전 계산해 f-string 들여쓰기 문제 방지
            _gap_color  = "#f87171" if _score_gap >= 15 else "#94a3b8"
            _pnl_html   = ""
            _stop_html  = ""
            if _buy_price > 0:
                _pc = "#f43f5e" if _pnl_pct >= 0 else "#38bdf8"
                _pnl_html  = (f"<div><div style='font-size:11px;color:#64748b'>평가손익</div>"
                              f"<div style='font-size:14px;font-weight:700;color:{_pc}'>"
                              f"{_pnl_pct:+.2f}% ({_pnl_amt:+,.0f}원)</div></div>")
                _sp        = _buy_price * (1 - _STOP_LOSS_PCT)
                _stop_html = (f"<div><div style='font-size:11px;color:#64748b'>손절 라인</div>"
                              f"<div style='font-size:14px;font-weight:700;color:#f87171'>"
                              f"{_sp:,.0f}원 (-{int(_STOP_LOSS_PCT*100)}%)</div></div>")

            st.markdown(
                f"<div style='background:{_sig_bg};border:2px solid {_sig_color};"
                f"border-radius:14px;padding:20px 24px;margin:12px 0'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center;"
                f"flex-wrap:wrap;gap:12px'>"
                f"<div><div style='font-size:22px;font-weight:800;color:{_sig_color}'>{_sig_label}</div>"
                f"<div style='font-size:13px;color:#94a3b8;margin-top:4px'>{_sig_msg}</div></div>"
                f"<div style='text-align:right'>"
                f"<div style='font-size:12px;color:#64748b'>현재 순위</div>"
                f"<div style='font-size:28px;font-weight:800;color:{_sig_color}'>{_hold_rank}위</div>"
                f"</div></div>"
                f"<div style='display:flex;gap:24px;margin-top:16px;flex-wrap:wrap'>"
                f"<div><div style='font-size:11px;color:#64748b'>보유 ETF</div>"
                f"<div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_hold_name}</div></div>"
                f"<div><div style='font-size:11px;color:#64748b'>현재가</div>"
                f"<div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_hold_price:,.0f}원</div></div>"
                f"{_pnl_html}"
                f"<div><div style='font-size:11px;color:#64748b'>보유 점수</div>"
                f"<div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_hold_score}점</div></div>"
                f"<div><div style='font-size:11px;color:#64748b'>1위와 차이</div>"
                f"<div style='font-size:14px;font-weight:700;color:{_gap_color}'>{_score_gap:+d}점</div></div>"
                f"{_stop_html}"
                f"</div></div>",
                unsafe_allow_html=True
            )

            # ── ETF 차트 + 매도 신호 ──
            try:
                _is_kr_etf = _hold_code.isdigit()
                _chart_sym  = f"{_hold_code}.KS" if _is_kr_etf else _hold_code
                _ch = fetch_ohlcv(_hold_code if _is_kr_etf else _hold_code, 120)
                if _ch is None or len(_ch) < 30:
                    import yfinance as _yf_ec
                    _raw = _yf_ec.Ticker(_chart_sym).history(period="6mo", interval="1d")
                    if _raw is not None and not _raw.empty:
                        _ch = _raw.rename(columns={'Open':'시가','High':'고가','Low':'저가','Close':'종가','Volume':'거래량'})[['시가','고가','저가','종가','거래량']]
                if _ch is not None and len(_ch) >= 30:
                    _ch_c = _ch['종가'].astype(float)
                    _ch_h = _ch['고가'].astype(float)
                    _ch_l = _ch['저가'].astype(float)
                    _ch_o = _ch['시가'].astype(float)
                    _ch_v = _ch['거래량'].astype(float)
                    _ch_idx = _ch.index

                    # MA 계산
                    _ma5  = _ch_c.rolling(5).mean()
                    _ma20 = _ch_c.rolling(20).mean()
                    _ma60 = _ch_c.rolling(60).mean()

                    # ADX(14) for sell signal
                    _tr2 = pd.concat([_ch_h-_ch_l,(_ch_h-_ch_c.shift()).abs(),(_ch_l-_ch_c.shift()).abs()],axis=1).max(axis=1)
                    _atr2 = _tr2.rolling(14).mean().replace(0, float('nan'))
                    _pdm2 = _ch_h.diff().clip(lower=0)
                    _ndm2 = (-_ch_l.diff()).clip(lower=0)
                    _pdi2 = 100*_pdm2.rolling(14).mean()/_atr2
                    _ndi2 = 100*_ndm2.rolling(14).mean()/_atr2
                    _dx2  = 100*(_pdi2-_ndi2).abs()/(_pdi2+_ndi2).replace(0,float('nan'))
                    _adx2 = float(_dx2.rolling(14).mean().iloc[-1])

                    # RSI(14)
                    _d2   = _ch_c.diff()
                    _rsi2 = float((100 - 100/(1+_d2.clip(lower=0).rolling(14).mean()/_d2.clip(upper=0).abs().rolling(14).mean().replace(0,float('nan')))).iloc[-1])

                    # MACD
                    _macd2    = _ch_c.ewm(span=12).mean() - _ch_c.ewm(span=26).mean()
                    _macd2sig = _macd2.ewm(span=9).mean()
                    _macd2_v  = float(_macd2.iloc[-1]); _macd2_p = float(_macd2.iloc[-2])
                    _sig2_v   = float(_macd2sig.iloc[-1]); _sig2_p = float(_macd2sig.iloc[-2])
                    _macd_dead = (_macd2_v < _sig2_v and _macd2_p >= _sig2_p)

                    # 매도 신호 판단
                    _sell_signals = []
                    if _adx2 < 25:
                        _sell_signals.append(("🔴 ADX 추세 소멸", f"ADX {_adx2:.1f} < 25 — 추세 종료, 전량 현금화 검토"))
                    if _rsi2 >= 78:
                        _sell_signals.append(("🟠 RSI 과매수", f"RSI {_rsi2:.1f} ≥ 78 — 단기 과열, 부분 익절 검토"))
                    if _macd_dead:
                        _sell_signals.append(("🟡 MACD 데드크로스", "추세 전환 신호 — 다음날 재확인"))
                    if _buy_price > 0 and _pnl_pct >= 15:
                        _sell_signals.append(("💰 +15% 익절 구간", f"수익률 {_pnl_pct:+.1f}% — 절반 익절 후 나머지 추세 추종"))

                    if _sell_signals:
                        for _stitle, _smsg in _sell_signals:
                            st.warning(f"**{_stitle}** — {_smsg}")
                    else:
                        st.success("✅ 매도 신호 없음 — 현재 추세 지속 중")

                    # 차트
                    with st.expander(f"📈 {_hold_name} 차트 보기", expanded=True):
                        _cf = go.Figure()
                        _cf.add_trace(go.Candlestick(x=_ch_idx, open=_ch_o, high=_ch_h, low=_ch_l, close=_ch_c,
                            increasing_line_color='#f63d68', decreasing_line_color='#4da6ff',
                            increasing_fillcolor='#f63d68', decreasing_fillcolor='#4da6ff', name='가격'))
                        _cf.add_trace(go.Scatter(x=_ch_idx, y=_ma5,  line=dict(color='#ffd166',width=1), name='MA5'))
                        _cf.add_trace(go.Scatter(x=_ch_idx, y=_ma20, line=dict(color='#a78bfa',width=1.5), name='MA20'))
                        _cf.add_trace(go.Scatter(x=_ch_idx, y=_ma60, line=dict(color='#38bdf8',width=1.5), name='MA60'))
                        # 매수가 라인
                        if _buy_price > 0:
                            _cf.add_hline(y=_buy_price, line_color='#34d399', line_dash='dash', line_width=1.5,
                                annotation_text=f"매수가 {_buy_price:,.0f}", annotation_position="left")
                            _cf.add_hline(y=_buy_price*0.93, line_color='#f87171', line_dash='dot', line_width=1,
                                annotation_text="손절 -7%", annotation_position="left")
                        _cur_p = float(_ch_c.iloc[-1])
                        _n60 = min(60, len(_ch_c))
                        _ylo = float(_ch_l.iloc[-_n60:].min()); _yhi = float(_ch_h.iloc[-_n60:].max())
                        _ypad = (_yhi - _ylo) * 0.08
                        if _buy_price > 0:
                            _ylo = min(_ylo, _buy_price * 0.91)
                            _yhi = max(_yhi, _buy_price * 1.05)
                        _cf.update_layout(
                            height=380, paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='#0d1117',
                            font_color='#f0f4ff', xaxis_rangeslider_visible=False,
                            margin=dict(l=0,r=0,t=10,b=0),
                            legend=dict(orientation='h', yanchor='bottom', y=1.02),
                            yaxis=dict(range=[_ylo-_ypad, _yhi+_ypad*1.5], gridcolor='rgba(255,255,255,0.05)')
                        )
                        _cf.update_xaxes(gridcolor='rgba(255,255,255,0.05)')
                        st.plotly_chart(_cf, use_container_width=True)
            except Exception as _ec:
                st.caption(f"차트 로딩 실패: {_ec}")

            # 스위칭 대상 안내
            if _signal in ("SWITCH", "WATCH") and _top1['종목코드'] != _hold_code:
                st.markdown(f"""
<div style='background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.3);border-radius:12px;padding:16px 20px;margin-bottom:12px'>
  <div style='font-size:13px;color:#a5b4fc;font-weight:700;margin-bottom:8px'>🎯 스위칭 대상 (현재 1위)</div>
  <div style='display:flex;gap:24px;flex-wrap:wrap'>
    <div><div style='font-size:11px;color:#64748b'>ETF명</div>
         <div style='font-size:15px;font-weight:800;color:#f0f4ff'>{_top1['ETF명']}</div></div>
    <div><div style='font-size:11px;color:#64748b'>현재가</div>
         <div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_top1['현재가']:,.0f}원</div></div>
    <div><div style='font-size:11px;color:#64748b'>종합점수</div>
         <div style='font-size:14px;font-weight:700;color:#fbbf24'>{_top1['종합점수']}점</div></div>
    <div><div style='font-size:11px;color:#64748b'>MACD</div>
         <div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_top1['MACD']}</div></div>
    <div><div style='font-size:11px;color:#64748b'>모멘텀</div>
         <div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_top1["모멘텀(%)"]:+.1f}%</div></div>
  </div>
</div>""", unsafe_allow_html=True)

        elif _hold_sel == "(없음 / 신규진입)" and _top1 is not None:
            # 신규 진입 안내 — 통화 인식 + 눌림목 타점 가이드
            _t1_kr   = bool(_top1.get('_원화', True))
            _t1_u    = "원" if _t1_kr else "$"
            _t1_cur  = float(_top1.get('현재가', 0) or 0)
            _t1_ent  = float(_top1.get('타점', 0) or 0)
            _fmt_e   = (lambda v: f"{v:,.0f}원") if _t1_kr else (lambda v: f"${v:,.2f}")
            _pullback = (not _t1_kr) and _t1_ent > 0 and _t1_cur > _t1_ent   # 미장 & 현재가>타점 = 눌림목 대기
            st.markdown(f"""
<div style='background:rgba(52,211,153,0.07);border:1px solid rgba(52,211,153,0.25);border-radius:12px;padding:16px 20px;margin-bottom:12px'>
  <div style='font-size:13px;color:#34d399;font-weight:700;margin-bottom:8px'>🟢 신규 진입 추천 (현재 1위 · {_etf_market})</div>
  <div style='display:flex;gap:24px;flex-wrap:wrap'>
    <div><div style='font-size:11px;color:#64748b'>ETF명</div>
         <div style='font-size:15px;font-weight:800;color:#f0f4ff'>{_top1['ETF명']}</div></div>
    <div><div style='font-size:11px;color:#64748b'>현재가</div>
         <div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_fmt_e(_t1_cur)}</div></div>
    <div><div style='font-size:11px;color:#64748b'>🎯 매수 타점</div>
         <div style='font-size:14px;font-weight:700;color:#fbbf24'>{_fmt_e(_t1_ent)}</div></div>
    <div><div style='font-size:11px;color:#64748b'>종합점수</div>
         <div style='font-size:14px;font-weight:700;color:#fbbf24'>{_top1['종합점수']}점</div></div>
    <div><div style='font-size:11px;color:#64748b'>ADX(추세강도)</div>
         <div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_top1['ADX']}</div></div>
    <div><div style='font-size:11px;color:#64748b'>모멘텀</div>
         <div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_top1["모멘텀(%)"]:+.1f}%</div></div>
  </div>
</div>""", unsafe_allow_html=True)
            if _pullback:
                st.warning(
                    f"⏳ 3일 연속 조건은 만족해가나, 현재 **눌림목 대기 상태**입니다. "
                    f"오늘 밤 미국 장에 타점가 **{_fmt_e(_t1_ent)}**로 지정가 예약 매수를 걸어두십시오.")
            elif not _t1_kr:
                st.success(f"✅ 현재가가 타점({_fmt_e(_t1_ent)}) 이하 — 진입 유효 구간입니다.")

        # 스위칭 규칙 요약
        with st.expander("📋 스위칭 규칙 보기"):
            st.markdown("""
| 신호 | 조건 | 액션 |
|------|------|------|
| 🟢 홀드 | 보유 ETF 1~2위 유지 & 1위와 점수차 15점 미만 | 계속 보유 |
| 🟡 주의 | 3위 진입 OR 1위와 점수차 15점 이상 | 모니터링 강화, 다음날 재확인 |
| 🔴 스위칭 | 4위 이하 진입 OR 점수차 20점 이상 | 장 시작 후 현재 1위로 교체 |
| ⚫ 손절 | 매수가 대비 -7% 이하 | 즉시 매도, 당일 재진입 금지 |

**🗓️ 3거래일 연속 1위 룰 (Whipsaw 방지)**

| 일차 | 뱃지 | 의미 | 액션 |
|------|------|------|------|
| 🟡 1일차 | `[연속 1위: 1일차]` | 오늘 처음 1위 진입 | 매수 보류, 내일 재확인 |
| 🟡 2일차 | `[연속 1위: 2일차]` | 2거래일 연속 1위 | 매수 보류, 내일 최종 확인 |
| 🟢 3일차 | `[연속 1위: 3일차]` | ✨ 스위칭 조건 최종 충족 | **09:30 매수 집행** |

> 중간에 단 하루라도 1위 티커가 바뀌면 카운트는 즉시 1일차로 리셋됩니다.
> 카운트 기준은 **날짜 단위 1일 1회** 고정 — 장중 새로고침 횟수와 무관합니다.

**💡 실전 팁**
- 스위칭은 **당일 장 시작 후 10분 뒤** 체결 (09:30 이후)
- 하루에 한 번만 확인 — 매일 09:30 또는 장 마감 후
- 수수료 + 세금 고려 시 스위칭 최소 간격: **2주 이상**
""")

        st.divider()

        # ── 🛡️ 백테스팅 전체를 Expander로 격리 (일간 스캐너와 시각 충돌 방지) ──
        with st.expander("🛡️ [중장기 참고용] 월간 ETF 로테이션 백테스팅 결과 보기", expanded=False):
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
            def run_etf_backtest(fee_buy, fee_sell, slip):
                import yfinance as yf
                import numpy as np

                _buy_cost  = (fee_buy  + slip) / 100
                _sell_cost = (fee_sell + slip) / 100

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
                # ── ⛔ Look-ahead Bias 차단: 월봉이 '완전히 마감된' 과거 달까지만 사용 ──
                #    yfinance 1mo 봉은 진행 중인 당월(예: 2026-07)도 포함 → 부분 데이터가
                #    수익률/추천에 선반영됨. KST 기준 '이번 달 1일' 이전 봉만 남긴다.
                _kst_now  = datetime.utcnow() + timedelta(hours=9)
                _cur_ym   = (_kst_now.year, _kst_now.month)
                _dates    = [d for d in _dates if (d.year, d.month) < _cur_ym]
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

                # ── 이번 달(진행 중) 추천 종목 — 최근 3개월 완료분 모멘텀 기준 ──
                _next_pick = None
                try:
                    _recent = _dates[-3:]
                    _ns = {}
                    for t in _all_tickers:
                        _rd = dict(zip(_monthly[t]['returns'].index, _monthly[t]['returns']))
                        _ns[t] = sum(_rd.get(d, 0) for d in _recent)
                    if _ns and _dates:
                        _nb = max(_ns, key=_ns.get)
                        _nm_dt = (_dates[-1].to_pydatetime().replace(day=1) + timedelta(days=32)).replace(day=1)
                        # 추천 월이 '현재 진행 중인 이번 달'을 넘어서면(미래) 렌더 차단
                        if (_nm_dt.year, _nm_dt.month) <= _cur_ym:
                            _next_pick = {'month': _nm_dt.strftime('%Y-%m'), 'name': _monthly[_nb]['name']}
                except Exception:
                    _next_pick = None

                return {
                    'dates':        _dates[3:],
                    'next_pick':    _next_pick,
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
                _bt = run_etf_backtest(_fee_buy, _fee_sell, _slip)

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
                    # 이번 달(진행 중) 추천 — 월봉 미완성이라 누적 수익률은 '진행중'
                    _np = _bt.get('next_pick')
                    if _np and _np.get('name'):
                        _hist_rows.append({
                            '월': f"{_np.get('month','')} (진행중)",
                            '선택 ETF': f"🎯 {_np['name']}",
                            '전략 누적(%)': "집계중",
                            '코스피 누적(%)': "집계중",
                        })
                    st.dataframe(pd.DataFrame(_hist_rows), use_container_width=True, hide_index=True)
                    if _np and _np.get('name'):
                        st.caption(f"🎯 이번 달({_np.get('month','')}) 추천: **{_np['name']}** — "
                                   "월봉이 끝나야 수익률이 확정되므로 누적은 '집계중'으로 표시됩니다.")

                if st.button("🔄 백테스팅 재실행", key="bt_rerun"):
                    run_etf_backtest.clear()
                    st.rerun()
            else:
                st.warning("백테스팅 데이터 부족 (2년 데이터 필요)")

    # ══════════════════════════════════════════════════════════════════════
    # [영역 3] 퀀트 엔진 백엔드 (평소 닫아둠)
    # ══════════════════════════════════════════════════════════════════════
    with st.expander("⚙️ 시스템 백엔드 데이터 및 상세 지표 (평소 닫아둠)", expanded=False):

      # ── 개별 종목 ADX / RSI 원시값 ──
      if _sum_rows:
        for _srow in _sum_rows:
            _tk2 = _srow['name']
            _adx2, _rsi2 = _get_adx_rsi_pos(_tk2, _tk2.isdigit() and len(_tk2) == 6)
            st.markdown(
                f"**{_tk2}** — ADX: `{_adx2 or '?'}` | RSI: `{_rsi2 or '?'}` | "
                f"손절가: `{_srow['stop']:,.0f}{_srow['unit']}` | "
                f"1차목표: `{_srow['t1']:,.0f}{_srow['unit']}` | "
                f"2차목표: `{_srow['t2']:,.0f}{_srow['unit']}`"
            )
        st.divider()

      # ── ETF 로테이션 종합 랭킹판 및 AI 최적화 ──

    # ── 🔥 AI 파라미터 자동 최적화 (Walk-Forward) — ETF 로테이션 ──
    with st.expander("🔥 AI 파라미터 자동 최적화 (Walk-Forward) — ETF 로테이션", expanded=False):
        st.markdown("""
**Walk-Forward Grid Search** — ETF 로테이션 핵심 파라미터를 과거 데이터로 자동 튜닝합니다.

| 파라미터 | 탐색 범위 | 설명 |
|---|---|---|
| 모멘텀 룩백 | 1~6개월 | 순위 산정 기준 기간 |
| ADX 임계값 | 15~30 | 추세 강도 필터 기준 |
| RSI 과열 기준 | 70~85 | 부분 익절 트리거 |
| In-sample | 선택 기간 × 2/3 | 파라미터 학습 구간 |
| Out-of-sample | 선택 기간 × 1/3 | 과적합 검증 구간 |
        """)

        _wf_etf_c1, _wf_etf_c2, _wf_etf_c3 = st.columns([2, 1, 1])
        with _wf_etf_c1:
            _wf_etf_months = st.slider("백테스트 기간 (개월)", 6, 24, 12, key="wf_etf_months",
                                        help="길수록 안정적이지만 속도가 느립니다")
        with _wf_etf_c2:
            _wf_etf_market = st.selectbox("대상 ETF", ["🇰🇷 국장 ETF", "🇺🇸 미장 ETF", "🌐 전체 통합"],
                                           key="wf_etf_market_sel")
        with _wf_etf_c3:
            st.markdown("<br>", unsafe_allow_html=True)
            _run_wf_etf = st.button("🔥 ETF 최적화 시작", use_container_width=True,
                                     type="primary", key="run_wf_etf")

        # 현재 적용 파라미터 표시
        _cur_wf_mom  = st.session_state.get("wf_etf_best_momentum_months", 3)
        _cur_wf_adx  = st.session_state.get("wf_etf_best_adx", 25)
        _cur_wf_rsi  = st.session_state.get("wf_etf_best_rsi_ob", 78)
        st.info(f"📌 현재 적용 파라미터 — 모멘텀 룩백: **{_cur_wf_mom}개월** | ADX 임계값: **{_cur_wf_adx}** | RSI 과열: **{_cur_wf_rsi}**")

        if _run_wf_etf:
            import itertools as _itertools_wf
            import numpy as _np_wf
            import yfinance as _yf_wf

            # 대상 ETF 목록 선택
            if "국장" in _wf_etf_market:
                _wf_etf_targets = [("379800", "KODEX 미국S&P500TR"), ("069500", "KODEX 200"),
                                    ("229200", "KODEX 코스닥150"), ("114800", "KODEX 인버스"),
                                    ("102110", "TIGER 200"), ("251340", "KODEX 코스피100"),
                                    ("395160", "KODEX AI반도체TOP2+"), ("396500", "TIGER Fn반도체TOP10"),
                                    ("381170", "TIGER 미국테크TOP10 INDXX"), ("148020", "KBSTAR 200")]
                _wf_suffix = ".KS"
            elif "미장" in _wf_etf_market:
                _wf_etf_targets = [("SPY","S&P500"), ("QQQ","나스닥100"), ("IWM","러셀2000"),
                                    ("VTI","전체주식시장"), ("SOXX","반도체"), ("XLK","테크"),
                                    ("GLD","금"), ("TLT","장기국채"), ("SCHD","배당"), ("ITA","방산")]
                _wf_suffix = ""
            else:
                _wf_etf_targets = [("SPY","S&P500"), ("QQQ","나스닥100"), ("VTI","전체주식시장"),
                                    ("SOXX","반도체"), ("GLD","금"), ("TLT","장기국채"),
                                    ("379800","KODEX S&P500"), ("069500","KODEX 200"),
                                    ("395160","KODEX AI반도체"), ("396500","TIGER 반도체TOP10")]
                _wf_suffix = ""

            # 그리드 파라미터 정의
            _mom_grid = [1, 2, 3, 4, 6]        # 모멘텀 룩백 (개월)
            _adx_grid = [15, 20, 25, 30]        # ADX 임계값
            _rsi_grid = [70, 73, 76, 78, 80, 85] # RSI 과열 기준

            _total_combos = len(_mom_grid) * len(_adx_grid) * len(_rsi_grid)

            st.markdown("**① ETF 데이터 다운로드 중...**")
            _wf_etf_dl_prog = st.progress(0)
            _wf_etf_status  = st.empty()

            # 데이터 수집
            _wf_etf_data = {}
            for _wi, (_wtick, _wname) in enumerate(_wf_etf_targets):
                try:
                    _wsym = _wtick + (".KS" if _wtick.isdigit() else "")
                    _wdf  = _yf_wf.Ticker(_wsym).history(period=f"{_wf_etf_months}mo", interval="1mo")
                    if _wdf is not None and len(_wdf) >= 4:
                        _wdf = _wdf[['Close']].dropna()
                        _wdf['ret'] = _wdf['Close'].pct_change()
                        _wdf['adx_proxy'] = _wdf['Close'].pct_change().abs().rolling(3).mean() * 100
                        _wdf['rsi14'] = _wdf['Close'].ewm(span=14).mean().pct_change()
                        _wf_etf_data[_wtick] = _wdf
                except Exception:
                    pass
                _wf_etf_dl_prog.progress((_wi + 1) / len(_wf_etf_targets))
                _wf_etf_status.caption(f"{_wi+1}/{len(_wf_etf_targets)} ETF 다운로드 중...")

            _wf_etf_dl_prog.progress(1.0)
            _wf_etf_status.caption(f"✅ {len(_wf_etf_data)}/{len(_wf_etf_targets)} ETF 데이터 로드 완료")

            if len(_wf_etf_data) < 3:
                st.error("ETF 데이터를 충분히 가져오지 못했습니다. 네트워크를 확인해주세요.")
            else:
                st.markdown("**② Walk-Forward Grid Search 실행 중...**")
                _wf_etf_gs_prog   = st.progress(0)
                _wf_etf_gs_status = st.empty()

                # Walk-Forward 분할: in-sample 2/3, out-of-sample 1/3
                _all_dates_wf = None
                for _wt in _wf_etf_data.values():
                    _idx = set(_wt.index)
                    _all_dates_wf = _idx if _all_dates_wf is None else _all_dates_wf & _idx
                _all_dates_wf = sorted(_all_dates_wf)

                _split_wf   = int(len(_all_dates_wf) * 2 / 3)
                _in_dates   = _all_dates_wf[:_split_wf]
                _out_dates  = _all_dates_wf[_split_wf:]

                if len(_in_dates) < 3 or len(_out_dates) < 1:
                    st.error("데이터 기간이 너무 짧습니다. 백테스트 기간을 늘려주세요.")
                else:
                    def _wf_etf_score(mom_m, adx_th, rsi_ob, dates_subset):
                        """단순화된 모멘텀 스코어 기반 수익률 시뮬레이션."""
                        _port = [1.0]
                        _prev_pick = None
                        for _di in range(mom_m, len(dates_subset)):
                            _d = dates_subset[_di]
                            _scores_wf = {}
                            for _t, _df in _wf_etf_data.items():
                                _sub = _df[_df.index <= _d]
                                if len(_sub) < mom_m + 1:
                                    continue
                                _mom_ret = float(_sub['Close'].iloc[-1] / _sub['Close'].iloc[-mom_m] - 1)
                                _adx_v   = float(_sub['adx_proxy'].iloc[-1]) if not _np_wf.isnan(_sub['adx_proxy'].iloc[-1]) else 0
                                if _adx_v < adx_th / 100:
                                    continue
                                _scores_wf[_t] = _mom_ret
                            if not _scores_wf:
                                _port.append(_port[-1])
                                continue
                            _best_t = max(_scores_wf, key=_scores_wf.get)
                            if _di < len(dates_subset) - 1:
                                _nd = dates_subset[_di + 1] if _di + 1 < len(dates_subset) else None
                                if _nd is not None and _best_t in _wf_etf_data:
                                    _ndf = _wf_etf_data[_best_t]
                                    _nret_ser = _ndf[_ndf.index == _nd]['ret']
                                    _nret = float(_nret_ser.iloc[0]) if len(_nret_ser) else 0.0
                                    if _np_wf.isnan(_nret):
                                        _nret = 0.0
                                    # RSI 과열 시 50% 익절
                                    _rsi_val = float(_ndf[_ndf.index == _nd]['rsi14'].iloc[0]) * 100 if len(_ndf[_ndf.index == _nd]) else 0
                                    _scale = 0.5 if _rsi_val >= rsi_ob else 1.0
                                    _port.append(_port[-1] * (1 + _nret * _scale))
                        if len(_port) < 2:
                            return 0.0
                        _total_ret = _port[-1] / _port[0] - 1
                        _max_dd = min((_port[i] / max(_port[:i+1]) - 1) for i in range(1, len(_port)))
                        if _max_dd < -0.15:
                            return -999.0  # MDD 15% 초과 패널티
                        return _total_ret

                    # Grid Search on in-sample
                    _best_params_wf = None
                    _best_score_wf  = -9999.0
                    _combo_done     = 0

                    for _mp, _ap, _rp in _itertools_wf.product(_mom_grid, _adx_grid, _rsi_grid):
                        _sc = _wf_etf_score(_mp, _ap, _rp, _in_dates)
                        if _sc > _best_score_wf:
                            _best_score_wf  = _sc
                            _best_params_wf = (_mp, _ap, _rp)
                        _combo_done += 1
                        _wf_etf_gs_prog.progress(_combo_done / _total_combos)
                        _wf_etf_gs_status.caption(f"그리드 탐색: {_combo_done}/{_total_combos} 조합")

                    _wf_etf_gs_prog.progress(1.0)
                    _wf_etf_gs_status.caption("✅ 그리드 탐색 완료")

                    # Out-of-sample 검증
                    _best_mp, _best_ap, _best_rp = _best_params_wf
                    _oos_score = _wf_etf_score(_best_mp, _best_ap, _best_rp, _out_dates)

                    # 세션 저장 (랭킹판에 반영)
                    st.session_state["wf_etf_best_momentum_months"] = _best_mp
                    st.session_state["wf_etf_best_adx"]             = _best_ap
                    st.session_state["wf_etf_best_rsi_ob"]          = _best_rp

                    _oos_label = f"{_oos_score*100:+.1f}%" if _oos_score != -999.0 else "MDD 초과 (불합격)"
                    st.success(
                        f"🎯 최적 파라미터 도출 완료!\n\n"
                        f"**모멘텀 룩백: {_best_mp}개월** | **ADX 임계값: {_best_ap}** | **RSI 과열: {_best_rp}**\n\n"
                        f"In-sample 수익률: {_best_score_wf*100:+.1f}% | "
                        f"Out-of-sample 검증: {_oos_label}\n\n"
                        f"랭킹판에 즉시 반영됩니다!"
                    )

                    # 결과 테이블
                    st.markdown("##### 📊 상위 5개 조합 (In-sample 기준)")
                    _all_combos_results = []
                    for _mp2, _ap2, _rp2 in _itertools_wf.product(_mom_grid, _adx_grid, _rsi_grid):
                        _sc2 = _wf_etf_score(_mp2, _ap2, _rp2, _in_dates)
                        if _sc2 > -999.0:
                            _all_combos_results.append({
                                "모멘텀(개월)": _mp2, "ADX 임계": _ap2,
                                "RSI 과열": _rp2, "In-sample 수익(%)": round(_sc2*100, 2)
                            })
                    if _all_combos_results:
                        import pandas as _pd_wf_res
                        _res_df = _pd_wf_res.DataFrame(_all_combos_results).sort_values(
                            "In-sample 수익(%)", ascending=False).head(5).reset_index(drop=True)
                        st.dataframe(_res_df, use_container_width=True, hide_index=True)

# ══════════════════════════════════════════
# 탭 7: 페이퍼 트레이딩
# ══════════════════════════════════════════

with tab_e:
    _sub_e1, _sub_e2, _sub_e3, _sub_e4, _sub_e5 = st.tabs(["⭐ 관심종목", "📝 페이퍼", "🌏 시장지수", "📊 현황판", "💰 하이브리드"])

    with _sub_e1:
        st.markdown("### ⚙️ 상태 제어 센터")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 1. 연동 상태 대형 카드 3개
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        _conn_c1, _conn_c2, _conn_c3 = st.columns(3)

        # Sheets 상태
        _sh_ok = False; _sh_msg = ""
        try:
            _ws = get_gsheet(); _sh_ok = True; _sh_msg = st.secrets.get("SHEET_ID","")[:16] + "…"
        except Exception as _e: _sh_msg = str(_e)[:40]
        _conn_c1.markdown(
            f"<div style='background:#0d1117;border:2px solid {'#39ff14' if _sh_ok else '#ff003c'};"
            f"border-radius:14px;padding:16px 18px;position:relative'>"
            f"<div style='position:absolute;top:12px;right:14px;width:14px;height:14px;border-radius:50%;"
            f"background:{'#39ff14' if _sh_ok else '#ff003c'};box-shadow:0 0 8px {'#39ff14' if _sh_ok else '#ff003c'}'></div>"
            f"<div style='font-size:22px;margin-bottom:6px'>📊</div>"
            f"<div style='font-size:13px;font-weight:700;color:#f0f4ff;margin-bottom:4px'>Google Sheets</div>"
            f"<div style='font-size:10px;color:{'#39ff14' if _sh_ok else '#ff003c'};margin-bottom:4px'>{'● 연결됨' if _sh_ok else '● 연결 실패'}</div>"
            f"<div style='font-size:10px;color:#64748b;word-break:break-all'>{_sh_msg}</div>"
            f"</div>", unsafe_allow_html=True
        )

        # App 상태 (yfinance / 데이터 가용)
        _app_ok = len(all_data) > 0
        _app_cnt = len(all_data)
        _conn_c2.markdown(
            f"<div style='background:#0d1117;border:2px solid {'#39ff14' if _app_ok else '#ff003c'};"
            f"border-radius:14px;padding:16px 18px;position:relative'>"
            f"<div style='position:absolute;top:12px;right:14px;width:14px;height:14px;border-radius:50%;"
            f"background:{'#39ff14' if _app_ok else '#ff003c'};box-shadow:0 0 8px {'#39ff14' if _app_ok else '#ff003c'}'></div>"
            f"<div style='font-size:22px;margin-bottom:6px'>📡</div>"
            f"<div style='font-size:13px;font-weight:700;color:#f0f4ff;margin-bottom:4px'>앱 데이터 (yfinance)</div>"
            f"<div style='font-size:10px;color:{'#39ff14' if _app_ok else '#ff003c'};margin-bottom:4px'>{'● 정상' if _app_ok else '● 데이터 없음'}</div>"
            f"<div style='font-size:10px;color:#64748b'>{_app_cnt}개 종목 캐시됨</div>"
            f"</div>", unsafe_allow_html=True
        )

        # Firebase DB 상태
        _fb_ok = False; _fb_msg = ""
        try:
            _get_firebase_app()
            _td = _fb_ref("/quant_watchlist").get()
            _fb_ok = True; _fb_msg = f"관심종목 {len(_td) if _td else 0}개"
        except Exception as _e: _fb_msg = str(_e)[:40]
        _conn_c3.markdown(
            f"<div style='background:#0d1117;border:2px solid {'#39ff14' if _fb_ok else '#ff003c'};"
            f"border-radius:14px;padding:16px 18px;position:relative'>"
            f"<div style='position:absolute;top:12px;right:14px;width:14px;height:14px;border-radius:50%;"
            f"background:{'#39ff14' if _fb_ok else '#ff003c'};box-shadow:0 0 8px {'#39ff14' if _fb_ok else '#ff003c'}'></div>"
            f"<div style='font-size:22px;margin-bottom:6px'>🔥</div>"
            f"<div style='font-size:13px;font-weight:700;color:#f0f4ff;margin-bottom:4px'>Firebase DB</div>"
            f"<div style='font-size:10px;color:{'#39ff14' if _fb_ok else '#ff003c'};margin-bottom:4px'>{'● 연결됨' if _fb_ok else '● 연결 실패'}</div>"
            f"<div style='font-size:10px;color:#64748b;word-break:break-all'>{_fb_msg}</div>"
            f"</div>", unsafe_allow_html=True
        )
        st.markdown("<div style='margin-top:8px'></div>", unsafe_allow_html=True)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 2 & 3. 중단 2열: 좌=스마트 입력, 우=섹터/시장 현황
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        _wl    = get_watchlist()
        _lines = [l.strip() for l in _wl.split("\n") if "," in l.strip()]
        _pairs = []
        for _l in _lines:
            _p = _l.split(",", 1)
            if len(_p) == 2:
                _pairs.append((_p[0].strip(), _p[1].strip()))
        _tids = [t for t, n in _pairs]

        def _do_delete(tk): remove_ticker(tk)

        _mid_l, _mid_r = st.columns([1, 1])

        with _mid_l:
            st.markdown("<div style='font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:10px'>➕ 종목 추가</div>", unsafe_allow_html=True)
            with st.form("add_ticker_form", clear_on_submit=True):
                _fc2, _fn2 = st.columns(2)
                _f_code = _fc2.text_input("종목코드", placeholder="005930")
                _f_name = _fn2.text_input("종목명",   placeholder="삼성전자")
                st.form_submit_button("✅ 추가", use_container_width=True)
                if _f_code and _f_name:
                    _code = _f_code.strip(); _name = _f_name.strip()
                    if _code not in _tids:
                        if add_ticker(_code, _name):
                            st.rerun()
                    else:
                        st.warning("이미 등록됨")

            # 태그형 목록 + 인라인 X 버튼
            st.markdown(f"<div style='font-size:11px;color:#64748b;margin:10px 0 6px'>📋 관심종목 {len(_pairs)}개 — X 클릭 시 즉시 삭제</div>", unsafe_allow_html=True)
            for _idx, (_tk, _nm) in enumerate(_pairs):
                _is_kr = _tk.isdigit()
                _flag = "🇰🇷" if _is_kr else "🇺🇸"
                try:
                    _tag_col, _del_col = st.columns([5, 1], vertical_alignment="center")
                except TypeError:
                    _tag_col, _del_col = st.columns([5, 1])
                _tag_col.markdown(
                    f"<div style='background:#1e293b;border:1px solid #334155;border-radius:20px;"
                    f"padding:5px 14px;font-size:12px;display:inline-flex;align-items:center;gap:6px'>"
                    f"{_flag} <span style='color:#f0f4ff;font-weight:700'>{_nm[:10]}</span>"
                    f"<span style='color:#64748b;font-size:10px'>{_tk}</span></div>",
                    unsafe_allow_html=True
                )
                _del_col.button("✕", key=f"tag_del_{_idx}_{_tk}", on_click=_do_delete, args=(_tk,))

        with _mid_r:
            st.markdown("<div style='font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:10px'>📈 시장별 종목 현황</div>", unsafe_allow_html=True)
            # 국장 / 미장 분류
            _kr_pairs = [(t, n) for t, n in _pairs if t.isdigit()]
            _us_pairs = [(t, n) for t, n in _pairs if not t.isdigit()]
            _sectors  = [("🇰🇷 국장 ETF/주식", _kr_pairs), ("🇺🇸 미장 ETF", _us_pairs)]
            _tbl_html = (
                "<div style='background:#0d1117;border:1px solid #1e293b;border-radius:12px;overflow:hidden'>"
                "<div style='display:grid;grid-template-columns:2fr 1fr 1fr 1fr;"
                "padding:8px 12px;background:#1e293b;font-size:10px;font-weight:700;color:#64748b;gap:4px'>"
                "<div>테마/시장</div><div style='text-align:center'>종목수</div>"
                "<div style='text-align:center'>평균등락</div><div style='text-align:center'>상태</div></div>"
            )
            for _sec_name, _sec_pairs in _sectors:
                if not _sec_pairs:
                    continue
                # 평균 등락률 계산
                _chgs = []
                for _st2, _sn2 in _sec_pairs:
                    if _st2 in all_data:
                        try:
                            _sdf = all_data[_st2]['df']
                            _sc  = _sdf['Close'].iloc[-1]; _sp = _sdf['Close'].iloc[-2]
                            _chgs.append((_sc / _sp - 1) * 100 if _sp and _sp > 0 else 0)
                        except Exception: pass
                _avg_chg = sum(_chgs)/len(_chgs) if _chgs else 0
                _chg_c = "#39ff14" if _avg_chg > 0 else "#ff003c"
                _status = "▲ 상승" if _avg_chg > 0.3 else ("▼ 하락" if _avg_chg < -0.3 else "→ 중립")
                _st_c   = "#39ff14" if _avg_chg > 0.3 else ("#ff003c" if _avg_chg < -0.3 else "#94a3b8")
                _tbl_html += (
                    "<div style='display:grid;grid-template-columns:2fr 1fr 1fr 1fr;"
                    "padding:8px 12px;border-top:1px solid #1e293b30;font-size:11px;gap:4px;align-items:center'>"
                    f"<div style='color:#f0f4ff;font-weight:600'>{_sec_name}</div>"
                    f"<div style='text-align:center;color:#fbbf24;font-weight:700'>{len(_sec_pairs)}</div>"
                    f"<div style='text-align:center;color:{_chg_c};font-weight:700'>{_avg_chg:+.2f}%</div>"
                    f"<div style='text-align:center;color:{_st_c}'>{_status}</div>"
                    "</div>"
                )
                # 개별 종목 행 (최대 5개)
                for _st2, _sn2 in _sec_pairs[:5]:
                    _sc_chg = 0
                    if _st2 in all_data:
                        try:
                            _sdf2 = all_data[_st2]['df']
                            _sc2  = _sdf2['Close'].iloc[-1]; _sp2 = _sdf2['Close'].iloc[-2]
                            _sc_chg = (_sc2 / _sp2 - 1) * 100 if _sp2 and _sp2 > 0 else 0
                        except Exception: pass
                    _sc_c = "#39ff14" if _sc_chg > 0 else "#ff003c"
                    _tbl_html += (
                        "<div style='display:grid;grid-template-columns:2fr 1fr 1fr 1fr;"
                        "padding:5px 12px;font-size:10px;gap:4px;align-items:center;background:#0a0f1a'>"
                        f"<div style='color:#94a3b8;padding-left:8px'>{_sn2[:12]}</div>"
                        f"<div style='text-align:center;color:#64748b;font-size:9px'>{_st2}</div>"
                        f"<div style='text-align:center;color:{_sc_c};font-weight:600'>{_sc_chg:+.2f}%</div>"
                        f"<div></div>"
                        "</div>"
                    )
            _tbl_html += "</div>"
            st.markdown(_tbl_html, unsafe_allow_html=True)

        st.divider()

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # 4. 스캐너 종목 그리드 타일 (C1~C6 2×3)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        st.markdown("<div style='font-size:13px;font-weight:700;color:#94a3b8;margin-bottom:10px'>📊 스캐너 발굴 종목 — 점수 타일 (C1~C6)</div>", unsafe_allow_html=True)

        def _do_add(tk, nm): add_ticker(tk, nm)

        if st.session_state.passed:
            _tile_html = "<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px'>"
            for _item in st.session_state.passed:
                _tk2  = _item["ticker"]; _nm2 = _item["name"]
                _chg  = _item.get("等락(%)", _item.get("등락(%)", 0))
                _ssc2 = _item.get("score", 0)
                _sgrd2 = _item.get("등급","")
                _done = _tk2 in _tids
                _gc2  = "#ffd166" if '🏆' in _sgrd2 else "#3b82f6"
                _chg_c2 = "#39ff14" if _chg > 0 else "#ff003c"
                _gcond2 = _item.get("조건","")
                def _cx2(cs, n): return 1 if f"C{n}✅" in cs else 0
                _scores = [_cx2(_gcond2, i) for i in range(1, 7)]
                _score_html = "<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:2px;margin-top:6px'>"
                for _ci2, _cv2 in enumerate(_scores):
                    _sc_bg = "#0a2a0a" if _cv2 else "#2a0a0a"
                    _sc_c2 = "#39ff14" if _cv2 else "#ff003c"
                    _score_html += (
                        f"<div style='background:{_sc_bg};border-radius:3px;padding:2px;text-align:center;"
                        f"font-size:9px;color:{_sc_c2};font-weight:700'>C{_ci2+1}</div>"
                    )
                _score_html += "</div>"
                _tile_html += (
                    f"<div style='background:#0d1117;border:1px solid {_gc2}40;border-radius:10px;"
                    f"padding:10px 10px;{'opacity:0.6;' if _done else ''}'>"
                    f"<div style='font-size:11px;font-weight:700;color:#f0f4ff'>{_nm2[:9]}</div>"
                    f"<div style='font-size:9px;color:#64748b;margin-top:1px'>{_tk2}</div>"
                    f"<div style='display:flex;justify-content:space-between;margin-top:4px'>"
                    f"<span style='font-size:10px;color:{_chg_c2}'>{'▲' if _chg>0 else '▼'}{abs(_chg):.1f}%</span>"
                    f"<span style='font-size:10px;color:#fbbf24;font-weight:700'>{_ssc2}점</span>"
                    f"</div>"
                    + _score_html +
                    ("<div style='font-size:9px;color:#39ff14;margin-top:4px'>✅ 관심등록됨</div>" if _done else "") +
                    f"</div>"
                )
            _tile_html += "</div>"
            st.markdown(_tile_html, unsafe_allow_html=True)
            st.markdown("<div style='margin-top:10px'></div>", unsafe_allow_html=True)
            # 일괄 추가 버튼
            _new_items2 = [i for i in st.session_state.passed if i['ticker'] not in _tids]
            if _new_items2:
                if st.button(f"⭐ 미등록 {len(_new_items2)}개 전체 추가", key="bulk_add_e1", use_container_width=True, type="primary"):
                    _added = sum(1 for _it in _new_items2 if add_ticker(_it['ticker'], _it['name']))
                    if _added: st.success(f"✅ {_added}개 추가!"); st.rerun()
        else:
            st.info("💡 스캐너 탭에서 먼저 스캔을 실행하면 발굴 종목이 여기에 표시됩니다.")

    # ══════════════════════════════════════════
    # 탭 6: ETF 로테이션 랭킹판
    # ══════════════════════════════════════════

    with _sub_e2:
        st.markdown("### 📝 페이퍼 트레이딩 (모의투자)")
        st.caption("실제 자금 없이 V9.1 전략을 검증합니다. 슬리피지·수수료·세금 자동 반영.")

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
            _rst_col1, _rst_col2 = st.columns(2)
            if _rst_col1.button("🔄 계좌 초기화 (전체 리셋)", key="reset_account"):
                _new_acc = {'initial':_new_cap,'cash':_new_cap,'positions':[],'peak':_new_cap,'trough':_new_cap}
                save_account(_new_acc)
                st.success(f"✅ {_new_cap:,.0f}원으로 초기화!")
                st.rerun()
            if _rst_col2.button("🔁 거래일지로 포지션 복구", key="restore_positions",
                                help="거래 일지(BUY/SELL 기록)를 분석해 현재 보유 포지션을 재구성합니다"):
                _trades = _load_trade_log_firebase()
                if not _trades:
                    st.warning("⚠️ 거래 일지가 비어있거나 불러올 수 없습니다.")
                else:
                    # BUY/SELL 기록으로 포지션 재구성
                    # 실제 저장 필드: 종목코드, 종목명, 매매(BUY/SELL), 수량, 순체결가, 잔고
                    _rebuilt = {}  # ticker → {name, qty, avg_price, entry_date}
                    _cash = float(_acc['initial'])
                    for _t in _trades:
                        _tk  = _t.get('종목코드', _t.get('ticker', ''))
                        _act = _t.get('매매', _t.get('액션', ''))
                        _qty = int(_t.get('수량', 0))
                        _net = float(_t.get('순체결가', _t.get('순매수가', _t.get('체결단가', 0))))
                        _nm  = _t.get('종목명', _tk)
                        _dt  = _t.get('날짜', '')
                        # 잔고 직접 기록이 있으면 현금으로 활용 (마지막 값 사용)
                        if _t.get('잔고', 0):
                            _cash = float(_t['잔고'])
                        if not _tk or _qty <= 0 or _net <= 0:
                            continue
                        if _act in ('BUY', '매수'):
                            if _tk in _rebuilt:
                                _old = _rebuilt[_tk]
                                _tot_qty = _old['qty'] + _qty
                                _old['avg_price'] = round((_old['avg_price']*_old['qty'] + _net*_qty) / _tot_qty, 4)
                                _old['qty'] = _tot_qty
                            else:
                                _rebuilt[_tk] = {'ticker':_tk,'name':_nm,'qty':_qty,'avg_price':_net,'entry_date':_dt}
                        elif _act in ('SELL', '매도'):
                            if _tk in _rebuilt:
                                _rebuilt[_tk]['qty'] -= _qty
                                if _rebuilt[_tk]['qty'] <= 0:
                                    del _rebuilt[_tk]
                    _pos_list = list(_rebuilt.values())
                    _acc['positions'] = _pos_list
                    _acc['cash'] = max(_cash, 0)
                    save_account(_acc)
                    st.success(f"✅ {len(_pos_list)}개 포지션 복구 완료! (현금 {_acc['cash']:,.0f}원)")
                    st.rerun()

        st.divider()

        # ── 2. 보유 포지션 ──
        st.markdown("#### 📊 보유 포지션")
        st.caption("📡 현재가 기준: yfinance 캐시 5분 + 한국주식 15~20분 지연 = **최대 25분 전 가격** / 미국주식 실시간(장중) | 새로고침하면 캐시 초기화")

        # 환율 조회 (캐시 활용)
        _pos_usd_krw = get_usd_krw()

        if not _acc['positions']:
            st.info("💡 보유 포지션 없음. 아래 가상 매수를 실행해보세요.")
        else:
            # 가격 데이터 취득 시각 기록
            import time as _pos_time
            _price_fetched_at = _pos_time.time()
            for _pi, _pos in enumerate(_acc['positions']):
                _pos_is_kr = is_korean_ticker(_pos['ticker'])
                _price_is_stale = False
                try:
                    _cur_df = fetch_ohlcv(_pos['ticker'], 5)
                    if _cur_df is not None and not _cur_df.empty:
                        _cur_p = float(_cur_df['종가'].iloc[-1])
                        # 5분 캐시 기준: 취득 시각이 5분 초과면 stale 표시
                        _cache_age = _pos_time.time() - st.session_state.get('all_data_time', _pos_time.time())
                        _price_is_stale = _cache_age > 300
                    else:
                        _cur_p = float(_pos['avg_price'])
                        _price_is_stale = True
                except Exception:
                    _cur_p = float(_pos['avg_price'])
                    _price_is_stale = True

                # 원화 환산 (미국주식은 USD → KRW)
                _fx       = 1.0 if _pos_is_kr else _pos_usd_krw
                _cur_p_krw    = _cur_p * _fx
                _avg_p_krw    = float(_pos['avg_price']) * _fx
                _pos_val_krw  = _cur_p_krw * _pos['qty']
                _pos_pnl_krw  = (_cur_p_krw - _avg_p_krw) * _pos['qty']
                _pos_pct      = (_cur_p / _pos['avg_price'] - 1) * 100 if _pos['avg_price'] > 0 else 0
                _pc           = 'up' if _pos_pnl_krw >= 0 else 'down'
                _kill_krw     = _avg_p_krw * (1 - _STOP_LOSS_PCT)
                _kill_alert   = _cur_p_krw <= _kill_krw

                # V8.9.2 동적 손절가 (ATR 기반) + 하드 서킷 -10% 병행
                try:
                    from paper_trading import calc_dynamic_stoploss, check_killswitch, format_stoploss_label
                    _atr14_pos = float(all_data.get(_pos['ticker'], {}).get('df', pd.DataFrame()).get('ATR14', pd.Series([0])).iloc[-1]) if _pos['ticker'] in all_data else 0
                    _kill_action, _kill_msg = check_killswitch(float(_avg_p_krw), float(_cur_p_krw), _atr14_pos if _atr14_pos > 0 else None)
                    _kill_alert = _kill_action != "HOLD"
                    _stop_label = format_stoploss_label(float(_avg_p_krw), _atr14_pos if _atr14_pos > 0 else None, _pos_is_kr)
                    _dynamic_stop, _hard_circuit = calc_dynamic_stoploss(float(_avg_p_krw), _atr14_pos) if _atr14_pos > 0 else (float(_avg_p_krw) * (1 - _STOP_LOSS_PCT), float(_avg_p_krw) * (1 - _STOP_LOSS_HARD))
                    _kill_krw = max(_dynamic_stop, _hard_circuit)
                except Exception:
                    _kill_krw   = _avg_p_krw * 0.93
                    _kill_alert = _cur_p_krw <= _kill_krw
                    _kill_msg   = f"🚨 킬스위치 발동! 즉각 매도 검토" if _kill_alert else ""
                    _stop_label = f"손절가: {_kill_krw:,.0f}원 (-7%)"
                _avg_disp = f"{_pos['avg_price']:,.0f}원" if _pos_is_kr else f"${_pos['avg_price']:,.2f}\n(≈{_avg_p_krw:,.0f}원)"
                _cur_disp = f"{_cur_p:,.0f}원" if _pos_is_kr else f"${_cur_p:,.2f}\n(≈{_cur_p_krw:,.0f}원)"
                _val_disp = f"{_pos_val_krw:,.0f}원"
                _pnl_disp = f"{_pos_pnl_krw:+,.0f}원"

                # V8.9.1 스마트 킬스위치 체크
                _ks_result = run_v891_system_check(
                    ticker=_pos['ticker'],
                    entry_price=float(_avg_p_krw),
                    current_price=float(_cur_p_krw)
                )
                _ks_action = _ks_result['killswitch']

                st.markdown(
                    f"<div style='background:rgba(255,255,255,0.04);border:2px solid {'#ff4d6d' if _kill_alert else '#1e3a5f'};border-radius:10px;padding:14px;margin-bottom:8px'>"
                    f"<div style='display:flex;justify-content:space-between'>"
                    f"<b style='font-size:15px'>{_pos['name']} <span style='color:#64748b;font-size:12px'>({_pos['ticker']})</span></b>"
                    f"<span class='{_pc}' style='font-size:16px;font-weight:700'>{_pos_pct:+.2f}%</span></div>"
                    f"<div style='display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:10px'>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>수량</div><div style='font-weight:700'>{_pos['qty']:,}주</div></div>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>평단가</div><div style='font-weight:700;white-space:pre-line'>{_avg_disp}</div></div>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>현재가{'  ⏱지연' if _price_is_stale else ''}</div><div style='font-weight:700;white-space:pre-line'>{_cur_disp}</div></div>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>평가금액(원)</div><div style='font-weight:700'>{_val_disp}</div></div>"
                    f"<div style='text-align:center'><div style='font-size:10px;color:#64748b'>평가손익(원)</div><div class='{_pc}' style='font-weight:700'>{_pnl_disp}</div></div>"
                    f"</div>"
                    f"<div style='margin-top:8px;font-size:12px;color:#f43f5e'>{_stop_label}"
                    f"{'  ' + _kill_msg if _kill_alert and _kill_msg else ''}</div>"
                    f"</div>",
                    unsafe_allow_html=True
                )

                _sc1, _sc2, _sc3, _sc4 = st.columns([1, 1, 1, 2])
                _sell_qty = _sc1.number_input("매도수량", min_value=1, max_value=_pos['qty'],
                                               value=_pos['qty'], key=f"sq_{_pi}_{_pos['ticker']}")
                if _sc2.button("📤 가상 매도", key=f"sell_{_pi}_{_pos['ticker']}", use_container_width=True):
                    _net_p    = calc_slippage(_cur_p, False, is_korean_ticker(_pos['ticker']))
                    _proceeds = _net_p * _sell_qty
                    _sell_fx  = 1.0 if is_korean_ticker(_pos['ticker']) else _pos_usd_krw
                    _acc['cash'] += _proceeds * _sell_fx
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

                # ── 포지션 직접 편집 버튼 ──
                _edit_key = f"edit_pos_{_pi}_{_pos['ticker']}"
                if _sc3.button("✏️ 편집", key=f"btn_{_edit_key}", use_container_width=True):
                    st.session_state[_edit_key] = not st.session_state.get(_edit_key, False)

                if st.session_state.get(_edit_key, False):
                    with st.container():
                        _e1, _e2, _e3, _e4 = st.columns([2, 2, 2, 1])
                        _new_name = _e1.text_input("종목명", value=_pos['name'], key=f"en_{_edit_key}")
                        _new_qty  = _e2.number_input("수량 (주)", value=int(_pos['qty']), min_value=1, key=f"eq_{_edit_key}")
                        _new_avg  = _e3.number_input(
                            "평단가", value=float(_pos['avg_price']),
                            min_value=0.01, format="%.4f" if not is_korean_ticker(_pos['ticker']) else "%.0f",
                            key=f"ea_{_edit_key}"
                        )
                        if _e4.button("💾 저장", key=f"save_{_edit_key}", use_container_width=True):
                            if not _new_name.strip():
                                st.error("❌ 종목명을 입력하세요.")
                            elif float(_new_avg) <= 0:
                                st.error("❌ 평단가는 0보다 커야 합니다.")
                            else:
                                _acc['positions'][_pi]['name']      = _new_name.strip()
                                _acc['positions'][_pi]['qty']       = int(_new_qty)
                                _acc['positions'][_pi]['avg_price'] = float(_new_avg)
                                save_account(_acc)
                                st.session_state[_edit_key] = False
                                st.success(f"✅ {_new_name} 포지션 업데이트 완료")
                                st.rerun()

        st.divider()

        # ── 3. 포지션 직접 추가 (실제 보유 종목 수기 등록) ──
        with st.expander("➕ 포지션 직접 추가 (수기 등록)"):
            st.caption("실제 증권사에서 보유 중인 종목을 수동으로 등록합니다. 현금 차감 없이 포지션만 추가됩니다.")
            _m1, _m2, _m3, _m4 = st.columns([2, 2, 2, 2])
            _man_ticker = _m1.text_input("티커", placeholder="예: 005930, AAPL", key="man_ticker").strip().upper()
            _man_name   = _m2.text_input("종목명", placeholder="예: 삼성전자", key="man_name").strip()
            _man_qty    = _m3.number_input("수량 (주)", min_value=1, value=1, key="man_qty")
            _man_avg    = _m4.number_input("평단가", min_value=0.01, value=0.01, format="%.2f", key="man_avg",
                                           help="한국주식: 원화 / 미국주식: 달러")
            if st.button("📌 포지션 등록", key="man_add_pos", use_container_width=True):
                if not _man_ticker:
                    st.error("❌ 티커를 입력하세요.")
                elif not _man_name:
                    st.error("❌ 종목명을 입력하세요.")
                elif _man_avg <= 0:
                    st.error("❌ 평단가는 0보다 커야 합니다.")
                elif True:
                    _dup_tickers = [p['ticker'] for p in _acc['positions']]
                    if _man_ticker in _dup_tickers:
                        st.warning(f"⚠️ {_man_ticker} 이미 보유 중 — 편집 버튼으로 수정해주세요.")
                    else:
                        _acc['positions'].append({
                            'ticker':    _man_ticker,
                            'name':      _man_name,
                            'qty':       int(_man_qty),
                            'avg_price': float(_man_avg),
                        })
                        save_account(_acc)
                        st.success(f"✅ {_man_name} ({_man_ticker}) {_man_qty}주 @ {_man_avg:,.2f} 등록 완료!")
                        st.rerun()
                else:
                    st.error("티커·종목명·평단가를 모두 입력해주세요.")

        st.divider()

        # ── 4. 가상 매수 ──
        st.markdown("#### 📥 가상 매수 실행")

        # 매수 가능 종목 = 관심종목 + 현재 보유 + 기본목록(중복 제거) — 관심종목도 매수 가능
        _buy_universe = {}
        for _t, _n in (get_watchlist_tickers() + [(p['ticker'], p.get('name', p['ticker']))
                        for p in _acc.get('positions', [])] + list(TICKERS)):
            if _t not in _buy_universe:
                _buy_universe[_t] = _n
        _buy_opts = [f"{_n} ({_t})" for _t, _n in _buy_universe.items()]
        _bc1, _bc2 = st.columns([2, 3])
        _buy_ticker_sel = _bc1.selectbox("종목 선택", _buy_opts or ["(관심종목 없음)"],
                                         key="buy_ticker_sel")
        # 형식: "종목명 (티커)" → 괄호 안 티커 추출
        _bt = _buy_ticker_sel.split('(')[-1].replace(')','').strip()
        _bn = _buy_universe.get(_bt, _bt)

        # 현재가 자동 로드
        _is_kr = is_korean_ticker(_bt)

        # USD/KRW 환율 — 포지션 카드에서 조회한 값 재사용
        _usd_krw = _pos_usd_krw

        # 종목 변경 시 매수가 session_state 초기화
        if st.session_state.get('_last_buy_ticker') != _bt:
            st.session_state['_last_buy_ticker'] = _bt
            st.session_state.pop('buy_price_inp', None)

        try:
            _buy_df  = fetch_ohlcv(_bt, 5)
            _buy_cur = float(_buy_df['종가'].iloc[-1]) if _buy_df is not None and not _buy_df.empty else 0
        except:
            _buy_cur = 0

        _buy_cur_krw = _buy_cur * (_usd_krw if not _is_kr else 1.0)
        _cur_disp = f"{_buy_cur:,.0f}원" if _is_kr else (
            f"${_buy_cur:,.2f} (≈{_buy_cur_krw:,.0f}원)" if _buy_cur > 0 else "가격 로드 실패 — 수동 입력 필요"
        )

        _fx_disp = f" | 환율: <b style='color:#94a3b8'>{_usd_krw:,.0f}원/$</b>" if not _is_kr else ""
        _bc2.markdown(
            f"<div style='background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:12px;margin-top:28px'>"
            f"현재가: <b style='font-size:18px;color:#fbbf24'>{_cur_disp}</b>"
            f"{_fx_disp} | "
            f"현금잔고: <b style='color:#34d399'>{_acc['cash']:,.0f}원</b></div>",
            unsafe_allow_html=True
        )
        if not _is_kr and _buy_cur == 0:
            st.warning("⚠️ 현재가를 가져오지 못했습니다. 매수가를 직접 입력해주세요.")

        # ── 5AI 점수 자동계산 (RSI·MACD·MA·모멘텀·거래량) ──
        def _calc_5ai(_df):
            if _df is None or len(_df) < 20:
                return 0
            try:
                _cl = _df['종가']; _vol = _df['거래량']
                _score = 0
                # RSI
                _d = _cl.diff(); _g = _d.clip(lower=0).rolling(14).mean(); _l = (-_d.clip(upper=0)).rolling(14).mean()
                _rsi = (100 - 100/(1+_g/_l.replace(0,np.nan))).iloc[-1]
                if _rsi >= 60: _score += 1
                elif _rsi <= 40: _score -= 1
                # MACD
                _m = _cl.ewm(span=12).mean() - _cl.ewm(span=26).mean()
                _s = _m.ewm(span=9).mean()
                if _m.iloc[-1] > _s.iloc[-1] and _m.iloc[-2] <= _s.iloc[-2]: _score += 2
                elif _m.iloc[-1] > _s.iloc[-1]: _score += 1
                elif _m.iloc[-1] < _s.iloc[-1]: _score -= 1
                # MA 정배열
                if _cl.iloc[-1] > _cl.rolling(20).mean().iloc[-1] > _cl.rolling(60).mean().iloc[-1]: _score += 1
                elif _cl.iloc[-1] < _cl.rolling(20).mean().iloc[-1]: _score -= 1
                # 모멘텀
                _mom = (_cl.iloc[-1]/_cl.iloc[-20]-1)*100
                if _mom >= 5: _score += 1
                elif _mom <= -5: _score -= 1
                # 거래량
                if _vol.iloc[-1] > _vol.tail(20).mean()*1.5: _score += 1
                return max(-5, min(5, _score))
            except:
                return 0
        _auto_5ai = _calc_5ai(_buy_df)

        # ── 빠른 투자금액 버튼 ──
        st.markdown("**💰 투자금액 선택**")
        _qb1, _qb2, _qb3, _qb4 = st.columns(4)
        if _qb1.button("10만원",    key="inv_10w",   use_container_width=True): st.session_state['invest_amt_inp'] = 100000
        if _qb2.button("100만원",   key="inv_100w",  use_container_width=True): st.session_state['invest_amt_inp'] = 1000000
        if _qb3.button("1,000만원", key="inv_1000w", use_container_width=True): st.session_state['invest_amt_inp'] = 10000000
        if _qb4.button("전액",      key="inv_all",   use_container_width=True): st.session_state['invest_amt_inp'] = int(_acc['cash'])

        # ── 투자금액(원) → 수량 자동계산 ──
        _inv_col1, _inv_col2 = st.columns([3, 2])
        _invest_amt = _inv_col1.number_input(
            "또는 직접 입력 (원)",
            value=st.session_state.get('invest_amt_inp', 10000000),
            step=100000, min_value=0, key="invest_amt_inp",
            help="원화 기준 투자금액 → 현재가(환율 반영) 기준 매수 가능 수량 자동 계산"
        )
        _auto_qty  = int(_invest_amt / _buy_cur_krw) if _buy_cur_krw > 0 and _invest_amt > 0 else 0
        _auto_cost_krw = _auto_qty * _buy_cur_krw
        _auto_cost_usd = _auto_qty * _buy_cur
        _cost_str  = f"{_auto_cost_krw:,.0f}원" if _is_kr else f"${_auto_cost_usd:,.2f} (≈{_auto_cost_krw:,.0f}원)"
        _inv_col2.markdown(
            f"<div style='background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:12px;margin-top:28px'>"
            f"<span style='font-size:12px;color:#166534'>매수 가능 수량</span><br>"
            f"<b style='font-size:22px;color:#15803d'>{_auto_qty:,}주</b>"
            f"<span style='font-size:12px;color:#166534'> (실투자: {_cost_str})</span></div>",
            unsafe_allow_html=True
        )

        # 매수가·수량·5AI 를 session_state에 항상 동기화 (종목/금액 변경 즉시 반영)
        try:
            _buy_cur_safe = float(_buy_cur) if _buy_cur and not (isinstance(_buy_cur, float) and np.isnan(_buy_cur)) else 0.0
            if _is_kr:
                _price_val = max(1, int(_buy_cur_safe)) if _buy_cur_safe > 0 else 1
            else:
                _price_val = round(_buy_cur_safe, 2) if _buy_cur_safe > 0 else 1.0
        except (TypeError, ValueError):
            _price_val = 1 if _is_kr else 1.0
        # 종목이 바뀔 때만 기본값 갱신 → 같은 종목 내 수동 입력이 snap-back 되지 않음
        if st.session_state.get('_buy_last_ticker') != _bt:
            st.session_state['_buy_last_ticker'] = _bt
            st.session_state['buy_price_inp'] = float(_price_val)
            st.session_state['buy_qty_inp']   = max(1, _auto_qty)
        st.session_state['buy_ai'] = _auto_5ai

        _brow1, _brow2, _brow3, _brow4 = st.columns(4)
        _price_label = "매수가 (원)" if _is_kr else "매수가 ($)"
        _price_step  = 100 if _is_kr else 1
        # value= 제거: key+session_state가 값 관리 (중복 경고/덮어쓰기 방지)
        _buy_price = _brow1.number_input(_price_label, step=float(_price_step),
                                          min_value=0.01, key="buy_price_inp")
        _buy_qty   = _brow2.number_input("수량 (주)", min_value=1, key="buy_qty_inp")
        _ai_color  = "#16a34a" if _auto_5ai > 0 else "#dc2626" if _auto_5ai < 0 else "#64748b"
        _brow3.markdown(f"<div style='font-size:11px;color:#64748b;margin-bottom:4px'>5AI 점수 (자동계산)</div>"
                        f"<div style='font-size:26px;font-weight:700;color:{_ai_color}'>{_auto_5ai:+d}점</div>",
                        unsafe_allow_html=True)
        _ai_score  = _auto_5ai
        _net_buy_preview = calc_slippage(_buy_price, True, _is_kr)
        # 현금 검증은 '실제 차감액(슬리피지 포함)' 기준 → 검증 통과 후 현금 음수 방지
        _buy_total = _net_buy_preview * _buy_qty
        _buy_total_krw = _buy_total if _is_kr else _buy_total * _usd_krw
        _total_str = f"{_buy_total:,.0f}원" if _is_kr else f"${_buy_total:,.2f} (≈{_buy_total_krw:,.0f}원)"
        _slip_str  = f"{_net_buy_preview:,.0f}원/주" if _is_kr else f"${_net_buy_preview:,.2f}/주"
        _brow4.markdown(
            f"<div style='padding-top:28px'>"
            f"필요금액: <b>{_total_str}</b><br>"
            f"<span style='font-size:11px;color:#64748b'>슬리피지 반영: {_slip_str}</span></div>",
            unsafe_allow_html=True
        )

        # 위젯 key(buy_memo)로 직접 관리 → 매수 후 초기화가 실제로 반영됨
        _buy_memo = st.text_input("매수 근거 (Why)",
                                   placeholder="예: BB하단 반등, 골든크로스 확인, 5AI +3점", key="buy_memo")

        _cash_ok = _acc['cash'] >= _buy_total_krw
        if not _cash_ok:
            st.warning(f"⚠️ 현금 부족 — 필요: {_buy_total_krw:,.0f}원 / 보유: {_acc['cash']:,.0f}원")

        # ETF 여부 판단 (종목명에 ETF 키워드 포함 또는 미국 ETF 티커)
        _is_etf = any(kw in _bn.upper() for kw in ["KODEX","TIGER","ACE","SOL","KBSTAR","HANARO","KOSEF","RISE","PLUS","ETF"]) \
                  or (not _bt.isdigit() and len(_bt) <= 5)

        # V8.9.1 진입 가능 여부 확인
        _v891_check = run_v891_system_check()
        _blocked = not _v891_check['can_enter']

        if _blocked:
            for _a in _v891_check['alerts']:
                if _is_etf:
                    st.warning(f"⚠️ 참고: {_a}")  # ETF는 경고만
                else:
                    st.error(_a)
            if _is_etf:
                st.info("ℹ️ ETF 로테이션은 매크로 이벤트 차단 제외 — 진입 가능합니다.")
            else:
                st.warning("⚠️ V9.1 방어 시스템 — 현재 신규 진입 차단 상태입니다.")

        # ETF는 블랙아웃 차단 무시, 개별주만 차단
        _entry_blocked = _blocked and not _is_etf

        if not _cash_ok:
            st.error(f"❌ 현금 부족 — 필요: {_buy_total_krw:,.0f}원 / 보유: {_acc['cash']:,.0f}원")
        if _entry_blocked:
            st.error("❌ V9.1 매크로 블랙아웃 — 개별주 신규 진입 차단 중")
        if st.button("📥 가상 매수 실행", key="exec_buy", use_container_width=True,
                     type="primary", disabled=(not _cash_ok or _entry_blocked)):
            _net_b = calc_slippage(_buy_price, True, is_korean_ticker(_bt))
            _buy_fx = 1.0 if is_korean_ticker(_bt) else _usd_krw   # 미국주식은 원화로 환산 차감
            _cost  = _net_b * _buy_qty          # native 통화(원 or $)
            _acc['cash'] -= _cost * _buy_fx     # 현금은 항상 KRW
            # 평단가는 native 통화로 저장 (US는 센트 보존 → 정수 round 금지)
            _avg_ndigits = 0 if is_korean_ticker(_bt) else 2
            _pos_exist = get_position(_acc, _bt)
            if _pos_exist:
                _old_v = _pos_exist['avg_price'] * _pos_exist['qty']
                _new_v = _net_b * _buy_qty
                _pos_exist['qty']      += _buy_qty
                _pos_exist['avg_price'] = round((_old_v + _new_v) / _pos_exist['qty'], _avg_ndigits)
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
                      _acc['cash'], _tv_now, ai_score=_ai_score, memo=st.session_state.get('buy_memo',''))
            st.session_state['buy_memo'] = ''   # 위젯 key 직접 초기화
            st.success(f"✅ {_bn} {_buy_qty}주 @ {_net_b:,.0f}원 체결! (슬리피지+수수료 반영)")
            st.rerun()

        st.divider()

        # ── 4. 성과 분석 ──
        st.markdown("#### 📈 성과 분석 (vs 벤치마크)")

        if st.session_state.get('_trade_log_err'):
            st.warning(f"⚠️ Firebase 거래일지 저장 오류: {st.session_state['_trade_log_err']}")

        try:
            # Firebase 우선, 실패 시에만 session_state 폴백 사용 (중복 방지)
            _fb_log   = _load_trade_log_firebase()
            if _fb_log:
                _all_rows = _fb_log
            else:
                _all_rows = st.session_state.get('local_trade_log', [])
            _log_df   = pd.DataFrame(_all_rows) if _all_rows else pd.DataFrame()

            _log_df = pd.concat([_log_df], ignore_index=True)
            if not _log_df.empty and {'날짜','시간','종목코드'}.issubset(_log_df.columns):
                _log_df = _log_df.drop_duplicates(subset=['날짜','시간','종목코드'], keep='last')
            if not _log_df.empty:
                # 날짜를 문자열로 정규화 (YYYY-MM-DD) — datetime 변환 전에 저장
                _log_df['날짜_str'] = _log_df['날짜'].astype(str).str[:10]
                _log_df['날짜']     = pd.to_datetime(_log_df['날짜'], errors='coerce')
                _log_df['평가금액'] = pd.to_numeric(_log_df['평가금액'], errors='coerce')
                _log_df = _log_df.dropna(subset=['날짜']).sort_values('날짜', ascending=True).reset_index(drop=True)

            if not _log_df.empty and '평가금액' in _log_df.columns and _log_df['평가금액'].notna().any():
                _log_df['수익률(%)'] = (_log_df['평가금액'] / _acc['initial'] - 1) * 100

                # 벤치마크 비교
                import yfinance as yf
                _start_bm = _log_df['날짜'].min()
                _is_dark_perf = st.session_state.get('ui_dark', True)
                _perf_bg = '#0b0e17' if _is_dark_perf else '#f8fafc'
                _perf_grid = 'rgba(255,255,255,0.05)' if _is_dark_perf else 'rgba(0,0,0,0.05)'
                _perf_txt = '#7a8ba8' if _is_dark_perf else '#64748b'
                _fig_perf = go.Figure()
                # 포트폴리오 수익률 선
                _port = _log_df.set_index('날짜')['수익률(%)']
                _port.index = pd.to_datetime(_port.index, errors='coerce').tz_localize(None)
                _port = _port[~_port.index.isna()].sort_index()
                _fig_perf.add_trace(go.Scatter(
                    x=_port.index, y=_port.values,
                    name='내 포트폴리오', line=dict(color='#f63d68', width=2),
                    fill='tozeroy', fillcolor='rgba(246,61,104,0.07)',
                    hovertemplate='%{x|%Y-%m-%d}<br>수익률: %{y:+.2f}%<extra>포트폴리오</extra>'
                ))
                # 코스피 벤치마크 (실패해도 포트폴리오 차트는 표시)
                try:
                    _bm = yf.Ticker("^KS11").history(start=_port.index.min(), interval="1d")
                    if not _bm.empty and len(_bm) > 0:
                        _bm_idx = pd.to_datetime(_bm.index).tz_localize(None) if _bm.index.tzinfo is not None else pd.to_datetime(_bm.index)
                        _bm_r = (_bm['Close'].values / _bm['Close'].values[0] - 1) * 100
                        _fig_perf.add_trace(go.Scatter(
                            x=_bm_idx, y=_bm_r,
                            name='코스피', line=dict(color='#3b82f6', width=1.5, dash='dot'),
                            hovertemplate='%{x|%Y-%m-%d}<br>수익률: %{y:+.2f}%<extra>코스피</extra>'
                        ))
                except Exception:
                    pass
                if len(_port) >= 2:
                    _fig_perf.add_shape(type='line', x0=_port.index[0], x1=_port.index[-1],
                        y0=0, y1=0, line=dict(color='rgba(255,255,255,0.2)', width=1, dash='dot'))
                _fig_perf.update_layout(
                    paper_bgcolor=_perf_bg, plot_bgcolor=_perf_bg,
                    height=280, margin=dict(l=10, r=10, t=10, b=10),
                    legend=dict(orientation='h', y=1.08, x=0, font=dict(size=11, color=_perf_txt)),
                    xaxis=dict(showgrid=True, gridcolor=_perf_grid, tickfont=dict(color=_perf_txt, size=10)),
                    yaxis=dict(showgrid=True, gridcolor=_perf_grid, tickfont=dict(color=_perf_txt, size=10),
                               ticksuffix='%', side='right'),
                    hovermode='x unified',
                )
                st.plotly_chart(_fig_perf, use_container_width=True)

                # MDD
                _cm    = _log_df['평가금액'].cummax()
                _dd    = (_log_df['평가금액'] - _cm) / _cm * 100
                _mdd_v = _dd.min()
                _mc1, _mc2, _mc3 = st.columns(3)
                _mc1.metric("최대낙폭(MDD)", f"{_mdd_v:.2f}%")
                _mc2.metric("총 거래 횟수", f"{len(_log_df)}회")
                _mc3.metric("최종 수익률", f"{_log_df['수익률(%)'].iloc[-1]:+.2f}%")

                # 거래 일지
                _jl1, _jl2 = st.columns([4, 1])
                _jl1.markdown("##### 📋 거래 일지")

                # 전체 삭제
                if _jl2.button("🗑️ 전체삭제", key="del_all_trades", use_container_width=True):
                    st.session_state['_confirm_del_all'] = True
                if st.session_state.get('_confirm_del_all'):
                    st.warning("⚠️ 모든 거래기록을 삭제합니다. 정말 삭제하시겠습니까?")
                    _dc1, _dc2 = st.columns(2)
                    if _dc1.button("✅ 확인 삭제", key="confirm_del_yes", use_container_width=True):
                        _del_ok = False
                        try:
                            _fb_ref("/quant_trades").delete()
                            st.session_state.pop('local_trade_log', None)
                            st.session_state.pop('_trade_log_err', None)
                            st.session_state['_confirm_del_all'] = False
                            _del_ok = True
                        except Exception as _de:
                            st.error(f"❌ Firebase 삭제 실패: {_de}\n로그인 상태 또는 Firebase 권한을 확인하세요.")
                        if _del_ok:   # st.rerun()은 try 밖에서 (예외로 삼켜지지 않도록)
                            st.success("✅ 전체 거래기록 삭제 완료")
                            st.rerun()
                    if _dc2.button("❌ 취소", key="confirm_del_no", use_container_width=True):
                        st.session_state['_confirm_del_all'] = False
                        st.rerun()

                # ── 필터 ──
                _jf1, _jf2, _jf3 = st.columns([2, 2, 2])
                _filter_ticker = _jf1.selectbox(
                    "종목 필터", ["전체"] + sorted(_log_df['종목명'].dropna().unique().tolist()),
                    key="jl_filter_ticker"
                )
                _filter_action = _jf2.selectbox("매매 유형", ["전체", "매수", "매도"], key="jl_filter_action")
                _filter_days   = _jf3.selectbox("기간", ["전체", "최근 7일", "최근 30일", "최근 90일"], key="jl_filter_days")

                _log_view = _log_df.copy()
                if _filter_ticker != "전체":
                    _log_view = _log_view[_log_view['종목명'] == _filter_ticker]
                if _filter_action != "전체":
                    _log_view = _log_view[_log_view['매매'] == _filter_action]
                if _filter_days != "전체":
                    _days_map = {"최근 7일": 7, "최근 30일": 30, "최근 90일": 90}
                    _cutoff = pd.Timestamp.now() - pd.Timedelta(days=_days_map[_filter_days])
                    _log_view = _log_view[_log_view['날짜'] >= _cutoff]
                _log_view = _log_view.reset_index(drop=True)

                _show_cols = [c for c in ['날짜','시간','종목명','매매','수량','순체결가','평가금액','메모'] if c in _log_df.columns]

                # 개별 삭제 — Firebase key 기반
                try:
                    _fb_raw = _fb_ref("/quant_trades").get() or {}
                except:
                    _fb_raw = {}

                _is_dark_jl = st.session_state.get('ui_dark', True)
                _jl_bg   = 'rgba(255,255,255,0.04)' if _is_dark_jl else 'rgba(0,0,0,0.025)'
                _jl_br   = 'rgba(255,255,255,0.09)' if _is_dark_jl else 'rgba(0,0,0,0.10)'
                _jl_sub  = '#64748b'

                if _log_view.empty:
                    st.info("필터 조건에 맞는 거래 기록이 없습니다.")
                for _ri, _row_r in _log_view.iloc[::-1].iterrows():
                    _is_buy   = _row_r.get('매매') == '매수'
                    _action_c = '#f63d68' if _is_buy else '#3b82f6'
                    _action_bg= 'rgba(246,61,104,0.12)' if _is_buy else 'rgba(59,130,246,0.12)'
                    _action_lbl = '매수' if _is_buy else '매도'
                    _is_kr_j  = str(_row_r.get('종목코드','')).isdigit()
                    _price_j  = float(_row_r.get('순체결가', 0))
                    _price_str= f"{_price_j:,.0f}원" if _is_kr_j else f"${_price_j:,.2f}"
                    _eval_j   = float(_row_r.get('평가금액', 0))
                    _memo_j   = str(_row_r.get('메모','')) if _row_r.get('메모') else ''
                    _date_j   = str(_row_r.get('날짜_str', _row_r.get('날짜','')))[:10]
                    _time_j   = str(_row_r.get('시간',''))[:5]
                    _qty_j    = int(_row_r.get('수량', 0))

                    _rc2, _rc3 = st.columns([11, 1])
                    _rc2.markdown(
                        f"<div style='background:{_jl_bg};border:1px solid {_jl_br};"
                        f"border-left:3px solid {_action_c};"
                        f"border-radius:8px;padding:10px 14px;margin-bottom:5px;"
                        f"display:flex;justify-content:space-between;align-items:center'>"
                        f"<div style='display:flex;align-items:center;gap:12px'>"
                        f"<span style='background:{_action_bg};color:{_action_c};font-weight:700;"
                        f"font-size:12px;padding:2px 8px;border-radius:4px'>{_action_lbl}</span>"
                        f"<div>"
                        f"<div style='font-weight:600;font-size:14px'>{_row_r.get('종목명','')}"
                        f"<span style='color:{_jl_sub};font-size:11px;margin-left:6px'>{_row_r.get('종목코드','')}</span></div>"
                        f"<div style='font-size:11px;color:{_jl_sub};margin-top:2px'>{_date_j} {_time_j}"
                        f"{'&nbsp;·&nbsp;📝 ' + _memo_j if _memo_j else ''}</div>"
                        f"</div></div>"
                        f"<div style='text-align:right'>"
                        f"<div style='font-family:IBM Plex Mono;font-size:14px;font-weight:600'>{_price_str} × {_qty_j:,}주</div>"
                        f"<div style='font-size:11px;color:{_jl_sub};margin-top:2px'>잔고 {_eval_j:,.0f}원</div>"
                        f"</div></div>",
                        unsafe_allow_html=True
                    )

                    # Firebase에서 해당 레코드 키 찾기
                    _match_key = None
                    _del_date = _date_j  # YYYY-MM-DD 문자열
                    _del_time = str(_row_r.get('시간',''))
                    _del_code = str(_row_r.get('종목코드',''))
                    for _fk, _fv in _fb_raw.items():
                        if (str(_fv.get('날짜',''))[:10] == _del_date and
                            str(_fv.get('시간','')) == _del_time and
                            str(_fv.get('종목코드','')) == _del_code):
                            _match_key = _fk
                            break
                    if _rc3.button("🗑️", key=f"del_trade_{_ri}", help="이 기록 삭제"):
                        if _match_key:
                            try:
                                _fb_ref(f"/quant_trades/{_match_key}").delete()
                            except Exception:
                                pass
                        _local = st.session_state.get('local_trade_log', [])
                        st.session_state['local_trade_log'] = [
                            r for r in _local
                            if not (str(r.get('날짜',''))[:10] == _del_date and
                                    str(r.get('시간','')) == _del_time and
                                    str(r.get('종목코드','')) == _del_code)
                        ]
                        st.success("✅ 삭제 완료")
                        st.rerun()

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
                _sample = pd.DataFrame({'내 포트폴리오(%)': [0,1.2,0.8,2.1,1.5,3.2,2.8],
                                        '코스피(%)':        [0,0.5,0.3,1.1,0.9,1.8,1.5]})
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
                "코스피200(KODEX)": "069500.KS",  # 만료된 KSF24 선물 대신 KODEX200 ETF 대용
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
            """외인/기관/개인 투자자 동향 — pykrx (방어적 컬럼 매핑 적용)"""
            try:
                from pykrx import stock
                today = datetime.today().strftime('%Y%m%d')
                start = (datetime.today() - timedelta(days=10)).strftime('%Y%m%d')
                df = stock.get_market_trading_value_by_date(start, today, "KOSPI")
                if df is None or df.empty or df.shape[1] == 0:
                    return None
                df.index = pd.to_datetime(df.index)
                # 컬럼명 방어적 정규화: 공백·띄어쓰기 무관하게 매핑
                _col_map = {}
                for _c in df.columns:
                    _cn = str(_c).replace(" ", "")
                    if "기관" in _cn and "합계" in _cn:  _col_map[_c] = "기관합계"
                    elif "외국인" in _cn or "외인" in _cn: _col_map[_c] = "외국인"
                    elif "개인" in _cn:                    _col_map[_c] = "개인"
                if _col_map:
                    df = df.rename(columns=_col_map)
                return df.tail(5)
            except Exception:
                return None

        with st.spinner("지수 데이터 로딩 중..."):
            idx_data    = fetch_index_data()
            inv_data    = fetch_investor_data()

        # ── 지수 카드 ──
        st.markdown("#### 📈 주요 지수")
        if idx_data:
            # 1행: 국내
            domestic = ["코스피","코스닥","코스피200(KODEX)"]
            # 라이트/다크 모드 색상 분기 헬퍼
            _lm = not st.session_state.get('ui_dark', True)
            _c_up   = "#991B1B" if _lm else "#ff4d6d"
            _c_down = "#1E40AF" if _lm else "#4da6ff"
            _c_vix_up = "#991B1B" if _lm else "#ff4d6d"
            _c_vix_dn = "#166534" if _lm else "#4dff91"

            cols_d = st.columns(3)
            for i, name in enumerate(domestic):
                if name in idx_data:
                    d = idx_data[name]
                    chg_c = _c_up if d['등락']>0 else _c_down
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
                    chg_c = _c_up if d['등락']>0 else _c_down
                    # VIX는 오를수록 위험 — 색상 반전
                    if name == "공포탐욕(VIX)":
                        chg_c = _c_vix_up if d['등락']>0 else _c_vix_dn
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
                    # pandas 2.x: applymap deprecated → map 사용
                    _style_fn = lambda v: 'color: #39ff14' if v > 0 else 'color: #ff003c'
                    try:
                        _styled = inv_disp.style.map(_style_fn)
                    except AttributeError:
                        _styled = inv_disp.style.applymap(_style_fn)
                    st.dataframe(_styled, use_container_width=True)
            except Exception as e:
                st.warning(f"투자자 데이터 표시 오류: {e}")
        else:
            st.info("💡 투자자 순매수 데이터는 장 마감 후 업데이트됩니다.")

        # ── 고도화 지수 차트 (Heikin-Ashi + 볼린저 + 이벤트 마커) ──
        st.markdown("---")
        st.markdown("#### 📊 변동성 관측 차트")

        _chart_syms = {
            "코스피 (^KS11)": "^KS11",
            "코스닥 (^KQ11)": "^KQ11",
            "S&P500 (^GSPC)": "^GSPC",
            "나스닥 (^IXIC)": "^IXIC",
            "VIX (^VIX)": "^VIX",
        }
        _sel_chart = st.selectbox("지수 선택", list(_chart_syms.keys()), key="idx_chart_sel")
        _sel_sym   = _chart_syms[_sel_chart]

        # 하이킨 아시 ON/OFF 토글
        _ha_on = st.toggle("🕯 Heikin-Ashi 평활 캔들", value=True, key="ha_toggle")

        @st.cache_data(ttl=1800, show_spinner=False)
        def _fetch_chart_df(symbol, period="6mo"):
            try:
                import yfinance as _yf_c
                _df = _yf_c.Ticker(symbol).history(period=period, interval="1d")
                if _df.empty:
                    return None
                _df = _df[['Open','High','Low','Close','Volume']].dropna()
                return _df
            except Exception:
                return None

        def _to_heikin_ashi(df):
            ha = df.copy()
            # HA Close: 4가 평균
            ha['Close'] = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4
            # HA Open: 전봉 (HA_Open + HA_Close) / 2 — shift()로 벡터화
            ha['Open'] = ((df['Open'].shift(1) + df['Close'].shift(1)) / 2)
            ha.iloc[0, ha.columns.get_loc('Open')] = (df['Open'].iloc[0] + df['Close'].iloc[0]) / 2
            # HA High/Low: 원본 고가/저가와 HA Open/Close 중 max/min (정확한 꼬리 계산)
            ha['High'] = ha[['Open', 'Close']].join(df['High']).max(axis=1)
            ha['Low']  = ha[['Open', 'Close']].join(df['Low']).min(axis=1)
            return ha

        def _calc_rsi(close, period=14):
            delta = close.diff()
            gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
            loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
            rs    = gain / loss.replace(0, 1e-9)
            return 100 - 100 / (1 + rs)

        # 하드코딩된 이벤트 마커 (FOMC·금리·매크로 이벤트)
        _EVENT_DATES = [
            ("2024-11-07", "FOMC"),
            ("2024-12-19", "FOMC"),
            ("2025-01-29", "FOMC"),
            ("2025-03-19", "FOMC"),
            ("2025-05-07", "FOMC"),
            ("2025-06-18", "FOMC"),
            ("2025-07-30", "FOMC"),
            ("2025-09-17", "FOMC"),
            ("2025-10-29", "FOMC"),
            ("2025-12-10", "FOMC"),
        ]

        with st.spinner("차트 데이터 로딩 중..."):
            _cdf = _fetch_chart_df(_sel_sym)

        # U1: 마지막 갱신 시각 표시 (캐시 TTL 1800초 기준)
        from datetime import datetime as _dt_chart
        _chart_fetched_at = _dt_chart.now().strftime('%H:%M:%S')
        st.caption(f"📡 데이터 기준: {_chart_fetched_at} (30분 캐시 — 최대 30분 지연 가능)")

        if _cdf is not None and len(_cdf) >= 20:
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots
            import numpy as np

            _plot_df = _to_heikin_ashi(_cdf) if _ha_on else _cdf

            # 지표 계산
            _cl = _cdf['Close']
            _ma20  = _cl.rolling(20).mean()
            _ma60  = _cl.rolling(60).mean()
            _bb_m  = _cl.rolling(20).mean()
            _bb_s  = _cl.rolling(20).std()
            _bb_up = _bb_m + 2 * _bb_s
            _bb_lo = _bb_m - 2 * _bb_s
            _rsi   = _calc_rsi(_cl)

            # MA20 색상 동적 결정 (현재가 대비 거리)
            _cur_price = float(_cl.iloc[-1])
            _ma20_last = float(_ma20.iloc[-1]) if not np.isnan(_ma20.iloc[-1]) else _cur_price
            _ma20_dist = (_cur_price / _ma20_last - 1) * 100 if _ma20_last > 0 else 0
            if _ma20_dist > 5:
                _ma20_color = "#fbbf24"   # 골드 — 과열
                _ma20_label = f"MA20 (과열 +{_ma20_dist:.1f}%)"
            elif _ma20_dist < -5:
                _ma20_color = "#38bdf8"   # 라이트 블루 — 침체
                _ma20_label = f"MA20 (침체 {_ma20_dist:.1f}%)"
            else:
                _ma20_color = "#06d6a0"
                _ma20_label = f"MA20 ({_ma20_dist:+.1f}%)"

            # 서브플롯: 메인차트 + RSI
            _fig = make_subplots(
                rows=2, cols=1,
                row_heights=[0.75, 0.25],
                shared_xaxes=True,
                vertical_spacing=0.03
            )

            # 캔들 (Heikin-Ashi or 일반)
            _up_c   = "#39ff14"   # 형광 그린
            _dn_c   = "#ff003c"   # 형광 레드
            _fig.add_trace(go.Candlestick(
                x=_plot_df.index,
                open=_plot_df['Open'], high=_plot_df['High'],
                low=_plot_df['Low'],   close=_plot_df['Close'],
                increasing_line_color=_up_c, decreasing_line_color=_dn_c,
                increasing_fillcolor=_up_c,  decreasing_fillcolor=_dn_c,
                name="HA캔들" if _ha_on else "캔들",
                showlegend=False,
                # 커스텀 툴팁
                customdata=list(zip(
                    ((_cl - _cl.shift(1)) / _cl.shift(1) * 100).round(2).fillna(0),
                    _rsi.round(1).fillna(50)
                )),
                hovertemplate=(
                    "<b>%{x|%Y-%m-%d}</b><br>"
                    "등락률: <b>%{customdata[0]:+.2f}%</b><br>"
                    "RSI: <b>%{customdata[1]:.1f}</b><br>"
                    "종가: %{close:,.2f}<br>"
                    "고가: %{high:,.2f} / 저가: %{low:,.2f}<extra></extra>"
                )
            ), row=1, col=1)

            # 볼린저 밴드 (반투명 배경)
            _fig.add_trace(go.Scatter(
                x=list(_bb_up.index) + list(_bb_lo.index[::-1]),
                y=list(_bb_up) + list(_bb_lo[::-1]),
                fill='toself', fillcolor='rgba(148,163,184,0.07)',
                line=dict(color='rgba(148,163,184,0)', width=0),
                name='볼린저밴드', showlegend=True, legendgroup='bb',
                hoverinfo='skip'
            ), row=1, col=1)
            _fig.add_trace(go.Scatter(
                x=_bb_up.index, y=_bb_up,
                line=dict(color='rgba(148,163,184,0.3)', width=0.8, dash='dot'),
                name='BB상단', showlegend=False, hoverinfo='skip'
            ), row=1, col=1)
            _fig.add_trace(go.Scatter(
                x=_bb_lo.index, y=_bb_lo,
                line=dict(color='rgba(148,163,184,0.3)', width=0.8, dash='dot'),
                name='BB하단', showlegend=False, hoverinfo='skip'
            ), row=1, col=1)

            # MA20 (동적 색상, 굵게)
            _fig.add_trace(go.Scatter(
                x=_ma20.index, y=_ma20,
                line=dict(color=_ma20_color, width=2.5),
                name=_ma20_label
            ), row=1, col=1)

            # MA60
            if len(_cdf) >= 60:
                _fig.add_trace(go.Scatter(
                    x=_ma60.index, y=_ma60,
                    line=dict(color='#a78bfa', width=1.2, dash='dot'),
                    name='MA60'
                ), row=1, col=1)

            # RSI 서브플롯
            _fig.add_trace(go.Scatter(
                x=_rsi.index, y=_rsi,
                line=dict(color='#fbbf24', width=1.5),
                name='RSI(14)',
                hovertemplate="RSI: <b>%{y:.1f}</b><extra></extra>"
            ), row=2, col=1)
            _fig.add_hline(y=70, line_color='#ff003c', line_width=0.8,
                           line_dash='dash', row=2, col=1)
            _fig.add_hline(y=30, line_color='#39ff14', line_width=0.8,
                           line_dash='dash', row=2, col=1)

            # FOMC 이벤트 수직선
            import pandas as pd
            _df_start = _cdf.index[0].to_pydatetime().replace(tzinfo=None)
            for _ev_dt_str, _ev_lbl in _EVENT_DATES:
                try:
                    _ev_dt = pd.Timestamp(_ev_dt_str)
                    if hasattr(_cdf.index[0], 'tzinfo') and _cdf.index[0].tzinfo:
                        import pytz
                        _ev_dt = _ev_dt.tz_localize('UTC')
                    if _cdf.index[0] <= _ev_dt <= _cdf.index[-1]:
                        _fig.add_vline(
                            x=_ev_dt.value / 1e6,
                            line_color='rgba(251,191,36,0.5)',
                            line_width=1.2,
                            line_dash='dot',
                            row='all', col=1,
                            annotation_text=_ev_lbl,
                            annotation_font_color='#fbbf24',
                            annotation_font_size=9,
                            annotation_position="top left"
                        )
                except Exception:
                    pass

            _fig.update_layout(
                paper_bgcolor='#0a0e1a',
                plot_bgcolor='#0f1726',
                font=dict(color='#8899bb', size=11),
                xaxis_rangeslider_visible=False,
                height=520,
                autosize=True,
                margin=dict(l=10, r=10, t=30, b=10),
                legend=dict(orientation='h', y=1.02, x=0,
                            font=dict(size=10), bgcolor='rgba(0,0,0,0)'),
                hovermode='x unified',
            )
            # U3: 모바일 반응형 — Plotly autosize + CSS로 높이 제한 해제
            st.markdown(
                "<style>.js-plotly-plot .plotly{width:100%!important;}"
                ".js-plotly-plot .plotly svg{max-height:none!important;}</style>",
                unsafe_allow_html=True
            )
            _fig.update_xaxes(gridcolor='#1a2535', showgrid=True)
            _fig.update_yaxes(gridcolor='#1a2535', showgrid=True)
            _fig.update_yaxes(title_text="RSI", row=2, col=1,
                              range=[0, 100], fixedrange=True)

            st.plotly_chart(_fig, use_container_width=True)

            # 현재 상태 요약 칩
            _rsi_now = float(_rsi.iloc[-1]) if not np.isnan(_rsi.iloc[-1]) else 50
            _bb_pos  = (_cur_price - float(_bb_lo.iloc[-1])) / max(float(_bb_up.iloc[-1]) - float(_bb_lo.iloc[-1]), 1) * 100
            _rsi_lbl = "과매수🔴" if _rsi_now >= 70 else ("과매도🟢" if _rsi_now <= 30 else "중립⚪")
            _bb_lbl  = "BB상단돌파🔴" if _bb_pos >= 95 else ("BB하단이탈🟢" if _bb_pos <= 5 else f"BB{_bb_pos:.0f}%")
            st.markdown(
                f"<div style='display:flex;gap:10px;flex-wrap:wrap;margin-top:4px'>"
                f"<span style='background:#1e293b;color:#fbbf24;font-size:12px;padding:4px 12px;border-radius:20px'>"
                f"RSI {_rsi_now:.1f} — {_rsi_lbl}</span>"
                f"<span style='background:#1e293b;color:{_ma20_color};font-size:12px;padding:4px 12px;border-radius:20px'>"
                f"{_ma20_label}</span>"
                f"<span style='background:#1e293b;color:#94a3b8;font-size:12px;padding:4px 12px;border-radius:20px'>"
                f"볼린저 {_bb_lbl}</span>"
                f"</div>",
                unsafe_allow_html=True
            )
        else:
            st.warning("차트 데이터를 불러올 수 없습니다. 잠시 후 다시 시도해주세요.")

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
                    st.session_state.all_data_cache[_mt] = {'name': _mn, 'df': calc_indicators(_mdf)}

        if not all_data:
            _lookback = 80
            st.session_state.all_data_cache.clear()
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
                st.session_state.all_data_cache[ticker] = {'name': name, 'df': df}
            prog_bar.empty()
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
                        _tok = st.session_state.get('_k_t')
                        _tok_age = _time_kis.time() - st.session_state.get('_k_ts', 0)
                        if _tok and _tok_age > 21600:
                            st.warning("⏰ KIS 토큰 만료 (6시간) — 페이지를 새로고침하면 자동 갱신됩니다.")
                        elif not _tok:
                            st.error("❌ KIS 토큰 없음 — API 키(KIS_APP_KEY / KIS_APP_SECRET)를 secrets에 등록해주세요.")
                        else:
                            st.warning("⚠️ 잔고 조회 실패 — KIS API 응답 오류. 잠시 후 새로고침해주세요.")

                with _kis_col2:
                    st.markdown("**📡 관심종목 실시간 현재가**")
                    for _t, _n in get_watchlist_tickers()[:5]:  # 실제 관심종목 상위 5개
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

        # 환율 조회 (캐시 활용)
        _dsh_usd_krw = get_usd_krw()

        # all_data = {} 제거 — 기존 캐시 유지 (다른 탭 데이터 소멸 방지)
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

        _cur_tickers_e4 = get_watchlist_tickers()
        _e4_missing = [(t, n) for t, n in _cur_tickers_e4 if t not in all_data]
        if _e4_missing:
            _e4_prog = st.progress(0, text="데이터 로딩 중...")
            for _ei, (_et, _en) in enumerate(_e4_missing):
                _e4_prog.progress((_ei+1)/max(len(_e4_missing),1), text=f"📡 {_en} 수집 중... ({_ei+1}/{len(_e4_missing)})")
                _edf = fetch_ohlcv(_et, lookback)
                if _edf is not None and len(_edf) >= 20:
                    st.session_state.all_data_cache[_et] = {'name': _en, 'df': calc_indicators(_edf)}
            _e4_prog.empty()

        for ticker, name in _cur_tickers_e4:
            if ticker not in all_data:
                continue
            df = all_data[ticker]['df']
            l = df.iloc[-1]; p = df.iloc[-2]
            chg  = (l['종가']/p['종가']-1)*100
            volr = l['거래량']/(df['거래량'].iloc[-21:-1].mean() if len(df)>=21 else df['거래량'].iloc[:-1].mean())*100
            sigs = get_signal(df)
            chg_color = 'up' if chg > 0 else 'down' if chg < 0 else 'flat'

            # 미국 주식은 달러 표시
            _is_kr_d = is_korean_ticker(ticker)
            _price_disp = f"{l['종가']:,.0f}원" if _is_kr_d else f"${l['종가']:,.2f}"
            _ma5_disp   = f"{l['MA5']:,.0f}" if _is_kr_d else f"${l['MA5']:,.2f}"
            _ma20_disp  = f"{l['MA20']:,.0f}" if _is_kr_d else f"${l['MA20']:,.2f}"

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
                    f"<span style='font-family:IBM Plex Mono; font-size:14px; font-weight:700'>{_price_disp}</span><br>"
                    f"<span class='{chg_color}' style='font-size:12px'>{chg:+.2f}%</span>",
                    unsafe_allow_html=True)
                cols[2].markdown(
                    f"<span style='color:{rsi_color}; font-family:IBM Plex Mono; font-size:15px; font-weight:700'>{l['RSI']:.1f}</span>",
                    unsafe_allow_html=True)
                cols[3].markdown(badge_html, unsafe_allow_html=True)
            else:
                cols = st.columns([2, 1.2, 1, 0.8, 1, 1, 1, 2.5])
                cols[0].markdown(f"<b style='font-size:13px'>{name}</b><br><span style='font-size:10px; color:#64748b; font-family:IBM Plex Mono'>{ticker}</span>", unsafe_allow_html=True)
                cols[1].markdown(f"<span style='font-family:IBM Plex Mono; font-size:13px; font-weight:600'>{_price_disp}</span>", unsafe_allow_html=True)
                cols[2].markdown(f"<span class='{chg_color}' style='font-family:IBM Plex Mono; font-size:13px'>{chg:+.2f}%</span>", unsafe_allow_html=True)
                cols[3].markdown(f"<span style='color:{rsi_color}; font-family:IBM Plex Mono; font-size:13px'>{l['RSI']:.1f}</span>", unsafe_allow_html=True)
                cols[4].markdown(f"<span style='font-family:IBM Plex Mono; font-size:12px; color:#94a3b8'>{_ma5_disp}</span>", unsafe_allow_html=True)
                cols[5].markdown(f"<span style='font-family:IBM Plex Mono; font-size:12px; color:#94a3b8'>{_ma20_disp}</span>", unsafe_allow_html=True)
                cols[6].markdown(f"<span style='color:{vol_color}; font-family:IBM Plex Mono; font-size:12px'>{volr:.0f}%</span>", unsafe_allow_html=True)
                cols[7].markdown(badge_html, unsafe_allow_html=True)

            # NXT 거래소 가용성 (코스피/코스닥 종목만)
            _is_kr = ticker.isdigit() and len(ticker) == 6
            st.markdown("<hr style='margin:4px 0; border-color:#0f1726'>", unsafe_allow_html=True)

        # 요약 통계 — 상단 표와 동일하게 실제 관심종목 기준으로 집계
        if all_data:
            st.markdown("### 📊 요약 통계")
            c1, c2, c3, c4 = st.columns(4)
            _wl_stat = get_watchlist_tickers()
            buy_cnt  = sum(1 for t,_ in _wl_stat if t in all_data and
                           any(s[1]=='buy' for s in get_signal(all_data[t]['df'])))
            sell_cnt = sum(1 for t,_ in _wl_stat if t in all_data and
                           any(s[1]=='sell' for s in get_signal(all_data[t]['df'])))
            oversold = sum(1 for t,_ in _wl_stat if t in all_data and
                           all_data[t]['df'].iloc[-1]['RSI'] <= 35)
            overbought = sum(1 for t,_ in _wl_stat if t in all_data and
                             all_data[t]['df'].iloc[-1]['RSI'] >= 65)

            c1.markdown(f"<div class='metric-card'><div class='label'>매수신호</div><div class='value up'>{buy_cnt}종목</div></div>", unsafe_allow_html=True)
            c2.markdown(f"<div class='metric-card'><div class='label'>매도신호</div><div class='value down'>{sell_cnt}종목</div></div>", unsafe_allow_html=True)
            c3.markdown(f"<div class='metric-card'><div class='label'>과매도(RSI≤35)</div><div class='value' style='color:#38bdf8'>{oversold}종목</div></div>", unsafe_allow_html=True)
            c4.markdown(f"<div class='metric-card'><div class='label'>과매수(RSI≥65)</div><div class='value' style='color:#f43f5e'>{overbought}종목</div></div>", unsafe_allow_html=True)


    # ══════════════════════════════════════════════════════════════
    # 탭 5: 💰 하이브리드 시스템 — 공격(국장) + 방어(미장 배당)
    # ══════════════════════════════════════════════════════════════
    with _sub_e5:
        st.markdown("### 💰 V9.7 퀀트-배당 하이브리드 시스템")
        st.caption("국장(삼성증권) 수익금 → 환율 필터 → 미장 배당 자산(토스) 자동 순환 전략")

        # ─── 환율 필터 헤더 카드 ───────────────────────────────
        _fx_now = get_usd_krw()
        _fx_result = check_profit_recycling(_fx_now)
        _fx_c = _fx_result['color']
        _fx_bg = (
            "linear-gradient(135deg,#0a2a0a,#0d1f0d)" if _fx_result['status'] in ('ACTION_REQUIRED','BUY_THE_DIP')
            else "linear-gradient(135deg,#1a1200,#2a1800)"
        )
        st.markdown(
            f"<div style='background:{_fx_bg};border:2px solid {_fx_c}60;border-radius:16px;"
            f"padding:20px 24px;margin-bottom:16px'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center'>"
            f"<div>"
            f"<div style='font-size:28px;margin-bottom:4px'>{_fx_result['icon']}</div>"
            f"<div style='font-size:16px;font-weight:800;color:{_fx_c}'>{_fx_result['message']}</div>"
            f"<div style='font-size:12px;color:#94a3b8;margin-top:6px'>→ {_fx_result['action']}</div>"
            f"</div>"
            f"<div style='text-align:right'>"
            f"<div style='font-size:11px;color:#64748b'>기준 환율</div>"
            f"<div style='font-size:32px;font-weight:900;color:{_fx_c};font-family:monospace'>{_fx_now:,.0f}</div>"
            f"<div style='font-size:10px;color:#64748b'>KRW/USD</div>"
            f"</div>"
            f"</div></div>",
            unsafe_allow_html=True
        )

        # ─── 환율 임계값 슬라이더 ────────────────────────────
        _fx_threshold = st.slider(
            "환율 이동 기준선 (원)", min_value=1300, max_value=1600,
            value=st.session_state.get('fx_threshold', 1450), step=10,
            key="fx_threshold_slider",
            help="이 값 이하일 때 미장 자산 이동 신호 발생"
        )
        st.session_state['fx_threshold'] = _fx_threshold
        if _fx_threshold != 1450:
            _fx_custom = check_profit_recycling(_fx_now, _fx_threshold)
            st.caption(f"🎯 커스텀 기준 {_fx_threshold:,}원 적용 시: **{_fx_custom['message']}**")

        st.divider()

        # ─── 이중 엔진 현황판 ────────────────────────────────
        st.markdown("#### ⚡ 이중 엔진 현황 — 공격(국장) vs 방어(미장)")
        _acc_h = load_account()

        # 계좌를 국장(KR)/미장(US)로 분리 평가 (국장 카드가 미장까지 합산하던 버그 수정)
        def _value_positions(_positions, _fx):
            """포지션 리스트의 (현재평가액, 원가) — 모두 KRW. cur는 fetch_ohlcv 최신가."""
            _val, _cost = 0.0, 0.0
            for _p in _positions:
                try:
                    _pdf = fetch_ohlcv(_p['ticker'], 5)
                    _cp = float(_pdf['종가'].iloc[-1]) if (_pdf is not None and not _pdf.empty) else float('nan')
                    if not (_cp == _cp) or _cp <= 0:
                        _cp = _p['avg_price']
                except Exception:
                    _cp = _p['avg_price']
                _val  += _cp * _p['qty'] * _fx
                _cost += _p['avg_price'] * _p['qty'] * _fx
            return _val, _cost

        _usd_krw_h = get_usd_krw()
        _kr_pos = [p for p in _acc_h.get('positions', []) if is_korean_ticker(p['ticker'])]
        _us_pos = [p for p in _acc_h.get('positions', []) if not is_korean_ticker(p['ticker'])]
        _kr_val, _kr_cost = _value_positions(_kr_pos, 1.0)
        _us_val, _us_cost = _value_positions(_us_pos, _usd_krw_h)
        _cash_h = _acc_h.get('cash', 0)

        # 국장(공격) = 현금(KRW) + 국내 종목 평가 / 손익은 국내 종목 기준
        _tv_h      = _cash_h + _kr_val            # 국장 엔진 평가(현금 포함)
        _pnl_h     = _kr_val - _kr_cost           # 국내 종목 손익
        _kr_initial = _acc_h['initial'] - _us_cost  # 미장 투입분 제외한 국장 초기자본
        _kr_initial = _kr_initial if _kr_initial > 0 else _acc_h['initial']
        _pnl_pct_h = (_pnl_h / _kr_initial * 100) if _kr_initial > 0 else 0
        # 미장(방어) 실제 평가/손익
        _us_pnl     = _us_val - _us_cost
        _us_pnl_pct = (_us_pnl / _us_cost * 100) if _us_cost > 0 else 0

        _lm_h = not st.session_state.get('ui_dark', True)
        _pan_bg = "#ffffff" if _lm_h else "#0d1117"
        _pan_tx = "#0f172a" if _lm_h else "#f0f4ff"
        _pan_bd = "#e2e8f0" if _lm_h else "#1e293b"
        _profit_c  = ("#166534" if _lm_h else "#39ff14") if _pnl_h >= 0 else ("#991B1B" if _lm_h else "#ff003c")
        _us_profit_c = ("#166534" if _lm_h else "#39ff14") if _us_pnl >= 0 else ("#991B1B" if _lm_h else "#ff003c")

        _eng_l, _eng_r = st.columns(2)

        # 국장 공격 엔진
        _eng_l.markdown(
            f"<div style='background:{_pan_bg};border:2px solid {_pan_bd};border-radius:14px;"
            "padding:16px 20px;height:100%'>"
            "<div style='font-size:12px;font-weight:700;color:#3b82f6;margin-bottom:10px'>"
            "🇰🇷 공격 엔진 — 국장 (삼성증권)</div>"
            "<div style='display:grid;grid-template-columns:1fr 1fr;gap:8px'>"
            f"<div><div style='font-size:10px;color:#64748b'>초기자본(현금+국내)</div>"
            f"<div style='font-size:14px;font-weight:700;color:{_pan_tx}'>{_kr_initial/1e6:.1f}M</div></div>"
            f"<div><div style='font-size:10px;color:#64748b'>현재 평가(현금+국내)</div>"
            f"<div style='font-size:14px;font-weight:700;color:{_pan_tx}'>{_tv_h/1e6:.1f}M</div></div>"
            f"<div><div style='font-size:10px;color:#64748b'>총 손익</div>"
            f"<div style='font-size:16px;font-weight:800;color:{_profit_c}'>{_pnl_h:+,.0f}원</div></div>"
            f"<div><div style='font-size:10px;color:#64748b'>수익률</div>"
            f"<div style='font-size:16px;font-weight:800;color:{_profit_c}'>{_pnl_pct_h:+.2f}%</div></div>"
            "</div>"
            "<div style='margin-top:12px;padding-top:10px;border-top:1px solid #1e293b'>"
            "<div style='font-size:10px;color:#64748b;margin-bottom:4px'>킬스위치 규칙</div>"
            "<div style='font-size:11px;color:#94a3b8'>-7% 스마트 킬 · -10% 하드 서킷 브레이커</div>"
            "</div>"
            "</div>",
            unsafe_allow_html=True
        )

        # 미장 방어 엔진
        _div_etfs = {
            "JEPQ": {"name": "JPMorgan 나스닥 커버드콜", "freq": "월배당", "yield_pct": 10.5},
            "SCHD": {"name": "Schwab 배당성장", "freq": "분기배당", "yield_pct": 3.4},
            "MAIN": {"name": "Main Street Capital", "freq": "월배당+특별", "yield_pct": 6.2},
            "JEPI": {"name": "JPMorgan S&P500 커버드콜", "freq": "월배당", "yield_pct": 7.8},
        }
        _daily_krw = st.session_state.get('daily_div_krw', 5000)
        _monthly_div = _daily_krw * 30
        _eng_r.markdown(
            f"<div style='background:{_pan_bg};border:2px solid {_pan_bd};border-radius:14px;"
            "padding:16px 20px;height:100%'>"
            "<div style='font-size:12px;font-weight:700;color:#fbbf24;margin-bottom:10px'>"
            "🇺🇸 방어 엔진 — 미장 배당 (토스)</div>"
            "<div style='display:grid;grid-template-columns:1fr 1fr;gap:8px'>"
            f"<div><div style='font-size:10px;color:#64748b'>일 적립 목표</div>"
            f"<div style='font-size:14px;font-weight:700;color:#fbbf24'>{_daily_krw:,}원/일</div></div>"
            f"<div><div style='font-size:10px;color:#64748b'>월 예상 배당</div>"
            f"<div style='font-size:14px;font-weight:700;color:#39ff14'>{_monthly_div:,}원</div></div>"
            "<div><div style='font-size:10px;color:#64748b'>핵심 종목</div>"
            f"<div style='font-size:11px;color:{_pan_tx}'>JEPQ · SCHD · MAIN</div></div>"
            "<div><div style='font-size:10px;color:#64748b'>전략</div>"
            f"<div style='font-size:11px;color:{_pan_tx}'>Buy the Dip ≤1,400원</div></div>"
            "</div>"
            + (
                "<div style='margin-top:10px;padding-top:8px;border-top:1px solid #1e293b'>"
                "<div style='font-size:10px;color:#64748b;margin-bottom:2px'>미장 실제 보유 평가 / 손익</div>"
                f"<div style='font-size:13px;font-weight:700;color:{_pan_tx}'>{_us_val/1e6:.2f}M "
                f"<span style='color:{_us_profit_c}'>({_us_pnl:+,.0f}원 · {_us_pnl_pct:+.2f}%)</span></div>"
                "</div>"
                if _us_pos else ""
            )
            + "<div style='margin-top:12px;padding-top:10px;border-top:1px solid #1e293b'>"
            "<div style='font-size:10px;color:#64748b;margin-bottom:4px'>수익 순환 규칙</div>"
            "<div style='font-size:11px;color:#94a3b8'>익절 수익 30% 달러 파킹 → 환율 ≤1,450 시 매수</div>"
            "</div>"
            "</div>",
            unsafe_allow_html=True
        )

        st.markdown("<div style='margin-top:12px'></div>", unsafe_allow_html=True)

        # 일 배당 목표 설정
        _new_daily = st.number_input(
            "💵 일 배당 목표 (원)", min_value=1000, max_value=500000,
            value=_daily_krw, step=1000, key="daily_div_krw_input"
        )
        st.session_state['daily_div_krw'] = int(_new_daily)

        st.divider()

        # ─── 배당 캘린더 ─────────────────────────────────────
        st.markdown("#### 📅 배당 스케줄 — 매일 들어오는 현금 흐름")

        # 배당 ETF 스케줄 (월별 ex-dividend 예상일)
        import calendar as _cal_mod
        from datetime import datetime as _dt_div, date as _date_div
        _today = _date_div.today()
        _yr, _mo = _today.year, _today.month

        # 배당 종목별 지급 패턴
        _DIV_SCHEDULE = {
            "JEPQ":  {"color": "#3b82f6", "months": list(range(1,13)),    "day": 7,  "yield": 10.5, "freq": "매월",    "name": "JP모건 나스닥"},
            "JEPI":  {"color": "#8b5cf6", "months": list(range(1,13)),    "day": 7,  "yield": 7.8,  "freq": "매월",    "name": "JP모건 프리미엄"},
            "MAIN":  {"color": "#f59e0b", "months": list(range(1,13)),    "day": 15, "yield": 6.2,  "freq": "매월+특별", "name": "메인 스트리트"},
            "SCHD":  {"color": "#10b981", "months": [3,6,9,12],           "day": 25, "yield": 3.4,  "freq": "분기",    "name": "슈왑 배당주"},
        }

        # 이번 달 캘린더 그리드
        _cal_days = _cal_mod.monthcalendar(_yr, _mo)
        _mo_name  = f"{_yr}년 {_mo}월"
        _div_days  = {}
        for _sym, _info in _DIV_SCHEDULE.items():
            if _mo in _info['months']:
                _div_days[_info['day']] = _div_days.get(_info['day'], [])
                _div_days[_info['day']].append((_sym, _info['color']))

        _days_label = ["월","화","수","목","금","토","일"]
        _cal_html = (
            f"<div style='background:#0d1117;border:1px solid #1e293b;border-radius:14px;"
            f"padding:16px 20px;margin-bottom:16px'>"
            f"<div style='font-size:13px;font-weight:700;color:#f0f4ff;margin-bottom:12px'>{_mo_name} 배당 캘린더</div>"
            f"<div style='display:grid;grid-template-columns:repeat(7,1fr);gap:4px'>"
        )
        for _dl in _days_label:
            _cal_html += f"<div style='text-align:center;font-size:10px;font-weight:700;color:#64748b;padding:4px'>{_dl}</div>"
        for _week in _cal_days:
            for _d in _week:
                if _d == 0:
                    _cal_html += "<div></div>"
                else:
                    _is_today = (_d == _today.day)
                    _has_div  = _d in _div_days
                    _is_past  = _d < _today.day
                    _bg = "#1e3a5f" if _is_today else ("#0a2a0a" if _has_div else "#0d1117")
                    _border = "2px solid #3b82f6" if _is_today else ("1px solid #22c55e40" if _has_div else "1px solid #1e293b")
                    _day_str = f"<div style='font-size:11px;font-weight:700;color:{'#3b82f6' if _is_today else ('#94a3b8' if _is_past else '#f0f4ff')}'>{_d}</div>"
                    _badge_str = ""
                    if _has_div:
                        for _sym, _sc in _div_days[_d]:
                            _badge_str += f"<div style='font-size:8px;color:{_sc};font-weight:700'>{_sym}</div>"
                    _cal_html += (
                        f"<div style='background:{_bg};border:{_border};border-radius:6px;"
                        f"padding:5px 4px;text-align:center;min-height:44px'>"
                        f"{_day_str}{_badge_str}</div>"
                    )
        _cal_html += "</div></div>"
        st.markdown(_cal_html, unsafe_allow_html=True)

        # ─── 배당 ETF 상세 카드 ─────────────────────────────
        st.markdown("#### 📊 배당 자산 현황")
        _div_cols = st.columns(len(_DIV_SCHEDULE))
        for _di, (_sym, _info) in enumerate(_DIV_SCHEDULE.items()):
            # yfinance로 현재가 조회
            try:
                import yfinance as _yf_div
                _dh = _yf_div.Ticker(_sym).history(period="5d")
                _dprice = float(_dh['Close'].iloc[-1]) if not _dh.empty else 0
                _dprev  = float(_dh['Close'].iloc[-2]) if len(_dh) >= 2 else _dprice
                _dchg   = (_dprice / _dprev - 1) * 100 if _dprev > 0 else 0
                _annual_div = _dprice * _info['yield'] / 100
                _monthly_est = _annual_div / 12 if '월' in _info['freq'] else _annual_div / 4
            except Exception:
                _dprice = 0; _dchg = 0; _monthly_est = 0
            _dc = _info['color']
            _chg_c = ("#166534" if _lm_h else "#39ff14") if _dchg >= 0 else ("#991B1B" if _lm_h else "#ff003c")
            _div_cols[_di].markdown(
                f"<div style='background:#0d1117;border:2px solid {_dc}30;border-radius:12px;padding:12px 14px;text-align:center'>"
                f"<div style='font-size:14px;font-weight:800;color:{_dc}'>{_sym}</div>"
                f"<div style='font-size:9px;color:#64748b;margin-bottom:8px'>{_info['name'][:12]}</div>"
                f"<div style='font-size:16px;font-weight:700;color:#f0f4ff'>${_dprice:.2f}</div>"
                f"<div style='font-size:11px;color:{_chg_c};margin:2px 0'>{'▲' if _dchg>=0 else '▼'}{abs(_dchg):.2f}%</div>"
                f"<div style='border-top:1px solid #1e293b;margin-top:8px;padding-top:8px'>"
                f"<div style='font-size:9px;color:#64748b'>예상 배당수익률</div>"
                f"<div style='font-size:13px;font-weight:800;color:#fbbf24'>{_info['yield']:.1f}%</div>"
                f"<div style='font-size:9px;color:#64748b;margin-top:2px'>{_info['freq']}</div>"
                f"<div style='font-size:10px;color:#39ff14;margin-top:4px'>月 ${_monthly_est:.2f}/주</div>"
                f"</div></div>",
                unsafe_allow_html=True
            )

        st.divider()

        # ─── 수익 순환 가이드 ────────────────────────────────
        st.markdown("#### 🔄 수익 순환 프로세스")
        _guide_html = (
            "<div style='background:#0d1117;border:1px solid #1e293b;border-radius:14px;padding:16px 20px'>"
            "<div style='display:grid;grid-template-columns:repeat(4,1fr);gap:4px;text-align:center'>"
        )
        _steps_g = [
            ("🏆", "익절 발생", "국장 -7% 킬스위치\n이전 목표가 도달", "#3b82f6"),
            ("💵", "30% 달러 파킹", "수익의 30%를\n달러 환전 후 대기", "#fbbf24"),
            ("📡", "환율 모니터링", f"현재 {_fx_now:,.0f}원\n기준 {_fx_threshold:,}원 이하", _fx_c),
            ("📈", "배당 자산 매수", "JEPQ · SCHD · MAIN\n시장가 즉시 매수", "#39ff14"),
        ]
        for _gi, (_icon, _title, _desc, _gc) in enumerate(_steps_g):
            _arrow = "<div style='font-size:18px;color:#334155;align-self:center'>→</div>" if _gi < 3 else ""
            _guide_html += (
                f"<div style='background:#111827;border:1px solid {_gc}30;border-radius:10px;padding:12px 8px'>"
                f"<div style='font-size:24px;margin-bottom:6px'>{_icon}</div>"
                f"<div style='font-size:11px;font-weight:700;color:{_gc};margin-bottom:4px'>{_title}</div>"
                f"<div style='font-size:10px;color:#64748b;white-space:pre-line'>{_desc}</div>"
                f"</div>"
            )
        _guide_html += "</div></div>"
        st.markdown(_guide_html, unsafe_allow_html=True)

    # ══════════════════════════════════════════
    # 탭 2: 차트 분석
    # ══════════════════════════════════════════

st.markdown("---")
st.markdown("<div style='text-align:center;font-size:11px;color:rgba(255,255,255,0.1);font-family:IBM Plex Mono'>퀀트 관제탑 V9.1 | 투자 자문 아님 — 모든 손익의 책임은 본인에게 있습니다</div>", unsafe_allow_html=True)
