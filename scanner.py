"""
scanner.py — V8.9.2 단기 스윙 스캐너 엔진

V8.9.2 변경사항:
  - cond5: 5일 누적 수익률 ≥ 15% → 8% (과최적화 완화)
  - cond6: 거래량 < 최대 30% → 50% (포착률 개선)
  - cond4: OBV 5일 연속 증가 → CMF20 > 0.05 (스마트 머니 정확도 향상)
  - Wilder 오리지널 RSI 적용 (indicators.py)
  - KIS API 비동기 병렬 처리 유지 (kis_api.py)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from indicators import calc_rsi_wilder, calc_atr, calc_cmf, calc_macd, calc_bb
from kis_api import KISClient, get_client, run_async

# ── V8.9.2 스캐너 파라미터 (상수로 관리) ─────────────────────────────────────
COND1_MKTCAP_MIN  = 5_000    # 억원
COND1_MKTCAP_MAX  = 30_000   # 억원
COND2_ATR_RATIO   = 0.035    # ATR14 / 현재가 ≥ 3.5%
COND4_CMF_MIN     = 0.05     # CMF20 > 0.05 (스마트머니 유입)
COND5_CUM5_RET    = 0.08     # 5일 누적 수익률 ≥ 8%  ← V8.9.2 완화 (기존 15%)
COND6_VOL_RATIO   = 0.50     # 거래량 < 최대의 50%   ← V8.9.2 완화 (기존 30%)

CONCURRENCY_DEFAULT = 10     # KIS Rate-limit 고려 동시 처리 수


# ── 결과 데이터클래스 ─────────────────────────────────────────────────────────
@dataclass
class ScanResult:
    ticker:         str
    name:           str
    price:          int
    change_pct:     float
    market_cap_bil: float
    atr_ratio:      float    # ATR14 / price (%)
    cum5_ret:       float    # 5일 누적 수익률 (%)
    vol_ratio:      float    # 당일 거래량 / 20일 최대 거래량 (%)
    cmf:            float    # CMF20 값
    foreign_net:    int      # 5일 누적 외인 순매수 (KIS 모드)
    inst_net:       int      # 5일 누적 기관 순매수 (KIS 모드)
    op_profit:      Optional[float]
    rev_yoy:        Optional[float]
    tradable_nxt:   bool
    passed:         bool
    cond_detail:    str
    rsi:            float = 0.0
    macd_cross:     bool  = False
    atr14:          float = 0.0   # 절대값 ATR (동적 손절가 계산용)
    reasons:        List[str] = field(default_factory=list)


# ── 지표 계산 (OHLCV DataFrame 기반) ─────────────────────────────────────────
def _compute_from_ohlcv(df: pd.DataFrame) -> Dict:
    """
    OHLCV df → V8.9.2 스캐너에 필요한 지표 딕셔너리 반환.
    컬럼명: 시가, 고가, 저가, 종가, 거래량  (한국어) 또는
            open, high, low, close, volume  (영어, KIS 모드)
    """
    # 컬럼 정규화
    col_map = {
        "open": "시가", "high": "고가", "low": "저가",
        "close": "종가", "volume": "거래량",
    }
    df = df.rename(columns=col_map)

    if len(df) < 22:
        return {}

    c = df["종가"].astype(float)
    h = df["고가"].astype(float)
    l = df["저가"].astype(float)
    v = df["거래량"].astype(float)

    cur       = float(c.iloc[-1])
    vol_today = float(v.iloc[-1])

    # ATR14 (Wilder)
    atr14 = float(calc_atr(h, l, c, 14, "wilder").iloc[-1])

    # 최근 20일 최대 거래량 (당일 제외)
    max_vol_20 = float(v.iloc[-21:-1].max()) if len(v) >= 21 else float(v.iloc[:-1].max())

    # 5거래일 누적 수익률
    cum5 = (cur - float(c.iloc[-6])) / float(c.iloc[-6]) if len(c) >= 6 else 0.0

    # CMF20 (스마트 머니)
    cmf20 = float(calc_cmf(h, l, c, v, 20).iloc[-1])

    # RSI (Wilder 오리지널)
    rsi = float(calc_rsi_wilder(c).iloc[-1])

    # MACD 골든크로스
    macd, sig, _ = calc_macd(c)
    macd_cross = bool(
        macd.iloc[-1] > sig.iloc[-1] and macd.iloc[-2] <= sig.iloc[-2]
    )

    return {
        "cur":        cur,
        "atr14":      atr14,
        "atr_ratio":  atr14 / cur if cur > 0 else 0,
        "cum5":       cum5,
        "vol_today":  vol_today,
        "max_vol_20": max_vol_20,
        "vol_ratio":  vol_today / max_vol_20 if max_vol_20 > 0 else 0,
        "cmf20":      cmf20 if not np.isnan(cmf20) else 0.0,
        "rsi":        rsi if not np.isnan(rsi) else 50.0,
        "macd_cross": macd_cross,
    }


# ── 6대 조건 평가 ────────────────────────────────────────────────────────────
def _evaluate_v892(
    ind:        Dict,
    price_info: Dict,
    fin_info:   Dict,
    inv_info:   Dict,
) -> Tuple[bool, str, List[str]]:
    """
    V8.9.2 6대 조건 + NXT 검증.
    Returns: (passed, cond_detail, reasons)
    """
    mktcap_bil   = float(price_info.get("market_cap_bil", 0))
    op_profit    = fin_info.get("operating_profit")
    rev_yoy      = fin_info.get("revenue_yoy")
    foreign_net  = int(inv_info.get("foreign_net_5d", 0))
    inst_net     = int(inv_info.get("inst_net_5d",    0))
    tradable_nxt = bool(price_info.get("tradable_nxt", False))

    # cond1: 시총 5,000억~3조
    cond1 = COND1_MKTCAP_MIN <= mktcap_bil <= COND1_MKTCAP_MAX \
            if mktcap_bil > 0 else True   # 데이터 없으면 통과

    # cond2: ATR14/현재가 ≥ 3.5%
    cond2 = ind.get("atr_ratio", 0) >= COND2_ATR_RATIO

    # cond3: 영업이익 흑자 OR 매출 YoY ≥ 20%
    if op_profit is not None or rev_yoy is not None:
        cond3 = (op_profit is not None and op_profit > 0) or \
                (rev_yoy  is not None and rev_yoy  >= 0.20)
    else:
        cond3 = True  # 데이터 없으면 통과

    # cond4: CMF20 > 0.05 (스마트 머니 유입) — KIS 수급 데이터 있으면 AND 조건 추가
    cond4_cmf = ind.get("cmf20", 0) > COND4_CMF_MIN
    if foreign_net != 0 or inst_net != 0:
        # KIS 실수급 데이터 존재 시 쌍끌이 AND 조건 추가
        cond4 = cond4_cmf and (foreign_net > 0) and (inst_net > 0)
    else:
        cond4 = cond4_cmf  # yfinance 모드: CMF만으로 판정

    # cond5: 5일 누적 수익률 ≥ 8%  ← V8.9.2 완화
    cond5 = ind.get("cum5", 0) >= COND5_CUM5_RET

    # cond6: 당일 거래량 < 최대의 50%  ← V8.9.2 완화
    cond6 = ind.get("vol_ratio", 1) < COND6_VOL_RATIO

    passed = all([cond1, cond2, cond3, cond4, cond5, cond6])

    def _e(b): return "✅" if b else "❌"
    cond_detail = (
        f"C1{_e(cond1)} C2{_e(cond2)} C3{_e(cond3)} "
        f"C4{_e(cond4)} C5{_e(cond5)} C6{_e(cond6)} "
        f"NXT{_e(tradable_nxt)}"
    )

    reasons = []
    if cond2: reasons.append(f"📐ATR {ind['atr_ratio']*100:.1f}%")
    if cond5: reasons.append(f"📈5일 {ind['cum5']*100:.1f}%")
    if cond6: reasons.append(f"📉거래량 {ind['vol_ratio']*100:.0f}%")
    cmf_v = ind.get("cmf20", 0)
    if cond4: reasons.append(f"💰CMF {cmf_v:+.3f}")
    if not tradable_nxt: reasons.append("⚠️NXT불가")

    return passed, cond_detail, reasons


# ── 단일 종목 비동기 스캔 (KIS 모드) ─────────────────────────────────────────
async def _scan_one_kis(
    client:    KISClient,
    ticker:    str,
    name:      str,
    min_price: int,
    max_price: int,
) -> Optional[ScanResult]:
    try:
        price_task = client.get_price(ticker)
        ohlcv_task = client.get_ohlcv(ticker, n_days=60)
        price_info, ohlcv_rows = await asyncio.gather(price_task, ohlcv_task)

        cur = price_info.get("price", 0)
        if cur < min_price or cur > max_price:
            return None

        if not ohlcv_rows or len(ohlcv_rows) < 22:
            return None

        df = pd.DataFrame(ohlcv_rows)
        ind = _compute_from_ohlcv(df)
        if not ind:
            return None

        fin_info, inv_info = await asyncio.gather(
            client.get_financial_summary(ticker),
            client.get_investor_trend(ticker, days=5),
        )

        passed, cond_detail, reasons = _evaluate_v892(ind, price_info, fin_info, inv_info)
        if not passed:
            return None

        return ScanResult(
            ticker        = ticker,
            name          = name,
            price         = cur,
            change_pct    = float(price_info.get("change_pct", 0)),
            market_cap_bil= float(price_info.get("market_cap_bil", 0)),
            atr_ratio     = round(ind["atr_ratio"] * 100, 2),
            cum5_ret      = round(ind["cum5"] * 100, 2),
            vol_ratio     = round(ind["vol_ratio"] * 100, 1),
            cmf           = round(ind["cmf20"], 4),
            foreign_net   = int(inv_info.get("foreign_net_5d", 0)),
            inst_net      = int(inv_info.get("inst_net_5d",    0)),
            op_profit     = fin_info.get("operating_profit"),
            rev_yoy       = fin_info.get("revenue_yoy"),
            tradable_nxt  = bool(price_info.get("tradable_nxt", False)),
            passed        = True,
            cond_detail   = cond_detail,
            rsi           = round(ind["rsi"], 1),
            macd_cross    = ind["macd_cross"],
            atr14         = round(ind["atr14"], 2),
            reasons       = reasons,
        )
    except Exception:
        return None


# ── 단일 종목 동기 스캔 (yfinance 폴백 모드) ─────────────────────────────────
def scan_one_yfinance(
    df:        pd.DataFrame,
    ticker:    str,
    name:      str,
    min_price: int,
    max_price: int,
) -> Optional[ScanResult]:
    """yfinance fetch_ohlcv 결과 DataFrame을 직접 받아 스캔."""
    try:
        if df is None or len(df) < 22:
            return None

        ind = _compute_from_ohlcv(df)
        if not ind:
            return None

        cur = ind["cur"]
        if cur < min_price or cur > max_price:
            return None

        # yfinance 모드: 재무/수급 없음
        price_info = {"market_cap_bil": 0, "tradable_nxt": True, "change_pct": 0}
        fin_info   = {"operating_profit": None, "revenue_yoy": None}
        inv_info   = {"foreign_net_5d": 0, "inst_net_5d": 0}

        # 당일 등락률 계산
        c = df["종가"].astype(float) if "종가" in df.columns else df["close"].astype(float)
        chg = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100 if len(c) >= 2 else 0.0

        price_info["change_pct"] = round(chg, 2)

        passed, cond_detail, reasons = _evaluate_v892(ind, price_info, fin_info, inv_info)
        if not passed:
            return None

        return ScanResult(
            ticker        = ticker,
            name          = name,
            price         = int(cur),
            change_pct    = round(chg, 2),
            market_cap_bil= 0,
            atr_ratio     = round(ind["atr_ratio"] * 100, 2),
            cum5_ret      = round(ind["cum5"] * 100, 2),
            vol_ratio     = round(ind["vol_ratio"] * 100, 1),
            cmf           = round(ind["cmf20"], 4),
            foreign_net   = 0,
            inst_net      = 0,
            op_profit     = None,
            rev_yoy       = None,
            tradable_nxt  = True,
            passed        = True,
            cond_detail   = cond_detail,
            rsi           = round(ind["rsi"], 1),
            macd_cross    = ind["macd_cross"],
            atr14         = round(ind["atr14"], 2),
            reasons       = reasons,
        )
    except Exception:
        return None


# ── 배치 스캔 (KIS 비동기) ───────────────────────────────────────────────────
async def _run_scan_async(
    tickers:     List[Tuple[str, str]],
    min_price:   int,
    max_price:   int,
    concurrency: int,
) -> List[ScanResult]:
    client = get_client()
    sem    = asyncio.Semaphore(concurrency)

    async def _bounded(ticker, name):
        async with sem:
            return await _scan_one_kis(client, ticker, name, min_price, max_price)

    tasks  = [_bounded(t, n) for t, n in tickers]
    raw    = await asyncio.gather(*tasks, return_exceptions=True)
    return sorted(
        [r for r in raw if isinstance(r, ScanResult)],
        key=lambda x: x.cum5_ret,
        reverse=True,
    )


def run_v892_scan(
    tickers:     List[Tuple[str, str]],
    min_price:   int = 5_000,
    max_price:   int = 2_000_000,
    concurrency: int = CONCURRENCY_DEFAULT,
) -> List[ScanResult]:
    """
    Streamlit 동기 진입점 — KIS 비동기 배치 스캔.

    예시:
        from scanner import run_v892_scan
        results = run_v892_scan([("005930","삼성전자"), ...])
    """
    coro = _run_scan_async(tickers, min_price, max_price, concurrency)
    return run_async(coro)


# ── 결과 → DataFrame ─────────────────────────────────────────────────────────
def results_to_df(results: List[ScanResult]) -> pd.DataFrame:
    if not results:
        return pd.DataFrame()
    rows = []
    for r in results:
        rows.append({
            "종목명":    r.name,
            "코드":      r.ticker,
            "현재가":    f"{r.price:,}",
            "등락(%)":   f"{'▲' if r.change_pct >= 0 else '▼'}{abs(r.change_pct):.2f}%",
            "시총(억)":  f"{r.market_cap_bil:,.0f}" if r.market_cap_bil else "?",
            "ATR%":      f"{r.atr_ratio:.1f}%",
            "5일수익률": f"{r.cum5_ret:+.1f}%",
            "거래량%":   f"{r.vol_ratio:.0f}%",
            "CMF":       f"{r.cmf:+.3f}",
            "RSI":       f"{r.rsi:.0f}",
            "NXT":       "✅" if r.tradable_nxt else "⚠️",
            "조건":      r.cond_detail,
            "신호":      " ".join(r.reasons),
        })
    return pd.DataFrame(rows)
