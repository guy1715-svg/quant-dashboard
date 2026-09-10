# ============================================================
# 퀀트 관제탑 — macro_watcher.py 신호 뷰어 (V26 전면 재구축)
#
# 설계 원칙: 이 대시보드는 신호를 "다시 계산"하지 않는다.
#   - 실시간 판단(매크로 국면·홀딩판정·재료등급·장세판독·변동성스캔)은
#     macro_watcher.py의 함수를 그대로 import해서 호출한다(재구현 금지).
#   - 집계 데이터(신호별 성적·종배픽 기록·시장복기)는 macro_watcher.py가
#     이미 쓰고 있는 파일(signal_scorecard.json 등)을 직접 읽어 표시한다.
#   → macro_watcher.py를 고치면 대시보드도 자동으로 최신 로직을 반영한다.
#
# 실행: streamlit run quant_dashboard.py
# 설치: pip install -r requirements.txt
# ============================================================
import os
import sys
import json
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timedelta

import streamlit as st
import pandas as pd

st.set_page_config(page_title="퀀트 관제탑", page_icon="📊", layout="wide",
                    initial_sidebar_state="expanded")

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
import macro_watcher as mw  # noqa: E402  (신호 로직의 유일한 출처 — 재구현하지 않고 그대로 재사용)


# ══════════════════════════════════════════
# 🔐 다중 사용자 인증 (기존 그대로 유지)
#
# secrets.toml 설정 예시:
#   [users.guy]
#   password = "내비밀번호"
# ※ 구버전 호환: [auth] password = "..." 도 계속 지원(단일 사용자 "default")
# ══════════════════════════════════════════

def _get_user_db() -> dict:
    try:
        _users_cfg = dict(st.secrets.get("users", {}))
        if _users_cfg:
            return {u: dict(v).get("password", "") for u, v in _users_cfg.items()}
    except Exception:
        pass
    try:
        _pw = st.secrets.get("auth", {}).get("password", "")
        if _pw:
            return {"default": _pw}
    except Exception:
        pass
    return {}


_AUTH_TOKEN_DAYS = 14


