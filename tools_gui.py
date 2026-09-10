# -*- coding: utf-8 -*-
"""
퀀트 도구 GUI — tools.bat 대체(인코딩 문제 없음). 버튼 클릭으로 macro_watcher 기능 실행.
실행: py tools_gui.py   (Tkinter는 파이썬 기본 내장·설치 불필요)
.exe로 만들려면(선택): pip install pyinstaller  →  pyinstaller --onefile --windowed tools_gui.py
"""
import os
import sys
import subprocess
import threading
import json
import urllib.request
import urllib.parse
import re
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk

# ────────────────────────────────────────────────────────────
# 🔑 키 설정 — 아래 값을 본인 것으로 채우세요(tools.bat에 있던 값 복붙)
# ────────────────────────────────────────────────────────────
KEYS = {
    "TELEGRAM_BOT_TOKEN": "PUT_YOUR_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID":   "PUT_YOUR_CHAT_ID",
    "KIS_APP_KEY":        "PUT_YOUR_KIS_APP_KEY",
    "KIS_APP_SECRET":     "PUT_YOUR_KIS_APP_SECRET",
    "DART_API_KEY":       "PUT_YOUR_DART_KEY",
    "GEMINI_API_KEY":     "PUT_YOUR_GEMINI_KEY",
    "NAVER_CLIENT_ID":    "PUT_YOUR_NAVER_ID",
    "NAVER_CLIENT_SECRET": "PUT_YOUR_NAVER_SECRET",
    "GITHUB_TOKEN":       "PUT_YOUR_GITHUB_TOKEN",
    # OpenBLAS 메모리 크래시 방지
    "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    # 한글/특수문자(—) print 크래시 방지 — 자식 파이썬 stdout을 UTF-8로
    "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
}

BASE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
WATCHER = os.path.join(BASE, "macro_watcher.py")
_env = {**os.environ, **KEYS}
_watch_proc = [None]   # 감시 프로세스 핸들(시작/중지용)
HOLDINGS_FILE = os.path.join(BASE, "my_holdings.json")
REVIEW_FILE = os.path.join(BASE, "market_review.md")


def _view_market_review():
    """시장복기 원장(market_review.md)을 창으로 표시 — 최신 날짜가 위로 오게 역순."""
    win = tk.Toplevel(root)
    win.title("📓 시장 복기 원장")
    win.geometry("640x560")
    tk.Label(win, text="📓 시장 복기 원장 (market_review.md)", font=("맑은 고딕", 12, "bold")).pack(pady=6)
    _box = scrolledtext.ScrolledText(win, width=76, height=30, font=("맑은 고딕", 10), wrap=tk.WORD)
    _box.pack(padx=8, pady=4, fill=tk.BOTH, expand=True)
    try:
        with open(REVIEW_FILE, encoding="utf-8") as f:
            _txt = f.read().strip()
        # '## 날짜' 블록 단위로 쪼개 최신(뒤쪽)이 위로 오게 역순 표시
        _blocks = _txt.split("\n## ")
        if len(_blocks) > 1:
            _head, _days = _blocks[0], ["## " + b for b in _blocks[1:]]
            _txt = _head + "\n\n" + "\n\n".join(reversed(_days))
        _box.insert(tk.END, _txt or "(비어있음)")
    except FileNotFoundError:
        _box.insert(tk.END, "아직 없음 — 감시 마감복기(15:35~) 또는 '과거복기 학습(--backfill)'을 먼저 실행하세요.")
    except Exception as _e:
        _box.insert(tk.END, f"읽기 오류: {type(_e).__name__}: {_e}")
    _box.config(state=tk.DISABLED)
    tk.Button(win, text="🔄 새로고침", command=lambda: (win.destroy(), _view_market_review()),
              font=("맑은 고딕", 9)).pack(pady=4)


def _load_holdings():
    try:
        with open(HOLDINGS_FILE, encoding="utf-8-sig") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("stocks"), list):
            return d["stocks"]
    except Exception:
        pass
    return []


def _save_holdings(stocks):
    d = {"on": True,
         "_설명": "보유종목 손절/익절. code=코드,name=이름,avg=매수평균,qty=수량,stop=손절%(기본-2),target=익절%(기본+3)",
         "stocks": stocks}
    with open(HOLDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _resolve_stock(query):
    """종목명/코드 → [(code, name)] 후보 — 네이버 자동완성(키 불필요). 실패 시 []."""
    q = (query or "").strip()
    if re.fullmatch(r"\d{6}", q):        # 이미 6자리 코드면 그대로
        return [(q, q)]
    try:
        url = "https://ac.stock.naver.com/ac?" + urllib.parse.urlencode(
            {"q": q, "target": "stock,index"})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read().decode("utf-8", "ignore"))
        out = []

        def _walk(o):
            if isinstance(o, dict):
                _c = o.get("code") or o.get("cd")
                _n = o.get("name") or o.get("nm")
                if _c and re.fullmatch(r"\d{6}", str(_c)) and _n:
                    out.append((str(_c), str(_n)))
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for it in o:
                    _walk(it)
        _walk(j)
        _seen, _res = set(), []
        for _c, _n in out:
            if _c not in _seen:
                _seen.add(_c); _res.append((_c, _n))
        return _res[:10]
    except Exception:
        return []


