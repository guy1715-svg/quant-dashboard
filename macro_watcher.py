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

# [V25.22] stdout/stderr을 UTF-8로 강제 — GUI/일반 콘솔(cp949)에서 print(—·특수문자) 크래시 방지.
#   (tools.bat은 chcp 65001로 됐지만 GUI subprocess·기본 콘솔은 cp949라 UnicodeEncodeError 발생)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
        # [V21.8] POST 방식 + 4096자 초과 자동 분할 — 긴 브리핑(뉴스+차트검증)도 안전 전송.
        #   (기존 GET은 긴 한글 URL 초과로 전송 실패/누락 발생)
        _chunks = []
        _t = text or ""
        while _t:
            _chunks.append(_t[:3900]); _t = _t[3900:]
        _ok = True
        for _c in _chunks:
            _r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                               data={"chat_id": chat_id, "text": _c}, timeout=8)
            _seg_ok = bool(_r.status_code == 200 and (_r.json() or {}).get("ok"))
            if not _seg_ok:
                print(f"텔레그램 전송 실패: HTTP {_r.status_code} {_r.text[:200]}")
                _ok = False
                break
    except Exception as e:
        print("텔레그램 전송 실패:", e)
        _ok = False
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


def _hist_pct(sym):
    """일봉 종가 2개로 전일대비% — fast_info.previous_close가 튀는 지수(코스피 등)용 안정 산출."""
    try:
        h = yf.Ticker(sym).history(period="5d")["Close"].dropna()
        if len(h) >= 2:
            return (float(h.iloc[-1]) / float(h.iloc[-2]) - 1) * 100
    except Exception:
        pass
    return None


def _wti_pct():
    return _hist_pct("CL=F")


def _us_fut_pct(state, now_kst, ttl=600):
    """[V25.33] 나스닥100 선물 전일대비%(10분 캐시) — 눌림/낙주 '오버나이트 게이트'용.
    강의: 눌림 홀딩은 밤사이 미국장이 받쳐줄 때만. 한국 장중 NQ=F=오늘밤 미국장 방향 선반영. None 가능."""
    c = state.get("_usfut_cache") or {}
    ts = int(now_kst.timestamp())
    if c.get("ts") and (ts - c["ts"]) < ttl and "nq" in c:
        return c["nq"]
    nq = _pct("NQ=F")
    state["_usfut_cache"] = {"ts": ts, "nq": nq}
    return nq


def _overnight_note(nq):
    """미국선물%로 오버나이트(홀딩) 여건 한 줄. (태그문자열, 홀딩가능bool)."""
    if nq is None:
        return "", True                                   # 데이터 없음 → 판단보류(막지 않음)
    if nq <= -0.7:
        return f"\n🌙 미국선물 {nq:+.1f}% 약세 — 오버나이트 비권장, 당일 청산 우선", False
    if nq >= 0.2:
        return f"\n🌙 미국선물 {nq:+.1f}% — 홀딩 여건 OK(눌림 종배 가능)", True
    return f"\n🌙 미국선물 {nq:+.1f}% 보합 — 홀딩은 소량만", True


def compute_macro(kis_token=None, kis_key=None, kis_secret=None):
    nq, sox = _pct("NQ=F"), _pct("^SOX")
    peers = [x for x in (_pct("NVDA"), _pct("AVGO"), _pct("MU")) if x is not None]
    wti = _wti_pct()
    ups = sum(1 for v in peers if v > 0)
    semi_sync = (sox is not None and sox > 0) and (len(peers) > 0 and ups >= max(1, round(len(peers) * 0.6)))
    riskoff = (wti is not None and wti >= WTI_RISK)
    # [SOX 방어막] K-국장은 반도체 시총 비중 커 SOX 급락에 종속 동조 → −5% 폭락=차단 / −3% 조정=경고
    sox_crash = (sox is not None and sox <= -5.0)
    sox_warn  = (sox is not None and sox <= -3.0)
    # [SOX 예외] 나스닥이 '완만한 음전'(NQ_BLOCK~-0.8%)뿐인데 SOX가 강세(+1%↑)면 반도체 장세로 보고
    #   리스크오프→중립으로 완화(반도체 선별 서치 허용). WTI 리스크오프·SOX 폭락·나스닥 급락(-0.8%↓)은 차단 유지.
    sox_strong = (sox is not None and sox >= 1.0)
    nq_mild = (nq is not None and NQ_BLOCK >= nq > -0.8)
    sox_rescue = (nq_mild and sox_strong and semi_sync and not riskoff and not sox_crash)
    # [V24.9] fail-safe — 미국 3대 지표(나스닥·SOX·WTI)가 전부 None(데이터 outage)이면
    #   킬스위치가 '중립(sev1)'으로 열려버리는 fail-open 방지: 보수적으로 sev=2(신규매수 억제).
    if nq is None and sox is None and wti is None:
        _us = "미국지표 조회 실패(데이터 지연·보수적 차단)"
        ks_fs = _kospi_index_kis(kis_token, kis_key, kis_secret)
        if ks_fs is None:
            ks_fs = _hist_pct("^KS11")
            if ks_fs is not None and abs(ks_fs) > 4.0:
                ks_fs = None
        ewy_fs = _pct("EWY"); fxl_fs, fxc_fs = _level("USDKRW=X")
        _kr_fs = ("코스피 " + (f"{ks_fs:+.2f}%" if ks_fs is not None else "—")
                  + " · 야간(EWY) " + (f"{ewy_fs:+.2f}%" if ewy_fs is not None else "—")
                  + " · 환율 " + (f"{fxl_fs:,.0f}({fxc_fs:+.2f}%)" if (fxl_fs is not None and fxc_fs is not None) else "—"))
        return 2, "🔴 데이터 outage · 신규매수 보수적 차단(지표 조회 실패)", f"🇺🇸 미국(밤) {_us}\n🇰🇷 한국    {_kr_fs}", "outage"
    if (riskoff or (nq is not None and nq <= NQ_BLOCK) or sox_crash) and not sox_rescue:
        sev = 2
        text = "🔴 리스크오프 · 신규매수 차단" + (f" (반도체 폭락 SOX {sox:+.1f}%)" if sox_crash else "")
    elif sox_rescue:
        sev = 1
        text = f"🟠 나스닥 약보합({nq:+.1f}%)이나 SOX 강세({sox:+.1f}%) — 반도체 선별 진입"
    elif (nq is not None and nq >= NQ_GO) and semi_sync and not sox_warn:
        sev, text = 0, "🟢 진입 허용 (매크로 3대 양호)"
    elif sox_warn:
        sev, text = 1, f"🟠 경고 · 반도체 조정(SOX {sox:+.1f}%) — 한도 50%"
    else:
        sev, text = 1, "🟡 중립 · 선별 진입"
    _us = (f"나스닥 {nq:+.2f}% · SOX {sox:+.2f}% · WTI {wti:+.2f}%"
           if None not in (nq, sox, wti) else "미국지표 대기")
    # [V20.1] 코스피 주간(^KS11)·야간 프록시(EWY 美상장 한국ETF)·원달러 환율 추가.
    #   EWY=한국 밤(美장중) 거래 → 익일 갭 선행. 환율↑=외국인 이탈 압력.
    #   코스피는 fast_info.previous_close가 튀는 케이스(+5%대 오류) 있어 히스토리 기반으로 산출.
    # [V24.9] 코스피는 KIS 지수 우선(yfinance ^KS11 하루 지연 버그 회피), 실패 시 yfinance 폴백
    ks = _kospi_index_kis(kis_token, kis_key, kis_secret)
    if ks is None:
        ks = _hist_pct("^KS11")
        if ks is not None and abs(ks) > 4.0:          # 코스피 하루 ±4% 초과=데이터 이상 → 표기 제외
            ks = None
    ewy = _pct("EWY")
    fxl, fxc = _level("USDKRW=X")
    _kr = ("코스피 " + (f"{ks:+.2f}%" if ks is not None else "—")
           + " · 야간(EWY) " + (f"{ewy:+.2f}%" if ewy is not None else "—")
           + " · 환율 " + (f"{fxl:,.0f}({fxc:+.2f}%)" if (fxl is not None and fxc is not None) else "—"))
    # 미국(밤)/한국 두 그룹으로 줄 분리 — 한눈에 구분되게(heartbeat·텔레그램 공통)
    detail = f"🇺🇸 미국(밤) {_us}\n🇰🇷 한국    {_kr}"
    # [V25.1] data_state: "ok"(3대 지표 정상) / "partial"(일부 None — sev 튈 수 있어 직전 유지)
    #   full outage(전부 None)는 위에서 별도 처리(sev=2 fail-safe·"outage").
    data_state = "ok" if (nq is not None and sox is not None and wti is not None) else "partial"
    return sev, text, detail, data_state


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


_DAYTRADE_KINDS = ("시가저격", "진입", "조기포착", "급증진입", "돌파초입", "공시발굴", "거래량급증", "15분봉", "눌림타점", "레인지매매", "과매도낙주", "재료투매반등", "시간외단일가", "시가배팅")
_OVERNIGHT_KINDS = ("종배픽", "브리핑")


