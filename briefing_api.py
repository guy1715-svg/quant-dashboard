"""
briefing_api.py — Morning Briefing Dashboard 초경량 백엔드 (FastAPI)
─────────────────────────────────────────────────────────────────────────────
설계 원칙
  • No DB : 호출 시점에 즉시 파싱해 JSON 반환(인메모리, 상태 없음).
  • 강력한 폴백 : 소스별 try/except로 격리 — 한 항목이 죽어도 나머지는 정상 반환,
                  실패 항목은 {"value": None, "status": "N/A"}.
  • CORS * : 로컬 HTML의 fetch()가 막히지 않도록 Allow-Origins:* 완전 개방.
  • 소스 : USD/KRW=네이버 시장지표, 나스닥선물·WTI=yfinance,
           반도체 피어=yfinance(본장/전일종가 대비), 뉴스=네이버 증권 속보.

실행 :  uvicorn briefing_api:app --host 0.0.0.0 --port 8000 --reload
호출 :  GET http://localhost:8000/api/dashboard
"""
from __future__ import annotations

import concurrent.futures as _cf
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import requests
try:
    import yfinance as yf
except Exception:
    yf = None
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

app = FastAPI(title="Morning Briefing API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
       "Referer": "https://finance.naver.com/"}

_NA = lambda: {"value": None, "change": None, "status": "N/A"}


# ═════════════════════════════════════════════════════════════════════════════
# KIS(한국투자증권) 정밀 시세 — 국내/해외. 키는 환경변수에서 (없으면 yfinance 폴백).
#   export KIS_APP_KEY=...   export KIS_APP_SECRET=...
# 토큰: 인메모리 캐시 + 발급 1분1회(EGW00133) 쿨다운 보호.
# ═════════════════════════════════════════════════════════════════════════════
import os as _os
import time as _time

_KIS_BASE = "https://openapi.koreainvestment.com:9443"
_KIS_KEY = _os.environ.get("KIS_APP_KEY", "")
_KIS_SECRET = _os.environ.get("KIS_APP_SECRET", "")
_KIS_TOKEN = {"tok": None, "exp": 0.0, "cooldown": 0.0}


def _kis_enabled():
    return bool(_KIS_KEY and _KIS_SECRET)


def _kis_token():
    if not _kis_enabled():
        return None
    _now = _time.time()
    if _KIS_TOKEN["tok"] and _KIS_TOKEN["exp"] > _now:
        return _KIS_TOKEN["tok"]
    if _KIS_TOKEN["cooldown"] > _now:      # 발급 제한 보호(재요청 폭주 방지)
        return None
    try:
        r = requests.post(f"{_KIS_BASE}/oauth2/tokenP", timeout=8, json={
            "grant_type": "client_credentials", "appkey": _KIS_KEY, "appsecret": _KIS_SECRET})
        j = r.json()
        _tok = j.get("access_token")
        if _tok:
            _ttl = int(float(j.get("expires_in", 86400))) - 60
            _KIS_TOKEN.update(tok=_tok, exp=_now + max(60, _ttl), cooldown=0.0)
            return _tok
        _KIS_TOKEN["cooldown"] = _now + 60   # EGW00133 등 → 60초 대기
    except Exception:
        _KIS_TOKEN["cooldown"] = _now + 30
    return None


def _kis_headers(tr_id):
    return {"authorization": f"Bearer {_kis_token()}", "appkey": _KIS_KEY,
            "appsecret": _KIS_SECRET, "tr_id": tr_id, "custtype": "P"}


def kis_domestic(code):
    """국내주식 현재가(삼성증권/거래소 기준 정확). 실패 시 None."""
    if not _kis_token():
        return None
    try:
        r = requests.get(f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                         headers=_kis_headers("FHKST01010100"),
                         params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=5)
        o = r.json().get("output", {})
        _p = float(str(o.get("stck_prpr", 0)).replace(",", "") or 0)
        _c = float(str(o.get("prdy_ctrt", 0)).replace(",", "") or 0)
        return _ok(round(_p, 2), round(_c, 2)) if _p > 0 else None
    except Exception:
        return None


