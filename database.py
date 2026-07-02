"""
競馬データベース管理モジュール
SQLiteでレース・馬・騎手・調教師・結果データを管理
"""

import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "keiba.db")


def get_connection(db_path=None):
    """DB接続を取得"""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db(db_path=None):
    """コンテキストマネージャでDB接続を管理"""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path=None):
    """データベースの初期化（テーブル作成）"""
    with get_db(db_path) as conn:
        conn.executescript("""
            -- レース情報
            CREATE TABLE IF NOT EXISTS races (
                race_id TEXT PRIMARY KEY,          -- 例: 202505030811
                race_date TEXT NOT NULL,            -- 開催日 YYYY-MM-DD
                venue TEXT NOT NULL,                -- 競馬場名（東京, 中山, 阪神 等）
                race_number INTEGER NOT NULL,       -- レース番号
                race_name TEXT,                     -- レース名
                grade TEXT,                         -- グレード（G1, G2, G3, OP, 条件 等）
                distance INTEGER NOT NULL,          -- 距離(m)
                surface TEXT NOT NULL,              -- 芝/ダート/障害
                direction TEXT,                     -- 右/左
                weather TEXT,                       -- 天候
                track_condition TEXT,               -- 馬場状態（良/稍重/重/不良）
                horse_count INTEGER,                -- 出走頭数
                start_time TEXT,                    -- 発走時刻 (HH:MM)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 馬マスター
            CREATE TABLE IF NOT EXISTS horses (
                horse_id TEXT PRIMARY KEY,          -- netkeiba上の馬ID
                horse_name TEXT NOT NULL,           -- 馬名
                sex TEXT,                           -- 性別（牡/牝/セ）
                birth_year INTEGER,                -- 生年
                sire TEXT,                          -- 父（種牡馬）
                dam TEXT,                           -- 母
                damsire TEXT,                       -- 母父
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 騎手マスター
            CREATE TABLE IF NOT EXISTS jockeys (
                jockey_id TEXT PRIMARY KEY,
                jockey_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 調教師マスター
            CREATE TABLE IF NOT EXISTS trainers (
                trainer_id TEXT PRIMARY KEY,
                trainer_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- レース結果（出走馬ごとのデータ）
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id TEXT NOT NULL,
                horse_id TEXT NOT NULL,
                jockey_id TEXT,
                trainer_id TEXT,
                post_position INTEGER,             -- 枠番
                horse_number INTEGER,               -- 馬番
                odds REAL,                          -- 単勝オッズ
                popularity INTEGER,                 -- 人気
                finish_position INTEGER,            -- 着順 (0=除外/取消)
                finish_time TEXT,                   -- 走破タイム (例: "1:34.5")
                finish_time_seconds REAL,           -- 走破タイム(秒)
                margin TEXT,                        -- 着差
                last_3f REAL,                       -- 上がり3F(秒)
                passing_order TEXT,                 -- 通過順 (例: "3-3-2-1")
                weight INTEGER,                    -- 馬体重(kg)
                weight_change INTEGER,              -- 馬体重増減
                impost REAL,                        -- 斤量
                corner_positions TEXT,              -- コーナー通過順位
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (race_id) REFERENCES races(race_id),
                FOREIGN KEY (horse_id) REFERENCES horses(horse_id),
                UNIQUE(race_id, horse_number)
            );

            -- 配当テーブル
            CREATE TABLE IF NOT EXISTS payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id TEXT NOT NULL,
                bet_type TEXT NOT NULL,             -- 単勝/複勝/枠連/馬連/ワイド/馬単/三連複/三連単
                combination TEXT NOT NULL,          -- 組み合わせ (例: "3", "3-5", "3-5-8")
                payout_amount INTEGER NOT NULL,     -- 払戻金(円)
                popularity INTEGER DEFAULT 0,       -- 人気
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (race_id) REFERENCES races(race_id),
                UNIQUE(race_id, bet_type, combination)
            );

            CREATE INDEX IF NOT EXISTS idx_payouts_race ON payouts(race_id);

            -- 追い切り評価テーブル(netkeiba oikiri.html)
            CREATE TABLE IF NOT EXISTS workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id TEXT NOT NULL,
                horse_id TEXT NOT NULL,
                horse_number INTEGER,
                evaluation_grade TEXT,         -- A/B/C
                evaluation_text TEXT,          -- "気力充実" 等の自然言語ラベル
                comment TEXT,                  -- 詳細コメント(あれば)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (race_id) REFERENCES races(race_id),
                UNIQUE(race_id, horse_id)
            );

            CREATE INDEX IF NOT EXISTS idx_workouts_race ON workouts(race_id);
            CREATE INDEX IF NOT EXISTS idx_workouts_horse ON workouts(horse_id);

            -- 予測キャッシュテーブル（予想固定化用）
            CREATE TABLE IF NOT EXISTS predictions_cache (
                race_id TEXT PRIMARY KEY,
                predictions_json TEXT NOT NULL,    -- 全馬の予測結果（horses配列）
                all_bets_json TEXT NOT NULL,       -- 全6券種の買い目
                confidence TEXT NOT NULL,           -- S/A/B/C/D
                conf_reason TEXT,                   -- 信頼度理由
                should_bet INTEGER DEFAULT 1,
                bet_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                posted_at TIMESTAMP NULL            -- post_predict 投稿時刻(これが入ったら以降変更不可)
            );

            -- 収支管理テーブル
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id TEXT NOT NULL,
                bet_type TEXT NOT NULL,             -- 単勝/複勝/馬連/ワイド/三連複/三連単
                bet_detail TEXT NOT NULL,           -- 馬番の組み合わせ
                amount INTEGER NOT NULL,            -- 賭け金(円)
                odds REAL,                          -- オッズ
                is_hit INTEGER DEFAULT 0,           -- 的中したか (0/1)
                payout INTEGER DEFAULT 0,           -- 払戻金(円)
                predicted_prob REAL,                -- モデル予測確率
                expected_value REAL,                -- 期待値
                bet_date TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (race_id) REFERENCES races(race_id)
            );

            -- インデックス作成
            CREATE INDEX IF NOT EXISTS idx_races_date ON races(race_date);
            CREATE INDEX IF NOT EXISTS idx_races_venue ON races(venue);
            CREATE INDEX IF NOT EXISTS idx_results_race ON results(race_id);
            CREATE INDEX IF NOT EXISTS idx_results_horse ON results(horse_id);
            CREATE INDEX IF NOT EXISTS idx_results_jockey ON results(jockey_id);
            CREATE INDEX IF NOT EXISTS idx_bets_race ON bets(race_id);
            CREATE INDEX IF NOT EXISTS idx_bets_date ON bets(bet_date);

            -- 投稿スロット排他制御 (1日1スロット1回保証) — #41
            -- 任意の trigger (cron, watchdog, 手動) が走っても 1 回しか実行されない
            CREATE TABLE IF NOT EXISTS posted_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_date TEXT NOT NULL,              -- JST 'YYYY-MM-DD'
                slot_name TEXT NOT NULL,              -- 'morning'/'weekday'/'evening'/'odds_flash'/'post_predict'/'hit_flash'/'results' 等
                posted_at TEXT NOT NULL,              -- ISO 8601 UTC, lock 取得時刻
                run_id TEXT,                          -- GitHub Actions GITHUB_RUN_ID
                workflow_event TEXT,                  -- 'schedule'/'workflow_dispatch'
                success INTEGER DEFAULT 1,            -- 1=投稿成功 / 0=lock 取得後に投稿失敗
                note TEXT,                            -- 補足 (失敗理由など)
                UNIQUE(post_date, slot_name)          -- ← 二重投稿を物理的に防ぐ核
            );
            CREATE INDEX IF NOT EXISTS idx_posted_slots_date ON posted_slots(post_date);
        """)

        # ── マイグレーション: predictions_cache に posted_at がなければ追加 ──
        cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions_cache)").fetchall()]
        if 'posted_at' not in cols:
            conn.execute("ALTER TABLE predictions_cache ADD COLUMN posted_at TIMESTAMP NULL")
            print("🔧 predictions_cache: posted_at カラム追加")
    print("✅ データベース初期化完了")


