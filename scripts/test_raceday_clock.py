#!/usr/bin/env python3
"""#179 (2): 土日以外の開催日の時計 (raceday_clock) と周辺の検査。ネットワーク・gh を使わない。

  python3 scripts/test_raceday_clock.py     (失敗があれば exit 1)

検査:
  1. raceday_clock.yml の schedule == SCHEDULE、各 tick の対象日が JST 月〜金、土日に動くのは日曜の前夜 (対象日=月曜) だけ
  2. auto_post_x.yml の schedule == dedup_cron の case == determine の case (今回は変えないが取り違えを止める)
  3. planned_fire (定刻 / 遅延 / 日付またぎ / 前倒し / 20時間超)
  4. 11/23 (月・祝) の再現、前夜、順延、縮退、待機・重複
  5. race_utils.calendar_race_day の全分岐
  6. scripts/prefetch_targets.py が移設前の heredoc と同じ出力 (golden) + 対象日の指定
  7. build_race_baseline._weekend_gate_hold の土日以外の開催日
  8. 指定の時刻でのゲート表 (土日 07:00 / 月祝 04:00・07:00・09:00・09:30・17:30 / 普通の月曜 07:00 /
     月祝前の日曜 22:30 / 金曜 22:30)
  9. 一律の遅れ (0〜10時間) で、最初の predict と runner の起動がいつになるか (#179 レビュー M1)。odds_flash が
     どこかから出るか (T-clock-3) を、カレンダーに載っている日と問い合わせで見つける日の両方で 0〜13h19m 流す (round 4)
 10. (Part D) build_race_calendar --probe を偽の scraper と実ページと同じ形の HTML で: 平日の開催日を足す / 土日は
     足さない・問い合わせない / DB にある平日は補うだけ (T-partd-4) / JRA 場コード以外は足さない / 翌月が空・取れないのは
     正常 (T-weekend-1/T-partd-2) / 天候パネルの kaisai_date を拾わない (T-partd-3) / 一覧を取れない日は「開催なし」に
     しない (T-partd-3) / 前回の版で見つけた平日を引き継ぎ、消えたと確かめた日だけ外す (T-clock-1/T-partd-1/T-landing-3) /
     --probe なしも引き継ぐ。さらに、probe だけが見つけた日 (DB にレースが無い) で時計が predict と前夜予想を出す
 11. (T-clock-2) カレンダーの作り直しより後に決まった平日の開催を、時計が netkeiba の一覧で確かめて動く
     (round 4: 遅れて 8:30〜19:00 に着いた predict / 前夜の tick も、載っている日と同じく runner を起こす)
 12. (T-landing-4) 予想 (post_predict) の lock が無い日は results を出さない
"""
import atexit
import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import yaml  # noqa: E402

import raceday_clock as rc  # noqa: E402
import race_utils  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc
FAILS = []


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def jst(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=JST)


def wf_doc(name):
    with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return doc, doc.get("on", doc.get(True))     # PyYAML は on: を True と読む


def crons(name):
    _doc, on = wf_doc(name)
    return [c["cron"] for c in (on or {}).get("schedule") or []]


def cron_fires(cron, when):
    """cron (UTC) が時刻 when (aware) ちょうどに発火するか。"""
    mi, hh, _d, _m, dow = cron.split()
    u = when.astimezone(UTC)
    return (u.minute == int(mi) and u.hour == int(hh)
            and (u.weekday() + 1) % 7 in rc._dow_set(dow))


# ── 偽のカレンダーと lock ───────────────────────────────────────────
TMP = tempfile.mkdtemp(prefix="clock179_")
atexit.register(shutil.rmtree, TMP, True)


def make_calendar(dates, generated, window=("2026-11-09", "2026-12-07"), name="cal.json"):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated, "window": list(window),
                   "dates": {d: {"races": 12, "venues": ["東京"]} for d in dates}}, f)
    return p


def slots_dir(locks_by_date=None):
    d = tempfile.mkdtemp(dir=TMP)
    for day8, slots in (locks_by_date or {}).items():
        with open(os.path.join(d, f"posted_slots_{day8}.json"), "w", encoding="utf-8") as f:
            json.dump({"date": day8, "slots": {s: {"posted_at": "x"} for s in slots}}, f)
    return d


def run_clock(schedule=None, now=None, cal=None, slots=None, fake=None, intent=None, date=None):
    """main を呼んで (終了コード, dispatch したラベルの list, 出力) を返す。"""
    argv = ["--now", now, "--calendar", cal, "--slots-dir", slots or slots_dir(),
            "--fake-gh", json.dumps(fake or {}), "--dry-run"]
    if schedule:
        argv += ["--schedule", schedule]
    if intent:
        argv += ["--intent", intent]
    if date:
        argv += ["--date", date]
    env_bak = {k: os.environ.pop(k, None) for k in ("CLOCK_SCHEDULE", "CLOCK_INTENT", "CLOCK_DATE")}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = rc.main(argv)
    finally:
        for k, v in env_bak.items():
            if v is not None:
                os.environ[k] = v
    out = buf.getvalue()
    sent = re.findall(r"would dispatch: (.+)", out)
    return code, sent, out


# 2026-11-23 (月・祝) は東京・福島で開催、カレンダーは当日 07:13 生成 (本番と同じ)
HOLIDAY = "2026-11-23"
CAL_HOL = make_calendar(["2026-11-21", "2026-11-22", HOLIDAY], "2026-11-23T07:13:51+09:00")
# 前夜 (11/22 日曜) に読むカレンダー: 11/22 07:00 生成 (11/23 が載っている)
CAL_EVE = make_calendar(["2026-11-21", "2026-11-22", HOLIDAY], "2026-11-22T07:00:00+09:00", name="cal_eve.json")


def test_tables():
    print("\n1. raceday_clock.yml と SCHEDULE")
    ys = crons("raceday_clock.yml")
    check(set(ys) == set(rc.SCHEDULE) and len(ys) == len(rc.SCHEDULE),
          f"schedule ({len(ys)}本) == SCHEDULE のキー ({len(rc.SCHEDULE)}本)")
    weekend_fire = []
    bad = []
    for c, (intents, off) in rc.SCHEDULE.items():
        mi, hh, _d, _m, dow = c.split()
        for d in rc._dow_set(dow):
            u = dt.datetime(2026, 11, 22, int(hh), int(mi), tzinfo=UTC)   # 2026-11-22 は日曜 (cron 0)
            u += dt.timedelta(days=(d - 0) % 7)
            j = u.astimezone(JST)
            target = j.date() + dt.timedelta(days=off)
            if target.weekday() >= 5:
                bad.append(f"{c}: {j:%a %H:%M} → T={target:%a}")
            if j.weekday() >= 5:
                weekend_fire.append((c, intents))
    check(not bad, "すべての tick の対象日が JST の月〜金" + (f" (違反: {bad})" if bad else ""))
    check(weekend_fire and all(rc.SCHEDULE[c][1] == 1 and j_is_sun(c) and i in (("prefetch_eve",), ("runner",))
                               for c, i in weekend_fire),
          f"JST の土日に動く tick は日曜の前夜 (前夜予想・runner だけ、対象日=月曜) だけ ({len(weekend_fire)}本: "
          f"{sorted({c for c, _ in weekend_fire})})")
    doc, _on = wf_doc("raceday_clock.yml")
    runs = [st.get("run", "") for j in doc["jobs"].values() for st in j.get("steps", [])]
    check(all("${{" not in r for r in runs), "raceday_clock.yml の run ブロックに式が無い (#174)")

    print("\n2. auto_post_x.yml の3表 (schedule / dedup_cron / determine) の一致")
    text = open(os.path.join(ROOT, ".github", "workflows", "auto_post_x.yml"), encoding="utf-8").read()
    sched = set(crons("auto_post_x.yml"))
    dedup = dict(re.findall(r'"([0-9*, /-]+ \* \* [0-9,*-]+)"\)\s+EXPECTED_MODE="([a-z_]+)"', text))
    det = dict(re.findall(r'"([0-9*, /-]+ \* \* [0-9,*-]+)"\)\s+TYPE="([a-z_]+)"', text))
    check(sched == set(dedup) == set(det), f"3表のキーが一致 ({len(sched)}本)")
    check(dedup == det, "dedup_cron と determine の mode が一致")


def j_is_sun(c):
    mi, hh, _d, _m, dow = c.split()
    return int(hh) + 9 < 24 and rc._dow_set(dow) >= {0}


def test_planned_fire():
    print("\n3. planned_fire")
    c = "3 19 * * 0-4"     # JST 月〜金 04:03
    p = rc.planned_fire(c, jst("2026-11-23T04:03").astimezone(UTC))
    check(p and p.astimezone(JST) == jst("2026-11-23T04:03"), "定刻: 予定 = 11/23 04:03")
    p = rc.planned_fire(c, jst("2026-11-23T09:05").astimezone(UTC))
    check(p and p.astimezone(JST) == jst("2026-11-23T04:03"), "5時間遅れ: 予定 = 同日 04:03")
    p = rc.planned_fire(c, jst("2026-11-23T03:55").astimezone(UTC))
    check(p and p.astimezone(JST) == jst("2026-11-23T04:03"), "8分の前倒し: 予定 = 当日 04:03")
    p = rc.planned_fire(c, jst("2026-11-23T03:40").astimezone(UTC))
    check(p and p.astimezone(JST) == jst("2026-11-20T04:03"),
          "23分前 (前倒しの許容外): 直近の予定は曜日欄に合う前の日 = 金 11/20 04:03 (土日の分は無い)")
    c = "41 14 * * 0-4"    # JST 日〜木 23:41
    p = rc.planned_fire(c, jst("2026-11-23T00:30").astimezone(UTC))
    check(p and p.astimezone(JST) == jst("2026-11-22T23:41"), "日付またぎ: 月 00:30 の tick の予定は日 23:41")
    code, sent, out = run_clock("3 19 * * 0-4", "2026-11-24T02:00", CAL_HOL)
    check(code == 0 and not sent and "時間遅れた tick" in out, "20時間超の遅れは何もしない")