def kis_overseas(excd, symb):
    """해외주식 현재가(NAS/NYS). 실패 시 None."""
    if not _kis_token():
        return None
    try:
        r = requests.get(f"{_KIS_BASE}/uapi/overseas-price/v1/quotations/price",
                         headers=_kis_headers("HHDFS00000300"),
                         params={"AUTH": "", "EXCD": excd, "SYMB": symb}, timeout=5)
        o = r.json().get("output", {})
        _p = float(str(o.get("last", 0)).replace(",", "") or 0)
        _c = float(str(o.get("rate", 0)).replace(",", "") or 0)   # 등락률
        return _ok(round(_p, 2), round(_c, 2)) if _p > 0 else None
    except Exception:
        return None


def _ok(value, change=None):
    return {"value": value, "change": change, "status": "OK"}


# ── 소스 1: 네이버 시장지표(정확한 USD/KRW) ──────────────────────────────────
def naver_usdkrw():
    try:
        r = requests.get("https://api.stock.naver.com/marketindex/exchange/FX_USDKRW",
                         headers=_UA, timeout=4)
        j = r.json()
        _price = float(str(j.get("closePrice", "")).replace(",", ""))
        _chg = j.get("fluctuationsRatio")
        _chg = float(str(_chg).replace(",", "").replace("%", "")) if _chg not in (None, "") else None
        # 등락 방향(하락 시 부호 보정)
        if _chg is not None and str(j.get("fluctuationsType", {}).get("code", "")) in ("2", "5"):
            _chg = -abs(_chg)
        return _ok(round(_price, 2), _chg) if _price > 0 else _NA()
    except Exception:
        return _NA()


# ── 소스 2: yfinance 단건 시세(현재가 + 전일종가 대비 등락%) ─────────────────
def yf_quote(sym):
    if yf is None:
        return _NA()
    try:
        _fi = yf.Ticker(sym).fast_info
        _last = float(_fi.last_price)
        _prev = float(_fi.previous_close)
        if _last > 0 and _prev > 0:
            return _ok(round(_last, 2), round((_last / _prev - 1) * 100, 2))
    except Exception:
        pass
    # 폴백: 최근 2일 종가
    try:
        _h = yf.Ticker(sym).history(period="5d")["Close"].dropna()
        if len(_h) >= 2:
            _last, _prev = float(_h.iloc[-1]), float(_h.iloc[-2])
            return _ok(round(_last, 2), round((_last / _prev - 1) * 100, 2))
    except Exception:
        pass
    return _NA()


# ── 소스 2-b: 네이버 증권 해외주식(키 불필요·안정적) ────────────────────────
def naver_worldstock(reuters):
    """네이버 해외주식 현재가. reuters 예: 'MU.O'(나스닥)/'TSM.N'(뉴욕). 실패 시 None."""
    try:
        r = requests.get(f"https://api.stock.naver.com/stock/{reuters}/basic",
                         headers=_UA, timeout=4)
        j = r.json()
        _p = float(str(j.get("closePrice", "")).replace(",", "") or 0)
        _rat = j.get("fluctuationsRatio")
        _chg = float(str(_rat).replace(",", "").replace("%", "")) if _rat not in (None, "") else None
        # 부호: 전일대비 값 or 등락코드(4=하한,5=하락)로 하락 판정
        _cmp = str(j.get("compareToPreviousClosePrice", ""))
        _code = str((j.get("compareToPreviousPrice") or {}).get("code", ""))
        if _chg is not None and (_cmp.startswith("-") or _code in ("4", "5")):
            _chg = -abs(_chg)
        return _ok(round(_p, 2), _chg) if _p > 0 else None
    except Exception:
        return None


# ── 소스 3: 네이버 증권 속보 헤드라인 ────────────────────────────────────────
def naver_news(n=5):
    if BeautifulSoup is None:
        return []
    try:
        r = requests.get("https://finance.naver.com/news/mainnews.naver", headers=_UA, timeout=4)
        r.encoding = "euc-kr"
        _soup = BeautifulSoup(r.text, "lxml")
        _out = []
        for _a in _soup.select(".mainNewsList a.tit, .mainNewsList dd.articleSubject a, ul.newsList a"):
            _t = _a.get_text(strip=True)
            if _t and len(_t) > 6 and _t not in _out:
                _out.append(_t)
            if len(_out) >= n:
                break
        return _out
    except Exception:
        return []


