@echo off
REM ============================================================
REM  📡 매크로+수급 텔레그램 감시 자동 실행
REM  최초 1회: 아래 두 줄의 값을 본인 것으로 바꿔 저장하세요.
REM   - 봇토큰: BotFather가 준 토큰
REM   - chat_id: getUpdates에서 확인한 숫자
REM  이후 이 파일을 더블클릭하면 감시가 켜집니다.
REM ============================================================

set TELEGRAM_BOT_TOKEN=8215476952:AAFGEsAFM9Nx2epahXmNuelxICuQ_KVUjtE
set TELEGRAM_CHAT_ID=1781972453

cd /d "%~dp0"

echo ============================================
echo  매크로+수급 감시 시작 (5분 주기)
echo  이 창을 닫으면 감시가 멈춥니다. 최소화해서 켜두세요.
echo ============================================

REM 필요 패키지 자동 설치(최초 1회만 실제 설치)
py -m pip install --quiet yfinance requests 2>nul

REM 끊겨도 자동 재시작(네트워크 순단 대비)
:loop
py macro_watcher.py --interval 300
echo.
echo [감시 종료됨] 10초 후 자동 재시작... (완전히 끄려면 이 창을 닫으세요)
timeout /t 10 /nobreak >nul
goto loop
