"""Run Stockfish over stored games and write per-move judgments.

Each position in a game is evaluated exactly once. A move's cost is then the
difference between the eval of the position it came from and the eval of the
position it led to, both taken from the mover's point of view. That is half the
engine work of evaluating "my move" and "the best move" separately, and it gives
the same answer.
"""

import os
import re
import shutil
import time

import chess
import chess.engine
import chess.pgn

from . import db, evals

CLOCK_RE = re.compile(r"\[%clk\s+(\d+):(\d+):(\d+(?:\.\d+)?)\]")


def find_engine(explicit=None):
    path = explicit or os.environ.get("CHESS_REVIEW_ENGINE") or shutil.which("stockfish")
    if not path or not os.path.exists(path):
        raise RuntimeError(
            "Stockfish not found. Install it with `brew install stockfish`, "
            "or pass --engine /path/to/stockfish."
        )
    return path


def open_engine(path=None, threads=2, hash_mb=256):
    engine = chess.engine.SimpleEngine.popen_uci(find_engine(path))
    options = {}
    if "Threads" in engine.options:
        options["Threads"] = threads
    if "Hash" in engine.options:
        options["Hash"] = hash_mb
    if options:
        engine.configure(options)
    return engine


def _clock_from_comment(comment):
    m = CLOCK_RE.search(comment or "")
    if not m:
        return None
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


_PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                 chess.ROOK: 5, chess.QUEEN: 9}


def _material(board, color):
    """Material in pawns from `color`'s point of view; positive means ahead."""
    total = 0
    for piece_type, value in _PIECE_VALUES.items():
        total += value * len(board.pieces(piece_type, color))
        total -= value * len(board.pieces(piece_type, not color))
    return total


def _terminal_score(board):
    """Eval for a position where the game is already over, POV side to move."""
    if board.is_checkmate():
        return -evals.MATE_CP, -1
    return 0, None


def _eval_position(engine, board, limit, multipv=2):
    """Evaluate one position, from the side-to-move's point of view.

    Asks for two lines rather than one. The second is what makes "this was the
    only move" detectable, and the principal variation it returns is also the
    forced continuation after whatever gets played here — so the material swing
    behind a sacrifice costs no extra search.
    """
    if board.is_game_over(claim_draw=False):
        cp, mate = _terminal_score(board)
        return {"cp": cp, "mate": mate, "best_uci": None, "second_cp": None,
                "pv": []}
    infos = engine.analyse(board, limit, multipv=multipv)
    if isinstance(infos, dict):        # some engines ignore multipv
        infos = [infos]
    best = infos[0]
    pov = best["score"].pov(board.turn)
    pv = list(best.get("pv") or [])
    second_cp = None
    if len(infos) > 1 and infos[1].get("score") is not None:
        second_cp = infos[1]["score"].pov(board.turn).score(
            mate_score=evals.MATE_CP)
    return {
        "cp": pov.score(mate_score=evals.MATE_CP),
        "mate": pov.mate(),
        "best_uci": pv[0].uci() if pv else None,
        "second_cp": second_cp,
        "pv": pv,
    }