def _make_auth_token(uid: str, pw: str) -> str:
    import hmac, hashlib, base64, time
    _exp = int(time.time()) + _AUTH_TOKEN_DAYS * 86400
    _msg = f"{uid}.{_exp}"
    _sig = hmac.new(pw.encode(), _msg.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{_msg}.{_sig}".encode()).decode()


def _verify_auth_token(token: str, user_db: dict):
    import hmac, hashlib, base64, time
    try:
        _raw = base64.urlsafe_b64decode(token.encode()).decode()
        _uid, _exp, _sig = _raw.rsplit(".", 2)
        if int(_exp) < int(time.time()):
            return None
        _pw = user_db.get(_uid, "")
        if not _pw:
            return None
        _expect = hmac.new(_pw.encode(), f"{_uid}.{_exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(_sig, _expect):
            return _uid
    except Exception:
        pass
    return None


def _current_username() -> str:
    return st.session_state.get("_username", "default") or "default"


def _check_auth() -> bool:
    if st.session_state.get("_auth_ok"):
        return True
    _user_db = _get_user_db()
    if not _user_db:
        st.session_state["_auth_ok"] = True
        st.session_state["_username"] = "default"
        return True
    try:
        _tok = st.query_params.get("t", "")
    except Exception:
        _tok = ""
    if _tok:
        _uid_ok = _verify_auth_token(_tok, _user_db)
        if _uid_ok:
            st.session_state["_auth_ok"] = True
            st.session_state["_username"] = _uid_ok
            return True
    _is_multi = len(_user_db) > 1
    st.markdown(
        "<div style='text-align:center;margin:40px 0 8px'>"
        "<div style='font-size:48px;margin-bottom:12px'>📊</div>"
        "<div style='font-size:24px;font-weight:900;margin-bottom:6px'>퀀트 관제탑</div>"
        "<div style='font-size:13px;color:#64748b;margin-bottom:20px'>접근 권한이 필요합니다</div>"
        "</div>", unsafe_allow_html=True)
    if _is_multi:
        _inp_user = st.text_input("사용자 ID", placeholder="아이디를 입력하세요",
                                   label_visibility="collapsed", key="_auth_user_input")
    else:
        _inp_user = list(_user_db.keys())[0]
    _inp_pw = st.text_input("비밀번호", type="password", placeholder="비밀번호를 입력하세요",
                             label_visibility="collapsed", key="_auth_pw_input")
    if st.button("🔓 입장", use_container_width=True, type="primary", key="_auth_login_btn"):
        _uid = _inp_user.strip().lower() if _inp_user else ""
        _expected_pw = _user_db.get(_uid, "")
        if _uid and _expected_pw and _inp_pw == _expected_pw:
            st.session_state["_auth_ok"] = True
            st.session_state["_username"] = _uid
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


# ══════════════════════════════════════════
# Firebase (로그인 인프라 유지 — 그 외 용도는 사용 안 함)
# ══════════════════════════════════════════
try:
    import firebase_admin
    from firebase_admin import credentials as fb_credentials
except Exception:
    firebase_admin = None


@st.cache_resource(show_spinner=False)
def _get_firebase_app():
    if firebase_admin is None:
        return None
    try:
        if not firebase_admin._apps:
            _fb_cfg = st.secrets.get("firebase")
            _fb_conf = st.secrets.get("firebase_config")
            if not _fb_cfg or not _fb_conf or not (_fb_conf.get("database_url") if hasattr(_fb_conf, "get") else None):
                return None
            firebase_admin.initialize_app(fb_credentials.Certificate(dict(_fb_cfg)),
                                           {"databaseURL": _fb_conf["database_url"]})
        return firebase_admin.get_app()
    except Exception:
        return None


# ══════════════════════════════════════════
# 공용 유틸 — 파일 읽기 · KIS 연결
# ══════════════════════════════════════════

def _read_json_safe(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0


@st.cache_data(ttl=60, show_spinner=False)
def _kis_ready(_bust: float):
    """(token, key, secret) 또는 (None,None,None). _bust는 캐시 강제 갱신용(호출부에서 time.time()//60 전달)."""
    key, secret = mw.read_kis_keys()
    if not (key and secret):
        return None, None, None
    token = mw.kis_token(key, secret)
    return token, key, secret


def _kis():
    import time as _t
    return _kis_ready(_t.time() // 60)


SEV_BADGE = {0: "🟢", 1: "🟡", 2: "🔴"}
GRADE_BADGE = {"S": "🔥S", "A": "🟢A", "A-": "🟢A-", "B": "🔵B", "C": "🟠C", "D": "⚪D"}


def _pct_color(x):
    if x is None:
        return "gray"
    return "#e74c3c" if x < 0 else ("#2ecc71" if x > 0 else "#888")


# ══════════════════════════════════════════
# 사이드바 — 매크로 국면 상시 표시
# ══════════════════════════════════════════
with st.sidebar:
    st.markdown("### 🧭 매크로 국면")
    _tok, _key, _sec = _kis()
    if _tok:
        try:
            sev, mtext, mdetail, dstate = mw.compute_macro(_tok, _key, _sec)
            st.markdown(f"**{SEV_BADGE.get(sev, '⚪')} {mtext}**")
            st.caption(mdetail.replace("\n", "  \n"))
            if dstate == "outage":
                st.warning("⚠️ 지표 조회 실패 — 데이터 장애(실제 리스크오프 여부 불명)")
            elif dstate == "partial":
                st.caption("※ 일부 지표 결측")
        except Exception as _e:
            st.error(f"매크로 조회 실패: {type(_e).__name__}")
    else:
        st.info("KIS 키 없음 — 국면 조회 불가")
    st.divider()
    st.caption(f"👤 {_current_username()}")
    if st.button("로그아웃", use_container_width=True):
        st.session_state.clear()
        try:
            st.query_params.clear()
        except Exception:
            pass
        st.rerun()


st.title("📊 퀀트 관제탑")
st.caption("macro_watcher.py가 실제로 텔레그램에 보내는 판단을 그대로 보여줍니다 — 별도 계산 없음.")


# ══════════════════════════════════════════
# 탭 1 — 🏠 오늘 현황
# ══════════════════════════════════════════

def render_home():
    today = datetime.now().strftime("%Y-%m-%d")
    scorecard = _read_json_safe(mw.SCORECARD_FILE, [])
    if not isinstance(scorecard, list):
        scorecard = []

    c1, c2, c3 = st.columns(3)
    today_rows = [r for r in scorecard if r.get("date") == today]
    c1.metric("오늘 발생 신호", f"{len(today_rows)}건")
    holdings = mw._read_holdings()
    c2.metric("보유종목", f"{len(holdings)}종")
    watch = mw._read_my_watch()
    c3.metric("관심종목", f"{len(watch)}종")

    st.subheader("💼 보유종목 요약")
    if not holdings:
        st.caption("my_holdings.json에 등록된(on:true) 보유종목이 없습니다.")
    else:
        _tok, _key, _sec = _kis()
        if not _tok:
            st.info("KIS 키 없음 — 실시간가 조회 불가")
        else:
            cols = st.columns(min(4, len(holdings)))
            for i, s in enumerate(holdings):
                code = str(s.get("code", "")).zfill(6)
                avg = s.get("avg") or 0
                with cols[i % len(cols)]:
                    try:
                        px, chg, _ = mw._price_and_turnover(_tok, _key, _sec, code)
                    except Exception:
                        px = None
                    if px and avg:
                        ret = (px / avg - 1) * 100
                        st.metric(s.get("name", code), f"{px:,}", f"{ret:+.1f}%")
                    else:
                        st.metric(s.get("name", code), "조회실패")

    st.subheader("🕒 오늘 신호 타임라인 (최신순)")
    if not today_rows:
        st.caption("오늘 기록된 신호가 없습니다(감시가 켜져 있어야 쌓입니다).")
    else:
        df = pd.DataFrame(sorted(today_rows, key=lambda r: r.get("t", ""), reverse=True))
        st.dataframe(df[["t", "kind", "name", "code", "px"]], use_container_width=True, hide_index=True)


# ══════════════════════════════════════════
# 탭 2 — 📊 성과 분석  (_analyze_history / _analyze_exit_timing과 동일 로직을 화면용으로 재현)
# ══════════════════════════════════════════

_KMAP = {"dolpanty": "종배픽(NXT)", "dolpanty_nonxt": "종배픽(NXT미거래)",
         "dolpanty_div": "종배분산", "dolpanty_shadow": "종배그림자"}


@st.cache_data(ttl=1800, show_spinner="신호별 성적 계산 중...")
def _compute_signal_stats(_bust):
    tok, key, sec = _kis()
    if not tok:
        return None, "KIS 키 없음"
    rows = _read_json_safe(mw.SCORECARD_FILE, [])
    if not isinstance(rows, list):
        rows = []
    for p in mw._pick_read():
        rows.append({"date": p.get("date"), "code": p.get("code"), "name": p.get("name"),
                     "px": p.get("px"), "kind": _KMAP.get(p.get("signal"), p.get("signal", "종배"))})
    if not rows:
        return None, "기록 없음"
    from collections import defaultdict
    by_kind = defaultdict(list)
    cache = {}
    for r in rows[-200:]:
        code, date, px, kind = str(r.get("code", "")).zfill(6), r.get("date", ""), r.get("px"), r.get("kind")
        if not (code.isdigit() and px and kind and date):
            continue
        ymd = date.replace("-", "")
        if code not in cache:
            cache[code] = mw._daily_closes(tok, key, sec, code)
        cl = cache[code]
        later = sorted(d for d in cl if d > ymd)
        if later:
            by_kind[kind].append((cl[later[0]] / px - 1) * 100)
    if not by_kind:
        return None, "익일 결과 대조 가능한 기록 없음"
    out = []
    for kind, rets in sorted(by_kind.items(), key=lambda x: -len(x[1])):
        wr = sum(1 for x in rets if x > 0) / len(rets) * 100
        avg = sum(rets) / len(rets)
        out.append({"신호": kind, "건수": len(rets), "승률%": round(wr, 0), "평균수익%": round(avg, 2),
                     "표본작음": len(rets) < 10})
    return pd.DataFrame(out), None


def render_performance():
    import time as _t
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 새로고침", key="perf_refresh"):
            _compute_signal_stats.clear()
    df, err = _compute_signal_stats(_t.time() // 1800)
    if err:
        st.info(err)
        return
    st.subheader("신호별 승률 · 평균수익 (최근 200건, 익일 종가 대조)")
    st.dataframe(
        df.style.apply(lambda r: [f"background-color:{_pct_color(r['평균수익%'])}22"] * len(r), axis=1),
        use_container_width=True, hide_index=True)
    st.bar_chart(df.set_index("신호")["평균수익%"])
    if df["표본작음"].any():
        st.caption("⚠️ 표본작음 = 10건 미만 — 실행중단 등 판단 근거로 쓰기엔 노이즈에 취약합니다.")


# ══════════════════════════════════════════
# 탭 3 — 🌙 시장 복기
# ══════════════════════════════════════════

def render_market_review():
    daily, other = mw._mr_read_all()
    if not daily and not other:
        st.info("market_review.md가 없습니다 — '과거복기 학습' 또는 감시를 먼저 돌려야 쌓입니다.")
        return
    weekly = [o for o in other if o.startswith(mw._WEEKLY_META_TAG)]
    if weekly:
        st.subheader("📊 주간 메타복기")
        for w in weekly[-3:][::-1]:
            _title = w.split("\n", 1)[0]
            with st.expander(_title, expanded=False):
                st.markdown(w.split("\n", 1)[1] if "\n" in w else w)
    st.subheader("📅 일별 복기 (최신순)")
    for d in sorted(daily, reverse=True)[:14]:
        body = daily[d]
        _title = body.split("\n", 1)[0]
        with st.expander(_title, expanded=(d == max(daily))):
            st.markdown(body.split("\n", 1)[1] if "\n" in body else body)


# ══════════════════════════════════════════
# 탭 4 — 💼 보유종목
# ══════════════════════════════════════════

def render_holdings():
    holdings = mw._read_holdings()
    if not holdings:
        st.info("my_holdings.json에 등록된(on:true) 보유종목이 없습니다.")
        st.caption("파일은 macro_watcher.py와 완전히 공유됩니다 — 여기서 바꾸면 봇 알림에도 바로 반영됩니다.")
        return
    tok, key, sec = _kis()
    if not tok:
        st.warning("KIS 키 없음 — 실시간 홀딩판정 불가")
        return
    total_pl = 0
    for s in holdings:
        code = str(s.get("code", "")).zfill(6)
        name = s.get("name", code)
        avg = s.get("avg") or 0
        qty = s.get("qty") or 0
        stop = float(s.get("stop", -2.0))
        try:
            px, chg, _ = mw._price_and_turnover(tok, key, sec, code)
        except Exception:
            px = None
        with st.container(border=True):
            if not px or not avg:
                st.markdown(f"**{name}** — 시세 조회 실패")
                continue
            ret = (px / avg - 1) * 100
            pl = int((px - avg) * qty) if qty else 0
            total_pl += pl
            cols = st.columns([2, 2, 2, 3])
            cols[0].markdown(f"**{name}** `{code}`")
            cols[1].markdown(f"{px:,}원 ({chg or 0:+.1f}%)")
            cols[2].markdown(f":{'red' if ret < 0 else 'green'}[{ret:+.1f}%]  ·  {pl:+,}원")
            try:
                cl = mw._daily_closes(tok, key, sec, code)
                prevc = cl[sorted(cl)[-1]] if cl else avg
                verdict = mw._holding_judge(tok, key, sec, code, px, prevc, ret=ret, stop=stop)
            except Exception as _e:
                verdict = f"판정 조회 실패: {type(_e).__name__}"
            cols[3].markdown(verdict.replace("\n", "  \n"))
    st.metric("💰 총 평가손익", f"{total_pl:+,}원")


# ══════════════════════════════════════════
# 탭 5 — 🎯 종배픽 · 재료등급 · 장세판독
# ══════════════════════════════════════════

def render_dolpanty():
    tok, key, sec = _kis()
    st.subheader("🧭 장세 판독")
    if tok:
        try:
            r = mw._regime_detect(tok, key, sec, datetime.utcnow() + timedelta(hours=9))
            st.markdown(f"**{r['text']}**")
        except Exception as _e:
            st.error(f"조회 실패: {type(_e).__name__}")
    else:
        st.info("KIS 키 없음")

    st.subheader("🌒 최근 종배픽 기록")
    picks = [p for p in mw._pick_read()
             if p.get("signal") in ("dolpanty", "dolpanty_nonxt", "dolpanty_div", "dolpanty_shadow")]
    if not picks:
        st.caption("종배 기록 없음 — 감시를 며칠 돌린 뒤 쌓입니다.")
        return
    df = pd.DataFrame(picks[-60:][::-1])
    df["신호"] = df["signal"].map(_KMAP).fillna(df["signal"])
    st.dataframe(df[["date", "name", "code", "px", "score", "신호"]], use_container_width=True, hide_index=True)
    st.caption("종배그림자 = 텔레그램 미발송 대조군(뽑힐 뻔한 후보) — 실제 추천이 아닙니다.")


# ══════════════════════════════════════════
# 탭 6 — 🔍 관심종목 (my_watch.json 직접 CRUD — macro_watcher와 동일 파일)
# ══════════════════════════════════════════

def render_watchlist():
    raw = _read_json_safe(mw.MY_WATCH_FILE, {"on": True, "stocks": []})
    stocks = raw.get("stocks") or []
    tok, key, sec = _kis()

    with st.form("watch_add", clear_on_submit=True):
        c1, c2, c3 = st.columns([1, 2, 1])
        code = c1.text_input("종목코드(6자리)")
        name = c2.text_input("종목명")
        add = c3.form_submit_button("➕ 추가", use_container_width=True)
        if add and code.strip().isdigit():
            code = code.strip().zfill(6)
            stocks = [s for s in stocks if str(s.get("code")) != code]
            stocks.append({"code": code, "name": name.strip() or code})
            raw["stocks"] = stocks
            mw._atomic_write_json(mw.MY_WATCH_FILE, raw)
            st.success(f"{name or code} 추가됨")
            st.rerun()

    if not stocks:
        st.info("관심종목이 없습니다.")
        return
    to_remove = []
    for s in stocks:
        code = str(s.get("code", "")).zfill(6)
        cols = st.columns([3, 2, 1])
        px_txt = "—"
        if tok:
            try:
                px, chg, _ = mw._price_and_turnover(tok, key, sec, code)
                if px:
                    px_txt = f"{px:,}원 ({chg or 0:+.1f}%)"
            except Exception:
                pass
        cols[0].markdown(f"**{s.get('name', code)}** `{code}`")
        cols[1].markdown(px_txt)
        if cols[2].button("삭제", key=f"rm_{code}"):
            to_remove.append(code)
    if to_remove:
        raw["stocks"] = [s for s in stocks if str(s.get("code")) not in to_remove]
        mw._atomic_write_json(mw.MY_WATCH_FILE, raw)
        st.rerun()


# ══════════════════════════════════════════
# 탭 7 — 🔎 라이브 스캐너 (macro_watcher의 순수 스캔 함수 그대로 호출)
# ══════════════════════════════════════════

def render_scanner():
    tok, key, sec = _kis()
    if not tok:
        st.info("KIS 키 없음 — 스캔 불가")
        return
    if st.button("📈 주간 변동성 상위 스캔 실행"):
        with st.spinner("스캔 중..."):
            try:
                text = mw._volatility_scan(tok, key, sec, gemini_key=None)
                st.markdown(text.replace("\n", "  \n"))
            except Exception as _e:
                st.error(f"스캔 실패: {type(_e).__name__}: {_e}")
    st.caption("※ 매수신호가 아닌 관찰용 후보 목록입니다(macro_watcher.py --volatility와 동일 로직).")


# ══════════════════════════════════════════
# 탭 8 — ⚙️ 설정 · 진단
# ══════════════════════════════════════════

def render_settings():
    st.subheader("🔑 키 연결 상태")
    kis_key, kis_secret = mw.read_kis_keys()
    gemini = mw.read_gemini_key()
    naver_id, naver_secret = mw.read_naver_keys()
    dart = mw.read_dart_key()
    tg_tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    rows = [
        ("KIS", bool(kis_key and kis_secret)),
        ("Gemini", bool(gemini)),
        ("Naver", bool(naver_id and naver_secret)),
        ("DART", bool(dart)),
        ("Telegram", bool(tg_tok and tg_chat)),
    ]
    cols = st.columns(len(rows))
    for c, (label, ok) in zip(cols, rows):
        c.metric(label, "🟢 연결됨" if ok else "🔴 없음")

    st.subheader("🩺 감시 프로세스 상태")
    mtime = _mtime(mw.STATE_FILE)
    if mtime:
        dt = datetime.fromtimestamp(mtime)
        age_min = (datetime.now() - dt).total_seconds() / 60
        st.markdown(f"macro_watcher_state.json 최근 갱신: **{dt.strftime('%Y-%m-%d %H:%M:%S')}** "
                    f"({age_min:.0f}분 전)")
        if age_min > 30:
            st.warning("30분 이상 갱신이 없습니다 — 감시가 꺼져 있거나 멈춘 상태일 수 있습니다.")
    else:
        st.info("상태 파일 없음 — 감시를 한 번도 안 돌렸을 수 있습니다.")

    st.subheader("👤 계정")
    st.markdown(f"로그인: **{_current_username()}**")
    if st.button("로그아웃", key="settings_logout"):
        st.session_state.clear()
        try:
            st.query_params.clear()
        except Exception:
            pass
        st.rerun()


# ══════════════════════════════════════════
# 렌더
# ══════════════════════════════════════════
tabs = st.tabs(["🏠 오늘 현황", "📊 성과 분석", "🌙 시장 복기", "💼 보유종목",
                "🎯 종배픽·재료등급", "🔍 관심종목", "🔎 라이브 스캐너", "⚙️ 설정·진단"])

with tabs[0]:
    render_home()
with tabs[1]:
    render_performance()
with tabs[2]:
    render_market_review()
with tabs[3]:
    render_holdings()
with tabs[4]:
    render_dolpanty()
with tabs[5]:
    render_watchlist()
with tabs[6]:
    render_scanner()
with tabs[7]:
    render_settings()

st.divider()
st.caption("📊 퀀트 관제탑 · macro_watcher.py 신호를 그대로 보여주는 뷰어(자체 판단 로직 없음)")