def _run(args, label):
    """일회성 명령 실행 → 출력을 로그창에 표시(백그라운드 스레드)."""
    def _worker():
        _log(f"\n▶ {label} 실행...\n")
        try:
            p = subprocess.Popen([PY, WATCHER, *args], cwd=BASE, env=_env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="ignore")
            for line in p.stdout:
                _log(line)
            p.wait()
            _log(f"✔ {label} 완료 (텔레그램 확인)\n")
        except Exception as e:
            _log(f"✖ 오류: {e}\n")
    threading.Thread(target=_worker, daemon=True).start()


def _start_watch():
    if _watch_proc[0] and _watch_proc[0].poll() is None:
        messagebox.showinfo("감시", "이미 감시가 켜져 있어요.")
        return
    _log("\n📡 감시 시작(상시)...\n")
    _watch_proc[0] = subprocess.Popen([PY, WATCHER, "--interval", "300"], cwd=BASE, env=_env)
    _status.set("감시: 🟢 켜짐")


def _stop_watch():
    if _watch_proc[0] and _watch_proc[0].poll() is None:
        _watch_proc[0].terminate()
        _log("\n⏹ 감시 중지됨\n")
    _status.set("감시: 🔴 꺼짐")


def _run_stock():
    code = _stock_entry.get().strip()
    _run(["--stock", code] if code else ["--stock"], f"종목해석 {code or '(관심종목)'}")


def _open_holdings_manager():
    """보유종목 추가/삭제 관리 창 — 종목명 검색→코드 자동, 평단·수량 입력."""
    win = tk.Toplevel(root)
    win.title("💼 보유종목 관리")
    win.geometry("520x560")
    _cands = {"list": []}   # 검색 후보 저장

    tk.Label(win, text="💼 보유종목 관리", font=("맑은 고딕", 13, "bold")).pack(pady=6)

    # 현재 보유 리스트
    tk.Label(win, text="현재 보유종목 (선택 후 삭제):", font=("맑은 고딕", 9)).pack(anchor="w", padx=10)
    _lb = tk.Listbox(win, width=64, height=8, font=("맑은 고딕", 10))
    _lb.pack(padx=10, pady=4)

    def _refresh():
        _lb.delete(0, tk.END)
        for s in _load_holdings():
            _lb.insert(tk.END, f"{s.get('name','')} ({s.get('code','')}) · 평단 {s.get('avg',0):,} · {s.get('qty',0)}주")
    _refresh()

    def _delete():
        i = _lb.curselection()
        if not i:
            messagebox.showinfo("삭제", "삭제할 종목을 선택하세요."); return
        st = _load_holdings()
        del st[i[0]]
        _save_holdings(st); _refresh()
    tk.Button(win, text="🗑 선택 삭제", command=_delete, font=("맑은 고딕", 10)).pack(pady=2)

    # 추가 영역
    tk.Label(win, text="─" * 60, fg="#999").pack()
    tk.Label(win, text="➕ 종목 추가 — 이름 입력 후 검색", font=("맑은 고딕", 10, "bold")).pack(pady=2)
    _sf = tk.Frame(win); _sf.pack(pady=2)
    _q = tk.Entry(_sf, width=18, font=("맑은 고딕", 11)); _q.pack(side=tk.LEFT, padx=4)
    _combo = ttk.Combobox(win, width=48, font=("맑은 고딕", 10), state="readonly")

    def _search():
        res = _resolve_stock(_q.get())
        if not res:
            messagebox.showinfo("검색", "결과 없음 — 이름 확인 or 6자리 코드 직접 입력"); return
        _cands["list"] = res
        _combo["values"] = [f"{n} ({c})" for c, n in res]
        _combo.current(0)
    tk.Button(_sf, text="🔍 검색", command=_search, font=("맑은 고딕", 10)).pack(side=tk.LEFT)
    _combo.pack(pady=3)

    _af = tk.Frame(win); _af.pack(pady=2)
    tk.Label(_af, text="매수평균:", font=("맑은 고딕", 10)).pack(side=tk.LEFT)
    _avg = tk.Entry(_af, width=10, font=("맑은 고딕", 10)); _avg.pack(side=tk.LEFT, padx=2)
    tk.Label(_af, text="수량:", font=("맑은 고딕", 10)).pack(side=tk.LEFT)
    _qty = tk.Entry(_af, width=6, font=("맑은 고딕", 10)); _qty.pack(side=tk.LEFT, padx=2)

    def _add():
        i = _combo.current()
        if i < 0 or not _cands["list"]:
            messagebox.showinfo("추가", "먼저 종목을 검색·선택하세요."); return
        code, name = _cands["list"][i]
        try:
            avg = int(float(_avg.get().replace(",", "")))
            qty = int(float(_qty.get() or 0))
        except Exception:
            messagebox.showinfo("추가", "매수평균/수량을 숫자로 입력하세요."); return
        st = [s for s in _load_holdings() if str(s.get("code")) != code]  # 중복 코드 제거(갱신)
        st.append({"code": code, "name": name, "avg": avg, "qty": qty})
        _save_holdings(st); _refresh()
        _q.delete(0, tk.END); _avg.delete(0, tk.END); _qty.delete(0, tk.END)
        _combo.set("")
    tk.Button(win, text="➕ 추가/수정", command=_add, bg="#d4f8d4",
              font=("맑은 고딕", 10, "bold")).pack(pady=4)
    tk.Label(win, text="※ 같은 종목 다시 추가하면 평단·수량 갱신됨", fg="#666",
             font=("맑은 고딕", 8)).pack()


