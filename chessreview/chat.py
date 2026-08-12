"""Follow-up questions about an explained move or game.

The same rule as `explain` applies: the model narrates, the engine calculates.
That is harder here, because the most useful follow-up is "what if I'd played X
instead?" and answering it needs a real evaluation of X.

So before the question goes to the model, any legal move named in it is pulled
out and run through Stockfish. The model then receives verified analysis of
exactly the moves that were asked about, and is told to answer from that and
refuse anything it cannot see. No tool-calling protocol is involved, which
matters because small local models are unreliable at tool use.
"""

import re
import time

import chess
import chess.engine

from . import analyze as analyze_mod
from . import db, evals, explain

MAX_CANDIDATES = 3
MAX_HISTORY = 12
CANDIDATE_PLIES = 6

CHAT_SYSTEM = """You are a chess coach answering follow-up questions from an \
improving beginner (roughly 800-1200 rating), about a position you already \
explained.

The conversation began with verified Stockfish analysis and your explanation of \
it. Everything you know about this position comes from that analysis.

If the player asks about a specific move, the message may include a block headed \
"VERIFIED ANALYSIS OF MOVES YOU ASKED ABOUT". That block is engine output and is \
ground truth. Use it.

Rules:
- Never calculate a variation yourself. If you are asked about a move and there \
is no engine analysis of it in the conversation, say plainly that you would need \
to check it and ask them to name the move in standard notation so it can be \
analysed. Do not guess.
- Never contradict the engine numbers you were given.
- Answer the question actually asked, in 2 to 4 sentences of plain prose. No \
headings, no bullet lists.
- Never give advice that depends on having an engine at the board. "Play the \
engine's move" or "trust your instincts" are not answers.
- Define chess terms briefly and in-line the first time you use them.
- If the question is not about chess, say so briefly and stop."""


def _thread_key(ply):
    return (0, "game") if ply is None else (int(ply), "move")


def history(conn, game_uuid, ply=None):
    key_ply, kind = _thread_key(ply)
    rows = conn.execute(
        "SELECT role, content, model, created_at FROM chat_messages "
        "WHERE game_uuid=? AND ply=? AND kind=? ORDER BY id",
        (game_uuid, key_ply, kind)).fetchall()
    return [dict(r) for r in rows]


def clear(conn, game_uuid, ply=None):
    key_ply, kind = _thread_key(ply)
    conn.execute("DELETE FROM chat_messages WHERE game_uuid=? AND ply=? AND kind=?",
                 (game_uuid, key_ply, kind))
    conn.commit()


def _store(conn, game_uuid, ply, role, content, model=None):
    key_ply, kind = _thread_key(ply)
    conn.execute(
        "INSERT INTO chat_messages(game_uuid, ply, kind, role, content, model, "
        "created_at) VALUES (?,?,?,?,?,?,?)",
        (game_uuid, key_ply, kind, role, content, model, int(time.time())))
    conn.commit()


# --------------------------------------------------------------------------- #
# pulling candidate moves out of the question
# --------------------------------------------------------------------------- #

TOKEN_RE = re.compile(r"\b(?:O-O-O|O-O|0-0-0|0-0|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8]"
                      r"(?:=[QRBN])?[+#]?)\b")


def candidate_moves(fen, text, limit=MAX_CANDIDATES):
    """Legal moves named in the question, as (san, Move) pairs.

    Deliberately permissive: a false positive costs one engine evaluation and is
    clearly labelled, whereas a miss means the model has to refuse a reasonable
    question.
    """
    board = chess.Board(fen)
    found, seen = [], set()
    for raw in TOKEN_RE.findall(text or ""):
        token = raw.replace("0", "O") if raw.startswith("0") else raw
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        move = None
        try:
            move = board.parse_san(token)
        except ValueError:
            try:
                candidate = chess.Move.from_uci(token.lower())
                if candidate in board.legal_moves:
                    move = candidate
            except ValueError:
                move = None
        if move is None:
            continue
        found.append((board.san(move), move))
        if len(found) >= limit:
            break
    return board, found


