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


CALENDAR_MAX_AGE_H = 36   # 日次再生成 + GitHub cron の遅延 (最大 ~8h) を見込んだ上限


def calendar_age_hours(cal, now=None):
    """race_calendar.json の古さ (時間)。generated_at が読めなければ None。"""
    try:
        gen = datetime.datetime.fromisoformat(cal.get("generated_at"))
        now = now or jst_now()
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
        return (now - gen).total_seconds() / 3600
    except Exception:
        return None


def is_race_day(day_iso):
    """race_calendar.json で今日が開催日か。(True/False/None, cal, 理由)

    #161: 旧版は docstring で「古い時は None」と書きながら generated_at を一切見ておらず、
    窓内で dates に無い日は **確信を持って False** を返していた。カレンダーは1日1回しか
    作られないので、後から DB に登録された開催 (台風順延・急な月曜開催) は翌日まで載らない。
    その間セーフティネットは「開催日でない」と断定して全 slot を黙って skip する。

    - dates に載っている → True (肯定は古くても信用できる: 開催予定は消えない)
    - 載っていない & カレンダーが新しい → False
    - 載っていない & カレンダーが古い/鮮度不明 → None (判定不能 = 通知する)
    """
    cal = _load(os.path.join(DATA, "race_calendar.json"))
    if not cal or not isinstance(cal.get("dates"), dict):
        return None, None, "race_calendar.json が無い/壊れている"
    if (cal["dates"] or {}).get(day_iso):
        return True, cal, ""
    age = calendar_age_hours(cal)
    if age is None or age > CALENDAR_MAX_AGE_H:
        return None, cal, (f"カレンダーが古い (生成 {cal.get('generated_at')}, "
                           f"{'不明' if age is None else f'{age:.0f}時間前'})")
    win = cal.get("window") or []
    if len(win) == 2 and win[0] <= day_iso <= win[1]:
        return False, cal, ""
    return None, cal, f"{day_iso} はカレンダーの窓 {win} の外"


def expected_races(info):
    """その日に予測されているべきレース数の下限。

    #161: カレンダーの races は **生成時点の DB スナップショット**で、週末の出走馬登録
    (fetch_weekend_races) より前に作られると過少になる (実測: 2026-09-25 07:52 生成の版は
    9/26 を 7R と記録、実際は 24R)。それを不足判定の分母にすると 7/24 で「揃った」と
    誤認していた。JRA は1場1日12レースなので、場数×12 を下限として併用する。
    """
    return max(int(info.get("races") or 0), 12 * len(info.get("venues") or []))


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
    """実行中・待機中の run 数。失敗時は -1 (判定不能 → dispatch しない側に倒す)。

    #161: concurrency で順番待ちの run は API 上 status=pending になり、queued には出ない。
    旧版はそれを数えず、待機中の run を「無い」と読んで重ねて dispatch していた
    (GitHub の concurrency は待機を1本しか持たないので、新しい方が古い方を取り消す)。
    """
    total = 0
    for status in ("in_progress", "queued", "pending", "waiting", "requested"):
        try:
            out = subprocess.run(
                ["gh", "api", f"repos/{os.environ.get('GITHUB_REPOSITORY','')}"
                 f"/actions/workflows/{workflow}/runs?status={status}&per_page=20",
                 "--jq", ".workflow_runs[].display_title"],
                capture_output=True, text=True, timeout=60,
            )
            if out.returncode != 0:
                print(f"::warning::gh api 失敗 ({workflow} status={status}) — セーフティネットが判定できない")
                return -1
            titles = [t for t in out.stdout.split("\n") if t.strip()]
            total += len(titles) if title is None else sum(1 for t in titles if t == title)
        except Exception:
            return -1
    return total



def gh_minutes_since_last_run(workflow):
    """その workflow の直近 run から何分経ったか。取得できなければ -1。"""
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{os.environ.get('GITHUB_REPOSITORY','')}"
             f"/actions/workflows/{workflow}/runs?per_page=1",
             "--jq", ".workflow_runs[0].created_at // empty"],
            capture_output=True, text=True, timeout=60,
        )
        ts = out.stdout.strip()
        if out.returncode != 0 or not ts:
            return -1
        t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        return int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() // 60)
    except Exception:
        return -1


def _auto_post_pending():
    """auto_post_x の待機中 (pending/queued/waiting/requested) run 数。判定不能なら 0。"""
    n = 0
    for status in ("pending", "queued", "waiting", "requested"):
        try:
            out = subprocess.run(
                ["gh", "api", f"repos/{os.environ.get('GITHUB_REPOSITORY','')}"
                 f"/actions/workflows/auto_post_x.yml/runs?status={status}&per_page=20",
                 "--jq", ".total_count"],
                capture_output=True, text=True, timeout=60)
            if out.returncode == 0 and out.stdout.strip().isdigit():
                n += int(out.stdout.strip())
        except Exception:
            pass
    return n


