"""
コース別過去統計モジュール
netkeibaから特定コースの過去6年分レース結果を取得し、
枠順・騎手・種牡馬・脚質等の統計を算出する。
平日投稿コンテンツの生成に使用。
"""

import re
import time
import json
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}
REQUEST_INTERVAL = 1.5

# 場コード
VENUE_CODES = {
    '札幌': '01', '函館': '02', '福島': '03', '新潟': '04',
    '東京': '05', '中山': '06', '中京': '07', '京都': '08',
    '阪神': '09', '小倉': '10',
}

CACHE_DIR = "docs/data"


def _get(url, params=None):
    """GETリクエスト"""
    time.sleep(REQUEST_INTERVAL)
    try:
        res = requests.get(url, params=params, headers=HEADERS, timeout=30)
        res.encoding = 'euc-jp'
        if res.status_code == 200:
            return res
    except Exception as e:
        print(f"❌ リクエストエラー: {e}")
    return None


def get_race_ids(venue, surface, distance, start_year=None, end_year=None, max_pages=3):
    """netkeibaの検索からrace_id一覧を取得

    Args:
        venue: 場名（例: '中山'）
        surface: '芝' or 'ダート'
        distance: 距離（例: 2000）
        start_year: 開始年（例: 2020）
        end_year: 終了年（例: 2025）
        max_pages: 最大ページ数
    Returns:
        list of race_id strings
    """
    if start_year is None:
        start_year = datetime.now().year - 6
    if end_year is None:
        end_year = datetime.now().year - 1

    venue_code = VENUE_CODES.get(venue)
    if not venue_code:
        print(f"⚠️ 不明な場名: {venue}")
        return []

    track_code = '1' if surface == '芝' else '2'

    all_race_ids = []
    for page in range(1, max_pages + 1):
        params = {
            'pid': 'race_list',
            'start_year': str(start_year),
            'end_year': str(end_year),
            'jyo[]': venue_code,
            'kyori_min': str(distance),
            'kyori_max': str(distance),
            'track[]': track_code,
            'sort': 'date',
            'list': '100',
            'page': str(page),
        }
        resp = _get('https://db.netkeiba.com/', params=params)
        if not resp:
            break

        soup = BeautifulSoup(resp.text, 'lxml')
        table = soup.find('table', summary='レース検索結果')
        if not table:
            break

        page_ids = []
        for a in table.find_all('a', href=True):
            m = re.search(r'/race/(\d{12})/', a['href'])
            if m and m.group(1) not in all_race_ids:
                page_ids.append(m.group(1))

        if not page_ids:
            break

        all_race_ids.extend(page_ids)
        print(f"  📄 ページ{page}: {len(page_ids)}レース取得")

        # 次ページがあるか
        pager = soup.find('div', class_='pager')
        if not pager or f'page={page + 1}' not in str(pager):
            break

    # 重複除去
    seen = set()
    unique = []
    for rid in all_race_ids:
        if rid not in seen:
            seen.add(rid)
            unique.append(rid)

    print(f"✅ {venue}{surface}{distance}m ({start_year}-{end_year}): {len(unique)}レース")
    return unique


