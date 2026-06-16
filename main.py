# ============================================================
# 퀀트 대시보드 — FastAPI 백엔드 (V1.0)
# 실행: uvicorn main:app --reload --port 8000
# 설치: pip install fastapi uvicorn httpx firebase-admin google-generativeai
# ============================================================

from __future__ import annotations

import os
import time
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── 로거 ──
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quant")

# ── 앱 인스턴스 ──
app = FastAPI(title="Quant Dashboard API", version="1.0.0")

# ── CORS 설정 ──
# 환경변수 ALLOWED_ORIGINS 에 콤마로 도메인 나열 (프로덕션)
# 미설정 시 개발 편의상 전체 허용
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
if _raw_origins == "*":
    _origins = ["*"]
else:
    _origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=(_origins != ["*"]),  # wildcard일 때 credentials 비활성화
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    expose_headers=["X-Process-Time"],
    max_age=600,  # preflight 캐시 10분
)

# ════════════════════════════════════════════════════════════
# 환경 변수
# ════════════════════════════════════════════════════════════
KIS_APP_KEY    = os.environ.get("KIS_APP_KEY", "")
KIS_APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
KIS_ACCT_NO    = os.environ.get("KIS_ACCT_NO", "")
KIS_ENABLED    = bool(KIS_APP_KEY)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "")
FIREBASE_CRED_PATH = os.environ.get("FIREBASE_CRED_PATH", "firebase-creds.json")

# ════════════════════════════════════════════════════════════
# Firebase 초기화 (싱글턴)
# ════════════════════════════════════════════════════════════
import firebase_admin
from firebase_admin import credentials as fb_credentials, db as fb_db

def _init_firebase():
    if firebase_admin._apps:
        return
    # 우선순위 1: 환경변수 FIREBASE_CRED_JSON (Render 배포용 — JSON 문자열)
    _cred_json_str = os.environ.get("FIREBASE_CRED_JSON", "")
    if _cred_json_str:
        import json as _json
        try:
            _cred_dict = _json.loads(_cred_json_str)
            cred = fb_credentials.Certificate(_cred_dict)
            firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
            log.info("Firebase 초기화 완료 (환경변수)")
            return
        except Exception as e:
            log.error(f"Firebase 환경변수 파싱 실패: {e}")
    # 우선순위 2: 로컬 파일 (개발용)
    if os.path.exists(FIREBASE_CRED_PATH):
        cred = fb_credentials.Certificate(FIREBASE_CRED_PATH)
        firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
        log.info("Firebase 초기화 완료 (파일)")
        return
    log.warning("Firebase 인증 정보 없음 — DB 기능 비활성화")

_init_firebase()

def _fb_ref(path: str):
    if not firebase_admin._apps:
        raise HTTPException(503, "Firebase 미연결")
    return fb_db.reference(path)

# ════════════════════════════════════════════════════════════
# KIS API 헬퍼
# ════════════════════════════════════════════════════════════
_KIS_BASE         = "https://openapi.koreainvestment.com:9443"
_KIS_URL_TOKEN    = f"{_KIS_BASE}/oauth2/tokenP"
_KIS_URL_PRICE    = f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
_KIS_URL_INVESTOR = f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-investor"

_kis_token_cache: dict[str, Any] = {}

async def _kis_token() -> str:
    now = time.time()
    if _kis_token_cache.get("exp", 0) > now:
        return _kis_token_cache["token"]
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(_KIS_URL_TOKEN, json={
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        })
        res.raise_for_status()
        data = res.json()
    token = data["access_token"]
    _kis_token_cache.update({"token": token, "exp": now + 79200})  # 22h
    return token

async def _kis_get(url: str, params: dict) -> dict:
    token = await _kis_token()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": params.pop("tr_id"),
        "custtype": "P",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.get(url, headers=headers, params=params)
        res.raise_for_status()
    return res.json()

# ════════════════════════════════════════════════════════════
# Gemini AI 헬퍼
# ════════════════════════════════════════════════════════════
import google.generativeai as genai

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    _gemini_model = genai.GenerativeModel("gemini-1.5-flash")
else:
    _gemini_model = None