# ─── 投稿スロット排他制御 (#41) ───────────────────────────────────
def acquire_post_slot(slot_name, run_id=None, workflow_event=None, post_date=None):
    """投稿前に呼ぶ atomic lock.

    Returns:
        (True, None) — lock 取得成功、投稿を続行してよい
        (False, existing_dict) — 既に同じ (date, slot) が登録済み、投稿しない

    使い方:
        ok, prev = acquire_post_slot('post_predict', run_id=os.environ.get('GITHUB_RUN_ID'),
                                     workflow_event=os.environ.get('GITHUB_EVENT_NAME'))
        if not ok:
            print(f'⏭️ 既に投稿済み: {prev["posted_at"]} (run {prev["run_id"]})')
            sys.exit(0)
        # ... 投稿処理 ...
        # 失敗時は release_post_slot() を呼ぶ
    """
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    if post_date is None:
        post_date = datetime.now(JST).strftime('%Y-%m-%d')
    elif len(post_date) == 8:
        post_date = f"{post_date[:4]}-{post_date[4:6]}-{post_date[6:8]}"
    now_utc = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        try:
            conn.execute("""
                INSERT INTO posted_slots (post_date, slot_name, posted_at, run_id, workflow_event)
                VALUES (?, ?, ?, ?, ?)
            """, (post_date, slot_name, now_utc, run_id, workflow_event))
            return True, None
        except sqlite3.IntegrityError:
            row = conn.execute("""
                SELECT post_date, slot_name, posted_at, run_id, workflow_event, success
                FROM posted_slots WHERE post_date=? AND slot_name=?
            """, (post_date, slot_name)).fetchone()
            return False, dict(row) if row else None


