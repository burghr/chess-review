"""Terminal rendering of the reports in `stats`.

This module owns formatting only. Every number comes from `stats`, which the
web API serves as JSON, so the two front ends cannot drift apart.
"""

import datetime

from rich.console import Console
from rich.table import Table

from . import stats
from .stats import Filters  # re-exported: the CLI builds filters from here

__all__ = ["Filters", "summary", "openings", "mistakes", "clock", "blunders",
           "blunder_fens", "sessions", "opponents", "trend", "review_game"]


def _fmt(value, spec=".1f", dash="-"):
    return dash if value is None else format(value, spec)


def _wld(row):
    return f"{row['wins']}-{row['losses']}-{row['draws']}"


def _table(title, *columns):
    t = Table(title=title, title_style="bold cyan", header_style="bold")
    for col in columns:
        if isinstance(col, tuple):
            t.add_column(col[0], justify=col[1])
        else:
            t.add_column(col)
    return t


def summary(conn, f, console: Console):
    data = stats.summary(conn, f)
    totals = data["totals"]
    if not totals["games"]:
        console.print("[yellow]No games match those filters.[/yellow]")
        return False

    span = ""
    if totals["first_at"] and totals["last_at"]:
        d1 = datetime.date.fromtimestamp(totals["first_at"])
        d2 = datetime.date.fromtimestamp(totals["last_at"])
        span = f"  {d1} to {d2}"
    console.print(
        f"\n[bold]{totals['games']} games[/bold]{span}   "
        f"[green]{totals['wins']}W[/green] [red]{totals['losses']}L[/red] "
        f"{totals['draws']}D   score [bold]{_fmt(totals['score'])}%[/bold]   "
        f"({totals['analyzed']} analyzed)"
    )
    if totals["acpl"] is not None:
        console.print(
            f"Average centipawn loss [bold]{_fmt(totals['acpl'])}[/bold]   "
            f"accuracy [bold]{_fmt(totals['accuracy'])}%[/bold]   "
            f"blunders per game [bold]{_fmt(totals['blunders_per_game'], '.2f')}"
            f"[/bold]"
        )

    t = _table("By color and time control", "Split", ("Games", "right"),
               ("W-L-D", "right"), ("Score", "right"), ("ACPL", "right"),
               ("Accuracy", "right"))
    for row in data["by_color"] + data["by_time_class"]:
        t.add_row(str(row["k"]), str(row["games"]), _wld(row),
                  f"{_fmt(row['score'])}%", _fmt(row["acpl"]),
                  _fmt(row["accuracy"]))
    console.print(t)

    t = _table("How games end", "Result", "Termination", ("Games", "right"),
               ("Share", "right"))
    for row in data["endings"]:
        t.add_row(row["result"], row["termination"], str(row["games"]),
                  f"{row['share']:.0f}%")
    console.print(t)

    if data["ratings"]:
        t = _table("Rating range (rated games)", "Time class", ("Low", "right"),
                   ("High", "right"), ("Latest", "right"))
        for row in data["ratings"]:
            t.add_row(row["time_class"], str(row["low"]), str(row["high"]),
                      str(row["latest"] or "-"))
        console.print(t)
    return True


def openings(conn, f, console: Console, by="opening", limit=25):
    rows = stats.openings(conn, f, by=by, limit=limit)
    if not rows:
        console.print(f"[yellow]No opening has {f.min_games}+ games yet. "
                      f"Lower it with --min-games.[/yellow]")
        return
    t = _table(f"Openings by score (worst first, {f.min_games}+ games)",
               "Opening", "Color", ("Games", "right"), ("W-L-D", "right"),
               ("Score", "right"), ("ACPL", "right"))
    for row in rows:
        score = row["score"]
        style = "red" if score < 40 else ("green" if score > 60 else "")
        cell = f"[{style}]{_fmt(score)}%[/{style}]" if style else f"{_fmt(score)}%"
        t.add_row(row["opening"][:52], row["color"], str(row["games"]),
                  _wld(row), cell, _fmt(row["acpl"]))
    console.print(t)


