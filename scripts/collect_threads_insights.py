#!/usr/bin/env python3
"""Threads 投稿の反応 (閲覧数・いいね等) を収集する (#131 2026-08-28)

背景: 予想の精度は6年分のデータで徹底的に検証してきたのに、**配信の効果は一度も
計測していなかった**。どの投稿が読まれたのか、どの型が刺さるのかが全く分からない
状態で「フォロワーが増えない」と悩んでいた。X が使えない今、Threads が主戦場
(ユーザー指示 2026-08-28) なのでまず Threads から計測を始める。

やること:
  1. docs/data/threads_posts.json (投稿時に記録した id × 型) を読む
  2. Threads Graph API で各投稿の insights (views/likes/replies/reposts/quotes) を取得
  3. docs/data/threads_insights.json に追記 (同じ post_id は最新値で更新)
  4. 型別・スロット別の集計をレポート出力

実行: python3 scripts/collect_threads_insights.py [--report-only]
必要: THREADS_ACCESS_TOKEN / THREADS_USER_ID (GitHub Secrets)
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTS = os.path.join(ROOT, "docs", "data", "threads_posts.json")
INSIGHTS = os.path.join(ROOT, "docs", "data", "threads_insights.json")
METRICS = "views,likes,replies,reposts,quotes"
API = "https://graph.threads.net/v1.0"


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _api(path, params, token):
    params = dict(params)
    params["access_token"] = token
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.load(r)
    except Exception as e:
        body = ""
        if hasattr(e, "read"):
            try:
                body = e.read().decode()[:200]
            except Exception:
                body = str(e)
        return {"__error": body or str(e)}


def fetch_insights(post_id, token):
    """1投稿の指標を取得。取れた metric だけ dict で返す。"""
    r = _api(f"{post_id}/insights", {"metric": METRICS}, token)
    if "__error" in r:
        return None, r["__error"]
    out = {}
    for m in r.get("data", []):
        name = m.get("name")
        val = m.get("values", [{}])[0].get("value")
        if name is not None and val is not None:
            out[name] = val
    return out, None


def report(insights, posts):
    """型別・スロット別の反応を集計して出力。"""
    meta = {p["post_id"]: p for p in posts}
    by_pattern = defaultdict(lambda: defaultdict(list))
    for pid, rec in insights.items():
        m = meta.get(pid, {})
        # チェーンの2投稿目以降は露出が構造的に落ちるので先頭のみ比較対象にする
        if m.get("chunk", 0) != 0:
            continue
        key = m.get("pattern") or m.get("slot") or "unknown"
        for k in ("views", "likes", "replies", "reposts", "quotes"):
            if rec.get(k) is not None:
                by_pattern[key][k].append(rec[k])
    if not by_pattern:
        print("📭 集計対象なし (投稿記録が貯まるまで待ち)")
        return
    print(f"\n{'型':<16}{'件数':>5}{'閲覧(中央)':>11}{'いいね(平均)':>13}{'返信':>7}")
    rows = []
    for key, mm in by_pattern.items():
        views = sorted(mm.get("views", []))
        n = len(views)
        med = views[n // 2] if n else 0
        likes = sum(mm.get("likes", [])) / max(len(mm.get("likes", [])), 1)
        repl = sum(mm.get("replies", []))
        rows.append((med, key, n, med, likes, repl))
    for _, key, n, med, likes, repl in sorted(rows, reverse=True):
        print(f"{key:<16}{n:>5}{med:>11,}{likes:>13.1f}{repl:>7}")
    print("\n※ 閲覧は中央値 (1本の伸びに引っ張られないため)。チェーン先頭のみ集計。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true", help="APIを叩かず既存データを集計")
    ap.add_argument("--max", type=int, default=60, help="1回に取得する投稿数の上限")
    args = ap.parse_args()

    posts = _load(POSTS, [])
    insights = _load(INSIGHTS, {})
    if not posts:
        print(f"📭 {POSTS} が空です (投稿が記録されるまで待ち)")
        return 0

    if not args.report_only:
        token = os.environ.get("THREADS_ACCESS_TOKEN", "")
        if not token:
            print("⚠️ THREADS_ACCESS_TOKEN 未設定 — 集計のみ実行します")
        else:
            # 新しい投稿から順に取得。24h 未満のものは数値が伸び続けるので毎回更新する。
            targets = [p["post_id"] for p in reversed(posts)][:args.max]
            ok = ng = 0
            for pid in targets:
                data, err = fetch_insights(pid, token)
                if data:
                    insights[pid] = data
                    ok += 1
                else:
                    ng += 1
                    if ng <= 3:
                        print(f"  ⚠️ {pid}: {err}")
                time.sleep(0.4)
            print(f"📈 取得 {ok}件 / 失敗 {ng}件")
            os.makedirs(os.path.dirname(INSIGHTS), exist_ok=True)
            with open(INSIGHTS, "w", encoding="utf-8") as f:
                json.dump(insights, f, ensure_ascii=False, indent=1)

    report(insights, posts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
