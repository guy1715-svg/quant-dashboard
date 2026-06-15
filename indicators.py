"""
indicators.py — V8.9.2 기술지표 계산 모듈

변경 이력:
  V8.9.2 - Wilder 오리지널 RSI (SMA 시드 + RMA)
          - OBV 폐기 → CMF (Chaikin Money Flow) 교체
          - ATR14 표준화
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Optional


# ── 1. Wilder 오리지널 RSI ──────────────────────────────────────────────────
def calc_rsi_wilder(close: pd.Series, period: int = 14) -> pd.Series:
    """
    Wells Wilder 오리지널 RSI.

    - 첫 period일: 단순이동평균(SMA)으로 초기 avg_gain / avg_loss 시드 설정
    - 이후: Wilder Smoothing (RMA) = prev * (period-1)/period + cur / period
    - pandas ewm(alpha=1/period)은 초기값 바이어스 존재 → 본 함수로 대체

    Returns:
        pd.Series (0~100), 이름 'RSI'
    """
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta.clip(upper=0))

    rsi_vals = np.full(len(close), np.nan)

    # 충분한 데이터가 없으면 NaN 반환
    if len(close) < period + 1:
        return pd.Series(rsi_vals, index=close.index, name="RSI")

    # 시드: 첫 period개의 SMA
    avg_gain = float(gain.iloc[1 : period + 1].mean())
    avg_loss = float(loss.iloc[1 : period + 1].mean())

    # period번째 인덱스에 첫 RSI 기록
    if avg_loss == 0:
        rsi_vals[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_vals[period] = 100.0 - 100.0 / (1.0 + rs)

    # period+1 이후: Wilder Smoothing (RMA)
    alpha = 1.0 / period
    for i in range(period + 1, len(close)):
        avg_gain = avg_gain * (1 - alpha) + float(gain.iloc[i]) * alpha
        avg_loss = avg_loss * (1 - alpha) + float(loss.iloc[i]) * alpha
        if avg_loss == 0:
            rsi_vals[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_vals[i] = round(100.0 - 100.0 / (1.0 + rs), 1)

    return pd.Series(rsi_vals, index=close.index, name="RSI")


# ── 2. ATR14 (Average True Range) ────────────────────────────────────────────
def calc_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
    method: str = "wilder",   # "wilder" | "sma"
) -> pd.Series:
    """
    ATR 계산.

    method="wilder": Wilder Smoothing (HTS 기본값)
    method="sma"   : 단순이동평균 (비교용)

    Returns:
        pd.Series, 이름 'ATR{period}'
    """
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    if method == "wilder":
        # Wilder Smoothing = RMA
        atr_vals = np.full(len(tr), np.nan)
        if len(tr) >= period:
            atr_vals[period - 1] = float(tr.iloc[:period].mean())
            alpha = 1.0 / period
            for i in range(period, len(tr)):
                atr_vals[i] = atr_vals[i - 1] * (1 - alpha) + float(tr.iloc[i]) * alpha
        return pd.Series(atr_vals, index=high.index, name=f"ATR{period}")
    else:
        return tr.rolling(period).mean().rename(f"ATR{period}")


# ── 3. CMF (Chaikin Money Flow) ───────────────────────────────────────────────
def calc_cmf(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 20,
) -> pd.Series:
    """
    Chaikin Money Flow (CMF).

    수식:
      MFM (Money Flow Multiplier) = ((Close - Low) - (High - Close)) / (High - Low)
      MFV (Money Flow Volume)     = MFM × Volume
      CMF                         = Σ(MFV, period) / Σ(Volume, period)

    값 범위: -1 ~ +1
      > 0.1  : 매집 (스마트 머니 유입)
      < -0.1 : 분산 (스마트 머니 이탈)

    Returns:
        pd.Series, 이름 'CMF{period}'
    """
    hl_range = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / hl_range
    mfv = mfm * volume

    cmf = mfv.rolling(period).sum() / volume.rolling(period).sum()
    return cmf.rename(f"CMF{period}").round(4)


# ── 4. 볼린저 밴드 ────────────────────────────────────────────────────────────
def calc_bb(
    close: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    볼린저 밴드.

    Returns:
        (upper, mid, lower) — 각각 pd.Series
    """
    mid   = close.rolling(period).mean()
    std   = close.rolling(period).std(ddof=0)
    upper = (mid + std_dev * std).round(0)
    lower = (mid - std_dev * std).round(0)
    mid   = mid.round(0)
    return upper.rename("BB_upper"), mid.rename("BB_mid"), lower.rename("BB_lower")


# ── 5. MACD ───────────────────────────────────────────────────────────────────
def calc_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    MACD / Signal / Histogram.

    Returns:
        (macd, signal_line, histogram)
    """
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd     = (ema_fast - ema_slow).round(2)
    sig      = macd.ewm(span=signal, adjust=False).mean().round(2)
    hist     = (macd - sig).round(2)
    return macd.rename("MACD"), sig.rename("Signal"), hist.rename("MACD_hist")


# ── 6. 이동평균 ───────────────────────────────────────────────────────────────
def calc_ma(close: pd.Series, periods: list[int] = [5, 20, 60, 120]) -> dict[str, pd.Series]:
    """단순이동평균 딕셔너리 반환."""
    return {f"MA{p}": close.rolling(p).mean().round(2) for p in periods}


# ── 7. 통합 지표 계산 (DataFrame 입력 → 지표 컬럼 추가) ──────────────────────
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    OHLCV DataFrame에 전체 기술지표를 추가하여 반환.

    입력 컬럼 필수: 시가, 고가, 저가, 종가, 거래량
    추가 컬럼:
        RSI, ATR14
        MACD, Signal, MACD_hist
        BB_upper, BB_mid, BB_lower
        MA5, MA20, MA60, MA120
        CMF20
        거래량_비율 (당일 제외 직전 20일 평균 대비)
    """
    df = df.copy()

    c = df["종가"].astype(float)
    h = df["고가"].astype(float)
    l = df["저가"].astype(float)
    v = df["거래량"].astype(float)

    # RSI (Wilder 오리지널)
    df["RSI"] = calc_rsi_wilder(c)

    # ATR14
    df["ATR14"] = calc_atr(h, l, c, period=14, method="wilder")

    # MACD
    df["MACD"], df["Signal"], df["MACD_hist"] = calc_macd(c)

    # 볼린저 밴드
    df["BB_upper"], df["BB_mid"], df["BB_lower"] = calc_bb(c)

    # 이평선
    for name, series in calc_ma(c).items():
        df[name] = series

    # CMF20 (OBV 대체)
    df["CMF20"] = calc_cmf(h, l, c, v, period=20)

    # 거래량 비율 (당일 제외 직전 20일 평균)
    vol_avg20 = v.shift(1).rolling(20).mean()
    df["거래량_비율"] = (v / vol_avg20 * 100).round(1)

    # 52주 고저
    df["52W_high"] = h.rolling(min(252, len(df))).max()
    df["52W_low"]  = l.rolling(min(252, len(df))).min()

    return df