async def _gemini_generate(prompt: str) -> str:
    if not _gemini_model:
        raise HTTPException(503, "Gemini API 키 미설정")
    for attempt in range(4):
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: _gemini_model.generate_content(prompt)
            )
            return resp.text
        except Exception as e:
            if attempt == 3:
                raise HTTPException(502, f"Gemini 오류: {e}")
            wait = min(10 * (2 ** attempt), 120)
            await asyncio.sleep(wait)
    return ""

# ════════════════════════════════════════════════════════════
# 스캐너 — C1~C6 조건 평가 (yfinance 기반)
# ════════════════════════════════════════════════════════════
BLACKLIST = {"002790"}

def _calc_adx(high, low, close, n=14) -> float:
    import numpy as np
    h, l, c = [list(x) for x in (high, low, close)]
    tr_list, pdm_list, ndm_list = [], [], []
    for i in range(1, len(c)):
        tr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        pdm = max(h[i]-h[i-1], 0) if h[i]-h[i-1] > l[i-1]-l[i] else 0
        ndm = max(l[i-1]-l[i], 0) if l[i-1]-l[i] > h[i]-h[i-1] else 0
        tr_list.append(tr); pdm_list.append(pdm); ndm_list.append(ndm)
    def ema(s, p):
        r = [sum(s[:p])/p]
        for v in s[p:]: r.append(r[-1]*(p-1)/p + v)
        return r
    atr = ema(tr_list, n); pdi = ema(pdm_list, n); ndi = ema(ndm_list, n)
    dx = [100*abs(p-d)/(p+d+1e-9) for p, d in zip(pdi[-len(atr):], ndi[-len(atr):])]
    return float(np.mean(dx[-n:]))

def _calc_cmf(high, low, close, volume, n=20) -> float:
    import numpy as np
    mfv = [(( (c-l)-(h-c) ) / (h-l+1e-9)) * v
           for h, l, c, v in zip(high, low, close, volume)]
    return float(sum(mfv[-n:]) / (sum(volume[-n:]) + 1e-9))

_NAME_MAP = {
    "005930": "삼성전자", "000660": "SK하이닉스", "042700": "한미반도체",
    "012450": "한화에어로스페이스", "329180": "HD현대중공업", "247540": "에코프로비엠",
    "373220": "LG에너지솔루션", "196170": "알테오젠", "122630": "KODEX 레버리지",
    "069500": "KODEX 200", "114800": "KODEX 인버스", "005380": "현대차",
    "035420": "NAVER", "035720": "카카오", "051910": "LG화학",
}

async def _scan_one(ticker: str) -> dict | None:
    if ticker in BLACKLIST:
        return None
    try:
        import yfinance as yf
        df = await asyncio.get_event_loop().run_in_executor(
            None, lambda: yf.download(f"{ticker}.KS", period="6mo", interval="1d",
                                       auto_adjust=True, progress=False)
        )
        if df is None or len(df) < 60:
            return None

        hi = df["High"].tolist()
        lo = df["Low"].tolist()
        cl = df["Close"].tolist()
        vo = df["Volume"].tolist()

        adx   = _calc_adx(hi, lo, cl)
        cmf20 = _calc_cmf(hi, lo, cl, vo)

        ma5  = sum(cl[-5:])  / 5
        ma20 = sum(cl[-20:]) / 20
        ma60 = sum(cl[-60:]) / 60
        price = cl[-1]
        ma5_diff = (price - ma5) / ma5 * 100

        # RSI-14
        gains, losses = [], []
        for i in range(-14, 0):
            d = cl[i] - cl[i-1]
            gains.append(max(d, 0)); losses.append(max(-d, 0))
        ag, al = sum(gains)/14, sum(losses)/14
        rsi = 100 - 100/(1 + ag/(al+1e-9))

        # C1 추세: ADX≥20 (완화) + 단기 상승 (ma5 > ma20)
        c1 = adx >= 20 and ma5 > ma20

        # C2 눌림목: MA5이격 -5%~+5% (완화)
        c2 = -5 <= ma5_diff <= 5

        # C3 재무: yfinance 모드 간이 통과
        c3 = True

        # C4 수급: CMF20 > 0
        c4 = cmf20 > 0

        # C5 모멘텀: RSI 35~75 (완화)
        c5 = 35 <= rsi <= 75

        # C6 눌림목: MA5이격 -3%~+3%
        c6 = -3 <= ma5_diff <= 3

        score = (
            (25 if c3 else 0) +
            (30 if c4 else 0) +
            (25 if c5 else 0) +
            (20 if c6 else 0)
        )

        all6 = c1 and c2 and c3 and c4 and c5 and c6

        # C1+C2 하드필터
        if not (c1 and c2):
            return None

        grade = "A" if all6 and score >= 70 else "B"

        # B등급도 표시 (score >= 50 이상이면 반환)
        if score < 50:
            return None

        return {
            "ticker": ticker,
            "name": _NAME_MAP.get(ticker, ticker),
            "price": round(price, 0),
            "ma5_diff": round(ma5_diff, 2),
            "adx": round(adx, 2),
            "cmf": round(cmf20, 3),
            "rsi": round(rsi, 1),
            "score": score,
            "grade": grade,
            "conditions": {"c1": c1, "c2": c2, "c3": c3, "c4": c4, "c5": c5, "c6": c6},
        }
    except Exception as e:
        log.warning(f"스캔 오류 {ticker}: {e}")
        return None

