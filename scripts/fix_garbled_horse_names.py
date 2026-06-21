#!/usr/bin/env python3
"""#90: horses テーブルの文字化け馬名 (U+FFFD 含む) を、修正済 scraper で再取得して直す。

## 背景
netkeiba の UTF-8 移行に scraper が未追従だった時期 (#85 修正前、~2026-06-17) に
登録された馬名が `INSERT OR IGNORE` で上書きされず文字化けのまま残った
(2026-06-21 に函館の 2歳新馬などで「6番が文字化け」とユーザー指摘)。
scraper は #85 で UTF-8 対応済なので、化け馬を含むレースの出馬表を引き直せば正しい名が取れる。
saver 側も #90 で「化け名なら上書き」UPSERT 化済 → 今後は自己修復するが、既存分はこれで一括修復。

使い方: python3 scripts/fix_garbled_horse_names.py
"""
import sys
import time

sys.path.insert(0, ".")
from database import get_db  # noqa: E402
from scraper import NetkeibaScraper  # noqa: E402

GARBLE = "%' || CHAR(65533) || '%"  # U+FFFD を含む = 文字化け


def main():
    with get_db() as conn:
        garbled = [r[0] for r in conn.execute(
            "SELECT horse_id FROM horses WHERE horse_name LIKE '%' || CHAR(65533) || '%'"
        ).fetchall()]
        if not garbled:
            print("✅ 文字化け馬名なし")
            return
        ph = ",".join("?" * len(garbled))
        races = [r[0] for r in conn.execute(
            f"SELECT DISTINCT race_id FROM results WHERE horse_id IN ({ph})", garbled
        ).fetchall()]
    print(f"🔧 文字化け {len(garbled)}頭 / 含むレース {len(races)}件を再取得")

    scraper = NetkeibaScraper()
    fixed = 0
    for rid in races:
        try:
            data = scraper.scrape_shutuba(rid)
        except Exception as e:
            print(f"  ⚠️ {rid}: scrape失敗 {e}")
            continue
        if not data:
            continue
        with get_db() as conn:
            for e in data.get("entries", []):
                hid = e.get("horse_id")
                nm = (e.get("horse_name") or "").strip()
                # 正しい名 (化けてない・非空) のみで、化け/空の既存名を上書き
                if hid and nm and "�" not in nm:
                    cur = conn.execute(
                        "UPDATE horses SET horse_name=? WHERE horse_id=? AND "
                        "(horse_name LIKE '%' || CHAR(65533) || '%' OR horse_name='' OR horse_name IS NULL)",
                        (nm, hid))
                    fixed += cur.rowcount
        time.sleep(1)

    with get_db() as conn:
        remain = conn.execute(
            "SELECT COUNT(*) FROM horses WHERE horse_name LIKE '%' || CHAR(65533) || '%'"
        ).fetchone()[0]
    print(f"✅ 修復 {fixed}頭 / 残り文字化け {remain}頭")


if __name__ == "__main__":
    main()