def test_holiday_and_eve():
    print("\n4. 11/23 (月・祝) の再現")
    code, sent, _ = run_clock("3 19 * * 0-4", "2026-11-23T04:05", CAL_HOL)
    check(sent == ["auto_post_x.yml mode=predict"], f"04:05 の predict tick → predict ({sent})")
    code, sent, _ = run_clock("3 19 * * 0-4", "2026-11-23T08:55", CAL_HOL)
    check(sent == ["race_day_runner.yml"], f"同じ tick が 08:55 に遅れて来る → predict は出さず runner ({sent})")
    code, sent, _ = run_clock("1 0 * * 1-5", "2026-11-23T09:01", CAL_HOL)
    check(sent == ["race_day_runner.yml", "auto_post_x.yml mode=odds_flash"], f"09:01 → runner + odds_flash ({sent})")
    code, sent, _ = run_clock("1 0 * * 1-5", "2026-11-23T09:01", CAL_HOL, slots_dir({"20261123": ["odds_flash"]}),
                              fake={"active": {"race_day_runner.yml": 1}})
    check(sent == [], f"odds_flash の lock 済み + runner 稼働中 → 何も出さない ({sent})")
    code, sent, _ = run_clock("1 0 * * 1-5", "2026-11-23T09:16", CAL_HOL, fake={"active": {"race_day_runner.yml": 1}})
    check(sent == [], f"09:16 (代打の締切 9:15 の後) → odds_flash を出さない ({sent})")
    code, sent, _ = run_clock("51 0 * * 1-5", "2026-11-23T09:51", CAL_HOL, fake={"active": {"race_day_runner.yml": 1}})
    check(sent == ["auto_post_x.yml mode=post_predict"], f"09:51 → post_predict ({sent})")
    code, sent, _ = run_clock("51 0 * * 1-5", "2026-11-23T11:56", CAL_HOL, fake={"active": {"race_day_runner.yml": 1}})
    check(sent == [], f"11:56 (12時ガードの手前の締切 11:55 の後) → 出さない ({sent})")
    code, sent, _ = run_clock("33 8 * * 1-5", "2026-11-24T00:20", CAL_HOL, slots_dir({"20261123": ["post_predict"]}))
    check(sent == ["auto_post_x.yml mode=results date_override=20261123"],
          f"17:33 の tick が 11/24 00:20 に遅れて来る → results (対象日 11/23) ({sent})")
    code, sent, _ = run_clock("1 0 * * 1-5", "2026-11-24T01:00", CAL_HOL)
    check(sent == [], f"09:01 の tick が翌日に遅れて来る → 前日分なので何もしない ({sent})")
    code, sent, _ = run_clock("7 11 * * 1-5", "2026-11-23T20:07", CAL_HOL, fake={"since": {"collect_results.yml": 300}})
    check(sent == ["collect_results.yml date=20261123"], f"20:07 → 夜間スイープ (date=11/23) ({sent})")

    print("\n   夜間スイープの2本目・results の再試行 (#179 レビュー m2/m3)")
    code, sent, _ = run_clock("47 13 * * 1-5", "2026-11-23T22:47", CAL_HOL, fake={"since": {"collect_results.yml": 160}})
    check(sent == ["collect_results.yml date=20261123"], f"22:47 (1本目 20:07 の160分後) → 2本目を出す ({sent})")
    code, sent, _ = run_clock("47 13 * * 1-5", "2026-11-23T22:47", CAL_HOL, fake={"since": {"collect_results.yml": 130}})
    check(sent == ["collect_results.yml date=20261123"], f"1本目が30分遅れて 20:37 に走った夜でも 22:47 → 出す ({sent})")
    code, sent, _ = run_clock("7 11 * * 1-5", "2026-11-23T20:07", CAL_HOL, fake={"since": {"collect_results.yml": 7}})
    check(sent == [], f"月祝: collect_results 自身の月曜 cron (20:00) の7分後の 20:07 → 見送り ({sent})")
    code, sent, _ = run_clock("47 13 * * 1-5", "2026-11-23T22:47", CAL_HOL, fake={"since": {"collect_results.yml": 167}})
    check(sent == ["collect_results.yml date=20261123"], f"月祝: 20:00 の月曜 cron の後の 22:47 → 2本目を出す ({sent})")
    code, sent, _ = run_clock("33 10 * * 1-5", "2026-11-23T19:33", CAL_HOL, slots_dir({"20261123": ["post_predict"]}))
    check(sent == ["auto_post_x.yml mode=results date_override=20261123"],
          f"19:33 (results の lock 無し = 17:33 の分が待機中に取り消された) → results を出し直す ({sent})")
    code, sent, _ = run_clock("33 12 * * 1-5", "2026-11-23T21:33", CAL_HOL,
                              slots_dir({"20261123": ["post_predict", "results"]}))
    check(sent == [], f"21:33 で results が lock 済み → 出さない ({sent})")
    code, sent, _ = run_clock("33 12 * * 1-5", "2026-11-24T02:10", CAL_HOL, slots_dir({"20261123": ["post_predict"]}))
    check(sent == ["auto_post_x.yml mode=results date_override=20261123"],
          f"21:33 の tick が翌 02:10 に遅れて来る → results (対象日 11/23) ({sent})")
    code, sent, _ = run_clock("33 10 * * 1-5", "2026-11-23T19:33", CAL_HOL, slots_dir({"20261123": ["post_predict"]}),
                              fake={"pending": {"auto_post_x.yml": 1}})
    check(sent == [], f"auto_post_x に待機1本 → results を重ねない ({sent})")

    print("\n   前夜・順延")
    code, sent, _ = run_clock("41 6 * * 0-4", "2026-11-22T15:41", CAL_EVE)
    check(sent == ["weekend_prefetch.yml dates=20261123"], f"11/22(日) 15:41 で 11/23 が載っている → 前夜予想 ({sent})")
    cal_plain = make_calendar(["2026-11-28", "2026-11-29"], "2026-11-29T07:00:00+09:00", name="cal_plain.json")
    code, sent, _ = run_clock("41 6 * * 0-4", "2026-11-29T15:41", cal_plain)
    check(sent == [], f"普通の日曜 (翌月曜は非開催) → 何もしない ({sent})")
    cal_post = make_calendar(["2026-09-19", "2026-09-20", "2026-09-22"], "2026-09-21T07:00:00+09:00",
                             window=("2026-09-07", "2026-10-05"), name="cal_post.json")
    code, sent, _ = run_clock("41 10 * * 0-4", "2026-09-21T19:41", cal_post)
    check(sent == ["weekend_prefetch.yml dates=20260922"], f"順延 (月は非開催・火だけ載っている) → 月曜夜に火曜分 ({sent})")
    code, sent, _ = run_clock("3 19 * * 0-4", "2026-09-21T04:05", cal_post)
    check(sent == [], f"順延の月曜 (非開催) の朝 → 何もしない ({sent})")

    print("\n   縮退 (カレンダーを使えない時は土日だけを開催日とみなす)")
    cases = [
        ("カレンダー無し", os.path.join(TMP, "nothing.json")),
        ("JSON 壊れ", _broken()),
        ("載っているが8日前", make_calendar([HOLIDAY], "2026-11-15T04:00:00+09:00", name="c8.json")),
        ("載っていないが40時間前", make_calendar(["2026-11-22"], "2026-11-21T12:00:00+09:00", name="c40.json")),
    ]
    for label, cal in cases:
        code, sent, out = run_clock("3 19 * * 0-4", "2026-11-23T04:05", cal)
        check(code == 0 and not sent and "縮退" in out, f"{label} → dispatch 無し + 縮退の警告")
    cal2 = make_calendar([HOLIDAY], "2026-11-21T04:00:00+09:00", name="c2.json")
    code, sent, _ = run_clock("3 19 * * 0-4", "2026-11-23T04:05", cal2)
    check(sent == ["auto_post_x.yml mode=predict"], "載っていて2日前 → 出す")

    print("\n   待機・重複")
    code, sent, _ = run_clock("1 0 * * 1-5", "2026-11-23T09:01", CAL_HOL,
                              fake={"pending": {"auto_post_x.yml": 1}, "active": {"race_day_runner.yml": 1}})
    check(sent == [], f"auto_post_x に待機1本 → slot を出さない ({sent})")
    code, sent, _ = run_clock("3 22 * * 0-4", "2026-11-23T07:03", CAL_HOL, fake={"active": {"auto_post_x.yml": 1}})
    check(sent == [], f"predict は auto_post_x の実行中1本でも出さない ({sent})")
    code, sent, _ = run_clock("3 22 * * 0-4", "2026-11-23T07:03", CAL_HOL, fake={"active": {"auto_post_x.yml": -1}})
    check(sent == [], f"gh の取得失敗 (-1) → 出さない ({sent})")
    code, sent, _ = run_clock("33 23 * * 0-4", "2026-11-23T08:33", CAL_HOL, fake={"active": {"race_day_runner.yml": 1}})
    check(sent == [], f"runner 稼働中 → runner を出さない ({sent})")
    code, sent, _ = run_clock("33 23 * * 0-4", "2026-11-23T08:33", CAL_HOL, fake={"active": {"race_day_runner.yml": -1}})
    check(sent == [], f"runner の稼働を確認できない (-1) → 出さない ({sent})")
    code, sent, _ = run_clock("47 13 * * 1-5", "2026-11-23T22:47", CAL_HOL, fake={"since": {"collect_results.yml": 60}})
    check(sent == [], f"collect_results が60分前 → スイープを出さない ({sent})")
    code, sent, out = run_clock(None, "2026-11-21T07:00", CAL_HOL, intent="predict", date="20261121")
    check(sent == [] and "土日" in out, "手動で対象日が土曜 → 拒否 (土日はネイティブ)")
    code, sent, _ = run_clock(None, "2026-11-23T07:00", CAL_HOL, intent="predict")
    check(sent == ["auto_post_x.yml mode=predict"], f"手動 intent=predict (dry-run) → 判定どおり ({sent})")
    code, sent, _ = run_clock("1 0 * * 1-5", "2026-11-23T09:01", CAL_HOL,
                              fake={"dispatch_fail": ["auto_post_x.yml"]})
    check(code == 1, "dispatch 失敗 (422 など) → 終了コード 1 (run を赤にする)")


