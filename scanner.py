"""
scanner.py — V8.9 단기 스윙 스캐너 엔진 (KIS API 비동기 버전)

6대 조건 (AND):
  cond1: 시총 5,000억 ~ 3조  (KIS get_price → market_cap_bil)
  cond2: ATR14 / 현재가 ≥ 3.5%  (OHLCV 연산)
  cond3: 영업이익 흑자 OR 매출 YoY ≥ 20%  (KIS get_financial_summary)
  cond4: 외인 & 기관 5일 쌍끌이 순매수  (KIS get_investor_trend)
  cond5: 5거래일 누적 수익률 ≥ 15%  (OHLCV 연산)
  cond6: 당일 거래량 < 최근 20일 최대 거래량 × 30%  (OHLCV 연산)
  NXT   : tradable_nxt 플래그 사전 검증 (주문 라우팅 에러 방지)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from kis_api import KISClient, get_client, run_async, TTL_DAILY, TTL_FINANCIAL, TTL_REALTIME

# ── 캐시 상수 (통합) ──────────────────────────────────────────────────────────
GLOBAL_CACHE_TTL = 300        # 기본 5분
FINANCIAL_CACHE_TTL = 86400   # 재무 1일


# ── 결과 데이터클래스 ─────────────────────────────────────────────────────────
@dataclass
class ScanResult:
    ticker:        str
    name:          str
    price:         int
    change_pct:    float
    market_cap_bil: float
    atr_ratio:     float    # ATR14 / price (%)
    cum5_ret:      float    # 5일 누적 수익률 (%)
    vol_ratio:     float    # 당일 거래량 / 20일 최대 거래량 (%)
    foreign_net:   int      # 5일 누적 외인 순매수 (주)
    inst_net:      int      # 5일 누적 기관 순매수 (주)
    op_profit:     Optional[float]
    rev_yoy:       Optional[float]
    tradable_nxt:  bool
    passed:        bool
    cond_detail:   str      # "C1✅ C2✅ C3✅ C4✅ C5✅ C6✅"
    rsi:           float    = 0.0
    macd_cross:    bool     = False
    reasons:       List[str] = field(default_factory=list)


# ── 조건 평가 ────────────────────────────────────────────────────────────────
def _calc_atr14(df: pd.DataFrame) -> float:
    tr = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            abs(df["high"] - df["close"].shift(1)),
            abs(df["low"]  - df["close"].shift(1)),
        ),
    )
    return float(tr.rolling(14).mean().iloc[-1])


def _calc_rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1])


def _calc_macd_cross(close: pd.Series) -> bool:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    return bool(macd.iloc[-1] > sig.iloc[-1] and macd.iloc[-2] <= sig.iloc[-2])


def _evaluate_conditions(
    ohlcv:   List[Dict],
    price_info: Dict,
    fin_info:   Dict,
    inv_info:   Dict,
) -> Tuple[bool, Dict]:
    """6대 조건 + NXT 검증 → (passed, meta_dict)"""
    if not ohlcv or len(ohlcv) < 22:
        return False, {}

    df    = pd.DataFrame(ohlcv)
    close = df["close"].astype(float)
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    vol   = df["volume"].astype(float)

    df["high"]  = high
    df["low"]   = low
    df["close"] = close

    cur       = float(close.iloc[-1])
    vol_today = float(vol.iloc[-1])
    atr14     = _calc_atr14(df)

    max_vol_20 = float(vol.iloc[-21:-1].max()) if len(vol) >= 21 else float(vol.iloc[:-1].max())
    cum5       = (cur - float(close.iloc[-6])) / float(close.iloc[-6]) if len(close) >= 6 else 0.0

    mktcap_bil   = float(price_info.get("market_cap_bil", 0))
    op_profit    = fin_info.get("operating_profit")
    rev_yoy      = fin_info.get("revenue_yoy")
    foreign_net  = int(inv_info.get("foreign_net_5d", 0))
    inst_net     = int(inv_info.get("inst_net_5d", 0))
    tradable_nxt = bool(price_info.get("tradable_nxt", False))

    # ── 6대 조건 ──────────────────────────────────────────────────────────────
    cond1 = 5000 <= mktcap_bil <= 30000
    cond2 = (atr14 / cur) >= 0.035 if cur > 0 else False
    cond3 = (
        (op_profit is not None and op_profit > 0)
        or (rev_yoy is not None and rev_yoy >= 0.20)
    ) if (op_profit is not None or rev_yoy is not None) else True  # 데이터 없으면 통과
    cond4 = foreign_net > 0 and inst_net > 0
    cond5 = cum5 >= 0.15
    cond6 = vol_today < (max_vol_20 * 0.30) if max_vol_20 > 0 else False

    # NXT 사전 검증 (실패 시 통과 가능하나 주문 라우팅 경고 표시)
    nxt_ok = tradable_nxt

    passed = all([cond1, cond2, cond3, cond4, cond5, cond6])

    def _fmt(b): return "✅" if b else "❌"
    cond_detail = (
        f"C1{_fmt(cond1)} C2{_fmt(cond2)} C3{_fmt(cond3)} "
        f"C4{_fmt(cond4)} C5{_fmt(cond5)} C6{_fmt(cond6)} "
        f"NXT{_fmt(nxt_ok)}"
    )

    rsi        = _calc_rsi(close)
    macd_cross = _calc_macd_cross(close)

    reasons = []
    if cond2: reasons.append(f"📐ATR {atr14/cur*100:.1f}%")
    if cond5: reasons.append(f"📈5일 {cum5*100:.1f}%")
    if cond6: reasons.append(f"📉거래량 {vol_today/max_vol_20*100:.0f}%")
    if cond4: reasons.append(f"💰외인+{foreign_net:,} 기관+{inst_net:,}")
    if not nxt_ok: reasons.append("⚠️NXT불가")

    meta = {
        "atr_ratio":    round(atr14 / cur * 100, 2) if cur > 0 else 0,
        "cum5_ret":     round(cum5 * 100, 2),
        "vol_ratio":    round(vol_today / max_vol_20 * 100, 1) if max_vol_20 > 0 else 0,
        "market_cap_bil": mktcap_bil,
        "op_profit":    op_profit,
        "rev_yoy":      rev_yoy,
        "foreign_net":  foreign_net,
        "inst_net":     inst_net,
        "tradable_nxt": tradable_nxt,
        "cond_detail":  cond_detail,
        "rsi":          round(rsi, 1),
        "macd_cross":   macd_cross,
        "reasons":      reasons,
    }
    return passed, meta


# ── 단일 종목 비동기 스캔 ────────────────────────────────────────────────────
async def _scan_one(
    client: KISClient,
    ticker: str,
    name:   str,
    min_price: int,
    max_price: int,
) -> Optional[ScanResult]:
    """단일 종목 V8.9 스캔. 조건 미충족이거나 에러면 None 반환."""
    try:
        # 가격 + OHLCV 동시 조회
        price_task = client.get_price(ticker)
        ohlcv_task = client.get_ohlcv(ticker, n_days=60)
        price_info, ohlcv = await asyncio.gather(price_task, ohlcv_task)

        cur = price_info.get("price", 0)
        if cur < min_price or cur > max_price:
            return None

        # 재무는 일 캐시 필요 — 여기서는 단순 호출 (호출 측에서 캐시 딕셔너리 활용)
        fin_info = await client.get_financial_summary(ticker)
        inv_info = await client.get_investor_trend(ticker, days=5)

        passed, meta = _evaluate_conditions(ohlcv, price_info, fin_info, inv_info)
        if not passed:
            return None

        return ScanResult(
            ticker        = ticker,
            name          = name,
            price         = cur,
            change_pct    = float(price_info.get("change_pct", 0)),
            market_cap_bil= meta["market_cap_bil"],
            atr_ratio     = meta["atr_ratio"],
            cum5_ret      = meta["cum5_ret"],
            vol_ratio     = meta["vol_ratio"],
            foreign_net   = meta["foreign_net"],
            inst_net      = meta["inst_net"],
            op_profit     = meta["op_profit"],
            rev_yoy       = meta["rev_yoy"],
            tradable_nxt  = meta["tradable_nxt"],
            passed        = True,
            cond_detail   = meta["cond_detail"],
            rsi           = meta["rsi"],
            macd_cross    = meta["macd_cross"],
            reasons       = meta["reasons"],
        )
    except Exception:
        return None


# ── 배치 스캔 (메인 진입점) ──────────────────────────────────────────────────
async def _run_scan_async(
    tickers:   List[Tuple[str, str]],  # [(ticker, name), ...]
    min_price: int,
    max_price: int,
    concurrency: int = 10,             # 동시 요청 수 (KIS Rate-limit 고려)
) -> List[ScanResult]:
    """
    비동기 배치 스캔.
    concurrency: 동시 처리 종목 수 (기본 10 — KIS 18req/s 한도 내)
    """
    client  = get_client()
    sem     = asyncio.Semaphore(concurrency)
    results = []

    async def _bounded(ticker, name):
        async with sem:
            return await _scan_one(client, ticker, name, min_price, max_price)

    tasks = [_bounded(t, n) for t, n in tickers]
    raw   = await asyncio.gather(*tasks, return_exceptions=True)

    for r in raw:
        if isinstance(r, ScanResult):
            results.append(r)

    return sorted(results, key=lambda x: x.cum5_ret, reverse=True)


def run_v89_scan(
    tickers:    List[Tuple[str, str]],
    min_price:  int = 5000,
    max_price:  int = 2_000_000,
    concurrency: int = 10,
) -> List[ScanResult]:
    """
    Streamlit에서 호출하는 동기 진입점.
    내부적으로 비동기 배치 스캔을 실행합니다.

    예시:
        from scanner import run_v89_scan
        results = run_v89_scan(tickers=[("005930","삼성전자"), ...])
        for r in results:
            print(r.ticker, r.cond_detail)
    """
    coro = _run_scan_async(tickers, min_price, max_price, concurrency)
    return run_async(coro)


# ── 결과 → DataFrame 변환 ────────────────────────────────────────────────────
def results_to_df(results: List[ScanResult]) -> pd.DataFrame:
    """ScanResult 리스트를 Streamlit dataframe용 DataFrame으로 변환."""
    if not results:
        return pd.DataFrame()
    rows = []
    for r in results:
        rows.append({
            "종목명":    r.name,
            "코드":      r.ticker,
            "현재가":    f"{r.price:,}",
            "등락(%)":   f"{'▲' if r.change_pct >= 0 else '▼'}{abs(r.change_pct):.2f}%",
            "시총(억)":  f"{r.market_cap_bil:,.0f}",
            "ATR%":      f"{r.atr_ratio:.1f}%",
            "5일수익률": f"{r.cum5_ret:+.1f}%",
            "거래량%":   f"{r.vol_ratio:.0f}%",
            "외인순매수": f"{r.foreign_net:+,}",
            "기관순매수": f"{r.inst_net:+,}",
            "RSI":       f"{r.rsi:.0f}",
            "NXT":       "✅" if r.tradable_nxt else "⚠️",
            "조건":      r.cond_detail,
            "신호":      " ".join(r.reasons),
        })
    return pd.DataFrame(rows)
