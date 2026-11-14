#!/usr/bin/env python3
"""GitHub に workflow ファイルごと拒否されている workflow を見つける (#174)。

このリポジトリの workflow はどれも `on: push` を持たない。それでも push の後に
event=push の失敗 run が出るのは、GitHub が workflow ファイルを解析できなかった時だけ
(画面には「This run likely failed because of a workflow file issue」と出る)。
拒否された workflow は cron も dispatch も一切起動しない (dispatch は HTTP 422)。

2026-11-10 に race_day_runner.yml が式の長さ上限で拒否され、11/14 (土) の朝まで
4日間だれも気づかなかった (結果収集が一度も起動しなかった)。この検査は
「その workflow の最新の run が event=push の失敗」= いまも拒否されている、を見る。
その後に直して別の run が走っていれば (cron・dispatch)、もう拒否されていない。

使い方: python3 scripts/check_workflow_rejections.py
  終了コード 0 = 拒否されている workflow なし / 1 = あり / 2 = 判定不能 (API 失敗)
"""
import json
import os
import subprocess
import sys

REPO = os.environ.get("GITHUB_REPOSITORY") or "daiki0726m-oss/equinox-lab"


def gh_json(path):
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return json.loads(r.stdout)


def main():
    try:
        pushes = gh_json(f"repos/{REPO}/actions/runs?event=push&per_page=50").get("workflow_runs") or []
    except Exception as e:
        print(f"⚠️ run 一覧を取得できない ({e}) → 判定不能")
        return 2
    seen, rejected = set(), []
    for run in pushes:
        path = run.get("path") or ""
        if not path.startswith(".github/workflows/") or path in seen:
            continue
        seen.add(path)
        wf = os.path.basename(path)
        try:
            latest = (gh_json(f"repos/{REPO}/actions/workflows/{wf}/runs?per_page=1")
                      .get("workflow_runs") or [None])[0]
        except Exception as e:
            print(f"⚠️ {wf}: 最新 run を取得できない ({e})")
            continue
        if not latest:
            continue
        if latest.get("event") == "push" and latest.get("conclusion") == "failure":
            rejected.append((wf, latest.get("created_at", ""), latest.get("html_url", ""),
                             latest.get("head_sha", "")[:8]))
    if not rejected:
        print("✅ GitHub に拒否されている workflow はありません")
        return 0
    for wf, at, url, sha in rejected:
        print(f"❌ {wf}: {at} の push ({sha}) 以降、GitHub が workflow ファイルを解析できず拒否中 "
              f"(cron も dispatch も起動しない) {url}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
