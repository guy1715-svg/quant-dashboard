"""
kis_api.py — KIS Developers API 비동기 클라이언트 (V8.9 프로덕션)

사용 전 환경변수 또는 Streamlit secrets에 아래 키 설정 필요:
  KIS_APP_KEY    : KIS Developers 앱 키
  KIS_APP_SECRET : KIS Developers 앱 시크릿
  KIS_ACCOUNT_NO : 계좌번호 (예: 50123456-01)
  KIS_MOCK       : "true" → 모의투자 서버, 없거나 "false" → 실전

의존성:
  pip install aiohttp nest_asyncio
"""

from __future__ import annotations

import asyncio
import os
import time
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import nest_asyncio
import requests

# Streamlit asyncio 루프 충돌 방지
nest_asyncio.apply()

# ── 상수 ──────────────────────────────────────────────────────────────────────
REAL_BASE  = "https://openapi.koreainvestment.com:9443"
MOCK_BASE  = "https://openapivts.koreainvestment.com:29443"

# 캐시 TTL (초)
TTL_REALTIME  = 30    # 호가·체결·수급 (30초)
TTL_DAILY     = 300   # 일봉·기술지표  (5분)
TTL_FINANCIAL = 86400 # 재무·시총       (1일)

# Rate-limit: KIS 초당 20건 제한
RATE_LIMIT_PER_SEC = 18   # 여유 2건 확보
BACKOFF_BASE       = 1.0  # 지수 백오프 초기 대기(초)
BACKOFF_MAX        = 32.0 # 최대 대기
MAX_RETRY          = 5

# NXT (대체거래소) — 2026년 이후 운영
NXT_ELIGIBLE_MARKETS = {"KRX", "NXT"}   # NXT 체결 가능 시장 코드


# ── 토큰 관리 (스레드 안전 싱글턴) ──────────────────────────────────────────
class _TokenManager:
    _lock   = threading.Lock()
    _token  : Optional[str]  = None
    _expiry : Optional[float] = None   # Unix timestamp

    @classmethod
    def get(cls, app_key: str, app_secret: str, base_url: str) -> str:
        with cls._lock:
            if cls._token and cls._expiry and time.time() < cls._expiry - 60:
                return cls._token
            cls._token, cls._expiry = cls._issue(app_key, app_secret, base_url)
            return cls._token

    @staticmethod
    def _issue(app_key: str, app_secret: str, base_url: str) -> Tuple[str, float]:
        url  = f"{base_url}/oauth2/tokenP"
        body = {"grant_type": "client_credentials",
                "appkey": app_key, "appsecret": app_secret}
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data   = resp.json()
        token  = data["access_token"]
        expiry = time.time() + int(data.get("expires_in", 86400))
        return token, expiry


# ── 속도 제어 (토큰 버킷) ─────────────────────────────────────────────────────
class _RateLimiter:
    def __init__(self, rps: int = RATE_LIMIT_PER_SEC):
        self._sem       = asyncio.Semaphore(rps)
        self._interval  = 1.0 / rps
        self._last_call = 0.0

    async def acquire(self):
        async with self._sem:
            now  = asyncio.get_event_loop().time()
            wait = self._interval - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()