def scrape_race_results(race_ids, max_races=80):
    """race_idリストからレース結果を取得

    Returns:
        list of dicts: [
            {
                'race_id': str,
                'finish': int, 'frame': int, 'number': int,
                'horse_name': str, 'jockey': str,
                'passing': str, 'last_3f': float,
                'odds': float, 'popularity': int,
                'sire': str,
            }, ...
        ]
    """
    all_results = []
    for i, race_id in enumerate(race_ids[:max_races]):
        if i > 0 and i % 10 == 0:
            print(f"  📊 {i}/{min(len(race_ids), max_races)}レース処理済み")

        resp = _get(f'https://db.netkeiba.com/race/{race_id}/')
        if not resp:
            continue

        soup = BeautifulSoup(resp.text, 'lxml')
        table = soup.find('table', summary='レース結果')
        if not table:
            continue

        rows = table.find_all('tr')
        for row in rows[1:]:
            tds = row.find_all('td')
            if len(tds) < 18:
                continue

            try:
                finish_text = tds[0].text.strip()
                if not finish_text.isdigit():
                    continue  # 除外・中止等

                finish = int(finish_text)
                frame = int(tds[1].text.strip()) if tds[1].text.strip().isdigit() else 0
                number = int(tds[2].text.strip()) if tds[2].text.strip().isdigit() else 0
                horse_name = tds[3].text.strip()
                jockey = tds[6].text.strip()
                passing = tds[14].text.strip()  # 通過順位
                last_3f_text = tds[15].text.strip()
                last_3f = float(last_3f_text) if last_3f_text else 0
                odds_text = tds[16].text.strip()
                odds = float(odds_text) if odds_text else 0
                pop_text = tds[17].text.strip()
                popularity = int(pop_text) if pop_text.isdigit() else 0

                # 馬ページリンクから種牡馬は後で取得（重いので省略）
                # 代わりに血統データはhorseリンクから取る
                horse_link = tds[3].find('a', href=True)
                horse_id = ''
                if horse_link:
                    m = re.search(r'/horse/(\w+)/', horse_link['href'])
                    if m:
                        horse_id = m.group(1)

                # 脚質判定（通過順位から）
                running_style = _judge_running_style(passing)

                all_results.append({
                    'race_id': race_id,
                    'finish': finish,
                    'frame': frame,
                    'number': number,
                    'horse_name': horse_name,
                    'horse_id': horse_id,
                    'jockey': jockey,
                    'passing': passing,
                    'last_3f': last_3f,
                    'odds': odds,
                    'popularity': popularity,
                    'running_style': running_style,
                })
            except (ValueError, IndexError):
                continue

    print(f"✅ {len(all_results)}頭分のデータ取得完了")
    return all_results


def _judge_running_style(passing):
    """通過順位から脚質を判定"""
    if not passing:
        return '不明'
    positions = [int(p) for p in passing.split('-') if p.isdigit()]
    if not positions:
        return '不明'

    first = positions[0]
    if first <= 2:
        return '逃げ' if first == 1 else '先行'
    elif first <= 5:
        return '先行'
    elif first <= 10:
        return '差し'
    else:
        return '追込'


# ═══════════════════════════════════════════
# 統計算出
# ═══════════════════════════════════════════

def calc_frame_stats(results):
    """枠順別成績"""
    frames = {}
    for r in results:
        f = r['frame']
        if f < 1 or f > 8:
            continue
        if f not in frames:
            frames[f] = {'runs': 0, 'wins': 0, 'top3': 0}
        frames[f]['runs'] += 1
        if r['finish'] == 1:
            frames[f]['wins'] += 1
        if r['finish'] <= 3:
            frames[f]['top3'] += 1

    stats = []
    for f in sorted(frames.keys()):
        d = frames[f]
        stats.append({
            'frame': f,
            'runs': d['runs'],
            'win_rate': round(d['wins'] / d['runs'] * 100, 1) if d['runs'] > 0 else 0,
            'top3_rate': round(d['top3'] / d['runs'] * 100, 1) if d['runs'] > 0 else 0,
        })
    return stats


def calc_jockey_stats(results, min_rides=3):
    """騎手別成績"""
    jockeys = {}
    for r in results:
        j = r['jockey']
        if not j:
            continue
        if j not in jockeys:
            jockeys[j] = {'runs': 0, 'wins': 0, 'top3': 0}
        jockeys[j]['runs'] += 1
        if r['finish'] == 1:
            jockeys[j]['wins'] += 1
        if r['finish'] <= 3:
            jockeys[j]['top3'] += 1

    stats = []
    for j, d in jockeys.items():
        if d['runs'] >= min_rides:
            stats.append({
                'jockey': j,
                'runs': d['runs'],
                'win_rate': round(d['wins'] / d['runs'] * 100, 1),
                'top3_rate': round(d['top3'] / d['runs'] * 100, 1),
            })
    stats.sort(key=lambda x: x['top3_rate'], reverse=True)
    return stats


