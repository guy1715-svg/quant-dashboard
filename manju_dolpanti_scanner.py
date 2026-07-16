"""
manju_dolpanti_scanner.py
─────────────────────────────────────────────────────────────────────────────
V6.2 모닝 브리핑 대시보드 — 유튜버 기법 이식 모듈

  ① 만쥬식 (초단타/당일청산) : 오전 순매도 → 오후 순매수 '수급 턴어라운드' 실시간 스캔
                               + 15:15 즉시 청산(EXIT) 타임리밋 경고
  ② 돌팬티식 (종가베팅/오버나잇): 15:00 정각 가동 → 3필터(20MA 상단·기관 순매수+·아래꼬리)
                                동시충족 종목 [오늘의 돌팬티 타겟] 리스트업

데이터: 네이버 증권(일봉 OHLC + 종목별 투자자 순매매) 파싱
연동  : Python(크롤링·가공) → Google Sheets API → GAS(대시보드 렌더/시각처리)

⚠️ 실무 주의
  - 네이버 종목별 '투자자 순매매량'은 거래소 가집계(잠정)라 장중 값은 근사치입니다.
  - '금융투자' 세부 주체는 네이버 무료 표에 없어 '기관 합계'를 프록시로 사용합니다.
    (금융투자 세부가 꼭 필요하면 KIS inquire-investor / KRX 데이터로 교체하세요.)
  - 이 스크립트는 5분 주기 등으로 반복 실행(cron/스케줄러)되는 것을 전제로,
    만쥬식 오전 스냅샷은 _STATE_PATH(JSON)에 보존됩니다.
"""

from __future__ import annotations

import io
import json
import os
import datetime as dt
from dataclasses import dataclass, asdict
from typing import Optional

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────────────────────────────────
WATCHLIST: list[tuple[str, str]] = [
    ("005930", "삼성전자"),
    ("000660", "SK하이닉스"),
    ("042700", "한미반도체"),
    ("196170", "알테오젠"),
    ("247540", "에코프로비엠"),
]

_KST = dt.timezone(dt.timedelta(hours=9))
_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manju_state.json")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Referer": "https://finance.naver.com/",
}

# 만쥬식 오전 스냅샷 기준 시각(이 이전=오전 표본 기록, 이후=턴어라운드 판정)
_AM_CUTOFF = dt.time(11, 30)
_MANJU_EXIT = dt.time(15, 15)      # 즉시 청산 경고
_DOLPANTI_TRIGGER = dt.time(15, 0)  # 종가베팅 스캐너 가동
_MARKET_CLOSE = dt.time(15, 20)


# ─────────────────────────────────────────────────────────────────────────────
# 네이버 크롤링 (requests + BeautifulSoup + pandas)
# ─────────────────────────────────────────────────────────────────────────────
def naver_daily_ohlc(code: str, days: int = 30) -> Optional[pd.DataFrame]:
    """네이버 차트 API(siseJson) 일봉 OHLC.
    반환: DataFrame[date, open, high, low, close, volume] (오래된→최신) 또는 None."""
    _end = dt.datetime.now(_KST).strftime("%Y%m%d")
    url = ("https://api.finance.naver.com/siseJson.naver"
           f"?symbol={code}&requestType=1&count={max(days, 25)}&timeframe=day&endTime={_end}")
    try:
        _r = requests.get(url, headers=_HEADERS, timeout=6)
        # 응답은 파이썬 리터럴 유사 문자열 → 헤더행 제거 후 안전 파싱
        _rows = json.loads(_r.text.replace("'", '"'))
        if not _rows or len(_rows) < 2:
            return None
        _df = pd.DataFrame(_rows[1:], columns=["date", "open", "high", "low", "close", "volume", "_fx"])
        _df = _df[["date", "open", "high", "low", "close", "volume"]].copy()
        for _c in ["open", "high", "low", "close", "volume"]:
            _df[_c] = pd.to_numeric(_df[_c], errors="coerce")
        return _df.dropna().reset_index(drop=True)
    except Exception as _e:
        print(f"[naver_daily_ohlc] {code} 실패: {type(_e).__name__}: {_e}")
        return None


