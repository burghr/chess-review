"""Command line entry point."""

import argparse
import datetime
import os
import sys

from rich.console import Console
from rich.progress import (BarColumn, Progress, SpinnerColumn, TextColumn,
                           TimeElapsedColumn)
from rich.table import Table

from . import analyze as analyze_mod
from . import db, explain, fetch as fetch_mod, pgnimport, reports

console = Console()

REPORTS = ["summary", "openings", "mistakes", "clock", "blunders", "sessions",
           "opponents", "trend"]


def _add_filters(p):
    p.add_argument("--user", help="Chess.com username (defaults to the one in the DB)")
    p.add_argument("--time-class", choices=["bullet", "blitz", "rapid", "daily"])
    p.add_argument("--color", choices=["white", "black"])
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--rated-only", action="store_true")
    p.add_argument("--min-games", type=int, default=3,
                   help="minimum sample size for grouped rows (default 3)")


def _resolve_player(conn, args):
    player = getattr(args, "user", None) or db.default_player(conn)
    if not player:
        console.print("[red]No games in the database yet. Run: "
                      "chess-review fetch --user YOURNAME[/red]")
        sys.exit(1)
    return player


def _filters(conn, args):
    return reports.Filters(
        player=_resolve_player(conn, args),
        time_class=args.time_class,
        color=args.color,
        since=args.since,
        until=args.until,
        rated_only=args.rated_only,
        min_games=args.min_games,
    )


def _resolve_game(conn, player, ref):
    """Accept a game URL, a uuid (or prefix), or 1-based index from most recent."""
    if ref.isdigit():
        row = conn.execute(
            "SELECT uuid FROM games WHERE player=? ORDER BY played_at DESC "
            "LIMIT 1 OFFSET ?", (player, int(ref) - 1)
        ).fetchone()
        return row["uuid"] if row else None
    if ref.startswith("http"):
        row = conn.execute("SELECT uuid FROM games WHERE url = ?", (ref,)).fetchone()
        return row["uuid"] if row else None
    row = conn.execute(
        "SELECT uuid FROM games WHERE uuid LIKE ? LIMIT 1", (ref + "%",)
    ).fetchone()
    return row["uuid"] if row else None


# --------------------------------------------------------------------------- #

def cmd_fetch(conn, args):
    player = args.user
    console.print(f"Fetching archives for [bold]{player}[/bold] ...")

    def progress(url, total, kept):
        month = "/".join(url.rsplit("/", 2)[-2:])
        console.print(f"  {month}: {total} games, {kept} new")

    try:
        new, months = fetch_mod.fetch(
            conn, player, since=args.since, refetch=args.refetch,
            rules=None if args.include_variants else "chess", progress=progress,
        )
    except fetch_mod.FetchError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    console.print(f"[green]{new} new games[/green] from {months} month(s).")
    if new:
        console.print("Next: [bold]chess-review analyze[/bold]")


def cmd_import(conn, args):
    imported, skipped = pgnimport.import_file(
        conn, args.path, args.user, default_time_class=args.time_class or "unknown"
    )
    console.print(f"[green]{imported} imported[/green], {skipped} skipped "
                  f"(already present, unfinished, or not {args.user}'s game).")


def cmd_analyze(conn, args):
    player = _resolve_player(conn, args)
    if args.game:
        uuid = _resolve_game(conn, player, args.game)
        if not uuid:
            console.print("[red]Game not found.[/red]")
            sys.exit(1)
        games = conn.execute("SELECT * FROM games WHERE uuid = ?", (uuid,)).fetchall()
    else:
        games = analyze_mod.pending_games(
            conn, player, limit=args.limit, reanalyze=args.reanalyze,
            min_depth=args.depth if args.upgrade else None,
        )
    if not games:
        console.print("[green]Nothing to analyze.[/green]")
        return

    console.print(
        f"Analyzing [bold]{len(games)}[/bold] game(s) at depth {args.depth} "
        f"with {args.threads} thread(s). Ctrl-C is safe, progress is saved "
        f"per game."
    )
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"),
                  TimeElapsedColumn(), console=console) as prog:
        task = prog.add_task("analyzing", total=len(games))

        def on_done(game_row, agg, exc):
            if exc:
                prog.console.print(f"[red]skip {game_row['url']}: {exc}[/red]")
            else:
                prog.console.print(
                    f"[dim]{game_row['url'] or game_row['uuid']}  "
                    f"ACPL {agg['my_acpl']:.0f}  acc {agg['my_accuracy']:.1f}%[/dim]"
                )
            prog.advance(task)

        ok, failed = analyze_mod.analyze_games(
            conn, games, depth=args.depth, engine_path=args.engine,
            threads=args.threads, hash_mb=args.hash, max_ply=args.max_ply,
            on_done=on_done,
        )
    console.print(f"[green]{ok} analyzed[/green]" +
                  (f", [red]{failed} failed[/red]" if failed else ""))


