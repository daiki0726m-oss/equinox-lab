#!/usr/bin/env python3
"""予測時点スナップショット (prediction_snapshot.py, #169) の単体テスト。

  python3 scripts/test_prediction_snapshot.py        (失敗があれば exit 1)

小さな合成 DB (一時ファイル) と一時ディレクトリだけを使い、本番 DB・docs/data は触らない。
確かめること:
  - 既定はローカルで書かない / CI (GITHUB_ACTIONS=true) で書く / PREDICTION_SNAPSHOT=0 で止まる
  - skip したレースと過去日のレースは書かない
  - 予測時オッズ・人気・印・取消 (999.9倍) が cache / results のとおりに入る。実オッズが無い馬は null
  - 中身が直前の版と同じなら書かない (重複除去)、変われば変わったレースだけ書く
  - cache の created_at が run の時刻でない時は書き込み時刻で代用し time_src を付ける
  - 読み出し: latest_before (指定時刻より前の最終版) / at_posting (投稿印との一致)
  - run 単位の版 (sha256 先頭12桁)・フラグ・GITHUB_* が入る
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import database  # noqa: E402
import prediction_snapshot as ps  # noqa: E402

JST = timezone(timedelta(hours=9))
FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name} {detail}")


def _env(**kw):
    """テスト用に env を差し替える (None は削除)。"""
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _seed(conn, race_id, race_date, horses, created_at="now"):
    """horses: [(馬番, 馬名, results.odds, 印, cache の odds_win, 人気)]"""
    conn.execute(
        "INSERT OR REPLACE INTO races (race_id, race_date, venue, race_number, distance, surface,"
        " track_condition, weather) VALUES (?,?,?,?,?,?,?,?)",
        (race_id, race_date, "東京", int(race_id[-2:]), 1600, "芝", "稍", "曇"))
    conn.execute("DELETE FROM results WHERE race_id=?", (race_id,))
    for hn, name, ro, _mk, _ow, _pop in horses:
        conn.execute("INSERT OR IGNORE INTO horses (horse_id, horse_name) VALUES (?,?)",
                     (f"h{race_id}{hn}", name))
        conn.execute("INSERT INTO results (race_id, horse_id, horse_number, odds, popularity,"
                     " finish_position) VALUES (?,?,?,?,?,0)",
                     (race_id, f"h{race_id}{hn}", hn, ro, _pop))
    preds = [{"horse_number": hn, "horse_name": name, "mark": mk, "odds_win": ow,
              "popularity": pop, "pred_win_pct": 10.0 + hn, "pred_win_display_pct": 12.5 + hn,
              "pred_top3_pct": 20.0 + hn, "pred_top3_abs_pct": 40.0 + hn, "rank_score": -0.1234 * hn,
              "ability_score": 0.12345 * hn, "sc_points": 3.14159 * hn}
             for hn, name, _ro, mk, ow, pop in horses]
    bets = {"ワイド": [{"type": "ワイド", "detail": "1-2", "amount": 100},
                    {"type": "ワイド", "detail": "1-3", "amount": 0}]}
    ca = "datetime('now')" if created_at == "now" else f"'{created_at}'"
    conn.execute(
        "INSERT OR REPLACE INTO predictions_cache (race_id, predictions_json, all_bets_json,"
        " confidence, conf_reason, should_bet, bet_reason, created_at)"
        f" VALUES (?,?,?,?,?,?,?,{ca})",
        (race_id, json.dumps(preds, ensure_ascii=False), json.dumps(bets, ensure_ascii=False),
         "A", "◎勝率31% / 信頼騎手+2", 1, "OK"))


def main():
    tmp = tempfile.mkdtemp(prefix="snaptest_")
    database.DB_PATH = os.path.join(tmp, "keiba.db")
    database.init_db()
    snap = os.path.join(tmp, "snapshots")
    saved = {k: os.environ.get(k) for k in (
        "GITHUB_ACTIONS", "PREDICTION_SNAPSHOT", "PREDICTION_SNAPSHOT_DIR",
        "PREDICTION_SNAPSHOT_ALLOW_PAST", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
        "GITHUB_EVENT_NAME", "GITHUB_SHA", "GITHUB_WORKFLOW", "MARKS_TEST_FLAG")}

    today = datetime.now(JST)
    fut = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    fut_ymd = fut.replace("-", "")
    past = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    R1, R2, R3, RP = "209905050101", "209905050102", "209905050103", "209905050199"
    with database.get_db() as c:
        _seed(c, R1, fut, [(1, "アルファ", 3.2, "◎", 3.2, 1), (2, "ベータ", 999.9, "", 999.9, 3),
                           (3, "ガンマ", 0.0, "○", 12.0, 0)])
        _seed(c, R2, fut, [(1, "デルタ", 5.0, "◎", 5.0, 1), (2, "イプシロン", 8.0, "☆", 8.0, 2)])
        _seed(c, R3, fut, [(1, "ゼータ", 2.0, "◎", 2.0, 1)])
        _seed(c, RP, past, [(1, "オメガ", 4.0, "◎", 4.0, 1)])

    class A:      # argparse の代わり
        date = fut_ymd
        force = False

    try:
        # ── 1. 既定はローカルで書かない ──
        _env(GITHUB_ACTIONS=None, PREDICTION_SNAPSHOT=None, PREDICTION_SNAPSHOT_DIR=snap)
        check("local-default-off", ps.record_run([R1], [], args=A) == [])
        check("local-default-off-nofile", not os.path.exists(snap))
        # CI でも PREDICTION_SNAPSHOT=0 なら書かない
        _env(GITHUB_ACTIONS="true", PREDICTION_SNAPSHOT="0")
        check("ci-forced-off", ps.record_run([R1], [], args=A) == [])

        # ── 2. CI で書く。skip したレース (R3) と過去日 (RP) は書かない ──
        _env(PREDICTION_SNAPSHOT=None, GITHUB_RUN_ID="111", GITHUB_RUN_ATTEMPT="1",
             GITHUB_EVENT_NAME="schedule", GITHUB_SHA="deadbeef", GITHUB_WORKFLOW="auto",
             MARKS_TEST_FLAG="7")
        w = ps.record_run([R1, R2, R3, RP], [(R3, "オッズ全頭0 (degenerate回避)")], args=A)
        check("ci-written-one-file", len(w) == 1, str(w))
        doc = ps.load_snapshot(w[0]) if w else {}
        check("path-layout", w and w[0].startswith(os.path.join(snap, fut_ymd) + os.sep)
              and w[0].endswith("_111.json"), str(w))
        rids = [r["race_id"] for r in doc.get("races", [])]
        check("skip-and-past-excluded", rids == [R1, R2], str(rids))
        check("compact-json", w and "\n" not in open(w[0], encoding="utf-8").read())
        run = doc.get("run", {})
        check("run-meta", run.get("run_id") == "111" and run.get("event") == "schedule"
              and run.get("sha") == "deadbeef" and run.get("workflow") == "auto", str(run))
        vers = doc.get("versions", {})
        check("versions-models", any(k.startswith("models/") and k.endswith(".pkl") for k in vers)
              and all(v is None or len(v) == 12 for v in vers.values()), str(vers)[:200])
        check("versions-tables", "models/train_boundary.json" in vers
              and "docs/data/race_baseline.json" in vers, str(list(vers)))
        fl = doc.get("flags", {})
        check("flags-config", "MARKS_SCORECARD" in fl, str(fl))
        check("flags-env-prefix", fl.get("MARKS_TEST_FLAG") == "7", str(fl))
        check("written_at-jst", str(doc.get("written_at", "")).endswith("+09:00"))

        r1 = ps._expand(doc["races"][0], doc, w[0])
        hs = {h["horse_number"]: h for h in r1["horses"]}
        check("odds-real", hs[1]["odds"] == 3.2 and hs[1]["popularity"] == 1, str(hs[1]))
        check("odds-estimated-null", hs[3]["odds"] is None and hs[3]["popularity"] == 0, str(hs[3]))
        check("scratched-999", hs[2]["scratched"] == 1 and hs[1]["scratched"] == 0)
        check("marks", hs[1]["mark"] == "◎" and hs[3]["mark"] == "○" and hs[2]["mark"] == "")
        check("rounding", hs[3]["sc_points"] == 9.425 and hs[3]["ability_score"] == 0.3704
              and hs[3]["rank_score"] == -0.37, str(hs[3]))
        check("race-fields", r1["track_condition"] == "稍" and r1["weather"] == "曇"
              and r1["confidence"] == "A" and r1["should_bet"] == 1, str(r1)[:200])
        check("funded-bets-only", r1["bets"] == [["ワイド", "1-2", 100]], str(r1["bets"]))
        check("created_at-from-cache", "time_src" not in r1
              and r1["created_at"].endswith("+09:00"), str(r1.get("created_at")))

        # ── 3. 中身が同じなら書かない ──
        _env(GITHUB_RUN_ID="112")
        check("dedupe-all-same", ps.record_run([R1, R2], [], args=A) == [])

        # ── 4. R2 のオッズが動いた再予測 → R2 だけ書く ──
        t_before_r2b = datetime.now(JST)
        with database.get_db() as c:
            _seed(c, R2, fut, [(1, "デルタ", 4.4, "◎", 4.4, 1), (2, "イプシロン", 9.1, "", 9.1, 2)],
                  created_at=(datetime.now(timezone.utc) + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S"))
        _env(GITHUB_RUN_ID="113")
        w2 = ps.record_run([R1, R2], [], args=A)
        d2 = ps.load_snapshot(w2[0]) if w2 else {}
        check("dedupe-changed-only", [r["race_id"] for r in d2.get("races", [])] == [R2],
              str([r["race_id"] for r in d2.get("races", [])]))

        # ── 5. created_at が古い (run の時刻でない) → 書き込み時刻で代用 ──
        time.sleep(2.1)     # 代用時刻 (秒単位) が上の t_before_r2b+1秒 より後になるように
        with database.get_db() as c:
            old = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
            _seed(c, R3, fut, [(1, "ゼータ", 2.5, "◎", 2.5, 1)], created_at=old)
        _env(GITHUB_RUN_ID="114")
        w3 = ps.record_run([R3], [], args=A)
        d3 = ps.load_snapshot(w3[0]) if w3 else {}
        r3 = (d3.get("races") or [{}])[0]
        check("stale-created_at-substituted", r3.get("time_src") == "written_at"
              and r3.get("created_at") == d3.get("written_at"), str(r3)[:160])

        # ── 6. 読み出し ──
        lst = ps.list_snapshots(fut_ymd)
        check("list", [s["run_id"] for s in lst] == ["111", "113", "114"], str(lst))
        lb_all = ps.latest_before(fut_ymd)
        check("latest-all", set(lb_all) == {R1, R2, R3}
              and lb_all[R2]["horses"][0]["odds"] == 4.4, str(lb_all.get(R2, {}).get("horses")))
        lb_mid = ps.latest_before(fut_ymd, t_before_r2b + timedelta(seconds=1))
        check("latest-before-mid", lb_mid.get(R2, {}).get("horses", [{}])[0].get("odds") == 5.0,
              str(lb_mid.get(R2, {}).get("horses")))
        check("latest-before-excludes-later", R3 not in lb_mid)
        lb_early = ps.latest_before(fut_ymd, "2000-01-01 00:00")
        check("latest-before-none", lb_early == {}, str(lb_early))
        check("latest-attached-meta", lb_all[R1]["versions"] and lb_all[R1]["run"]["run_id"] == "111")

        # at_posting: R2 は最初の版の印 (☆2 付き) で投稿した (= 後の再予測が配信に届かなかった形)
        pm = os.path.join(tmp, f"posted_marks_{fut_ymd}.json")
        json.dump({"date": fut_ymd, "posted_at": (datetime.now(JST) + timedelta(minutes=1)).isoformat(),
                   "races": {R1: [{"mark": "◎", "horse_number": 1}, {"mark": "○", "horse_number": 3}],
                             R2: [{"mark": "◎", "horse_number": 1}, {"mark": "☆", "horse_number": 2}]}},
                  open(pm, "w"), ensure_ascii=False)
        ap = ps.at_posting(fut_ymd, marks_path=pm)
        check("at_posting-latest", ap.get(R1, {}).get("posting_match") == "latest", str(ap.get(R1, {}).get("posting_match")))
        check("at_posting-earlier", ap.get(R2, {}).get("posting_match") == "earlier"
              and ap[R2]["horses"][0]["odds"] == 5.0, str(ap.get(R2, {}).get("posting_match")))
        check("at_posting-only-posted", set(ap) == {R1, R2}, str(set(ap)))

        # ── 7. ALLOW_PAST で過去日も書ける ──
        _env(PREDICTION_SNAPSHOT_ALLOW_PAST="1", GITHUB_RUN_ID="115")
        wp = ps.record_run([RP], [], args=A)
        check("allow-past", len(wp) == 1 and os.sep + past.replace("-", "") + os.sep in wp[0], str(wp))
    finally:
        _env(**saved)
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 8. 配線: predict.cmd_predict が呼び、予測を回す全 workflow が snapshots を commit する ──
    src = open(os.path.join(ROOT, "predict.py"), encoding="utf-8").read()
    body = src.split("def cmd_predict", 1)[-1].split("\ndef ", 1)[0]
    i_call = body.find("prediction_snapshot.record_run(")
    i_exit = body.rfind("sys.exit(1)")   # コメント中の言及ではなく実際の呼び出し (最後のもの)
    check("predict-calls-record_run", i_call > 0, "cmd_predict に record_run の呼び出しが無い")
    check("record_run-before-exit", i_call > 0 and (i_exit < 0 or i_call < i_exit),
          "メインレース skip の sys.exit(1) より前に呼ぶこと")
    wf_dir = os.path.join(ROOT, ".github", "workflows")
    for fn in sorted(os.listdir(wf_dir)):
        if not fn.endswith(".yml"):
            continue
        t = open(os.path.join(wf_dir, fn), encoding="utf-8").read()
        runs_predict = ("predict.py predict" in t) or ("preflight_check.py" in t and "--auto-fix" in t)
        if runs_predict:
            check(f"workflow-commits-snapshots:{fn}",
                  "git add docs/data/ " in t or "git add docs/data/\n" in t or "docs/data/snapshots" in t,
                  "予測を回すのに docs/data/snapshots/ を git add していない")

    if FAILS:
        print(f"❌ {len(FAILS)} 件失敗")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("✅ prediction_snapshot: 全テスト合格")


if __name__ == "__main__":
    main()
