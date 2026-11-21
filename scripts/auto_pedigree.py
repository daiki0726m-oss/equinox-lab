#!/usr/bin/env python3
"""血統データの自動補完スクリプト (cron/workflow から呼ぶ)。

直近の出走馬で 父/母/母父 のどれかが空の馬を抽出し、netkeiba ped.html から
scrape して DB を埋める。レース予測前に走らせることで、8軸スコアや
sire_course cross feature の精度を上げる。

対象は「最後に走った (走る) 日が新しい馬から」順に取る。
旧版は ORDER BY が無く、races の日付索引の順 = **古いレースから** 拾っていたため、
日曜の歴史モード (--from 2020-01-01 --max 4000) が 2020 年から順に消化し、
母 (#127 で取得開始) の空欄が「新しい馬ほど後回し」= 2026年の出走馬で 25-31% 残っていた。
また前方モードは「今日以降」しか見ないため、月曜祝日・順延開催 (#160) のように
土曜の実行後に登録されて走った馬は二度と拾われなかった → --back 日ぶん過去も見る。

**空の列だけ埋め、既にある父・母父は書き換えない** (collect_pedigree.save_pedigree の既定)。
旧版は取れた値で既存値も上書きしていたが、DB の父・母父には時期によって
「ナダル」と「ナダルNadal(米)」のような別表記が混在しており (母が空の馬の約半数)、
補完のついでに今の表記へ書き換えると sire_course_top3_rate などの集計単位が
「どの処理が先に走ったか」で変わる。backfill_horse_meta と同じ規則に揃えた。

Usage:
    python3 scripts/auto_pedigree.py            # デフォルト: 7日前〜7日先
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
from collect_pedigree import scrape_horse_pedigree, save_pedigree


def main():
    ap = argparse.ArgumentParser(description='出走馬の血統データを自動補完')
    ap.add_argument('--days', type=int, default=7,
                    help='今日から N 日先までの出走馬対象 (default: 7)')
    ap.add_argument('--back', type=int, default=7,
                    help='今日から N 日前までの出走馬も対象 (default: 7。月曜祝日・順延開催の取りこぼし対策)')
    ap.add_argument('--from', dest='date_from',
                    help='対象開始日 YYYY-MM-DD (--back より優先)')
    ap.add_argument('--max', type=int, default=2000,
                    help='1回の実行で補完する最大頭数 (default: 2000)')
    ap.add_argument('--db', default='keiba.db')
    args = ap.parse_args()

    today = date.today()
    if args.date_from:
        date_from = args.date_from
    else:
        date_from = (today - timedelta(days=max(args.back, 0))).isoformat()
    date_to = (today + timedelta(days=args.days)).isoformat()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # 新しい馬から順に (最後に走る/走った日の降順)。--max で打ち切られても、
    # 予測に効く今週・直近の馬が必ず先に埋まる。
    missing = conn.execute('''
        SELECT h.horse_id, h.horse_name, MAX(ra.race_date) AS last_date
        FROM results r
        JOIN races ra ON r.race_id = ra.race_id
        JOIN horses h ON r.horse_id = h.horse_id
        WHERE ra.race_date BETWEEN ? AND ?
          AND h.horse_id <> ''
          AND (h.sire IS NULL OR h.sire = '' OR h.damsire IS NULL OR h.damsire = ''
               OR h.dam IS NULL OR h.dam = '')
        GROUP BY h.horse_id
        ORDER BY last_date DESC, h.horse_id DESC
        LIMIT ?
    ''', (date_from, date_to, args.max)).fetchall()

    n = len(missing)
    if n == 0:
        print(f'✅ 血統欠落なし ({date_from} 〜 {date_to})')
        return

    print(f'🧬 血統補完開始: {n}頭 ({date_from} 〜 {date_to})')

    sess = requests.Session()
    sess.headers.update({'User-Agent': 'Mozilla/5.0'})

    saved = unchanged = failed = 0
    for i, row in enumerate(missing, 1):
        if i % 50 == 0:
            print(f'  進捗: {i}/{n} (保存{saved} / 変化なし{unchanged} / 失敗{failed})')
        try:
            p = scrape_horse_pedigree(sess, row['horse_id'])
            if not p:
                failed += 1
            # 空の列だけ埋める (既存の父・母父の表記は変えない)。#127: 取れなかった項目で既存値を消さない
            elif save_pedigree(conn, row['horse_id'], p):
                conn.commit()
                saved += 1
            else:
                unchanged += 1   # ページにも無い (例: 母の欄が空) = 埋められる値が無かった
        except Exception as ex:
            failed += 1
        time.sleep(0.3)  # netkeiba レート制限考慮

    print(f'✅ 完了: 保存 {saved} / 変化なし {unchanged} / 失敗 {failed} / 全 {n}')


if __name__ == '__main__':
    main()
