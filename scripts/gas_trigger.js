// ─── Google Apps Script: X自動投稿トリガー ───
// 
// 設定方法:
// 1. https://script.google.com にアクセス
// 2. 「新しいプロジェクト」を作成
// 3. このコードを貼り付け
// 4. GITHUB_TOKEN を自分のトークンに置き換え
// 5. 「実行」→「triggerMorning」を一度実行して権限を許可
// 6. 「トリガー」（時計アイコン）→「トリガーを追加」で以下を設定
//
// ═══════════════════════════════════════════════
// ★ 必要なトリガー一覧（全10個）
// ═══════════════════════════════════════════════
//
// ① triggerMorning         → 毎日 7:00〜8:00   (月〜金: おはようツイート)
// ② triggerPredict         → 毎日 7:00〜8:00   (土日: AI予測・内部のみ)
// ③ triggerOddsFlash       → 毎日 9:00〜10:00  (土日: オッズ更新・内部のみ)
// ③b triggerPostPredict    → 毎日 10:00〜11:00 (土日: 確定予想をX投稿)
// ④ triggerRefreshDashboard→ 毎日 11:00〜12:00 (土日: ダッシュボード更新)
// ⑤ triggerWeekday         → 毎日 12:00〜13:00 (月〜金: コース分析ツイート)
// ⑥ triggerHitFlash        → 毎日 15:00〜16:00 (土日: 的中速報)
// ⑦ triggerResults         → 毎日 17:00〜18:00 (土日: 結果報告)
// ⑧ triggerEvening         → 毎日 20:00〜21:00 (月〜金: 夜ツイート / 土: 答え合わせ / 日: 週間レビュー)
// ⑨ triggerRefreshDashboard2→毎日 18:00〜19:00 (土日: 結果後のダッシュボード最終更新)
//
// ═══════════════════════════════════════════════

var GITHUB_TOKEN = "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";
var REPO = "daiki0726m-oss/equinox-lab";
var WORKFLOW = "auto_post_x.yml";

function dispatchWorkflow(mode) {
  var url = "https://api.github.com/repos/" + REPO + "/actions/workflows/" + WORKFLOW + "/dispatches";
  
  var options = {
    "method": "post",
    "headers": {
      "Authorization": "Bearer " + GITHUB_TOKEN,
      "Accept": "application/vnd.github.v3+json",
      "Content-Type": "application/json"
    },
    "payload": JSON.stringify({
      "ref": "main",
      "inputs": {"mode": mode}
    }),
    "muteHttpExceptions": true
  };
  
  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  
  Logger.log("Mode: " + mode + " / Status: " + code);
  
  if (code === 204) {
    Logger.log("✅ " + mode + " トリガー成功");
  } else {
    Logger.log("❌ エラー: " + response.getContentText());
  }
  
  return code;
}

// ─── 平日用 ───

// ① 毎日 7:00〜8:00（月〜金のみ: おはようツイート）
function triggerMorning() {
  var dow = new Date().getDay(); // 0=日, 1=月, ..., 6=土
  if (dow >= 1 && dow <= 5) {
    dispatchWorkflow("morning");
  } else {
    Logger.log("⏭️ 土日はスキップ (morning)");
  }
}

// ⑤ 毎日 12:00〜13:00（月〜金のみ: 豆知識ツイート）
function triggerWeekday() {
  var dow = new Date().getDay();
  if (dow >= 1 && dow <= 5) {
    dispatchWorkflow("weekday");
  } else {
    Logger.log("⏭️ 土日はスキップ (weekday)");
  }
}

// ⑧ 毎日 20:00〜21:00（全曜日: 月〜金=evening / 土=answer_check / 日=weekly_review）
function triggerEvening() {
  var dow = new Date().getDay();
  if (dow >= 1 && dow <= 5) {
    dispatchWorkflow("evening");
  } else if (dow === 6) {
    // 土曜夜 = 答え合わせ
    dispatchWorkflow("answer_check");
  } else if (dow === 0) {
    // 日曜夜 = 週間レビュー
    dispatchWorkflow("weekly_review");
  }
}

// ─── 土日レース日用 ───

// ② 毎日 7:00〜8:00（土日のみ: AI予測）
function triggerPredict() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("predict");
  } else {
    Logger.log("⏭️ 平日はスキップ (predict)");
  }
}

// ③ 毎日 9:00〜10:00（土日のみ: オッズ更新・内部処理のみ）
function triggerOddsFlash() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("odds_flash");
  } else {
    Logger.log("⏭️ 平日はスキップ (odds_flash)");
  }
}

// ③b 毎日 10:00〜11:00（土日のみ: 確定予想をX投稿）
function triggerPostPredict() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("post_predict");
  } else {
    Logger.log("⏭️ 平日はスキップ (post_predict)");
  }
}

