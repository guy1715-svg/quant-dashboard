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
            # [V10.2 P2] 하드 인덱싱 → .get() + 누락 시 graceful degradation(크래시 대신 세션 폴백)
            _fb_cfg  = st.secrets.get("firebase")
            _fb_conf = st.secrets.get("firebase_config")
            if not _fb_cfg or not _fb_conf or not (_fb_conf.get("database_url") if hasattr(_fb_conf, "get") else None):
                st.warning("⚠️ Firebase 설정 키가 누락되었습니다 — 저장은 세션에 임시 보관됩니다.")
                return None
            _fb_cred = fb_credentials.Certificate(dict(_fb_cfg))
            _db_url  = _fb_conf["database_url"]
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

# [V9.28] 시장별 1위 스냅샷 영구 저장 — 국/미장 격리 + 과거 역산 방식
_LOCAL_TOP1_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "top1_history.json"
)


def _top1_history_read_local() -> dict:
    """로컬 top1_history.json 읽기. 없거나 파싱 실패 시 {}."""
    try:
        with open(_LOCAL_TOP1_HISTORY_PATH, "r", encoding="utf-8") as _f:
            _d = _json_tracker.load(_f)
            return _d if isinstance(_d, dict) else {}
    except (FileNotFoundError, _json_tracker.JSONDecodeError, OSError):
        return {}


def _top1_history_write_local(data: dict) -> None:
    """로컬 top1_history.json 덮어쓰기. 실패는 조용히 무시."""
    try:
        with open(_LOCAL_TOP1_HISTORY_PATH, "w", encoding="utf-8") as _f:
            _json_tracker.dump(data, _f, ensure_ascii=False)
    except OSError:
        pass