def cmd_report(conn, args):
    f = _filters(conn, args)
    console.print(f"[dim]{f.describe()}[/dim]")
    names = REPORTS if args.name == "all" else [args.name]
    for i, name in enumerate(names):
        if name == "summary":
            if not reports.summary(conn, f, console) and args.name == "all":
                return
        elif name == "openings":
            reports.openings(conn, f, console, by=args.by, limit=args.limit)
        elif name == "mistakes":
            reports.mistakes(conn, f, console)
        elif name == "clock":
            reports.clock(conn, f, console)
        elif name == "blunders":
            if args.fens:
                for r in reports.blunder_fens(conn, f, limit=args.limit):
                    console.print(f"{r['move_no']}. {r['san']} (best {r['best_san']}) "
                                  f"-{r['winp_loss']:.0f}%\n  {r['fen_before']}")
            else:
                reports.blunders(conn, f, console, limit=args.limit, phase=args.phase)
        elif name == "sessions":
            reports.sessions(conn, f, console, gap_minutes=args.gap)
        elif name == "opponents":
            reports.opponents(conn, f, console)
        elif name == "trend":
            reports.trend(conn, f, console, bucket=args.bucket)
        if i < len(names) - 1:
            console.print()


def cmd_games(conn, args):
    f = _filters(conn, args)
    where, params = f.where()
    rows = conn.execute(
        f"""SELECT uuid, url, played_at, color, result, termination, opening,
                   my_rating, opp_rating, my_acpl, analyzed_at, time_class
            FROM games WHERE {where} ORDER BY played_at DESC LIMIT ?""",
        params + [args.limit],
    ).fetchall()
    t = Table(title=f"Recent games ({f.describe()})", header_style="bold")
    for col, just in (("#", "right"), ("Date", "left"), ("Class", "left"),
                      ("Color", "left"), ("Result", "left"), ("Opp", "right"),
                      ("ACPL", "right"), ("Opening", "left")):
        t.add_column(col, justify=just)
    for i, r in enumerate(rows, 1):
        date = (datetime.datetime.fromtimestamp(r["played_at"]).strftime("%Y-%m-%d")
                if r["played_at"] else "?")
        color = {"win": "green", "loss": "red"}.get(r["result"], "yellow")
        result = f"[{color}]{r['result']}[/{color}]"
        if r["termination"]:
            result += f" [dim]{r['termination']}[/dim]"
        t.add_row(str(i), date, r["time_class"] or "", r["color"], result,
                  str(r["opp_rating"] or "-"),
                  "-" if r["my_acpl"] is None else f"{r['my_acpl']:.0f}",
                  (r["opening"] or "")[:38])
    console.print(t)
    console.print("[dim]Review one with: chess-review review 1[/dim]")


def cmd_review(conn, args):
    player = _resolve_player(conn, args)
    uuid = _resolve_game(conn, player, args.game)
    if not uuid:
        console.print("[red]Game not found. Try `chess-review games` for the list."
                      "[/red]")
        sys.exit(1)
    reports.review_game(conn, uuid, console)


def cmd_explain(conn, args):
    player = _resolve_player(conn, args)
    uuid = _resolve_game(conn, player, args.game)
    if not uuid:
        console.print("[red]Game not found.[/red]")
        sys.exit(1)

    ply = args.ply
    if ply is None and args.worst:
        row = conn.execute(
            "SELECT ply FROM moves WHERE game_uuid = ? AND is_player = 1 "
            "AND judgment = 'blunder' ORDER BY winp_loss DESC LIMIT 1", (uuid,)
        ).fetchone()
        if not row:
            console.print("[yellow]No blunder in that game; explaining the whole "
                          "game instead.[/yellow]")
        else:
            ply = row["ply"]

    with console.status("Thinking..."):
        try:
            if ply is None:
                result = explain.explain_game(conn, uuid, depth=args.depth,
                                              engine_path=args.engine,
                                              refresh=args.refresh,
                                              model=args.model)
            else:
                result = explain.explain_move(conn, uuid, ply, depth=args.depth,
                                              engine_path=args.engine,
                                              refresh=args.refresh,
                                              model=args.model)
        except explain.ExplainError as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(1)

    console.print()
    console.print(result["text"])
    tag = "cached" if result.get("cached") else result.get("model", "")
    console.print(f"[dim]{tag}[/dim]")


def cmd_prompt(conn, args):
    if args.action == "show":
        for kind, info in explain.prompt_state(conn).items():
            tag = "default" if info["is_default"] else "edited"
            console.print(f"\n[bold cyan]{kind}[/bold cyan] [dim]({tag})[/dim]")
            console.print(info["text"])
        console.print(f"\n[dim]User message preamble: "
                      f"{explain.USER_PREAMBLE}[/dim]")
        return
    if args.action == "reset":
        explain.set_prompt(conn, args.kind, "")
        console.print(f"[green]{args.kind} prompt reset to the default.[/green]")
        return
    if not args.file:
        console.print("[red]--file is required for `prompt set`.[/red]")
        sys.exit(1)
    text = open(args.file, encoding="utf-8").read()
    explain.set_prompt(conn, args.kind, text)
    console.print(f"[green]{args.kind} prompt updated from {args.file}.[/green]")


