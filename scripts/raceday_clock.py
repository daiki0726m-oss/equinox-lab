#!/usr/bin/env python3
"""土日以外の開催日 (月曜祝日・台風順延) の定刻の起動 (#179)。

背景:
  土日の朝の処理 (予測の作り直し 04:00〜07:40・race_day_runner の起動・前夜予想) は、どれも
  曜日で書いた cron (UTC の '5,6' など) に乗っている。土日以外の開催日には自前の時計が無く、
  posting_watchdog の */30 の中のセーフティネット1本 (#160/#161) だけが頼りだった。
  2026-11-23(月・祝) は GitHub がその watchdog を5時間以上飛ばし、07:07 に手で dispatch する
  まで予測がゼロだった。1本の cron に全部を賭ける構造が原因。

仕組み:
  raceday_clock.yml が土日と同じ時刻 (分だけ数分ずらす) に20本の cron を持ち、各 tick がこの
  スクリプトを数秒走らせる。対象日 T が「土日以外の開催日」と race_calendar.json が言う時だけ、
  本体 (auto_post_x / race_day_runner / weekend_prefetch / collect_results) を mode と日付を
  明示して dispatch する。非開催日は何も出さずに終わる。本体の cron 表は1行も変えない
  (= 土日の挙動は構造的に不変)。セーフティネットは見張り役として残る。

  - 対象日は「起動した cron の直近の予定時刻」から決める (実行時刻ではない)。cron は数時間
    遅れる (#22) ので、23:41 の前夜 tick が翌 00:30 に来ても対象日は翌日 (= その夜の翌日) のまま。
  - 窓・lock・待機の判定はセーフティネットと同じ道具 (raceday_safety_net の関数) を使う。
    二重投稿の最終防止は post_x の lock (#141)。ここは無駄打ちを減らすだけ。

使い方 (テスト・ローカル):
  python3 scripts/raceday_clock.py --schedule '3 19 * * 0-4' --now 2026-11-23T04:05 \\
      --calendar <cal.json> --slots-dir <dir> --fake-gh '{}' --dry-run
  workflow は env (CLOCK_SCHEDULE / CLOCK_INTENT / CLOCK_DATE / CLOCK_DRY_RUN) を渡す。
終了コード: 0 = 正常 (何もしない・縮退を含む) / 1 = 例外・dispatch 失敗
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

JST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc

# cron (UTC) → (intents, 対象日の offset [予定時刻の JST 日 + offset])。唯一の対応表。
# raceday_clock.yml の schedule と scripts/test_raceday_clock.py で突き合わせる。
# 曜日欄: JST の 00:00-08:59 は前日の UTC なので JST 月〜金 = UTC '0-4'、09:00 以降は '1-5'。
# 前夜 (JST 日〜木の 15:41/19:41/23:41) は同日の UTC で '0-4'。
# → この時計が JST の土・日に動くのは「日曜の前夜3本」だけ (翌月曜が開催日の時しか何もしない)。
SCHEDULE = {
    "41 6 * * 0-4": (("prefetch_eve",), 1),            # 日〜木 15:41 前夜予想 (翌日分)
    "41 10 * * 0-4": (("prefetch_eve",), 1),           # 日〜木 19:41
    "41 14 * * 0-4": (("prefetch_eve",), 1),           # 日〜木 23:41
    "37 18 * * 0-4": (("prefetch_early",), 0),         # 月〜金 03:37 当日分の前夜予想
    "3 19 * * 0-4": (("predict",), 0),                 # 月〜金 04:03
    "33 20 * * 0-4": (("predict",), 0),                # 05:33
    "3 22 * * 0-4": (("predict",), 0),                 # 07:03
    "43 22 * * 0-4": (("predict",), 0),                # 07:43
    "33 23 * * 0-4": (("runner",), 0),                 # 08:33
    "53 23 * * 0-4": (("runner",), 0),                 # 08:53
    "1 0 * * 1-5": (("runner", "odds_flash"), 0),      # 09:01
    "31 0 * * 1-5": (("runner", "post_predict"), 0),   # 09:31
    "51 0 * * 1-5": (("runner", "post_predict"), 0),   # 09:51
    "7 1 * * 1-5": (("runner",), 0),                   # 10:07
    "7 2 * * 1-5": (("runner",), 0),                   # 11:07
    "7 4 * * 1-5": (("runner",), 0),                   # 13:07
    "7 6 * * 1-5": (("runner",), 0),                   # 15:07
    "33 8 * * 1-5": (("runner", "results"), 0),        # 17:33
    "7 11 * * 1-5": (("sweep",), 0),                   # 20:07 夜間スイープ
    "7 13 * * 1-5": (("sweep",), 0),                   # 22:07
}
INTENTS = ("prefetch_eve", "prefetch_early", "predict", "runner", "odds_flash",
           "post_predict", "results", "sweep")
MANUAL_OFFSET = {"prefetch_eve": 1}
PREDICT_RUN_MIN = 12          # predict の run の所要 (fetch の chain #173 と同じ) — 9:00 の12分前で締める
EARLY_TOLERANCE_MIN = 10      # cron が予定より早く起きた時の許容
MAX_DELAY_H = 20              # これより遅れた tick は対象日を決められないので何もしない
SWEEP_MIN_GAP = 150           # 夜間スイープは前回から150分以上空ける (1晩2回まで、セーフティネットと同じ)


def log(msg=""):
    print(msg, flush=True)


# ── 予定時刻 ─────────────────────────────────────────────────────
def _dow_set(field):
    """cron の曜日欄 ('0-4' / '1-5' / '*' / '6,0') → 曜日の集合 (cron の数え方 0=日 … 6=土)。"""
    if field.strip() == "*":
        return set(range(7))
    out = set()
    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            out.update(range(a, b + 1))
        elif part:
            out.add(int(part))
    return {x % 7 for x in out}


def planned_fire(cron, now_utc):
    """起動した cron の直近の予定時刻 (UTC)。cron は早くは起きないが、念のため10分の前倒しを許す。
    曜日欄に合うまで1日ずつ戻す。読めなければ None。"""
    try:
        mi, hh, _dom, _mon, dow = cron.split()
        allowed = _dow_set(dow)
        cand = now_utc.astimezone(UTC).replace(hour=int(hh), minute=int(mi), second=0, microsecond=0)
    except Exception:
        return None
    if cand > now_utc + dt.timedelta(minutes=EARLY_TOLERANCE_MIN):
        cand -= dt.timedelta(days=1)
    for _ in range(8):
        if (cand.weekday() + 1) % 7 in allowed:      # Python 月=0 → cron 月=1
            return cand
        cand -= dt.timedelta(days=1)
    return None


def final_rebuild_hm():
    """predict.FINAL_REBUILD_JST (当日の最後の作り直し #173) を HHMM の整数で。predict は import しない
    (lightgbm 等が要る) — fetch_weekend_races の chain と同じく正規表現で読む。読めなければ 900。"""
    try:
        with open(os.path.join(ROOT, "predict.py"), encoding="utf-8") as f:
            m = re.search(r"^FINAL_REBUILD_JST\s*=\s*\(\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)", f.read(), re.M)
        if m:
            return int(m.group(1)) * 100 + int(m.group(2))
    except OSError:
        pass
    log("::warning::#179: predict.FINAL_REBUILD_JST を読めない → 9:00 とみなす")
    return 900


def _minus_min(hm, minutes):
    t = (hm // 100) * 60 + hm % 100 - minutes
    return (t // 60) * 100 + t % 60


# ── 判定 (純粋関数) ───────────────────────────────────────────────
def decide(intents, target, now, locks, gh, final_hm):
    """1 tick で出す dispatch を決める。戻り値 [(workflow, mode, fields, 理由)]。

    target: 対象日 (date)、now: JST の aware datetime、locks: posted_slots_T の slots (dict)、
    gh: active(wf, title=None) / pending(wf) / since(wf) を持つオブジェクト (-1 / None = 取得失敗)。
    カレンダーの判定・土日の除外は呼び出し側で済ませてから呼ぶ。
    1 tick で auto_post_x へ出すのは最大1本 (concurrency の待機は1本しか持てない #161)。"""
    from race_utils import (PREDICTION_LOCK_HM, ODDS_FLASH_LATEST_HM,
                            prediction_locked_for)
    acts = []
    hm = now.hour * 100 + now.minute
    same_day = now.date() == target
    t8 = target.strftime("%Y%m%d")
    # 翌朝 05:00 までは T の続き (results・夜間スイープ)
    t_start = dt.datetime.combine(target, dt.time(0, 0), JST)
    after = lambda h, m: now >= t_start + dt.timedelta(hours=h, minutes=m)   # noqa: E731
    before_next5 = now < t_start + dt.timedelta(days=1, hours=5)

    # runner: 当日 8:30-19:00 の tick はどれも確認する (安い。昼に runner が死んでも次の tick が起こす)
    if same_day and 830 <= hm < 1900:
        n = gh.active("race_day_runner.yml")
        if n == 0:
            acts.append(("race_day_runner.yml", None, {}, "race_day_runner が不在"))
        elif n < 0:
            log("   ⚠️ race_day_runner の稼働状況を確認できない → 起動を見送り")
        else:
            log(f"   ✅ race_day_runner 稼働中/待機中 ({n}件)")

    ap_sent = False
    for intent in intents:
        if intent == "runner":
            continue
        if intent in ("prefetch_eve", "prefetch_early"):
            cutoff = t_start + dt.timedelta(hours=final_hm // 100, minutes=final_hm % 100)
            if now >= cutoff:
                log(f"   ⏭️ {intent}: 対象日 {t8} の最後の作り直し ({final_hm // 100}:{final_hm % 100:02d}) を過ぎた")
                continue
            n = gh.active("weekend_prefetch.yml")
            if n != 0:
                log(f"   ⏳ weekend_prefetch が実行中/待機中/確認不能 ({n}) → 重ねない")
                continue
            acts.append(("weekend_prefetch.yml", None, {"dates": t8}, f"{t8} の前夜予想"))
            continue
        if intent == "sweep":
            if not (after(20, 0) and before_next5):
                log(f"   ⏭️ sweep: 窓 ({t8} 20:00〜翌05:00) の外")
                continue
            since = gh.since("collect_results.yml")
            running = gh.active("collect_results.yml")
            if since is None or running != 0:
                log(f"   ⏳ collect_results: 直近 {since} 分前 / 実行中 {running} → 見送り")
                continue
            if since < SWEEP_MIN_GAP:
                log(f"   ✅ collect_results は {since}分前に実行済み")
                continue
            acts.append(("collect_results.yml", None, {"date": t8}, f"{t8} の夜間スイープ"))
            continue
        # ここから auto_post_x (predict / odds_flash / post_predict / results)
        if ap_sent:
            log(f"   ⏭️ {intent}: この tick は auto_post_x を既に1本出した")
            continue
        if intent == "predict":
            limit = _minus_min(final_hm, PREDICT_RUN_MIN)
            if not same_day:
                log(f"   ⏭️ predict: 日付をまたいだ tick (対象日 {t8}) → 前日分なので出さない")
                continue
            if hm >= limit or prediction_locked_for(t8, now):
                log(f"   ⏭️ predict: 作り直しの締切 ({limit // 100}:{limit % 100:02d}) を過ぎた/確定済み")
                continue
            n = gh.active("auto_post_x.yml")
            if n != 0:
                log(f"   ⏳ auto_post_x が実行中/待機中/確認不能 ({n}) → 後ろに積まない")
                continue
            acts.append(("auto_post_x.yml", "predict", {}, f"{t8} の予測の作り直し"))
            ap_sent = True
            continue
        if intent in ("odds_flash", "post_predict", "results"):
            if intent == "results":
                ok = after(17, 30) and before_next5
                win = f"{t8} 17:30〜翌05:00"
            else:
                start, end = (900, ODDS_FLASH_LATEST_HM) if intent == "odds_flash" else (PREDICTION_LOCK_HM, 1155)
                ok = same_day and start <= hm < end
                win = f"当日 {start // 100}:{start % 100:02d}〜{end // 100}:{end % 100:02d}"
            if not ok:
                log(f"   ⏭️ {intent}: 窓 ({win}) の外")
                continue
            if intent in locks:
                log(f"   ⏭️ {intent} は lock 済み ({(locks[intent] or {}).get('posted_at')})")
                continue
            p = gh.pending("auto_post_x.yml")
            a = gh.active("auto_post_x.yml", title=intent)
            if p != 0 or a != 0:
                log(f"   ⏳ {intent}: auto_post_x の待機 {p} / 同じ mode の実行中 {a} → 重ねない")
                continue
            fields = {"date_override": t8} if intent == "results" else {}
            acts.append(("auto_post_x.yml", intent, fields, f"{t8} の {intent}"))
            ap_sent = True
    return acts


# ── GitHub (本番) と偽物 (テスト) ─────────────────────────────────
class LiveGh:
    """raceday_safety_net の gh 呼び出しを包む (同じ判定をする)。"""

    def __init__(self):
        import raceday_safety_net as sn
        self.sn = sn

    def active(self, wf, title=None):
        return self.sn.gh_runs_active(wf, title=title)

    def pending(self, wf):
        return self.sn._auto_post_pending() if wf == "auto_post_x.yml" else self.active(wf)

    def since(self, wf):
        """直近 run からの経過分。run が1本も無ければ大きな値、取得失敗は None
        (セーフティネットの gh_minutes_since_last_run は両者を区別しないので自前で引く)。"""
        try:
            out = subprocess.run(
                ["gh", "api", f"repos/{os.environ.get('GITHUB_REPOSITORY', '')}"
                 f"/actions/workflows/{wf}/runs?per_page=1", "--jq",
                 ".workflow_runs[0].created_at // \"none\""],
                capture_output=True, text=True, timeout=60)
        except Exception:
            return None
        ts = out.stdout.strip()
        if out.returncode != 0 or not ts:
            return None
        if ts == "none":
            return 10 ** 6
        try:
            t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None
        return int((dt.datetime.now(UTC) - t).total_seconds() // 60)

    def dispatch(self, wf, mode, fields, dry):
        return self.sn.dispatch(wf, mode, dry, fields or None)


class FakeGh:
    """--fake-gh '{"active": {"auto_post_x.yml": 0, "auto_post_x.yml:odds_flash": 0},
    "pending": {...}, "since": {"collect_results.yml": 300}, "dispatch_fail": [...]}'。無いキーは 0。"""

    def __init__(self, spec):
        self.s = spec or {}
        self.sent = []

    def active(self, wf, title=None):
        a = self.s.get("active") or {}
        if title is not None and f"{wf}:{title}" in a:
            return a[f"{wf}:{title}"]
        return a.get(wf, 0) if title is None else 0

    def pending(self, wf):
        return (self.s.get("pending") or {}).get(wf, 0)

    def since(self, wf):
        return (self.s.get("since") or {}).get(wf, 10 ** 6)

    def dispatch(self, wf, mode, fields, dry):
        label = wf + (f" mode={mode}" if mode else "") + "".join(f" {k}={v}" for k, v in (fields or {}).items())
        self.sent.append(label)
        if wf in (self.s.get("dispatch_fail") or []):
            log(f"   ❌ dispatch 失敗: {label}")
            return False
        log(f"   [dry-run] would dispatch: {label}" if dry else f"   ✅ (fake) dispatched: {label}")
        return True


def read_locks(t8, slots_dir=None):
    """posted_slots_T の lock。contents API (CDN を通らない #173) → checkout の版 → 無し。"""
    if not slots_dir:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if repo:
            try:
                r = subprocess.run(
                    ["gh", "api", "-H", "Accept: application/vnd.github.raw",
                     f"repos/{repo}/contents/docs/data/posted_slots_{t8}.json?ref=main"],
                    capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and r.stdout.strip():
                    return (json.loads(r.stdout).get("slots") or {})
                if "Not Found" in (r.stdout + r.stderr):
                    return {}
            except Exception:
                pass
        slots_dir = os.path.join(ROOT, "docs", "data")
    try:
        with open(os.path.join(slots_dir, f"posted_slots_{t8}.json"), encoding="utf-8") as f:
            return json.load(f).get("slots") or {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"   ⚠️ posted_slots_{t8}.json を読めない ({type(e).__name__}) → lock 無しとみなす")
        return {}


def _parse_now(s):
    t = dt.datetime.fromisoformat(s)
    return t.replace(tzinfo=JST) if t.tzinfo is None else t.astimezone(JST)


def main(argv=None):
    ap = argparse.ArgumentParser(description="土日以外の開催日の定刻の起動 (#179)")
    ap.add_argument("--schedule", default=os.environ.get("CLOCK_SCHEDULE", ""))
    ap.add_argument("--intent", default=os.environ.get("CLOCK_INTENT", ""))
    ap.add_argument("--date", default=os.environ.get("CLOCK_DATE", ""), help="対象日 YYYYMMDD (手動)")
    ap.add_argument("--now", help="現在時刻 (JST, ISO8601。tz 無しは JST) — テスト用")
    ap.add_argument("--calendar", help="race_calendar.json の場所 — テスト用")
    ap.add_argument("--slots-dir", help="posted_slots_*.json の場所 — テスト用 (指定時は API を見ない)")
    ap.add_argument("--fake-gh", help="偽の gh (JSON) — テスト用")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    now = _parse_now(a.now) if a.now else dt.datetime.now(JST)
    sched = (a.schedule or "").strip()
    manual = not sched
    dry = a.dry_run or (manual and os.environ.get("CLOCK_DRY_RUN", "1") != "0")

    if sched:
        if sched not in SCHEDULE:
            log(f"::warning::#179: 対応表に無い cron '{sched}' → 何もしない")
            return 0
        intents, offset = SCHEDULE[sched]
        planned = planned_fire(sched, now.astimezone(UTC))
        if planned is None:
            log(f"::warning::#179: cron '{sched}' の予定時刻を決められない → 何もしない")
            return 0
        planned_jst = planned.astimezone(JST)
        delay_min = (now - planned_jst).total_seconds() / 60
        if delay_min > MAX_DELAY_H * 60:
            log(f"::warning::#179: 予定 {planned_jst:%m/%d %H:%M} から {delay_min / 60:.1f} 時間遅れた tick → 何もしない")
            return 0
        target = planned_jst.date() + dt.timedelta(days=offset)
        log(f"🗓 #179 時計: cron '{sched}' 予定 {planned_jst:%Y-%m-%d %H:%M} JST / 実行 {now:%Y-%m-%d %H:%M} JST "
            f"(遅れ {max(0, delay_min):.0f}分) → 対象日 {target} / intents {list(intents)}")
    else:
        if a.intent not in INTENTS:
            log(f"使い方: --schedule か --intent ({'/'.join(INTENTS)}) を指定 (#179)")
            return 0
        intents = ("runner", a.intent) if a.intent != "runner" else ("runner",)
        if a.date:
            d = a.date.replace("-", "")
            target = dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        else:
            target = now.date() + dt.timedelta(days=MANUAL_OFFSET.get(a.intent, 0))
        log(f"🗓 #179 時計 (手動): intent {a.intent} / 対象日 {target} / 実行 {now:%Y-%m-%d %H:%M} JST"
            f"{' / dry-run (判定だけ)' if dry else ''}")

    if target.weekday() >= 5:
        log(f"⏭️ {target} は土日 → ネイティブの cron が担当 (この時計は何もしない)")
        return 0

    from race_utils import calendar_race_day
    listed, why = calendar_race_day(target.isoformat(), now=now, path=a.calendar)
    if listed is None:
        log(f"::warning::#179: race_calendar.json を判定に使えない ({why}) → 土日だけを開催日とみなす (縮退)。"
            f"この tick は何もしない。土日以外の開催日なら posting_watchdog のセーフティネットが Issue で知らせる")
        return 0
    if not listed:
        log(f"⏭️ {why} → 何もしない")
        return 0
    log(f"🏇 {why}")

    gh = FakeGh(json.loads(a.fake_gh)) if a.fake_gh is not None else LiveGh()
    locks = read_locks(target.strftime("%Y%m%d"), a.slots_dir)
    log(f"   lock 済み slot: {sorted(locks) or 'なし'}")
    acts = decide(intents, target, now, locks, gh, final_rebuild_hm())
    failed = 0
    for wf, mode, fields, why in acts:
        log(f"🚨 {why} → dispatch")
        if not gh.dispatch(wf, mode, fields, dry):
            failed += 1
    log(f"\n📋 dispatch: {[f'{w}' + (f' {m}' if m else '') for w, m, _, _ in acts] or 'なし'}"
        + (" (dry-run)" if dry else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
