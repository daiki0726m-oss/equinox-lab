#!/usr/bin/env python3
"""開催日セーフティネット (#160) — 曜日でなくカレンダーで race-day slot を保証する。

背景:
  auto_post_x.yml の race-day cron は全て DOW '6,0' (土日)、race_day_runner は
  '5,6'/'6,0' 固定。JRA は月曜・祝日にも開催し、台風順延で火曜開催にもなるため、
  曜日は「開催日」の代理変数として壊れている。実例:
    2026-09-21(月・祝) 阪神12R … 予測は手動生成、odds_flash/post_predict/results/
                                  結果収集はどれも発火せず
    2026-09-22(火)     中山12R … 9/21 からの台風順延。同上
  そこで DB 由来の docs/data/race_calendar.json を正として、今日が開催日なら
  未実施の slot だけを dispatch する。

安全性:
  * dispatch 先は全て posted_slots サイドカー (#141) の atomic lock を尊重するので、
    多重発火しても実投稿は1回。ここでも事前 check して無駄打ちを減らす。
  * post_predict は post_x.py 側の 12:00 JST ハード上限 (#69) があるため、
    それを越える時刻では dispatch しない (発走後の予想投稿は事故)。
  * 土日は呼び出し側 (posting_watchdog.yml) が skip する。
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "docs", "data")


def jst_now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_race_day(day_iso):
    """race_calendar.json で今日が開催日か。カレンダーが無い/古い時は None (判定不能)。"""
    cal = _load(os.path.join(DATA, "race_calendar.json"))
    if not cal or not isinstance(cal.get("dates"), dict):
        return None, None
    entry = (cal["dates"] or {}).get(day_iso)
    if not entry:
        # 窓 (±14日) の中なら「開催日でない」と確定できる。窓外なら判定不能。
        win = cal.get("window") or []
        if len(win) == 2 and win[0] <= day_iso <= win[1]:
            return False, cal
        return None, cal
    return True, cal


def locked_slots(day8):
    d = _load(os.path.join(DATA, f"posted_slots_{day8}.json")) or {}
    return d.get("slots") or {}


def predictions_ready(day8):
    """公開済み予測JSONのレース数。export_predictions.py は
    {date, total_races, venues:{場名:[race,...]}, ...} を書く。"""
    d = _load(os.path.join(DATA, f"predictions_{day8}.json"))
    if not d:
        return 0
    if isinstance(d, list):
        return len(d)
    n = d.get("total_races")
    if isinstance(n, int):
        return n
    venues = d.get("venues") or {}
    if isinstance(venues, dict):
        return sum(len(v or []) for v in venues.values())
    return len(d.get("races") or [])


def gh_runs_active(workflow, title=None):
    """in_progress / queued の run 数。失敗時は -1 (判定不能 → dispatch しない側に倒す)。"""
    total = 0
    for status in ("in_progress", "queued"):
        try:
            out = subprocess.run(
                ["gh", "api", f"repos/{os.environ.get('GITHUB_REPOSITORY','')}"
                 f"/actions/workflows/{workflow}/runs?status={status}&per_page=20",
                 "--jq", ".workflow_runs[].display_title"],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode != 0:
                return -1
            titles = [t for t in out.stdout.split("\n") if t.strip()]
            total += len(titles) if title is None else sum(1 for t in titles if t == title)
        except Exception:
            return -1
    return total


def dispatch(workflow, mode=None, dry=False):
    cmd = ["gh", "workflow", "run", workflow, "--ref", "main"]
    if mode:
        cmd += ["-f", f"mode={mode}"]
    label = f"{workflow}" + (f" mode={mode}" if mode else "")
    if dry:
        print(f"   [dry-run] would dispatch: {label}")
        return True
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"   ✅ dispatched: {label}")
        return True
    print(f"   ❌ dispatch 失敗: {label} — {r.stderr.strip()[:200]}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--now", help="HH:MM で現在時刻を上書き (テスト用)")
    ap.add_argument("--date", help="YYYY-MM-DD で対象日を上書き (テスト用)")
    args = ap.parse_args()

    now = jst_now()
    day_iso = args.date or now.strftime("%Y-%m-%d")
    day8 = day_iso.replace("-", "")
    if args.now:
        hh, mm = args.now.split(":")
        hm = int(hh) * 100 + int(mm)
    else:
        hm = now.hour * 100 + now.minute

    raceday, cal = is_race_day(day_iso)
    if raceday is None:
        print(f"⚠️ race_calendar.json で {day_iso} を判定できない → 何もしない")
        return 0
    if not raceday:
        print(f"⏭️ {day_iso} は開催日でない → skip")
        return 0

    info = (cal["dates"] or {}).get(day_iso, {})
    print(f"🏇 {day_iso} は開催日 ({info.get('races')}R / {'・'.join(info.get('venues') or [])}), "
          f"現在 {hm//100:02d}:{hm%100:02d} JST")

    locked = locked_slots(day8)
    n_pred = predictions_ready(day8)
    print(f"   予測JSON: {n_pred}R / lock 済み slot: {sorted(locked) or 'なし'}")

    acted = []

    # ── 1. 予測 (ダッシュボード + 後続 slot の前提) ───────────────────────
    # 朝5時以降に予測JSONが無ければ生成。既に走っていれば重ねない。
    #
    # 「一部だけ生成された」場合も埋め直す。オッズが順次公開される日は
    # 先に売り出された数レースだけ予測が通り、残りが #52 のオッズゲートで
    # skip される (export は生成できた分だけ書く)。0件しか見ないと、
    # 1レースでも通った瞬間に「予測済み」と誤認して残りが永久に埋まらない。
    # 投稿窓が閉じる 11:00 までは不足があれば再試行する。
    n_cal = info.get("races") or 0
    short = n_cal and n_pred and n_pred < n_cal and hm < 1100
    if short:
        print(f"   ⚠️ 予測が不足 ({n_pred}/{n_cal}R) → 埋め直しを試みる")
    if hm >= 500 and (n_pred == 0 or short):
        active = gh_runs_active("auto_post_x.yml", title="predict")
        if active == 0:
            print(f"🚨 開催日なのに予測が{'不足' if short else '無い'} → predict を dispatch")
            if dispatch("auto_post_x.yml", "predict", args.dry_run):
                acted.append("predict")
        elif active < 0:
            print("   ⚠️ 実行中 run を確認できない → predict dispatch を見送り")
        else:
            print(f"   ⏳ predict が実行中/待機中 ({active}件) → 重ねない")

    # ── 2. race_day_runner (結果収集ループ) ──────────────────────────────
    if 840 <= hm < 1700:
        active = gh_runs_active("race_day_runner.yml")
        if active == 0:
            print("🚨 開催日なのに race_day_runner 不在 → 起動")
            if dispatch("race_day_runner.yml", None, args.dry_run):
                acted.append("race_day_runner")
        elif active < 0:
            print("   ⚠️ runner の稼働状況を確認できない → 起動を見送り")
        else:
            print(f"   ✅ race_day_runner 稼働中 ({active}件)")

    # ── 3. 投稿 slot (lock があれば触らない) ─────────────────────────────
    #  odds_flash 9:30 / post_predict 10:15 / results 17:30。
    #  post_predict は post_x.py の 12:00 ハード上限 (#69) 手前までしか出さない。
    for mode, start, end in (("odds_flash", 930, 1130),
                             ("post_predict", 1015, 1155),
                             ("results", 1730, 2300)):
        if not (start <= hm < end):
            continue
        if mode in locked:
            print(f"   ⏭️ {mode} は lock 済み ({locked[mode].get('posted_at')}) → skip")
            continue
        if mode in ("odds_flash", "post_predict") and n_pred == 0:
            print(f"   ⏳ {mode}: 予測JSONが未生成 → 次の tick に回す")
            continue
        active = gh_runs_active("auto_post_x.yml", title=mode)
        if active != 0:
            print(f"   ⏳ {mode} は実行中/確認不能 ({active}) → 重ねない")
            continue
        print(f"🚨 開催日なのに {mode} が未実施 → dispatch")
        if dispatch("auto_post_x.yml", mode, args.dry_run):
            acted.append(mode)

    print(f"\n📋 dispatch: {acted or 'なし'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