# ════════════════════════════════════════════════════════════
# Pydantic 요청 모델
# ════════════════════════════════════════════════════════════
class TradeRequest(BaseModel):
    ticker: str
    name: str
    action: str          # "매수" | "매도"
    price: float
    qty: int
    memo: str = ""

class AnalyzeRequest(BaseModel):
    ticker: str
    name: str
    price: float
    adx: float
    cmf: float
    rsi: float
    score: int
    grade: str

# ════════════════════════════════════════════════════════════
# API 라우터
# ════════════════════════════════════════════════════════════

# ── Health ──────────────────────────────────────────────────
@app.get("/")
async def root():
    return {"status": "ok", "version": "1.0.0", "kis": KIS_ENABLED}


# ── 1. 실시간 주가 ──────────────────────────────────────────
# JS 호출 예시:
# const res = await fetch('/api/market/quote/005930');
# const data = await res.json();  // { ticker, price, change_pct, volume, ... }
@app.get("/api/market/quote/{ticker}")
async def get_quote(ticker: str):
    if not KIS_ENABLED:
        # KIS 없을 때 yfinance 폴백
        try:
            import yfinance as yf
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(
                None, lambda: yf.Ticker(f"{ticker}.KS").fast_info
            )
            return {
                "ticker": ticker,
                "price": info.last_price,
                "prev_close": info.previous_close,
                "change_pct": round((info.last_price - info.previous_close) / info.previous_close * 100, 2),
                "volume": info.three_month_average_volume,
                "source": "yfinance",
            }
        except Exception as e:
            raise HTTPException(502, f"시세 조회 실패: {e}")

    try:
        data = await _kis_get(_KIS_URL_PRICE, {
            "tr_id": "FHKST01010100",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
        })
        out = data.get("output", {})
        price = int(out.get("stck_prpr", 0))
        prev  = int(out.get("stck_sdpr", 0))
        return {
            "ticker": ticker,
            "price": price,
            "prev_close": prev,
            "change_pct": round((price - prev) / (prev or 1) * 100, 2),
            "volume": int(out.get("acml_vol", 0)),
            "high": int(out.get("stck_hgpr", 0)),
            "low":  int(out.get("stck_lwpr", 0)),
            "source": "kis",
        }
    except Exception as e:
        raise HTTPException(502, str(e))


# ── 2. 스캐너 결과 ──────────────────────────────────────────
@app.get("/api/scanner/results")
async def get_scanner(
    watchlist: str = "005930,000660,042700,012450,329180,247540,373220,196170,112610"
):
    tickers = [t.strip() for t in watchlist.split(",") if t.strip()]
    tasks   = [_scan_one(t) for t in tickers]
    results = await asyncio.gather(*tasks)
    hits    = [r for r in results if r is not None]
    hits.sort(key=lambda x: x["score"], reverse=True)
    return {
        "count": len(hits),
        "scanned": len(tickers),
        "timestamp": datetime.now().isoformat(),
        "results": hits,
    }


