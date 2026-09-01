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
import tkinter as tk
from tkinter import scrolledtext, messagebox

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


# ── GUI ──
root = tk.Tk()
root.title("퀀트 도구")
root.geometry("560x620")

tk.Label(root, text="📊 퀀트 트레이딩 도구", font=("맑은 고딕", 15, "bold")).pack(pady=8)
_status = tk.StringVar(value="감시: 🔴 꺼짐")
tk.Label(root, textvariable=_status, font=("맑은 고딕", 10)).pack()

_btns = tk.Frame(root); _btns.pack(pady=6)
_specs = [
    ("🌒 종배픽 강제", lambda: _run(["--force-pick"], "종배픽")),
    ("🌙 저녁뉴스 테스트", lambda: _run(["--test-news"], "저녁뉴스")),
    ("📋 성적표(Report)", lambda: _run(["--report"], "성적표")),
    ("📊 신호분석", lambda: _run(["--analyze"], "신호분석")),
    ("📈 변동성 스캐너", lambda: _run(["--volatility"], "변동성")),
    ("🧭 장세 판독기", lambda: _run(["--regime"], "장세판독")),
    ("⏱️ 종배 청산분석", lambda: _run(["--exit-analysis"], "청산분석")),
    ("💼 보유종목 조회", lambda: _run(["--holdings"], "보유조회")),
]
for i, (txt, fn) in enumerate(_specs):
    tk.Button(_btns, text=txt, width=22, height=2, command=fn,
              font=("맑은 고딕", 10)).grid(row=i // 2, column=i % 2, padx=5, pady=4)

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