def analyse_candidates(engine, limit, board, pairs, mover_color):
    """Run the engine on each named move so the answer is grounded."""
    out = []
    for san, move in pairs:
        after = board.copy(stack=False)
        after.push(move)
        info = analyze_mod._eval_position(engine, after, limit)
        cp = -info["cp"]         # flip to the point of view of whoever moved
        mate = -info["mate"] if info["mate"] is not None else None
        line_san, line_moves = explain.pv_line(engine, after, limit,
                                               plies=CANDIDATE_PLIES)
        features = explain.error_features(board, move, line_moves, mover_color)
        out.append({
            "move": san,
            "eval_after_pawns": round(cp / 100.0, 1),
            "mate_in": mate,
            "win_pct_for_the_mover_after": round(evals.win_percent(cp), 1),
            "material_change_pawns": features["material_change_pawns"],
            "this_piece_is_captured_next_move":
                features["piece_you_moved_is_captured_next"],
            "your_undefended_attacked_pieces_after":
                features["your_undefended_attacked_pieces"],
            "what_follows": explain.numbered_line(after, line_san),
        })
    return out


def _format_candidates(rows):
    import json
    return ("\n\nVERIFIED ANALYSIS OF MOVES YOU ASKED ABOUT (engine output, "
            "ground truth):\n" + json.dumps(rows, indent=2))


# --------------------------------------------------------------------------- #

def ask(conn, game_uuid, message, ply=None, depth=16, engine_path=None,
        provider=None, model=None, base_url=None, api_key=None):
    """Answer one follow-up question. Returns {text, model, candidates}."""
    message = (message or "").strip()
    if not message:
        raise explain.ExplainError("empty question")

    key_ply, kind = _thread_key(ply)
    explanation = conn.execute(
        "SELECT text FROM explanations WHERE game_uuid=? AND ply=? AND kind=?",
        (game_uuid, key_ply, kind)).fetchone()
    if not explanation:
        raise explain.ExplainError(
            "Explain this one first — the chat continues from that explanation.")

    engine = analyze_mod.open_engine(engine_path, threads=1, hash_mb=128)
    limit = chess.engine.Limit(depth=depth)
    candidates = []
    try:
        if ply is None:
            context = explain.game_context(conn, engine, limit, game_uuid)
        else:
            context = explain.move_context(conn, engine, limit, game_uuid, int(ply))
            row = conn.execute(
                "SELECT fen_before, side FROM moves WHERE game_uuid=? AND ply=?",
                (game_uuid, int(ply))).fetchone()
            board, pairs = candidate_moves(row["fen_before"], message)
            if pairs:
                colour = chess.WHITE if row["side"] == "white" else chess.BLACK
                candidates = analyse_candidates(engine, limit, board, pairs, colour)
    finally:
        engine.quit()

    user_turn = message + (_format_candidates(candidates) if candidates else "")

    messages = [
        {"role": "user", "content": explain._user_prompt(context)},
        {"role": "assistant", "content": explanation["text"]},
    ]
    for row in history(conn, game_uuid, ply)[-MAX_HISTORY:]:
        messages.append({"role": row["role"], "content": row["content"]})
    messages.append({"role": "user", "content": user_turn})

    text, meta = explain.ask_chat(
        CHAT_SYSTEM, messages, provider=provider, model=model, max_tokens=1200,
        base_url=base_url, api_key=api_key)

    # Store the plain question, not the engine block bolted onto it — the block
    # is regenerated per turn and would otherwise bloat the stored history.
    _store(conn, game_uuid, ply, "user", message)
    _store(conn, game_uuid, ply, "assistant", text, meta.get("model"))
    return {"text": text, "model": meta.get("model"),
            "candidates": [c["move"] for c in candidates]}