def calc_running_style_stats(results):
    """脚質別成績"""
    styles = {}
    for r in results:
        s = r['running_style']
        if s == '不明':
            continue
        if s not in styles:
            styles[s] = {'runs': 0, 'wins': 0, 'top3': 0}
        styles[s]['runs'] += 1
        if r['finish'] == 1:
            styles[s]['wins'] += 1
        if r['finish'] <= 3:
            styles[s]['top3'] += 1

    stats = []
    for s in ['逃げ', '先行', '差し', '追込']:
        d = styles.get(s, {'runs': 0, 'wins': 0, 'top3': 0})
        if d['runs'] > 0:
            stats.append({
                'style': s,
                'runs': d['runs'],
                'win_rate': round(d['wins'] / d['runs'] * 100, 1),
                'top3_rate': round(d['top3'] / d['runs'] * 100, 1),
            })
    return stats


def calc_popularity_stats(results):
    """人気別成績"""
    pops = {}
    for r in results:
        p = r['popularity']
        if p < 1:
            continue
        # グループ化
        if p <= 3:
            label = '1-3人気'
        elif p <= 6:
            label = '4-6人気'
        elif p <= 9:
            label = '7-9人気'
        else:
            label = '10人気以下'

        if label not in pops:
            pops[label] = {'runs': 0, 'wins': 0, 'top3': 0, 'total_odds': 0}
        pops[label]['runs'] += 1
        if r['finish'] == 1:
            pops[label]['wins'] += 1
            pops[label]['total_odds'] += r['odds']
        if r['finish'] <= 3:
            pops[label]['top3'] += 1

    stats = []
    for label in ['1-3人気', '4-6人気', '7-9人気', '10人気以下']:
        d = pops.get(label)
        if d and d['runs'] > 0:
            # 単勝回収率
            recovery = round(d['total_odds'] * 100 / d['runs']) if d['runs'] > 0 else 0
            stats.append({
                'label': label,
                'runs': d['runs'],
                'win_rate': round(d['wins'] / d['runs'] * 100, 1),
                'top3_rate': round(d['top3'] / d['runs'] * 100, 1),
                'recovery': recovery,
            })
    return stats


def calc_last3f_stats(results):
    """上がり3F別成績"""
    # レースごとに上がり順位を計算
    race_groups = {}
    for r in results:
        rid = r['race_id']
        if rid not in race_groups:
            race_groups[rid] = []
        race_groups[rid].append(r)

    fast = {'runs': 0, 'wins': 0, 'top3': 0}  # 上がり1位
    top3_3f = {'runs': 0, 'wins': 0, 'top3': 0}  # 上がり3位以内

    for rid, horses in race_groups.items():
        sorted_by_3f = sorted([h for h in horses if h['last_3f'] > 0], key=lambda x: x['last_3f'])
        for i, h in enumerate(sorted_by_3f):
            if i == 0:
                fast['runs'] += 1
                if h['finish'] == 1:
                    fast['wins'] += 1
                if h['finish'] <= 3:
                    fast['top3'] += 1
            if i < 3:
                top3_3f['runs'] += 1
                if h['finish'] == 1:
                    top3_3f['wins'] += 1
                if h['finish'] <= 3:
                    top3_3f['top3'] += 1

    stats = []
    if fast['runs'] > 0:
        stats.append({
            'label': '上がり最速',
            'runs': fast['runs'],
            'win_rate': round(fast['wins'] / fast['runs'] * 100, 1),
            'top3_rate': round(fast['top3'] / fast['runs'] * 100, 1),
        })
    if top3_3f['runs'] > 0:
        stats.append({
            'label': '上がり3位以内',
            'runs': top3_3f['runs'],
            'win_rate': round(top3_3f['wins'] / top3_3f['runs'] * 100, 1),
            'top3_rate': round(top3_3f['top3'] / top3_3f['runs'] * 100, 1),
        })
    return stats


# ═══════════════════════════════════════════
# キャッシュ管理
# ═══════════════════════════════════════════

def _cache_key(venue, surface, distance):
    """キャッシュファイルのキー"""
    return f"course_stats_{venue}_{surface}_{distance}.json"


