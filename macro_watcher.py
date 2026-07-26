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


def send_telegram(token, chat_id, text):
    try:
        requests.get(f"https://api.telegram.org/bot{token}/sendMessage",
                     params={"chat_id": chat_id, "text": text}, timeout=8)
        return True
    except Exception as e:
        print("텔레그램 전송 실패:", e); return False


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
    if riskoff or (nq is not None and nq <= NQ_BLOCK):
        sev, text = 2, "🔴 리스크오프 · 신규매수 차단"
    elif (nq is not None and nq >= NQ_GO) and semi_sync:
        sev, text = 0, "🟢 진입 허용 (매크로 3대 양호)"
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


def pension_codes():
    try:
        with open(PENSION_FILE, encoding="utf-8") as f:
            return {r.get("code") for r in json.load(f).get("records", [])}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300, help="체크 주기(초), 기본 300=5분")
    ap.add_argument("--notify-worse", action="store_true", help="매크로 악화 시에도 알림")
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
    print(f"📡 감시 시작 — {args.interval}초 · 매크로 ON · 수급(전조/A급) {'ON' if kis_on else 'OFF'}")
    send_telegram(token_tg, chat_id,
                  f"📡 감시 시작 — 국면 개선·전조·A급 알림 대기중\n수급 감시 {'ON' if kis_on else 'OFF(KIS키 없음)'}")

    while True:
        try:
            st = load_state()
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
            stamp = now.strftime("%m/%d %H:%M")

            # 1) 매크로
            sev, mtext, mdetail = compute_macro()
            prev_sev = st.get("sev")
            if prev_sev is not None and sev != prev_sev:
                if sev < prev_sev or args.notify_worse:
                    icon = "📈 매크로 개선!" if sev < prev_sev else "📉 매크로 악화"
                    send_telegram(token_tg, chat_id, f"{icon}\n{mtext}\n{mdetail}\n{stamp} KST")
            st["sev"] = sev
            print(f"[{stamp}] 매크로 sev={sev} {mtext} | {mdetail}")

            # [1단계] 웹 속보판용 스냅샷 — 매크로는 항상, 수급/A급은 KIS ON일 때 채운다.
            snap = {
                "updated": stamp,
                "updated_ts": int(now.timestamp()),
                "kis_on": kis_on,
                "macro": {"sev": sev, "text": mtext, "detail": mdetail},
                "indicators": compute_indicators(),   # 환율·VIX·지수 세부
                "supply": None,
                "ace": [],
                "top_pick": None,                     # 원톱 픽(수급 최상위 종목)
            }

            # 2)/3) 수급 전조·A급 (KIS 있을 때 + 매크로가 리스크오프 아닐 때만 유의미)
            if kis_on:
                tok = kis_token(kis_key, kis_secret)
                if tok:
                    secs = sector_moneyflow(tok, kis_key, kis_secret)
                    rows = sorted(secs.items(), key=lambda kv: kv[1]["net"], reverse=True)
                    inflow = rows[0] if rows else None
                    outflow = rows[-1] if rows else None
                    # 전조 시그널
                    if (inflow and outflow and inflow[0] != outflow[0]
                            and inflow[1]["net"] > 0 and outflow[1]["net"] < 0 and sev != 2):
                        key = f"{outflow[0]}>{inflow[0]}"
                        if st.get("tour_key") != key:
                            send_telegram(token_tg, chat_id,
                                          f"🚀 전조 시그널!\n자금 {outflow[0]} 이탈 → {inflow[0]} 유입\n"
                                          f"유입 {inflow[1]['net']/1e8:,.0f}억 · {stamp} KST\n폭등 前 선취 후보 — 대시보드 확인")
                            st["tour_key"] = key
                    # A급(유입 종목 ∩ 연기금)
                    pens = pension_codes()
                    ace_now = []
                    for sname, info in secs.items():
                        if info["net"] <= 0:
                            continue
                        for s in info["stocks"]:
                            if s["amt"] and s["amt"] > 0 and s["code"] in pens:
                                ace_now.append((s["code"], s["name"], sname, s["amt"]))
                    prev_ace = set(st.get("ace", []))
                    new_ace = [a for a in ace_now if a[0] not in prev_ace]
                    if new_ace and sev != 2:
                        lines = "\n".join(f"• {n} ({sn}) +{amt/1e8:,.0f}억" for _c, n, sn, amt in new_ace)
                        send_telegram(token_tg, chat_id,
                                      f"🥇 A급 종목 신규 포착!\n{lines}\n{stamp} KST\n(자금유입 × 연기금 겹침)")
                    st["ace"] = [a[0] for a in ace_now]
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

            _snap_str = json.dumps(snap, ensure_ascii=False)
            save_snapshot(snap)
            push_snapshot_github(_snap_str)   # GitHub 'data' 브랜치 업로드(토큰 있을 때만)
            save_state(st)
        except Exception as e:
            print("체크 오류:", e)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