def mistakes(conn, f, console: Console):
    data = stats.move_quality(conn, f)
    if not data["games"]:
        console.print("[yellow]No analyzed games yet. Run `analyze` first.[/yellow]")
        return

    t = _table(f"Move quality over {data['games']} analyzed games", "Who",
               ("Moves", "right"), ("Blunders", "right"), ("Mistakes", "right"),
               ("Inaccuracies", "right"), ("Per game", "right"), ("ACPL", "right"))
    for row in data["by_who"]:
        t.add_row(row["who"], str(row["moves"]), str(row["blunders"]),
                  str(row["mistakes"]), str(row["inaccuracies"]),
                  _fmt(row["per_game"], ".2f"), _fmt(row["acpl"]))
    console.print(t)

    t = _table("Your errors by phase", "Phase", ("Moves", "right"),
               ("Blunders", "right"), ("Mistakes", "right"),
               ("Blunder rate", "right"), ("ACPL", "right"))
    for row in data["by_phase"]:
        t.add_row(row["phase"], str(row["moves"]), str(row["blunders"]),
                  str(row["mistakes"]), f"{row['blunder_rate']:.1f}%",
                  _fmt(row["acpl"]))
    console.print(t)

    t = _table("Your errors by move number", "Moves", ("Played", "right"),
               ("Blunders", "right"), ("Blunder rate", "right"), ("ACPL", "right"))
    for row in data["by_move"]:
        t.add_row(row["bucket"], str(row["moves"]), str(row["blunders"]),
                  f"{row['blunder_rate']:.1f}%", _fmt(row["acpl"]))
    console.print(t)


def clock(conn, f, console: Console):
    rows = stats.clock(conn, f)
    if not rows:
        console.print("[yellow]No clock data in the analyzed games.[/yellow]")
        return
    t = _table("Your errors by time remaining", "Clock", ("Moves", "right"),
               ("Blunders", "right"), ("Mistakes", "right"),
               ("Blunder rate", "right"), ("ACPL", "right"))
    for row in rows:
        t.add_row(row["bucket"], str(row["moves"]), str(row["blunders"]),
                  str(row["mistakes"]), f"{row['blunder_rate']:.1f}%",
                  _fmt(row["acpl"]))
    console.print(t)


def blunders(conn, f, console: Console, limit=20, phase=None):
    rows = stats.blunders(conn, f, limit=limit, phase=phase)
    if not rows:
        console.print("[yellow]No blunders found (or nothing analyzed yet).[/yellow]")
        return
    t = _table("Worst moves, review these", ("Move", "right"), "Played", "Best",
               ("Win% lost", "right"), "Phase", "Opening", "Link")
    for row in rows:
        dots = "..." if row["side"] == "black" else "."
        t.add_row(f"{row['move_no']}{dots}", row["san"], row["best_san"] or "?",
                  f"{row['winp_loss']:.0f}", row["phase"],
                  (row["opening"] or "")[:28], row["url"] or "")
    console.print(t)
    console.print("[dim]Set the positions up on a board: "
                  "`chess-review report blunders --fens`[/dim]")


def blunder_fens(conn, f, limit=20):
    return stats.blunders(conn, f, limit=limit)


def sessions(conn, f, console: Console, gap_minutes=60):
    rows = stats.sessions(conn, f, gap_minutes=gap_minutes)
    if not rows:
        console.print("[yellow]Not enough games for session analysis.[/yellow]")
        return
    t = _table(f"Performance by game number within a session "
               f"(new session after {gap_minutes} min idle)",
               "Game #", ("Games", "right"), ("W-L-D", "right"),
               ("Score", "right"), ("ACPL", "right"))
    for row in rows:
        t.add_row(row["bucket"], str(row["games"]), _wld(row),
                  f"{_fmt(row['score'])}%", _fmt(row["acpl"]))
    console.print(t)


def opponents(conn, f, console: Console):
    rows = stats.opponents(conn, f)
    if not rows:
        console.print("[yellow]No rated games with both ratings recorded.[/yellow]")
        return
    t = _table("Performance by opponent strength", "Opponent", ("Games", "right"),
               ("W-L-D", "right"), ("Score", "right"), ("ACPL", "right"))
    for row in rows:
        t.add_row(row["bucket"], str(row["games"]), _wld(row),
                  f"{_fmt(row['score'])}%", _fmt(row["acpl"]))
    console.print(t)


