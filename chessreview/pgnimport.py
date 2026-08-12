"""Import games from a PGN file.

Chess.com is the main source, but any game you can export as PGN lands in the
same tables: over-the-board games, Lichess exports, a position you typed out by
hand. The analysis and reports do not care where a game came from.
"""

import hashlib
import io
import time

import chess.pgn

from . import db

DRAW_RESULTS = {"1/2-1/2"}


def _link(game):
    """The game's URL, if the PGN carries a real one rather than Site "?"."""
    for tag in ("Link", "Site"):
        value = game.headers.get(tag) or ""
        if value.startswith("http"):
            return value
    return None


def _uuid_for(game, movetext):
    link = _link(game)
    if link:
        return link
    key = "|".join([
        game.headers.get("White", ""), game.headers.get("Black", ""),
        game.headers.get("Date", ""), movetext,
    ])
    return "pgn:" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _epoch(game):
    date = game.headers.get("UTCDate") or game.headers.get("Date") or ""
    clock = game.headers.get("UTCTime") or "12:00:00"
    date = date.replace(".", "-")
    if "?" in date or len(date) != 10:
        return None
    try:
        import datetime
        dt = datetime.datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
    except ValueError:
        return None


def _termination(game, result, color):
    raw = (game.headers.get("Termination") or "").lower()
    for word in ("checkmate", "resign", "time", "abandon", "agree",
                 "repetition", "stalemate", "insufficient"):
        if word in raw:
            return word
    if result == "draw":
        return "agreed"
    return "unknown"


def import_file(conn, path, player, default_time_class="unknown"):
    """Import every game in a PGN file. Returns (imported, skipped)."""
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        text = fh.read()
    return import_text(conn, text, player, default_time_class)


def import_text(conn, text, player, default_time_class="unknown"):
    stream = io.StringIO(text)
    imported = skipped = 0
    target = player.lower()

    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        h = game.headers
        white = (h.get("White") or "").lower()
        black = (h.get("Black") or "").lower()
        if target == white:
            color = "white"
        elif target == black:
            color = "black"
        else:
            skipped += 1
            continue

        movetext = " ".join(node.move.uci() for node in game.mainline())
        if not movetext:
            skipped += 1
            continue

        raw_result = h.get("Result", "*")
        if raw_result in DRAW_RESULTS:
            result = "draw"
        elif raw_result == "*":
            skipped += 1
            continue
        else:
            won = (raw_result == "1-0") == (color == "white")
            result = "win" if won else "loss"

        uuid = _uuid_for(game, movetext)
        if conn.execute("SELECT 1 FROM games WHERE uuid = ?", (uuid,)).fetchone():
            skipped += 1
            continue

        def rating(tag):
            try:
                return int(h.get(tag, ""))
            except ValueError:
                return None

        exporter = chess.pgn.StringExporter(headers=True, variations=False,
                                            comments=True)
        db.upsert_game(conn, {
            "uuid": uuid,
            "player": player,
            "url": _link(game),
            "played_at": _epoch(game),
            "color": color,
            "result": result,
            "termination": _termination(game, result, color),
            "rated": 0,
            "rules": "chess",
            "time_class": h.get("TimeClass") or default_time_class,
            "time_control": h.get("TimeControl"),
            "base_seconds": None,
            "increment": None,
            "eco": h.get("ECO"),
            "opening": h.get("Opening"),
            "white_username": h.get("White"),
            "white_rating": rating("WhiteElo"),
            "black_username": h.get("Black"),
            "black_rating": rating("BlackElo"),
            "my_rating": rating("WhiteElo" if color == "white" else "BlackElo"),
            "opp_rating": rating("BlackElo" if color == "white" else "WhiteElo"),
            "site_accuracy": None,
            "ply_count": len(movetext.split()),
            "pgn": game.accept(exporter),
            "fetched_at": int(time.time()),
        })
        imported += 1

    conn.commit()
    return imported, skipped
