"""
📡 매크로 + 수급 상시 감시 → 텔레그램 폰 알림 (대시보드 안 켜도 백그라운드로 동작)

알림 종류:
  1) 매크로 국면 개선  🔴리스크오프 → 🟡중립/🟢진입허용
  2) 🚀 전조 시그널    섹터 자금 대이동(이탈원→유입처) + 매크로 정상
  3) 🥇 A급 종목       자금 유입처 × 연기금 포착 교집합(신규 등장 시)

데이터:
  - 매크로: yfinance (NQ=F/^SOX/NVDA/AVGO/MU/CL=F)  ← 항상 동작
  - 수급(전조·A급): KIS API  ← .streamlit/secrets.toml 의 KIS_APP_KEY/KIS_APP_SECRET 필요
    · 연기금 겹침은 pension_track_log.json(대시보드가 쌓는 파일) 사용

사용법 (윈도우):
  1) pip install yfinance requests
  2) set TELEGRAM_BOT_TOKEN=봇토큰
     set TELEGRAM_CHAT_ID=내chat_id
  3) py macro_watcher.py --interval 300
  (start_watcher.bat 더블클릭으로도 실행 가능)
"""
import os
import sys
import json
import time
import argparse
import datetime
import warnings
warnings.filterwarnings("ignore")

try:
    import requests
except ImportError:
    print("requests 필요: py -m pip install requests"); sys.exit(1)
try:
    import yfinance as yf
except ImportError:
    print("yfinance 필요: py -m pip install yfinance"); sys.exit(1)

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "macro_watcher_state.json")
# secrets.toml 탐색 후보: 프로젝트 .streamlit → 홈 .streamlit
SECRETS_CANDIDATES = [
    os.path.join(BASE, ".streamlit", "secrets.toml"),
    os.path.join(os.path.expanduser("~"), ".streamlit", "secrets.toml"),
]
SECRETS_FILE = next((p for p in SECRETS_CANDIDATES if os.path.exists(p)), SECRETS_CANDIDATES[0])
PENSION_FILE = os.path.join(BASE, "pension_track_log.json")
KIS_BASE = "https://openapi.koreainvestment.com:9443"

NQ_BLOCK, NQ_GO, WTI_RISK = -0.2, 0.5, 2.0

# 코스피200 선물 근월물 단축코드(KIS 국내선물옵션 inquire-price용) — 분기 만기(3·6·9·12월 두번째 목요일)마다 롤오버.
# 만기 지나면 KIS 공식 예시 코드 갱신(예: 101W09→차월물)하거나 kospi_fut.json로 덮어쓰기. 잘못되면 데이터 None(가짜 안 씀).
KOSPI200_FUT_CODE = "101W09"
KOSPI_FUT_CFG = os.path.join(BASE, "kospi_fut.json")

# 섹터 구성(대시보드 _BRIEF_SECTORS 동일)
SECTORS = {
    "반도체": [("000660", "SK하이닉스"), ("005930", "삼성전자"), ("042700", "한미반도체")],
    "2차전지": [("373220", "LG에너지솔루션"), ("006400", "삼성SDI"), ("247540", "에코프로비엠")],
    "바이오": [("207940", "삼성바이오로직스"), ("068270", "셀트리온"), ("196170", "알테오젠")],
    "방산/우주": [("012450", "한화에어로스페이스"), ("047810", "한국항공우주"), ("272210", "한화시스템")],
    "원전/우라늄": [("034020", "두산에너빌리티"), ("052690", "한전기술"), ("051600", "한전KPS")],
    "인터넷/빅테크": [("035420", "NAVER"), ("035720", "카카오")],
}


# ── 유틸 ────────────────────────────────────────────────────────────────────
def _to_int(v, d=0):
    try:
        if v is None:
            return d
        s = str(v).replace(",", "").replace("+", "").strip()
        return int(float(s)) if s not in ("", "-", "N/A", "None") else d
    except Exception:
        return d


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


# [1단계] 초고속 웹 속보판(live_dashboard.html)이 읽을 스냅샷 — 매 루프마다 최신값 저장.
#   대시보드(Streamlit)의 무거운 재계산/콜드스타트를 우회해 <1초 로딩을 가능케 한다.
SNAPSHOT_FILE = os.path.join(BASE, "snapshot.json")


def save_snapshot(snap):
    try:
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
    except Exception:
        pass


# [1단계-b] snapshot.json을 GitHub 'data' 브랜치에 업로드 → GitHub Pages 속보판이 읽는다.
#   git 설치와 무관하게 동작하도록 GitHub Contents API 사용. 필요 환경변수:
#     GITHUB_TOKEN : repo 권한 Personal Access Token (start_watcher.bat에서 set)
#     GH_REPO      : "owner/repo" (기본 guy1715-svg/quant-dashboard)
#   토큰 없으면 조용히 건너뜀(로컬 파일만 갱신) → 텔레그램/수급 로직엔 영향 없음.
import base64 as _b64

_GH_REPO   = os.environ.get("GH_REPO", "guy1715-svg/quant-dashboard")
_GH_BRANCH = os.environ.get("GH_DATA_BRANCH", "data")
_gh_warned = {"done": False}


def _gh_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def _gh_hint(code):
    return {401: "토큰이 틀렸거나 만료(재발급 필요)",
            403: "권한 부족 — classic PAT면 'repo' 체크, fine-grained면 이 저장소 선택 + Contents: Read and write",
            404: "저장소 접근 불가 — GH_REPO 오타이거나 토큰에 이 저장소 권한 없음"}.get(code, "")


def _gh_ensure_branch(token):
    """data 브랜치가 없으면 기본 브랜치 HEAD에서 생성. 이미 있으면 무시. 실패 시 원인 출력."""
    base = f"https://api.github.com/repos/{_GH_REPO}"
    try:
        r = requests.get(f"{base}/git/ref/heads/{_GH_BRANCH}", headers=_gh_headers(token), timeout=8)
        if r.status_code == 200:
            return True
        # 저장소 접근/기본 브랜치 확인
        rp = requests.get(base, headers=_gh_headers(token), timeout=8)
        if rp.status_code != 200:
            print(f"   ↳ 저장소 조회 실패 {rp.status_code}: {_gh_hint(rp.status_code)} (repo={_GH_REPO})")
            return False
        default = rp.json().get("default_branch", "main")
        head = requests.get(f"{base}/git/ref/heads/{default}", headers=_gh_headers(token), timeout=8)
        sha = head.json().get("object", {}).get("sha") if head.status_code == 200 else None
        if not sha:
            print(f"   ↳ 기본 브랜치({default}) HEAD 조회 실패 {head.status_code}")
            return False
        cr = requests.post(f"{base}/git/refs", headers=_gh_headers(token), timeout=8,
                           json={"ref": f"refs/heads/{_GH_BRANCH}", "sha": sha})
        if cr.status_code in (200, 201):
            print(f"   ↳ '{_GH_BRANCH}' 브랜치 생성 완료")
            return True
        print(f"   ↳ 브랜치 생성 실패 {cr.status_code}: {cr.json().get('message','')[:80]} · {_gh_hint(cr.status_code)}")
        return False
    except Exception as e:
        print("   ↳ 브랜치 확인 오류:", e)
        return False


def push_snapshot_github(json_str):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        if not _gh_warned["done"]:
            print("ℹ️ GITHUB_TOKEN 미설정 — 스냅샷 GitHub 업로드 건너뜀(로컬 파일만). 웹 속보판 쓰려면 토큰 설정.")
            _gh_warned["done"] = True
        return
    base = f"https://api.github.com/repos/{_GH_REPO}/contents/snapshot.json"
    try:
        # 기존 파일 sha 조회(업데이트에 필요). 없으면(404) 신규 생성.
        g = requests.get(f"{base}?ref={_GH_BRANCH}", headers=_gh_headers(token), timeout=8)
        sha = g.json().get("sha") if g.status_code == 200 else None
        if g.status_code == 404 and not _gh_ensure_branch(token):
            print("⚠️ data 브랜치 생성 실패 — 토큰 권한(repo) 확인 필요"); return
        payload = {
            "message": f"data: snapshot {datetime.datetime.utcnow().strftime('%m/%d %H:%M')}Z",
            "content": _b64.b64encode(json_str.encode("utf-8")).decode("ascii"),
            "branch": _GH_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        p = requests.put(base, headers=_gh_headers(token), json=payload, timeout=10)
        if p.status_code in (200, 201):
            if not _gh_warned.get("ok"):
                print(f"✅ 스냅샷 업로드 OK → {_GH_REPO} ({_GH_BRANCH} 브랜치)"); _gh_warned["ok"] = True
        else:
            print(f"⚠️ 스냅샷 업로드 실패 {p.status_code}: {p.json().get('message','')[:80]} · {_gh_hint(p.status_code)}")
    except Exception as e:
        print("스냅샷 업로드 오류:", e)


_ALERT_FEED = []      # [V13.2] 모든 텔레그램 알람의 당일 버퍼 — 메인 루프가 스냅샷으로 flush(대시보드 타임라인용)


def send_telegram(token, chat_id, text):
    _ok = False
    try:
        # [V17.1] 실제 전송 성공 여부 확인 — HTTP 200 + Telegram JSON ok:true 일 때만 성공.
        #   (기존엔 예외만 없으면 성공 처리 → 400/429/메시지초과에도 dedup 잠겨 신호 유실)
        _r = requests.get(f"https://api.telegram.org/bot{token}/sendMessage",
                          params={"chat_id": chat_id, "text": text}, timeout=8)
        _ok = bool(_r.status_code == 200 and (_r.json() or {}).get("ok"))
        if not _ok:
            print(f"텔레그램 전송 실패: HTTP {_r.status_code} {_r.text[:200]}")
    except Exception as e:
        print("텔레그램 전송 실패:", e)
    # 발송 성공/실패 무관하게 피드에 기록(대시보드에서 오늘 알람 타임라인으로 표시)
    try:
        _n = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        _ALERT_FEED.append({"day": _n.strftime("%Y%m%d"), "t": _n.strftime("%H:%M"), "text": text})
    except Exception:
        pass
    return _ok


# [V13.2] 통일 매수/매도 배지 — 모든 알림 첫 줄에 붙여 '지금 사라/팔라/기다려라'를 한눈에.
SIG_BUY = "🟢 매수검토"          # 단일 매수 신호
SIG_BUY_STRONG = "🟢🟢 매수(강)"  # 다중 확인·실행 타이밍(3조건·대장정렬·A급 등)
SIG_SELL = "🔴 매도·손절점검"     # 수급 이탈 등 청산 검토
SIG_CAUTION = "🔴 경계"          # 신규매수 주의(리스크↑)
SIG_WATCH = "🟡 관망"            # 보조·대기
SIG_INFO = "⚪ 정보"             # 시스템·요약


KEY_ALIASES = {"kis_app_key", "kis_key", "app_key", "kis_appkey"}
SECRET_ALIASES = {"kis_app_secret", "kis_secret", "app_secret", "kis_appsecret"}


def read_kis_keys():
    """KIS App Key/Secret 탐색 — 환경변수 → secrets.toml(프로젝트/홈, 앱과 동일 별칭·섹션). 없으면 (None,None)."""
    key = secret = None
    # 0) 환경변수(배치에서 set 가능)
    for _n in ("KIS_APP_KEY", "KIS_KEY", "APP_KEY", "KIS_APPKEY"):
        if not key and os.environ.get(_n):
            key = os.environ[_n].strip()
    for _n in ("KIS_APP_SECRET", "KIS_SECRET", "APP_SECRET", "KIS_APPSECRET"):
        if not secret and os.environ.get(_n):
            secret = os.environ[_n].strip()
    if key and secret:
        return key, secret
    try:
        data = None
        try:
            import tomllib
            with open(SECRETS_FILE, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            data = None
        if isinstance(data, dict):
            def _walk(d):
                nonlocal key, secret
                for k, v in d.items():
                    kl = str(k).lower()
                    if isinstance(v, dict):
                        _walk(v)
                    elif kl in KEY_ALIASES and not key and isinstance(v, str):
                        key = v.strip()
                    elif kl in SECRET_ALIASES and not secret and isinstance(v, str):
                        secret = v.strip()
            _walk(data)
        if not (key and secret):   # tomllib 실패/부재 → 라인 파싱 폴백
            with open(SECRETS_FILE, encoding="utf-8") as f:
                for line in f:
                    if "=" not in line or line.strip().startswith("#"):
                        continue
                    name = line.split("=", 1)[0].strip().lower()
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if name in KEY_ALIASES and not key:
                        key = val
                    elif name in SECRET_ALIASES and not secret:
                        secret = val
    except Exception:
        pass
    return key, secret


# ── 매크로 ──────────────────────────────────────────────────────────────────
def _pct(sym):
    try:
        fi = yf.Ticker(sym).fast_info
        l, p = float(fi.last_price), float(fi.previous_close)
        if l > 0 and p > 0:
            return (l / p - 1) * 100
    except Exception:
        pass
    return None


def _wti_pct():
    try:
        h = yf.Ticker("CL=F").history(period="5d")["Close"].dropna()
        if len(h) >= 2:
            return (float(h.iloc[-1]) / float(h.iloc[-2]) - 1) * 100
    except Exception:
        pass
    return None


def compute_macro():
    nq, sox = _pct("NQ=F"), _pct("^SOX")
    peers = [x for x in (_pct("NVDA"), _pct("AVGO"), _pct("MU")) if x is not None]
    wti = _wti_pct()
    ups = sum(1 for v in peers if v > 0)
    semi_sync = (sox is not None and sox > 0) and (len(peers) > 0 and ups >= max(1, round(len(peers) * 0.6)))
    riskoff = (wti is not None and wti >= WTI_RISK)
    # [SOX 방어막] K-국장은 반도체 시총 비중 커 SOX 급락에 종속 동조 → −5% 폭락=차단 / −3% 조정=경고
    sox_crash = (sox is not None and sox <= -5.0)
    sox_warn  = (sox is not None and sox <= -3.0)
    if riskoff or (nq is not None and nq <= NQ_BLOCK) or sox_crash:
        sev = 2
        text = "🔴 리스크오프 · 신규매수 차단" + (f" (반도체 폭락 SOX {sox:+.1f}%)" if sox_crash else "")
    elif (nq is not None and nq >= NQ_GO) and semi_sync and not sox_warn:
        sev, text = 0, "🟢 진입 허용 (매크로 3대 양호)"
    elif sox_warn:
        sev, text = 1, f"🟠 경고 · 반도체 조정(SOX {sox:+.1f}%) — 한도 50%"
    else:
        sev, text = 1, "🟡 중립 · 선별 진입"
    detail = (f"나스닥 {nq:+.2f}% · SOX {sox:+.2f}% · WTI {wti:+.2f}%"
              if None not in (nq, sox, wti) else "일부 데이터 대기")
    return sev, text, detail


def _level(sym):
    """현재가/전일대비% 반환 (환율·VIX처럼 '값+변화' 지표용). (last, chg%) 또는 (None,None)."""
    try:
        fi = yf.Ticker(sym).fast_info
        l, p = float(fi.last_price), float(fi.previous_close)
        if l > 0 and p > 0:
            return l, (l / p - 1) * 100
    except Exception:
        pass
    return None, None


def _last(sym):
    """절대 가격만 반환(야간 변동 추적용). float 또는 None."""
    try:
        l = float(yf.Ticker(sym).fast_info.last_price)
        return l if l > 0 else None
    except Exception:
        return None


def track_overnight_futures(now_kst, state):
    """국장 넥스트레이드 마감(20:00) → 익일 07:00 나스닥100 선물 야간 변동 추적.
    20:00~20:20 창에서 NQ 가격을 '야간 기준'으로 고정, 07:00~07:20 창에서 아침 가격 캡처.
    송출은 send_morning_brief가 담당. 반환: 없음(state에 기록)."""
    m = now_kst.hour * 60 + now_kst.minute
    today = now_kst.strftime("%Y%m%d")
    # ① 20:00~20:20 — 국장 야간 마감 기준가 고정(당일 1회)
    if (20 * 60) <= m <= (20 * 60 + 20) and state.get("nq2000_day") != today:
        px = _last("NQ=F")
        if px:
            state["nq2000"] = px
            state["nq2000_day"] = today
            print(f"           🌙 나스닥선물 야간기준(20:00) 고정: {px:,.1f}")
    # ② 07:00~07:20 — 익일 아침 가격 캡처(당일 1회)
    if (7 * 60) <= m <= (7 * 60 + 20) and state.get("nq0700_day") != today:
        px = _last("NQ=F")
        if px:
            state["nq0700"] = px
            state["nq0700_day"] = today
            print(f"           🌙 나스닥선물 아침(07:00) 캡처: {px:,.1f}")


def _overnight_nq_line(state, today):
    """20:00→07:00 야간 변동 브리핑 한 줄. 기준·아침가 둘 다 있고 아침 캡처가 오늘이면 반환, 아니면 None."""
    base = state.get("nq2000"); morn = state.get("nq0700")
    if base and morn and state.get("nq0700_day") == today:
        pct = (morn / base - 1) * 100
        icon = "🟢" if pct >= 0 else "🔴"
        return f"🌙 나스닥선물 야간(20:00→07:00): {pct:+.2f}% {icon}  ({base:,.0f}→{morn:,.0f})"
    return None


def compute_indicators():
    """속보판 '지표 세부' 카드용 — 나스닥·SOX·WTI(등락%)·원달러 환율·VIX(값+변화).
    tone: 'up'(호재/초록) · 'down'(악재/빨강) · 'flat'. 환율·VIX는 상승이 리스크라 반대로 색칠."""
    out = []
    nq, sox, wti = _pct("NQ=F"), _pct("^SOX"), _wti_pct()
    for lbl, v, unit in (("나스닥선물", nq, "%"), ("필라델피아반도체", sox, "%"), ("WTI 유가", wti, "%")):
        if v is not None:
            out.append({"label": lbl, "value": f"{v:+.2f}", "unit": unit,
                        "tone": "up" if v > 0 else "down" if v < 0 else "flat"})
    krw, krw_c = _level("KRW=X")
    if krw is not None:
        out.append({"label": "원/달러 환율", "value": f"{krw:,.1f}", "unit": f" ({krw_c:+.2f}%)",
                    "tone": "down" if krw_c > 0.1 else "up" if krw_c < -0.1 else "flat"})  # 환율↑=리스크
    vix, vix_c = _level("^VIX")
    if vix is not None:
        out.append({"label": "VIX 공포지수", "value": f"{vix:,.1f}", "unit": f" ({vix_c:+.1f}%)",
                    "tone": "down" if vix_c > 0 else "up" if vix_c < 0 else "flat"})   # VIX↑=리스크
    return out


# ── KIS 수급(금액 기준) ─────────────────────────────────────────────────────
# [토큰 폭주 방지] KIS 접근토큰은 서버측 24h 유효 + '발급 1분1회'(EGW00133) 제한.
#   5분 루프마다 새로 발급하면 카톡 발급알림이 쏟아지고 API가 막힐 수 있어 반드시 재사용한다.
#   대시보드(quant_dashboard.py)와 동일한 kis_token_cache.json 포맷({fp,token,exp})을 공유 →
#   같은 PC면 대시보드와 토큰을 함께 재사용(하루 1회 발급으로 수렴).
TOKEN_FILE = os.path.join(BASE, "kis_token_cache.json")


def kis_token(key, secret):
    _fp = f"{key[:8]}|False"          # 실전 도메인(openapi:9443) → 대시보드 real-mode fp와 일치
    _now = time.time()
    # 1) 캐시(파일)에서 유효 토큰 재사용 — 만료 60초 여유
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            _d = json.load(f)
        if _d.get("fp") == _fp and _d.get("token") and float(_d.get("exp", 0)) > _now + 60:
            return _d["token"]
    except Exception:
        pass
    # 2) 없거나 만료 → 신규 발급 후 저장
    try:
        r = requests.post(f"{KIS_BASE}/oauth2/tokenP",
                          json={"grant_type": "client_credentials", "appkey": key, "appsecret": secret},
                          timeout=8)
        _j = r.json()
        _tok = _j.get("access_token")
        if not _tok:
            return None
        _exp = _now + int(_j.get("expires_in", 86400))
        try:
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump({"fp": _fp, "token": _tok, "exp": _exp}, f)
        except Exception:
            pass
        return _tok
    except Exception:
        return None


def sector_moneyflow(token, key, secret):
    """섹터별 순매수 거래대금(원) + 종목별 세부. 실패 항목은 격리."""
    hdr = {"authorization": f"Bearer {token}", "appkey": key, "appsecret": secret}
    out = {}
    for sname, stocks in SECTORS.items():
        net_amt, detail = 0, []
        for code, nm in stocks:
            qty = None
            try:
                r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
                                 headers={**hdr, "tr_id": "HHPTJ04160200"},
                                 params={"MKSC_SHRN_ISCD": code}, timeout=6)
                o2 = r.json().get("output2", [])
                if isinstance(o2, list) and o2:
                    for row in reversed(o2):
                        if isinstance(row, dict) and (_to_int(row.get("frgn_fake_ntby_qty")) or _to_int(row.get("orgn_fake_ntby_qty"))):
                            qty = _to_int(row.get("frgn_fake_ntby_qty")) + _to_int(row.get("orgn_fake_ntby_qty"))
                            break
            except Exception:
                pass
            price = None
            try:
                rp = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                                  headers={**hdr, "tr_id": "FHKST01010100"},
                                  params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=6)
                price = _to_int(rp.json().get("output", {}).get("stck_prpr"))
            except Exception:
                pass
            amt = (qty * price) if (qty is not None and price) else None
            detail.append({"code": code, "name": nm, "amt": amt})
            if amt is not None:
                net_amt += amt
        out[sname] = {"net": net_amt, "stocks": detail}
    return out


