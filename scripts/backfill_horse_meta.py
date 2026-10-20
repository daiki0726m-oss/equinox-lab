#!/usr/bin/env python3
"""馬マスターの生年・母 と 追い切りの D 評価 を過去分まで埋める backfill。

  1. 生年 (horses.birth_year): 全件 NULL だった。horse_id 先頭4桁が生年なので計算で埋める
     (ネット不要・冪等。database.birth_year_from_horse_id)。
  2. 母 (horses.dam): #127 (2026-08-24) まで母を一度も読んでおらず、その後の補完 (auto_pedigree の
     日曜歴史モード) が古いレース順に 4,000頭/週ずつ進んでいたため、新しい馬ほど空のまま
     (2026年1〜8月の出走馬で 25〜31%)。血統ページ (db.netkeiba.com/horse/ped/) から取り直す。
  3. 追い切り評価 (workouts.evaluation_grade): 収集時に A/B/C 以外を None にしており、
     実在する D 評価を捨てていた。D が付いていたはずの行 (評価なし & 評価文あり) を持つレースの
     追い切りページを取り直す。評価文も空の行 (= ページ上も空欄の「評価なし」) は取り直さない。

usage:
  python3 scripts/backfill_horse_meta.py birth_year                     # 生年だけ (ネット不要)
  python3 scripts/backfill_horse_meta.py dam --record U.jsonl           # 母を取得して DB に書き JSONL にも残す
  python3 scripts/backfill_horse_meta.py workouts --record U.jsonl      # 追い切り評価を取り直す
  python3 scripts/backfill_horse_meta.py all --record U.jsonl           # 3つとも
  python3 scripts/backfill_horse_meta.py --apply U.jsonl                # 記録を (最新の) DB に当て直す (+生年)
  オプション: --db PATH (既定 keiba.db) / --limit N / --horse-ids ID... / --race-ids ID... / --sleep 秒

--record / --apply は scripts/backfill_passing_order.py (#164) と同じ考え方。取得に時間がかかる間に
他の workflow が keiba.db.gz を更新しても消さないよう (#70 の gz clobber)、取得した値は JSONL に残し、
push 直前に最新の DB を取り直して「空欄だった所だけ」に当て直す。既存の値は一切上書きしない
(父・母父の既存表記 — 「ナダル」/「ナダルNadal(米)」の混在 — も今の血統ページの表記に書き換えない。
auto_pedigree も同じ規則。collect_pedigree.save_pedigree の既定)。
2回目の --apply は何も変えない (冪等)。
母の記録には血統表のリンク先の馬ID (sire_id / dam_id / damsire_id) も残す。母の名前は
「カナ名」と「カナ名+英字名(国名)」に割れて同じ繁殖牝馬を一意に指せないため。horses にはまだ
ID の列が無いので今は DB に入らないが、列を足した後に同じ記録を --apply すれば埋まる。
実行は workflow (.github/workflows/backfill_meta.yml) から。開催の無い火〜木に限る (理由は workflow 冒頭)。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)  # repo root を import path に

import database  # noqa: E402

WORKOUT_FILL_SQL = """UPDATE workouts SET
    evaluation_grade = ?,
    evaluation_text = CASE WHEN COALESCE(evaluation_text, '') = '' AND ? <> ''
                           THEN ? ELSE evaluation_text END
  WHERE race_id = ? AND horse_id = ? AND evaluation_grade IS NULL"""


def _conn(db):
    c = database.get_connection(db)
    return c


def stats(c, label=""):
    """欠けの状況を表示する (before / after の比較用)。"""
    by_null = c.execute("SELECT COUNT(*) FROM horses WHERE birth_year IS NULL").fetchone()[0]
    dam_all = c.execute("SELECT COUNT(*) FROM horses WHERE COALESCE(dam, '') = ''").fetchone()[0]
    dam_26 = c.execute("""
        SELECT COUNT(DISTINCT r.horse_id),
               COUNT(DISTINCT CASE WHEN COALESCE(h.dam, '') = '' THEN r.horse_id END)
        FROM results r JOIN races ra ON ra.race_id = r.race_id
        LEFT JOIN horses h ON h.horse_id = r.horse_id
        WHERE ra.race_date >= '2026-01-01'""").fetchone()
    wk = c.execute("""
        SELECT SUM(evaluation_grade IS NULL AND COALESCE(evaluation_text, '') <> ''),
               SUM(evaluation_grade IS NULL AND COALESCE(evaluation_text, '') = ''),
               SUM(evaluation_grade = 'D'), COUNT(*)
        FROM workouts""").fetchone()
    print(f"📊 {label} 生年NULL {by_null} 頭 / 母が空 {dam_all} 頭 "
          f"(2026年の出走馬 {dam_26[1]}/{dam_26[0]}) / 追い切り: 評価なし&評価文あり {wk[0] or 0} 行・"
          f"評価なし&評価文なし {wk[1] or 0} 行・D {wk[2] or 0} 行 / 全 {wk[3]} 行", flush=True)


# ─── 1. 生年 ───────────────────────────────────────────────────
def cmd_birth_year(c):
    n = database.fill_birth_years(c)
    c.commit()
    left = c.execute("SELECT horse_id FROM horses WHERE birth_year IS NULL").fetchall()
    print(f"🐴 生年: {n} 頭を埋めた (残り NULL {len(left)} 頭: "
          f"{', '.join(r[0] for r in left[:5])}{' …' if len(left) > 5 else ''})", flush=True)
    return n


# ─── 2. 母 (血統ページ) ─────────────────────────────────────────
def dam_targets(c, limit=None, horse_ids=None):
    if horse_ids:
        return list(horse_ids)
    rows = c.execute("""
        SELECT h.horse_id, MAX(ra.race_date) AS last_date
        FROM horses h
        LEFT JOIN results r ON r.horse_id = h.horse_id
        LEFT JOIN races ra ON ra.race_id = r.race_id
        WHERE h.horse_id <> ''
          AND (COALESCE(h.dam, '') = '' OR COALESCE(h.sire, '') = '' OR COALESCE(h.damsire, '') = '')
        GROUP BY h.horse_id
        ORDER BY last_date IS NULL, last_date DESC, h.horse_id DESC""").fetchall()
    ids = [r[0] for r in rows]
    return ids[:limit] if limit else ids


def cmd_dam(c, rec, limit=None, horse_ids=None, sleep=1.0):
    import requests
    from collect_pedigree import HEADERS, scrape_horse_pedigree, save_pedigree
    ids = dam_targets(c, limit, horse_ids)
    print(f"🧬 母の取得: {len(ids)} 頭 (新しい馬から)", flush=True)
    sess = requests.Session()
    sess.headers.update(HEADERS)
    got = filled = failed = 0
    t0 = time.time()
    for i, hid in enumerate(ids, 1):
        try:
            p = scrape_horse_pedigree(sess, hid)
        except Exception as e:  # scrape 側で握っているが念のため
            print(f"  ⚠️ {hid}: {e}", flush=True)
            p = None
        if p and any(p.get(k) for k in ("sire", "dam", "damsire")):
            got += 1
            # *_id (血統表のリンク先の馬ID) も記録する。horses にまだ列は無いが、
            # 母の表記は「カナ名」と「カナ名+英字名(国名)」に割れるので、母の産駒成績を
            # 集計する時に名前でなく ID で引けるよう残しておく (列を足せば --apply で入る)
            u = {"kind": "pedigree", "horse_id": hid,
                 **{k: p.get(k, "") for k in ("sire", "dam", "damsire",
                                              "sire_id", "dam_id", "damsire_id")}}
            filled += save_pedigree(c, hid, u)   # 空の列だけ埋める
            if rec:
                rec.write(json.dumps(u, ensure_ascii=False) + "\n")
            if not u["dam"]:
                print(f"  ⚠️ {hid}: 血統ページに母が見つからない (父={u['sire']!r})", flush=True)
        else:
            failed += 1
            print(f"  ⚠️ {hid}: 血統ページを取得・解析できず", flush=True)
        if i % 50 == 0:
            c.commit()
            if rec:
                rec.flush()
            el = time.time() - t0
            print(f"  {i}/{len(ids)} 頭 / 取得 {got} / 埋めた {filled} / 失敗 {failed} "
                  f"({el / i:.2f} 秒/頭、残り約 {(len(ids) - i) * el / i / 60:.0f} 分)", flush=True)
        time.sleep(sleep)
    c.commit()
    print(f"✅ 母: 取得 {got} / 埋めた {filled} / 失敗 {failed} / 全 {len(ids)}", flush=True)


# ─── 3. 追い切りの D 評価 ────────────────────────────────────────
def workout_targets(c, limit=None, race_ids=None):
    if race_ids:
        return list(race_ids)
    rows = c.execute("""
        SELECT w.race_id, MAX(ra.race_date) AS d
        FROM workouts w LEFT JOIN races ra ON ra.race_id = w.race_id
        WHERE w.evaluation_grade IS NULL AND COALESCE(w.evaluation_text, '') <> ''
        GROUP BY w.race_id
        ORDER BY d DESC, w.race_id DESC""").fetchall()
    ids = [r[0] for r in rows]
    return ids[:limit] if limit else ids


def apply_workout(c, u):
    g = u.get("evaluation_grade")
    if not g:
        return 0
    txt = u.get("evaluation_text") or ""
    return c.execute(WORKOUT_FILL_SQL, (g, txt, txt, u["race_id"], u["horse_id"])).rowcount


def cmd_workouts(c, rec, limit=None, race_ids=None):
    from scraper import NetkeibaScraper   # _get が 1.5秒間隔を入れる
    s = NetkeibaScraper()
    rids = workout_targets(c, limit, race_ids)
    print(f"🏃 追い切り評価の取り直し: {len(rids)} レース", flush=True)
    filled = recorded = empty = 0
    t0 = time.time()
    for i, rid in enumerate(rids, 1):
        try:
            ws = s.scrape_workouts(rid)
        except Exception as e:
            print(f"  ⚠️ {rid}: {e}", flush=True)
            ws = []
        if not ws:
            empty += 1
            print(f"  ⚠️ {rid}: 追い切りページを取得できず (またはテーブルなし)", flush=True)
        for w in ws:
            if not w.get("evaluation_grade"):
                continue
            u = {"kind": "workout", "race_id": rid, "horse_id": w["horse_id"],
                 "horse_number": w.get("horse_number"),
                 "evaluation_grade": w["evaluation_grade"],
                 "evaluation_text": w.get("evaluation_text") or ""}
            n = apply_workout(c, u)
            if n:
                filled += n
                recorded += 1
                if rec:
                    rec.write(json.dumps(u, ensure_ascii=False) + "\n")
        if i % 50 == 0:
            c.commit()
            if rec:
                rec.flush()
            el = time.time() - t0
            print(f"  {i}/{len(rids)} レース / 埋めた {filled} 行 "
                  f"({el / i:.2f} 秒/レース、残り約 {(len(rids) - i) * el / i / 60:.0f} 分)", flush=True)
    c.commit()
    print(f"✅ 追い切り: 埋めた {filled} 行 / 取得できなかったレース {empty} / 全 {len(rids)} レース",
          flush=True)


# ─── --apply ──────────────────────────────────────────────────
def cmd_apply(c, path):
    """記録 (JSONL) を DB に当て直す。空欄だった所だけ埋める。生年もここで埋める。"""
    from collect_pedigree import save_pedigree
    by = database.fill_birth_years(c)
    n = ped = wk = bad = 0
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    u = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                n += 1
                if u.get("kind") == "pedigree":
                    ped += save_pedigree(c, u["horse_id"], u)   # 空の列だけ埋める
                elif u.get("kind") == "workout":
                    wk += apply_workout(c, u)
                else:
                    bad += 1
    elif path:
        print(f"📭 記録ファイルなし: {path} (生年だけ埋める)")
    c.commit()
    print(f"✅ 当て直し: 記録 {n} 件 → 生年 {by} 頭 / 血統 {ped} 頭 / 追い切り {wk} 行を埋めた"
          f"{f' (読めない行 {bad})' if bad else ''}", flush=True)
    return by + ped + wk


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("what", nargs="?", default="all",
                    choices=["birth_year", "dam", "workouts", "all"])
    ap.add_argument("--db", default=os.path.join(ROOT, "keiba.db"))
    ap.add_argument("--record", help="取得した値を JSONL に追記する")
    ap.add_argument("--apply", help="JSONL の記録を DB に当て直す (取得はしない)")
    ap.add_argument("--limit", type=int, default=0, help="対象数の上限 (テスト用。0=全件)")
    ap.add_argument("--horse-ids", nargs="*", help="母を取得する馬を直接指定")
    ap.add_argument("--race-ids", nargs="*", help="追い切りを取り直すレースを直接指定")
    ap.add_argument("--sleep", type=float, default=1.0, help="血統ページの取得間隔 (秒)")
    args = ap.parse_args()

    db = os.path.abspath(args.db)
    database.DB_PATH = db           # scraper / collect_pedigree の get_db もこの DB を使う
    c = _conn(db)
    stats(c, "before")
    if args.apply:
        cmd_apply(c, args.apply)
    else:
        # 行バッファ: 取得ステップが時間切れで kill されても、そこまでの記録が残る
        rec = open(args.record, "a", encoding="utf-8", buffering=1) if args.record else None
        try:
            if args.what in ("birth_year", "all"):
                cmd_birth_year(c)
            if args.what in ("dam", "all"):
                cmd_dam(c, rec, args.limit or None, args.horse_ids, args.sleep)
            if args.what in ("workouts", "all"):
                cmd_workouts(c, rec, args.limit or None, args.race_ids)
        finally:
            if rec:
                rec.close()
    stats(c, "after ")
    c.close()


if __name__ == "__main__":
    main()
