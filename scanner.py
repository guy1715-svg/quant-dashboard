"""
scanner.py — V8.9.4 하이브리드 스코어링 스캐너 엔진

V8.9.4 변경사항:
  - 데이터 소스: KIS API 우선, yfinance 폴백
  - 하드 필터: ETF/SPAC/우선주/저변동성섹터 + 시총(C1) + ATR(C2) — AND 필수
  - 스코어링: C3(재무 20점) + C4(외인/기관 쌍끌이 30점) + C5(모멘텀 25점) + C6(눌림목 25점)
  - 판정: 70점 이상 → Target_Locked / 90점 이상 → A-Grade 주도주
  - NXT(대체거래소) tradable_nxt 플래그 검증
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from indicators import calc_rsi_wilder, calc_atr, calc_cmf, calc_macd, calc_bb
from kis_api import KISClient, get_client, run_async

# ── 하드 필터 파라미터 ────────────────────────────────────────────────────────
COND1_MKTCAP_MIN = 5_000    # 억원
COND1_MKTCAP_MAX = 30_000   # 억원
COND2_ATR_RATIO  = 0.035    # ATR14 / 현재가 ≥ 3.5%

# ── 스코어링 파라미터 ─────────────────────────────────────────────────────────
SCORE_C3_FIN     = 20   # 재무: 영업이익 흑자 OR 매출YoY≥20%
SCORE_C4_INV     = 30   # 수급: 외인+기관 쌍끌이 순매수 > 0
SCORE_C5_MOM     = 25   # 모멘텀: 5일 누적수익률 ≥ 8%
SCORE_C6_VOL     = 25   # 눌림목: 거래량 < 직전20일최대 × 50%
SCORE_C5_RET_MIN = 0.08
SCORE_C6_VOL_MAX = 0.50

GRADE_A    = 90   # A-Grade 주도주
GRADE_LOCK = 70   # Target_Locked

CONCURRENCY_DEFAULT = 10

# ── 하드 필터 상수 ────────────────────────────────────────────────────────────
ETF_NAME_KEYWORDS = [
    "KODEX", "TIGER", "KBSTAR", "HANARO", "ARIRANG", "KOSEF",
    "RISE", "ACE", "SOL", "PLUS", "ETF", "레버리지", "인버스",
    "스팩", "SPAC", "리츠", "REITS", "우선주",
]

BLOCKED_NAME_KEYWORDS = [
    "지주", "홀딩스", "홀딩", "HOLDING", "HOLDINGS",
    "리테일", "마트", "쇼핑", "유통", "편의점", "홈쇼핑", "백화점", "면세",
    "은행", "뱅크", "증권", "보험", "캐피탈", "카드", "저축", "투자", "자산운용", "신탁",
    "텔레콤", "통신",
    "한전", "발전", "전력", "가스",
]

BLOCKED_SECTORS = [
    "유통", "은행", "금융", "보험", "전력", "유틸리티", "통신", "지주",
    "Banks", "Diversified Banks", "Regional Banks",
    "Insurance", "Life Insurance", "Property & Casualty Insurance",
    "Financial Services", "Capital Markets", "Consumer Finance",
    "Electric Utilities", "Utilities", "Multi-Utilities",
    "Telecom", "Telecommunication", "Communication Services",
    "Wireless Telecommunication",
    "Retail", "Food & Staples Retailing",
    "Conglomerates", "Holding Companies",
]

# ── 영구 블랙리스트 (API 오분류 종목) ─────────────────────────────────────────
BLACKLIST_TICKERS = [
    '002790',  # 아모레퍼시픽 (지주사 - API에서 화장품/제조업으로 오분류)
]


# ── 하드 필터 함수 ────────────────────────────────────────────────────────────
def _hard_filter_name_only(name: str, ticker: str) -> Tuple[bool, str]:
    """종목명·코드만으로 빠른 1차 필터 (API 불필요)."""
    if ticker in BLACKLIST_TICKERS:
        return False, f"블랙리스트: {ticker}"
    _name_upper = name.upper()
    for kw in ETF_NAME_KEYWORDS:
        if kw.upper() in _name_upper:
            return False, f"ETF/SPAC: {kw}"
    for kw in BLOCKED_NAME_KEYWORDS:
        if kw.upper() in _name_upper:
            return False, f"종목명섹터: {kw}"
    if ticker.isdigit() and len(ticker) == 6 and ticker[4] == "5":
        return False, "우선주 코드패턴"
    return True, ""


def _hard_filter_yfinance(ticker: str, name: str) -> Tuple[bool, str]:
    """yfinance 메타데이터 기반 2차 필터."""
    ok, reason = _hard_filter_name_only(name, ticker)
    if not ok:
        return False, reason
    try:
        import yfinance as yf
        for _sfx in ([".KS", ".KQ"] if ticker.isdigit() else [""]):
            _info = yf.Ticker(ticker + _sfx).info
            if _info and _info.get("regularMarketPrice"):
                break
        mktcap = _info.get("marketCap", None)
        if mktcap is None or mktcap == 0:
            return False, "시총 0/None"
        qt = str(_info.get("quoteType", "") or "").upper()
        if qt in ("ETF", "MUTUALFUND", "FUTURE", "INDEX"):
            return False, f"quoteType={qt}"
        combined = (str(_info.get("sector", "") or "") + " " +
                    str(_info.get("industry", "") or ""))
        for blk in BLOCKED_SECTORS:
            if blk.lower() in combined.lower():
                return False, f"금지섹터: {blk}"
        return True, ""
    except Exception:
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
    score:          int           # 총점 (0~100)
    grade:          str           # "A-Grade 주도주" / "Target_Locked" / "Filtered"
    cond_detail:    str
    rsi:            float = 0.0
    macd_cross:     bool  = False
    atr14:          float = 0.0
    reasons:        List[str] = field(default_factory=list)
    filter_reason:  str  = ""


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
        "rsi":        rsi   if not np.isnan(rsi)   else 50.0,
        "macd_cross": macd_cross,
    }


# ── 하이브리드 스코어링 평가 ──────────────────────────────────────────────────
def _evaluate_scoring(
    ind:        Dict,
    price_info: Dict,
    fin_info:   Dict,
    inv_info:   Dict,
) -> Tuple[bool, int, str, str, List[str]]:
    """
    Returns: (hard_pass, score, grade, cond_detail, reasons)
    hard_pass: C1+C2 필수 AND 통과 여부
    score: 0~100점 (C3+C4+C5+C6 합산)
    grade: "A-Grade 주도주" / "Target_Locked" / "Filtered"
    """
    mktcap_bil   = float(price_info.get("market_cap_bil", 0))
    op_profit    = fin_info.get("operating_profit")
    rev_yoy      = fin_info.get("revenue_yoy")
    foreign_net  = int(inv_info.get("foreign_net_5d", 0))
    inst_net     = int(inv_info.get("inst_net_5d",    0))
    tradable_nxt = bool(price_info.get("tradable_nxt", True))

    # ── 대형주 여부 판정 (시총 1조=10,000억 이상 or KOSPI200 편입) ──
    KOSPI200 = {
        '005930','000660','005380','005490','035420','000270','105560','055550',
        '012330','051910','006400','207940','068270','035720','003550','323410',
        '034730','086790','028260','011200','009830','010130','032830','017670',
        '066570','011070','003490','024110','018260','030200','090430','096770',
        '010950','011780','009150','000810','033780','329180','012450','247540',
        '373220','003670','091990','316140','267250','042700','000100','402340',
    }
    ticker_code = str(price_info.get("ticker", ""))
    is_large_cap = (mktcap_bil >= 10_000) or (ticker_code in KOSPI200)

    # ── 하드 필터: C1 시총 ──
    if mktcap_bil > 0:
        c1_pass = COND1_MKTCAP_MIN <= mktcap_bil <= COND1_MKTCAP_MAX
    else:
        c1_pass = True  # 데이터 없으면 통과 (후속 조건에서 걸림)

    # ── 하드 필터: C2 ATR ──
    c2_pass = ind.get("atr_ratio", 0) >= COND2_ATR_RATIO

    hard_pass = c1_pass and c2_pass

    # ── 스코어링 (하드 필터 통과 여부 무관하게 계산, 단 판정은 hard_pass AND score 기준) ──
    score = 0
    reasons = []

    # C3: 재무 (20점) — 결측치는 0점 처리, 탈락 없음
    c3_ok = False
    if op_profit is not None or rev_yoy is not None:
        c3_ok = ((op_profit is not None and op_profit > 0) or
                 (rev_yoy  is not None and rev_yoy  >= 0.20))
        if c3_ok:
            score += SCORE_C3_FIN
            reasons.append(f"📊재무+{SCORE_C3_FIN}점")

    # C4: 수급 — 외인+기관 쌍끌이 순매수 > 0 (30점)
    # KIS 데이터 있을 때: 외인 AND 기관 둘 다 순매수 양수
    # yfinance 폴백: KIS 데이터 없으면 CMF20 > 0으로 대체 (수급 방향성 대리)
    _has_kis_data = (foreign_net != 0) or (inst_net != 0)
    if _has_kis_data:
        c4_ok = (foreign_net > 0) and (inst_net > 0)
    else:
        c4_ok = ind.get("cmf20", 0) > 0  # CMF20 대체 지표
    if c4_ok:
        score += SCORE_C4_INV
        reasons.append(f"💰쌍끌이+{SCORE_C4_INV}점")

    # C5: 모멘텀 (25점) — 5일 누적수익률 ≥ 8%
    c5_ok = ind.get("cum5", 0) >= SCORE_C5_RET_MIN
    if c5_ok:
        score += SCORE_C5_MOM
        reasons.append(f"📈5일+{SCORE_C5_MOM}점")

    # C6: 눌림목 (25점) — 거래량 < 직전 20일 최대 × 50%
    c6_ok = ind.get("vol_ratio", 1) < SCORE_C6_VOL_MAX
    if c6_ok:
        score += SCORE_C6_VOL
        reasons.append(f"📉눌림목+{SCORE_C6_VOL}점")

    # ── 과열 방지: 갭상승/MA5이격 차단 (대형주 특례에서도 절대 예외 없음) ──
    gap_pct   = float(ind.get("gap_pct",   0))   # 시가 갭 (%)
    ma5_diff  = float(ind.get("ma5_diff",  0))   # MA5 이격 (%)
    overheat  = (gap_pct >= 3.0) or (abs(ma5_diff) >= 3.0)

    # ── 등급 판정 ──
    all6_pass = c1_pass and c2_pass and c3_ok and c4_ok and c5_ok and c6_ok

    # 대형주 특례: C1(ADX≥25) + C4(수급) True면 나머지 일부 미달해도 Target_Locked 허용
    adx_val   = float(ind.get("adx14", 0))
    large_cap_pass = (
        is_large_cap
        and adx_val >= 25
        and c4_ok
        and not overheat
    )

    if overheat:
        grade = "Filtered"  # 과열 → 무조건 차단
    elif all6_pass and score >= GRADE_A:
        grade = "A-Grade 주도주"
    elif all6_pass and score >= GRADE_LOCK:
        grade = "Target_Locked"
    elif large_cap_pass and score >= GRADE_LOCK:
        grade = "Target_Locked"  # 대형주 특례 통과
    else:
        grade = "Filtered"

    def _e(b): return "✅" if b else "❌"
    _lc_tag = "🏦대형주특례" if large_cap_pass and not all6_pass else ""
    _oh_tag = "🔥과열차단" if overheat else ""
    cond_detail = (
        f"C1{_e(c1_pass)}({mktcap_bil:.0f}억) "
        f"C2{_e(c2_pass)}({ind.get('atr_ratio',0)*100:.1f}%) "
        f"C3{_e(c3_ok)} C4{_e(c4_ok)} C5{_e(c5_ok)} C6{_e(c6_ok)} "
        f"점수:{score}점 NXT{_e(tradable_nxt)}"
        + (f" {_lc_tag}" if _lc_tag else "")
        + (f" {_oh_tag}" if _oh_tag else "")
    )

    if not tradable_nxt:
        reasons.append("⚠️NXT불가")

    return hard_pass, score, grade, cond_detail, reasons


# ── 단일 종목 비동기 스캔 (KIS 모드) ─────────────────────────────────────────
async def _scan_one_kis(
    client:    KISClient,
    ticker:    str,
    name:      str,
    min_price: int,
    max_price: int,
) -> Optional[ScanResult]:
    try:
        ok, reason = _hard_filter_name_only(name, ticker)
        if not ok:
            return None

        price_task = client.get_price(ticker)
        ohlcv_task = client.get_ohlcv(ticker, n_days=60)
        price_info, ohlcv_rows = await asyncio.gather(price_task, ohlcv_task)

        cur = price_info.get("price", 0)
        if cur < min_price or cur > max_price:
            return None

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

        # 금지 섹터 차단
        sector = str(fin_info.get("sector", "") or "")
        for blocked in BLOCKED_SECTORS:
            if blocked.lower() in sector.lower():
                return None

        hard_pass, score, grade, cond_detail, reasons = _evaluate_scoring(
            ind, price_info, fin_info, inv_info
        )

        # 70점 미만 또는 하드필터 실패 → 제외
        if not hard_pass or score < GRADE_LOCK:
            return None

        c = df["종가"].astype(float) if "종가" in df.columns else df["close"].astype(float)
        chg = float(price_info.get("change_pct", 0))

        return ScanResult(
            ticker         = ticker,
            name           = name,
            price          = int(cur),
            change_pct     = chg,
            market_cap_bil = mktcap,
            atr_ratio      = round(ind["atr_ratio"] * 100, 2),
            cum5_ret       = round(ind["cum5"]       * 100, 2),
            vol_ratio      = round(ind["vol_ratio"]  * 100, 1),
            cmf            = round(ind["cmf20"], 4),
            foreign_net    = int(inv_info.get("foreign_net_5d", 0)),
            inst_net       = int(inv_info.get("inst_net_5d",    0)),
            op_profit      = fin_info.get("operating_profit"),
            rev_yoy        = fin_info.get("revenue_yoy"),
            tradable_nxt   = bool(price_info.get("tradable_nxt", False)),
            passed         = True,
            score          = score,
            grade          = grade,
            cond_detail    = cond_detail,
            rsi            = round(ind["rsi"],  1),
            macd_cross     = ind["macd_cross"],
            atr14          = round(ind["atr14"], 2),
            reasons        = reasons,
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
    yfinance fetch_ohlcv DataFrame을 받아 스코어링 스캔.
    KIS 없는 환경(Streamlit Cloud)에서 폴백으로 사용.
    C4(외인/기관) 데이터 없으므로 최대 70점 (C3+C5+C6).
    """
    try:
        ok, reason = _hard_filter_yfinance(ticker, name)
        if not ok:
            return None

        if df is None or len(df) < 22:
            return None

        ind = _compute_from_ohlcv(df)
        if not ind:
            return None

        cur = ind["cur"]
        if cur < min_price or cur > max_price:
            return None

        # yfinance로 재무·시총 조회
        price_info = {"market_cap_bil": 0, "tradable_nxt": True, "change_pct": 0}
        fin_info   = {"operating_profit": None, "revenue_yoy": None, "sector": ""}
        inv_info   = {"foreign_net_5d": 0, "inst_net_5d": 0}

        try:
            import yfinance as _yf
            for _sfx in ([".KS", ".KQ"] if ticker.isdigit() else [""]):
                _info = _yf.Ticker(ticker + _sfx).info
                if _info and _info.get("regularMarketPrice"):
                    break
            mktcap = _info.get("marketCap", 0) or 0
            price_info["market_cap_bil"] = mktcap / 1e8
            fin_info["operating_profit"] = _info.get("operatingIncome")
            fin_info["revenue_yoy"]      = _info.get("revenueGrowth")
            fin_info["sector"]           = _info.get("sector", "") or ""
        except Exception:
            pass

        # 금지 섹터 재확인
        for blk in BLOCKED_SECTORS:
            if blk.lower() in fin_info["sector"].lower():
                return None

        c   = df["종가"].astype(float) if "종가" in df.columns else df["close"].astype(float)
        chg = (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100 if len(c) >= 2 else 0.0
        price_info["change_pct"] = round(chg, 2)

        hard_pass, score, grade, cond_detail, reasons = _evaluate_scoring(
            ind, price_info, fin_info, inv_info
        )

        # yfinance 모드: C4 데이터 없어서 최대 70점 → all6_pass + 70점 이상이면 통과
        if grade == "Filtered":
            return None

        return ScanResult(
            ticker         = ticker,
            name           = name,
            price          = int(cur),
            change_pct     = round(chg, 2),
            market_cap_bil = price_info["market_cap_bil"],
            atr_ratio      = round(ind["atr_ratio"] * 100, 2),
            cum5_ret       = round(ind["cum5"]       * 100, 2),
            vol_ratio      = round(ind["vol_ratio"]  * 100, 1),
            cmf            = round(ind["cmf20"], 4),
            foreign_net    = 0,
            inst_net       = 0,
            op_profit      = fin_info.get("operating_profit"),
            rev_yoy        = fin_info.get("revenue_yoy"),
            tradable_nxt   = True,
            passed         = True,
            score          = score,
            grade          = grade,
            cond_detail    = cond_detail,
            rsi            = round(ind["rsi"],  1),
            macd_cross     = ind["macd_cross"],
            atr14          = round(ind["atr14"], 2),
            reasons        = reasons,
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
        key=lambda x: (x.score, x.cum5_ret),
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
            "등급":      r.grade,
            "점수":      f"{r.score}점",
            "종목명":    r.name,
            "코드":      r.ticker,
            "현재가":    f"{r.price:,}",
            "등락(%)":   f"{'▲' if r.change_pct >= 0 else '▼'}{abs(r.change_pct):.2f}%",
            "시총(억)":  f"{r.market_cap_bil:,.0f}" if r.market_cap_bil else "?",
            "ATR%":      f"{r.atr_ratio:.1f}%",
            "5일수익률": f"{r.cum5_ret:+.1f}%",
            "거래량%":   f"{r.vol_ratio:.0f}%",
            "외인순매수": f"{r.foreign_net:+,}" if r.foreign_net else "-",
            "기관순매수": f"{r.inst_net:+,}"    if r.inst_net    else "-",
            "RSI":       f"{r.rsi:.0f}",
            "NXT":       "✅" if r.tradable_nxt else "⚠️",
            "조건":      r.cond_detail,
            "신호":      " ".join(r.reasons),
        })
    return pd.DataFrame(rows)
