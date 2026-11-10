"""
🏇 確定馬番リフレッシュスクリプト

枠順確定前 (月曜朝) の出馬表は netkeiba の馬番欄が空で、scrape_shutuba が
五十音順/登録順で「仮馬番 1..N」を採番してしまう。枠順抽選 (金 11:00) 後に
確定馬番へ更新しないと、馬番と馬名がズレたまま予測・投稿される (#44)。

このスクリプトは当日の全レースを scrape_shutuba で再取得し、save_race_to_db
(確定馬番で INSERT OR REPLACE + 仮馬番の同期削除) で DB を最新化する。
予測 (predict) の直前に実行するのが肝心。

#169 (2026-11-05): **自動の予測経路はすべてここを通し、失敗したら予測しない**。
11/01 未明、refresh_entries を通らない予測経路 (preflight の auto-fix) が仮馬番のまま
23レースを予測・公開し、6時間18分ダッシュボードに出ていた (出走しない馬に◎、オッズは
別の馬に付いていた)。経路ごとに同じ処理を書くと必ず抜けるので、判定はこの1か所に置き、
workflow は終了コード、Python の呼び出し元は ensure_confirmed_entries() を使う。

旧版は取得に失敗したレースがあっても常に終了コード0で、workflow 側の
`if ! refresh_entries.py` ゲート (#150c) は timeout と例外でしか効かなかった。

使い方:
  python refresh_entries.py --date 20260531
  python refresh_entries.py            (→ 当日分を自動判定)

終了コード:
  0 = 予測してよい (全レースを取り直せた / 失敗したレースも DB の馬番が確定済み)
      ※ 枠順前で netkeiba にまだ馬番が無い日は「仮馬番のまま」0 を返す (#169 の対象外:
        枠順前は仮馬番で予測してよく、表示側は #27/#150 のガードで馬番を出さない)
  1 = 予測してはいけない (馬番が確定していないのに取り直せなかったレースがある)
  2 = その日のレースが DB に無い
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_db
from scraper import NetkeibaScraper

JST = timezone(timedelta(hours=9))


def now_jst():
    return datetime.now(JST)


def _numbers_confirmed_in_db(conn, race_id):
    """DB の馬番が枠順確定後のものか (#27/#150 と同じ判定: post_position=枠番 が入っている)。"""
    row = conn.execute(
        "SELECT MAX(post_position) FROM results WHERE race_id = ?", (race_id,)
    ).fetchone()
    return bool(row and row[0] and int(row[0]) > 0)


def _db_runner_count(conn, race_id):
    """DB に入っている出走馬の行数 (#169)。0 のレースは予測の対象にならない。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM results WHERE race_id = ?", (race_id,)
    ).fetchone()
    return int(row[0] or 0) if row else 0


def _entries_changed(before, after):
    """取り直しで馬番↔馬の対応が変わったか (#173)。before/after = {馬番: (horse_id, 馬名)}。

    旧版は before を finish_position=0 の行だけ、after を出馬表の全頭で作って比べていた。
    確定したレースに着順0の馬 (取消・除外・競走中止) が1頭でもいると before が空でなくなり、
    着順のある馬番はすべて「before に無い」= 変化ありと判定され、馬番は何も変わっていないのに
    「🔧 馬番を確定版に更新」と出ていた (11/07 13:28 京都3R・11/08 13:12 福島4R)。
    両方とも全頭で比べる。馬の同一性は horse_id (両方ある時) で見て、無い時だけ馬名で見る
    (化け名 #90 は馬名の違いと数えない)。DB が空 (初回の取得) は「更新」と言わない (旧版と同じ)。
    """
    if not before:
        return False
    if set(before) != set(after):
        return True   # 馬番の集合が違う (仮馬番の幽霊が消える・頭数が変わる)
    _ffd = chr(65533)
    for hn, (bid, bnm) in before.items():
        aid, anm = after[hn]
        if bid and aid:
            if bid != aid:
                return True
        elif bnm and anm and _ffd not in bnm and _ffd not in anm and bnm != anm:
            return True
    return False


def _refresh_one(scraper, rid, date_str):
    """1レースを取り直す。成功なら (True, shutuba)、失敗なら (False, 理由)。"""
    try:
        shutuba = scraper.scrape_shutuba(rid, race_date_hint=date_str)
    except Exception as e:  # pragma: no cover - ネットワーク例外
        return False, f"取得エラー: {e}"
    if not shutuba:
        return False, "ページを取得できない"
    if not shutuba.get("entries"):
        return False, "出馬表に出走馬が無い"
    # #169: 枠番の無いページ (仮馬番) を、確定済みの DB に上書きしない。
    # save_race_to_db の UPSERT は post_position=0 なら既存の枠番を残したまま horse_id を
    # 入れ替えるので、「枠番は確定済みのまま、馬番の下の馬だけ仮の並び (五十音順)」になる。
    # 確定の判定 (枠番が入っているか) は通ってしまい、オッズが別の馬に付く 11/01 の型を
    # ゲートが見逃す。枠順後に netkeiba が枠番の無いページを返した時は、DB を残して
    # 「取り直せなかった (DB は確定済み)」として扱う。
    ents = shutuba["entries"]
    if (any(e.get("_provisional_number") for e in ents)
            or all(not (e.get("post_position") or 0) for e in ents)):
        with get_db() as conn:
            if _numbers_confirmed_in_db(conn, rid):
                return False, "枠番の無いページ (DB は確定馬番 → 上書きしない)"
    scraper.save_race_to_db(shutuba)
    return True, shutuba


def refresh_entries(date_str, retry_wait=2.0):
    """指定日の全レースの出走馬を確定馬番で再取得して DB を更新。

    戻り値 (dict):
      ok          : 予測してよいか (終了コード0に対応)
      no_races    : その日のレースが DB に無い
      races       : 対象レース数
      updated     : 取り直せたレース数
      changed     : 馬番が変わったレース数
      failed      : 取り直せなかったレース [(label, 理由, DB馬番が確定済みか)]
      provisional : 取り直した後も馬番が確定していないレース (枠順前) のラベル
    """
    init_db()
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    out = {"ok": False, "no_races": False, "races": 0, "updated": 0, "changed": 0,
           "failed": [], "provisional": []}

    with get_db() as conn:
        races = conn.execute("""
            SELECT race_id, venue, race_number
            FROM races
            WHERE race_date = ? OR race_date = ?
            ORDER BY venue, race_number
        """, (date_str, date_hyphen)).fetchall()

    if not races:
        print(f"❌ {date_str} のレースが見つかりません")
        out["no_races"] = True
        return out

    out["races"] = len(races)
    print(f"🏇 {len(races)}レースの確定馬番を取得中...")
    scraper = NetkeibaScraper()
    failed_first = []

    def _label(race):
        return f"{race['venue']}{race['race_number']}R"

    def _attempt(race):
        rid = race["race_id"]
        # 更新前の馬番→(horse_id, 馬名) スナップショット (変化検出用)。
        # #173: 着順で絞らず全頭 (after = 出馬表の全頭と母集団をそろえる)
        with get_db() as conn:
            before = {
                r["horse_number"]: (r["horse_id"] or "", r["horse_name"] or "")
                for r in conn.execute("""
                    SELECT re.horse_number, re.horse_id, h.horse_name
                    FROM results re LEFT JOIN horses h ON re.horse_id = h.horse_id
                    WHERE re.race_id = ?
                """, (rid,)).fetchall()
            }
        ok, res = _refresh_one(scraper, rid, date_str)
        if not ok:
            return False, res
        out["updated"] += 1
        after = {e.get("horse_number"): (e.get("horse_id") or "", e.get("horse_name") or "")
                 for e in res["entries"]}
        if _entries_changed(before, after):
            out["changed"] += 1
            print(f"  🔧 {_label(race)}: 馬番を確定版に更新")
        return True, None

    for race in races:
        ok, why = _attempt(race)
        if not ok:
            failed_first.append((race, why))
            print(f"  ⚠️ {_label(race)} {why}")
        time.sleep(0.3)

    # 一時的な取得失敗で1日分の予測を止めないよう、失敗したレースだけ1回取り直す
    if failed_first:
        print(f"  🔁 取り直せなかった {len(failed_first)}レースを再試行...")
        time.sleep(retry_wait)
    no_runner_labels = set()   # #169: DB に出走馬が1頭も無いレース (予測の対象外)
    for race, why in failed_first:
        ok, why2 = _attempt(race)
        if ok:
            print(f"  ✅ {_label(race)}: 再試行で取得")
            continue
        with get_db() as conn:
            confirmed = _numbers_confirmed_in_db(conn, race["race_id"])
            if _db_runner_count(conn, race["race_id"]) == 0:
                no_runner_labels.add(_label(race))
        out["failed"].append((_label(race), why2 or why, confirmed))
        time.sleep(0.3)
    out["no_runners"] = sorted(no_runner_labels)

    with get_db() as conn:
        for race in races:
            if not _numbers_confirmed_in_db(conn, race["race_id"]):
                out["provisional"].append(_label(race))

    # 取り直せなかったレースのうち、DB の馬番がまだ仮のものがあれば予測させない。
    # #169: ただし DB に出走馬が1頭も無いレース (週中の取得が作る空のスタブ・中止など) は除く。
    # 予測は出走馬の無いレースを skip するので、そのレースから仮馬番の予想が出ることはない。
    # 止めると、取れない1レースのために同じ日の他のレースまで予測できなくなる
    # (#169 の日付補完でスタブにも日付が入るので、開催日の一覧にこの種のレースが現れうる)。
    blocking = [f for f in out["failed"] if not f[2] and f[0] not in no_runner_labels]
    # 開催当日 (以降) は枠順が必ず出ている。取り直しが通っても枠番が無いのは
    # ページの読み違い等で、そのまま予測すると仮馬番で公開する (11/01 の型) ので止める
    failed_labels = {f[0] for f in out["failed"]}
    if date_str.replace("-", "") <= now_jst().strftime("%Y%m%d"):
        for lab in out["provisional"]:
            if lab not in failed_labels:
                blocking.append((lab, "開催当日なのに取り直した後も枠番が無い", False))
    out["ok"] = not blocking

    print(f"\n✅ {out['updated']}/{len(races)}レース更新完了 (馬番変更: {out['changed']}レース)")
    for lab, why, confirmed in out["failed"]:
        if lab in no_runner_labels:
            state = "DB に出走馬が無い → 予測の対象外 (他のレースは予測する)"
        elif confirmed:
            state = "DB は確定馬番 → 予測に支障なし"
        else:
            state = "DB は仮馬番のまま"
        print(f"  ❌ {lab}: 取り直せず ({why}) — {state}")
    if out["provisional"]:
        print(f"  ℹ️ 馬番が未確定 (枠順前) のレース {len(out['provisional'])}件: "
              f"{', '.join(out['provisional'][:12])}")
    if blocking:
        print(f"🚫 確定馬番にできないレースが {len(blocking)}件 "
              f"({', '.join(f'{b[0]}: {b[1]}' for b in blocking[:4])}) → 予測しない "
              f"(既存の予測を残す。仮馬番で予測すると馬番と馬名がズレる #44/#169)")
    return out


# #169: 取り直しに失敗した直後は、しばらく同じ日の取り直しを繰り返さない。
# race_day_runner は 10分ごとに preflight の auto-fix を回すので、取り直せない状態
# (netkeiba の障害・ページ構造の変化) が続くと、1回 36レース+再試行の取得を1日数十回
# 繰り返して数千リクエストになり、IP 制限で結果収集まで止まりうる。失敗は予測しない側に
# 倒れているので、待っても誤配信にはならない。成功は覚えない (予測の直前に必ず取り直す)。
ENSURE_FAIL_TTL_SEC = 45 * 60


def _fail_memo_path(date_str):
    import tempfile
    return os.path.join(tempfile.gettempdir(),
                        f"keiba_ensure_entries_fail_{date_str.replace('-', '')}")


def _recent_failure(date_str):
    """直近 ENSURE_FAIL_TTL_SEC 以内に取り直しが失敗していれば、その経過秒数。"""
    if os.environ.get("ENSURE_ENTRIES_NO_MEMO") == "1":
        return None
    try:
        age = time.time() - os.path.getmtime(_fail_memo_path(date_str))
    except OSError:
        return None
    return age if 0 <= age < ENSURE_FAIL_TTL_SEC else None


def _remember_failure(date_str, ok):
    p = _fail_memo_path(date_str)
    try:
        if ok:
            if os.path.exists(p):
                os.remove(p)
        else:
            with open(p, "w") as f:
                f.write(now_jst().isoformat())
    except OSError:
        pass


def ensure_confirmed_entries(date_str):
    """予測の直前に呼ぶ共通ゲート (#169)。True なら予測してよい。

    preflight の auto-fix・幽霊馬除去後の再予測など、Python から予測を起動する経路は
    すべてこれを通す (workflow から呼ぶ時は refresh_entries.py の終了コードで同じ判定)。
    直前 (45分以内) に失敗していれば取り直さずに False を返す (ENSURE_ENTRIES_NO_MEMO=1 で無効)。
    """
    age = _recent_failure(date_str)
    if age is not None:
        print(f"🚫 {date_str}: {int(age // 60)}分前に確定馬番を取り直せなかった → 今回は取り直さず予測しない "
              f"(再試行は {int((ENSURE_FAIL_TTL_SEC - age) // 60) + 1}分後 #169)")
        return False
    try:
        res = refresh_entries(date_str)
    except Exception as e:
        print(f"🚫 確定馬番の取り直しで例外 ({e}) → 予測しない (#169)")
        _remember_failure(date_str, False)
        return False
    ok = bool(res.get("ok"))
    # レースが DB に無い (no_races) のは取得の失敗ではないので覚えない
    if not res.get("no_races"):
        _remember_failure(date_str, ok)
    return ok


def main():
    parser = argparse.ArgumentParser(description="🏇 確定馬番リフレッシュ")
    parser.add_argument("--date", help="対象日 (YYYYMMDD, デフォルト=当日)")
    args = parser.parse_args()
    date_str = args.date or now_jst().strftime("%Y%m%d")
    print(f"🔄 {date_str} の確定馬番を更新します\n")
    res = refresh_entries(date_str)
    if res.get("no_races"):
        sys.exit(2)
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