def _broken():
    p = os.path.join(TMP, "broken.json")
    with open(p, "w") as f:
        f.write("{not json")
    return p


def test_calendar_race_day():
    print("\n5. race_utils.calendar_race_day")
    now = jst("2026-11-23T08:00")
    f = race_utils.calendar_race_day
    check(f(HOLIDAY, now, CAL_HOL)[0] is True, "載っている & 新しい → True")
    check(f("20261123", now, CAL_HOL)[0] is True, "YYYYMMDD も受ける")
    check(f("2026-11-24", now, CAL_HOL)[0] is False, "載っていない & 36h以内 & 窓内 → False")
    check(f("2026-12-20", now, CAL_HOL)[0] is None, "窓の外 → None")
    check(f(HOLIDAY, jst("2026-12-01T08:00"), CAL_HOL)[0] is None, "載っているが7日超 → None")
    check(f("2026-11-24", jst("2026-11-25T08:00"), CAL_HOL)[0] is None, "載っていないが36h超 → None")
    check(f(HOLIDAY, now, os.path.join(TMP, "nothing.json"))[0] is None, "ファイル無し → None")
    check(f(HOLIDAY, now, _broken())[0] is None, "壊れている → None")
    p = os.path.join(TMP, "nogen.json")
    with open(p, "w") as fh:
        json.dump({"dates": {HOLIDAY: {"races": 6}}}, fh)
    check(f(HOLIDAY, now, p)[0] is None, "生成時刻が読めない → None")
    check(f("2026-13-01", now, CAL_HOL)[0] is None, "不正な日付でも例外にせず None")
    check(f("abc", now, CAL_HOL)[0] is None, "読めない日付 → None")
    os.environ["RACE_CALENDAR_PATH"] = CAL_HOL
    try:
        check(f(HOLIDAY, now)[0] is True, "env RACE_CALENDAR_PATH を読む")
    finally:
        del os.environ["RACE_CALENDAR_PATH"]


# 移設前の weekend_prefetch.yml の heredoc (origin/main 44a9df1 の 121-152行) をそのまま写したもの
ORIGINAL_HEREDOC = '''
import contextlib, datetime, sys
from race_utils import prediction_locked_for, PREDICTION_LOCK_LABEL
# predict の import はモデル周りまで読む (数秒)。import 時の出力が eval に混ざらないよう stderr へ
with contextlib.redirect_stdout(sys.stderr):
    from predict import FINAL_REBUILD_JST
fr = tuple(FINAL_REBUILD_JST)
now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
d = now.date()
today = d.strftime("%Y%m%d")
cands = [c for c in (d + datetime.timedelta(days=i) for i in range(3))
         if c.weekday() in (5, 6)][:2]
dates, locked, final = [], [], []
for c in cands:
    ds = c.strftime("%Y%m%d")
    if prediction_locked_for(ds, now):
        locked.append(ds)
    elif ds == today and (now.hour, now.minute) >= fr:
        final.append(ds)
    else:
        dates.append(ds)
print('DATES="%s"' % " ".join(dates))
print('LOCKED="%s"' % " ".join(locked))
print('FINAL_SKIP="%s"' % " ".join(final))
print('LOCK_LABEL="%s"' % PREDICTION_LOCK_LABEL)
print('FINAL_LABEL="%d:%02d"' % fr)
print('FINAL_HM="%02d%02d"' % fr)
print('TODAY="%s"' % today)
print('BEFORE21="%d"' % (1 if now.hour < 21 else 0))
# 今日が開催日 (土日) で、今日の分をもう作らない回 = 土曜の夕方〜夜の回 (日曜分だけを作る)
print('TODAY_EXCLUDED="%d"' % (1 if today in locked + final else 0))
'''


def _run_original(now):
    """移設前の heredoc を偽の now・偽の predict で実行する。"""
    class FakeDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    fake_dt_mod = types.ModuleType("datetime")
    for k in dir(dt):
        setattr(fake_dt_mod, k, getattr(dt, k))
    fake_dt_mod.datetime = FakeDT
    fake_pred = types.ModuleType("predict")
    fake_pred.FINAL_REBUILD_JST = (9, 0)
    saved = {k: sys.modules.get(k) for k in ("datetime", "predict")}
    sys.modules["datetime"] = fake_dt_mod
    sys.modules["predict"] = fake_pred
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(ORIGINAL_HEREDOC, "heredoc", "exec"), {"__name__": "__heredoc__"})
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return buf.getvalue()


def _run_new(now, dates=None):
    import prefetch_targets as pt
    buf = io.StringIO()
    argv = ["--now", now.isoformat(), "--final-hm", "0900"]
    if dates is not None:
        argv += ["--dates", dates]
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        pt.main(argv)
    return buf.getvalue(), err.getvalue()


def test_prefetch_targets():
    print("\n6. scripts/prefetch_targets.py (golden: 移設前の heredoc と同じ出力)")
    moments = ["2026-11-20T17:30", "2026-11-20T22:30", "2026-11-21T03:30", "2026-11-21T08:59",
               "2026-11-21T09:00", "2026-11-21T09:31", "2026-11-21T15:41", "2026-11-21T23:41",
               "2026-11-22T03:30", "2026-11-22T09:05", "2026-11-23T03:37", "2026-11-19T22:30"]
    for m in moments:
        now = jst(m)
        old = _run_original(now)
        new, _err = _run_new(now)
        extra = 'REQUESTED="0"\n'
        check(new == old + extra, f"{m}: 出力が一致 ({old.splitlines()[0]})")
    new, _ = _run_new(jst("2026-11-22T15:41"), "20261123")
    check('DATES="20261123"' in new and 'REQUESTED="1"' in new, "日 15:41 + dates=20261123 → DATES=20261123")
    new, _ = _run_new(jst("2026-11-23T03:37"), "20261123")
    check('DATES="20261123"' in new, "月 03:37 + dates=20261123 → DATES=20261123")
    new, _ = _run_new(jst("2026-11-23T09:05"), "20261123")
    check('FINAL_SKIP="20261123"' in new and 'DATES=""' in new, "月 09:05 → FINAL_SKIP=20261123")
    new, err = _run_new(jst("2026-11-22T15:41"), "2026112, 20261130 abc 20261123")
    check('DATES="20261123"' in new and err.count("捨てる") == 3, "不正値・範囲外は捨てて警告 (3件)")
    new, _ = _run_new(jst("2026-11-22T15:41"), "")
    check('REQUESTED="0"' in new and 'DATES=""' in new and 'LOCKED="20261122"' in new,
          "dates が空 → 従来どおり (日 15:41 は日曜が確定済みで対象なし。月曜は見ない)")


def test_gate_hold():
    print("\n7. build_race_baseline._weekend_gate_hold (土日以外の開催日)")
    import build_race_baseline as brb
    brb._final_rebuild_jst = lambda: (9, 0)
    cal = make_calendar(["2026-11-21", "2026-11-22", HOLIDAY, "2026-09-22"], "2026-11-23T03:00:00+09:00",
                        window=("2026-09-01", "2026-12-07"), name="gate.json")
    os.environ["RACE_CALENDAR_PATH"] = cal
    try:
        h, why = brb._weekend_gate_hold(jst("2026-11-23T03:37"))
        check(h is True, f"月祝 (前日=日) 03:37 → 据え置き ({why})")
        cal_p = make_calendar(["2026-09-19", "2026-09-20", "2026-09-22"], "2026-09-22T03:00:00+09:00",
                              window=("2026-09-07", "2026-10-05"), name="gate_p.json")
        os.environ["RACE_CALENDAR_PATH"] = cal_p
        h, why = brb._weekend_gate_hold(jst("2026-09-22T03:37"))
        check(h is False, f"火 (載っている・月=非開催) 03:37 → 引き直す ({why})")
        h, why = brb._weekend_gate_hold(jst("2026-09-22T09:05"))
        check(h is True, f"火 (単独の開催日) 09:05 → 据え置き ({why})")
        os.environ["RACE_CALENDAR_PATH"] = cal
        h, why = brb._weekend_gate_hold(jst("2026-11-27T22:30"))
        check(h is False, f"金 (載っていない) 22:30 → 引き直す (従来どおり) ({why})")
        h, _ = brb._weekend_gate_hold(jst("2026-11-21T08:59"))
        h2, _ = brb._weekend_gate_hold(jst("2026-11-21T09:00"))
        h3, _ = brb._weekend_gate_hold(jst("2026-11-22T03:30"))
        check((h, h2, h3) == (False, True, True), "土曜 8:59 / 9:00・日曜は従来どおり")
    finally:
        del os.environ["RACE_CALENDAR_PATH"]


