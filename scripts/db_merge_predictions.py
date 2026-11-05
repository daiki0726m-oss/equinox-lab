#!/usr/bin/env python3
"""#169: origin/main の predictions_cache (予測キャッシュ) を local の keiba.db に取り込む。

## 背景 (2026-10-31 / 11-01 の反省 §2-1)
race_day_runner は起動時 (08:3x) に一度だけ origin へ reset し、以後は手元の keiba.db を
固めて `-X theirs` (= 自分の版を優先) で push し、予測 JSON も毎 iter その DB から書き出していた。
そのため 08:3x 以降に別 workflow が作った再予測 (odds_flash 09:4x の「オッズを反映した再予測」を
含む) は、JSON では数分、DB でも次の push で必ず巻き戻り、post_predict (10:2x) は
「runner 起動前の最後の予測」を配信していた。仕組み上 #68 (6/13) 以降の毎開催日。
post_predict が書いた seal (posted_at) も同じ push で消え、refresh_odds の投稿時点凍結 (#99) が
効かずに cache のオッズが発走直前の値になっていた (#70 の単一バイナリ clobber と同型)。

## やること
local (これから push する DB) に、origin の行を次の規則で取り込む。
  1. local に無い行 → 取り込む (race_id が local の races にある時だけ。無い行は孤児になるので見送る)。
     sync_cache_race_ids で片側だけが別 ID に付け替えたレース (同じ場・回・R・日付の行が local に
     別 ID である) は行を増やさず、2 の規則で中身を選んで local の ID の行に写す
     (origin 側にも同じレースが2つの ID である時は判断できないので見送る)
  2. 両方にある行は「どちらの内容を残すか」を決める:
     a. 投稿記録 (docs/data/posted_marks_YYYYMMDD.json サイドカー) に載ったレース → 投稿した印と
        投稿時オッズ (odds_win_at_post) によく一致する版 (印・オッズとも完全一致 = 投稿した版そのもの)
     b. 投稿版 (posted_marks_json あり = post_predict が実際に投稿した行) が片方だけ → その版
        (投稿したレースの予測は以後どの経路でも書き換えない = #169 の性質 (b))
     c. それ以外 → created_at が新しい方 (同時刻なら local)
     posted_at / posted_marks_json は「非 NULL を優先」(残した版の値 → 無ければもう一方の値)
  3. サイドカーに載っているのに posted_at が NULL の行 → サイドカーから seal を復元する
     (サイドカーは git の text なので rebase で消えない。DB の seal が並行 push で消えても
      ここで毎回作り直せる = 性質 (c))。posted_marks_json は投稿した版そのもの (印もオッズも
      完全一致) にだけ付ける (投稿後に取り直したオッズの版を「投稿版」にしない)

created_at / posted_at は UTC の CURRENT_TIMESTAMP 形式 ('YYYY-MM-DD HH:MM:SS')。
古い posted_at には JST の ISO 文字列 ('...T10:25:00+09:00', post_x の旧 _ts) が混ざっているので、
比較は文字列でなく UTC に直してから行い、この処理で書く値も UTC 形式に揃える。

## 使い方
  # db_pack.sh (CI) から: commit 直前に origin を取り込む (どの workflow の DB push でも。
  # race_day_runner = env DB_MERGE_STATE ありの時は runner 自身が取り込むので db_pack は呼ばない)
  python3 scripts/db_merge_predictions.py --ref origin/main
  # race_day_runner の iter 頭: 未来レースの補填 + サイドカーの反映も行う
  python3 scripts/db_merge_predictions.py --ref origin/main --future-races --sync-sidecar 20261101
  # race_day_runner の commit 直前 / pull --rebase の後: origin が取り込み済みの版から
  # 動いた時だけやり直す (版は env DB_MERGE_STATE のファイルに記録)
  python3 scripts/db_merge_predictions.py --ref origin/main --if-blob-changed --future-races --sync-sidecar 20261101
  # テスト: 展開済みの DB を origin 役に使う
  python3 scripts/db_merge_predictions.py --origin-db /path/origin.db --local /path/local.db

終了コード: 0 = 何も変えていない / 3 = local を変えた (呼び出し側が JSON を書き出し直す目印)。
失敗しても push を止めないよう、例外は握りつぶして 0 で終わる (db_guard_future_races と同じ方針)。
"""
import argparse
import gzip
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIGIN_TMP = "/tmp/_origin_keiba_merge_predictions.db"
EXIT_CHANGED = 3
JST = timezone(timedelta(hours=9))