def trend(conn, f, console: Console, bucket="month"):
    rows = stats.trend(conn, f, bucket=bucket)
    if not rows:
        console.print("[yellow]No games to trend.[/yellow]")
        return
    t = _table(f"By {bucket}", bucket.capitalize(), ("Games", "right"),
               ("W-L-D", "right"), ("Score", "right"), ("Avg rating", "right"),
               ("ACPL", "right"), ("Accuracy", "right"))
    for row in rows:
        t.add_row(row["bucket"], str(row["games"]), _wld(row),
                  f"{_fmt(row['score'])}%", _fmt(row["rating"], ".0f"),
                  _fmt(row["acpl"]), _fmt(row["accuracy"]))
    console.print(t)


def review_game(conn, game_uuid, console: Console):
    data = stats.game_detail(conn, game_uuid)
    if not data:
        console.print("[red]Game not found.[/red]")
        return
    g, moves = data["game"], data["moves"]

    def named(user, rating):
        return f"{user or '?'}" + (f" ({rating})" if rating else "")

    played = (datetime.datetime.fromtimestamp(g["played_at"]).strftime("%Y-%m-%d %H:%M")
              if g["played_at"] else "?")
    console.print(
        f"\n[bold]{named(g['white_username'], g['white_rating'])} vs "
        f"{named(g['black_username'], g['black_rating'])}[/bold]\n"
        f"{played}  {g['time_class']} {g['time_control']}  "
        f"you played {g['color']}, {g['result']} by {g['termination']}\n"
        f"{g['opening'] or 'unknown opening'} ({g['eco'] or '-'})  {g['url'] or ''}"
    )
    if g["analyzed_at"]:
        console.print(
            f"Your ACPL [bold]{_fmt(g['my_acpl'])}[/bold] "
            f"(opponent {_fmt(g['opp_acpl'])})   "
            f"accuracy [bold]{_fmt(g['my_accuracy'])}%[/bold] "
            f"(opponent {_fmt(g['opp_accuracy'])}%)"
        )
    if not moves:
        console.print(f"[yellow]Not analyzed yet. Run `analyze --game "
                      f"{game_uuid}`.[/yellow]")
        return

    marks = {"blunder": ("??", "red"), "mistake": ("?", "yellow"),
             "inaccuracy": ("?!", "cyan")}
    t = _table("Moves", ("#", "right"), "White", ("Eval", "right"), "Black",
               ("Eval", "right"))
    pair = {}
    for r in moves:
        mark, style = marks.get(r["judgment"], ("", ""))
        cell = f"{r['san']}{mark}"
        if style and r["is_player"]:
            cell = f"[{style}]{cell}[/{style}]"
        elif style:
            cell = f"[dim]{cell}[/dim]"
        flip = 1 if r["side"] == "white" else -1
        if r["mate_after"] is not None:
            mate = r["mate_after"] * flip
            evtxt = f"{'#' if mate > 0 else '#-'}{abs(mate)}"
        else:
            evtxt = f"{r['eval_after'] * flip / 100:+.1f}"
        pair.setdefault(r["move_no"], {})[r["side"]] = (cell, evtxt)
    for no in sorted(pair):
        w = pair[no].get("white", ("", ""))
        b = pair[no].get("black", ("", ""))
        t.add_row(str(no), w[0], w[1], b[0], b[1])
    console.print(t)

    bad = [r for r in moves if r["is_player"] and
           r["judgment"] in ("blunder", "mistake")]
    if bad:
        t = _table("What to look at", ("Move", "right"), "Played", "Better",
                   ("Win% lost", "right"), "FEN before")
        for r in bad:
            dots = "..." if r["side"] == "black" else "."
            t.add_row(f"{r['move_no']}{dots}", r["san"], r["best_san"] or "?",
                      f"{r['winp_loss']:.0f}", r["fen_before"])
        console.print(t)