def _get_rotation_day_count(top1_ticker: str, market: str = "KR") -> dict:
    """
    [V9.28] 시장별 1위 스냅샷 append + 과거 역산 방식 연속 1위 일차 계산.

    세션/캐시 누적 카운터를 전면 폐기 — 대신 매일 시장별 1위 티커·날짜를 영구
    저장소에 append 하고, 조회 시마다 최근 기록을 '역산'해 연속 일차를 동적 계산.
    → 세션 초기화·국/미장 전환·서버 재부팅과 무관하게 기록만 있으면 일차가 복원됨.

    저장 포맷 (국장/미장 상태공간 완전 격리):
      {"KR": [{"date": "YYYY-MM-DD", "ticker": "..."}, ...],
       "US": [{"date": "YYYY-MM-DD", "ticker": "..."}, ...]}

    저장소 우선순위: Firebase /top1_history (1순위) → top1_history.json (폴백). 듀얼 라이트.

    날짜 Lock: 해당 시장에 오늘 기록이 이미 있으면 append 없이 그 티커로 역산
      (장중 순위 flip-flop 방지 — 그날 첫 1위를 확정).

    역산 규칙: 최신(오늘) → 과거로 동일 티커가 연속되는 run 길이를 계산.
      연속 판정은 인접 기록일 간격이 1~5 캘린더일(주말·공휴일 허용)일 때만 성립.
      "D-2 == D-1 == D-Day" 모두 일치 시 3일차. count 는 1~3로 클램프.

    반환 dict: count(1~3), ticker(역산 기준 티커), last_date(오늘, KST), is_locked(bool).
    """
    import datetime as _dt
    _mk = "US" if str(market).upper().startswith("US") else "KR"
    _kst_today = (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y-%m-%d")
    _top1_ticker = str(top1_ticker)

    # ── [하이브리드 READ] Firebase 우선, 실패/비설정 시 로컬 JSON 폴백 ──
    _ref       = _fb_ref("/top1_history")
    _fb_data   = _ref.get()            # NullRef.get() → None
    _use_local = (_fb_data is None)
    _data      = _fb_data if not _use_local else _top1_history_read_local()
    if not isinstance(_data, dict):
        _data = {}
    _kr_list = _data.get("KR") if isinstance(_data.get("KR"), list) else []
    _us_list = _data.get("US") if isinstance(_data.get("US"), list) else []
    _mk_list = _us_list if _mk == "US" else _kr_list

    # ── 날짜 Lock: 오늘 기록이 이미 있으면 append 없이 그 티커로 역산 ──
    _today_entry = next((e for e in _mk_list
                         if isinstance(e, dict) and e.get("date") == _kst_today), None)
    _is_locked = _today_entry is not None
    if _is_locked:
        _eff_ticker = str(_today_entry.get("ticker", _top1_ticker))
    else:
        # 오늘 스냅샷 append 후 듀얼 라이트 (최근 30개만 유지 — 무한증식 방지)
        _eff_ticker = _top1_ticker
        _mk_list = _mk_list + [{"date": _kst_today, "ticker": _top1_ticker}]
        _mk_list = sorted([e for e in _mk_list if isinstance(e, dict) and e.get("date")],
                          key=lambda e: e["date"])[-30:]
        if _mk == "US":
            _data["US"] = _mk_list; _data.setdefault("KR", _kr_list)
        else:
            _data["KR"] = _mk_list; _data.setdefault("US", _us_list)
        _ref.set(_data)                 # NullRef.set() → 조용히 무시
        _top1_history_write_local(_data)

    # ── 역산: 날짜별 1위(중복일은 마지막 우선)로 접은 뒤 최신→과거 동일 티커 run 계산 ──
    _by_date = {}
    for e in _mk_list:
        if isinstance(e, dict) and e.get("date"):
            _by_date[str(e["date"])] = str(e.get("ticker", ""))
    _dates = sorted(_by_date.keys())
    _count, _prev_d = 0, None
    for _d in reversed(_dates):
        if _by_date[_d] != _eff_ticker:
            break
        if _prev_d is not None:
            try:
                _gap = (_dt.datetime.strptime(_prev_d, "%Y-%m-%d").date()
                        - _dt.datetime.strptime(_d, "%Y-%m-%d").date()).days
                if not (1 <= _gap <= 5):
                    break
            except Exception:
                break
        _count += 1
        _prev_d = _d
        if _count >= 3:
            break
    _count = max(1, min(_count, 3))

    return {
        "count": _count,
        "ticker": _eff_ticker,
        "last_date": _kst_today,
        "is_locked": _is_locked,
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

def _secret_lookup(*names):
    """st.secrets에서 값 탐색 — 최상위 + 1단계 섹션([kis] 등)까지, 키 이름 대소문자 무시.
    이름이 조금 달라도(별칭) 잡히도록. 반환: 문자열 값 or ''."""
    _wanted = {n.lower() for n in names}
    try:
        _sec = st.secrets
    except Exception:
        return ""
    # 1) 최상위 키
    try:
        for _k in list(_sec.keys()):
            if str(_k).lower() in _wanted:
                _v = _sec[_k]
                if isinstance(_v, str) and _v.strip():
                    return _v.strip()
    except Exception:
        pass
    # 2) 섹션(1단계 중첩) 내부
    try:
        for _k in list(_sec.keys()):
            _v = _sec[_k]
            if hasattr(_v, "keys"):
                for _kk in list(_v.keys()):
                    if str(_kk).lower() in _wanted:
                        _vv = _v[_kk]
                        if isinstance(_vv, str) and _vv.strip():
                            return _vv.strip()
    except Exception:
        pass
    return ""

def _secret_top_keys():
    """진단용 — secrets에 실제로 존재하는 항목 이름 목록(값 아님)."""
    _out = []
    try:
        for _k in list(st.secrets.keys()):
            _v = st.secrets[_k]
            if hasattr(_v, "keys"):
                _out.append(f"[{_k}]:" + "/".join(str(x) for x in list(_v.keys())))
            else:
                _out.append(str(_k))
    except Exception:
        pass
    return _out

def _kis_key():
    """KIS App Key — 사이드바 입력 → secrets(별칭·섹션 포함) → 환경변수."""
    _v = st.session_state.get('_kis_app_key_input', '')
    if _v:
        return _v
    _s = _secret_lookup("KIS_APP_KEY", "KIS_KEY", "APP_KEY", "KIS_APPKEY", "kis_app_key")
    if _s:
        return _s
    import os as _os_k
    return _os_k.environ.get("KIS_APP_KEY", "")

def _kis_secret():
    """KIS App Secret — 사이드바 입력 → secrets(별칭·섹션 포함) → 환경변수."""
    _v = st.session_state.get('_kis_app_secret_input', '')
    if _v:
        return _v
    _s = _secret_lookup("KIS_APP_SECRET", "KIS_SECRET", "APP_SECRET", "KIS_APPSECRET", "kis_app_secret")
    if _s:
        return _s
    import os as _os_k
    return _os_k.environ.get("KIS_APP_SECRET", "")

def _kis_mock_mode():
    """모의투자 모드 여부 — 사이드바 토글 → secrets KIS_MODE → 기본 실전."""
    _t = st.session_state.get('_kis_mock_input')
    if _t is not None:
        return bool(_t)
    try:
        return str(st.secrets.get("KIS_MODE", "")).lower() in ("mock", "vts", "모의")
    except Exception:
        return False

def _kis_base():
    """실전/모의투자 도메인 분기 — 모의 키를 실전 도메인에 쓰면 무조건 토큰 실패."""
    return ("https://openapivts.koreainvestment.com:29443" if _kis_mock_mode()
            else "https://openapi.koreainvestment.com:9443")

# 토큰 수동 캐시 — st.cache_resource는 실패(None)까지 6시간 캐시하는 버그가 있어
# 성공만 캐시하고 실패는 즉시 재시도 가능하도록 dict로 직접 관리.
_KIS_TOKEN_CACHE: dict = {}      # {key_fp: (token, expiry_ts)}
_KIS_TOKEN_COOLDOWN: dict = {}   # {key_fp: retry_after_ts} — EGW00133(1분1회) 재발급 폭주 차단
_KIS_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kis_token_cache.json")


def _kis_token_load_persist(fp):
    """[영속] Firebase→로컬파일에서 유효 토큰 복원 — 앱 재부팅에도 재사용해 불필요한
    재발급(EGW00133 1분1회 제한)을 방지. 반환 (token, exp) 또는 None."""
    _nowp = _time_kis.time()
    try:
        _d = _fb_ref("/kis_token").get()          # NullRef.get()→None (토스트 없음)
        if isinstance(_d, dict) and _d.get('fp') == fp and _d.get('token') and float(_d.get('exp', 0)) > _nowp:
            return _d['token'], float(_d['exp'])
    except Exception:
        pass
    try:
        import json as _jkt
        with open(_KIS_TOKEN_FILE, encoding='utf-8') as _f:
            _d = _jkt.load(_f)
        if _d.get('fp') == fp and _d.get('token') and float(_d.get('exp', 0)) > _nowp:
            return _d['token'], float(_d['exp'])
    except Exception:
        pass
    return None


def _kis_token_save_persist(fp, token, exp):
    """[영속] 발급 성공 토큰을 Firebase(연결 시)+로컬파일에 저장. NullRef 토스트 회피 위해
    Firebase는 실제 연결 확인 후에만 기록."""
    _payload = {'fp': fp, 'token': token, 'exp': float(exp)}
    try:
        if _get_firebase_app() is not None:
            _fb_ref("/kis_token").set(_payload)
    except Exception:
        pass
    try:
        import json as _jkt
        with open(_KIS_TOKEN_FILE, 'w', encoding='utf-8') as _f:
            _jkt.dump(_payload, _f)
    except Exception:
        pass


def kis_get_token():
    """KIS API 접근 토큰 — 성공만 캐시(동적 TTL), 실패는 쿨다운으로 재발급 폭주 차단.
    ⚠️ KIS 토큰 발급은 '1분당 1회'(EGW00133) 제한. 발급 실패 시 60초 쿨다운을 걸어
    rerun/다중호출마다 재요청해 계속 막히는 악순환을 방지하고, 만료 캐시 토큰은 유예 재사용."""
    _key    = _kis_key()
    _secret = _kis_secret()
    if not _key or not _secret:
        st.session_state['_kis_token_err'] = "App Key/Secret 미입력"
        return None
    _fp = f"{_key[:8]}|{_kis_mock_mode()}"
    _now_ts = _time_kis.time()
    _hit = _KIS_TOKEN_CACHE.get(_fp)
    if _hit and _hit[1] > _now_ts:
        return _hit[0]
    # 영속 저장소(Firebase/파일)에서 유효 토큰 복원 — 재부팅으로 메모리 캐시가 날아가도
    # 서버측 24h 유효 토큰을 재사용해 EGW00133(재발급 1분1회) 폭주를 원천 방지.
    _persist = _kis_token_load_persist(_fp)
    if _persist:
        _KIS_TOKEN_CACHE[_fp] = _persist
        _KIS_TOKEN_COOLDOWN.pop(_fp, None)
        st.session_state.pop('_kis_token_err', None)
        return _persist[0]
    # 쿨다운 중이면 네트워크 재요청 금지(1분1회 제한 보호). 만료 캐시 토큰이 있으면 유예 재사용
    # (KIS 토큰은 서버측 24h 유효 — 로컬 만료여도 대개 아직 살아있음).
    _cd = _KIS_TOKEN_COOLDOWN.get(_fp, 0)
    if _cd > _now_ts:
        if _hit:
            return _hit[0]
        st.session_state['_kis_token_err'] = (
            f"토큰 재발급 대기(1분 제한) — {int(_cd - _now_ts)}초 후 자동 재시도")
        return None
    try:
        _res = _requests.post(f"{_kis_base()}/oauth2/tokenP", json={
            "grant_type": "client_credentials",
            "appkey":     _key,
            "appsecret":  _secret
        }, timeout=10)
        _body = _res.json()
        _token = _body.get("access_token")
        if _token:
            # [V10.3 P4] TTL 동적 파싱 — 서버 expires_in(초) 우선, 60초 안전마진 선차감.
            try:
                _ttl = int(float(_body.get("expires_in", 21600)))
            except (TypeError, ValueError):
                _ttl = 21600
            if _ttl <= 0:
                _ttl = 21600
            _ttl = max(60, _ttl - 60)
            _KIS_TOKEN_CACHE[_fp] = (_token, _now_ts + _ttl)
            _KIS_TOKEN_COOLDOWN.pop(_fp, None)
            _kis_token_save_persist(_fp, _token, _now_ts + _ttl)   # 재부팅 재사용용 영속화
            st.session_state.pop('_kis_token_err', None)
            return _token
        # 실패 — KIS 에러 코드/메시지 보존 + 쿨다운(EGW00133=1분1회 → 60초)
        _errc = str(_body.get('error_code', _res.status_code))
        _KIS_TOKEN_COOLDOWN[_fp] = _now_ts + 60
        st.session_state['_kis_token_err'] = (
            f"{_errc}: {_body.get('error_description', str(_body)[:120])}")
    except Exception as _e:
        _KIS_TOKEN_COOLDOWN[_fp] = _now_ts + 30   # 네트워크 오류는 30초 쿨다운
        st.session_state['_kis_token_err'] = f"{type(_e).__name__}: {str(_e)[:100]}"
    return None

def kis_get_price(ticker):
    """KIS API 실시간 현재가 조회"""
    try:
        _token  = kis_get_token()
        if not _token: return None
        _key    = _kis_key()
        _secret = _kis_secret()
        _url    = f"{_kis_base()}/uapi/domestic-stock/v1/quotations/inquire-price"
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
        _key    = _kis_key()
        _secret = _kis_secret()
        # [V10.2 P2] 하드 인덱싱 → .get() + 누락 시 graceful degradation(실계좌 잔고만 건너뜀)
        _acc_no = st.secrets.get("KIS_ACCOUNT_NO")
        if not _acc_no:
            st.warning("⚠️ KIS 계좌번호(KIS_ACCOUNT_NO) 설정 키가 누락되었습니다 — 실계좌 잔고 조회를 건너뜁니다.")
            return None
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
        _key    = _kis_key()
        _secret = _kis_secret()
        _url    = f"{_kis_base()}/uapi/domestic-stock/v1/quotations/inquire-investor"
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


def _md_investor_naver(code):
    """[수급 폴백] 네이버 종목별 외국인/기관 순매매(전일·주) — item/frgn.naver 표 첫 행.
    KRX/pykrx가 클라우드에서 차단돼도 네이버는 대개 열림. 순수 네트워크(캐시 함수 내 안전).
    반환 {'외인','기관','unit','src'} 또는 None."""
    import io as _io_nv
    _hdr = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
            "Referer": "https://finance.naver.com/"}
    try:
        _r = _requests.get(f"https://finance.naver.com/item/frgn.naver?code={code}",
                           headers=_hdr, timeout=6)
        _r.encoding = "euc-kr"
        _tables = pd.read_html(_io_nv.StringIO(_r.text), thousands=",")
        for _t in _tables:
            # 2단 헤더(기관/외국인 상위 · 순매매량/보유주수 하위)를 문자열로 합쳐 평탄화
            _joined = [" ".join(str(_x) for _x in _c) if isinstance(_c, tuple) else str(_c)
                       for _c in _t.columns]
            _org_col = next((_j for _j in _joined if "기관" in _j and "순매매" in _j), None)
            _frn_col = next((_j for _j in _joined if "외국인" in _j and "순매매" in _j), None)
            if not (_org_col and _frn_col):
                continue
            _t2 = _t.copy(); _t2.columns = _joined
            _t2 = _t2.dropna(subset=[_joined[0]])
            if _t2.empty:
                continue
            _first = _t2.iloc[0]
            _org = pd.to_numeric(_first[_org_col], errors="coerce")
            _frn = pd.to_numeric(_first[_frn_col], errors="coerce")
            if (_org == _org) or (_frn == _frn):
                return {'외인': int(_frn) if _frn == _frn else 0,
                        '기관': int(_org) if _org == _org else 0,
                        'unit': '주', 'src': 'naver(전일)'}
    except Exception as _e:
        import logging as _lg_nv
        _lg_nv.warning("naver 수급 폴백 %s 실패: %s: %s", code, type(_e).__name__, _e)
    return None


# ── 🛰️ 수급 펌프 추적기 — 반도체 대장주 외인·기관 매집 감시 ────────────────────
_TOP2_SUPPLY_TARGETS = [("005930", "삼성전자"), ("000660", "SK하이닉스")]


@st.cache_data(ttl=60, show_spinner=False)
def fetch_top2_supply(token):
    """[수급 펌프 추적기] 삼성전자·SK하이닉스 당일 외국인/기관 누적 순매수(가집계) + 등락률 실시간 조회.
    ⚠️ token은 호출측(캐시 밖)에서 미리 발급해 주입 — 캐시 함수 내부 session_state 쓰기 회피(오염 차단).
    네트워크 실패는 종목·항목별로 격리(부분 성공 허용) → 절대 예외 전파/크래시 없음.
    반환: {ticker: {name, 현재가, 등락률, 외인순매수, 기관순매수, ok}, '_ok': bool}."""
    import logging as _lg
    if not token:
        return {'_ok': False, '_err': '토큰 없음'}
    _out = {}
    _any_ok = False
    _hdr_base = {"authorization": f"Bearer {token}", "appkey": _kis_key(), "appsecret": _kis_secret()}
    for _tk, _nm in _TOP2_SUPPLY_TARGETS:
        _rec = {'name': _nm, '현재가': None, '등락률': None,
                '외인순매수': None, '기관순매수': None, 'ok': False}
        # (1) 투자자 수급 — 외인/기관 누적 순매수(주)
        try:
            _ri = _requests.get(
                f"{_kis_base()}/uapi/domestic-stock/v1/quotations/inquire-investor",
                headers={**_hdr_base, "tr_id": "FHKST01010900"},
                params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": _tk}, timeout=5)
            _oi = _ri.json().get("output", [])
            if isinstance(_oi, list) and _oi and isinstance(_oi[0], dict):
                _l = _oi[0]
                _rec['외인순매수'] = int(str(_l.get("frgn_ntby_qty", 0)).replace(",", "") or 0)
                _rec['기관순매수'] = int(str(_l.get("orgn_ntby_qty", 0)).replace(",", "") or 0)
        except Exception as _e:
            _lg.warning("fetch_top2_supply[%s] 투자자 조회 실패: %s: %s", _tk, type(_e).__name__, _e)
        # KIS 투자자 API가 빈 값(장마감/미제공)이면 네이버 전일 수급으로 폴백(순수 네트워크=캐시 안전)
        if not _rec['외인순매수'] and not _rec['기관순매수']:
            _nv = _md_investor_naver(_tk)
            if _nv:
                _rec['외인순매수'] = _nv['외인']
                _rec['기관순매수'] = _nv['기관']
                _rec['출처'] = _nv['src']
        # (2) 현재가/등락률 — 수급-가격 괴리 판정용
        try:
            _rp = _requests.get(
                f"{_kis_base()}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers={**_hdr_base, "tr_id": "FHKST01010100"},
                params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": _tk}, timeout=5)
            _op = _rp.json().get("output", {})
            if isinstance(_op, dict) and _op:
                _rec['현재가'] = int(str(_op.get("stck_prpr", 0)).replace(",", "") or 0)
                _rec['등락률'] = float(str(_op.get("prdy_ctrt", 0)).replace(",", "") or 0)
        except Exception as _e:
            _lg.warning("fetch_top2_supply[%s] 현재가 조회 실패: %s: %s", _tk, type(_e).__name__, _e)
        _rec['ok'] = (_rec['외인순매수'] is not None) and (_rec['등락률'] is not None)
        _any_ok = _any_ok or _rec['ok']
        _out[_tk] = _rec
    _out['_ok'] = _any_ok
    return _out


def render_top2_supply_widget(supply_data):
    """[수급 펌프 추적기] 사이드바 렌더 — 삼성전자·SK하이닉스 외인/기관 누적 순매수 표출 +
    가격↓·순매수↑ '수급 다이버전스'(세력 언더슈팅 기만전술) 매집 경고. 예외 전파 없음."""
    if not isinstance(supply_data, dict) or not supply_data.get('_ok'):
        st.caption("🛰️ 수급 펌프 추적기 — 데이터 대기 (장중·KIS 연결 시 60초 갱신)")
        return
    st.markdown("**🛰️ 수급 펌프 추적기 (외인·기관 매집)**")
    _alerts = []
    for _tk, _nm in _TOP2_SUPPLY_TARGETS:
        _r = supply_data.get(_tk)
        if not isinstance(_r, dict) or not _r.get('ok'):
            st.caption(f"· {_nm}: 데이터 수신 실패")
            continue
        _chg = _r['등락률'] or 0.0
        _frn = _r['외인순매수'] or 0
        _org = _r['기관순매수'] or 0
        _px  = _r['현재가'] or 0
        _stale = '전일' in str(_r.get('출처', ''))   # 네이버 전일 폴백 여부
        _cc  = '#ef4444' if _chg < 0 else '#16a34a'
        _st_tag = " <span style='color:#64748b;font-size:10px'>(전일수급)</span>" if _stale else ""
        st.markdown(
            f"<div style='font-size:12px;font-weight:800;color:#e2e8f0;margin-top:6px'>"
            f"{_nm} <span style='color:{_cc}'>{_chg:+.2f}%</span> "
            f"<span style='color:#64748b'>· {_px:,}원</span>{_st_tag}</div>", unsafe_allow_html=True)
        _c1, _c2 = st.columns(2)
        _c1.metric("외인 누적", f"{_frn:+,}주")
        _c2.metric("기관 누적", f"{_org:+,}주")
        # 수급 다이버전스: 가격 하락 중 순매수 양(+)전환 → 매집. 단, '전일수급'은 실시간 신호가
        # 아니므로 강한 '기만전술' 경보를 띄우지 않음(오탐 방지) — 관찰 캡션까지만.
        if _chg <= -3.0 and (_frn > 0 or _org > 0) and not _stale:
            _who = " · ".join([w for w, v in [("외인", _frn), ("기관", _org)] if v > 0])
            _alerts.append((_nm, _who, _chg, True))
        elif _chg < 0 and (_frn > 0 or _org > 0):
            _alerts.append((_nm, None, _chg, False))
    for _nm, _who, _chg, _strong in _alerts:
        if _strong:
            st.error(f"🔴 [{_nm}] 세력 언더슈팅 기만전술 포착 — 매집 징후 "
                     f"(가격 {_chg:+.1f}% 폭락 중 {_who} 순매수 강력 전환)")
        else:
            st.caption(f"🟡 [{_nm}] 수급 괴리 관찰 — 하락({_chg:+.1f}%) 중 순매수 유입")

def kis_get_org_net_daily(ticker, days=10):
    """종목별 일별 '기관 순매수 수량' 리스트 — KIS FHKST01010900(외인기관 추정).
    반환: (org_list_oldest_first, foreign_total) 또는 (None, 0).
    org_list: 최근 days일의 기관 순매수(오래된→최신 순). 연기금은 기관에 포함됨."""
    try:
        _token = kis_get_token()
        if not _token:
            return None, 0
        _key    = _kis_key()
        _secret = _kis_secret()
        _res = _requests.get(f"{_kis_base()}/uapi/domestic-stock/v1/quotations/inquire-investor", headers={
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
    """KIS API 사용 가능 여부 — 시세/수급 조회는 App Key+Secret만으로 충분.
    (계좌번호 KIS_ACCOUNT_NO는 주문/잔고 전용 — 여기서 요구하면 시세 조회까지
    '키 미설정'으로 오판되는 버그가 있었음 → 2키만 검사하도록 완화)"""
    # 실제 토큰 발급과 '동일한' 감지기(_kis_key/_kis_secret: 사이드바→별칭·섹션→환경변수)를 사용.
    #   기존엔 exact "KIS_APP_KEY" in st.secrets만 검사 → 별칭/섹션에 키가 있으면 지수·수급은
    #   되는데 이 판정만 False로 오판하던 버그. 토큰 경로와 일원화해 근본 수정.
    try:
        return bool(_kis_key() and _kis_secret())
    except Exception:
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

def kis_get_index(iscd, _token=None):
    """KIS 국내 업종/지수 현재가 — iscd: '0001'(코스피)/'1001'(코스닥).
    _token: 캐시 함수 밖에서 미리 발급한 토큰(세션쓰기 회피). 없으면 즉석 발급.
    반환: {'현재': float, '등락': float(%)} 또는 None. 실패는 은폐 없이 None(과거값 반환 금지)."""
    try:
        _token = _token or kis_get_token()
        if not _token:
            return None
        _res = _requests.get(
            f"{_kis_base()}/uapi/domestic-stock/v1/quotations/inquire-index-price",
            headers={
                "authorization": f"Bearer {_token}",
                "appkey": _kis_key(), "appsecret": _kis_secret(),
                "tr_id": "FHPUP02100000", "custtype": "P",
            },
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd},
            timeout=5,
        )
        _out = _res.json().get("output", {})
        if not _out:
            return None
        def _f(_k):
            try:
                return float(str(_out.get(_k, 0) or 0).replace(",", ""))
            except Exception:
                return 0.0
        _cur = _f("bstp_nmix_prpr")
        _chg = _f("bstp_nmix_prdy_ctrt")
        if _cur > 0:
            # [Phase2 prep] 장중 고가/저가 — 휩쏘 override용(수신 시에만 채워짐)
            return {'현재': _cur, '등락': _chg,
                    '고가': _f("bstp_nmix_hgpr"), '저가': _f("bstp_nmix_lwpr")}
        return None
    except Exception:
        return None


def kis_futures_basis_proxy():
    """[V6.1 ①-b] 선물 수급 베이시스 프록시 — KODEX200 ETF vs 코스피200 지수 괴리율.
    외국인이 '현물만 매도(단기 헷지)'하는지, '현·선물 동반 투매(완전 리스크오프)'인지 구별.
    선물 차익거래가 KODEX200 ETF 가격에 즉시 반영되므로, ETF가 지수 대비 디스카운트로
    벌어지면 선물까지 투매(리스크오프), 프리미엄을 지키면 현물성 하락(헷지)으로 근사.
    ▸ 만기 도래하는 선물 종목코드에 의존하지 않아 상시 조회 가능(검증된 엔드포인트만 사용).
    ▸ 90초 세션 스로틀(캐시 함수 아님 → 토큰 세션쓰기 안전).
    반환: {'idx_chg','etf_chg','basis','signal'} 또는 None(데이터 결측=판정 보류)."""
    import time as _t
    try:
        _now = _t.time()
        _cache = st.session_state.get('_basis_cache')
        if _cache and (_now - _cache.get('ts', 0) < 90):
            return _cache.get('val')
        _val = None
        _token = kis_get_token()
        if _token:
            _idx = kis_get_index("2001", _token)     # 코스피200 현물 지수
            _etf = kis_get_price("069500")           # KODEX 200 ETF(선물 차익수급 반영)
            if _idx and _etf:
                _ic = float(_idx.get('등락', 0) or 0)
                _ec = float(_etf.get('등락률', 0) or 0)
                _basis = _ec - _ic                   # ETF등락 - 지수등락 = 선물/차익 프리미엄 프록시
                _sig = "neutral"
                if _ic <= -1.0:                       # 지수가 실제 하락 중일 때만 유의미
                    if _basis <= -0.40:
                        _sig = "riskoff"              # ETF 디스카운트 → 선물 동반투매
                    elif _basis >= 0.05:
                        _sig = "hedge"                # ETF 프리미엄 유지 → 현물성 하락
                _val = {'idx_chg': _ic, 'etf_chg': _ec, 'basis': _basis, 'signal': _sig}
        st.session_state['_basis_cache'] = {'ts': _now, 'val': _val}
        return _val
    except Exception:
        return None


def _kis_index_probe(iscd="0001"):
    """진단용 — KIS 지수 API 원시 응답(status/rt_cd/msg) 반환. 실패 원인 특정용."""
    try:
        _tok = kis_get_token()
        if not _tok:
            return {"단계": "토큰 발급 실패", "사유": st.session_state.get('_kis_token_err', '원인 미상')}
        _res = _requests.get(
            f"{_kis_base()}/uapi/domestic-stock/v1/quotations/inquire-index-price",
            headers={"authorization": f"Bearer {_tok}", "appkey": _kis_key(),
                     "appsecret": _kis_secret(), "tr_id": "FHPUP02100000", "custtype": "P"},
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd}, timeout=5)
        _out = {"HTTP": _res.status_code}
        try:
            _j = _res.json()
            _out["rt_cd"] = _j.get("rt_cd")
            _out["msg1"]  = _j.get("msg1")
            _out["현재지수"] = _j.get("output", {}).get("bstp_nmix_prpr")
            _out["등락률"]  = _j.get("output", {}).get("bstp_nmix_prdy_ctrt")
            # [Phase2 test] 장중 고가/저가 필드 수신 여부 확인용
            _out["고가(hgpr)"] = _j.get("output", {}).get("bstp_nmix_hgpr")
            _out["저가(lwpr)"] = _j.get("output", {}).get("bstp_nmix_lwpr")
        except Exception:
            _out["body"] = _res.text[:400]
        return _out
    except Exception as _e:
        return {"예외": f"{type(_e).__name__}: {str(_e)[:200]}"}


@st.cache_data(ttl=60, show_spinner=False)
def _get_index_quotes_impl(_token, _bucket):
    """실제 지수 수집 (캐시 본체). 토큰은 밖에서 주입 — 캐시 함수 내 세션쓰기(=예외) 회피.
    _bucket: 60초 캐시 버킷 키. 코스피/코스닥=KIS, 나스닥/환율/유가/VIX=yfinance."""
    _r = {}
    # ── 코스피/코스닥: KIS API only (yfinance/FDR/네이버 크롤링 전면 폐기) ──
    for _n, _iscd in [("코스피", "0001"), ("코스닥", "1001")]:
        _idx = kis_get_index(_iscd, _token)   # 미리 발급한 토큰 사용 (세션쓰기 없음)
        if _idx:
            _r[_n] = _idx
        # 실패 시 아무것도 넣지 않음 (Silent Failure 제거 — 과거값 반환 안 함)
    # ── 나스닥/환율/유가/VIX: KIS 미제공 해외물 → yfinance 유지 ──
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


def get_index_quotes():
    """★ 지수/매크로 단일 소스(SSOT) 래퍼.
    KIS 토큰을 캐시 함수 '밖'에서 발급(세션쓰기 허용) 후 캐시 본체에 주입 →
    @st.cache_data 안에서 세션쓰기 예외로 인한 '수신 불가' 버그 원천 차단."""
    try:
        _tok = kis_get_token()
    except Exception:
        _tok = None
    _bucket = int((datetime.utcnow().timestamp()) // 60)   # 60초 캐시 버킷
    return _get_index_quotes_impl(_tok or "", _bucket)


@st.cache_data(ttl=300, show_spinner=False)
def check_index_shutdown() -> tuple:
    """국장 지수 킬스위치 — 코스피/코스닥 당일 -2% 급락 시 신규매수 차단.
    반환: (is_shutdown: bool, reason: str, kospi_chg: float|None, kosdaq_chg: float|None).
    ⚠️ [D1] 데이터 수신 실패 시 0.00%(평온)로 기만하지 않고 is_shutdown=True + chg=None 하드블락."""
    import logging as _lg
    try:
        # 단일 소스(get_index_quotes)에서 코스피/코스닥 등락 참조 (헤더와 값 일치)
        _q = get_index_quotes()
        _kp_raw = (_q or {}).get("코스피", {}).get("등락", None)
        _kq_raw = (_q or {}).get("코스닥", {}).get("등락", None)
        # 데이터 정합성 검사: 둘 중 하나라도 미수신 → 무음 0% 금지, 명시적 위험전환
        if not isinstance(_kp_raw, (int, float)) or not isinstance(_kq_raw, (int, float)):
            _lg.warning("check_index_shutdown: 지수 데이터 수신 실패 (코스피=%r, 코스닥=%r)", _kp_raw, _kq_raw)
            return True, "🔴 지수 데이터 장애 — 수동 확인 요망 (신규매수 차단)", None, None
        _kospi_chg  = round(float(_kp_raw), 2)
        _kosdaq_chg = round(float(_kq_raw), 2)
        if _kospi_chg <= -2.0 or _kosdaq_chg <= -2.0:
            _reason = (
                f"🚨 지수 셧다운 — 코스피 {_kospi_chg:+.2f}% / 코스닥 {_kosdaq_chg:+.2f}% "
                f"(-2.0% 급락) | 개별 지지선 무효 / 신규 매수 차단"
            )
            return True, _reason, _kospi_chg, _kosdaq_chg
        return False, "", _kospi_chg, _kosdaq_chg
    except Exception as _e:
        _lg.warning("check_index_shutdown 예외: %s: %s", type(_e).__name__, _e)
        return True, f"🔴 지수 조회 오류 — 수동 확인 요망 ({type(_e).__name__})", None, None


@st.cache_data(ttl=300, show_spinner=False)
def check_index_shutdown_us() -> tuple:
    """미장 전용 지수 킬스위치 — S&P500(^GSPC)/나스닥(^IXIC) 당일 -2% 급락 시 신규매수 차단.
    반환: (is_shutdown: bool, reason: str, spx_chg: float|None, ndx_chg: float|None).
    ⚠️ [D1] 데이터 수신 실패 시 0.00%(평온)로 기만하지 않고 is_shutdown=True + chg=None 하드블락."""
    import logging as _lg
    try:
        import yfinance as _yf_us
        _res = {}
        for _n, _s in [("S&P500", "^GSPC"), ("나스닥", "^IXIC")]:
            try:
                _h = _yf_us.Ticker(_s).history(period="5d", interval="1d").dropna(subset=['Close'])
                if len(_h) >= 2:
                    _c = float(_h['Close'].iloc[-1]); _p = float(_h['Close'].iloc[-2])
                    if _c > 0 and _p > 0:
                        _res[_n] = round((_c / _p - 1) * 100, 2)
            except Exception as _fe:
                _lg.warning("check_index_shutdown_us[%s] 페치 실패: %s: %s", _n, type(_fe).__name__, _fe)
        _spx = _res.get("S&P500", None)
        _ndx = _res.get("나스닥", None)
        # 데이터 정합성 검사: 둘 중 하나라도 미수신 → 명시적 위험전환
        if not isinstance(_spx, (int, float)) or not isinstance(_ndx, (int, float)):
            _lg.warning("check_index_shutdown_us: 지수 데이터 수신 실패 (spx=%r, ndx=%r)", _spx, _ndx)
            return True, "🔴 美 지수 데이터 장애 — 수동 확인 요망 (신규매수 차단)", None, None
        if _spx <= -2.0 or _ndx <= -2.0:
            _reason = (f"🚨 美 지수 셧다운 — S&P500 {_spx:+.2f}% / 나스닥 {_ndx:+.2f}% "
                       f"(-2.0% 급락) | 신규 매수 차단")
            return True, _reason, _spx, _ndx
        return False, "", _spx, _ndx
    except Exception as _e:
        _lg.warning("check_index_shutdown_us 예외: %s: %s", type(_e).__name__, _e)
        return True, f"🔴 美 지수 조회 오류 — 수동 확인 요망 ({type(_e).__name__})", None, None

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
    # [V10.2 P2] 하드 인덱싱 → .get() + try/except. 키 누락/오류 시 크래시 대신 None 반환.
    #   (Sheets는 Firebase의 폴백 계층 — 누락 시 조용히 None 격하, 관리탭 진단 패널에 상태 표시)
    try:
        _gcp      = st.secrets.get("gcp_service_account")
        _sheet_id = st.secrets.get("SHEET_ID")
        if not _gcp or not _sheet_id:
            return None
        creds = Credentials.from_service_account_info(dict(_gcp), scopes=_GS_SCOPES)
        return gspread.authorize(creds).open_by_key(_sheet_id)
    except Exception as _e:
        import logging as _logging
        _logging.error("Google Sheets 초기화 오류: %s", type(_e).__name__)
        return None

def get_gsheet():
    _wb = _get_gspread_workbook()
    return _wb.sheet1 if _wb is not None else None

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
    white-space: nowrap;      /* 금액 숫자 중간 줄바꿈(32,275,93/5원) 방지 */
}
/* 페이퍼 계좌 5컬럼: 8자리 원화가 안 깨지도록 반응형 축소 */
.pa-metric .value { font-size: clamp(12px, 1.15vw, 17px) !important; white-space: nowrap; }
.pa-metric .label { font-size: 10px !important; }
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
def _fetch_usd_krw_raw():
    """[C2] USD/KRW 순수 네트워크 페치 — session_state 접근 금지(캐시 함수 오염 차단).
    성공 시 float, 실패/비정상 시 None."""
    try:
        import yfinance as _yf_fx
        _h = _yf_fx.Ticker("USDKRW=X").history(period="5d")
        if _h is None or _h.empty or 'Close' not in _h.columns:
            return None
        _ser = _h['Close'].dropna()
        if _ser.empty:
            return None
        _val = float(_ser.iloc[-1])
        if not (_val == _val) or _val <= 0:        # NaN / 비정상 차단
            return None
        return _val
    except Exception:
        return None


def get_usd_krw():
    """USD/KRW 환율 — 캐시된 순수 페치 + session_state 기록/폴백은 호출측 레이어에서 처리.
    실패 시 마지막값(없으면 1350) 폴백. 절대 예외 전파 안 함."""
    _val = _fetch_usd_krw_raw()
    if isinstance(_val, (int, float)) and _val == _val and _val > 0:
        st.session_state['_last_usd_krw'] = _val
        return _val
    return st.session_state.get('_last_usd_krw', 1350.0)


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_current_fx_rate():
    """[D4] 환율 배너용 실시간 USD/KRW(KRW=X) — 60초 캐시. 리런마다 비캐시 yfinance
    무한호출로 인한 앱 지연·Rate-limit 병목 제거. 성공 시 float, 실패 시 None."""
    import logging as _lg
    try:
        import yfinance as _yf_fx2
        _h = _yf_fx2.Ticker("KRW=X").history(period="1d")
        if _h is None or _h.empty or 'Close' not in _h.columns:
            return None
        _v = float(_h['Close'].iloc[-1])
        return _v if (_v == _v and _v > 0) else None
    except Exception as _e:
        _lg.warning("_fetch_current_fx_rate 실패: %s: %s", type(_e).__name__, _e)
        return None


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_wti_oil_raw():
    """[C2] WTI 유가 순수 네트워크 페치 — session_state 접근 금지(캐시 함수 오염 차단).
    성공 시 float, 실패/비정상 시 None."""
    try:
        import yfinance as _yf_oil
        _h = _yf_oil.Ticker("CL=F").history(period="5d")
        if _h is None or _h.empty or 'Close' not in _h.columns:
            return None
        _ser = _h['Close'].dropna()
        if _ser.empty:
            return None
        _val = float(_ser.iloc[-1])
        if not (_val == _val) or _val <= 0:
            return None
        return _val
    except Exception:
        return None


def get_wti_oil():
    """WTI 유가($/배럴) — 캐시된 순수 페치 + session_state 기록/폴백은 호출측 레이어에서 처리.
    실패 시 마지막값(없으면 None) 폴백. 예외 전파 안 함."""
    _val = _fetch_wti_oil_raw()
    if isinstance(_val, (int, float)) and _val == _val and _val > 0:
        st.session_state['_last_wti'] = _val
        return _val
    return st.session_state.get('_last_wti', None)


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def whipsaw_override(intra_high, intra_low, close, prev_close, atr14_pct=None):
    """[V6.1 Phase2] 장중 휩쏘/Bull Trap 감지 — 종가 무관 즉시 위험전환.
    ⚠️ 배선 대기: KIS 고가(bstp_nmix_hgpr)/저가(lwpr) 정상 수신 확인 후 게이트에 연결.
    [②동적화] 고정 -4%/5% 상수 → ATR14 연동. 저변동장은 더 민감, 고변동장은 더 관대하게
    임계값이 시장 변동성에 맞춰 스스로 조정됨. ATR 결측 시에만 V6.1 고정값으로 폴백.
    반환: (triggered:bool, reason:str)."""
    try:
        if not all(isinstance(v, (int, float)) and v > 0 for v in (intra_high, intra_low, close, prev_close)):
            return False, ""
        # ── 동적 임계값: ATR14×배수 (하한을 둬 초저변동장 과민반응 차단) ──
        if isinstance(atr14_pct, (int, float)) and atr14_pct > 0:
            range_thr = max(3.5, atr14_pct * 2.5)   # 진폭 발작: ATR의 2.5배 (최소 3.5%)
            dd_thr    = -max(2.5, atr14_pct * 1.6)   # 급락 임계: ATR의 1.6배 (최소 -2.5%)
            _thr_note = f"ATR{atr14_pct:.1f}%연동"
        else:
            range_thr, dd_thr = 5.0, -4.0            # ATR 결측 → V6.1 고정 폴백
            _thr_note = "고정폴백"
        day_range = (intra_high - intra_low) / prev_close * 100      # 장중 진폭
        low_dd    = (intra_low  - prev_close) / prev_close * 100      # 장중 최저 낙폭
        close_chg = (close - prev_close) / prev_close * 100
        if day_range >= range_thr:
            return True, f"🚨 장중 진폭 {day_range:.1f}%≥{range_thr:.1f}%({_thr_note}) — 변동성 발작(위험전환)"
        if low_dd <= dd_thr and close_chg >= -1.0:
            return True, f"🚨 장중 {low_dd:.1f}%(임계 {dd_thr:.1f}%,{_thr_note}) 급락 후 {close_chg:+.1f}% 반등 — Bull Trap 의심(위험전환)"
        return False, ""
    except Exception:
        return False, ""


# ══════════════════════════════════════════════════════════════════
# [V10.3 P6/P7] 매크로 임계값 전역 상수 — FX·수급 판정을 단일 소스로 통일(파편화 제거)
# ══════════════════════════════════════════════════════════════════
FX_WARN            = 1450.0   # 경계: 사이드바/배너 경고 시작
FX_USD_HEDGE       = 1500.0   # 미국주식 환헷지 경고(원화 급등 시 신규진입 자제)
FX_DANGER          = 1520.0   # 위험: scale-in 차단·브리핑 위험 판정
FX_HARD_PANIC      = 1550.0   # 절대 하드블락 — MA밴드 상승과 무관하게 무조건 danger(적응형 무감각 차단)
FX_FALLBACK_ANCHOR = 1510.0   # MA90 표본 부족 시 fx_nonlinear_risk 폴백 앵커
FOREIGN_SELL_PANIC_KRW = -1_000_000_000_000   # 외국인 -1조 순매도 = 패닉셀(원 단위)


def fx_nonlinear_risk(krw, krw_series=None):
    """[V6.1 Phase2] 환율 비선형 발작 리스크 — MA90+Nσ 동적 소프트 앵커 + 절대 하드블락 하이브리드.
    ▸ 동적: 90일 이동평균+1.5σ 상단을 발작 경계로 삼아 뉴노멀(밴드 상향)을 자동 추종.
    ▸ 하이브리드: 밴드가 올라도 절대 FX_HARD_PANIC(1,550) 위는 무조건 danger → '적응형 무감각' 차단.
    ▸ 폴백: MA 표본(60개) 미확보 시 V6.1 검증 고정모델(1,510 하드) 그대로 사용.
    반환: (score:float, state:'safe'|'warn'|'danger', reason:str). 절대 예외 없음."""
    try:
        if not isinstance(krw, (int, float)) or krw != krw or krw <= 0:
            return 0.0, "unknown", ""
        # ── 동적 소프트 앵커 산출: MA90 + 1.5σ (표본 60+ 확보 시에만) ──
        _ma = _sd = _anchor = None
        if isinstance(krw_series, (list, tuple)) and len(krw_series) >= 60:
            import numpy as _np
            # 최근 90개만 슬라이스 → 시계열이 길어져도 연산량 O(90) 상한 고정(볼린저 유사연산 부하 차단)
            _arr = _np.fromiter((float(x) for x in krw_series[-90:]
                                 if isinstance(x, (int, float)) and x > 0), dtype=float)
            if _arr.size >= 60:
                _ma = float(_arr.mean()); _sd = float(_arr.std())
                _anchor = _ma + 1.5 * _sd            # 1.5σ 상단 = 동적 발작 경계

        if _anchor and _sd and _sd > 3:
            # [동적] 앵커·σ 기반 정규화 비선형 모델
            _warn_lo = _ma + 0.5 * _sd
            _unit    = _sd
            base  = _clamp((krw - _warn_lo) / max(_anchor - _warn_lo, 1e-9), 0.0, 1.0)
            over  = max(0.0, krw - _anchor)
            accel = (over / _unit) ** 1.6
            _dyn  = True
        else:
            # [폴백] V6.1 고정 모델 — 표본 부족 시 기존 검증 로직 유지(1,510 하드)
            _anchor = FX_FALLBACK_ANCHOR
            base  = _clamp((krw - 1480) / 40.0, 0.0, 1.0)       # 1,480~1,520 선형 warn
            over  = max(0.0, krw - 1500)
            accel = (over / 10.0) ** 1.6                        # 1500:0 · 1510:1.44 · 1520:2.9 · 1550:9.4
            _unit = 10.0
            _dyn  = False

        vel = 0.0
        if isinstance(krw_series, (list, tuple)) and len(krw_series) >= 3 and krw >= (_anchor - 10):
            _d2 = krw - float(krw_series[-3])                    # 최근 2거래일 상승폭
            if _d2 >= 12:
                vel = min(_d2 / 10.0, 3.0)                       # 이틀 +12원↑ = 외인 이탈 가속
        score = base + accel + vel

        # ── 하이브리드 판정: 절대 하드블락 최우선 → 동적앵커 초과 → score ──
        if krw >= FX_HARD_PANIC:
            state, _hard = "danger", True
        elif krw >= _anchor or score >= 2.0:
            state, _hard = "danger", False
        elif score >= 0.8:
            state, _hard = "warn", False
        else:
            state, _hard = "safe", False

        _mode = (f"동적앵커 {_anchor:,.0f}(MA90 {_ma:,.0f}+1.5σ)" if _dyn else f"고정앵커 {_anchor:,.0f}")
        if state == "danger":
            _r = f"환율 {krw:,.0f}원 " + (f"≥{FX_HARD_PANIC:,.0f} 절대패닉 하드블락"
                                        if _hard else f"발작({_mode}, 가속 {accel:.1f}·속도 {vel:.1f})")
        elif state == "warn":
            _r = f"환율 {krw:,.0f}원 경계({_mode})"
        else:
            _r = f"환율 {krw:,.0f}원 안정({_mode})"
        return score, state, _r
    except Exception:
        return 0.0, "unknown", ""


def compute_macro_regime_gate(krw=None, oil=None, foreign_net_krw=None, krw_series=None,
                              basis_info=None, intraday=None):
    """매크로 레짐 게이트 — 환율(비선형 발작)·유가·외국인수급·선물베이시스 종합 신호등.
    모든 입력 None/NaN 허용(부분판정). 절대 예외 없이 dict 반환.
    krw_series: 최근 거래일 환율 시계열(속도 항 계산용, 선택).
    basis_info: kis_futures_basis_proxy() 결과(①-b 선물수급 프록시, 선택).
    intraday:  [Phase2] 장중 고가/저가 dict {'high','low','close','prev_close','atr14_pct'}.
               hgpr/lwpr 실수신 오차 <0.1% 검증 후에만 주입 → 휩쏘 최우선 오버라이드 발동.
    반환: light('green'|'amber'|'red'), verdict, risk(int), krw/oil/flow 상태, reasons[]"""
    def _num(x):
        return isinstance(x, (int, float)) and (x == x)   # not None, not NaN

    # ── [Phase2] 휩쏘 최우선 오버라이드 — 장중 변동성 발작은 다른 지표 무관 즉시 red ──
    # dormant: intraday 미주입(None) 시 완전 무해. 오차율 검증 후 사이드바에서 dict 주입 시 발동.
    if isinstance(intraday, dict):
        _wtrig, _wreason = whipsaw_override(
            intraday.get('high'), intraday.get('low'),
            intraday.get('close'), intraday.get('prev_close'), intraday.get('atr14_pct'))
        if _wtrig:
            return {"light": "red", "verdict": "🔴 장중 변동성 발작 — 즉시 방어(휩쏘 오버라이드)",
                    "risk": 99, "krw": "unknown", "oil": "unknown", "flow": "unknown",
                    "reasons": [_wreason]}

    reasons, risk = [], 0

    krw_state = "unknown"
    if _num(krw) and krw > 0:
        # [Phase1] 비선형 발작 모델 — 1,500 초과 지수가속 + 속도 + 1,510 하드 격상
        _fx_score, krw_state, _fx_reason = fx_nonlinear_risk(krw, krw_series)
        risk += int(round(min(_fx_score, 4.0))) if krw_state != "danger" else max(2, int(round(min(_fx_score, 4.0))))
        if _fx_reason:
            reasons.append(_fx_reason)

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
        if foreign_net_krw <= FOREIGN_SELL_PANIC_KRW:      # -1조 이하 = 패닉셀
            risk += 2; flow_state = "danger"; reasons.append("외국인 -1조↑ 순매도 (패닉셀)")
        elif foreign_net_krw < 0:
            risk += 1; flow_state = "warn"; reasons.append("외국인 순매도 진행")
        else:
            flow_state = "safe"; reasons.append("외국인 순매수")

    # [V6.1 ①-b] 선물 베이시스 프록시 — 현물성 헷지 vs 완전 리스크오프 구별
    if isinstance(basis_info, dict) and basis_info.get('signal'):
        _sig = basis_info['signal']; _b = basis_info.get('basis', 0.0)
        if _sig == "riskoff":
            risk += 2
            if flow_state in ("unknown", "safe"):
                flow_state = "danger"
            reasons.append(f"🚨 선물 베이시스 {_b:+.2f}%p — 현·선물 동반투매(리스크오프 확증)")
        elif _sig == "hedge":
            risk = max(0, risk - 1)   # 현물성 하락 → 과잉 위험가산 1p 완화(추격 방지, 공포 억제)
            reasons.append(f"선물 베이시스 {_b:+.2f}%p 방어 — 현물성 하락(헷지 추정, 위험 1p 완화)")

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
    krw_ok  = _num(krw) and 0 < krw <= FX_DANGER
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


def _pg_won_price(tk, fallback):
    """연기금 표시가 정상화 — KIS 정확 원화가 우선(yfinance KRX 10배 왜곡 회피), 실패 시 폴백."""
    try:
        _kp = kis_get_price(str(tk))
        if _kp and _kp.get('현재가'):
            _v = float(_kp['현재가'])
            if _v > 0:
                return _v
    except Exception:
        pass
    try:
        return float(fallback)
    except Exception:
        return 0.0


def _kis_price_cached(code, ttl=60):
    """[C3] KIS 정확 원화가 — 60초 세션 캐시. 실패 시 0.
    ⚠️ 캐시 함수 '밖'에서만 호출(kis_get_token이 세션에 쓰므로). 반복 스캔의 KIS 호출량 억제용."""
    import time as _t
    try:
        _now = _t.time()
        _store = st.session_state.setdefault('_kis_px_cache', {})
        _e = _store.get(str(code))
        if _e and (_now - _e[1] < ttl):
            return _e[0]
        _v = _pg_won_price(code, 0)
        _store[str(code)] = (_v, _now)
        return _v
    except Exception:
        return 0.0


def _normalize_kr_etf_prices(rows):
    """[C3] 국내 ETF 표시가 yfinance KRX 10배 왜곡 보정 — KIS 정확가 대비 ~10배(또는 1/10)
    벌어지면 절대가격 필드(현재가/MA5가격/전일종가)만 KIS 기준으로 재정규화.
    지표(ADX/RSI/등락%/점수 등)는 비율기반이라 왜곡 무영향 → 손대지 않음.
    ⚠️ 반드시 @st.cache_data 함수 '밖'에서 호출. 캐시객체 변형 방지 위해 dict를 복사해 반환."""
    if not rows:
        return rows
    _out = []
    for _r in rows:
        _r = dict(_r)   # 캐시 원본 불변 — 사본에만 보정 적용
        try:
            _code = str(_r.get('코드', '')).strip()
            _yf_px = float(_r.get('현재가', 0) or 0)
            if _code.isdigit() and _yf_px > 0:
                _kis_px = _kis_price_cached(_code)
                if _kis_px and _kis_px > 0:
                    _ratio = _yf_px / _kis_px
                    if _ratio > 3.0 or _ratio < 0.34:      # ~10배(또는 1/10) 왜곡 구간
                        _corr = _kis_px / _yf_px
                        for _k in ('현재가', 'MA5가격', '전일종가'):
                            if isinstance(_r.get(_k), (int, float)) and _r[_k] > 0:
                                _r[_k] = round(_r[_k] * _corr, 2)
                        _r['_px_corrected'] = True
        except Exception:
            pass
        _out.append(_r)
    return _out


def render_pension_results(pg_df, streak_map, streak_locked, mode_label, top_n, n_results):
    """연기금 스캔 결과 표시 + 관심종목 버튼 — 세션 캐시 기반으로 스캔 없이도 렌더.
    ⚠️ 반드시 try/except 밖에서 호출 (버튼 st.rerun 예외가 삼켜지지 않도록)."""
    if pg_df is None or len(pg_df) == 0:
        return

    # 종합점수 내림차순 정렬 (레거시 표/버튼 폐기 → V9.13 TARGET LOCK-ON 카드 통일)
    _sorted = pg_df.sort_values('종합점수', ascending=False).reset_index(drop=True)
    if streak_locked:
        st.caption("🔒 오늘 스캔 기록 확정 (날짜 Lock — 재스캔해도 연속일 카운트 고정)")
    st.markdown(f"#### 🏛️ {mode_label} — 연기금 추종 TARGET")

    def _pg_parse_won(_s):
        try:
            return float(str(_s).replace(',', '').replace('원', '').strip())
        except Exception:
            return 0.0

    _rank_ic = ["🥇", "🥈", "🥉"]
    _top3 = _sorted.head(3)
    _tcols = st.columns(len(_top3)) if len(_top3) else []
    for _ti, (_, _row) in enumerate(_top3.iterrows()):
        _tk = str(_row['종목코드']); _nm = str(_row['종목명'])
        _score = _row.get('종합점수', 0)
        _rsi   = _row.get('RSI', '-')
        _cur   = _pg_parse_won(_row.get('현재가', 0))   # 이미 KIS 정상화된 값
        _cons  = _row.get('연기금연속(일)', _row.get('연속상승(일)', '-'))
        # 연속등장 배지
        _stk = int(streak_map.get(_tk, _row.get('연속등장(일)', 1)))
        if _stk >= 3:   _stk_txt, _stk_c = "🟢 3일 연속 (매수 검토)", "#22c55e"
        elif _stk == 2: _stk_txt, _stk_c = "🟡 2일 연속 (대기)",     "#fbbf24"
        else:           _stk_txt, _stk_c = "⚪ 1일차 신규 (보류)",    "#94a3b8"
        # 진입/손절 타점
        _p_ep = None
        try:
            _pdf = st.session_state.get('all_data_cache', {}).get(_tk, {}).get('df')
            if _pdf is None:
                _praw = fetch_ohlcv(_tk, 80)
                if _praw is not None and len(_praw) >= 20:
                    _pdf = calc_indicators(_praw)
            if _pdf is not None:
                _p_ep = calc_entry_point(_pdf, 'bounce')
        except Exception:
            _p_ep = None
        _ent_s = f"{_p_ep['entry']:,.0f}" if _p_ep and _p_ep.get('entry') else "-"
        _stp_s = f"{_p_ep['stoploss']:,.0f}" if _p_ep and _p_ep.get('stoploss') else "-"
        _gc = "#ffd166"
        with _tcols[_ti]:
            st.markdown(f"""
<div style='background:linear-gradient(160deg,#0f172a,#1a1a2e);border:2px solid {_gc}80;border-radius:14px;
padding:14px 16px;box-shadow:0 0 14px {_gc}25;margin-bottom:6px'>
  <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>
    <span style='font-size:15px;font-weight:900;color:{_gc}'>{_rank_ic[_ti]} 🏛️ 연기금</span>
    <span style='font-size:12px;font-weight:700;color:#fbbf24'>{_score}점</span>
  </div>
  <div style='display:inline-block;background:{_stk_c}22;border:1px solid {_stk_c}77;color:{_stk_c};
  font-size:11px;font-weight:800;padding:2px 10px;border-radius:10px;margin-bottom:6px'>연속등장 {_stk_txt}</div>
  <div style='font-size:15px;font-weight:800;color:#f0f4ff'>{_nm}</div>
  <div style='font-size:10px;color:#64748b;margin-bottom:8px'>{_tk} · {_cur:,.0f}원 · RSI {_rsi} · 연기금 {_cons}일</div>
  <div style='display:flex;justify-content:space-between;font-size:11px'>
    <span style='color:#fbbf24'>🎯 진입 {_ent_s}</span>
    <span style='color:#f43f5e'>🛑 손절 {_stp_s}</span>
  </div>
</div>""", unsafe_allow_html=True)
            if st.button("🔍 분석 탭으로 이동", key=f"pg_target_{_tk}", use_container_width=True):
                add_ticker(_tk, _nm)
                st.session_state['scanner_selection'] = _tk
                # 위젯 key 직접 수정 금지 → 사전선택은 pending 키로 전달(다음 run에서 적용)
                st.session_state['_pending_unified_sel'] = f"{_nm} ({_tk})"
                st.toast(f"🔍 {_nm} → 관심종목 추가 · 상단 '분석' 탭에서 확인", icon="🎯")

    # 4위 이하 → 서랍 (V9.13 스펙 통일)
    _rest = _sorted.iloc[3:]
    if not _rest.empty:
        with st.expander(f"📂 4위 이하 스캔 결과 · 전체 상세 (대기 종목 {len(_rest)}개)", expanded=False):
            st.caption("종합점수 = 연속일×10 + 순매수강도×2 + 외인쌍끌이 20점 (KRX모드) | "
                       "현재가는 KIS 정상화값(yfinance 10배 왜곡 보정)")
            _disp = ['연속등장(일)'] + [c for c in _sorted.columns if c != '연속등장(일)']
            st.dataframe(_rest[_disp], use_container_width=True, hide_index=True)


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


def _http_get_text(_url, _headers=None, _timeout=6):
    """requests 우선, 실패 시 urllib 폴백으로 텍스트 취득. 반환:(text|None, err|None)."""
    _headers = _headers or {"User-Agent": "Mozilla/5.0"}
    try:
        import requests as _rq
        _r = _rq.get(_url, headers=_headers, timeout=_timeout)
        return _r.text, (None if _r.status_code == 200 else f"HTTP {_r.status_code}")
    except Exception as _e1:
        try:
            import urllib.request as _ur
            _raw = _ur.urlopen(_ur.Request(_url, headers=_headers), timeout=_timeout).read()
            for _enc in ("euc-kr", "utf-8"):
                try:
                    return _raw.decode(_enc), None
                except Exception:
                    continue
            return _raw.decode("utf-8", "replace"), None
        except Exception as _e2:
            return None, f"{type(_e1).__name__}/{type(_e2).__name__}: {str(_e2)[:60]}"


def _parse_naver_investor_html(_html):
    """네이버 투자자 매매동향 HTML → 첫 데이터 행의 외국인 순매수(원). 실패 시 None."""
    import re as _re_fn
    _rows = _re_fn.findall(r"<tr[^>]*>(.*?)</tr>", _html, _re_fn.S)
    for _row in _rows:
        _cells = _re_fn.findall(r"<td[^>]*>(.*?)</td>", _row, _re_fn.S)
        _clean = [_re_fn.sub(r"<[^>]+>", "", _c).replace("&nbsp;", "").strip().replace(",", "")
                  for _c in _cells]
        if len(_clean) >= 4 and _re_fn.match(r"\d{2}[.\-/]\d{2}", _clean[0] or ""):
            _fn_txt = _clean[2]   # 날짜 | 개인 | 외국인 | 기관계 ...
            if _fn_txt and _fn_txt.lstrip("+-").replace(".", "").isdigit():
                return float(_fn_txt) * 100_000_000, _clean[0]
    return None, None


def _scrape_naver_foreign_net_kospi(_debug=None):
    """네이버 증권 투자자별 매매동향에서 코스피 외국인 순매수(원) 스크래핑.
    반환: float(원) 또는 None. _debug(list)에 단계별 진단 기록.
    브라우저 헤더(Referer 등) 필수 — 없으면 네이버가 스텁/차단 페이지를 반환."""
    def _log(m):
        if _debug is not None:
            _debug.append(m)
    # 네이버 봇 차단 회피용 풀 브라우저 헤더 (Referer 없으면 1~2KB 스텁 반환됨)
    _hdr = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Referer": "https://finance.naver.com/sise/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }
    # 후보 엔드포인트 순회 (구조 변경/차단 대비)
    _urls = [
        "https://finance.naver.com/sise/investorDealTrendDay.naver",
        "https://finance.naver.com/sise/sise_deal_trend_day.naver",
    ]
    for _url in _urls:
        _html, _err = _http_get_text(_url, _hdr)
        if not _html:
            _log(f"네이버[{_url.rsplit('/',1)[-1]}]: 연결 실패 ({_err})")
            continue
        _log(f"네이버[{_url.rsplit('/',1)[-1]}]: HTML {len(_html)}바이트 수신")
        try:
            _val, _basis = _parse_naver_investor_html(_html)
            if _val is not None:
                _log(f"네이버: 파싱 성공 (외국인 {_val/1e8:+,.0f}억원, 기준 {_basis})")
                return _val
            # 파싱 실패 + 응답이 작으면(스텁/차단) 내용 일부를 진단에 노출
            if len(_html) < 3000:
                _snip = _re_import_sub(_html)[:120]
                _log(f"네이버: 스텁/차단 의심 — 응답 미리보기: {_snip}")
            else:
                _log("네이버: HTML은 받았으나 데이터 행 파싱 실패(구조 변경 가능)")
        except Exception as _e:
            _log(f"네이버: 파싱 예외 {type(_e).__name__}")
    return None


def _re_import_sub(_html):
    """HTML 태그 제거한 순수 텍스트 미리보기 (진단용)."""
    import re as _re_p
    return _re_p.sub(r"\s+", " ", _re_p.sub(r"<[^>]+>", " ", _html)).strip()


def _deep_find_foreign_amount(_obj, _depth=0):
    """네이버 JSON 응답에서 외국인 순매수 금액 키를 재귀 탐색 (스키마 변동 내성).
    후보 키: foreignerNetBuyAmount / foreignerPureBuyQuant / frgn* 등."""
    if _depth > 6:
        return None
    _KEYS = ("foreignerNetBuyAmount", "foreignerPureBuyAmount", "foreignerNetBuy",
             "frgnNetBuyAmt", "foreigner")
    if isinstance(_obj, dict):
        for _k, _val in _obj.items():
            if _k in _KEYS and isinstance(_val, (int, float, str)):
                try:
                    return float(str(_val).replace(",", ""))
                except ValueError:
                    pass
        for _val in _obj.values():
            _r = _deep_find_foreign_amount(_val, _depth + 1)
            if _r is not None:
                return _r
    elif isinstance(_obj, list):
        for _it in _obj:
            _r = _deep_find_foreign_amount(_it, _depth + 1)
            if _r is not None:
                return _r
    return None


def _fetch_naver_polling_api(_debug=None):
    """[대안 1] 네이버 페이 증권 내부 실시간 JSON API — HTML 스크래핑보다 차단에 유연.
    브라우저 User-Agent + Referer 필수. 반환: 원 단위 float 또는 None."""
    def _log(m):
        if _debug is not None:
            _debug.append(m)
    _hdr = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://finance.naver.com/sise/sise_deal_trend.naver",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    # 후보 JSON 엔드포인트 순회 (스키마/경로 변동 대비)
    _urls = [
        "https://polling.finance.naver.com/api/realtime/domestic/investor/trend",
        "https://m.stock.naver.com/api/stocks/investor/trend?category=kospi",
    ]
    for _url in _urls:
        _short = _url.split("/api/")[-1][:40]
        try:
            import requests as _rq
            _resp = _rq.get(_url, headers=_hdr, timeout=5)
            if _resp.status_code != 200:
                _log(f"네이버JSON[{_short}]: HTTP {_resp.status_code}")
                continue
            try:
                _data = _resp.json()
            except ValueError:
                _log(f"네이버JSON[{_short}]: JSON 아님 ({len(_resp.text)}바이트)")
                continue
            # 1순위: 지시서 경로 (result.kospi.foreignerNetBuyAmount)
            _kospi = (_data.get('result', {}) or {}).get('kospi', {}) if isinstance(_data, dict) else {}
            _v = _kospi.get('foreignerNetBuyAmount') if isinstance(_kospi, dict) else None
            if _v is None:
                # 2순위: 재귀 키 탐색 (스키마 변동 대응)
                _v = _deep_find_foreign_amount(_data)
            if _v is not None:
                _won = float(str(_v).replace(",", ""))
                # 네이버 투자자 동향 금액 단위는 억원 → |값|<100만이면 억원으로 간주해 환산
                if abs(_won) < 1_000_000:
                    _won *= 100_000_000
                _log(f"네이버JSON[{_short}]: 성공 ({_won/1e8:+,.0f}억원)")
                return _won
            _log(f"네이버JSON[{_short}]: 응답 수신했으나 외국인 금액 키 없음")
        except Exception as _e:
            _log(f"네이버JSON[{_short}]: {type(_e).__name__}")
    return None


def _fetch_fdr_foreign_net(_debug=None):
    """[대안 2] FinanceDataReader 우회 수집 — KRX 마켓 스냅샷에서 외국인 순매수.
    FDR 버전에 따라 API가 다르므로 다단 시도. 반환: 원 단위 float 또는 None."""
    def _log(m):
        if _debug is not None:
            _debug.append(m)
    try:
        import FinanceDataReader as _fdr_fn
    except ImportError:
        _log("FDR: 라이브러리 미설치")
        return None
    _today = (datetime.utcnow() + timedelta(hours=9)).strftime('%Y%m%d')
    # 시도 1: SnapShot (지시서 경로 — 일부 FDR 버전에만 존재)
    try:
        if hasattr(_fdr_fn, 'SnapShot'):
            _df = _fdr_fn.SnapShot(_today, market='KOSPI')
            if _df is not None and '외국인' in getattr(_df, 'index', []):
                _col = '순매수' if '순매수' in _df.columns else _df.columns[-1]
                _v = float(_df.loc['외국인', _col])
                if _v == _v:
                    if abs(_v) < 1_000_000:   # 억원 단위 보정
                        _v *= 100_000_000
                    _log(f"FDR SnapShot: 성공 ({_v/1e8:+,.0f}억원)")
                    return _v
            _log("FDR SnapShot: 응답에 외국인 행 없음")
        else:
            _log("FDR: SnapShot API 없음(버전 미지원) — 투자자 API 시도")
    except Exception as _e:
        _log(f"FDR SnapShot: {type(_e).__name__}")
    # 시도 2: StockListing/DataReader 계열 투자자 데이터 (버전별 상이)
    try:
        if hasattr(_fdr_fn, 'InvestorTrading'):
            _df2 = _fdr_fn.InvestorTrading('KOSPI', _today)
            if _df2 is not None and not _df2.empty and '외국인' in _df2.index:
                _v2 = float(_df2.loc['외국인'].iloc[-1])
                if _v2 == _v2:
                    if abs(_v2) < 1_000_000:
                        _v2 *= 100_000_000
                    _log(f"FDR InvestorTrading: 성공 ({_v2/1e8:+,.0f}억원)")
                    return _v2
    except Exception as _e:
        _log(f"FDR InvestorTrading: {type(_e).__name__}")
    return None


def _fetch_naver_cloudscraper(_debug=None):
    """[28차 WAF 우회] cloudscraper로 네이버 투자자별 매매동향 페이지 취득 →
    pandas.read_html 테이블 파싱. 일반 requests가 403/에러HTML로 막힐 때의 주력 경로.
    반환: 원 단위 float 또는 None."""
    def _log(m):
        if _debug is not None:
            _debug.append(m)
    try:
        import cloudscraper as _cs
    except ImportError:
        _log("cloudscraper: 라이브러리 미설치 (requirements.txt 배포 후 재시도)")
        return None
    _urls = [
        "https://finance.naver.com/sise/sise_trans_style.naver",   # 투자자별 매매동향 종합
        "https://finance.naver.com/sise/investorDealTrendDay.naver",
    ]
    try:
        _scraper = _cs.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})
    except Exception as _e:
        _log(f"cloudscraper: 스크래퍼 생성 실패 {type(_e).__name__}")
        return None
    for _url in _urls:
        _short = _url.rsplit('/', 1)[-1]
        try:
            _resp = _scraper.get(_url, timeout=8)
            if _resp.status_code != 200:
                _log(f"cloudscraper[{_short}]: HTTP {_resp.status_code}")
                continue
            _html = _resp.text
            if len(_html) < 3000:
                _log(f"cloudscraper[{_short}]: 스텁 응답 {len(_html)}바이트 — "
                     f"{_re_import_sub(_html)[:80]}")
                continue
            _log(f"cloudscraper[{_short}]: HTML {len(_html)}바이트 수신")
            # pandas read_html 테이블 파싱 (외국인 컬럼/행 유연 탐색)
            try:
                import io as _io_cs
                _tables = pd.read_html(_io_cs.StringIO(_html))
            except Exception as _pe:
                _log(f"cloudscraper[{_short}]: read_html 실패 {type(_pe).__name__}")
                # 폴백: 기존 정규식 파서 재활용
                _v_rx, _basis = _parse_naver_investor_html(_html)
                if _v_rx is not None:
                    _log(f"cloudscraper[{_short}]: 정규식 파싱 성공 ({_v_rx/1e8:+,.0f}억원)")
                    return _v_rx
                continue
            for _ti, _tb in enumerate(_tables):
                if _tb is None or _tb.empty:
                    continue
                # (a) '외국인' 컬럼이 있는 시계열 테이블 → 첫 데이터 행
                _cols = [str(c) for c in _tb.columns]
                _fcol = next((c for c in _tb.columns if '외국인' in str(c)), None)
                if _fcol is not None:
                    _ser = pd.to_numeric(
                        _tb[_fcol].astype(str).str.replace(',', '', regex=False),
                        errors='coerce').dropna()
                    if not _ser.empty:
                        _v = float(_ser.iloc[0])
                        if abs(_v) < 1_000_000:   # 억원/백만원 단위 보정
                            _v *= 100_000_000
                        _log(f"cloudscraper: 테이블{_ti} '외국인' 컬럼 파싱 성공 ({_v/1e8:+,.0f}억원)")
                        return _v
                # (b) 행 인덱스/첫 컬럼에 '외국인'이 있는 요약 테이블
                _first = _tb.iloc[:, 0].astype(str)
                _hit = _tb[_first.str.contains('외국인', na=False)]
                if not _hit.empty:
                    _nums = pd.to_numeric(
                        _hit.iloc[0].astype(str).str.replace(',', '', regex=False),
                        errors='coerce').dropna()
                    if not _nums.empty:
                        _v = float(_nums.iloc[-1])   # 마지막 숫자 컬럼 = 순매수
                        if abs(_v) < 1_000_000:
                            _v *= 100_000_000
                        _log(f"cloudscraper: 테이블{_ti} '외국인' 행 파싱 성공 ({_v/1e8:+,.0f}억원)")
                        return _v
            _log(f"cloudscraper[{_short}]: 테이블 {len(_tables)}개 수신했으나 외국인 데이터 없음")
        except Exception as _e:
            _log(f"cloudscraper[{_short}]: {type(_e).__name__} {str(_e)[:60]}")
    return None


def fetch_foreign_net_buying():
    """실시간 코스피 외국인 순매수(원) — 28차 철벽 우회 엔진.
    ⓪ KIS 공식 API (키 입력 시 스크래핑 대신 무조건 우선 — 창과 방패 싸움 회피)
    ① cloudscraper WAF 우회 → ② 네이버 JSON API → ③ FinanceDataReader
    → ④ pykrx → ⑤ 네이버 HTML(레거시).
    반환: (value_krw|None, source:str, diagnostics:list[str])."""
    _diag = []
    # ⓪ KIS 공식 API — 키가 설정돼 있으면 스크래핑 생략하고 무조건 공식 경로
    if kis_available():
        _diag.append("KIS: 키 감지 → 공식 API 우선 조회")
        try:
            _kis0, _hit0 = get_foreign_net_kospi_kis_estimate()
        except Exception as _e:
            _kis0, _hit0 = None, 0
            _diag.append(f"KIS: 예외 {type(_e).__name__}")
        if _kis0 is not None:
            return _kis0, f"KIS 공식 API (대형주 {_hit0}종목 합산)", _diag + ["KIS: 성공"]
        _diag.append("KIS: 키는 있으나 응답 없음 → 스크래핑 폴백")
    # ① cloudscraper WAF 우회 (requests 403 차단 대응 주력)
    _v = _fetch_naver_cloudscraper(_diag)
    if _v is not None:
        return _v, "네이버(cloudscraper WAF 우회)", _diag
    # ② 네이버 실시간 JSON API
    _v = _fetch_naver_polling_api(_diag)
    if _v is not None:
        return _v, "네이버 실시간 JSON API", _diag
    # ② FinanceDataReader 우회 수집
    _v = _fetch_fdr_foreign_net(_diag)
    if _v is not None:
        return _v, "FinanceDataReader(KRX)", _diag
    # ③ pykrx (KRX 공식 — 클라우드 IP·로그인 요구 시 차단 가능)
    try:
        _v = get_foreign_net_kospi()
    except Exception as _e:
        _v = None; _diag.append(f"pykrx: 예외 {type(_e).__name__}")
    if _v is not None:
        return _v, "pykrx(KRX)", _diag + ["pykrx: 성공"]
    _diag.append("pykrx: 무응답(데이터 없음·KRX 차단·로그인 요구 가능)")
    # ④ 네이버 HTML 스크래핑 (레거시 폴백)
    _v = _scrape_naver_foreign_net_kospi(_diag)
    if _v is not None:
        return _v, "네이버 증권(HTML)", _diag
    # ⑤ KIS 대형주 추정
    try:
        _kis, _hit = get_foreign_net_kospi_kis_estimate()
    except Exception as _e:
        _kis, _hit = None, 0; _diag.append(f"KIS: 예외 {type(_e).__name__}")
    if _kis is not None:
        return _kis, f"KIS 추정(대형주 {_hit}종목)", _diag + ["KIS: 성공"]
    _diag.append("KIS: 무응답(API 키 미설정 또는 응답 없음)")
    return None, "조회 실패", _diag


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
        elif krw <= FX_DANGER:
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
        elif foreign_net_krw <= FOREIGN_SELL_PANIC_KRW:
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

    return {"lines": [l1, l2, l3], "verdict": verdict, "light": light,
            "states": {"krw": s1, "flow": s2, "score": s3},
            "score_val": _score}

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

def _clear_macro_caches():
    """사이드바·헤더 매크로 데이터 전체 캐시 초기화 — 킬스위치/환율/유가/지수/수출 동기화.
    메인 [🔄 지수 갱신]·[🔄 새로고침]·사이드바 [🔄 실시간 동기화]가 공통 호출."""
    for _mfn in (_get_index_quotes_impl, check_index_shutdown, check_index_shutdown_us,
                 get_usd_krw, get_wti_oil, fetch_motie_exports):
        try:
            _mfn.clear()
        except Exception:
            pass

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
        # [Phase2] 환율 일별 시계열 기록(속도항 + MA90 소프트앵커용) — 거래일당 1샘플, 최근 95일 유지
        if isinstance(_sb_krw, (int, float)) and _sb_krw > 0:
            from datetime import date as _date_fx
            _fx_store = st.session_state.setdefault('_krw_daily', {'dates': [], 'vals': {}})
            _tfx = str(_date_fx.today())
            _fx_store['vals'][_tfx] = float(_sb_krw)
            if _tfx not in _fx_store['dates']:
                _fx_store['dates'].append(_tfx)
            _fx_store['dates'] = _fx_store['dates'][-95:]
            _fx_store['vals'] = {d: _fx_store['vals'][d] for d in _fx_store['dates']}
            _krw_series = [_fx_store['vals'][d] for d in _fx_store['dates']]
        else:
            _krw_series = None
        # [V6.1 ①-b] 선물 수급 베이시스 프록시 — 국장에서만, 실패 무해(None→게이트 무시)
        _sb_basis = None
        if '미장' not in str(st.session_state.get('etf_market_sel', '🇰🇷 국장 ETF')):
            try:
                _sb_basis = kis_futures_basis_proxy()
            except Exception:
                _sb_basis = None
        _sb_gate  = compute_macro_regime_gate(_sb_krw, _sb_oil, _sb_flow,
                                              krw_series=_krw_series, basis_info=_sb_basis)

        # ── 시장별(국장/미장) 독립 지수 킬스위치 — 메인 라디오(etf_market_sel)와 동기화 ──
        _sb_mkt    = st.session_state.get('etf_market_sel', '🇰🇷 국장 ETF')
        _sb_is_us  = ('미장' in str(_sb_mkt))
        if _sb_is_us:
            _idx_sd, _idx_msg, _idx_a, _idx_b = check_index_shutdown_us()
        else:
            _idx_sd, _idx_msg, _idx_a, _idx_b = check_index_shutdown()
        _sb_black = _sb_black or _idx_sd   # 지수 급락도 블랙아웃으로 승격
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
            f"padding:10px 12px;margin-bottom:14px;text-align:center'>"
            f"<div style='font-size:26px;line-height:1'>{_sbi}</div>"
            f"<div style='font-size:17px;font-weight:900;color:{_sbc};margin-top:2px'>{_sbt}</div>"
            f"</div>", unsafe_allow_html=True)
        # 상태 박스 ↔ 동기화 버튼 사이 안전 여백 확보 (겹침 방지)
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        # 🔄 실시간 동기화 미니 버튼 — 매크로 캐시만 즉시 비우고 사이드바 최신화
        def _sb_macro_sync():
            _clear_macro_caches()
            st.session_state['_macro_sync_ts'] = (datetime.utcnow() + timedelta(hours=9)).strftime('%H:%M:%S')
            st.toast("🔄 매크로 동기화 완료 — 최신 시세로 갱신", icon="✅")
        st.button("🔄 실시간 동기화", key="sb_macro_sync", use_container_width=True,
                  on_click=_sb_macro_sync,
                  help="킬스위치·환율·유가·지수 캐시를 즉시 비우고 최신값으로 갱신")
        _sync_ts = st.session_state.get('_macro_sync_ts')
        if _sync_ts:
            st.caption(f"🕒 마지막 동기화: {_sync_ts} KST · 장마감 시 값이 동일할 수 있음")
        # 시장별 지수 킬스위치 경고 (미장 선택 시 S&P500/나스닥, 국장 시 코스피/코스닥)
        _mkt_tag = "🇺🇸 미장" if _sb_is_us else "🇰🇷 국장"
        if _idx_sd and _idx_msg:
            st.error(f"[{_mkt_tag} 지수 킬스위치] {_idx_msg}")
        if _sb_black and not _idx_sd:
            _al = _sbv.get('alerts', ['이벤트 48시간 이내'])
            st.error(f"🚨 매크로 블랙아웃: {_al[0] if _al else '이벤트 임박'}")
        # 핵심 수치 — 좁은 사이드바에서 2컬럼 대신 수직 배열(위아래로 넓게)
        st.metric("💱 원/달러 환율", f"{_sb_krw:,.0f}원" if isinstance(_sb_krw,(int,float)) else "—",
                  delta=("⚠️ 1,450 경계" if isinstance(_sb_krw,(int,float)) and _sb_krw>=FX_WARN else "안정"),
                  delta_color="inverse")
        # (🌍 외국인 수급 지표 제거 — 스크래핑/KRX 차단으로 폐기. 게이트는 None 방어)
        # WTI 유가 · 반도체 수출 YoY — 메인 매크로 카드행에서 사이드바로 이관(2열 압축)
        _sb_oil2 = get_wti_oil()
        _sb_me   = fetch_motie_exports()
        _sb_yoy  = _sb_me.get("semi_yoy") if _sb_me else None
        _sbm1, _sbm2 = st.columns(2)
        _sbm1.metric("🛢️ WTI", f"${_sb_oil2:.1f}" if isinstance(_sb_oil2,(int,float)) else "—")
        _sbm2.metric("💾 반도체 YoY", f"{_sb_yoy:+.1f}%" if isinstance(_sb_yoy,(int,float)) else "대기")
        # 🛰️ 수급 펌프 추적기 — 반도체 대장주 외인·기관 매집 감시 (KIS 연결 시, 토큰은 캐시 밖 발급)
        try:
            if kis_available():
                _t2_tok = kis_get_token()
                if _t2_tok:
                    render_top2_supply_widget(fetch_top2_supply(_t2_tok))
        except Exception as _t2e:
            import logging as _lg2
            _lg2.warning("수급 펌프 추적기 렌더 실패: %s: %s", type(_t2e).__name__, _t2e)
    except Exception:
        st.caption("⚠️ 상태 패널 일시 비활성 (데이터 지연)")

    st.markdown("---")
    st.markdown("## ⚙️ 설정")

    # ── 🔑 KIS API 연동 (secrets 키 있으면 입력창 숨김 · 없을 때만 런타임 입력) ──
    #    별칭·섹션까지 탐색하는 _kis_key()/_kis_secret()로 감지 (이름 조금 달라도 잡음)
    _sec_kis_key    = _kis_key()
    _sec_kis_secret = _kis_secret()

    if _sec_kis_key and _sec_kis_secret:
        # secrets 키로 자동 연동 → 입력창 불필요, 회색 한 줄만
        st.caption("🔑 KIS API — secrets 키로 자동 연동됨")
    else:
        # secrets 미등록 시에만 런타임 입력 폼 (미연동은 무음, 에러는 테스트 실패 시만)
        if st.session_state.get('_kis_conn_ok'):
            with st.expander("✅ KIS API 연동 완료", expanded=False):
                if st.button("🔌 연결 해제 / 키 변경", key="kis_disconnect", use_container_width=True):
                    st.session_state['_kis_conn_ok'] = False
                    st.rerun()
        else:
            with st.expander("🔑 KIS API 키 입력 (선택 — secrets 미사용 시)", expanded=False):
                st.caption("secrets에 KIS_APP_KEY/SECRET을 넣으면 이 입력창은 자동으로 사라집니다.")
                # 진단 — secrets에 실제로 어떤 항목이 있는지(값 아닌 '이름'만) 표시
                _sk = _secret_top_keys()
                if _sk:
                    st.caption("🗝️ secrets 감지 항목: " + ", ".join(_sk))
                    st.caption("↑ 여기에 KIS_APP_KEY/KIS_APP_SECRET이 안 보이면 이름이 다르거나 "
                               "[섹션] 아래 있는 것 — 최상단으로 옮기거나 이름을 맞춰주세요.")
                else:
                    st.caption("⚠️ secrets가 비어있음 — Manage app → Settings → Secrets에 등록 필요")
                st.text_input("KIS App Key", type="password", key="_kis_app_key_input")
                st.text_input("KIS App Secret", type="password", key="_kis_app_secret_input")
                st.toggle("모의투자 키 사용 (VTS)", key="_kis_mock_input",
                          help="모의투자용 키는 도메인이 달라(29443) 이 토글을 켜야 토큰이 발급됩니다")
                if st.button("🧪 KIS 연결 테스트", key="kis_conn_test", use_container_width=True):
                    if not (_kis_key() and _kis_secret()):
                        st.info("App Key와 Secret을 먼저 입력하세요.")
                    else:
                        with st.spinner("KIS 토큰 발급 테스트 중..."):
                            _tok_t = kis_get_token()
                        if _tok_t:
                            st.session_state['_kis_conn_ok'] = True
                            st.rerun()
                        else:
                            _terr = st.session_state.get('_kis_token_err', '원인 미상')
                            st.error(f"❌ 연결 실패 — {_terr}")
                            if 'EGW00133' in str(_terr):
                                st.caption("⏳ KIS 토큰은 1분당 1회만 발급 — 60초 후 재시도하세요.")
                            elif 'EGW00201' in str(_terr) or 'appkey' in str(_terr).lower():
                                st.caption("🔑 키 값 오류 — 공백/따옴표 확인. 모의투자 키면 VTS 토글 ON.")

    # (🔄 외인 수급 스크래핑 블록 전체 폐기 — 버튼/경고/진단/비상입력 제거.
    #  외인 수급은 매크로 게이트·5AI 브리핑에서 None으로 안전하게 무시됨.)

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

    # (🔑 Gemini 키·모델·강제 새로고침은 하단 '🛠️ 시스템 백엔드' Expander로 이동)

    # (📋 관심 종목 헤더는 아래 Expander 제목으로 대체 — 사이드바 슬림화)

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

    # 관심 종목 리스트 — Expander로 접어 사이드바 슬림화 (평소 한 줄만 노출)
    with st.expander(f"📋 내 관심 종목 (Watchlist) · {len(_sb_pairs)}개", expanded=False):
        if not _sb_pairs:
            st.caption("등록된 관심 종목이 없습니다. 아래에서 추가하세요.")
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

    # ── 🛠️ 시스템 백엔드 및 API 설정 (평소 숨김 — Gemini 키·모델·강제 새로고침 격리) ──
    with st.expander("🛠️ 시스템 백엔드 및 API 설정", expanded=False):
        gemini_key = st.text_input("🔑 Gemini API 키", type="password",
                                    help="aistudio.google.com에서 발급")
        model_name = st.selectbox("Gemini 모델", [
            "models/gemini-2.5-flash",
            "models/gemini-2.5-pro",
            "models/gemini-2.0-flash",
        ], help="Flash: 빠름·하루 500회 무료 / Pro: 정밀분석·하루 25회 무료")
        st.caption(f"마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}")
        if st.button("🔄 강제 새로고침", use_container_width=True):
            st.cache_data.clear()
            st.success("캐시 초기화 완료!")
            import time; time.sleep(0.5)
            st.rerun()

    # 📌 보안 규칙 — 세로 줄글 → 가로 캡슐 배지 한 줄 (시각 부피 50%↓)
    st.markdown(
        "<div style='display:flex;flex-wrap:wrap;gap:4px;margin-top:6px'>"
        + "".join(
            f"<span style='background:rgba(100,116,139,0.15);color:#94a3b8;"
            f"font-size:9px;padding:2px 7px;border-radius:10px;"
            f"border:1px solid rgba(100,116,139,0.3)'>{_r}</span>"
            for _r in ["R:R≥2.0", "손절 -7%", "09:00~09:30 금지", "물타기 금지", "현금 20%"]
        )
        + "</div>",
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
    <span style='font-size:12px; color:#64748b; font-family:"IBM Plex Mono",monospace'>V9.2</span>
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
/* V9.11 롤백: 과도한 압축(0.38rem)으로 헤더↔5AI 배지 겹침 → 정상 간격 복원 */
div[data-testid="stVerticalBlock"] { gap:0.8rem; }
div[data-testid="stMetric"] { padding:8px 13px; }
div[data-testid="stExpander"] { margin-bottom:0.5rem; }
</style>""", unsafe_allow_html=True)

    # ── 상단 상태 바 (지수 배지와 세로 중앙 정렬) ──
    try:
        _sb_cols = st.columns([3, 1, 1, 1, 1], vertical_alignment="center")
    except TypeError:
        _sb_cols = st.columns([3, 1, 1, 1, 1])   # 구버전 폴백
    # H2(##) 대신 여백 없는 인라인 타이틀 → 배지와 같은 수평선 정렬
    _sb_cols[0].markdown(
        "<div style='font-size:23px;font-weight:900;color:#f0f4ff;line-height:1.2;margin:0'>"
        "🎯 V9.2 <span style='background:linear-gradient(90deg,#4da6ff,#a78bfa);"
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
        else:
            # 은폐 금지 — 과거값 대신 명시적 수신 실패 표기
            _sb_cols[2+_i_sb].markdown(
                f"<div style='font-size:11px;color:#64748b'>{_nm_sb}</div>"
                f"<div style='font-size:12px;font-weight:700;color:#f59e0b'>⚠️ 수신 불가</div>",
                unsafe_allow_html=True)

    # ── 지수 새로고침 (버튼·시간 텍스트 세로 중앙 정렬 → 가로 라인 정돈) ──
    try:
        _rf1, _rf2 = st.columns([1, 6], vertical_alignment="center")
    except TypeError:
        _rf1, _rf2 = st.columns([1, 6])   # 구버전 폴백
    if _rf1.button("🔄 지수 갱신", key="refresh_index", use_container_width=True):
        _clear_macro_caches()             # 사이드바 킬스위치/환율/유가/지수/수출 캐시 동시 초기화
        st.rerun()
    _rf2.caption(f"🕒 현재: {st.session_state.get('_now_kst', datetime.utcnow()+timedelta(hours=9)).strftime('%H:%M:%S')} KST "
                 f"· 자동 캐시 60초 (실시간 반영하려면 🔄)")

    # ── 🔬 KIS 지수 API 진단 (코스피/코스닥 '수신 불가' 원인 특정용) ──
    if not _mkt_home.get("코스피") or not _mkt_home.get("코스닥"):
        with st.expander("🔬 지수 API 진단 (코스피/코스닥 수신 불가 원인 보기)", expanded=False):
            st.caption("KIS 국내 업종/지수 API가 실제로 어떤 응답을 주는지 확인합니다.")
            if st.button("▶ KIS 지수 API 테스트 실행", key="kis_idx_probe_btn"):
                _pr_kospi  = _kis_index_probe("0001")
                _pr_kosdaq = _kis_index_probe("1001")
                st.write("**코스피(0001):**", _pr_kospi)
                st.write("**코스닥(1001):**", _pr_kosdaq)
                _msg = str(_pr_kospi.get("msg1", "")) + str(_pr_kospi.get("사유", "")) + str(_pr_kospi.get("예외", ""))
                if _pr_kospi.get("rt_cd") == "0":
                    st.success("✅ API 정상 응답 — 위 현재지수/등락률이 보이면 파싱만 맞추면 됩니다.")
                elif "모의" in _msg or "권한" in _msg or "없" in _msg:
                    st.error("⛔ 계정 권한/모드 문제 — KIS Developers에서 '국내주식 시세' 신청 또는 실전/모의 키 확인 필요")
                else:
                    st.warning("⚠️ 위 rt_cd·msg1(또는 예외) 내용을 그대로 복사해 알려주시면 정확히 고칩니다.")

    # (전략 방향 · 블랙아웃 경고 · 수동 입력은 모두 사이드바 Sticky 패널로 이전 —
    #  본문은 데이터 모니터링에만 집중. 여기서는 아무것도 렌더링하지 않음.)

    # (🌐 외국인 수급 자동 연동 폐기 — KRX/네이버 차단으로 신뢰 불가 → 사용 중단.
    #  _foreign_net_krw는 None 유지, 매크로 게이트·5AI 브리핑이 방어적으로 무시함.)

    # ══════════════════════════════════════════════════════════════════════
    # 🤖 5AI Top-Down 레짐 브리핑 패널 (오늘의 AI 코멘트 — 3줄 요약)
    # ══════════════════════════════════════════════════════════════════════
    try:
        _ai_krw  = get_usd_krw()
        _ai_flow = st.session_state.get('_foreign_net_krw', None)
        try:
            _ai_tops = _normalize_kr_etf_prices(_get_home_etf_top(1))   # [C3] 10배 왜곡 보정(캐시 밖)
            _ai_top1 = _ai_tops[0] if _ai_tops else None
        except Exception:
            _ai_top1 = None
        _brief = generate_ai_briefing(_ai_krw, _ai_flow, _ai_top1)
        _st = _brief.get("states", {})
        # 변동성(VIX) · 레짐(종합 신호등) 배지 상태 산출
        _vix = _mkt_home.get("VIX", {}).get("현재") if isinstance(_mkt_home, dict) else None
        _vix_s = (1 if _vix < 20 else 0) if isinstance(_vix, (int, float)) else -1
        _regime_s = {"green": 1, "red": 0, "amber": -1}.get(_brief["light"], -1)

        def _pill(_label, _s, _extra=""):
            _icon, _txt, _c = {
                1:  ("🟢", "PASS", "#16a34a"),
                0:  ("🔴", "WAIT", "#ef4444"),
                -1: ("⚪", "N/A",  "#64748b"),
            }.get(_s, ("⚪", "N/A", "#64748b"))
            return (f"<div style='background:{_c}14;border:1px solid {_c}55;border-radius:10px;"
                    f"padding:6px 4px;text-align:center'>"
                    f"<div style='font-size:10px;color:#94a3b8'>{_label}</div>"
                    f"<div style='font-size:13px;font-weight:800;color:{_c}'>{_icon} {_txt}</div>"
                    f"<div style='font-size:9px;color:#64748b'>{_extra}</div></div>")

        _sv = _brief.get("score_val")
        # 헤더(지수 갱신·시간) ↔ 5AI 배지 사이 세로 여백 확보 (겹침 방지)
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        _bcols = st.columns(5)
        _bcols[0].markdown(_pill("환율", _st.get("krw", -1),
                                 f"{_ai_krw:,.0f}" if isinstance(_ai_krw,(int,float)) else "—"), unsafe_allow_html=True)
        _bcols[1].markdown(_pill("외인수급", _st.get("flow", -1), "미사용"), unsafe_allow_html=True)
        _bcols[2].markdown(_pill("스코어", _st.get("score", -1),
                                 f"{int(_sv)}점" if isinstance(_sv,(int,float)) else "—"), unsafe_allow_html=True)
        _bcols[3].markdown(_pill("변동성", _vix_s,
                                 f"VIX {_vix:.0f}" if isinstance(_vix,(int,float)) else "—"), unsafe_allow_html=True)
        _bcols[4].markdown(_pill("레짐", _regime_s, _brief["light"].upper()), unsafe_allow_html=True)

        # 배지 행 ↔ 상세 가이드 Expander 사이 여백 확보 (겹침 방지)
        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
        # 상세 줄글 설명은 Expander로 격리 (평소 접힘)
        with st.expander(f"🔎 5AI 분석 상세 가이드 및 기각 사유 보기 — {_brief['verdict']}", expanded=False):
            for _ln in _brief["lines"]:
                st.markdown(f"- {_ln[3:].strip() if _ln[:2].strip().rstrip('.').isdigit() else _ln}")
    except Exception:
        st.caption("⚠️ 5AI 브리핑 일시 비활성 (데이터 지연)")


    # ── 1행: 계좌 요약(전체폭) / 2행: 포트폴리오 관제 + 차트 ──
    #    (중복 매크로 카드행 · 홈 통합 랭킹 패널 제거 → 스캐너 탭으로 역할 분리)
    _left, _right = st.columns([1, 2], gap="medium")  # V9.2 좌우 2단 관제 (33:67 — 우측 관제 확대)

    # ══════════════════════════════════════════════
    # PANEL 1 — Account Summary + Live Signal Stream
    # ══════════════════════════════════════════════
    with _left:
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
    # 2행 — PANEL 3(관제) + PANEL 4(차트) : 40% / 60%
    # ══════════════════════════════════════════════
    with _left:  # 계좌 아래 LIVE SIGNAL을 좌측 열에 append
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
                        f"padding:5px 10px;margin-bottom:3px;font-size:11px;"
                        f"display:flex;justify-content:space-between;align-items:center;gap:6px'>"
                        f"<span style='min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>"
                        f"<b style='color:#f0f4ff'>{_sn}</b> <span style='color:#64748b'>{_ss}</span></span>"
                        f"<span style='color:{_scc};flex-shrink:0;font-weight:700'>{_sc:+.1f}%</span>"
                        f"</div>",
                        unsafe_allow_html=True
                    )
            else:
                st.markdown("<div style='color:#374151;font-size:11px;padding:6px'>관심종목 신호 없음</div>", unsafe_allow_html=True)

    # (V9.2: PANEL 3·4는 우측 열 _right에 세로 스택)

    # ══════════════════════════════════════════════
    # PANEL 3 — Active Portfolio 관제
    # ══════════════════════════════════════════════
    with _right:
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

                        # ── 퀵 액션 바 (카드 위쪽) — 버튼 간격 확보(미스클릭 방지) ──
                        try:
                            _qa1, _qa2, _qa3 = st.columns(3, gap="medium")
                        except TypeError:
                            _qa1, _qa2, _qa3 = st.columns(3)   # 구버전 폴백
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
                        st.markdown(f"""<div class='{_glow_class}' style='background:#0d1117;border:2px solid {_card_border_p3};border-radius:12px;padding:14px 16px;margin-bottom:8px'><div style='display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px'><div><div style='font-weight:800;font-size:14px;color:#f0f4ff'>{_nm_p3} {_ts_badge}</div><div style='color:#64748b;font-size:11px;margin-top:2px'>{_tk_p3} · {_qty_p3:,}주 · 평균 {_fmt_p3(_avg_p3)} · 평가 {_fmt_p3(_eval_p3)}</div></div><div style='text-align:right'><div style='font-size:22px;font-weight:900;color:{_pnl_color};line-height:1'>{_pnl_pct_p3:+.2f}%</div><div style='font-size:12px;color:{_pnl_color}'>{"+" if _pnl_abs_p3>=0 else "-"}{_fmt_p3(abs(_pnl_abs_p3))}</div></div></div><div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:10px'><div style='background:#111827;border-radius:8px;padding:8px;text-align:center'><div style='font-size:10px;color:#64748b'>현재가</div><div style='font-size:14px;font-weight:700;color:#f0f4ff'>{_fmt_p3(_cur_p3)}</div></div><div style='background:#1a0a0a;border-radius:8px;padding:8px;text-align:center;border:1px solid {"#ef4444" if _stop_warn else "#3f1515"}'><div style='font-size:10px;color:#ef4444'>🛑 손절 -7%</div><div style='font-size:14px;font-weight:700;color:#ef4444'>{_fmt_p3(_stop_p3)}</div></div><div style='background:#0a1a0d;border-radius:8px;padding:8px;text-align:center;border:1px solid {"#16a34a" if _target_hit else "#14532d"}'><div style='font-size:10px;color:#16a34a'>🎯 1차 +8%</div><div style='font-size:14px;font-weight:700;color:#16a34a'>{_fmt_p3(_target_p3)}</div></div></div><div style='background:#111827;border-radius:6px;padding:4px 8px;margin-bottom:8px'><div style='display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px;font-size:9px;color:#64748b;margin-bottom:3px'><span>손절 {_fmt_p3(_stop_p3)}</span><span>현재 {_fmt_p3(_cur_p3)}</span><span>목표 {_fmt_p3(_target_p3)}</span></div><div style='background:#1e293b;border-radius:4px;height:6px;overflow:hidden'><div style='background:{"#ef4444" if _prog_p3<25 else "#f97316" if _prog_p3<60 else "#16a34a"};height:100%;width:{_prog_p3:.0f}%;border-radius:4px;transition:width 0.3s'></div></div></div><div style='display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;font-size:11px;color:#64748b'><span>R:R <b style='color:#f0f4ff'>1:{(_target_p3-_avg_p3)/max(_avg_p3-_stop_p3,1):.1f}</b></span><span>2차목표 <b style='color:#22d3ee'>{_fmt_p3(_t2_p3)}</b></span><span style='font-weight:{"800" if _stop_breached else "400"};color:{"#ef4444" if _stop_breached else "#64748b"}'>{_status_msg}</span></div></div>""", unsafe_allow_html=True)


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
    with _right:
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
            # [V10.0] 줄글 나열 → 정돈된 Dataframe (날짜·종목명·수량·단가·액션). height=150 스크롤 제어.
            _ob_rows = []
            for _tr4 in reversed(_fb_trades_p4[-12:]):
                _act4 = _tr4.get('매매', '')
                _act_disp = ('🟢 매수' if _act4 in ('BUY', '매수')
                             else '🔴 매도' if _act4 in ('SELL', '매도') else (_act4 or '-'))
                try:
                    _qty4 = f"{int(_tr4.get('수량', 0) or 0):,}주"
                except Exception:
                    _qty4 = str(_tr4.get('수량', '-'))
                try:
                    _prc4 = f"{float(_tr4.get('순체결가', 0) or 0):,.0f}원"
                except Exception:
                    _prc4 = str(_tr4.get('순체결가', '-'))
                _ob_rows.append({
                    '날짜':   str(_tr4.get('날짜', '')),
                    '종목명': str(_tr4.get('종목명', '?')),
                    '수량':   _qty4,
                    '단가':   _prc4,
                    '액션':   _act_disp,
                })
            _ob_df = pd.DataFrame(_ob_rows, columns=['날짜', '종목명', '수량', '단가', '액션'])
            st.dataframe(_ob_df, height=150, use_container_width=True, hide_index=True)
        else:
            st.markdown("<div style='color:#374151;font-size:11px;padding:4px'>거래 기록 없음</div>", unsafe_allow_html=True)

    # ── 하단: 가이드 + 매크로 이벤트 (접힘) ──
    st.markdown("<hr style='margin:12px 0;border-color:#1e2a3a'>", unsafe_allow_html=True)
    _bot1, _bot2 = st.columns(2)
    with _bot1:
        with st.expander("📖 퀀트 관제탑 사용 가이드 및 일일 루틴 보기", expanded=False):
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


def _calc_adx14(df, period=14):
    """Wilder ADX(14) — 지표 df에 ADX 컬럼이 없어 즉석 계산. 실패 시 0."""
    try:
        _h = df['고가'].astype(float); _l = df['저가'].astype(float); _c = df['종가'].astype(float)
        _up = _h.diff(); _dn = -_l.diff()
        _plus_dm  = ((_up > _dn) & (_up > 0)) * _up
        _minus_dm = ((_dn > _up) & (_dn > 0)) * _dn
        _tr = pd.concat([_h - _l, (_h - _c.shift()).abs(), (_l - _c.shift()).abs()], axis=1).max(axis=1)
        _atr  = _tr.ewm(alpha=1/period, adjust=False).mean()
        _pdi  = 100 * _plus_dm.ewm(alpha=1/period, adjust=False).mean() / _atr
        _mdi  = 100 * _minus_dm.ewm(alpha=1/period, adjust=False).mean() / _atr
        _dx   = 100 * (_pdi - _mdi).abs() / (_pdi + _mdi).replace(0, np.nan)
        _adx  = _dx.ewm(alpha=1/period, adjust=False).mean().iloc[-1]
        return round(float(_adx), 1) if _adx == _adx else 0.0
    except Exception:
        return 0.0


def get_ai_recommended_strategy(df):
    """ADX / RSI / 20MA 이격도 기반 전략 자동 추천.
    반환: (preset_key, reason:str, adx:float).
      추세(trend): ADX>25 AND 종가>20MA
      반등(bounce): RSI<30 OR (RSI가 30 상향 돌파하며 상승 중)
      바닥(bottom): 종가-20MA 이격도 ≤ -15%
    우선순위: 바닥(극단) → 반등 → 추세 → 기본(반등)."""
    try:
        _l = df.iloc[-1]
        _close = float(_l['종가'])
        _ma20  = float(_l.get('MA20', _close)) or _close
        _rsi   = float(_l.get('RSI', 50))
        _disp  = (_close / _ma20 - 1) * 100 if _ma20 > 0 else 0.0
        _adx   = _calc_adx14(df)
        _rsi_prev = float(df.iloc[-2].get('RSI', _rsi)) if len(df) >= 2 else _rsi
        _rsi_cross_up = (_rsi_prev < 30 <= _rsi)   # 30 상향 돌파
        if _disp <= -15:
            return 'bottom', f"20MA 이격도 {_disp:.1f}% (≤ -15%) — 과매도 극단, 바닥 확인이 유리합니다.", _adx
        if _rsi < 30 or _rsi_cross_up:
            _why = f"RSI {_rsi:.0f} 과매도" if _rsi < 30 else f"RSI 30 상향 돌파({_rsi:.0f})"
            return 'bounce', f"{_why} — 반등 매매가 유리합니다.", _adx
        if _adx > 25 and _close > _ma20:
            return 'trend', f"ADX {_adx:.0f} (>25)·종가>20MA — 추세 매매가 유리합니다.", _adx
        return 'bounce', f"뚜렷한 신호 없음 (ADX {_adx:.0f}·RSI {_rsi:.0f}) — 기본 반등 전략.", _adx
    except Exception:
        return 'bounce', "데이터 부족 — 기본 반등 전략.", 0.0


with tab_b:
    st.markdown("### 🔍 분석")
    # ── 진입 금지 / 매크로 블랙아웃 대형 배너 ──
    _v891_b = run_v891_system_check()
    from datetime import datetime as _dt_tb
    _kh_b = (_dt_tb.utcnow().hour + 9) % 24
    _km_b = _dt_tb.utcnow().minute
    _time_block_b = (9 <= _kh_b < 10) or (_kh_b == 10 and _km_b <= 30)
    if not _v891_b['can_enter'] or _time_block_b:
        # ── 1줄 얇은 배너 (거대 박스·레짐 카운트다운 철거 → 차트 시인성 확보) ──
        _ban_msg  = _v891_b['alerts'][0] if not _v891_b['can_enter'] else "09:00~10:30 변동성 과다 구간"
        _ban_kind = ("FOMC 블랙아웃" if _v891_b.get('blackout')
                     else "장초반 변동성 구간" if _time_block_b else "지수 셧다운")
        st.markdown(
            f"<div style='background:rgba(239,68,68,0.12);border-left:3px solid #ef4444;"
            f"border-radius:6px;padding:6px 14px;margin-bottom:10px;font-size:12.5px;color:#fca5a5'>"
            f"🚨 <b style='color:#ef4444'>[매크로 킬스위치 발동] {_ban_kind}</b> — 신규 매수 차단 "
            f"<span style='color:#7f1d1d'>(차트·타점 분석만 가능 · {_ban_msg})</span></div>",
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
    # ── 🎛️ Control Ribbon — 종목/전략 단일 통합 (상하단 중복 드롭다운 제거) ──
    def _display_name(ticker, name):
        return f"{name} ({ticker})" if is_korean_ticker(ticker) else f"{ticker} ({name})"
    if not _b_tickers:
        st.info("👈 사이드바에서 관심종목을 추가해주세요.")
        st.stop()
    _b1_opts = [_display_name(t, n) for t, n in _b_tickers if t in all_data]
    if not _b1_opts:
        st.warning("데이터를 불러올 수 없습니다. 잠시 후 다시 시도해주세요.")
        st.stop()
    # 스캐너/연기금 '분석 탭으로 이동' 버튼이 남긴 사전선택값 적용
    # (위젯 key는 위젯 생성 후 수정 불가 → 반드시 selectbox 생성 전에 세팅)
    _pend_sel = st.session_state.pop('_pending_unified_sel', None)
    if _pend_sel and _pend_sel in _b1_opts:
        st.session_state['b_unified_sel'] = _pend_sel
    if 'analysis_preset' not in st.session_state:
        st.session_state.analysis_preset = 'bounce'
    _pr_map  = {"📉 반등": "bounce", "📈 추세": "trend", "🎯 바닥": "bottom"}
    _key_map = {"bounce": "📉 반등", "trend": "📈 추세", "bottom": "🎯 바닥"}
    _rib1, _rib2, _rib3 = st.columns([2, 1, 1], vertical_alignment="bottom")
    with _rib1:
        selected = st.selectbox("🔎 분석 종목", _b1_opts, key="b_unified_sel")
    # 라디오보다 먼저 종목/데이터 확정 → AI 추천 계산에 사용
    sel_ticker = selected.split('(')[-1].replace(')', '').strip()
    if not is_korean_ticker(sel_ticker):
        sel_ticker = selected.split(' ')[0].strip()
    sel_name = all_data[sel_ticker]['name']
    sel_df   = all_data[sel_ticker]['df']

    # ── ✨ AI 전략 자동 추천 (ADX/RSI/20MA 이격도) ──
    _ai_reco, _ai_reason, _ai_adx = get_ai_recommended_strategy(sel_df)
    _reco_lbl = _key_map.get(_ai_reco, "📉 반등")
    # 종목 최초 분석 시 → 추천 전략을 라디오 기본값으로 자동 세팅
    if st.session_state.get('_analysis_last_ticker') != sel_ticker:
        st.session_state['_analysis_last_ticker'] = sel_ticker
        st.session_state['preset_radio_b1'] = _reco_lbl
        st.session_state['analysis_preset'] = _ai_reco

    with _rib2:
        def _fmt_strat(_opt):
            return f"{_opt} ✨AI추천" if _opt == _reco_lbl else _opt
        _pr_sel = st.radio("매매 전략", list(_pr_map.keys()), horizontal=True,
                           format_func=_fmt_strat, key="preset_radio_b1")
        if _pr_map[_pr_sel] != st.session_state.analysis_preset:
            st.session_state.analysis_preset = _pr_map[_pr_sel]
            st.rerun()
    st.caption(f"✨ AI 전략 추천: **{_reco_lbl}** — {_ai_reason}")

    # ══════════════════════════════════════════════════════════════
    # 📊 분석 요약 (탭 위로 승격 — 종목/전략 즉시 결론) : Verdict + Checklist + 추천가
    # ══════════════════════════════════════════════════════════════
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
    _vd_check = "✅" if _vd_icon == "🟢" else "⚠️" if _vd_icon == "🟡" else "❌"
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
        return (
            f"<div style='background:{bg};border:1px solid {bd};border-radius:9px;"
            f"padding:7px 4px;text-align:center'>"
            f"<div style='font-size:16px'>{ic}</div>"
            f"<div style='font-size:10px;font-weight:700;color:{c};margin-top:2px'>{label}</div>"
            f"<div style='font-size:9px;color:#64748b'>{detail}</div>"
            f"</div>"
        )

    # ── 💾 저장 버튼을 Control Ribbon Col 3에 배치 (verdict 계산 완료 후 렌더) ──
    with _rib3:
        if st.button("💾 분석 실행 및 저장", key=f"save_log_{sel_ticker}", use_container_width=True):
            save_analysis_log(
                sel_ticker, sel_name, _vd_label, _ep['rr'],
                _ep['entry'], _ep['stoploss'], _ep['target1'], _ep['target2'],
                preset=st.session_state.analysis_preset, score=_buy_cnt, source="분석탭"
            )
            st.toast(f"✅ {sel_name} 분석 기록 저장됨", icon="💾")

    # ── 📊 요약 그리드 [1.5 : 2.5] — 좌: Verdict+체크리스트 / 우: 자동 추천가 (PROJECTION 제외) ──
    _sumc1, _sumc2 = st.columns([1.5, 2.5], gap="medium")
    with _sumc1:
        st.markdown(f"""
<div style='background:{_vd_bg};border:2px solid {_vd_border};border-radius:14px;
padding:14px 16px;margin-bottom:10px'>
  <div style='display:flex;align-items:center;gap:12px'>
    <div style='font-size:38px;line-height:1'>{_vd_icon}</div>
    <div style='flex:1'>
      <div style='font-size:18px;font-weight:900;color:{_vd_color}'>{_vd_label}</div>
      <div style='font-size:10px;color:#64748b'>{sel_name[:14]} · 신호 {_buy_cnt}매수/{_sell_cnt}매도</div>
    </div>
    <div style='text-align:right'>
      <div style='font-size:9px;color:#64748b'>R:R</div>
      <div style='font-size:26px;font-weight:900;color:{_vd_color};font-family:IBM Plex Mono;line-height:1'>{_ep["rr"]}</div>
    </div>
  </div>
  <div style='margin-top:8px'>
    {''.join(f"<div style='font-size:11px;color:#94a3b8;margin-bottom:2px'>{_vd_check} {ln}</div>" for ln in _vd_lines)}
  </div>
</div>
<div style='display:grid;grid-template-columns:repeat(3,1fr);gap:6px'>
  {_ck_badge("R:R 2.0+", _rr_ok, str(_ep["rr"]))}
  {_ck_badge("매수신호 2+", _sig_ok, f"{_buy_cnt}개")}
  {_ck_badge("시스템", _sys_ok, "매크로")}
  {_ck_badge("거래량", _vol_ok, f"{l.get('거래량_비율',100):.0f}%")}
  {_ck_badge("RSI 30-65", _rsi_ok, f"{l['RSI']:.0f}")}
  {_ck_badge("MA20 위", _ma_ok, f"{l.get('MA20',0):,.0f}")}
</div>""", unsafe_allow_html=True)
    with _sumc2:
        st.markdown(f"""
<div style='background:#0d1117;border:1px solid #1e293b;border-radius:12px;padding:14px 16px'>
  <div style='font-size:11px;color:#64748b;font-weight:700;margin-bottom:10px'>
    🎯 자동 추천가 (진입/손절/목표)
    <span style='float:right;color:#64748b'>현재가 <b style='color:#f0f4ff'>{format_price(_display_price, sel_ticker)}</b>{_kis_badge}</span>
  </div>
  <div style='display:grid;grid-template-columns:repeat(2,1fr);gap:10px'>
    <div style='background:rgba(251,191,36,0.1);border:1px solid rgba(251,191,36,0.3);border-radius:10px;padding:14px;text-align:center'>
      <div style='font-size:11px;color:#64748b'>🎯 진입 타점</div>
      <div style='font-size:22px;font-weight:800;color:#fbbf24'>{_ep["entry"]:,.0f}</div>
      <div style='font-size:10px;color:#64748b'>{_ep["gap_pct"]:+.1f}% 대기</div>
    </div>
    <div style='background:rgba(244,63,94,0.1);border:1px solid rgba(244,63,94,0.3);border-radius:10px;padding:14px;text-align:center'>
      <div style='font-size:11px;color:#64748b'>🛑 손절가</div>
      <div style='font-size:22px;font-weight:800;color:#f43f5e'>{_ep["stoploss"]:,.0f}</div>
      <div style='font-size:10px;color:#64748b'>-7%</div>
    </div>
    <div style='background:rgba(52,211,153,0.1);border:1px solid rgba(52,211,153,0.3);border-radius:10px;padding:14px;text-align:center'>
      <div style='font-size:11px;color:#64748b'>🎯 1차 목표</div>
      <div style='font-size:22px;font-weight:800;color:#34d399'>{_ep["target1"]:,.0f}</div>
      <div style='font-size:10px;color:#64748b'>+8%</div>
    </div>
    <div style='background:rgba(167,139,250,0.1);border:1px solid rgba(167,139,250,0.3);border-radius:10px;padding:14px;text-align:center'>
      <div style='font-size:11px;color:#64748b'>✨ 2차 목표</div>
      <div style='font-size:22px;font-weight:800;color:#a78bfa'>{_ep["target2"]:,.0f}</div>
      <div style='font-size:10px;color:#64748b'>+15%</div>
    </div>
  </div>
</div>""", unsafe_allow_html=True)

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

            # (종목/전략 선택은 상단 Control Ribbon으로 통합 이관 — sel_ticker/sel_df 재사용)

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

            # ── 현재가 기준선 (흰색 점선 — 타점선과 시각 분리, 우측 Y축 라벨) ──
            _cur_line = float(_display_price or 0)
            if _cur_line > 0:
                _mp_fig.add_hline(y=_cur_line, line=dict(color='#f8fafc', dash='dot', width=1.4),
                                  annotation_text=f"<b>현재가 {_cur_line:,.0f}</b>",
                                  annotation_font=dict(color='#f8fafc', size=12, family='IBM Plex Mono'),
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

            # ── ✏️ 수동 타점 조정 ↔ 실시간 PROJECTION (좌우 [1:1] 결합) ──
            _mcol, _pcol = st.columns([1, 1], gap="medium")
            with _mcol:
                st.markdown("<div style='font-size:11px;color:#64748b;font-weight:700;margin-bottom:6px'>✏️ 수동 타점 조정</div>", unsafe_allow_html=True)
                _unit    = get_currency(sel_ticker)
                _is_kr_m = is_korean_ticker(sel_ticker)
                _step    = 100.0 if _is_kr_m else 0.01
                _mca, _mcb = st.columns(2)
                entry_price   = _mca.number_input(f"매수가 ({_unit})",   value=float(entry_price or 0),   step=_step, key=f"madj_entry_{sel_ticker}")
                stop_price    = _mcb.number_input(f"손절가 ({_unit})",   value=float(stop_price or 0),    step=_step, key=f"madj_stop_{sel_ticker}")
                target1_price = _mca.number_input(f"1차 목표 ({_unit})", value=float(target1_price or 0), step=_step, key=f"madj_t1_{sel_ticker}")
                target2_price = _mcb.number_input(f"2차 목표 ({_unit})", value=float(target2_price or 0), step=_step, key=f"madj_t2_{sel_ticker}")
                st.caption("← 숫자를 바꾸면 우측 시나리오가 즉시 반응")
            with _pcol:
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

# ══════════════════════════════════════════════════════════════════
# ⚔️ 만쥬식 / 돌팬티식 모닝 브리핑 — KIS 실시간 수급 기반(구글시트/GAS 미사용)
# ══════════════════════════════════════════════════════════════════
_MD_TARGETS = [
    ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("042700", "한미반도체"),
    ("196170", "알테오젠"), ("247540", "에코프로비엠"), ("035420", "NAVER"),
]


def _md_lower_tail(_o, _h, _l, _c, _ratio=0.33):
    """당일 캔들 '아래꼬리' 판정 — 아래꼬리(min(시,종)-저) ≥ 전체범위×ratio."""
    if not all(isinstance(_v, (int, float)) and _v > 0 for _v in (_o, _h, _l, _c)):
        return False
    _rng = _h - _l
    return bool(_rng > 0 and (min(_o, _c) - _l) >= _rng * _ratio)


def _md_investor(code):
    """종목별 외국인/기관 순매수 — KIS(장중·주) → 네이버(전일·주) → pykrx(전일·원) 순 폴백.
    KIS 투자자 API(FHKST01010900) 빈 값·pykrx 클라우드 차단에도 수급을 최대한 채움.
    반환: {'외인': int, '기관': int, 'unit': '주'|'원', 'src': str} 또는 None."""
    _k = kis_get_investor(code)
    if _k:
        return {'외인': int(_k.get('외인순매수', 0)), '기관': int(_k.get('기관순매수', 0)),
                'unit': '주', 'src': 'KIS'}
    _nv = _md_investor_naver(code)   # 폴백2: 네이버(클라우드 친화)
    if _nv:
        return _nv
    try:
        from pykrx import stock as _pk_md
        import datetime as _dmd
        _kst = _dmd.datetime.utcnow() + _dmd.timedelta(hours=9)
        _end = _kst.strftime("%Y%m%d")
        _start = (_kst - _dmd.timedelta(days=10)).strftime("%Y%m%d")
        _df = _pk_md.get_market_trading_value_by_investor(_start, _end, code)
        if _df is None or _df.empty:
            return None
        _col = "순매수" if "순매수" in _df.columns else _df.columns[-1]

        def _pick(*_keys):
            for _kk in _keys:
                if _kk in _df.index:
                    _v = float(_df.loc[_kk, _col])
                    if _v == _v:
                        return _v
            return 0.0
        return {'외인': int(_pick("외국인", "외국인합계")),
                '기관': int(_pick("기관합계", "기관계", "기관")),
                'unit': '원', 'src': 'pykrx(전일누적)'}
    except Exception as _e:
        import logging as _lg_pk
        _lg_pk.warning("pykrx 수급 폴백 %s 실패: %s: %s", code, type(_e).__name__, _e)
        return None


@st.cache_data(ttl=300, show_spinner=False)
def _fetch_premarket_macro() -> dict:
    """[매크로 기상도] 프리마켓 글로벌 지표 — yfinance. session_state 미접근(캐시 안전).
    반환: {vix, vix_chg, krw, krw_chg, ixic_chg, sox_chg} — 실패 항목은 None."""
    import yfinance as _yf_mw
    _out = {"vix": None, "vix_chg": None, "krw": None, "krw_chg": None,
            "ixic_chg": None, "sox_chg": None}

    def _closes(_sym, _period="5d"):
        try:
            _h = _yf_mw.Ticker(_sym).history(period=_period)
            _c = _h["Close"].dropna()
            return _c if len(_c) >= 2 else None
        except Exception:
            return None

    _v = _closes("^VIX")
    if _v is not None:
        _out["vix"] = round(float(_v.iloc[-1]), 2)
        _out["vix_chg"] = round(float(_v.iloc[-1] - _v.iloc[-2]), 2)
    _k = _closes("KRW=X")
    if _k is not None:
        _out["krw"] = round(float(_k.iloc[-1]), 1)
        _out["krw_chg"] = round((float(_k.iloc[-1]) / float(_k.iloc[-2]) - 1) * 100, 2)
    for _key, _sym in [("ixic_chg", "^IXIC"), ("sox_chg", "^SOX")]:
        _s = _closes(_sym)
        if _s is not None:
            _out[_key] = round((float(_s.iloc[-1]) / float(_s.iloc[-2]) - 1) * 100, 2)
    return _out


def render_macro_weather():
    """[🌍 Top-Down 매크로 기상도] 프리마켓 미국발 지표(VIX·환율·나스닥·SOX)를 융합해
    오늘 국장 레짐을 3단계(🟢/🟡/🔴)로 판독 + 권장 실탄 투입 비중 배너 출력.
    만쥬/돌팬티 등 KIS 스캐너와 독립 작동(별도 함수·별도 데이터소스)."""
    _m = _fetch_premarket_macro()
    _vix, _vchg = _m["vix"], _m["vix_chg"]
    _kchg = _m["krw_chg"]
    _ix, _sox = _m["ixic_chg"], _m["sox_chg"]
    _tech = [_x for _x in (_ix, _sox) if _x is not None]

    if all(_v is None for _v in (_vix, _kchg, _ix, _sox)):
        st.caption("🌍 매크로 기상도 — 지표 수신 대기 중")
        return

    # ── 레짐 판정 (강한 단일 리스크는 즉시 🔴) ──
    _red = ((_vix is not None and _vix >= 30)
            or (_kchg is not None and _kchg >= 1.5)
            or (_tech and min(_tech) <= -3.0))
    _green = ((_vix is not None and _vix < 20)
              and (_kchg is not None and _kchg <= 0.1)
              and (bool(_tech) and min(_tech) >= 0.0))
    _regime = "red" if _red else "green" if _green else "amber"

    # ── 지표 인라인(메트릭 행 제거로 세로 압축) ──
    _pf = lambda v, s="%": f"{v:+.2f}{s}" if isinstance(v, (int, float)) else "—"
    _vixs = f"VIX {_vix:.1f}{'↑' if (_vchg or 0)>0 else '↓' if (_vchg or 0)<0 else ''}" if _vix is not None else "VIX —"
    _krws = f"환율 {_m['krw']:,.0f}({_pf(_kchg)})" if _m["krw"] is not None else "환율 —"
    _detail = f"🌍 {_vixs} · {_krws} · 나스닥 {_pf(_ix)} · SOX {_pf(_sox)}"

    _cfg = {"green": ("#052e16", "#22c55e", "🟢 리스크온 · 실탄 100%"),
            "amber": ("#422006", "#f59e0b", "🟡 경계 · 실탄 50%↓"),
            "red":   ("#450a0a", "#ef4444", "🔴 리스크오프 · 신규차단")}
    _bg, _bd, _lbl = _cfg[_regime]
    st.markdown(
        f"<div style='background:{_bg};border:1px solid {_bd};border-radius:8px;padding:5px 12px;"
        f"margin-bottom:6px;font-size:12px;display:flex;justify-content:space-between;align-items:center'>"
        f"<b style='color:{_bd}'>{_lbl}</b><span style='color:#94a3b8'>{_detail}</span></div>",
        unsafe_allow_html=True)


def render_manju_dolpanti_briefing():
    """만쥬(장중 수급 턴어라운드+15:15 청산) / 돌팬티(15:00 종가베팅 3필터) 브리핑.
    KIS 실시간 수급·시세 재활용. 오전 스냅샷은 session_state 보존. 예외는 종목별 격리."""
    import datetime as _dtmd
    _now = _dtmd.datetime.utcnow() + _dtmd.timedelta(hours=9)   # KST
    _today = _now.strftime("%Y-%m-%d"); _t = _now.time()
    _MOPEN = _dtmd.time(9, 0); _AM = _dtmd.time(11, 30); _EXIT = _dtmd.time(15, 15)
    _CLOSE = _dtmd.time(15, 20); _DP = _dtmd.time(15, 0)
    _is_weekday = _now.weekday() < 5   # 월~금

    with st.expander("⚔️ 만쥬 / 돌팬티 모닝 브리핑 (실시간 수급)", expanded=False):
        if not kis_available():
            st.info("🔌 KIS 연결 시 실시간 수급 기반으로 가동됩니다 (앱키/시크릿 필요).")
            return

        # ── 분 단위 세션 캐시 — rerun(체크박스 토글 등)마다 KIS 30여회 재호출로 레이트리밋에
        #    걸려 '토큰 실패'가 뜨던 문제 차단. 같은 분(minute) 안에서는 캐시 재사용. ──
        _bucket = _now.strftime("%Y%m%d%H%M")
        _mdc = st.session_state.get('_md_raw_cache')
        if not (_mdc and _mdc.get('bucket') == _bucket):
            _raw = {}
            for _c, _n in _MD_TARGETS:
                try:
                    _raw[_c] = {'inv': _md_investor(_c), 'pr': kis_get_price(_c)}
                except Exception as _e:
                    import logging as _lg_md
                    _lg_md.warning("만쥬/돌팬티 %s 조회 실패: %s: %s", _c, type(_e).__name__, _e)
                    _raw[_c] = {'inv': None, 'pr': None}
            _mdc = {'bucket': _bucket, 'raw': _raw}
            st.session_state['_md_raw_cache'] = _mdc
        _raw = _mdc['raw']

        # ── 수신 진단 — 실패 원인(토큰/투자자엔드포인트/시세)을 표면화 ──
        _n = len(_MD_TARGETS)
        _inv_ok = sum(1 for _v in _raw.values() if _v.get('inv'))
        _pr_ok  = sum(1 for _v in _raw.values() if _v.get('pr'))
        if _inv_ok == 0:
            _terr = st.session_state.get('_kis_token_err')
            _diag = f"수급 {_inv_ok}/{_n} · 시세 {_pr_ok}/{_n} 수신"
            if _terr:
                _diag += f" · 토큰오류: {_terr}"
            if _pr_ok > 0:
                # 시세는 되는데 투자자(수급)만 실패 → KIS·pykrx 폴백 모두 빈 값
                st.warning("⚠️ **투자자(수급) 데이터** 수신 실패 — 시세는 정상입니다. "
                           f"({_diag})\n\nKIS·네이버·pykrx 3중 폴백 모두 빈 값입니다. "
                           "휴장/장 마감 직후 집계 지연일 수 있으니 **평일 장중**에 다시 시도하세요.")
            else:
                st.warning("⚠️ KIS 데이터 수신 실패 — 장중(09:00~15:20)·평일에 다시 확인하세요. "
                           f"({_diag})\n\n주말·장마감·호출제한 시 빈 값이 정상입니다.")
            # ⚠️ 토큰 캐시는 비우지 않음 — 비우면 즉시 재발급 시도로 EGW00133(1분1회) 재유발.
            #    데이터 분캐시만 비워 다음 rerun에서 (쿨다운 해제 후) 자연 재시도.
            if st.button("🔄 데이터 캐시만 비우고 재시도", key="md_retry"):
                st.session_state.pop('_md_raw_cache', None)
                st.rerun()
            st.caption("💡 EGW00133은 '토큰 1분당 1회' 제한입니다 — 약 1분 뒤 자동으로 풀립니다.")
            return

        # 오전 스냅샷 기록 조건 = 실제 오전 장중(평일 09:00~11:30). 이 창에서만 '오늘 오전' 수급이
        # 의미 있으므로, 이 시간대에 화면을 열면 소스(KIS/네이버) 무관하게 스냅샷을 남긴다.
        _is_am = _is_weekday and (_MOPEN <= _t < _AM)
        _exit  = _EXIT <= _t < _CLOSE
        # ── 오전 스냅샷 영속화 — session_state만 쓰면 앱 재부팅(파일 교체 등)마다 소실되어
        #    '오전에 열었는데 미기록' 문제 발생 → Firebase(/manju_am_snapshot)+로컬파일 복원.
        _snap = st.session_state.get('_manju_am_snapshot')
        if not isinstance(_snap, dict) or _today not in _snap:
            _restored = {}
            try:
                _fb_d = _fb_ref("/manju_am_snapshot").get()   # NullRef→None(토스트 없음)
                if isinstance(_fb_d, dict) and _fb_d.get('date') == _today:
                    _restored = {str(k): int(v) for k, v in (_fb_d.get('flows') or {}).items()}
            except Exception:
                pass
            if not _restored:
                try:
                    import json as _jms
                    _sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manju_am_snapshot.json")
                    with open(_sp, encoding='utf-8') as _f:
                        _fd = _jms.load(_f)
                    if _fd.get('date') == _today:
                        _restored = {str(k): int(v) for k, v in (_fd.get('flows') or {}).items()}
                except Exception:
                    pass
            _snap = st.session_state.setdefault('_manju_am_snapshot', {})
            _snap.setdefault(_today, {}).update(_restored)
        _am_store = _snap.setdefault(_today, {})
        _am_dirty = False   # 이번 run에서 새 오전 기록 발생 시 영속 저장

        # ── ① 만쥬식 — 장중 수급 턴어라운드 ──
        st.markdown("**① 만쥬식 — 장중 수급 턴어라운드**"
                    + ("  ·  ⏰ **15:15 청산 구간(EXIT)**" if _exit else ""))
        if _exit:
            st.error("⏰ 15:15 타임리밋 — 만쥬 포지션 **즉시 청산(EXIT)** 실행하세요.")
        _manju_hit = False
        for _c, _n in _MD_TARGETS:
            _d = _raw.get(_c, {}); _inv = _d.get('inv'); _pr = _d.get('pr')
            if not _inv or not _pr:
                continue
            _flow = int(_inv.get('외인', 0)) + int(_inv.get('기관', 0))
            _unit = _inv.get('unit', '주')
            _have_am = _c in _am_store
            if _is_am:
                # 오전 장중 창에서는 소스 무관하게 '오늘 오전' 수급으로 기록(오후 비교 기준선)
                _am_store[_c] = _flow; _am_dirty = True; _have_am = True
                _amflow = _flow; _turn = False
            else:
                _amflow = int(_am_store.get(_c, _flow))
                _turn = (_have_am and _amflow <= 0 and _flow > 0)   # 오전 스냅샷 있을 때만 판정
            _chg = _pr.get('등락률', 0.0)
            _fc = "#e11d48" if _flow > 0 else "#2563eb" if _flow < 0 else "#64748b"
            _bg = "#3b0d16" if (_turn and not _exit) else "#0d1117"
            _tag = (" <span style='color:#f43f5e;font-weight:800'>🔴 매수전환(진입)</span>"
                    if (_turn and not _exit) else "")
            _am_txt = f"오전 {_amflow:+,}" if _have_am else "오전 미기록"
            _src_tag = "" if _inv.get('src') == 'KIS' else " <span style='color:#64748b;font-size:10px'>(전일)</span>"
            st.markdown(
                f"<div style='background:{_bg};border-radius:6px;padding:5px 10px;margin-bottom:3px;"
                f"font-size:12px;display:flex;justify-content:space-between'>"
                f"<span><b>{_n}</b> <span style='color:#64748b'>{_c}</span> {_tag}</span>"
                f"<span style='color:#94a3b8'>{_am_txt} → 현재 <b style='color:{_fc}'>{_flow:+,}</b>{_unit}{_src_tag} "
                f"<span style='color:#64748b'>({_chg:+.2f}%)</span></span></div>",
                unsafe_allow_html=True)
            _manju_hit = _manju_hit or (_turn and not _exit)
        # 새 오전 기록 발생 시 Firebase+로컬파일 영속 저장(재부팅에도 보존)
        if _am_dirty and _am_store:
            _payload = {'date': _today, 'flows': dict(_am_store)}
            try:
                if _get_firebase_app() is not None:
                    _fb_ref("/manju_am_snapshot").set(_payload)
            except Exception:
                pass
            try:
                import json as _jms2
                _sp2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manju_am_snapshot.json")
                with open(_sp2, 'w', encoding='utf-8') as _f2:
                    _jms2.dump(_payload, _f2, ensure_ascii=False)
            except Exception:
                pass
        if _is_am:
            st.caption(f"🌅 오전 장중 — {len(_am_store)}종목 스냅샷 저장됨(재부팅에도 보존). 11:30 이후 '오전→오후 전환' 판정 가동")
        elif not _am_store:
            _why = ("오늘 오전 장중(09:00~11:30)에 이 화면을 아직 안 열었습니다"
                    if _is_weekday else "오늘은 주말/휴장입니다")
            st.caption(f"ℹ️ 오전 스냅샷 없음 — {_why}. 평일 오전에 한 번 열면 오후에 '오전→오후 전환'이 표시됩니다. "
                       "(지금은 현재 수급만 표시)")
        elif not _manju_hit and not _exit:
            st.caption("감시 중 — 오전 순매도→오후 순매수 전환 종목 없음")

        # 📌 수동 기준선 — 원할 때 '현재 수급'을 오전 기준으로 저장(재부팅에도 보존).
        #    자동 오전기록을 못 잡았거나, 임의 시점부터 변화를 추적하고 싶을 때 사용.
        if st.button("📌 지금 수급을 '오전 기준선'으로 저장", key="md_set_baseline",
                     help="현재 외인+기관 순매수를 오전 기준선으로 저장 → 이후 값 변화가 '전환'으로 표시됩니다."):
            _base = {}
            for _bc, _bn in _MD_TARGETS:
                _biv = _raw.get(_bc, {}).get('inv')
                if _biv:
                    _base[_bc] = int(_biv.get('외인', 0)) + int(_biv.get('기관', 0))
            if _base:
                _am_store.clear(); _am_store.update(_base)
                _bp = {'date': _today, 'flows': dict(_am_store)}
                try:
                    if _get_firebase_app() is not None:
                        _fb_ref("/manju_am_snapshot").set(_bp)
                except Exception:
                    pass
                try:
                    import json as _jmb
                    _spb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manju_am_snapshot.json")
                    with open(_spb, 'w', encoding='utf-8') as _fb:
                        _jmb.dump(_bp, _fb, ensure_ascii=False)
                except Exception:
                    pass
                st.session_state.pop('_md_raw_cache', None)
                st.success(f"✅ {len(_base)}종목 현재 수급을 오전 기준선으로 저장했습니다.")
                st.rerun()

        st.divider()

        # ── ② 돌팬티식 — 15:00 종가베팅 3필터 (만쥬와 동일 캐시 재사용, KIS 추가호출 없음) ──
        _force = st.checkbox("🧪 돌팬티 스캐너 강제 가동(15:00 이전 테스트)", value=False, key="md_dp_force")
        st.markdown("**② 돌팬티식 — [오늘의 돌팬티 타겟]** (15:00 가동 · 20MA↑ / 기관 순매수+ / 아래꼬리)")
        if _t < _DP and not _force:
            st.caption("⏳ 15:00 종가베팅 스캐너 대기 중 — 장중 소음 회피(정각 가동)")
            return
        _targets = []
        _detail = []   # 탈락 근거(투명성): [(종목, c1, c2, c3, org)]
        for _c, _n in _MD_TARGETS:
            _d = _raw.get(_c, {}); _pr = _d.get('pr'); _inv = _d.get('inv')
            if not _pr or not _inv:
                _detail.append((_n, None, None, None, None)); continue
            try:
                _ind = calc_indicators(fetch_ohlcv(_c, 60))   # fetch_ohlcv는 @st.cache_data(1800s)
                _ma20 = float(_ind['MA20'].iloc[-1]); _close = float(_ind['종가'].iloc[-1])
            except Exception as _e:
                import logging as _lg_md2
                _lg_md2.warning("돌팬티 %s 지표 실패: %s: %s", _c, type(_e).__name__, _e)
                _detail.append((_n, None, None, None, None)); continue
            _c1 = _close > _ma20 > 0
            _org = int(_inv.get('기관', 0)); _c2 = _org > 0
            _c3 = _md_lower_tail(_pr.get('시가'), _pr.get('고가'), _pr.get('저가'), _pr.get('현재가'))
            _detail.append((_n, _c1, _c2, _c3, _org))
            if _c1 and _c2 and _c3:
                _targets.append((_n, _c, _pr.get('현재가', 0), _ma20, _org))
        if not _targets:
            st.caption("오늘 3조건(20MA↑·기관 순매수+·아래꼬리) 동시충족 종목 없음 — 관망")
            # 왜 관망인지 종목별 근거(✅=통과) — '진짜 탈락'인지 데이터오류인지 구분(투명성)
            def _m(_b):
                return "✅" if _b else "❌"
            _rows = ["<div style='font-size:11px;color:#94a3b8;line-height:1.7'><b>탈락 근거</b><br>"]
            for _dn, _dc1, _dc2, _dc3, _dorg in _detail:
                if _dc1 is None:
                    _rows.append(f"· {_dn}: ⚠️ 데이터 조회 실패<br>")
                else:
                    _rows.append(f"· {_dn}: 20MA {_m(_dc1)} · 기관순매수 {_m(_dc2)}({_dorg:+,}) · 아래꼬리 {_m(_dc3)}<br>")
            _rows.append("</div>")
            st.markdown("".join(_rows), unsafe_allow_html=True)
        else:
            for _n, _c, _px, _ma20, _org in _targets:
                st.markdown(
                    f"<div style='background:#2e1065;border-radius:8px;padding:8px 12px;margin-bottom:4px;"
                    f"font-size:13px;display:flex;justify-content:space-between'>"
                    f"<span>🎯 <b>{_n}</b> <span style='color:#a78bfa'>{_c}</span></span>"
                    f"<span>{_px:,}원 · 20MA {_ma20:,.0f} · 기관 <b style='color:#e11d48'>{_org:+,}</b>주</span>"
                    f"</div>", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════
# 🔴 폭락장 전술 모드 — KODEX 200선물인버스2X(곱버스) 하방 타격 모듈
# ══════════════════════════════════════════════════════════════════
_INVERSE_TICKER = "252670"
_INVERSE_NAME = "KODEX 200선물인버스2X"
_TACTICAL_AMMO = 8_390_000   # 기동타격대 실탄(839만원)


def _inverse_5m_signal(sym="252670.KS"):
    """5분봉 아래꼬리 + 우상향(최근 15분 상승) 여부. 데이터 없으면 None(확인불가)."""
    try:
        import yfinance as _yf_iv
        _h = _yf_iv.Ticker(sym).history(period="1d", interval="5m").dropna(subset=['Close'])
        if _h is None or len(_h) < 4:
            return None
        _last = _h.iloc[-1]
        _rng = float(_last['High'] - _last['Low'])
        _tail = ((min(float(_last['Open']), float(_last['Close'])) - float(_last['Low'])) >= _rng * 0.33) if _rng > 0 else False
        _uptrend = float(_h['Close'].iloc[-1]) > float(_h['Close'].iloc[-4])   # 최근 15분 우상향
        return bool(_tail and _uptrend)
    except Exception:
        return None


def scan_inverse_target():
    """[하방 타격] 폭락장 판정 + KODEX 200선물인버스2X 단일 집중 진입 시그널.
    독립 모듈 — 기존 KIS/yfinance 구조 재활용, 예외 전파 없음.
    반환 dict: {is_crash, kospi_chg, price, ma20, net, c1, c2, c3, signal, entry, qty, t3, t5, stop}."""
    _out = {'is_crash': False, 'kospi_chg': None, 'price': None, 'ma20': None,
            'net': None, 'c1': False, 'c2': False, 'c3': None, 'signal': False,
            'entry': None, 'qty': 0, 't3': None, 't5': None, 'stop': None}
    # ── 1) 폭락장 레짐 판정 — 코스피 등락률(≤-1.5%) ──
    try:
        _q = get_index_quotes()
        _kchg = (_q or {}).get("코스피", {}).get("등락")
        _out['kospi_chg'] = _kchg if isinstance(_kchg, (int, float)) else None
    except Exception:
        pass
    _out['is_crash'] = bool(_out['kospi_chg'] is not None and _out['kospi_chg'] <= -1.5)

    # ── 2) 곱버스 진입 3조건 ──
    try:
        _pr = kis_get_price(_INVERSE_TICKER)
        if _pr and _pr.get('현재가'):
            _out['price'] = int(_pr['현재가'])
            # (1) 20MA 상향돌파/지지
            _ind = calc_indicators(fetch_ohlcv(_INVERSE_TICKER, 60))
            _ma20 = float(_ind['MA20'].iloc[-1])
            _out['ma20'] = round(_ma20, 1)
            _out['c1'] = bool(_out['price'] >= _ma20 > 0)
            # (2) 기관/외인 순매수 유입
            _iv = _md_investor(_INVERSE_TICKER)
            if _iv:
                _out['net'] = int(_iv.get('외인', 0)) + int(_iv.get('기관', 0))
                _out['c2'] = _out['net'] > 0
            # (3) 5분봉 아래꼬리+우상향
            _out['c3'] = _inverse_5m_signal(f"{_INVERSE_TICKER}.KS")
            # 시그널: (1)(2) 필수 + (3)은 확인불가(None) 시 통과 처리
            _c3_ok = _out['c3'] if _out['c3'] is not None else True
            _out['signal'] = bool(_out['c1'] and _out['c2'] and _c3_ok)
            # 타점 카드
            _entry = _out['price']
            _out['entry'] = _entry
            _out['qty'] = int(_TACTICAL_AMMO // _entry) if _entry > 0 else 0
            _out['t3'] = int(round(_entry * 1.03))
            _out['t5'] = int(round(_entry * 1.05))
            _out['stop'] = int(round(_ma20))
    except Exception as _e:
        import logging as _lg_iv
        _lg_iv.warning("scan_inverse_target 실패: %s: %s", type(_e).__name__, _e)
    return _out


def render_tactical_mode():
    """전술 모드 배너 렌더 + (폭락장) 곱버스 타격 카드. 반환: is_crash(bool)."""
    _iv = scan_inverse_target()
    _crash = _iv['is_crash']
    _kchg = _iv['kospi_chg']
    _kchg_s = f"코스피 {_kchg:+.2f}%" if _kchg is not None else "코스피 수신대기"

    if _crash:
        st.markdown(
            f"<div style='background:#450a0a;border:2px solid #ef4444;border-radius:12px;"
            f"padding:12px 16px;margin-bottom:10px;text-align:center'>"
            f"<div style='font-size:16px;font-weight:900;color:#fca5a5'>🔴 전술 모드: 폭락장 곱버스 타격</div>"
            f"<div style='font-size:12px;color:#fecaca;margin-top:3px'>{_kchg_s} — 개별주(만쥬/돌팬티) 매수 자동 차단</div>"
            f"</div>", unsafe_allow_html=True)
    else:
        st.markdown(
            f"<div style='background:#052e16;border:1px solid #22c55e;border-radius:8px;"
            f"padding:5px 12px;margin-bottom:6px;font-size:12px;display:flex;justify-content:space-between;align-items:center'>"
            f"<b style='color:#86efac'>🟢 전술: 상승장 개별주 타격</b>"
            f"<span style='color:#bbf7d0'>{_kchg_s} · 만쥬/돌팬티 가동</span>"
            f"</div>", unsafe_allow_html=True)
        return False

    # ── 폭락장: 곱버스 단일 타격 카드 ──
    st.markdown(f"**🎯 하방 타격 타겟 — {_INVERSE_NAME} ({_INVERSE_TICKER})**")
    def _m(_b):
        return "✅" if _b else ("⚪" if _b is None else "❌")
    _c3d = "확인불가" if _iv['c3'] is None else ("✅" if _iv['c3'] else "❌")
    _net_s = f"{_iv['net']:+,}주" if _iv['net'] is not None else "—"
    st.markdown(
        f"<div style='font-size:12px;color:#cbd5e1;line-height:1.7'>"
        f"① 20MA 지지/돌파 {_m(_iv['c1'])} (현재 {_iv['price']:,}원 / 20MA {_iv['ma20']:,}원)<br>"
        f"② 기관·외인 순매수 유입 {_m(_iv['c2'])} ({_net_s})<br>"
        f"③ 5분봉 아래꼬리+우상향 {_c3d}</div>", unsafe_allow_html=True)

    if _iv['signal']:
        st.error("🔥 **곱버스 타격 신호** — 3조건 충족 · 15:00~15:20 종가 베팅")
        _e, _q, _t3, _t5, _s = _iv['entry'], _iv['qty'], _iv['t3'], _iv['t5'], _iv['stop']
        # 타점 3종을 세로 콤팩트 라인으로(반쪽폭 컬럼 중첩 제거)
        st.markdown(
            f"<div style='font-size:12px;line-height:1.8'>"
            f"<span style='color:#94a3b8'>진입</span> <b style='color:#f0f4ff'>{_e:,}원</b> "
            f"<span style='color:#64748b'>×{_q}주({_e*_q:,}원)</span><br>"
            f"<span style='color:#94a3b8'>목표</span> <b style='color:#22c55e'>{_t3:,}~{_t5:,}원</b> "
            f"<span style='color:#64748b'>(+{(_t3-_e)*_q:,}~{(_t5-_e)*_q:,}원)</span><br>"
            f"<span style='color:#94a3b8'>손절</span> <b style='color:#ef4444'>{_s:,}원(20MA)</b> "
            f"<span style='color:#64748b'>({(_s-_e)*_q:,}원)</span></div>", unsafe_allow_html=True)
    else:
        st.caption("⏳ 곱버스 진입 대기 — 3조건 미충족(위 체크 참고)")
    return True


# ══════════════════════════════════════════════════════════════════
# 🌙 야간 타격 모드 — 3분할 종가베팅(선취매) 대시보드 (EWY 프록시 + mPOP 딥링크)
# ══════════════════════════════════════════════════════════════════
_NIGHT_TARGET_FALLBACK = ("000660", "SK하이닉스")


def _get_night_target():
    """[V10.4] 야간 타격 타겟 동적 연동 — 스캔된 주도주 1위(활성 랭킹) 우선, 없으면 폴백.
    반환 (code, name). 하드코딩 제거: 시스템이 스캔한 실시간 타겟과 연동."""
    try:
        _key = st.session_state.get('_scanner_ranked_active')
        _df = st.session_state.get(_key) if _key else None
        if _df is not None and not _df.empty:
            _row = _df.iloc[0]
            _code = str(_row.get('코드') or _row.get('종목코드') or '').strip()
            _name = str(_row.get('종목명') or _row.get('ETF명') or _row.get('name') or _code).strip()
            # KR 6자리 종목만 야간 선취매 대상(ETF/미국 제외)
            if _code.isdigit() and len(_code) == 6 and _name:
                return _code, _name
    except Exception:
        pass
    return _NIGHT_TARGET_FALLBACK


@st.cache_data(ttl=60, show_spinner=False)
def _fetch_night_radar():
    """[야간 레이더] 나스닥선물·WTI·마이크론·EWY(코스피200 야간 프록시)·환율 — yfinance.
    session_state 미접근(캐시 안전). 실패 항목 None."""
    import yfinance as _yf_nr
    _out = {"nq": None, "wti": None, "mu": None, "ewy": None, "krw": None}

    def _chg(_sym):
        try:
            _h = _yf_nr.Ticker(_sym).history(period="5d")["Close"].dropna()
            return round((float(_h.iloc[-1]) / float(_h.iloc[-2]) - 1) * 100, 2) if len(_h) >= 2 else None
        except Exception:
            return None
    _out["nq"] = _chg("NQ=F")     # 나스닥100 선물
    _out["wti"] = _chg("CL=F")    # WTI 유가
    _out["mu"] = _chg("MU")       # 마이크론(반도체 피어)
    _out["ewy"] = _chg("EWY")     # iShares 한국 ETF = 코스피 야간 방향 프록시
    _out["krw"] = _chg("KRW=X")   # 달러-원
    return _out


def _detect_night_trigger():
    """야간 타격 활성화 게이트 — 지수 하락마감 + 기관 양매도 + 코스피200 폭락(프록시).
    반환 (active:bool, info:dict)."""
    _info = {"kospi": None, "kosdaq": None, "k200": None, "org_sell": None}
    try:
        _q = get_index_quotes()
        _info["kospi"] = (_q or {}).get("코스피", {}).get("등락")
        _info["kosdaq"] = (_q or {}).get("코스닥", {}).get("등락")
    except Exception:
        pass
    try:
        _k2 = kis_get_index("2001")   # 코스피200 = 주간선물 프록시
        _info["k200"] = _k2.get("등락") if _k2 else None
    except Exception:
        pass
    # 기관 양매도: 야간 타겟 종목 기관 순매도 여부(대표 프록시)
    try:
        _iv = _md_investor(_get_night_target()[0])
        _info["org_sell"] = (int(_iv.get('기관', 0)) < 0) if _iv else None
    except Exception:
        pass
    _idx_down = (isinstance(_info["kospi"], (int, float)) and _info["kospi"] < 0
                 and isinstance(_info["kosdaq"], (int, float)) and _info["kosdaq"] < 0)
    _fut_crash = isinstance(_info["k200"], (int, float)) and _info["k200"] <= -2.0
    _active = bool(_idx_down and _fut_crash and (_info["org_sell"] in (True, None)))
    return _active, _info


def _mpop_link(code):
    """삼성증권 mPOP 앱 딥링크(수동 주문용). ⚠️ 스킴은 기기별로 1회 확인 필요."""
    return f"samsungpop://stock?code={code}"   # 미동작 시 앱스토어/웹 링크로 교체


def render_night_strike_mode():
    """🌙 야간 타격 3분할 대시보드. 활성 조건 미충족 시 관망 안내만."""
    with st.expander("🌙 야간 타격 모드 (선취매 종가베팅 · 3분할)", expanded=False):
        _active, _info = _detect_night_trigger()
        _ks = f"코스피 {_info['kospi']:+.2f}%" if isinstance(_info['kospi'], (int, float)) else "코스피 —"
        _k2s = f"선물(K200) {_info['k200']:+.2f}%" if isinstance(_info['k200'], (int, float)) else "선물 —"
        if not _active:
            st.markdown(
                f"<div style='background:#1e293b;border:1px solid #475569;border-radius:10px;padding:10px 14px;text-align:center'>"
                f"<b style='color:#94a3b8'>🌙 야간 타격 조건 미충족 — 오늘은 관망</b>"
                f"<div style='font-size:11px;color:#64748b;margin-top:3px'>{_ks} · {_k2s} · 발동=지수↓마감+기관양매도+선물≤-2%</div></div>",
                unsafe_allow_html=True)
            return
        _r = _fetch_night_radar()
        _nt = _get_night_target()   # [V10.4] 스캔 주도주 동적 연동(하드코딩 제거)
        # [V10.5] 타이틀+설명 단일 행 슬림 배너(리스크온 배너와 통일된 하이엔드 다크 스타일)
        st.markdown(
            f"<div style='background:#3b0764;border:1px solid #a855f7;border-radius:8px;padding:5px 12px;"
            f"margin-bottom:6px;font-size:12px;white-space:nowrap;overflow-x:auto'>"
            f"<b style='color:#e9d5ff'>🌙 야간 타격 모드 활성</b> "
            f"<span style='color:#d8b4fe'>· ({_ks} · {_k2s} 폭락 → 익일 반등 선취매)</span></div>",
            unsafe_allow_html=True)

        def _pf(v):   # 안전 포맷: None → '—'
            return f"{v:+.2f}%" if isinstance(v, (int, float)) else "—"

        def _won(v):
            return f"{v:,}원" if isinstance(v, (int, float)) else "—"

        # ── 세로 콤팩트 재설계(내부 st.columns 제거 → 반쪽폭에서 안 찌그러짐) ──
        _nq, _wti, _mu = _r["nq"], _r["wti"], _r["mu"]
        _ewy, _krw = _r["ewy"], _r["krw"]
        _dot = lambda v, up=True: ("⚪" if not isinstance(v, (int, float)) else "🟢" if ((v > 0) == up) else "🔴")
        _wti_dot = ("⚪" if not isinstance(_wti, (int, float)) else "🔵" if _wti < 0 else "🔴")
        _ewy_dot = ("⚪" if not isinstance(_ewy, (int, float)) else "🟢" if _ewy > 0 else "🔴")
        _krw_dot = ("⚪" if not isinstance(_krw, (int, float)) else "🟢" if _krw <= 0.5 else "🔴")
        _sig1 = (isinstance(_nq, (int, float)) and _nq > 0) and (isinstance(_wti, (int, float)) and _wti < 0) \
                and (isinstance(_mu, (int, float)) and _mu > 0)
        _sig2 = _sig1 and (isinstance(_ewy, (int, float)) and _ewy > 0) and (isinstance(_krw, (int, float)) and _krw <= 0.5)
        # ③ 동적 타겟 데이터
        _px = _sup = _t1 = None
        try:
            _pr = kis_get_price(_nt[0]); _ind = calc_indicators(fetch_ohlcv(_nt[0], 60))
            if _pr and _pr.get('현재가'):
                _px = int(_pr['현재가']); _t1 = int(round(_px * 1.035))
            _sup = int(round(float(_ind['MA20'].iloc[-1])))
        except Exception:
            pass
        _gap = ("▲갭상승" if (isinstance(_ewy, (int, float)) and _ewy > 0)
                else "▼갭하락" if isinstance(_ewy, (int, float)) else "—")

        _HD = "font-size:12px;font-weight:800;color:#c084fc;margin-top:4px"
        _BD = "font-size:11px;color:#cbd5e1;margin:1px 0 3px 0"
        # ① 18:00 레이더
        st.markdown(f"<div style='{_HD}'>① 18:00 레이더 {'🔔' if _sig1 else '·'}</div>"
                    f"<div style='{_BD}'>{_dot(_nq)}나스닥 <b>{_pf(_nq)}</b> · {_wti_dot}WTI <b>{_pf(_wti)}</b> · {_dot(_mu)}MU <b>{_pf(_mu)}</b></div>",
                    unsafe_allow_html=True)
        st.link_button("📲 1차 진입 30%", _mpop_link(_nt[0]), use_container_width=True, disabled=not _sig1)
        # ② 20:00 수급확정
        st.markdown(f"<div style='{_HD}'>② 20:00 수급확정 {'🔔' if _sig2 else '·'}</div>"
                    f"<div style='{_BD}'>{_ewy_dot}EWY <b>{_pf(_ewy)}</b> · {_krw_dot}환율 <b>{_pf(_krw)}</b></div>",
                    unsafe_allow_html=True)
        st.link_button("📲 2차 진입 50%", _mpop_link(_nt[0]), use_container_width=True, disabled=not _sig2)
        # ③ 익일 09:00 탈출 — 동적 타겟 한 줄 요약
        st.markdown(f"<div style='{_HD}'>③ 09:00 탈출 · {_nt[1]}({_nt[0]})</div>"
                    f"<div style='{_BD}'>{_gap} · 현재 {_won(_px)} · 지지 {_won(_sup)} · 익절 <b style='color:#22c55e'>{_won(_t1)}(+3.5%)</b></div>",
                    unsafe_allow_html=True)
        st.caption("트레일링스탑 −2.5% · 구조이탈(지지선-1%) 손절")

        # 🛡️ 서킷브레이커 — expander로 접어 밀도 유지(인풋 세로 스택, 컬럼 중첩 없음)
        with st.expander("🛡️ 오버나잇 절대손실 서킷브레이커", expanded=False):
            _acct = st.number_input("계좌 규모(만원)", min_value=100, value=5000, step=100, key="night_acct") * 10000
            _loss_lim = st.number_input("절대손실 한도(%)", min_value=0.5, value=2.0, step=0.5, key="night_lim") / 100
            _gap_ds = st.number_input("최악 갭하락(%)", min_value=1.0, value=7.0, step=1.0, key="night_gap") / 100
            _worst = _TACTICAL_AMMO * _gap_ds; _cap = _acct * _loss_lim
            if _worst <= _cap:
                st.success(f"✅ 진입 허용 — 최악갭(-{_gap_ds*100:.0f}%) -{_worst:,.0f}원 ≤ 한도 {_cap:,.0f}원")
            else:
                st.error(f"🚫 진입 차단 — -{_worst:,.0f}원 > 한도 {_cap:,.0f}원 · 비중 {_cap/_gap_ds:,.0f}원↓로 축소")


# ══════════════════════════════════════════════════════════════════
# ☀️ 주간 타격 모드 — 정규장 09:00~15:30 실시간 3대 선행지표 타점
# ══════════════════════════════════════════════════════════════════
@st.cache_data(ttl=60, show_spinner=False)
def _fetch_day_indicators():
    """[주간 지표] 달러-원 1분봉(꺾임)·니케이·상해 — yfinance. session_state 미접근(캐시 안전)."""
    import yfinance as _yf_day
    _out = {"krw_last": None, "krw_peak": None, "krw_turn": False, "nikkei": None, "shanghai": None}
    try:
        _h = _yf_day.Ticker("KRW=X").history(period="1d", interval="1m")["Close"].dropna()
        if len(_h) >= 5:
            _last = float(_h.iloc[-1]); _peak = float(_h.max())
            _out["krw_last"] = round(_last, 1); _out["krw_peak"] = round(_peak, 1)
            # 고점 찍고 꺾임: 당일 고점 대비 하락 전환 + 최근 3틱 하락
            _out["krw_turn"] = bool(_last < _peak * 0.999 and _last < float(_h.iloc[-3]))
    except Exception:
        pass
    for _k, _sym in [("nikkei", "^N225"), ("shanghai", "000001.SS")]:
        try:
            _hh = _yf_day.Ticker(_sym).history(period="5d")["Close"].dropna()
            _out[_k] = round((float(_hh.iloc[-1]) / float(_hh.iloc[-2]) - 1) * 100, 2) if len(_hh) >= 2 else None
        except Exception:
            pass
    return _out


def render_day_strike_mode():
    """☀️ 주간 실시간 타점 모드 — 정규장(평일 09:00~15:30) 자동 활성.
    3대 선행지표: ①프로그램/차익수급 턴어라운드 ②환율 1분봉 꺾임 ③아시아 증시 방어력."""
    import datetime as _dtd
    _now = _dtd.datetime.utcnow() + _dtd.timedelta(hours=9)
    _t = _now.time(); _today = _now.strftime("%Y-%m-%d")
    _OPEN = _dtd.time(9, 0); _CLOSE = _dtd.time(15, 30)
    _in_session = (_now.weekday() < 5) and (_OPEN <= _t < _CLOSE)

    with st.expander("☀️ 주간 실시간 타점 모드 (정규장 09:00~15:30)", expanded=_in_session):
        if not _in_session:
            st.caption("☀️ 평일 정규장(09:00~15:30)에 자동 활성화 — 현재는 장 시간 외")
            return
        _pf = lambda v, s="%": f"{v:+.2f}{s}" if isinstance(v, (int, float)) else "—"

        # ── ① 프로그램/차익 수급 턴어라운드(선물 베이시스 프록시, -→+ 전환) ──
        _basis = None
        try:
            _bp = kis_futures_basis_proxy()
            _basis = _bp.get('basis') if _bp else None
        except Exception:
            pass
        _store = st.session_state.setdefault('_day_prog_base', {})
        _c1 = False; _prog_txt = "—"
        if isinstance(_basis, (int, float)):
            _lo = min(_store.get(_today, _basis), _basis); _store[_today] = _lo
            _c1 = (_lo < 0 and _basis > 0)   # 장중 마이너스 → 플러스 전환
            _prog_txt = f"{_basis:+.2f}%p (당일저점 {_lo:+.2f})"
        _d1 = "🟢" if _c1 else ("🔴" if isinstance(_basis, (int, float)) and _basis < 0 else "⚪")

        # ── ② 환율 1분봉 꺾임 / ③ 아시아 증시 ──
        _di = _fetch_day_indicators()
        _c2 = bool(_di["krw_turn"])
        _d2 = "🟢" if _c2 else ("🔴" if _di["krw_last"] is not None else "⚪")
        _nk, _sh = _di["nikkei"], _di["shanghai"]
        _c3 = (isinstance(_nk, (int, float)) and _nk >= 0) or (isinstance(_sh, (int, float)) and _sh >= 0)
        _d3 = "🟢" if _c3 else ("🔴" if (_nk is not None or _sh is not None) else "⚪")

        _HD = "font-size:12px;font-weight:800;color:#fbbf24;margin-top:4px"
        _BD = "font-size:11px;color:#cbd5e1;margin:1px 0 4px 0"
        st.markdown(
            f"<div style='{_HD}'>① 프로그램/차익 수급 {_d1}</div>"
            f"<div style='{_BD}'>베이시스 {_prog_txt} · -→+ 전환 시 점등</div>"
            f"<div style='{_HD}'>② 달러-원 1분봉 꺾임 {_d2}</div>"
            f"<div style='{_BD}'>현재 {(_di['krw_last'] or '—')} · 당일고점 {(_di['krw_peak'] or '—')} · 고점후 하락전환 시 점등</div>"
            f"<div style='{_HD}'>③ 아시아 증시 방어 {_d3}</div>"
            f"<div style='{_BD}'>니케이 <b>{_pf(_nk)}</b> · 상해 <b>{_pf(_sh)}</b></div>",
            unsafe_allow_html=True)

        # ── 핵심 조건: ①프로그램 턴 + ②환율 꺾임 (③은 방어 확인 보조) ──
        _core = _c1 and _c2
        _nt = _get_night_target()
        if _core:
            st.error(f"🎯 **정규장 1차 바닥 타격 신호** — 프로그램 매수전환 + 환율 꺾임"
                     + (" · 아시아 방어 ✅" if _c3 else " · ⚠️아시아 약세 주의"))
        else:
            _need = []
            if not _c1: _need.append("프로그램 +전환")
            if not _c2: _need.append("환율 꺾임")
            st.caption("대기 — 핵심조건 미충족: " + " · ".join(_need))
        st.link_button(f"📲 정규장 1차 바닥 타격 · {_nt[1]} · mPOP", _mpop_link(_nt[0]),
                       use_container_width=True, disabled=not _core)


with tab_c:
    st.markdown("### 📡 V9.1 단기 스윙 스캐너")
    render_macro_weather()            # 🌍 최상단: 프리마켓 매크로 레짐 판독(1줄 배너)
    # 🌙 야간 타격 · 🔴 전술 모드를 가로 2분할로 콤팩트 배치
    with st.container(border=True):
        render_day_strike_mode()          # ☀️ 정규장 09:00~15:30 실시간 타점(자동 활성)
    _tcol1, _tcol2 = st.columns(2, gap="medium")
    with _tcol1:
        with st.container(border=True):
            render_night_strike_mode()
    with _tcol2:
        with st.container(border=True):
            _crash_mode = render_tactical_mode()
    if _crash_mode:
        st.info("🔴 폭락장 전술 모드 — 개별주(만쥬/돌팬티) 매수 스캐너는 자동 차단되었습니다. "
                "하방 타격(곱버스)에 집중하세요.")
    else:
        render_manju_dolpanti_briefing()

    # ── 📖 실전 매뉴얼 (기본 닫힘) ──────────────────────────────────────
    with st.expander("📖 [필독] V9.1 스캐너 운용 수칙 및 매뉴얼", expanded=False):
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

    # ── 진입 금지 통합 배너(단일화) — 대형 카드+중복 경고 제거, 1줄로 압축 ──
    _v891_c = run_v891_system_check()
    from datetime import datetime as _dt_tc
    _kh_c = (_dt_tc.utcnow().hour + 9) % 24
    _km_c = _dt_tc.utcnow().minute
    _tblock_c = (9 <= _kh_c < 10) or (_kh_c == 10 and _km_c <= 30)
    if not _v891_c['can_enter'] or _tblock_c:
        _bc_msg = (_v891_c['alerts'][0] if not _v891_c['can_enter']
                   else "09:00~10:30 변동성 과다 구간")
        _bc_ttl = ("FOMC 대기" if _v891_c.get('blackout')
                   else "진입 금지 구간" if not _v891_c['can_enter'] else "장초 변동성")
        st.markdown(
            f"<div style='background:#2d0a0a;border:1px solid #ef4444;border-radius:8px;"
            f"padding:6px 12px;margin-bottom:6px;font-size:12px;display:flex;justify-content:space-between;align-items:center'>"
            f"<b style='color:#ef4444'>🚫 {_bc_ttl} — 주문 금지</b>"
            f"<span style='color:#fca5a5'>{_bc_msg} · 스캔/복기만 가능</span></div>",
            unsafe_allow_html=True)
    st.caption("하드필터(시총·ATR) + 스코어링(재무·수급·모멘텀·눌림목) — 70점 이상 종목만 포착")


    # ══════════════════════════════════════════
    # 🏛️ 연기금 추종 스캐너 (Gemini V2 설계)
    # ══════════════════════════════════════════
    # 연기금 모드가 선택됐거나 결과 캐시가 있으면 자동 펼침(결과가 서랍에 숨지 않도록)
    _pension_active = ("연기금" in str(st.session_state.get('scan_mode', ''))
                       or bool(st.session_state.get('_pg_cache'))
                       or bool(st.session_state.get('_trigger_pension')))
    with st.expander("🏛️ 연기금 모드 설정 · 결과 (스캔 모드 🏛️ 연기금 선택 시)", expanded=_pension_active):
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
        # 전용 발사 버튼 폐지 → 상단 Control Ribbon [스캔 모드: 🏛️ 연기금] + [🚀 스캔 시작]으로 통합
        _run_pg = bool(st.session_state.pop('_trigger_pension', False))
        st.caption("🚀 상단 Control Ribbon에서 스캔 모드를 **🏛️ 연기금**으로 두고 **[🚀 스캔 시작]**을 누르세요.")

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
                                '현재가':          f"{int(_pg_won_price(_tk, _cur)):,}원",
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
                                '현재가':        f"{int(_pg_won_price(_tk, _cur)):,}원",
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

    # ── 🔬 AI 파라미터 자동 최적화(Walk-Forward)는 스캐너에서 제거 → 추후 관리 탭으로 이전 ──
    #    (스캔은 opt_best_cond5/6 기본값 사용 · opt_applied=False 경로로 정상 동작)

    with st.expander("⚡ 전략 프리셋 & 지표 필터 선택", expanded=False):
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

        # (🔥 AI 최적화 중복 스텁 제거 — 전용 '🔬 AI 파라미터 자동 최적화' Expander로 일원화)

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

    # ── 🎯 스캔 Control Ribbon — 시장 / 종목수 / 모드 / 시작 버튼 1줄 압축 ──
    try:
        _rc1, _rc2, _rc3, _rc4 = st.columns([2.2, 1, 2, 1.3], vertical_alignment="bottom")
    except TypeError:
        _rc1, _rc2, _rc3, _rc4 = st.columns([2.2, 1, 2, 1.3])
    with _rc1:
        market_type = st.selectbox("🌏 대상 시장", _SC_OPTS, key="scanner_market")
    with _rc2:
        top_n = st.slider("종목 수", 20, 300, 100, key="scanner_topn")
    # ── 스캔 모드: 잠금(disabled)·강제고정 완전 제거 — 시장 무관 자유 선택 ──
    #    (ETF 유니버스는 스캔 핸들러의 _IS_ETF_UNIVERSE 가드가 모드와 무관하게 ETF 채점 처리)
    with _rc3:
        scan_mode = st.radio(
            "스캔 모드", ["📈 개별주", "🏦 ETF", "🔀 통합", "🏛️ 연기금"], horizontal=True, key="scan_mode",
            help="🏛️ 연기금은 국내(KOSPI/KOSDAQ) 대상 · 아래 '🏛️ 연기금 모드 설정'에서 세부 조건 지정",
        )
    with _rc4:
        scan_btn = st.button("🚀 스캔 시작", use_container_width=True, type="primary", key="scan_start_btn")

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

    # ── ⚙️ 스캐너 상세 설정 (가격 필터) — 라벨 없이 떠돌던 유령 입력창 라벨화 + 격리 ──
    with st.expander("⚙️ 스캐너 상세 설정 (가격 필터)", expanded=False):
        _pf1, _pf2 = st.columns(2)
        _hidden_mp = _pf1.number_input(
            f"최소 주가 ({'$' if _is_us else '원'})",
            value=st.session_state.get('f_minp', 1 if _is_us else 5000),
            step=1 if _is_us else 1000, key="f_minp",
            help="이 가격 미만 종목은 스캔에서 제외")
        _hidden_mx = _pf2.number_input(
            f"최대 주가 ({'$' if _is_us else '원'})",
            value=st.session_state.get('f_maxp', 100000 if _is_us else 2000000),
            step=100 if _is_us else 10000, key="f_maxp",
            help="이 가격 초과 종목은 스캔에서 제외 (초고가주 배제)")
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
    # (🚀 스캔 시작 버튼은 상단 Control Ribbon으로 이동)

    if scan_btn and "연기금" in scan_mode:
        # 🏛️ 연기금 모드 → 연기금 스캔 로직으로 분기 (상단 블록이 다음 런에서 실행)
        st.session_state['_trigger_pension'] = True
        st.rerun()

    if scan_btn and "연기금" not in scan_mode:
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

        # ══════════════════════════════════════════════════════
        # 🎯 TARGET LOCK-ON — 1~3위 종목 카드 (st.columns(3))
        # ══════════════════════════════════════════════════════
        _top3 = _p_list[:3]
        if _top3:
            _rank_ic = ["🥇", "🥈", "🥉"]
            _tcols = st.columns(len(_top3))
            for _ti, _tx in enumerate(_top3):
                _ttk  = _tx['ticker']
                _tkr  = is_korean_ticker(_ttk)
                _tu   = '원' if _tkr else '$'
                _tcur = float(_tx.get('현재가', 0) or 0)
                _tgrade = _tx.get('등급', '')
                _tscore = _tx.get('점수', _tx.get('score', 0))
                _tadx   = _tx.get('ADX', _tx.get('adx', '-'))
                # ── 연속등장 배지 (스나이퍼 수칙 STEP3: 3일 연속 = 사격 허가) ──
                _tstk = _streak_now.get(str(_ttk), 1)
                if _tstk >= 3:
                    _tstk_txt, _tstk_c = "🟢 3일 연속 (사격)", "#22c55e"
                elif _tstk == 2:
                    _tstk_txt, _tstk_c = "🟡 2일 연속 (대기)", "#fbbf24"
                else:
                    _tstk_txt, _tstk_c = "⚪ 1일차 신규 (보류)", "#94a3b8"
                _t_ep = None
                try:
                    _tdf = st.session_state.get('all_data_cache', {}).get(_ttk, {}).get('df')
                    if _tdf is None and _ttk:
                        _traw = fetch_ohlcv(_ttk, 80)
                        if _traw is not None and len(_traw) >= 20:
                            _tdf = calc_indicators(_traw)
                            st.session_state.setdefault('all_data_cache', {})[_ttk] = {'name': _tx.get('name',''), 'df': _tdf}
                    if _tdf is not None:
                        _t_ep = calc_entry_point(_tdf, st.session_state.get('analysis_preset','bounce'))
                except Exception:
                    _t_ep = None
                _entry_s = f"{_t_ep['entry']:,.0f}" if _t_ep and _t_ep.get('entry') else "-"
                _stop_s  = f"{_t_ep['stoploss']:,.0f}" if _t_ep and _t_ep.get('stoploss') else "-"
                _gc = "#ffd166" if 'S등급' in _tgrade else "#34d399" if 'A등급' in _tgrade else "#60a5fa"
                with _tcols[_ti]:
                    st.markdown(f"""
<div style='background:linear-gradient(160deg,#0f172a,#1a1a2e);border:2px solid {_gc}80;border-radius:14px;
padding:14px 16px;box-shadow:0 0 14px {_gc}25;margin-bottom:6px'>
  <div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:6px'>
    <span style='font-size:15px;font-weight:900;color:{_gc}'>{_rank_ic[_ti]} {_tgrade}</span>
    <span style='font-size:12px;font-weight:700;color:#fbbf24'>{_tscore}점</span>
  </div>
  <div style='display:inline-block;background:{_tstk_c}22;border:1px solid {_tstk_c}77;color:{_tstk_c};
  font-size:11px;font-weight:800;padding:2px 10px;border-radius:10px;margin-bottom:6px'>연속등장 {_tstk_txt}</div>
  <div style='font-size:15px;font-weight:800;color:#f0f4ff'>{_tx.get('name','')}</div>
  <div style='font-size:10px;color:#64748b;margin-bottom:8px'>{_ttk} · {_tcur:,.0f}{_tu} · ADX {_tadx}</div>
  <div style='display:flex;justify-content:space-between;font-size:11px'>
    <span style='color:#fbbf24'>🎯 진입 {_entry_s}</span>
    <span style='color:#f43f5e'>🛑 손절 {_stop_s}</span>
  </div>
</div>""", unsafe_allow_html=True)
                    if st.button("🔍 분석 탭으로 이동", key=f"scan_target_{_ttk}", use_container_width=True):
                        _tnm = _tx.get('name', _ttk)
                        add_ticker(_ttk, _tnm)                    # 분석 탭 드롭다운에 노출
                        st.session_state['scanner_selection'] = _ttk
                        # 분석 탭 종목 드롭다운(b_unified_sel) 사전 선택
                        _disp = f"{_tnm} ({_ttk})" if str(_ttk).isdigit() else f"{_ttk} ({_tnm})"
                        st.session_state['_pending_unified_sel'] = _disp
                        st.toast(f"🔍 {_tnm} → 관심종목 추가 · 상단 '분석' 탭에서 확인", icon="🎯")
            st.divider()

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

        # ── 📂 4위 이하 · 전체 스캔 결과 상세를 Expander로 캡슐화 (평소 접힘) ──
        with st.expander("📂 4위 이하 스캔 결과 · 전체 상세 (대기 종목)", expanded=False):
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
                        # 현재가 = 실제 당일 종가('종가' 컬럼) — 'Close'(영문)는 없어 항상 진입가로 덮이던 버그 수정
                        if '종가' in _df_ov.columns:
                            _ep_cur = float(_df_ov['종가'].iloc[-1])
                        elif 'Close' in _df_ov.columns:
                            _ep_cur = float(_df_ov['Close'].iloc[-1])
                        else:
                            _ep_cur = float(_ep_ov.get('cur') or _sel_item.get('현재가') or _ep_ent)

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


def _render_rampup_panel(df_ranked, rank_history, key_prefix='etf'):
    """🚀 아래서 치고 올라오는 샛별 — 최근 순위 가속도(과거→현재 상승폭) 상위 3.
    당일 순위와 무관하게 rank_delta(=과거순위-현재순위)가 가장 큰 종목을 선취매 힌트로 노출."""
    if not rank_history or '코드' not in getattr(df_ranked, 'columns', []):
        return
    _name_map = df_ranked.set_index(df_ranked['코드'].astype(str))['ETF명'].to_dict()
    _cur_rank = {str(r['코드']): _i + 1 for _i, (_, r) in enumerate(df_ranked.iterrows())
                 if r.get('상태') == '활성'}
    _vel = []
    for _tk, _ranks in rank_history.items():
        if len(_ranks) < 2:
            continue                      # 스냅샷 2개 이상 있어야 가속도 계산
        _cur_r  = _cur_rank.get(str(_tk), _ranks[0])
        _past_r = _ranks[-1]
        _delta  = _past_r - _cur_r
        if _delta > 0:
            _vel.append((str(_tk), _name_map.get(str(_tk), str(_tk)), _past_r, _cur_r, _delta))
    _vel.sort(key=lambda x: x[4], reverse=True)
    _top3 = _vel[:3]
    if not _top3:
        st.caption("🚀 Ramp-up: 순위 스냅샷 누적 중 — 2거래일 이상 데이터가 쌓이면 급상승 종목이 표시됩니다.")
        return
    st.markdown("<div style='font-size:13px;font-weight:800;color:#22c55e;margin:6px 0 2px'>"
                "🚀 아래에서 치고 올라오는 샛별 (Ramp-up TOP 3)</div>", unsafe_allow_html=True)
    st.caption("⚠️ 최근 순위 가속도 최상위 — **1위 달성 전 선취매 검토 가능 구간** (당일 절대순위 무관)")
    _rc = st.columns(len(_top3))
    for _ci, (_tk, _nm, _pr, _cr, _dl) in enumerate(_top3):
        with _rc[_ci]:
            st.markdown(
                f"<div style='background:rgba(34,197,94,0.08);border:1px solid #22c55e66;"
                f"border-radius:10px;padding:10px 12px'>"
                f"<div style='font-size:13px;font-weight:800;color:#f0f4ff'>{_nm}</div>"
                f"<div style='font-size:10px;color:#64748b'>{_tk}</div>"
                f"<div style='font-size:18px;font-weight:900;color:#22c55e;margin-top:4px'>🚀 ▲{_dl}</div>"
                f"<div style='font-size:10px;color:#94a3b8'>{_pr}위 → {_cr}위 (가속)</div>"
                f"</div>", unsafe_allow_html=True)

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

        # 순위 변동 배지 — 다일 누적(가장 오래된 스냅샷 대비) → 아래서 치고 오르는 종목 강조
        _rank_change_html = ""
        if len(_hist_ranks) >= 2:
            _cur_r  = _i + 1
            _past_r = _hist_ranks[-1]          # 최대 7일 전 순위
            _delta  = _past_r - _cur_r         # +면 상승
            _dback  = len(_hist_ranks) - 1
            if _delta >= 3:                    # 3계단 이상 급상승 = 불기둥
                _rank_change_html = (f"<span title='{_past_r}위 → {_cur_r}위 ({_dback}일)' "
                    f"style='color:#22c55e;font-size:10px;font-weight:800;margin-left:4px'>🚀▲{_delta}</span>")
            elif _delta > 0:
                _rank_change_html = (f"<span title='{_past_r}위 → {_cur_r}위' "
                    f"style='color:#34d399;font-size:10px;margin-left:4px'>▲{_delta}</span>")
            elif _delta < 0:
                _rank_change_html = (f"<span title='{_past_r}위 → {_cur_r}위' "
                    f"style='color:#94a3b8;font-size:10px;margin-left:4px'>▼{-_delta}</span>")

        _bg     = '#1a1400' if _is_top else '#111827'
        _macd   = row.get('MACD', '')
        _border_color = '#ffd166' if _is_top else ('#d4a017' if _macd == '골든크로스' else '#c0392b' if _macd == '데드크로스' else '#1e3a5f')
        _cc     = '#ff4d6d' if row['등락(%)'] > 0 else '#4da6ff'
        _ac     = '#4dff91' if row.get('ADX', 0) >= 25 else '#ff4d6d'
        _tag    = ' <span style="background:#ffd166;color:#000;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700">🏆 1위</span>' if _is_top else ''
        # [V10.3 P3] 무음 0값 방지 — 가격 0/무효는 '0원'이 아니라 N/A로 표기
        # (오류행은 위 _is_dead 분기에서 이미 처리됨 → 여기선 활성행 가격 유효성만 검사)
        _cur_val = row.get('현재가', 0)
        if not (isinstance(_cur_val, (int, float)) and _cur_val > 0):
            _price_str = "N/A ⚠️"
        else:
            _price_str = f"{_cur_val:,.2f}{currency_symbol}" if currency_symbol == '$' else f"{_cur_val:,.0f}{currency_symbol}"

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
                # 종목명: 코드(395160) 대신 한글명 매핑 → "KODEX AI반도체TOP2+ (395160)"
                _disp_name = resolve_korean_name(_tk, _pos.get('name', _tk))
                _disp_label = f"{_disp_name} ({_tk})" if _disp_name and _disp_name != _tk else _tk
                st.session_state.setdefault('_live_pos_summary', {})[_tk] = {
                    'name': _disp_label, 'cur': _cur_p, 'pnl': round(_pnl_pct, 2),
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
                    f"<span style='font-size:16px;font-weight:900;color:#f0f4ff'>{_disp_label}{_price_warn}</span>"
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
            _sum_df.style.apply(_tbl_row_style, axis=1)
                         .format({'수익률(%)': '{:+.2f}%'}),   # -19.210000 → -19.21%
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

    # ── 🎯 Control Ribbon: 대상 시장 / 카테고리 / 새로고침 / 강제 갱신 1줄 통합 ──
    _kr_sc = st.session_state.get('_kr_top_score')
    _us_sc = st.session_state.get('_us_top_score')
    def _fmt_etf_market(_opt):
        if _kr_sc is None or _us_sc is None:
            return _opt
        if _opt == "🇰🇷 국장 ETF" and _kr_sc > _us_sc:
            return f"{_opt} (🔥 우위 {int(_kr_sc)})"
        if _opt == "🇺🇸 미장 ETF" and _us_sc > _kr_sc:
            return f"{_opt} (🔥 우위 {int(_us_sc)})"
        return _opt
    try:
        _erc1, _erc2, _erc3, _erc4 = st.columns([2.4, 2, 1, 1.2], vertical_alignment="bottom")
    except TypeError:
        _erc1, _erc2, _erc3, _erc4 = st.columns([2.4, 2, 1, 1.2])
    with _erc1:
        _etf_market = st.radio("대상 시장", ["🇰🇷 국장 ETF", "🇺🇸 미장 ETF"],
                               format_func=_fmt_etf_market, horizontal=True, key="etf_market_sel")
    with _erc2:
        if _etf_market == "🇰🇷 국장 ETF":
            st.selectbox("카테고리", ["전체","국내지수","미국지수추종","반도체/IT","방산/중공업",
                                     "에너지/전력","2차전지","금/원자재","채권","배당","헬스케어"],
                         key="kr_etf_cat")
        else:
            st.selectbox("카테고리", ["전체","주요지수","섹터","테마/성장","방산","에너지/원자재",
                                     "채권","레버리지/인버스","배당","국제"], key="us_etf_cat")
    with _erc3:
        if st.button("🔄 새로고침", key="etf_ribbon_refresh", use_container_width=True,
                     help="현재 시장 랭킹 데이터만 새로 불러옵니다"):
            (fetch_kr_etf_data if _etf_market == "🇰🇷 국장 ETF" else fetch_us_etf_data).clear()
            st.rerun()
    with _erc4:
        _last_rf = st.session_state.get('_etf_refresh_ts')
        st.button("🔄 강제 갱신", key="etf_force_refresh_btn", on_click=_force_refresh_etf,
                  use_container_width=True,
                  help=("전체 캐시를 비우고 최신 호가 재조회 (시세 멈춤 시 사용·TTL 60초)"
                        + (f" · 마지막 {_last_rf}" if _last_rf else "")))

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
    def fetch_kr_etf_data(etf_universe: tuple):
        # [D3] etf_universe를 명시적 인자(캐시 키)로 수신 — 전역 _KR_ETF_LIST 의존 제거(캐시 오염 차단)
        results = []
        _mismatch_log = []
        # batch download (rate-limit 회피) — 실패 시 개별 호출로 자동 폴백
        _kr_syms = [f"{t}.KS" for t, _ in etf_universe]
        _kr_batch = _batch_download_ohlcv(_kr_syms)
        for ticker, name in etf_universe:
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
    def fetch_us_etf_data(etf_universe: tuple):
        # [D3] etf_universe를 명시적 인자(캐시 키)로 수신 — 전역 _US_ETF_LIST 의존 제거(캐시 오염 차단)
        results = []
        # batch download (rate-limit 회피) — 56개 1회 요청, 실패 시 개별 폴백
        _us_syms = [t for t, _ in etf_universe]
        _us_batch = _batch_download_ohlcv(_us_syms)
        for ticker, name in etf_universe:
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
        # (새로고침/카테고리는 상단 Control Ribbon으로 통합 이관)
        with st.spinner("국장ETF 데이터 로딩 중..."):
            try:
                _kr_data = _normalize_kr_etf_prices(fetch_kr_etf_data(tuple(_KR_ETF_LIST)))   # [C3]왜곡보정 [D3]universe주입
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

            _kr_cat = st.session_state.get("kr_etf_cat", "전체")   # 상단 리본에서 선택

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
            _render_rampup_panel(_kr_ranked, _kr_rh, key_prefix='kr_etf')
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

            # (🔫 개별종목 스나이핑 삭제 — 전용 스캐너/분석 탭에서 수행, ETF 로테이션엔 불필요)

    elif _etf_market == "🇺🇸 미장 ETF":
        # (새로고침/카테고리는 상단 Control Ribbon으로 통합 이관)
        _us_cat_options = ["전체", "주요지수", "섹터", "테마/성장", "방산", "에너지/원자재", "채권", "레버리지/인버스", "배당", "국제"]
        _us_cat = st.session_state.get("us_etf_cat", "전체")   # 상단 리본에서 선택

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
                _us_data = fetch_us_etf_data(tuple(_US_ETF_LIST))   # [D3] universe 주입
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
            _render_rampup_panel(_us_ranked, _us_rh, key_prefix='us_etf')
            _render_etf_ranking(_us_ranked, currency_symbol='$', key_prefix='us_etf', show_add_btn=True, rank_history=_us_rh)
            st.caption("종합점수 = ADX(25) + RSI(15) + MACD(20) + Z-Score(15) + 모멘텀(15) + 정배열(10) + 거래량(10) | ADX 25미만 자동 탈락")

            # (🔫 개별종목 스나이핑 삭제 — 전용 스캐너/분석 탭에서 수행)

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
                _kr_data_all = _normalize_kr_etf_prices(fetch_kr_etf_data(tuple(_KR_ETF_LIST)))   # [C3]왜곡보정 [D3]universe주입
                _us_data_all = fetch_us_etf_data(tuple(_US_ETF_LIST))   # [D3] universe 주입
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
            _etf_data = _normalize_kr_etf_prices(fetch_etf_data(tuple(ETF_LIST)))   # [C3] 10배 왜곡 보정(캐시 밖)
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
        _dc = 0            # 연속 1위 일차 — 신규 진입 게이트 공용(try 실패 대비 기본값)
        _switch_ok = False # 방어 3조건 통과 여부
        if _top1 is not None:
            try:
                _day_info = _get_rotation_day_count(
                    str(_top1['종목코드']),
                    "KR" if _etf_market == "🇰🇷 국장 ETF" else "US")   # [V9.28] 국/미장 상태공간 격리
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

            # ── 🚦 3거래일 연속 1위 게이트 — 1·2일차는 매수 제안 절대 차단(Whipsaw 방지) ──
            _ready = (_dc >= 3)
            _panel_c  = "#34d399" if _ready else "#fbbf24"
            _panel_bg = "rgba(52,211,153,0.07)" if _ready else "rgba(251,191,36,0.07)"
            _panel_bd = "rgba(52,211,153,0.25)" if _ready else "rgba(251,191,36,0.30)"
            _panel_title = (f"🟢 신규 진입 추천 (현재 1위 · {_etf_market})" if _ready
                            else f"🟡 신규 진입 대기 (카운트 누적 중 · 연속 1위 {_dc}일차)")
            st.markdown(f"""
<div style='background:{_panel_bg};border:1px solid {_panel_bd};border-radius:12px;padding:16px 20px;margin-bottom:12px'>
  <div style='font-size:13px;color:{_panel_c};font-weight:700;margin-bottom:8px'>{_panel_title}</div>
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

            if not _ready:
                # 1·2일차 → 매수 유도 문구 완전 차단, 관망 강제
                st.warning(
                    "⚠️ 아직 3거래일 연속 1위 조건이 충족되지 않았습니다. "
                    "Whipsaw 방지를 위해 오늘 장은 매수를 집행하지 않고 절대 관망합니다.")
            elif _pullback:
                st.warning(
                    f"⏳ 3일 연속 조건 충족 · 현재 **눌림목 대기 상태**입니다. "
                    f"오늘 밤 미국 장에 타점가 **{_fmt_e(_t1_ent)}**로 지정가 예약 매수를 걸어두십시오.")
            elif not _t1_kr:
                st.success(f"✅ 3일 연속 조건 충족 · 현재가가 타점({_fmt_e(_t1_ent)}) 이하 — 진입 유효 구간입니다.")
            else:
                st.success(f"✅ 3일 연속 조건 충족 — 09:30 이후 타점 {_fmt_e(_t1_ent)} 진입 검토 구간입니다.")

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
            # ── 국장/미장 완전 분리: 벤치마크 지수·비용을 대상 시장 라디오에 연동 ──
            _bt_is_us   = "미장" in str(_etf_market)
            _bench_name = "S&P500" if _bt_is_us else "코스피"
            _bench_sym  = "^GSPC" if _bt_is_us else "^KS11"
            st.caption(f"1위 ETF에 매월 스위칭 전략 vs **{_bench_name}** 수익률 비교 · "
                       f"대상 시장: {'🇺🇸 미장 ETF' if _bt_is_us else '🇰🇷 국장 ETF'}")

            # 수수료/세금 설정 UI (시장별 기본값·키 분리)
            st.markdown("#### ⚙️ 백테스팅 비용 설정")
            _bt_c1, _bt_c2, _bt_c3 = st.columns(3)
            if _bt_is_us:
                _def_buy, _def_sell, _def_slip = 0.100, 0.100, 0.05
                _buy_help  = "해외주식 매매 수수료 (보통 0.1%)"
                _sell_help = "해외주식 매도 수수료 + SEC/ECN fee 등 (≈0.1%)"
            else:
                _def_buy, _def_sell, _def_slip = 0.015, 0.330, 0.10
                _buy_help  = "증권사 수수료 (보통 0.015%)"
                _sell_help = "수수료 0.015% + 거래세 0.18% + 농특세 0.15% ≈ 0.33%"
            _mk = "us" if _bt_is_us else "kr"
            _fee_buy  = _bt_c1.number_input("매수 수수료(%)", value=_def_buy, step=0.005,
                                             format="%.3f", key=f"bt_fee_buy_{_mk}", help=_buy_help)
            _fee_sell = _bt_c2.number_input("매도 수수료+세금(%)", value=_def_sell, step=0.01,
                                             format="%.3f", key=f"bt_fee_sell_{_mk}", help=_sell_help)
            _slip     = _bt_c3.number_input("슬리피지(%)", value=_def_slip, step=0.05,
                                             format="%.2f", key=f"bt_slip_{_mk}",
                                             help="호가 공백 오차 (보통 0.05~0.2%)")

            # 총 거래비용 (매수+매도 합산)
            _total_cost = (_fee_buy + _fee_sell + _slip * 2) / 100
            st.caption(f"💡 스위칭 1회당 총 비용: 약 {(_fee_buy + _fee_sell + _slip*2):.3f}% "
                       f"(매수 {_fee_buy+_slip:.3f}% + 매도 {_fee_sell+_slip:.3f}%)")

            @st.cache_data(ttl=86400, show_spinner=False)
            def run_etf_backtest(fee_buy, fee_sell, slip, bench_sym="^KS11", is_us=False, etf_universe=()):
                # etf_universe: 대상 시장 ETF 튜플(캐시 키에 포함 → 국장/미장 유니버스 혼선 차단)
                import yfinance as yf
                import numpy as np

                _buy_cost  = (fee_buy  + slip) / 100
                _sell_cost = (fee_sell + slip) / 100

                # 각 ETF 월별 수익률 — 배치 다운로드(순차 호출 rate-limit 회피).
                # 한국 6자리=.KS, 미국 티커=접미사 없음.
                def _bt_sym(_t):
                    return f"{_t}.KS" if (str(_t).isdigit() and len(str(_t)) == 6) else str(_t)
                _sym_map_bt = {_bt_sym(t): (t, name) for t, name, _ in etf_universe}
                _all_syms_bt = list(_sym_map_bt.keys())
                _monthly = {}
                try:
                    _batch_bt = yf.download(_all_syms_bt, period="2y", interval="1mo",
                                            group_by="ticker", progress=False, threads=True)
                except Exception:
                    _batch_bt = None
                for _sym, (ticker, name) in _sym_map_bt.items():
                    try:
                        if _batch_bt is None:
                            _df = yf.Ticker(_sym).history(period="2y", interval="1mo")
                            _cl = _df['Close'] if _df is not None else None
                        else:
                            _sub = _batch_bt[_sym] if len(_all_syms_bt) > 1 else _batch_bt
                            _cl = _sub['Close'].dropna()
                        if _cl is None or len(_cl) < 6:
                            continue
                        _ret = _cl.pct_change().dropna()
                        if len(_ret) >= 4:
                            _monthly[ticker] = {'name': name, 'returns': _ret}
                    except Exception:
                        pass

                # 벤치마크 — 국장=^KS11, 미장=^GSPC/^IXIC (대상 시장 연동). yfinance 실패 시 폴백.
                _bm_ret = None
                try:
                    _bm_df  = yf.Ticker(bench_sym).history(period="2y", interval="1mo")
                    if _bm_df is not None and len(_bm_df) >= 4:
                        _bm_ret = _bm_df['Close'].pct_change().dropna()
                except Exception:
                    _bm_ret = None
                if _bm_ret is None or len(_bm_ret) < 4:
                    if not is_us:
                        # 국장 코스피 월봉 수익률 2중 폴백: ① FDR(날짜 지정) → ② pykrx
                        _end_bt = datetime.utcnow() + timedelta(hours=9)
                        _start_bt = _end_bt - timedelta(days=760)
                        # ① FinanceDataReader (명시적 2년 구간)
                        try:
                            import FinanceDataReader as _fdr_bt
                            _bm_d = _fdr_bt.DataReader("KS11", _start_bt.strftime('%Y-%m-%d'),
                                                       _end_bt.strftime('%Y-%m-%d'))
                            _bm_m = _bm_d['Close'].resample('M').last().dropna()
                            if len(_bm_m) >= 4:
                                _bm_ret = _bm_m.pct_change().dropna()
                        except Exception:
                            pass
                        # ② pykrx 코스피 지수(1001)
                        if _bm_ret is None or len(_bm_ret) < 4:
                            try:
                                from pykrx import stock as _pk_bt
                                _pk_df = _pk_bt.get_index_ohlcv(_start_bt.strftime('%Y%m%d'),
                                                                _end_bt.strftime('%Y%m%d'), "1001")
                                if _pk_df is not None and not _pk_df.empty:
                                    _pk_m = _pk_df['종가'].resample('M').last().dropna()
                                    if len(_pk_m) >= 4:
                                        _bm_ret = _pk_m.pct_change().dropna()
                            except Exception:
                                pass
                    else:
                        # 미장: ^GSPC 실패 시 ^IXIC(나스닥)로 재시도
                        try:
                            _alt = "^IXIC" if bench_sym != "^IXIC" else "^GSPC"
                            _bm_df2 = yf.Ticker(_alt).history(period="2y", interval="1mo")
                            if _bm_df2 is not None and len(_bm_df2) >= 4:
                                _bm_ret = _bm_df2['Close'].pct_change().dropna()
                        except Exception:
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
                _bt = run_etf_backtest(_fee_buy, _fee_sell, _slip, _bench_sym, _bt_is_us,
                                       tuple(tuple(_x) for _x in ETF_LIST))

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
                    f"<div class='metric-card'><div class='label'>{_bench_name} 수익률</div>"
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
                    name=_bench_name,
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
                            f'{_bench_name} 누적(%)': f"{_b:+.2f}%",
                        })
                    # 이번 달(진행 중) 추천 — 월봉 미완성이라 누적 수익률은 '진행중'
                    _np = _bt.get('next_pick')
                    if _np and _np.get('name'):
                        _hist_rows.append({
                            '월': f"{_np.get('month','')} (진행중)",
                            '선택 ETF': f"🎯 {_np['name']}",
                            '전략 누적(%)': "집계중",
                            f'{_bench_name} 누적(%)': "집계중",
                        })
                    st.dataframe(pd.DataFrame(_hist_rows), use_container_width=True, hide_index=True)
                    if _np and _np.get('name'):
                        st.caption(f"🎯 이번 달({_np.get('month','')}) 추천: **{_np['name']}** — "
                                   "월봉이 끝나야 수익률이 확정되므로 누적은 '집계중'으로 표시됩니다.")

                if st.button("🔄 백테스팅 재실행", key="bt_rerun"):
                    run_etf_backtest.clear()
                    st.rerun()
            else:
                st.warning("⏳ 백테스팅 데이터 로드 실패 — yfinance 월봉 조회가 일시 지연/차단된 상태입니다 "
                           "(백테스팅 기능은 정상, 데이터만 미수신). 아래 버튼으로 재시도하세요.")
                if st.button("🔄 백테스팅 데이터 재시도", key="bt_retry_empty"):
                    run_etf_backtest.clear()
                    st.rerun()

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
            _ws = get_gsheet(); _sh_ok = _ws is not None
            _sh_msg = (st.secrets.get("SHEET_ID","")[:16] + "…") if _sh_ok else "SHEET_ID/gcp 키 누락"
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

        _mid_r = st.container()  # (관심종목 추가/리스트는 사이드바 Watchlist와 100% 중복 → 제거)


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
                            _sc  = _sdf['종가'].iloc[-1]; _sp = _sdf['종가'].iloc[-2]
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
                            _sc2  = _sdf2['종가'].iloc[-1]; _sp2 = _sdf2['종가'].iloc[-2]
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
        _pa1.markdown(f"<div class='metric-card pa-metric'><div class='label'>초기자본</div><div class='value flat'>{_acc['initial']:,.0f}원</div></div>", unsafe_allow_html=True)
        _pa2.markdown(f"<div class='metric-card pa-metric'><div class='label'>현금잔고</div><div class='value flat'>{_acc['cash']:,.0f}원</div></div>", unsafe_allow_html=True)
        _pa3.markdown(f"<div class='metric-card pa-metric'><div class='label'>총평가금액</div><div class='value flat'>{_total_val:,.0f}원</div></div>", unsafe_allow_html=True)
        _pnl_c = 'up' if _pnl >= 0 else 'down'
        _pa4.markdown(f"<div class='metric-card pa-metric'><div class='label'>총손익</div><div class='value {_pnl_c}'>{_pnl:+,.0f}원<br>({_pnl_pct:+.2f}%)</div></div>", unsafe_allow_html=True)
        _mdd_c = 'down' if _mdd < -5 else 'flat'
        _pa5.markdown(f"<div class='metric-card pa-metric'><div class='label'>MDD</div><div class='value {_mdd_c}'>{_mdd:.2f}%</div></div>", unsafe_allow_html=True)

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

        # ── 🛠️ 가상 매수 실행을 Expander로 격리 (평소 시야 안 가림) ──
        with st.expander("🛠️ 가상 매수 실행 (수동)", expanded=False):
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

            # 위젯 key(buy_memo) 초기화는 위젯 생성 '전'에만 합법 → pending 플래그로 처리
            # (매수 직후 위젯키를 직접 덮어쓰면 StreamlitAPIException 크래시 — b_unified_sel과 동일 패턴)
            if st.session_state.pop('_buy_memo_clear', False):
                st.session_state['buy_memo'] = ''
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
                st.session_state['_buy_memo_clear'] = True   # 다음 run(위젯 생성 전)에 초기화 예약
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

        # ── 환율 경고 배너 (D4: 60초 캐시로 리런마다 yfinance 호출 병목 제거) ──
        _krw = _fetch_current_fx_rate()
        if isinstance(_krw, (int, float)):
            if _krw >= FX_USD_HEDGE:
                st.error(f"🚨 환차손 헷지 경고! 원/달러 환율 {_krw:,.1f}원 — 1,500원 돌파! 미국 주식 신규 진입 자제 및 환헷지 검토 필요")
            elif _krw >= FX_WARN:
                st.warning(f"⚠️ 환율 주의 — 원/달러 {_krw:,.1f}원 (1,500원 경계 접근 중)")

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

        # [V10.2 P1] 이벤트 마커 — 홈 탭 macro_events(2026 최신)와 일원화. 미로드 시 2026 하반기 폴백.
        #   → 하드코딩 2024~2025 날짜가 현재(2026) 차트에서 소멸되던 버그 수정.
        _mev = st.session_state.get('macro_events')
        if isinstance(_mev, list) and _mev:
            _EVENT_DATES = [(str(_e.get('date', '')), str(_e.get('name', '이벤트')))
                            for _e in _mev if isinstance(_e, dict) and _e.get('date')]
        else:
            _EVENT_DATES = [
                ("2026-07-15", "CPI"),   ("2026-07-17", "금통위"), ("2026-07-30", "FOMC"),
                ("2026-08-07", "NFP"),   ("2026-08-12", "CPI"),   ("2026-08-28", "금통위"),
                ("2026-09-11", "CPI"),   ("2026-09-17", "FOMC"),  ("2026-10-15", "CPI"),
                ("2026-10-29", "FOMC"),  ("2026-11-13", "CPI"),   ("2026-12-10", "FOMC"),
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
                        # 실제 토큰 상태로 정직하게 진단 (옛 _k_t 세션키 오판 제거)
                        _tok_now = kis_get_token()
                        _has_acc = False
                        try:
                            _has_acc = bool(st.secrets.get("KIS_ACCOUNT_NO"))
                        except Exception:
                            _has_acc = False
                        if not _tok_now:
                            _terr2 = st.session_state.get('_kis_token_err', '키 확인 필요')
                            st.error(f"❌ KIS 토큰 발급 실패 — {_terr2}")
                        elif not _has_acc:
                            st.warning("⚠️ 잔고 조회에는 **KIS_ACCOUNT_NO(계좌번호)**가 필요합니다 — "
                                       "secrets에 추가하면 실제 잔고가 표시됩니다. (시세·수급 기능은 정상)")
                        else:
                            st.info("⏳ 잔고 조회 일시 지연 — 상단 [🔄 실시간 갱신]을 누르면 정상 표시됩니다.")

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
                _edf = fetch_ohlcv(_et, 60)   # 기본 60거래일 (글로벌 lookback 제거)
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
            # [V10.3 P3] 무음 0값 방지 — 조회 실패(가격 0/무효)를 $0.00·▼0.00%로 위장하지 않고 N/A 표기
            _dvalid = isinstance(_dprice, (int, float)) and _dprice > 0
            _dc = _info['color']
            _chg_c = ("#64748b" if not _dvalid
                      else ("#166534" if _lm_h else "#39ff14") if _dchg >= 0
                      else ("#991B1B" if _lm_h else "#ff003c"))
            _price_html   = f"${_dprice:.2f}" if _dvalid else "N/A"
            _chg_html     = (f"{'▲' if _dchg >= 0 else '▼'}{abs(_dchg):.2f}%" if _dvalid else "⚠️ 확인 필요")
            _monthly_html = f"月 ${_monthly_est:.2f}/주" if _dvalid else "月 —"
            _div_cols[_di].markdown(
                f"<div style='background:#0d1117;border:2px solid {_dc}30;border-radius:12px;padding:12px 14px;text-align:center'>"
                f"<div style='font-size:14px;font-weight:800;color:{_dc}'>{_sym}</div>"
                f"<div style='font-size:9px;color:#64748b;margin-bottom:8px'>{_info['name'][:12]}</div>"
                f"<div style='font-size:16px;font-weight:700;color:#f0f4ff'>{_price_html}</div>"
                f"<div style='font-size:11px;color:{_chg_c};margin:2px 0'>{_chg_html}</div>"
                f"<div style='border-top:1px solid #1e293b;margin-top:8px;padding-top:8px'>"
                f"<div style='font-size:9px;color:#64748b'>예상 배당수익률</div>"
                f"<div style='font-size:13px;font-weight:800;color:#fbbf24'>{_info['yield']:.1f}%</div>"
                f"<div style='font-size:9px;color:#64748b;margin-top:2px'>{_info['freq']}</div>"
                f"<div style='font-size:10px;color:#39ff14;margin-top:4px'>{_monthly_html}</div>"
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
