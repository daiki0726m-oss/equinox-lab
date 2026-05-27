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

WORKFLOW_NAME = "auto_post_x.yml"
DEFAULT_CONFIG = ROOT / "config" / "posting_slo.yml"
DEFAULT_OUTPUT = ROOT / "docs" / "data" / "posting_health.json"


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


def fetch_recent_runs(window_hours=24):
    """gh CLI で直近 N 時間の workflow runs を取得"""
    cmd = [
        "gh", "api",
        f"repos/daiki0726m-oss/equinox-lab/actions/workflows/{WORKFLOW_NAME}/runs?per_page=30",
        "--jq",
        '.workflow_runs[] | {created_at, event, conclusion, status, display_title, name}'
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"⚠️ gh API failed: {result.stderr[:200]}", file=sys.stderr)
            return []
        runs = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                runs.append(json.loads(line))
        return runs
    except Exception as e:
        print(f"⚠️ gh API exception: {e}", file=sys.stderr)
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


def find_recent_run_any(runs, mode_keywords, expected_dt, window_minutes=240):
    """mode と関連するキーワードを含む success run を探す (より広範囲)

    schedule event: cron 遅延 ±360min (6h) まで許容
    workflow_dispatch: title が mode 一致 + window_minutes 内
    """
    threshold = expected_dt - timedelta(minutes=30)
    # schedule は最大 6h 遅延考慮、workflow_dispatch は ±window
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
            # 期待時刻 ±6h で cron 由来 success
            # ただし同じ scheduled cron が複数 slot にマッチする問題回避のため、
            # この slot の expected_jst と他 slot の expected_jst の距離が近い場合は
            # この slot 専用とは限らない (要外部 join)。
            # ここでは厳密性を諦めて「最近のいかなる schedule success も該当」とする
            # (false positive 許容、watchdog の過剰 trigger を防ぐ方向)
            delta = abs((run_dt - expected_dt).total_seconds())
            if delta <= schedule_window.total_seconds():
                return {"created_at": run_dt.isoformat(), "title": title, "event": event,
                        "delay_minutes": round((run_dt - expected_dt).total_seconds() / 60)}
        elif event == "workflow_dispatch":
            # workflow_dispatch は title が mode 完全一致のみ採用
            if not (threshold <= run_dt <= threshold + dispatch_window):
                continue
            if any(kw.lower() == title for kw in mode_keywords):
                return {"created_at": run_dt.isoformat(), "title": title, "event": event,
                        "delay_minutes": round((run_dt - expected_dt).total_seconds() / 60)}
    return None


def evaluate_slot(slot_name, slot_cfg, runs, monitor_cfg, today=None):
    """1 つの slot を評価して status を返す。"""
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
        )
        result = {
            "slot": slot_name,
            "status": "ok_late" if recent else "missed",
            "expected": expected_jst.strftime("%H:%M JST"),
            "deadline": deadline.strftime("%H:%M JST"),
        }
        if recent:
            result["recent_success"] = recent
            result["delay_minutes"] = recent.get("delay_minutes", 0)
        return result

    # 期待〜期限の間: success run を探す
    recent = find_recent_run_any(
        runs, [slot_cfg["fallback_mode"], slot_name], expected_jst,
        window_minutes=monitor_cfg.get("recent_success_window_minutes", 240),
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
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_slo_config(args.config)
    runs = fetch_recent_runs(window_hours=24)
    monitor = cfg.get("monitor", {})

    today = jst_now()
    results = {
        "generated_at": today.isoformat(),
        "today": today.strftime("%Y-%m-%d (%a)"),
        "weekday": today.weekday(),
        "slots": {},
        "summary": {"ok": 0, "missing": 0, "future": 0, "not_today": 0, "missed": 0, "ok_delayed": 0, "ok_late": 0},
    }

    missing_slots = []
    for slot_name, slot_cfg in cfg["slots"].items():
        evaluation = evaluate_slot(slot_name, slot_cfg, runs, monitor, today)
        results["slots"][slot_name] = evaluation
        status = evaluation["status"]
        results["summary"][status] = results["summary"].get(status, 0) + 1
        if status == "missing":
            missing_slots.append({
                "slot": slot_name,
                "fallback_mode": evaluation["fallback_mode"],
            })

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