def release_post_slot(slot_name, note='post failed', post_date=None):
    """投稿失敗時のロールバック。success=0 でマークするのみ (二重投稿はやはり防ぐ)。

    note: 失敗理由など短い文字列。
    """
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    if post_date is None:
        post_date = datetime.now(JST).strftime('%Y-%m-%d')
    elif len(post_date) == 8:
        post_date = f"{post_date[:4]}-{post_date[4:6]}-{post_date[6:8]}"
    with get_db() as conn:
        conn.execute("""
            UPDATE posted_slots SET success=0, note=COALESCE(note,'')||?||'; '
            WHERE post_date=? AND slot_name=?
        """, (note[:200], post_date, slot_name))


def clear_post_slot(slot_name, post_date=None):
    """lock を完全に削除して再取得可能にする (#70)。

    release_post_slot は success=0 マークのみで行を残すため、UNIQUE 制約で
    再 acquire できない。「まだデータが無いだけ」で投稿していないケースは、
    本関数で行ごと削除し、後続 run が改めて lock を取って再試行できるようにする。
    投稿に成功していないことが確実な場合のみ呼ぶこと (二重投稿防止のため)。
    """
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    if post_date is None:
        post_date = datetime.now(JST).strftime('%Y-%m-%d')
    elif len(post_date) == 8:
        post_date = f"{post_date[:4]}-{post_date[4:6]}-{post_date[6:8]}"
    with get_db() as conn:
        conn.execute("DELETE FROM posted_slots WHERE post_date=? AND slot_name=?",
                     (post_date, slot_name))


def is_slot_posted(slot_name, post_date=None):
    """既に投稿済みかチェック (lock 取得せず参照だけ)。watchdog/health check 用。"""
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    if post_date is None:
        post_date = datetime.now(JST).strftime('%Y-%m-%d')
    elif len(post_date) == 8:
        post_date = f"{post_date[:4]}-{post_date[4:6]}-{post_date[6:8]}"
    with get_db() as conn:
        row = conn.execute("""
            SELECT posted_at, run_id, success FROM posted_slots
            WHERE post_date=? AND slot_name=?
        """, (post_date, slot_name)).fetchone()
        return dict(row) if row else None


def _is_degenerate_predictions(predictions_json):
    """予測が degenerate (≒全頭均等) かを判定する。

    判定基準: 全馬の表示勝率 (%) の最大値が 12% 未満 = ML 出力が flat。
    16頭立てなら均等 6.25%、12% は均等 + 6pt 上乗せ程度で「ばらつき有」とみなせる閾値。
    18頭立てで均等 5.5% 〜 通常本命 22% の中央付近に 12% を置く。

    #95 (2026-07-02): 12% 閾値は温度×3 (シャープ化) 時代の分布で設計されたため、
    pred_win_display_pct (温度×3、表示用) を優先して判定する。温度1 の
    pred_win_pct で判定すると 16頭立ての正常な本命 (~9.5%) まで flat 扱いになる。
    旧 cache (display フィールド無し = 温度×3 の pred_win_pct) はそのまま使える。

    #41 (2026-06-07) seal lockout 対策:
    07:00 で flat prediction が seal される → 10:15 以降 --force でも修正不能になる事故が
    6/7 に発生。degenerate な予測は seal しないことで、後段の再 predict を可能にする。
    """
    import json as _json
    try:
        preds = _json.loads(predictions_json) if isinstance(predictions_json, str) else predictions_json
    except Exception:
        return False  # 解析失敗時は安全側 (degenerate でない扱い)
    if not preds:
        return False
    max_win_pct = 0.0
    for p in preds:
        pw = p.get('pred_win_display_pct') or p.get('pred_win_pct') or 0
        if pw > max_win_pct:
            max_win_pct = pw
    return max_win_pct < 12.0


