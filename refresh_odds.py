"""
📡 最新オッズ取得 & 馬場状態更新 & 予測キャッシュ更新スクリプト
10時のロック前に最新のリアルオッズ＋馬場状態を取得して予測に反映する

使い方:
  python refresh_odds.py --date 20260321
  python refresh_odds.py --no-track   (馬場更新をスキップ)
  python refresh_odds.py  (→ 当日分を自動判定)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

import re
import requests
from bs4 import BeautifulSoup
from database import init_db, get_db


JST = timezone(timedelta(hours=9))


def now_jst():
    return datetime.now(JST)


# netkeiba のオッズ API が返す status。'yoso' は **発売前の「予想オッズ」** で、
# 実際の投票に基づかない参考値。これを実オッズとして DB に書くと、
# 印 (☆妙味枠・注) も should_bet も EV も架空の数字で決まってしまう。
# #150c: 前夜 prefetch を入れたことで「発売前に予測を作る」経路ができたため、
# ここで弾かないと日曜の予想が予想オッズ由来になる (監査で実測: 土曜07:05 時点で
# 日曜レースは status='yoso'、08:45 には 'middle' に変わっていた)。
PROVISIONAL_ODDS_STATUS = {"yoso"}

# #171: 直近の API 応答の登録頭数 (touroku) と出走頭数 (shusso)。{race_id: (touroku, shusso)}
# 出走頭数が少なければ取消・除外がある。発売中の API は取消馬も 999.9倍・人気=頭数で返し、
# 票の少ない走る馬と値で区別できないので、どの馬かは出馬表の取消行で確かめる (refresh_odds)。
API_ENTRY_COUNTS = {}


def _api_entry_counts(data):
    """API 応答 data から (登録頭数, 出走頭数)。読めなければ None。"""
    try:
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            return None
        t, s = int(d.get("touroku") or 0), int(d.get("shusso") or 0)
        return (t, s) if t > 0 and s > 0 else None
    except (TypeError, ValueError):
        return None


def fetch_odds_from_api(race_id, allow_provisional=False):
    """netkeiba APIから単勝オッズを取得。

    発売前の「予想オッズ」(status='yoso') は既定で **返さない**。
    呼び出し側は空 dict を受け取り、predict のオッズ充足ゲート (#52) が
    「オッズ未取得」として当該レースを skip する = 架空オッズでの予測を防ぐ。
    """
    url = f"https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={race_id}&type=1&action=update"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://race.netkeiba.com/"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        _cnt = _api_entry_counts(data)
        if _cnt:
            API_ENTRY_COUNTS[race_id] = _cnt

        _st = str(data.get("status") or "")
        if _st in PROVISIONAL_ODDS_STATUS and not allow_provisional:
            print(f"  ⏭️ {race_id}: 発売前の予想オッズ (status={_st}) → 実オッズとして扱わない")
            return {}

        if data.get("status") and data.get("data", {}).get("odds"):
            odds_raw = data["data"]["odds"].get("1", {})
            result = {}
            for horse_num_str, vals in odds_raw.items():
                if isinstance(vals, list) and len(vals) >= 3:
                    odds_val = float(vals[0]) if vals[0] and vals[0] != '---.-' else 0
                    pop_val = int(vals[2]) if vals[2] else 0
                    result[int(horse_num_str)] = {
                        "odds_win": odds_val,
                        "popularity": pop_val,
                    }
            return result
    except Exception as e:
        print(f"  ⚠️ API取得エラー: {e}")
    return {}


def fetch_track_condition(race_id):
    """netkeibaの出馬表ページから、そのレースの馬場状態・天候を取得。

    戻り値: {"track_condition", "weather", "surface"} (取れたものだけ) / 何も取れなければ None。
    馬場はそのレースの芝ダの値 (障害は芝)。surface は scraper と同じ判定 (芝を含めば芝)。
    """
    url = f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://race.netkeiba.com/"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        # #169: scraper._get (#85) と同じく、サーバが Content-Type で宣言した charset を最優先。
        # 旧実装は resp.encoding="EUC-JP" を強制しており、netkeiba の UTF-8 化以降は
        # 文字化けで一度も値を取れていなかった (runner のログは毎回「⚠️ 馬場状態はまだ未発表」)。
        _ct = resp.headers.get("Content-Type", "")
        _m = re.search(r"charset=([\w-]+)", _ct, re.I)
        resp.encoding = _m.group(1) if _m else (resp.apparent_encoding or "utf-8")
        soup = BeautifulSoup(resp.text, "lxml")

        race_data01 = soup.find("div", class_="RaceData01") or soup.find("dl", class_="RaceData01")
        if not race_data01:
            return None

        # #169: 新旧両方の書式 (旧「天候:晴 / 馬場:稍」、新 アイコン + span.Item03/Item04 の
        # 1文字表記) を scraper と同じ関数で読み、DB の正規表記 (良/稍/重/不) に揃える。
        from race_meta import parse_weather_track
        weather, track = parse_weather_track(race_data01)
        text = race_data01.get_text()
        result = {}
        if weather:
            result["weather"] = weather
        if track:
            result["track_condition"] = track
        if result:
            result["surface"] = ("芝" if "芝" in text else "ダート" if "ダ" in text else "")

        return result if result else None
    except Exception as e:
        print(f"  ⚠️ 馬場状態取得エラー: {e}")
        return None


def refresh_odds(date_str, update_track=True):
    """指定日の全レースのオッズを取得してDB/キャッシュを更新"""
    init_db()

    # 日付フォーマット変換
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    with get_db() as conn:
        races = conn.execute("""
            SELECT race_id, race_name, venue, race_number, race_date, start_time
            FROM races
            WHERE race_date = ? OR race_date = ?
            ORDER BY venue, race_number
        """, (date_str, date_hyphen)).fetchall()

    if not races:
        print(f"❌ {date_str} のレースが見つかりません")
        return False

    print(f"📡 {len(races)}レースの最新オッズを取得中...")

    # 馬場状態の更新（開催場 × 芝ダ ごとに1回だけ取得）
    # #169: 旧実装は「場ごとに1レース取って、その場の全レースに同じ値を書く」だったため、
    # 芝とダートで馬場が違う日 (10/31 京都 芝=良 / ダ=重 等) にもう片方を誤った値で上書きし、
    # 確定済みレース (結果ページの値 = 正本) まで書き換える実装だった。EUC-JP 強制で一度も
    # 値を取れていなかったので表面化しなかったが、#169 で取れるようにしたので同時に直す:
    #   - 代表レースは 場×芝ダ ごとに「まだ着順が無い最初のレース」(障害は芝として扱う = DB と同じ)
    #   - 書き換えるのは 同じ場・同じ芝ダの、まだ着順が無いレースだけ (確定済みは触らない)
    #   - 天候が取れなかった時は既存の天候を残す
    if update_track:
        with get_db() as conn:
            _trows = conn.execute("""
                SELECT r.race_id, r.venue, r.surface FROM races r
                WHERE (r.race_date = ? OR r.race_date = ?)
                  AND NOT EXISTS (SELECT 1 FROM results x
                                  WHERE x.race_id = r.race_id AND x.finish_position > 0)
                ORDER BY r.venue, r.race_number
            """, (date_str, date_hyphen)).fetchall()
        groups = {}
        for r in _trows:
            if r['surface'] in ('芝', 'ダート'):
                groups.setdefault((r['venue'], r['surface']), r['race_id'])
        groups_updated = set()
        for (venue, surface), rid in groups.items():
            track_info = fetch_track_condition(rid)
            tc = (track_info or {}).get('track_condition', '')
            wt = (track_info or {}).get('weather', '')
            if tc and (track_info or {}).get('surface', surface) == surface:
                with get_db() as conn:
                    conn.execute("""
                        UPDATE races SET track_condition = ?,
                                         weather = COALESCE(NULLIF(?, ''), weather)
                        WHERE (race_date = ? OR race_date = ?) AND venue = ? AND surface = ?
                          AND NOT EXISTS (SELECT 1 FROM results x
                                          WHERE x.race_id = races.race_id AND x.finish_position > 0)
                    """, (tc, wt, date_str, date_hyphen, venue, surface))
                print(f"  🏇 {venue}{surface}: 馬場={tc} 天候={wt or '(取得できず・既存値を維持)'}")
                groups_updated.add((venue, surface))
            time.sleep(0.5)
        if groups_updated:
            print(f"  ✅ {len(groups_updated)}件 (場×芝ダ) の馬場状態を更新")
        elif groups:
            print(f"  ⚠️ 馬場状態はまだ未発表")
        else:
            print(f"  ℹ️ 着順未確定のレースが無い — 馬場状態の更新は不要")
        print()

    updated = 0
    for race in races:
        rid = race['race_id']
        odds = fetch_odds_from_api(rid)

        if not odds:
            continue

        # #171: 取消・除外の照合。API の出走頭数が登録頭数より少ない時と、999.9倍の馬がいる時
        # (発売中の API は取消馬を 999.9倍・人気=頭数で返し、票の少ない走る馬と区別できない) に
        # 出馬表の取消行を読み、その馬に SCRATCH_SENTINEL (999.9倍・人気9999) を書く。取消の判定は
        # 人気>=999 で行う (999.9倍だけでは取消にしない — 前日発売の直後は走る馬も 999.9倍になる)。
        # 出馬表が読めない時は何もしない (既存の値のまま。分からないものを取消にしない)。
        scratched_now = set()
        _cancel_checked = False   # 出馬表で取消の有無を確かめたか (確かめた馬は目印を外してよい)
        _cnt = API_ENTRY_COUNTS.get(rid)
        _has_cap = any((o.get("odds_win") or 0) >= 999.9 for o in odds.values())
        # 既に取消の目印・フラグが付いている馬がいる時も照合する (前の版の誤ったフラグを外すため。
        # 11/06 19:05 の前夜予想が立てた94頭のフラグは、票が増えて 999.9倍でなくなると照合の
        # きっかけが無くなり、予測を作り直すまで残っていた)
        _has_mark = False
        try:
            with get_db() as _mc:
                _has_mark = bool(_mc.execute(
                    "SELECT 1 FROM results WHERE race_id = ? AND finish_position = 0 "
                    "AND COALESCE(popularity, 0) >= 999 LIMIT 1", (rid,)).fetchone())
                if not _has_mark:
                    _pc = _mc.execute("SELECT predictions_json FROM predictions_cache WHERE race_id = ?",
                                      (rid,)).fetchone()
                    if _pc and _pc['predictions_json']:
                        _has_mark = any(q.get('scratched') for q in json.loads(_pc['predictions_json']))
        except Exception:
            _has_mark = False
        if (_cnt and _cnt[1] < _cnt[0]) or _has_cap or _has_mark:
            try:
                from scraper import NetkeibaScraper
                _cn = NetkeibaScraper().scrape_cancelled_numbers(rid)
            except Exception as e:
                print(f"  ⚠️ {rid}: 出馬表の取消行を読めない ({e}) → 取消の照合をしない")
                _cn = None
            if _cn is not None:
                scratched_now = set(_cn)
                _want = (_cnt[0] - _cnt[1]) if _cnt else len(scratched_now)
                if len(scratched_now) != _want:
                    # 数が合わない時は、読めた取消に目印を付けるだけにし、既存の目印は外さない
                    # (出馬表の読み落としで本当の取消の目印を消さない)
                    print(f"  ⚠️ {rid}: 取消・除外の数が合わない (API: 登録{_cnt[0]}・出走{_cnt[1]} "
                          f"= {_want}頭 / 出馬表の取消行 {len(scratched_now)}頭 {sorted(scratched_now)}) "
                          f"→ 既存の取消の目印は残す")
                else:
                    _cancel_checked = True
                    if scratched_now:
                        print(f"  🚫 {rid}: 取消・除外 {sorted(scratched_now)} (出馬表で確認)")

        with get_db() as conn:
            # DBのresultsテーブルを更新
            # #149: netkeiba のオッズAPIは**出走取消の馬に odds='-3.0' / popularity=9999**
            # を返す。旧実装は検証せず保存していたため、公開JSONに
            # 「◎ -3.0倍 9999人気」がそのまま出ていた (8/30 新潟11R の◎が実際に該当)。
            # results に odds<0 が58行 / popularity>=9999 が58行 蓄積済み。
            # sync_github_pages.py:68 と app.py:76 は既に `if ov > 0` で弾いており、
            # 本番に配線されている refresh_odds だけがガード漏れだった。
            from race_utils import SCRATCH_SENTINEL
            for horse_num in scratched_now:
                conn.execute("""
                    UPDATE results SET odds = ?, popularity = ?
                    WHERE race_id = ? AND horse_number = ? AND finish_position = 0
                """, (SCRATCH_SENTINEL[0], SCRATCH_SENTINEL[1], rid, horse_num))
            for horse_num, o in odds.items():
                if horse_num in scratched_now:
                    continue          # #171: 取消と確かめた馬は上で目印を書いた
                _ov = o.get("odds_win") or 0
                _pv = o.get("popularity") or 0
                if _ov <= 0 or _pv >= 999:
                    continue          # 取消馬 — 既存の値を壊さず素通り
                # #171: 取消の目印 (人気9999) が付いた行は、API の 999.9倍・人気=頭数で上書きしない
                # (発売中の API は取消馬もその値で返す)。目印を外すのは出馬表で取消でないと
                # 確かめた時だけ (このレースで照合した時 / scraper.save_race_to_db)。
                conn.execute("""
                    UPDATE results SET odds = ?, popularity = ?
                    WHERE race_id = ? AND horse_number = ?
                      AND (? OR NOT (COALESCE(popularity, 0) >= 999 AND finish_position = 0))
                """, (_ov, _pv, rid, horse_num, 1 if _cancel_checked else 0))

            # predictions_cache を更新
            # #99: seal 済み (posted_at = 投稿済みの予測) と発走済みレースは書き換えない。
            # 旧実装は終日 (完走後も) 最終オッズで上書きし続けたため、cache の odds_win の
            # 89.6% が最終オッズと一致 = 「投稿時点の予測の記録」が事後情報で汚染され、
            # #98/#99 のバックテストが look-ahead で誤誘導される事故が実際に起きた。
            # DB results のオッズ更新 (上) は継続 (集計・結果処理の正本)。cache は
            # 「予測時点のスナップショット」として不変性を守る。
            cache = conn.execute(
                "SELECT predictions_json, posted_at FROM predictions_cache WHERE race_id = ?",
                (rid,)
            ).fetchone()

            # #169: DB の posted_at だけでは凍結が効かない (並行 push で seal が消え、
            # 10/31・11/01 は配信レースの cache オッズが投稿時 0/139 頭一致 = 発走直前の値に
            # なっていた)。投稿記録サイドカー (posted_marks_YYYYMMDD.json) に載っていれば凍結。
            from database import posted_race_ids
            _sealed = bool(cache and cache['posted_at']) or rid in posted_race_ids(date_str)
            _started = False
            try:
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                _now = _dt.now(_tz(_td(hours=9)))
                _today = _now.strftime('%Y-%m-%d')
                _rd = str(race['race_date'] if 'race_date' in race.keys() else '')[:10]
                _st = race['start_time'] if 'start_time' in race.keys() else None
                if not _rd:
                    # #141: 日付が取れない = 発走済みか判定できない。
                    # 判定できない時は「凍結する側」に倒す (cache を壊さない方が安全)。
                    # 旧実装は SELECT に race_date が無く常にここを通らず _started=False =
                    # 全レースで上書き許可になっていた (#99 の凍結が半分死んでいた真因)。
                    _started = True
                elif _today > _rd:
                    _started = True          # 過去日のレースは常に発走済み
                elif _today == _rd and _st and _now.strftime('%H:%M') >= str(_st):
                    _started = True          # 当日で発走時刻を過ぎた
            except Exception:
                _started = True              # #141: 例外時も凍結側 (旧: False=上書き許可)

            if cache and not _sealed and not _started:
                preds = json.loads(cache['predictions_json'])
                for p in preds:
                    hn = p['horse_number']
                    if hn in scratched_now:
                        # #171: 予測の後に分かった取消。export・投稿・ダッシュボードが見る scratched を立てる
                        p['scratched'] = True
                        continue
                    if p.get('scratched'):
                        if not _cancel_checked:
                            continue  # #171: 取消と分かっていた馬の値は API で上書きしない
                        # #171: 出馬表で取消でないと確かめた (取消行の数も API と一致)。前の版の誤った
                        # 取消フラグを外す (11/06 19:05 の前夜予想は票の少ない 999.9倍の94頭に立てていた)
                        p['scratched'] = False
                    if hn in odds:
                        # #149: 取消馬の番兵値 (-3.0 / 9999) で cache を壊さない
                        _o = odds[hn].get("odds_win") or 0
                        _p = odds[hn].get("popularity") or 0
                        if _o > 0 and _p < 999:
                            p['odds_win'] = _o
                            p['popularity'] = _p

                conn.execute("""
                    UPDATE predictions_cache SET predictions_json = ?
                    WHERE race_id = ?
                """, (json.dumps(preds, ensure_ascii=False), rid))
            elif cache:
                pass  # sealed/発走済み → cache は凍結 (results 側のみ更新)

        updated += 1
        rn = race['race_number']
        if rn in (9, 10, 11):
            sample = list(odds.items())[:2]
            s_txt = ", ".join([f"{k}番={v['odds_win']}倍({v['popularity']}人気)" for k, v in sample])
            print(f"  ✅ {race['venue']}R{rn} {race['race_name']}: {s_txt}")

        time.sleep(0.3)

    print(f"\n✅ {updated}/{len(races)}レースのオッズを更新完了")
    return True


def main():
    parser = argparse.ArgumentParser(description="📡 最新オッズ＋馬場状態取得")
    parser.add_argument("--date", help="対象日 (YYYYMMDD, デフォルト=当日)")
    parser.add_argument("--no-track", action="store_true", help="馬場状態の更新をスキップ")
    args = parser.parse_args()

    if args.date:
        date_str = args.date
    else:
        date_str = now_jst().strftime("%Y%m%d")

    print(f"🔄 {date_str} のオッズ＋馬場状態を更新します\n")
    refresh_odds(date_str, update_track=not args.no_track)


if __name__ == "__main__":
    main()
