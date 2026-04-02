// ─── Google Apps Script: X自動投稿トリガー ───
// 
// 設定方法:
// 1. https://script.google.com にアクセス
// 2. 「新しいプロジェクト」を作成
// 3. このコードを貼り付け
// 4. GITHUB_TOKEN を自分のトークンに置き換え
// 5. 「実行」→「triggerMorning」を一度実行して権限を許可
// 6. 「トリガー」（時計アイコン）→「トリガーを追加」で以下3つを設定
//
// ─── トリガー設定 ───
// triggerMorning  → 毎日 7:00〜8:00 の時間ベース（日〜木のみ動作）
// triggerWeekday  → 毎日 12:00〜13:00 の時間ベース（月〜金のみ動作）
// triggerEvening  → 毎日 20:00〜21:00 の時間ベース（全曜日、土日は別モード）
//
// ─── 土日レース日用 ───
// triggerPredict   → 毎日 7:00〜8:00（土日のみ動作）
// triggerOddsFlash → 毎日 9:00〜10:00（土日のみ動作）
// triggerHitFlash  → 毎日 15:00〜16:00（土日のみ動作）

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

function triggerMorning() {
  var dow = new Date().getDay(); // 0=日, 1=月, ..., 6=土
  // 月〜金のみ (前日UTC 22:30 = JST 7:30 なので、JST基準で月〜金)
  if (dow >= 1 && dow <= 5) {
    dispatchWorkflow("morning");
  } else {
    Logger.log("⏭️ 土日はスキップ (morning)");
  }
}

function triggerWeekday() {
  var dow = new Date().getDay();
  if (dow >= 1 && dow <= 5) {
    dispatchWorkflow("weekday");
  } else {
    Logger.log("⏭️ 土日はスキップ (weekday)");
  }
}

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

function triggerPredict() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("predict");
  } else {
    Logger.log("⏭️ 平日はスキップ (predict)");
  }
}

function triggerOddsFlash() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("odds_flash");
  } else {
    Logger.log("⏭️ 平日はスキップ (odds_flash)");
  }
}

function triggerHitFlash() {
  var dow = new Date().getDay();
  if (dow === 0 || dow === 6) {
    dispatchWorkflow("hit_flash");
  } else {
    Logger.log("⏭️ 平日はスキップ (hit_flash)");
  }
}

// ─── テスト用 ───
function testTrigger() {
  var code = dispatchWorkflow("morning");
  Logger.log("テスト完了: HTTP " + code);
}
