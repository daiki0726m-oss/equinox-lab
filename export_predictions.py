#!/usr/bin/env python3
"""
predictions_cache → docs/data/predictions_{date}.json に書き出し
GitHub Pages用の静的データ生成スクリプト
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from database import get_db
from race_utils import is_scratched_horse, is_scratch_values, prediction_locked_for

JST = timezone(timedelta(hours=9))


def scratched_for_export(h, db_r, race_finished=False):
    """公開 JSON の馬が取消と分かっているか (#170)。判定は race_utils と共通。

    - cache の値: scratched フラグ (#169 の予測時に分かった取消・#171 で refresh_odds が後から
      立てた取消) / 番兵 9999人気 / 負のオッズ
      (cache の正の odds_win は実オッズの無い日の推定値 0.8/勝率 でありうるので見ない)
    - DB (results) の値: 出馬表の取消行・API の照合で書いた人気 9999 (#171) / 古い行の番兵
    - race_finished (1着が出た = 確定したレース) の時だけ、DB の 999.9倍も取消とみなす。#171 より前の取消馬は
      results に 999.9倍・人気=頭数で入っており (9/05〜11/01 の17頭)、着順の無い 999.9倍は取消。
      発走前の 999.9倍は票の少ない走る馬でもありうるので取消にしない (11/06 前夜予想の94頭)。
      着順のある馬はこの関数の外 (呼び出し側) で取消から外す。
    """
    if is_scratched_horse({**h, "_has_real_odds": False}):
        return True
    try:
        if float(h.get("odds_win") or 0) < 0:
            return True
    except (TypeError, ValueError):
        pass
    if not db_r:
        return False
    if is_scratch_values(db_r["odds"], db_r["popularity"]):
        return True
    try:
        return bool(race_finished) and float(db_r["odds"] or 0) >= 999.9
    except (TypeError, ValueError):
        return False


# #173: 確定したレースで着順の無い馬 (finish_position=0) の表示名。
# 着順0 は「走っていない (取消・除外 = 返還)」と「走った (競走中止・失格 = 外れ)」の両方を含む。
UNPLACED_LABEL = {"scratched": "取消", "excluded": "除外", "dnf": "中止", "dq": "失格",
                  "unknown": "除外/中止"}
# 走った (外れとして集計の分母に入れる) 分類
UNPLACED_RAN = ("dnf", "dq")


def unplaced_status(h, db_r, race_confirmed):
    """確定したレースで着順の無い馬が走ったか (#173)。着順のある馬は呼ばない。

    戻り値:
      'scratched' : 取消の目印がある (= 走っていない・返還)。判定は scratched_for_export と同じ
                    (#170/#171: cache の scratched・人気9999・負のオッズ、確定レースの 999.9倍)
      'dnf'       : 目印が無く、通過順 (results.passing_order) がある = 走って着順が付かなかった
                    (競走中止)。外れとして集計の分母に入れる
      'dq'        : 'dnf' の条件に加えて走破時計もある = 完走した上で着順が付かなかった (失格)。
                    外れとして分母に入れる。2020〜2026 で1頭 (2020-11-07 東京4R ソーヴァリアント、
                    netkeiba の着順欄「失」) だけで、走破時計のある着順0 はこの1頭以外いない
      'excluded'  : 競走除外 (results.finish_status='除'。走っていない = 返還)
      'unknown'   : どちらでもない = 競走除外か、最初の通過地点の前に止まった競走中止。
                    finish_status (#173 で追加。netkeiba の着順欄の先頭1字) が入る前に収集した
                    レースは区別する材料が無い (旧 scraper は着順欄の「中」「除」を 0 にして捨てていた)
      None        : レースが未確定 (1着がいない) — 発走直後の部分取得で走った馬を誤判定しない (#150e)

    11/07・08 の着順0 は5頭 (取消の目印なし)。4頭は通過順があり走っている。netkeiba で確かめた
    3頭 (11/08 福島4R の2頭・11/07 京都JS グラニットピーク = 通過順なし) はすべて「中」(競走中止)
    で、旧表示「取消/出走取消・除外」は誤りだった。通過順の無い馬は「中」と「除」が混ざる
    (10/24 京都7R リーチグローリー = 除、10/12 東京4R パワーホール = 中) ので、'unknown' を
    「除外」と言い切らない。
    目印と通過順の両方を持つ馬は 2020 年以降 0頭 (矛盾は観測されていない)。目印を先に見るので
    #171 の取消判定 (export の scratched・ダッシュボードの isScratched) とは必ず排他になる。
    """
    if not race_confirmed:
        return None
    # #173: netkeiba の着順欄が残っていればそれが正本 (中=競走中止 / 失=失格 / 除=競走除外 / 取=出走取消)。
    # 取消の目印より先に見る: 999.9倍 (票の少ない大穴) で走って競走中止した馬を、確定後の 999.9倍の
    # 取消判定 (#171) で「取消」と誤らないため。着順欄の無い古い行は従来どおり目印 → 通過順の順に見る
    fs = ""
    if db_r is not None:
        try:
            fs = str(db_r["finish_status"] or "").strip()[:1]
        except (IndexError, KeyError):
            fs = ""
    if fs in ("中", "失", "除", "取"):
        return {"中": "dnf", "失": "dq", "除": "excluded", "取": "scratched"}[fs]
    if scratched_for_export(h or {}, db_r, race_finished=True):
        return "scratched"
    po = ""
    timed = False
    if db_r is not None:
        try:
            po = str(db_r["passing_order"] or "").strip()
        except (IndexError, KeyError):
            po = ""
        try:
            timed = float(db_r["finish_time_seconds"] or 0) > 0
        except (IndexError, KeyError, TypeError, ValueError):
            timed = False
    if not po:
        return "unknown"
    return "dq" if timed else "dnf"


def _finish_status_col(conn):
    """results に finish_status (#173) があれば SELECT に足す断片。無い DB (テスト・古い版) では空。"""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(results)").fetchall()}
    except Exception:
        cols = set()
    return ", finish_status" if "finish_status" in cols else ""


def unplaced_status_map(conn, race_id):
    """#173: レースの着順の無い馬 → unplaced_status。未確定のレースは {}。

    結果投稿 (post_x の結果ビルダー) が results から直接引く時に使う。印のスナップショットには
    cache の scratched が無いので、results の値 (人気9999・負のオッズ・確定後の 999.9倍) だけで見る。
    """
    rows = conn.execute(
        f"SELECT horse_number, finish_position, odds, popularity, passing_order, finish_time_seconds"
        f"{_finish_status_col(conn)} FROM results WHERE race_id = ?", (race_id,)).fetchall()
    if not any((r["finish_position"] or 0) == 1 for r in rows):
        return {}
    return {r["horse_number"]: unplaced_status({}, r, True)
            for r in rows if not ((r["finish_position"] or 0) > 0)}


def numbers_state(conn, race_id, horses):
    """予測の馬番が枠順確定後の正しいものか (#169)。戻り値 (numbers_confirmed, 理由 or None)。

    2026-11-01 00:35〜06:53、仮馬番 (五十音順) のまま予測した23レースが公開され、
    出走しない馬に◎が付き、オッズは別の馬に付いていた。成果物ゲートはレース数しか
    見ておらず「予測あり」と判定した。ここで2点を確かめ、JSON に書いてゲートに渡す:
      (1) DB の馬番が確定済みか (#27/#150 と同じ判定: post_position=枠番 が入っている)
      (2) 予測 (cache) の馬番↔馬名が DB と一致しているか — 仮馬番で予測した後に DB だけ
          確定版に直った場合や、予測後に幽霊馬が消えた場合を捕まえる
    枠順前の日付 (土曜朝に作る日曜分など) は (1) で False になるが、それは正当な仮馬番。
    ゲートが見るのは開催当日の JSON なので、当日に False なら作り直しが要る。
    """
    _FFFD = chr(65533)
    rows = conn.execute(
        "SELECT res.horse_number AS n, res.post_position AS pp, hh.horse_name AS nm "
        "FROM results res LEFT JOIN horses hh ON hh.horse_id = res.horse_id "
        "WHERE res.race_id = ?", (race_id,)).fetchall()
    if not rows:
        return False, "DB に出走馬が無い"
    if not any((r["pp"] or 0) > 0 for r in rows):
        return False, "DB の馬番が仮のまま (枠番なし = 枠順確定前)"
    db = {r["n"]: (r["nm"] or "") for r in rows}
    bad = []
    for h in horses:
        n = h.get("horse_number")
        nm = h.get("horse_name") or ""
        if n not in db:
            bad.append(f"{n}番 (DBに無い)")
            continue
        dn = db[n]
        if nm and dn and _FFFD not in nm and _FFFD not in dn and nm != dn:
            bad.append(f"{n}番 {nm}≠{dn}")
    if bad:
        return False, f"予測の馬番が確定馬番とずれ {len(bad)}頭: " + ", ".join(bad[:3])
    return True, None


def _hold_back_unconfirmed(output_dir, ds, all_races):
    """開催当日 (以前) の書き出しで、馬番が未確定のレースを公開しない (#169)。戻り値 (races, withheld)。

    書き出しは auto_post_x だけでなく race_day_runner (10分ごと)・refresh_dashboard・results・
    collect_results からも走る。workflow 側のゲートは一部の type にしか無く、しかも JSON 全体を
    戻すので、1レースが未確定なだけで他のレースの更新まで止めていた。判定をここ (書き込み側) に
    置き、レースごとに決める:
      - 新しい版の馬番が確定済み (または判定材料なし) → 新しい版を書く
      - 未確定で、既存の JSON にそのレースがある → 既存の版を残す (前夜 prefetch の版など。
        未確定の cache を確定済みの DB と突き合わせると、オッズや着順が別の馬に付くので、
        同じ未確定でも新しい版の方が悪い)
      - 未確定で、既存の版も無い → 書かずに withheld に記録する (仮馬番を当日に初めて出さない)
    枠順前の未来日 (木金の事前予測・土曜に作る日曜分) は仮馬番が正当なので対象外。
    """
    if ds > datetime.now(JST).strftime("%Y%m%d"):
        return all_races, []
    if not any(r.get("numbers_confirmed") is False for r in all_races):
        return all_races, []
    prev = {}
    out_path = os.path.join(output_dir, f"predictions_{ds}.json")
    try:
        with open(out_path, encoding="utf-8") as f:
            for rs in (json.load(f).get("venues") or {}).values():
                for r in rs or []:
                    if isinstance(r, dict) and r.get("race_id"):
                        prev[r["race_id"]] = r
    except Exception:
        prev = {}
    kept, withheld, out = [], [], []
    for r in all_races:
        if r.get("numbers_confirmed") is not False:
            out.append(r)
        elif r["race_id"] in prev:
            out.append(prev[r["race_id"]])
            kept.append(r)
        else:
            withheld.append({"race_id": r["race_id"], "venue": r.get("venue", ""),
                             "race_number": r.get("race_number", 0),
                             "numbers_issue": r.get("numbers_issue")})
    def _labels(rs):
        return ", ".join("%s%sR" % (x.get("venue", ""), x.get("race_number", "")) for x in rs[:6])
    if kept:
        print(f"  🛡 {ds}: 馬番未確定の {len(kept)}R は既存の版を残す ({_labels(kept)}) #169")
    if withheld:
        print(f"  🚫 {ds}: 馬番未確定で既存の版も無い {len(withheld)}R は公開しない "
              f"({_labels(withheld)}) #169")
    return out, withheld


# 公開 JSON の馬ごとの結果の項目 (export が DB の results から書くもの)
_RESULT_HORSE_KEYS = ("finish", "time", "actual_odds", "last_3f", "margin", "dnf", "dq", "excluded")


def _carry_forward_results(out_path, ds, all_races):
    """#174: 公開済みの JSON にある結果を、今回の DB に結果が無いレースへ引き継ぐ (結果を後退させない)。

    11/14 10:35 に race_day_runner が結果入りの JSON を公開した1分後、runner 自身が dispatch した
    post_predict の run が **origin の DB (runner はまだ push していない = 結果なし)** から export し直し、
    全36レースの結果を消した。#68/#90 と同じ「古い DB からの再 export で JSON が巻き戻る」型で、
    runner の DB push の間引き (#153) のたびに起こりうる。結果は確定した後は変わらないので、
    今回の DB にそのレースの着順が1つも無い時だけ、公開済みの版の結果を残す。DB に着順があれば
    DB を正とする (訂正・降着は DB 側で入る)。過去の日・確定前の日にも同じ規則で効く。
    """
    try:
        with open(out_path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        return 0
    prev_races = {}
    for rs in (prev.get("venues") or {}).values():
        for r in (rs if isinstance(rs, list) else (rs or {}).get("races", [])) or []:
            if isinstance(r, dict) and r.get("race_id") and r.get("has_results"):
                prev_races[r["race_id"]] = r
    if not prev_races:
        return 0
    kept = []
    for r in all_races:
        if r.get("has_results") or r.get("race_id") not in prev_races:
            continue
        pr = prev_races[r["race_id"]]
        ph = {h.get("horse_number"): h for h in (pr.get("horses") or []) if isinstance(h, dict)}
        for h in r.get("horses") or []:
            src = ph.get(h.get("horse_number"))
            if not src:
                continue
            for k in _RESULT_HORSE_KEYS:
                if k in src:
                    h[k] = src[k]
                else:
                    h.pop(k, None)
            if src.get("scratched"):
                h["scratched"] = True
            if (src.get("finish") or 0) > 0 and src.get("popularity"):
                h["popularity"] = src["popularity"]
        r["has_results"] = True
        r["payouts"] = pr.get("payouts") or []
        kept.append(r)
    if kept:
        print(f"  🛡 {ds}: DB に着順が無い {len(kept)}R は公開済みの結果を残す (古い DB からの再 export で"
              f"結果を消さない #174): " + ", ".join(f"{x.get('venue', '')}{x.get('race_number', '')}R"
                                                     for x in kept[:8]))
    return len(kept)


def export_predictions(date_str=None):
    """指定日の予測データをJSONファイルに書き出し"""

    if not date_str:
        # 今週末の土日を自動判定
        now = datetime.now(JST)
        weekday = now.weekday()  # 0=Mon
        # 次の土曜を計算
        days_until_sat = (5 - weekday) % 7
        if days_until_sat == 0 and now.hour >= 18:
            days_until_sat = 7
        sat = now + timedelta(days=days_until_sat)
        sun = sat + timedelta(days=1)
        dates = [sat.strftime("%Y%m%d"), sun.strftime("%Y%m%d")]
    else:
        dates = [date_str]

    output_dir = os.path.join(os.path.dirname(__file__), "docs", "data")
    os.makedirs(output_dir, exist_ok=True)

    exported = []

    for ds in dates:
        race_date_hyphen = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"

        with get_db() as conn:
            # レース一覧
            races = conn.execute("""
                SELECT * FROM races
                WHERE race_date = ? OR race_date = ?
                ORDER BY venue, race_number
            """, (ds, race_date_hyphen)).fetchall()

            if not races:
                print(f"  ⏭️ {ds}: レースなし")
                continue

            all_races = []
            skipped_no_cache = 0
            for race in races:
                race_id = race["race_id"]
                race_info = dict(race)

                # 予測キャッシュ取得（race_id完全一致 → 会場コード+レース番号+日付でフォールバック）
                cached = conn.execute(
                    "SELECT * FROM predictions_cache WHERE race_id = ?",
                    (race_id,)
                ).fetchone()

                if not cached:
                    # race_idが朝と昼で変わる場合のフォールバック:
                    # race_id形式: YYYY(4) + venue(2) + kai(2) + day(2) + race(2)
                    # 日目(day=8-9桁目)が変わるため、会場+開催回+レース番号+作成日で検索
                    venue_code = race_id[4:6]
                    kai_code = race_id[6:8]
                    race_num_str = race_id[10:12]
                    # created_atの日付がrace_dateと同日のもののみ
                    cached = conn.execute("""
                        SELECT * FROM predictions_cache
                        WHERE substr(race_id,5,2) = ?
                          AND substr(race_id,7,2) = ?
                          AND substr(race_id,11,2) = ?
                          AND date(created_at, '+9 hours') = ?   -- #150: created_at は UTC 保存なので JST に揃える
                        ORDER BY created_at DESC LIMIT 1
                    """, (venue_code, kai_code, race_num_str, race_date_hyphen)).fetchone()
                    if cached:
                        print(f"  🔄 {race_id} → cache {cached['race_id']} (フォールバック一致)")

                if not cached:
                    # メインレース以外は予測対象外なので静かにスキップし、最後にサマリー出力
                    skipped_no_cache += 1
                    continue

                horses_raw = json.loads(cached["predictions_json"])
                # 馬番重複・馬番0・馬番19以上・同名馬を除去
                seen_nums = set()
                seen_names = set()
                horses = []
                for h in horses_raw:
                    num = h.get('horse_number', 0)
                    name = h.get('horse_name', '')
                    if num == 0 or num > 18:
                        continue
                    if num in seen_nums:
                        continue
                    if name and name in seen_names:
                        continue
                    seen_nums.add(num)
                    if name:
                        seen_names.add(name)
                    horses.append(h)
                all_bets = json.loads(cached["all_bets_json"])
                confidence = cached["confidence"]
                conf_reason = cached["conf_reason"] or ""
                should_bet = bool(cached["should_bet"])
                bet_reason = cached["bet_reason"] or ""

                # #169: 馬番が確定済みか (化け名の上書き前の cache の馬名で照合する)
                _numbers_ok, _numbers_issue = numbers_state(conn, race_id, horses)

                # 🆕 #90: 書き出し時に馬名を horses 正本で上書きし文字化けを根絶。
                #   cache(predictions_json/all_bets_json) には予測時(#85/#90修正前)の化け名
                #   スナップショットが残りうる (runner が古い cache を再 export する) ため、
                #   export 側で常に正す = JSON は cache の状態に関わらず常にクリーン。
                _FFFD = chr(65533)
                name_by_num = {}
                for rr in conn.execute(
                    "SELECT res.horse_number AS n, hh.horse_name AS nm FROM results res "
                    "JOIN horses hh ON hh.horse_id = res.horse_id WHERE res.race_id = ?",
                    (race_id,)).fetchall():
                    if rr['nm'] and _FFFD not in rr['nm']:
                        name_by_num[rr['n']] = rr['nm']
                for h in horses:
                    n = h.get('horse_number')
                    if n in name_by_num and (_FFFD in str(h.get('horse_name') or '') or not h.get('horse_name')):
                        h['horse_name'] = name_by_num[n]
                if isinstance(all_bets, dict):
                    for _bt, _bets in all_bets.items():
                        for _b in (_bets or []):
                            _nums = _b.get('horse_numbers') or []
                            _nms = [name_by_num.get(x) for x in _nums]
                            if _nums and all(_nms):
                                _b['horse_name'] = "-".join(_nms)

                # #169: 画面の SI を点数表の内訳 (平均スピード指数 N位) と同じ V2d にそろえる。
                # 新しいキャッシュは predict が si_avg_v を書く (そのまま出る)。キーの無い
                # 古いキャッシュだけ、内訳の区分と V2d の順位が全件一致した時に補う。
                try:
                    from ml.si_display import backfill_cached
                    _bf = backfill_cached(get_db, race_id, race_date_hyphen, horses)
                    if _bf not in ("skip", "noevidence"):
                        print(f"  🔢 {race_id} SI(V2d) の補完: {_bf}")
                except Exception as _e:
                    print(f"  ⚠️ {race_id} SI(V2d) の補完に失敗 (旧指数のまま): {_e}")
                # #169 手直し: 古いキャッシュの内訳に残る内部表記 ("5/5") と、新馬戦の初出走馬への
                # 「休み明け」等 (全頭同じ区分) を、今の表示規則にそろえる (新しいキャッシュは素通り)
                try:
                    from ml.scorecard import sanitize_cached_reasons
                    _tr, _dr = sanitize_cached_reasons(horses, race_info.get("race_name", ""))
                    if _tr or _dr:
                        print(f"  🧹 {race_id} 内訳の表記をそろえた: 訳した {_tr} / 外した {_dr}")
                except Exception as _e:
                    print(f"  ⚠️ {race_id} 内訳の表記の整理に失敗 (キャッシュのまま): {_e}")

                # 結果データ取得
                res_rows = conn.execute("""
                    SELECT r.horse_number, r.finish_position, r.finish_time,
                           r.odds, r.popularity, r.last_3f, r.margin
                    FROM results r
                    WHERE r.race_id = ? AND r.finish_position > 0
                    ORDER BY r.finish_position
                """, (race_id,)).fetchall()

                has_results = len(res_rows) > 0
                # #171: 「着順の無い 999.9倍 = 取消」と言えるのは1着が出た (レース確定) 時だけ (#150e)。
                # 発走直後の部分取得で一部の着順しか無い間は、走った大穴を取消にしない
                race_confirmed = any(rr['finish_position'] == 1 for rr in res_rows)
                race_results = {}
                for rr in res_rows:
                    race_results[rr['horse_number']] = {
                        'finish': rr['finish_position'],
                        'time': rr['finish_time'] or '',
                        'odds': rr['odds'] or 0,
                        'popularity': rr['popularity'] or 0,
                        'last_3f': rr['last_3f'] or 0,
                        'margin': rr['margin'] or '',
                    }

                # 最新のオッズ・人気をマージ
                db_results = conn.execute(
                    "SELECT horse_number, odds, popularity, passing_order, finish_time_seconds"
                    f"{_finish_status_col(conn)} FROM results WHERE race_id = ?",
                    (race_id,)
                ).fetchall()
                odds_map = {r['horse_number']: r for r in db_results}
                for h in horses:
                    db_r = odds_map.get(h['horse_number'])
                    # #170: 取消と分かっている馬に scratched: true を立てる (判定は X/Threads の
                    # 💡能力値1位・能力値ランキングと共通 = scratched_for_export)。下の上書きで
                    # cache の番兵 (9999人気) が消えることがあるので、マージの前に判定する。
                    _scr = scratched_for_export(h, db_r, race_finished=race_confirmed)
                    if db_r:
                        if db_r['odds'] and db_r['odds'] > 0:
                            h['odds_win'] = db_r['odds']
                        # #150: 取消・除外センチネル (9999人気) を公開JSONに出さない
                        # (#149 で results/cache 側は塞いだが export に残っていた)
                        if db_r['popularity'] and 0 < db_r['popularity'] < 999:
                            h['popularity'] = db_r['popularity']
                    # #170: odds_win / popularity の値は変えない (投稿側は 9999 でも判定する)。
                    # 取消でない馬のキーは触らない (旧 cache は無し / #169 以降の cache は false)。
                    # 着順のある馬は走っているので取消にしない (999.9倍で実際に走った大穴もいる)。
                    if h['horse_number'] in race_results:
                        if h.get('scratched'):
                            h['scratched'] = False
                    elif _scr:
                        h['scratched'] = True
                    # #173: 確定したレースで着順が無く、取消の目印も無い馬のうち、通過順がある馬は
                    # 競走中止 (走った = 外れ)。旧表示は着順0を一律「取消 / 出走取消・除外」にしていた。
                    # 取消の目印を先に見るので scratched とは排他 (scratched の判定は変えない)。
                    # キーは走った馬にだけ立て、それ以外は消す (cache に残っていても出さない)。
                    # dnf = 走って着順が付かなかった (中止・失格 = 外れ)、dq = そのうち失格
                    _unpl = (unplaced_status(h, db_r, race_confirmed)
                             if h['horse_number'] not in race_results else None)
                    if _unpl in UNPLACED_RAN:
                        h['dnf'] = True
                        # 着順欄 (#173 finish_status) で走ったと分かった馬は、確定後の 999.9倍で
                        # 立った取消の目印を外す (走った大穴の競走中止を「取消」と出さない)
                        if h.get('scratched'):
                            h['scratched'] = False
                    else:
                        h.pop('dnf', None)
                    if _unpl == "dq":
                        h['dq'] = True
                    else:
                        h.pop('dq', None)
                    # 競走除外 (netkeiba の着順欄「除」#173)。走っていない = 返還。scratched (取消) とは別に出す
                    if _unpl == "excluded":
                        h['excluded'] = True
                    else:
                        h.pop('excluded', None)
                    # 結果をマージ
                    res = race_results.get(h['horse_number'])
                    if res:
                        h['finish'] = res['finish']
                        h['time'] = res['time']
                        h['actual_odds'] = res['odds']
                        if res['popularity'] and res['popularity'] > 0:
                            h['popularity'] = res['popularity']
                        h['last_3f'] = res['last_3f']
                        h['margin'] = res['margin']
                    else:
                        h['finish'] = 0
                        h['time'] = ''
                        h['actual_odds'] = 0
                        h['last_3f'] = 0
                        h['margin'] = ''

                # 配当
                payout_rows = conn.execute("""
                    SELECT bet_type, combination, payout_amount, popularity
                    FROM payouts WHERE race_id = ?
                    ORDER BY bet_type, popularity
                """, (race_id,)).fetchall()
                race_payouts = [{
                    'bet_type': pr['bet_type'],
                    'combination': pr['combination'],
                    'payout': pr['payout_amount'],
                    'popularity': pr['popularity'],
                } for pr in payout_rows]

                # #163: 旧「💎妙味」(買い目EVの最大 / 実オッズが無い日は混戦度から 1.0〜5.0 を
                # 当てはめた値) は撤去。買い目ゼロのレース406件中201件に★が付き、★が多いほど
                # むしろ配当が安いなど、実際の回収と逆向きだった。

                # レース傾向 (pred_win_pct ベース: 0-100 範囲)
                # 旧版は pred_win (0-1 範囲) を見ていたが、JSON には pred_win_pct のみ存在
                # → top_prob 常に 0 で全レース「波乱含み」になっていた
                sorted_probs = sorted([h.get("pred_win_pct", 0) for h in horses], reverse=True)
                top_prob = sorted_probs[0] if sorted_probs else 0
                second_prob = sorted_probs[1] if len(sorted_probs) > 1 else 0
                gap = top_prob - second_prob
                top3_total = sum(sorted_probs[:3])
                # v3 (2026-05-17): gap 廃止 — ◎の絶対値のみで判定。
                # 「AI 20% > 18% なのに『やや堅い』になる」直感矛盾を解消。
                if top_prob >= 18:
                    race_tendency = "堅い（本命突出）"
                elif top_prob >= 14:
                    race_tendency = "やや堅い（軸馬明確）"
                elif top3_total >= 35:
                    race_tendency = "上位拮抗（実力伯仲）"
                elif top_prob <= 8:
                    race_tendency = "波乱含み（大混戦）"
                else:
                    race_tendency = "普通（中穴狙い可）"

                # #96: 同名レースの歴史的荒れ度
                try:
                    from volatility import compute_race_upset_history
                    upset_hist = compute_race_upset_history(
                        race_info.get("race_name", "") or "",
                        race_info.get("race_date", "") or race_date_hyphen,
                    )
                except Exception:
                    upset_hist = None

                all_races.append({
                    "race_id": race_id,
                    "venue": race_info.get("venue", ""),
                    "race_number": race_info.get("race_number", 0),
                    "race_name": race_info.get("race_name", ""),
                    "grade": race_info.get("grade", ""),
                    "distance": race_info.get("distance", 0),
                    "surface": race_info.get("surface", ""),
                    "track_condition": race_info.get("track_condition", "良"),
                    "start_time": race_info.get("start_time", ""),
                    "horse_count": len(horses),
                    "horses": horses,
                    "all_bets": all_bets,
                    "should_bet": should_bet,
                    "bet_reason": bet_reason,
                    "confidence": confidence,
                    "conf_reason": conf_reason,
                    "race_tendency": race_tendency,
                    # #96: 同名レースの歴史的荒れ度 (temporal-safe、out-of-time検証済)
                    "upset_hist": upset_hist,
                    "has_results": has_results,
                    "payouts": race_payouts if has_results else [],
                    "prediction_locked": prediction_locked_for(ds, datetime.now(JST)),   # #172: 9:30 確定 (対象日ごと)
                })
                # #169: 成果物ゲートは numbers_confirmed=False のレースを「予測あり」と数えない
                all_races[-1]["numbers_confirmed"] = _numbers_ok
                all_races[-1]["numbers_issue"] = _numbers_issue

            if skipped_no_cache:
                print(f"  ℹ️ {ds}: メインレース以外 {skipped_no_cache}件はキャッシュなしでスキップ")
            _unconf = [r for r in all_races if r.get("numbers_confirmed") is False]
            if _unconf:
                print(f"  ⚠️ {ds}: 馬番が未確定の予測 {len(_unconf)}/{len(all_races)}R "
                      f"(例: {_unconf[0]['venue']}{_unconf[0]['race_number']}R "
                      f"{_unconf[0].get('numbers_issue')}) — 成果物ゲートは予測なしと数える #169")
            all_races, _withheld = _hold_back_unconfirmed(output_dir, ds, all_races)

            if not all_races:
                print(f"  ⏭️ {ds}: 予測データなし")
                continue

            # 会場グループ化
            venues = {}
            for r in all_races:
                v = r["venue"] or "不明"
                if v not in venues:
                    venues[v] = []
                venues[v].append(r)

            output = {
                "date": ds,
                "total_races": len(all_races),
                # #169: 馬番が未確定のレース数 (ゲートは scripts/prediction_gate.py で判定)
                "numbers_unconfirmed_races": sum(
                    1 for r in all_races if r.get("numbers_confirmed") is False),
                # #169: 開催当日 (以前) に馬番が未確定で、前の版も無いので公開しなかったレース。
                # ゲート (prediction_gate) はこれも「馬番未確定」と数え、作り直しを起動する
                "numbers_withheld": _withheld,
                "venues": venues,
                "is_locked": prediction_locked_for(ds, datetime.now(JST)),   # #172: 9:30 確定 (対象日ごと)
                "exported_at": datetime.now(JST).isoformat(),
            }

            out_path = os.path.join(output_dir, f"predictions_{ds}.json")
            _carry_forward_results(out_path, ds, all_races)
            # #150c: 既存 JSON より大幅にレースが減る上書きを止める。
            # 前夜 prefetch が36レースを公開した後、翌朝の predict が部分成功すると
            # その export が N レースで **全置換**し、ダッシュボードが前夜より後退する
            # (確定ロック (9:30 #172) 以降は再予測されないので、その日は復旧しない)。
            _prev_n = 0
            if os.path.exists(out_path):
                try:
                    with open(out_path, encoding="utf-8") as _f:
                        _prev_n = int(json.load(_f).get("total_races") or 0)
                except Exception:
                    _prev_n = 0
            if _prev_n and len(all_races) < _prev_n * 0.8:
                print(f"  🚫 {ds}: 既存JSON {_prev_n}レース → 今回 {len(all_races)}レース "
                      f"(2割超の減少) のため上書きを中止。部分的な予測で公開済みの内容を"
                      f"後退させない (再予測で揃ってから再実行してください)")
                continue

            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, separators=(',', ':'))

            print(f"  ✅ {ds}: {len(all_races)}レース → {out_path}")
            exported.append(ds)

    # 最新日付のインデックスを作成（既存とマージ）
    if exported:
        idx_path = os.path.join(output_dir, "index.json")

        # 既存のインデックスを読み込み
        existing_dates = []
        if os.path.exists(idx_path):
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                    existing_dates = existing.get("dates", [])
            except Exception:
                pass

        # マージして重複排除・ソート
        all_dates = sorted(set(existing_dates + exported))

        # #150c: latest は「今日以前の最大日付」にする。
        # 前夜 prefetch が土日2日分を同時に export するようになったため、
        # 旧実装 (all_dates[-1]) だと土曜の朝に latest=日曜 になり、
        # ダッシュボードの既定表示が「明日のレース」になってしまう。
        _today = datetime.now(JST).strftime("%Y%m%d")
        _past = [d for d in all_dates if d <= _today]
        _latest = _past[-1] if _past else all_dates[-1]

        index = {
            "latest": _latest,
            "dates": all_dates,
            "updated_at": datetime.now(JST).isoformat(),
        }
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        print(f"  📋 インデックス更新: {idx_path}")

    return exported


if __name__ == "__main__":
    from database import init_db
    init_db()

    # positional("20260509") と --date flag の両方を受け付ける(誤呼び出し耐性)
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("date_pos", nargs='?', help="対象日 YYYYMMDD")
    parser.add_argument("--date", dest="date_flag", help="対象日 YYYYMMDD (--date 形式)")
    args = parser.parse_args()
    date_arg = args.date_pos or args.date_flag
    if date_arg:
        export_predictions(date_arg)
    else:
        export_predictions()
