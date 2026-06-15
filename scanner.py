"""
scanner.py — V8.9.3 단기 스윙 스캐너 엔진

V8.9.3 변경사항:
  - 하드 필터 1: ETF / SPAC / 우선주 원천 차단 (시총 0 또는 None → 즉시 폐기)
  - 하드 필터 2: 저변동성 금지 섹터 킬스위치 (유통/은행/금융/보험/전력·유틸/통신/지주사)
  - 기존 V8.9.2 cond1~cond6 AND 로직 유지
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from indicators import calc_rsi_wilder, calc_atr, calc_cmf, calc_macd, calc_bb
from kis_api import KISClient, get_client, run_async

# ── V8.9.2 스캐너 파라미터 ────────────────────────────────────────────────────
COND1_MKTCAP_MIN  = 5_000    # 억원
COND1_MKTCAP_MAX  = 30_000   # 억원
COND2_ATR_RATIO   = 0.035    # ATR14 / 현재가 ≥ 3.5%
COND4_CMF_MIN     = 0.05     # CMF20 > 0.05
COND5_CUM5_RET    = 0.08     # 5일 누적 수익률 ≥ 8%
COND6_VOL_RATIO   = 0.50     # 거래량 < 최대의 50%

CONCURRENCY_DEFAULT = 10

# ── 하드 필터 상수 ────────────────────────────────────────────────────────────
# ETF 판별 키워드 (종목명 포함 시 차단)
ETF_NAME_KEYWORDS = [
    "KODEX", "TIGER", "KBSTAR", "HANARO", "ARIRANG", "KOSEF",
    "RISE", "ACE", "SOL", "PLUS", "ETF", "레버리지", "인버스",
    "스팩", "SPAC", "리츠", "REITS", "우선주",
]

# 금지 섹터 블랙리스트 (yfinance sector/industry 문자열 포함 매칭)
BLOCKED_SECTORS = [
    # 한국어
    "유통", "은행", "금융", "보험", "전력", "유틸리티", "통신", "지주",
    # yfinance 영어 sector/industry 값
    "Banks", "Diversified Banks", "Regional Banks",
    "Insurance", "Life Insurance", "Property & Casualty Insurance",
    "Financial Services", "Capital Markets", "Consumer Finance",
    "Electric Utilities", "Utilities", "Multi-Utilities",
    "Telecom", "Telecommunication", "Communication Services",
    "Wireless Telecommunication",
    "Retail", "Food & Staples Retailing",
    "Conglomerates", "Holding Companies",
]


# ── 하드 필터 1+2: ETF/저변동성 섹터 킬스위치 ───────────────────────────────
def _hard_filter_yfinance(ticker: str, name: str) -> Tuple[bool, str]:
    """
    yfinance 메타데이터 기반 하드 필터.
    Returns: (통과 여부, 거부 사유)
    통과: (True, "")  /  차단: (False, 사유 문자열)
    """
    # ── 필터 1-A: 종목명 키워드로 ETF/SPAC/우선주 즉시 차단 ──
    _name_upper = name.upper()
    for kw in ETF_NAME_KEYWORDS:
        if kw.upper() in _name_upper:
            return False, f"ETF/SPAC 차단: {kw}"

    # ── 필터 1-B: 티커 패턴으로 우선주 차단 (한국 우선주: 코드 끝 5번째 자리 5)
    if ticker.isdigit() and len(ticker) == 6 and ticker[4] == "5":
        return False, "우선주 차단 (종목코드 패턴)"

    # ── yfinance 메타데이터 조회 ──
    try:
        import yfinance as yf
        _suffix = ".KS" if ticker.isdigit() else ""
        _info   = yf.Ticker(ticker + _suffix).info

        # ── 필터 1-C: 시총 0 또는 None → ETF/거래정지 의심 ──
        mktcap = _info.get("marketCap", None)
        if mktcap is None or mktcap == 0:
            return False, "시총 0/None (ETF 또는 거래정지 의심)"

        # ── 필터 2: 저변동성 금지 섹터 킬스위치 ──
        sector   = str(_info.get("sector",   "") or "")
        industry = str(_info.get("industry", "") or "")
        combined = sector + " " + industry

        for blocked in BLOCKED_SECTORS:
            if blocked.lower() in combined.lower():
                return False, f"금지 섹터 차단: {blocked} ({sector}/{industry})"

        # ── 필터 1-D: quoteType이 ETF이면 차단 ──
        quote_type = str(_info.get("quoteType", "") or "").upper()
        if quote_type in ("ETF", "MUTUALFUND", "FUTURE", "INDEX"):
            return False, f"quoteType 차단: {quote_type}"

        return True, ""

    except Exception:
        # 메타데이터 조회 실패 시 통과 (보수적으로 허용 — 이후 조건에서 걸림)
        return True, ""


def _hard_filter_name_only(name: str, ticker: str) -> Tuple[bool, str]:
    """
    API 없이 종목명·코드만으로 빠르게 1차 필터링 (KIS 모드 사전 필터).
    yfinance 조회 없이 즉시 차단 가능한 케이스만 처리.
    """
    _name_upper = name.upper()
    for kw in ETF_NAME_KEYWORDS:
        if kw.upper() in _name_upper:
            return False, f"ETF/SPAC 차단: {kw}"
    if ticker.isdigit() and len(ticker) == 6 and ticker[4] == "5":
        return False, "우선주 차단"
    return True, ""


# ── 결과 데이터클래스 ─────────────────────────────────────────────────────────
@dataclass
class ScanResult:
    ticker:         str
    name:           str
    price:          int
    change_pct:     float
    market_cap_bil: float
    atr_ratio:      float
    cum5_ret:       float
    vol_ratio:      float
    cmf:            float
    foreign_net:    int
    inst_net:       int
    op_profit:      Optional[float]
    rev_yoy:        Optional[float]
    tradable_nxt:   bool
    passed:         bool
    cond_detail:    str
    rsi:            float = 0.0
    macd_cross:     bool  = False
    atr14:          float = 0.0
    reasons:        List[str] = field(default_factory=list)
    filter_reason:  str  = ""   # 하드 필터 거부 사유 (디버그용)


# ── 지표 계산 ─────────────────────────────────────────────────────────────────
def _compute_from_ohlcv(df: pd.DataFrame) -> Dict:
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

    atr14      = float(calc_atr(h, l, c, 14, "wilder").iloc[-1])
    max_vol_20 = float(v.iloc[-21:-1].max()) if len(v) >= 21 else float(v.iloc[:-1].max())
    cum5       = (cur - float(c.iloc[-6])) / float(c.iloc[-6]) if len(c) >= 6 else 0.0
    cmf20      = float(calc_cmf(h, l, c, v, 20).iloc[-1])
    rsi        = float(calc_rsi_wilder(c).iloc[-1])
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
        "rsi":        rsi  if not np.isnan(rsi)   else 50.0,
        "macd_cross": macd_cross,
    }


# ── 6대 조건 평가 ─────────────────────────────────────────────────────────────
def _evaluate_v892(
    ind:        Dict,
    price_info: Dict,
    fin_info:   Dict,
    inv_info:   Dict,
) -> Tuple[bool, str, List[str]]:
    mktcap_bil   = float(price_info.get("market_cap_bil", 0))
    op_profit    = fin_info.get("operating_profit")
    rev_yoy      = fin_info.get("revenue_yoy")
    foreign_net  = int(inv_info.get("foreign_net_5d", 0))
    inst_net     = int(inv_info.get("inst_net_5d",    0))
    tradable_nxt = bool(price_info.get("tradable_nxt", False))

    cond1 = (COND1_MKTCAP_MIN <= mktcap_bil <= COND1_MKTCAP_MAX
             if mktcap_bil > 0 else True)

    cond2 = ind.get("atr_ratio", 0) >= COND2_ATR_RATIO

    if op_profit is not None or rev_yoy is not None:
        cond3 = ((op_profit is not None and op_profit > 0) or
                 (rev_yoy  is not None and rev_yoy  >= 0.20))
    else:
        cond3 = True

    cond4_cmf = ind.get("cmf20", 0) > COND4_CMF_MIN
    if foreign_net != 0 or inst_net != 0:
        cond4 = cond4_cmf and (foreign_net > 0) and (inst_net > 0)
    else:
        cond4 = cond4_cmf

    cond5 = ind.get("cum5", 0)       >= COND5_CUM5_RET
    cond6 = ind.get("vol_ratio", 1)  <  COND6_VOL_RATIO

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
        # ── 하드 필터 0순위: 종목명/코드 기반 즉시 차단 ──
        ok, reason = _hard_filter_name_only(name, ticker)
        if not ok:
            return None

        price_task = client.get_price(ticker)
        ohlcv_task = client.get_ohlcv(ticker, n_days=60)
        price_info, ohlcv_rows = await asyncio.gather(price_task, ohlcv_task)

        cur = price_info.get("price", 0)
        if cur < min_price or cur > max_price:
            return None

        # ── 하드 필터 1-C: KIS 시총 0 차단 ──
        mktcap = float(price_info.get("market_cap_bil", 0))
        if mktcap == 0:
            return None

        if not ohlcv_rows or len(ohlcv_rows) < 22:
            return None

        df  = pd.DataFrame(ohlcv_rows)
        ind = _compute_from_ohlcv(df)
        if not ind:
            return None

        fin_info, inv_info = await asyncio.gather(
            client.get_financial_summary(ticker),
            client.get_investor_trend(ticker, days=5),
        )

        # ── 하드 필터 2: KIS sector 정보로 금지 섹터 차단 ──
        sector = str(fin_info.get("sector", "") or "")
        for blocked in BLOCKED_SECTORS:
            if blocked.lower() in sector.lower():
                return None

        passed, cond_detail, reasons = _evaluate_v892(ind, price_info, fin_info, inv_info)
        if not passed:
            return None

        return ScanResult(
            ticker        = ticker,
            name          = name,
            price         = cur,
            change_pct    = float(price_info.get("change_pct", 0)),
            market_cap_bil= mktcap,
            atr_ratio     = round(ind["atr_ratio"] * 100, 2),
            cum5_ret      = round(ind["cum5"]       * 100, 2),
            vol_ratio     = round(ind["vol_ratio"]  * 100, 1),
            cmf           = round(ind["cmf20"], 4),
            foreign_net   = int(inv_info.get("foreign_net_5d", 0)),
            inst_net      = int(inv_info.get("inst_net_5d",    0)),
            op_profit     = fin_info.get("operating_profit"),
            rev_yoy       = fin_info.get("revenue_yoy"),
            tradable_nxt  = bool(price_info.get("tradable_nxt", False)),
            passed        = True,
            cond_detail   = cond_detail,
            rsi           = round(ind["rsi"],  1),
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
    """
    yfinance fetch_ohlcv 결과 DataFrame을 직접 받아 스캔.

    하드 필터 → 가격 필터 → 지표 계산 → 6대 조건 순서로 실행.
    """
    try:
        # ── 하드 필터 0순위: ETF/SPAC/섹터 킬스위치 ──
        ok, reason = _hard_filter_yfinance(ticker, name)
        if not ok:
            return None  # 거부 (reason은 디버그 로그용)

        if df is None or len(df) < 22:
            return None

        ind = _compute_from_ohlcv(df)
        if not ind:
            return None

        cur = ind["cur"]
        if cur < min_price or cur > max_price:
            return None

        price_info = {"market_cap_bil": 0, "tradable_nxt": True, "change_pct": 0}
        fin_info   = {"operating_profit": None, "revenue_yoy": None}
        inv_info   = {"foreign_net_5d": 0, "inst_net_5d": 0}

        c   = df["종가"].astype(float) if "종가" in df.columns else df["close"].astype(float)
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
            cum5_ret      = round(ind["cum5"]       * 100, 2),
            vol_ratio     = round(ind["vol_ratio"]  * 100, 1),
            cmf           = round(ind["cmf20"], 4),
            foreign_net   = 0,
            inst_net      = 0,
            op_profit     = None,
            rev_yoy       = None,
            tradable_nxt  = True,
            passed        = True,
            cond_detail   = cond_detail,
            rsi           = round(ind["rsi"],  1),
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

    tasks = [_bounded(t, n) for t, n in tickers]
    raw   = await asyncio.gather(*tasks, return_exceptions=True)
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
    """Streamlit 동기 진입점 — KIS 비동기 배치 스캔."""
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