// ④⑨ 毎日 11:00〜12:00 & 18:00〜19:00（土日のみ: ダッシュボード更新）
function triggerRefreshDashboard() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("refresh_dashboard");
  } else {
    Logger.log("⏭️ 平日はスキップ (refresh_dashboard)");
  }
}
// ⑨のエイリアス
function triggerRefreshDashboard2() {
  triggerRefreshDashboard();
}

// ⑥ 毎日 15:00〜16:00（土日のみ: 的中速報）
function triggerHitFlash() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("hit_flash");
  } else {
    Logger.log("⏭️ 平日はスキップ (hit_flash)");
  }
}

// ⑦ 毎日 17:00〜18:00（土日のみ: 結果報告）
function triggerResults() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("results");
  } else {
    Logger.log("⏭️ 平日はスキップ (results)");
  }
}

// ─── 土日リアルタイム更新（15分おき） ───

// Live Dashboard Refresh: 土日10:00-17:00に15分おきにダッシュボードを更新
function triggerLiveRefresh() {
  var dow = new Date().getDay(); // 0=日, 6=土
  var hour = new Date().getHours();
  
  if ((dow === 0 || dow === 6) && hour >= 10 && hour <= 17) {
    dispatchLiveRefresh();
  } else {
    Logger.log("⏭️ レース時間外またはは平日のためスキップ (live_refresh)");
  }
}

function dispatchLiveRefresh() {
  var url = "https://api.github.com/repos/" + REPO + "/actions/workflows/refresh_live.yml/dispatches";
  
  var options = {
    "method": "post",
    "headers": {
      "Authorization": "Bearer " + GITHUB_TOKEN,
      "Accept": "application/vnd.github.v3+json",
      "Content-Type": "application/json"
    },
    "payload": JSON.stringify({
      "ref": "main",
      "inputs": {"force_predict": "false"}
    }),
    "muteHttpExceptions": true
  };
  
  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  
  Logger.log("Live Refresh: HTTP " + code);
  if (code === 204) {
    Logger.log("✅ Live Refresh トリガー成功");
  } else {
    Logger.log("❌ エラー: " + response.getContentText());
  }
}

// ─── テスト用 ───
function testTrigger() {
  var code = dispatchWorkflow("morning");
  Logger.log("テスト完了: HTTP " + code);
}

// ─── 一括トリガー登録（初回のみ実行） ───
function setupAllTriggers() {
  // 既存のトリガーを全削除
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    ScriptApp.deleteTrigger(triggers[i]);
  }
  Logger.log("🗑️ 既存トリガーを全削除");

  // ① triggerMorning: 毎日 7:00〜8:00
  ScriptApp.newTrigger("triggerMorning")
    .timeBased().everyDays(1).atHour(7).create();

  // ② triggerPredict: 毎日 7:00〜8:00 (内部で土日判定)
  ScriptApp.newTrigger("triggerPredict")
    .timeBased().everyDays(1).atHour(7).create();

  // ③ triggerOddsFlash: 毎日 9:00〜10:00
  ScriptApp.newTrigger("triggerOddsFlash")
    .timeBased().everyDays(1).atHour(9).create();

  // ③b triggerPostPredict: 毎日 10:00〜11:00（確定予想投稿）
  ScriptApp.newTrigger("triggerPostPredict")
    .timeBased().everyDays(1).atHour(10).create();

  // ④ triggerRefreshDashboard: 毎日 11:00〜12:00
  ScriptApp.newTrigger("triggerRefreshDashboard")
    .timeBased().everyDays(1).atHour(11).create();

  // ⑤ triggerWeekday: 毎日 12:00〜13:00
  ScriptApp.newTrigger("triggerWeekday")
    .timeBased().everyDays(1).atHour(12).create();

  // ⑥ triggerHitFlash: 毎日 15:00〜16:00
  ScriptApp.newTrigger("triggerHitFlash")
    .timeBased().everyDays(1).atHour(15).create();

  // ⑦ triggerResults: 毎日 17:00〜18:00
  ScriptApp.newTrigger("triggerResults")
    .timeBased().everyDays(1).atHour(17).create();

  // ⑧ triggerEvening: 毎日 20:00〜21:00
  ScriptApp.newTrigger("triggerEvening")
    .timeBased().everyDays(1).atHour(20).create();

  // ⑨ triggerRefreshDashboard2: 毎日 18:00〜19:00
  ScriptApp.newTrigger("triggerRefreshDashboard2")
    .timeBased().everyDays(1).atHour(18).create();

  // ⑩ triggerLiveRefresh: 15分おき（土日レース時間帯のみ実行）
  ScriptApp.newTrigger("triggerLiveRefresh")
    .timeBased().everyMinutes(15).create();

  Logger.log("✅ 全11トリガーを登録完了");
}
