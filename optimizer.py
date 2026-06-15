"""
optimizer.py — V8.9.3 Walk-Forward 파라미터 자동 최적화 엔진

[V8.9.3 퀀트 감사 반영 4대 수정]
  1. 진입가: 신호 당일 종가 → 다음 날 시가(Open[i+1]) + 양방향 슬리피지 0.15% × 2
  2. 동적 청산: 5일 고정 → ATR 손절(entry - 1.5×ATR) / 목표가(entry + 3.0×ATR, R:R=2.0)
  3. Plateau 탐색: 단일 최고점 → 이웃 파라미터 평균 고려한 안정적 언덕 중앙값
  4. 샤프 교정: rf=3.5%, 일별 수익률 기반 √252, 최소 거래 30회 미만 제외
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── 탐색 공간 ──────────────────────────────────────────────────────────────────
GRID_COND5 = [c / 100 for c in range(5, 16, 1)]   # 0.05 ~ 0.15 (11개)
GRID_COND6 = [c / 100 for c in range(20, 55, 5)]  # 0.20 ~ 0.50 (7개)

# Walk-Forward 설정
WF_IN_SAMPLE_MONTHS  = 4     # 최적화 윈도우
WF_OUT_SAMPLE_MONTHS = 2     # 검증 윈도우

# 거래비용 (슬리피지 + 수수료 + 세금 합산)
COST_PER_SIDE = 0.0015       # 0.15% × 매수/매도 각각
TOTAL_COST    = COST_PER_SIDE * 2  # 0.30%

# 동적 청산 파라미터
ATR_STOP_MULT   = 1.5        # 손절: entry - 1.5 × ATR14
RR_RATIO        = 2.0        # 익절: entry + ATR_STOP_MULT × RR_RATIO × ATR14
MAX_HOLD_DAYS   = 20         # 최대 보유 기간 (어느 조건도 미충족 시 강제 청산)

# 샤프 지수 교정
RF_ANNUAL       = 0.035      # 무위험수익률 연 3.5% (한국 국고채 기준)
RF_DAILY        = RF_ANNUAL / 252
SHARPE_ANNUALIZE = np.sqrt(252)  # 일별 수익률 기반 연간환산

# 통계적 유효성 필터
MIN_TRADES      = 30         # 윈도우당 최소 거래 수 (미달 시 제외)
MDD_MAX         = 0.10       # MDD 10% 초과 시 제외

# Plateau 탐색 설정
PLATEAU_NEIGHBOR_RADIUS = 1  # 이웃 탐색 반경 (cond5: ±1단계, cond6: ±1단계)
PLATEAU_WEIGHT_CENTER   = 0.5   # 중심 점수 가중치
PLATEAU_WEIGHT_NEIGHBOR = 0.5   # 이웃 평균 점수 가중치


# ── 결과 데이터클래스 ──────────────────────────────────────────────────────────
@dataclass
class OptResult:
    cond5:    float
    cond6:    float
    win_rate: float    # 승률 (%)
    sharpe:   float    # 초과 수익 샤프 지수 (연간환산, rf=3.5%)
    mdd:      float    # 최대낙폭 (소수점)
    n_trades: int      # 거래 횟수
    avg_ret:  float    # 평균 순수익률 (%, 거래비용 차감 후)
    score:    float    # 종합 점수 (plateau 가중 sharpe × 승률)
    plateau_score: float = 0.0   # 이웃 안정성 반영 점수
    is_oos:   bool = False


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


# ── 1. 동적 청산 시뮬레이션 ───────────────────────────────────────────────────
def _simulate_trade(
    entry_open: float,
    atr14_entry: float,
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
) -> float:
    """
    진입 후 일별 고가/저가를 추적하여 동적 청산 수행.

    청산 우선순위 (하루 내 동시 도달 시):
      1. 손절가(stop) 먼저 확인 → 손절 처리 (보수적)
      2. 익절가(target) 확인
      3. MAX_HOLD_DAYS 도달 → 종가 청산

    Returns:
        순수익률 (거래비용 차감 후, 소수점)
    """
    if entry_open <= 0 or atr14_entry <= 0:
        return 0.0

    stop_price   = entry_open - ATR_STOP_MULT * atr14_entry
    target_price = entry_open + ATR_STOP_MULT * RR_RATIO * atr14_entry

    exit_price = None
    n = len(closes)

    for day in range(min(MAX_HOLD_DAYS, n)):
        low_d  = float(lows.iloc[day])
        high_d = float(highs.iloc[day])

        # 손절 먼저 확인 (보수적)
        if low_d <= stop_price:
            exit_price = stop_price
            break

        # 익절 확인
        if high_d >= target_price:
            exit_price = target_price
            break

    # 최대 보유 기간 도달 → 종가 청산
    if exit_price is None:
        exit_price = float(closes.iloc[min(MAX_HOLD_DAYS - 1, n - 1)])

    gross_ret = (exit_price - entry_open) / entry_open
    net_ret   = gross_ret - TOTAL_COST     # 슬리피지 + 수수료 차감
    return net_ret


# ── 2. V8.9.2 조건 신호 생성 ─────────────────────────────────────────────────
def _apply_v892_conditions(
    df:    pd.DataFrame,
    cond5: float,
    cond6: float,
    cond2_atr_ratio: float = 0.035,
    cond3_rsi_min:   float = 40.0,
    cond3_rsi_max:   float = 65.0,
    cond4_cmf_min:   float = 0.05,
) -> pd.Series:
    """
    V8.9.2 6대 조건 → 신호 시리즈(bool) 반환.

    중요: 모든 지표 계산에 shift(1) 또는 당일 기준 데이터만 사용.
    미래 참조 편향 없음.
    """
    from indicators import calc_rsi_wilder, calc_atr, calc_cmf

    if len(df) < 25:
        return pd.Series(False, index=df.index)

    c = df["종가"].astype(float)
    h = df["고가"].astype(float)
    l = df["저가"].astype(float)
    v = df["거래량"].astype(float)

    atr14     = calc_atr(h, l, c, period=14, method="wilder")
    atr_ratio = atr14 / c
    rsi       = calc_rsi_wilder(c, period=14)
    cmf       = calc_cmf(h, l, c, v, period=20)
    cum5_ret  = c.pct_change(5)

    # 거래량 비율: 당일 제외 직전 20일 최대 (shift(1) → 미래 참조 없음)
    vol_max20 = v.shift(1).rolling(20).max()
    vol_ratio = v / vol_max20

    signal = (
        (atr_ratio >= cond2_atr_ratio) &
        (rsi >= cond3_rsi_min) & (rsi <= cond3_rsi_max) &
        (cmf > cond4_cmf_min) &
        (cum5_ret >= cond5) &
        (vol_ratio < cond6)
    )
    return signal.fillna(False)


# ── 3. 단일 파라미터 조합 백테스트 ───────────────────────────────────────────
def _backtest_single(
    df:    pd.DataFrame,
    cond5: float,
    cond6: float,
) -> OptResult:
    """
    V8.9.3 백테스트:
      - 진입: 신호 당일(i) 다음 날 시가 Open[i+1]
      - 청산: ATR 동적 손절/익절 (최대 MAX_HOLD_DAYS일)
      - 거래비용: 총 0.30% 차감
      - 연속 신호: 보유 중 새 신호 무시 (비겹침)
    """
    from indicators import calc_atr

    if len(df) < 30:
        return OptResult(cond5, cond6, 0.0, -9.9, 1.0, 0, 0.0, -9.9)

    signal = _apply_v892_conditions(df, cond5, cond6)

    c    = df["종가"].astype(float).reset_index(drop=True)
    h    = df["고가"].astype(float).reset_index(drop=True)
    l    = df["저가"].astype(float).reset_index(drop=True)
    o    = df["시가"].astype(float).reset_index(drop=True)
    sig  = signal.reset_index(drop=True)

    # ATR14 배열 (신호 시점 ATR 사용)
    atr_series = calc_atr(
        df["고가"].astype(float),
        df["저가"].astype(float),
        df["종가"].astype(float),
        period=14, method="wilder"
    ).reset_index(drop=True)

    daily_returns: List[float] = []   # 거래별 일평균 수익률 (샤프 계산용)
    trade_returns: List[float] = []   # 거래별 순수익률
    hold_days_list: List[int] = []

    i = 0
    while i < len(sig) - 2:
        if not sig.iloc[i]:
            i += 1
            continue

        # 다음 날 시가 진입 (i+1) — 미래 참조 없음
        entry_idx = i + 1
        if entry_idx >= len(o):
            break

        entry_open  = float(o.iloc[entry_idx])
        atr14_entry = float(atr_series.iloc[i])   # 신호 당일 ATR 사용

        if entry_open <= 0 or np.isnan(atr14_entry) or atr14_entry <= 0:
            i += 1
            continue

        # 동적 청산 시뮬레이션 (entry_idx+1 부터)
        trade_start = entry_idx + 1
        trade_end   = min(trade_start + MAX_HOLD_DAYS, len(c))

        if trade_start >= len(c):
            break

        net_ret = _simulate_trade(
            entry_open   = entry_open,
            atr14_entry  = atr14_entry,
            highs        = h.iloc[trade_start:trade_end],
            lows         = l.iloc[trade_start:trade_end],
            closes       = c.iloc[trade_start:trade_end],
        )

        # 실제 보유 기간 역산 (샤프 일별 환산용)
        days_held = _calc_hold_days(
            entry_open, atr14_entry,
            h.iloc[trade_start:trade_end],
            l.iloc[trade_start:trade_end],
        )
        days_held = max(days_held, 1)

        trade_returns.append(net_ret)
        daily_returns.append(net_ret / days_held)   # 일별 수익률 근사
        hold_days_list.append(days_held)

        # 보유 기간 동안 새 신호 무시
        i = trade_start + days_held

    n = len(trade_returns)
    if n < MIN_TRADES:
        return OptResult(cond5, cond6, 0.0, -9.9, 1.0, n, 0.0, -9.9)

    arr      = np.array(trade_returns)
    daily    = np.array(daily_returns)

    win_rate = float(np.mean(arr > 0)) * 100
    avg_ret  = float(np.mean(arr)) * 100

    # 샤프: 일별 초과수익률 기반, rf=3.5%/252
    excess_daily = daily - RF_DAILY
    std_daily    = float(np.std(excess_daily, ddof=1))
    sharpe = (float(np.mean(excess_daily)) / std_daily * SHARPE_ANNUALIZE
              if std_daily > 0 else 0.0)

    # MDD
    eq        = np.cumprod(1 + arr)
    peak      = np.maximum.accumulate(eq)
    drawdown  = (peak - eq) / peak
    mdd       = float(np.max(drawdown))

    score = sharpe * (win_rate / 100)

    return OptResult(
        cond5, cond6,
        round(win_rate, 1), round(sharpe, 3), round(mdd, 4),
        n, round(avg_ret, 2), round(score, 4),
    )


def _calc_hold_days(
    entry_open:  float,
    atr14_entry: float,
    highs:       pd.Series,
    lows:        pd.Series,
) -> int:
    """실제 보유 기간 계산 (동적 청산 기준)."""
    stop_price   = entry_open - ATR_STOP_MULT * atr14_entry
    target_price = entry_open + ATR_STOP_MULT * RR_RATIO * atr14_entry
    for day in range(len(lows)):
        if float(lows.iloc[day]) <= stop_price:
            return day + 1
        if float(highs.iloc[day]) >= target_price:
            return day + 1
    return min(MAX_HOLD_DAYS, len(lows))


# ── 4. Plateau (안정적 언덕) 탐색 ────────────────────────────────────────────
def _calc_plateau_scores(
    grid_rows: List[Dict],
) -> List[Dict]:
    """
    각 파라미터 조합에 대해 '이웃 안정성' 가중 점수를 계산.

    plateau_score = PLATEAU_WEIGHT_CENTER × score
                  + PLATEAU_WEIGHT_NEIGHBOR × mean(이웃 score)

    이웃: cond5 ±1단계, cond6 ±1단계 (대각선 포함 8방향)
    n_trades < MIN_TRADES 조합의 점수는 -9.9로 패널티.
    """
    score_map: Dict[Tuple, float] = {}
    for row in grid_rows:
        k = (round(row["cond5"], 4), round(row["cond6"], 4))
        score_map[k] = row["score"] if row["n_trades"] >= MIN_TRADES else -9.9

    c5_step = 0.01
    c6_step = 0.05

    result = []
    for row in grid_rows:
        c5 = round(row["cond5"], 4)
        c6 = round(row["cond6"], 4)
        center_score = score_map.get((c5, c6), -9.9)

        neighbor_scores = []
        for dc5 in [-c5_step, 0, c5_step]:
            for dc6 in [-c6_step, 0, c6_step]:
                if dc5 == 0 and dc6 == 0:
                    continue
                nk = (round(c5 + dc5, 4), round(c6 + dc6, 4))
                if nk in score_map:
                    neighbor_scores.append(score_map[nk])

        neighbor_avg = float(np.mean(neighbor_scores)) if neighbor_scores else center_score
        plateau = (PLATEAU_WEIGHT_CENTER * center_score
                   + PLATEAU_WEIGHT_NEIGHBOR * neighbor_avg)

        new_row = dict(row)
        new_row["plateau_score"] = round(plateau, 4)
        result.append(new_row)

    return result


def _select_plateau_center(grid_rows_with_plateau: List[Dict]) -> Tuple[float, float]:
    """
    Plateau 점수 기준 상위 구간에서 cond5/cond6 중앙값 반환.

    상위 20% 조합을 "안정적 언덕"으로 정의 → 해당 구간의 중앙값 선택.
    유효 조합(n_trades >= MIN_TRADES) 없으면 기본값 (0.08, 0.35) 반환.
    """
    valid = [r for r in grid_rows_with_plateau
             if r["n_trades"] >= MIN_TRADES and r["plateau_score"] > -9.0]

    if not valid:
        return (0.08, 0.35)

    valid_sorted = sorted(valid, key=lambda r: r["plateau_score"], reverse=True)
    top_n = max(1, len(valid_sorted) // 5)    # 상위 20%
    top   = valid_sorted[:top_n]

    med_c5 = float(np.median([r["cond5"] for r in top]))
    med_c6 = float(np.median([r["cond6"] for r in top]))

    # 탐색 격자 중 가장 가까운 값으로 스냅
    best_c5 = min(GRID_COND5, key=lambda v: abs(v - med_c5))
    best_c6 = min(GRID_COND6, key=lambda v: abs(v - med_c6))
    return (best_c5, best_c6)


# ── 5. Walk-Forward 전체 실행 ─────────────────────────────────────────────────
def run_walk_forward(
    ticker_dfs: Dict[str, pd.DataFrame],
    in_months:  int = WF_IN_SAMPLE_MONTHS,
    out_months: int = WF_OUT_SAMPLE_MONTHS,
    progress_cb = None,
) -> WalkForwardReport:
    """
    Walk-Forward 파라미터 최적화 (V8.9.3).

    알고리즘:
      1. 전체 날짜를 (in+out)개월 윈도우로 슬라이딩
      2. IS 기간: Grid Search → Plateau 점수 → 안정적 언덕 중앙값 선택
      3. OOS 기간: IS 선택 파라미터 검증
      4. 최종 파라미터: OOS 다수결 (동률 시 OOS 샤프 합산 기준)
    """
    if not ticker_dfs:
        raise ValueError("ticker_dfs가 비어있습니다.")

    all_dates: List[pd.Timestamp] = []
    for df in ticker_dfs.values():
        if df is not None and len(df) > 0:
            all_dates.extend(df.index.tolist())
    if not all_dates:
        raise ValueError("유효한 데이터가 없습니다.")

    all_dates_sorted = sorted(set(all_dates))
    total_start = all_dates_sorted[0]
    total_end   = all_dates_sorted[-1]

    window_size = timedelta(days=30 * (in_months + out_months))
    slide_size  = timedelta(days=30 * out_months)
    in_duration = timedelta(days=30 * in_months)

    windows = []
    win_start = total_start
    while win_start + window_size <= total_end:
        in_end  = win_start + in_duration
        out_end = win_start + window_size
        windows.append((win_start, in_end, in_end, out_end))
        win_start += slide_size

    if not windows:
        windows = [(total_start, total_end, total_end, total_end)]

    grid = list(itertools.product(GRID_COND5, GRID_COND6))
    total_steps = len(windows) * len(grid)
    step = 0

    window_results  = []
    oos_returns_all: List[OptResult] = []
    last_grid_rows  = []

    for is_start, is_end, oos_start, oos_end in windows:

        # ── IS: Grid Search ──
        grid_rows: List[Dict] = []

        for cond5, cond6 in grid:
            step += 1
            if progress_cb:
                progress_cb(step, total_steps)

            ticker_results: List[OptResult] = []
            for df in ticker_dfs.values():
                if df is None or len(df) < 30:
                    continue
                mask = (df.index >= is_start) & (df.index <= is_end)
                sub  = df[mask]
                if len(sub) < 30:
                    continue
                res = _backtest_single(sub, cond5, cond6)
                if res.n_trades >= MIN_TRADES and res.mdd <= MDD_MAX:
                    ticker_results.append(res)

            if not ticker_results:
                grid_rows.append({
                    "cond5": cond5, "cond6": cond6,
                    "win_rate": 0.0, "sharpe": -9.9,
                    "mdd": 1.0, "n_trades": 0, "score": -9.9,
                })
                continue

            agg_win = float(np.mean([r.win_rate for r in ticker_results]))
            agg_sh  = float(np.mean([r.sharpe   for r in ticker_results]))
            agg_mdd = float(np.mean([r.mdd      for r in ticker_results]))
            agg_n   = sum(r.n_trades for r in ticker_results)
            agg_sc  = agg_sh * (agg_win / 100)

            grid_rows.append({
                "cond5": cond5, "cond6": cond6,
                "win_rate": round(agg_win, 1),
                "sharpe":   round(agg_sh, 3),
                "mdd":      round(agg_mdd, 4),
                "n_trades": agg_n,
                "score":    round(agg_sc, 4),
            })

        # ── Plateau 점수 계산 → 안정적 언덕 중앙값 선택 ──
        grid_rows_p  = _calc_plateau_scores(grid_rows)
        best_c5, best_c6 = _select_plateau_center(grid_rows_p)
        last_grid_rows   = grid_rows_p   # 히트맵용 보관

        # ── OOS 검증 ──
        oos_results: List[OptResult] = []
        for df in ticker_dfs.values():
            if df is None or len(df) < 30:
                continue
            mask = (df.index > oos_start) & (df.index <= oos_end)
            sub  = df[mask]
            if len(sub) < 15:
                continue
            res = _backtest_single(sub, best_c5, best_c6)
            if res.n_trades > 0:
                oos_results.append(res)

        oos_win = float(np.mean([r.win_rate for r in oos_results])) if oos_results else 0.0
        oos_sh  = float(np.mean([r.sharpe   for r in oos_results])) if oos_results else 0.0
        oos_mdd = float(np.mean([r.mdd      for r in oos_results])) if oos_results else 0.0
        oos_n   = sum(r.n_trades for r in oos_results)

        window_results.append({
            "window":       f"{is_start.strftime('%y.%m')}~{oos_end.strftime('%y.%m')}",
            "best_cond5":   best_c5,
            "best_cond6":   best_c6,
            "oos_win_rate": round(oos_win, 1),
            "oos_sharpe":   round(oos_sh, 3),
            "oos_mdd":      round(oos_mdd * 100, 2),
            "oos_trades":   oos_n,
        })
        oos_returns_all.extend(oos_results)

    # ── 최종 파라미터: OOS 다수결 (동률 → OOS 샤프 합산 기준) ──
    if window_results:
        param_sharpe: Dict[Tuple, float] = {}
        param_votes:  Dict[Tuple, int]   = {}
        for wr in window_results:
            k = (wr["best_cond5"], wr["best_cond6"])
            param_votes[k]  = param_votes.get(k, 0) + 1
            param_sharpe[k] = param_sharpe.get(k, 0.0) + wr["oos_sharpe"]

        max_votes = max(param_votes.values())
        candidates = [k for k, v in param_votes.items() if v == max_votes]
        final_params = max(candidates, key=lambda k: param_sharpe.get(k, 0.0))
    else:
        final_params = (0.08, 0.35)

    # OOS 집계
    f_win = float(np.mean([r.win_rate for r in oos_returns_all])) if oos_returns_all else 0.0
    f_sh  = float(np.mean([r.sharpe   for r in oos_returns_all])) if oos_returns_all else 0.0
    f_mdd = float(np.mean([r.mdd      for r in oos_returns_all])) if oos_returns_all else 0.0
    f_n   = sum(r.n_trades for r in oos_returns_all)
    f_avg = float(np.mean([r.avg_ret  for r in oos_returns_all])) if oos_returns_all else 0.0

    grid_df = (
        pd.DataFrame(last_grid_rows)
        .sort_values("plateau_score", ascending=False)
        if last_grid_rows else pd.DataFrame()
    )

    return WalkForwardReport(
        best_cond5   = final_params[0],
        best_cond6   = final_params[1],
        oos_win_rate = round(f_win, 1),
        oos_sharpe   = round(f_sh,  3),
        oos_mdd      = round(f_mdd * 100, 2),
        oos_n_trades = int(f_n),
        oos_avg_ret  = round(f_avg, 2),
        window_results = window_results,
        grid_summary   = grid_df,
        timestamp      = datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


# ── 6. 데이터 다운로드 헬퍼 (yfinance) ──────────────────────────────────────
def fetch_ohlcv_for_optimization(
    tickers:     List[Tuple[str, str]],
    months:      int = 6,
    progress_cb  = None,
) -> Dict[str, pd.DataFrame]:
    """
    yfinance로 최근 N개월 OHLCV 다운로드.
    한국 종목: {ticker}.KS → 실패 시 .KQ 자동 재시도.
    반환 컬럼: 시가, 고가, 저가, 종가, 거래량 (동적 청산에 시가 필수).
    """
    import yfinance as yf

    end   = datetime.now()
    start = end - timedelta(days=30 * months + 20)   # 여유분 +20일

    result: Dict[str, pd.DataFrame] = {}

    for i, (ticker, name) in enumerate(tickers):
        if progress_cb:
            progress_cb(i + 1, len(tickers))
        try:
            is_kr = ticker.isdigit()

            if is_kr:
                raw = None
                for suffix in [".KS", ".KQ"]:
                    _raw = yf.download(
                        ticker + suffix,
                        start=start.strftime("%Y-%m-%d"),
                        end=end.strftime("%Y-%m-%d"),
                        progress=False, auto_adjust=True,
                    )
                    if _raw is not None and len(_raw) > 25:
                        raw = _raw
                        break
            else:
                raw = yf.download(
                    ticker,
                    start=start.strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    progress=False, auto_adjust=True,
                )

            if raw is None or len(raw) < 25:
                continue

            # MultiIndex 컬럼 평탄화
            raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]

            df = raw.rename(columns={
                "Open":  "시가",
                "High":  "고가",
                "Low":   "저가",
                "Close": "종가",
                "Volume":"거래량",
            })
            df.index = pd.to_datetime(df.index)

            required = ["시가", "고가", "저가", "종가", "거래량"]
            if not all(col in df.columns for col in required):
                continue

            result[ticker] = df[required].dropna()

        except Exception:
            continue

    return result
