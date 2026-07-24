"""
📡 매크로 상시 감시 → 텔레그램 폰 알림 (대시보드 안 켜도 백그라운드로 동작)

기능:
  - 5분(기본)마다 나스닥선물(NQ=F)·필라델피아반도체(^SOX)·WTI(CL=F)를 체크
  - 매크로 국면이 🔴리스크오프 → 🟡중립/🟢진입허용 으로 '개선'되면 텔레그램으로 즉시 알림
  - 나빠질 때(🟢→🔴)도 원하면 알림 (--notify-worse)

사용법 (윈도우):
  1) pip install yfinance requests
  2) 텔레그램 봇 만들기(아래 3분 안내) → 토큰·chat_id 확보
  3) 실행:
     set TELEGRAM_BOT_TOKEN=봇토큰
     set TELEGRAM_CHAT_ID=내chat_id
     py macro_watcher.py --interval 300
  (백그라운드로 계속 켜두면 됨. 끄려면 창 닫기)

텔레그램 봇 3분 셋업:
  - 텔레그램에서 @BotFather 검색 → /newbot → 이름 정하면 '봇 토큰' 발급
  - 만든 봇과 대화 시작(아무 메시지 전송) 후, 브라우저에서
    https://api.telegram.org/bot<봇토큰>/getUpdates 열면 "chat":{"id":숫자} 가 내 chat_id
"""
import os
import sys
import time
import argparse
import datetime

try:
    import requests
except ImportError:
    print("requests 필요: pip install requests"); sys.exit(1)
try:
    import yfinance as yf
except ImportError:
    print("yfinance 필요: pip install yfinance"); sys.exit(1)

NQ_BLOCK = -0.2      # 나스닥선물 차단(%)
NQ_GO = 0.5          # 나스닥선물 긍정(%)
WTI_RISK = 2.0       # WTI 급등 리스크오프(%)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "macro_watcher_state.txt")


def _pct_prev_close(sym):
    """fast_info로 (현재/전일종가-1)*100. 실패 시 None."""
    try:
        fi = yf.Ticker(sym).fast_info
        l = float(fi.last_price); p = float(fi.previous_close)
        if l > 0 and p > 0:
            return (l / p - 1) * 100
    except Exception:
        pass
    return None


def _wti_pct():
    try:
        h = yf.Ticker("CL=F").history(period="5d")["Close"].dropna()
        if len(h) >= 2:
            return (float(h.iloc[-1]) / float(h.iloc[-2]) - 1) * 100
    except Exception:
        pass
    return None


def compute_verdict():
    """(sev, text, detail) — sev: 2=리스크오프 1=중립 0=진입허용."""
    nq = _pct_prev_close("NQ=F")
    sox = _pct_prev_close("^SOX")
    peers = [x for x in (_pct_prev_close("NVDA"), _pct_prev_close("AVGO"), _pct_prev_close("MU")) if x is not None]
    wti = _wti_pct()
    ups = sum(1 for v in peers if v > 0)
    semi_sync = (sox is not None and sox > 0) and (len(peers) > 0 and ups >= max(1, round(len(peers) * 0.6)))
    riskoff = (wti is not None and wti >= WTI_RISK)
    nq_block = (nq is not None and nq <= NQ_BLOCK)
    nq_go = (nq is not None and nq >= NQ_GO)
    detail = (f"나스닥선물 {nq:+.2f}% · SOX {sox:+.2f}% · WTI {wti:+.2f}%"
              if None not in (nq, sox, wti) else "일부 데이터 대기")
    if riskoff or nq_block:
        return 2, "🔴 리스크오프 · 신규매수 차단", detail
    if nq_go and semi_sync:
        return 0, "🟢 진입 허용 (매크로 3대 양호)", detail
    return 1, "🟡 중립 · 선별 진입", detail


def load_sev():
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def save_sev(s):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(str(s))
    except Exception:
        pass


def send_telegram(token, chat_id, text):
    try:
        requests.get(f"https://api.telegram.org/bot{token}/sendMessage",
                     params={"chat_id": chat_id, "text": text}, timeout=8)
        return True
    except Exception as e:
        print("텔레그램 전송 실패:", e); return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300, help="체크 주기(초), 기본 300=5분")
    ap.add_argument("--notify-worse", action="store_true", help="나빠질 때도 알림")
    args = ap.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 설정 필요"); sys.exit(1)
    print(f"📡 매크로 감시 시작 — {args.interval}초 주기. Ctrl+C로 종료.")
    send_telegram(token, chat_id, "📡 매크로 감시 시작 — 국면 바뀌면 알려드립니다.")
    while True:
        try:
            sev, text, detail = compute_verdict()
            prev = load_sev()
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
            stamp = now.strftime("%m/%d %H:%M")
            line = f"[{stamp}] sev={sev} {text} | {detail}"
            print(line)
            if prev is not None and sev != prev:
                improved = sev < prev
                if improved or args.notify_worse:
                    icon = "📈 국면 개선!" if improved else "📉 국면 악화"
                    send_telegram(token, chat_id,
                                  f"{icon}\n{text}\n{detail}\n{stamp} KST — 대시보드 확인하세요")
            if prev != sev:
                save_sev(sev)
        except Exception as e:
            print("체크 오류:", e)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
