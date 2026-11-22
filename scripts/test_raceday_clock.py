#!/usr/bin/env python3
"""#179 (2): 土日以外の開催日の時計 (raceday_clock) と周辺の検査。ネットワーク・gh を使わない。

  python3 scripts/test_raceday_clock.py     (失敗があれば exit 1)

検査:
  1. raceday_clock.yml の schedule == SCHEDULE、各 tick の対象日が JST 月〜金、土日に動くのは日曜の前夜だけ
  2. auto_post_x.yml の schedule == dedup_cron の case == determine の case (今回は変えないが取り違えを止める)
  3. planned_fire (定刻 / 遅延 / 日付またぎ / 前倒し / 20時間超)
  4. 11/23 (月・祝) の再現、前夜、順延、縮退、待機・重複
  5. race_utils.calendar_race_day の全分岐
  6. scripts/prefetch_targets.py が移設前の heredoc と同じ出力 (golden) + 対象日の指定
  7. build_race_baseline._weekend_gate_hold の土日以外の開催日
  8. 指定の時刻でのゲート表 (土日 07:00 / 月祝 04:00・07:00・09:00・09:30・17:30 / 普通の月曜 07:00 /
     月祝前の日曜 22:30 / 金曜 22:30)
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
    check(weekend_fire and all(i == ("prefetch_eve",) and j_is_sun(c) for c, i in weekend_fire),
          f"JST の土日に動く tick は日曜の前夜だけ ({sorted({c for c, _ in weekend_fire})})")
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
    code, sent, _ = run_clock("33 8 * * 1-5", "2026-11-24T00:20", CAL_HOL)
    check(sent == ["auto_post_x.yml mode=results date_override=20261123"],
          f"17:33 の tick が 11/24 00:20 に遅れて来る → results (対象日 11/23) ({sent})")
    code, sent, _ = run_clock("1 0 * * 1-5", "2026-11-24T01:00", CAL_HOL)
    check(sent == [], f"09:01 の tick が翌日に遅れて来る → 前日分なので何もしない ({sent})")
    code, sent, _ = run_clock("7 11 * * 1-5", "2026-11-23T20:07", CAL_HOL, fake={"since": {"collect_results.yml": 300}})
    check(sent == ["collect_results.yml date=20261123"], f"20:07 → 夜間スイープ (date=11/23) ({sent})")

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
    code, sent, _ = run_clock("7 13 * * 1-5", "2026-11-23T22:07", CAL_HOL, fake={"since": {"collect_results.yml": 100}})
    check(sent == [], f"collect_results が100分前 → スイープを出さない ({sent})")
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
            code, sent, _out = run_clock(clock_cron, when, calp,
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


if __name__ == "__main__":
    test_tables()
    test_planned_fire()
    test_holiday_and_eve()
    test_calendar_race_day()
    test_prefetch_targets()
    test_gate_hold()
    test_gate_table()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 件失敗")
        for m in FAILS:
            print("   - " + m)
        sys.exit(1)
    print("✅ すべて通過 (#179 (2))")
