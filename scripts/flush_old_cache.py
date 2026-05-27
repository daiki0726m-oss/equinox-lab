#!/usr/bin/env python3
"""古い predictions_cache を flush して Phase α 適用後の logic で再生成

5/9-5/24 の cache は Phase α 補正 (体重・馬場・whitelist) が掛かっていない。
ROI weekly monitor が古い数字で集計してしまうため、再生成する。

戦略: 過去 race の predictions_cache を全削除 → 元の予測 pipeline は 1 race
あたり全データロード必要なので時間かかる。代替: 既存 cache の予測 JSON だけ
保持して confidence/should_bet を再計算する。

このスクリプトは「マークだけ stale にする」方針:
  - posted_at, should_bet, confidence を NULL に
  - all_bets_json を {} に
  - predictions_json は保持 (ML predict を rerun したくないため)

そして cmd_predict --force --date YYYYMMDD で再 predict させる。

Usage:
    python3 scripts/flush_old_cache.py [--from 2026-05-09] [--to 2026-05-24] [--dry-run]
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import get_db, init_db


def list_target_dates(date_from, date_to):
    """対象期間内の race_date リスト (cache 持ち)"""
    init_db()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT r.race_date
            FROM races r
            JOIN predictions_cache pc ON r.race_id = pc.race_id
            WHERE r.race_date BETWEEN ? AND ?
            ORDER BY r.race_date
        """, (date_from, date_to)).fetchall()
    return [r["race_date"] for r in rows]


def count_cache(date_from, date_to):
    with get_db() as conn:
        row = conn.execute("""
            SELECT COUNT(*) as c FROM predictions_cache pc
            JOIN races r ON pc.race_id = r.race_id
            WHERE r.race_date BETWEEN ? AND ?
        """, (date_from, date_to)).fetchone()
    return row["c"]


def flush_cache(date_from, date_to, dry_run=False):
    """cache の confidence/should_bet/all_bets_json をリセット"""
    n_before = count_cache(date_from, date_to)
    print(f"📊 対象 cache: {n_before} 件 ({date_from} - {date_to})")
    if dry_run:
        print("⚠️ dry-run mode: 変更しません")
        return
    with get_db() as conn:
        # confidence は NOT NULL なので placeholder "_STALE_" にする
        conn.execute("""
            UPDATE predictions_cache
            SET all_bets_json = '{}',
                confidence = '_STALE_',
                conf_reason = 'cache flushed - awaiting re-predict',
                should_bet = 0,
                bet_reason = 'stale, re-predict needed',
                posted_at = NULL
            WHERE race_id IN (
                SELECT race_id FROM races
                WHERE race_date BETWEEN ? AND ?
            )
        """, (date_from, date_to))
    print(f"✅ {n_before} 件の cache を flush 済 (predictions_json は保持)")


def repredict_dates(dates):
    """各 date で predict.py predict --date X --force を実行"""
    success, fail = 0, 0
    for d in dates:
        # YYYY-MM-DD → YYYYMMDD
        d_yyyymmdd = d.replace("-", "")
        print(f"\n🔄 re-predict {d}...")
        try:
            result = subprocess.run(
                ["python3", str(ROOT / "predict.py"), "predict", "--date", d_yyyymmdd, "--force"],
                capture_output=True, text=True, timeout=900, cwd=str(ROOT)
            )
            if result.returncode == 0:
                # 成否判定 (最後の行に集計あり)
                last_lines = result.stdout.strip().split("\n")[-10:]
                if any("予測完了" in ln or "完了サマリー" in ln for ln in last_lines):
                    success += 1
                    print(f"  ✅ {d}: 完了")
                else:
                    fail += 1
                    print(f"  ⚠️ {d}: 完了マーカ無し")
            else:
                fail += 1
                print(f"  ❌ {d}: returncode={result.returncode}")
                print(f"     stderr: {result.stderr[-200:]}")
        except Exception as e:
            fail += 1
            print(f"  ❌ {d}: {e}")
    print(f"\n📊 re-predict 結果: 成功 {success} / 失敗 {fail}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="2026-05-09")
    ap.add_argument("--to", dest="date_to", default="2026-05-24")
    ap.add_argument("--dry-run", action="store_true", help="実行せず対象のみ表示")
    ap.add_argument("--flush-only", action="store_true", help="flush のみ (re-predict なし)")
    args = ap.parse_args()

    dates = list_target_dates(args.date_from, args.date_to)
    print(f"📋 対象 date: {len(dates)} 日")
    for d in dates:
        print(f"  - {d}")

    if args.dry_run:
        flush_cache(args.date_from, args.date_to, dry_run=True)
        return

    # Step 1: flush
    flush_cache(args.date_from, args.date_to)

    # Step 2: re-predict (skip with --flush-only)
    if args.flush_only:
        print("\n⚠️ flush のみ実行。再生成は別途 'predict.py predict --date X --force' を各日実行")
        return

    print(f"\n🔄 各日 re-predict 開始 ({len(dates)} 日)...")
    repredict_dates(dates)


if __name__ == "__main__":
    main()