# ── 3. 페이퍼 트레이딩 ──────────────────────────────────────
@app.post("/api/trade/paper")
async def paper_trade(req: TradeRequest, background_tasks: BackgroundTasks):
    amount = req.price * req.qty
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    trade_record = {
        "종목코드":  req.ticker,
        "종목명":    req.name,
        "매매":      req.action,
        "순체결가":  req.price,
        "수량":      req.qty,
        "금액":      amount,
        "메모":      req.memo,
        "일시":      now_str,
    }

    def _write():
        try:
            ref = _fb_ref("/quant_trades")
            ref.push(trade_record)

            acct_ref = _fb_ref("/quant_account")
            acct = acct_ref.get() or {
                "cash": 10_000_000, "positions": [], "peak": 10_000_000
            }

            if req.action == "매수":
                acct["cash"] = acct.get("cash", 0) - amount
                positions = acct.get("positions", [])
                matched = next((p for p in positions if p["종목코드"] == req.ticker), None)
                if matched:
                    total_qty = matched["수량"] + req.qty
                    matched["평균단가"] = (matched["평균단가"] * matched["수량"] + req.price * req.qty) / total_qty
                    matched["수량"] = total_qty
                else:
                    positions.append({"종목코드": req.ticker, "종목명": req.name,
                                      "수량": req.qty, "평균단가": req.price})
                acct["positions"] = positions

            elif req.action == "매도":
                acct["cash"] = acct.get("cash", 0) + amount
                acct["positions"] = [
                    p for p in acct.get("positions", [])
                    if p["종목코드"] != req.ticker
                ]

            acct_ref.set(acct)
        except Exception as e:
            log.error(f"Firebase 기록 오류: {e}")

    background_tasks.add_task(_write)

    return {
        "status": "ok",
        "message": f"{req.action} 주문 접수 — {req.name} {req.qty}주 @ {req.price:,.0f}원",
        "amount": amount,
        "timestamp": now_str,
    }


# ── 4. AI 종목 분석 ─────────────────────────────────────────
@app.post("/api/ai/analyze")
async def ai_analyze(req: AnalyzeRequest):
    prompt = f"""
당신은 퀀트 트레이딩 전문가입니다. 아래 데이터를 기반으로 종목을 분석하세요.

종목: {req.name} ({req.ticker})
현재가: {req.price:,.0f}원
ADX: {req.adx} (25 이상이면 추세 강함)
CMF20: {req.cmf} (양수면 자금 유입)
RSI14: {req.rsi} (70 초과 과매수 / 30 미만 과매도)
V8.9 스코어: {req.score}/100 ({req.grade}등급)

다음 3가지를 각 2~3문장으로 간결하게 작성하세요:
1. 기술적 현황 요약
2. 단기 리스크 요인
3. 매매 전략 제안 (진입/관망/회피)
"""
    text = await _gemini_generate(prompt)
    return {
        "ticker": req.ticker,
        "name": req.name,
        "analysis": text,
        "model": "gemini-1.5-flash",
        "timestamp": datetime.now().isoformat(),
    }


# ── 5. 계좌 조회 ────────────────────────────────────────────
@app.get("/api/account")
async def get_account():
    try:
        loop = asyncio.get_event_loop()
        acct = await loop.run_in_executor(None, lambda: _fb_ref("/quant_account").get())
        if not acct:
            return {"cash": 10_000_000, "positions": [], "peak": 10_000_000}
        return acct
    except Exception as e:
        raise HTTPException(502, str(e))


# ── 6. 거래 일지 조회 ───────────────────────────────────────
@app.get("/api/trades")
async def get_trades(limit: int = 50):
    try:
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(None, lambda: _fb_ref("/quant_trades").get())
        if not raw:
            return {"count": 0, "trades": []}
        trades = list(raw.values()) if isinstance(raw, dict) else raw
        trades.sort(key=lambda x: x.get("일시", ""), reverse=True)
        return {"count": len(trades), "trades": trades[:limit]}
    except Exception as e:
        raise HTTPException(502, str(e))


# ════════════════════════════════════════════════════════════
# 진입점
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