def test_gate_table():
    print("\n8. 指定時刻のゲート表 (土日はネイティブ cron、土日以外の開催日だけ時計が dispatch)")
    native = {name: crons(name) for name in ("auto_post_x.yml", "race_day_runner.yml", "weekend_prefetch.yml")}
    # 普通の月曜 (11/30) と金曜 (11/27) を含むカレンダー: 11/23 だけが平日開催
    cal = make_calendar(["2026-11-21", "2026-11-22", HOLIDAY, "2026-11-28", "2026-11-29"],
                        "2026-11-23T03:00:00+09:00", name="table.json")
    cal_late = make_calendar(["2026-11-21", "2026-11-22", HOLIDAY, "2026-11-28", "2026-11-29"],
                             "2026-11-30T03:00:00+09:00", window=("2026-11-16", "2026-12-14"), name="table2.json")
    cal_fri = make_calendar(["2026-11-28", "2026-11-29"], "2026-11-27T05:00:00+09:00",
                            window=("2026-11-13", "2026-12-11"), name="table3.json")
    rows = [
        # (表示, ネイティブ cron を確かめる時刻, 時計の tick の実行時刻, 時計の cron, カレンダー, 期待する dispatch)
        # 時計の cron は「その時刻に起きる tick」か「遅れてその時刻に来る tick」。None = 時計は起きない
        ("土 07:00", "2026-11-21T07:00", "2026-11-21T07:00", None, cal, None),
        ("日 07:00", "2026-11-22T07:00", "2026-11-22T07:00", None, cal, None),
        ("月祝 04:00", "2026-11-23T04:00", "2026-11-23T04:03", "3 19 * * 0-4", cal, ["auto_post_x.yml mode=predict"]),
        ("月祝 07:00", "2026-11-23T07:00", "2026-11-23T07:03", "3 22 * * 0-4", cal, ["auto_post_x.yml mode=predict"]),
        ("月祝 09:00", "2026-11-23T09:00", "2026-11-23T09:01", "1 0 * * 1-5", cal,
         ["race_day_runner.yml", "auto_post_x.yml mode=odds_flash"]),
        ("月祝 09:30", "2026-11-23T09:30", "2026-11-23T09:31", "31 0 * * 1-5", cal,
         ["race_day_runner.yml", "auto_post_x.yml mode=post_predict"]),
        ("月祝 17:30", "2026-11-23T17:30", "2026-11-23T17:33", "33 8 * * 1-5", cal,
         ["race_day_runner.yml", "auto_post_x.yml mode=results date_override=20261123"]),
        ("普通の月 07:00", "2026-11-30T07:00", "2026-11-30T07:03", "3 22 * * 0-4", cal_late, []),
        # 日曜の夜は 19:41 の tick が遅れて 22:30 に来た場合 (時計の前夜 tick は 15:41/19:41/23:41)
        ("月祝前の日 22:30", "2026-11-22T22:30", "2026-11-22T22:30", "41 10 * * 0-4", cal,
         ["weekend_prefetch.yml dates=20261123"]),
        ("金 22:30", "2026-11-27T22:30", "2026-11-27T22:30", None, cal_fri, None),
    ]
    print(f"     {'時刻':<14}{'ネイティブ cron (土日の層)':<40}{'時計の dispatch'}")
    for label, nat_when, when, clock_cron, calp, expect in rows:
        nat_at = jst(nat_when)
        fired = [f"{n.split('.')[0]}:{c}" for n, cs in native.items() for c in cs if cron_fires(c, nat_at)]
        clock_fired = [c for c in rc.SCHEDULE if cron_fires(c, nat_at)]
        if clock_cron is None:
            check(not clock_fired, f"{label}: 時計の tick は起きない (起きたもの {clock_fired})")
            shown = "(時計は動かない)"
        else:
            # 17:30 の results は予想 (post_predict) を配信できた日だけ出す (#179 T-landing-4) → 配信済みの日として流す
            pp = ({when[:10].replace("-", ""): ["post_predict"]}
                  if any("mode=results" in x for x in (expect or [])) else None)
            code, sent, _out = run_clock(clock_cron, when, calp, slots_dir(pp),
                                         fake={"since": {"collect_results.yml": 999}})
            check(sent == expect, f"{label}: 時計 ({clock_cron} を {when[-5:]} に実行) → {sent or 'なし'}")
            shown = sent or "なし (開催日でない)"
        print(f"     {label:<14}{(', '.join(fired) or '—'):<40}{shown}")
    # 土日はネイティブが動く・月祝はネイティブが動かない、を表とは別に確認
    sat7 = [c for c in native["auto_post_x.yml"] if cron_fires(c, jst("2026-11-21T07:00"))]
    mon7 = [c for cs in native.values() for c in cs if cron_fires(c, jst("2026-11-23T07:00"))]
    fri2230 = [c for c in native["weekend_prefetch.yml"] if cron_fires(c, jst("2026-11-27T22:30"))]
    check(sat7 == ["0 22 * * 5,6"], f"土 07:00 は auto_post_x の predict cron が動く (不変) {sat7}")
    check(mon7 == [], f"月 07:00 はネイティブの土日 cron が動かない → 時計が必要 {mon7}")
    check(fri2230 == ["30 13 * * 5"], f"金 22:30 の weekend_prefetch cron は不変 {fri2230}")


