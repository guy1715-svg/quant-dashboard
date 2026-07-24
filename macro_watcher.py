"""
📡 매크로 + 수급 상시 감시 → 텔레그램 폰 알림 (대시보드 안 켜도 백그라운드로 동작)

알림 종류:
  1) 매크로 국면 개선  🔴리스크오프 → 🟡중립/🟢진입허용
  2) 🚀 전조 시그널    섹터 자금 대이동(이탈원→유입처) + 매크로 정상
  3) 🥇 A급 종목       자금 유입처 × 연기금 포착 교집합(신규 등장 시)

데이터:
  - 매크로: yfinance (NQ=F/^SOX/NVDA/AVGO/MU/CL=F)  ← 항상 동작
  - 수급(전조·A급): KIS API  ← .streamlit/secrets.toml 의 KIS_APP_KEY/KIS_APP_SECRET 필요
    · 연기금 겹침은 pension_track_log.json(대시보드가 쌓는 파일) 사용

사용법 (윈도우):
  1) pip install yfinance requests
  2) set TELEGRAM_BOT_TOKEN=봇토큰
     set TELEGRAM_CHAT_ID=내chat_id
  3) py macro_watcher.py --interval 300
  (start_watcher.bat 더블클릭으로도 실행 가능)
"""
import os
import sys
import json
import time
import argparse
import datetime

try:
    import requests
except ImportError:
    print("requests 필요: py -m pip install requests"); sys.exit(1)
try:
    import yfinance as yf
except ImportError:
    print("yfinance 필요: py -m pip install yfinance"); sys.exit(1)

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "macro_watcher_state.json")
SECRETS_FILE = os.path.join(BASE, ".streamlit", "secrets.toml")
PENSION_FILE = os.path.join(BASE, "pension_track_log.json")
KIS_BASE = "https://openapi.koreainvestment.com:9443"

NQ_BLOCK, NQ_GO, WTI_RISK = -0.2, 0.5, 2.0

# 섹터 구성(대시보드 _BRIEF_SECTORS 동일)
SECTORS = {
    "반도체": [("000660", "SK하이닉스"), ("005930", "삼성전자"), ("042700", "한미반도체")],
    "2차전지": [("373220", "LG에너지솔루션"), ("006400", "삼성SDI"), ("247540", "에코프로비엠")],
    "바이오": [("207940", "삼성바이오로직스"), ("068270", "셀트리온"), ("196170", "알테오젠")],
    "방산/우주": [("012450", "한화에어로스페이스"), ("047810", "한국항공우주"), ("272210", "한화시스템")],
    "원전/우라늄": [("034020", "두산에너빌리티"), ("052690", "한전기술"), ("051600", "한전KPS")],
    "인터넷/빅테크": [("035420", "NAVER"), ("035720", "카카오")],
}


# ── 유틸 ────────────────────────────────────────────────────────────────────
def _to_int(v, d=0):
    try:
        if v is None:
            return d
        s = str(v).replace(",", "").replace("+", "").strip()
        return int(float(s)) if s not in ("", "-", "N/A", "None") else d
    except Exception:
        return d


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def send_telegram(token, chat_id, text):
    try:
        requests.get(f"https://api.telegram.org/bot{token}/sendMessage",
                     params={"chat_id": chat_id, "text": text}, timeout=8)
        return True
    except Exception as e:
        print("텔레그램 전송 실패:", e); return False