# ─── 時刻 ────────────────────────────────────────────────────────────────
def parse_ts(s):
    """created_at / posted_at を UTC の naive datetime にする。読めなければ None。
    'YYYY-MM-DD HH:MM:SS' (CURRENT_TIMESTAMP = UTC) と、旧 post_x の JST ISO
    ('2026-08-29T10:10:53.698981+09:00') の両方を受ける。オフセット無しは UTC とみなす。"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def norm_ts(s):
    """書き込む posted_at を UTC の CURRENT_TIMESTAMP 形式に揃える。読めない値はそのまま返す。"""
    dt = parse_ts(s)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else s


def _newer(a, b):
    """a が b より新しいか (どちらも読めなければ False = local を保つ)。"""
    da, db = parse_ts(a), parse_ts(b)
    if da is None:
        return False
    if db is None:
        return True
    return da > db


# ─── git / サイドカー ──────────────────────────────────────────────────────
def _git(*args, binary=False, timeout=120):
    r = subprocess.run(["git", *args], capture_output=True, timeout=timeout, cwd=REPO)
    if r.returncode != 0:
        return None
    return r.stdout if binary else r.stdout.decode("utf-8", "replace").strip()


def blob_id(ref):
    return _git("rev-parse", f"{ref}:keiba.db.gz")


def state_key(ref, dates):
    """取り込んだ origin の版を表す鍵 = keiba.db.gz と投稿記録サイドカーの blob。
    サイドカーだけが先に届く (DB の push が clobber された) 場合も「動いた」と見なすため、
    DB の blob だけでなくサイドカーの blob も含める。"""
    db = blob_id(ref)
    if not db:
        return None
    scs = [(_git("rev-parse", f"{ref}:{_sidecar_rel(d)}") or "-") for d in sorted(dates)]
    return "|".join([db, *scs])


def extract_origin(ref, dest=ORIGIN_TMP):
    """ref の keiba.db.gz を dest に展開する。取れなければ None。"""
    blob = _git("show", f"{ref}:keiba.db.gz", binary=True)
    if not blob:
        return None
    try:
        if os.path.exists(dest):
            os.remove(dest)
        with open(dest, "wb") as f:
            f.write(gzip.decompress(blob))
    except Exception as e:
        print(f"🔄 #169 予測の取り込み: origin DB の展開に失敗 ({e}) → skip")
        return None
    return dest


def _sidecar_rel(date_yyyymmdd):
    return f"docs/data/posted_marks_{date_yyyymmdd}.json"


def load_sidecars(dates, ref=None):
    """{date: {"posted_at": str, "races": {race_id: [marks]}}}。
    作業ツリーのファイルと ref の版を合わせる (同じレースは ref の版を優先 = origin が正本)。"""
    out = {}
    for d in dates:
        merged = {"posted_at": None, "races": {}}
        srcs = []
        p = os.path.join(REPO, _sidecar_rel(d))
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    srcs.append(json.load(f))
            except Exception:
                pass
        if ref:
            txt = _git("show", f"{ref}:{_sidecar_rel(d)}")
            if txt:
                try:
                    srcs.append(json.loads(txt))
                except Exception:
                    pass
        for s in srcs:          # 後の (= ref の) 版で上書き
            if not isinstance(s, dict):
                continue
            if s.get("posted_at"):
                merged["posted_at"] = s["posted_at"]
            for rid, marks in (s.get("races") or {}).items():
                merged["races"][rid] = marks or []
        if merged["races"]:
            out[d] = merged
    return out


def sync_sidecar(date_yyyymmdd, ref):
    """runner 用: ref のサイドカーを作業ツリーへ反映する。
    refresh_odds / is_prediction_sealed は作業ツリーのファイルを読むので、runner が次に
    pull するまでの間も「投稿済み」を見えるようにする。runner は posted_marks を書かないので、
    origin の版で上書きして良い。反映したら True。"""
    # バイト単位でそのまま写す (改行1つでも違うと runner の commit がこのファイルを「変更」し、
    # 後で post_predict が書き直した時に -X theirs が runner の古い版を残してしまう)
    raw = _git("show", f"{ref}:{_sidecar_rel(date_yyyymmdd)}", binary=True)
    if not raw:
        return False
    p = os.path.join(REPO, _sidecar_rel(date_yyyymmdd))
    try:
        cur = open(p, "rb").read() if os.path.exists(p) else None
    except Exception:
        cur = None
    if cur == raw:
        return False
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(raw)
    print(f"📌 #169: origin の投稿記録を作業ツリーへ反映: {_sidecar_rel(date_yyyymmdd)}")
    return True


# ─── 内容の比較 ───────────────────────────────────────────────────────────
def _mark_pairs(predictions_json):
    try:
        preds = json.loads(predictions_json) if isinstance(predictions_json, str) else (predictions_json or [])
    except Exception:
        return {}, {}
    pairs, odds = {}, {}
    for h in preds or []:
        m = h.get("mark")
        if m:
            pairs.setdefault(m, set()).add(h.get("horse_number"))
        odds[h.get("horse_number")] = h.get("odds_win")
    return pairs, odds


def sidecar_score(predictions_json, marks):
    """その版がサイドカー (投稿した印) とどれだけ一致するか。
    (印が全部一致したか, 投稿時オッズと cache のオッズが一致した頭数) の組で返す。"""
    if not marks:
        return (0, 0)
    pairs, odds = _mark_pairs(predictions_json)
    want = {}
    for m in marks:
        want.setdefault(m.get("mark"), set()).add(m.get("horse_number"))
    same = all(pairs.get(k, set()) == v for k, v in want.items())
    # post_x は odds_win_at_post を `_p.get('odds_win', 0)` で書く (オッズ欠落は 0)。
    # cache 側も None/欠落を 0 として比べる (その馬が予測に居ることは必須)。
    n_odds = sum(1 for m in marks
                 if m.get("odds_win_at_post") is not None
                 and m.get("horse_number") in odds
                 and abs(float(odds.get(m.get("horse_number")) or 0)
                         - float(m.get("odds_win_at_post") or 0)) < 1e-6)
    return (1 if same else 0, n_odds)


def is_exact_post_version(predictions_json, marks):
    """#169: その版が「投稿した版そのもの」か (印が全部一致 + 印の馬の cache オッズが投稿時オッズと全部一致)。

    post_predict は cache の predictions_json から印と odds_win_at_post を取るので、投稿した版は
    必ず完全一致する。印だけ一致してオッズが違う版は「投稿の前後に runner の refresh_odds が
    取り直した版」(投稿記録が DB より先に runner に届いた時に起きる) で、投稿版ではない。"""
    if not marks:
        return False
    need = sum(1 for m in marks if m.get("odds_win_at_post") is not None)
    return sidecar_score(predictions_json, marks) == (1, need)


# ─── 本体 ────────────────────────────────────────────────────────────────
def merge_predictions(local_path, origin_path, sidecars=None):
    """origin_path の predictions_cache を local_path に取り込む。統計 dict を返す。"""
    sidecars = sidecars or {}
    posted = {}                       # race_id -> (posted_at, marks)
    for _d, sc in sidecars.items():
        for rid, marks in (sc.get("races") or {}).items():
            posted[rid] = (sc.get("posted_at"), marks)

    st = {"inserted": 0, "replaced": 0, "kept_posted": 0, "chose_by_sidecar": 0,
          "seal_from_origin": 0, "seal_from_sidecar": 0, "skipped_no_race": 0, "skipped_renamed": 0,
          "sidecar_mismatch": 0, "sidecar_odds_mismatch": 0, "details": []}
    conn = sqlite3.connect(local_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("ATTACH DATABASE ? AS o", (origin_path,))
        lcols = [r[1] for r in conn.execute("PRAGMA main.table_info(predictions_cache)")]
        ocols = [r[1] for r in conn.execute("PRAGMA o.table_info(predictions_cache)")]
        if not lcols or not ocols:
            return st
        cols = [c for c in lcols if c in ocols]
        has_pm_l = "posted_marks_json" in lcols
        has_pm_o = "posted_marks_json" in ocols
        om = "o.posted_marks_json" if has_pm_o else "NULL"
        lm = "l.posted_marks_json" if has_pm_l else "NULL"
        has_races = conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name='races'").fetchone()
        race_join = ("LEFT JOIN main.races r ON r.race_id = o.race_id" if has_races
                     else "LEFT JOIN (SELECT NULL AS race_id, NULL AS race_date) r ON 0")
        rows = conn.execute(f"""
            SELECT o.race_id AS rid, o.created_at AS oc, l.created_at AS lc,
                   o.posted_at AS op, l.posted_at AS lp,
                   {om} AS om, {lm} AS lm,
                   (l.race_id IS NOT NULL) AS lexists,
                   (r.race_id IS NOT NULL) AS has_race, r.race_date AS race_date
            FROM o.predictions_cache o
            LEFT JOIN main.predictions_cache l ON l.race_id = o.race_id
            {race_join}
        """).fetchall()

        col_list = ",".join(f'"{c}"' for c in cols)
        # 付け替え済みの別 ID 行に origin の内容を写す時用 (race_id は local の ID のまま)
        set_list = ",".join(f'"{c}"' for c in cols if c != "race_id")
        lsel = ("SELECT race_id, created_at, posted_at, "
                f"{'posted_marks_json' if has_pm_l else 'NULL'} AS pm "
                "FROM main.predictions_cache WHERE race_id = ?")
        for r in rows:
            rid = r["rid"]
            lrid = rid                     # この origin 行と突き合わせる local の行
            lc, lp, lm = r["lc"], r["lp"], r["lm"]
            if not r["lexists"]:
                if not r["has_race"]:
                    st["skipped_no_race"] += 1
                    continue
                # sync_cache_race_ids (netkeiba の日目が朝と昼で変わる対策) で片側だけが別 ID に
                # 付け替えたレース (同じ場・回・R・日付の行が local に別 ID である)。旧 ID 版を
                # そのまま INSERT すると同じレースの cache が2行になるので、行は増やさない。
                # #169 レビュー: 旧実装は向きを見ずに常に見送っていたため、「origin が新 ID で
                # 新しい版・local は旧 ID の古い版」の時に origin の新しい版を捨て、同じ iter の
                # sync_cache_race_ids が local の古い版を新 ID に付け替えて push → origin の
                # 新しい版が永久に失われた。→ 中身は通常と同じ規則で選び、local の ID の行に写す
                # (ID は local の races に合わせる sync_cache_race_ids に任せる)。
                sib = conn.execute(
                    "SELECT pc.race_id FROM main.predictions_cache pc JOIN main.races rr ON rr.race_id = pc.race_id "
                    "WHERE substr(pc.race_id,5,4) = substr(?,5,4) AND substr(pc.race_id,11,2) = substr(?,11,2) "
                    "AND rr.race_date = ? AND pc.race_id <> ? ORDER BY pc.created_at DESC LIMIT 1",
                    (rid, rid, r["race_date"], rid)).fetchone()
                if sib is None:
                    conn.execute(f"INSERT INTO main.predictions_cache ({col_list}) "
                                 f"SELECT {col_list} FROM o.predictions_cache WHERE race_id = ?", (rid,))
                    if "posted_at" in cols and r["op"]:
                        conn.execute("UPDATE main.predictions_cache SET posted_at = ? WHERE race_id = ?",
                                     (norm_ts(r["op"]), rid))
                    st["inserted"] += 1
                    st["details"].append((r["race_date"], rid, "新規"))
                    continue
                if conn.execute("SELECT 1 FROM o.predictions_cache WHERE race_id = ?",
                                (sib["race_id"],)).fetchone():
                    # origin 側にも同じレースが2つの ID である = どちらが正か判断できない。
                    # local の行は origin の同じ ID の行と通常どおり突き合わせる (ここでは見送る)。
                    st["skipped_renamed"] += 1
                    continue
                srow = conn.execute(lsel, (sib["race_id"],)).fetchone()
                lrid, lc, lp, lm = srow["race_id"], srow["created_at"], srow["posted_at"], srow["pm"]

            marks = (posted.get(rid) or posted.get(lrid) or (None, None))[1]
            same_ts = (r["oc"] == lc)
            need_seal = (r["op"] is not None and lp is None) or \
                        (r["om"] is not None and lm is None)
            if same_ts and not need_seal and not marks and lrid == rid:
                continue            # 大半の行はここ (同じ版)

            # どちらの内容を残すか
            winner, why = "local", ""
            o_post, l_post = r["om"] is not None, lm is not None
            if marks:
                # 投稿記録 (サイドカー) がある = 投稿した版の中身が分かる。posted_marks_json の
                # 有無より先に「投稿記録と一致する版」で決める (#169 レビュー: 投稿記録が DB より
                # 先に runner に届くと、runner が取り直したオッズの版にも投稿印が付き得た。
                # 印・オッズとも完全一致する版が投稿版)。同点なら下の規則に回す。
                pj = conn.execute(
                    "SELECT (SELECT predictions_json FROM o.predictions_cache WHERE race_id = ?) AS oj, "
                    "       (SELECT predictions_json FROM main.predictions_cache WHERE race_id = ?) AS lj",
                    (rid, lrid)).fetchone()
                so = sidecar_score(pj["oj"], marks)
                sl = sidecar_score(pj["lj"], marks)
                if so != sl and max(so, sl)[0] == 1:
                    winner, why = ("origin" if so > sl else "local"), "投稿記録と一致"
                    if winner == "origin":
                        st["chose_by_sidecar"] += 1
            if not why and o_post != l_post:
                winner, why = ("origin" if o_post else "local"), "投稿版"
            if not why:
                winner = "origin" if _newer(r["oc"], lc) else "local"
                why = "新しい方" if winner == "origin" else ""
            if winner == "origin" and lrid != rid:
                why += f" / local の付け替え済み ID {lrid} に写す"

            # seal / 投稿印は非 NULL を優先
            if winner == "origin":
                posted_at = r["op"] if r["op"] is not None else lp
                marks_json = r["om"] if r["om"] is not None else lm
            else:
                posted_at = lp if lp is not None else r["op"]
                marks_json = lm if lm is not None else r["om"]

            if winner == "origin":
                if lrid == rid:
                    conn.execute(
                        f"UPDATE main.predictions_cache SET ({col_list}) = "
                        f"(SELECT {col_list} FROM o.predictions_cache WHERE race_id = ?) "
                        f"WHERE race_id = ?", (rid, rid))
                else:
                    conn.execute(
                        f"UPDATE main.predictions_cache SET ({set_list}) = "
                        f"(SELECT {set_list} FROM o.predictions_cache WHERE race_id = ?) "
                        f"WHERE race_id = ?", (rid, lrid))
                st["replaced"] += 1
                st["details"].append((r["race_date"], rid, f"origin の版 ({why})"))
            elif why in ("投稿版", "投稿記録と一致"):
                st["kept_posted"] += 1      # 投稿した版を守った (性質 (b))
            elif lrid != rid:
                st["skipped_renamed"] += 1  # local の付け替え済み行の方が新しい = origin の旧 ID 版は戻さない

            sets, vals = [], []
            if "posted_at" in lcols and (winner == "origin" or posted_at != lp):
                sets.append("posted_at = ?")
                vals.append(norm_ts(posted_at) if posted_at is not None else None)
            if has_pm_l and (winner == "origin" or marks_json != lm):
                sets.append("posted_marks_json = ?")
                vals.append(marks_json)
            if sets:
                conn.execute(f"UPDATE main.predictions_cache SET {', '.join(sets)} WHERE race_id = ?",
                             (*vals, lrid))
            if winner == "local" and (posted_at != lp or marks_json != lm):
                st["seal_from_origin"] += 1
                st["details"].append((r["race_date"], lrid, "seal を origin から引き継ぎ"))

        # サイドカー (投稿記録) から seal を復元
        for rid, (sc_posted_at, marks) in posted.items():
            row = conn.execute(
                f"SELECT predictions_json, posted_at, {'posted_marks_json' if has_pm_l else 'NULL'} AS pm "
                f"FROM main.predictions_cache WHERE race_id = ?", (rid,)).fetchone()
            if row is None:
                continue
            sets, vals = [], []
            if row["posted_at"] is None:
                sets.append("posted_at = ?")
                vals.append(norm_ts(sc_posted_at) or
                            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
            if has_pm_l and row["pm"] is None and marks:
                # posted_marks_json は「この行の内容 = 投稿した版」の印にもなる (規則 2a)。
                # 投稿した版そのもの (印もオッズも完全一致) にだけ付け、それ以外は seal だけ付ける。
                # #169 レビュー: 旧実装は印の一致だけで付けていたため、投稿記録が post_predict の
                # DB より先に runner に届くと、runner が取り直したオッズの版が「投稿版」になり、
                # 後から届いた本物の投稿版 (投稿時オッズ) に勝って origin を上書きしていた。
                # seal は付くので、この間も refresh_odds / predict --force は書き換えない。
                if is_exact_post_version(row["predictions_json"], marks):
                    sets.append("posted_marks_json = ?")
                    vals.append(json.dumps(marks, ensure_ascii=False))
                elif sidecar_score(row["predictions_json"], marks)[0] == 1:
                    st["sidecar_odds_mismatch"] += 1   # 印は一致・オッズだけ違う (投稿版の到着待ち)
                else:
                    st["sidecar_mismatch"] += 1
            if sets:
                conn.execute(f"UPDATE main.predictions_cache SET {', '.join(sets)} WHERE race_id = ?",
                             (*vals, rid))
                st["seal_from_sidecar"] += 1
                st["details"].append((None, rid, "投稿記録から seal を復元"))
        conn.commit()
    finally:
        conn.close()
    return st


def changed(st):
    return any(st.get(k) for k in ("inserted", "replaced", "seal_from_origin", "seal_from_sidecar"))


def _skips(st):
    return ((f" / race 無しで見送り {st['skipped_no_race']}" if st.get("skipped_no_race") else "")
            + (f" / 付け替え済みの旧IDで見送り {st['skipped_renamed']}" if st.get("skipped_renamed") else ""))


def report(st, label):
    if not changed(st):
        print(f"🔄 #169 予測の取り込み ({label}): 差分なし (local は origin の予測を全部持っている)"
              + _skips(st))
        return
    print(f"🔄 #169 予測の取り込み ({label}): 新規 {st['inserted']} / origin の版に更新 {st['replaced']}"
          f" (うち投稿記録で判定 {st['chose_by_sidecar']}) / 投稿版を保持 {st['kept_posted']}"
          f" / seal 引き継ぎ {st['seal_from_origin']} / サイドカーから seal 復元 {st['seal_from_sidecar']}"
          + _skips(st)
          + (f" / ⚠️ 投稿印と内容が違う行 {st['sidecar_mismatch']}" if st["sidecar_mismatch"] else "")
          + (f" / 印は一致・オッズが投稿時と違う行 {st['sidecar_odds_mismatch']} (seal のみ。投稿版の到着待ち)"
             if st.get("sidecar_odds_mismatch") else ""))
    det = sorted(st["details"], key=lambda x: (str(x[0] or ""), x[1]), reverse=True)
    for d, rid, what in det[:12]:
        print(f"    {d} {rid}: {what}")
    if len(det) > 12:
        print(f"    … ほか {len(det) - 12} 件")


def default_dates():
    today = datetime.now(JST)
    return [(today - timedelta(days=1)).strftime("%Y%m%d"), today.strftime("%Y%m%d")]


def main():
    ap = argparse.ArgumentParser(description="#169: origin の予測キャッシュを local に取り込む")
    ap.add_argument("--ref", default="origin/main", help="取り込む元 (git ref)")
    ap.add_argument("--origin-db", help="展開済みの origin DB (テスト用。指定時は --ref を使わない)")
    ap.add_argument("--local", default=os.path.join(REPO, "keiba.db"))
    ap.add_argument("--date", action="append", default=[],
                    help="投稿記録 (サイドカー) を読む日 YYYYMMDD。既定は今日と昨日 (JST)")
    ap.add_argument("--future-races", action="store_true",
                    help="db_guard_future_races の未来レース補填も行う (runner の iter 頭用)")
    ap.add_argument("--sync-sidecar", metavar="YYYYMMDD",
                    help="ref の投稿記録を作業ツリーへ反映する (runner 用)")
    ap.add_argument("--state-file", default=os.environ.get("DB_MERGE_STATE"),
                    help="取り込んだ origin の版を記録するファイル (env DB_MERGE_STATE)")
    ap.add_argument("--if-blob-changed", action="store_true",
                    help="state-file の版 (DB とサイドカーの blob) から origin が動いていなければ何もしない。"
                         "state-file が無ければ常に取り込む")
    args = ap.parse_args()

    if not os.path.exists(args.local):
        print(f"🔄 #169 予測の取り込み: {args.local} が無い → skip")
        return 0

    did = False
    if args.sync_sidecar and not args.origin_db:
        did = sync_sidecar(args.sync_sidecar, args.ref) or did

    dates = args.date or default_dates()
    bid = None
    if args.origin_db:
        origin = args.origin_db
        label = os.path.basename(origin)
    else:
        bid = state_key(args.ref, dates)
        if not bid:
            print(f"🔄 #169 予測の取り込み: {args.ref}:keiba.db.gz が無い → skip")
            return EXIT_CHANGED if did else 0
        if args.if_blob_changed and args.state_file and os.path.exists(args.state_file):
            try:
                if open(args.state_file).read().strip() == bid:
                    print(f"🔄 #169 予測の取り込み: origin は取り込み済みの版 ({bid[:8]}) → skip")
                    return EXIT_CHANGED if did else 0
            except Exception:
                pass
        origin = extract_origin(args.ref)
        if not origin:
            print(f"🔄 #169 予測の取り込み: {args.ref} の DB を取れない → skip")
            return EXIT_CHANGED if did else 0
        label = f"{args.ref} {bid[:8]}"

    if args.future_races:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from db_guard_future_races import merge_future_races
            today = datetime.now(JST).strftime("%Y-%m-%d")
            added = merge_future_races(args.local, origin, today)
            if added:
                did = True
                print(f"🛡 future-race guard: origin から未来レース {len(added)}件を補填")
        except Exception as e:
            print(f"🛡 future-race guard: skip ({e})")

    sidecars = load_sidecars(dates, ref=None if args.origin_db else args.ref)
    st = merge_predictions(args.local, origin, sidecars)
    report(st, label)
    if args.state_file and bid:
        try:
            with open(args.state_file, "w") as f:
                f.write(bid)
        except Exception:
            pass
    return EXIT_CHANGED if (did or changed(st)) else 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:
        print(f"🔄 #169 予測の取り込み: 予期せぬエラー ({e}) → skip (push は継続)")
        rc = 0
    sys.exit(rc)