def test_uniform_delay():
    print("\n9. 一律の遅れ (#179 レビュー M1: 11/22-23 に実測した 4〜8時間) — 遅れて着いた tick でも予測を作れるか")
    # 11/22(日) 12:00 〜 11/23(月・祝) 12:00 に予定された tick を、全部同じだけ遅らせて流す
    t, end, planned = jst("2026-11-22T12:00"), jst("2026-11-23T12:00"), []
    while t < end:
        planned += [(t, c) for c in rc.SCHEDULE if cron_fires(c, t)]
        t += dt.timedelta(minutes=1)
    deadline = jst("2026-11-23T08:48")        # predict の締切 (9:00 の最後の作り直し - 12分)
    lock = jst("2026-11-23T09:30")
    rows = {}
    for delay in (0, 120, 240, 300, 360, 420, 480, 510, 520, 540, 600):
        first_pred = first_runner = None
        for p, c in planned:
            arrive = p + dt.timedelta(minutes=delay)
            _code, sent, _out = run_clock(c, arrive.strftime("%Y-%m-%dT%H:%M"), CAL_EVE,
                                          fake={"since": {"collect_results.yml": 999}})
            if any("mode=predict" in x for x in sent) and first_pred is None:
                first_pred = arrive
            if "race_day_runner.yml" in sent and first_runner is None:
                first_runner = arrive
        rows[delay] = (first_pred, first_runner)
        fp = f"{first_pred:%m/%d %H:%M}" if first_pred else "なし"
        fr = f"{first_runner:%H:%M}" if first_runner else "なし"
        print(f"     遅れ {delay // 60}h{delay % 60:02d}m: 最初の predict {fp} / 最初の runner 起動 {fr}")
    ok = all(rows[d][0] is not None and rows[d][0] < deadline for d in (0, 120, 240, 300, 360, 420, 480, 510, 520))
    check(ok, "一律 0〜8h40m の遅れ: 8:48 の締切前に predict を出す (修正前は 4h45m 以上の遅れで0本、runner の起動も 8h で 11:37)")
    check(rows[540][0] is None or rows[540][0] >= deadline,
          "9h の遅れには勝てない (同じスケジューラ上の時計の限界。独立した時計は GAS だけ #155) — 限界として記録")
    check(all(rows[d][1] is not None and rows[d][1] < jst("2026-11-23T12:00") for d in (0, 120, 240, 360, 480)),
          "0〜8h の遅れ: runner は正午前に起動する (9:30 を過ぎる遅れでは post_predict を runner が代打する)")
    check(rows[0][1] is not None and rows[0][1] < lock, "定刻なら runner は 9:30 の確定前に起動する")

    # #179 T-clock-3: odds_flash (9:00 の最後の作り直しを兼ねる) の窓は 9:00〜9:15。時計の 09:01 tick を
    # 外した日は runner の代打が頼りで、runner の判定は起動から ~8〜14分後 (起動29秒 + 1周目 436〜763秒、
    # 本番ログ実測)。→ 「8:30〜9:01 に着く tick」(runner を起こす) か時計自身の odds_flash が、どの遅れでも要る。
    of_last_start = jst("2026-11-23T09:01")
    # (a) 構造: 対象日 T の tick の予定時刻の間隔が 19:41 (前夜) 〜 08:53 ですべて30分以下 (8:30〜9:01 は31分の窓)
    hol = jst(HOLIDAY).date()
    ts = sorted(p for p, c in planned if p.date() + dt.timedelta(days=rc.SCHEDULE[c][1]) == hol)
    ts = [p for p in ts if jst("2026-11-22T19:41") <= p <= jst("2026-11-23T08:53")]
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > dt.timedelta(minutes=30)]
    check(not gaps and ts and ts[0] == jst("2026-11-22T19:41"),
          "19:41 (前夜) 〜08:53 の対象日 T の tick の間隔がすべて30分以下 (一律 0〜13h19m の遅れで runner が 9:01 までに起きる)"
          + (f" — 空き: {[f'{a:%H:%M}→{b:%H:%M}' for a, b in gaps]}" if gaps else ""))
    # (b) 実走: 旧表で odds_flash がどこからも出なかった遅れ (34/150/240/330/360 分 等)、round 3 の表で出なかった
    #     9h20m 以上 (570/600 分 等)、round 3 の問い合わせの道で出なかった遅れ (65/105/197/290/405/465/525 分 等) を、
    #     カレンダーに載っている日と、カレンダーが否定して netkeiba の一覧で見つける日 (急な順延) の両方で流す。
    #     13h20m 以上は 19:41 の tick も 9:01 を過ぎる = 同じスケジューラ上の時計の限界 (上の 9h と同じ扱い)
    cal_neg = make_calendar(["2026-11-21", "2026-11-22"], "2026-11-22T07:00:00+09:00", name="cal_eve_neg.json")
    rl_hol = "".join(f'<a href="../race/shutuba.html?race_id=20260505{r:02d}{r:02d}&rf=race_list">' for r in range(1, 13))

    def odds_flash_reachable(delay, cal, extra):
        runner = of = None
        for p, c in planned:
            arrive = p + dt.timedelta(minutes=delay)
            _code, sent, _out = run_clock(c, arrive.strftime("%Y-%m-%dT%H:%M"), cal,
                                          fake={"since": {"collect_results.yml": 999},
                                                "active": {"race_day_runner.yml": 1 if runner else 0}, **extra})
            if "race_day_runner.yml" in sent and runner is None:
                runner = arrive
            if any("mode=odds_flash" in x for x in sent) and of is None:
                of = arrive
        return (of is not None or (runner is not None and runner <= of_last_start)), runner
    delays = (0, 34, 65, 105, 110, 150, 197, 205, 240, 290, 330, 360, 405, 465, 480, 525, 540, 559, 570, 600,
              660, 720, 780, 799)
    for label, cal, extra in (("カレンダーに載っている日", CAL_EVE, {}),
                              ("問い合わせで見つける日 (カレンダーは否定)", cal_neg, {"race_list": {"20261123": rl_hol}})):
        bad = []
        for delay in delays:
            ok, runner = odds_flash_reachable(delay, cal, extra)
            if not ok:
                bad.append(f"{delay}分 (runner {runner:%H:%M})" if runner else f"{delay}分 (runner なし)")
        check(not bad, f"{label}: 一律 0〜13h19m の遅れでも odds_flash を時計が出すか、runner を 9:01 までに起こす"
              + (f" — 外れ: {bad}" if bad else ""))
    ok, runner = odds_flash_reachable(801, CAL_EVE, {})
    check(not ok, f"13h21m の遅れには勝てない (runner {runner:%H:%M} — 同じスケジューラ上の時計の限界)。限界として記録"
          if runner else "13h21m の遅れには勝てない — 限界として記録")


# ── 10. (Part D) build_race_calendar --probe ───────────────────────────
def cal_page(links=(), unpublished=(), weather=None):
    """netkeiba の月カレンダーと同じ形の HTML (2026-11-23 に実ページの11月・12月・1月を読んで写した形)。
    links: レース一覧が公開済みの日 (セルに race_list.html のリンクと HaveData) / unpublished: 開催予定だが未公開の日
    (セルに場名だけ、リンクも HaveData も無い) / weather: 開催日の開催時間帯にだけ全ページに出る天候パネルの日付。"""
    cells = []
    for d in links:
        cells.append(f'<td class="RaceCellBox">\n<a href="../top/race_list.html?kaisai_date={d}"  tile="" '
                     f'target="_parent">\n<div class="RaceKaisaiBox HaveData">\n<p><span class="Day">{int(d[-2:])}'
                     f'</span></p>\n<p><span class="JyoName">東京</span></p>\n</div>\n</a>\n</td>')
    for d in unpublished:
        cells.append(f'<td class="RaceCellBox">\n<div class="RaceKaisaiBox">\n<p><span class="Day">{int(d[-2:])}'
                     f'</span></p>\n<p><span class="JyoName">中山</span></p>\n</div>\n</td>')
    panel = ("" if not weather else
             f'<div class="RealtimeWeatherContent Active"><p class="RealtimeWeatherCourse">東京競馬場</p>'
             f'<a class="RealtimeWeatherSummary" href="/race/weather_summary.html?kaisai_date={weather}&amp;jyo_cd=05">'
             f'天候</a></div>')
    return (f'<div class="Race_Calendar_Main"><table class="Calendar_Table"><tbody><tr class="Week">'
            f'{"".join(cells)}</tr></tbody></table></div>{panel}')


CAL_HTML = {
    (2026, 11): cal_page(("20261119", "20261121", "20261122", "20261123", "20261124", "20261128", "20261129")),
    (2026, 12): cal_page(("20261201", "20261205", "20261206")),
}
JRA_1123 = ([f"20260505{r:02d}{r:02d}" for r in range(1, 13)]          # 東京 (05) 12R
            + [f"20260302{r:02d}{r:02d}" for r in range(1, 13)]        # 福島 (03) 12R
            + ["202665112301"])                                        # 場コード 65 (JRA 以外) は数えない
LISTS = {"20261123": JRA_1123, "20261124": ["202644112401", "202644112402"]}   # 11/24 は JRA 以外だけ


class FakeScraper:
    def __init__(self, fail_month=None, raise_list=False, html=None, lists=None, fail_list=()):
        self.fail_month, self.raise_list, self.calls = fail_month, raise_list, []
        self.html = {**CAL_HTML, **(html or {})}
        self.lists = {**LISTS, **(lists or {})}
        self.fail_list = set(fail_list)

    def _get(self, url, encoding=None):
        m = re.search(r"year=(\d+)&month=(\d+)", url)
        ym = (int(m.group(1)), int(m.group(2)))
        self.calls.append(("cal", ym))
        if ym == self.fail_month or ym in (self.fail_month or ()):
            return None
        return types.SimpleNamespace(text=self.html.get(ym, cal_page()))

    def get_race_list_by_date(self, d8, strict=False):
        self.calls.append(("list", d8))
        if self.raise_list:
            raise RuntimeError("接続が切れた")
        if d8 in self.fail_list:              # HTTP エラー・タイムアウト (scraper は strict=True で None)
            return None if strict else []
        return self.lists.get(d8, [])


def _probe_db(extra=()):
    import sqlite3
    p = os.path.join(TMP, "probe.db")
    if os.path.exists(p):
        os.remove(p)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE races (race_id TEXT PRIMARY KEY, race_date TEXT, venue TEXT)")
    rows = [(d, code, venue, r) for d, code, venue in (("2026-11-21", "08", "京都"), ("2026-11-22", "05", "東京"),
                                                       ("2026-12-01", "06", "中山")) for r in range(1, 13)]
    rows += list(extra)
    for d, code, venue, r in rows:
        c.execute("INSERT INTO races VALUES (?, ?, ?)", (f"2026{code}05{d[-2:]}{r:02d}", d, venue))
    c.commit()
    c.close()
    return p


def _run_probe(fake, args=("--probe",), deadline=None, prev=None, today="2026-11-20", db_extra=()):
    """build_race_calendar.main を偽の scraper で。prev: 前回の版 (dates) を書いておく (引き継ぎの検査)。"""
    import build_race_calendar as brc
    out = os.path.join(TMP, "cal_probe.json")
    if os.path.exists(out):
        os.remove(out)
    if prev is not None:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"generated_at": "2026-11-19T05:00:00+09:00", "dates": prev}, fh, ensure_ascii=False)
    bak = (brc.make_scraper, brc.PROBE_DEADLINE_S)
    brc.make_scraper = (lambda: fake) if fake is not None else (lambda: (_ for _ in ()).throw(AssertionError("呼ばれた")))
    if deadline is not None:
        brc.PROBE_DEADLINE_S = deadline
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = brc.main(list(args) + ["--db", _probe_db(db_extra), "--out", out, "--today", today])
    finally:
        brc.make_scraper, brc.PROBE_DEADLINE_S = bak
    with open(out, encoding="utf-8") as f:
        return code, json.load(f), buf.getvalue()


NK_1123 = {"races": 24, "venues": ["東京", "福島"], "source": "netkeiba"}