# ── 피어그룹: (표시명, yfinance심볼, KIS힌트, 네이버reuters) ──────────────────
#   KIS힌트: ("KR", 국내코드) or (해외거래소 NAS/NYS, 심볼)
#   네이버 reuters: 나스닥=.O / 뉴욕=.N / 국내=None(KIS 사용)
_PEERS = [
    ("SK하이닉스", "000660.KS", ("KR", "000660"), None),
    ("마이크론",   "MU",        ("NAS", "MU"),   "MU.O"),
    ("TSMC",      "TSM",       ("NYS", "TSM"),  "TSM.N"),   # TSMC ADR = NYSE
    ("AMD",       "AMD",       ("NAS", "AMD"),  "AMD.O"),
    ("인텔",       "INTC",      ("NAS", "INTC"), "INTC.O"),
    ("샌디스크",    "SNDK",      ("NAS", "SNDK"), "SNDK.O"),
]


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return _NA()


def peer_quote(yf_sym, kis_hint, naver_reuters):
    """정확도 우선 3중 폴백: KIS(삼성증권 기준) → 네이버 해외주식 → yfinance."""
    _mkt, _sym = kis_hint
    # 1) KIS
    _r = kis_domestic(_sym) if _mkt == "KR" else kis_overseas(_mkt, _sym)
    if _r:
        _r["src"] = "KIS"
        return _r
    # 2) 네이버(해외만)
    if naver_reuters:
        _n = naver_worldstock(naver_reuters)
        if _n:
            _n["src"] = "naver"
            return _n
    # 3) yfinance
    _y = yf_quote(yf_sym)
    _y["src"] = "yfinance" if _y.get("status") == "OK" else "N/A"
    return _y


@app.get("/api/dashboard")
def dashboard():
    """3개 존을 한 번에 반환. 각 항목은 개별 격리 — 일부 실패해도 나머지 정상."""
    with _cf.ThreadPoolExecutor(max_workers=12) as ex:
        f_krw = ex.submit(_safe, naver_usdkrw)
        f_nq = ex.submit(_safe, yf_quote, "NQ=F")     # 나스닥 선물
        f_wti = ex.submit(_safe, yf_quote, "CL=F")    # WTI
        f_k200 = ex.submit(_safe, yf_quote, "069500.KS")  # KOSPI200 야간선물 프록시(KODEX200)
        f_news = ex.submit(naver_news, 5)
        f_peer = {nm: ex.submit(_safe, peer_quote, ysym, khint, nrt)
                  for nm, ysym, khint, nrt in _PEERS}

        zone1 = {
            "usdkrw":  f_krw.result(),
            "nasdaq_fut": f_nq.result(),
            "wti":     f_wti.result(),
            "kospi200_fut": f_k200.result(),   # ⚠️ KODEX200 프록시(정식 야간선물은 KIS 야간선물 TR 필요)
        }
        zone2 = [{"name": nm, "symbol": ysym, **f_peer[nm].result()}
                 for nm, ysym, khint, nrt in _PEERS]
        zone3 = f_news.result() or []

    return {"zone1": zone1, "zone2": zone2, "zone3": zone3,
            "kis": _kis_enabled()}


@app.get("/api/health")
def health():
    return {"ok": True, "yfinance": yf is not None, "bs4": BeautifulSoup is not None,
            "kis_enabled": _kis_enabled(), "kis_token": bool(_kis_token())}


# ── 대시보드 HTML을 같은 서버에서 서빙 → file:// / CORS 문제 원천 차단 ──────────
from fastapi.responses import FileResponse, HTMLResponse   # noqa: E402


@app.get("/")
def index():
    _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                       "morning_briefing_dashboard.html")
    if _os.path.exists(_p):
        return FileResponse(_p)
    return HTMLResponse("<h3 style='color:#fff;background:#111;padding:20px'>"
                        "morning_briefing_dashboard.html 을 briefing_api.py와 같은 폴더에 두세요.</h3>")
