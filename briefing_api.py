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


# ── 병렬 취합(각 항목 격리) ──────────────────────────────────────────────────
_PEERS = [("SK하이닉스", "000660.KS"), ("마이크론", "MU"), ("TSMC", "TSM"),
          ("AMD", "AMD"), ("인텔", "INTC"), ("샌디스크", "SNDK")]


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return _NA()


@app.get("/api/dashboard")
def dashboard():
    """3개 존을 한 번에 반환. 각 항목은 개별 격리 — 일부 실패해도 나머지 정상."""
    with _cf.ThreadPoolExecutor(max_workers=12) as ex:
        f_krw = ex.submit(_safe, naver_usdkrw)
        f_nq = ex.submit(_safe, yf_quote, "NQ=F")     # 나스닥 선물
        f_wti = ex.submit(_safe, yf_quote, "CL=F")    # WTI
        f_k200 = ex.submit(_safe, yf_quote, "069500.KS")  # KOSPI200 야간선물 프록시(KODEX200)
        f_news = ex.submit(naver_news, 5)
        f_peer = {nm: ex.submit(_safe, yf_quote, sym) for nm, sym in _PEERS}

        zone1 = {
            "usdkrw":  f_krw.result(),
            "nasdaq_fut": f_nq.result(),
            "wti":     f_wti.result(),
            "kospi200_fut": f_k200.result(),   # ⚠️ KODEX200 프록시(정식 야간선물은 KIS TR 필요)
        }
        zone2 = [{"name": nm, "symbol": sym, **f_peer[nm].result()}
                 for nm, sym in _PEERS]
        zone3 = f_news.result() or []

    return {"zone1": zone1, "zone2": zone2, "zone3": zone3}


@app.get("/api/health")
def health():
    return {"ok": True, "yfinance": yf is not None, "bs4": BeautifulSoup is not None}
