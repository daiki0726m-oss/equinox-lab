#!/usr/bin/env python3
"""投稿 SLO ヘルスチェック

config/posting_slo.yml を読み、各 slot について:
  - 今日「期待された」slot か (曜日 + 期限内)
  - 直近 N 時間 (config recent_success_window_minutes) に成功 run があるか
  - 期限を過ぎていないか

結果を docs/data/posting_health.json に出力し、
未投稿 slot リストを stdout (JSON) で返す → watchdog workflow がこれを読んで trigger。

Usage:
    python3 scripts/check_posting_health.py                    # 通常
    python3 scripts/check_posting_health.py --output PATH      # JSON 出力先
    python3 scripts/check_posting_health.py --json-only        # missing_slots JSON のみ出力
    python3 scripts/check_posting_health.py --dry-run          # gh CLI 呼ばない

Exit codes:
    0: 全 slot OK or 該当 slot なし
    1: 未投稿 slot あり (watchdog で trigger 推奨)
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# yaml 読み込み (PyYAML or 簡易代替)
try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))

WORKFLOW_NAME = "auto_post_x.yml"  # 投稿系のデフォルト
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = ROOT / "config" / "posting_slo.yml"
DEFAULT_OUTPUT = ROOT / "docs" / "data" / "posting_health.json"

# workflow ID マッピング (gh API には workflow file 名でアクセス)
WORKFLOW_IDS = {
    "auto_post_x.yml": 247415211,
    "fetch_weekend_races.yml": 267465142,
}


def load_slo_config(config_path):
    """SLO config を読み込む。PyYAML が無ければ簡易 parser で fallback。"""
    with open(config_path, encoding="utf-8") as f:
        content = f.read()
    if yaml:
        return yaml.safe_load(content)
    # 簡易 fallback (この project の YAML 構造専用)
    raise RuntimeError("PyYAML がインストールされていません: pip install pyyaml")


def jst_now():
    return datetime.now(JST)


def parse_jst_hhmm(hhmm_str, base_date=None):
    """'07:30' → datetime in JST"""
    base = base_date or jst_now()
    h, m = hhmm_str.split(":")
    return base.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def fetch_recent_runs(workflow_name=WORKFLOW_NAME, window_hours=24):
    """gh CLI で指定 workflow の直近 N 時間の runs を取得"""
    cmd = [
        "gh", "api",
        f"repos/daiki0726m-oss/equinox-lab/actions/workflows/{workflow_name}/runs?per_page=30",
        "--jq",
        '.workflow_runs[] | {created_at, event, conclusion, status, display_title, name}'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"⚠️ gh API failed ({workflow_name}): {result.stderr[:200]}", file=sys.stderr)
            return []
        runs = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                runs.append(json.loads(line))
        return runs
    except Exception as e:
        print(f"⚠️ gh API exception ({workflow_name}): {e}", file=sys.stderr)
        return []


def find_recent_success(runs, mode, expected_dt, window_minutes=240):
    """指定 mode の最新 success run を返す (なければ None)"""
    threshold = expected_dt - timedelta(minutes=30)  # 期待時刻の30分前以降
    window_end = expected_dt + timedelta(minutes=window_minutes)

    for run in runs:
        if run.get("conclusion") != "success":
            continue
        # display_title が mode と一致 (cmd_xxx タイプの判定)
        title = (run.get("display_title") or "").lower()
        if mode.lower() not in title:
            # cron で title=cron の場合は内部 type を見たいが API では取れない
            # cron 由来の success run は schedule event の場合は除外
            if run.get("event") != "schedule":
                continue
        utc_str = run.get("created_at", "")
        if not utc_str:
            continue
        run_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00")).astimezone(JST)
        if threshold <= run_dt <= window_end:
            return {"created_at": run_dt.isoformat(), "title": title, "event": run.get("event")}
    return None


def find_recent_run_any(runs, mode_keywords, expected_dt, window_minutes=240,
                         all_slot_expected_dts=None):
    """mode と関連するキーワードを含む success run を探す。

    BUG FIX (2026-05-28): cron success が「最近接 slot」にのみ割り当てられるよう改修。
    旧版は ±6h 内のすべての cron run をこの slot のものと誤認識していた
    (例: morning cron 08:44 が weekday 12:30 の cron success と誤認識)。

    schedule event: 期待時刻に最も近接した slot にのみ割り当てる (exclusive)
    workflow_dispatch: title が mode 完全一致 + window_minutes 内
    """
    threshold = expected_dt - timedelta(minutes=30)
    schedule_window = timedelta(minutes=max(window_minutes, 360))
    dispatch_window = timedelta(minutes=window_minutes)

    for run in runs:
        if run.get("conclusion") != "success":
            continue
        title = (run.get("display_title") or "").lower()
        event = run.get("event", "")
        utc_str = run.get("created_at", "")
        if not utc_str:
            continue
        run_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00")).astimezone(JST)

        if event == "schedule":
            this_delta = abs((run_dt - expected_dt).total_seconds())
            if this_delta > schedule_window.total_seconds():
                continue
            # この cron run が「他 slot」より「この slot」に近接か判定
            if all_slot_expected_dts:
                closest_dist = this_delta
                for other_dt in all_slot_expected_dts:
                    if other_dt == expected_dt:
                        continue
                    other_dist = abs((run_dt - other_dt).total_seconds())
                    if other_dist < closest_dist:
                        closest_dist = other_dist
                if closest_dist < this_delta:
                    # 他 slot の方がより近接 → この cron はこの slot のものでない
                    continue
            return {"created_at": run_dt.isoformat(), "title": title, "event": event,
                    "delay_minutes": round((run_dt - expected_dt).total_seconds() / 60)}
        elif event == "workflow_dispatch":
            if not (threshold <= run_dt <= threshold + dispatch_window):
                continue
            if any(kw.lower() == title for kw in mode_keywords):
                return {"created_at": run_dt.isoformat(), "title": title, "event": event,
                        "delay_minutes": round((run_dt - expected_dt).total_seconds() / 60)}
    return None


def evaluate_slot(slot_name, slot_cfg, runs, monitor_cfg, today=None, all_slots_today=None):
    """1 つの slot を評価して status を返す。

    all_slots_today: {slot_name: expected_dt} — cron-to-slot exclusive 割当に使う
    """
    today = today or jst_now()
    weekday = today.weekday()  # Mon=0 ... Sun=6
    days = slot_cfg.get("days", [])
    if weekday not in days:
        return {
            "slot": slot_name,
            "status": "not_today",
            "reason": f"今日 (weekday={weekday}) は対象外",
        }

    expected_jst = parse_jst_hhmm(slot_cfg["expected_jst"], today)
    deadline = parse_jst_hhmm(slot_cfg["watchdog_deadline_jst"], today)
    time_guard = slot_cfg["time_guard_jst"]
    now = today

    # all_slot_expected_dts: 他 slot との比較用 (exclusive assignment)
    other_dts = []
    if all_slots_today:
        other_dts = [v for k, v in all_slots_today.items() if k != slot_name]

    # 期待時刻前は何もしない
    if now < expected_jst:
        return {
            "slot": slot_name,
            "status": "future",
            "expected": expected_jst.strftime("%H:%M JST"),
        }

    # 期限後は noop (時間ガードで投稿不可)
    if now > deadline:
        # 期限後で見つからなければ「missed」
        recent = find_recent_run_any(
            runs, [slot_cfg["fallback_mode"], slot_name], expected_jst,
            window_minutes=monitor_cfg.get("recent_success_window_minutes", 240),
            all_slot_expected_dts=other_dts,
        )
        # #150c: 「期限を1分過ぎた瞬間に復旧を放棄する」のをやめる。
        # 旧実装は deadline 超過を一律 missed とし、posting_watchdog は
        # missing しか dispatch しないため、**その日はもう二度と自動復旧しなかった**。
        # 9/5 は 06:55 の watchdog が fetch 欠落を正しく検知しながら Issue を出しただけで、
        # 復旧したのは 07:02 に GitHub cron が2時間遅れで自力発火したからにすぎない。
        # 投稿を伴わない生成系 (データ取得・予測生成) には「遅すぎるからやめる」理由がない。
        # レースが始まるまでは何度でも作り直してよいので、missing として復旧対象に残す。
        _is_generation = (
            slot_cfg.get("recoverable_after_deadline")
            or slot_cfg.get("workflow", "").startswith("fetch_")
            or slot_name in ("weekend_morning_predict",)
        )
        _st = "ok_late" if recent else ("missing" if _is_generation else "missed")
        result = {
            "slot": slot_name,
            "status": _st,
            "expected": expected_jst.strftime("%H:%M JST"),
            "deadline": deadline.strftime("%H:%M JST"),
        }
        if _st == "missing":
            result["fallback_mode"] = slot_cfg["fallback_mode"]
            result["workflow"] = slot_cfg.get("workflow", "auto_post_x.yml")
            result["note"] = "deadline 超過だが生成系のため復旧を継続 (#150c)"
        
        if recent:
            result["recent_success"] = recent
            result["delay_minutes"] = recent.get("delay_minutes", 0)
        return result

    # 期待〜期限の間: success run を探す
    recent = find_recent_run_any(
        runs, [slot_cfg["fallback_mode"], slot_name], expected_jst,
        window_minutes=monitor_cfg.get("recent_success_window_minutes", 240),
        all_slot_expected_dts=other_dts,
    )
    if recent:
        delay_min = recent.get("delay_minutes", 0)
        return {
            "slot": slot_name,
            "status": "ok" if abs(delay_min) <= 60 else "ok_delayed",
            "expected": expected_jst.strftime("%H:%M JST"),
            "actual": recent["created_at"],
            "delay_minutes": delay_min,
            "event": recent["event"],
        }

    # success run が無い → 未投稿
    return {
        "slot": slot_name,
        "status": "missing",
        "expected": expected_jst.strftime("%H:%M JST"),
        "deadline": deadline.strftime("%H:%M JST"),
        "fallback_mode": slot_cfg["fallback_mode"],
        "action": f"gh workflow run auto_post_x.yml --ref main -f mode={slot_cfg['fallback_mode']}",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--json-only", action="store_true",
                    help="missing slots の JSON のみ stdout (watchdog 用)")
    ap.add_argument("--missed-json", action="store_true",
                    help="missed slots (期限切れ・復旧不能) の JSON のみ stdout (Issue 通知用)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_slo_config(args.config)
    posting_runs = fetch_recent_runs("auto_post_x.yml", window_hours=24)
    fetch_runs = fetch_recent_runs("fetch_weekend_races.yml", window_hours=24)
    monitor = cfg.get("monitor", {})

    today = jst_now()
    results = {
        "generated_at": today.isoformat(),
        "today": today.strftime("%Y-%m-%d (%a)"),
        "weekday": today.weekday(),
        "slots": {},
        "summary": {"ok": 0, "missing": 0, "future": 0, "not_today": 0, "missed": 0, "ok_delayed": 0, "ok_late": 0},
    }

    # 今日対象の全 slot の expected_dt を事前に集める (exclusive assignment 用)
    all_slots_today = {}
    weekday_now = today.weekday()
    for sn, sc in cfg["slots"].items():
        if weekday_now in sc.get("days", []):
            all_slots_today[sn] = parse_jst_hhmm(sc["expected_jst"], today)

    missing_slots = []
    missed_slots = []  # 期限切れ・復旧不能 (Issue 通知対象)
    # ① 投稿系 slot (auto_post_x.yml)
    for slot_name, slot_cfg in cfg["slots"].items():
        evaluation = evaluate_slot(slot_name, slot_cfg, posting_runs, monitor, today,
                                   all_slots_today=all_slots_today)
        evaluation["workflow"] = "auto_post_x.yml"
        results["slots"][slot_name] = evaluation
        status = evaluation["status"]
        results["summary"][status] = results["summary"].get(status, 0) + 1
        if status == "missing":
            missing_slots.append({
                "slot": slot_name,
                "workflow": "auto_post_x.yml",
                "fallback_mode": evaluation["fallback_mode"],
            })
        elif status == "missed":
            missed_slots.append({
                "slot": slot_name,
                "workflow": "auto_post_x.yml",
                "expected": evaluation.get("expected"),
                "deadline": evaluation.get("deadline"),
            })

    # ② データ収集系 slot (fetch_weekend_races.yml)
    fetch_slots_today = {}
    for sn, sc in cfg.get("data_fetch_slots", {}).items():
        if weekday_now in sc.get("days", []):
            fetch_slots_today[sn] = parse_jst_hhmm(sc["expected_jst"], today)

    for slot_name, slot_cfg in cfg.get("data_fetch_slots", {}).items():
        # data_fetch_slots は fallback_mode 無し (workflow_dispatch with no input)
        slot_cfg2 = dict(slot_cfg)
        slot_cfg2.setdefault("fallback_mode", "auto")  # ダミー
        slot_cfg2.setdefault("time_guard_jst", [0, 23])  # データ収集は時間ガード無し相当
        evaluation = evaluate_slot(slot_name, slot_cfg2, fetch_runs, monitor, today,
                                   all_slots_today=fetch_slots_today)
        evaluation["workflow"] = slot_cfg.get("workflow", "fetch_weekend_races.yml")
        results["slots"][slot_name] = evaluation
        status = evaluation["status"]
        results["summary"][status] = results["summary"].get(status, 0) + 1
        if status == "missing":
            missing_slots.append({
                "slot": slot_name,
                "workflow": slot_cfg.get("workflow", "fetch_weekend_races.yml"),
                "fallback_mode": None,
            })
        elif status == "missed":
            missed_slots.append({
                "slot": slot_name,
                "workflow": slot_cfg.get("workflow", "fetch_weekend_races.yml"),
                "expected": evaluation.get("expected"),
                "deadline": evaluation.get("deadline"),
            })

    # ── #150c: 成果物ゲート「ダッシュボードに今日の予想があるか」 ──
    # これが今まで **どこにも無かった**。2026-09-05 06:48 にユーザーが 404 を踏んだ瞬間、
    # health.json は missing=0 (異常なし) と報告していた。
    # SLO が「workflow が走ったか」しか見ておらず、「ユーザーが見るものがあるか」を
    # 見ていなかったのが原因。cron の発火状況に関係なく、実物を直接確認する。
    if today.weekday() in (5, 6):     # 土日 = 開催日
        _ds = today.strftime("%Y%m%d")
        _path = os.path.join(REPO_ROOT, "docs", "data", f"predictions_{_ds}.json")
        _n, _exported = 0, None
        try:
            with open(_path, encoding="utf-8") as _f:
                _j = json.load(_f)
            _n = int(_j.get("total_races") or 0)
            _exported = _j.get("exported_at") or _j.get("generated_at")
        except Exception:
            pass
        if _n < 1:
            _ev = {"slot": "dashboard_predictions", "status": "missing",
                   "expected": "開催日は常時", "deadline": "1R発走まで",
                   "fallback_mode": "predict", "workflow": "auto_post_x.yml",
                   "note": f"docs/data/predictions_{_ds}.json が無い/空 = ダッシュボードが404"}
            missing_slots.append({"slot": "dashboard_predictions",
                                  "workflow": "auto_post_x.yml",
                                  "fallback_mode": "predict"})
            results["summary"]["missing"] = results["summary"].get("missing", 0) + 1
        else:
            _ev = {"slot": "dashboard_predictions", "status": "ok",
                   "expected": "開催日は常時",
                   "actual": f"{_n}レース (exported_at={_exported})"}
            results["summary"]["ok"] = results["summary"].get("ok", 0) + 1
        results["slots"]["dashboard_predictions"] = _ev

    results["missed_slots"] = missed_slots

    # --missed-json: missed slot のみ出力 (Issue 通知用) して終了
    if args.missed_json:
        print(json.dumps(missed_slots, ensure_ascii=False))
        sys.exit(0)

    # 出力
    if not args.json_only:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        # 人間向け要約
        print(f"📊 投稿ヘルスチェック ({today.strftime('%Y-%m-%d %H:%M JST')})")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for slot_name, ev in results["slots"].items():
            icon = {
                "ok": "✅", "ok_delayed": "⚠️", "ok_late": "⚠️",
                "missing": "🚨", "missed": "❌",
                "future": "🕐", "not_today": "  ",
            }.get(ev["status"], "?")
            extra = ""
            if ev["status"] in ("ok", "ok_delayed", "ok_late"):
                extra = f" (delay {ev.get('delay_minutes', 0):+d}min)"
            elif ev["status"] == "missing":
                extra = f" — 期限 {ev.get('deadline')}"
            print(f"  {icon} {slot_name:25} status={ev['status']}{extra}")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"📝 結果保存: {args.output}")

    if missing_slots:
        if args.json_only:
            print(json.dumps(missing_slots, ensure_ascii=False))
        else:
            print(f"🚨 未投稿 slot: {len(missing_slots)} 件")
            for m in missing_slots:
                print(f"   - {m['slot']} → trigger mode={m['fallback_mode']}")
        sys.exit(1)
    else:
        if args.json_only:
            print("[]")
        sys.exit(0)


if __name__ == "__main__":
    main()