def seal_predictions_for_date(date_str):
    """指定日の predictions_cache を「投稿済み」としてロック。
    post_predict 投稿成功時に呼ぶ。

    #41 (2026-06-07): degenerate (ML flat) な予測は seal しない (seal lockout 対策)。
    seal してしまうと以降 --force でも上書き不能になり、朝の bad prediction が
    1 日中固定されてしまう (6/7 で発生)。
    """
    if len(date_str) == 8:
        hy = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    else:
        hy = date_str
    with get_db() as conn:
        # 対象レースを抽出 (まだ posted_at が NULL のもの)
        candidates = conn.execute("""
            SELECT pc.race_id, pc.predictions_json
            FROM predictions_cache pc
            JOIN races r ON pc.race_id = r.race_id
            WHERE (r.race_date = ? OR r.race_date = ?) AND pc.posted_at IS NULL
        """, (date_str, hy)).fetchall()

        sealed = 0
        skipped_degenerate = 0
        for row in candidates:
            if _is_degenerate_predictions(row['predictions_json']):
                skipped_degenerate += 1
                continue
            conn.execute(
                "UPDATE predictions_cache SET posted_at = CURRENT_TIMESTAMP WHERE race_id = ?",
                (row['race_id'],)
            )
            sealed += 1
    msg = f"🔒 predictions_cache を {sealed} レース seal({date_str})"
    if skipped_degenerate:
        msg += f" / ⚠ degenerate {skipped_degenerate} レースは seal せず再 predict 可能のまま"
    print(msg)
    return sealed


def is_prediction_sealed(race_id):
    """そのレースの予測が投稿済み(seal)かを返す。

    #41 (2026-06-07): degenerate predictions (全頭均等 = ML flat output) は
    seal されていても sealed=False として扱う → 再 predict --force が可能になる。
    朝の bad prediction が seal で固定される事故 (6/7) の対策。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT posted_at, predictions_json FROM predictions_cache WHERE race_id = ?",
            (race_id,)
        ).fetchone()
        if row is None or row['posted_at'] is None:
            return False
        # posted_at あり = 通常は sealed だが、degenerate なら escape hatch として上書きを許す
        if _is_degenerate_predictions(row['predictions_json']):
            return False
        return True


def sync_cache_race_ids(race_date_str):
    """predictions_cacheのrace_idをracesテーブルの最新IDに同期する。

    netkeibaのrace_idの日目(day)部分が朝と昼で変わる問題を解決。
    race_id形式: YYYY(4) + venue(2) + kai(2) + day(2) + race(2)

    例: 朝に 202606030101 で予測保存 → 昼に races が 202606030501 に更新
        → cache の race_id を 202606030501 にリネーム
    """
    # race_dateを YYYY-MM-DD 形式に統一
    if len(race_date_str) == 8:
        date_hyphen = f"{race_date_str[:4]}-{race_date_str[4:6]}-{race_date_str[6:8]}"
    else:
        date_hyphen = race_date_str

    renamed = 0
    with get_db() as conn:
        # 当日のracesを取得
        races = conn.execute("""
            SELECT race_id, venue, race_number FROM races
            WHERE race_date = ? OR race_date = ?
        """, (race_date_str, date_hyphen)).fetchall()

        for race in races:
            rid = race['race_id']
            venue_code = rid[4:6]
            kai_code = rid[6:8]
            race_num = rid[10:12]

            # 既にcacheにある → スキップ
            exists = conn.execute(
                "SELECT 1 FROM predictions_cache WHERE race_id = ?", (rid,)
            ).fetchone()
            if exists:
                continue

            # 同じ会場+開催回+レース番号で、当日作成されたcacheを検索
            old = conn.execute("""
                SELECT race_id FROM predictions_cache
                WHERE substr(race_id,5,2) = ?
                  AND substr(race_id,7,2) = ?
                  AND substr(race_id,11,2) = ?
                  AND date(created_at) = ?
                ORDER BY created_at DESC LIMIT 1
            """, (venue_code, kai_code, race_num, date_hyphen)).fetchone()

            if old and old['race_id'] != rid:
                old_id = old['race_id']
                conn.execute(
                    "UPDATE predictions_cache SET race_id = ? WHERE race_id = ?",
                    (rid, old_id)
                )
                renamed += 1

    if renamed > 0:
        print(f"🔄 predictions_cache: {renamed}件のrace_idを同期しました")


if __name__ == "__main__":
    init_db()
