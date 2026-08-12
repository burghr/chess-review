"""SQLite storage for games and analyzed moves."""

import os
import sqlite3

SCHEMA_VERSION = 5

# CHESS_REVIEW_DB is what the container sets, so honouring it here means
# `docker compose exec ... python -m chessreview.cli` finds the real database
# instead of quietly creating an empty one under the home directory.
DEFAULT_DB = os.environ.get("CHESS_REVIEW_DB") or os.path.expanduser(
    "~/projects/chess-review/chess.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS games (
    uuid            TEXT PRIMARY KEY,
    player          TEXT NOT NULL,      -- the account this game was fetched for
    url             TEXT,
    played_at       INTEGER,            -- unix seconds, game end
    color           TEXT,               -- white | black
    result          TEXT,               -- win | loss | draw
    termination     TEXT,               -- resigned, checkmated, timeout, agreed, ...
    rated           INTEGER,
    rules           TEXT,               -- chess, chess960, bughouse, ...
    time_class      TEXT,               -- bullet, blitz, rapid, daily
    time_control    TEXT,               -- raw, e.g. "600+5"
    base_seconds    INTEGER,
    increment       INTEGER,
    eco             TEXT,
    opening         TEXT,
    white_username  TEXT,
    white_rating    INTEGER,
    black_username  TEXT,
    black_rating    INTEGER,
    my_rating       INTEGER,
    opp_rating      INTEGER,
    site_accuracy   REAL,               -- accuracy Chess.com computed for me, if any
    ply_count       INTEGER,
    pgn             TEXT,
    fetched_at      INTEGER,
    analyzed_at     INTEGER,
    analysis_depth  INTEGER,
    my_acpl         REAL,               -- avg centipawn loss, my moves
    opp_acpl        REAL,
    my_accuracy     REAL,               -- computed from win% drops
    opp_accuracy    REAL
);

CREATE INDEX IF NOT EXISTS idx_games_player_time ON games(player, played_at);
CREATE INDEX IF NOT EXISTS idx_games_analyzed    ON games(analyzed_at);
CREATE INDEX IF NOT EXISTS idx_games_opening     ON games(eco);

CREATE TABLE IF NOT EXISTS moves (
    game_uuid    TEXT NOT NULL,
    ply          INTEGER NOT NULL,      -- 1-based half-move
    move_no      INTEGER,               -- full move number
    side         TEXT,                  -- white | black
    is_player    INTEGER,               -- 1 if the tracked account made this move
    san          TEXT,
    uci          TEXT,
    fen_before   TEXT,
    clock_secs   REAL,                  -- clock left after the move, if PGN has it
    eval_before  INTEGER,               -- centipawns, POV of the mover
    eval_after   INTEGER,               -- centipawns, POV of the mover
    mate_before  INTEGER,               -- signed mate distance, POV of the mover
    mate_after   INTEGER,
    best_san     TEXT,
    best_uci     TEXT,
    cp_loss      INTEGER,
    winp_before  REAL,                  -- win probability 0-100, POV of the mover
    winp_after   REAL,
    winp_loss    REAL,
    accuracy     REAL,
    judgment     TEXT,                  -- best | good | inaccuracy | mistake | blunder
    special      TEXT,                  -- brilliant | great | NULL
    material_swing REAL,                -- pawns won/lost over the forced line
    alt_winp_gap REAL,                  -- win% the second-best move would have cost
    phase        TEXT,                  -- opening | middlegame | endgame
    PRIMARY KEY (game_uuid, ply),
    FOREIGN KEY (game_uuid) REFERENCES games(uuid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_moves_judgment ON moves(judgment);
CREATE INDEX IF NOT EXISTS idx_moves_player   ON moves(is_player, judgment);

CREATE TABLE IF NOT EXISTS archives (
    player     TEXT NOT NULL,
    url        TEXT NOT NULL,
    fetched_at INTEGER,
    game_count INTEGER,
    complete   INTEGER DEFAULT 0,       -- 0 for the current month, which can still grow
    PRIMARY KEY (player, url)
);

-- Background job history, so the dashboard still shows the last sync after a
-- container restart.
CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,       -- sync | fetch | analyze
    status         TEXT NOT NULL,       -- queued | running | done | failed | cancelled
    trigger        TEXT,                -- manual | schedule | startup
    created_at     INTEGER,
    started_at     INTEGER,
    finished_at    INTEGER,
    message        TEXT,
    progress_done  INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    params         TEXT,                -- json
    result         TEXT,                -- json
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);

-- Written explanations from the language model. Cached so the same position is
-- never paid for twice; `fingerprint` covers the engine facts the explanation
-- was built from, so re-analyzing deeper invalidates it.
CREATE TABLE IF NOT EXISTS explanations (
    game_uuid   TEXT NOT NULL,
    ply         INTEGER NOT NULL,   -- 0 means the whole-game summary
    kind        TEXT NOT NULL,      -- move | game
    fingerprint TEXT NOT NULL,
    model       TEXT,
    text        TEXT NOT NULL,
    created_at  INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    PRIMARY KEY (game_uuid, ply, kind),
    FOREIGN KEY (game_uuid) REFERENCES games(uuid) ON DELETE CASCADE
);

-- Follow-up conversation attached to one explanation. Keyed the same way, so a
-- move's chat and its explanation live and die together.
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_uuid  TEXT NOT NULL,
    ply        INTEGER NOT NULL,   -- 0 for the whole-game thread
    kind       TEXT NOT NULL,      -- move | game
    role       TEXT NOT NULL,      -- user | assistant
    content    TEXT NOT NULL,
    model      TEXT,
    created_at INTEGER,
    FOREIGN KEY (game_uuid) REFERENCES games(uuid) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_thread
    ON chat_messages(game_uuid, kind, ply, id);
"""


def connect(path=DEFAULT_DB, timeout=30.0):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL plus a busy timeout is what lets the web requests read while the
    # analysis worker is writing in another thread.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


# CREATE TABLE IF NOT EXISTS never alters an existing table, so new columns on
# an old database have to be added explicitly. Additive only — nothing is
# dropped or rewritten, so an older build keeps working against the same file.
NEW_COLUMNS = {
    "moves": [
        ("special", "TEXT"),
        ("material_swing", "REAL"),
        ("alt_winp_gap", "REAL"),
    ],
}


def _add_missing_columns(conn):
    for table, columns in NEW_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


def upsert_game(conn, row):
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "uuid")
    sql = (
        f"INSERT INTO games ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(uuid) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])


def replace_moves(conn, game_uuid, rows):
    conn.execute("DELETE FROM moves WHERE game_uuid = ?", (game_uuid,))
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT INTO moves ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [[r[c] for c in cols] for r in rows])


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                 (key, None if value is None else str(value)))
    conn.commit()


def default_player(conn):
    """The account with the most games in the DB, used when --user is omitted."""
    row = conn.execute(
        "SELECT player, COUNT(*) n FROM games GROUP BY player ORDER BY n DESC LIMIT 1"
    ).fetchone()
    return row["player"] if row else None