def _scorecard_report(token, key, secret, now_kst, token_tg, chat_id):
    """[V23.3] 추천 종목 성적표 — signal_scorecard+pick_history 읽어 현재가 대조.
    🌅 오늘 아침(당일단타) / 🌒 어제 저녁(종배·브리핑) 구분해 텔레그램 1건. 복붙 불필요."""
    today = now_kst.strftime("%Y-%m-%d")
    yday = (now_kst - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        with open(SCORECARD_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []

    def _res(code, base):
        try:
            _p, _c, _ = _price_and_turnover(token, key, secret, code)
            if _p and base:
                return _p, (_p / base - 1) * 100
        except Exception:
            pass
        return None, None

    def _fmt(r):
        _p, _pct = _res(r["code"], r.get("px"))
        if _p is None:
            return f"• {r.get('name', r['code'])} ({r.get('t', '')}) 추천 {r.get('px', 0):,} → 조회실패"
        _ic = "🔴" if _pct < 0 else "🟢" if _pct > 0 else "⚪"
        return f"{_ic} {r.get('name', r['code'])} ({r.get('t', '')}) 추천 {r.get('px', 0):,} → 현재 {_p:,} ({_pct:+.1f}%)"

    _morning = [r for r in rows if r.get("date") == today and r.get("kind") in _DAYTRADE_KINDS]
    _evening = [r for r in rows if r.get("date") == yday and r.get("kind") in _OVERNIGHT_KINDS]
    _today_on = [r for r in rows if r.get("date") == today and r.get("kind") in _OVERNIGHT_KINDS]
    if not rows:
        send_telegram(token_tg, chat_id,
                      "📋 추천 성적표 — 기록 없음\n"
                      "signal_scorecard.json이 비어있어. ①감시(옵션3)를 장중(09~15시) 켜둬야 신호가 쌓임 "
                      "②기록 파일은 PC마다 따로(집/회사 다름). 감시 며칠 돌린 PC에서 --report 하세요.")
        print("[성적표] 기록 파일 비어있음(0건)")
        return
    _lines = ["📋 추천 종목 성적표"]
    _lines.append(f"\n🌅 오늘 아침 당일단타 ({today})")
    if _morning:
        _mp = [_fmt(r) for r in _morning[:15]]
        _lines += _mp
        _pcts = [(_res(r["code"], r.get("px"))[1]) for r in _morning]
        _pcts = [x for x in _pcts if x is not None]
        if _pcts:
            _lines.append(f"   → 평균 {sum(_pcts)/len(_pcts):+.1f}% · 승률 {sum(1 for x in _pcts if x>0)/len(_pcts)*100:.0f}%")
    else:
        _lines.append("   (신호 없음)")
    _lines.append(f"\n🌒 어제 저녁 종배·브리핑 ({yday} → 오늘 결과)")
    if _evening:
        _lines += [_fmt(r) for r in _evening[:15]]
        _pcts = [(_res(r["code"], r.get("px"))[1]) for r in _evening]
        _pcts = [x for x in _pcts if x is not None]
        if _pcts:
            _lines.append(f"   → 평균 {sum(_pcts)/len(_pcts):+.1f}% · 승률 {sum(1 for x in _pcts if x>0)/len(_pcts)*100:.0f}%")
    else:
        _lines.append("   (기록 없음)")
    if _today_on:                                   # 오늘 수동/자동 등록된 종배·브리핑(결과는 내일)
        _lines.append(f"\n📌 오늘 등록 종배·브리핑 ({today} · 결과는 내일)")
        _lines += [f"• {r.get('name', r['code'])} [{r.get('kind')}] 등록가 {r.get('px', 0):,}" for r in _today_on[:15]]
    _lines.append("\n※ 현재가 기준 실시간 대조 — 추천가 대비 등락")
    send_telegram(token_tg, chat_id, "\n".join(_lines))
    print(f"[성적표] 아침 {len(_morning)}건 · 저녁 {len(_evening)}건 · 오늘등록 {len(_today_on)}건 발송")


def _daily_ohlc(token, key, secret, code):
    """종목 최근 일봉 {YYYYMMDD: {'o':시가,'h':고가,'c':종가}} — 청산 타이밍 분석용(1콜). 실패 시 {}."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        out = {}
        for x in (r.json().get("output", []) or []):
            if isinstance(x, dict):
                _d = x.get("stck_bsop_date")
                _o, _h, _c = _to_int(x.get("stck_oprc")), _to_int(x.get("stck_hgpr")), _to_int(x.get("stck_clpr"))
                if _d and _o and _c:
                    out[_d] = {"o": _o, "h": _h or _c, "c": _c}
        return out
    except Exception:
        return {}


def _daily_opens(token, key, secret, code):
    """종목 최근 일봉 시가 맵 {YYYYMMDD: 시가} — 종배(익일 시가 청산) 갭 측정용. 실패 시 {}."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        out = {}
        for x in (r.json().get("output", []) or []):
            if isinstance(x, dict):
                _d = x.get("stck_bsop_date"); _o = _to_int(x.get("stck_oprc"))
                if _d and _o:
                    out[_d] = _o
        return out
    except Exception:
        return {}


def _daily_closes(token, key, secret, code):
    """종목 최근 일봉 종가 맵 {YYYYMMDD: 종가} — inquire-daily-price. 실패 시 {}."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        out = {}
        for x in (r.json().get("output", []) or []):
            if isinstance(x, dict):
                _d = x.get("stck_bsop_date"); _c = _to_int(x.get("stck_clpr"))
                if _d and _c:
                    out[_d] = _c
        return out
    except Exception:
        return {}


def _analyze_history(token, key, secret, now_kst, token_tg, chat_id):
    """[V24.2] 과거 누적 신호 종합 분석 — signal_scorecard+pick_history 전체를 KIS 일봉으로
    익일 종가 대조, 신호 종류별 승률·평균수익 집계. 이미 쌓인 데이터로 '뭐가 먹히나' 판정."""
    try:
        with open(SCORECARD_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        if not isinstance(rows, list):
            rows = []
    except Exception:
        rows = []
    # pick_history(종배/그림자)도 합침 — signal→kind 매핑
    _kmap = {"dolpanty": "종배픽(NXT)", "dolpanty_nonxt": "종배픽(NXT미거래)",
             "dolpanty_div": "종배분산", "dolpanty_shadow": "종배그림자"}
    for p in _pick_read():
        rows.append({"date": p.get("date"), "code": p.get("code"), "name": p.get("name"),
                     "px": p.get("px"), "kind": _kmap.get(p.get("signal"), p.get("signal", "종배"))})
    if not rows:
        send_telegram(token_tg, chat_id, "📊 신호 분석 — 기록 없음(signal_scorecard/pick_history 비어있음). 감시 며칠 돌린 PC에서.")
        return
    from collections import defaultdict
    _by_kind = defaultdict(list)
    _cache = {}
    for r in rows[-200:]:                          # 최근 200건(일봉 30일 커버 범위)
        code, date, px, kind = str(r.get("code", "")).zfill(6), r.get("date", ""), r.get("px"), r.get("kind")
        if not (code.isdigit() and px and kind and date):
            continue
        _ymd = date.replace("-", "")
        if code not in _cache:
            _cache[code] = _daily_closes(token, key, secret, code)
        _cl = _cache[code]
        _later = sorted(d for d in _cl if d > _ymd)   # 신호 다음 거래일들
        if _later:
            _by_kind[kind].append((_cl[_later[0]] / px - 1) * 100)   # 익일 종가 대비 %
    if not any(_by_kind.values()):
        send_telegram(token_tg, chat_id, "📊 신호 분석 — 익일 결과 대조 가능한 기록이 아직 없음(최근 신호는 내일 이후 집계).")
        return
    _lines = ["📊 신호별 종합 성적 (과거 누적 · 익일 종가 대비)"]
    for kind, rets in sorted(_by_kind.items(), key=lambda x: -len(x[1])):
        if rets:
            _wr = sum(1 for x in rets if x > 0) / len(rets) * 100
            _avg = sum(rets) / len(rets)
            _ic = "🟢" if _avg > 0 else "🔴"
            _lines.append(f"{_ic} {kind}: {len(rets)}건 · 승률 {_wr:.0f}% · 평균 {_avg:+.1f}%")
    _lines.append("\n💡 승률↑·평균+ 신호는 살리고, 승률↓·평균− 신호는 실행 중단 판단 근거")
    send_telegram(token_tg, chat_id, "\n".join(_lines))
    print(f"[신호분석] {sum(len(v) for v in _by_kind.values())}건 집계 · {len(_by_kind)}종류")


def _analyze_exit_timing(token, key, secret, now_kst, token_tg, chat_id):
    """[V25.18] 종배 청산 타이밍 분석 — 쌓인 종배픽으로 '익일 시가 청산 vs 익일 종가 청산'을
    KIS 일봉(시가·종가)으로 소급 대조 + NXT거래/미거래 분리. '조기(시가)가 나은가 홀딩(종가)이 나은가' 답.
    ※ NXT 애프터 그날저녁가는 과거 미저장이라 소급 불가(시가/종가만)."""
    _picks = [p for p in _pick_read()
              if p.get("signal") in ("dolpanty", "dolpanty_nonxt", "dolpanty_div", "dolpanty_shadow")]
    if not _picks:
        send_telegram(token_tg, chat_id, "📊 종배 청산분석 — 종배 기록 없음(감시 며칠 돌린 PC에서).")
        return
    _ohlc, _nxt_cache = {}, {}
    _agg = {k: {"open": [], "high": [], "close": []} for k in ("전체", "NXT거래", "NXT미거래")}
    for p in _picks:
        cd = str(p.get("code", "")).zfill(6); px = p.get("px") or 0
        if not px:
            continue
        if cd not in _ohlc:
            _ohlc[cd] = _daily_ohlc(token, key, secret, cd)
        _pdate = str(p.get("date", "")).replace("-", "")
        _nx = next((d for d in sorted(_ohlc[cd]) if d > _pdate), None)
        if not _nx:
            continue
        _bar = _ohlc[cd][_nx]
        _gopen = (_bar["o"] / px - 1) * 100
        _ghigh = (_bar["h"] / px - 1) * 100
        _gclose = (_bar["c"] / px - 1) * 100
        if cd not in _nxt_cache:
            _nxt_cache[cd] = _nxt_tradable(token, key, secret, cd)
        _buckets = ["전체"] + (["NXT거래"] if _nxt_cache[cd] is True else ["NXT미거래"] if _nxt_cache[cd] is False else [])
        for _b in _buckets:
            _agg[_b]["open"].append(_gopen)
            _agg[_b]["high"].append(_ghigh)
            _agg[_b]["close"].append(_gclose)

    def _stat(xs):
        if not xs:
            return None
        _w = sum(1 for x in xs if x > 0) / len(xs) * 100
        return (len(xs), _w, sum(xs) / len(xs))
    _lines = ["📊 종배 청산 타이밍 분석 (쌓인 데이터)"]
    for _b in ("전체", "NXT거래", "NXT미거래"):
        _so, _sh, _sc = _stat(_agg[_b]["open"]), _stat(_agg[_b]["high"]), _stat(_agg[_b]["close"])
        if not _so:
            continue
        _lines.append(f"\n■ {_b} ({_so[0]}건)")
        _lines.append(f"  ⏱️ 9시 시초가: 승률 {_so[1]:.0f}% · 평균 {_so[2]:+.1f}%")
        if _sh:
            _lines.append(f"  🚀 익일 고가(팝 완벽청산): 승률 {_sh[1]:.0f}% · 평균 {_sh[2]:+.1f}%")
        if _sc:
            _lines.append(f"  🌆 종가(하루홀딩): 승률 {_sc[1]:.0f}% · 평균 {_sc[2]:+.1f}%")
        if _sh:
            _lines.append(f"  → 팝 여력(시초가→고가): {_sh[2] - _so[2]:+.1f}%p (아침 튐 노려 팔 여지)")
    _lines.append("\n※ 고가=당일 최고가 완벽 청산(상한선) · 네 방식(9~9:30 팝 매도)은 시초가~고가 사이 · 종가=하루홀딩")
    send_telegram(token_tg, chat_id, "\n".join(_lines))
    print("[청산분석] 발송:", " / ".join(_lines).replace("\n", " "))


def _deep_stock(token, key, secret, code, name="", gemini_key=None):
    """[V24.6] 특정종목 종합 해석 — 차트(이격·정배열·거래량)+수급(외인·기관)+큰추세(60분)+뉴스(AI)+타점.
    '이 종목 어때?' 한 방 분석. 반환: 텔레그램용 텍스트."""
    px, chg, turn = _price_and_turnover(token, key, secret, code)
    if not px:
        return f"❌ {code} — 시세 조회 실패(코드 확인)"
    ds = _daily_setup(token, key, secret, code, px) or {}
    _ma5, _ma20, _disp = ds.get("ma5"), ds.get("ma20"), ds.get("disp")
    _align = "🟢정배열(5>20MA↑)" if (_ma5 and _ma20 and px > _ma5 > _ma20) else "🔴비정배열"
    _vr = _vol_ratio_5d(token, key, secret, code)
    _vt = f"{_vr[2]:.1f}배" if _vr else "–"
    _f, _o = _investor_est(token, key, secret, code)
    _sup = f"외인 {_f*px/1e8:+.0f}억·기관 {_o*px/1e8:+.0f}억 " + ("✅유입" if (_f + _o) > 0 else "⚠️이탈")
    _bt = _big_trend_tag(token, key, secret, code, px).strip() or "60분 추세 –"
    _ng, _nbad = _news_grade(code)
    _ngt = "🔴악재" if _nbad else ("🔥재료S급" if _ng == "S" else "🟢재료A급" if _ng == "A" else "⚠️재료 미확인")
    _dispt = f"{_disp:+.0f}%" if _disp is not None else "–"
    _heat = "🔴심한과열" if (_disp or 0) >= 12 else "🟠과열" if (_disp or 0) >= 7 else "🟢정상" if (_disp or 0) >= -2 else "🔵낙폭과대"
    _pull = _pullback_levels(token, key, secret, code, px, chg, ds)
    _stop = int(px * 0.98); _t1 = int(px * 1.03)
    _lines = [f"🔎 [종목 해석] {name or code} ({code})",
              f"현재 {px:,}({(chg or 0):+.1f}%) · 거래대금 {(turn or 0)/1e8:,.0f}억 · 거래량 {_vt}(5일평균)",
              f"📊 20MA 이격 {_dispt} {_heat} · {_align}",
              f"💰 수급: {_sup}",
              f"📈 {_bt} · 재료: {_ngt}"]
    if _pull:
        _lines.append(_pull.strip())
    _lines.append(f"진입 {px:,} · 손절 {_stop:,}(−2%) · 익절 {_t1:,}(+3%)")
    if gemini_key:
        _ai = _gemini_stock_news_verdict(gemini_key, code, name or code)
        if _ai:
            _lines.append(_ai.strip())
    return "\n".join(_lines)


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


def _recent_high(token, key, secret, code, days=20, exclude_today=False):
    """최근 N일 고가 — inquire-daily-price. 실패 시 None.
    exclude_today=True면 오늘 봉(o[0]) 제외한 '직전 N일 전고' 반환(돌파 판정용 — 오늘 고가 포함 시
    px>=전고가 HOD에서만 참이 되는 버그 방지)."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        o = r.json().get("output", [])
        _rows = (o or [])[1:days + 1] if exclude_today else (o or [])[:days]
        highs = [_to_int(row.get("stck_hgpr")) for row in _rows if isinstance(row, dict)]
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


def _nxt_tradable(token, key, secret, code):
    """[V25.17] 넥스트레이드(NXT) 거래 가능 종목인지 — NX 시세 조회로 판별. True/False/None(미확인).
    NXT 거래 종목: 밤 재료가 NXT에 흡수돼 9시 갭 작음·대신 NXT서 오버나이트 청산 가능.
    NXT 미거래 종목: 9시 갭 엣지 살아있으나 밤새 탈출 불가(풀노출) → 소액·손절 철저.
    ※ 정규장~애프터 사이(15:30~16:00 등)엔 NX 시세가 비어 None(미확인) 나올 수 있음."""
    try:
        _p, _c, _t = _price_and_turnover(token, key, secret, code, mrkt="NX")
        return bool(_p and _p > 0)
    except Exception:
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


def _kospi_index_kis(token, key, secret):
    """[V24.9] 코스피 종합지수 전일대비% — KIS 국내업종 현재가(FHPUP02100000, U/0001).
    yfinance ^KS11은 일봉이 하루 밀려(stale) 목요일값을 금요일로 오산하는 버그가 있어 KIS로 대체.
    반환: float(%) 또는 None(키·데이터 없음)."""
    if not (token and key and secret):
        return None
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-index-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHPUP02100000"},
                         params={"fid_cond_mrkt_div_code": "U", "fid_input_iscd": "0001"}, timeout=6)
        o = r.json().get("output") or {}
        if isinstance(o, dict):
            _c = str(o.get("bstp_nmix_prdy_ctrt", "")).replace(",", "").strip()
            if _c not in ("", None):
                return float(_c)
    except Exception as _e:
        print(f"[KIS코스피 진단] {type(_e).__name__}: {_e}")
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


# ── [V21.4] 뉴스 시황 — 네이버 검색 API + Gemini 판정 ──────────────────────────
def read_naver_keys():
    """네이버 검색 API Client ID/Secret — 환경변수 우선. 없으면 (None,None)."""
    return (os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET"))


def _read_secret_alias(aliases):
    """secrets.toml에서 별칭 키 값 탐색(공용). 없으면 None."""
    _al = {a.lower() for a in aliases}
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
    return None


def read_gemini_key():
    """Gemini API 키 — 환경변수 → secrets.toml. 없으면 None(뉴스 브리핑은 헤드라인만)."""
    for _n in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY", "GOOGLE_GEMINI_API_KEY"):
        if os.environ.get(_n):
            return os.environ[_n].strip()
    return _read_secret_alias({"gemini_api_key", "google_api_key", "gemini_key", "gemini"})


def _gemini_factcheck(gkey, brief, mdetail=""):
    """[V24.8] Gemini 브리핑을 Gemini+구글검색(grounding)으로 팩트체크·보정.
    Perplexity API(유료) 대신 기존 Gemini 키로 실시간 검색 교차검증. grounding 미지원 시
    검색 없이 실측데이터 대조로 폴백(최소한 유가 '급등' 등 실측과 안 맞는 오탐은 잡음).
    반환: 텔레그램용 간결 텍스트. 키 없거나 실패 시 ''(브리핑은 그대로 발송)."""
    if not gkey or not brief:
        return ""
    _today = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")
    _prompt = (
        f"★오늘 날짜는 {_today}(KST)다. 모든 뉴스 최신성을 이 날짜 기준으로 판단하라.★\n"
        "너는 한국 주식 트레이더의 애널리스트다. 아래 [1차 브리핑]은 다른 패스가 RSS 뉴스로 만든 것이라 "
        "팩트 오류·과장이 있을 수 있다. 구글 검색으로 교차검증하라.\n"
        "★중립 규칙: 사용자가 '듣고 싶어할 답'이 아니라 '사실'만 말하라. 매수를 유도하지 말고, "
        "좋은 픽이 없으면 '오늘은 살 것 없음'이라고 솔직히 결론내라(억지 추천 금지).★\n"
        f"★날짜 규칙(중요): 각 재료의 '발생일'을 반드시 확인하라. 오늘({_today})로부터 2일 넘게 지난 뉴스는 "
        "이미 주가에 선반영됐거나 재료 소멸이다 → 보정 우선순위에 올리지 말고 '⚠️구뉴스(N일 전·선반영)'로 강등하라. "
        "며칠·몇달 전 뉴스를 '오늘 나와서 선반영 덜 됨'이라고 절대 쓰지 마라(치명적 오류). "
        "발생일이 불확실하면 검색으로 확인하고, 각 재료 옆에 발생일(MM/DD)을 명시하라.★\n"
        "규칙: ①지수 등락·유가·실적·수주·인물발언 등 구체 수치/사실을 검증하고 틀리면 실제값으로 보정. "
        "②각 핵심 주장(재료)은 반드시 구글 검색을 실제로 실행해 '✅확인/⚠️부분확인/❌반박' 중 하나로 판정하고, "
        "확인되면 근거(회사·금액·날짜)를 1줄로 요약하라. '❓미확인'은 검색을 진짜 해봐도 근거가 안 나올 때만 쓰고 남발 금지. "
        "확인 가능한 재료를 게을러서 미확인으로 뭉개지 마라(실제 호재를 놓치면 손해다). "
        "③뉴스만 있고 수주·계약·공시 근거 없는 테마는 '추격주의'로 강등. "
        "★선반영 규칙(가장 중요): 재료가 '진짜'인 것과 '지금 살 만한 것'은 다르다. "
        "각 종목마다 검색으로 '그 재료가 언제 나왔고, 그 뒤 주가가 이미 크게 올랐는지'를 확인하라. "
        "재료가 며칠 전 뉴스이고 주가가 이미 급등했으면 = 선반영 → '⚠️선반영(추격금지)'로 표시하고 보정 우선순위에서 제외/강등하라. "
        "보정 우선순위 TOP3는 '재료가 확인되면서도 아직 주가에 덜 반영된(선반영 안 된)' 종목만 올려라. 확인만 됐다고 이미 오른 종목을 1위로 올리지 마라.★ "
        "④[실측 시장데이터]와 브리핑이 다르면 실측을 정답으로 간주. "
        "특히 유가·지수는 '인트라데이 등락'과 '최종/주간 종가'가 다를 수 있으니, 검색으로 최종 종가를 확인해 방향을 재판정하라.\n"
        "★★재료 분류(각 종목·테마마다 필수)★★\n"
        "⑤직접성: [직접수혜(그 기업의 공시·계약·실적·수주)] / [산업수혜(공급망·업황)] / [심리수혜(정책발언·지정학·해외사례)] / [무관·억지연결] 중 하나로 분류.\n"
        "⑥등급: A=공시·계약·실적·정부확정 / B=Reuters·Bloomberg 등 구체 산업뉴스 / C=정책·지정학·해외사례(심리) / D=루머·과거뉴스·직접수혜 미확인. A·B만 매매 근거로, C는 심리 참고, D는 사용 금지.\n"
        "⑦테마금지: 해외 전쟁·방공계약→국내 방산, 해외 로봇·자율주행→국내 로봇·전장, 재난·유가→국내 건설·정유 — 이런 연결은 "
        "국내 기업의 '직접 계약·공급관계·공시'가 검색으로 확인되지 않으면 절대 '직접수혜'로 쓰지 말고 '심리수혜(C)'로 강등하라.\n"
        "★안전규칙: 검색 결과가 없을 때만, 뉴스의 '시점·최신성·진위'를 단정하지 마라. "
        "특히 학습기억으로 '과거 뉴스다/몇년도 일이다'라고 추측해 실제 오늘 재료를 가짜로 몰지 마라(과거 사건이 지금 재발할 수도 있음). "
        "이 경우에만 '❓미확인(개장 후 확인)'으로 표기.★\n\n"
        f"[실측 시장데이터]\n{mdetail}\n\n[1차 브리핑]\n{brief}\n\n"
        "★출력 규칙: 검색·분석 과정이나 서론을 절대 쓰지 마라. 아래 4개 항목만, 딱 한 번씩, "
        "정확히 이 순서·이 제목으로 출력하라(항목 제목이나 형식을 반복 출력 금지). 텔레그램용으로 간결하게.★\n"
        "🔎 팩트체크: (틀린/과장된 수치·주장 2~4개를 '주장→✅확인/⚠️부분/❌반박(근거 1줄)'로)\n"
        "🏆 보정 우선순위 TOP3: (재료 확인+선반영 안 된 종목만·딱 3개 — '종목명 [등급A~D·직접성] 재료(발생일) / ⚡반대근거:안 갈 이유 1개' 형식)\n"
        "🚫 강등/제외: (억지 테마연결·심리수혜(C)·이미 급등한 선반영·D등급 — 종목/테마·이유 1줄)\n"
        "🔴 자기비평: (위 보정안을 비평가 입장에서 다시 봐 — 이 판단이 틀릴 수 있는 최대 약점 1개를 솔직히)\n"
        "한 줄 결론:")
    _errs = []
    # ── 1순위: 신버전 SDK(google-genai) + 구글검색 grounding (Gemini 2.x 정식 방식) ──
    try:
        from google import genai as _ng
        from google.genai import types as _nt
        _client = _ng.Client(api_key=gkey)
        for _mn in ("gemini-2.5-flash", "gemini-2.5-pro"):
            try:
                _resp = _client.models.generate_content(
                    model=_mn, contents=_prompt,
                    config=_nt.GenerateContentConfig(
                        tools=[_nt.Tool(google_search=_nt.GoogleSearch())]))
                _txt = getattr(_resp, "text", None)
                if _txt:
                    return _txt.strip() + "\n🌐(구글검색 grounding)"
            except Exception as _ne:
                _errs.append(f"신SDK/{_mn}:{type(_ne).__name__}")
    except Exception as _nie:
        _errs.append(f"신SDK미설치:{type(_nie).__name__}")
    # ── 2순위: 구버전 SDK(google-generativeai) — grounding 시도 후 실패 시 검색 없이 폴백 ──
    try:
        import google.generativeai as genai
        genai.configure(api_key=gkey)
        _tool_variants = ("google_search_retrieval", [{"google_search_retrieval": {}}], None)
        for _mn in ("gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-pro"):
            for _tv in _tool_variants:
                try:
                    _kw = {"request_options": {"timeout": 60}}
                    if _tv is not None:
                        _kw["tools"] = _tv
                    _resp = genai.GenerativeModel(_mn).generate_content(_prompt, **_kw)
                    _txt = getattr(_resp, "text", None)
                    if _txt:
                        return _txt.strip() + ("\n🌐(구글검색 grounding)" if _tv is not None
                                               else "\n(검색 미지원 — 실측 대조만·구SDK)")
                except Exception as _ge:
                    _errs.append(f"구SDK/{_mn}/{('검색' if _tv else '기본')}:{type(_ge).__name__}")
    except Exception as _oie:
        _errs.append(f"구SDK설정:{type(_oie).__name__}")
    print(f"[팩트체크 진단] 전 시도 실패 — {' / '.join(_errs)[:300]}")
    return ""


def _naver_news(cid, csec, query, display=10):
    """네이버 뉴스 검색 최신순 — [{title,description,link}]. 실패 시 [](진단 출력)."""
    try:
        r = requests.get("https://openapi.naver.com/v1/search/news.json",
                         headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec},
                         params={"query": query, "display": display, "sort": "date"}, timeout=6)
        if r.status_code == 200:
            return r.json().get("items", []) or []
        print(f"[네이버뉴스 진단] '{query}' HTTP {r.status_code} · 응답: {r.text[:200]}")
    except Exception as _e:
        print(f"[네이버뉴스 진단] '{query}' 예외: {type(_e).__name__}: {_e}")
    return []


# [V21.5] RSS 폴백 — 네이버 검색 스코프 없거나 실패 시 국내 경제 RSS로 뉴스 수집(키 불필요).
_RSS_FEEDS = (
    ("증권", "https://www.yna.co.kr/rss/market.xml"),         # 국내 증권(재료 밀집) — 핵심
    ("세계", "https://www.yna.co.kr/rss/international.xml"),   # 세계 메인(미국장·지정학·중국·유가)
    ("산업", "https://www.yna.co.kr/rss/industry.xml"),       # 산업(반도체·기업 글로벌)
)   # [V21.9] 경제 일반(정치·부고 노이즈) 제외 — 증권+세계+산업만(사용자 요청)


def _rss_news(per_feed=40, hours=12):
    """국내 증권 + 세계/산업 RSS에서 최근 `hours`시간 이내 뉴스 수집(피드별 상한 per_feed).
    [{title,description,src,time,ts}]. pubDate로 12시간 필터 → 최신순. feedparser 없이 stdlib 파싱."""
    import xml.etree.ElementTree as _ET
    import re as _re
    from email.utils import parsedate_to_datetime as _pdt
    _kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    _cutoff = _kst - datetime.timedelta(hours=hours)
    arts = []
    for _nm, _url in _RSS_FEEDS:
        try:
            r = requests.get(_url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                print(f"[RSS 진단] {_nm} HTTP {r.status_code}")
                continue
            _root = _ET.fromstring(r.content)
            _cnt = 0
            for _it in _root.iter("item"):
                if _cnt >= per_feed:
                    break
                _t = (_it.findtext("title") or "").strip()
                _d = _re.sub(r"<[^>]+>", "", _it.findtext("description") or "").strip()
                _pd = _it.findtext("pubDate") or ""
                _tm, _ts = "", None
                try:
                    _dt = _pdt(_pd)                        # RFC822 → datetime
                    if _dt.tzinfo:
                        _dt = _dt.astimezone(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
                    _ts = _dt
                    _tm = _dt.strftime("%H:%M")
                except Exception:
                    pass
                if _ts is not None and _ts < _cutoff:     # 12시간 초과된 오래된 뉴스 제외
                    continue
                if _t:
                    arts.append({"title": _t, "description": _d, "src": _nm, "time": _tm, "ts": _ts})
                    _cnt += 1
            print(f"[RSS 진단] {_nm} {_cnt}건(최근 {hours}h)")
        except Exception as _e:
            print(f"[RSS 진단] {_nm} 예외: {type(_e).__name__}")
    # 최신순 정렬(시각 있는 것 우선)
    arts.sort(key=lambda a: a.get("ts") or datetime.datetime(1970, 1, 1), reverse=True)
    return arts


_GEMINI_MODELS = ("gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-pro")   # flash 우선(빠름)·pro 폴백


def _gemini_generate(gkey, prompt):
    """Gemini 텍스트 생성 — 모델 후보 순차 시도. 실패/미설치 시 None(진단 출력)."""
    try:
        import google.generativeai as genai
    except Exception as _ie:
        print(f"[Gemini 진단] 라이브러리 미설치 → py -m pip install google-generativeai  ({_ie})")
        return None
    try:
        genai.configure(api_key=gkey)
    except Exception as _ce:
        print(f"[Gemini 진단] 설정 오류: {_ce}")
        return None
    _errs = []
    for _mn in _GEMINI_MODELS:
        try:
            _resp = genai.GenerativeModel(_mn).generate_content(
                prompt, request_options={"timeout": 60})   # 큰 프롬프트 대비(무한대기 방지)
            _txt = getattr(_resp, "text", None)
            if _txt:
                return _txt.strip()
            _errs.append(f"{_mn}:빈응답")
        except Exception as _ge:
            _errs.append(f"{_mn}:{type(_ge).__name__}")
    print(f"[Gemini 진단] 전 모델 실패 — {' / '.join(_errs)[:250]}")
    return None


_NEWS_KEYWORDS = ("특징주", "수주", "실적", "신약 임상", "정책 수혜")


def _stock_news_titles(code, n=10):
    """종목 최근 뉴스 제목 리스트 — 네이버 모바일 뉴스 API. 실패 시 []."""
    titles = []
    try:
        r = requests.get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize={n}&page=1",
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"},
                         timeout=5)
        _j = r.json()

        def _w(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("title", "titleText", "aiTitle") and isinstance(v, str):
                        titles.append(v)
                    else:
                        _w(v)
            elif isinstance(o, list):
                for it in o:
                    _w(it)
        _w(_j)
    except Exception:
        pass
    # 중복 제거·상한
    _out = []
    for t in titles:
        if t not in _out:
            _out.append(t)
    return _out[:n]


def _gemini_stock_news_verdict(gemini_key, code, name):
    """[V21.7] 종배 확정픽 AI 뉴스판정 — 최근 뉴스 제목을 Gemini가 읽고 오버나이트 적합성 한 줄.
    반환: '\\n🤖 AI뉴스: ...' or ''. Gemini/뉴스 없으면 빈 문자열(무영향)."""
    if not gemini_key:
        return ""
    _titles = _stock_news_titles(code, 10)
    if not _titles:
        return ""
    _prompt = (f"종목 {name}({code})의 최근 뉴스 제목이야. "
               "종가배팅(오늘 종가 매수→내일 아침 시가에 익절하는 오버나이트 단타)에 적합한지 딱 한 줄로 판정해.\n"
               "[뉴스제목]\n" + "\n".join("- " + t for t in _titles) + "\n"
               "[출력 형식·한 줄]: 판정(호재/중립/악재) · 재료강도(상/중/하) · "
               "오버나이트적합(적합/주의/부적합) · 핵심이유(짧게). "
               "이미 재료로 급등해 차익실현 위험이면 '주의', 악재면 '부적합'.")
    _v = _gemini_generate(gemini_key, _prompt)
    return f"\n🤖 AI뉴스: {_v.strip()}" if _v else ""


def _watchlist_check(token, key, secret, code, px, chg, turn, ng=None):
    """[V22.6] 관심종목 10대 기준 중 자동측정 가능 항목 체크(사용자 매매원칙 적용).
    측정: ①거래대금상위 ②500억+ ③외인/기관수급 ⑤정배열·전고/신고 ⑥재료 ⑨끼(변동성). 반환: 통과 리스트."""
    _p = []
    try:
        if turn and turn >= 50_000_000_000:                 # ② 거래대금 500억+
            _p.append("거래대금500억+")
        if code in {s["code"] for s in _volume_rank(token, key, secret, top=40)}:  # ① 거래대금 상위
            _p.append("거래대금상위")
        _f, _o = _investor_est(token, key, secret, code)    # ③ 외인/기관 수급(+)
        if (_f + _o) > 0:
            _p.append("수급유입")
        ds = _daily_setup(token, key, secret, code, px)     # ⑤ 정배열(px>5MA>20MA)
        if ds and ds.get("ma5") and ds.get("ma20") and px > ds["ma5"] > ds["ma20"]:
            _p.append("정배열")
        _rh = _recent_high(token, key, secret, code, 20)    # ⑤ 전고 근접/신고가
        if _rh and px >= _rh:
            _p.append("신고가")
        elif _rh and px >= _rh * 0.98:
            _p.append("전고근접")
        if ng in ("S", "A"):                                # ⑥ 재료 모멘텀
            _p.append(f"재료{ng}급")
    except Exception:
        pass
    return _p


def _verify_news_picks(token, key, secret, report):
    """[V21.6] Gemini 브리핑에서 6자리 종목코드 추출 → KIS로 오늘 차트상태 검증(선반영/거래대금).
    반환: 검증 텍스트 or ''. 뉴스픽이 이미 급등했으면 sell-the-news, 안 움직였으면 내일 여지."""
    if not (token and report):
        return ""
    import re as _re
    # [V25.14] 추천 구간(📌 주목 테마 ~ ⚠️피할것 앞)만 검증 — 피할것·주도주 서술 종목을
    #   '✅내일 주목'으로 표시하던 모순 제거(브리핑 성적표 기록과 동일 기준).
    _c1 = report.find("📌"); _c2 = report.find("⚠️")
    if _c2 < 0:
        _c2 = report.find("피할")
    _region = report[(_c1 if _c1 >= 0 else 0):(_c2 if _c2 > 0 else len(report))]
    # '종목명(코드)' 쌍에서 이름 맵 추출 → 검증줄에 코드 대신 종목명 표시
    _name_map = {}
    for _nm, _cd in _re.findall(r"([가-힣A-Za-z0-9·&.\-]{2,20}?)\s*\((\d{6})\)", _region):
        _name_map.setdefault(_cd, _nm.strip())
    _codes = []
    for _c in _re.findall(r"\(?(\d{6})\)?", _region):        # 괄호 안/밖 6자리(추천 구간만)
        if _c not in _codes:
            _codes.append(_c)
    _codes = _codes[:8]
    if not _codes:
        return ""
    _lines = []
    for _cd in _codes:
        try:
            _px, _chg, _turn = _price_and_turnover(token, key, secret, _cd)
            if not _px:
                continue
            _disp = _ma20_disparity(token, key, secret, _cd, _px)
            _dt = f"이격 {_disp:+.0f}%" if _disp is not None else "이격 –"
            _tk = f"거래대금 {(_turn or 0)/1e8:,.0f}억"
            # 판정: 이미 급등(등락≥5 or 이격≥10) = sell-the-news / 저조 거래 = 관심밖 / 그 외 = 주목
            if (_chg or 0) >= 5.0 or (_disp is not None and _disp >= 10.0):
                _vd = "⚠️이미 급등(선반영·추격주의)"
            elif (_turn or 0) < 10_000_000_000:
                _vd = "💤거래 저조(관심 유입 확인 필요)"
            else:
                _vd = "✅거래 받쳐줌(내일 주목)"
            _dn = _name_map.get(_cd, _cd)              # 종목명(없으면 코드)
            _lines.append(f"• {_dn} {_px:,}({(_chg or 0):+.1f}%)·{_dt}·{_tk} → {_vd}")
        except Exception:
            continue
    if not _lines:
        return ""
    return "\n🔍 뉴스픽 차트검증(오늘 종가 기준)\n" + "\n".join(_lines)


def check_evening_news(now_kst, state, token_tg, chat_id, naver_id, naver_secret, gemini_key,
                       kis_key=None, kis_secret=None):
    """[V21.4] 저녁 뉴스 시황 스캐너(17:00~22:00, 당일 1회) — 마감 후 뉴스는 내일 갭·수급 선행지표.
    네이버/RSS 뉴스 수집 → Gemini가 '내일 주목 테마·대장주·해외변수·선반영주의' 브리핑 → KIS 차트검증 첨부.
    매수 아님(참고)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((17 * 60) <= m <= (22 * 60)):
        return
    today = now_kst.strftime("%Y%m%d")
    if state.get("evening_news_day") == today:
        return
    import re as _re
    seen = set(); arts = []
    # [V22.1] 네이버 검색 API는 스코프 막힘(401 영구) → 시도 스킵, RSS만 사용(국내+세계 피드별 골고루)
    _src = "RSS"
    # [V22.8] 뉴스 기간 — 평일 24h(신선 재료). 월요일은 주말·금요일 마감후 뉴스 커버 위해 72h로 자동 확대.
    # [V25.9] 저녁 브리핑은 '항상 다음 거래일용' → 언제나 24h(최신 재료).
    #   월요일 저녁도 화요일용이라 주말(72h) 뉴스는 이미 월요일 장에 소화돼 노이즈일 뿐. 주말 커버는 '월요일 아침' 몫.
    _news_hours = 24
    for it in _rss_news(50, _news_hours):                 # 피드별 최대 50건·최근 N시간
        _t = it.get("title", "").replace("&quot;", '"').replace("&amp;", "&")
        _d = it.get("description", "")
        _k = _t[:40]
        if not _t or _k in seen:
            continue
        _tg = f"{it.get('src', '')} {it.get('time', '')}".strip()
        seen.add(_k); arts.append(f"[{_tg}] {_t} :: {_d}")   # [출처 시각] 태그(최근·구분)
    if not arts:
        print("[저녁뉴스] 수집 0건 — 네이버·RSS 모두 실패(네트워크/피드 확인)")
        return
    print(f"[저녁뉴스] 소스={_src} · 수집 {len(arts)}건(최근 {_news_hours}h)")
    _batch = "\n".join(arts[:80])
    # [V22.2] 실측 시장데이터 주입 — AI가 뉴스 서사로 방향 상상(예:'유가 상승') 못 하게, 실제 수치를 우선시키게.
    try:
        _ct = kis_token(kis_key, kis_secret) if (kis_key and kis_secret) else None
        _, _, _mdetail, _ = compute_macro(_ct, kis_key, kis_secret)
    except Exception:
        _mdetail = ""
    # [V22.4] 오늘 거래대금 상위 20종 주입 — 뉴스 테마 vs 실제 자금 몰린 종목 교차(거래대금이 먼저)
    _vtok = kis_token(kis_key, kis_secret) if (kis_key and kis_secret) else None
    _vrank_txt = ""; _vlead_news = ""
    try:
        if _vtok:
            _vr = _volume_rank(_vtok, kis_key, kis_secret, top=20)
            _vrank_txt = "\n".join(f"- {s['name']}({s['code']}) {s['chg']:+.1f}% 거래대금 {s['turnover']/1e8:,.0f}억"
                                   for s in _vr if s.get("turnover"))
            # [V22.5] 역방향 — 거래대금 상위 8종의 종목뉴스 조회(돈 몰린 이유·모멘텀 지속성 분석용)
            _lead = []
            for s in [x for x in _vr if x.get("turnover")][:8]:
                _tt = _stock_news_titles(s["code"], 3)
                if _tt:
                    _lead.append(f"● {s['name']}({s['code']}) {s['chg']:+.1f}%·{s['turnover']/1e8:,.0f}억: "
                                 + " / ".join(_tt[:3]))
            _vlead_news = "\n".join(_lead)
    except Exception as _vre:
        print("거래대금랭킹 주입 오류:", _vre)
    report = None
    if gemini_key:
        _prompt = ("너는 한국 주식 실전 트레이더야. 아래는 오늘 장 마감 후 뉴스(각 줄 앞 [출처 시각] — 증권/세계/산업). "
                   "★장 마감(15:30) 이후 나온 최근 뉴스일수록 내일 갭·수급에 더 직접적이니 우선 고려해★ "
                   "한국 증시는 미국장·반도체 글로벌·지정학·환율에 크게 좌우되니 "
                   "★[세계] 뉴스가 내일 한국장(코스피/코스닥)에 미칠 영향을 반드시 반영해★ 내일 주목 종목/테마를 골라줘. "
                   "이미 오늘 크게 오른 재료는 sell-the-news 주의, 불확실하면 솔직히 '재료 약함'이라고 해.\n\n"
                   "★★중요1: 아래 [실측 시장데이터]가 실제 현재 수치야. 뉴스에서 '유가 상승·환율 급등' 같은 방향을 "
                   "네 마음대로 추론하지 말고, 반드시 이 실측값을 우선해. 뉴스 서사와 실측이 다르면(예: 지정학 우려 뉴스지만 "
                   "WTI 실제 하락) 실측을 따르고 그 괴리를 명시해.★★\n"
                   "★★중요2: 주가는 팩트보다 '개인투자자(개미) 군중심리'로 움직여. 각 재료가 개미에게 어떤 감정"
                   "(공포/탐욕/추격/기대/실망)을 유발할지, 그래서 내일 수급이 몰릴지(매수 유입) 빠질지(회피) 예측해. "
                   "단, 심리는 추정이니 실측·차트와 충돌하면 무리한 낙관 금지.★★\n"
                   "★★중요3: [오늘 거래대금 상위]가 오늘 실제 자금이 몰린 종목이야. 뉴스 테마가 여기 상위 종목과 겹치면 "
                   "'실제 수급 확인=강한 재료', 뉴스만 있고 거래대금 상위에 없으면 '재료만·자금 미유입=약함'으로 판정해. "
                   "거래대금 상위인데 관련 뉴스 없으면 '숨은 주도주'로 이유를 추정해봐.★★\n"
                   "★★중요4: [거래대금 주도주 뉴스]는 오늘 실제 돈이 몰린 종목의 뉴스야. 각 종목이 오늘 왜 올랐는지(재료), "
                   "그 모멘텀이 단발인지 며칠 갈지(지속성), 내일도 자금이 더 들어올지 판정해. 이게 '정답(자금)을 먼저 보고 이유를 찾는' 방식이야.★★\n"
                   f"[실측 시장데이터]\n{_mdetail}\n\n"
                   f"[오늘 거래대금 상위]\n{_vrank_txt or '(조회 실패)'}\n\n"
                   f"[거래대금 주도주 뉴스]\n{_vlead_news or '(조회 실패)'}\n\n"
                   f"[뉴스]\n{_batch}\n\n"
                   "[출력: 텔레그램용·간결·이모지]\n"
                   "🌍 해외 변수: (미국장·반도체·지정학·환율 중 내일 국장에 영향줄 것 1~2줄, 실측 기준)\n"
                   "🧠 시장심리: (내일 개미 심리 방향 — 공포/탐욕/관망 중 + 자금 몰릴 섹터 vs 회피할 섹터, 1~2줄)\n"
                   "💰 거래대금 주도주 모멘텀: (오늘 자금 몰린 상위 종목 2~3개 — 각: 종목 · 오른 이유 · 지속성(단발/며칠+) · 내일 추격가능?)\n"
                   "🌙 내일 시황 브리핑\n"
                   "📌 주목 테마 TOP 3 — 딱 3개만(집중·고확신). 각: 테마 · 대장주(반드시 종목명 옆에 6자리 종목코드 괄호로! 예: 두산에너빌리티(034020)) · "
                   "재료강도(상/중/하) · 지속성(단발/며칠) · 개미심리(몰릴/빠질) · 선반영주의 · "
                   "★반대근거(이 종목이 안 갈/빠질 이유 1개 — 선반영·수급이탈·재료약함·차익실현 등 반드시 명시)★\n"
                   "   ※반대근거가 재료보다 강한 종목은 3개에 넣지 말고 빼라(진짜 확신 3개만).\n"
                   "⚠️ 피할 것 (재료소멸·이미급등·악재·심리악화)\n"
                   "한 줄 총평.")
        print("[저녁뉴스] 🤖 Gemini 판정 중... (5~20초 소요)")
        report = _gemini_generate(gemini_key, _prompt)
        print(f"[저녁뉴스] Gemini 판정 {'완료' if report else '실패→헤드라인'}")
    if report:
        _verify = ""
        try:
            _verify = _verify_news_picks(_vtok, kis_key, kis_secret, report)   # 위에서 만든 토큰 재사용
            if _verify:
                print("[저녁뉴스] 차트검증 첨부 완료")
        except Exception as _ve:
            print("차트검증 오류:", _ve)
        # [V25.14] 브리핑 픽 기록 — ★'📌 주목 테마 TOP5(대장주)' 구간만★ 성적표에 적립.
        #   (버그: 이전엔 리포트 전체를 긁어 ⚠️피할것·거래대금 주도주 서술의 종목까지 '브리핑 픽'으로
        #    저장 → 추천 안 한/경고한 종목이 '적중'으로 찍히고 --analyze 승률 오염. 추천 구간만 기록.)
        try:
            import re as _re2
            _c1 = report.find("📌")                       # 주목 테마 시작
            _c2 = report.find("⚠️")                       # 피할 것 시작(그 앞까지가 추천)
            if _c2 < 0:
                _c2 = report.find("피할")
            _pick_region = report[(_c1 if _c1 >= 0 else 0):(_c2 if _c2 > 0 else len(report))]
            _seen_bc = set()
            for _bn, _bc in _re2.findall(r"([가-힣A-Za-z0-9·&.\-]{2,20}?)\s*\((\d{6})\)", _pick_region):
                if _bc in _seen_bc:
                    continue
                _seen_bc.add(_bc)
                _bp, _, _ = _price_and_turnover(_vtok, kis_key, kis_secret, _bc) if _vtok else (None, None, None)
                if _bp:
                    _scorecard_append(now_kst, "브리핑", _bc, _bn.strip(), _bp)
        except Exception as _be:
            print("브리핑 성적표 기록 오류:", _be)
        # [V24.8] 팩트체크 레이어 — Gemini+구글검색(grounding)으로 1차 브리핑 교차검증·보정
        _fc = ""
        try:
            if gemini_key:
                print("[저녁뉴스] 🔎 Gemini 검색 팩트체크 중... (10~40초)")
                _fc = _gemini_factcheck(gemini_key, report, _mdetail)
                print(f"[저녁뉴스] 팩트체크 {'완료' if _fc else '실패/생략'}")
        except Exception as _fce:
            print("팩트체크 오류:", _fce)
        _fc_block = f"\n\n━━ 🔎 검색 교차검증 ━━\n{_fc}" if _fc else ""
        _msg = (f"{SIG_WATCH}\n{report}\n{_verify}{_fc_block}\n\n"
                "※ AI 참고용 — 개장 후 거래대금·수급 확인 필수(뉴스는 보조·후행 가능)")
    else:
        _heads = "\n".join("• " + a.split(" :: ")[0] for a in arts[:8])
        _msg = (f"{SIG_WATCH}\n🌙 내일 참고 뉴스(헤드라인)\n{_heads}\n"
                "※ AI 판정 미가동(Gemini 키 없음/실패) — 헤드라인만")
    if send_telegram(token_tg, chat_id, _msg):
        state["evening_news_day"] = today
        print(f"[저녁뉴스] 브리핑 발송 — 수집 {len(arts)}건 · AI {'ON' if report else 'OFF'}")


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


def _sector_name(token, key, secret, code):
    """종목 업종명 — inquire-price bstp_kor_isnm. 종배 분산(다른 섹터) 판정용. 실패 시 ''."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010100"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=6)
        o = r.json().get("output", {})
        if isinstance(o, dict):
            return (o.get("bstp_kor_isnm") or "").strip()
    except Exception:
        pass
    return ""


def _market_cap(token, key, secret, code):
    """시가총액(억원) — inquire-price hts_avls. 실패 시 None. 수주 임팩트 = 계약금액/시총 판정용."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010100"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=6)
        o = r.json().get("output", {})
        if isinstance(o, dict):
            return _to_int(o.get("hts_avls"))     # 억원 단위
    except Exception:
        pass
    return None


def _contract_detail(dart_key, rcept_no):
    """[Phase2] DART 공급계약 상세문서에서 '최근매출액 대비(%)'·계약기간(년) 추출(best-effort).
    반환 {'sales_ratio': float|None, 'years': float|None, 'amount': float|None(억원)}. 파싱 실패 시 None."""
    out = {"sales_ratio": None, "years": None, "amount": None}
    try:
        import io as _io, zipfile as _zip, re as _re
        r = requests.get("https://opendart.fss.or.kr/api/document.xml",
                         params={"crtfc_key": dart_key, "rcept_no": rcept_no}, timeout=8)
        if r.status_code != 200 or not r.content:
            return out
        try:
            _zf = _zip.ZipFile(_io.BytesIO(r.content))
            _raw = b"".join(_zf.read(n) for n in _zf.namelist())
        except Exception:
            _raw = r.content
        try:
            txt = _raw.decode("utf-8", "ignore")
        except Exception:
            txt = _raw.decode("cp949", "ignore")
        txt = _re.sub(r"<[^>]+>", " ", txt)       # 태그 제거
        txt = _re.sub(r"\s+", " ", txt)
        # 최근 매출액 대비(%) — 라벨 뒤 첫 숫자
        m = _re.search(r"매출액\s*대비[^0-9\-]{0,15}([0-9]+(?:\.[0-9]+)?)", txt)
        if m:
            out["sales_ratio"] = float(m.group(1))
        # [V25.16] 계약금액(절대액) — 대형주라도 절대 규모 크면 강신호 유지용(삼성전기 1조722억 놓침 대응)
        ma = _re.search(r"계약금액[^0-9]{0,20}([0-9][0-9,]{7,})", txt)   # 8자리↑(천만원↑) 숫자
        if ma:
            try:
                out["amount"] = int(ma.group(1).replace(",", "")) / 1e8    # 원 → 억원
            except Exception:
                pass
        # 계약기간: 시작~종료일(YYYY.MM.DD 또는 YYYY-MM-DD 2개)로 연수 추정
        ds = _re.findall(r"(20[0-9]{2})[.\-/ ]\s*([01]?[0-9])[.\-/ ]\s*([0-3]?[0-9])", txt)
        if len(ds) >= 2:
            try:
                y0, m0, d0 = map(int, ds[0]); y1, m1, d1 = map(int, ds[-1])
                _days = (datetime.date(y1, m1, d1) - datetime.date(y0, m0, d0)).days
                if _days > 0:
                    out["years"] = round(_days / 365.0, 1)
            except Exception:
                pass
    except Exception:
        pass
    return out


def check_dart_disclosures(now_kst, state, token_tg, chat_id, dart_key, kis_key=None, kis_secret=None, sev=1):
    """DART 당일 신규 공시 폴링 → 호재 공시는 우리 엔진(거래대금·이격)으로 교차검증해 '진입후보 선정'.
    악재=경고 / 실적=내용확인 / 호재=거래대금·비과열이면 🎯진입후보, 아니면 관망·선점. 예외 전파 없음.
    [V20.5] 승률 개선: 계약 해지/철회=악재 재분류 · 거래대금 0/미미=선점만 · 리스크오프(sev2)=강매수 억제 ·
            하락과대(-3%↓)·낙폭과대(이격-15%↓)=강매수 금지(관망)."""
    if not dart_key:
        return
    m = now_kst.hour * 60 + now_kst.minute
    # [V25.16] 창 확대 07:00~20:00 — 대형 공급계약·수주는 장 마감 후(16~18시) 공시 많음(삼성전기 놓침 대응).
    #   마감후 공시는 종가 매수 불가지만 NXT(16~20시)/익일 대응 가능 → 알림 가치 큼.
    if not ((7 * 60) <= m <= (20 * 60)):
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
    _vrank_codes = None                            # [V21.0] 당일 거래대금 랭킹 top40(주도주 교차검증용·지연조회)
    for _it in (j.get("list") or []):
        _rcp = _it.get("rcept_no")
        _stock = (_it.get("stock_code") or "").strip()
        if not _rcp or not _stock or sent.get(_rcp):
            continue                            # 신규·상장사(종목코드 有)만
        _nm = _it.get("report_nm", "") or ""
        _corp = _it.get("corp_name", "")
        _neg = any(k in _nm for k in _DART_NEG)
        _pos = any(k in _nm for k in _DART_POS)
        _perf = any(k in _nm for k in _DART_PERF)  # [V18.6] 진짜 잠정실적만(증권발행실적 오탐 제거)
        # [V25.16] SKIP은 '호재·악재 키워드 없을 때만' 적용 — "단일판매ㆍ공급계약체결(자율공시)"가
        #   '자율공시)'에 걸려 스킵되던 버그(삼성전기 1조722억 놓침). 진짜 재료면 자율공시여도 처리.
        if any(k in _nm for k in _DART_SKIP) and not (_pos or _neg):
            sent[_rcp] = True
            continue
        # [V20.5 버그수정] 호재 키워드라도 '해지·철회·취소·무산·불발·중단' 붙으면 계약 무산 = 악재로 재분류
        #   예: "단일판매공급계약해지" → '단일판매'로 호재 오탐 → 실제론 악재
        if _pos and any(k in _nm for k in ("해지", "철회", "취소", "무산", "불발", "중단")):
            _pos = False
            _neg = True
        # [V20.5] '매매거래정지해제'=거래재개(악재 아님) → 악재 오탐 제거
        if _neg and ("매매거래정지" in _nm) and ("해제" in _nm):
            _neg = False
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
                          f"{SIG_WATCH}\n👀 [공시 관찰·선점] {_corp}({_stock})\n"
                          f"공시: {_nm} (호재 재료)\n"
                          f"🔥 장외/거래 전 — 개장 후 거래대금 붙는지 확인 · 아직 매수 아님\n{_url}")
            continue
        _disp = None
        try:
            _disp = _ma20_disparity(_tok, kis_key, kis_secret, _stock, _px)
        except Exception:
            pass
        _dtxt = f" · 이격 {_disp:+.0f}%" if _disp is not None else ""
        _st = f"지금 {_px:,}({(_chg or 0):+.1f}%) 상승중" if (_chg or 0) > 0 else f"지금 {_px:,}({(_chg or 0):+.1f}%)"
        # [V20.5 버그2] 거래대금 0/미미(50억↓) = 거래 안 붙음 → 강매수 금지, '관찰'로만(장전 0억 강매수 오발 차단)
        if (not _turn) or _turn < 5_000_000_000:
            send_telegram(token_tg, chat_id,
                          f"{SIG_WATCH}\n👀 [공시 관찰·선점] {_corp}({_stock})\n"
                          f"공시: {_nm} (호재 재료)\n"
                          f"{_st}{_dtxt} · 거래대금 {((_turn or 0)/1e8):,.0f}억(미형성/미미)\n"
                          f"🔥 거래 붙는지 확인 후 — 아직 매수 아님\n{_url}")
            continue
        _overheat = ((_chg or 0) >= 10.0) or (_disp is not None and _disp >= 12.0)
        if _overheat:                            # 이미 급등 → 추격 금지
            send_telegram(token_tg, chat_id,
                          f"{SIG_WATCH}\n📢 [공시·과열] {_corp}({_stock})\n"
                          f"공시: {_nm}\n{_st}{_dtxt} — 이미 급등, 추격 금지·눌림 대기\n{_url}")
            continue
        # [V20.5 버그3] 리스크오프(sev2) = 강매수 억제(모순 방지) — 관망 정보만
        if sev == 2:
            send_telegram(token_tg, chat_id,
                          f"{SIG_WATCH}\n📢 [공시·리스크오프 관망] {_corp}({_stock})\n"
                          f"공시: {_nm}\n{_st}{_dtxt} — 매크로 리스크오프라 강매수 보류(재료만 참고)\n{_url}")
            continue
        # [V20.5 버그4] 하락과대(-3%↓)·낙폭과대(이격 -15%↓) = 떨어지는 칼 → 강매수 금지, 관망
        if ((_chg or 0) <= -3.0) or (_disp is not None and _disp <= -15.0):
            send_telegram(token_tg, chat_id,
                          f"{SIG_WATCH}\n📢 [공시·하락중 관망] {_corp}({_stock})\n"
                          f"공시: {_nm}\n{_st}{_dtxt} — 호재나 하락/낙폭과대 중, 추격 금지·반등 확인 후\n{_url}")
            continue
        # [V20.8] 수주/공급계약 임팩트 판정(멘토 피드백) — 금액 크기만 X, 매출대비·계약기간·시총으로.
        #   Phase2: DART 상세문서에서 '매출액 대비%'·계약기간→연환산 임팩트. 연 5% 미만=미미.
        #   Phase1(폴백): 매출대비 파싱 실패 시 시총으로 — 시총 5조+ 대형주는 수주 임팩트 작음.
        _impact_txt = ""
        if any(k in _nm for k in ("공급계약", "단일판매", "수주")):
            _cd = _contract_detail(dart_key, _rcp)
            _ratio, _yrs, _amt = _cd.get("sales_ratio"), _cd.get("years"), _cd.get("amount")
            _weak = False; _why = ""
            if _ratio is not None:
                _eff = _ratio / max(_yrs or 1.0, 1.0)     # 연환산 매출대비%
                _impact_txt = (f" · 매출대비 {_ratio:.0f}%"
                               + (f"·{_yrs:.0f}년→연 {_eff:.1f}%" if _yrs else ""))
                if _eff < 5.0:                            # 연매출 대비 5% 미만 = 실적 영향 미미
                    _weak = True; _why = f"매출대비 임팩트 미미(연 {_eff:.1f}%)"
            elif _amt is not None and _amt >= 3000:       # [V25.16] 계약금액 3천억+ = 절대 규모 큼 → 대형주라도 강신호
                _impact_txt = f" · 계약 {_amt/10000:.2f}조(대형 수주)" if _amt >= 10000 else f" · 계약 {_amt:,.0f}억(대형 수주)"
            else:
                _mc = _market_cap(_tok, kis_key, kis_secret, _stock)   # Phase1 폴백
                if _mc and _mc >= 50_000:                 # 시총 5조+ 대형주 = 수주 임팩트 작음
                    _weak = True; _why = f"시총 {_mc/10000:.0f}조 대형주(수주 임팩트 작음)"
                elif _mc:
                    _impact_txt = f" · 시총 {_mc/10000:.1f}조"
            if _weak:
                send_telegram(token_tg, chat_id,
                              f"{SIG_WATCH}\n📢 [공시·임팩트 약함 관망] {_corp}({_stock})\n"
                              f"공시: {_nm}\n{_st}{_dtxt} — {_why} → 강신호 아님(참고만)\n{_url}")
                continue
        # [V23.5] 거래대금 랭킹은 '태그'로만 — 공시는 선행 재료라 아직 거래대금 안 붙은 게 정상.
        #   랭킹 강등(V21.0)은 공시 선행성과 모순 → 매수(강)은 유지하고 주도/비주도만 표시.
        if _vrank_codes is None:
            _vrank_codes = {s["code"] for s in _volume_rank(_tok, kis_key, kis_secret, top=100)}
        _lead_tag = "🔥주도주(거래대금 랭킹 內)" if _stock in _vrank_codes else "🌱비주도(선행 재료·거래 확인 필요)"
        # 🎯 진입후보 선정 — 호재 + 거래대금 50억↑ + 비과열 + 비하락 + 매크로 양호 + 임팩트 유효
        _stop = int(_px * 0.98); _t1 = int(_px * 1.03)
        _pull = _pullback_levels(_tok, kis_key, kis_secret, _stock, _px, _chg) if (_disp is not None and _disp >= 7) else ""
        send_telegram(token_tg, chat_id,
                      f"{SIG_BUY_STRONG}\n🎯 [공시 발굴 진입후보]{_elite_tag(_tok, kis_key, kis_secret, _stock)} {_corp}({_stock})\n"
                      f"공시: {_nm} (호재·선행 재료)\n"
                      f"{_st}{_dtxt} · 거래대금 {_turn/1e8:,.0f}억{_impact_txt} · {_lead_tag} · 비과열 ✅\n"
                      f"진입 {_px:,} · 손절 {_stop:,}(−2%) · 1차익절 {_t1:,}(+3%){_pull}\n"
                      f"⚠️ 소액·칼손절 · 공시=선행이라 빠름 · {_url}")
        _log_signal(state, now_kst, "공시발굴", _corp, _stock, _px)
    state["dart_sent"] = sent


# [V18.3] 종목 뉴스 재료 등급 — watcher 시가저격/진입에 뉴스 확인 연계(악재 스킵·재료 태그).
_NEWS_S_KW = ("수주", "계약 체결", "공급 계약", "납품", "수출 계약", "어닝 서프라이즈",
              "예상 상회", "컨센 상회", "목표주가 상향", "목표가 상향", "투자의견 상향", "기술수출", "FDA 승인")
_NEWS_A_KW = ("실적", "영업이익", "순이익", "흑자전환", "역대 최대", "호실적", "신약", "임상")
_NEWS_NEG_KW = ("무산", "해지", "철회", "횡령", "배임", "상장폐지", "감자", "유상증자", "적자전환",
                "소송", "불성실공시", "분식", "거래정지", "관리종목", "리콜")


_NEWS_GRADE_CACHE = {}   # [V25.23] {code_YYYYMMDD: (grade, is_bad)} — 일당 캐시로 판정 안정화


def _news_grade(code):
    """종목 뉴스 재료 등급 — 네이버 모바일 뉴스 제목 키워드. 반환 (grade, is_bad).
    grade: 'S'/'A'/'none'. is_bad: 악재. [V25.23] 일당 캐시 — 조회 실패/빈응답이면 그날 성공한
    등급을 재사용(같은 날 판정이 A→none 뒤집히던 버그 방지). 성공 결과만 캐시."""
    _key = code + (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y%m%d")
    titles = []
    try:
        r = requests.get(f"https://m.stock.naver.com/api/news/stock/{code}?pageSize=15&page=1",
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"},
                         timeout=7)
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
        return _NEWS_GRADE_CACHE.get(_key, ("none", False))   # 조회 실패 → 그날 캐시 재사용(판정 안정)
    if not titles:
        return _NEWS_GRADE_CACHE.get(_key, ("none", False))   # 빈응답(일시적) → 캐시 우선
    _res = ("none", False)
    for t in titles:
        if any(n in t for n in _NEWS_NEG_KW):
            _res = ("none", True); break                      # 제목 하나라도 악재 → 악재 판정
    else:
        blob = " ".join(titles)
        if any(k in blob for k in _NEWS_S_KW):
            _res = ("S", False)
        elif any(k in blob for k in _NEWS_A_KW):
            _res = ("A", False)
    _NEWS_GRADE_CACHE[_key] = _res                            # 성공 결과만 캐시 → 이후 안정
    return _res


# ══════════════════════════════════════════════════════════════════════════
# [V18.7] 조기 포착·급증진입을 watcher로 이관 — 대시보드 없이 상시 텔레그램(초입 신호).
#   거래대금 랭킹 → 일봉 셋업(기준선·5일선·이격·급증배수) → 조기포착/급증진입 판정 + 뉴스.
# ══════════════════════════════════════════════════════════════════════════
_EARLY_ETF_KW = ("ETN", "ETF", "선물", "레버리지", "인버스", "KODEX", "TIGER", "PLUS",
                 "ACE", "SOL", "KBSTAR", "리츠", "스팩", "채권",
                 "RISE", "KoAct", "히어로즈", "마이티", "WON", "BNK", "TIMEFOLIO",
                 "1Q", "FOCUS", "파워", "KIWOOM", "HANARO", "액티브", "커버드콜")   # [V22.9] 신규 ETF 브랜드 추가


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


def _vol_ratio_5d(token, key, secret, code):
    """[V22.7] 오늘 거래량 / 최근 5거래일(전일까지) 평균 거래량 배수. (오늘vol, 5일평균, 배수) or None."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        rows = [x for x in (r.json().get("output", []) or []) if isinstance(x, dict)]
        vols = [_to_int(x.get("acml_vol")) for x in rows]      # 최신순(오늘=[0])
        if len(vols) >= 6 and vols[0]:
            _avg5 = sum(vols[1:6]) / 5.0                        # 전일부터 5거래일 평균
            if _avg5 > 0:
                return vols[0], _avg5, vols[0] / _avg5
    except Exception:
        pass
    return None


def check_vol_surge(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V22.7] 거래량 급증 서치(마감권 15:00~15:25, 당일 1회) — 거래대금 상위 중
    '오늘 거래량 > 5일평균 2배' 종목을 거래대금 순위와 함께 알림. 관심종목 발굴용(매수 아님)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((15 * 60) <= m <= (15 * 60 + 25)):
        return
    today = now_kst.strftime("%Y%m%d")
    if state.get("volsurge_day") == today:
        return
    hits = []
    for _i, s in enumerate(_volume_rank(token, key, secret, top=40), start=1):
        if any(k in str(s["name"]) for k in _EARLY_ETF_KW):
            continue
        _vr = _vol_ratio_5d(token, key, secret, s["code"])
        if not _vr:
            continue
        _tv, _avg5, _mult = _vr
        if _mult >= 2.0:                                    # 오늘 거래량이 5일평균의 2배+
            hits.append((_mult, f"• {s['name']} {s['chg']:+.1f}% · 거래량 {_mult:.1f}배(5일평균) · "
                                f"거래대금 {s['turnover']/1e8:,.0f}억(거래대금 {_i}위)"))
        if len(hits) >= 15:
            break
    if hits:
        hits.sort(reverse=True)                             # 급증 배수 큰 순
        _body = "\n".join(h[1] for h in hits[:12])
        if send_telegram(token_tg, chat_id,
                         f"{SIG_WATCH}\n📊 [거래량 급증 서치] 오늘 거래량 > 5일평균 2배 + 거래대금 상위\n"
                         f"{_body}\n※ 관심종목 후보 — 개장 후/익일 수급·차트 확인(매수 아님)"):
            state["volsurge_day"] = today
        print(f"[거래량급증] {len(hits)}종 발송")
    else:
        state["volsurge_day"] = today
        print("[거래량급증] 해당 종목 없음")


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
        _hi20 = max([h for h in hgpr[1:21] if h]) if any(hgpr[1:21]) else 0   # 전고점(오늘 제외 최근20일 최고가)
        return {"ma5": ma5, "ma20": ma20,
                "disp": ((px / ma20 - 1) * 100) if ma20 else 0,
                "above5": bool(ma5 and px >= ma5),
                "kij_cross": bool(kijun and prevc < kijun <= px),
                "kij_near": bool(kijun and abs(px / kijun - 1) <= 0.02),
                "turnavg": turnavg, "hi20": _hi20,
                "prevlow": (lwpr[1] if len(lwpr) >= 2 and lwpr[1] else 0)}   # 전일 저점(익일 무효화 기준)
    except Exception:
        return None


def _big_trend_tag(token, key, secret, code, px):
    """[V23.7] MTF 큰추세 필터(방식B) — 일봉 정배열로 큰 방향 판정, 진입신호에 태그.
    큰 봉(추세)이 방향, 작은 봉이 타점 — 신호 떠도 큰추세 하락이면 '역방향 주의'로 걸러줌. 실패 시 ''."""
    try:
        ds = _daily_setup(token, key, secret, code, px)
        if not ds:
            return ""
        _ma5, _ma20 = ds.get("ma5"), ds.get("ma20")
        if not (_ma5 and _ma20):
            return ""
        if px > _ma5 > _ma20:
            return "\n📈 큰추세(일봉): 상승 ✅ (진입 방향 일치 — 큰 봉이 허락)"
        if px < _ma5 < _ma20:
            return "\n📉 큰추세(일봉): 하락 ⚠️ (역방향 진입 — 속임수 주의·보류 권장)"
        return "\n➖ 큰추세(일봉): 횡보 (방향 불명확 — 신중)"
    except Exception:
        return ""


def _weekly_volatility(token, key, secret, code):
    """[V25.0] 이번주(최근 5거래일) 변동성 — 주간 레인지%·5일 수익률·최신 종가.
    변동성 = (5일 최고가 − 5일 최저가) / 5일 최저가 × 100. 실패 시 None."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        rows = [x for x in (r.json().get("output", []) or []) if isinstance(x, dict)][:5]
        if len(rows) < 3:
            return None
        highs = [_to_int(x.get("stck_hgpr")) for x in rows]
        lows = [_to_int(x.get("stck_lwpr")) for x in rows]
        clos = [_to_int(x.get("stck_clpr")) for x in rows]
        if not (all(highs) and all(lows) and all(clos)):
            return None
        hi, lo = max(highs), min(lows)
        vol_pct = ((hi - lo) / lo * 100) if lo else 0
        ret5 = ((clos[0] / clos[-1] - 1) * 100) if clos[-1] else 0   # 최신순: [0]=오늘 [-1]=주초
        return {"vol": vol_pct, "ret5": ret5, "close": clos[0], "hi": hi, "lo": lo}
    except Exception:
        return None


def _volatility_scan(token, key, secret, gemini_key=None, top_n=8):
    """[V25.0] 주간 변동성 상위 스캐너(래리 윌리엄스式 물색). 거래대금 상위 유니버스에서
    이번주 변동성 큰 종목을 랭킹 → 재료/선반영/수급/눌림 필터 태그 첨부.
    ★매수신호 아님 — '관찰 후보 리스트'. 진입은 필터 통과분만.★ 반환: 텔레그램 텍스트."""
    cands = []
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg = s["code"], s["name"], s["px"], s["chg"]
        if not px or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        _budget += 1
        if _budget > 32:
            break
        wv = _weekly_volatility(token, key, secret, cd)
        if not wv:
            continue
        cands.append({"code": cd, "name": nm, "px": px, "chg": chg, **wv})
    if not cands:
        return "📊 주간 변동성 스캐너 — 후보 없음(데이터 조회 실패/휴장)."
    cands.sort(key=lambda c: c["vol"], reverse=True)
    lines = ["📊 주간 변동성 상위 (래리 윌리엄스式 물색 · 이번주 5일 레인지)",
             "⚠️ 매수신호 아님 — 관찰 후보. 재료·수급·눌림 필터 통과분만 진입.", ""]
    for i, c in enumerate(cands[:top_n], 1):
        ds = _daily_setup(token, key, secret, c["code"], c["px"])
        disp = ds["disp"] if ds else None
        # [V25.2] 재료(키워드) 태그 제거 — 오탐 많음(삼성전기 '악재' 등). 대신 신뢰도 높은 수급 표시.
        #   재료 확인은 --stock / 저녁브리핑 팩트체크(검색 grounding)가 정확.
        _sup = ""
        try:
            _f, _o = _investor_est(token, key, secret, c["code"])
            if _f is not None and _o is not None:
                _net = (_f + _o) * c["px"] / 1e8
                _sup = f" · 수급 {_net:+.0f}억" + ("✅" if _net >= 0 else "⚠️")
        except Exception:
            pass
        if c["ret5"] >= 15 or (disp is not None and disp >= 12):
            _pre = " ⚠️선반영(이미급등·추격주의)"
        elif disp is not None and 0 <= disp <= 4:
            _pre = " 🟢눌림권(진입 여지)"
        else:
            _pre = ""
        lines.append(f"{i}. {c['name']}({c['code']}) {c['px']:,}({c['chg']:+.1f}%)")
        lines.append(f"   변동성 {c['vol']:.0f}% · 주간 {c['ret5']:+.0f}%"
                     + (f" · 20MA이격 {disp:+.0f}%" if disp is not None else "")
                     + f"{_sup}{_pre}")
    lines.append("\n※ 변동성=물색만. 재료는 --stock/저녁브리핑 팩트체크로 확인 · 선반영 회피 · 눌림 타점 필수")
    return "\n".join(lines)


MY_WATCH_FILE = os.path.join(BASE, "my_watch.json")


def _read_my_watch():
    """내 관심종목 — my_watch.json({"on":true,"stocks":[{"code","name"}]}). off/없으면 []."""
    try:
        with open(MY_WATCH_FILE, encoding="utf-8-sig") as f:  # utf-8-sig: 메모장 BOM 허용
            d = json.load(f)
        if isinstance(d, dict) and d.get("on") and isinstance(d.get("stocks"), list):
            return d["stocks"]
    except Exception:
        pass
    return []


def check_my_watch(token, key, secret, now_kst, state, token_tg, chat_id):
    """[V23.8] 내 관심종목 타점 검색기 — my_watch.json 종목을 장중 실시간 감시.
    큰추세(일봉 정배열) 상승 + ①모멘텀(거래량 급증·양전) or ②5일선 눌림반등이면 타점 알림. 종목별 30분 쿨다운.
    감시창: 정규장 09:00~15:30 + NXT 야간 18:00~20:00(넥스트레이드 실시간가)."""
    m = now_kst.hour * 60 + now_kst.minute
    _reg = (9 * 60) <= m <= (15 * 60 + 30)
    _nxt = (18 * 60) <= m <= (20 * 60)
    if not (_reg or _nxt):
        return
    _mrkt = "NX" if _nxt else "J"                        # NXT는 넥스트레이드 실시간가
    stocks = _read_my_watch()
    if not stocks:
        return
    today = now_kst.strftime("%Y%m%d")
    mw = state.get("my_watch_ts", {})
    if mw.get("_day") != today:
        mw = {"_day": today}
    for s in stocks:
        code = str(s.get("code", "")).zfill(6); name = s.get("name", code)
        if not (code.isdigit() and len(code) == 6):
            continue
        px, chg, turn = _price_and_turnover(token, key, secret, code, mrkt=_mrkt)
        if not px:
            continue
        ds = _daily_setup(token, key, secret, code, px)
        if not ds:
            continue
        _ma5, _ma20 = ds.get("ma5"), ds.get("ma20")
        _up = bool(_ma5 and _ma20 and px > _ma5 > _ma20)         # 큰추세 상승(정배열)
        _vr = _vol_ratio_5d(token, key, secret, code)
        _mult = _vr[2] if _vr else 0
        # [V25.4] 🚀 돌파 확인 알림 — 20일 전고 돌파 + 거래량 2배↑(가짜돌파 필터). 별도 쿨다운(60분).
        #   ※ 진짜 돌파 조건: 전고 위 + 거래량 동반. 장중 잠깐 찍는 속임수 걸러내려 배수 게이트.
        _bk = code + "_brk"
        _rhigh = _recent_high(token, key, secret, code, days=20, exclude_today=True)  # 직전 20일 전고(오늘 제외)
        if (_rhigh and px >= _rhigh and _mult >= 2.0
                and (int(now_kst.timestamp()) - int(mw.get(_bk, 0))) >= 60 * 60):
            _bstop = int(_rhigh * 0.98); _bt1 = int(px * 1.05)
            _sess_b = "NXT 야간 실시간" if _nxt else "정규장"
            if send_telegram(token_tg, chat_id,
                             f"{SIG_BUY}\n🚀 [돌파 확인·{_sess_b}] {name} — 20일 전고 돌파!\n"
                             f"현재 {px:,}({(chg or 0):+.1f}%) · 전고 {_rhigh:,} 상향 · 거래량 {_mult:.1f}배 동반\n"
                             f"진입 {px:,} · 손절 {_bstop:,}(전고 아래 −2%) · 익절 {_bt1:,}(+5%)\n"
                             f"⚠️ 종가로 돌파 굳는지 확인 · 눌림 없이 급하면 소액 · 거래량 빠지면 속임수 주의"):
                mw[_bk] = int(now_kst.timestamp())
                print(f"[돌파확인] {name} {px:,} 전고 {_rhigh:,} 돌파(거래량 {_mult:.1f}배)")
        if (int(now_kst.timestamp()) - int(mw.get(code, 0))) < 120 * 60:  # [V25.15] 쿨다운 30→120분(같은 종목 반복 발송 방지)
            continue
        if not _up:
            continue                                              # 큰추세 하락/횡보 = 타점 아님(역추세 회피)
        # [V25.15] 눌림도 '반등 확인'(저가 5일선 터치 후 현재가 회복)일 때만 — 5일선 근처 하루종일 맴돌 때
        #   반복 발송(SK하이닉스 등)하던 문제 해결. 시장 눌림 스캐너와 동일 기준.
        _pf = _price_full(token, key, secret, code)
        _low = _pf[4] if _pf else None
        _sig = None
        if (chg or 0) > 0 and _mult >= 1.5:
            _sig = ("🎯 모멘텀 타점", f"큰추세 상승 + 오늘 {(chg or 0):+.1f}% + 거래량 {_mult:.1f}배 급증 → 상승 초입")
        elif (_ma5 and _low and _low <= _ma5 * 1.005 and px >= _ma5 * 0.998
                and px > _low * 1.002 and (chg or 0) >= -1.0):
            _sig = ("🎯 눌림 반등", f"큰추세 상승 · 저가 {int(_low):,}(5일선 터치) → 현재 5일선 회복 · 반등 확인")
        if _sig:
            _stop = int(px * 0.98); _t1 = int(px * 1.03)
            _sess = "NXT 야간 실시간" if _nxt else "정규장"
            if send_telegram(token_tg, chat_id,
                             f"{SIG_BUY}\n👁️ [내 관심종목 타점·{_sess}] {name} — {_sig[0]}\n"
                             f"{_sig[1]}\n현재 {px:,}({(chg or 0):+.1f}%) · 거래대금 {(turn or 0)/1e8:,.0f}억\n"
                             f"진입 {px:,} · 손절 {_stop:,}(−2%) · 익절 {_t1:,}(+3%)\n"
                             f"※ 니가 지정한 관심종목 타점 · 개장 후 수급 확인 · -2% 손절"):
                mw[code] = int(now_kst.timestamp())
                print(f"[내관심타점] {name} — {_sig[0]}")
    state["my_watch_ts"] = mw


HOLDINGS_FILE = os.path.join(BASE, "my_holdings.json")


def _read_holdings():
    """보유종목 — my_holdings.json({"on":true,"stocks":[{code,name,avg,qty,stop?,target?}]}). off/없으면 []."""
    try:
        with open(HOLDINGS_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("on") and isinstance(d.get("stocks"), list):
            return d["stocks"]
    except Exception:
        pass
    return []


def _holding_judge(token, key, secret, code, px, prev_close):
    """[V25.21] 보유종목 오버나이트 홀딩 판정 — 4요인(재료질·수급·선반영·밤사이등락)으로
    '9시까지 보유 vs 지금 매도'. 반환: 텔레그램 1줄 판정 텍스트."""
    _ng, _nbad = _news_grade(code)
    _strong = _ng in ("S", "A")
    _sup_pos = None
    try:
        _f, _o = _investor_est(token, key, secret, code)
        if _f is not None and _o is not None:
            _sup_pos = (_f + _o) >= 0
    except Exception:
        pass
    _g = ((px / prev_close - 1) * 100) if prev_close else 0
    _reflected = _g >= 3.0
    _why = []; _hold = True
    if _reflected:
        _hold = False; _why.append(f"밤사이 +{_g:.1f}%(선반영)")
    if not _strong:
        _hold = False; _why.append("재료 약함(A급 아님)")
    if _sup_pos is False:
        _hold = False; _why.append("수급 이탈")
    _verdict = ("🟢 9시까지 보유 (A급재료+수급+선반영無 → 9시 갭·장중 여력)" if _hold
                else "🔴 지금(8시 NXT) 매도 검토 (" + "·".join(_why) + ")")
    _supmark = "✅유입" if _sup_pos else ("⚠️이탈" if _sup_pos is False else "미확인")
    return f"재료:{_ng or '없음'} · 수급:{_supmark} · 밤사이:{_g:+.1f}%(선반영 {'예' if _reflected else '아니오'})\n→ {_verdict}"


def check_holdings(token, key, secret, now_kst, state, token_tg, chat_id):
    """[V25.20] 보유종목 관리 알림 — my_holdings.json의 매수평균 대비 손절/익절 감시.
    수익률 ≤ 손절%(기본-2) → 🔴손절 / ≥ 익절%(기본+3) → 🟢익절 / 손절 근접(-1.5%) → ⚠️경고.
    감시창: 정규장 09:00~15:30 + NXT 프리(08:00~08:50)·애프터(16:00~20:00). 종목·상태별 60분 쿨다운."""
    m = now_kst.hour * 60 + now_kst.minute
    _reg = (9 * 60) <= m <= (15 * 60 + 30)
    _pre = (8 * 60) <= m <= (8 * 60 + 50)
    _aft = (16 * 60) <= m <= (20 * 60)
    if not (_reg or _pre or _aft):
        return
    _mrkt = "J" if _reg else "NX"
    hold = _read_holdings()
    if not hold:
        return
    today = now_kst.strftime("%Y%m%d")
    hs = state.get("holdings_sent", {})
    if hs.get("_day") != today:
        hs = {"_day": today}
    for s in hold:
        code = str(s.get("code", "")).zfill(6); name = s.get("name", code)
        avg = s.get("avg") or 0
        if not (code.isdigit() and len(code) == 6) or not avg:
            continue
        qty = s.get("qty") or 0
        _stop = float(s.get("stop", -2.0)); _target = float(s.get("target", 3.0))
        try:
            px, chg, _ = _price_and_turnover(token, key, secret, code, mrkt=_mrkt)
        except Exception:
            px = None
        if not px:
            continue
        _ret = (px / avg - 1) * 100
        _pl = int((px - avg) * qty) if qty else 0
        # [V25.21] 8시 프리마켓 오버나이트 홀딩 판정 — 보유 전체에 '9시 보유 vs 8시 매도'(당일 1회/종목)
        if _pre:
            _jk = code + "_judge"
            if hs.get(_jk) != today:
                try:
                    _cl = _daily_closes(token, key, secret, code)
                    _prevc = _cl[sorted(_cl)[-1]] if _cl else avg
                    _jtxt = _holding_judge(token, key, secret, code, px, _prevc)
                    send_telegram(token_tg, chat_id,
                                  f"{SIG_WATCH}\n🌅 보유 홀딩 판정(8시) — {name}\n"
                                  f"현재 {px:,}({(chg or 0):+.1f}%) · 평단 {avg:,} · 수익률 {_ret:+.1f}%\n{_jtxt}\n"
                                  f"※ 8시 NXT 하락은 얇아 가짜일 수 있음 — 애매하면 9시 첫10분 저점 확인")
                    hs[_jk] = today
                    print(f"[보유판정] {name} 8시 홀딩판정")
                except Exception as _je:
                    print("보유판정 오류:", _je)
        _kind = None
        if _ret <= _stop:
            _kind = ("🔴 손절선 이탈", f"손절 기준({_stop:+.0f}%) 이탈 — 규칙대로 정리 검토(존버 금지)")
        elif _ret >= _target:
            _kind = ("🟢 익절 도달", f"익절 목표({_target:+.0f}%) 도달 — 분할 익절·이익 확보 검토")
        elif _ret <= _stop + 0.5:
            _kind = ("⚠️ 손절 근접", f"손절선({_stop:+.0f}%) 0.5%p 이내 — 이탈 시 정리 준비")
        if not _kind:
            continue
        _ck = f"{code}_{_kind[0][:2]}"                     # 종목+상태별 쿨다운(중복 방지)
        if (int(now_kst.timestamp()) - int(hs.get(_ck, 0))) < 4 * 60 * 60:   # [V25.30] 60분→4시간(손절 매시간 반복 스팸 해결·하루 2회)
            continue
        _sess = "정규장" if _reg else "NXT 프리(8시)" if _pre else "NXT 애프터"
        if send_telegram(token_tg, chat_id,
                         f"{SIG_CAUTION}\n💼 [보유관리·{_sess}] {name} — {_kind[0]}\n"
                         f"현재 {px:,}({(chg or 0):+.1f}%) · 평단 {avg:,} · 수익률 {_ret:+.1f}%"
                         + (f" · 평가손익 {_pl:+,}원" if qty else "") + "\n"
                         f"{_kind[1]}"):
            hs[_ck] = int(now_kst.timestamp())
            print(f"[보유관리] {name} {_ret:+.1f}% — {_kind[0]}")
    state["holdings_sent"] = hs


def _holdings_report(token, key, secret, now_kst, token_tg, chat_id):
    """[V25.21] 보유종목 수동 조회 — 전 보유종목 수익률·평가손익 + 오버나이트 홀딩 판정. --holdings."""
    hold = _read_holdings()
    if not hold:
        send_telegram(token_tg, chat_id, "💼 보유종목 없음 — my_holdings.json 확인(on:true·stocks 등록).")
        return
    m = now_kst.hour * 60 + now_kst.minute
    _mrkt = "J" if ((9 * 60) <= m <= (15 * 60 + 30)) else "NX"
    _lines = ["💼 보유종목 현황·판정"]
    _tot = 0
    for s in hold:
        code = str(s.get("code", "")).zfill(6); name = s.get("name", code); avg = s.get("avg") or 0
        qty = s.get("qty") or 0
        if not avg:
            continue
        try:
            px, chg, _ = _price_and_turnover(token, key, secret, code, mrkt=_mrkt)
        except Exception:
            px = None
        if not px:
            _lines.append(f"\n■ {name} — 시세 조회 실패")
            continue
        _ret = (px / avg - 1) * 100; _pl = int((px - avg) * qty) if qty else 0
        _tot += _pl
        try:
            _cl = _daily_closes(token, key, secret, code)
            _prevc = _cl[sorted(_cl)[-1]] if _cl else avg
            _j = _holding_judge(token, key, secret, code, px, _prevc)
        except Exception:
            _j = "판정 조회 실패"
        _lines.append(f"\n■ {name} {px:,}({(chg or 0):+.1f}%) · 평단 {avg:,} · {_ret:+.1f}%"
                      + (f" · {_pl:+,}원" if qty else "") + f"\n  {_j}")
    _lines.append(f"\n💰 총 평가손익 {_tot:+,}원")
    send_telegram(token_tg, chat_id, "\n".join(_lines))
    print(f"[보유조회] {len(hold)}종 · 총손익 {_tot:+,}")


def _box_range(token, key, secret, code, days=20):
    """[V25.29] 박스권 범위 — 최근 N일 고가최고(상단)·저가최저(하단)·ma5·ma20. 실패 시 None."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010400"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code,
                                 "fid_period_div_code": "D", "fid_org_adj_prc": "1"}, timeout=6)
        rows = [x for x in (r.json().get("output", []) or []) if isinstance(x, dict)][:days]
        if len(rows) < 15:
            return None
        highs = [_to_int(x.get("stck_hgpr")) for x in rows]
        lows = [_to_int(x.get("stck_lwpr")) for x in rows]
        clos = [_to_int(x.get("stck_clpr")) for x in rows]
        if not (all(highs) and all(lows) and all(clos)):
            return None
        return {"top": max(highs), "bottom": min(lows),
                "ma5": sum(clos[:5]) / 5.0, "ma20": sum(clos) / len(clos)}
    except Exception:
        return None


