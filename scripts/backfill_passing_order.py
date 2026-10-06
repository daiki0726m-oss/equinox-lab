#!/usr/bin/env python3
"""通過順 (passing_order) と厩舎 (trainer_id) の系統的 backfill (#65 / #164)。

旧 seed が race.netkeiba.com の result.html を使っており、過去レースでは
「通過」列が無いため 2021-2023 がほぼ全滅していた。db.netkeiba.com の
アーカイブページ (通過・上り・厩舎が常設) から補完する。
#164 で厩舎 (trainer_id) の欠け (2021-2023 ほぼ全部、2026-05〜08 の大半) も同じページから補う。

usage:
  python3 scripts/backfill_passing_order.py 2021 2022            # 取得して keiba.db に書く
  python3 scripts/backfill_passing_order.py 2022 --record U.jsonl  # 書いた内容を JSONL にも残す
  python3 scripts/backfill_passing_order.py --apply U.jsonl        # 記録を (最新の) keiba.db に当て直す

--record / --apply は、取得に数時間かかる間に他の workflow が DB を更新しても
その更新を消さないためのもの (#70 の gz clobber 対策)。取得が終わったら最新の DB を
取り直し、そこに「空欄だった所だけ」を当て直してから push する。
"""
import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root を import path に

UPDATE_SQL = """UPDATE results SET
    passing_order = CASE WHEN (passing_order IS NULL OR passing_order = '') AND ? != ''
                         THEN ? ELSE passing_order END,
    last_3f = CASE WHEN (last_3f IS NULL OR last_3f = 0) AND ? > 0 THEN ? ELSE last_3f END,
    trainer_id = CASE WHEN (trainer_id IS NULL OR trainer_id = '') AND ? != ''
                      THEN ? ELSE trainer_id END
  WHERE race_id = ? AND horse_number = ?"""


def apply_row(c, u):
    """1頭分を当てる。空欄だった列だけ埋める (既存の値は上書きしない)。"""
    po, l3, tid = u.get("passing_order") or "", u.get("last_3f") or 0, u.get("trainer_id") or ""
    if tid:
        c.execute("INSERT OR IGNORE INTO trainers (trainer_id, trainer_name) VALUES (?, ?)",
                  (tid, u.get("trainer_name") or ""))
    cur = c.execute(UPDATE_SQL, (po, po, l3, l3, tid, tid, u["race_id"], u["horse_number"]))
    return cur.rowcount


def cmd_apply(path, db="keiba.db"):
    c = sqlite3.connect(db)
    n = rows = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows += apply_row(c, json.loads(line))
                n += 1
    c.commit()
    left = c.execute("""SELECT
        SUM(CASE WHEN COALESCE(passing_order,'')='' THEN 1 ELSE 0 END),
        SUM(CASE WHEN COALESCE(trainer_id,'')='' THEN 1 ELSE 0 END), COUNT(*)
        FROM results WHERE finish_position > 0""").fetchone()
    print(f"✅ 記録 {n} 件を当て直し ({rows} 行に該当)。残る欠け: 通過順 {left[0]} / 厩舎 {left[1]} / 全 {left[2]}")


def cmd_fetch(years, record=None, db="keiba.db"):
    from scraper import NetkeibaScraper
    s = NetkeibaScraper()
    c = sqlite3.connect(db)
    rec = open(record, "a", encoding="utf-8") if record else None
    for y in years:
        rids = [r[0] for r in c.execute("""
            SELECT DISTINCT ra.race_id FROM races ra JOIN results res ON ra.race_id = res.race_id
            WHERE ra.race_date LIKE ? || '%' AND res.finish_position > 0
              AND (COALESCE(res.passing_order, '') = '' OR COALESCE(res.trainer_id, '') = '')
            ORDER BY ra.race_id""", (y,))]
        print(f"=== {y}: {len(rids)} races に欠落 (通過順 or 厩舎) ===", flush=True)
        done = fixed = 0
        for rid in rids:
            try:
                d = s.scrape_race_result_archive(rid)
                for r in (d.get("results") if d else []) or []:
                    u = {"race_id": rid, "horse_number": r.get("horse_number"),
                         "passing_order": r.get("passing_order") or "",
                         "last_3f": r.get("last_3f") or 0,
                         "trainer_id": r.get("trainer_id") or "",
                         "trainer_name": r.get("trainer_name") or ""}
                    if not (u["passing_order"] or u["trainer_id"]):
                        continue
                    fixed += apply_row(c, u)
                    if rec:
                        rec.write(json.dumps(u, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"  ⚠️ {rid}: {e}", flush=True)
            done += 1
            if done % 50 == 0:
                c.commit()
                if rec:
                    rec.flush()
                print(f"  {y}: {done}/{len(rids)} races / {fixed} rows", flush=True)
            time.sleep(1.0)
        c.commit()
        print(f"=== {y} 完了: {fixed} rows 補完 ===", flush=True)
    if rec:
        rec.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("years", nargs="*", default=[])
    ap.add_argument("--record", help="取得した値を JSONL に追記する")
    ap.add_argument("--apply", help="JSONL の記録を keiba.db に当て直す (取得はしない)")
    args = ap.parse_args()
    if args.apply:
        cmd_apply(args.apply)
    else:
        cmd_fetch(args.years or ["2021", "2022", "2023"], record=args.record)


if __name__ == "__main__":
    main()
