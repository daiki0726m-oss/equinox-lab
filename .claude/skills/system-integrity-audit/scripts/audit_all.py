#!/usr/bin/env python3
"""5パス監査: callers / thresholds / docs / workflows / tests

Usage:
    python3 audit_all.py <changed_concept>
    例: python3 audit_all.py evaluate_from_horses
"""
import argparse
import json
import os
import re
import subprocess
import sys


def grep(pattern, paths=None, exts=None):
    """ripgrep wrapper. Returns list of (file, line_no, line)."""
    cmd = ["grep", "-rn", "-E", pattern]
    if paths:
        cmd.extend(paths)
    else:
        cmd.append(".")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        results = []
        for ln in out.stdout.splitlines():
            parts = ln.split(":", 2)
            if len(parts) >= 3:
                f, n, txt = parts
                if exts and not any(f.endswith(e) for e in exts):
                    continue
                # skip irrelevant paths
                if any(x in f for x in ["/.git/", "__pycache__", "/venv/", "/.venv/", "/node_modules/"]):
                    continue
                results.append((f, int(n), txt.strip()))
        return results
    except Exception as e:
        return []


def audit_callers(target):
    """Find callers of the changed function across repo."""
    print(f"## ① Callers grep: '{target}'")
    hits = grep(rf"\b{re.escape(target)}\b", paths=["."], exts=[".py"])
    if not hits:
        print(f"  ℹ️ 「{target}」の呼び出し箇所なし")
        return
    by_file = {}
    for f, n, line in hits:
        by_file.setdefault(f, []).append((n, line))
    for f, calls in by_file.items():
        print(f"\n  {f}:")
        for n, line in calls[:10]:
            print(f"    L{n}: {line[:100]}")
    print()


def audit_stale_thresholds():
    """Hard-coded thresholds が残ってないか — 信頼度系/閾値系の典型値"""
    print(f"## ② Stale threshold scan")
    paths = ["app.py", "post_x.py", "generate_note.py", "predict.py"]
    suspicious_patterns = [
        # 信頼度 v2/v3 の hard-coded 閾値
        r"honmei_win\s*>=\s*(?:50|35|30|22|20|15|12|10)",
        r"pred_win\s*>=\s*(?:50|35|30|22|20|15|12|10)",
        # WEIGHTS の hard-coded 旧値
        r"pop_score\s*[:=]\s*0\.(?:30|25)",
        r"size_score\s*[:=]\s*0\.15",
        # NORMS 残骸
        r"NORMS\s*=",
    ]
    found = False
    for pat in suspicious_patterns:
        hits = grep(pat, paths=[p for p in paths if os.path.exists(p)], exts=[".py"])
        for f, n, line in hits[:5]:
            print(f"  ⚠️ {f}:L{n} {line[:90]}")
            found = True
    if not found:
        print("  ✅ 古い hard-coded 閾値なし")
    print()


def audit_docs():
    """CLAUDE.md / README に古い記述が残ってないか"""
    print(f"## ③ Doc consistency")
    stale_phrases = [
        ("NORMS は ML 分布に依存", "v4 で ROI 期待値ベースに更新済 → 古い記述"),
        ("信頼度.*v2", "v4 に進化済"),
        ("土日朝5時 cron", "土日朝7時 (UTC 22時) に統合済"),
        ("印.*tweets\\[1\\]", "重複検出は「集計」キーワード判定に変更"),
        ("印 × 注 が「機能不全」", "穴馬スコアで改修済み"),
    ]
    if not os.path.exists("CLAUDE.md"):
        print("  ℹ️ CLAUDE.md なし")
        return
    with open("CLAUDE.md") as f:
        text = f.read()
    found = False
    for phrase, comment in stale_phrases:
        m = re.search(phrase, text)
        if m:
            # 「現状の記述」「更新済」等の注記があれば許容
            ctx_start = max(0, m.start() - 50)
            ctx_end = min(len(text), m.end() + 100)
            ctx = text[ctx_start:ctx_end]
            if "更新" not in ctx and "改修" not in ctx and "v4" not in ctx and "現在" not in ctx:
                print(f"  ⚠️ '{phrase[:30]}' 出現 → {comment}")
                found = True
    if not found:
        print("  ✅ CLAUDE.md は最新化されてる")
    print()


def audit_workflows():
    """GitHub Actions workflow の典型ミスをチェック"""
    print(f"## ④ Workflow chain check")
    wf_dir = ".github/workflows"
    if not os.path.isdir(wf_dir):
        print("  ℹ️ workflows dir なし")
        return
    findings = []
    for fname in sorted(os.listdir(wf_dir)):
        if not fname.endswith(".yml"): continue
        path = os.path.join(wf_dir, fname)
        with open(path) as f:
            content = f.read()
        # cache restore 直後に git checkout HEAD があるか
        if "actions/cache@v4" in content and "Restore" in content:
            if "git checkout HEAD -- keiba.db" not in content:
                findings.append(f"  ❌ {fname}: actions/cache restore 後の `git checkout HEAD -- keiba.db` 欠落 (#19 対策必須)")
        # cache key の run_id vs run_number
        if "github.run_id" in content:
            findings.append(f"  ⚠️ {fname}: cache key に run_id 使用 → run_number に統一推奨")
    if findings:
        for f in findings: print(f)
    else:
        print("  ✅ workflow チェーン整合")
    print()


def audit_tests():
    """テストカバレッジ — 変更を catch する test があるか"""
    print(f"## ⑤ Test coverage")
    test_dirs = ["tests", "scripts/backtest_v4_confidence.py"]
    found_tests = [p for p in test_dirs if os.path.exists(p)]
    if not found_tests:
        print("  ⚠️ tests/ も backtest_*.py もなし → regression 検知不能")
    else:
        for p in found_tests:
            print(f"  ✅ {p}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", default="evaluate_from_horses",
                    help="変更されたコンセプト名 (関数名・概念)")
    args = ap.parse_args()
    print(f"# システム整合性監査: target='{args.target}'\n")
    audit_callers(args.target)
    audit_stale_thresholds()
    audit_docs()
    audit_workflows()
    audit_tests()


if __name__ == "__main__":
    main()
