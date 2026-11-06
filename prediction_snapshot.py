"""予測時点のスナップショット (#169)。

なぜ必要か
----------
2026-11-05 の週次反省 (10/31・11/01) で次の2点が確定した。

1. LGB は確定オッズ (results.odds) で学習・評価されているのに、本番は朝の薄い
   オッズで予測している。ところが「予測した時点のオッズ」がどこにも残っていない。
   predictions_cache のオッズは refresh_odds が発走直前まで上書きし (#99 の凍結は
   seal が runner の push で消えるため効かない)、9時台の再予測は race_day_runner の
   push で丸ごと巻き戻される。予測時オッズで学習した場合との比較も、
   「☆/注 は朝の人気からのズレにすぎないのか」の検証も、材料が無い。
2. 予想の記録にモデル・点数表・基準表の版が残っていない。再学習のたびに
   ☆ 約15%・注 約29% が同じ入力で入れ替わる (#17 の反省) のに、
   ☆/注 の成績をモデルの版ごとに分けて集計できない。

そこで predict の1回の run ごとに、その run が予測した全レース・全頭の
「予測した瞬間の値」と、使ったモデル・点数表・基準表の版 (sha256) とフラグを
テキストで残す。

保存の設計
----------
- 正本は git tracked のテキスト (docs/data/snapshots/YYYYMMDD/HHMMSS_<runid>.json)。
  DB (keiba.db.gz) は並行 workflow が丸ごと上書きする (#70/#101/#141) ので使わない。
- **1 run = 1 ファイル**。複数 workflow が同じファイルを編集すると rebase の
  -X theirs で片方が黙って落ちる (#70 と同じ型)。新規ファイルだけを足す形なら
  rebase は必ずクリーンに通る。
- 改行なしの compact JSON。馬は列名を1回だけ書いた配列 (horse_cols + 行) にする。
  リポジトリ容量の制約 (#153) があるので、1開催日あたり 1MB を超えない設計。
  実測 (DB コピーの cache から作成、horse_id 等を足した後): 1 run = 24R 44KB / 36R 64KB
  (zlib 後 14KB / 20KB)。1開催日の予測は 7〜9回 (10/31・11/01 の run ログ) なので
  1日 ~0.6MB・git 上 ~0.2MB。
  直前の版と中身が同じレースは書かない (重複除去)。1日 1MB を超えたら警告を出す。
- 書くのは既定で CI (GITHUB_ACTIONS=true) のときだけ。ローカルの手作業・過去日の
  再予測・バックテストで git tracked な場所を汚さないため。
    PREDICTION_SNAPSHOT=1 … どこでも書く / =0 … CI でも書かない
    PREDICTION_SNAPSHOT_DIR … 出力先を変える (テスト用)
    PREDICTION_SNAPSHOT_ALLOW_PAST=1 … 過去日 (JST の今日より前) のレースも書く
- 予測を skip したレース (オッズ充足ゲート・seal 等) は書かない。
- 予測した時点で発走済みのレース (当日の発走後の --force 再予測 = 幽霊馬除去の
  --repredict、preflight の auto-fix、9時台に遅れて動いた朝の予測の R1 等) も書かない。
  発売締切後のオッズで作り直した版は「予測時点」ではなく、レースごとに最新版を取ると
  確定に近いオッズが混ざる (#99 と同じ look-ahead)。過去日を書かないのと同じ扱い。
  PREDICTION_SNAPSHOT_ALLOW_PAST=1 の時だけ post_start=1 を付けて書き、読み出し側は
  既定でそれを読まない。
- 予測本体を止めないため、ここでの失敗は警告だけ出して握る。

値の出どころ
------------
predict.cmd_predict が predictions_cache に書いた直後 (同じプロセスの末尾) に、
その run で予測したレースの cache 行・races・results を読み直して作る。
同じプロセスの中で cache を書き換えるものは他に無いので、これは予測した瞬間の値。
レースごとの created_at (cache の書き込み時刻、UTC → JST に変換) を予測時刻とする。
created_at がこの run の時刻でない (2時間より古い) 行は、書き込み時刻で代用して
time_src="written_at" を付ける (未来の版を「より前」に混ぜない側に倒れる)。

記録されるのは「predict が計算して cache に書いた版」であって、配信された版とは限らない。
9時台の再予測が runner に巻き戻された日 (#169 の反省1) や、push に失敗した run の版も残る。
配信した版を引くときは at_posting を使う。投稿印サイドカー (posted_marks) に投稿した版の
cache 時刻 (pred_created_at) があればそれと一致する版を返す。無い日 (導入前・Pages JSON
経由の投稿) は「印の集合が投稿印と一致する最後の版」で、一致する版が複数あれば
posting_match="ambiguous" (どれを配信したかは特定できない) と明示する。

馬の同定は horse_id で行う。馬番は予測した時点の値で、枠順確定前・仮馬番 (五十音順) の
まま予測が走った版 (#169 の反省2: 11/01 未明) では確定馬番と別の馬を指す。
レースごとの numbers_confirmed (全頭 post_position>0 = 枠順確定後) も残す。

読み出し
--------
    import prediction_snapshot as ps
    ps.list_snapshots("20261101")                 # その日の run 一覧 (時刻順)
    ps.latest_before("20261101", "2026-11-01 10:25")   # race_id → 10:25 より前の最終版
    ps.iter_race_versions("20261101")             # 全版 (重複除去済み)
    ps.at_posting("20261101")                     # 配信した版 (posting_match で確度を示す)
    結果と突き合わせる時は horse_id で結合する (馬番は仮馬番の版で別の馬を指す)。
    ps.latest_before(d, at, confirmed_only=True)  # 枠順確定後 (numbers_confirmed=1) の版だけ
CLI: python3 prediction_snapshot.py list 20261101
     python3 prediction_snapshot.py latest 20261101 --before "2026-11-01 10:25"
     python3 prediction_snapshot.py posted 20261101
"""
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
JST = timezone(timedelta(hours=9))
SCHEMA = 1