def dispatch(workflow, mode=None, dry=False, fields=None):
    cmd = ["gh", "workflow", "run", workflow, "--ref", "main"]
    if mode:
        cmd += ["-f", f"mode={mode}"]
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    label = f"{workflow}" + (f" mode={mode}" if mode else "") + \
        "".join(f" {k}={v}" for k, v in (fields or {}).items())
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

    alerts = []   # 人が対応すべき異常。1つでもあれば exit 2 → workflow が Issue を立てる

    raceday, cal, why = is_race_day(day_iso)
    if raceday is None:
        print(f"⚠️ {day_iso} が開催日か判定できない: {why}")
        alerts.append(f"開催日の判定ができない — {why}。"
                      f"この状態ではセーフティネットは何もしない (開催日なら全 slot が無防備)")
        return _finish(alerts, [], day_iso)
    if not raceday:
        print(f"⏭️ {day_iso} は開催日でない → skip")
        return 0

    info = (cal["dates"] or {}).get(day_iso, {})
    n_exp = expected_races(info)
    print(f"🏇 {day_iso} は開催日 (カレンダー {info.get('races')}R / "
          f"{'・'.join(info.get('venues') or [])} → 期待 {n_exp}R), "
          f"現在 {hm//100:02d}:{hm%100:02d} JST")

    locked = locked_slots(day8)
    n_pred = predictions_ready(day8)
    print(f"   予測JSON: {n_pred}R / lock 済み slot: {sorted(locked) or 'なし'}")

    acted = []
    # #161: auto_post_x への dispatch は 1 tick に1本まで。すでに待機中の run があれば出さない。
    # concurrency group は待機を1本しか持たず、2本目を投げると1本目が黙って取り消される
    # (9/22 に odds_flash と post_predict が2秒差で互いを取り消したのを実測)。
    ap_busy = _auto_post_pending()
    if ap_busy:
        print(f"   ⏳ auto_post_x に待機中の run が {ap_busy} 本 → この tick では auto_post_x を dispatch しない")
    sent_ap = bool(ap_busy)

    # ── 1. 予測 (ダッシュボード + 後続 slot の前提) ───────────────────────
    # 無い・足りないなら生成。オッズが順次公開される日は先に売り出された数レースだけ
    # 予測が通り、残りが #52 のオッズゲートで skip される — 0件しか見ないと
    # 1レース通った瞬間に「予測済み」と誤認して残りが埋まらない。
    # #161: 上限は 10:00。auto_post_x の predict は「10時以降は確定済み予測を保護」で
    # 予測を再実行しないので、10時以降に dispatch しても何も起きない (旧版は 11:00 まで
    # 空打ちしていた)。10時を過ぎて足りない場合は人に知らせる。
    short = n_pred < n_exp
    if short and 500 <= hm < 1000 and not sent_ap:
        if n_pred:
            print(f"   ⚠️ 予測が不足 ({n_pred}/{n_exp}R) → 埋め直しを試みる")
        active = gh_runs_active("auto_post_x.yml", title="predict")
        if active == 0:
            print(f"🚨 開催日なのに予測が{'不足' if n_pred else '無い'} → predict を dispatch")
            if dispatch("auto_post_x.yml", "predict", args.dry_run):
                acted.append("predict")
                sent_ap = True
        elif active < 0:
            print("   ⚠️ 実行中 run を確認できない → predict dispatch を見送り")
        else:
            print(f"   ⏳ predict が実行中/待機中 ({active}件) → 重ねない")
    elif short and hm >= 1000:
        msg = (f"予測が {n_pred}/{n_exp}R のまま 10:00 を過ぎた "
               f"(auto_post_x は10時以降に予測を作らない)")
        print(f"🚨 {msg}")
        alerts.append(msg)

    # ── 2. race_day_runner (結果収集ループ) ──────────────────────────────
    # #161: 窓の終わりを runner 自身の終了時刻 (end_hour=19) に揃えた。旧版は 17:00 で
    # 閉じており、runner が 15:08 に6時間上限で死んだ 9/21 は 17:03 の tick が
    # 「窓外」で素通りし、最終3レースと results が失われた。
    runner_started = False
    if 840 <= hm < 1900:
        active = gh_runs_active("race_day_runner.yml")
        if active == 0:
            print("🚨 開催日なのに race_day_runner 不在 → 起動")
            if dispatch("race_day_runner.yml", None, args.dry_run):
                acted.append("race_day_runner")
                runner_started = True
        elif active < 0:
            print("   ⚠️ runner の稼働状況を確認できない → 起動を見送り")
        else:
            print(f"   ✅ race_day_runner 稼働中 ({active}件)")

    # ── 3. 投稿 slot (lock があれば触らない) ─────────────────────────────
    # #161 で窓を実ガードに合わせた:
    #  - odds_flash: post_x.py 側は 9:00-10:59 のみ受け付ける → 旧 11:30 までの dispatch は空打ち
    #  - post_predict: post_x.py の 12:00 ハード上限 (#69) の手前まで
    #  - results / refresh_dashboard: 日付が変わるまで続ける。旧版は 17:30-23:00 と
    #    夜間スイープの 20-21/22-23 時が分断しており、watchdog の実発火 (中央値 195分) が
    #    その隙間に落ちると一晩中何も起きなかった (9/21)。cmd_results は未完走なら
    #    lock を解放して投稿しない (#91) ので、早め・多めに叩いても安全。
    # refresh_dashboard はここでは叩かない: 投稿 lock を取らない mode なので「実施済み」を
    # 判定できず、tick のたびに DB を push し直してしまう (容量 #153 / 上書き #70)。
    # ダッシュボードの再生成は runner の trigger_post (18:30) と夜間スイープ (export) が担う。
    # 優先順: 本体の予想 (post_predict) > 朝オッズ (odds_flash) > 結果 (results)
    SLOTS = (("post_predict", 1015, 1155),
             ("odds_flash", 930, 1100),
             ("results", 1730, 2400))
    for mode, start, end in SLOTS:
        if not (start <= hm < end):
            continue
        if mode in locked:
            print(f"   ⏭️ {mode} は lock 済み ({locked[mode].get('posted_at')}) → skip")
            continue
        if runner_started:
            # 起動した runner は1周目で trigger_post が同じ slot を叩く。ここでも叩くと
            # 2分差で同じ mode が2本走る (9/22 に odds_flash/post_predict で実測)。
            print(f"   ⏭️ {mode}: 今起動した runner の trigger_post に任せる (二重 dispatch 防止)")
            continue
        if sent_ap:
            print(f"   ⏭️ {mode}: この tick は auto_post_x を既に1本出した/待機中 → 次の tick に回す")
            continue
        if mode in ("odds_flash", "post_predict") and n_pred == 0:
            print(f"   ⏳ {mode}: 予測JSONが未生成 → 次の tick に回す")
            continue
        active = gh_runs_active("auto_post_x.yml", title=mode)
        if active != 0:
            print(f"   ⏳ {mode} は実行中/確認不能 ({active}) → 重ねない")
            continue
        print(f"🚨 開催日なのに {mode} が未実施 → dispatch")
        # results は日付を明示する: 0時前後に投げると auto_post_x 側で翌日 (非開催日) 扱いになり、
        # 「予測データがありません」で終わっていた (#161 review)。
        fields = {"date_override": day8} if mode == "results" else None
        if dispatch("auto_post_x.yml", mode, args.dry_run, fields):
            acted.append(mode)
            sent_ap = True

    # ── 4. 夜間の最終スイープ (遅出し配当・取りこぼし回収) ──────────────
    # collect_results.yml の cron は '6,0' 固定。#161: 旧版は日付を渡せず、同 workflow は
    # 月曜に「前日」を対象にしていたので、月曜開催の日は発火しても当日を拾えなかった。
    # 日付を明示して渡す。窓も 20:00 から日付が変わるまで連続させた。
    if 2000 <= hm < 2400:
        since = gh_minutes_since_last_run("collect_results.yml")
        if since < 0:
            print("   ⚠️ collect_results の実行履歴を確認できない → 見送り")
        elif since < 150:   # 1晩あたり最大2回 (1回ごとに DB を push するので打ち過ぎない)
            print(f"   ✅ collect_results は {since}分前に実行済み")
        else:
            print("🚨 開催日なのに夜間スイープが未実行 → collect_results を dispatch")
            if dispatch("collect_results.yml", None, args.dry_run, {"date": day8}):
                acted.append("collect_results")

    return _finish(alerts, acted, day_iso)


def _finish(alerts, acted, day_iso):
    print(f"\n📋 dispatch: {acted or 'なし'}")
    if alerts:
        # workflow 側がこのファイルを読んで Issue を立てる (#161: 旧版は常に exit 0 で、
        # 取りこぼしが一切通知されなかった — 9/21 の欠落は監査まで5日間誰も知らなかった)
        body = "\n".join(f"- {a}" for a in alerts)
        try:
            with open(os.environ.get("SAFETY_NET_ALERTS", "/tmp/safety_net_alerts.md"),
                      "w", encoding="utf-8") as f:
                f.write(f"{day_iso}\n{body}\n")
        except OSError:
            pass
        print(f"🔔 要対応:\n{body}")
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:   # #161: 例外で落ちても黙らない (旧版は run が緑のまま痕跡が残らなかった)
        import traceback
        traceback.print_exc()
        sys.exit(_finish([f"セーフティネットが例外で停止: {type(e).__name__}: {e}"], [],
                         jst_now().strftime("%Y-%m-%d")))
