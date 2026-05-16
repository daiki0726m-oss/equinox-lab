#!/usr/bin/env python3
"""記事執筆用: 予測 JSON から該当レースの ML 予測印を取得する。

「記事の印が ダッシュボード予測 と一致しない」というユーザー指摘への対応。
個別レース記事 (ヴィクトリアマイル後編など) を書く前にこのスクリプトを
実行して、予測 JSON 由来の印を取得する。

使い方:
    python3 scripts/get_marks.py --race-id 202605020811
    python3 scripts/get_marks.py --race-name "ヴィクトリアマイル" --date 20260517
    python3 scripts/get_marks.py --race-name "ヴィクトリアマイル"  # 直近の予測を検索

出力: 馬番順または印順で ◎○▲△×注 + 馬名 + 人気 + AI 勝率 を表示。
"""
import argparse
import json
import sys
from glob import glob


def find_race(race_id=None, race_name=None, date=None):
    """予測 JSON ファイルから該当レースを探す。

    date 指定なしなら直近 7 日分の predictions_*.json を新しい順に探索。
    """
    if date:
        pattern = f'docs/data/predictions_{date}.json'
    else:
        pattern = 'docs/data/predictions_2026*.json'
    files = sorted(glob(pattern), reverse=True)
    if not files:
        return None, None

    for path in files[:10]:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        for venue, races in data.get('venues', {}).items():
            for r in races:
                if race_id and r.get('race_id') == race_id:
                    return r, path
                if race_name and race_name in r.get('race_name', ''):
                    return r, path
    return None, None


def format_marks(race, by_mark=True):
    """印付き馬を整形して表示。

    by_mark=True: 印順 (◎○▲△×注 → 無印馬)
    by_mark=False: 馬番順
    """
    horses = race.get('horses', [])
    if not horses:
        return ''

    mark_order = {'◎': 1, '○': 2, '▲': 3, '△': 4, '×': 5, '注': 6}
    if by_mark:
        sorted_h = sorted(
            horses,
            key=lambda h: (mark_order.get(h.get('mark', ''), 99), h.get('horse_number', 99))
        )
    else:
        sorted_h = sorted(horses, key=lambda h: h.get('horse_number', 99))

    lines = []
    lines.append(f"=== {race.get('race_name')} ({race.get('venue')}{race.get('surface')}{race.get('distance')}m) ===")
    lines.append(f"race_id: {race.get('race_id')} / 出走 {race.get('horse_count', len(horses))} 頭")
    lines.append('')
    lines.append('馬番 | 印 | 馬名             | 人気 | AI勝率 | rank   | SI')
    lines.append('-' * 70)
    for h in sorted_h:
        mark = h.get('mark', '') or ' '
        num = h.get('horse_number', 0)
        name = (h.get('horse_name', '?'))[:14]
        pop = h.get('popularity', 0)
        pw = h.get('pred_win_pct', 0)
        rk = h.get('rank_score', 0)
        si = h.get('si_avg', 0)
        lines.append(f' {num:>2}  | {mark} | {name:<14} | {pop:>3}  |{pw:>5.1f}% | {rk:>+.2f} | {si:>5.1f}')
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser(description='予測 JSON から該当レースの印を取得')
    p.add_argument('--race-id', help='race_id (例: 202605020811)')
    p.add_argument('--race-name', help='レース名の部分一致 (例: ヴィクトリアマイル)')
    p.add_argument('--date', help='対象日 YYYYMMDD (省略時は直近)')
    p.add_argument('--by-num', action='store_true', help='馬番順で表示 (デフォルトは印順)')
    p.add_argument('--json', action='store_true', help='JSON 形式で出力 (スクリプト用)')
    args = p.parse_args()

    if not args.race_id and not args.race_name:
        p.error('--race-id か --race-name のどちらか必須')

    race, path = find_race(args.race_id, args.race_name, args.date)
    if not race:
        print('該当レース見つからず', file=sys.stderr)
        sys.exit(1)

    if args.json:
        # 印付き馬のみ
        marks = [
            {
                'mark': h.get('mark', ''),
                'num': h.get('horse_number'),
                'name': h.get('horse_name'),
                'pop': h.get('popularity'),
                'pred_win_pct': h.get('pred_win_pct'),
                'si_avg': h.get('si_avg'),
            }
            for h in race.get('horses', []) if h.get('mark')
        ]
        print(json.dumps({
            'race_id': race.get('race_id'),
            'race_name': race.get('race_name'),
            'source': path,
            'marks': marks,
        }, ensure_ascii=False, indent=2))
    else:
        print(format_marks(race, by_mark=not args.by_num))
        print(f"\n(source: {path})")


if __name__ == '__main__':
    main()
