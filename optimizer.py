"""
optimizer.py — V8.9.2 룰 기반 파라미터 자동 최적화 엔진

Walk-Forward Grid Search:
  - cond5 (5일 누적 수익률 하한): 5% ~ 15%, 1% 단위
  - cond6 (거래량 대비 최대 비율): 20% ~ 50%, 5% 단위
  - In-sample 4개월 최적화 → Out-of-sample 2개월 검증 (롤링)
  - 평가 지표: 샤프 지수 × 승률 (과수익률 기준 과적합 방지)
  - MDD 10% 초과 파라미터 자동 제외 (리스크 필터)
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 탐색 공간 ──────────────────────────────────────────────────────────────────
GRID_COND5 = [c / 100 for c in range(5, 16, 1)]          # 0.05 ~ 0.15
GRID_COND6 = [c / 100 for c in range(20, 55, 5)]         # 0.20 ~ 0.50

# Walk-Forward 설정
WF_IN_SAMPLE_MONTHS  = 4    # 최적화 윈도우
WF_OUT_SAMPLE_MONTHS = 2    # 검증 윈도우
WF_HOLD_DAYS         = 5    # 신호 후 보유 기간 (5거래일)
MDD_MAX              = 0.10 # MDD 10% 초과 시 제외


# ── 결과 데이터클래스 ──────────────────────────────────────────────────────────
@dataclass
class OptResult:
    cond5: float
    cond6: float
    win_rate: float       # 승률 (%)
    sharpe:   float       # 샤프 지수 (연간환산)
    mdd:      float       # 최대낙폭 (%)
    n_trades: int         # 신호 수
    avg_ret:  float       # 평균 수익률 (%)
    score:    float       # 종합 점수 (sharpe × win_rate)
    is_oos:   bool = False  # Out-of-sample 결과 여부


@dataclass
class WalkForwardReport:
    best_cond5:   float
    best_cond6:   float
    oos_win_rate: float
    oos_sharpe:   float
    oos_mdd:      float
    oos_n_trades: int
    oos_avg_ret:  float
    window_results: List[Dict] = field(default_factory=list)
    grid_summary:   pd.DataFrame = field(default_factory=pd.DataFrame)
    timestamp:      str = ""


# ── 핵심 백테스트 함수 ────────────────────────────────────────────────────────
def _apply_v892_conditions(
    df:    pd.DataFrame,
    cond5: float,
    cond6: float,
    # 고정 조건 (V8.9.2 기본값)
    cond2_atr_ratio: float = 0.035,
    cond3_rsi_min:   float = 40.0,
    cond3_rsi_max:   float = 65.0,
    cond4_cmf_min:   float = 0.05,
) -> pd.Series:
    """
    V8.9.2 6대 조건 적용 → 신호 시리즈(bool) 반환.
    cond1(시총) 제외 — 단일 종목 df에선 적용 불가.
    """
    from indicators import calc_rsi_wilder, calc_atr, calc_cmf

    if len(df) < 25:
        return pd.Series(False, index=df.index)

    c = df["종가"].astype(float)
    h = df["고가"].astype(float)
    l = df["저가"].astype(float)
    v = df["거래량"].astype(float)

    # ATR14 비율 (cond2)
    atr14 = calc_atr(h, l, c, period=14, method="wilder")
    atr_ratio = atr14 / c

    # RSI (cond3)
    rsi = calc_rsi_wilder(c, period=14)

    # CMF20 (cond4)
    cmf = calc_cmf(h, l, c, v, period=20)

    # 5일 누적 수익률 (cond5)
    cum5_ret = c.pct_change(5)

    # 거래량 / 20일 최대 거래량 (cond6)
    vol_max20 = v.rolling(20).max().shift(1)
    vol_ratio = v / vol_max20

    signal = (
        (atr_ratio >= cond2_atr_ratio) &
        (rsi >= cond3_rsi_min) & (rsi <= cond3_rsi_max) &
        (cmf > cond4_cmf_min) &
        (cum5_ret >= cond5) &
        (vol_ratio < cond6)
    )
    return signal.fillna(False)


def _backtest_single(
    df:    pd.DataFrame,
    cond5: float,
    cond6: float,
    hold_days: int = WF_HOLD_DAYS,
) -> OptResult:
    """단일 파라미터 조합으로 백테스트 → OptResult."""
    signal = _apply_v892_conditions(df, cond5, cond6)
    c = df["종가"].astype(float).reset_index(drop=True)
    sig = signal.reset_index(drop=True)

    returns = []
    i = 0
    while i < len(sig) - hold_days:
        if sig.iloc[i]:
            entry = float(c.iloc[i])
            exit_ = float(c.iloc[i + hold_days])
            if entry > 0:
                returns.append((exit_ - entry) / entry)
            i += hold_days  # 겹치지 않도록 skip
        else:
            i += 1

    n = len(returns)
    if n < 3:
        return OptResult(cond5, cond6, 0.0, -9.9, 1.0, n, 0.0, -9.9)

    arr = np.array(returns)
    win_rate = float(np.mean(arr > 0)) * 100
    avg_ret  = float(np.mean(arr)) * 100
    std_ret  = float(np.std(arr))

    # 샤프 지수 (연간환산: 1 hold_period = 5영업일 ≈ 252/5=50.4 periods/year)
    periods_per_year = 252 / hold_days
    sharpe = (float(np.mean(arr)) / std_ret * np.sqrt(periods_per_year)) if std_ret > 0 else 0.0

    # MDD (누적 수익곡선 기준)
    eq = np.cumprod(1 + arr)
    peak = np.maximum.accumulate(eq)
    drawdown = (peak - eq) / peak
    mdd = float(np.max(drawdown))

    score = sharpe * (win_rate / 100)

    return OptResult(cond5, cond6, win_rate, round(sharpe, 3), round(mdd, 4), n, round(avg_ret, 2), round(score, 4))


# ── Walk-Forward 분석 ─────────────────────────────────────────────────────────
def run_walk_forward(
    ticker_dfs: Dict[str, pd.DataFrame],
    in_months:  int = WF_IN_SAMPLE_MONTHS,
    out_months: int = WF_OUT_SAMPLE_MONTHS,
    progress_cb = None,          # (current, total) → None
) -> WalkForwardReport:
    """
    Walk-Forward 파라미터 최적화.

    ticker_dfs: {ticker: OHLCV DataFrame (인덱스=날짜, 컬럼=시가/고가/저가/종가/거래량)}
    progress_cb: 진행률 콜백 (Streamlit progress bar용)

    알고리즘:
      1. 전체 날짜 범위를 (in_months + out_months) 단위 윈도우로 슬라이딩
      2. 각 윈도우 In-sample → Grid Search → 최적 파라미터 선택
      3. Out-of-sample로 선택 파라미터 검증
      4. OOS 결과 집계 → 최종 추천 파라미터
    """
    if not ticker_dfs:
        raise ValueError("ticker_dfs가 비어있습니다.")

    # ── 날짜 범위 추출 ──
    all_dates: List[pd.Timestamp] = []
    for df in ticker_dfs.values():
        if df is not None and len(df) > 0:
            all_dates.extend(df.index.tolist())
    if not all_dates:
        raise ValueError("유효한 데이터가 없습니다.")

    all_dates_sorted = sorted(set(all_dates))
    total_start = all_dates_sorted[0]
    total_end   = all_dates_sorted[-1]

    window_size  = timedelta(days=30 * (in_months + out_months))
    slide_size   = timedelta(days=30 * out_months)
    in_duration  = timedelta(days=30 * in_months)

    # ── 윈도우 생성 ──
    windows = []
    win_start = total_start
    while win_start + window_size <= total_end:
        in_end  = win_start + in_duration
        out_end = win_start + window_size
        windows.append((win_start, in_end, in_end, out_end))
        win_start += slide_size

    if not windows:
        # 데이터 부족 시 전체를 in-sample로 단일 최적화
        windows = [(total_start, total_end, total_end, total_end)]

    grid = list(itertools.product(GRID_COND5, GRID_COND6))
    total_steps = len(windows) * len(grid)
    step = 0

    window_results = []
    oos_returns_all = []

    for w_idx, (is_start, is_end, oos_start, oos_end) in enumerate(windows):
        # ── In-sample 최적화 ──
        best_score = -999
        best_params = (GRID_COND5[3], GRID_COND6[3])   # 기본값 8%, 35%
        grid_rows   = []

        for cond5, cond6 in grid:
            step += 1
            if progress_cb:
                progress_cb(step, total_steps)

            combined_results = []
            for df in ticker_dfs.values():
                if df is None or len(df) < 30:
                    continue
                mask = (df.index >= is_start) & (df.index <= is_end)
                sub  = df[mask]
                if len(sub) < 25:
                    continue
                res = _backtest_single(sub, cond5, cond6)
                if res.n_trades > 0 and res.mdd <= MDD_MAX:
                    combined_results.append(res)

            if not combined_results:
                grid_rows.append({
                    "cond5": cond5, "cond6": cond6,
                    "win_rate": 0, "sharpe": -9.9, "mdd": 1.0,
                    "n_trades": 0, "score": -9.9,
                })
                continue

            # 종목별 결과 집계 (동일가중)
            agg_win  = np.mean([r.win_rate for r in combined_results])
            agg_sh   = np.mean([r.sharpe   for r in combined_results])
            agg_mdd  = np.mean([r.mdd      for r in combined_results])
            agg_n    = sum(r.n_trades for r in combined_results)
            agg_sc   = agg_sh * (agg_win / 100)

            grid_rows.append({
                "cond5": cond5, "cond6": cond6,
                "win_rate": round(agg_win, 1),
                "sharpe":   round(agg_sh, 3),
                "mdd":      round(agg_mdd, 4),
                "n_trades": agg_n,
                "score":    round(agg_sc, 4),
            })

            if agg_sc > best_score:
                best_score  = agg_sc
                best_params = (cond5, cond6)

        # ── Out-of-sample 검증 ──
        oos_c5, oos_c6 = best_params
        oos_rets = []
        for df in ticker_dfs.values():
            if df is None or len(df) < 30:
                continue
            mask = (df.index > oos_start) & (df.index <= oos_end)
            sub  = df[mask]
            if len(sub) < 10:
                continue
            res = _backtest_single(sub, oos_c5, oos_c6)
            if res.n_trades > 0:
                oos_rets.append(res)

        oos_win  = np.mean([r.win_rate for r in oos_rets]) if oos_rets else 0
        oos_sh   = np.mean([r.sharpe   for r in oos_rets]) if oos_rets else 0
        oos_mdd  = np.mean([r.mdd      for r in oos_rets]) if oos_rets else 0
        oos_n    = sum(r.n_trades for r in oos_rets)

        window_results.append({
            "window": f"{is_start.strftime('%y.%m')}~{oos_end.strftime('%y.%m')}",
            "best_cond5": oos_c5,
            "best_cond6": oos_c6,
            "is_score":   round(best_score, 4),
            "oos_win_rate": round(oos_win, 1),
            "oos_sharpe":   round(oos_sh, 3),
            "oos_mdd":      round(oos_mdd * 100, 2),
            "oos_trades":   oos_n,
        })

        oos_returns_all.extend(oos_rets)

    # ── 최종 파라미터: OOS 결과가 가장 많이 선택된 조합 ──
    if window_results:
        param_votes: Dict[Tuple, int] = {}
        for wr in window_results:
            k = (wr["best_cond5"], wr["best_cond6"])
            param_votes[k] = param_votes.get(k, 0) + 1
        final_params = max(param_votes, key=lambda k: param_votes[k])
    else:
        final_params = (0.08, 0.50)

    # OOS 집계 지표
    f_win = np.mean([r.win_rate for r in oos_returns_all]) if oos_returns_all else 0
    f_sh  = np.mean([r.sharpe   for r in oos_returns_all]) if oos_returns_all else 0
    f_mdd = np.mean([r.mdd      for r in oos_returns_all]) if oos_returns_all else 0
    f_n   = sum(r.n_trades for r in oos_returns_all)
    f_avg = np.mean([r.avg_ret  for r in oos_returns_all]) if oos_returns_all else 0

    # 전체 그리드 요약 (마지막 윈도우 기준)
    grid_df = pd.DataFrame(grid_rows).sort_values("score", ascending=False)

    return WalkForwardReport(
        best_cond5   = final_params[0],
        best_cond6   = final_params[1],
        oos_win_rate = round(float(f_win), 1),
        oos_sharpe   = round(float(f_sh),  3),
        oos_mdd      = round(float(f_mdd) * 100, 2),
        oos_n_trades = int(f_n),
        oos_avg_ret  = round(float(f_avg), 2),
        window_results = window_results,
        grid_summary   = grid_df,
        timestamp      = datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


# ── 데이터 다운로드 헬퍼 (yfinance) ─────────────────────────────────────────
def fetch_ohlcv_for_optimization(
    tickers: List[Tuple[str, str]],
    months:  int = 6,
    progress_cb = None,
) -> Dict[str, pd.DataFrame]:
    """
    yfinance로 최근 N개월 OHLCV 다운로드.
    한국 종목: {ticker}.KS / {ticker}.KQ 자동 시도
    """
    import yfinance as yf

    end   = datetime.now()
    start = end - timedelta(days=30 * months + 10)

    result = {}
    for i, (ticker, name) in enumerate(tickers):
        if progress_cb:
            progress_cb(i + 1, len(tickers))
        try:
            is_kr = ticker.isdigit()
            if is_kr:
                # .KS 시도 → 실패 시 .KQ
                for suffix in [".KS", ".KQ"]:
                    raw = yf.download(
                        ticker + suffix,
                        start=start.strftime("%Y-%m-%d"),
                        end=end.strftime("%Y-%m-%d"),
                        progress=False, auto_adjust=True,
                    )
                    if raw is not None and len(raw) > 20:
                        break
            else:
                raw = yf.download(
                    ticker,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    progress=False, auto_adjust=True,
                )

            if raw is None or len(raw) < 20:
                continue

            # 컬럼 정규화
            raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
            df = raw.rename(columns={
                "Open": "시가", "High": "고가", "Low": "저가",
                "Close": "종가", "Volume": "거래량",
            })
            df.index = pd.to_datetime(df.index)
            result[ticker] = df[["시가", "고가", "저가", "종가", "거래량"]].dropna()

        except Exception:
            continue

    return result