def analyze_game(engine, game_row, limit, max_ply=None):
    """-> (move rows, aggregate dict) for one stored game."""
    import io

    pgn_text = game_row["pgn"]
    if not pgn_text:
        raise ValueError("game has no PGN")
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("could not parse PGN")

    player_color = chess.WHITE if game_row["color"] == "white" else chess.BLACK

    # Replay once to collect the move list, the position before each move, and
    # the clock reading attached to each move.
    board = game.board()
    steps = []
    for node in game.mainline():
        move = node.move
        if max_ply and len(steps) >= max_ply:
            break
        steps.append(
            {
                "fen_before": board.fen(),
                "board_before": board.copy(stack=False),
                "move": move,
                "san": board.san(move),
                "clock": _clock_from_comment(node.comment),
            }
        )
        board.push(move)
    final_board = board

    if not steps:
        raise ValueError("game has no moves")

    # One eval per position: N positions before each move, plus the final one.
    position_evals = []
    for step in steps:
        position_evals.append(_eval_position(engine, step["board_before"], limit))
    position_evals.append(_eval_position(engine, final_board, limit))

    rows = []
    agg = {
        "white": {"cp_losses": [], "accuracies": []},
        "black": {"cp_losses": [], "accuracies": []},
    }

    for i, step in enumerate(steps):
        b = step["board_before"]
        mover = b.turn
        before, after_info = position_evals[i], position_evals[i + 1]
        cp_before, mate_before = before["cp"], before["mate"]
        best_uci = before["best_uci"]

        # position_evals[i+1] is from the opponent's POV; flip it back to ours.
        cp_after = -after_info["cp"]
        mate_after = (-after_info["mate"]
                      if after_info["mate"] is not None else None)

        # How much win probability the second-best move here would have cost.
        alt_winp_gap = None
        if before["second_cp"] is not None:
            alt_winp_gap = max(0.0, evals.win_percent(cp_before)
                               - evals.win_percent(before["second_cp"]))

        # Material after the immediate exchange resolves, from the mover's side.
        # Four plies, not more: a sacrifice is a property of this move, and over
        # a longer window a lost position keeps shedding material and every king
        # shuffle looks like a sacrifice.
        played_board = b.copy(stack=False)
        played_board.push(step["move"])
        material_before = _material(b, mover)
        line = played_board.copy(stack=False)
        for pv_move in after_info["pv"][:4]:
            if not line.is_legal(pv_move):
                break
            line.push(pv_move)
        material_swing = _material(line, mover) - material_before

        cp_loss = max(0, evals.clamp_cp(cp_before) - evals.clamp_cp(cp_after))
        winp_before = evals.win_percent(cp_before)
        winp_after = evals.win_percent(cp_after)
        winp_loss = max(0.0, winp_before - winp_after)
        accuracy = evals.move_accuracy(winp_before, winp_after)

        best_san = None
        if best_uci:
            try:
                best_san = b.san(chess.Move.from_uci(best_uci))
            except (ValueError, AssertionError):
                best_san = None

        side = "white" if mover == chess.WHITE else "black"
        agg[side]["cp_losses"].append(cp_loss)
        agg[side]["accuracies"].append(accuracy)

        rows.append(
            {
                "game_uuid": game_row["uuid"],
                "ply": i + 1,
                "move_no": i // 2 + 1,
                "side": side,
                "is_player": 1 if mover == player_color else 0,
                "san": step["san"],
                "uci": step["move"].uci(),
                "fen_before": step["fen_before"],
                "clock_secs": step["clock"],
                "eval_before": cp_before,
                "eval_after": cp_after,
                "mate_before": mate_before,
                "mate_after": mate_after,
                "best_san": best_san,
                "best_uci": best_uci,
                "cp_loss": cp_loss,
                "winp_before": winp_before,
                "winp_after": winp_after,
                "winp_loss": winp_loss,
                "accuracy": accuracy,
                "judgment": evals.judge(winp_loss, cp_loss),
                "special": evals.classify_special(
                    evals.judge(winp_loss, cp_loss), material_swing,
                    cp_before, cp_after, alt_winp_gap),
                "material_swing": material_swing,
                "alt_winp_gap": alt_winp_gap,
                "phase": evals.phase_of(b, i + 1),
            }
        )

    me = "white" if player_color == chess.WHITE else "black"
    them = "black" if me == "white" else "white"

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    aggregate = {
        "ply_count": len(rows),
        "my_acpl": mean(agg[me]["cp_losses"]),
        "opp_acpl": mean(agg[them]["cp_losses"]),
        "my_accuracy": evals.game_accuracy(agg[me]["accuracies"]),
        "opp_accuracy": evals.game_accuracy(agg[them]["accuracies"]),
    }
    return rows, aggregate


def pending_games(conn, player, limit=None, reanalyze=False, min_depth=None):
    sql = "SELECT * FROM games WHERE player = ? AND pgn IS NOT NULL"
    params = [player]
    if not reanalyze:
        clause = "analyzed_at IS NULL"
        if min_depth:
            clause = f"({clause} OR analysis_depth < ?)"
            params.append(min_depth)
        sql += f" AND {clause}"
    sql += " ORDER BY played_at DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def analyze_games(conn, games, depth=14, engine_path=None, threads=2, hash_mb=256,
                  max_ply=None, on_done=None, should_stop=None):
    """Analyze a list of game rows, committing after each one.

    `should_stop` is checked between games, so cancelling from the web UI costs
    at most one game's work rather than the whole batch.
    """
    limit = chess.engine.Limit(depth=depth)
    engine = open_engine(engine_path, threads=threads, hash_mb=hash_mb)
    ok, failed = 0, 0
    try:
        for game_row in games:
            if should_stop and should_stop():
                break
            try:
                rows, agg = analyze_game(engine, game_row, limit, max_ply=max_ply)
            except Exception as exc:  # a single bad PGN shouldn't kill the batch
                failed += 1
                if on_done:
                    on_done(game_row, None, exc)
                continue
            db.replace_moves(conn, game_row["uuid"], rows)
            conn.execute(
                "UPDATE games SET analyzed_at=?, analysis_depth=?, ply_count=?, "
                "my_acpl=?, opp_acpl=?, my_accuracy=?, opp_accuracy=? WHERE uuid=?",
                (
                    int(time.time()), depth, agg["ply_count"], agg["my_acpl"],
                    agg["opp_acpl"], agg["my_accuracy"], agg["opp_accuracy"],
                    game_row["uuid"],
                ),
            )
            conn.commit()
            ok += 1
            if on_done:
                on_done(game_row, agg, None)
    finally:
        engine.quit()
    return ok, failed
