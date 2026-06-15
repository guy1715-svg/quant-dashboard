"""
paper_trading.py — V8.9.2 가상 계좌 & 손절가 모듈

V8.9.2 변경사항:
  - 고정 -7% 킬스위치 → ATR 기반 동적 손절가 (진입가 - 1.5 × ATR14)
  - 하드 서킷 브레이커 -10% 절대 방어선 병행 유지 (V8.9.1 계승)
  - 슬리피지 · 수수료 · 양도세 계산 분리
"""

from __future__ import annotations

from typing import Optional, Tuple


# ── 1. 동적 손절가 계산 ───────────────────────────────────────────────────────
def calc_dynamic_stoploss(
    entry_price: float,
    atr14:       float,
    multiplier:  float = 1.5,
    hard_floor:  float = 0.10,  # 하드 서킷 -10% 절대 방어선
) -> Tuple[float, float]:
    """
    V8.9.2 동적 손절가.

    동적 손절가 = 진입가 - (multiplier × ATR14)
    하드 서킷   = 진입가 × (1 - hard_floor)

    최종 손절가 = max(동적 손절가, 하드 서킷)
    → 동적 손절이 하드 서킷보다 낮으면 하드 서킷이 우선

    Returns:
        (dynamic_stop, hard_circuit)
    """
    dynamic_stop  = entry_price - multiplier * atr14
    hard_circuit  = entry_price * (1 - hard_floor)
    return round(dynamic_stop, 2), round(hard_circuit, 2)


def get_effective_stoploss(entry_price: float, atr14: float) -> float:
    """
    실질 적용 손절가 반환 (dynamic vs hard 중 높은 쪽 = 더 보수적).
    """
    dynamic, hard = calc_dynamic_stoploss(entry_price, atr14)
    return max(dynamic, hard)


# ── 2. 킬스위치 판정 ─────────────────────────────────────────────────────────
def check_killswitch(
    entry_price:   float,
    current_price: float,
    atr14:         Optional[float] = None,
) -> Tuple[str, str]:
    """
    V8.9.2 킬스위치 판정.

    우선순위:
      1. 하드 서킷 브레이커 (-10%) — 즉각 시장가 청산 (V8.9.1 계승)
      2. 동적 손절가 (진입가 - 1.5×ATR14) — 청산 권고
      3. 고정 -7% 폴백 (ATR 데이터 없을 시)

    Returns:
        (action, message)
        action: "EXECUTE_MARKET_SELL" | "RECOMMEND_SELL" | "HOLD"
    """
    if entry_price <= 0:
        return "HOLD", ""

    chg_pct = (current_price - entry_price) / entry_price * 100

    # ── 하드 서킷 브레이커 (-10%) — 최우선, 절대 방어선 ──
    if chg_pct <= -10.0:
        return (
            "EXECUTE_MARKET_SELL",
            f"🚨 하드 서킷 브레이커! {chg_pct:.2f}% (-10% 절대 방어선) → 즉각 시장가 청산",
        )

    # ── 동적 손절가 (ATR 기반) ──
    if atr14 and atr14 > 0:
        dynamic_stop, _ = calc_dynamic_stoploss(entry_price, atr14)
        if current_price <= dynamic_stop:
            atr_pct = (current_price - entry_price) / entry_price * 100
            return (
                "RECOMMEND_SELL",
                f"⚠️ 동적 손절가 발동! 현재가 {current_price:,.0f} ≤ 손절가 {dynamic_stop:,.0f} "
                f"({atr_pct:.2f}%, ATR×1.5) → 청산 권고",
            )
    else:
        # ATR 데이터 없을 시 -7% 폴백
        if chg_pct <= -7.0:
            return (
                "RECOMMEND_SELL",
                f"⚠️ 고정 손절가 발동 ({chg_pct:.2f}%, -7% 폴백) → 청산 권고",
            )

    return "HOLD", ""


# ── 3. 슬리피지 · 수수료 · 세금 ──────────────────────────────────────────────
# 기본 설정값 (사용자 UI에서 override 가능)
DEFAULT_FEE_BUY   = 0.015   # 매수 수수료 (%)
DEFAULT_FEE_SELL  = 0.015   # 매도 수수료 (%)
DEFAULT_SLIP      = 0.10    # 슬리피지 (%)
DEFAULT_TAX_KR    = 0.20    # 한국 주식 양도세 (%) — 소액 면세 적용 가능
DEFAULT_TAX_US    = 22.0    # 미국 주식 양도세 (%)


def calc_buy_price(
    price:   float,
    is_kr:   bool,
    fee_pct: float = DEFAULT_FEE_BUY,
    slip_pct: float = DEFAULT_SLIP,
) -> float:
    """매수 체결가 (슬리피지 + 수수료 포함, 위로)."""
    cost = (fee_pct + slip_pct) / 100
    return round(price * (1 + cost), 2 if not is_kr else 0)


def calc_sell_price(
    price:   float,
    is_kr:   bool,
    fee_pct: float = DEFAULT_FEE_SELL,
    slip_pct: float = DEFAULT_SLIP,
) -> float:
    """매도 체결가 (슬리피지 + 수수료 포함, 아래로)."""
    cost = (fee_pct + slip_pct) / 100
    return round(price * (1 - cost), 2 if not is_kr else 0)


# ── 4. 포트폴리오 가치 계산 ───────────────────────────────────────────────────
def calc_portfolio_value(
    positions:   list,
    current_prices: dict,   # {ticker: float}
    usd_krw:     float = 1350.0,
    is_korean_fn = None,    # is_korean_ticker 함수 주입
) -> float:
    """
    보유 포지션 원화 환산 총평가금액.

    positions: [{'ticker':str, 'qty':int, 'avg_price':float}, ...]
    current_prices: {ticker: 현재가(원 or USD)}
    """
    total = 0.0
    for pos in positions:
        ticker = pos["ticker"]
        qty    = pos.get("qty", 0)
        avg    = pos.get("avg_price", 0)
        is_kr  = (is_korean_fn(ticker) if is_korean_fn else ticker.isdigit())
        fx     = 1.0 if is_kr else usd_krw
        cur    = current_prices.get(ticker, avg)
        total += float(cur) * qty * fx
    return total


# ── 5. 손절가 표시 텍스트 ─────────────────────────────────────────────────────
def format_stoploss_label(
    entry_price: float,
    atr14:       Optional[float],
    is_kr:       bool,
) -> str:
    """포지션 카드에 표시할 손절가 텍스트."""
    sym = "원" if is_kr else "$"
    if atr14 and atr14 > 0:
        dynamic, hard = calc_dynamic_stoploss(entry_price, atr14)
        effective     = max(dynamic, hard)
        eff_pct       = (effective - entry_price) / entry_price * 100
        return (
            f"동적 손절가: {effective:,.0f}{sym} ({eff_pct:.1f}%)"
            f" | 하드서킷: {hard:,.0f}{sym} (-10%)"
        )
    else:
        hard    = entry_price * 0.93
        hard_10 = entry_price * 0.90
        return (
            f"손절가: {hard:,.0f}{sym} (-7% 폴백)"
            f" | 하드서킷: {hard_10:,.0f}{sym} (-10%)"
        )