def test_probe():
    print("\n10. (Part D) build_race_calendar --probe (偽の scraper・実ページと同じ形の HTML、ネットワークに出ない)")
    db_only = {"2026-11-21", "2026-11-22", "2026-12-01"}
    fake = FakeScraper()
    code, cal, out = _run_probe(fake)
    d = cal["dates"]
    check(code == 0 and d.get("2026-11-23") == NK_1123,
          f"平日 (11/23 月) の開催日を netkeiba から足す: JRA の 24R だけ数え、source=netkeiba ({d.get('2026-11-23')})")
    check("2026-11-24" not in d, "netkeiba のカレンダーにあっても JRA のレースが無い平日 (11/24) は足さない")
    check(not any(k in d for k in ("2026-11-28", "2026-11-29", "2026-12-05", "2026-12-06")),
          "DB に無い土日 (11/28・11/29・12/05・12/06) は足さない")
    asked = sorted(x[1] for x in fake.calls if x[0] == "list")
    check(asked == ["20261123", "20261124", "20261201"],
          f"レース一覧を問い合わせるのは窓内の平日だけ (DB にある平日 12/01 は補うために問い合わせる。土日・窓外 11/19 は"
          f"問い合わせない: {asked})")
    check(d.get("2026-12-01") == {"races": 12, "venues": ["中山"]} and d.get("2026-11-21") == {"races": 12, "venues": ["京都"]},
          "DB にある日 (平日 12/01 は一覧が空・土曜 11/21) は DB 由来のまま (減らさない・source を付けない)")
    check(sorted(x[1] for x in fake.calls if x[0] == "cal") == [(2026, 11), (2026, 12)],
          "窓 [11/20, 12/04] にかかる2か月のカレンダーだけ読む")
    check(cal.get("probe", {}).get("status") == "ok" and cal["probe"]["added"] == ["2026-11-23"], "probe の記録 (ok・追加日)")

    print("\n   翌月のページ (#179 T-weekend-1 / T-partd-2) と天候パネル (T-partd-3)")
    dec_empty = cal_page(unpublished=("20261205", "20261206"))      # 実際の 11/17〜 の12月: 場名だけでリンク0本
    code, cal, out = _run_probe(FakeScraper(html={(2026, 12): dec_empty}))
    check(cal["dates"].get("2026-11-23") == NK_1123 and cal["probe"]["status"] == "ok",
          f"12月がまだ未公開 (リンク0本) でも当月の 11/23 を足す・status ok ({cal['probe']['status']})")
    code, cal, out = _run_probe(FakeScraper(html={(2026, 12): cal_page(unpublished=("20261205",), weather="20261125")}))
    check(cal["dates"].get("2026-11-23") == NK_1123 and "2026-11-25" not in cal["dates"],
          "12月のページの天候パネル (weather_summary.html?kaisai_date=20261125) は開催日の候補にしない")
    f = FakeScraper(fail_month=(2026, 12))
    code, cal, out = _run_probe(f)
    check(code == 0 and cal["dates"].get("2026-11-23") == NK_1123 and cal["probe"]["status"].startswith("partial")
          and "::warning::" in out,
          f"12月 (翌月) を取れない → 当月の発見は残し status=partial + 警告 ({cal['probe']['status'][:40]})")
    # 当月が空・パネルだけ: DB に今日以前の当月の開催日があるのにリンク0本 → 形式が変わった疑い
    code, cal, out = _run_probe(FakeScraper(html={(2026, 11): cal_page(weather="20261123")}), today="2026-11-23")
    check(code == 0 and cal["probe"]["status"].startswith("failed") and "形式が変わった" in cal["probe"]["status"]
          and "DB 由来の日付だけ" in out,
          f"今日を含む月 (11月) がパネルだけ (DB の 11/21 があるのにリンク0本) → failed・DB 由来だけ ({cal['probe']['status'][:60]})")
    code, cal, out = _run_probe(FakeScraper(html={(2026, 11): cal_page(unpublished=("20261123",)).replace(
        '<div class="RaceKaisaiBox">', '<div class="RaceKaisaiBox HaveData">')}))
    check(cal["probe"]["status"].startswith("failed") and "HaveData" in cal["probe"]["status"],
          "HaveData のセルがあるのに日付のリンクが0本 (リンクの形式が変わった) → failed")
    code, cal, out = _run_probe(FakeScraper(html={(2026, 11): cal_page()}), today="2026-11-02")
    check(cal["probe"]["status"] == "ok",
          f"月初で当月 (11月) にまだ過去の開催日もリンクも無い → 失敗にしない ({cal['probe']['status']})")

    print("\n   失敗の扱い")
    for label, f, kw in (("11月 (今日を含む月) のカレンダーを取れない", FakeScraper(fail_month=(2026, 11)), {}),
                         ("締切を過ぎた (カレンダーの前)", FakeScraper(), {"deadline": -1})):
        code, cal, out = _run_probe(f, **kw)
        check(code == 0 and set(cal["dates"]) == db_only and cal["probe"]["status"].startswith("failed")
              and "DB 由来の日付だけ" in out,
              f"{label} → DB 由来の日付だけで書く (exit 0・警告つき)")
    code, cal, out = _run_probe(FakeScraper(raise_list=True))
    check(code == 0 and set(cal["dates"]) == db_only and cal["probe"]["status"].startswith("partial")
          and "JRA のレースが無い" not in out and "::warning::" in out,
          "レース一覧の取得で例外 → その日は判定しない (足さない・「開催なし」と言わない・partial + 警告)")
    code, cal, out = _run_probe(FakeScraper(fail_list={"20261123"}))
    check("2026-11-23" not in cal["dates"] and cal["probe"]["status"].startswith("partial")
          and "20261123" not in re.findall(r"JRA のレースが無い.*", out) and "2026-11-23 (平日) は netkeiba のカレンダーにあるが" not in out,
          "一覧を取れない (HTTP 500 = strict で None) 11/23 → 「JRA のレースが無い」と確定しない (partial)")
    code, cal, out = _run_probe(None, args=())
    check(code == 0 and set(cal["dates"]) == db_only and "probe" not in cal,
          "--probe なし → 従来どおり DB 由来だけ (scraper を作らない・ネットワークに出ない)")

    print("\n   前回の版の引き継ぎ (#179 T-clock-1 / T-partd-1 / T-landing-3)")
    prev = {"2026-11-21": {"races": 12, "venues": ["京都"]}, "2026-11-23": dict(NK_1123),
            "2026-11-26": {"races": 12, "venues": ["阪神"], "source": "netkeiba"},       # 木曜 (今回のカレンダーに無い)
            "2026-11-25": {"races": 8, "venues": ["東京"]},                                # DB 由来だった古い行 → 生き返らせない
            "2026-11-28": {"races": 12, "venues": ["東京"], "source": "netkeiba"},       # 土曜 → 引き継がない
            "2026-12-25": {"races": 12, "venues": ["中山"], "source": "netkeiba"},       # 窓の外 → 引き継がない
            "2026-11-17": {"races": 12, "venues": ["福島"], "source": "netkeiba"}}       # 過去の平日 (窓内) → 残す
    for label, f, kw in (("カレンダーを取れない", FakeScraper(fail_month=(2026, 11)), {}),
                         ("締切", FakeScraper(), {"deadline": -1}),
                         ("--probe なし (|| の呼び直し)", None, {"args": ()})):
        code, cal, out = _run_probe(f, prev=prev, **kw)
        dd = cal["dates"]
        check(dd.get("2026-11-23") == NK_1123 and dd.get("2026-11-26", {}).get("source") == "netkeiba"
              and dd.get("2026-11-17") and "2026-11-25" not in dd and "2026-11-28" not in dd and "2026-12-25" not in dd,
              f"{label} → 前回 netkeiba で見つけた平日 (11/23・11/26・過去の 11/17) を引き継ぐ。DB 由来の古い行・土曜・窓外は"
              f"引き継がない ({sorted(dd)})")
    code, cal, out = _run_probe(FakeScraper(raise_list=True), prev=prev)
    check(cal["dates"].get("2026-11-23") == NK_1123 and "2026-11-26" not in cal["dates"],
          "一覧で例外 → カレンダーに載っている 11/23 は引き継ぐ (判定できない)。読めたカレンダーに無い 11/26 は外す")
    code, cal, out = _run_probe(FakeScraper(lists={"20261123": []}), prev=prev)
    dd = cal["dates"]
    check(dd.get("2026-11-23") == NK_1123 and "2026-11-23" in cal["probe"].get("kept", []),
          "probe は成功、11/23 はカレンダーに載っているが一覧が空 (通信失敗と区別できない) → 引き継ぐ")
    check("2026-11-26" not in dd and "2026-11-26" in cal["probe"].get("dropped", []) and "::warning::" in out,
          "読めた11月のカレンダーにもう載っていない 11/26 (中止・移動) → 警告して外す")
    check(dd.get("2026-11-17") is not None, "今日より前の平日 (確かめようがない) は窓を出るまで残す")
    code, cal, out = _run_probe(FakeScraper(fail_list={"20261123"}), prev=prev)
    check(cal["dates"].get("2026-11-23") == NK_1123, "一覧を取れない (HTTP 500) → 前回の 11/23 を引き継ぐ")
    code, cal, out = _run_probe(FakeScraper(fail_month=(2026, 12)),
                                prev={"2026-12-02": {"races": 12, "venues": ["中山"], "source": "netkeiba"}})
    check(cal["dates"].get("2026-12-02") is not None,
          "12月を確かめられない (取れない) → 12月の引き継ぎ日 (12/02) は外さない")
    code, cal, out = _run_probe(FakeScraper(html={(2026, 12): dec_empty}),
                                prev={"2026-12-02": {"races": 12, "venues": ["中山"], "source": "netkeiba"}})
    check(cal["dates"].get("2026-12-02") is not None,
          "12月のページにリンクが0本 (= 形式を確かめられない月) → 12月の引き継ぎ日は外さない")
    # 引き継いだ日で時計・セーフティネットが開催日とみなす
    code, cal, _ = _run_probe(FakeScraper(fail_month=(2026, 11)), prev=prev)
    p = os.path.join(TMP, "cal_carried.json")
    cal["generated_at"] = "2026-11-22T05:00:00+09:00"
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cal, fh, ensure_ascii=False)
    ok, why = race_utils.calendar_race_day(HOLIDAY, jst("2026-11-23T04:05"), p)
    check(ok is True, f"問い合わせに失敗した版でも、引き継いだ 11/23 を calendar_race_day は開催日とみなす ({why})")
    import raceday_safety_net as sn
    check(sn.expected_races(cal["dates"].get(HOLIDAY)) == 24, "セーフティネットの期待レース数も引き継いだ値 (24R)")

    print("\n   DB にある平日を補う (#179 T-partd-4)")
    one = [("2026-11-23", "05", "東京", 11)]          # 特別登録の1レースだけ (東京11R)
    code, cal, out = _run_probe(FakeScraper(), db_extra=one)
    d23 = cal["dates"].get("2026-11-23")
    check(d23 == {"races": 24, "venues": ["東京", "福島"], "source": "db+netkeiba"} and cal["probe"].get("merged") == ["2026-11-23"]
          and cal["probe"]["added"] == [],
          f"DB に 11/23 の1R (東京) だけ → 一覧で 24R・東京/福島 に補う (source=db+netkeiba) ({d23})")
    check(sn.expected_races(d23) == 24, f"セーフティネットの期待レース数 = 24 (補う前は 12) ({sn.expected_races(d23)})")
    code, cal, out = _run_probe(FakeScraper(fail_list={"20261123"}), db_extra=one)
    check(cal["dates"].get("2026-11-23") == {"races": 1, "venues": ["東京"]} and cal["probe"]["status"].startswith("partial"),
          "DB にある平日の一覧を取れない → DB 由来のまま (補いは打ち切るだけ)")
    many = [("2026-11-23", "05", "東京", r) for r in range(1, 13)] + [("2026-11-23", "03", "福島", r) for r in range(1, 13)]
    code, cal, out = _run_probe(FakeScraper(lists={"20261123": JRA_1123[:12]}), db_extra=many)
    check(cal["dates"].get("2026-11-23") == {"races": 24, "venues": ["東京", "福島"]},
          "DB の方が多い (一覧は 12R だけ) → DB 由来のまま (減らさない)")
    code, cal, out = _run_probe(FakeScraper(fail_month=(2026, 11)), db_extra=one,
                                prev={"2026-11-23": {"races": 24, "venues": ["東京", "福島"], "source": "db+netkeiba"}})
    check(cal["dates"].get("2026-11-23", {}).get("races") == 24,
          "前回補った値も引き継ぐ (今回の問い合わせが失敗しても 1R に戻さない)")

    print("\n   前回補った値が今回の一覧で支えられなくなった時 (#179 round 4 T-partd-4 / R3-T-stale-merge)")
    tokyo12 = [("2026-11-23", "05", "東京", r) for r in range(1, 13)]
    sup = {"races": 24, "venues": ["東京", "福島"], "source": "db+netkeiba"}
    code, cal, out = _run_probe(FakeScraper(), db_extra=tokyo12)
    check(cal["dates"].get("2026-11-23") == sup, f"1回目: DB 12R 東京 + 一覧 24R 東京・福島 → 補う ({cal['dates'].get('2026-11-23')})")
    prev1 = cal["dates"]
    code, cal, out = _run_probe(FakeScraper(lists={"20261123": JRA_1123[:12]}), prev=prev1, db_extra=tokyo12)
    d23 = cal["dates"].get("2026-11-23")
    check(d23 == {"races": 12, "venues": ["東京"]} and cal["probe"].get("reverted") == ["2026-11-23"]
          and cal["probe"]["status"] == "ok" and "2026-11-23" not in cal["probe"].get("kept", [])
          and sn.expected_races(d23) == 12,
          f"2回目: 一覧も DB も 東京 12R だけ (福島が中止・移動) → 前回補った 24R・福島を外して DB 由来に戻す "
          f"(期待レース数 12) ({d23} / {cal['probe'].get('reverted')})")
    code, cal, out = _run_probe(FakeScraper(lists={"20261123": JRA_1123[:12]}), prev=cal["dates"], db_extra=tokyo12)
    check(cal["dates"].get("2026-11-23") == {"races": 12, "venues": ["東京"]} and "reverted" not in cal["probe"],
          "3回目: 戻した後は引き継ぐものが無い (DB 由来のまま・何度でも同じ)")
    code, cal, out = _run_probe(None, args=(), prev=prev1, db_extra=tokyo12)
    check(cal["dates"].get("2026-11-23") == sup, "--probe なしの作り直しは前回補った値を引き継ぐ (確かめていない)")
    code, cal, out = _run_probe(FakeScraper(lists={"20261123": JRA_1123[:12]}), prev=cal["dates"], db_extra=tokyo12)
    check(cal["dates"].get("2026-11-23") == {"races": 12, "venues": ["東京"]},
          "その後の --probe で一覧が 東京 12R だけ → DB 由来に戻る (--probe なしを挟んでも治る)")
    six = [("2026-11-23", "05", "東京", r) for r in range(1, 7)]
    code, cal, out = _run_probe(FakeScraper(lists={"20261123": JRA_1123[:12]}), prev=prev1, db_extra=six)
    check(cal["dates"].get("2026-11-23") == {"races": 12, "venues": ["東京"], "source": "db+netkeiba"},
          "DB がまだ追いついていない (6R) 時に一覧が 東京 12R に縮む → 一覧で補った 12R・東京 (福島は外れる)")
    code, cal, out = _run_probe(FakeScraper(lists={"20261123": []}), prev=prev1, db_extra=tokyo12)
    check(cal["dates"].get("2026-11-23") == {"races": 12, "venues": ["東京"]} and cal["probe"].get("reverted") == ["2026-11-23"],
          "DB にある日の一覧が空 (取得は成功) → 補いを外して DB 由来に戻す (開催日のまま・DB 由来は減らさない)")
    code, cal, out = _run_probe(FakeScraper(fail_list={"20261123"}), prev=prev1, db_extra=tokyo12)
    check(cal["dates"].get("2026-11-23") == sup and "2026-11-23" in cal["probe"].get("kept", []),
          "一覧を取れない (HTTP 500) → 判定できないので前回補った値を引き継ぐ")
    code, cal, out = _run_probe(FakeScraper(lists={"20261123": []}), prev={"2026-11-23": dict(NK_1123)})
    check(cal["dates"].get("2026-11-23") == NK_1123 and "一覧が今回は空" in out and "::warning::" in out,
          "DB に無い日 (netkeiba だけで見つけた日) の一覧が空 → 中止か一時的な空ページか区別できないので警告して引き継ぐ")

    print("\n   probe だけが見つけた日 (DB にレースが無い) で時計が動く")
    code, cal, _ = _run_probe(FakeScraper())
    cal["generated_at"] = "2026-11-22T05:00:00+09:00"     # 前日朝の feature_parity が書いた版とする
    p = os.path.join(TMP, "cal_probe_only.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cal, fh, ensure_ascii=False)
    ok, why = race_utils.calendar_race_day(HOLIDAY, jst("2026-11-23T04:05"), p)
    check(ok is True and "24R" in why, f"calendar_race_day は probe の日を開催日とみなす ({why})")
    code, sent, _ = run_clock("41 6 * * 0-4", "2026-11-22T15:41", p)
    check(sent == ["weekend_prefetch.yml dates=20261123"], f"前夜 15:41 → 前夜予想 dates=20261123 ({sent})")
    code, sent, _ = run_clock("3 19 * * 0-4", "2026-11-23T04:05", p)
    check(sent == ["auto_post_x.yml mode=predict"], f"当日 04:05 → predict ({sent})")
    code, sent, _ = run_clock("33 23 * * 0-4", "2026-11-23T08:33", p)
    check(sent == ["race_day_runner.yml"], f"当日 08:33 → runner ({sent})")
    # dispatch 先が DB にレースの無い日でも先に出走表を集めること (静的に確認)
    with open(os.path.join(ROOT, ".github", "workflows", "auto_post_x.yml"), encoding="utf-8") as fh:
        apx = fh.read()
    with open(os.path.join(ROOT, ".github", "workflows", "weekend_prefetch.yml"), encoding="utf-8") as fh:
        wpf = fh.read()
    check(re.search(r'if \[ "\$RUNNERS" -ge 50 \]; then.*?else.*?python predict\.py collect --date "\$DATE"', apx, re.S)
          is not None, "auto_post_x mode=predict は出走馬 50 頭未満なら先に predict.py collect する")
    check(re.search(r'if \[ "\$REQUESTED" = "1" \]; then.*?predict\.py collect --date "\$D"', wpf, re.S) is not None,
          "weekend_prefetch dates=T は指定日の出走表が欠けていれば先に predict.py collect する")
    # fetch_weekend_races: 問い合わせ (通信) は db_pack の前 (#179 T-partd-5)
    with open(os.path.join(ROOT, ".github", "workflows", "fetch_weekend_races.yml"), encoding="utf-8") as fh:
        fwr = fh.read()
    i_probe, i_pack = fwr.find("build_race_calendar.py --probe"), fwr.find("bash scripts/db_pack.sh")
    check(0 < i_probe < i_pack, "fetch_weekend_races: カレンダーの問い合わせは db_pack (origin の取り込み) より前 (T-partd-5)")


# ── 11. (T-clock-2) 作り直しより後に決まった平日の開催 ───────────────────────
RL_0922 = ("".join(f'<a href="../race/result.html?race_id=20260604{r:02d}{r:02d}&rf=race_list">'
                   f'<a href="../race/movie.html?race_id=20260604{r:02d}{r:02d}&rf=race_list">' for r in range(1, 12))
           + '<a href="../race/shutuba_debut.html?race_id=202606041212&rf=race_list">')     # 中山 12R (実ページの形)
RL_EMPTY = '<div class="RaceList_Body RaceList_Top" id="RaceTopRace"><div class="RaceList_Box clearfix">\n\n</div></div>'
RL_ABROAD = '<a href="../race/shutuba.html?race_id=202644092201&rf=race_list">'


def test_late_postponement():
    print("\n11. (T-clock-2) カレンダーの作り直しより後に決まった平日の開催 (急な順延) を時計が netkeiba で確かめる")
    check(rc.race_ids_from_list_html(RL_0922) == sorted(f"20260604{r:02d}{r:02d}" for r in range(1, 12)) + ["202606041212"],
          "レース一覧の HTML から result/shutuba/shutuba_debut の race_id だけ拾う (movie は数えない)")
    # 月 9/21 10:29 に作り直したカレンダー (9/22 火 は載っていない = 新しい否定)
    cal_a = make_calendar(["2026-09-19", "2026-09-20"], "2026-09-21T10:29:00+09:00",
                          window=("2026-09-07", "2026-10-05"), name="cal_late.json")
    hit = {"race_list": {"20260922": RL_0922}}
    code, sent, out = run_clock("41 10 * * 0-4", "2026-09-21T19:41", cal_a, fake=hit)
    check(sent == ["weekend_prefetch.yml dates=20260922"] and "食い違い" in out,
          f"月 19:41: カレンダーは否定だが netkeiba に 9/22 の一覧 → 前夜予想を出す ({sent})")
    code, sent, _ = run_clock("3 22 * * 0-4", "2026-09-22T07:03", cal_a, fake=hit)
    check(sent == ["auto_post_x.yml mode=predict"], f"火 07:03 → predict ({sent})")
    code, sent, _ = run_clock("1 0 * * 1-5", "2026-09-22T09:01", cal_a, fake=hit)
    check(sent == ["race_day_runner.yml", "auto_post_x.yml mode=odds_flash"], f"火 09:01 → runner + odds_flash ({sent})")
    code, sent, _ = run_clock("31 0 * * 1-5", "2026-09-22T09:31", cal_a, fake={**hit, "active": {"race_day_runner.yml": 1}})
    check(sent == ["auto_post_x.yml mode=post_predict"], f"火 09:31 → post_predict ({sent})")
    for label, spec in (("問い合わせない (race_list に無い)", {}), ("取れない", {"race_list": {"20260922": None}}),
                        ("一覧が空", {"race_list": {"20260922": RL_EMPTY}}),
                        ("海外のレースだけ", {"race_list": {"20260922": RL_ABROAD}})):
        code, sent, out = run_clock("3 22 * * 0-4", "2026-09-22T07:03", cal_a, fake=spec)
        check(sent == [] and "開催日でない" in out, f"{label} → カレンダーどおり何もしない ({sent})")
    code, sent, out = run_clock("33 10 * * 1-5", "2026-09-22T19:33", cal_a, fake=hit)
    check(sent == [] and "問い合わせ" not in out, "results だけの tick は問い合わせない (何もしない)")
    code, sent, out = run_clock("37 15 * * 0-4", "2026-09-22T00:37", cal_a, fake=hit)
    check(sent == [] and "問い合わせ" not in out, "定刻に着いた runner だけの tick (8:30 前) は問い合わせない")
    code, sent, out = run_clock("3 22 * * 0-4", "2026-09-26T07:03", cal_a, fake=hit)
    check(sent == [] and "問い合わせ" not in out, "土曜の対象日は問い合わせない (ネイティブの cron)")
    # round 4 (T-clock-2 / R3-T-probe-worth-runner): decide は当日 8:30〜19:00 の tick なら intent に関係なく runner を
    #   確認する。問い合わせの要否 (probe_worth) も同じ条件にそろえる (載っている日と同じ動きになる)
    code, sent, out = run_clock("33 20 * * 0-4", "2026-09-22T08:50", cal_a, fake=hit)
    check(sent == ["race_day_runner.yml"] and "問い合わせ" in out,
          f"05:33 の predict の tick が遅れて 08:50 に着く (predict の締切後) → 問い合わせて runner を起こす ({sent})")
    code, sent, _ = run_clock("33 20 * * 0-4", "2026-09-22T08:50", make_calendar(
        ["2026-09-19", "2026-09-20", "2026-09-22"], "2026-09-21T10:29:00+09:00", window=("2026-09-07", "2026-10-05"),
        name="cal_late_listed.json"))
    check(sent == ["race_day_runner.yml"], f"(比較) 9/22 がカレンダーに載っている日の同じ tick も runner だけ ({sent})")
    code, sent, out = run_clock("3 22 * * 0-4", "2026-09-22T09:00", cal_a, fake=hit)
    check(sent == ["race_day_runner.yml"], f"07:03 の predict の tick が 09:00 に着く → runner ({sent})")
    code, sent, out = run_clock("41 14 * * 0-4", "2026-09-22T09:00", cal_a, fake=hit)
    check(sent == ["race_day_runner.yml"], f"前夜 23:41 の tick が 09:00 に着く (前夜予想の締切後) → runner ({sent})")
    code, sent, out = run_clock("11 14 * * 0-4", "2026-09-22T08:41", cal_a, fake=hit)
    check(sent == ["race_day_runner.yml"], f"前夜 23:11 の runner だけの tick が 9h30m 遅れて 08:41 に着く → runner ({sent})")
    code, sent, out = run_clock("11 11 * * 0-4", "2026-09-21T20:11", cal_a, fake=hit)
    check(sent == [] and "問い合わせ" not in out,
          f"前夜 20:11 の runner だけの tick が定刻に着く (対象日の前日) → 何もしない・問い合わせない ({sent})")
    code, sent, out = run_clock("11 11 * * 0-4", "2026-11-22T20:11", CAL_EVE)
    check(sent == [], f"(載っている日) 前夜 20:11 の tick が定刻に着く → 何もしない ({sent})")
    code, sent, out = run_clock("33 20 * * 0-4", "2026-09-22T08:50", cal_a, fake={"race_list": {"20260922": RL_EMPTY}})
    check(sent == [] and "問い合わせ" in out and "開催日でない" in out,
          "普通の平日に遅れた predict の tick が 08:50 に着く → 1回問い合わせて何もしない (コストは1リクエスト)")
    stale = make_calendar(["2026-09-19"], "2026-09-19T10:00:00+09:00", window=("2026-09-07", "2026-10-05"),
                          name="cal_stale.json")
    code, sent, out = run_clock("3 22 * * 0-4", "2026-09-22T07:03", stale, fake=hit)
    check(sent == [] and "縮退" in out, "カレンダーが古い (None) は従来どおり縮退 (問い合わせで上書きしない)")
    doc, _on = wf_doc("raceday_clock.yml")
    sparse = doc["jobs"]["tick"]["steps"][0]["with"]["sparse-checkout"].split()
    check("scripts/build_race_calendar.py" in sparse, "raceday_clock.yml の sparse checkout に build_race_calendar.py")


# ── 12. (T-landing-4) 予想を配信できていない日の results ──────────────────────
def test_results_needs_post_predict():
    print("\n12. (T-landing-4) 予想 (post_predict) の lock が無い日は results を出さない")
    code, sent, out = run_clock("33 10 * * 1-5", "2026-11-23T19:35", CAL_HOL)
    check(sent == [] and "報告対象なし" in out, f"19:35 で post_predict の lock 無し → results を出さない ({sent})")
    code, sent, _ = run_clock("33 12 * * 1-5", "2026-11-24T02:10", CAL_HOL)
    check(sent == [], f"21:33 の tick が翌 02:10 に来ても post_predict の lock が無ければ出さない ({sent})")
    code, sent, _ = run_clock("33 8 * * 1-5", "2026-11-23T17:33", CAL_HOL)
    check(sent == ["race_day_runner.yml"], f"17:33: runner は確認するが results は出さない ({sent})")


if __name__ == "__main__":
    test_tables()
    test_planned_fire()
    test_holiday_and_eve()
    test_calendar_race_day()
    test_prefetch_targets()
    test_gate_hold()
    test_gate_table()
    test_uniform_delay()
    test_probe()
    test_late_postponement()
    test_results_needs_post_predict()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 件失敗")
        for m in FAILS:
            print("   - " + m)
        sys.exit(1)
    print("✅ すべて通過 (#179 (2))")