ACE_AMT_MIN = 5_000_000_000   # [V13.9] 연기금 파일 없을 때 A급 대체 기준: 순매수 500억↑


def pension_codes():
    try:
        with open(PENSION_FILE, encoding="utf-8") as f:
            return {r.get("code") for r in json.load(f).get("records", [])}
    except Exception:
        return set()


def _cash_guide(sev):
    """현금비중·대응 권장 — 매크로 신호등(sev) 직결. 대시보드 '현금비중 신호등'과 동일 원칙."""
    return {
        2: {"level": "🔴 경고", "ratio": "현금 50%+", "msg": "미수/신용 금지 · 무포지션 권장", "tone": "down"},
        1: {"level": "🟡 주의", "ratio": "현금 30%+", "msg": "선별 진입 · 분할 대응", "tone": "flat"},
        0: {"level": "🟢 양호", "ratio": "정상 대응", "msg": "원칙 매매 유지", "tone": "up"},
    }.get(sev, {"level": "🟡 주의", "ratio": "현금 30%+", "msg": "선별 진입", "tone": "flat"})


# ── [V12.1] 09:10 시가저격 텔레그램 — 만쥬 라인업(외부 JSON, 매일 교체 가능) ──
# 기본값(파일 없을 때 폴백). 실제 감시 대상은 manju_watchlist.json에서 매 루프 로드.
MANJU_LINEUP_DEFAULT = [
    ("207940", "삼성바이오로직스"), ("068270", "셀트리온"),
    ("011070", "LG이노텍"),        ("090460", "비에이치"),
    ("103140", "풍산"),            ("010130", "고려아연"),
]
WATCHLIST_FILE = os.path.join(BASE, "manju_watchlist.json")


def load_lineup():
    """manju_watchlist.json에서 감시 라인업 로드 — 매 루프 호출(핫리로드, 재시작 불필요).
    포맷: {"lineup": [["207940","삼성바이오로직스"], ...]}. 없거나 깨지면 기본값."""
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            _d = json.load(f)
        _lst = _d.get("lineup") if isinstance(_d, dict) else _d
        _out = []
        for _it in (_lst or []):
            if isinstance(_it, (list, tuple)) and len(_it) >= 2:
                _code = str(_it[0]).strip().zfill(6)
                _name = str(_it[1]).strip()
                if _code and _code.isdigit():
                    _out.append((_code, _name or _code))
            elif isinstance(_it, dict) and _it.get("code"):
                _code = str(_it["code"]).strip().zfill(6)
                _out.append((_code, str(_it.get("name", _code)).strip() or _code))
        return _out or MANJU_LINEUP_DEFAULT
    except Exception:
        return MANJU_LINEUP_DEFAULT


def watchlist_is_auto():
    """manju_watchlist.json의 'auto' 플래그 — True면 매일 자동 편입."""
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("auto"))
    except Exception:
        return False


def auto_lineup_from_secs(secs, n=6):
    """[완전 자동] 섹터 수급에서 순매수 금액(자금유입) 상위 n종을 라인업으로 선정.
    주도 섹터 curated pool 내 실측 순매수액 상위 → 잡주 배제. 반환 [(code,name),...]."""
    _cands = []
    for _sname, _info in (secs or {}).items():
        for _s in _info.get("stocks", []):
            _amt = _s.get("amt")
            if _amt and _amt > 0:
                _cands.append((_amt, str(_s.get("code", "")).zfill(6), _s.get("name", "")))
    _cands.sort(reverse=True)
    _seen, _out = set(), []
    for _amt, _cd, _nm in _cands:
        if _cd and _cd not in _seen:
            _seen.add(_cd); _out.append((_cd, _nm or _cd))
        if len(_out) >= n:
            break
    return _out


def save_auto_lineup(pairs):
    """자동 선정 라인업을 manju_watchlist.json에 기록(대시보드와 공유). auto 플래그 유지."""
    try:
        _payload = {"auto": True, "_설명": "완전자동 — 매일 장중 자금유입 상위 6종 자동 편입(watcher가 갱신).",
                    "lineup": [[c, n] for c, n in pairs]}
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(_payload, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


SNIPER_LARGE = 30_000_000_000   # 대형주 임계 300억
SNIPER_SMALL = 15_000_000_000   # 중소형주 임계 150억
SNIPER_LARGECAP_PX = 50_000     # 현재가 ≥ 5만 → 대형주 판정


def _price_and_turnover(token, key, secret, code, mrkt="J"):
    """종목 현재가·등락률·누적거래대금(원) — inquire-price. (px, chg, turnover) 또는 (None,None,None).
    mrkt: J=KRX(정규장) · NX=넥스트레이드(NXT 야간) · UN=통합. 넥장 타점은 'NX'로 야간 세션 조회."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010100"},
                         params={"fid_cond_mrkt_div_code": mrkt, "fid_input_iscd": code}, timeout=6)
        o = r.json().get("output", {})
        if isinstance(o, dict) and o:
            return (_to_int(o.get("stck_prpr")),
                    float(str(o.get("prdy_ctrt", 0)).replace(",", "") or 0),
                    _to_int(o.get("acml_tr_pbmn")))
    except Exception:
        pass
    return None, None, None


def _ma20_disparity(token, key, secret, code, px):
    """현재가의 20일선 이격도(%) — inquire-daily-price 최근 20 종가 평균 기준. 실패 시 None.
    이격 = (현재가/20일선 −1)×100. 양수 클수록 과열(눌림 위험)."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        o = r.json().get("output", [])
        closes = [_to_int(row.get("stck_clpr")) for row in (o or []) if isinstance(row, dict)]
        closes = [c for c in closes if c][:20]
        if len(closes) >= 5 and px:
            ma20 = sum(closes) / len(closes)
            if ma20:
                return round((px / ma20 - 1) * 100, 1)
    except Exception:
        pass
    return None


SCORECARD_FILE = os.path.join(BASE, "signal_scorecard.json")


def _scorecard_append(now_kst, kind, code, name, px):
    """[V17.6] 신호별 자동 성적표(대시보드 공유 파일)에 적립 — 종류별 날짜당 1회. 예외 전파 없음."""
    if not code or not kind or not px:
        return
    today = now_kst.strftime("%Y-%m-%d")
    cd = str(code).zfill(6)
    try:
        with open(SCORECARD_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []
    if any(r.get("date") == today and r.get("kind") == kind and r.get("code") == cd for r in rows):
        return
    rows.append({"date": today, "t": now_kst.strftime("%H:%M"), "kind": kind,
                 "code": cd, "name": name or "", "px": int(px or 0), "r1": None, "r3": None})
    try:
        with open(SCORECARD_FILE, "w", encoding="utf-8") as f:
            json.dump(rows[-2000:], f, ensure_ascii=False)
    except Exception:
        pass


def _log_signal(state, now_kst, kind, name, code, px):
    """[V13.2] 매수 알림을 시각·가격과 함께 당일 기록 — '알림 성적'(진입했다면?) 추적용. 날짜 바뀌면 초기화."""
    today = now_kst.strftime("%Y%m%d")
    sl = state.get("signal_log", {})
    if sl.get("_day") != today:
        sl = {"_day": today, "items": []}
    sl.setdefault("items", []).append(
        {"t": now_kst.strftime("%H:%M"), "kind": kind, "name": name, "code": code, "px": px})
    sl["items"] = sl["items"][-120:]          # 최근 120건만 유지
    state["signal_log"] = sl
    try:
        _scorecard_append(now_kst, kind, code, name, px)   # [V17.6] 다중일 성적표에도 적립
    except Exception:
        pass


def _recent_high(token, key, secret, code, days=20):
    """최근 N일 고가 중 현재가 위의 '저항선' 근사 — inquire-daily-price. 실패 시 None.
    현재가보다 높은 최근 고가들 중 가장 가까운 값(=다음 저항). 없으면 최근 최고가."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        o = r.json().get("output", [])
        highs = [_to_int(row.get("stck_hgpr")) for row in (o or [])[:days] if isinstance(row, dict)]
        highs = [h for h in highs if h]
        return max(highs) if highs else None
    except Exception:
        pass
    return None


def _prev_3min_high(token, key, secret, code):
    """직전 3분봉(완성) 고가 — inquire-time-itemchartprice(1분봉) 3개 집계. 실패 시 None.
    현재 형성 중 봉을 제외하고, 직전 3분(완성 구간)의 최고가를 반환. '3분봉 전고 돌파' 판정용."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST03010200"},
                         params={"fid_etc_cls_code": "", "fid_cond_mrkt_div_code": "J",
                                 "fid_input_iscd": code, "fid_input_hour_1": "",
                                 "fid_pw_data_incu_yn": "N"}, timeout=6)
        o2 = r.json().get("output2", [])
        if isinstance(o2, list) and len(o2) >= 4:
            # o2[0] = 현재 형성 중(가장 최근) 1분봉 → 제외. o2[1..3] = 직전 완성 3분.
            highs = [_to_int(row.get("stck_hgpr")) for row in o2[1:4]]
            highs = [h for h in highs if h]
            if highs:
                return max(highs)
    except Exception:
        pass
    return None


# [V13.2] 시가저격에 '3분봉 전고 돌파'를 필수 조건으로 강제할지. 기본 False = 태그 표시만(거래대금 임계 유지).
#   실전 하루 관찰 후 오발이 적으면 True로 승격(직전 3분 고가 돌파 시에만 격발).
SNIPER_REQUIRE_BREAKOUT = False


def _kospi_fut_code():
    """근월물 코스피200 선물 단축코드 — kospi_fut.json({"code":"..."}) 있으면 우선, 없으면 상수."""
    try:
        if os.path.exists(KOSPI_FUT_CFG):
            c = (json.load(open(KOSPI_FUT_CFG, encoding="utf-8")) or {}).get("code")
            if c:
                return str(c).strip()
    except Exception:
        pass
    return KOSPI200_FUT_CODE


def kospi200_futures(token, key, secret):
    """코스피200 선물 실측(KIS 국내선물옵션 inquire-price·FHMIF10000000). 주간·야간 세션 모두 현재가/등락% 반환.
    반환: {"px":float,"chg":float,"code":str} 또는 None(데이터 없음=가짜 안 씀)."""
    code = _kospi_fut_code()
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-futureoption/v1/quotations/inquire-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHMIF10000000"},
                         params={"fid_cond_mrkt_div_code": "F", "fid_input_iscd": code}, timeout=6)
        o = r.json().get("output1") or r.json().get("output") or {}
        if isinstance(o, dict) and o.get("futs_prpr") not in (None, "", "0"):
            px = float(str(o.get("futs_prpr", 0)).replace(",", "") or 0)
            chg = float(str(o.get("futs_prdy_ctrt", 0)).replace(",", "") or 0)
            if px > 0:
                return {"px": px, "chg": chg, "code": code}
    except Exception:
        pass
    return None


def _kospi_fut_session(now_kst):
    """코스피200 선물 거래 세션 태그 — 주간(09:00~15:45)/야간(18:00~익일05:00)/휴장."""
    m = now_kst.hour * 60 + now_kst.minute
    if (9 * 60) <= m <= (15 * 60 + 45):
        return "주간"
    if m >= (18 * 60) or m <= (5 * 60):
        return "야간"
    return "휴장"