def check_range_trade(token, key, secret, now_kst, state, token_tg, chat_id, sev=1, force=False):
    """[V25.29] 레인지(박스) 매매 — 박스장 전용 무기. 횡보 종목이 박스 하단 지지에서 반등하면
    '하단 매수→상단 목표' 알림. 추세장 종목은 배제(ma5≈ma20 횡보만). 09:05~15:20, 리스크오프 억제, 종목별 당일1회.
    force=True: 시간창·쿨다운·당일락 무시(수동 테스트, --range)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not force and (not ((9 * 60 + 5) <= m <= (15 * 60 + 20)) or sev == 2):
        return
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("range_sent", {})
    if force:
        sent = {"_day": today}                        # 강제: 당일락 무시하고 새로 스캔
    elif sent.get("_day") != today:
        sent = {"_day": today}
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn or any(k in str(nm) for k in _EARLY_ETF_KW) or sent.get(cd):
            continue
        if not (-2.5 <= (chg or 0) <= 3.0):          # 급락(박스깨짐)·급등(상단탈출) 사전 컷
            continue
        _budget += 1
        if _budget > 24:
            break
        br = _box_range(token, key, secret, cd)
        if not br:
            continue
        _top, _bot, _ma5, _ma20 = br["top"], br["bottom"], br["ma5"], br["ma20"]
        if not (_bot and _top and _ma20):
            continue
        _width = (_top / _bot - 1) * 100
        if not (8.0 <= _width <= 35.0):              # 박스다운 폭(너무 좁으면 무의미·너무 넓으면 추세)
            continue
        if abs(_ma5 / _ma20 - 1) * 100 > 3.5:        # 횡보 확인 — 추세장(정/역배열 강함) 배제
            continue
        _d_bot = (px / _bot - 1) * 100               # 박스 하단 이격
        if not (0 <= _d_bot <= 5.0):                 # 하단 5% 이내(지지 근처)만
            continue
        if px <= _bot * 0.98:                        # 하단 이탈(박스 붕괴) → 매수 아님
            continue
        _pf = _price_full(token, key, secret, cd)
        _low = _pf[4] if _pf else None
        if not _low or _low > _bot * 1.03 or px <= _low * 1.002:   # 저가 하단 터치 + 현재가 반등 확인
            continue
        _exp = (_top / px - 1) * 100                 # 상단까지 여력
        if _exp < 3.0:                               # 먹을 여력 3%+ 있어야 의미
            continue
        _ng, _nbad = _news_grade(cd)
        if _nbad:
            continue
        _stop = max(int(px * 0.965), int(_bot * 0.97)); _t1 = int(_top)    # [V25.30] 손절 -3.5% 상한(하단 멀면)·목표=박스 상단
        if send_telegram(token_tg, chat_id,
                         f"{SIG_BUY}\n📦 [레인지 매매·박스하단] {nm} {px:,}({(chg or 0):+.1f}%)\n"
                         f"박스 {int(_bot):,}~{int(_top):,}({_width:.0f}%) · 하단 지지 반등(하단+{_d_bot:.1f}%) · 상단여력 +{_exp:.0f}%\n"
                         f"진입 {px:,} · 손절 {_stop:,}(하단 이탈) · 목표 {_t1:,}(박스 상단)\n"
                         f"※ 박스장 무기 — 상단서 익절·하단 깨지면 손절 · 횡보 종목 전용"):
            sent[cd] = True
            _log_signal(state, now_kst, "레인지매매", nm, cd, px)
            print(f"[레인지매매] {nm} {px:,} 박스 {int(_bot):,}~{int(_top):,}")
    state["range_sent"] = sent


def check_pullback_scan(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.7] 눌림 타점 스캐너(시장 전역) — 저갭 장세의 주력 무기. 거래대금 상위 중
    정배열(큰추세 상승·ma5>ma20) 종목이 지지선(5일선/20일선)까지 눌렸다가 지지·반등하면 타점 알림.
    ★하락추세 종목은 제외(떨어지는 칼 방지) — 정배열만.★ 09:05~15:20, 리스크오프 억제. 종목별 40분 쿨다운."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60 + 5) <= m <= (15 * 60 + 20)) or sev == 2:
        return
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("pullback_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    _onote, _ = _overnight_note(_us_fut_pct(state, now_kst))   # [V25.33] 미국선물 오버나이트 게이트(안내)
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if sent.get(cd):                              # [V25.30] 같은 종목 하루 1회(40분 반복 스팸 해결)
            continue
        if not (-2.0 <= (chg or 0) <= 4.0):          # 급락(칼)·급등(눌림 아님) 사전 컷 → 일봉조회 절약
            continue
        _budget += 1
        if _budget > 24:
            break
        ds = _daily_setup(token, key, secret, cd, px)
        if not ds:
            continue
        _ma5, _ma20 = ds.get("ma5"), ds.get("ma20")
        if not (_ma5 and _ma20 and _ma5 > _ma20):    # 정배열(큰추세 상승) 필수 — 하락추세 눌림 금지
            continue
        _disp = ds.get("disp", 0)
        if _disp >= 10.0:                            # 과열은 '눌림' 아님
            continue
        _d5 = (px / _ma5 - 1) * 100                  # 5일선 이격
        _d20 = (px / _ma20 - 1) * 100                # 20일선 이격
        # [V25.10] 반등 확인 — '지지선 근처'만으로 발송하면 떨어지는 칼을 잡음(감사 지적).
        #   진짜 눌림 = 오늘 저가가 지지선(MA) 근처까지 눌렸다가 현재가가 그 위로 회복(반등)한 것.
        _pf = _price_full(token, key, secret, cd)     # (현재가,등락,시가,고가,저가)
        _low = _pf[4] if _pf else None
        if not _low or (chg or 0) < -1.5:             # 저가 미확보 or 오늘 크게 밀리는 중 → 반등 아님
            continue
        _sig = None
        if (_low <= _ma5 * 1.005 and px >= _ma5 * 0.998   # 저가 5일선 터치 + 현재가 5일선 회복(반등)
                and px > _low * 1.002 and _d5 <= 3.0):
            _sig = ("5일선 눌림반등", f"큰추세 상승 · 저가 {int(_low):,}(5일선 터치) → 현재 5일선 회복 · 반등 확인")
        elif (px < _ma5 and _low <= _ma20 * 1.01 and px >= _ma20 * 0.998   # 5일선 아래 조정 후 20일선 반등
                and px > _low * 1.002):
            _sig = ("20일선 눌림반등", f"큰추세 상승 · 저가 {int(_low):,}(20일선 터치) → 현재 20일선 회복 · 반등 확인")
        if not _sig:
            continue
        _ng, _nbad = _news_grade(cd)                 # 악재 제외
        if _nbad:
            continue
        # ── [V25.32] 수급 게이트(강의 핵심) ──────────────────────────────
        #   수급단타왕: "외국인·기관 수급주 위주로만 눌림. 무수급 재료·테마주 눌림은 떨어지는 칼."
        #   당일 외인+기관 추정 순매수 유입 OR 일별 연속매수 中 하나는 반드시 있어야 발송.
        #   (수급 fetch 1회로 게이트·연속성·평단·정예태그 전부 처리 — _elite_tag 중복호출 제거)
        _fe, _oe = _investor_est(token, key, secret, cd)          # 당일 추정 순매수(수량)
        _idaily = _investor_daily(token, key, secret, cd, days=5)  # 일별 순매수(연속성·평단)
        _stag, _strong = _supply_daily_tag_from(_idaily)
        _today_in = ((_fe or 0) + (_oe or 0)) > 0
        if not (_today_in or _stag):                 # 당일 유입도, 일별 연속도 없음 → 무수급 → 눌림 금지
            continue
        # ── [V25.32] 주포 매도→매수 전환 확인 ────────────────────────────
        #   강의: 프로그램(주포)이 매도 소진 후 매수 전환하는 자리가 눌림 급소. 당일 프로그램 순매수(+)면 강신호.
        _prog = _program_net(token, key, secret, cd)              # 당일 프로그램 순매수 금액(원)
        _prog_tag = ""
        if _prog and _prog > 0:
            _prog_tag = f" · 🟩프로그램 매수전환 +{_prog/1e8:,.0f}억"
        # ── [V25.32] 외인/기관 평단 아래 = 추가매수 급소 ──────────────────
        _avg = _supply_avgprice(_idaily)
        _avg_tag = ""
        if _avg and px <= _avg * 1.005:              # 현재가가 수급 평단 이하(±0.5%) → 강의 '평단 아래 매수' 자리
            _avg_tag = f"\n💡 외인/기관 평단(~{_avg:,}) 이하 — 세력 원가 구간(강의 '평단 아래 매수')"
        _pull = _pullback_levels(token, key, secret, cd, px, chg, ds) or ""
        # [V25.30] 손절폭 -3% 상한 — '20일선 아래'가 멀면 -9%까지 가던 문제(삼성SDI 등). 20일선/−3% 中 높은쪽.
        _stop = max(int(px * 0.97), int(_ma20 * 0.98))
        _stoppct = (_stop / px - 1) * 100
        _t1 = int(px * 1.03)
        _mat = "🔥재료S" if _ng == "S" else "🟢재료A" if _ng == "A" else ""
        # 정예 태그 인라인(이미 가져온 수급으로 판정 — _elite_tag 재조회 안 함)
        _elite = ""
        if _ng in ("S", "A") and _today_in:
            _elite = f" ⭐⭐정예(수급강){_stag}" if _strong else f" ⭐정예{_stag}"
        elif _stag:                                  # 재료는 약해도 수급 연속이면 표시
            _elite = _stag
        if send_telegram(token_tg, chat_id,
                         f"{SIG_BUY}\n🎯 [눌림 타점·정배열]{_elite} {nm} — {_sig[0]}\n"
                         f"{_sig[1]}\n현재 {px:,}({(chg or 0):+.1f}%) · 거래대금 {(turn or 0)/1e8:,.0f}억"
                         + (f" · {_mat}" if _mat else "") + _prog_tag + _avg_tag + "\n"
                         f"진입 {px:,} · 손절 {_stop:,}({_stoppct:+.1f}%·지지이탈시 전량) · 익절 {_t1:,}(+3%){_pull}"
                         + _onote + "\n"
                         f"※ 수급주 눌림 · 10분할로 나눠 담고 다음날 갭하락 대비 총알 일부 남길 것"):
            sent[cd] = int(now_kst.timestamp())
            _log_signal(state, now_kst, "눌림타점", nm, cd, px)
            print(f"[눌림타점] {nm} {px:,} — {_sig[0]}")
    state["pullback_sent"] = sent


def check_oversold_bounce(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.32] 과매도 낙주 반등(강의 ⑧, 조건부) — 지수 급락일 전용 별도 경로.
    수급단타왕: "지수/미국 급락일에 대형주가 악재 아닌 이유로 -5~-9% 낙폭과대 → 수급 받쳐주고 반등하면 최고 기회
    (8/5 서킷·모건스탠리 리포트式)." 평상시 눌림스캐너는 -2% 밑을 '떨어지는 칼'로 컷하므로 이 경로만 예외 허용.
    엄격 게이트: ①지수 급락일 ②시총 대형(≥1조) ③낙폭과대(-4%↓) ④저가대비 반등확인 ⑤당일 수급유입 ⑥악재無.
    09:05~14:30, 종목별 하루 1회. 손절 오늘 저가 이탈시 전량(타이트)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60 + 5) <= m <= (14 * 60 + 30)):
        return
    # ① 지수 급락일 게이트 — 코스피 당일 등락률 -1.3% 이하일 때만 낙주 경로 활성(과매도 국면)
    _kospi = _kospi_index_kis(token, key, secret)
    if _kospi is None or _kospi > -1.3:
        return
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("oversold_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    _onote, _ = _overnight_note(_us_fut_pct(state, now_kst))   # [V25.33] 미국선물 게이트 — 급락일 홀딩 위험판정
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if sent.get(cd):
            continue
        # ③ 낙폭과대 — 오늘 -4%↓(과열/약보합 배제). -15%보다 더 빠지는 건 진짜 악재 가능 → 하한 -13%.
        if not (-13.0 <= (chg or 0) <= -4.0):
            continue
        _budget += 1
        if _budget > 20:
            break
        # ② 시총 대형(≥1조=10000억) — 호가 탄탄한 대장주/주도주만(강의: 초대형주는 1억 사도 티도 안 남)
        _cap = _market_cap(token, key, secret, cd)
        if not _cap or _cap < 10000:
            continue
        # ④ 반등 확인 — 오늘 저가 대비 현재가 +1%↑ 회복(바닥에서 올라오는 중). 저가=현재면 아직 칼.
        _pf = _price_full(token, key, secret, cd)
        _low = _pf[4] if _pf else None
        if not _low or px < _low * 1.01:
            continue
        # ⑥ 악재 컷 — 진짜 악재로 빠진 거면 배제(강의: 악재가 선반영/실질무영향일 때만). 등급 악재면 스킵.
        _ng, _nbad = _news_grade(cd)
        if _nbad:
            continue
        # ⑤ 수급 유입 필수 — 급락 속에서도 외인/기관이 받는 종목만(8/5식 양매수). 이게 승부 필터.
        _fe, _oe = _investor_est(token, key, secret, cd)
        if ((_fe or 0) + (_oe or 0)) <= 0:
            continue
        _prog = _program_net(token, key, secret, cd)          # 프로그램 받침(보너스)
        _prog_tag = f" · 🟩프로그램 +{_prog/1e8:,.0f}억" if (_prog and _prog > 0) else ""
        _who = "외인+기관" if (_fe or 0) > 0 and (_oe or 0) > 0 else ("외인" if (_fe or 0) > 0 else "기관")
        # 손절 = 오늘 저가 -1.5%(지지 이탈시 전량, 강의 만주式 타이트). 익절 +2.5%(반등 2%만 먹기).
        _stop = int(_low * 0.985)
        _stoppct = (_stop / px - 1) * 100
        _t1 = int(px * 1.025)
        if send_telegram(token_tg, chat_id,
                         f"{SIG_BUY}\n🩸 [과매도 낙주·반등] {nm} — 지수급락({_kospi:+.1f}%)일 낙폭과대\n"
                         f"현재 {px:,}({(chg or 0):+.1f}%) · 저가 {int(_low):,} 대비 반등 · 시총 {_cap/10000:,.1f}조\n"
                         f"💧 급락 속 {_who} 수급 유입{_prog_tag}\n"
                         f"진입 {px:,} · 손절 {_stop:,}({_stoppct:+.1f}%·저가이탈시 전량) · 익절 {_t1:,}(+2.5%)"
                         + _onote + "\n"
                         f"※ 악재性 급락 아닌지 반드시 확인 · 시가/저가 분할 · 미국선물 급락 지속시 당일 청산"):
            sent[cd] = True
            _log_signal(state, now_kst, "과매도낙주", nm, cd, px)
            print(f"[과매도낙주] {nm} {px:,} ({chg:+.1f}%) 지수{_kospi:+.1f}%")
    state["oversold_sent"] = sent


def check_material_washout(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.33] 강한 재료주 일시 투매 반등(강의 유형4) — 당일청산 전용(오버나이트 금지).
    수급단타왕/만주: 강한 재료(계약·실적)로 장중 크게 오른 주도주가 일시 투매로 눌렸지만
    프로그램/수급·호가가 지지하면 짧은 되돌림(1~2%)만 먹고 빠르게 청산. 종배와 달리 밤 안 넘김.
    09:10~14:40, 종목별 하루 1회, 리스크오프(sev2) 억제. 게이트: 강한재료(S/A)+당일고점 +5%↑+
    고점대비 2~7% 눌림+저가대비 반등+프로그램/수급 지지+거래대금 큼."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60 + 10) <= m <= (14 * 60 + 40)) or sev == 2:
        return
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("washout_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if sent.get(cd):
            continue
        if not (2.0 <= (chg or 0) <= 9.0):           # 아직 강세 유지 중(눌렸어도 +) · 이미 죽은 종목 제외
            continue
        if turn < 5_000_000_000:                     # 거래대금 50억+ (주도주·유동성)
            continue
        _budget += 1
        if _budget > 20:
            break
        _ng, _nbad = _news_grade(cd)                 # 강한 재료 필수 — 재료 없는 급등락은 제외(강의: 재료 살아있을 때만)
        if _nbad or _ng not in ("S", "A"):
            continue
        _pf = _price_full(token, key, secret, cd)     # (현재가,등락,시가,고가,저가)
        if not _pf:
            continue
        _high, _low = _pf[3], _pf[4]
        _prev = px / (1 + (chg or 0) / 100) if chg else None
        if not (_prev and _high and _low):
            continue
        _high_pct = (_high / _prev - 1) * 100          # 당일 고점 등락률
        if _high_pct < 5.0:                            # 오늘 +5%↑ 강하게 올랐어야(강한 재료주)
            continue
        _pull = (_high - px) / _high * 100             # 고점 대비 현재 되돌림%
        if not (2.0 <= _pull <= 7.0):                  # 2~7% 눌림 = '일시 투매'(너무 얕으면 그냥 강세, 깊으면 붕괴)
            continue
        if px < _low * 1.005:                          # 저가 대비 반등 확인(아직 흘러내리면 제외)
            continue
        _prog = _program_net(token, key, secret, cd)   # 프로그램 지지
        _fe, _oe = _investor_est(token, key, secret, cd)
        _prog_ok = bool(_prog and _prog > 0)
        if not (_prog_ok or ((_fe or 0) + (_oe or 0) > 0)):   # 프로그램 or 당일 수급 지지 필수
            continue
        _mat = "🔥재료S" if _ng == "S" else "🟢재료A"
        _prog_tag = f" · 🟩프로그램 +{_prog/1e8:,.0f}억" if _prog_ok else ""
        _stop = int(_low * 0.99)                        # 저가 이탈시(타이트)
        _stoppct = (_stop / px - 1) * 100
        _t1 = int(px * 1.02)                            # +2% 짧게
        if send_telegram(token_tg, chat_id,
                         f"{SIG_BUY}\n⚡ [재료주 투매반등·당일청산] {nm} — 고점 {int(_high):,}(+{_high_pct:.0f}%) 대비 −{_pull:.1f}% 눌림\n"
                         f"현재 {px:,}({(chg or 0):+.1f}%) · {_mat}{_prog_tag} · 거래대금 {(turn or 0)/1e8:,.0f}억\n"
                         f"진입 {px:,} · 손절 {_stop:,}({_stoppct:+.1f}%·저가이탈) · 익절 {_t1:,}(+2%·짧게)\n"
                         f"⚠️ 오버나이트 금지! 당일 되돌림 1~2%만 먹고 청산 · 재료 소멸/투매 재개시 즉시 정리"):
            sent[cd] = True
            _log_signal(state, now_kst, "재료투매반등", nm, cd, px)
            print(f"[재료투매반등] {nm} {px:,} 고점대비 −{_pull:.1f}%")
    state["washout_sent"] = sent


def check_afterhours(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.35] 시간외 단일가(강의 3강) — 17:00~18:30, 후반(17:30+)까지 '실제 체결'이 이어지는 대장주 알림.
    강의 핵심: 시간외 등락률(허수)이 아니라 10분당 실제 체결대금이 지속·증가하는지가 관건.
    → 누적거래대금(acml_tr_pbmn)은 항상 증가하므로, 시간간격 델타(≈10분)로 실체결 지속을 판정.
    게이트: 정규장 거래대금 상위 + 강한재료(S/A) or 브리핑테마 + NXT 등락 강세(+1.5%↑) + 10분 델타 3억↑.
    등급 A(재료S/A)·B(브리핑). 종목당 하루 1회. 리스크오프(sev2) 억제. 5분 스로틀로 API 절약."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((17 * 60) <= m <= (18 * 60 + 30)) or sev == 2:
        return
    _nowts = int(now_kst.timestamp())
    _last = state.get("afterhours_scan_ts", 0)
    if _nowts - _last < 300:                      # 5분 스로틀(스냅샷 간격 확보 + API 절약)
        return
    state["afterhours_scan_ts"] = _nowts
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("afterhours_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    snap = state.get("afterhours_snap", {})
    if snap.get("_day") != today:
        snap = {"_day": today}
    _late = m >= (17 * 60 + 30)                   # 후반(17:30+) — 강의가 선호하는 구간
    _brief = _recent_brief_codes(now_kst)
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, turn0 = s["code"], s["name"], s["turnover"]
        if not turn0 or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if turn0 < 30_000_000_000:                # 정규장 거래대금 300억 미달 = 유동성 부족 컷
            continue
        _budget += 1
        if _budget > 20:
            break
        npx, nchg, nturn = _price_and_turnover(token, key, secret, cd, mrkt="NX")   # NXT 실시간
        if not npx or nchg is None:
            continue
        # ── 실체결 지속 판정: 8분↑ 간격 델타(≈10분당 체결) ──
        _pv = snap.get(cd)
        _delta = None
        if isinstance(_pv, dict) and (_nowts - _pv.get("t", 0)) >= 480:
            _delta = (nturn or 0) - _pv.get("v", 0)
            snap[cd] = {"t": _nowts, "v": nturn or 0}     # 앵커 갱신(최근 10분 델타 유지)
        elif not isinstance(_pv, dict):
            snap[cd] = {"t": _nowts, "v": nturn or 0}     # 첫 관측 — 저장만
        if sent.get(cd):
            continue
        if (nchg or 0) < 1.5:                     # 시간외 강세 아님
            continue
        if _delta is None:                        # 아직 델타 측정 전(다음 스캔서 판정)
            continue
        if _delta < 300_000_000:                  # 10분당 실체결 3억 미달 = 꺼지는 중(허수/일회성)
            continue
        _ng, _nbad = _news_grade(cd)
        if _nbad:
            continue
        _isbrief = cd in _brief
        if not (_ng in ("S", "A") or _isbrief):   # 재료·테마 없는 시간외 급등 배제(강의: 뉴스 없는 급등 위험)
            continue
        _grade = "A" if _ng in ("S", "A") else "B"    # A=강한재료 / B=브리핑테마성
        _mat = "🔥재료S" if _ng == "S" else "🟢재료A" if _ng == "A" else "🎯브리핑테마"
        _stop = int(npx * 0.98)
        _t1 = int(npx * 1.03)
        _late_tag = " · 후반매수(17:30+·강의선호)" if _late else " · 초반(허수 주의)"
        if send_telegram(token_tg, chat_id,
                         f"{SIG_BUY}\n🌆 [시간외 단일가·{_grade}등급] {nm} — 실체결 지속(10분 {_delta/1e8:,.1f}억)\n"
                         f"NXT {npx:,}({(nchg or 0):+.1f}%) · 정규장 거래대금 {(turn0 or 0)/1e8:,.0f}억 · {_mat}{_late_tag}\n"
                         f"진입 {npx:,} · 손절 {_stop:,}(−2%) · 익절 {_t1:,}(+3%)\n"
                         f"🚫 익일 무효화: 시초가 이탈 · 재료 무효화 → 즉시 정리\n"
                         f"※ 예상체결(허수) 말고 마지막 실체결 확인 · 분할 · 갭하락 전제 소액 · NXT +3%↑면 갭엣지↓(일부 확정)"):
            sent[cd] = True
            _log_signal(state, now_kst, "시간외단일가", nm, cd, npx)
            print(f"[시간외단일가] {nm} NXT {npx:,}({(nchg or 0):+.1f}%) 10분델타 {_delta/1e8:,.1f}억")
    state["afterhours_sent"] = sent
    state["afterhours_snap"] = snap


def check_opening_bet(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.36] 시가배팅(강의 4강) — 09:00~09:10, 장초반 5~10분 변동성 단기 공략(당일).
    강의: 갭 추격이 아니라 '밤사이 신규재료 + 시간외 강도 + 시초 프로그램 수급'이 재료를 확인해줄 때만 진입.
    4유형 통합: ①장전 신규뉴스(재료S/A 갭상) ②해외발(미선물 강세 동조) ③시간외 강세 연장 ④갭하락 과매도(대형주+수급).
    시초가 이탈 -1.5% 손절·물타기 금지·첫 슈팅 분할익절. 종목당 하루 1회, 리스크오프(sev2)는 갭하락 과매도만 허용."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (9 * 60 + 10)):
        return
    _nowts = int(now_kst.timestamp())                # [V25.39] 90초 스로틀(API 부하·반복스캔 절감)
    if _nowts - state.get("openbet_scan_ts", 0) < 90:
        return
    state["openbet_scan_ts"] = _nowts
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("openbet_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    _nq = _us_fut_pct(state, now_kst)                 # 미국 나스닥선물%(해외발 동조 판정)
    _brief = _recent_brief_codes(now_kst)
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if sent.get(cd) or turn < 3_000_000_000:      # 거래대금 30억 미달(초반이라 낮춤) 컷
            continue
        _is_gapdown = (-13.0 <= (chg or 0) <= -4.0)   # 유형④ 갭하락 과매도
        _is_gapup = (1.0 <= (chg or 0) <= 8.0)        # ①②③ 갭상(과열 추격 배제: +8%↑ 제외)
        if not (_is_gapup or _is_gapdown):
            continue
        if sev == 2 and not _is_gapdown:              # 리스크오프 땐 갭하락 과매도만
            continue
        _budget += 1
        if _budget > 18:
            break
        _pf = _price_full(token, key, secret, cd)      # (현재가,등락,시가,고가,저가)
        if not _pf:
            continue
        _open, _low = _pf[2], _pf[4]
        if not _open:
            continue
        _ng, _nbad = _news_grade(cd)
        if _nbad:
            continue
        _prog = _program_net(token, key, secret, cd)   # 시초 프로그램 순매수(매수전환 확인)
        _prog_ok = bool(_prog and _prog > 0)
        _isbrief = cd in _brief
        _fe, _oe = _investor_est(token, key, secret, cd)
        _supply_ok = ((_fe or 0) + (_oe or 0)) > 0
        if _is_gapdown:
            # 유형④ — 대형주 낙폭과대 + 수급/프로그램 반등 + 저가대비 회복
            _cap = _market_cap(token, key, secret, cd)
            if not _cap or _cap < 10000:               # 시총 1조↑ 대형주만
                continue
            if not (_supply_ok or _prog_ok):           # 급락 속 수급/프로그램 받침 필수
                continue
            if _low and px < _low * 1.005:             # 저가 대비 반등 확인
                continue
            _type = "갭하락 과매도"
            _stop = int((_low or px) * 0.985); _t1 = int(px * 1.025)
        else:
            # ①②③ 갭상 — 재료S/A or 브리핑 or (미선물 강세+프로그램) 中 하나 필수(무근거 갭 추격 배제)
            _overseas = (_nq is not None and _nq >= 0.5)
            if not (_ng in ("S", "A") or _isbrief or (_overseas and _prog_ok)):
                continue
            if px < _open * 0.99:                       # 시초가 이미 이탈 중이면 진입 안 함
                continue
            _type = ("장전 신규뉴스" if _ng in ("S", "A") else
                     "해외발 동조" if _overseas else "시간외/테마 연장")
            _stop = int(_open * 0.985); _t1 = int(px * 1.02)
        _stoppct = (_stop / px - 1) * 100
        _mat = "🔥재료S" if _ng == "S" else "🟢재료A" if _ng == "A" else ("🎯브리핑" if _isbrief else "⚪재료미확인")
        _tags = _mat
        if _prog_ok:
            _tags += f" · 🟩프로그램 매수전환 +{_prog/1e8:,.0f}억"
        if _supply_ok:
            _tags += " · 💧수급유입"
        if _nq is not None:
            _tags += f" · 美선물 {_nq:+.1f}%"
        if send_telegram(token_tg, chat_id,
                         f"{SIG_BUY}\n🌅 [시가배팅·{_type}] {nm} {px:,}({(chg or 0):+.1f}%) · 거래대금 {(turn or 0)/1e8:,.0f}억\n"
                         f"{_tags}\n"
                         f"진입 {px:,} · 손절 {_stop:,}({_stoppct:+.1f}%·시초가 이탈시) · 1차익절 {_t1:,}\n"
                         f"⚠️ 첫 슈팅 분할익절 · 물타기 금지 · 시초가 이탈 후 회복 실패면 즉시 손절(스윙 전환 금지)"):
            sent[cd] = True
            _log_signal(state, now_kst, "시가배팅", nm, cd, px)
            print(f"[시가배팅·{_type}] {nm} {px:,}({(chg or 0):+.1f}%)")
    state["openbet_sent"] = sent


def _round_level(px):
    """[V25.37] 라운드 피겨(심리적 매물대) — px 바로 아래 라운드 레벨과 스텝. (level, step)."""
    if px < 10000:
        step = 1000
    elif px < 50000:
        step = 5000
    elif px < 100000:
        step = 10000
    elif px < 500000:
        step = 50000
    else:
        step = 100000
    return (int(px) // step) * step, step


def check_breakout(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.37] 돌파매매(강의 5강) — 시장 전역. 09:10~15:00, 저갭/리스크오프 억제.
    강의: '고점 추격'이 아니라 '저항 돌파가 확인된 뒤의 추세 참여'. 매도물량 흡수하며 전고점·라운드피겨·신고가 돌파.
    게이트: 20MA위 + (전고점 hi20 돌파 or 라운드피겨 돌파) + 거래량 2배↑ + 프로그램 순매수(+) or 재료 + 악재無.
    손절 돌파기준가 아래 -2%(돌파실패=근거훼손·스윙전환 금지). 종목당 하루 2회까지(3번째 돌파 회피). 종목당 쿨다운 없음(2회 상한)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60 + 11) <= m <= (15 * 60)) or sev == 2:   # 09:11+ (시가배팅과 1분 겹침 제거)
        return
    _nowts = int(now_kst.timestamp())                # [V25.39] 180초 스로틀(6시간 매분 스캔 → API 폭주 방지)
    if _nowts - state.get("breakout_scan_ts", 0) < 180:
        return
    state["breakout_scan_ts"] = _nowts
    if _regime_today(token, key, secret, now_kst, state) == "lowgap":   # 저갭/박스장 돌파 억제
        return
    today = now_kst.strftime("%Y%m%d")
    cnt = state.get("breakout_cnt", {})
    if cnt.get("_day") != today:
        cnt = {"_day": today}
    _budget = 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if cnt.get(cd, 0) >= 2:                        # 하루 2회 상한(강의: 3번째 돌파 회피)
            continue
        if not (1.0 <= (chg or 0) <= 12.0) or turn < 5_000_000_000:   # 오르는 중·과추격 제외·거래대금 50억+
            continue
        _budget += 1
        if _budget > 22:
            break
        ds = _daily_setup(token, key, secret, cd, px)
        if not ds or not ds.get("ma20"):
            continue
        if px <= ds["ma20"] or (ds.get("disp") or 0) >= 12:   # 20MA위(추세) + 과열 상한
            continue
        _pf = _price_full(token, key, secret, cd)      # (현재가,등락,시가,고가,저가)
        if not _pf:
            continue
        _open = _pf[2]
        _hi20 = ds.get("hi20") or 0
        _lvl, _step = _round_level(px)
        # 돌파 판정 — ①전고점(hi20) 돌파/신고가 ②라운드피겨(시가 아래→현재 위 관통)
        _bpx, _btype = None, None
        if _hi20 and px >= _hi20:
            _bpx, _btype = _hi20, "전고점/신고가"
        elif _lvl and _open and _open < _lvl <= px and (px - _lvl) / _lvl <= 0.02:
            _bpx, _btype = _lvl, f"라운드피겨({_lvl:,})"
        if not _bpx:
            continue
        _vr = _vol_ratio_5d(token, key, secret, cd)
        _mult = _vr[2] if _vr else 0
        if _mult < 2.0:                                # 거래량 2배 미달 = 가짜돌파 위험
            continue
        _ng, _nbad = _news_grade(cd)
        if _nbad:
            continue
        _prog = _program_net(token, key, secret, cd)
        _prog_ok = bool(_prog and _prog > 0)
        if not (_prog_ok or _ng in ("S", "A")):        # 프로그램 순매수(방향일치) or 재료 필수
            continue
        _stop = int(_bpx * 0.98)                        # 돌파기준가 아래 -2%
        _stoppct = (_stop / px - 1) * 100
        _t1 = int(px * 1.03)
        _mat = "🔥재료S" if _ng == "S" else "🟢재료A" if _ng == "A" else ""
        _prog_tag = f" · 🟩프로그램 +{_prog/1e8:,.0f}억" if _prog_ok else ""
        _nth = cnt.get(cd, 0) + 1
        if send_telegram(token_tg, chat_id,
                         f"{SIG_BUY}\n🚀 [돌파매매·{_btype}] {nm} {px:,}({(chg or 0):+.1f}%) · {_nth}차 돌파\n"
                         f"돌파기준 {_bpx:,} 상향 · 거래량 {_mult:.1f}배 동반"
                         + (f" · {_mat}" if _mat else "") + _prog_tag + "\n"
                         f"진입 {px:,} · 손절 {_stop:,}({_stoppct:+.1f}%·돌파기준 아래) · 익절 {_t1:,}(+3%)\n"
                         f"⚠️ 돌파 안착 확인 후 분할(불타기)·거래량 빠지면 속임수 · 돌파 실패시 즉시 손절(스윙 전환 금지)"):
            cnt[cd] = _nth
            _log_signal(state, now_kst, "돌파초입", nm, cd, px)
            print(f"[돌파매매] {nm} {px:,} {_btype} {_nth}차(거래량 {_mult:.1f}배)")
    state["breakout_cnt"] = cnt


_SUPPLY_RISK_KW = ("유상증자", "전환사채", "신주인수권", "교환사채", "추가상장", "최대주주",
                   "지분매각", "블록딜", "블록 딜", "감자", "CB", "BW", "보호예수 해제", "오버행")


def _supply_risk_news(code):
    """[V25.38] 공급 악재(잠재 매물) 뉴스 감지 — 유증·CB·추가상장·대주주매도 등. 스윙 배제용. 감지 시 키워드."""
    try:
        for _t in (_stock_news_titles(code, 8) or []):
            for _k in _SUPPLY_RISK_KW:
                if _k in str(_t):
                    return _k
    except Exception:
        pass
    return None


def check_swing_scan(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.38] 단기스윙(강의 9강) — 15:00~15:25 당일 1회. 며칠~수주 조건부 보유 후보.
    강의: 저가 장기보유가 아니라 '외인/기관 연속 매수 + 살아있는 재료'를 확인해 분할 진입, 수급 약화·공급악재 시 청산.
    게이트(추세형): 정배열 + 일별 수급 연속(2일↑) + 재료(S/A or 브리핑) + 지수 대비 상대강세 + 공급악재(유증·CB) 無.
    ※ 바닥권 매집형은 거래대금 랭킹 밖이라 이 스캔이 못 잡음(한계). 하루 최대 3종. 종배자금과 분리 안내."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((15 * 60) <= m <= (15 * 60 + 25)) or sev == 2:
        return
    today = now_kst.strftime("%Y%m%d")
    if state.get("swing_scan_day") == today:
        return
    _kospi = _kospi_index_kis(token, key, secret)      # 상대강도 기준(지수 등락)
    _kchg = _kospi if _kospi is not None else 0.0
    _brief = _recent_brief_codes(now_kst)
    _picks, _budget = [], 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if turn < 30_000_000_000:                      # 거래대금 300억+ (유동성)
            continue
        if (chg or 0) <= _kchg:                         # 지수 대비 상대강세 필수(강의: 상대강도)
            continue
        _budget += 1
        if _budget > 24:
            break
        ds = _daily_setup(token, key, secret, cd, px)
        if not ds or not ds.get("ma20"):
            continue
        _ma5, _ma20 = ds.get("ma5"), ds.get("ma20")
        if not (_ma5 and _ma20 and _ma5 > _ma20 and px > _ma20):   # 정배열·20MA 위(추세)
            continue
        _stag, _strong = _supply_daily_tag(token, key, secret, cd)   # 일별 수급 연속(핵심)
        if not _stag:                                  # 연속 수급 없음 → 스윙 부적합
            continue
        _ng, _nbad = _news_grade(cd)
        if _nbad:
            continue
        _isbrief = cd in _brief
        if not (_ng in ("S", "A") or _isbrief):        # 살아있는 재료 필수
            continue
        _risk = _supply_risk_news(cd)                  # 공급악재(유증·CB·추가상장) 배제
        if _risk:
            print(f"[단기스윙] {nm} 제외 — 공급악재 뉴스({_risk})")
            continue
        _score = (12 if _strong else 6) + (10 if _ng == "S" else 6 if _ng == "A" else 0) + (8 if _isbrief else 0)
        _picks.append({"code": cd, "name": nm, "px": px, "chg": chg, "turn": turn,
                       "stag": _stag, "strong": _strong, "ng": _ng, "brief": _isbrief, "score": _score,
                       "ma20": _ma20})
    state["swing_scan_day"] = today
    if not _picks:
        print("[단기스윙] 후보 0종")
        return
    _picks.sort(key=lambda x: x["score"], reverse=True)
    for p in _picks[:3]:                                # 하루 최대 3종
        _stop = int(p["ma20"] * 0.98)                   # 무효화: 20일선 이탈
        _t1 = int(p["px"] * 1.10); _t2 = int(p["px"] * 1.18)
        _mat = "🔥재료S" if p["ng"] == "S" else "🟢재료A" if p["ng"] == "A" else "🎯브리핑"
        if send_telegram(token_tg, chat_id,
                         f"{SIG_WATCH}\n📈 [단기스윙 후보] {p['name']} {p['px']:,}({(p['chg'] or 0):+.1f}%) · 지수대비 강세\n"
                         f"{_mat} · 수급{p['stag']} · 거래대금 {(p['turn'] or 0)/1e8:,.0f}억\n"
                         f"분할진입(며칠~수주 보유) · 1차익절 {_t1:,}(+10%·절반) · 2차 {_t2:,}(+18%)\n"
                         f"🚫 무효화(즉시청산): 20일선 {int(p['ma20']):,} 이탈 · 수급 대량매도 전환 · 재료 훼손\n"
                         f"※ 종배자금과 분리 · 물타기 금지(불타기만) · 공급악재(유증·CB) 공시 시 청산"):
            _log_signal(state, now_kst, "단기스윙", p["name"], p["code"], p["px"])
            print(f"[단기스윙] {p['name']} {p['px']:,} 수급{p['stag']}")


def check_limitup_follow(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.40] 상따 관찰용(강의 8강) — ⚠️매수신호 아님, 관찰·극소액 실습용 경보.
    실시간 상한가 잔량·풀림·VI·허수는 KIS 실시간 웹소켓 없이는 못 봐서, '첫테마·대장·거래대금'만 근사한다.
    상한가 근접(+25%↑) + 강한재료/브리핑 + 거래대금 충분 종목을 관찰 대상으로 알림. 09:30~15:20, 하루 1회, 리스크오프 억제.
    강의 경고: 2번 이상 풀리면 포기 · -1~2% 빠른손절 · 극소액 · 직장인/모바일 부적합."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60 + 30) <= m <= (15 * 60 + 20)) or sev == 2:
        return
    _nowts = int(now_kst.timestamp())
    if _nowts - state.get("limitup_scan_ts", 0) < 180:      # 3분 스로틀
        return
    state["limitup_scan_ts"] = _nowts
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("limitup_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    _brief = _recent_brief_codes(now_kst)
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or any(k in str(nm) for k in _EARLY_ETF_KW) or sent.get(cd):
            continue
        if (chg or 0) < 25.0 or (turn or 0) < 10_000_000_000:   # 상한가 근접(+25%↑) + 거래대금 100억+
            continue
        _ng, _nbad = _news_grade(cd)
        if _nbad:
            continue
        _isbrief = cd in _brief
        if not (_ng in ("S", "A") or _isbrief):     # 첫테마/강한재료 근사(뉴스 없는 상한가 배제)
            continue
        _sec = _sector_name(token, key, secret, cd) or ""
        _mat = "🔥재료S" if _ng == "S" else "🟢재료A" if _ng == "A" else "🎯브리핑"
        _stop = int(px * 0.98)
        if send_telegram(token_tg, chat_id,
                         f"{SIG_WATCH}\n🔺 [상따 관찰·매수아님] {nm} +{(chg or 0):.1f}% 상한가 근접"
                         + (f" · {_sec}" if _sec else "") + "\n"
                         f"{_mat} · 거래대금 {(turn or 0)/1e8:,.0f}억\n"
                         f"⚠️ 관찰용 — 실시간 상한가 잔량·풀림·VI·허수 확인 불가(API 한계). HTS에서 직접 확인 필수\n"
                         f"강의: 2회↑ 풀리면 포기 · 극소액 · 손절 −1~2% · 익일 갭 대응 · 직장인/모바일 부적합\n"
                         f"참고 손절선 {_stop:,}(−2%)"):
            sent[cd] = True
            _log_signal(state, now_kst, "상따관찰", nm, cd, px)
            print(f"[상따관찰] {nm} +{(chg or 0):.1f}% 상한가 근접")
    state["limitup_sent"] = sent


def check_pair_trade(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V25.40] 짝꿍 관찰용(강의 7강) — ⚠️매수신호 아님, 관찰·극소액 실습용 경보.
    실시간 상한가 잔량·VI 해제시각·호가 흡수는 API로 못 봐서, '강한 섹터 대장 급등 → 같은 섹터 2등주'만 근사한다.
    섹터 내 대장(거래대금 1위+급등 +15%↑) 형성 시 2등주(2위)를 관찰 대상으로 알림. 09:30~15:00, 하루 1회, 저갭/리스크오프 억제.
    강의: 후속주 5분 이내 청산 · 대장 꺾이면 즉시 매도 · 3등↓ 금지 · 초보 난도 높음."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60 + 30) <= m <= (15 * 60)) or sev == 2:
        return
    if _regime_today(token, key, secret, now_kst, state) == "lowgap":   # 테마장세 아니면 짝꿍 무의미
        return
    _nowts = int(now_kst.timestamp())
    if _nowts - state.get("pair_scan_ts", 0) < 300:         # 5분 스로틀(섹터조회 비용)
        return
    state["pair_scan_ts"] = _nowts
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("pair_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    # 급등 무버(+5%↑)만 섹터 그룹핑(대장·2등 판정) — API 절약 위해 무버로 한정
    _movers, _budget = [], 0
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn or any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if (chg or 0) < 5.0 or (turn or 0) < 5_000_000_000:
            continue
        _budget += 1
        if _budget > 16:
            break
        _sec = _sector_name(token, key, secret, cd)
        if _sec:
            _movers.append({"code": cd, "name": nm, "px": px, "chg": chg, "turn": turn, "sec": _sec})
    _by_sec = {}
    for mv in _movers:
        _by_sec.setdefault(mv["sec"], []).append(mv)
    for _sec, mem in _by_sec.items():
        if len(mem) < 2:
            continue
        mem.sort(key=lambda x: x["turn"], reverse=True)     # 거래대금 순 = 대장/2등
        _lead, _second = mem[0], mem[1]
        if (_lead["chg"] or 0) < 15.0:                       # 대장이 강하게(+15%↑) 움직여야 짝꿍 성립
            continue
        if sent.get(_second["code"]):
            continue
        _ng2, _nbad2 = _news_grade(_second["code"])
        if _nbad2:
            continue
        _stop = int(_second["px"] * 0.98)
        if send_telegram(token_tg, chat_id,
                         f"{SIG_WATCH}\n🔗 [짝꿍 관찰·매수아님] {_sec} 테마\n"
                         f"대장 {_lead['name']} +{(_lead['chg'] or 0):.1f}% → 2등주 {_second['name']} +{(_second['chg'] or 0):.1f}% 관찰\n"
                         f"⚠️ 관찰용 — 실시간 상한가 잔량·VI 확인 불가(API 한계). 대장 상한가 유지/풀림은 HTS로 직접\n"
                         f"강의: 후속주 5분 내 청산 · 대장 꺾이면 즉시 매도 · 극소액 · 3등↓ 금지\n"
                         f"참고 손절선 {_stop:,}(−2%)"):
            sent[_second["code"]] = True
            _log_signal(state, now_kst, "짝꿍관찰", _second["name"], _second["code"], _second["px"])
            print(f"[짝꿍관찰] {_sec} 대장 {_lead['name']}+{(_lead['chg'] or 0):.1f}% → 2등 {_second['name']}")
    state["pair_sent"] = sent


def check_gap_analysis(token, key, secret, now_kst, state, token_tg, chat_id, gemini_key=None):
    """[V24.0] 아침 갭상승 원인 역분석(09:03~09:12, 당일 1회) — 오늘 실제 갭상승 종목을 역추적.
    ①어제 브리핑/종배 예측 적중 여부(검증) ②Gemini로 공통 원인(테마·뉴스·미국장) 분석(학습)."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60 + 3) <= m <= (9 * 60 + 12)):
        return
    today = now_kst.strftime("%Y%m%d")
    if state.get("gap_analysis_day") == today:
        return
    _gaps = [s for s in _volume_rank(token, key, secret, top=40)
             if (s.get("chg") or 0) >= 3.0 and not any(k in str(s["name"]) for k in _EARLY_ETF_KW)]
    _gaps = sorted(_gaps, key=lambda x: x.get("chg", 0), reverse=True)[:8]
    if not _gaps:
        state["gap_analysis_day"] = today
        print("[갭분석] 갭상승 3%+ 종목 없음")
        return
    yday = (now_kst - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    _pred = set()
    try:
        with open(SCORECARD_FILE, encoding="utf-8") as f:
            _sc = json.load(f)
        _pred = {r["code"] for r in _sc if r.get("date") == yday and r.get("kind") in ("브리핑", "종배픽")}
    except Exception:
        pass
    _lines, _hit_n = [], 0
    for s in _gaps:
        _hit = s["code"] in _pred
        _hit_n += 1 if _hit else 0
        _lines.append(f"• {s['name']} +{s['chg']:.1f}% {'🎯예측적중' if _hit else '❓미예측(놓침)'}")
    _msg = (f"{SIG_WATCH}\n🌅 오늘 갭상승 원인 역분석\n"
            f"📊 갭상승(+3%↑) {len(_gaps)}종 · 어제 브리핑/종배 적중 {_hit_n}/{len(_gaps)}종\n"
            + "\n".join(_lines))
    if gemini_key:
        # [V25.15 B] 놓친 종목(미예측) 재료 분석 강화 — 왜 놓쳤나·감지 가능했나·다음에 잡을 카테고리.
        _missed = [s for s in _gaps if s["code"] not in _pred][:6]
        _batch = []
        for s in (_missed or _gaps[:6]):
            _tt = _stock_news_titles(s["code"], 3)
            _batch.append(f"{s['name']}(+{s['chg']:.1f}%): " + (" / ".join(_tt[:3]) if _tt else "뉴스없음"))
        _prompt = ("아래는 오늘 갭상승했는데 우리 예측이 '놓친' 종목들과 최근 뉴스야. 각 종목마다:\n"
                   "① 갭 원인 재료를 분류: [신약/임상] [정책/정부] [수주/계약] [실적] [테마순환] [미국연동] [수급/세력] [불명] 중 하나\n"
                   "② 그 재료가 '전날 미리 감지 가능'했나(전날 공시·거래대금 조짐 존재) vs '당일 사후성'인가\n"
                   "③ 맨 끝에 '다음에 잡으려면 강화할 감지 1가지' 제안(예: DART 임상공시 감시, 정책수혜 키워드 등).\n"
                   "종목당 1줄, 총 6줄 이내. 학습용이니 간결히.\n\n" + "\n".join(_batch))
        _v = _gemini_generate(gemini_key, _prompt)
        if _v:
            _msg += f"\n\n🧠 놓친 종목 원인·감지개선:\n{_v.strip()}"
    if send_telegram(token_tg, chat_id, _msg):
        state["gap_analysis_day"] = today
        print(f"[갭분석] {len(_gaps)}종 · 예측적중 {_hit_n}")


def check_early_catch(token, key, secret, now_kst, state, token_tg, chat_id, sev=1):
    """[V18.7] 조기 포착(기준선 초입)·급증진입 — 거래대금 랭킹 상시 스캔. 09:00~15:20, 리스크오프 억제."""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((9 * 60) <= m <= (15 * 60 + 20)) or sev == 2:
        return
    if _regime_today(token, key, secret, now_kst, state) == "lowgap":   # [V25.26] 저갭/박스장 아침단타 억제
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
        # [V24.7] 급증진입 OFF — 누적성적 0%·평균 -6.2%(실행 중단). 조기포착만 유지.
        if (ds["kij_cross"] or ds["kij_near"]) and mult >= 1.2 and disp < 7 and -1.0 <= chg <= 8.0:
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


# ══════════════════════════════════════════════════════════════════════════
# [V20.0] 종가베팅 픽을 watcher로 이관 — 대시보드 없이 장 마감 직전 자동 선정·발송.
#   거래대금 상위 → 20MA↑·비과열·악재無 → 원톱 + 분산 2·3위. pick_history.json 공유
#   (대시보드 backfill_pick_outcomes/명중률이 그대로 익일 갭 대조·집계).
# ══════════════════════════════════════════════════════════════════════════
PICK_FILE = os.path.join(BASE, "pick_history.json")


def _pick_read():
    try:
        with open(PICK_FILE, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except Exception:
        return []


def _pick_write(rows):
    try:
        with open(PICK_FILE, "w", encoding="utf-8") as f:
            json.dump(rows[-1500:], f, ensure_ascii=False)
    except OSError:
        pass


def _log_pick(now_kst, code, name, score, px, nq=None, signal="dolpanty"):
    """대시보드 log_dolpanty_pick와 동일 포맷으로 당일 종목별 1회 기록(백필·명중률 공유)."""
    if not code or not score:
        return
    today = now_kst.strftime("%Y-%m-%d")          # 대시보드와 동일한 날짜 포맷
    rows = _pick_read()
    if any(r.get("date") == today and r.get("code") == str(code) for r in rows):
        return
    rows.append({"date": today, "code": str(code), "name": name or "",
                 "score": round(float(score), 1), "px": int(px or 0),
                 "regime": "", "signal": signal,
                 "nq": (round(float(nq), 2) if isinstance(nq, (int, float)) else None),
                 "open_next": None, "gap": None})
    _pick_write(rows)


def _elite_tag(token, key, secret, code):
    """[V25.31] ⭐정예 판정 — 재료 A/S급 + 당일 수급 유입 필수. 여기에 '일별 수급 연속성'을 얹어
    ⭐정예(기본) / ⭐⭐정예(수급 3일연속·전일比급증) 2단계. 신호에 붙여 확신 강도 표시. 미달 시 ''."""
    _ng, _nbad = _news_grade(code)
    if _nbad or _ng not in ("S", "A"):
        return ""
    try:
        _f, _o = _investor_est(token, key, secret, code)
        if _f is None or _o is None or (_f + _o) < 0:   # 당일 수급 미확인 or 이탈 → 정예 아님
            return ""
    except Exception:
        return ""
    _stag, _strong = _supply_daily_tag(token, key, secret, code)   # 일별 연속성·강도
    if _strong:
        return f" ⭐⭐정예(수급강){_stag}"                          # 재료A + 수급 연속/급증 = 최상위
    return f" ⭐정예{_stag}"                                        # 재료A + 당일 유입(+2일연속 있으면 태그)


def _regime_today(token, key, secret, now_kst, state):
    """[V25.26] 당일 장세 상태 캐시 — _regime_detect를 하루 1회만 계산(API 절약), 이후 재사용.
    반환: 'gap'/'neutral'/'lowgap'/'unknown'. 아침 단타 억제 게이트용."""
    today = now_kst.strftime("%Y%m%d")
    _rc = state.get("regime_cache") or {}
    if _rc.get("day") == today and _rc.get("state"):
        return _rc["state"]
    try:
        _r = _regime_detect(token, key, secret, now_kst)
        _st = _r.get("state", "unknown")
    except Exception:
        _st = "unknown"
    state["regime_cache"] = {"day": today, "state": _st}
    return _st


def _regime_detect(token, key, secret, now_kst, lookback_days=21, min_n=6):
    """[V25.10] 장세 판독기 — 최근 종배(픽+그림자)의 '익일 시가 갭'(종배 실제 청산가) 중앙값으로
    지금이 종배 통하는 장인지 판정. 갭 잘 뜨는 장(+)=종배 유효 / 저갭·불리 장(−)=종배 억제·대형주 눌림.
    ★익일 종가가 아닌 익일 시가로 측정(종배=종가매수→익일 시가청산). 이상치엔 중앙값으로 강건.★
    반환 {state,avg,n,text,tag}. 데이터 부족 시 state='unknown'."""
    from datetime import timedelta
    _cut = (now_kst - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    gaps, _cache = [], {}
    for p in _pick_read():
        if p.get("signal") not in ("dolpanty", "dolpanty_shadow", "dolpanty_div"):
            continue
        if str(p.get("date", "")) < _cut:
            continue
        cd = str(p.get("code", "")).zfill(6); px = p.get("px") or 0
        if not px:
            continue
        opens = _cache.get(cd)
        if opens is None:
            opens = _daily_opens(token, key, secret, cd); _cache[cd] = opens
        _pdate = str(p.get("date", "")).replace("-", "")
        _nxt = next((d for d in sorted(opens.keys()) if d > _pdate), None)
        if _nxt and opens.get(_nxt):
            gaps.append((opens[_nxt] / px - 1) * 100)     # 익일 시가 갭 = 종배 실제 수익
    n = len(gaps)
    if n < min_n:
        return {"state": "unknown", "avg": None, "n": n,
                "text": f"🌫️ 장세판독 데이터 부족(종배 표본 {n}<{min_n}) — 며칠 더 축적", "tag": ""}
    gaps.sort()
    avg = gaps[n // 2] if n % 2 else (gaps[n // 2 - 1] + gaps[n // 2]) / 2   # 중앙값(이상치 강건)
    if avg >= 0.5:
        return {"state": "gap", "avg": avg, "n": n,
                "text": f"🟢 갭 장세(종배 유효) — 최근 종배 익일 시가갭(중앙값) {avg:+.1f}% (n={n})", "tag": "🟢종배유효장세"}
    if avg <= -0.5:
        return {"state": "lowgap", "avg": avg, "n": n,
                "text": f"🔵 저갭 장세(종배 불리) — 최근 종배 익일 시가갭(중앙값) {avg:+.1f}% (n={n}) · 대형주 눌림·인버스 권장",
                "tag": "🔵저갭장세(종배 억제)"}
    return {"state": "neutral", "avg": avg, "n": n,
            "text": f"🟡 중립 장세 — 최근 종배 익일 시가갭(중앙값) {avg:+.1f}% (n={n})", "tag": "🟡중립장세"}


def _recent_brief_codes(now_kst, days=2):
    """[V24.7] 최근 브리핑(선행 테마) 종목 — 종배 재설계용 우선 유니버스.
    데이터상 브리핑(55%·+1.1%)이 종배픽(36%·-3.3%)보다 압도적. 종배도 이 종목풀에서 뽑는다.
    최근 days일 내 kind=='브리핑' 종목의 {code:name} 반환(중복 제거)."""
    out = {}
    try:
        from datetime import timedelta
        _cut = (now_kst - timedelta(days=days)).strftime("%Y-%m-%d")
        with open(SCORECARD_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        for r in rows if isinstance(rows, list) else []:
            if r.get("kind") == "브리핑" and str(r.get("date", "")) >= _cut:
                cd = str(r.get("code", "")).zfill(6)
                if cd and cd != "000000":
                    out[cd] = r.get("name", "")
    except Exception:
        pass
    return out


def _pick_supply_score(token, key, secret, code):
    """[V25.34] 종배 수급 가점(강의 1강 ②·④ 핵심) — 당일 외인/기관 유입 + 일별 연속성 + 프로그램 매수전환.
    무수급이면 감점(강의: 수급 없는 종목 종배 부적합). 반환 (점수델타, 태그문자열)."""
    delta = 0.0
    _fe, _oe = _investor_est(token, key, secret, code)             # 당일 추정 순매수(수량)
    _today_in = ((_fe or 0) + (_oe or 0)) > 0
    _stag, _strong = _supply_daily_tag(token, key, secret, code)   # 일별 연속성·강도
    _prog = _program_net(token, key, secret, code)                 # 프로그램 순매수(금액)
    if _today_in:
        delta += 8
    if _strong:                                                   # 3일연속·급증
        delta += 12
    elif _stag:                                                   # 2일연속·급증
        delta += 6
    if _prog and _prog > 0:
        delta += 6
    if not (_today_in or _stag):                                  # 당일도 일별도 수급 없음 → 종배 부적합
        delta -= 10
    tag = (_stag or "").strip()
    if _prog and _prog > 0:
        tag = (tag + " 🟩프로그램+").strip()
    if not (_today_in or _stag):
        tag = "⚠️무수급"
    return delta, tag


def _valuation(token, key, secret, code):
    """[V25.34] 기본 체력 — inquire-price에서 시총(억)·EPS. 적자주 판정용(강의 1강 ⑥ 재무 안전장치).
    반환 (mcap억|None, eps|None). EPS<0 = 적자. 실패 시 (None,None)."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010100"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=6)
        o = r.json().get("output", {})
        if isinstance(o, dict):
            return _to_int(o.get("hts_avls")), _to_int(o.get("eps"))
    except Exception:
        pass
    return None, None


def _pick_extra_score(token, key, secret, code, px, turn, ds):
    """[V25.34] 종배 추가 가점 통합(강의 1강) — 수급 연속성 + 재무(적자 감점) + 거래대금 상대증가 + 전고점.
    반환 (점수델타, 태그문자열). ds=_daily_setup 결과(turnavg·hi20 재사용)."""
    delta, tag = _pick_supply_score(token, key, secret, code)      # ② 수급
    # [V25.41] 블록딜성 '가짜 수급' 차단(동주 일지 9/4) — 기관 대량매수처럼 보여도 블록딜·유증·CB·추가상장이면
    #   진짜 매집이 아니라 이미 정해진 물량. 수급 가점을 상쇄하고 감점. (강의: "수급만 보면 기관대량매수=블록딜")
    _srisk = _supply_risk_news(code)
    if _srisk:
        delta = min(delta, 0.0) - 12                              # 수급 가점 무효화 + 공급악재 감점
        tag = (tag + f" ⚠️{_srisk}(가짜수급)").strip()
    _mc, _eps = _valuation(token, key, secret, code)               # ⑥ 재무 체력
    if _eps is not None and _eps < 0:                             # 적자주 → 강의: 적자 테마주보다 이익주 우선
        delta -= 12
        tag = (tag + " ⚠️적자").strip()
    if ds and ds.get("turnavg") and turn >= ds["turnavg"] * 2:    # ① 평소比 거래대금 2배↑(자금 신규 유입)
        delta += 6
        tag = (tag + " 💵대금급증").strip()
    _hi20 = ds.get("hi20") if ds else 0
    if _hi20 and px >= _hi20 * 0.99:                              # ④ 전고점 근접/돌파(신고가 흐름)
        delta += 6
        tag = (tag + " 📈전고돌파").strip()
    return delta, tag


def check_dolpanty_pick(token, key, secret, now_kst, state, token_tg, chat_id, sev=1, nq=None, force=False,
                        gemini_key=None):
    """[V20.4] 종가베팅 픽 — 거래대금 상위 중 20MA↑·비과열(등락<7·이격<7)·악재無 자동 선정.
    창을 15:05~19:50로 확대(정규장 마감~NXT 야간). 종목 선정은 정규장 거래대금 랭킹 기준이나,
    NXT 시간대(18:00~19:50)엔 각 후보의 현재가를 NX(넥스트레이드)로 실시간 갱신 — 종가 아닌 실시간가로 판정.
    force=True: 시간창·당일락 무시(수동 강제). 리스크오프(sev2)면 관망."""
    m = now_kst.hour * 60 + now_kst.minute
    # 정규장 마감권(15:05~15:30) 또는 NXT 야간(18:00~19:50). 그 사이 휴장 갭(15:30~18:00)은 스킵.
    if not force and not (((15 * 60 + 5) <= m <= (15 * 60 + 30)) or ((18 * 60) <= m <= (19 * 60 + 50))):
        return
    _in_nxt = (18 * 60) <= m <= (19 * 60 + 50)       # NXT 야간창이면 실시간 NX가로 갱신
    today = now_kst.strftime("%Y%m%d")
    if not force and state.get("dolpanty_pick_day") == today:      # 당일 1회(flip-flop 방지)
        return
    # [V20.9] 리스크오프(sev2)여도 그림자 로깅은 계속 — "관망이 옳았나(상위주가 익일 빠졌나)" 검증 데이터.
    #   발송은 관망 그대로, 아래 스캔 후 그림자만 기록하고 리턴.
    cands = []
    raw = []                                          # [검증] 필터 통과 여부 무관 거래대금 상위(그림자 로깅용)
    _budget = 0
    _brief = _recent_brief_codes(now_kst)             # [V24.7] 최근 브리핑 테마(선행) — 종배 우선 유니버스
    _seen = set()                                     # 이미 스코어링한 종목(브리핑 보강 루프 중복 방지)
    for s in _volume_rank(token, key, secret, top=40):
        cd, nm, px, chg, turn = s["code"], s["name"], s["px"], s["chg"], s["turnover"]
        if not px or not turn:
            continue
        if any(k in str(nm) for k in _EARLY_ETF_KW):
            continue
        if turn < 50_000_000_000:                    # 거래대금 500억 미달 컷
            continue
        if _in_nxt:                                  # NXT 야간 — 종가 대신 넥스트레이드 실시간가로 갱신
            _npx, _nchg, _nturn = _price_and_turnover(token, key, secret, cd, mrkt="NX")
            if _npx:
                px, chg = _npx, _nchg                # 현재가·등락은 NXT 실시간(거래대금은 정규장 랭킹 유지)
        raw.append({"code": cd, "name": nm, "px": px, "chg": chg, "turn": turn})
        if sev == 2:                                 # 리스크오프 — raw(그림자용)만 모으고 스코어링 스킵
            continue
        if chg >= 7.0:                               # 이미 과열 — 추격 금지
            continue
        if chg < -2.0:                               # 하락 과대(떨어지는 칼) — 종배 제외
            continue
        _budget += 1
        if _budget > 24:                             # API 절약(루프당 일봉조회 상한)
            break
        ds = _daily_setup(token, key, secret, cd, px)
        if not ds or not ds.get("ma20"):
            continue
        disp = ds["disp"]
        if px <= ds["ma20"]:                          # 20MA↑ 필수(종배 정석)
            continue
        if disp >= 7.0:                               # 20MA 이격 과열
            continue
        ng, nbad = _news_grade(cd)                    # 악재 종목 제외
        if nbad:
            continue
        score = 40.0                                  # 거래대금 관문 통과 기본
        if ds.get("above5"):
            score += 8
        if ds.get("kij_cross"):
            score += 12
        elif ds.get("kij_near"):
            score += 6
        # [V24.3] 재료(뉴스·테마) 가점 대폭↑ — 데이터상 브리핑(재료·선행)이 종배픽(거래대금·후행)보다 승률 3배.
        #   종배도 재료 있는 종목을 우선하도록 S/A 가점을 2배로.
        if ng == "S":
            score += 20
        elif ng == "A":
            score += 12
        if 0 <= disp <= 3:                            # 20일선 눌림 근처(과열 아닌 초입) 가점
            score += 5
        # [V24.3] 초대형주 페널티 — 거래대금 초상위 대형주(삼성/하이닉스급)는 지수종속·갭 작아 종배 부적합.
        if turn >= 300_000_000_000:                   # 3천억↑ = 초대형(지수 대장주)
            score -= 12
        # [V24.7] 브리핑 테마 가점 — 종배 재설계 핵심. 검증된 선행 신호와 겹치면 강한 우선순위.
        _isbrief = cd in _brief
        if _isbrief:
            score += 25
        # [V25.34] 강의 1강 반영 — 수급 연속성·재무(적자감점)·거래대금 상대증가·전고점 가감점
        _ex, _extag = _pick_extra_score(token, key, secret, cd, px, turn, ds)
        score += _ex
        if ng not in ("S", "A"):                       # [V25.42] 재료 미확인 점수 상한 49(<발송임계 50) —
            score = min(score, 49.0)                   #   브리핑겹침·수급만으로 99점 원톱 되던 것 차단(흥구석유式).
            #   → 확정픽/분산 자격 없음, 그림자로만 검증. 강의 2강: 종배 핵심=재료 지속성
        _seen.add(cd)
        cands.append({"code": cd, "name": nm, "px": px, "chg": chg, "turn": turn, "disp": disp,
                      "score": score, "ng": ng, "brief": _isbrief, "xtag": _extag})
    # [V24.7] 브리핑 테마 보강 — 거래대금 top40에 아직 안 든 선행 테마주도 종배 후보로.
    #   종배픽이 진 이유=후행 대형주만 담아서. 선행 테마주는 거래대금 낮아도(500억 floor 면제) 넣는다.
    if sev != 2:
        for _bc, _bn in _brief.items():
            if _bc in _seen or _budget > 30:
                continue
            _bpx, _bchg, _bturn = _price_and_turnover(token, key, secret, _bc,
                                                       mrkt=("NX" if _in_nxt else "J"))
            if not _bpx or _bchg is None:              # [V24.9] 등락 미확인이면 컷(과거: chg=0.0으로 위장돼 급락 우회)
                continue
            if _bchg >= 7.0 or _bchg < -2.0:           # 과열·급락 컷(본 루프와 동일)
                continue
            _budget += 1
            _bds = _daily_setup(token, key, secret, _bc, _bpx)
            if not _bds or not _bds.get("ma20") or _bpx <= _bds["ma20"]:  # 20MA↑ 필수
                continue
            _bdisp = _bds["disp"]
            if _bdisp >= 7.0:
                continue
            _bng, _bnbad = _news_grade(_bc)
            if _bnbad:
                continue
            _bscore = 40.0 + 25                        # 기본 + 브리핑(선행) 가점
            if _bds.get("above5"):
                _bscore += 8
            if _bds.get("kij_cross"):
                _bscore += 12
            elif _bds.get("kij_near"):
                _bscore += 6
            if _bng == "S":
                _bscore += 20
            elif _bng == "A":
                _bscore += 12
            if 0 <= _bdisp <= 3:
                _bscore += 5
            _bex, _bextag = _pick_extra_score(token, key, secret, _bc, _bpx, _bturn or 0, _bds)
            _bscore += _bex
            if _bng not in ("S", "A"):                  # [V25.42] 재료 미확인 점수 상한 49(<발송임계)
                _bscore = min(_bscore, 49.0)
            _seen.add(_bc)
            cands.append({"code": _bc, "name": _bn or "", "px": _bpx, "chg": _bchg or 0.0,
                          "turn": _bturn or 0, "disp": _bdisp, "score": _bscore,
                          "ng": _bng, "brief": True, "xtag": _bextag})

    def _log_shadow(exclude=()):
        # [검증] 그림자 픽 — "우리가 뽑을 뻔한 후보"를 텔레그램 없이 로깅. 대시보드 백필이 익일 갭 대조.
        #   [V22.9] 점수 있는 후보(cands) 우선 — 매일 삼성/하이닉스 거래대금 top만 찍히던 문제 해결.
        #   cands 있으면 점수 상위(실제픽 제외)를, 없으면(리스크오프/미형성) 거래대금 상위로 폴백.
        if cands:
            _pool = sorted(cands, key=lambda c: c["score"], reverse=True)
        else:
            # [V25.11] 폴백도 초대형주(3천억↑ 지수 대장주) 제외 — 만년 삼성/하이닉스만 찍히던 노이즈 차단.
            #   종배로 뽑을 일 없는 종목을 그림자에 남기면 검증 데이터가 오염됨. 없으면 로깅 스킵.
            _pool = sorted([x for x in raw if x["turn"] < 300_000_000_000],
                           key=lambda x: x["turn"], reverse=True)
        _sh = [c for c in _pool if c["code"] not in exclude][:3]
        for c in _sh:
            _log_pick(now_kst, c["code"], c["name"], 30.0, c["px"], nq, "dolpanty_shadow")
        if _sh:
            print(f"[종배픽] 그림자 로깅 {len(_sh)}종({'NXT실시간' if _in_nxt else '종가'}): "
                  + ", ".join(f"{c['name']} {c['px']:,}({c['chg']:+.1f}%)" for c in _sh))

    if sev == 2:                                     # [V20.9] 리스크오프 — 관망 발송 + 그림자만 로깅(검증 유지)
        _log_shadow()
        if send_telegram(token_tg, chat_id,
                         "🌒[종배] 오늘은 리스크오프 — 종가베팅 관망(현금 방어). "
                         "매크로 🟢 전환·낙폭 진정 후 재산출."):
            state["dolpanty_pick_day"] = today
        print("[종배픽] 리스크오프 관망 — 그림자만 로깅")
        return
    if not cands:
        _log_shadow()                                # 관망 날에도 검증 데이터 축적
        if send_telegram(token_tg, chat_id,
                         "🌒[종배] 후보 미형성 — 거래대금 500억↑·20MA↑·비과열 통과 종목 없음(관망)."):
            state["dolpanty_pick_day"] = today
        print("[종배픽] 후보 0종 — 관망")
        return
    # [V25.35] 테마(섹터) 대장/2등 순위(강의 1·2강) — 같은 테마에 후보 2개↑ 몰릴 때(=테마 형성)만
    #   거래대금 순위로 대장 +10 / 2등 +5 / 3등↓ 후발주 -8. 단독 섹터는 중립(테마 아님).
    _by_sec = {}
    for _c in cands:
        _c["sector"] = _sector_name(token, key, secret, _c["code"])   # 이후 분산선정에서도 재사용
        if _c["sector"]:
            _by_sec.setdefault(_c["sector"], []).append(_c)
    for _sec, _members in _by_sec.items():
        if len(_members) < 2:
            continue                                                 # 단독 = 테마 아님 → 중립
        _members.sort(key=lambda c: c["turn"], reverse=True)
        for _rk, _c in enumerate(_members, 1):
            if _rk == 1:
                _c["score"] += 10; _c["theme_rank"] = "🥇대장"
            elif _rk == 2:
                _c["score"] += 5;  _c["theme_rank"] = "🥈2등"
            else:
                _c["score"] -= 8;  _c["theme_rank"] = "🔻후발"
            _c["xtag"] = (_c.get("xtag", "") + " " + _c["theme_rank"]).strip()
    cands.sort(key=lambda c: c["score"], reverse=True)
    # [V20.7] 금요일 종배 억제 — 금요일 픽은 주말(3일 밤) 홀딩이라 주말 이벤트 리스크 폭증
    #   (예: 삼성생명 금 +10%→월 -11%). 확정픽 발송 금지, 그림자만 로깅(검증 데이터 유지).
    if now_kst.weekday() == 4:                    # 월0~금4 · 금요일
        _log_shadow()
        if send_telegram(token_tg, chat_id,
                         "🌒[종배] 금요일 — 종가베팅 억제(주말 3일 홀딩=이벤트 리스크). "
                         "월요일 장 재개 후 재산출 권장."):
            state["dolpanty_pick_day"] = today
        print("[종배픽] 금요일 억제 — 주말 리스크 회피(그림자만 로깅)")
        return
    # [V25.6] 장세 판독 — 저갭 장세(종배 불리)면 임계 상향(50→60): 강한 픽만 발송, 나머지 관망.
    #   (모카 교훈: 갭 안 뜨는 장에선 종배 자체를 쉬어라. 데이터로 장세 감지해 자동 억제.)
    _regime = _regime_detect(token, key, secret, now_kst)
    _thr = 60 if _regime["state"] == "lowgap" else 50
    if cands[0]["score"] < _thr:
        _log_shadow()
        _rmsg = (f" · {_regime['tag']}" if _regime.get("tag") else "")
        if send_telegram(token_tg, chat_id,
                         f"🌒[종배] 확정픽 없음 — 최상위({cands[0]['name']} {cands[0]['score']:.0f}점) "
                         f"기준({_thr}점) 미달{_rmsg}. 강한 셋업 아님(관망).\n{_regime['text']}"):
            state["dolpanty_pick_day"] = today
        print(f"[종배픽] 확정픽 없음 — 최고 {cands[0]['name']}({cands[0]['score']:.0f}) < {_thr} · {_regime['state']} · 관망")
        return
    pick = cands[0]
    # [V23.2] 분산 후보는 '다른 섹터'로 — 같은 섹터면 동반 갭다운이라 분산 효과 없음(사용자 룰).
    _pick_sec = pick.get("sector") or _sector_name(token, key, secret, pick["code"])
    pick["sector"] = _pick_sec
    _used_sec = {_pick_sec} if _pick_sec else set()
    div = []
    for c in cands[1:]:
        if c["score"] < _thr:                        # [V25.10] 분산 후보도 장세 임계(_thr) 적용 — 저갭엔 약한 분산 금지
            continue
        _csec = c.get("sector") or _sector_name(token, key, secret, c["code"])
        if _csec and _csec in _used_sec:            # 이미 담은 섹터(원톱 포함) → 스킵
            continue
        c["sector"] = _csec
        div.append(c)
        if _csec:
            _used_sec.add(_csec)
        if len(div) >= 2:
            break
    _mat = {"S": "🔥재료 강함(S급)", "A": "🟢재료 있음(A급)"}.get(pick["ng"], "⚠️재료 미확인")
    # [V25.34] 익일 무효화 조건(강의 2강 필수 출력항목 ④) — 전일 저점 기준 + 시초가 이탈 + 재료 훼손.
    #   손절도 고정 -2% 대신 '전일 저점 이탈'을 무효화 가격으로(변동성 반영), −3% 상한으로 캡.
    _pds = _daily_setup(token, key, secret, pick["code"], pick["px"])
    _plow = (_pds or {}).get("prevlow") or 0
    if _plow and _plow < pick["px"]:
        _stop = max(int(_plow), int(pick["px"] * 0.97))   # 전일저점/−3% 中 높은쪽(무효화 가격)
    else:
        _stop = int(pick["px"] * 0.98)
    _stoppct = (_stop / pick["px"] - 1) * 100
    _t1 = int(pick["px"] * 1.03)
    _invalidate = ("\n🚫 익일 무효화(즉시 청산): ①9시 시초가 이탈 "
                   + (f"②전일 저점 {int(_plow):,} 이탈 " if _plow else "②전일 저점 이탈 ")
                   + "③재료 뒤집는 공시/뉴스(논리 훼손)")
    _pbasis = "NXT 실시간가" if _in_nxt else "종가"       # 가격 기준 표기
    # [V21.7] 확정픽 AI 뉴스판정(Gemini) — 최종 1종만 뉴스 본문 읽어 오버나이트 적합성 첨부(비용 미미)
    _ai_news = ""
    try:
        _ai_news = _gemini_stock_news_verdict(gemini_key, pick["code"], pick["name"])
    except Exception:
        pass
    # [V22.6] 관심종목 10대 기준(사용자 매매원칙) 자동 체크 첨부
    _wl = ""
    try:
        _wlp = _watchlist_check(token, key, secret, pick["code"], pick["px"], pick["chg"],
                                pick["turn"], pick["ng"])
        if _wlp:
            _wl = f"\n📋 관심기준 {len(_wlp)}개 충족: {'·'.join(_wlp)}"
    except Exception:
        pass
    # [V23.4 종배룰 #3] 외인·기관 수급 방향
    _sup = ""; _supply_neg = False
    try:
        _f, _o = _investor_est(token, key, secret, pick["code"])
        if _f is None or _o is None:
            raise ValueError("수급 데이터 없음")
        _fa, _oa = _f * pick["px"] / 1e8, _o * pick["px"] / 1e8
        _supply_neg = (_f + _o) < 0
        _sup = f"\n💰 수급: 외인 {_fa:+.0f}억 · 기관 {_oa:+.0f}억" + (" ✅유입" if not _supply_neg else " ⚠️이탈")
    except Exception:
        # [V24.9] 수급 조회 실패를 조용히 '이상무'로 넘기지 말고 명시(사람이 소액·확인 판단)
        _sup = "\n💰 수급: ⚠️미확인(조회 실패 — 개장 후 외인/기관 직접 확인·소액 대응)"
    # [V23.6] 자기모순 방지 — AI뉴스가 '부적합/악재'거나 수급 이탈이면 확정픽(매수) 강등 → 관망.
    #   (SK스퀘어 실패 케이스: AI '부적합'인데 확정픽 발송 → 다음날 하락. 데이터로 검증된 강등 규칙.)
    _ai_bad = ("부적합" in _ai_news) or ("악재" in _ai_news)
    # [V25.12 B] 저갭 장세에선 '재료 없는 종배' 금지 — 이 장에서 갭 나는 건 강한 개별 재료(공시·실적·브리핑)뿐.
    #   무재료(ng 없음 + 브리핑 아님) 픽은 저갭 장세에 오버나이트 근거 없음 → 강등(관망).
    _regime_block = (_regime["state"] == "lowgap"
                     and pick.get("ng") not in ("S", "A") and not pick.get("brief"))
    # [V25.42] 재료 미확인 원톱 강등 — 브리핑 겹침만으로 재료 없이 원톱 확정되던 문제(흥구석유式).
    #   강의 2강: 종배 핵심=재료 지속성. 재료 미확인(ng none)은 원톱 자격 없음 → 관망(분산 후보로는 잔존).
    _nograde_block = pick.get("ng") not in ("S", "A")
    if _ai_bad or _supply_neg or _regime_block or _nograde_block:
        _why = []
        if _ai_bad:
            _why.append("AI 부적합/악재")
        if _supply_neg:
            _why.append("수급 이탈")
        if _regime_block:
            _why.append("저갭 장세+무재료(갭 근거 없음)")
        if _nograde_block:
            _why.append("재료 미확인(오버나이트 근거 약함)")
        send_telegram(token_tg, chat_id,
                      f"{SIG_WATCH}\n🌒[종배·관망] {pick['name']} {pick['px']:,} — 확정픽 강등\n"
                      f"점수 {pick['score']:.0f}이나 {'·'.join(_why)}로 오버나이트 부적합 → 매수 보류(관망).{_ai_news}{_sup}\n"
                      f"※ 기술적 셋업은 있으나 뉴스/수급이 반대 — 종배는 쉬는 게 정답")
        _log_shadow(exclude={pick["code"], *[c["code"] for c in div]})   # 강등돼도 검증 데이터는 남김
        _log_pick(now_kst, pick["code"], pick["name"], 30.0, pick["px"], nq, "dolpanty_shadow")  # 강등=그림자
        state["dolpanty_pick_day"] = today
        print(f"[종배픽] 확정픽 강등(관망) — {pick['name']}: {'·'.join(_why)}")
        return
    # [V25.17] NXT 거래 여부 판별 → 태그·청산 가이드. 로깅 시 NXT/미거래 분리(--analyze 비교용).
    def _nxt_label(_st):
        if _st is True:
            return ("🟢NXT거래", "청산: NXT(16~20시·8시)서 +3% 뜨면 즉시(밤 재료 NXT 흡수)·안 뜨면 9시 시가")
        if _st is False:
            return ("🔴NXT미거래", "⚠️밤새 탈출 불가(풀노출)·9시 시가만 청산 — 소액·손절 철저 / 단 9시 갭엣지는 살아있음")
        return ("⚪NXT미확인", "청산: NXT 조회되면 +3%서, 아니면 9시 시가")
    _pick_nxt = _nxt_tradable(token, key, secret, pick["code"])
    _pick_sig = "dolpanty_nonxt" if _pick_nxt is False else "dolpanty"    # 미확인/거래=dolpanty
    # 통과 — 확정픽 로깅(NXT 여부별 분리 저장)
    _log_pick(now_kst, pick["code"], pick["name"], pick["score"], pick["px"], nq, _pick_sig)
    for c in div:
        _log_pick(now_kst, c["code"], c["name"], c["score"], c["px"], nq, "dolpanty_div")
    _log_shadow(exclude={pick["code"], *[c["code"] for c in div]})
    # [V23.4 종배룰 #5] 시황 선반영 판정 — 美선물 상승분을 한국이 이미 따라왔나(대형주 종배 여지)
    _mkt = ""
    try:
        _nqp = _pct("NQ=F")
        _ksp = _kospi_index_kis(token, key, secret)      # [V24.9] KIS 우선(yfinance 지연 회피)
        if _ksp is None:
            _ksp = _hist_pct("^KS11")
            if _ksp is not None and abs(_ksp) > 4.0:
                _ksp = None
        if _nqp is not None and _ksp is not None:
            _mhead = f"\n📊 시황: 美선물 {_nqp:+.1f}% vs 코스피 {_ksp:+.1f}%"
            if _nqp > 0.3 and _ksp >= _nqp * 0.8:
                _mkt = _mhead + " → ⚠️선반영(지수 이미 따라옴·대형주 종배 여지↓)"
            elif _nqp > 0.5 and _ksp <= 0.1:
                _mkt = _mhead + " → 🔴갭하락 위험(美↑ 한국 보합)"
            elif _nqp <= 0.1 and _ksp >= 0.5:            # 미국 지지 없이 한국만 올랐다 = 갭 여지 적음
                _mkt = _mhead + " → ⚠️한국 단독 상승(美 지지 없음·다음날 갭 여지↓)"
            elif _nqp > 0 and _ksp < _nqp * 0.5:
                _mkt = _mhead + " → 🟢여지 있음(한국 덜 따라옴·내일 갭업 여지)"
            else:
                _mkt = _mhead
    except Exception:
        pass
    _psec_txt = f"[{_pick_sec}] " if _pick_sec else ""
    _brief_tag = " 🎯브리핑테마(선행)" if pick.get("brief") else ""   # [V24.7] 재설계: 브리핑 겹침 표시
    _elite_j = " ⭐정예" if ((pick.get("ng") in ("S", "A") or pick.get("brief")) and not _supply_neg) else ""
    _ntag, _nguide = _nxt_label(_pick_nxt)
    _divtxt = ("\n🌒 분산(다른 섹터): "
               + " · ".join(f"{c['name']}[{c.get('sector','')}]{(' '+c['theme_rank']) if c.get('theme_rank') else ''} "
                            f"{c['px']:,}({c['chg']:+.1f}%)"
                            for c in div)) if div else "\n🌒 분산: 다른 섹터 후보 없음(원톱만)"
    _prank = f" {pick['theme_rank']}" if pick.get("theme_rank") else ""   # 원톱 테마 순위
    if send_telegram(token_tg, chat_id,
                     f"{SIG_BUY}\n🌒[종배·오버나이트] 확정픽 {_psec_txt}{pick['name']}{_prank}{_brief_tag}{_elite_j} {_ntag} "
                     f"{pick['px']:,}({pick['chg']:+.1f}%) · {_pbasis}\n"
                     f"{_mat} · 20MA 이격 {pick['disp']:+.0f}% · 점수 {pick['score']:.0f}"
                     + (f" · {pick['xtag']}" if pick.get("xtag") else "")
                     + f"{_ai_news}{_wl}{_sup}{_mkt}\n"
                     f"🧭 {_regime['text']}\n"
                     f"진입 {pick['px']:,} · 손절 {_stop:,}({_stoppct:+.1f}%·전일저점/−3%) · 익절 {_t1:,}(+3%)"
                     f"{_invalidate}"
                     f"{_divtxt}\n"
                     f"⚠️ 종가 굳는 것 확인 후 매수 · 극소액 분산(몰빵 금지)"
                     + (" · 🔵저갭장세라 소액·신중" if _regime['state'] == 'lowgap' else "") + "\n"
                     f"★{_nguide}★"):
        state["dolpanty_pick_day"] = today
        _log_signal(state, now_kst, "종배픽", pick["name"], pick["code"], pick["px"])
        state["dolpanty_pick_info"] = {"day": today, "code": pick["code"], "name": pick["name"],
                                       "px": pick["px"], "stop": _stop, "t1": _t1, "reminded": False,
                                       "ng": pick.get("ng"), "brief": bool(pick.get("brief"))}  # [V25.19] 홀딩판정용
    # [V25.17] 비교 종배픽 — 원톱과 '반대 NXT 유형' 최고 후보 1종 추가(둘 다 선정·검증 비교용).
    #   NXT거래 vs 미거래 어느 쪽 종배가 이기나 --analyze로 대조. 소액 실험.
    _used = {pick["code"], *[c["code"] for c in div]}
    _cmp = None
    for c in cands:
        if c["code"] in _used or c["score"] < _thr:
            continue
        _cst = _nxt_tradable(token, key, secret, c["code"])
        if _cst is not None and _cst != bool(_pick_nxt):     # 원톱과 반대 유형
            c["nxt"] = _cst
            _cmp = c
            break
    if _cmp:
        _ct2, _cg2 = _nxt_label(_cmp["nxt"])
        _cstop = int(_cmp["px"] * 0.98); _ct1 = int(_cmp["px"] * 1.03)
        _csig = "dolpanty_nonxt" if _cmp["nxt"] is False else "dolpanty"
        _log_pick(now_kst, _cmp["code"], _cmp["name"], _cmp["score"], _cmp["px"], nq, _csig)
        send_telegram(token_tg, chat_id,
                      f"{SIG_WATCH}\n🌒[종배·비교픽({_ct2})] {_cmp['name']} {_cmp['px']:,}({_cmp['chg']:+.1f}%) · 점수 {_cmp['score']:.0f}\n"
                      f"원톱({_ntag})과 반대 유형 — 어느 종배가 이기나 검증용 소액\n"
                      f"진입 {_cmp['px']:,} · 손절 {_cstop:,}(−2%) · 익절 {_ct1:,}(+3%)\n★{_cg2}★")
        print(f"[종배픽] 비교픽 {_cmp['name']} ({_ct2})")
    print(f"[종배픽] 후보 {len(cands)}종 · 원톱 {pick['name']}({pick['score']:.0f}·{_ntag}) · 분산 {len(div)}종")


def check_dolpanty_entry(token, key, secret, now_kst, state, token_tg, chat_id):
    """[V23.1] 종배 진입 타이밍 상시 감시 — 종가(15:23~15:30)+NXT(18:00~19:50) 동안 오늘 확정픽을
    실시간 조회해 '진입 좋은 자리'면 알림. 20분 쿨다운(스팸 방지). 8시 NXT 마감까지 커버.
    판정: 진입가 이하·안정=적정 / 이미 오름=추격주의 / 급락=갭다운주의."""
    m = now_kst.hour * 60 + now_kst.minute
    _close = (15 * 60 + 23) <= m <= (15 * 60 + 30)       # 종가 동시호가
    _nxt = (18 * 60) <= m <= (19 * 60 + 50)              # NXT 야간
    if not (_close or _nxt):
        return
    today = now_kst.strftime("%Y%m%d")
    _info = state.get("dolpanty_pick_info") or {}
    if _info.get("day") != today:
        return
    # 20분 쿨다운
    _last = _info.get("entry_ts", 0)
    if (int(now_kst.timestamp()) - int(_last)) < 20 * 60:
        return
    _base = _info["px"]                                   # 종가 확정픽 진입가 기준
    _mrkt = "NX" if _nxt else "J"
    try:
        _cur, _chg, _turn = _price_and_turnover(token, key, secret, _info["code"], mrkt=_mrkt)
    except Exception:
        _cur = None
    if not _cur:
        return
    _gap = (_cur / _base - 1) * 100                       # 종가픽 대비 현재가 괴리
    _stop = int(_cur * 0.98); _t1 = int(_cur * 1.03)
    _when = "종가 동시호가" if _close else "NXT 야간"
    if _gap <= -2.0:
        _vd = ("🔴 NXT 약세 — 갭다운 주의", f"진입가 대비 {_gap:+.1f}% 하락. 지금 잡으면 싸지만 약세 신호 — 재고 권장.")
    elif _gap <= 0.5:
        _vd = ("🟢 진입 적정 자리", f"진입가 근처({_gap:+.1f}%) — 안 비싸게 잡을 자리. 극소액·-2% 손절.")
    elif _gap <= 2.0:
        _vd = ("🟡 소폭 상승", f"진입가 대비 {_gap:+.1f}% — 살짝 올랐지만 아직 추격은 아님. 눌림 보며.")
    else:
        _vd = ("⚠️ 추격 주의", f"진입가 대비 {_gap:+.1f}% 급등 — 지금 추격 금물, 눌림 대기.")
    if send_telegram(token_tg, chat_id,
                     f"{SIG_WATCH}\n🌒⏰ 종배 진입타이밍({_when}) — {_info['name']}\n"
                     f"{_vd[0]} · 현재 {_cur:,}({_chg:+.1f}%)\n{_vd[1]}\n"
                     f"진입 {_cur:,} · 손절 {_stop:,}(−2%) · 익절 {_t1:,}(+3%) · 청산 내일 9시 시가"):
        _info["entry_ts"] = int(now_kst.timestamp())
        state["dolpanty_pick_info"] = _info
        print(f"[종배픽] 진입타이밍 알림({_when}) — {_info['name']} {_gap:+.1f}%")


def check_dolpanty_exit(token, key, secret, now_kst, state, token_tg, chat_id):
    """[V25.12 A] 종배 NXT 청산 알림 — 종배 근본문제(NXT가 오버나이트 갭을 9시 전에 흡수) 대응.
    NXT 애프터(16:00~20:00, 픽 당일)·프리마켓(08:00~08:50, 익일)에서 종배픽이 목표(+3%) 도달하면
    '9시 기다리지 말고 지금 NXT 청산'(갭은 NXT서 이미 남), 손절선 이탈이면 'NXT 손절' 알림. 픽당 1회."""
    m = now_kst.hour * 60 + now_kst.minute
    _after = (16 * 60) <= m <= (20 * 60)                  # 당일 NXT 애프터
    _pre = (8 * 60) <= m <= (8 * 60 + 50)                 # 익일 NXT 프리마켓
    if not (_after or _pre):
        return
    _info = state.get("dolpanty_pick_info") or {}
    if not _info.get("code") or _info.get("exit_done"):
        return
    today = now_kst.strftime("%Y%m%d")
    if _after and _info.get("day") != today:             # 애프터는 픽 당일만
        return
    if _pre and _info.get("day") == today:               # 프리마켓은 픽 익일(당일 픽이면 아직 애프터)
        return
    _base = _info["px"]
    _t1 = _info.get("t1") or int(_base * 1.03)
    _stop = _info.get("stop") or int(_base * 0.98)
    try:
        _cur, _chg, _ = _price_and_turnover(token, key, secret, _info["code"], mrkt="NX")
    except Exception:
        _cur = None
    if not _cur:
        return
    _g = (_cur / _base - 1) * 100
    _when = "NXT 애프터(16~20시)" if _after else "NXT 프리마켓(8시)"
    _sig = None
    if _cur >= _t1:
        _sig = ("🎯 NXT 청산 타이밍!", f"목표 +{_g:.1f}% 도달({_cur:,}) — 갭은 NXT서 이미 남. 9시 시가 기다리다 사라지기 전에 지금 익절 검토")
    elif _cur <= _stop:
        _sig = ("🔴 NXT 손절", f"진입가 대비 {_g:+.1f}%({_cur:,}) 손절선 이탈 — NXT서 손절해 밤/갭다운 리스크 차단")
    # [V25.19] 8시 프리마켓 '매도 vs 홀딩' 자동판정 — 목표(+3%)·손절 사이 애매 구간(사용자 핵심 고민).
    #   4요인: 재료질(A급) · 수급(순매수) · 선반영(+3%↑) · NXT 미거래. 픽당 1회.
    if _pre and not _sig and not _info.get("hold_judged"):
        _ng = _info.get("ng"); _isbrief = _info.get("brief")
        _strong_mat = (_ng in ("S", "A")) or _isbrief
        _sup_pos = None
        try:
            _f, _o = _investor_est(token, key, secret, _info["code"])
            if _f is not None and _o is not None:
                _sup_pos = (_f + _o) >= 0
        except Exception:
            pass
        _reflected = _g >= 3.0
        _why = []
        _hold = True
        if _reflected:
            _hold = False; _why.append(f"이미 +{_g:.1f}%(선반영)")
        if not _strong_mat:
            _hold = False; _why.append("재료 약함(A급/브리핑 아님)")
        if _sup_pos is False:
            _hold = False; _why.append("수급 이탈")
        _verdict = ("🟢 9시까지 보유 (A급재료+수급+선반영無 → 9시 갭·장중 추가 여력)" if _hold
                    else "🔴 8시 NXT 매도 (" + "·".join(_why) + " → 홀딩 근거 약함)")
        _supmark = "✅유입" if _sup_pos else ("⚠️이탈" if _sup_pos is False else "미확인")
        if send_telegram(token_tg, chat_id,
                         f"{SIG_WATCH}\n🌅 종배 홀딩 판정(8시 프리마켓) — {_info['name']}\n"
                         f"현재 {_cur:,}({(_chg or 0):+.1f}%) · 진입대비 {_g:+.1f}%\n"
                         f"재료:{_ng or '없음'}{'·브리핑' if _isbrief else ''} · 수급:{_supmark} · 선반영:{'예' if _reflected else '아니오'}\n"
                         f"→ {_verdict}\n"
                         f"※ 8시 NXT 하락은 유동성 얇아 가짜일 수 있음 — 애매하면 9시 첫10분 저점 확인 후 판단(NXT 미거래 종목은 승률 낮으니 특히 소액)"):
            _info["hold_judged"] = True
            state["dolpanty_pick_info"] = _info
            print(f"[종배홀딩판정] {_info['name']} {_g:+.1f}% — {'보유' if _hold else '매도'}")
        return
    if not _sig:
        return
    if send_telegram(token_tg, chat_id,
                     f"{SIG_WATCH}\n🌙💰 종배 청산 알림({_when}) — {_info['name']}\n"
                     f"{_sig[0]} · 현재 {_cur:,}({(_chg or 0):+.1f}%)\n{_sig[1]}"):
        _info["exit_done"] = True
        state["dolpanty_pick_info"] = _info
        print(f"[종배청산] {_info['name']} {_g:+.1f}% — {_sig[0]}")


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
    if _regime_today(token, key, secret, now_kst, state) == "lowgap":   # [V25.26] 저갭/박스장 시가저격 억제
        # 하루 1회 안내(왜 아침 신호가 없는지)
        if not sent.get("_lowgap_note"):
            send_telegram(token_tg, chat_id,
                          "🔵[박스장] 저갭 장세 — 아침 당일단타(시가저격·진입·조기포착) 억제.\n"
                          "박스장은 아침 추격 승률 낮음(오늘 23%) → 저녁 브리핑 테마·눌림 위주로.")
            sent["_lowgap_note"] = True
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
                _bt = _big_trend_tag(token, key, secret, code, px)
                _sdisp = _ma20_disparity(token, key, secret, code, px)   # [V24.5] 과열이면 눌림 목표
                _pull = _pullback_levels(token, key, secret, code, px, chg) if (_sdisp is not None and _sdisp >= 7) else ""
                send_telegram(token_tg, chat_id,
                              f"{SIG_BUY}\n🌅[아침단타·당일청산] 🎯 시가저격 (마의구간 09:00~09:15) — {name}\n"
                              f"거래대금 {turn/1e8:,.0f}억 (임계 {need/1e8:,.0f}억·{cap}) 돌파 · {_bk} · {_mattxt}{_bt}\n"
                              f"• 현재가 {px:,}원 ({chg:+.2f}%) · {now_kst.strftime('%H:%M')} KST{_pull}\n"
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


def _investor_daily(token, key, secret, code, days=5):
    """[V25.32] 종목 일별 외국인/기관 순매수 최근 N일 — inquire-investor(FHKST01010900).
    최신순 [{frgn,orgn(수량), frgn_amt,orgn_amt(금액원)}]. 수급 연속성·강도·평단 판정용. 실패 시 []."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-investor",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010900"},
                         params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=6)
        out = []
        for x in (r.json().get("output") or [])[:days]:
            if isinstance(x, dict):
                out.append({"frgn": _to_int(x.get("frgn_ntby_qty")), "orgn": _to_int(x.get("orgn_ntby_qty")),
                            "frgn_amt": _to_int(x.get("frgn_ntby_tr_pbmn")),
                            "orgn_amt": _to_int(x.get("orgn_ntby_tr_pbmn"))})
        return out
    except Exception:
        return []


def _supply_avgprice(rows):
    """[V25.32] 외인/기관 '평단' 근사 — 최근 연속 순매수 구간의 Σ순매수금액/Σ순매수수량(원).
    강의(수급단타왕) '평단 아래서 추가매수' 규칙용. 금액필드 없거나 계산불가 시 None."""
    if not rows:
        return None
    best = None
    for k, amt_k in (("frgn", "frgn_amt"), ("orgn", "orgn_amt")):
        tot_qty = tot_amt = 0
        for row in rows:                              # 최신→과거, 순매수(+)인 날만 누적(매도전환 만나면 중단)
            q, a = row.get(k, 0), row.get(amt_k, 0)
            if q > 0 and a:
                tot_qty += q
                tot_amt += abs(a)
            else:
                break
        if tot_qty > 0 and tot_amt > 0:
            avg = tot_amt / tot_qty
            best = avg if best is None else min(best, avg)   # 더 낮은(보수적) 평단 채택
    return int(best) if best else None


def _supply_daily_tag(token, key, secret, code):
    """[V25.31] 수급 연속성·강도 태그 — 외인/기관 연속 순매수 일수 + 최근 급증. 반환 (tag, strong)."""
    return _supply_daily_tag_from(_investor_daily(token, key, secret, code, days=5))


def _supply_daily_tag_from(d):
    """[V25.32] 위와 동일하나 이미 조회한 일별수급(rows)으로 판정 — 중복 API 호출 방지."""
    if not d:
        return "", False

    def _consec(k):
        n = 0
        for row in d:
            if row.get(k, 0) > 0:
                n += 1
            else:
                break
        return n
    _fc, _oc = _consec("frgn"), _consec("orgn")
    _best = max(_fc, _oc)
    _who = "외인" if _fc >= _oc else "기관"
    # 전일대비 급증: 최신일 순매수 > 직전 3일 평균 절대값 × 2 (외인 or 기관)
    _surge = False
    try:
        for _k in ("frgn", "orgn"):
            _today = d[0].get(_k, 0)
            _prev = [abs(r.get(_k, 0)) for r in d[1:4]]
            _avg = sum(_prev) / len(_prev) if _prev else 0
            if _today > 0 and _avg > 0 and _today >= _avg * 2:
                _surge = True
    except Exception:
        pass
    if _best >= 3:
        return f" 🔥{_who}{_best}일연속매수" + ("·전일比급증" if _surge else ""), True
    if _best == 2 or _surge:
        return f" 🟢{_who}{'2일연속' if _best >= 2 else '수급급증'}", False
    return "", False


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


# ── [V21.3] 장전 예열 스캔(08:00~08:55) — NXT 프리마켓/예상체결가로 09시 갭 미리 포착 ──
_PREMKT_START, _PREMKT_END = 8 * 60, 8 * 60 + 55
_PREMKT_GAP_UP, _PREMKT_GAP_DN = 2.0, -2.0


def _expected_price(token, key, secret, code):
    """장전 동시호가 예상체결가·예상등락% — inquire-price(antc_cnpr/antc_cntg_prdy_ctrt).
    필드 없으면 (None,None) → 호출부가 NXT로 대체. KIS 제공 여부 실전 검증용."""
    try:
        r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                         headers={"authorization": f"Bearer {token}", "appkey": key,
                                  "appsecret": secret, "tr_id": "FHKST01010100"},
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=6)
        o = r.json().get("output", {})
        if isinstance(o, dict):
            ap = _to_int(o.get("antc_cnpr"))                  # 예상체결가
            ac = o.get("antc_cntg_prdy_ctrt")                 # 예상체결 전일대비율
            if ap:
                return ap, float(str(ac or 0).replace(",", "") or 0)
    except Exception:
        pass
    return None, None


def check_premarket(token, key, secret, now_kst, state, token_tg, chat_id, lineup, sev=1):
    """[V21.3] 장전 예열 스캔(08:00~08:55) — 라인업을 NXT 프리마켓/예상체결가로 조회.
    갭업(+2%↑)=09시 시가저격 주목 예고 / 갭다운(-2%↓)=보유 대응 경고. 종목별 당일 1회.
    ※ 매수 신호 아님(관망/경계 등급) — 개장 후 거래대금·수급 확인이 원칙."""
    m = now_kst.hour * 60 + now_kst.minute
    if not (_PREMKT_START <= m <= _PREMKT_END):
        return
    today = now_kst.strftime("%Y%m%d")
    sent = state.get("premkt_sent", {})
    if sent.get("_day") != today:
        sent = {"_day": today}
    # [V24.1] 1차 조기감지 유니버스 = 라인업 + 어제 브리핑/종배 예측 + 내 관심종목(예측이 8시 NXT에 벌써 반응하나)
    _univ, _seen = [], set()
    for _c, _n in lineup:
        if _c not in _seen:
            _univ.append((_c, _n, "라인업")); _seen.add(_c)
    yday = (now_kst - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        with open(SCORECARD_FILE, encoding="utf-8") as f:
            _sc = json.load(f)
        for r in _sc:
            if r.get("date") == yday and r.get("kind") in ("브리핑", "종배픽") and r.get("code") not in _seen:
                _univ.append((r["code"], r.get("name", r["code"]), "어제예측")); _seen.add(r["code"])
    except Exception:
        pass
    for s in _read_my_watch():
        _c = str(s.get("code", "")).zfill(6)
        if _c.isdigit() and len(_c) == 6 and _c not in _seen:
            _univ.append((_c, s.get("name", _c), "내관심")); _seen.add(_c)
    for code, name, _origin in _univ:
        if sent.get(code):
            continue
        # 1) NXT 프리마켓 실가(08:00~08:50) 우선
        px, chg, turn = _price_and_turnover(token, key, secret, code, mrkt="NX")
        _src = "NXT"
        if not (px and chg is not None):
            _ap, _ac = _expected_price(token, key, secret, code)   # 2) NXT 미거래 → 예상체결가
            if _ap and _ac is not None:
                px, chg, _src = _ap, _ac, "예상체결"
        if not (px and chg is not None):
            continue
        if chg >= _PREMKT_GAP_UP:
            _ot = "🎯어제예측 조기반응" if _origin == "어제예측" else ("👁️내 관심종목" if _origin == "내관심" else "라인업")
            send_telegram(token_tg, chat_id,
                          f"{SIG_WATCH}\n🌅 [장전 조기감지·8시NXT] {name} 갭업 {chg:+.1f}% ({_src}) · {_ot}\n"
                          f"현재 {px:,} · {now_kst.strftime('%H:%M')} KST\n"
                          f"👀 9시 갭업 예고 — 개장(2차) 거래대금·수급 확인 후 대응(추격 금지)")
            sent[code] = True
        elif chg <= _PREMKT_GAP_DN:
            send_telegram(token_tg, chat_id,
                          f"{SIG_CAUTION}\n🌅 [장전 예열] {name} 갭다운 {chg:+.1f}% ({_src})\n"
                          f"현재 {px:,} · {now_kst.strftime('%H:%M')} KST\n"
                          f"⚠️ 보유 시 09시 대응 준비 — 갭다운 출발 가능")
            sent[code] = True
    state["premkt_sent"] = sent


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


def _pullback_levels(token, key, secret, code, px, chg, ds=None):
    """[V24.4] 과열 종목 눌림 매수 목표 — 현재가 아래 지지선(5일선·20일선·오늘시가·전일종가) 텍스트.
    '눌림 기다려'만 하지 말고 구체적 재진입 자리를 제시. 지지선 없으면 ''."""
    _lv = []
    if ds is None:
        ds = _daily_setup(token, key, secret, code, px)
    if ds:
        if ds.get("ma5"):
            _lv.append(("5일선", int(ds["ma5"])))
        if ds.get("ma20"):
            _lv.append(("20일선", int(ds["ma20"])))
    try:
        _p, _c, _o, _h, _l = _price_full(token, key, secret, code)
        if _o:
            _lv.append(("오늘시가", _o))
        if chg is not None and px:
            _lv.append(("전일종가", int(px / (1 + (chg or 0) / 100.0))))
    except Exception:
        pass
    _below = sorted({(_n, _v) for _n, _v in _lv if _v and _v < px}, key=lambda x: -x[1])  # 현재가 아래·가까운 순
    if not _below:
        return ""
    _txt = "\n🎯 눌림 매수 목표(추격 대신 여기서 재진입):"
    for _n, _v in _below[:4]:
        _txt += f"\n  • {_n} {_v:,} ({(_v / px - 1) * 100:+.1f}%)"
    _txt += "\n  → 이 지지선 근처로 눌리면 진입 검토 · 못 지키면 손절"
    return _txt


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
    # [V21.2] 시간대 적응형 봉 — 수급 몰리는 창(09:15~10:00·14:00~15:00)은 5분봉(빠른 포착),
    #   그 외(지루한 midday)는 15분봉(노이즈↓). 봉 크기·임계는 시간대 따라 자동 전환.
    _bsize = 5 if (((9 * 60 + 15) <= m <= (10 * 60)) or ((14 * 60) <= m <= (15 * 60))) else 15
    _bidx = m // _bsize                              # 현재 봉 버킷 인덱스(5 or 15분)
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
    _vrank_b15 = None                                # [V21.1] 주도주 교차검증용 거래대금 랭킹(지연조회)
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
                # [V21.1 주도주 교차검증] 당일 거래대금 랭킹(top40) 밖 = 비주도주 → 15분봉 매수 억제(손절 확률↑)
                if _vrank_b15 is None:
                    _vrank_b15 = {s["code"] for s in _volume_rank(token, key, secret, top=40)}
                _is_leader = code in _vrank_b15
                if sev != 2 and _is_leader:   # [V17.1] 리스크오프 억제 + [V21.1] 주도주만 발송
                    _bt = _big_trend_tag(token, key, secret, code, px)
                    # [V24.4] 과열(이격 7%↑)이면 '눌림 기다려'만 말고 구체적 눌림 매수 목표 제시
                    _pull = _pullback_levels(token, key, secret, code, px, chg) if (_disp is not None and _disp >= 7) else ""
                    send_telegram(token_tg, chat_id,
                                  f"{SIG_BUY}\n🌅[장중단타·당일청산] 📊 {_bsize}분봉 강한 양봉 — {name}\n"
                                  f"방금 막 끝난 {_bsize}분봉이 +{_move:.1f}% 강하게 올랐고 거래대금도 늘었어요.\n"
                                  f"{_nqtxt} · 🔥주도주(거래대금 랭킹 內){_bt}\n"
                                  f"• 현재가 {px:,}원 ({(chg or 0):+.2f}%) · {now_kst.strftime('%H:%M')} KST\n"
                                  f"{_warn}{_pull}\n"
                                  f"─── 가격표 ───\n"
                                  f"🎯 매수가(현재): {_buy:,}원\n"
                                  f"✂️ 손절가: {_stop:,}원 (−2%)\n"
                                  f"{_res_line}\n"
                                  f"─────────\n"
                                  f"👉 HTS 열어 ①기관 붙었나 ②이격 과열 아닌가 확인 후 타격")
                    sent[_key] = True
                elif sev != 2 and not _is_leader:
                    sent[_key] = True             # 비주도주 — 발송 억제(중복 방지 위해 마킹만)
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
    if _regime_today(token, key, secret, now_kst, state) == "lowgap":   # [V25.26] 저갭/박스장 진입 억제
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
            _bt = _big_trend_tag(token, key, secret, code, px)
            send_telegram(token_tg, chat_id,
                          f"{SIG_BUY_STRONG}\n🌅[아침단타·당일청산] 🟢 진입 시그널 — {name}\n"
                          f"거래대금 {turn/1e8:,.0f}억(임계 {need/1e8:,.0f}↑) · {_org_txt} · 순매수 합 (+){_disp_txt} · {_emat}{_bt}\n"
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
    # [V25.1] 미국 현물 개장(약 22:30 KST) 전 20:00~22:30은 SOX가 전일 종가(stale) → 알림 억제.
    #   (개장 전 어제 SOX로 '미장 반도체 강/약세' 헛알림 방지. 나스닥선물 실시간은 check_nq_cross가 담당.)
    if (20 * 60) <= m < (22 * 60 + 30):
        return sox
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


def check_morning_riskoff(now_kst, state, token_tg, chat_id):
    """[V25.5] 아침 비상 점검 넛지 — 밤사이 미국 급락 시 08:00~08:15 보유 점검 알림(당일 1회).
    나스닥선물 -1.5%↓(또는 SOX -3%↓)면 발송. ★강제 손절 아님 — '점검·약한 종목 우선 정리 검토·
    손절선 확인'을 유도해 감정적 경직(존버) 방지. NXT 프리마켓(08:00~08:50)에 대응 가능.★"""
    m = now_kst.hour * 60 + now_kst.minute
    if not ((8 * 60) <= m <= (8 * 60 + 15)):
        return
    today = now_kst.strftime("%Y%m%d")
    if state.get("morning_riskoff_day") == today:
        return
    nq = _pct("NQ=F"); sox = _pct("^SOX")
    _crash = (nq is not None and nq <= -1.5) or (sox is not None and sox <= -3.0)
    if not _crash:
        return
    _us = []
    if nq is not None:
        _us.append(f"나스닥선물 {nq:+.1f}%")
    if sox is not None:
        _us.append(f"SOX {sox:+.1f}%")
    if send_telegram(token_tg, chat_id,
                     f"{SIG_CAUTION}\n🚨 아침 비상 점검 — 밤사이 미국 급락\n"
                     f"{' · '.join(_us)}\n"
                     f"① 보유 종목 손절선(−2%) 재확인 ② 비주도주·약한 종목 우선 정리 검토 "
                     f"③ NXT 프리마켓(08:00~08:50)에서 미리 대응 가능\n"
                     f"⚠️ 강제 매도 아님 — 8시 선물은 9시 개장가와 다를 수 있음. "
                     f"감정적 경직 말고 '계획대로' 대응 · 물타기 금지\n{now_kst.strftime('%m/%d %H:%M')} KST"):
        state["morning_riskoff_day"] = today
        print(f"[아침비상] 미국 급락 점검 알림 발송 — {' · '.join(_us)}")


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
    # [V25.30] 시그니처엔 '변하는 금액' 빼고 구조(섹터·종목명·개수)만 — 금액 미세변동으로 30분마다
    #   재발송되던 스팸 해결. 원톱 종목명·수급 방향 섹터·개수 바뀔 때만 발송.
    _flow_sig = (f"{_outf['sector']}>{_inf['sector']}" if (_inf and _outf) else "none")
    _sig = f"{sev}|{_flow_sig}|{_ace_n}|{_entry_n}|{_top.get('name', '—')}|{1 if macro.get('overheat') else 0}"
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
    _ct = kis_token(kis_key, kis_secret) if (kis_key and kis_secret) else None
    sev, mtext, mdetail, _ = compute_macro(_ct, kis_key, kis_secret)
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
    ap.add_argument("--force-pick", action="store_true",
                    help="종가베팅 픽을 시간창 무시하고 지금 즉시 1회 발송 후 종료(수동 강제)")
    ap.add_argument("--test-news", action="store_true",
                    help="저녁 뉴스 시황 스캐너를 시간창 무시하고 지금 즉시 1회 실행 후 종료(키 테스트)")
    ap.add_argument("--report", action="store_true",
                    help="추천 종목 성적표(아침 당일단타/어제 저녁 종배·브리핑) 현재가 대조 후 텔레그램 발송·종료")
    ap.add_argument("--analyze", action="store_true",
                    help="과거 누적 신호 종합 분석 — 신호종류별 승률·평균수익(익일 종가 대비) 텔레그램·종료")
    ap.add_argument("--stock", nargs="?", const="__WATCH__", default=None,
                    help="특정종목 종합 해석(차트+수급+뉴스+타점). --stock 005930=그 종목 / --stock=my_watch 전체")
    ap.add_argument("--volatility", action="store_true",
                    help="주간 변동성 상위 스캐너(래리 윌리엄스式 물색) — 재료·선반영·눌림 태그 첨부 텔레그램·종료")
    ap.add_argument("--regime", action="store_true",
                    help="장세 판독기 — 최근 종배 익일 수익으로 종배 유효/저갭 장세 판정 텔레그램·종료")
    ap.add_argument("--range", dest="range_scan", action="store_true",
                    help="레인지(박스) 매매 강제 스캔 — 시간창 무시하고 박스 하단 반등 종목 텔레그램·종료")
    ap.add_argument("--holdings", action="store_true",
                    help="보유종목 현황·홀딩판정(my_holdings.json) 텔레그램·종료")
    ap.add_argument("--exit-analysis", dest="exit_analysis", action="store_true",
                    help="종배 청산 타이밍 분석 — 익일 시가청산 vs 종가청산, NXT거래/미거래 분리(쌓인 데이터)")
    args = ap.parse_args()
    token_tg = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token_tg or not chat_id:
        print("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 설정 필요"); sys.exit(1)
    kis_key, kis_secret = read_kis_keys()
    kis_on = bool(kis_key and kis_secret)

    if args.report:                               # [V23.3] 추천 성적표 수동 발송
        if not kis_on:
            print("⚠️ KIS 키 없음 — 성적표 현재가 대조 불가"); sys.exit(1)
        _rt = kis_token(kis_key, kis_secret)
        _rnow = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        _scorecard_report(_rt, kis_key, kis_secret, _rnow, token_tg, chat_id)
        sys.exit(0)

    if args.analyze:                              # [V24.2] 과거 누적 신호 종합 분석(신호종류별 승률)
        if not kis_on:
            print("⚠️ KIS 키 없음 — 신호 분석 일봉대조 불가"); sys.exit(1)
        _at = kis_token(kis_key, kis_secret)
        _anow = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        _analyze_history(_at, kis_key, kis_secret, _anow, token_tg, chat_id)
        sys.exit(0)

    if args.exit_analysis:                        # [V25.18] 종배 청산 타이밍 분석(시가 vs 종가·NXT별)
        if not kis_on:
            print("⚠️ KIS 키 없음 — 청산분석 일봉대조 불가"); sys.exit(1)
        _et = kis_token(kis_key, kis_secret)
        _enow = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        _analyze_exit_timing(_et, kis_key, kis_secret, _enow, token_tg, chat_id)
        sys.exit(0)

    if args.stock is not None:                    # [V24.6] 특정종목 종합 해석
        if not kis_on:
            print("⚠️ KIS 키 없음 — 종목 해석 불가"); sys.exit(1)
        _st = kis_token(kis_key, kis_secret)
        _gk = read_gemini_key()
        if args.stock == "__WATCH__":             # 인자 없으면 my_watch 전체
            print(f"[진단] my_watch 경로: {MY_WATCH_FILE}")
            print(f"[진단] 파일 존재: {os.path.exists(MY_WATCH_FILE)}")
            try:
                with open(MY_WATCH_FILE, encoding="utf-8-sig") as _f:
                    _raw = json.load(_f)
                print(f"[진단] on={_raw.get('on')!r}, stocks={len(_raw.get('stocks') or [])}개")
            except Exception as _e:
                print(f"[진단] 읽기 실패: {type(_e).__name__}: {_e}")
            _targets = [(str(s.get("code", "")).zfill(6), s.get("name", "")) for s in _read_my_watch()]
            if not _targets:
                print("⚠️ my_watch.json 비어있음 — 위 진단 확인 / 종목코드 지정: --stock 005930"); sys.exit(1)
        else:
            _targets = [(str(args.stock).zfill(6), "")]
        for _cd, _nm in _targets:
            _rep = _deep_stock(_st, kis_key, kis_secret, _cd, _nm, _gk)
            send_telegram(token_tg, chat_id, f"{SIG_WATCH}\n{_rep}")
            print(f"[종목해석] {_nm or _cd} 발송")
        sys.exit(0)

    if args.volatility:                           # [V25.0] 주간 변동성 상위 스캐너
        if not kis_on:
            print("⚠️ KIS 키 없음 — 변동성 스캔 불가"); sys.exit(1)
        _st = kis_token(kis_key, kis_secret)
        print("[변동성] 주간 변동성 상위 스캔 중... (거래대금 상위 일봉 조회)")
        _rep = _volatility_scan(_st, kis_key, kis_secret, read_gemini_key())
        send_telegram(token_tg, chat_id, f"{SIG_WATCH}\n{_rep}")
        print("[변동성] 발송 완료")
        sys.exit(0)

    if args.range_scan:                           # [V25.29] 레인지(박스) 매매 강제 스캔
        if not kis_on:
            print("⚠️ KIS 키 없음 — 레인지 스캔 불가"); sys.exit(1)
        _st = kis_token(kis_key, kis_secret)
        _rnow = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        print("[레인지] 박스권 하단 반등 강제 스캔 중...")
        _cnt0 = len(load_state().get("range_sent", {}))
        _stt = load_state()
        check_range_trade(_st, kis_key, kis_secret, _rnow, _stt, token_tg, chat_id, sev=1, force=True)
        _found = len([k for k in _stt.get("range_sent", {}) if k != "_day"])
        if _found == 0:
            send_telegram(token_tg, chat_id, "📦 레인지 매매 — 조건 통과 종목 없음(횡보+하단반등+상단여력 3%↑ 통과 없음).")
        print(f"[레인지] 강제 스캔 완료 — {_found}종")
        sys.exit(0)

    if args.holdings:                             # [V25.21] 보유종목 현황·홀딩판정
        if not kis_on:
            print("⚠️ KIS 키 없음 — 보유 조회 불가"); sys.exit(1)
        _ht = kis_token(kis_key, kis_secret)
        _hnow = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        _holdings_report(_ht, kis_key, kis_secret, _hnow, token_tg, chat_id)
        sys.exit(0)

    if args.regime:                               # [V25.6] 장세 판독기
        if not kis_on:
            print("⚠️ KIS 키 없음 — 장세 판독 불가"); sys.exit(1)
        _st = kis_token(kis_key, kis_secret)
        _rg = _regime_detect(_st, kis_key, kis_secret, datetime.datetime.utcnow() + datetime.timedelta(hours=9))
        send_telegram(token_tg, chat_id, f"{SIG_WATCH}\n🧭 장세 판독\n{_rg['text']}\n"
                      + ("→ 종배 임계 상향(강한 픽만)·대형주 눌림 위주 권장" if _rg['state'] == 'lowgap'
                         else "→ 종배 정상 운용" if _rg['state'] == 'gap' else "→ 선별 운용"))
        print(f"[장세판독] {_rg['state']} · {_rg['text']}")
        sys.exit(0)

    if args.test_news:                            # [V21.4] 저녁 뉴스 강제 테스트 — 시간창·당일락 무시
        _nid, _nsec = read_naver_keys()
        _gk = read_gemini_key()
        print(f"[테스트] 저녁뉴스 — 네이버 {'OK' if (_nid and _nsec) else '키없음(RSS폴백)'} · "
              f"Gemini {'OK' if _gk else '키없음(헤드라인만)'}")
        _st = load_state(); _st.pop("evening_news_day", None)
        _now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
        _now = _now.replace(hour=18, minute=0)    # 저녁 창(17~22) 안으로 강제
        check_evening_news(_now, _st, token_tg, chat_id, _nid, _nsec, _gk, kis_key, kis_secret)
        save_state(_st)
        sys.exit(0)

    if args.force_pick:                           # [V20.0] 수동 강제 — 시간창·당일락 무시하고 즉시 종배픽
        if not kis_on:
            print("⚠️ KIS 키 없음 — 종배픽 강제 실행 불가"); sys.exit(1)
        _now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)   # 실제 현재시각 사용
        _m = _now.hour * 60 + _now.minute
        # NXT 야간창(18:00~19:50)이면 실제 시각 그대로 → NX 실시간가 판정. 그 외엔 종가 기준(15:15로 표기).
        _nxt_now = (18 * 60) <= _m <= (19 * 60 + 50)
        if not _nxt_now:
            _now = _now.replace(hour=15, minute=15)   # 정규장 종가 기준 판정
        st = load_state()
        st.pop("dolpanty_pick_day", None)         # 당일락 해제(강제 재발송)
        _tok = kis_token(kis_key, kis_secret)
        _sev, _, _, _ = compute_macro(_tok, kis_key, kis_secret)
        _gk_fp = read_gemini_key()                # AI 뉴스판정용
        print(f"[강제] 종배픽 실행 — sev={_sev} · {_now.strftime('%H:%M')} 기준"
              + (" · NXT 실시간가" if _nxt_now else " · 종가"))
        check_dolpanty_pick(_tok, kis_key, kis_secret, _now, st, token_tg, chat_id, _sev, force=True,
                            gemini_key=_gk_fp)
        save_state(st)
        sys.exit(0)
    if not kis_on:
        if not os.path.exists(SECRETS_FILE):
            print(f"⚠️ secrets.toml 없음: {SECRETS_FILE}")
        else:
            print(f"⚠️ secrets.toml 있으나 KIS 키(KIS_APP_KEY/KIS_APP_SECRET 등) 못 찾음")
    dart_key = read_dart_key()                    # [V18.4] DART 공시 감시 키(없으면 자동 OFF)
    naver_id, naver_secret = read_naver_keys()    # [V21.4] 네이버 뉴스 검색(없으면 저녁 뉴스 OFF)
    gemini_key = read_gemini_key()                # [V21.4] Gemini 판정(없으면 헤드라인만)
    print(f"📡 감시 시작 — {args.interval}초 · 매크로 ON · 수급 {'ON' if kis_on else 'OFF'} · "
          f"DART공시 {'ON' if dart_key else 'OFF(키없음)'} · "
          f"저녁뉴스 {'ON' if (naver_id and naver_secret) else 'OFF(네이버키없음)'}"
          f"{'·AI' if gemini_key else '·헤드라인만'}")
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

            # [V25.1] 공휴일 감지 — 평일 장중(09:10~15:20)인데 KIS 거래대금 랭킹이 0건이면 휴장 추정.
            #   하드코딩 공휴일 리스트(매년 갱신·오류 위험) 대신 실데이터 자기교정. 정규장 신호만 스킵
            #   (저녁 브리핑·야간 미장은 그대로 — 다음 거래일 대비). 일시적 조회 실패면 다음 사이클 자동 복구.
            _mm0 = now.hour * 60 + now.minute
            if kis_on and (9 * 60 + 10) <= _mm0 <= (15 * 60 + 20):
                try:
                    _htok = kis_token(kis_key, kis_secret)
                    if _htok and len(_volume_rank(_htok, kis_key, kis_secret, top=5)) == 0:
                        print(f"[{stamp}] 휴장 추정(거래대금 랭킹 0건) — 정규장 신호 스킵")
                        time.sleep(max(60, args.interval))
                        continue
                except Exception as _hce:
                    print(f"[{stamp}] 휴장 감지 조회 오류(무시): {_hce}")

            # 1) 매크로 (코스피는 KIS 지수 우선 — yfinance 지연 버그 회피)
            _ct = kis_token(kis_key, kis_secret) if (kis_key and kis_secret) else None
            sev, mtext, mdetail, _dstate = compute_macro(_ct, kis_key, kis_secret)
            prev_sev = st.get("sev")
            # [V25.1] 부분 outage(지표 일부 None)면 sev가 튈 수 있어 → 직전 sev 유지·알림 억제.
            #   (예: WTI만 None → riskoff 풀려 가짜 '개선' 알림.) full outage("outage")는 sev=2 유지(방어).
            if _dstate == "partial":
                if prev_sev is not None:
                    print(f"[{stamp}] ⚠️ 지표 일부 조회 실패 — sev 판정 보류(직전 {prev_sev} 유지)")
                    sev = prev_sev
                elif sev < 1:                          # [V25.10] 콜드스타트+부분결측이면 초록(진입허용) 금지(보수)
                    print(f"[{stamp}] ⚠️ 첫 사이클 지표 일부 결측 — 보수적으로 sev {sev}→1")
                    sev = 1
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
                    # [V21.0] 리스크오프로 '전환'되면 보유 종목 손절라인 점검 경고 추가(오늘 -3% 크래시 교훈)
                    _hold = ("\n🚨 보유 종목 점검 — 리스크오프 전환! 손절 라인(−2%) 확인·비주도주 우선 정리 검토"
                             if sev >= 2 and prev_sev < 2 else "")
                    if send_telegram(token_tg, chat_id, f"{_mbadge}\n{icon}\n{mtext}\n{mdetail}{_hold}\n{stamp} KST"):
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
            # 🚨 [V25.5] 아침 비상 점검(08:00~08:15) — 밤사이 미국 급락 시 보유 점검 넛지
            try:
                check_morning_riskoff(now, st, token_tg, chat_id)
            except Exception as _mre:
                print("아침 비상 점검 오류:", _mre)
            # 📢 [V18.4] DART 실시간 공시 감시(07:00~17:00) — 호재 공시를 우리 엔진으로 교차검증해 진입후보 선정
            try:
                check_dart_disclosures(now, st, token_tg, chat_id, dart_key, kis_key, kis_secret, sev)
            except Exception as _dqe:
                print("DART 공시 감시 오류:", _dqe)
            # 🌙 [V21.4] 저녁 뉴스 시황 스캐너(17:00~22:00, 당일 1회) — 내일 주목 테마·대장주 브리핑
            try:
                check_evening_news(now, st, token_tg, chat_id, naver_id, naver_secret, gemini_key,
                                   kis_key, kis_secret)
            except Exception as _ene:
                print("저녁 뉴스 스캐너 오류:", _ene)
            print(f"[{stamp}] 매크로 sev={sev} {mtext}")
            for _dl in mdetail.split("\n"):           # 미국/한국 그룹을 들여쓰기해 한눈에 구분
                print(f"           {_dl}")

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
                        # [V25.30] 섹터별 60분 → '같은 유입섹터 하루 1회'(매시간 반복 스팸 해결)
                        if _mkt_hours and not _tour.get(key):
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
                    # [V21.3] 장전 예열 스캔(08:00~08:55) — NXT 프리마켓/예상체결가로 09시 갭 미리 포착
                    try:
                        check_premarket(tok, kis_key, kis_secret, now, st, token_tg, chat_id, _lineup, sev)
                    except Exception as _pme:
                        print("장전 예열 오류:", _pme)
                    # [V25.36] 시가배팅(강의 4강) — 09:00~09:10 장초반 갭·재료·시초수급 단기 공략
                    try:
                        check_opening_bet(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _obe2:
                        print("시가배팅 오류:", _obe2)
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
                    # [V25.37] 돌파매매(강의 5강) — 시장전역 전고점·라운드피겨·신고가 돌파(거래량2배+프로그램)
                    try:
                        check_breakout(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _bke:
                        print("돌파매매 오류:", _bke)
                    # [V25.40] 짝꿍·상따 관찰용(강의 7·8강) — 실시간 상한가/VI 못봐 관찰 경보만(매수신호 아님)
                    try:
                        check_limitup_follow(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _lue:
                        print("상따관찰 오류:", _lue)
                    try:
                        check_pair_trade(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _pte:
                        print("짝꿍관찰 오류:", _pte)
                    # [V22.7] 거래량 급증 서치(마감권) — 오늘 거래량>5일평균 2배 + 거래대금 상위 종목 알림
                    try:
                        check_vol_surge(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _vse:
                        print("거래량 급증 오류:", _vse)
                    # [V23.8] 내 관심종목 타점 검색기 — my_watch.json 종목 실시간 감시
                    try:
                        check_my_watch(tok, kis_key, kis_secret, now, st, token_tg, chat_id)
                    except Exception as _mwe:
                        print("내관심타점 오류:", _mwe)
                    # [V25.7] 눌림 타점 스캐너(시장 전역) — 저갭 장세 주력 무기(정배열 지지선 눌림)
                    try:
                        check_pullback_scan(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _pbe:
                        print("눌림타점 스캐너 오류:", _pbe)
                    # [V25.32] 과매도 낙주 반등 — 지수 급락일 전용(대형주 낙폭과대+수급유입+반등)
                    try:
                        check_oversold_bounce(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _obe:
                        print("과매도 낙주 오류:", _obe)
                    # [V25.33] 재료주 일시 투매 반등 — 당일청산(강한 재료주 급등 후 눌림 되돌림)
                    try:
                        check_material_washout(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _mwe2:
                        print("재료투매반등 오류:", _mwe2)
                    # [V25.29] 레인지(박스) 매매 — 박스장 전용(횡보 종목 하단 지지 반등)
                    try:
                        check_range_trade(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _rte:
                        print("레인지매매 오류:", _rte)
                    # [V25.20] 보유종목 관리 — my_holdings.json 매수평균 대비 손절/익절 알림
                    try:
                        check_holdings(tok, kis_key, kis_secret, now, st, token_tg, chat_id)
                    except Exception as _hde:
                        print("보유관리 오류:", _hde)
                    # [V24.0] 아침 갭상승 원인 역분석(09:03~09:12) — 브리핑 적중률 검증 + 원인 학습
                    try:
                        check_gap_analysis(tok, kis_key, kis_secret, now, st, token_tg, chat_id, gemini_key)
                    except Exception as _gae:
                        print("갭분석 오류:", _gae)
                    # [V25.38] 단기스윙 후보(강의 9강) — 15:00~15:25 수급연속+재료+상대강세+공급악재無
                    try:
                        check_swing_scan(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _swe:
                        print("단기스윙 오류:", _swe)
                    # [V20.0] 종가베팅 픽 — 장 마감 직전(15:05~15:22) 자동 선정·발송(대시보드 없이)
                    try:
                        check_dolpanty_pick(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev,
                                            gemini_key=gemini_key)
                    except Exception as _dpe:
                        print("종배픽 오류:", _dpe)
                    # [V23.0] 종배 진입 타이밍 리마인더(15:23~15:29 종가 동시호가)
                    try:
                        check_dolpanty_entry(tok, kis_key, kis_secret, now, st, token_tg, chat_id)
                    except Exception as _dee:
                        print("종배 진입알림 오류:", _dee)
                    # [V25.12 A] 종배 NXT 청산 알림 — 갭이 NXT서 나므로 목표 도달 시 9시 전 청산
                    try:
                        check_dolpanty_exit(tok, kis_key, kis_secret, now, st, token_tg, chat_id)
                    except Exception as _dxe:
                        print("종배 청산알림 오류:", _dxe)
                    # [V25.35] 시간외 단일가(강의 3강) — 17~18:30 실체결 지속 대장주(10분 델타 기반)
                    try:
                        check_afterhours(tok, kis_key, kis_secret, now, st, token_tg, chat_id, sev)
                    except Exception as _ahe:
                        print("시간외 단일가 오류:", _ahe)
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
        _tight = (((8 * 60) <= _km <= (15 * 60 + 22))          # [V21.3] 장전 예열(08:00~) 위해 08시부터 60초
                  or ((15 * 60 + 30) <= _km <= (15 * 60 + 50))
                  or ((16 * 60) <= _km <= (19 * 60 + 50)))   # 넥장 급등 타점 정확도 위해 야간도 60초
        _iv = 60 if _tight else args.interval
        time.sleep(max(30, _iv))


if __name__ == "__main__":
    main()