def _load_cache(cache_file):
    """キャッシュファイルを読み込む（存在しなければNone）"""
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return None


def get_course_stats(venue, surface, distance, force_refresh=False):
    """コース別統計を取得（キャッシュ付き）

    Returns:
        dict: {
            'venue', 'surface', 'distance',
            'start_year', 'end_year', 'total_races', 'total_horses',
            'frame_stats', 'jockey_stats', 'running_style_stats',
            'popularity_stats', 'last3f_stats',
            'cached_at': ISO timestamp,
        }
    """
    cache_file = os.path.join(CACHE_DIR, _cache_key(venue, surface, distance))
    cached = _load_cache(cache_file)

    # キャッシュ確認（30日以内なら再利用 — 過去6年の統計は頻繁に変わらない）
    if not force_refresh and cached:
        try:
            cached_at = datetime.fromisoformat(cached.get('cached_at', '2000-01-01'))
            if (datetime.now() - cached_at).days < 30:
                print(f"📦 キャッシュ利用: {venue}{surface}{distance}m")
                return cached
        except (ValueError, TypeError):
            pass

    # 新規取得を試みる
    print(f"🔍 {venue}{surface}{distance}m 過去6年データ取得開始...")
    now = datetime.now()
    start_year = now.year - 6
    end_year = now.year - 1

    race_ids = get_race_ids(venue, surface, distance, start_year, end_year)
    if not race_ids:
        # スクレイピング失敗 → 古いキャッシュがあればそれを返す
        if cached:
            print(f"⚠️ スクレイピング失敗。古いキャッシュを利用: {venue}{surface}{distance}m")
            return cached
        return None

    results = scrape_race_results(race_ids)
    if not results:
        # スクレイピング失敗 → 古いキャッシュがあればそれを返す
        if cached:
            print(f"⚠️ レース結果取得失敗。古いキャッシュを利用: {venue}{surface}{distance}m")
            return cached
        return None

    # 統計算出
    total_races = len(set(r['race_id'] for r in results))
    stats = {
        'venue': venue,
        'surface': surface,
        'distance': distance,
        'start_year': start_year,
        'end_year': end_year,
        'total_races': total_races,
        'total_horses': len(results),
        'frame_stats': calc_frame_stats(results),
        'jockey_stats': calc_jockey_stats(results),
        'running_style_stats': calc_running_style_stats(results),
        'popularity_stats': calc_popularity_stats(results),
        'last3f_stats': calc_last3f_stats(results),
        'cached_at': now.isoformat(),
    }

    # キャッシュ保存
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_file, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"💾 キャッシュ保存: {cache_file}")

    return stats


if __name__ == '__main__':
    import sys
    venue = sys.argv[1] if len(sys.argv) > 1 else '中山'
    surface = sys.argv[2] if len(sys.argv) > 2 else '芝'
    distance = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
    stats = get_course_stats(venue, surface, distance, force_refresh=True)
    if stats:
        print(f"\n{'='*50}")
        print(f"📊 {venue}{surface}{distance}m ({stats['start_year']}-{stats['end_year']})")
        print(f"   {stats['total_races']}レース / {stats['total_horses']}頭")
        print(f"\n【枠順別複勝率】")
        for f in stats['frame_stats']:
            bar = '█' * int(f['top3_rate'] / 5)
            print(f"  {f['frame']}枠: {f['top3_rate']:5.1f}% ({f['runs']}頭) {bar}")
        print(f"\n【脚質別勝率】")
        for s in stats['running_style_stats']:
            print(f"  {s['style']}: 勝率{s['win_rate']}% 複勝率{s['top3_rate']}% ({s['runs']}頭)")
        print(f"\n【騎手TOP5】")
        for j in stats['jockey_stats'][:5]:
            print(f"  {j['jockey']}: 複勝率{j['top3_rate']}% ({j['runs']}騎乗)")
        print(f"\n【人気別】")
        for p in stats['popularity_stats']:
            print(f"  {p['label']}: 勝率{p['win_rate']}% 複勝{p['top3_rate']}% 回収率{p['recovery']}%")