# ══════════════════════════════════════════════════════════════════════════
# [V18.4] DART 실시간 공시 감시 — 금감원 전자공시 OpenAPI. 공시는 기사보다 정확·선행.
#   수주·계약·실적·특허 = 호재 / 유증·감자·소송·거래정지 = 악재. 무료 키(open.dart.fss.or.kr).
# ══════════════════════════════════════════════════════════════════════════
def read_dart_key():
    """DART OpenAPI 키 탐색 — 환경변수 → secrets.toml. 없으면 None."""
    for _n in ("DART_API_KEY", "DART_KEY", "OPENDART_KEY", "DART_APP_KEY", "DART_APPKEY"):
        if os.environ.get(_n):
            return os.environ[_n].strip()
    _al = {"dart_api_key", "dart_key", "opendart_key", "dart_appkey", "dart"}
    try:
        import tomllib
        with open(SECRETS_FILE, "rb") as f:
            _d = tomllib.load(f)
        _found = [None]

        def _w(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if isinstance(v, dict):
                        _w(v)
                    elif str(k).lower() in _al and isinstance(v, str) and not _found[0]:
                        _found[0] = v.strip()
        if isinstance(_d, dict):
            _w(_d)
        if _found[0]:
            return _found[0]
    except Exception:
        pass
    try:
        with open(SECRETS_FILE, encoding="utf-8") as f:
            for line in f:
                if "=" in line and line.split("=", 1)[0].strip().lower() in _al:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


_DART_POS = ("공급계약체결", "단일판매", "수주", "기술이전", "특허권취득", "품목허가", "임상시험결과",
             "자기주식취득결정", "무상증자결정")
# [V18.8] 악재는 '진짜 중대'만 — 안내/조정 류 오탐 제거(전환가액조정 등 잡음 컷)
_DART_NEG = ("유상증자결정", "감자결정", "상장폐지", "매매거래정지", "감사의견거절", "감사의견한정",
             "횡령", "배임", "회생절차", "관리종목지정", "불성실공시법인지정", "영업정지")
# [V18.6/8] 형식·노이즈 공시 제외 — 증권발행실적·안내·정기보고서·가액조정 등(재료 아님)
_DART_SKIP = ("증권발행실적", "발행실적보고", "증권신고서", "투자설명서", "일괄신고", "자산유동화",
              "소액공모", "합병등종료", "조회공시", "자율공시)", "주식등의대량보유", "특정증권",
              "의결권대리행사", "가액조정", "안내공시", "부속명세", "결산", "감사보고서",
              "사업보고서", "분기보고서", "반기보고서", "주주총회", "주식명의개서", "권리행사")
# 실적 공시(내용 판단 불가 → V18.8부터 발송 OFF, 노이즈 폭주 방지)
_DART_PERF = ("영업(잠정)실적", "잠정실적", "매출액또는손익구조")


def check_dart_disclosures(now_kst, state, token_tg, chat_id, dart_key, kis_key=None, kis_secret=None):
    """DART 당일 신규 공시 폴링 → 호재 공시는 우리 엔진(거래대금·이격)으로 교차검증해 '진입후보 선정'.
    악재=경고 / 실적=내용확인 / 호재=거래대금·비과열이면 🎯진입후보, 아니면 관망·선점. 예외 전파 없음."""
    if not dart_key:
        return
    m = now_kst.hour * 60 + now_kst.minute
    if not ((7 * 60) <= m <= (17 * 60)):        # 장전~마감후 공시창(07:00~17:00)
        return
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("dart_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    try:
        r = requests.get("https://opendart.fss.or.kr/api/list.json",
                         params={"crtfc_key": dart_key, "bgn_de": today, "end_de": today,
                                 "page_no": "1", "page_count": "100",
                                 "sort": "date", "sort_mth": "desc"}, timeout=8)
        j = r.json()
    except Exception:
        return
    _status = j.get("status")
    if _status not in ("000", "013"):           # 013=데이터없음 / 그 외=키·한도 오류
        if not state.get("dart_err_warned"):
            send_telegram(token_tg, chat_id, f"⚠️ DART 공시 조회 실패 (status={_status}, {j.get('message','')}) — 키 확인")
            state["dart_err_warned"] = True
        return
    state["dart_err_warned"] = False
    _tok = kis_token(kis_key, kis_secret) if (kis_key and kis_secret) else None
    for _it in (j.get("list") or []):
        _rcp = _it.get("rcept_no")
        _stock = (_it.get("stock_code") or "").strip()
        if not _rcp or not _stock or sent.get(_rcp):
            continue                            # 신규·상장사(종목코드 有)만
        _nm = _it.get("report_nm", "") or ""
        _corp = _it.get("corp_name", "")
        if any(k in _nm for k in _DART_SKIP):     # [V18.6] 형식·노이즈 공시 제외(증권발행실적 등)
            sent[_rcp] = True
            continue
        _neg = any(k in _nm for k in _DART_NEG)
        _pos = any(k in _nm for k in _DART_POS)
        _perf = any(k in _nm for k in _DART_PERF)  # [V18.6] 진짜 잠정실적만(증권발행실적 오탐 제거)
        if not (_neg or _pos or _perf):
            continue
        sent[_rcp] = True
        _url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={_rcp}"
        # ── 악재/실적은 정보 알림 ──
        if _neg:
            send_telegram(token_tg, chat_id,
                          f"{SIG_CAUTION}\n📢 [DART 악재공시] {_corp}({_stock})\n{_nm}\n"
                          f"⚠️ 유증/감자/소송 등 악재성 — 보유 시 점검·신규 진입 주의\n{_url}")
            continue
        if _perf and not (_pos or _neg):
            continue                            # [V18.8] 실적 공시 발송 OFF — 내용판단 불가·노이즈 폭주 방지
        # ── 호재 공시 → 우리 엔진으로 교차검증 후 '종목 선정' ──
        _px = _chg = _turn = None
        if _tok:
            try:
                _px, _chg, _turn = _price_and_turnover(_tok, kis_key, kis_secret, _stock)
            except Exception:
                pass
        if not _px:                             # 장외/거래 전 → 선점 후보(재료만)
            send_telegram(token_tg, chat_id,
                          f"{SIG_BUY}\n🎯 [공시 발굴·선점] {_corp}({_stock})\n"
                          f"공시: {_nm} (호재 재료)\n"
                          f"🔥 장외/거래 전 — 개장 후 거래대금 붙는지 확인 후 소액 진입\n{_url}")
            continue
        _disp = None
        try:
            _disp = _ma20_disparity(_tok, kis_key, kis_secret, _stock, _px)
        except Exception:
            pass
        _dtxt = f" · 이격 {_disp:+.0f}%" if _disp is not None else ""
        _st = f"지금 {_px:,}({(_chg or 0):+.1f}%) 상승중" if (_chg or 0) > 0 else f"지금 {_px:,}({(_chg or 0):+.1f}%)"
        if _turn and _turn < 3_000_000_000:      # [V18.6] 거래대금 30억↓ = 거래 안 붙음 → 발송 안 함(소음 제거)
            continue                              # 소형주 수주라도 거래 붙어야 의미 → 급증스캔이 잡음
        _overheat = ((_chg or 0) >= 10.0) or (_disp is not None and _disp >= 12.0)
        if _overheat:                            # 이미 급등 → 추격 금지
            send_telegram(token_tg, chat_id,
                          f"{SIG_WATCH}\n📢 [공시·과열] {_corp}({_stock})\n"
                          f"공시: {_nm}\n{_st}{_dtxt} — 이미 급등, 추격 금지·눌림 대기\n{_url}")
            continue
        # 🎯 진입후보 선정 — 호재 공시 + 거래대금 살아있음 + 비과열
        _stop = int(_px * 0.98); _t1 = int(_px * 1.03)
        send_telegram(token_tg, chat_id,
                      f"{SIG_BUY_STRONG}\n🎯 [공시 발굴 진입후보] {_corp}({_stock})\n"
                      f"공시: {_nm} (호재·선행 재료)\n"
                      f"{_st}{_dtxt} · 거래대금 {_turn/1e8:,.0f}억 · 비과열 ✅\n"
                      f"진입 {_px:,} · 손절 {_stop:,}(−2%) · 1차익절 {_t1:,}(+3%)\n"
                      f"⚠️ 소액·칼손절 · 공시=선행이라 빠름 · {_url}")
        _log_signal(state, now_kst, "공시발굴", _corp, _stock, _px)
    state["dart_sent"] = sent


# [V18.3] 종목 뉴스 재료 등급 — watcher 시가저격/진입에 뉴스 확인 연계(악재 스킵·재료 태그).
_NEWS_S_KW = ("수주", "계약 체결", "공급 계약", "납품", "수출 계약", "어닝 서프라이즈",
              "예상 상회", "컨센 상회", "목표주가 상향", "목표가 상향", "투자의견 상향", "기술수출", "FDA 승인")
_NEWS_A_KW = ("실적", "영업이익", "순이익", "흑자전환", "역대 최대", "호실적", "신약", "임상")
_NEWS_NEG_KW = ("무산", "해지", "철회", "횡령", "배임", "상장폐지", "감자", "유상증자", "적자전환",
                "소송", "불성실공시", "분식", "거래정지", "관리종목", "리콜")


def _news_grade(code):
    """종목 뉴스 재료 등급 — 네이버 모바일 뉴스 제목 키워드. 반환 (grade, is_bad).
    grade: 'S'/'A'/'none'. is_bad: 악재 감지. 실패 시 ('none', False) — 매매 방해 안 함."""
    titles = []
    try:
        r = requests.get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize=15&page=1",
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"},
                         timeout=5)
        _j = r.json()

        def _walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("title", "titleText", "aiTitle") and isinstance(v, str):
                        titles.append(v)
                    else:
                        _walk(v)
            elif isinstance(o, list):
                for it in o:
                    _walk(it)
        _walk(_j)
    except Exception:
        return "none", False
    if not titles:
        return "none", False
    for t in titles:
        if any(n in t for n in _NEWS_NEG_KW):
            return "none", True                # 제목 하나라도 악재 → 악재 판정
    blob = " ".join(titles)
    if any(k in blob for k in _NEWS_S_KW):
        return "S", False
    if any(k in blob for k in _NEWS_A_KW):
        return "A", False
    return "none", False


# ══════════════════════════════════════════════════════════════════════════
# [V18.7] 조기 포착·급증진입을 watcher로 이관 — 대시보드 없이 상시 텔레그램(초입 신호).
#   거래대금 랭킹 → 일봉 셋업(기준선·5일선·이격·급증배수) → 조기포착/급증진입 판정 + 뉴스.
# ══════════════════════════════════════════════════════════════════════════
_EARLY_ETF_KW = ("ETN", "ETF", "선물", "레버리지", "인버스", "KODEX", "TIGER", "PLUS",
                 "ACE", "SOL", "KBSTAR", "리츠", "스팩", "채권")


def _volume_rank(token, key, secret, top=40):
    """당일 거래대금 상위 종목 — volume-rank(FHPST01710000). [{code,name,px,chg,turnover}]. 실패 시 []."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/volume-rank",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHPST01710000"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_cond_scr_div_code": "20171",
                                 "fid_input_iscd": "0000", "fid_div_cls_code": "0",
                                 "fid_blng_cls_code": "3", "fid_trgt_cls_code": "111111111",
                                 "fid_trgt_exls_cls_code": "000000", "fid_input_price_1": "",
                                 "fid_input_price_2": "", "fid_vol_cnt": "", "fid_input_date_1": ""},
                         timeout=6)
        out = r.json().get("output", []) or []
        rows = []
        for x in out[:top]:
            if not isinstance(x, dict):
                continue
            cd = str(x.get("mksc_shrn_iscd", "")).zfill(6)
            px = _to_int(x.get("stck_prpr"))
            if not (cd.isdigit() and len(cd) == 6 and px):
                continue
            rows.append({"code": cd, "name": x.get("hts_kor_isnm", cd), "px": px,
                         "chg": float(str(x.get("prdy_ctrt", 0)).replace(",", "") or 0),
                         "turnover": _to_int(x.get("acml_tr_pbmn"))})
        return rows
    except Exception:
        return []


def _daily_setup(token, key, secret, code, px):
    """일봉 셋업 — inquire-daily-price 최근 30일. ma5/ma20/이격/기준선(26)/돌파·근접/5일선위/20일평균거래대금."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        rows = [x for x in (r.json().get("output", []) or []) if isinstance(x, dict)]
        if len(rows) < 26:
            return None
        clpr = [_to_int(x.get("stck_clpr")) for x in rows]     # 최신순
        hgpr = [_to_int(x.get("stck_hgpr")) for x in rows]
        lwpr = [_to_int(x.get("stck_lwpr")) for x in rows]
        vol = [_to_int(x.get("acml_vol")) for x in rows]
        if not all(clpr[:26]):
            return None
        ma5 = sum(clpr[:5]) / 5.0
        ma20 = sum(clpr[:20]) / 20.0
        kijun = ((max(hgpr[:26]) + min(lwpr[:26])) / 2.0) if (all(hgpr[:26]) and all(lwpr[:26])) else 0
        prevc = clpr[1] if len(clpr) >= 2 else px
        _turns = [clpr[i] * vol[i] for i in range(1, min(21, len(clpr))) if clpr[i] and vol[i]]
        turnavg = (sum(_turns) / len(_turns)) if _turns else 0
        return {"ma5": ma5, "ma20": ma20,
                "disp": ((px / ma20 - 1) * 100) if ma20 else 0,
                "above5": bool(ma5 and px >= ma5),
                "kij_cross": bool(kijun and prevc < kijun <= px),
                "kij_near": bool(kijun and abs(px / kijun - 1) <= 0.02),
                "turnavg": turnavg}
    except Exception:
        return None


def check_early_catch(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V18.7] 조기 포착(기준선 초입)·급증진입 — 거래대금 랭킹 상시 스캔. 09:00~15:20, 리스크오프 억제."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (15 * 60 + 20)) or sev == 2:
        return
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("early_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if sent.get(cd) or not px or not turn:
            continue
        if any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if not (-2.0 <= chg <= 15.0) or turn < 3_000_000_000:   # 급락·이미급등·거래미미 사전 컷(일봉조회 절약)
            continue
        _budget += 1
        if _budget > 22:                          # API 절약(루프당 일봉조회 상한)
            break
        ds = _daily_setup(token, key, secret, cd, px)
        if not ds or ds["turnavg"] <= 0:
            continue
        mult = turn / ds["turnavg"]; disp = ds["disp"]; above5 = ds["above5"]
        if mult >= 2.0 and above5 and disp < 7 and chg < 12:
            _kind, _label, _kt = "급증진입", "🟢 [진입 신호·거래대금]", ""
        elif (ds["kij_cross"] or ds["kij_near"]) and mult >= 1.2 and disp < 7 and -1.0 <= chg <= 8.0:
            _kind, _label = "조기포착", "🟢 [조기 포착·기준선]"
            _kt = " · 일목 " + ("기준선 돌파✅" if ds["kij_cross"] else "기준선 걸침(±2%)")
        else:
            continue
        _ng, _nbad = _news_grade(cd)               # 뉴스 재료 확인(악재 스킵)
        if _nbad:
            continue
        _mat = "🔥재료 강함(S급)" if _ng == "S" else "🟢재료 있음(A급)" if _ng == "A" else "⚠️재료 미확인"
        _stop = int(px * 0.98); _t1 = int(px * 1.03)
        if send_telegram(token_tg, chat_id,
                         f"{SIG_BUY}\n🌅[장중단타·당일청산] {_label} {nm} {px:,}({chg:+.1f}%) · 거래대금 {mult:.1f}배{_kt}\n"
                         f"{_mat} · 20MA 이격 {disp:+.0f}%\n"
                         f"진입 {px:,} · 손절 {_stop:,}(−2%) · 1차익절 {_t1:,}(+3%)\n"
                         f"⚠️ 초입 신호=빠르지만 가짜 가능 · 소액·거래대금 계속 붙는지 확인"):
            sent[cd] = True
            _log_signal(state, now_kst, _kind, nm, cd, px)
    state["early_sent"] = sent


def check_snipers(token, key, secret, now_kst, state, token_tg, chat_id, lineup, sev=1):
    """09:00~09:10 KST 창에서 라인업 거래대금이 임계 돌파 시 종목별 1회 텔레그램.
    반환: 스냅샷용 리스트 [{name,code,px,chg,turnover_eok,cap}]. state['sniper_sent']로 당일 중복 차단.
    [V16.9 다이어트] sev==2(리스크오프)면 텔레그램 발송 억제 — 규제와 모순되는 매수 신호 차단(스냅샷엔 남김)."""
    m = now_kst.hour * 60 + now_kst.minute
    # [V13.2 투트랙] 마의 구간(09:00~09:15)은 초단타 시가저격 전용 — 창을 09:15까지 확대(기존 09:10)
    in_window = (9 * 60) <= m <= (9 * 60 + 15)
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("sniper_sent", {})
    if sent.get("_day") != today:               # 날짜 바뀌면 초기화
        sent = {"_day": today}
    out = []
    if not in_window:
        state["sniper_sent"] = sent
        return out
    for code, name in lineup:
        px, chg, turn = _price_and_turnover(token, key, secret, code)
        if not px or not turn:
            continue
        need = SNIPER_LARGE if px >= SNIPER_LARGECAP_PX else SNIPER_SMALL
        cap = "대형" if px >= SNIPER_LARGECAP_PX else "중소형"
        if turn >= need:
            # [V13.2] 3분봉 전고 돌파 확인 — 태그용(기본) 또는 필수조건(SNIPER_REQUIRE_BREAKOUT=True)
            _p3h = _prev_3min_high(token, key, secret, code)
            _broke = (_p3h is not None and px > _p3h)
            if SNIPER_REQUIRE_BREAKOUT and _p3h is not None and not _broke:
                continue                        # 강제 모드: 전고 미돌파면 격발 보류
            _bk = ("3분봉 전고 돌파✅" if _broke else
                   "전고 미돌파(관망)" if _p3h is not None else "분봉 확인불가")
            out.append({"name": name, "code": code, "px": px, "chg": chg,
                        "turnover_eok": round(turn / 1e8, 0), "cap": cap, "breakout": _broke})
            if sev == 2:                        # [V16.9] 리스크오프 — 매수 신호 억제(모순 방지). 스냅샷만.
                continue
            # [V17.1] 낙폭과대 급락주(떨어지는 칼) 차단 — 거래대금만으로 격발되던 sniper에 방향 게이트 추가.
            _sdisp = _ma20_disparity(token, key, secret, code, px)
            if _sdisp is not None and _sdisp <= -10.0 and (chg or 0) < -1.0:
                continue                        # 20MA -10%↓ + 하락 중 = 시가저격 매수 보류
            if not sent.get(code):              # 당일 첫 돌파만 발송
                # [V18.3] 뉴스 재료 확인 — 악재면 시가저격 스킵, 재료 등급은 메시지에 표기
                _ng, _nbad = _news_grade(code)
                if _nbad:
                    continue                    # 악재 감지 → 시가저격 매수 보류
                _mattxt = ("🔥재료 강함(S급)" if _ng == "S" else "🟢재료 있음(A급)" if _ng == "A"
                           else "⚠️재료 미확인(기술적)")
                _stop = int(px * 0.98); _res = _recent_high(token, key, secret, code)
                if _res and _res > px:
                    _res_line = (f"🚀 돌파 홀딩 기준: {_res:,}원 (뚫으면 계속 보유)\n"
                                 f"🔻 돌파 후 하락 매도: {int(_res*0.99):,}원 (뚫었다 다시 밑이면 매도)")
                else:
                    _res_line = "🚀 신고가권(뚜렷한 저항 없음) — 고점 갱신 실패 시 매도"
                send_telegram(token_tg, chat_id,
                              f"{SIG_BUY}\n🌅[아침단타·당일청산] 🎯 시가저격 (마의구간 09:00~09:15) — {name}\n"
                              f"거래대금 {turn/1e8:,.0f}억 (임계 {need/1e8:,.0f}억·{cap}) 돌파 · {_bk} · {_mattxt}\n"
                              f"• 현재가 {px:,}원 ({chg:+.2f}%) · {now_kst.strftime('%H:%M')} KST\n"
                              f"─── 가격표 ───\n"
                              f"🎯 매수가(현재): {px:,}원\n"
                              f"✂️ 손절가: {_stop:,}원 (−2%)\n"
                              f"{_res_line}\n"
                              f"─────────\n"
                              f"🔌 HTS 열어 금액 3종(프로그램·외인·기관) 모두 (+) 확인 → 타격")
                sent[code] = True
                _log_signal(state, now_kst, "시가저격", name, code, px)
    state["sniper_sent"] = sent
    return out


def _investor_est(token, key, secret, code):
    """종목 장중 외국인/기관 추정 순매수 '수량' — investor-trend-estimate. (frn_qty, org_qty)."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "HHPTJ04160200"},
                         params={"MKSC_SHRN_ISCD": code}, timeout=6)
        o2 = r.json().get("output2", [])
        if isinstance(o2, list) and o2:
            for row in reversed(o2):
                if isinstance(row, dict) and (_to_int(row.get("frgn_fake_ntby_qty")) or _to_int(row.get("orgn_fake_ntby_qty"))):
                    return _to_int(row.get("frgn_fake_ntby_qty")), _to_int(row.get("orgn_fake_ntby_qty"))
    except Exception:
        pass
    return 0, 0


# [V17.2] 종목별 프로그램매매 순매수 금액(원) — 진단(diag_program_trade)으로 실전 검증한 엔드포인트.
#   기존 program-trade-by-stock/FHPPG04650200은 rt_cd=2(FID_INPUT_DATE_1 없음)로 거부됐음 →
#   대시보드와 동일한 comp-program-trade-today/FHPPG04650101로 통일. 실값 필드: whol_smtn_ntby_tr_pbmn(원).
PROGRAM_TR_ID = "FHPPG04650101"       # 프로그램매매 종합현황(당일 시간대별). 실전 검증 완료.


def _program_net(token, key, secret, code):
    """종목 당일 프로그램 순매수 '금액(원)' — 최신 시간대(맨 앞) 누적 순매수대금. 실패 시 None."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/comp-program-trade-today",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": PROGRAM_TR_ID},
                         params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=6)
        j = r.json()
        rows = j.get("output") or j.get("output1") or j.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:                          # 최신(맨 앞)부터 비영 값 채택 — 이미 원(元) 단위
            if not isinstance(row, dict):
                continue
            _v = _to_int(row.get("whol_smtn_ntby_tr_pbmn"))
            if _v:
                return _v                         # whol_smtn_ntby_tr_pbmn = 프로그램 순매수 대금(원)
    except Exception:
        pass
    return None


def _program_cum(token, key, secret, code):
    """종목 당일 프로그램 누적 순매수 (금액원, 수량주) — 최신(맨 앞) 비영 행. 실패 시 (None,None)."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/comp-program-trade-today",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": PROGRAM_TR_ID},
                         params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=6)
        j = r.json()
        rows = j.get("output") or j.get("output1") or j.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if not isinstance(row, dict):
                continue
            _a = _to_int(row.get("whol_smtn_ntby_tr_pbmn"))
            _q = _to_int(row.get("whol_smtn_ntby_qty"))
            if _a or _q:
                return _a, _q
    except Exception:
        pass
    return None, None


# [V17.3] 프로그램 누적 시간대 기록 — KIS API는 최근 수분 스냅샷만 줘 하루 시간대 분할 불가.
#   watcher가 3분마다 라인업 종목의 '누적 순매수'를 파일에 적립 → 대시보드가 오전/오후 추세 판독.
PROG_HIST_FILE = os.path.join(BASE, "program_history.json")


def log_program_history(now_kst, token, key, secret, lineup):
    """정규장(09:00~15:30) 동안 라인업 종목의 프로그램 누적 순매수를 시계열로 적립. 예외 전파 없음."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (15 * 60 + 30)):
        return
    today = now_kst.strftime("%Y%m%d")
    try:
        with open(PROG_HIST_FILE, encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        hist = {}
    if hist.get("day") != today:
        hist = {"day": today, "codes": {}}
    codes = hist.setdefault("codes", {})
    for code, _name in lineup:
        try:
            _a, _q = _program_cum(token, key, secret, code)
        except Exception:
            _a = _q = None
        if _a is None and _q is None:
            continue
        ser = codes.setdefault(code, [])
        if not ser or (m - ser[-1][0]) >= 3:          # 최소 3분 간격 적립(중복 방지)
            ser.append([m, int(_a or 0), int(_q or 0)])
            codes[code] = ser[-200:]                   # 하루 상한
    try:
        with open(PROG_HIST_FILE, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
    except Exception:
        pass


# [V13.2 오신호 차단] 수급 전환 격발 임계 —
#   ① 완충대: |순매수 금액| ≥ 20억 넘어야 인정(0선 근처 +0.0억 진동 무시)
#   ② 2루프 연속 확인: 조건이 연속 2회 유지돼야 발송(단발 스파이크 무시)
#   ③ 하락 컷: 매수전환은 현재가 −3%보다 더 빠지는 중이면 억제(급락 중 데드캣 방지)
SUPPLY_FLIP_MIN_EOK = 20        # 완충대(억)
SUPPLY_FLIP_CONFIRM = 2         # 연속 확인 루프 수
SUPPLY_BUY_DROP_CUT = -3.0      # 매수전환 하락 컷(%)


def check_supply_turn(token, key, secret, now_kst, state, token_tg, chat_id, lineup, sev=1):
    """[이원화] 텔레그램은 '음(-)→양(+) 확정 전환'만(격발용). 대시보드 속보판엔 종목별 추세(관측용) 제공.
    반환 (flips, watch):
      flips = 이번에 양전 확정된 종목(텔레그램 발송분)
      watch = 라인업 전 종목 추세 [{name, net_eok, trend}] — trend: pos(🟢양전)/up(🟡개선중,매도둔화)/down(🔻악화)/flat"""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (15 * 60 + 30)):        # 정규장 시간대만
        return [], []
    today = now_kst.strftime("%Y%m%d")
    prev = state.get("supply_prev", {})
    sent = state.get("supply_turn_sent", {})
    if prev.get("_day") != today:
        prev = {"_day": today}
    if sent.get("_day") != today:
        sent = {"_day": today}
    pend = state.get("supply_pending", {})          # 완충대·연속확인·전환이력 추적
    if pend.get("_day") != today:
        pend = {"_day": today}
    _TH = SUPPLY_FLIP_MIN_EOK * 1e8
    def _trend_of(cur, pv):
        if cur >= 0:            return "pos"          # 🟢 순매수(양전)
        if pv is not None and cur > pv:  return "up"  # 🟡 개선 중(매도 둔화)
        if pv is not None and cur < pv:  return "down"
        return "flat"

    flips, watch = [], []
    for code, name in lineup:
        px, chg, _turn = _price_and_turnover(token, key, secret, code)
        if not px:
            continue
        frn_q, org_q = _investor_est(token, key, secret, code)
        _frn, _org = frn_q * px, org_q * px           # 외인·기관 순매수 금액(원) — 각각
        _pv = prev.get(code)                            # [frn_prev, org_prev]
        if not isinstance(_pv, (list, tuple)) or len(_pv) < 2:   # 옛 state(숫자) 방어
            _pv = [None, None]
        _fp, _op = _pv[0], _pv[1]
        # [V13.2] 4주체 분해 — 개인은 근사(= −(외인+기관)), 프로그램은 진짜 API(_program_net)
        _prog = _program_net(token, key, secret, code)   # 원 or None
        _indiv = -(_frn + _org)                          # 개인 근사(순매수 총합≈0 가정)
        # 관측용 추세 — 기관·외인 각각 + 개인/프로그램 값
        watch.append({"name": name, "org_eok": round(_org / 1e8, 1), "frn_eok": round(_frn / 1e8, 1),
                      "org_trend": _trend_of(_org, _op), "frn_trend": _trend_of(_frn, _fp),
                      "indiv_eok": round(_indiv / 1e8, 1),
                      "prog_eok": (round(_prog / 1e8, 1) if _prog is not None else None),
                      "px": px, "chg": round(chg or 0.0, 2)})
        # 🚨 [V13.2] 개인털이 경고 — 개인만 사고 외인·기관 둘 다 파는 자리(완충대↑) → 추격 금지
        if (_indiv >= _TH and _frn <= -_TH and _org <= -_TH
                and not sent.get(code + "_solo")):
            send_telegram(token_tg, chat_id,
                          f"{SIG_CAUTION}\n🚨 개인 홀로 매수 — {name}\n"
                          f"개인(근사) {_indiv/1e8:+,.0f}억 매수인데 외인 {_frn/1e8:+,.0f}억·기관 {_org/1e8:+,.0f}억 동반 매도\n"
                          f"현재가 {px:,} ({(chg or 0):+.2f}%) · {now_kst.strftime('%H:%M')} KST\n"
                          f"⚠️ 세력 이탈 자리 — 개인 추격매수 금지")
            sent[code + "_solo"] = True
        # 텔레그램 격발 — 주체(기관/외인)별. [V13.2] 완충대(±20억) + 2루프 연속 + 하락컷으로 오신호 차단.
        def _flip(who, cur, kb, hint_up, hint_dn):
            # 전환 이력 추적: 오늘 한 번이라도 음(-)/양(+) 완충대를 밟았는지(진짜 '전환'만 인정)
            if cur <= -_TH: pend[kb + "_negseen"] = True
            if cur >= _TH:  pend[kb + "_posseen"] = True
            # 매수전환(음→양): 완충대 양수 + 하락컷 미해당 → 연속 카운트
            _up_ok = (cur >= _TH) and (chg is None or chg > SUPPLY_BUY_DROP_CUT)
            pend[kb + "_upc"] = (pend.get(kb + "_upc", 0) + 1) if _up_ok else 0
            if (pend.get(kb + "_negseen") and pend.get(kb + "_upc", 0) >= SUPPLY_FLIP_CONFIRM
                    and not sent.get(kb + "_up")):
                flips.append({"name": name, "code": code, "who": who, "dir": "up", "net_eok": round(cur / 1e8, 1)})
                if sev != 2:              # [V17.1] 리스크오프면 매수전환 텔레그램 억제(스냅샷엔 유지·해제 시 재발송)
                    send_telegram(token_tg, chat_id,
                                  f"{SIG_BUY}\n🔄 {who} 수급 전환(+) — {name}\n"
                                  f"{who} 순매수 음(-)→양(+) 전환 (완충대 통과·{SUPPLY_FLIP_CONFIRM}루프 확인)\n"
                                  f"{who} 순매수 {cur/1e8:+,.0f}억 · 현재가 {px:,} ({(chg or 0):+.2f}%) · {now_kst.strftime('%H:%M')} KST\n{hint_up}")
                    sent[kb + "_up"] = True
            # 매도전환(양→음): 완충대 음수 → 연속 카운트(하락컷 없음 — 급락 중 이탈은 유효)
            _dn_ok = (cur <= -_TH)
            pend[kb + "_dnc"] = (pend.get(kb + "_dnc", 0) + 1) if _dn_ok else 0
            if (pend.get(kb + "_posseen") and pend.get(kb + "_dnc", 0) >= SUPPLY_FLIP_CONFIRM
                    and not sent.get(kb + "_dn")):
                flips.append({"name": name, "code": code, "who": who, "dir": "down", "net_eok": round(cur / 1e8, 1)})
                send_telegram(token_tg, chat_id,
                              f"{SIG_SELL}\n⚠️ {who} 수급 이탈(-) — {name}\n"
                              f"{who} 순매수 양(+)→음(-) 전환 (완충대 통과·{SUPPLY_FLIP_CONFIRM}루프 확인)\n"
                              f"{who} 순매수 {cur/1e8:+,.0f}억 · 현재가 {px:,} ({(chg or 0):+.2f}%) · {now_kst.strftime('%H:%M')} KST\n{hint_dn}")
                sent[kb + "_dn"] = True

        _flip("기관", _org, code + "_org", "👀 빨간선(기관) 고개 — 외인도 붙는지 확인", "🛡️ 보유 시 탈출/손절 점검 — 빨간선 꺾임")
        _flip("외인", _frn, code + "_frn", "👀 파란선(외인) 고개 — 기관과 쌍끌이면 강력", "🛡️ 보유 시 탈출/손절 점검 — 파란선 꺾임")
        prev[code] = [_frn, _org]                      # 다음 루프 비교용(각각·관측 trend에 사용)
    state["supply_prev"] = prev
    state["supply_turn_sent"] = sent
    state["supply_pending"] = pend
    return flips, watch


def _market_investor():
    """[시장 전체] 코스피 당일 누적 외국인·기관 순매수(원) — 네이버 일별 투자자 동향 스크랩.
    반환 (외인_원, 기관_원, 기준일) 또는 (None,None,None). watcher는 PC에서 돌아 네이버 접근 가능."""
    import re as _re
    _hdr = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Referer": "https://finance.naver.com/sise/",
            "Accept-Language": "ko-KR,ko;q=0.9"}
    for _url in ("https://finance.naver.com/sise/investorDealTrendDay.naver",
                 "https://finance.naver.com/sise/sise_deal_trend_day.naver"):
        try:
            _r = requests.get(_url, headers=_hdr, timeout=8)
            _r.encoding = "euc-kr"
            _html = _r.text
        except Exception:
            continue
        for _row in _re.findall(r"<tr[^>]*>(.*?)</tr>", _html, _re.S):
            _cells = _re.findall(r"<td[^>]*>(.*?)</td>", _row, _re.S)
            _clean = [_re.sub(r"<[^>]+>", "", _c).replace("&nbsp;", "").strip().replace(",", "")
                      for _c in _cells]
            # 컬럼: 날짜 | 개인 | 외국인 | 기관계 ...
            if len(_clean) >= 4 and _re.match(r"\d{2}[.\-/]\d{2}", _clean[0] or ""):
                def _num(_s):
                    try:
                        return float(_s) if _s.lstrip("+-").replace(".", "").isdigit() else None
                    except Exception:
                        return None
                _frn, _org = _num(_clean[2]), _num(_clean[3])
                if _frn is not None and _org is not None:
                    return _frn * 1e8, _org * 1e8, _clean[0]
    return None, None, None


def check_market_supply_turn(now_kst, state, token_tg, chat_id, sev=1):
    """[시장 전체] 코스피 기관·외인 순매수 음↔양 전환 알림 — 순환매/시장 바닥·꼭지 신호.
    방향·주체별 당일 1회. 반환 스냅샷용 dict 또는 None."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (15 * 60 + 30)):
        return None
    _frn, _org, _basis = _market_investor()
    if _org is None:
        return None
    today = now_kst.strftime("%Y%m%d")
    prev = state.get("mkt_prev", {}); sent = state.get("mkt_sent", {})
    if prev.get("_day") != today: prev = {"_day": today}
    if sent.get("_day") != today: sent = {"_day": today}
    _op, _fp = prev.get("org"), prev.get("frn")
    _stamp = now_kst.strftime("%H:%M")

    def _emit(cond_up, cond_dn, key, who, val):
        if cond_up and sev != 2 and not sent.get(key + "_up"):   # [V17.1] 리스크오프 매수 억제
            send_telegram(token_tg, chat_id,
                          f"{SIG_BUY}\n📈 시장 전체 {who} 순매수 전환(+) — 코스피\n"
                          f"코스피 {who} 순매수 음(-)→양(+) 전환\n"
                          f"시장 {who} {val/1e8:+,.0f}억 · {_stamp} KST\n"
                          f"👀 순환매 시작·시장 바닥 반등 초입 — 오늘 판 깔기 좋은 자리")
            sent[key + "_up"] = True
        if cond_dn and not sent.get(key + "_dn"):
            send_telegram(token_tg, chat_id,
                          f"{SIG_SELL}\n⚠️ 시장 전체 {who} 이탈(-) — 코스피\n"
                          f"코스피 {who} 순매수 양(+)→음(-) 전환\n"
                          f"시장 {who} {val/1e8:+,.0f}억 · {_stamp} KST\n"
                          f"🛡️ 시장 전체 매도 전환 — 신규 진입 자제·비중 축소 점검")
            sent[key + "_dn"] = True

    _emit(_op is not None and _op < 0 <= _org, _op is not None and _op >= 0 > _org, "org", "기관", _org)
    _emit(_fp is not None and _fp < 0 <= _frn, _fp is not None and _fp >= 0 > _frn, "frn", "외인", _frn)
    prev["org"], prev["frn"] = _org, _frn
    state["mkt_prev"], state["mkt_sent"] = prev, sent
    return {"org_eok": round(_org / 1e8), "frn_eok": round(_frn / 1e8), "basis": _basis}


# [2-Tier] 6대 코어 섹터 원톱 대장주 — 섹터 순환매 선도 감지용
SECTOR_LEADERS = {
    "반도체":   [("005930", "삼성전자"), ("000660", "SK하이닉스")],
    "바이오":   [("196170", "알테오젠"), ("068270", "셀트리온"), ("207940", "삼성바이오로직스")],
    "2차전지":  [("373220", "LG에너지솔루션"), ("247540", "에코프로비엠")],
    "원전":     [("034020", "두산에너빌리티")],
    "방산":     [("012450", "한화에어로스페이스")],
    "인터넷":   [("035420", "NAVER")],
}


def check_sector_leaders(token, key, secret, now_kst, state, token_tg, chat_id, market_positive, sev=1):
    """[2-Tier + Confluence] 6대 섹터 대장주 실측 수급 관측(속보판) + 시장·섹터 '정렬' 시에만 텔레그램.
    개별 대장주 단순 양전 알림은 발송 안 함(소음 차단). market_positive=시장 전체 양전 여부.
    반환: 관측용 리스트 [{sector,name,code,net_eok,positive}]."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (15 * 60 + 30)):
        return []
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("confluence_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    observe = []
    for sector, leaders in SECTOR_LEADERS.items():
        _best = None      # 섹터 내 순매수액 최대 대장주
        for code, name in leaders:
            px, chg, _turn = _price_and_turnover(token, key, secret, code)
            if not px:
                continue
            frn_q, org_q = _investor_est(token, key, secret, code)
            _net = (frn_q + org_q) * px
            observe.append({"sector": sector, "name": name, "code": code,
                            "net_eok": round(_net / 1e8, 1), "positive": _net >= 0})
            if _best is None or _net > _best[0]:
                _best = (_net, name, code, px, chg)
        # Confluence: 시장 전체 양전 + 이 섹터 대장 양전 정렬 → 섹터별 당일 1회
        if _best and _best[0] >= 0 and market_positive and sev != 2 and not sent.get(sector):   # [V17.1] 리스크오프 매수(강) 억제
            _net, _nm, _cd, _px, _chg = _best
            send_telegram(token_tg, chat_id,
                          f"{SIG_BUY_STRONG}\n🎯 [정렬 신호] 시장 + {sector} 대장 동시 양전!\n"
                          f"시장 전체 자금 유입 + {sector} 대장({_nm}) 수급 양전 정렬\n"
                          f"{_nm} 외인+기관 {_net/1e8:+,.0f}억 · {_px:,} ({_chg:+.2f}%) · {now_kst.strftime('%H:%M')} KST\n"
                          f"🚀 {sector} 소부장·테마 사격 준비 — 라인업 개별 양전 대기")
            sent[sector] = True
    state["confluence_sent"] = sent
    return observe


# ── [V6.1-B] 14:30 V자 턴어라운드 (watcher 이관) ──
TA_UNIVERSE = [("005930", "삼성전자"), ("000660", "SK하이닉스"), ("042700", "한미반도체"),
               ("196170", "알테오젠"), ("068270", "셀트리온"), ("207940", "삼성바이오로직스"),
               ("373220", "LG에너지솔루션"), ("247540", "에코프로비엠"), ("034020", "두산에너빌리티"),
               ("012450", "한화에어로스페이스"), ("035420", "NAVER")]


def _price_full(token, key, secret, code):
    """현재가·등락률·시가·고가·저가 — inquire-price. 실패 시 (None,...)."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010100"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=6)
        o = r.json().get("output", {})
        if isinstance(o, dict) and o:
            return (_to_int(o.get("stck_prpr")),
                    float(str(o.get("prdy_ctrt", 0)).replace(",", "") or 0),
                    _to_int(o.get("stck_oprc")), _to_int(o.get("stck_hgpr")), _to_int(o.get("stck_lwpr")))
    except Exception:
        pass
    return None, None, None, None, None


def _tail_ok(o, h, l, c, ratio=0.33):
    """아래꼬리 판정 — (min(시,종)-저) ≥ 범위×ratio."""
    if not all(isinstance(v, (int, float)) and v > 0 for v in (o, h, l, c)):
        return False
    rng = h - l
    return bool(rng > 0 and (min(o, c) - l) >= rng * ratio)


def check_turnaround(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V6.1-B] 14:15~15:20 정렬 대기 + 15:20 동시호가 타격 2단계 폰 알림(대시보드 불필요).
    4대 정렬: ①패닉셀(당일저점≤-7%) ②나스닥선물≥-0.5% ③매도둔화(순매수 기울기0↑) ④아래꼬리 지지.
    반환: 스냅샷용 리스트."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((14 * 60 + 15) <= m <= (15 * 60 + 22)):
        return []
    _strike = (15 * 60 + 20) <= m <= (15 * 60 + 22)
    nq = _pct("NQ=F")
    _macro_ok = (nq is not None and nq >= -0.5)
    today = now_kst.strftime("%Y%m%d")
    prev = state.get("ta_prev", {}); sent = state.get("ta_sent", {})
    if prev.get("_day") != today: prev = {"_day": today}
    if sent.get("_day") != today: sent = {"_day": today}
    out = []
    for code, name in TA_UNIVERSE:
        px, chg, o, h, l = _price_full(token, key, secret, code)
        if not px:
            continue
        _prev_close = px / (1 + (chg or 0) / 100.0) if chg is not None else 0
        _panic = bool(_prev_close > 0 and l and (l / _prev_close - 1) * 100 <= -7.0)
        frn_q, org_q = _investor_est(token, key, secret, code)
        _cur_net = frn_q + org_q
        _pv = prev.get(code)
        _supply = bool(_cur_net >= 0 or (_pv is not None and _cur_net >= _pv))
        prev[code] = _cur_net
        _tail = _tail_ok(o, h, l, px)
        if not (_panic and _macro_ok and _supply and _tail):
            continue
        out.append({"name": name, "code": code, "px": px, "chg": chg or 0.0})
        _ps = f"{px:,} ({(chg or 0):+.2f}%)"
        if not _strike and sev != 2 and not sent.get(code + "_wait"):   # [V17.1] 리스크오프 매수 억제
            send_telegram(token_tg, chat_id,
                          f"{SIG_BUY}\n🔥 14:30 턴어라운드 정렬 — {name}\n"
                          f"패닉셀 멈춤+나스닥선물 반등+지지 정렬\n"
                          f"현재가 {_ps} · {now_kst.strftime('%H:%M')} KST\n"
                          f"⏳ 지금은 관망 · 15:20 정각 동시호가 대기 (섣부른 진입 금지)")
            sent[code + "_wait"] = True
        if _strike and sev != 2 and not sent.get(code + "_strike"):   # [V17.1] 리스크오프 매수(강) 억제
            send_telegram(token_tg, chat_id,
                          f"{SIG_BUY_STRONG}\n⏰ 지금 타격 — {name} (15:20 동시호가)\n"
                          f"TURNAROUND_STRIKE 정렬 확정\n"
                          f"현재가 {_ps} · {now_kst.strftime('%H:%M')} KST\n"
                          f"🎯 여러 후보 중 하나만! 동시호가 시장가 '딱 1주'(테스트)~최대 50%(HALF_CAPS)\n"
                          f"⚠️ 저점 이탈 시 −2% 손절")
            sent[code + "_strike"] = True
    state["ta_prev"], state["ta_sent"] = prev, sent
    return out


# ── [넥장] 넥스트레이드 야간 애프터마켓(16:00~20:00) 급등 타점 ──
#   ⚠️ 야간엔 외인/기관 수급 추정 API가 안 나옴(정규장 전용) → 가격 급등 + 거래대금 급증만으로 잡음.
#   야간 유동성 얇아 착시 위험 → ①야간 시작가 대비 급등 ②거래대금 바닥값 이상 ③거래대금 증가 3중 게이트.
_NXT_START, _NXT_END = 16 * 60, 19 * 60 + 50   # 넥장 감시창(16:00~19:50 KST)
_NXT_JUMP_PCT = 1.5          # 야간 시작가(16시경) 대비 상승 몸통 기준(%)
_NXT_MIN_TURN = 5_000_000_000  # NXT 야간 누적거래대금 바닥값 50억(얇은 유동성 노이즈 컷)


def check_nxt_after(token, key, secret, now_kst, state, token_tg, chat_id, lineup, sev=1):
    """넥장(넥스트레이드 야간) 급등 타점 — 16:00~19:50, 라인업 종목을 'NX' 시장코드로 야간 조회.
    야간 시작가 대비 +1.5%↑ AND 거래대금 50억↑·증가 시 종목별 당일 1회 텔레그램. 반환: 스냅샷용 리스트.
    ※ 수급 확인 불가(야간 미제공) — 가격/거래대금 기반 급등 신호."""
    m = now_kst.hour * 60 + now_kst.minute
    if not (_NXT_START <= m <= _NXT_END):
        return []
    today = now_kst.strftime("%Y%m%d")
    base = state.get("nxt_base", {})
    sent = state.get("nxt_sent", {})
    if base.get("_day") != today: base = {"_day": today}
    if sent.get("_day") != today: sent = {"_day": today}
    out = []
    for code, name in lineup:
        px, chg, turn = _price_and_turnover(token, key, secret, code, mrkt="NX")
        if not px:
            continue
        _b = base.get(code)
        # 야간 시작 기준가 최초 1회 고정(16시 첫 관측가)
        if not _b:
            base[code] = {"px": px, "turn": turn or 0}
            continue
        _move = (px / _b["px"] - 1) * 100
        _tdelta = (turn - _b.get("turn", 0)) if turn else 0
        if (_move >= _NXT_JUMP_PCT and turn and turn >= _NXT_MIN_TURN
                and _tdelta > 0 and not sent.get(code)):
            out.append({"name": name, "code": code, "px": px, "chg": chg or 0.0,
                        "night_move": round(_move, 2), "turnover_eok": round(turn / 1e8, 0)})
            if sev != 2:              # [V17.1] 리스크오프면 야간 매수 억제(스냅샷엔 유지)
                send_telegram(token_tg, chat_id,
                              f"{SIG_BUY}\n🌙[야간·NXT소액] 넥장 급등 타점 — {name}\n"
                              f"야간 시작가 대비 +{_move:.1f}% · NXT 거래대금 {turn/1e8:,.0f}억\n"
                              f"현재가 {px:,} (전일 {(chg or 0):+.2f}%) · {now_kst.strftime('%H:%M')} KST\n"
                              f"⚠️ 야간=수급확인 불가·유동성 얇음 → 소액·−2% 손절 필수 · 추격 금지")
                sent[code] = True
    state["nxt_base"], state["nxt_sent"] = base, sent
    return out


# ── [김팀장式] 15분봉 강한 양봉 확정 → 다음 봉 진입 신호 ──
_BAR15_PCT = 1.0   # 15분 구간 상승 몸통 기준(%) — 김팀장 2%는 대형주엔 빡세 1.0%부터(조정 가능)


def check_bar15(token, key, secret, now_kst, state, token_tg, chat_id, lineup, sev=1):
    """15분봉 강한 양봉 확정 감지 — 15분 경계(09:15,09:30…)마다 직전 15분 구간 상승/거래대금 증가 판정.
    강한 양봉 마감 시 '다음 봉 진입' 알림(종목·봉별 당일 1회). ※ 현재가·거래대금 기반 근사(진짜 캔들 아님).
    반환: 스냅샷용 리스트."""
    m = now_kst.hour * 60 + now_kst.minute
    # [V13.2 투트랙] 마의 구간(09:00~09:15)은 시가저격(3분봉) 전용 → 15분봉 확정 알림은 09:15부터 가동.
    if not ((9 * 60 + 15) <= m <= (15 * 60 + 20)):
        return []
    today = now_kst.strftime("%Y%m%d")
    _bidx = m // 15                                  # 현재 15분 버킷 인덱스
    _nq = _pct("NQ=F")                               # 나스닥100 선물 — 김팀장式 '선물 동조' 확인
    _nq_pv = state.get("bar15_nq_prev")              # 직전 루프 NQ (상승 기울기 판정용)
    _nq_rising = (_nq_pv is None) or (_nq is None) or (_nq >= _nq_pv)   # 선물이 오르는 중(기울기 ≥ 0)
    _nq_sync = (_nq is None) or (_nq >= 0 and _nq_rising)  # 0선↑ AND 상승 기울기 = 진짜 동조
    state["bar15_nq_prev"] = _nq
    mark = state.get("bar15_mark", {})
    sent = state.get("bar15_sent", {})
    if mark.get("_day") != today: mark = {"_day": today}
    if sent.get("_day") != today: sent = {"_day": today}
    out = []
    for code, name in lineup:
        px, chg, turn = _price_and_turnover(token, key, secret, code)
        if not px:
            continue
        _mk = mark.get(code)
        # 버킷이 바뀌었다 = 직전 15분봉 마감 → 그 구간 상승/거래대금 증가 평가
        if _mk and _mk.get("bidx") != _bidx and _mk.get("px"):
            _move = (px / _mk["px"] - 1) * 100
            _tdelta = (turn - _mk.get("turn", 0)) if turn else 0
            _key = f"{code}_{_bidx}"
            # 김팀장式 3요건: ①15분 +1%↑ 강양봉 ②거래대금 증가 ③나스닥선물 동조(0선↑)
            if _move >= _BAR15_PCT and _tdelta > 0 and _nq_sync and not sent.get(_key):
                _nqtxt = (f"나스닥선물 {_nq:+.2f}% 상승동조🟢" if _nq is not None else "나스닥선물 확인불가")
                # 20일선 이격 — 과열(추격) 자리인지 친절히 안내
                _disp = _ma20_disparity(token, key, secret, code, px)
                _res = _recent_high(token, key, secret, code)     # 저항선(최근 20일 고가)
                out.append({"name": name, "code": code, "px": px, "chg": chg or 0.0,
                            "bar_move": round(_move, 2), "nq": _nq, "disp": _disp, "res": _res})
                _buy = px
                _stop = int(px * 0.98)                             # 손절 −2%
                if _disp is None:
                    _warn = "• 이격: 확인불가 (HTS에서 20일선 위치 확인)"
                elif _disp >= 12:
                    _warn = f"• ⚠️ 20일선 이격 +{_disp:.1f}% = 심한 과열! 지금은 추격 자리 — 눌림 기다리기 권장"
                elif _disp >= 7:
                    _warn = f"• ⚠️ 20일선 이격 +{_disp:.1f}% = 과열 주의 — 소량·타이트 손절만"
                else:
                    _warn = f"• 20일선 이격 +{_disp:.1f}% = 아직 여유 있음(추격 아님)"
                # 돌파 기준가(저항선) & 돌파 후 하락 매도가
                if _res and _res > px:
                    _hold = _res                                   # 이 가격 뚫으면(돌파) 홀딩
                    _resell = int(_res * 0.99)                     # 뚫었다 다시 이 밑으로 내려오면 매도
                    _res_line = (f"🚀 돌파 홀딩 기준: {_hold:,}원 (이 위로 뚫으면 계속 보유)\n"
                                 f"🔻 돌파 후 하락 매도: {_resell:,}원 (뚫었다 다시 이 밑이면 매도)")
                else:
                    _res_line = "🚀 저항 위 = 신고가권(뚜렷한 저항 없음) — 고점 갱신 실패 시 매도"
                if sev != 2:          # [V17.1] 리스크오프면 15분봉 매수 억제(스냅샷엔 유지)
                    send_telegram(token_tg, chat_id,
                                  f"{SIG_BUY}\n🌅[장중단타·당일청산] 📊 15분봉 강한 양봉 — {name}\n"
                                  f"방금 막 끝난 15분봉이 +{_move:.1f}% 강하게 올랐고 거래대금도 늘었어요.\n"
                                  f"{_nqtxt}\n"
                                  f"• 현재가 {px:,}원 ({(chg or 0):+.2f}%) · {now_kst.strftime('%H:%M')} KST\n"
                                  f"{_warn}\n"
                                  f"─── 가격표 ───\n"
                                  f"🎯 매수가(현재): {_buy:,}원\n"
                                  f"✂️ 손절가: {_stop:,}원 (−2%)\n"
                                  f"{_res_line}\n"
                                  f"─────────\n"
                                  f"👉 HTS 열어 ①기관 붙었나 ②이격 과열 아닌가 확인 후 타격")
                    sent[_key] = True
                    _log_signal(state, now_kst, "15분봉", name, code, px)
        # 새 버킷이면 기준점(봉 시작가·거래대금) 갱신
        if not _mk or _mk.get("bidx") != _bidx:
            mark[code] = {"bidx": _bidx, "px": px, "turn": turn or 0}
    state["bar15_mark"], state["bar15_sent"] = mark, sent
    return out


def check_entries(token, key, secret, now_kst, state, token_tg, chat_id, lineup, sev=1):
    """진입(초록) 3-조건 상시 감시 — 라인업, 제로아워(09:00~10:00) 창.
    조건: 거래대금 ≥ 임계(대형300/중소150억) AND 프로그램·외인·기관 추정금액 모두 (+).
    신규 충족 종목만 종목별 당일 1회 텔레그램. 반환: 스냅샷용 리스트.
    [V16.9 다이어트] sev==2(리스크오프) 또는 낙폭과대 급락주(이격≤-10%+하락)면 발송 억제(스냅샷엔 남김)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (10 * 60)):            # 만쥬 제로아워 밖 → 감시 안 함
        return []
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("entry_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    out = []
    for code, name in lineup:
        px, chg, turn = _price_and_turnover(token, key, secret, code)
        if not px or not turn:
            continue
        need = SNIPER_LARGE if px >= SNIPER_LARGECAP_PX else SNIPER_SMALL
        frn_q, org_q = _investor_est(token, key, secret, code)
        prog_amt, frn_amt, org_amt = (frn_q + org_q) * px, frn_q * px, org_q * px
        # [V13.9] 급등 대장주 역설 해소 — '외인·기관 모두 +'(and)는 갭상승 급등주에서 기관 차익실현(-)에 걸려 탈락.
        #   → 외인 순매수(+) 필수 & 외인+기관 순매수 합(+)이면 통과(기관 매도해도 외인이 더 크면 진입).
        entry_ok = (turn >= need and frn_amt > 0 and prog_amt > 0)
        if not entry_ok:
            continue
        _org_txt = "외인·기관 동반(+)" if org_amt > 0 else "외인 주도(기관 차익실현 中)"
        # [V16.1] 과열 게이트 — 이미 +5%↑ 급등 or 20MA 이격 7%↑면 '매수(강)' 대신 '관찰(눌림 대기)'.
        #   추격 방지: 수급 확인형 신호는 늦어서 이미 오른 뒤 뜸 → 과열이면 눌림에서 재진입.
        _disp = None
        try:
            _disp = _ma20_disparity(token, key, secret, code, px)
        except Exception:
            pass
        _overheat = ((chg is not None and chg >= 5.0) or (_disp is not None and _disp >= 7.0))
        # [V16.9] 낙폭과대 급락주(떨어지는 칼) — 이격 -10%↓ + 당일 하락. 수급(+)이어도 추격 금지.
        _falling = (_disp is not None and _disp <= -10.0 and (chg or 0) < -1.0)
        _disp_txt = f" · 20MA 이격 {_disp:+.1f}%" if _disp is not None else ""
        out.append({"name": name, "code": code, "px": px, "chg": chg,
                    "turnover_eok": round(turn / 1e8, 0),
                    "prog_eok": round(prog_amt / 1e8, 1)})
        if sev == 2 or _falling:               # [V16.9] 리스크오프·낙폭과대 → 매수 신호 억제(모순/추격 방지)
            continue
        if _overheat:
            _wkey = "w_" + code
            if not sent.get(_wkey):
                send_telegram(token_tg, chat_id,
                              f"{SIG_WATCH}\n🟡 관찰(과열) — {name}\n"
                              f"이미 +{(chg or 0):.1f}% 급등{_disp_txt} — 추격 금지, 눌림(20MA/전고 지지) 대기\n"
                              f"수급: 외인 {frn_amt/1e8:+,.0f}억 · 거래대금 {turn/1e8:,.0f}억 · {px:,} · {now_kst.strftime('%H:%M')} KST\n"
                              f"※눌림 와서 과열 풀리면 '진입 시그널' 재발송")
                sent[_wkey] = True
        elif not sent.get(code):
            # [V18.3] 뉴스 재료 확인 — 악재면 진입 스킵, 재료 등급은 메시지에 표기
            _eng, _ebad = _news_grade(code)
            if _ebad:
                continue                          # 악재 감지 → 진입 보류
            _emat = ("🔥재료 강함(S급)" if _eng == "S" else "🟢재료 있음(A급)" if _eng == "A"
                     else "⚠️재료 미확인(순수 수급)")
            send_telegram(token_tg, chat_id,
                          f"{SIG_BUY_STRONG}\n🌅[아침단타·당일청산] 🟢 진입 시그널 — {name}\n"
                          f"거래대금 {turn/1e8:,.0f}억(임계 {need/1e8:,.0f}↑) · {_org_txt} · 순매수 합 (+){_disp_txt} · {_emat}\n"
                          f"외인 {frn_amt/1e8:+,.0f}억 · 기관 {org_amt/1e8:+,.0f}억 · 현재가 {px:,} ({(chg or 0):+.2f}%) · {now_kst.strftime('%H:%M')} KST\n"
                          f"🔌 HTS 동기화 후 원클릭 타격 · -1R 손절 세팅")
            sent[code] = True
            _log_signal(state, now_kst, "진입", name, code, px)   # [V17.6] 성적표 적립
    state["entry_sent"] = sent
    return out


def check_nq_cross(now_kst, state, token_tg, chat_id):
    """나스닥선물 0선 돌파 알림 — 음(-)→양(+) 반등 / 양(+)→음(-) 악화. 방향별 120분 쿨다운(출렁임 방지).
    장 관련 시간(08:00~20:00)만. 반환: 현재 nq%(스냅샷 참고용) 또는 None."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((8 * 60) <= m <= (20 * 60)):
        return None
    nq = _pct("NQ=F")
    if nq is None:
        return None
    _prev = state.get("nq_prev")
    _now_ts = int(now_kst.timestamp())
    # [V16.9 다이어트] 0선 데드밴드 — |nq| < 0.15%면 '돌파'로 안 침(0.00% 근처 진동 무시).
    #   + 반전 가드: 직전 반대방향 크로스로부터 30분 이내면 발송 안 함(양↔음 핑퐁 차단).
    _NQ_DEAD = 0.15
    _last_cross_ts = max(int(state.get("nq_up_ts", 0)), int(state.get("nq_dn_ts", 0)))
    _reversal_ok = (_now_ts - _last_cross_ts) >= 1800
    if _prev is not None:
        if _prev < 0 <= nq and nq >= _NQ_DEAD and _reversal_ok and (_now_ts - int(state.get("nq_up_ts", 0))) >= 7200:
            send_telegram(token_tg, chat_id,
                          f"{SIG_INFO}\n📈 나스닥선물 양전(+) — 0선 상향 돌파\n"
                          f"나스닥100 선물 {nq:+.2f}% (음→양 전환)\n"
                          f"{now_kst.strftime('%m/%d %H:%M')} KST · 익일 갭상승 힌트·위험선호 회복")
            state["nq_up_ts"] = _now_ts
        elif _prev >= 0 > nq and nq <= -_NQ_DEAD and _reversal_ok and (_now_ts - int(state.get("nq_dn_ts", 0))) >= 7200:
            send_telegram(token_tg, chat_id,
                          f"{SIG_CAUTION}\n📉 나스닥선물 음전(-) — 0선 하향 돌파\n"
                          f"나스닥100 선물 {nq:+.2f}% (양→음 전환)\n"
                          f"{now_kst.strftime('%m/%d %H:%M')} KST · 위험회피↑·신규매수 주의")
            state["nq_dn_ts"] = _now_ts
    state["nq_prev"] = nq
    return nq


NQ_SUPPORT_MIN = 15   # 저점 근처에서 신저가 없이 이 분(分)만큼 버티면 '지지(하락 멈춤)' 예고


def check_nq_rebound(now_kst, state, token_tg, chat_id):
    """[V14.5/V14.7] 나스닥선물 2단계 — ①지지(하락 멈춤) 예고 → ②반등 확정. 반도체 선행지표.
    저점 -0.5%↓ 빠진 뒤: 15분+ 신저가 없으면 🟡지지 예고, +0.4%p 회복하면 🔔반등 확정. 08:00~20:00만."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((8 * 60) <= m <= (20 * 60)):
        return None
    nq = _pct("NQ=F")
    if nq is None:
        return None
    now_ts = int(now_kst.timestamp())
    today = now_kst.strftime("%Y%m%d")
    r = state.get("nq_reb") or {}
    if r.get("day") != today:
        r = {"day": today, "low": nq, "low_ts": now_ts, "alerted": False, "support": False}
    if nq < r.get("low", nq):                        # 새 저점 → 지지·반등 둘 다 재무장
        r["low"] = nq; r["low_ts"] = now_ts; r["alerted"] = False; r["support"] = False
    low = r.get("low", nq); reb = nq - low
    try:
        wti = _wti_pct()
    except Exception:
        wti = None
    wti_txt = f" · WTI {wti:+.2f}%" if isinstance(wti, (int, float)) else ""
    # ① 지지(하락 멈춤) 예고 — 저점 -0.5%↓ + 신저가 15분+ 없음 + 아직 반등확정 전(예고일 뿐, 매수 아님)
    if (low <= -0.5 and not r.get("support") and not r.get("alerted")
            and reb < 0.4 and (now_ts - r.get("low_ts", now_ts)) >= NQ_SUPPORT_MIN * 60):
        send_telegram(token_tg, chat_id,
                      f"{SIG_WATCH}\n🟡 나스닥선물 하락 멈춤(지지) — 저점 {low:+.2f}% 근처 {NQ_SUPPORT_MIN}분+ 버팀{wti_txt}\n"
                      f"반도체 대장주 반등 '준비' 단계 (아직 매수 아님 — 반등 확정 대기)\n"
                      f"{now_kst.strftime('%m/%d %H:%M')} KST")
        r["support"] = True
    # ② 반등 확정 — 저점 대비 +0.4%p 회복
    if low <= -0.5 and reb >= 0.4 and not r.get("alerted"):
        send_telegram(token_tg, chat_id,
                      f"{SIG_WATCH}\n🔔 나스닥선물 반등 확정 — 저점 {low:+.2f}% → 현재 {nq:+.2f}% (+{reb:.2f}%p){wti_txt}\n"
                      f"반도체 대장주(삼성전자·SK하이닉스) 반등 초입 주목\n"
                      f"{now_kst.strftime('%m/%d %H:%M')} KST · 나선 따라 반도체 반등 가능 — 수급 확인 후 대응")
        r["alerted"] = True
    state["nq_reb"] = r
    return nq


def check_index_rebound(now_kst, state, token_tg, chat_id):
    """[V14.6] 지수(코스피/코스닥) 오후 저점 대비 반등 알림 — 종가베팅 타이밍.
    오후 12:00~15:20 집중. 지수 -0.7%↓ 빠진 뒤 +0.3%p 회복 시 1회 알림(새 저점 시 재무장)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((12 * 60) <= m <= (15 * 60 + 20)):     # 오후(점심 후~종가전)만
        return None
    kp = _pct("^KS11"); kq = _pct("^KQ11")
    vals = [x for x in (kp, kq) if x is not None]
    if not vals:
        return None
    idx = sum(vals) / len(vals)
    today = now_kst.strftime("%Y%m%d")
    r = state.get("idx_reb") or {}
    if r.get("day") != today:
        r = {"day": today, "low": idx, "alerted": False}
    if idx < r.get("low", idx):
        r["low"] = idx; r["alerted"] = False       # 새 저점 → 재무장
    low = r.get("low", idx); reb = idx - low
    if low <= -0.7 and reb >= 0.3 and not r.get("alerted"):
        kp_txt = f"코스피 {kp:+.2f}%" if kp is not None else ""
        kq_txt = f"코스닥 {kq:+.2f}%" if kq is not None else ""
        _both = " · ".join(x for x in (kp_txt, kq_txt) if x)
        send_telegram(token_tg, chat_id,
                      f"{SIG_WATCH}\n🔔 지수 오후 반등 — 저점 {low:+.2f}% → 현재 {idx:+.2f}% (+{reb:.2f}%p)\n"
                      f"{_both}\n오후 저점 반등 = 종가베팅 타이밍 주목 · 수급(외인·기관)·종목 확인 후 대응\n"
                      f"{now_kst.strftime('%m/%d %H:%M')} KST")
        r["alerted"] = True
    state["idx_reb"] = r
    return idx


# [V13.7] 야간 미장 흐름 알림 — 20:00~익일 08:00(미국장 22:30~05:00 KST 커버).
US_OVN_SOX_THR = 2.0   # SOX ±2% 돌파 시 야간 알림(내일 아침 반도체 갭 힌트)


def check_us_overnight(now_kst, state, token_tg, chat_id):
    """미장 야간 흐름 알림 — 20:00~08:00. 반도체(SOX) 중심 강/약 이벤트 + 나스닥선물·주요 반도체 맥락.
    SOX ±2% 돌파 시 방향별 120분 쿨다운으로 1회 알림(스팸 방지). 국장 종배·아침 대응 힌트.
    ※ 20:00~22:30은 미국 현물 개장 전이라 SOX가 전일 종가일 수 있음(나스닥선물은 야간 실시간)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not (m >= 20 * 60 or m <= 8 * 60):   # 야간 창(20:00~익일 08:00)만
        return None
    sox = _pct("^SOX")
    nq = _pct("NQ=F")
    if sox is None:
        return None
    _now_ts = int(now_kst.timestamp())
    # [V16.9 다이어트] '밤당 1회(방향별)' — 같은 SOX 약세/강세를 새벽 내내 반복 발송하던 스팸 제거.
    #   밤 id: 새벽(08시 이전)은 전날 저녁 세션 소속 → 전일 날짜로 묶음.
    _night = (now_kst - datetime.timedelta(days=1)).strftime("%Y%m%d") if m <= 8 * 60 else now_kst.strftime("%Y%m%d")

    def _semis_txt():
        _parts = []
        for _tk, _nm in (("NVDA", "엔비디아"), ("AVGO", "브로드컴"), ("MU", "마이크론")):
            _v = _pct(_tk)
            if _v is not None:
                _parts.append(f"{_nm} {_v:+.1f}%")
        return " · ".join(_parts)

    _nqtxt = f"나스닥선물 {nq:+.2f}%" if nq is not None else "나스닥선물 확인불가"
    if sox >= US_OVN_SOX_THR and state.get("us_ovn_up_night") != _night:
        _semis = _semis_txt()
        send_telegram(token_tg, chat_id,
                      f"{SIG_BUY}\n🌙🟢 미장 반도체 강세 — SOX {sox:+.2f}%\n"
                      f"{_nqtxt}" + (f" · {_semis}" if _semis else "") + "\n"
                      f"{now_kst.strftime('%m/%d %H:%M')} KST · 내일 아침 반도체 갭상승 우호")
        state["us_ovn_up_night"] = _night
    elif sox <= -US_OVN_SOX_THR and state.get("us_ovn_dn_night") != _night:
        _semis = _semis_txt()
        send_telegram(token_tg, chat_id,
                      f"{SIG_CAUTION}\n🌙🔴 미장 반도체 약세 — SOX {sox:+.2f}%\n"
                      f"{_nqtxt}" + (f" · {_semis}" if _semis else "") + "\n"
                      f"{now_kst.strftime('%m/%d %H:%M')} KST · 내일 아침 갭하락 주의·종배 비중 축소")
        state["us_ovn_dn_night"] = _night
    return sox


def _pick_mode(now_kst):
    """현재 KST 시각 기준 오늘의 픽 성격. 만쥬=오전 초단타(09~10), 돌팬티=오후 종가베팅(13~15:30)."""
    m = now_kst.hour * 60 + now_kst.minute
    if 9 * 60 <= m <= 10 * 60:
        return {"tag": "만쥬式", "when": "오전 초단타 (09~10시 승부처)"}
    if 13 * 60 <= m <= 15 * 60 + 30:
        return {"tag": "돌팬티式", "when": "오후 종가베팅 구간"}
    return {"tag": "참고", "when": "픽 유효 시간대 아님 (참고용)"}


# ══════════════════════════════════════════════════════════════════════════
# 📓 매매일지 & 복기 — 초보용 자동 브리핑(08:50) + 복기 리포트(15:35)
#   하루 동안 뜬 신호(시가저격·진입·전조·A급·매크로 국면)를 state['journal']에 누적,
#   장전엔 '오늘 판', 마감엔 '오늘 복기'를 텔레그램으로 한 방에 정리. 어려운 용어 X.
# ══════════════════════════════════════════════════════════════════════════
def journal_reset_if_needed(state, today):
    j = state.get("journal") or {}
    if j.get("_day") != today:
        j = {"_day": today, "snipers": [], "entries": [], "precursors": [],
             "aces": [], "supply_turns": [], "sev_hi": 0, "sev_lo": 2, "macro_last": ""}
    state["journal"] = j
    return j


def journal_accumulate(state, snap):
    """매 루프 결과(snap)를 오늘 일지에 누적 — 중복 종목/시그널은 1회만."""
    j = state.get("journal") or {}
    def _addname(key, name):
        if name and name not in j.get(key, []):
            j.setdefault(key, []).append(name)
    for s in (snap.get("snipers") or []):
        _addname("snipers", s.get("name"))
    for e in (snap.get("entries") or []):
        _addname("entries", e.get("name"))
    for t in (snap.get("supply_turns") or []):
        _addname("supply_turns", t.get("name"))
    if snap.get("precursor"):
        _pc = f"{snap['precursor'].get('from')}→{snap['precursor'].get('to')}"
        _addname("precursors", _pc)
    for a in (snap.get("ace") or []):
        _addname("aces", a.get("name"))
    _sev = (snap.get("macro") or {}).get("sev")
    if isinstance(_sev, int):
        j["sev_hi"] = max(j.get("sev_hi", 0), _sev)   # 오늘 최악 국면
        j["sev_lo"] = min(j.get("sev_lo", 2), _sev)   # 오늘 최선 국면
    j["macro_last"] = (snap.get("macro") or {}).get("text", "")
    state["journal"] = j


def _lineup_perf(token, key, secret, lineup):
    """라인업 종목 등락률·거래대금 스냅 — 브리핑/복기 성적표용. [{name,chg,turn_eok}]."""
    rows = []
    if not token:
        return rows
    for code, name in lineup:
        px, chg, turn = _price_and_turnover(token, key, secret, code)
        if px:
            rows.append({"name": name, "chg": chg or 0.0, "turn_eok": round((turn or 0) / 1e8, 0)})
    return rows


_SEV_ICON = {0: "🟢 양호", 1: "🟡 중립", 2: "🔴 리스크오프"}


def send_interval_brief(now_kst, state, token_tg, chat_id, snap):
    """[V13.2 스팸 방지] 정규장(09:00~15:20) 30분 주기 '요약 브리핑' 1건.
    개별 격발(시가저격·15분봉 등)은 그대로 두고, 상태 변화가 없어도 30분마다 한 줄 현황만 발송.
    → 분 단위 체감 폭주를 억제하고 '지금 시장이 어떤 국면인지'를 정기적으로 상기."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (15 * 60 + 20)):
        return
    today = now_kst.strftime("%Y%m%d")
    _last = state.get("interval_brief_ts", 0)
    if state.get("interval_brief_day") != today:
        _last = 0
        state["interval_brief_sig"] = None            # 날짜 바뀌면 시그니처도 초기화
    if (int(now_kst.timestamp()) - int(_last)) < 1800:      # 30분 쿨다운(발송 최소 간격)
        return
    macro = snap.get("macro") or {}
    sev = macro.get("sev")
    _mtxt = macro.get("text") or _SEV_ICON.get(sev, "—")
    _sup = snap.get("supply") or {}
    _inf = _sup.get("inflow"); _outf = _sup.get("outflow")
    _flow = (f"{_outf['sector']}➡️{_inf['sector']}(+{_inf['net_eok']:,.0f}억)"
             if (_inf and _outf) else "특이 자금이동 없음")
    _ace_n = len(snap.get("ace") or [])
    _entry_n = len(snap.get("entries") or [])
    _top = snap.get("top_pick") or {}
    _topn = f"{_top.get('name')}(+{_top.get('amt_eok'):,.0f}억)" if _top else "—"
    # [V16.9 다이어트] 상태가 바뀔 때만 발송 — sev·자금흐름·A급수·진입수·원톱이 직전과 같으면 침묵.
    #   → 하루 11개 '리스크오프·0종' 복붙 스팸 제거. (첫 브리핑은 항상 1회 발송)
    _sig = f"{sev}|{_flow}|{_ace_n}|{_entry_n}|{_topn}|{1 if macro.get('overheat') else 0}"
    if state.get("interval_brief_sig") == _sig:
        state["interval_brief_ts"] = int(now_kst.timestamp())   # 침묵해도 타이머는 갱신(다음 판정 30분 뒤)
        return
    _msg = (f"{SIG_INFO}\n📋 요약 브리핑(변동) · {now_kst.strftime('%H:%M')} KST\n"
            f"매크로: {_mtxt}\n"
            f"수급흐름: {_flow}\n"
            f"A급 {_ace_n}종 · 진입후보 {_entry_n}종 · 원톱 {_topn}")
    if macro.get("overheat"):
        _msg += "\n🔥 주도주 과열 — 눌림목 대기(추격 금지)"
    if send_telegram(token_tg, chat_id, _msg):
        state["interval_brief_ts"] = int(now_kst.timestamp())
        state["interval_brief_day"] = today
        state["interval_brief_sig"] = _sig


def send_morning_brief(now_kst, state, token_tg, chat_id, kis_key, kis_secret, kis_on):
    """08:40~08:55 장전 브리핑 1회 — 오늘 국면·현금비중·라인업 어제 마감 상태."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((8 * 60 + 40) <= m <= (8 * 60 + 55)):
        return
    today = now_kst.strftime("%Y%m%d")
    if state.get("brief_day") == today:
        return
    sev, mtext, mdetail = compute_macro()
    cg = _cash_guide(sev)
    lines = [f"📅 오늘의 판 — {now_kst.strftime('%m/%d(%a)')} 장전 브리핑",
             f"",
             f"① 국면: {mtext}",
             f"   {mdetail}",
             f"② 현금비중: {cg['level']} · {cg['ratio']} — {cg['msg']}"]
    _ovn = _overnight_nq_line(state, today)
    if _ovn:
        lines.append(_ovn)   # 국장 야간 마감(20:00)→아침(07:00) 나스닥선물 흐름
    _kn = state.get("kfut_night") or {}   # 간밤 코스피200 선물 야간 세션 마지막 등락(18:00~05:00)
    _yday = (now_kst - datetime.timedelta(days=1)).strftime("%Y%m%d")
    if _kn.get("chg") is not None and _kn.get("day") in (today, _yday):
        _ki = "🟢" if _kn["chg"] >= 0 else "🔴"
        lines.append(f"🇰🇷 간밤 코스피200 선물(야간): {_kn['chg']:+.2f}% {_ki}  ({_kn.get('hm','')} 기준)")
    if kis_on:
        tok = kis_token(kis_key, kis_secret)
        perf = _lineup_perf(tok, kis_key, kis_secret, load_lineup())
        if perf:
            lines.append("③ 오늘 감시 라인업 (개장 후 실시간 갱신):")
            for p in perf:
                if (p.get("turn_eok") or 0) == 0 and abs(p.get("chg") or 0) < 0.01:
                    lines.append(f"   · {p['name']} — ⏳ 개장 대기(09시 후 거래 시작)")   # 장전=거래 전이라 0
                else:
                    lines.append(f"   · {p['name']} {p['chg']:+.2f}% · 거래대금 {p['turn_eok']:,.0f}억")
    lines += ["", "🧭 초보 가이드: 09~10시 '시가저격/진입' 알림 뜨면 그때 움직여요.",
              "   신호 전엔 관망! 급등 추격 금지 · 진입하면 −3% 칼손절부터."]
    if send_telegram(token_tg, chat_id, "\n".join(lines)):
        state["brief_day"] = today


def send_daily_review(now_kst, state, token_tg, chat_id, kis_key, kis_secret, kis_on):
    """15:35~15:50 마감 복기 리포트 1회 — 오늘 뜬 신호 총정리 + 라인업 성적 + 코칭."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((15 * 60 + 35) <= m <= (15 * 60 + 50)):
        return
    today = now_kst.strftime("%Y%m%d")
    if state.get("review_day") == today:
        return
    j = state.get("journal") or {}
    _SEVN = {0: "🟢 양호", 1: "🟡 중립/경고", 2: "🔴 리스크오프"}
    lines = [f"📓 오늘의 복기 — {now_kst.strftime('%m/%d(%a)')} 마감 리포트",
             f"",
             f"■ 매크로 국면: 최선 {_SEVN.get(j.get('sev_lo',1))} ~ 최악 {_SEVN.get(j.get('sev_hi',1))}",
             f"   마감: {j.get('macro_last','—')}"]
    _snp = j.get("snipers") or []; _ent = j.get("entries") or []
    _prc = j.get("precursors") or []; _ace = j.get("aces") or []
    lines.append(f"■ 오늘 뜬 신호:")
    lines.append(f"   🎯 시가저격: {', '.join(_snp) if _snp else '없음'}")
    lines.append(f"   🟢 진입: {', '.join(_ent) if _ent else '없음'}")
    lines.append(f"   🚀 전조: {', '.join(_prc) if _prc else '없음'}")
    lines.append(f"   🔄 수급전환: {', '.join(j.get('supply_turns') or []) if (j.get('supply_turns')) else '없음'}")
    lines.append(f"   🥇 A급: {', '.join(_ace) if _ace else '없음'}")
    if kis_on:
        tok = kis_token(kis_key, kis_secret)
        perf = _lineup_perf(tok, kis_key, kis_secret, load_lineup())
        if perf:
            _best = max(perf, key=lambda p: p["chg"]); _worst = min(perf, key=lambda p: p["chg"])
            lines.append(f"■ 라인업 성적: 최고 {_best['name']} {_best['chg']:+.2f}% / "
                         f"최저 {_worst['name']} {_worst['chg']:+.2f}%")
    # [V16.9 다이어트] 한 줄 결론 — "오늘 뭘 했어야 했나"를 국면·신호 기준으로 명확히.
    _sev_hi = j.get("sev_hi", 1)
    _real = bool(_snp or _ent or _ace)     # 실제 액션 가능 신호가 하나라도 떴나
    if _sev_hi >= 2:
        _verdict = "🔴 오늘 결론: 리스크오프 — 무포지션이 정답. 매수(강) 신호 떠도 규제 우선(관망)."
    elif _real:
        _verdict = "🟢 오늘 결론: 진입 신호 있던 날 — 신호 종목만, 눌림 진입·−2% 손절 지켰으면 성공."
    else:
        _verdict = "🟡 오늘 결론: 뚜렷한 진입 신호 없던 날 — 안 산 게 정답. 감으로 산 게 있다면 반성."
    lines += ["", _verdict,
              "🧭 복기 체크(초보): ①신호 종목 실제로 올랐나? ②감으로 산 것 없나? ③손절 지켰나?",
              "   신호+원칙만 반복하면 실력 늡니다. 오늘도 수고했어요 👏"]
    if send_telegram(token_tg, chat_id, "\n".join(lines)):
        state["review_day"] = today


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=180, help="체크 주기(초), 기본 180=3분")
    ap.add_argument("--notify-worse", action="store_true", help="[구] 악화 알림 플래그(이제 기본 ON)")
    ap.add_argument("--no-worse", action="store_true", help="매크로 악화 알림 끄기(개선만)")
    args = ap.parse_args()
    token_tg = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token_tg or not chat_id:
        print("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 설정 필요"); sys.exit(1)
    kis_key, kis_secret = read_kis_keys()
    kis_on = bool(kis_key and kis_secret)
    if not kis_on:
        if not os.path.exists(SECRETS_FILE):
            print(f"⚠️ secrets.toml 없음: {SECRETS_FILE}")
        else:
            print(f"⚠️ secrets.toml 있으나 KIS 키(KIS_APP_KEY/KIS_APP_SECRET 등) 못 찾음")
    dart_key = read_dart_key()                    # [V18.4] DART 공시 감시 키(없으면 자동 OFF)
    print(f"📡 감시 시작 — {args.interval}초 · 매크로 ON · 수급 {'ON' if kis_on else 'OFF'} · "
          f"DART공시 {'ON' if dart_key else 'OFF(키없음)'}")
    send_telegram(token_tg, chat_id,
                  f"📡 감시 시작 — 국면 개선·전조·A급 알림 대기중\n수급 감시 {'ON' if kis_on else 'OFF(KIS키 없음)'}")

    while True:
        try:
            st = load_state()
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
            stamp = now.strftime("%m/%d %H:%M")
            journal_reset_if_needed(st, now.strftime("%Y%m%d"))   # 날짜 바뀌면 일지 초기화

            # [V15.8] 주말(토/일) 국장·NXT·미선물 휴장 — 알림 전면 스킵(스팸 방지). 월0~금4, 토5·일6.
            if now.weekday() >= 5:
                print(f"[{stamp}] 주말 휴장 — 알림 대기")
                time.sleep(max(60, args.interval))
                continue

            # 1) 매크로
            sev, mtext, mdetail = compute_macro()
            prev_sev = st.get("sev")
            # [스팸 차단] 매크로 알림은 (a)장 관련 시간(08:00~20:00)에만 (b)60분 쿨다운.
            #   나스닥선물이 차단기준(-0.2%) 근처서 출렁이면 sev가 🔴↔🟡 오락가락 → 야간 알림 폭주 방지.
            _mm = now.hour * 60 + now.minute
            _in_hours = (8 * 60) <= _mm <= (20 * 60)
            _last_ts = st.get("macro_alert_ts", 0)
            _cooldown_ok = (int(now.timestamp()) - int(_last_ts)) >= 3600   # 60분
            # 개선·악화 모두 알림(악화 기본 ON — 리스크 경보). --no-worse면 개선만.
            _worse_on = not args.no_worse
            if prev_sev is not None and sev != prev_sev and _in_hours and _cooldown_ok:
                if sev < prev_sev or _worse_on:
                    icon = "📈 매크로 개선!" if sev < prev_sev else "📉 매크로 악화 — 리스크↑"
                    _mbadge = SIG_WATCH if sev < prev_sev else SIG_CAUTION
                    if send_telegram(token_tg, chat_id, f"{_mbadge}\n{icon}\n{mtext}\n{mdetail}\n{stamp} KST"):
                        st["macro_alert_ts"] = int(now.timestamp())
            st["sev"] = sev
            # 🌙 나스닥100 선물 야간 변동 추적(20:00 기준 → 07:00 아침) — 브리핑에서 송출
            try:
                track_overnight_futures(now, st)
            except Exception as _one:
                print("야간선물 추적 오류:", _one)
            # 나스닥선물 0선 돌파 알림(음↔양) — 방향별 120분 쿨다운(주간 08:00~20:00)
            try:
                check_nq_cross(now, st, token_tg, chat_id)
            except Exception as _nqe:
                print("나스닥 크로스 오류:", _nqe)
            # [V14.5] 나스닥선물 저점 대비 반등(음수권 반등) — 반도체 대장주 매수타이밍
            try:
                check_nq_rebound(now, st, token_tg, chat_id)
            except Exception as _nqre:
                print("나스닥 반등 오류:", _nqre)
            # [V14.6] 지수(코스피/코스닥) 오후 저점 대비 반등 — 종가베팅 타이밍
            try:
                check_index_rebound(now, st, token_tg, chat_id)
            except Exception as _ixre:
                print("지수 반등 오류:", _ixre)
            # 🌙 야간 미장 흐름 알림(20:00~08:00) — SOX ±2% 강/약 이벤트(방향별 120분 쿨다운)
            try:
                check_us_overnight(now, st, token_tg, chat_id)
            except Exception as _uoe:
                print("야간 미장 알림 오류:", _uoe)
            # 📢 [V18.4] DART 실시간 공시 감시(07:00~17:00) — 호재 공시를 우리 엔진으로 교차검증해 진입후보 선정
            try:
                check_dart_disclosures(now, st, token_tg, chat_id, dart_key, kis_key, kis_secret)
            except Exception as _dqe:
                print("DART 공시 감시 오류:", _dqe)
            print(f"[{stamp}] 매크로 sev={sev} {mtext} | {mdetail}")

            # [1단계] 웹 속보판용 스냅샷 — 매크로는 항상, 수급/A급은 KIS ON일 때 채운다.
            snap = {
                "updated": stamp,
                "updated_ts": int(now.timestamp()),
                "kis_on": kis_on,
                "macro": {"sev": sev, "text": mtext, "detail": mdetail},
                "cash_guide": _cash_guide(sev),       # 현금비중·대응 권장(sev 기반)
                "indicators": compute_indicators(),   # 환율·VIX·지수 세부
                "supply": None,
                "precursor": None,                    # 전조 시그널(자금 이동)
                "ace": [],
                "top_pick": None,                     # 원톱 픽(수급 최상위 종목)
                "pick_mode": _pick_mode(now),         # 시간대: 만쥬(오전)/돌팬티(오후)/참고
                "snipers": [],                        # 09:10 시가저격 돌파 종목
                "entries": [],                        # 진입 3-조건 충족 종목(상시)
                "supply_turns": [],                   # 수급 전환(음→양) 종목(텔레그램 격발분)
                "supply_watch": [],                   # 라인업 수급 추세(관측용): pos/up/down
                "lineup": [],                         # 오늘의 감시 라인업(대시보드 동기화)
                "turnaround": [],                     # 14:30 V자 턴어라운드 정렬 종목
                "bar15": [],                          # 15분봉 강한 양봉 확정 종목(김팀장式)
                "market_supply": None,                # 시장 전체(코스피) 기관·외인 순매수
                "sector_leaders": [],                 # 2-Tier 6대 섹터 대장주 수급 관측
                "kospi_fut": None,                    # 코스피200 선물 실측(주간/야간 세션·KIS)
                "nxt_after": [],                      # 넥장(넥스트레이드 야간 16~20시) 급등 타점
            }
            # 시장 전체(코스피) 기관·외인 전환 알림 — 네이버 소스(KIS 무관, 정규장만)
            try:
                snap["market_supply"] = check_market_supply_turn(now, st, token_tg, chat_id, sev)
            except Exception as _mse:
                print("시장수급 체크 오류:", _mse)
            # 시장 전체 양전 여부(외인+기관 합 ≥ 0) → 2-Tier 대장주 정렬 판정에 사용
            _mkt = snap.get("market_supply") or {}
            _mkt_pos = ((_mkt.get("org_eok", 0) + _mkt.get("frn_eok", 0)) >= 0) if _mkt else False

            # 2)/3) 수급 전조·A급 (KIS 있을 때 + 매크로가 리스크오프 아닐 때만 유의미)
            if kis_on:
                tok = kis_token(kis_key, kis_secret)
                if tok:
                    # 🇰🇷 코스피200 선물 실측(주간/야간) — KIS 국내선물옵션. 세션 태그 붙여 스냅샷/지표에 반영.
                    try:
                        _kf = kospi200_futures(tok, kis_key, kis_secret)
                        _sess = _kospi_fut_session(now)
                        if _kf and _sess != "휴장":
                            snap["kospi_fut"] = {"chg": _kf["chg"], "px": _kf["px"], "session": _sess}
                            _kt = "up" if _kf["chg"] > 0 else "down" if _kf["chg"] < 0 else "flat"
                            snap["indicators"].append(
                                {"label": f"코스피200선물({_sess})", "value": f"{_kf['chg']:+.2f}",
                                 "unit": "%", "tone": _kt})
                            # 야간 세션 마지막 등락을 저장 → 장전 브리핑에서 '간밤 코스피선물' 표기
                            if _sess == "야간":
                                st["kfut_night"] = {"chg": _kf["chg"], "day": now.strftime("%Y%m%d"),
                                                    "hm": now.strftime("%H:%M")}
                    except Exception as _kfe:
                        print("코스피선물 체크 오류:", _kfe)
                    secs = sector_moneyflow(tok, kis_key, kis_secret)
                    rows = sorted(secs.items(), key=lambda kv: kv[1]["net"], reverse=True)
                    inflow = rows[0] if rows else None
                    outflow = rows[-1] if rows else None
                    # [V13.8] 장중 판정 — KIS 추정 순매수는 정규장(09:00~15:30)만 실시간.
                    #   장외(새벽 등)엔 전일 마감값이 얼어붙어 재전송되므로: 텔레그램은 장중에만, 라벨로 기준 명시.
                    _pm = now.hour * 60 + now.minute
                    _mkt_hours = (9 * 60) <= _pm <= (15 * 60 + 30)
                    _basis = "장중 실시간" if _mkt_hours else "전일 마감 기준"
                    # 전조 시그널
                    if (inflow and outflow and inflow[0] != outflow[0]
                            and inflow[1]["net"] > 0 and outflow[1]["net"] < 0 and sev != 2):
                        snap["precursor"] = {"from": outflow[0], "to": inflow[0],
                                             "inflow_eok": round(inflow[1]["net"] / 1e8, 1),
                                             "basis": _basis, "live": _mkt_hours}
                        # [V16.3] 도배 차단 — '섹터별' 60분 쿨다운(바이오↔방산 번갈아 떠도 각각 1회/시간).
                        #   기존 단일 tour_key는 유입처가 매분 flip-flop하면 매번 재발송되던 버그.
                        _today3 = now.strftime("%Y%m%d")
                        key = inflow[0]
                        _tour = st.get("tour_sent") or {}
                        if _tour.get("_day") != _today3:
                            _tour = {"_day": _today3}
                        _last = int(_tour.get(key, 0))
                        if _mkt_hours and (int(now.timestamp()) - _last) >= 3600:   # 섹터별 60분
                            send_telegram(token_tg, chat_id,
                                          f"{SIG_BUY}\n🚀 전조 시그널!\n자금 {outflow[0]} 이탈 → {inflow[0]} 유입\n"
                                          f"유입 {inflow[1]['net']/1e8:,.0f}억 · {stamp} KST\n폭등 前 선취 후보 — 대시보드 확인")
                            _tour[key] = int(now.timestamp())
                        st["tour_sent"] = _tour
                    # [V13.9] A급 — 연기금 파일 있으면 (유입 종목 ∩ 연기금), 없으면 '순매수 500억↑' 실용 대체.
                    #   pension_track_log.json 미존재 시 A급이 영구 0이던 구조적 결함 해소.
                    pens = pension_codes()
                    _ace_reason = "자금유입 × 연기금 겹침" if pens else "자금유입 상위 · 순매수 500억↑(대금 폭발)"
                    ace_now = []
                    for sname, info in secs.items():
                        if info["net"] <= 0:
                            continue
                        for s in info["stocks"]:
                            if not (s["amt"] and s["amt"] > 0):
                                continue
                            _is_ace = (s["code"] in pens) if pens else (s["amt"] >= ACE_AMT_MIN)
                            if _is_ace:
                                ace_now.append((s["code"], s["name"], sname, s["amt"]))
                    # [V16.3] A급 종목별 '당일 1회' 발송 — 순간 리스트 이탈→재진입 시 재발송되던 버그 수정.
                    #   기존 prev_ace(직전 스냅) 방식은 종목이 잠깐 빠지면 '신규'로 오인 → 재알림. 당일 sent set으로 고정.
                    _today3 = now.strftime("%Y%m%d")
                    _ace_sent = st.get("ace_sent") or {}
                    if _ace_sent.get("_day") != _today3:
                        _ace_sent = {"_day": _today3}
                    new_ace = [a for a in ace_now if a[0] not in _ace_sent]
                    if new_ace and sev != 2 and _mkt_hours:
                        lines = "\n".join(f"• {n} ({sn}) +{amt/1e8:,.0f}억" for _c, n, sn, amt in new_ace)
                        send_telegram(token_tg, chat_id,
                                      f"{SIG_BUY_STRONG}\n🥇 A급 종목 신규 포착!\n{lines}\n{stamp} KST\n({_ace_reason})")
                        for _c, _n2, _sn, _amt in new_ace:
                            _ace_sent[_c] = True
                    if _mkt_hours:
                        st["ace_sent"] = _ace_sent
                        st["ace"] = [a[0] for a in ace_now]     # 스냅샷용 현재 A급 목록
                    print(f"           수급: 유입 {inflow[0] if inflow else '-'} / 이탈 {outflow[0] if outflow else '-'} · A급 {len(ace_now)}")

                    # 스냅샷 수급/A급 채우기 (억원 단위)
                    snap["supply"] = {
                        "ranking": [{"sector": s, "net_eok": round(info["net"] / 1e8, 1)}
                                    for s, info in rows],
                        "inflow": ({"sector": inflow[0], "net_eok": round(inflow[1]["net"] / 1e8, 1)}
                                   if inflow else None),
                        "outflow": ({"sector": outflow[0], "net_eok": round(outflow[1]["net"] / 1e8, 1)}
                                    if outflow else None),
                    }
                    snap["ace"] = [{"code": _c, "name": n, "sector": sn, "amt_eok": round(amt / 1e8, 1)}
                                   for _c, n, sn, amt in ace_now]

                    # 원톱 픽 — 유입 섹터 종목 중 순매수 금액 최대(단 하나). 리스크오프면 표시 안 함.
                    _cands = []
                    for _sn, _info in secs.items():
                        if _info["net"] <= 0:
                            continue
                        for _s in _info["stocks"]:
                            if _s["amt"] and _s["amt"] > 0:
                                _cands.append((_s["amt"], _s["name"], _s["code"], _sn))
                    if _cands and sev != 2:
                        _cands.sort(reverse=True)
                        _amt, _nm, _cd, _sc = _cands[0]
                        snap["top_pick"] = {"name": _nm, "code": _cd, "sector": _sc,
                                            "amt_eok": round(_amt / 1e8, 1),
                                            "pension": _cd in pens}

                    # [완전 자동] auto 모드면 매일 1회(장중) 자금유입 상위 6종을 라인업으로 자동 편입
                    _today_str = now.strftime("%Y%m%d")
                    if watchlist_is_auto() and st.get("auto_day") != _today_str and (9 * 60) <= (now.hour * 60 + now.minute) <= (15 * 60):
                        _auto = auto_lineup_from_secs(secs, 6)
                        if len(_auto) >= 3 and save_auto_lineup(_auto):
                            st["auto_day"] = _today_str
                            print(f"           🔄 라인업 자동 편입: {', '.join(n for _c, n in _auto)}")
                            send_telegram(token_tg, chat_id,
                                          "🔄 오늘의 감시 라인업 자동 편입\n"
                                          + " · ".join(f"{n}" for _c, n in _auto)
                                          + f"\n(자금유입 상위 6종 · {now.strftime('%m/%d %H:%M')} KST)")
                    # 라인업 핫리로드(manju_watchlist.json) — 파일만 고치면 재시작 없이 반영
                    _lineup = load_lineup()
                    snap["lineup"] = [[c, n] for c, n in _lineup]   # 대시보드 동기화용(GitHub 스냅샷에 실림)
                    # 09:10 시가저격 — 라인업 거래대금 임계 돌파 시 종목별 1회 텔레그램
                    try:
                        snap["snipers"] = check_snipers(tok, kis_key, kis_secret, now, st,
                                                        token_tg, chat_id, _lineup, sev)
                    except Exception as _se:
                        print("시가저격 체크 오류:", _se)
                    # 진입 3-조건 상시 알림 — 제로아워(09~10) 거래대금+수급 모두(+) 충족 시 텔레그램
                    try:
                        snap["entries"] = check_entries(tok, kis_key, kis_secret, now, st,
                                                        token_tg, chat_id, _lineup, sev)
                    except Exception as _ee:
                        print("진입 체크 오류:", _ee)
                    # [V18.7] 조기 포착·급증진입 — 대시보드 없이 거래대금 랭킹 상시 스캔(초입 신호)
                    try:
                        check_early_catch(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _ece:
                        print("조기 포착 오류:", _ece)
                    # [V17.3] 프로그램 누적 시간대 적립 — 대시보드가 오전/오후 추세로 종배 판독
                    try:
                        log_program_history(now, tok, kis_key, kis_secret, _lineup)
                    except Exception as _phe:
                        print("프로그램 이력 적립 오류:", _phe)
                    # 수급 전환(격발용 텔레그램) + 추세 관측(대시보드용) 이원화
                    try:
                        snap["supply_turns"], snap["supply_watch"] = check_supply_turn(
                            tok, kis_key, kis_secret, now, st, token_tg, chat_id, _lineup, sev)
                    except Exception as _ste:
                        print("수급전환 체크 오류:", _ste)
                    # 14:30 V자 턴어라운드 — 정렬 대기 + 15:20 동시호가 타격(폰 자동)
                    try:
                        snap["turnaround"] = check_turnaround(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _tae:
                        print("턴어라운드 체크 오류:", _tae)
                    # 김팀장式 15분봉 강한 양봉 확정 → 다음 봉 진입 신호
                    try:
                        snap["bar15"] = check_bar15(tok, kis_key, kis_secret, now, st, token_tg, chat_id, _lineup, sev)
                    except Exception as _b15e:
                        print("15분봉 체크 오류:", _b15e)
                    # 넥장(넥스트레이드 야간 16~20시) 급등 타점 — NX 시장코드·가격/거래대금 기반
                    try:
                        snap["nxt_after"] = check_nxt_after(tok, kis_key, kis_secret, now, st, token_tg, chat_id, _lineup, sev)
                    except Exception as _nxe:
                        print("넥장 체크 오류:", _nxe)
                    # 2-Tier 대장주 관측 + 시장·섹터 정렬 시에만 텔레그램(Confluence)
                    try:
                        snap["sector_leaders"] = check_sector_leaders(
                            tok, kis_key, kis_secret, now, st, token_tg, chat_id, _mkt_pos, sev)
                    except Exception as _sle:
                        print("섹터대장 체크 오류:", _sle)

            # 🔥 [V13.2] 과열 경고 — 매크로 🟢(진입 허용)이나 타겟이 이미 올라 A급/진입후보 0개면
            #   "진입 허용"을 "과열·눌림목 대기"로 바꿔 뇌동 추격매수 통제. (제로아워~장중, 대시보드 문구만 오버라이드)
            _mm2 = now.hour * 60 + now.minute
            if sev == 0 and (9 * 60) <= _mm2 <= (15 * 60 + 20):
                _no_ace = not snap.get("ace") and not snap.get("entries")
                if _no_ace:
                    _kf_chg = ((snap.get("kospi_fut") or {}).get("chg") or 0.0)
                    _mkt_up = _mkt_pos or _kf_chg > 0     # 시장은 오르는데 후보 0개 = 진짜 과열
                    if _mkt_up:
                        snap["macro"]["text"] = "🔥 매크로 양호하나 주도주 과열 — 눌림목(조정) 대기"
                        snap["macro"]["overheat"] = True
                    else:
                        snap["macro"]["text"] = "🟡 매크로 양호하나 진입 후보 없음 — 관망"
                        snap["macro"]["overheat"] = False

            # 📋 [V13.2] 30분 주기 요약 브리핑(정규장) — 개별 격발과 별개로 정기 현황만
            try:
                send_interval_brief(now, st, token_tg, chat_id, snap)
            except Exception as _ibe:
                print("요약 브리핑 오류:", _ibe)

            # 📍 [V13.2] 오늘 매수 알림 로그(시각·가격) — 대시보드 '알림 성적'용
            snap["signal_log"] = (st.get("signal_log") or {}).get("items", [])

            # 🗒️ [V13.2] 오늘 온 모든 알람 메시지 타임라인 — send_telegram 버퍼를 당일 누적
            _today_af = now.strftime("%Y%m%d")
            _af = st.get("alert_feed", {})
            if _af.get("_day") != _today_af:
                _af = {"_day": _today_af, "items": []}
            for _m in list(_ALERT_FEED):
                if _m.get("day") == _today_af:
                    _af.setdefault("items", []).append({"t": _m["t"], "text": _m["text"]})
            _ALERT_FEED.clear()
            _af["items"] = _af.get("items", [])[-200:]     # 최근 200건만 유지
            st["alert_feed"] = _af
            snap["alert_feed"] = _af["items"]

            # 📓 매매일지 누적 + 장전 브리핑(08:50)·마감 복기(15:35) 발송
            journal_accumulate(st, snap)
            try:
                send_morning_brief(now, st, token_tg, chat_id, kis_key, kis_secret, kis_on)
                send_daily_review(now, st, token_tg, chat_id, kis_key, kis_secret, kis_on)
            except Exception as _je:
                print("일지/복기 발송 오류:", _je)

            _snap_str = json.dumps(snap, ensure_ascii=False)
            save_snapshot(snap)
            push_snapshot_github(_snap_str)   # GitHub 'data' 브랜치 업로드(토큰 있을 때만)
            save_state(st)
        except Exception as e:
            print("체크 오류:", e)
        # 주요 시간대(장전 08:40~·제로아워 09~10·마감 15:30~)엔 60초로 촘촘히, 그 외엔 지정 간격
        _kn = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        _km = _kn.hour * 60 + _kn.minute
        # 15분봉 경계·시가저격·턴어라운드 정확도 위해 정규장(09:00~15:22)은 60초, 마감복기 60초
        _tight = (((8 * 60 + 40) <= _km <= (15 * 60 + 22))
                  or ((15 * 60 + 30) <= _km <= (15 * 60 + 50))
                  or ((16 * 60) <= _km <= (19 * 60 + 50)))   # 넥장 급등 타점 정확도 위해 야간도 60초
        _iv = 60 if _tight else args.interval
        time.sleep(max(30, _iv))


if __name__ == "__main__":
    main()
