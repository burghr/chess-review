"""Pull games from the public Chess.com API into the local database.

The API is unauthenticated and free, but it rejects requests with a default
requests User-Agent, so we always send a descriptive one. Archives are listed
per month; finished months never change, so once fetched they are marked
complete and skipped on later runs.
"""

import re
import time

import requests

from . import db

API = "https://api.chess.com/pub"
# Chess.com sits behind Cloudflare, which 403s the default requests
# User-Agent and anything else containing "python-requests". Any descriptive
# string works; do not put the library name back in it.
USER_AGENT = "chess-review/0.1 (local game analysis tool)"

DRAW_CODES = {
    "agreed",
    "repetition",
    "stalemate",
    "insufficient",
    "50move",
    "timevsinsufficient",
}


class FetchError(RuntimeError):
    pass


def _get(url, session, retries=4):
    for attempt in range(retries):
        resp = session.get(url, timeout=30)
        if resp.status_code == 429:
            wait = min(60, 2 ** (attempt + 1))
            time.sleep(wait)
            continue
        if resp.status_code == 404:
            raise FetchError(f"404 from {url} (is the username right?)")
        resp.raise_for_status()
        return resp.json()
    raise FetchError(f"rate limited repeatedly on {url}")


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def _parse_time_control(tc):
    """'600+5' or '900' or '1/259200' (daily) -> (base_seconds, increment)."""
    if not tc:
        return None, None
    if "/" in tc:  # daily games: moves/seconds
        try:
            return int(tc.split("/")[1]), 0
        except (ValueError, IndexError):
            return None, None
    if "+" in tc:
        base, inc = tc.split("+", 1)
        try:
            return int(base), int(inc)
        except ValueError:
            return None, None
    try:
        return int(tc), 0
    except ValueError:
        return None, None


def _pgn_tag(pgn, tag):
    if not pgn:
        return None
    m = re.search(rf'\[{tag} "([^"]*)"\]', pgn)
    return m.group(1) if m else None


def _opening_from_eco_url(url):
    """'.../openings/Vienna-Game-Max-Lange-Defense-3.Bc4' -> readable name."""
    if not url:
        return None
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-\d+\..*$", "", slug)  # drop the trailing move, e.g. '-3.Bc4'
    return slug.replace("-", " ").strip() or None


def game_row(game, player):
    """One Chess.com API game object -> a row for the games table."""
    white = game.get("white", {}) or {}
    black = game.get("black", {}) or {}
    wname = (white.get("username") or "").lower()
    bname = (black.get("username") or "").lower()
    target = player.lower()

    if target == wname:
        color, mine, theirs = "white", white, black
    elif target == bname:
        color, mine, theirs = "black", black, white
    else:
        return None  # not this player's game (shouldn't happen from their archive)

    my_result = mine.get("result")
    opp_result = theirs.get("result")
    if my_result == "win":
        result, termination = "win", opp_result
    elif my_result in DRAW_CODES:
        result, termination = "draw", my_result
    else:
        result, termination = "loss", my_result

    pgn = game.get("pgn")
    base, inc = _parse_time_control(game.get("time_control"))
    accuracies = game.get("accuracies") or {}

    return {
        "uuid": game.get("uuid") or game.get("url"),
        "player": player,
        "url": game.get("url"),
        "played_at": game.get("end_time"),
        "color": color,
        "result": result,
        "termination": termination,
        "rated": 1 if game.get("rated") else 0,
        "rules": game.get("rules"),
        "time_class": game.get("time_class"),
        "time_control": game.get("time_control"),
        "base_seconds": base,
        "increment": inc,
        "eco": _pgn_tag(pgn, "ECO"),
        "opening": _opening_from_eco_url(_pgn_tag(pgn, "ECOUrl")),
        "white_username": white.get("username"),
        "white_rating": white.get("rating"),
        "black_username": black.get("username"),
        "black_rating": black.get("rating"),
        "my_rating": mine.get("rating"),
        "opp_rating": theirs.get("rating"),
        "site_accuracy": accuracies.get(color),
        "ply_count": None,
        "pgn": pgn,
        "fetched_at": int(time.time()),
    }


def list_archives(player, session=None):
    session = session or _session()
    data = _get(f"{API}/player/{player.lower()}/games/archives", session)
    return data.get("archives", [])


def fetch(conn, player, since=None, refetch=False, rules="chess", progress=None):
    """Fetch monthly archives into the DB. Returns (new_games, months_fetched)."""
    session = _session()
    archives = list_archives(player, session)
    if since:
        archives = [u for u in archives if u[-7:].replace("/", "-") >= since]

    known = {
        r["url"]: r
        for r in conn.execute(
            "SELECT url, complete FROM archives WHERE player = ?", (player,)
        )
    }
    current_month = time.strftime("%Y/%m")

    new_games = 0
    months = 0
    for url in archives:
        is_current = url.endswith(current_month)
        if not refetch and known.get(url) and known[url]["complete"] and not is_current:
            continue

        data = _get(url, session)
        games = data.get("games", [])
        months += 1
        kept = 0
        for game in games:
            if rules and game.get("rules") != rules:
                continue
            row = game_row(game, player)
            if not row or not row["uuid"]:
                continue
            existing = conn.execute(
                "SELECT uuid FROM games WHERE uuid = ?", (row["uuid"],)
            ).fetchone()
            if existing:
                continue  # never clobber analysis results with a re-fetch
            db.upsert_game(conn, row)
            new_games += 1
            kept += 1

        conn.execute(
            "INSERT OR REPLACE INTO archives(player, url, fetched_at, game_count, complete) "
            "VALUES (?,?,?,?,?)",
            (player, url, int(time.time()), len(games), 0 if is_current else 1),
        )
        conn.commit()
        if progress:
            progress(url, len(games), kept)

    return new_games, months