# 1頭あたりの列 (この順で配列に入れる)。読み出し側はファイルの horse_cols を正とする。
HORSE_COLS = [
    "horse_number", "horse_name",
    "odds",                 # 予測時の単勝オッズ。実オッズが無く推定値で予測した馬は null
    "popularity",           # 予測時の人気 (システムが使った値。実オッズが無ければ 0)
    "mark",                 # ◎○▲△△☆注 (無印は "")
    "sc_points",            # 点数表 (オッズ非依存) の合計点
    "pred_top3_pct",        # 複勝のレース内シェア (意思決定用: confidence/買い目が使う)
    "pred_top3_abs_pct",    # 校正済みの複勝確率 (表示・再チューニング用)
    "pred_win_pct",         # 勝率 (意思決定用 = 温度1: EV/should_bet)
    "pred_win_display_pct",  # 勝率 (表示用 = z-score 化 #120)
    "rank_score",           # model_rank の生スコア
    "ability_score",        # 能力モデル (オッズ非依存 #72)
    "scratched",            # 予測時点で取消と分かっていたか (1/0)
    # #169: 馬の識別子。馬番は予測時点の値で、仮馬番 (五十音順) のまま予測した版では
    # 確定馬番と別の馬を指す。結果と突き合わせる時は馬番でなくこれで結合する
    "horse_id",
]

# 版を記録するファイル (ROOT からの相対パス)。models/*.pkl は動的に足す。
_VERSIONED_FILES = [
    "models/si_variant_table.json",
    "models/train_boundary.json",
    "docs/data/race_baseline.json",
    "config/prediction_flags.yml",
]

# config に無く env でだけ切り替えるフラグ (flags.get / os.environ で読まれているもの)
_EXTRA_FLAGS = [
    "MARKS_BLEND_W", "MARKS_LEGACY", "CONFIDENCE_SIZING", "BET_KEEP_D",
    "BET_GATE_FIXED", "USE_WHITELIST", "USE_ABILITY_CORE", "SI_VARIANT_TABLE_PATH",
]
# 上に無いフラグを他班が env で足しても拾えるよう、この接頭辞の env は値があれば記録する
# (秘密情報はこの接頭辞を使っていない: X_*/THREADS_*/GITHUB_TOKEN 等。USE_* は
#  無関係な env が多いので接頭辞にせず、上の明示リストだけにする)
_FLAG_PREFIXES = ("MARKS_", "BET_", "CONFIDENCE_")

# 取消をあらわす値 (API の番兵 -3.0倍/9999人気 #149、出馬表の取消行で書く人気 9999 #171)。
# 閾値は race_utils.is_scratch_values (odds<0 / 人気>=999) と揃える。#171: 999.9倍は取消の
# 判定に使わない (前日発売の直後は票の少ない走る馬も 999.9倍になる)
_SCRATCH_POP = 999
# 予測 JSON (cache) に取消フラグが入っている時の候補キー。predict は印の選定に
# "_scratched" を使う (P班 #169)。cache に保存されれば (列挙リストに足されれば) そのまま拾う
_SCRATCH_KEYS = ("scratched", "_scratched", "is_scratched", "cancelled", "torikeshi", "excluded")


# ───────────────────────── 書き込み ─────────────────────────