def read_kis_keys():
    """secrets.toml에서 KIS_APP_KEY/KIS_APP_SECRET 탐색(최상위+섹션). 없으면 (None,None)."""
    key = secret = None
    try:
        data = None
        try:
            import tomllib
            with open(SECRETS_FILE, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            data = None
        if isinstance(data, dict):
            def _walk(d):
                nonlocal key, secret
                for k, v in d.items():
                    ku = str(k).upper()
                    if isinstance(v, dict):
                        _walk(v)
                    elif ku == "KIS_APP_KEY" and not key:
                        key = str(v)
                    elif ku == "KIS_APP_SECRET" and not secret:
                        secret = str(v)
            _walk(data)
        if not (key and secret):   # tomllib 실패/부재 → 라인 파싱 폴백
            with open(SECRETS_FILE, encoding="utf-8") as f:
                for line in f:
                    up = line.upper()
                    if "KIS_APP_KEY" in up and "=" in line and not key:
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif "KIS_APP_SECRET" in up and "=" in line and not secret:
                        secret = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return key, secret


# ── 매크로 ──────────────────────────────────────────────────────────────────
def _pct(sym):
    try:
        fi = yf.Ticker(sym).fast_info
        l, p = float(fi.last_price), float(fi.previous_close)
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


def compute_macro():
    nq, sox = _pct("NQ=F"), _pct("^SOX")
    peers = [x for x in (_pct("NVDA"), _pct("AVGO"), _pct("MU")) if x is not None]
    wti = _wti_pct()
    ups = sum(1 for v in peers if v > 0)
    semi_sync = (sox is not None and sox > 0) and (len(peers) > 0 and ups >= max(1, round(len(peers) * 0.6)))
    riskoff = (wti is not None and wti >= WTI_RISK)
    if riskoff or (nq is not None and nq <= NQ_BLOCK):
        sev, text = 2, "🔴 리스크오프 · 신규매수 차단"
    elif (nq is not None and nq >= NQ_GO) and semi_sync:
        sev, text = 0, "🟢 진입 허용 (매크로 3대 양호)"
    else:
        sev, text = 1, "🟡 중립 · 선별 진입"
    detail = (f"나스닥 {nq:+.2f}% · SOX {sox:+.2f}% · WTI {wti:+.2f}%"
              if None not in (nq, sox, wti) else "일부 데이터 대기")
    return sev, text, detail


# ── KIS 수급(금액 기준) ─────────────────────────────────────────────────────
def kis_token(key, secret):
    try:
        r = requests.post(f"{KIS_BASE}/oauth2/tokenP",
                          json={"grant_type": "client_credentials", "appkey": key, "appsecret": secret},
                          timeout=8)
        return r.json().get("access_token")
    except Exception:
        return None


def sector_moneyflow(token, key, secret):
    """섹터별 순매수 거래대금(원) + 종목별 세부. 실패 항목은 격리."""
    hdr = {"authorization": f"Bearer {token}", "appkey": key, "appsecret": secret}
    out = {}
    for sname, stocks in SECTORS.items():
        net_amt, detail = 0, []
        for code, nm in stocks:
            qty = None
            try:
                r = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
                                 headers={**hdr, "tr_id": "HHPTJ04160200"},
                                 params={"MKSC_SHRN_ISCD": code}, timeout=6)
                o2 = r.json().get("output2", [])
                if isinstance(o2, list) and o2:
                    for row in reversed(o2):
                        if isinstance(row, dict) and (_to_int(row.get("frgn_fake_ntby_qty")) or _to_int(row.get("orgn_fake_ntby_qty"))):
                            qty = _to_int(row.get("frgn_fake_ntby_qty")) + _to_int(row.get("orgn_fake_ntby_qty"))
                            break
            except Exception:
                pass
            price = None
            try:
                rp = requests.get(f"{KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-price",
                                  headers={**hdr, "tr_id": "FHKST01010100"},
                                  params={"fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}, timeout=6)
                price = _to_int(rp.json().get("output", {}).get("stck_prpr"))
            except Exception:
                pass
            amt = (qty * price) if (qty is not None and price) else None
            detail.append({"code": code, "name": nm, "amt": amt})
            if amt is not None:
                net_amt += amt
        out[sname] = {"net": net_amt, "stocks": detail}
    return out


def pension_codes():
    try:
        with open(PENSION_FILE, encoding="utf-8") as f:
            return {r.get("code") for r in json.load(f).get("records", [])}
    except Exception:
        return set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=300, help="체크 주기(초), 기본 300=5분")
    ap.add_argument("--notify-worse", action="store_true", help="매크로 악화 시에도 알림")
    args = ap.parse_args()
    token_tg = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token_tg or not chat_id:
        print("환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 설정 필요"); sys.exit(1)
    kis_key, kis_secret = read_kis_keys()
    kis_on = bool(kis_key and kis_secret)
    print(f"📡 감시 시작 — {args.interval}초 · 매크로 ON · 수급(전조/A급) {'ON' if kis_on else 'OFF(secrets.toml KIS키 없음)'}")
    send_telegram(token_tg, chat_id,
                  f"📡 감시 시작 — 국면 개선·전조·A급 알림 대기중\n수급 감시 {'ON' if kis_on else 'OFF(KIS키 없음)'}")

    while True:
        try:
            st = load_state()
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
            stamp = now.strftime("%m/%d %H:%M")

            # 1) 매크로
            sev, mtext, mdetail = compute_macro()
            prev_sev = st.get("sev")
            if prev_sev is not None and sev != prev_sev:
                if sev < prev_sev or args.notify_worse:
                    icon = "📈 매크로 개선!" if sev < prev_sev else "📉 매크로 악화"
                    send_telegram(token_tg, chat_id, f"{icon}\n{mtext}\n{mdetail}\n{stamp} KST")
            st["sev"] = sev
            print(f"[{stamp}] 매크로 sev={sev} {mtext} | {mdetail}")

            # 2)/3) 수급 전조·A급 (KIS 있을 때 + 매크로가 리스크오프 아닐 때만 유의미)
            if kis_on:
                tok = kis_token(kis_key, kis_secret)
                if tok:
                    secs = sector_moneyflow(tok, kis_key, kis_secret)
                    rows = sorted(secs.items(), key=lambda kv: kv[1]["net"], reverse=True)
                    inflow = rows[0] if rows else None
                    outflow = rows[-1] if rows else None
                    # 전조 시그널
                    if (inflow and outflow and inflow[0] != outflow[0]
                            and inflow[1]["net"] > 0 and outflow[1]["net"] < 0 and sev != 2):
                        key = f"{outflow[0]}>{inflow[0]}"
                        if st.get("tour_key") != key:
                            send_telegram(token_tg, chat_id,
                                          f"🚀 전조 시그널!\n자금 {outflow[0]} 이탈 → {inflow[0]} 유입\n"
                                          f"유입 {inflow[1]['net']/1e8:,.0f}억 · {stamp} KST\n폭등 前 선취 후보 — 대시보드 확인")
                            st["tour_key"] = key
                    # A급(유입 종목 ∩ 연기금)
                    pens = pension_codes()
                    ace_now = []
                    for sname, info in secs.items():
                        if info["net"] <= 0:
                            continue
                        for s in info["stocks"]:
                            if s["amt"] and s["amt"] > 0 and s["code"] in pens:
                                ace_now.append((s["code"], s["name"], sname, s["amt"]))
                    prev_ace = set(st.get("ace", []))
                    new_ace = [a for a in ace_now if a[0] not in prev_ace]
                    if new_ace and sev != 2:
                        lines = "\n".join(f"• {n} ({sn}) +{amt/1e8:,.0f}억" for _c, n, sn, amt in new_ace)
                        send_telegram(token_tg, chat_id,
                                      f"🥇 A급 종목 신규 포착!\n{lines}\n{stamp} KST\n(자금유입 × 연기금 겹침)")
                    st["ace"] = [a[0] for a in ace_now]
                    print(f"           수급: 유입 {inflow[0] if inflow else '-'} / 이탈 {outflow[0] if outflow else '-'} · A급 {len(ace_now)}")

            save_state(st)
        except Exception as e:
            print("체크 오류:", e)
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    main()
