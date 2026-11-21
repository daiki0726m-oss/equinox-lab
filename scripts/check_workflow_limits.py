#!/usr/bin/env python3
"""GitHub Actions が workflow ファイルごと拒否する条件を、push の前に機械で検査する (#174)。

2026-11-10 の #173 で race_day_runner.yml の Loop step の run ブロックが 24,949 字になり、
GitHub に「Exceeded max expression length 21000」で workflow ごと拒否された。PyYAML では
普通に読めるので手元の検査 (YAML の読み込み・bash -n) を全部通り、11/14 (土) の朝まで
結果収集が一度も起動しなかった (ensure_runner の dispatch も 422 で失敗)。

GitHub の規則: `${{ }}` を1つでも含む文字列は全体が式のテンプレートとして扱われ、
21,000 字を超えると workflow ファイルの解析に失敗する。式を含まない文字列には
この上限は無い (run ブロックでは `${{ github.repository }}` の代わりに
`$GITHUB_REPOSITORY` などの環境変数を使えば式にならない)。

使い方: python3 scripts/check_workflow_limits.py [--warn N]
  終了コード 0 = 問題なし / 1 = 上限超え (GitHub が拒否する) あり
  --warn: この字数を超えたら警告だけ出す (既定 18000。上限の手前で気づくため)
"""
import argparse
import glob
import os
import sys

import yaml

EXPR_MAX = 21000      # GitHub の式の最大長 (Exceeded max expression length 21000)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _walk(obj, path):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, path + [str(k)])
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, path + [str(i)])
    elif isinstance(obj, str):
        yield path, obj


def _label(doc, path):
    """jobs.<job>.steps.<i>.run → 'jobs.<job>.steps[<i>] "<step 名>".run' のように読める形にする。"""
    try:
        if len(path) >= 4 and path[0] == "jobs" and path[2] == "steps":
            step = doc["jobs"][path[1]]["steps"][int(path[3])]
            name = step.get("name") or step.get("uses") or ""
            return f'jobs.{path[1]}.steps[{path[3]}] "{name}".' + ".".join(path[4:])
    except Exception:
        pass
    return ".".join(path)


def check(paths, warn_at):
    errors, warns = [], []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception as e:
            errors.append(f"{p}: YAML を読めない ({e})")
            continue
        for path, s in _walk(doc, []):
            if "${{" not in s:
                continue
            n = len(s)
            where = f"{os.path.relpath(p, ROOT)}: {_label(doc, path)} ({n:,} 字、式あり)"
            if n > EXPR_MAX:
                errors.append(where + f" > 上限 {EXPR_MAX:,} 字 → GitHub が workflow ごと拒否する")
            elif n > warn_at:
                warns.append(where + f" — 上限 {EXPR_MAX:,} 字まで残り {EXPR_MAX - n:,} 字")
    return errors, warns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warn", type=int, default=18000)
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()
    files = a.files or sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))
                              + glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yaml")))
    errors, warns = check(files, a.warn)
    for w in warns:
        print(f"⚠️ {w}")
    for e in errors:
        print(f"❌ {e}")
    if errors:
        print("\n対処: run ブロックの `${{ ... }}` を環境変数 ($GITHUB_REPOSITORY など、または step の env:) に"
              "置き換えて式を無くすか、step を分ける (#174)")
        return 1
    print(f"✅ workflow {len(files)} 本: 式を含む文字列はすべて {EXPR_MAX:,} 字以内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