def enabled():
    """既定は CI のときだけ書く。PREDICTION_SNAPSHOT=1/0 で上書き。"""
    v = (os.environ.get("PREDICTION_SNAPSHOT") or "").strip()
    if v == "1":
        return True
    if v == "0":
        return False
    return os.environ.get("GITHUB_ACTIONS") == "true"


def snapshot_dir():
    return os.environ.get("PREDICTION_SNAPSHOT_DIR") or os.path.join(ROOT, "docs", "data", "snapshots")


def _r(x, nd):
    try:
        if x is None:
            return None
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _ymd(race_date):
    s = (race_date or "").strip().replace("-", "")
    return s if len(s) == 8 and s.isdigit() else ""


def _utc_to_jst_iso(s):
    """predictions_cache.created_at (SQLite datetime('now') = UTC、時差表記なし) を JST ISO に。
    既に時差付き ISO ならそのまま JST に直す。読めなければ元の文字列を返す。"""
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return str(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(JST).isoformat(timespec="seconds")


def _sha12(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:12]
    except OSError:
        return None


def _versions():
    out = {}
    for p in sorted(glob.glob(os.path.join(ROOT, "models", "*.pkl"))):
        out[os.path.relpath(p, ROOT)] = _sha12(p)
    for rel in _VERSIONED_FILES:
        p = os.path.join(ROOT, rel)
        if rel == "models/si_variant_table.json" and os.environ.get("SI_VARIANT_TABLE_PATH"):
            p = os.environ["SI_VARIANT_TABLE_PATH"]   # ml/speed_variant.py と同じ優先順位
        if os.path.exists(p):
            out[rel] = _sha12(p)
    return out


def _flags():
    out = {}
    try:
        import flags as _fl
        names = list(_fl._load().keys()) + _EXTRA_FLAGS
        for n in names:
            v = _fl.get(n, "")
            if v != "":
                out[n] = v
    except Exception as e:
        out["_error"] = str(e)[:80]
    for k, v in os.environ.items():
        if k.startswith(_FLAG_PREFIXES) and k not in out and v != "":
            out[k] = v
    return dict(sorted(out.items()))


def _baseline_meta():
    """基準表 (信頼度・投資ゲートの閾値) は値そのものも残す (版の sha だけでは中身を引けない)。"""
    try:
        with open(os.path.join(ROOT, "docs", "data", "race_baseline.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    conf = d.get("confidence") or {}
    bg = d.get("bet_gate") or {}
    return {
        "generated_at": d.get("generated_at"),
        "confidence_thresholds": conf.get("thresholds"),
        "bet_gate": bg.get("thresholds"),
        "bet_gate_window_matches_model": bg.get("window_matches_model"),
    }


def _head_sha():
    """実際に動いているコードの commit。GITHUB_SHA は run 作成時点の SHA で、多くの workflow は
    起動直後に origin/main へ reset する (#61) ため、実行中のコードと一致しないことがある。"""
    try:
        import subprocess
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                           text=True, timeout=5)
        return p.stdout.strip()[:12] if p.returncode == 0 else None
    except Exception:
        return None


def _run_meta(args=None, scorecard=None):
    env = os.environ
    meta = {
        "head": _head_sha(),
        "sha": env.get("GITHUB_SHA"),
        "run_id": env.get("GITHUB_RUN_ID"),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "event": env.get("GITHUB_EVENT_NAME"),
        "workflow": env.get("GITHUB_WORKFLOW"),
        "ref": env.get("GITHUB_REF_NAME"),
        "argv": sys.argv[1:],
    }
    if args is not None:
        meta["force"] = bool(getattr(args, "force", False))
    sc = None
    if scorecard:
        sc = {"version": scorecard.get("version"),
              "train_until": scorecard.get("train_until"),
              "degraded": scorecard.get("_degraded")}
    return {k: v for k, v in meta.items() if v not in (None, "")}, sc


def _scratch_value(odds, popularity):
    """race_utils.is_scratch_values と同じ述語: 負のオッズ / 人気999以上 (#171 で 999.9倍は外した)。"""
    from race_utils import is_scratch_values
    return is_scratch_values(odds, popularity)


def _is_scratched(h, res_odds, res_pop=None):
    """予測時点で取消と分かっていたか (1/0)。predict が印の候補から外すのと同じ判定にする。

    - results の値 (出馬表の取消行・refresh_odds の照合で書く人気 9999 #171。#171 より前の
      取消馬は 999.9倍・人気=頭数で入っており、予測時点の値だけでは走る馬と区別できない)
    - cache の人気 (オッズ API の番兵 9999。#149/#150 のガードで results には書かれず、
      predict がループ内で API を引いた時だけ cache の popularity に入る — DB コピーでは
      cache の人気 9999 が61頭、全頭 finish_position=0)
    - cache のオッズ (負の値 = 古い版の番兵 -3.0/-2.0)
    - 予測 JSON の取消フラグ (_SCRATCH_KEYS)
    """
    if any(h.get(k) for k in _SCRATCH_KEYS):
        return 1
    if _scratch_value(res_odds, res_pop):
        return 1
    try:
        cache_pop = int(h.get("popularity") or 0)
    except (TypeError, ValueError):
        cache_pop = 0
    if cache_pop >= _SCRATCH_POP:
        return 1
    try:
        ow = float(h.get("odds_win") or 0)
    except (TypeError, ValueError):
        ow = 0.0
    if ow < 0:
        return 1
    return 0


def _funded_bets(all_bets_json):
    """金額の付いた買い目だけ [券種, 組番, 金額] で残す (0円の参考表示は捨てる)。"""
    try:
        d = json.loads(all_bets_json or "{}")
    except ValueError:
        return []
    out = []
    for bt, lines in (d.items() if isinstance(d, dict) else []):
        for b in lines or []:
            amt = b.get("amount") or 0
            if amt:
                out.append([b.get("type") or bt, b.get("detail") or "", int(amt)])
    return out


# cache の created_at を「この run の予測時刻」と信じてよい幅。1回の predict は job の
# timeout (最長50分) 以内に終わるので、それより古い created_at はこの run が書いた行ではない
# (seal 等で skip した行、または cache の保存方式が変わって created_at が保持された場合)。
_RUN_WINDOW = timedelta(hours=2)


def _pred_time(cache_created_at, now):
    """(予測時刻 JST ISO, 出どころ)。created_at が run の幅に入らなければ書き込み時刻で代用する
    (後ろにずれるだけなので「指定時刻より前の版」を引く時に未来の版を混ぜない側に倒れる)。"""
    iso = _utc_to_jst_iso(cache_created_at)
    try:
        t = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        t = None
    if t is not None and now - _RUN_WINDOW <= t <= now + timedelta(minutes=5):
        return iso, "cache"
    return now.isoformat(timespec="seconds"), "written_at"


def _race_start(race_date, start_time):
    """発走時刻 (JST の datetime)。日付か発走時刻が読めなければ None。"""
    ymd = _ymd(race_date)
    st = (start_time or "").strip()
    if not ymd or ":" not in st:
        return None
    try:
        hh, mm = (int(x) for x in st.split(":")[:2])
        return datetime.strptime(ymd, "%Y%m%d").replace(hour=hh, minute=mm, tzinfo=JST)
    except ValueError:
        return None


def build_records(race_ids, conn, now=None):
    """race_ids の cache 行を読んで、レース日ごとのレース記録に組み立てる。"""
    if not race_ids:
        return {}
    now = now or datetime.now(JST)
    ph = ",".join("?" * len(race_ids))
    cache = {r["race_id"]: r for r in conn.execute(
        f"SELECT race_id, predictions_json, all_bets_json, confidence, conf_reason,"
        f" should_bet, bet_reason, created_at FROM predictions_cache WHERE race_id IN ({ph})",
        race_ids)}
    # #169: start_time は「予測した時点で発走済みだったか」の判定と、読み出し側が DB 無しで
    # 「発走前の最終版」を引くために残す
    races = {r["race_id"]: r for r in conn.execute(
        f"SELECT race_id, race_date, start_time, track_condition, weather FROM races"
        f" WHERE race_id IN ({ph})", race_ids)}
    res = {}
    for r in conn.execute(
            f"SELECT race_id, horse_number, horse_id, post_position, odds, popularity"
            f" FROM results WHERE race_id IN ({ph})", race_ids):
        res[(r["race_id"], r["horse_number"])] = (r["odds"], r["popularity"], r["horse_id"],
                                                  r["post_position"])

    by_date = {}
    for rid in race_ids:
        c = cache.get(rid)
        if c is None:
            continue
        try:
            horses = json.loads(c["predictions_json"] or "[]")
        except ValueError:
            continue
        rc = races.get(rid)
        rows = []
        n_pp = 0        # 枠番 (post_position>0) が入っている頭数 = 枠順確定の判定 (#27 と同じ信号)
        for h in sorted(horses, key=lambda x: x.get("horse_number") or 0):
            hn = h.get("horse_number")
            ro, rp, hid, pp = res.get((rid, hn), (None, None, None, None))
            if (pp or 0) > 0:
                n_pp += 1
            # 実オッズで予測したか: predict は results.odds>0 の馬だけ実オッズを使い、
            # それ以外は 0.8/勝率 の推定オッズで計算する (cache の odds_win には区別が無い)
            real = ro is not None and ro > 0
            rows.append([
                hn, h.get("horse_name") or "",
                _r(h.get("odds_win"), 1) if real else None,
                int(h.get("popularity") or 0) if real else 0,
                h.get("mark") or "",
                _r(h.get("sc_points"), 3),
                _r(h.get("pred_top3_pct"), 2),
                _r(h.get("pred_top3_abs_pct"), 2),
                _r(h.get("pred_win_pct"), 2),
                _r(h.get("pred_win_display_pct"), 2),
                _r(h.get("rank_score"), 3),
                _r(h.get("ability_score"), 4),
                _is_scratched(h, ro, rp),
                hid,
            ])
        pt, src = _pred_time(c["created_at"], now)
        st = (rc["start_time"] if rc else None) or ""
        start = _race_start(rc["race_date"] if rc else "", st)
        rec = {
            "race_id": rid,
            "created_at": pt,
            # 投稿した版の特定用 (post_x が posted_marks の pred_created_at に同じ値を残す)。
            # 予測時刻を書き込み時刻で代用した行 (time_src) でも cache の値のまま残す
            "cache_created_at": c["created_at"],
            "start_time": st,
            # 全頭に枠番が入っている = 枠順確定後の馬番。0 の版の馬番は仮番の可能性がある
            "numbers_confirmed": int(bool(rows) and n_pp == len(rows)),
            "track_condition": (rc["track_condition"] if rc else None) or "",
            "weather": (rc["weather"] if rc else None) or "",
            "confidence": c["confidence"],
            "conf_reason": c["conf_reason"] or "",
            "should_bet": int(c["should_bet"] or 0),
            "bet_reason": c["bet_reason"] or "",
            "bets": _funded_bets(c["all_bets_json"]),
            "horses": rows,
        }
        if src != "cache":
            rec["time_src"] = src      # created_at は書き込み時刻での代用 (上の _pred_time)
        # 予測した時点で発走済み (refresh_odds の _started と同じ考え方: 予測時刻 >= 発走時刻)。
        # 発走時刻が分からないレースは判定できないので付けない
        if start is not None and datetime.fromisoformat(pt) >= start:
            rec["post_start"] = 1
        by_date.setdefault(_ymd(rc["race_date"] if rc else ""), []).append(rec)
    return by_date


_TIME_KEYS = ("created_at", "time_src", "cache_created_at")


def _content_key(rec):
    """予測時刻以外が同じなら同じ版とみなすためのキー。"""
    return json.dumps({k: v for k, v in rec.items() if k not in _TIME_KEYS},
                      ensure_ascii=False, sort_keys=True)


def _last_contents(ymd):
    """その日のディレクトリにある各レースの最後の版の中身 (重複記録の除去用)。"""
    last = {}
    try:
        for s in list_snapshots(ymd):
            doc = load_snapshot(s["path"])
            if (doc.get("horse_cols") or HORSE_COLS) != HORSE_COLS:
                continue        # 列が違う版とは比べない (常に書く)
            for rec in doc.get("races") or []:
                last[rec.get("race_id")] = _content_key(rec)
    except Exception:
        return {}
    return last


def _dir_bytes(ymd):
    d = os.path.join(snapshot_dir(), ymd)
    return sum(os.path.getsize(p) for p in glob.glob(os.path.join(d, "*.json")))


# 1開催日あたりの容量の目安 (#153)。超えたら警告だけ出す (記録は止めない)
_DAY_BUDGET_BYTES = 1_000_000


def _out_path(ymd, now):
    d = os.path.join(snapshot_dir(), ymd)
    run_id = os.environ.get("GITHUB_RUN_ID") or "local"
    att = os.environ.get("GITHUB_RUN_ATTEMPT") or ""
    stem = f"{now.strftime('%H%M%S')}_{run_id}" + (f"-{att}" if att and att != "1" else "")
    p = os.path.join(d, stem + ".json")
    if os.path.exists(p):
        p = os.path.join(d, f"{stem}_{os.getpid()}.json")
    return p


def record_run(race_ids, skipped_races=(), args=None, scorecard=None):
    """predict.cmd_predict の末尾から1回だけ呼ぶ。書いたファイルのパスのリストを返す。

    race_ids: その run が対象にした race_id (順序どおり)
    skipped_races: [(race_id, 理由), ...] — 予測しなかったレース。これは記録しない
    """
    try:
        if not enabled():
            return []
        skip = {r for r, _ in (skipped_races or ())}
        predicted = []
        for rid in race_ids or ():
            if rid not in skip and rid not in predicted:
                predicted.append(rid)
        if not predicted:
            print("📸 予測スナップショット: 予測したレースが無いので記録しない")
            return []

        from database import get_db
        now = datetime.now(JST)
        with get_db() as conn:
            by_date = build_records(predicted, conn, now=now)
        n_sub = sum(1 for recs in by_date.values() for r in recs if r.get("time_src"))
        if n_sub:
            print(f"  ⚠️ 予測スナップショット: {n_sub}R は cache の created_at がこの run の時刻でない "
                  f"→ 書き込み時刻で代用 (time_src=written_at)")
        today = now.strftime("%Y%m%d")
        fallback = (getattr(args, "date", None) or "").replace("-", "") if args else ""
        allow_past = os.environ.get("PREDICTION_SNAPSHOT_ALLOW_PAST") == "1"
        run, sc = _run_meta(args, scorecard)
        versions = _versions()
        flags_eff = _flags()
        baseline = _baseline_meta()

        written = []
        for ymd, recs in sorted(by_date.items()):
            ymd = ymd or (fallback if len(fallback) == 8 else "")
            if not ymd:
                print(f"  ⚠️ 予測スナップショット: レース日が不明な {len(recs)}R は記録しない")
                continue
            if ymd < today and not allow_past:
                # 過去日の再予測 (発走後の作り直し・バックテスト) は「予測時点」の記録ではない
                print(f"  📸 予測スナップショット: {ymd} は過去日なので記録しない ({len(recs)}R)")
                continue
            # 同じ日でも、予測した時点で発走済みのレースは過去日と同じく記録しない (#169)。
            # 発走後の --force 再予測 (幽霊馬除去の --repredict・preflight の auto-fix・
            # 9時台に遅れた朝の予測の R1) は発売締切後のオッズで計算されており、
            # 「レースごとの最新版」に混ざると #99 と同じ後出しになる
            n_ps = sum(1 for r in recs if r.get("post_start"))
            if n_ps and not allow_past:
                recs = [r for r in recs if not r.get("post_start")]
                print(f"  📸 予測スナップショット: {ymd} の {n_ps}R は予測時点で発走済みなので記録しない")
                if not recs:
                    continue
            # 直前の版と予測時刻以外が同じレースは書かない (オッズが動かない時間帯の
            # 繰り返し再予測 = preflight の auto-fix 等で同じ中身が積み上がるのを防ぐ #153)
            last = _last_contents(ymd)
            n_all = len(recs)
            recs = [r for r in recs if last.get(r["race_id"]) != _content_key(r)]
            if not recs:
                print(f"  📸 予測スナップショット: {ymd} は {n_all}R すべて直前の版と同じ → 記録しない")
                continue
            doc = {
                "schema": SCHEMA,
                "race_date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}",
                "written_at": now.isoformat(timespec="seconds"),
                "run": run,
                "versions": versions,
                "scorecard": sc,
                "baseline": baseline,
                "flags": flags_eff,
                "horse_cols": HORSE_COLS,
                "races": recs,
            }
            path = _out_path(ymd, now)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
            kb = os.path.getsize(path) / 1024
            shown = _shown(path)
            dup = f" / 直前と同じ {n_all - len(recs)}R は省略" if n_all > len(recs) else ""
            print(f"📸 予測スナップショット: {shown} "
                  f"({len(recs)}R / {sum(len(r['horses']) for r in recs)}頭 / {kb:.1f}KB{dup})")
            day = _dir_bytes(ymd)
            if day > _DAY_BUDGET_BYTES:
                print(f"  ⚠️ 予測スナップショット: {ymd} の合計が {day/1e6:.2f}MB で目安 "
                      f"{_DAY_BUDGET_BYTES/1e6:.0f}MB を超えた (#153 リポジトリ容量)。"
                      f"同じ日に予測が何度も走っていないか確認")
            written.append(path)
        return written
    except Exception as e:
        # 予測そのものは成功しているので止めない。ただし黙らない (#134 の silent 無効化の教訓)
        print(f"⚠️ 予測スナップショットの記録に失敗: {type(e).__name__}: {e}")
        return []


# ───────────────────────── 読み出し ─────────────────────────

def _parse_at(at):
    """datetime / ISO 文字列を tz 付き datetime に。時差の無い値は JST とみなす。"""
    if at is None:
        return None
    if isinstance(at, datetime):
        t = at
    else:
        t = datetime.fromisoformat(str(at).strip().replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=JST)


def list_snapshots(date, base_dir=None):
    """指定日 (YYYYMMDD / YYYY-MM-DD) の run 一覧を書き込み時刻順に返す。

    [{"path", "written_at", "run_id", "event", "workflow", "n_races"}, ...]
    """
    ymd = _ymd(str(date))
    d = os.path.join(base_dir or snapshot_dir(), ymd)
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.json"))):
        try:
            doc = load_snapshot(p)
        except (OSError, ValueError):
            continue
        run = doc.get("run") or {}
        out.append({"path": p, "written_at": doc.get("written_at"),
                    "run_id": run.get("run_id"), "event": run.get("event"),
                    "workflow": run.get("workflow"), "n_races": len(doc.get("races") or [])})
    out.sort(key=lambda x: (_parse_at(x["written_at"]) or datetime.min.replace(tzinfo=JST), x["path"]))
    return out


def load_snapshot(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _expand(rec, doc, path):
    cols = doc.get("horse_cols") or HORSE_COLS
    r = dict(rec)
    r["horses"] = [dict(zip(cols, row)) for row in rec.get("horses") or []]
    r["run"] = doc.get("run") or {}
    r["versions"] = doc.get("versions") or {}
    r["scorecard"] = doc.get("scorecard")
    r["baseline"] = doc.get("baseline")
    r["flags"] = doc.get("flags") or {}
    r["written_at"] = doc.get("written_at")
    r["path"] = path
    return r


def iter_race_versions(date, base_dir=None, include_post_start=False, confirmed_only=False):
    """指定日の全レース・全版を (created_at, written_at) 順に返す。同じ版の重複は1つに。

    各要素は {race_id, created_at, start_time, numbers_confirmed, track_condition, weather,
             confidence, should_bet, ..., horses: [{horse_number, horse_id, odds, ...}],
             run, versions, flags, path}
    include_post_start: 予測時点で発走済みだった版 (post_start=1。ALLOW_PAST の時だけ書かれる)
                        も返す。既定は返さない (#99 の後出しを読み出し側でも防ぐ)
    confirmed_only: 枠順確定後 (numbers_confirmed=1) の版だけ返す。馬番で結果と結合する
                    読み手向け (horse_id で結合するなら不要)
    """
    seen = set()
    out = []
    for s in list_snapshots(date, base_dir):
        doc = load_snapshot(s["path"])
        for rec in doc.get("races") or []:
            if rec.get("post_start") and not include_post_start:
                continue
            if confirmed_only and not rec.get("numbers_confirmed"):
                continue
            key = (rec.get("race_id"), rec.get("created_at"))
            if key in seen:
                continue
            seen.add(key)
            out.append(_expand(rec, doc, s["path"]))
    out.sort(key=lambda r: (_parse_at(r["created_at"]) or datetime.min.replace(tzinfo=JST),
                            _parse_at(r["written_at"]) or datetime.min.replace(tzinfo=JST)))
    return out


def latest_before(date, at=None, race_ids=None, base_dir=None,
                  include_post_start=False, confirmed_only=False):
    """race_id ごとに「予測時刻 (cache の created_at) が at より前」の最後の版を返す。

    at=None なら最新版 = 発走前の最終版 (予測時点で発走済みの版は記録しない・既定で読まない)。
    時差の無い at は JST とみなす。
    例: 投稿印と同じ時点の予測を引く → latest_before(d, "2026-11-01 10:25")
    結果と突き合わせる時は horses の horse_id で結合する (馬番は仮馬番の版で別の馬を指す)。
    """
    t = _parse_at(at)
    want = set(race_ids) if race_ids else None
    best = {}
    for r in iter_race_versions(date, base_dir, include_post_start=include_post_start,
                                confirmed_only=confirmed_only):
        rid = r.get("race_id")
        if want is not None and rid not in want:
            continue
        ca = _parse_at(r.get("created_at"))
        if t is not None and (ca is None or not ca < t):
            continue
        best[rid] = r      # iter は時刻順なので後勝ち = 最後の版
    return best


def _mark_set(horses):
    return {(h.get("mark"), h.get("horse_number")) for h in horses or [] if h.get("mark")}


def _cache_time(s):
    """cache の created_at (UTC、時差表記なし) を tz 付き datetime に。読めなければ None。"""
    try:
        return _parse_at(_utc_to_jst_iso(s)) if s else None
    except ValueError:
        return None


def at_posting(date, base_dir=None, marks_path=None):
    """投稿した各レースについて、配信した版 (と、その確度) を返す。

    投稿印は posted_marks_YYYYMMDD.json (テキストの凍結記録 #101/#141) を正とする。
    posting_match:
      "exact"     … サイドカーの pred_created_at (投稿した版の cache 時刻、post_x が #169 で記録)
                    と cache_created_at が一致する版。配信した版そのもの
      "carried"   … pred_created_at はあるが一致する版が無く、それ以前の最後の版の印が投稿印と
                    一致した (直前の版と同じ中身なので重複除去で書かれなかった run を配信した形)
      以下は pred_created_at が無い日 (導入前・Pages JSON 経由の投稿) の「印の集合」での照合。
      印 (記号×馬番) しか見ないので、同じ印のまま信頼度・買い目・オッズが違う版は区別できない:
      "latest"    … 投稿時刻より前で印が投稿印と一致する版が1つだけで、それが最後の版
      "earlier"   … 一致する版が1つだけで、最後の版ではない
                    (9時台の再予測が runner に巻き戻された #169 の痕跡)。返すのは一致した方
      "ambiguous" … 一致する版が複数あり、どれを配信したかは特定できない。返すのは一致した最後の版
      "none"      … 一致する版が無い (スナップショット導入前・記録漏れ)。最後の版を返す
    n_match に「印の集合が投稿印と一致する版の数」(投稿時刻より前) を付ける。
    戻り値: {race_id: 版 (latest_before と同じ形 + posting_match, n_match)}。投稿記録が無ければ {}
    """
    ymd = _ymd(str(date))
    mp = marks_path or os.path.join(ROOT, "docs", "data", f"posted_marks_{ymd}.json")
    try:
        with open(mp, encoding="utf-8") as f:
            pm = json.load(f)
    except (OSError, ValueError):
        return {}
    t = _parse_at(pm.get("posted_at"))
    posted = pm.get("races") or {}
    if not isinstance(posted, dict):
        return {}
    pca = pm.get("pred_created_at")
    pca = pca if isinstance(pca, dict) else {}
    vers = [v for v in iter_race_versions(ymd, base_dir) if v.get("race_id") in posted]
    out = {}
    for rid, pmarks in posted.items():
        want = _mark_set(pmarks)
        allv = [v for v in vers if v["race_id"] == rid]
        cand = [v for v in allv
                if t is None or ((_parse_at(v.get("created_at")) or t) < t)]
        matches = [v for v in cand if _mark_set(v["horses"]) == want]
        n_match = len(matches)

        def _ret(v, how):
            r = dict(v)
            r["posting_match"] = how
            r["n_match"] = n_match
            out[rid] = r

        # 1) 投稿した版の cache 時刻が分かる → その版を特定する
        kt = _cache_time(pca.get(rid))
        if kt is not None:
            exact = [v for v in allv if _cache_time(v.get("cache_created_at")) == kt]
            if exact:
                _ret(exact[-1], "exact")
                continue
            prev = [v for v in allv
                    if (_cache_time(v.get("cache_created_at")) or kt) < kt]
            prev.sort(key=lambda v: _cache_time(v.get("cache_created_at")))
            if prev and _mark_set(prev[-1]["horses"]) == want:
                _ret(prev[-1], "carried")
                continue
        # 2) 印の集合で照合 (配信した版を一意に決められるとは限らない)
        if not cand:
            continue
        if n_match == 0:
            _ret(cand[-1], "none")
        elif n_match >= 2:
            _ret(matches[-1], "ambiguous")
        elif matches[0] is cand[-1]:
            _ret(cand[-1], "latest")
        else:
            _ret(matches[0], "earlier")
    return out


def _shown(path):
    return os.path.relpath(path, ROOT) if path.startswith(ROOT + os.sep) else path


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="予測時点スナップショット (#169) の一覧・読み出し")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("list", help="指定日の run 一覧")
    a.add_argument("date")
    b = sub.add_parser("latest", help="race_id ごとの最終版 (--before より前)")
    b.add_argument("date")
    b.add_argument("--before", default=None)
    c = sub.add_parser("posted", help="投稿した各レースの投稿時点の版と、投稿印との一致")
    c.add_argument("date")
    ns = ap.parse_args(argv)
    if ns.cmd == "list":
        for s in list_snapshots(ns.date):
            print(f"{s['written_at']}  {s['n_races']:>3}R  {s['event'] or '-':<18} "
                  f"{s['workflow'] or '-':<24} {_shown(s['path'])}")
    elif ns.cmd == "posted":
        for rid, r in sorted(at_posting(ns.date).items()):
            marks = " ".join(f"{h['mark']}{h['horse_number']}" for h in r["horses"] if h.get("mark"))
            print(f"{rid}  予測 {r['created_at']}  一致={r['posting_match']} "
                  f"(印一致の版 {r.get('n_match', 0)})  {marks}")
    else:
        for rid, r in sorted(latest_before(ns.date, ns.before).items()):
            marks = " ".join(f"{h['mark']}{h['horse_number']}" for h in r["horses"] if h.get("mark"))
            print(f"{rid}  予測 {r['created_at']}  {r['confidence']} sb={r['should_bet']}  "
                  f"馬場={r['track_condition'] or '-'}  "
                  f"{'' if r.get('numbers_confirmed') else '[枠順未確定] '}{marks}")


if __name__ == "__main__":
    _main(sys.argv[1:])
