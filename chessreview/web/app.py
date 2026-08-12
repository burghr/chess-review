"""FastAPI app: JSON over the same `stats` functions the CLI prints."""

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import chat as chat_mod, db, explain as explain_mod, stats
from ..pgnimport import import_text
from .jobs import JobRunner, Settings, recent_jobs

STATIC = Path(__file__).parent / "static"


def _env_bool(name, default):
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def build_settings():
    return Settings(
        db_path=os.environ.get("CHESS_REVIEW_DB", db.DEFAULT_DB),
        player=os.environ.get("CHESS_REVIEW_USER") or None,
        depth=int(os.environ.get("CHESS_REVIEW_DEPTH", "14")),
        threads=int(os.environ.get("CHESS_REVIEW_THREADS", "2")),
        hash_mb=int(os.environ.get("CHESS_REVIEW_HASH", "256")),
        batch_size=int(os.environ.get("CHESS_REVIEW_BATCH", "200")),
        interval_minutes=int(os.environ.get("CHESS_REVIEW_INTERVAL", "60")),
        auto_sync=_env_bool("CHESS_REVIEW_AUTO_SYNC", True),
        engine_path=os.environ.get("CHESS_REVIEW_ENGINE"),
        sync_on_start=_env_bool("CHESS_REVIEW_SYNC_ON_START", False),
        llm_provider=os.environ.get("CHESS_REVIEW_LLM_PROVIDER", ""),
        llm_model=os.environ.get("CHESS_REVIEW_LLM_MODEL", ""),
        llm_base_url=os.environ.get("CHESS_REVIEW_LLM_BASE_URL", ""),
        llm_api_key=os.environ.get("CHESS_REVIEW_LLM_API_KEY", ""),
    )


