/***********************************************************************
 * dashboard_manju_dolpanti.gs
 * V6.2 모닝 브리핑 대시보드 — 만쥬식/돌팬티식 시각 처리 (Google Apps Script)
 *
 * 역할: Python 크롤러가 채워준 시트('MANJU','DOLPANTI')를 읽어
 *       ① 만쥬식 수급 알람(빨강/파랑 조건부 서식 + 턴어라운드/EXIT 강조)
 *       ② [오늘의 돌팬티 타겟] 리스트를 대시보드 탭에 렌더.
 *
 * 트리거(권장):
 *   - refreshDashboard : 시간 기반 5분 간격 (장중 09:00~15:30)
 *   - 편집 트리거 불필요 (Python이 데이터 소스, GAS는 렌더 전용)
 ***********************************************************************/

var TZ = 'Asia/Seoul';
var DASH = 'DASHBOARD';   // 최종 표출 탭

function refreshDashboard() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var dash = ss.getSheetByName(DASH) || ss.insertSheet(DASH);
  dash.clear();

  var now = new Date();
  var hm = Utilities.formatDate(now, TZ, 'HH:mm');

  // ── 헤더 ──
  dash.getRange('A1').setValue('⚔️ 만쥬 / 돌팬티 모닝 브리핑')
      .setFontSize(16).setFontWeight('bold');
  dash.getRange('A2').setValue('갱신 ' + Utilities.formatDate(now, TZ, 'yyyy-MM-dd HH:mm:ss'))
      .setFontColor('#888');

  renderManju_(ss, dash, hm);
  renderDolpanti_(ss, dash, hm);
}

/* ───────────────────────── 만쥬식 (실시간 수급 턴어라운드) ───────────────────────── */
function renderManju_(ss, dash, hm) {
  var src = ss.getSheetByName('MANJU');
  var startRow = 4;
  dash.getRange(startRow, 1).setValue('① 만쥬식 — 장중 수급 턴어라운드')
      .setFontWeight('bold').setFontSize(13).setFontColor('#e11d48');

  // 15:15 이후 전역 청산 경고 배너
  if (hm >= '15:15' && hm < '15:20') {
    dash.getRange(startRow + 1, 1)
        .setValue('⏰ 15:15 타임리밋 — 만쥬 포지션 즉시 청산(EXIT)!')
        .setFontWeight('bold').setFontColor('#ffffff').setBackground('#dc2626');
  }

  var head = ['코드', '종목명', '오전수급', '현재수급', '턴어라운드', '청산', '상태'];
  var top = startRow + 2;
  dash.getRange(top, 1, 1, head.length).setValues([head])
      .setFontWeight('bold').setBackground('#1f2937').setFontColor('#fff');

  if (!src) return;
  var data = src.getDataRange().getValues().slice(2); // 갱신/헤더 2행 스킵
  if (!data.length) return;

  dash.getRange(top + 1, 1, data.length, head.length).setValues(data);

  // 조건부 시각 처리: 현재수급 빨강(+)/파랑(-), 턴어라운드/EXIT 강조
  for (var i = 0; i < data.length; i++) {
    var r = top + 1 + i;
    var nowFlow = Number(data[i][3]) || 0;
    var turn = String(data[i][4]).toUpperCase() === 'TRUE';
    var exit = String(data[i][5]).toUpperCase() === 'EXIT';
    dash.getRange(r, 4).setFontColor(nowFlow > 0 ? '#e11d48' : nowFlow < 0 ? '#2563eb' : '#888')
        .setFontWeight('bold');
    if (turn) {
      dash.getRange(r, 1, 1, head.length).setBackground('#fee2e2'); // 매수전환 = 연빨강
      dash.getRange(r, 5).setValue('🔴 매수전환').setFontColor('#b91c1c').setFontWeight('bold');
    }
    if (exit) {
      dash.getRange(r, 6).setValue('EXIT').setFontColor('#fff').setBackground('#dc2626')
          .setFontWeight('bold');
    }
  }
}

/* ───────────────────────── 돌팬티식 (오늘의 종가베팅 타겟) ───────────────────────── */
function renderDolpanti_(ss, dash, hm) {
  var src = ss.getSheetByName('DOLPANTI');
  var base = 4 + 12; // 만쥬 블록 아래
  dash.getRange(base, 1).setValue('② 돌팬티식 — [오늘의 돌팬티 타겟] (15:00 가동)')
      .setFontWeight('bold').setFontSize(13).setFontColor('#7c3aed');

  if (hm < '15:00') {
    dash.getRange(base + 1, 1)
        .setValue('⏳ 15:00 종가베팅 스캐너 대기 중 — 장중 소음 회피(정각 가동)')
        .setFontColor('#888');
    return;
  }

  var head = ['코드', '종목명', '종가', '20MA', '기관순매수', '아래꼬리', '판정'];
  var top = base + 1;
  dash.getRange(top, 1, 1, head.length).setValues([head])
      .setFontWeight('bold').setBackground('#2e1065').setFontColor('#fff');

  if (!src) return;
  var data = src.getDataRange().getValues().slice(2);
  var targets = data.filter(function (row) { return String(row[6]).toUpperCase() === 'TARGET'; });

  if (!targets.length) {
    dash.getRange(top + 1, 1).setValue('오늘 3조건 동시충족 종목 없음 — 관망')
        .setFontColor('#888');
    return;
  }
  dash.getRange(top + 1, 1, targets.length, head.length).setValues(targets);
  // 타겟 강조: 보라 배경 + 판정 굵게
  dash.getRange(top + 1, 1, targets.length, head.length).setBackground('#ede9fe');
  for (var i = 0; i < targets.length; i++) {
    dash.getRange(top + 1 + i, 7).setValue('🎯 TARGET').setFontColor('#6d28d9').setFontWeight('bold');
  }
}

/* 최초 1회 실행: 5분 간격 시간 트리거 설치 */
function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'refreshDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('refreshDashboard').timeBased().everyMinutes(5).create();
}
