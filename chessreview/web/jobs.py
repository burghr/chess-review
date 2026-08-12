"""Background sync and analysis for the web dashboard.

One worker thread runs one job at a time. Stockfish already saturates the cores
it is given, and SQLite takes one writer, so a queue with a single consumer is
the honest model rather than a pool that would just fight itself.

Job state lives in the `jobs` table, not in memory, so the dashboard still shows
what happened last night after the container restarts.
"""

import json
import queue
import threading
import time

from .. import analyze as analyze_mod
from .. import db, fetch as fetch_mod


class Settings:
    """Runtime config, from environment defaults but editable from the UI."""

    KEYS = ("player", "depth", "threads", "hash_mb", "batch_size",
            "interval_minutes", "auto_sync", "llm_provider", "llm_model",
            "llm_base_url", "llm_api_key")

    # Never leave the process in an API response.
    SECRET_KEYS = ("llm_api_key",)

    def __init__(self, db_path, player=None, depth=14, threads=2, hash_mb=256,
                 batch_size=200, interval_minutes=60, auto_sync=True,
                 engine_path=None, sync_on_start=False, llm_provider="",
                 llm_model="", llm_base_url="", llm_api_key=""):
        self.db_path = db_path
        self.player = player
        self.depth = depth
        self.threads = threads
        self.hash_mb = hash_mb
        self.batch_size = batch_size
        self.interval_minutes = interval_minutes
        self.auto_sync = auto_sync
        self.engine_path = engine_path
        self.sync_on_start = sync_on_start
        # Empty string means "not set here" — explain.py falls back to the
        # environment, and from there to its own defaults.
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        self.llm_base_url = llm_base_url
        self.llm_api_key = llm_api_key

    def load_overrides(self, conn):
        """Anything set from the UI wins over the environment.

        A blank stored value counts as unset. Otherwise saving the settings
        form with an empty username would poison the database and permanently
        shadow a perfectly good CHESS_REVIEW_USER from the environment.
        """
        for key in self.KEYS:
            value = db.get_setting(conn, f"cfg.{key}")
            if value is None or value == "":
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                setattr(self, key, value == "1")
            elif isinstance(current, int):
                setattr(self, key, int(value))
            else:
                setattr(self, key, value)

    def save(self, conn, updates):
        for key, value in updates.items():
            if key not in self.KEYS:
                continue
            current = getattr(self, key)
            if isinstance(current, bool):
                value = bool(value)
                db.set_setting(conn, f"cfg.{key}", "1" if value else "0")
            elif isinstance(current, int) and value is not None:
                value = int(value)
                db.set_setting(conn, f"cfg.{key}", value)
            else:
                db.set_setting(conn, f"cfg.{key}", value)
            setattr(self, key, value)

    def to_dict(self):
        """Safe to serialise: secrets become a boolean, never the value."""
        d = {k: getattr(self, k) for k in self.KEYS if k not in self.SECRET_KEYS}
        for k in self.SECRET_KEYS:
            d[f"{k}_set"] = bool(getattr(self, k))
        d["db_path"] = self.db_path
        return d


class JobRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue = queue.Queue()
        self.current_id = None
        self._last_run = None          # set by the scheduler thread once it starts
        self.cancel_flag = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._worker = None
        self._scheduler = None

    # -- lifecycle ---------------------------------------------------------- #

    def start(self):
        conn = self._connect()
        # Anything left "running" belongs to a previous process that died.
        conn.execute(
            "UPDATE jobs SET status='failed', error='interrupted by restart', "
            "finished_at=? WHERE status IN ('running','queued')", (int(time.time()),))
        conn.commit()
        conn.close()

        self._worker = threading.Thread(target=self._work, daemon=True,
                                        name="chess-review-worker")
        self._worker.start()
        self._scheduler = threading.Thread(target=self._schedule, daemon=True,
                                           name="chess-review-scheduler")
        self._scheduler.start()
        if self.settings.sync_on_start and self.settings.player:
            self.enqueue("sync", trigger="startup")

    def stop(self):
        self._stop.set()
        self.cancel_flag.set()
        self.queue.put(None)

    def _connect(self):
        return db.connect(self.settings.db_path)

    # -- queue -------------------------------------------------------------- #

    def enqueue(self, kind, trigger="manual", params=None):
        conn = self._connect()
        cur = conn.execute(
            "INSERT INTO jobs(kind, status, trigger, created_at, params) "
            "VALUES (?,?,?,?,?)",
            (kind, "queued", trigger, int(time.time()), json.dumps(params or {})),
        )
        conn.commit()
        job_id = cur.lastrowid
        conn.close()
        self.queue.put(job_id)
        return job_id

    def cancel(self, job_id=None):
        """Cancel the running job, and drop anything still queued."""
        conn = self._connect()
        if job_id is None or job_id == self.current_id:
            self.cancel_flag.set()
        conn.execute(
            "UPDATE jobs SET status='cancelled', finished_at=? WHERE status='queued'",
            (int(time.time()),))
        conn.commit()
        conn.close()

    def busy(self):
        return self.current_id is not None

    # -- worker ------------------------------------------------------------- #

    def _work(self):
        while not self._stop.is_set():
            job_id = self.queue.get()
            if job_id is None:
                break
            conn = self._connect()
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row or row["status"] != "queued":
                conn.close()
                continue

            self.current_id = job_id
            self.cancel_flag.clear()
            conn.execute("UPDATE jobs SET status='running', started_at=?, "
                         "message='starting' WHERE id=?", (int(time.time()), job_id))
            conn.commit()

            kind = row["kind"]
            params = json.loads(row["params"] or "{}")
            try:
                if kind == "fetch":
                    result = self._do_fetch(conn, job_id, params)
                elif kind == "analyze":
                    result = self._do_analyze(conn, job_id, params)
                elif kind == "sync":
                    result = self._do_fetch(conn, job_id, params)
                    result.update(self._do_analyze(conn, job_id, params))
                else:
                    raise ValueError(f"unknown job kind {kind!r}")
                status = "cancelled" if self.cancel_flag.is_set() else "done"
                conn.execute(
                    "UPDATE jobs SET status=?, finished_at=?, result=?, "
                    "message=? WHERE id=?",
                    (status, int(time.time()), json.dumps(result),
                     self._describe(result), job_id))
            except Exception as exc:
                conn.execute(
                    "UPDATE jobs SET status='failed', finished_at=?, error=?, "
                    "message=? WHERE id=?",
                    (int(time.time()), str(exc), f"failed: {exc}", job_id))
            conn.commit()
            conn.close()
            self.current_id = None

    @staticmethod
    def _describe(result):
        bits = []
        if "new_games" in result:
            bits.append(f"{result['new_games']} new game(s)")
        if "analyzed" in result:
            bits.append(f"{result['analyzed']} analyzed")
        if result.get("failed"):
            bits.append(f"{result['failed']} failed")
        return ", ".join(bits) or "nothing to do"

    def _progress(self, conn, job_id, message, done=None, total=None):
        sets, params = ["message=?"], [message]
        if done is not None:
            sets.append("progress_done=?")
            params.append(done)
        if total is not None:
            sets.append("progress_total=?")
            params.append(total)
        params.append(job_id)
        conn.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", params)
        conn.commit()

    def _player(self, conn, params):
        """Same resolution order the API header uses, so the two never disagree.

        Without the database fallback the dashboard could show a username while
        a sync insisted none was configured.
        """
        player = (params.get("player") or self.settings.player
                  or db.default_player(conn))
        if not player:
            raise RuntimeError(
                "No Chess.com username configured. Set CHESS_REVIEW_USER in "
                ".env (or the environment), or enter it under Settings.")
        return player

    def _do_fetch(self, conn, job_id, params):
        player = self._player(conn, params)
        self._progress(conn, job_id, f"fetching archives for {player}", 0, 0)

        months = {"n": 0}

        def progress(url, total, kept):
            months["n"] += 1
            month = "/".join(url.rsplit("/", 2)[-2:])
            self._progress(conn, job_id, f"fetched {month}: {kept} new",
                           done=months["n"])

        new_games, month_count = fetch_mod.fetch(
            conn, player, since=params.get("since"), progress=progress)
        return {"player": player, "new_games": new_games, "months": month_count}

    def _do_analyze(self, conn, job_id, params):
        player = self._player(conn, params)
        limit = params.get("limit", self.settings.batch_size)
        depth = params.get("depth", self.settings.depth)
        redo = bool(params.get("reanalyze"))
        games = analyze_mod.pending_games(conn, player, limit=limit,
                                          reanalyze=redo)
        if not games:
            return {"analyzed": 0, "failed": 0, "pending": 0}

        self._progress(conn, job_id,
                       f"{'re-analyzing' if redo else 'analyzing'} {len(games)} "
                       f"game(s) at depth {depth}", 0, len(games))
        counter = {"n": 0}

        def on_done(game_row, agg, exc):
            counter["n"] += 1
            label = "failed" if exc else f"ACPL {agg['my_acpl']:.0f}"
            self._progress(conn, job_id,
                           f"analyzed {counter['n']}/{len(games)} ({label})",
                           done=counter["n"])

        ok, failed = analyze_mod.analyze_games(
            conn, games, depth=depth, engine_path=self.settings.engine_path,
            threads=self.settings.threads, hash_mb=self.settings.hash_mb,
            on_done=on_done, should_stop=self.cancel_flag.is_set)
        pending = conn.execute(
            "SELECT COUNT(*) n FROM games WHERE player=? AND analyzed_at IS NULL",
            (player,)).fetchone()["n"]
        return {"analyzed": ok, "failed": failed, "pending": pending}

    # -- scheduler ---------------------------------------------------------- #

    def _last_scheduled_run(self):
        conn = self._connect()
        row = conn.execute(
            "SELECT MAX(started_at) t FROM jobs WHERE trigger='schedule'").fetchone()
        conn.close()
        return row["t"] if row and row["t"] else None

    def _schedule(self):
        """Enqueue a sync every `interval_minutes`, skipping if one is running.

        The clock starts from the last scheduled run recorded in the database,
        not from process start. A container that restarts every few minutes
        would otherwise re-sync on every boot and never honour the interval.
        `sync_on_start` is the explicit way to ask for a run at startup.
        """
        self._last_run = self._last_scheduled_run() or time.time()
        while not self._stop.is_set():
            self._stop.wait(20)
            if self._stop.is_set():
                break
            if not self.settings.auto_sync or not self.settings.player:
                continue
            interval = max(5, self.settings.interval_minutes) * 60
            now = time.time()
            if now - self._last_run < interval:
                continue
            if self.busy() or not self.queue.empty():
                continue  # the previous run is still going; try again next tick
            self._last_run = now
            self.enqueue("sync", trigger="schedule")

    def next_run_at(self):
        """What the scheduler thread will actually do, not a re-derivation."""
        if not self.settings.auto_sync or not self.settings.player:
            return None
        if self._last_run is None:
            return None
        return int(self._last_run + max(5, self.settings.interval_minutes) * 60)


def recent_jobs(conn, limit=20):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]