settings = build_settings()
runner = JobRunner(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(settings.db_path)
    settings.load_overrides(conn)
    conn.close()
    runner.start()
    yield
    runner.stop()


app = FastAPI(title="chess-review", lifespan=lifespan)


def get_conn():
    conn = db.connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def resolve_player(conn, player=None):
    name = player or settings.player or db.default_player(conn)
    if not name:
        raise HTTPException(
            status_code=409,
            detail="No Chess.com username configured yet. Set one in Settings.",
        )
    return name


def get_filters(
    conn=Depends(get_conn),
    player: str | None = None,
    time_class: str | None = None,
    color: str | None = None,
    since: str | None = None,
    until: str | None = None,
    rated_only: bool = False,
    min_games: int = 3,
):
    return stats.Filters(
        player=resolve_player(conn, player), time_class=time_class, color=color,
        since=since or None, until=until or None, rated_only=rated_only,
        min_games=min_games,
    )


# --------------------------------------------------------------------------- #
# config and jobs
# --------------------------------------------------------------------------- #

@app.get("/api/config")
def read_config(conn=Depends(get_conn)):
    player = settings.player or db.default_player(conn)
    return {
        "settings": settings.to_dict(),
        "resolved_player": player,
        "players": stats.players(conn),
        "coverage": stats.coverage(conn, player) if player else {},
        "next_run_at": runner.next_run_at(),
        "server_time": int(time.time()),
    }


@app.post("/api/config")
def write_config(updates: dict, conn=Depends(get_conn)):
    # A blank username means "fall back to the environment", not "store empty".
    if "player" in updates:
        updates["player"] = str(updates["player"] or "").strip() or None
        if updates["player"] is None:
            updates.pop("player")
            db.set_setting(conn, "cfg.player", "")
            settings.player = os.environ.get("CHESS_REVIEW_USER") or None
    settings.save(conn, updates)
    return read_config(conn)


@app.get("/api/jobs")
def list_jobs(conn=Depends(get_conn), limit: int = 20):
    return {
        "jobs": recent_jobs(conn, limit=limit),
        "current_id": runner.current_id,
        "next_run_at": runner.next_run_at(),
        "server_time": int(time.time()),
    }


@app.post("/api/jobs")
def create_job(body: dict | None = None):
    body = body or {}
    kind = body.get("kind", "sync")
    if kind not in ("sync", "fetch", "analyze"):
        raise HTTPException(400, "kind must be sync, fetch, or analyze")
    if runner.busy():
        raise HTTPException(409, "a job is already running")
    params = {k: v for k, v in body.items()
              if k in ("player", "since", "limit", "depth", "reanalyze")
              and v is not None}
    return {"id": runner.enqueue(kind, trigger="manual", params=params)}


@app.post("/api/jobs/cancel")
def cancel_job():
    runner.cancel()
    return {"cancelled": True}


@app.post("/api/import")
def import_pgn(body: dict, conn=Depends(get_conn)):
    pgn = (body or {}).get("pgn", "")
    if not pgn.strip():
        raise HTTPException(400, "no PGN supplied")
    player = resolve_player(conn, (body or {}).get("player"))
    imported, skipped = import_text(conn, pgn, player,
                                    default_time_class=body.get("time_class")
                                    or "unknown")
    return {"imported": imported, "skipped": skipped, "player": player}


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #

@app.get("/api/summary")
def api_summary(conn=Depends(get_conn), f=Depends(get_filters)):
    return {"filters": f.to_dict(), **stats.summary(conn, f)}


@app.get("/api/ratings")
def api_ratings(conn=Depends(get_conn), f=Depends(get_filters),
                bucket: str = Query("day", pattern="^(day|week|month)$")):
    return {"series": stats.rating_series(conn, f, bucket=bucket)}


@app.get("/api/openings")
def api_openings(conn=Depends(get_conn), f=Depends(get_filters),
                 by: str = Query("opening", pattern="^(opening|eco)$"),
                 limit: int = 25, worst_first: bool = True):
    return {"rows": stats.openings(conn, f, by=by, limit=limit,
                                   worst_first=worst_first)}


@app.get("/api/mistakes")
def api_mistakes(conn=Depends(get_conn), f=Depends(get_filters)):
    return stats.move_quality(conn, f)


@app.get("/api/clock")
def api_clock(conn=Depends(get_conn), f=Depends(get_filters)):
    return {"rows": stats.clock(conn, f)}


@app.get("/api/blunders")
def api_blunders(conn=Depends(get_conn), f=Depends(get_filters),
                 limit: int = 25, phase: str | None = None):
    return {"rows": stats.blunders(conn, f, limit=limit, phase=phase)}


@app.get("/api/sessions")
def api_sessions(conn=Depends(get_conn), f=Depends(get_filters), gap: int = 60):
    return {"rows": stats.sessions(conn, f, gap_minutes=gap)}


@app.get("/api/opponents")
def api_opponents(conn=Depends(get_conn), f=Depends(get_filters)):
    return {"rows": stats.opponents(conn, f)}


@app.get("/api/trend")
def api_trend(conn=Depends(get_conn), f=Depends(get_filters),
              bucket: str = Query("month", pattern="^(day|week|month)$")):
    return {"rows": stats.trend(conn, f, bucket=bucket)}


@app.get("/api/games")
def api_games(conn=Depends(get_conn), f=Depends(get_filters), limit: int = 50,
              offset: int = 0, result: str | None = None,
              analyzed: bool | None = None):
    return stats.games(conn, f, limit=limit, offset=offset, result=result,
                       analyzed=analyzed)


# uuids are Chess.com URLs for some rows, so they travel as a query parameter
# rather than a path segment: an encoded slash never survives the path cleanly.
@app.get("/api/game")
def api_game(uuid: str, conn=Depends(get_conn)):
    data = stats.game_detail(conn, uuid)
    if not data:
        raise HTTPException(404, "game not found")
    return data


@app.get("/api/pgn", response_class=PlainTextResponse)
def api_pgn(uuid: str, conn=Depends(get_conn)):
    pgn = stats.game_pgn(conn, uuid)
    if not pgn:
        raise HTTPException(404, "game not found")
    return pgn


# Explanations run Stockfish on one position and then call the model, so they
# take a few seconds. Declared `def` rather than `async def` so FastAPI runs them
# in its threadpool and one slow explanation cannot stall the event loop.
@app.post("/api/explain")
def api_explain(body: dict, conn=Depends(get_conn)):
    uuid = (body or {}).get("uuid")
    if not uuid:
        raise HTTPException(400, "uuid is required")
    ply = (body or {}).get("ply")
    refresh = bool((body or {}).get("refresh"))
    kwargs = dict(depth=settings.depth, engine_path=settings.engine_path,
                  threads=1, refresh=refresh,
                  provider=settings.llm_provider or None,
                  model=settings.llm_model or None,
                  base_url=settings.llm_base_url or None,
                  api_key=settings.llm_api_key or None)
    try:
        if ply is None:
            return explain_mod.explain_game(conn, uuid, **kwargs)
        return explain_mod.explain_move(conn, uuid, int(ply), **kwargs)
    except explain_mod.ExplainError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/prompts")
def api_prompts(conn=Depends(get_conn)):
    return {"prompts": explain_mod.prompt_state(conn),
            "user_preamble": explain_mod.USER_PREAMBLE}


@app.post("/api/prompts")
def api_set_prompt(body: dict, conn=Depends(get_conn)):
    kind = (body or {}).get("kind")
    try:
        # An empty string is meaningful here: it resets to the built-in default.
        explain_mod.set_prompt(conn, kind, (body or {}).get("text", ""))
    except explain_mod.ExplainError as exc:
        raise HTTPException(400, str(exc))
    return {"prompts": explain_mod.prompt_state(conn),
            "user_preamble": explain_mod.USER_PREAMBLE}


@app.post("/api/explain-preview")
def api_explain_preview(body: dict, conn=Depends(get_conn)):
    uuid = (body or {}).get("uuid")
    if not uuid:
        raise HTTPException(400, "uuid is required")
    try:
        return explain_mod.preview(conn, uuid, ply=(body or {}).get("ply"),
                                   depth=settings.depth,
                                   engine_path=settings.engine_path)
    except explain_mod.ExplainError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/llm-test")
def api_llm_test(body: dict | None = None):
    """Probe the configured local server so a 404 becomes a diagnosis."""
    body = body or {}
    return explain_mod.probe_local(
        base_url=body.get("base_url") or settings.llm_base_url or None,
        # An empty string from the form means "use the saved key", not "no key".
        api_key=body.get("api_key") or settings.llm_api_key or None,
    )


@app.get("/api/chat")
def api_chat_history(uuid: str, ply: int | None = None, conn=Depends(get_conn)):
    return {"messages": chat_mod.history(conn, uuid, ply)}


@app.post("/api/chat")
def api_chat(body: dict, conn=Depends(get_conn)):
    uuid = (body or {}).get("uuid")
    if not uuid:
        raise HTTPException(400, "uuid is required")
    try:
        return chat_mod.ask(
            conn, uuid, (body or {}).get("message", ""),
            ply=(body or {}).get("ply"), depth=settings.depth,
            engine_path=settings.engine_path,
            provider=settings.llm_provider or None,
            model=settings.llm_model or None,
            base_url=settings.llm_base_url or None,
            api_key=settings.llm_api_key or None)
    except explain_mod.ExplainError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/chat/clear")
def api_chat_clear(body: dict, conn=Depends(get_conn)):
    uuid = (body or {}).get("uuid")
    if not uuid:
        raise HTTPException(400, "uuid is required")
    chat_mod.clear(conn, uuid, (body or {}).get("ply"))
    return {"cleared": True}


@app.get("/api/explanations")
def api_explanations(uuid: str, conn=Depends(get_conn)):
    return explain_mod.stored(conn, uuid)


@app.get("/api/health")
def health():
    return {"ok": True, "busy": runner.busy()}


# --------------------------------------------------------------------------- #

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
