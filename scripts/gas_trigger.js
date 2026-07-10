// ─── Google Apps Script: 投稿トリガー (外部 cron 層) — #63 (2026-06-10 再設計) ───
//
// 役割: GitHub Actions の cron は不発・遅延が常態 (CLAUDE.md #22/#36/#56/#62)。
// Google インフラから workflow_dispatch を叩く「故障モードが独立した第2の時計」。
// 多重発火しても auto_post_x 側の atomic lock (posted_slots) + concurrency で冪等なので安全。
//
// ⚠️ セキュリティ: トークンをこのファイルに書かないこと。
//    旧版 (2026-06-07 削除) は PAT をハードコードして git 履歴に漏洩し、
//    GitHub secret scanning に自動失効させられて GAS が静かに死んだ (#63)。
//    必ず「プロジェクトの設定 → スクリプト プロパティ」に GITHUB_TOKEN として保存する。
//
// セットアップ手順: docs/GAS_SETUP.md 参照 (10分)

var REPO = "daiki0726m-oss/equinox-lab";
var WORKFLOW = "auto_post_x.yml";

function getToken_() {
  var token = PropertiesService.getScriptProperties().getProperty("GITHUB_TOKEN");
  if (!token) throw new Error("Script Properties に GITHUB_TOKEN が未設定です (docs/GAS_SETUP.md 参照)");
  return token;
}

function dispatchWorkflow(mode) {
  var url = "https://api.github.com/repos/" + REPO + "/actions/workflows/" + WORKFLOW + "/dispatches";
  var options = {
    method: "post",
    headers: {
      "Authorization": "Bearer " + getToken_(),
      "Accept": "application/vnd.github+json",
    },
    contentType: "application/json",
    payload: JSON.stringify({ ref: "main", inputs: { mode: mode } }),
    muteHttpExceptions: true,
  };
  var res = UrlFetchApp.fetch(url, options);
  var code = res.getResponseCode();
  if (code === 204) {
    Logger.log("✅ dispatch 成功: " + mode);
  } else {
    // 401/404 = トークン失効や権限不足。気づけるようログ + 例外
    Logger.log("❌ dispatch 失敗 (" + code + "): " + res.getContentText().slice(0, 200));
    throw new Error("dispatch 失敗 mode=" + mode + " HTTP " + code);
  }
}

// JST の曜日 (0=日 .. 6=土)
function jstDay_() {
  return Number(Utilities.formatDate(new Date(), "Asia/Tokyo", "u")) % 7;
}
function isWeekday_() { var d = jstDay_(); return d >= 1 && d <= 5; }
function isWeekend_() { var d = jstDay_(); return d === 0 || d === 6; }

// ═══ 平日 slot (月-金) ═══
function triggerMorning()  { if (isWeekday_()) dispatchWorkflow("morning"); }   // 7-8時
function triggerWeekday()  { if (isWeekday_()) dispatchWorkflow("weekday"); }   // 12-13時
function triggerEvening()  { if (isWeekday_()) dispatchWorkflow("evening"); }   // 20-21時

// ═══ 土日 slot ═══
function triggerPredict()          { if (isWeekend_()) dispatchWorkflow("predict"); }           // 7-8時
function triggerOddsFlash()        { if (isWeekend_()) dispatchWorkflow("odds_flash"); }        // 9-10時
function triggerPostPredict()      { if (isWeekend_()) dispatchWorkflow("post_predict"); }      // 10-11時
function triggerResults()          { if (isWeekend_()) dispatchWorkflow("results"); }           // 17-18時
function triggerRefreshDashboard() { if (isWeekend_()) dispatchWorkflow("refresh_dashboard"); } // 18-19時
// (旧 hit_flash は #57 で廃止 — トリガー不要)

// 初回テスト用: 実行して 204 が返ればトークン設定 OK (refresh_dashboard は無害)
function testDispatch() { dispatchWorkflow("refresh_dashboard"); }

// ═══ セットアップ: 時刻トリガー8個を一括作成 (1回だけ実行) ═══
// UI でトリガーを8個手作業で作るのは面倒+ミスりやすいので、コードで冪等に作る。
// (2026-06-10 実セットアップで使用済み — 再セットアップ時もこれを実行するだけ)
function setupTriggers() {
  // 既存のトリガーを全削除してから作り直す (冪等)
  ScriptApp.getProjectTriggers().forEach(function(t) { ScriptApp.deleteTrigger(t); });
  var defs = [
    ["triggerMorning", 7],          // 平日朝 7-8時
    ["triggerWeekday", 12],         // 平日昼 12-13時
    ["triggerEvening", 20],         // 平日夜 20-21時
    ["triggerPredict", 7],          // 土日 予測 7-8時
    ["triggerOddsFlash", 9],        // 土日 オッズ 9-10時
    ["triggerPostPredict", 10],     // 土日 予想投稿 10-11時
    ["triggerResults", 17],         // 土日 結果 17-18時
    ["triggerRefreshDashboard", 18] // 土日 dashboard 18-19時
  ];
  defs.forEach(function(d) {
    ScriptApp.newTrigger(d[0]).timeBased().atHour(d[1]).everyDays(1)
      .inTimezone("Asia/Tokyo").create();
  });
  Logger.log("✅ トリガー " + ScriptApp.getProjectTriggers().length + " 個を作成");
}
