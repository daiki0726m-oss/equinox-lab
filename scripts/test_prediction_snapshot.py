#!/usr/bin/env python3
"""予測時点スナップショット (prediction_snapshot.py, #169) の単体テスト。

  python3 scripts/test_prediction_snapshot.py        (失敗があれば exit 1)

小さな合成 DB (一時ファイル) と一時ディレクトリだけを使い、本番 DB・docs/data は触らない。
確かめること:
  - 既定はローカルで書かない / CI (GITHUB_ACTIONS=true) で書く / PREDICTION_SNAPSHOT=0 で止まる
  - skip したレースと過去日のレースは書かない
  - 予測時オッズ・人気・印・取消 (人気9999 の目印 #171。999.9倍だけでは取消にしない) が cache / results のとおりに入る。実オッズが無い馬は null
  - 取消の判定が predict._is_scratch_odds と同じ (cache の人気 9999・"_scratched" キーも取消。
    実オッズの無い馬の推定オッズ 1000倍超は取消にしない)
  - horse_id と numbers_confirmed (全頭 post_position>0) が入る
  - 予測時点で発走済みのレースは書かない (ALLOW_PAST の時だけ post_start=1 で書き、既定で読まない)
  - 中身が直前の版と同じなら書かない (重複除去)、変われば変わったレースだけ書く
  - cache の created_at が run の時刻でない時は書き込み時刻で代用し time_src を付ける
  - 読み出し: latest_before (指定時刻より前の最終版) / at_posting (pred_created_at での完全一致・
    重複除去で書かれなかった版の引き継ぎ・印の集合での照合と ambiguous)
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


def _seed(conn, race_id, race_date, horses, created_at="now", start_time="15:40", drawn=True,
          extra=None, res_pop=None):
    """horses: [(馬番, 馬名, results.odds, 印, cache の odds_win, 人気)]
    drawn: 枠番 (post_position) を入れる = 枠順確定後 / extra: {馬番: cache に足すキー}
    res_pop: {馬番: results の人気} (#171: 出馬表の取消行で results に書く人気 9999 の再現用)"""
    conn.execute(
        "INSERT OR REPLACE INTO races (race_id, race_date, venue, race_number, distance, surface,"
        " track_condition, weather, start_time) VALUES (?,?,?,?,?,?,?,?,?)",
        (race_id, race_date, "東京", int(race_id[-2:]), 1600, "芝", "稍", "曇", start_time))
    conn.execute("DELETE FROM results WHERE race_id=?", (race_id,))
    for hn, name, ro, _mk, _ow, _pop in horses:
        conn.execute("INSERT OR IGNORE INTO horses (horse_id, horse_name) VALUES (?,?)",
                     (f"h{race_id}{hn}", name))
        # results の人気は API の番兵 (9999) を書かない (#149/#150) — cache にだけ入る形を再現
        conn.execute("INSERT INTO results (race_id, horse_id, horse_number, odds, popularity,"
                     " post_position, finish_position) VALUES (?,?,?,?,?,?,0)",
                     (race_id, f"h{race_id}{hn}", hn, ro,
                      (res_pop or {}).get(hn, 0 if _pop >= 999 else _pop),
                      (hn + 1) // 2 if drawn else 0))
    preds = [dict({"horse_number": hn, "horse_name": name, "mark": mk, "odds_win": ow,
                   "popularity": pop, "pred_win_pct": 10.0 + hn,
                   "pred_win_display_pct": 12.5 + hn, "pred_top3_pct": 20.0 + hn,
                   "pred_top3_abs_pct": 40.0 + hn, "rank_score": -0.1234 * hn,
                   "ability_score": 0.12345 * hn, "sc_points": 3.14159 * hn},
                  **((extra or {}).get(hn) or {}))
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
    today_s = today.strftime("%Y-%m-%d")
    R1, R2, R3, RP = "209905050101", "209905050102", "209905050103", "209905050199"
    RS, RT = "209905050104", "209905050105"
    with database.get_db() as c:
        # R1: 2番 = 999.9倍・3人気 (前日発売の直後の票の少ない走る馬。#171 で取消にしない) /
        #     3番 = 実オッズ無し (推定) / 4番 = API の番兵 (cache の人気 9999・results は 0倍) /
        #     5番 = predict の "_scratched" / 6番 = 実オッズ無しで推定オッズが 1000倍超 (取消ではない) /
        #     7番 = 出馬表の取消行で results に書いた目印 (999.9倍・人気9999 #171。cache は API の
        #           発売中の値 999.9倍・人気=頭数)
        _seed(c, R1, fut, [(1, "アルファ", 3.2, "◎", 3.2, 1), (2, "ベータ", 999.9, "", 999.9, 3),
                           (3, "ガンマ", 0.0, "○", 12.0, 0), (4, "シータ", 0.0, "", 22.6, 9999),
                           (5, "イオタ", 0.0, "", 30.0, 0), (6, "カッパ", 0.0, "", 1600.0, 0),
                           (7, "ラムダ", 999.9, "", 999.9, 7)],
              extra={5: {"_scratched": True}}, res_pop={7: 9999})
        # R2 は枠順確定前 (post_position=0) の馬番
        _seed(c, R2, fut, [(1, "デルタ", 5.0, "◎", 5.0, 1), (2, "イプシロン", 8.0, "☆", 8.0, 2)],
              drawn=False)
        _seed(c, R3, fut, [(1, "ゼータ", 2.0, "◎", 2.0, 1)])
        _seed(c, RP, past, [(1, "オメガ", 4.0, "◎", 4.0, 1)])
        # RS: 今日の 00:00 発走 = 予測した時点で発走済み (発走後の --force 再予測の形)
        _seed(c, RS, today_s, [(1, "シグマ", 3.0, "◎", 3.0, 1)], start_time="00:00")
        # RT: 今日のレースだが発走時刻が不明 → 判定できないので書く
        _seed(c, RT, today_s, [(1, "タウ", 3.0, "◎", 3.0, 1)], start_time="")

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
        # 今日のレース: 発走済み (RS) は書かず、発走時刻不明 (RT) は書く
        _env(GITHUB_RUN_ID="110")
        wt = ps.record_run([RS, RT], [], args=A)
        dt = ps.load_snapshot(wt[0]) if wt else {}
        check("post-start-excluded", [r["race_id"] for r in dt.get("races", [])] == [RT],
              str([r["race_id"] for r in dt.get("races", [])]))
        _env(GITHUB_RUN_ID="111")
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
        # #171: 999.9倍だけでは取消にしない / 出馬表の取消行の目印 (results の人気 9999) は取消
        check("not-scratched-999-thin-market", hs[2]["scratched"] == 0 and hs[1]["scratched"] == 0,
              str(hs[2]))
        check("scratched-sentinel-results-9999", hs[7]["scratched"] == 1, str(hs[7]))
        check("scratched-cache-pop-9999", hs[4]["scratched"] == 1, str(hs[4]))
        check("scratched-predict-key", hs[5]["scratched"] == 1, str(hs[5]))
        check("not-scratched-estimated-1600x", hs[6]["scratched"] == 0 and hs[3]["scratched"] == 0,
              str(hs[6]))
        check("horse_id", hs[1]["horse_id"] == f"h{R1}1" and hs[6]["horse_id"] == f"h{R1}6",
              str(hs[1]))
        r2 = ps._expand(doc["races"][1], doc, w[0])
        check("numbers_confirmed", r1["numbers_confirmed"] == 1 and r2["numbers_confirmed"] == 0,
              f"{r1['numbers_confirmed']} {r2['numbers_confirmed']}")
        check("start_time", r1["start_time"] == "15:40" and "post_start" not in r1, str(r1)[:120])
        check("cache_created_at-raw", r1["cache_created_at"] and "+" not in r1["cache_created_at"],
              str(r1.get("cache_created_at")))
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
        # R2 の1版目は枠順確定前 (post_position=0)、2版目は確定後。確定後だけを読むと
        # 1版目しか無い時点では R2 は出てこない
        lb_conf_mid = ps.latest_before(fut_ymd, t_before_r2b + timedelta(seconds=1), confirmed_only=True)
        check("latest-confirmed-only", R2 not in lb_conf_mid and R1 in lb_conf_mid
              and R2 in lb_mid, str(set(lb_conf_mid)))
        check("latest-confirmed-only-later",
              ps.latest_before(fut_ymd, confirmed_only=True).get(R2, {}).get("numbers_confirmed") == 1)

        # at_posting: R2 は最初の版の印 (☆2 付き) で投稿した (= 後の再予測が配信に届かなかった形)
        pm = os.path.join(tmp, f"posted_marks_{fut_ymd}.json")
        json.dump({"date": fut_ymd, "posted_at": (datetime.now(JST) + timedelta(minutes=1)).isoformat(),
                   "races": {R1: [{"mark": "◎", "horse_number": 1}, {"mark": "○", "horse_number": 3}],
                             R2: [{"mark": "◎", "horse_number": 1}, {"mark": "☆", "horse_number": 2}]}},
                  open(pm, "w"), ensure_ascii=False)
        ap = ps.at_posting(fut_ymd, marks_path=pm)
        check("at_posting-latest", ap.get(R1, {}).get("posting_match") == "latest"
              and ap[R1]["n_match"] == 1, str(ap.get(R1, {}).get("posting_match")))
        check("at_posting-earlier", ap.get(R2, {}).get("posting_match") == "earlier"
              and ap[R2]["horses"][0]["odds"] == 5.0, str(ap.get(R2, {}).get("posting_match")))
        check("at_posting-only-posted", set(ap) == {R1, R2}, str(set(ap)))

        # 投稿した版の cache 時刻 (post_x の pred_created_at) があれば、印でなく時刻で特定する
        v_r2 = [v for v in ps.iter_race_versions(fut_ymd) if v["race_id"] == R2]
        v_r1 = [v for v in ps.iter_race_versions(fut_ymd) if v["race_id"] == R1]
        r1_later = (datetime.fromisoformat(v_r1[0]["created_at"]).astimezone(timezone.utc)
                    + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
        pm2 = os.path.join(tmp, "posted_marks_exact.json")
        json.dump({"date": fut_ymd, "posted_at": (datetime.now(JST) + timedelta(minutes=1)).isoformat(),
                   "races": {R1: [{"mark": "◎", "horse_number": 1}, {"mark": "○", "horse_number": 3}],
                             R2: [{"mark": "◎", "horse_number": 1}]},
                   # R2 = 2版目そのもの / R1 = 重複除去で書かれなかった run (112) を配信した形
                   "pred_created_at": {R2: v_r2[-1]["cache_created_at"], R1: r1_later}},
                  open(pm2, "w"), ensure_ascii=False)
        ap2 = ps.at_posting(fut_ymd, marks_path=pm2)
        check("at_posting-exact", ap2.get(R2, {}).get("posting_match") == "exact"
              and ap2[R2]["horses"][0]["odds"] == 4.4, str(ap2.get(R2, {}).get("posting_match")))
        check("at_posting-carried", ap2.get(R1, {}).get("posting_match") == "carried",
              str(ap2.get(R1, {}).get("posting_match")))

        # 同じ印のまま信頼度・オッズだけ違う版が2つ → 印では特定できない (ambiguous)
        time.sleep(1.1)
        with database.get_db() as c:
            _seed(c, R1, fut, [(1, "アルファ", 3.6, "◎", 3.6, 1), (2, "ベータ", 999.9, "", 999.9, 3),
                               (3, "ガンマ", 0.0, "○", 12.0, 0), (4, "シータ", 0.0, "", 22.6, 9999),
                               (5, "イオタ", 0.0, "", 30.0, 0), (6, "カッパ", 0.0, "", 1600.0, 0),
                               (7, "ラムダ", 999.9, "", 999.9, 7)],
                  extra={5: {"_scratched": True}}, res_pop={7: 9999})
        _env(GITHUB_RUN_ID="116")
        check("r1-second-version", len(ps.record_run([R1], [], args=A)) == 1)
        ap3 = ps.at_posting(fut_ymd, marks_path=pm)
        check("at_posting-ambiguous", ap3.get(R1, {}).get("posting_match") == "ambiguous"
              and ap3[R1]["n_match"] == 2 and ap3[R1]["horses"][0]["odds"] == 3.6,
              str({k: ap3.get(R1, {}).get(k) for k in ("posting_match", "n_match")}))

        # ── 7. ALLOW_PAST で過去日も書ける。発走済みのレースは post_start=1 で書き、既定で読まない ──
        _env(PREDICTION_SNAPSHOT_ALLOW_PAST="1", GITHUB_RUN_ID="115")
        wp = ps.record_run([RP], [], args=A)
        check("allow-past", len(wp) == 1 and os.sep + past.replace("-", "") + os.sep in wp[0], str(wp))
        with database.get_db() as c:
            _seed(c, RS, today_s, [(1, "シグマ", 3.3, "◎", 3.3, 1)], start_time="00:00")
        ws = ps.record_run([RS], [], args=A)
        ds = ps.load_snapshot(ws[0]) if ws else {}
        check("allow-past-post_start-flag", [r.get("post_start") for r in ds.get("races", [])] == [1],
              str(ds.get("races", [{}])[:1])[:160])
        tymd = today_s.replace("-", "")
        check("reader-skips-post_start", RS not in ps.latest_before(tymd)
              and RT in ps.latest_before(tymd), str(set(ps.latest_before(tymd))))
        check("reader-include-post_start", RS in ps.latest_before(tymd, include_post_start=True))
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