# ── GUI ──
root = tk.Tk()
root.title("퀀트 도구")
root.geometry("560x680")

tk.Label(root, text="📊 퀀트 트레이딩 도구", font=("맑은 고딕", 15, "bold")).pack(pady=8)
_status = tk.StringVar(value="감시: 🔴 꺼짐")
tk.Label(root, textvariable=_status, font=("맑은 고딕", 10)).pack()

_btns = tk.Frame(root); _btns.pack(pady=6)
_specs = [
    ("🌒 종배픽 강제", lambda: _run(["--force-pick"], "종배픽")),
    ("🌙 저녁뉴스 테스트", lambda: _run(["--test-news"], "저녁뉴스")),
    ("📋 성적표(Report)", lambda: _run(["--report"], "성적표")),
    ("📊 신호분석(+청산분석)", lambda: _run(["--analyze"], "신호분석")),
    ("📈 변동성 스캐너", lambda: _run(["--volatility"], "변동성")),
    ("🧭 장세 판독기", lambda: _run(["--regime"], "장세판독")),
    ("💼 보유종목 조회", lambda: _run(["--holdings"], "보유조회")),
    ("🕰 과거복기 학습(10일)", lambda: _run(["--backfill-review", "10"], "과거복기")),
    ("📓 시장복기 보기", _view_market_review),
    ("📊 주간 메타복기", lambda: _run(["--weekly-review"], "주간메타복기")),
]
for i, (txt, fn) in enumerate(_specs):
    tk.Button(_btns, text=txt, width=22, height=2, command=fn,
              font=("맑은 고딕", 10)).grid(row=i // 2, column=i % 2, padx=5, pady=4)

# 보유종목 관리 버튼
tk.Button(root, text="💼 보유종목 추가/삭제 관리 (이름검색)", command=_open_holdings_manager,
          bg="#fff3cd", font=("맑은 고딕", 10, "bold")).pack(pady=3)

# 종목해석(코드 입력)
_sf = tk.Frame(root); _sf.pack(pady=4)
tk.Label(_sf, text="종목코드:", font=("맑은 고딕", 10)).pack(side=tk.LEFT)
_stock_entry = tk.Entry(_sf, width=10, font=("맑은 고딕", 10)); _stock_entry.pack(side=tk.LEFT, padx=4)
tk.Button(_sf, text="🔍 종목해석(빈칸=관심종목)", command=_run_stock, font=("맑은 고딕", 10)).pack(side=tk.LEFT)

# 감시 시작/중지
_wf = tk.Frame(root); _wf.pack(pady=6)
tk.Button(_wf, text="📡 감시 시작(상시)", width=18, height=2, bg="#d4f8d4",
          command=_start_watch, font=("맑은 고딕", 10, "bold")).pack(side=tk.LEFT, padx=6)
tk.Button(_wf, text="⏹ 감시 중지", width=14, height=2, bg="#f8d4d4",
          command=_stop_watch, font=("맑은 고딕", 10, "bold")).pack(side=tk.LEFT, padx=6)

# 로그창
tk.Label(root, text="실행 로그:", font=("맑은 고딕", 9)).pack(anchor="w", padx=10)
_logbox = scrolledtext.ScrolledText(root, width=66, height=12, font=("Consolas", 9))
_logbox.pack(padx=10, pady=4)


def _log(msg):
    _logbox.insert(tk.END, msg); _logbox.see(tk.END)


if "PUT_YOUR" in KEYS["KIS_APP_KEY"]:
    _log("⚠️ tools_gui.py 상단 KEYS에 본인 키를 채워주세요(메모장 편집).\n")

root.mainloop()