def naver_investor_today(code: str) -> Optional[dict]:
    """네이버 종목별 외국인/기관 '오늘' 순매매량(주) — item/frgn.naver 표 첫 행.
    반환: {'외인': int, '기관': int, 'date': 'YYYY.MM.DD'} 또는 None.
    ⚠️ 장중 값은 거래소 가집계(잠정)."""
    url = f"https://finance.naver.com/item/frgn.naver?code={code}"
    try:
        _r = requests.get(url, headers=_HEADERS, timeout=6)
        _r.encoding = "euc-kr"
        _soup = BeautifulSoup(_r.text, "lxml")
        # '외국인·기관 순매매 거래량' 표 → pandas로 파싱(구조 변동 내성)
        _tables = pd.read_html(io.StringIO(str(_soup)), thousands=",")
        _tgt = None
        for _t in _tables:
            _cols = [str(c) for c in _t.columns.get_level_values(-1)]
            if any("기관" in c for c in _cols) and any("외국인" in c for c in _cols):
                _tgt = _t
                break
        if _tgt is None:
            return None
        _tgt.columns = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in _tgt.columns]
        _first = _tgt.dropna(subset=[_tgt.columns[0]]).iloc[0]
        _org = _first.get("기관", _first.get("기관 순매매량"))
        _frn = _first.get("외국인", _first.get("외국인 순매매량"))
        return {
            "외인": int(pd.to_numeric(_frn, errors="coerce") or 0),
            "기관": int(pd.to_numeric(_org, errors="coerce") or 0),
            "date": str(_first.get("날짜", "")),
        }
    except Exception as _e:
        print(f"[naver_investor_today] {code} 실패: {type(_e).__name__}: {_e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 지표 계산
# ─────────────────────────────────────────────────────────────────────────────
def above_ma20(df: pd.DataFrame) -> bool:
    """현재가(최신 종가)가 20일 이동평균선 위인가."""
    if df is None or len(df) < 20:
        return False
    _ma20 = df["close"].rolling(20).mean().iloc[-1]
    return bool(df["close"].iloc[-1] > _ma20)


def has_lower_tail(df: pd.DataFrame, min_ratio: float = 0.33) -> bool:
    """당일 캔들이 '아래꼬리' 형태인가 — 아래꼬리 길이 ≥ 전체 범위의 min_ratio.
    아래꼬리 = min(시가,종가) - 저가.  매수세가 저가를 되받아친 형태."""
    if df is None or df.empty:
        return False
    _r = df.iloc[-1]
    _rng = _r["high"] - _r["low"]
    if _rng <= 0:
        return False
    _lower = min(_r["open"], _r["close"]) - _r["low"]
    return bool(_lower >= _rng * min_ratio)


# ─────────────────────────────────────────────────────────────────────────────
# 상태 저장 (만쥬식 오전 스냅샷 보존)
# ─────────────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as _f:
            _d = json.load(_f)
            return _d if isinstance(_d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as _f:
            json.dump(state, _f, ensure_ascii=False)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# ① 만쥬식 — 장중 수급 턴어라운드 스캔
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ManjuSignal:
    code: str
    name: str
    am_flow: int          # 오전 스냅샷 외인+기관 순매매(주)
    now_flow: int         # 현재 외인+기관 순매매(주)
    turnaround: bool      # 오전 순매도 → 현재 순매수 전환
    exit_warning: bool    # 15:15 이후 즉시 청산 경고
    color: str            # 'red'(매수전환) / 'blue'(순매도) / 'gray'
    note: str


def scan_manju(now: Optional[dt.datetime] = None) -> list[ManjuSignal]:
    """만쥬식 실시간 스캔. 반복 실행 전제:
       - _AM_CUTOFF(11:30) 이전: 종목별 오전 수급을 스냅샷으로 기록.
       - 이후: 오전 순매도(-)였다가 현재 순매수(+)로 전환된 종목을 turnaround=True로 표시.
       - _MANJU_EXIT(15:15) 이후: 모든 보유 후보에 즉시 청산(EXIT) 경고."""
    now = now or dt.datetime.now(_KST)
    _today = now.strftime("%Y-%m-%d")
    _state = _load_state()
    _day_state = _state.get(_today, {})
    _is_am = now.time() < _AM_CUTOFF
    _exit = now.time() >= _MANJU_EXIT and now.time() < _MARKET_CLOSE

    _out: list[ManjuSignal] = []
    for _code, _name in WATCHLIST:
        _inv = naver_investor_today(_code)
        if not _inv:
            continue
        _now_flow = int(_inv["외인"]) + int(_inv["기관"])

        if _is_am:
            # 오전: 스냅샷 기록(가장 최근 오전 값으로 갱신)
            _day_state[_code] = {"am_flow": _now_flow}
            _am_flow = _now_flow
            _turn = False
        else:
            _am_flow = int(_day_state.get(_code, {}).get("am_flow", _now_flow))
            # 턴어라운드: 오전 순매도(≤0) → 현재 순매수(>0)
            _turn = (_am_flow <= 0) and (_now_flow > 0)

        _color = "red" if _now_flow > 0 else "blue" if _now_flow < 0 else "gray"
        if _turn and not _exit:
            _note = "🔴 수급 턴어라운드 — 오전 순매도→오후 순매수(만쥬 진입 신호)"
        elif _exit:
            _note = "⏰ 15:15 타임리밋 — 당일 청산(EXIT) 실행"
        else:
            _note = "감시 중" if _now_flow <= 0 else "순매수 유지"

        _out.append(ManjuSignal(
            code=_code, name=_name, am_flow=_am_flow, now_flow=_now_flow,
            turnaround=bool(_turn), exit_warning=bool(_exit),
            color=_color, note=_note,
        ))

    _state[_today] = _day_state
    _save_state(_state)
    return _out


# ─────────────────────────────────────────────────────────────────────────────
# ② 돌팬티식 — 15:00 종가베팅 스캐너
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class DolpantiTarget:
    code: str
    name: str
    close: float
    ma20: float
    org_net: int          # 당일 기관 순매매(주) — 금융투자 프록시
    lower_tail: bool
    passed: bool
    note: str


def scan_dolpanti(now: Optional[dt.datetime] = None, force: bool = False) -> list[DolpantiTarget]:
    """돌팬티식 스캔 — 15:00 정각 이후에만 가동(force=True로 수동 강제 가능).
    3필터 동시 충족: ①20MA 상단 ②당일 기관 순매수(+) ③아래꼬리 캔들."""
    now = now or dt.datetime.now(_KST)
    if not force and now.time() < _DOLPANTI_TRIGGER:
        return []   # 15:00 이전엔 대기(빈 리스트)

    _out: list[DolpantiTarget] = []
    for _code, _name in WATCHLIST:
        _df = naver_daily_ohlc(_code, days=30)
        _inv = naver_investor_today(_code)
        if _df is None or _df.empty or not _inv:
            continue
        _c1 = above_ma20(_df)
        _org = int(_inv["기관"])
        _c2 = _org > 0
        _c3 = has_lower_tail(_df)
        _passed = _c1 and _c2 and _c3
        _ma20 = float(_df["close"].rolling(20).mean().iloc[-1]) if len(_df) >= 20 else 0.0
        _fails = []
        if not _c1: _fails.append("20MA 하단")
        if not _c2: _fails.append(f"기관 순매도({_org:,})")
        if not _c3: _fails.append("아래꼬리 없음")
        _out.append(DolpantiTarget(
            code=_code, name=_name,
            close=float(_df["close"].iloc[-1]), ma20=round(_ma20, 1),
            org_net=_org, lower_tail=_c3, passed=_passed,
            note="✅ 3조건 충족 — 종가베팅 타겟" if _passed else " / ".join(_fails),
        ))
    # 타겟(통과) 우선 정렬
    _out.sort(key=lambda t: (not t.passed, -t.org_net))
    return _out


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets 연동
# ─────────────────────────────────────────────────────────────────────────────
def push_to_sheets(manju: list[ManjuSignal], dolpanti: list[DolpantiTarget],
                   sheet_id: str, cred_path: str) -> None:
    """가공 결과를 Google Sheets에 기록 → GAS가 이 시트를 읽어 대시보드 렌더.
    시트 탭: 'MANJU'(만쥬 실시간), 'DOLPANTI'(종가베팅 타겟)."""
    import gspread
    from google.oauth2.service_account import Credentials

    _scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    _creds = Credentials.from_service_account_file(cred_path, scopes=_scopes)
    _wb = gspread.authorize(_creds).open_by_key(sheet_id)
    _ts = dt.datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S")

    # MANJU 탭
    _m_rows = [["갱신", _ts, "", "", "", "", ""],
               ["코드", "종목명", "오전수급", "현재수급", "턴어라운드", "청산경고", "상태"]]
    for _s in manju:
        _m_rows.append([_s.code, _s.name, _s.am_flow, _s.now_flow,
                        "TRUE" if _s.turnaround else "FALSE",
                        "EXIT" if _s.exit_warning else "",
                        f"[{_s.color}] {_s.note}"])
    _ws_m = _get_or_create(_wb, "MANJU")
    _ws_m.clear(); _ws_m.update("A1", _m_rows)

    # DOLPANTI 탭
    _d_rows = [["갱신", _ts, "", "", "", "", ""],
               ["코드", "종목명", "종가", "20MA", "기관순매수", "아래꼬리", "판정"]]
    for _t in dolpanti:
        _d_rows.append([_t.code, _t.name, _t.close, _t.ma20, _t.org_net,
                        "O" if _t.lower_tail else "X",
                        "TARGET" if _t.passed else "—"])
    _ws_d = _get_or_create(_wb, "DOLPANTI")
    _ws_d.clear(); _ws_d.update("A1", _d_rows)


def _get_or_create(wb, title: str):
    try:
        return wb.worksheet(title)
    except Exception:
        return wb.add_worksheet(title=title, rows=100, cols=12)


# ─────────────────────────────────────────────────────────────────────────────
# 메인 — 시각에 따라 자동 분기(스케줄러가 5분 주기 등으로 반복 호출)
# ─────────────────────────────────────────────────────────────────────────────
def run(sheet_id: Optional[str] = None, cred_path: Optional[str] = None) -> dict:
    _now = dt.datetime.now(_KST)
    _manju = scan_manju(_now)
    _dolpanti = scan_dolpanti(_now)   # 15:00 이전이면 []
    if sheet_id and cred_path:
        push_to_sheets(_manju, _dolpanti, sheet_id, cred_path)
    return {"time": _now.strftime("%H:%M:%S"),
            "manju": [asdict(s) for s in _manju],
            "dolpanti": [asdict(t) for t in _dolpanti]}


if __name__ == "__main__":
    import pprint
    pprint.pprint(run(
        sheet_id=os.environ.get("DASHBOARD_SHEET_ID"),
        cred_path=os.environ.get("GCP_CRED_PATH"),
    ))