def cmd_serve(conn, args):
    # The web app reads its own settings from the environment, so pass the
    # chosen database through rather than handing it a connection.
    os.environ.setdefault("CHESS_REVIEW_DB", args.db)
    if args.user:
        os.environ["CHESS_REVIEW_USER"] = args.user
    conn.close()
    import uvicorn
    console.print(f"Dashboard on [bold]http://{args.host}:{args.port}[/bold]  "
                  f"(db: {args.db})")
    uvicorn.run("chessreview.web.app:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


def build_parser():
    p = argparse.ArgumentParser(
        prog="chess-review",
        description="Local Chess.com game database with Stockfish analysis.",
    )
    p.add_argument("--db", default=db.DEFAULT_DB, help="path to the SQLite file")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download games from Chess.com")
    f.add_argument("--user", required=True)
    f.add_argument("--since", help="earliest archive month, YYYY-MM")
    f.add_argument("--refetch", action="store_true",
                   help="re-read months already marked complete")
    f.add_argument("--include-variants", action="store_true",
                   help="also store chess960, bughouse, etc.")
    f.set_defaults(func=cmd_fetch)

    i = sub.add_parser("import", help="import games from a PGN file")
    i.add_argument("path")
    i.add_argument("--user", required=True,
                   help="must match the White or Black tag in the PGN")
    i.add_argument("--time-class", help="label these games, e.g. rapid or otb")
    i.set_defaults(func=cmd_import)

    a = sub.add_parser("analyze", help="run Stockfish over unanalyzed games")
    a.add_argument("--user")
    a.add_argument("--limit", type=int, help="max games this run")
    a.add_argument("--game", help="one game: index, uuid, or URL")
    a.add_argument("--depth", type=int, default=14)
    a.add_argument("--threads", type=int, default=2)
    a.add_argument("--hash", type=int, default=256, help="engine hash in MB")
    a.add_argument("--max-ply", type=int, help="stop analyzing after N half-moves")
    a.add_argument("--engine", help="path to a Stockfish binary")
    a.add_argument("--reanalyze", action="store_true", help="redo analyzed games")
    a.add_argument("--upgrade", action="store_true",
                   help="also redo games analyzed below --depth")
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("report", help="trend reports")
    r.add_argument("name", nargs="?", default="all", choices=REPORTS + ["all"])
    _add_filters(r)
    r.add_argument("--limit", type=int, default=20, help="rows in list reports")
    r.add_argument("--by", choices=["opening", "eco"], default="opening")
    r.add_argument("--phase", choices=["opening", "middlegame", "endgame"])
    r.add_argument("--bucket", choices=["day", "week", "month"], default="month")
    r.add_argument("--gap", type=int, default=60,
                   help="minutes of idle time that start a new session")
    r.add_argument("--fens", action="store_true",
                   help="blunders report: print FENs to paste into a board")
    r.set_defaults(func=cmd_report)

    g = sub.add_parser("games", help="list stored games")
    _add_filters(g)
    g.add_argument("--limit", type=int, default=25)
    g.set_defaults(func=cmd_games)

    e = sub.add_parser("explain", help="ask Claude why a move was bad")
    e.add_argument("game", help="index from `games`, uuid, or Chess.com URL")
    e.add_argument("--ply", type=int, help="half-move to explain (default: the "
                                           "whole game)")
    e.add_argument("--worst", action="store_true",
                   help="explain your worst move in the game")
    e.add_argument("--depth", type=int, default=16,
                   help="engine depth for the explanation lines")
    e.add_argument("--engine", help="path to a Stockfish binary")
    e.add_argument("--model", help="override the Claude model")
    e.add_argument("--refresh", action="store_true", help="ignore the cache")
    e.add_argument("--user")
    e.set_defaults(func=cmd_explain)

    pr = sub.add_parser("prompt", help="view or edit the explanation prompts")
    pr.add_argument("action", choices=["show", "set", "reset"])
    pr.add_argument("--kind", choices=["move", "game"], default="move")
    pr.add_argument("--file", help="text file to load the prompt from (for set)")
    pr.set_defaults(func=cmd_prompt)

    w = sub.add_parser("serve", help="run the web dashboard")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--user", help="Chess.com username the dashboard defaults to")
    w.add_argument("--reload", action="store_true", help="auto-reload for dev")
    w.set_defaults(func=cmd_serve)

    v = sub.add_parser("review", help="move-by-move readout of one game")
    v.add_argument("game", help="index from `games`, uuid, or Chess.com URL")
    v.add_argument("--user")
    v.set_defaults(func=cmd_review)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    conn = db.connect(args.db)
    try:
        args.func(conn, args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped. Analyzed games are already saved.[/yellow]")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