# ── 메인 클라이언트 ────────────────────────────────────────────────────────────
class KISClient:
    """
    비동기 KIS API 클라이언트.

    예시:
        client = KISClient()
        price  = await client.get_price("005930")
        inv    = await client.get_investor_trend("005930")
    """

    def __init__(self):
        self._app_key    = os.environ.get("KIS_APP_KEY", "")
        self._app_secret = os.environ.get("KIS_APP_SECRET", "")
        self._account    = os.environ.get("KIS_ACCOUNT_NO", "")
        self._mock       = os.environ.get("KIS_MOCK", "false").lower() == "true"
        self._base       = MOCK_BASE if self._mock else REAL_BASE
        self._limiter    = _RateLimiter()
        self._session: Optional[aiohttp.ClientSession] = None

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────────────

    def _token(self) -> str:
        return _TokenManager.get(self._app_key, self._app_secret, self._base)

    def _headers(self, tr_id: str, extra: Dict = {}) -> Dict:
        h = {
            "content-type":  "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token()}",
            "appkey":        self._app_key,
            "appsecret":     self._app_secret,
            "tr_id":         tr_id,
            "custtype":      "P",
        }
        h.update(extra)
        return h

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=50, ssl=False)
            timeout   = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(
                connector=connector, timeout=timeout
            )
        return self._session

    async def _get(
        self,
        path: str,
        tr_id: str,
        params: Dict,
        extra_headers: Dict = {},
    ) -> Dict:
        """지수 백오프 재시도 포함 GET 요청."""
        url     = self._base + path
        headers = self._headers(tr_id, extra_headers)
        delay   = BACKOFF_BASE
        session = await self._session_get()

        for attempt in range(1, MAX_RETRY + 1):
            await self._limiter.acquire()
            try:
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 429:           # Rate limit
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, BACKOFF_MAX)
                        continue
                    resp.raise_for_status()
                    data = await resp.json()
                    if data.get("rt_cd") != "0":
                        # KIS 애플리케이션 레벨 에러
                        raise ValueError(f"KIS error {data.get('msg_cd')}: {data.get('msg1')}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == MAX_RETRY:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
        raise RuntimeError(f"KIS GET {path} 실패 — {MAX_RETRY}회 재시도 초과")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ── 공개 API ──────────────────────────────────────────────────────────────

    async def get_price(self, ticker: str) -> Dict[str, Any]:
        """
        현재가·등락·거래량 조회
        Returns: {price, change_pct, volume, market_cap_bil, tradable_nxt}
        """
        data = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker},
        )
        o = data["output"]
        mktcap_bil = round(int(o.get("hts_avls", 0)) / 1e4, 0)   # 억 → 억원 (원단위: 억)

        # NXT 체결 가능 여부 — 대체거래소 시장 구분 코드 확인
        mrkt_div = o.get("rprs_mrkt_kor_name", "")
        tradable_nxt = any(nxt in mrkt_div for nxt in NXT_ELIGIBLE_MARKETS)

        return {
            "price":          int(o.get("stck_prpr", 0)),
            "change_pct":     float(o.get("prdy_ctrt", 0)),
            "volume":         int(o.get("acml_vol", 0)),
            "market_cap_bil": mktcap_bil,   # 억원
            "tradable_nxt":   tradable_nxt,
            "mrkt_div":       mrkt_div,
        }

    async def get_investor_trend(self, ticker: str, days: int = 5) -> Dict[str, Any]:
        """
        5일 누적 외인·기관 순매수 조회
        Returns: {foreign_net_5d, inst_net_5d}  (단위: 주)
        """
        end   = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=days + 4)).strftime("%Y%m%d")

        data = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            tr_id="FHKST01010900",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd":         ticker,
                "fid_begin_date":         start,
                "fid_end_date":           end,
                "fid_period_div_code":    "D",
            },
        )
        rows = data.get("output2", [])[:days]
        foreign_net = sum(int(r.get("frgn_ntby_qty", 0))  for r in rows)
        inst_net    = sum(int(r.get("orgn_ntby_qty", 0))   for r in rows)
        return {"foreign_net_5d": foreign_net, "inst_net_5d": inst_net}

    async def get_financial_summary(self, ticker: str) -> Dict[str, Any]:
        """
        영업이익·매출 YoY 조회 (연간)
        Returns: {operating_profit, revenue_yoy}
        """
        data = await self._get(
            "/uapi/domestic-stock/v1/finance/income-statement",
            tr_id="FHKST66430300",
            params={
                "fid_div_cls_code": "1",    # 연간
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": ticker,
            },
        )
        rows = data.get("output", [])
        if len(rows) < 2:
            return {"operating_profit": None, "revenue_yoy": None}

        try:
            op    = float(rows[0].get("bsop_prfi", 0) or 0)       # 최근 영업이익
            rev_t = float(rows[0].get("sale_account", 1) or 1)    # 최근 매출
            rev_p = float(rows[1].get("sale_account", 1) or 1)    # 전년 매출
            rev_yoy = (rev_t - rev_p) / rev_p if rev_p != 0 else 0
        except (TypeError, ValueError):
            return {"operating_profit": None, "revenue_yoy": None}

        return {"operating_profit": op, "revenue_yoy": rev_yoy}

    async def get_ohlcv(self, ticker: str, n_days: int = 60) -> List[Dict]:
        """
        일봉 OHLCV 조회
        Returns: list of {date, open, high, low, close, volume}
        """
        end   = datetime.today().strftime("%Y%m%d")
        start = (datetime.today() - timedelta(days=n_days + 30)).strftime("%Y%m%d")

        data = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id="FHKST03010100",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd":         ticker,
                "fid_input_date_1":       start,
                "fid_input_date_2":       end,
                "fid_period_div_code":    "D",
                "fid_org_adj_prc":        "0",
            },
        )
        rows = data.get("output2", [])
        result = []
        for r in rows:
            result.append({
                "date":   r.get("stck_bsop_date", ""),
                "open":   int(r.get("stck_oprc", 0)),
                "high":   int(r.get("stck_hgpr", 0)),
                "low":    int(r.get("stck_lwpr", 0)),
                "close":  int(r.get("stck_clpr", 0)),
                "volume": int(r.get("acml_vol",  0)),
            })
        return sorted(result, key=lambda x: x["date"])[-n_days:]

    # ── 배치 조회 (병렬) ──────────────────────────────────────────────────────

    async def batch_price(self, tickers: List[str]) -> Dict[str, Dict]:
        """여러 종목 현재가 병렬 조회"""
        tasks   = [self.get_price(t) for t in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = {}
        for t, r in zip(tickers, results):
            if isinstance(r, Exception):
                out[t] = {"error": str(r)}
            else:
                out[t] = r
        return out

    async def batch_investor(self, tickers: List[str]) -> Dict[str, Dict]:
        """여러 종목 수급 병렬 조회"""
        tasks   = [self.get_investor_trend(t) for t in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = {}
        for t, r in zip(tickers, results):
            if isinstance(r, Exception):
                out[t] = {"foreign_net_5d": 0, "inst_net_5d": 0}
            else:
                out[t] = r
        return out

    async def batch_financial(self, tickers: List[str]) -> Dict[str, Dict]:
        """여러 종목 재무 병렬 조회 (일 캐시 적용 권장)"""
        tasks   = [self.get_financial_summary(t) for t in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = {}
        for t, r in zip(tickers, results):
            if isinstance(r, Exception):
                out[t] = {"operating_profit": None, "revenue_yoy": None}
            else:
                out[t] = r
        return out


# ── 동기 래퍼 (Streamlit에서 asyncio.run() 대신 사용) ───────────────────────
def run_async(coro):
    """Streamlit 환경에서 비동기 코루틴을 동기로 실행 (nest_asyncio 적용 전제)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── 싱글턴 클라이언트 접근자 ──────────────────────────────────────────────────
_client_instance: Optional[KISClient] = None

def get_client() -> KISClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = KISClient()
    return _client_instance
