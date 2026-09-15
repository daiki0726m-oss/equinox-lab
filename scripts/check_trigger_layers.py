#!/usr/bin/env python3
"""起動層 (スケジューラ) の生存確認 (#155)

なぜ要るか
----------
このシステムの配信は4層で起動する (#63): ①GitHub cron (本体+冗長) /
②runner (起動後は自前の時計) / ③GAS (Google の時計) / ④watchdog。
このうち **GitHub スケジューラから故障モードが独立しているのは ③GAS だけ**。

その GAS が 2026-09-08 に停止し、**7日間誰も気づかなかった**。
原因は Fine-grained PAT の失効 (発火主体を日別に数えると 09/07 まで 3-5件/日 →
09/09 以降ゼロ)。#57 でも「GAS は死んでいるのに yml のコメントは “GAS がメイン” の
ままだった」という同型の事故が起きている。**外部層は黙って死ぬ**。

そこで「外部層が最後に発火したのはいつか」を毎日機械で見る。
投稿が止まってから気づくのでは遅い — GitHub cron が生きている限り配信は続くので、
外部層の死はユーザーに見えないまま次の障害まで潜伏する。

判定
----
workflow_dispatch の triggering_actor が `github-actions[bot]` 以外 = 外部層。
(GAS は PAT の持ち主のログイン名で発火する。手動 dispatch も同じ扱いになるが、
 これは生存確認なので「誰かが外から叩けている」ことが確認できれば十分)

実行: python3 scripts/check_trigger_layers.py [--days 3]
返り値: 0=OK / 1=外部層が沈黙 / 2=API に到達できない
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "trigger_health.json")
BOT = "github-actions[bot]"
REPO = os.environ.get("GITHUB_REPOSITORY", "daiki0726m-oss/equinox-lab")


def fetch_dispatches(pages=2):
    """直近の workflow_dispatch を (日時JST, actor) で返す。"""
    rows = []
    for page in range(1, pages + 1):
        try:
            out = subprocess.run(
                ["gh", "api",
                 f"repos/{REPO}/actions/runs?event=workflow_dispatch&per_page=100&page={page}",
                 "--jq", ".workflow_runs[] | \"\\(.created_at) \\(.triggering_actor.login)\""],
                capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if out.returncode != 0:
            return None
        for line in out.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            try:
                ts = datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc).astimezone(JST)
            except ValueError:
                continue
            rows.append((ts, parts[1]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3,
                    help="この日数のあいだ外部層の発火がゼロなら異常 (default 3)")
    args = ap.parse_args()

    rows = fetch_dispatches()
    if rows is None:
        print("⚠️ GitHub API に到達できず判定不能 (gh 未認証/レート制限)")
        return 2

    now = datetime.now(JST)
    ext = [(ts, a) for ts, a in rows if a != BOT]
    last_ext = max((ts for ts, _ in ext), default=None)
    recent = [ts for ts, _ in ext if (now - ts).days < args.days]

    # 直近14日の日別内訳 (人が読んで原因を推測できるように)
    daily = {}
    for ts, a in rows:
        if (now - ts).days >= 14:
            continue
        d = ts.strftime("%Y-%m-%d")
        daily.setdefault(d, {}).setdefault("外部" if a != BOT else "bot", 0)
        daily[d]["外部" if a != BOT else "bot"] += 1

    ok = bool(recent)
    silent_days = (now - last_ext).days if last_ext else None

    print(f"🔎 起動層の生存確認 ({now:%Y-%m-%d %H:%M JST})")
    for d in sorted(daily)[-14:]:
        print(f"   {d}: " + " / ".join(f"{k}={v}" for k, v in sorted(daily[d].items())))
    if ok:
        print(f"✅ 外部層 (GAS等) は生存: 直近{args.days}日で {len(recent)}件、"
              f"最終 {last_ext:%Y-%m-%d %H:%M JST}")
    else:
        print(f"❌ 外部層が {silent_days} 日沈黙しています "
              f"(最終 {last_ext:%Y-%m-%d %H:%M JST})" if last_ext
              else "❌ 外部層の発火が記録に一切ありません")
        print("   → GitHub スケジューラから独立した層が今ゼロです。")
        print("   → 最有力の原因は GAS の Fine-grained PAT 失効 (2026-09-08 の前例)。")
        print("      Apps Script の実行ログで 401 を確認 → PAT を再発行し")
        print("      Script Properties の GITHUB_TOKEN を更新 (docs/GAS_SETUP.md)。")

    payload = {
        "generated_at": now.isoformat(),
        "external_layer_ok": ok,
        "last_external_dispatch": last_ext.isoformat() if last_ext else None,
        "silent_days": silent_days,
        "window_days": args.days,
        "daily": daily,
    }
    try:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f"⚠️ {OUT} に書けませんでした: {e}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
