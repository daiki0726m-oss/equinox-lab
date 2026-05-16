#!/usr/bin/env python3
"""血統データの自動補完スクリプト (cron/workflow から呼ぶ)。

直近の出走馬で sire/damsire が空の馬を抽出し、netkeiba ped.html から
scrape して DB を埋める。レース予測前に走らせることで、8軸スコアや
sire_course cross feature の精度を上げる。

Usage:
    python3 scripts/auto_pedigree.py            # デフォルト: 7日先まで
    python3 scripts/auto_pedigree.py --days 14  # 14日先までの出走馬
    python3 scripts/auto_pedigree.py --from 2026-04-01  # 指定日以降
"""
import argparse
import sqlite3
import requests
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# プロジェクトルートを sys.path に追加
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from collect_pedigree import scrape_horse_pedigree


def main():
    ap = argparse.ArgumentParser(description='出走馬の血統データを自動補完')
    ap.add_argument('--days', type=int, default=7,
                    help='今日から N 日先までの出走馬対象 (default: 7)')
    ap.add_argument('--from', dest='date_from',
                    help='対象開始日 YYYY-MM-DD (--days より優先)')
    ap.add_argument('--max', type=int, default=2000,
                    help='1回の実行で補完する最大頭数 (default: 2000)')
    ap.add_argument('--db', default='keiba.db')
    args = ap.parse_args()

    today = date.today()
    if args.date_from:
        date_from = args.date_from
    else:
        date_from = today.isoformat()
    date_to = (today + timedelta(days=args.days)).isoformat()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    missing = conn.execute('''
        SELECT DISTINCT h.horse_id, h.horse_name
        FROM results r
        JOIN races ra ON r.race_id = ra.race_id
        LEFT JOIN horses h ON r.horse_id = h.horse_id
        WHERE ra.race_date BETWEEN ? AND ?
          AND (h.sire IS NULL OR h.sire = '' OR h.damsire IS NULL OR h.damsire = '')
        LIMIT ?
    ''', (date_from, date_to, args.max)).fetchall()

    n = len(missing)
    if n == 0:
        print(f'✅ 血統欠落なし ({date_from} 〜 {date_to})')
        return

    print(f'🧬 血統補完開始: {n}頭 ({date_from} 〜 {date_to})')

    sess = requests.Session()
    sess.headers.update({'User-Agent': 'Mozilla/5.0'})

    saved = failed = 0
    for i, row in enumerate(missing, 1):
        if i % 50 == 0:
            print(f'  進捗: {i}/{n} (保存{saved} / 失敗{failed})')
        try:
            p = scrape_horse_pedigree(sess, row['horse_id'])
            if p and (p.get('sire') or p.get('damsire')):
                conn.execute(
                    'UPDATE horses SET sire=?, damsire=?, dam=? WHERE horse_id=?',
                    (p.get('sire', ''), p.get('damsire', ''), p.get('dam', ''),
                     row['horse_id'])
                )
                conn.commit()
                saved += 1
            else:
                failed += 1
        except Exception as ex:
            failed += 1
        time.sleep(0.3)  # netkeiba レート制限考慮

    print(f'✅ 完了: 保存 {saved} / 失敗 {failed} / 全 {n}')


if __name__ == '__main__':
    main()
