#!/usr/bin/env python3
"""
2026年の全レースでバッチ予測実行 → predictions_cache に蓄積
既にキャッシュがある日はスキップ
"""
import subprocess
import sys
from database import init_db, get_db

init_db()

with get_db() as conn:
    # 2026年で結果が確定している日を取得
    dates = conn.execute("""
        SELECT DISTINCT r.race_date
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.finish_position > 0
        WHERE r.race_date LIKE '2026-%'
        ORDER BY r.race_date
    """).fetchall()

    # 既にキャッシュがある日
    cached = conn.execute("""
        SELECT DISTINCT r.race_date
        FROM predictions_cache pc
        JOIN races r ON pc.race_id = r.race_id
        WHERE r.race_date LIKE '2026-%'
    """).fetchall()
    cached_set = {c['race_date'] for c in cached}

print(f"📊 2026年: {len(dates)}開催日, キャッシュ済み: {len(cached_set)}日")
print(f"   キャッシュ済: {sorted(cached_set)}")

todo = []
for d in dates:
    ds = d['race_date']
    if ds not in cached_set:
        # YYYY-MM-DD → YYYYMMDD 変換
        ds_compact = ds.replace('-', '')
        todo.append(ds_compact)

print(f"🔄 予測実行対象: {len(todo)}日")
for ds in todo:
    print(f"  → {ds}")

if not todo:
    print("✅ 全日キャッシュ済み")
    sys.exit(0)

print()
for i, ds in enumerate(todo):
    print(f"{'='*50}")
    print(f"🧠 [{i+1}/{len(todo)}] {ds} の予測を実行中...")
    print(f"{'='*50}")
    result = subprocess.run(
        [sys.executable, "predict.py", "predict", "--date", ds],
        cwd="/Users/daikimorimoto/Desktop/keiba",
        capture_output=False,
        timeout=300,
    )
    if result.returncode != 0:
        print(f"  ⚠️ {ds} でエラー (exit={result.returncode})")
    else:
        print(f"  ✅ {ds} 完了")
    print()

print("🎉 バッチ予測完了!")
