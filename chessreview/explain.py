"""Plain-English explanations of why a move was bad.

The division of labour matters more than the prompt. Language models are poor
chess players and will happily invent a tactic that isn't on the board, so this
module never asks one to evaluate a position. Stockfish does that, and the model
is handed the verified facts:

  * the position, as a FEN and a text board
  * the move played and the engine's preferred move
  * the eval and win probability before and after
  * the line the engine wanted to play
  * the refutation line: what the opponent does to punish the move played

That last one is the whole point. "You dropped 47% win probability" says nothing
a beginner can act on; "after 8.Qxg7 Black plays Qf6 and your queen has no safe
square" does. Getting it means running the engine on the position *after* the
move, which is one extra analysis of one position, done on demand rather than
stored for every move of every game.
"""

import hashlib
import json
import os
import time

import chess
import chess.engine

from . import analyze as analyze_mod
from . import db

DEFAULT_MODEL = "claude-opus-5"
PV_PLIES = 8

SYSTEM = """You are a chess coach explaining a single position to an improving \
beginner (roughly 800-1200 rating). They know how the pieces move and what \
check and checkmate are. They do not know opening theory or positional jargon.

You will be given verified analysis from the Stockfish engine: the position, the \
move that was played, the move the engine preferred, the evaluation before and \
after, and the concrete lines the engine calculated.

Rules:
- The engine's analysis is ground truth. Never contradict it and never claim a \
move is good when the engine says it lost material or advantage.
- Do not calculate new variations of your own. Explain only the lines you were \
given. If the given lines do not make the reason obvious, say what the move \
gives up in plain terms (a piece, a pawn, king safety, the initiative) rather \
than inventing a tactic.
- If `special` is "brilliant" or "great", this move was GOOD, not bad. Say what \
made it work — the sacrifice and what it bought, or what the alternative would \
have thrown away. Do not criticise it.
- Otherwise lead with the concrete consequence. What actually hangs, gets \
forked, gets trapped, or gets lost? Name the squares and pieces.
- Then say what the better move does, in one sentence.
- Write 3 to 5 sentences of plain prose. No headings, no bullet lists.
- Do not quote the raw evaluation numbers or win percentages back at them; \
translate into words like "loses a piece" or "throws away a winning position".
- Define any term a beginner might not know the first time you use it, briefly \
and in-line. Terms like fork, pin, discovered attack, and back rank all need a \
short gloss.
- Do not be encouraging or discouraging. Just explain."""

GAME_SYSTEM = """You are a chess coach reviewing one finished game for an \
improving beginner (roughly 800-1200 rating).

You will be given the result from the player's point of view, which colour they \
had, how long the game was, which phases it actually reached, their significant \
errors, and their opponent's significant errors. Each error carries derived \
facts: how much material it cost, whether the piece that moved was captured on \
the opponent's very next move, which pieces were left attacked and undefended, \
and how many seconds were spent on it. A `patterns_across_these_errors` block \
totals up the player's.

Write a short review, 3 to 5 sentences of plain prose:
- What actually decided the game. This is often something the opponent did — if \
the deciding moment is in `opponent_significant_errors`, say so.
- The one habit most worth fixing, but ONLY if the player's own errors support \
one. Base it on the derived facts, not on the fact that a move differed from \
the engine's choice.
- One concrete thing to do differently next game, again only if their errors \
support it.

Rules:
- The engine analysis is ground truth. Do not invent moves, lines, or mistakes.
- IF `you_made_no_significant_errors` IS TRUE: the player did not blunder. Say \
plainly that they played a clean game, state what actually decided it, and give \
NO habit to fix and NO criticism. Do not manufacture a weakness. A short review \
is correct here.
- Only discuss phases listed in `phases_this_game_reached`. A game that ended in \
the opening has no middlegame or endgame — never refer to one.
- Never describe an error the player did not make. Every criticism must point to \
a specific entry in `your_significant_errors`.
- Scale the claim to the evidence. One mistake is one mistake, not "several \
costly mistakes" or "a tendency".
- NEVER give advice that depends on having an engine. The player cannot consult \
one while playing. "Play the engine's move", "follow the main line", "trust your \
instincts" and anything similar are forbidden: they restate the definition of a \
mistake instead of naming its cause.
- The advice must be a check the player can run themselves at the board. Good \
shapes: "before you move, look at what your opponent's last move attacks"; \
"after choosing a move, ask whether the piece you are moving can just be taken"; \
"you spent under ten seconds on both blunders — slow down in sharp positions". \
Pick the one the derived facts actually support.
- No headings, no bullet lists, no praise padding.
- Do not quote evaluation numbers; translate into plain words."""


CLEAN_GAME_SYSTEM = """You are a chess coach writing a short note about a game \
an improving beginner has just played.

The engine found NO significant mistakes by this player in this game. That fact \
is the entire reason you are writing this note.

You will be given the result from the player's point of view, which colour they \
had, how long the game was, which phases it reached, and their opponent's \
significant errors if there were any.

Write 2 to 3 sentences of plain prose:
- Say plainly that they made no significant mistakes in this game.
- Say what actually decided it. If the opponent's error list is not empty, name \
that move and what it cost them. If it is empty, the game was decided by the \
result itself — a resignation, a timeout, an abandoned game — so say that.
- If `your_best_moves` is not empty, name one: "brilliant" is a sound sacrifice, \
"great" is the only move that held. This is the one place praise is earned, so \
state it plainly and without inflating it.

Rules:
- Do NOT give advice, a takeaway, a habit to fix, or anything to work on. There \
is nothing to fix in this game. A note with no advice in it is the correct and \
complete answer.
- Do NOT criticise the player, however gently, and do not hedge the praise. Do \
not write "however", "but", "the one thing to watch", or "focus on".
- Do NOT attribute the opponent's mistake to the player in any way. Their \
blunder is not evidence of anything about this player.
- Do not invent moves or lines, and only mention phases the game reached.
- No headings, no bullet lists, no evaluation numbers."""


USER_PREAMBLE = "Here is the verified engine analysis. Explain it."

DEFAULT_PROMPTS = {"move": SYSTEM, "game": GAME_SYSTEM,
                   "game_clean": CLEAN_GAME_SYSTEM}


class ExplainError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# editable prompts
# --------------------------------------------------------------------------- #

def get_prompt(conn, kind):
    """The system prompt in force for `move` or `game`, edits included."""
    if kind not in DEFAULT_PROMPTS:
        raise ExplainError(f"unknown prompt kind {kind!r}")
    stored = db.get_setting(conn, f"prompt.{kind}") if conn is not None else None
    return stored.strip() if stored and stored.strip() else DEFAULT_PROMPTS[kind]


def set_prompt(conn, kind, text):
    """Save an edited prompt. Empty text resets it to the built-in default."""
    if kind not in DEFAULT_PROMPTS:
        raise ExplainError(f"unknown prompt kind {kind!r}")
    db.set_setting(conn, f"prompt.{kind}", (text or "").strip())
    return get_prompt(conn, kind)


def prompt_state(conn):
    return {
        kind: {
            "text": get_prompt(conn, kind),
            "default": default,
            "is_default": get_prompt(conn, kind) == default,
        }
        for kind, default in DEFAULT_PROMPTS.items()
    }


# --------------------------------------------------------------------------- #
# building verified context
# --------------------------------------------------------------------------- #

def board_text(board):
    """A plain text board. Uppercase is White, lowercase is Black, '.' is empty."""
    rows = []
    for rank in range(7, -1, -1):
        cells = []
        for file in range(8):
            piece = board.piece_at(chess.square(file, rank))
            cells.append(piece.symbol() if piece else ".")
        rows.append(f"{rank + 1}  " + " ".join(cells))
    rows.append("   a b c d e f g h")
    return "\n".join(rows)


def material_text(board):
    names = {chess.PAWN: "P", chess.KNIGHT: "N", chess.BISHOP: "B",
             chess.ROOK: "R", chess.QUEEN: "Q"}
    out = []
    for color, label in ((chess.WHITE, "White"), (chess.BLACK, "Black")):
        bits = []
        for piece_type in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT,
                           chess.PAWN):
            count = len(board.pieces(piece_type, color))
            if count:
                bits.append(f"{names[piece_type]}x{count}" if count > 1
                            else names[piece_type])
        out.append(f"{label}: K " + " ".join(bits))
    return "  |  ".join(out)


PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                chess.ROOK: 5, chess.QUEEN: 9}
PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop",
               chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}


def pv_line(engine, board, limit, plies=PV_PLIES):
    """The engine's principal variation, as (SAN strings, Move objects)."""
    if board.is_game_over(claim_draw=False):
        return [], []
    info = engine.analyse(board, limit)
    line = board.copy(stack=False)
    sans, moves = [], []
    for move in (info.get("pv") or [])[:plies]:
        if not line.is_legal(move):
            break
        sans.append(line.san(move))
        moves.append(move)
        line.push(move)
    return sans, moves


def pv_san(engine, board, limit, plies=PV_PLIES):
    return pv_line(engine, board, limit, plies)[0]


def material_balance(board, color):
    """Material in pawns from `color`'s point of view; positive means ahead."""
    total = 0
    for piece_type, value in PIECE_VALUES.items():
        total += value * len(board.pieces(piece_type, color))
        total -= value * len(board.pieces(piece_type, not color))
    return total


def loose_pieces(board, color):
    """Your pieces the opponent attacks that you do not defend, worst first.

    This is the single most common losing pattern at beginner level, and it is
    cheap and exact to compute, unlike "was it a fork".
    """
    out = []
    for square, piece in board.piece_map().items():
        if piece.color != color or piece.piece_type == chess.KING:
            continue
        if not board.attackers(not color, square):
            continue
        defended = bool(board.attackers(color, square))
        if not defended:
            out.append({"piece": PIECE_NAMES[piece.piece_type],
                        "square": chess.square_name(square),
                        "value_pawns": PIECE_VALUES[piece.piece_type],
                        "defended": False})
    out.sort(key=lambda d: -d["value_pawns"])
    return out


def error_features(board_before, played, refutation_moves, color):
    """Derived, checkable facts about *what kind* of error this was.

    Without these the payload only ever says "you played X, the engine wanted
    Y", and the only pattern a model can extract from a list of those is "you
    didn't play the engine's move" — true by definition and useless as advice.
    """
    after = board_before.copy(stack=False)
    after.push(played)

    end = after.copy(stack=False)
    for move in refutation_moves:
        if not end.is_legal(move):
            break
        end.push(move)

    swing = material_balance(end, color) - material_balance(board_before, color)
    reply = refutation_moves[0] if refutation_moves else None
    moved_piece = board_before.piece_at(played.from_square)

    return {
        "material_change_pawns": round(swing, 1),
        "lost_material": swing <= -1,
        "moved_piece": PIECE_NAMES[moved_piece.piece_type] if moved_piece else None,
        "moved_to": chess.square_name(played.to_square),
        "was_a_capture": board_before.is_capture(played),
        "gave_check": after.is_check(),
        # The most damning single fact: the opponent's very next move takes the
        # piece you just moved.
        "piece_you_moved_is_captured_next": bool(
            reply is not None and reply.to_square == played.to_square
            and after.is_capture(reply)),
        "opponents_reply": after.san(reply) if reply is not None else None,
        "your_undefended_attacked_pieces": loose_pieces(after, color),
    }


def numbered_line(board, moves):
    """['e4','e5','Nf3'] -> '1. e4 e5 2. Nf3', starting from the right number."""
    if not moves:
        return "(none)"
    out = []
    number = board.fullmove_number
    white_to_move = board.turn == chess.WHITE
    if not white_to_move and moves:
        out.append(f"{number}... {moves[0]}")
        moves = moves[1:]
        number += 1
        white_to_move = True
    for i, san in enumerate(moves):
        if white_to_move:
            out.append(f"{number}. {san}")
        else:
            out.append(san)
            number += 1
        white_to_move = not white_to_move
    return " ".join(out)


def seconds_spent(conn, game_uuid, ply, clock_secs, base_seconds, increment):
    """How long this move took, from the clock delta since the same side's
    previous move. `clock_secs` is the reading *after* the move."""
    if clock_secs is None:
        return None
    prev = conn.execute("SELECT clock_secs FROM moves WHERE game_uuid=? AND ply=?",
                        (game_uuid, ply - 2)).fetchone()
    start = prev["clock_secs"] if prev and prev["clock_secs"] is not None else base_seconds
    if start is None:
        return None
    return round(max(0.0, start - clock_secs + (increment or 0)), 1)


def move_context(conn, engine, limit, game_uuid, ply):
    """Everything the model is allowed to know about one move."""
    game = conn.execute("SELECT * FROM games WHERE uuid = ?", (game_uuid,)).fetchone()
    move = conn.execute("SELECT * FROM moves WHERE game_uuid = ? AND ply = ?",
                        (game_uuid, ply)).fetchone()
    if not game or not move:
        raise ExplainError("move not found, or the game has not been analyzed")

    board = chess.Board(move["fen_before"])
    played = chess.Move.from_uci(move["uci"])
    best_uci = move["best_uci"]

    best_line = []
    if best_uci:
        best_line = pv_san(engine, board, limit)

    after = board.copy(stack=False)
    after.push(played)
    refutation_san, refutation_moves = pv_line(engine, after, limit)
    features = error_features(board, played, refutation_moves,
                              chess.WHITE if move["side"] == "white" else chess.BLACK)

    def pawns(cp):
        return None if cp is None else round(cp / 100.0, 1)

    return {
        "game": {
            "you_played": game["color"],
            "your_result": game["result"],
            "termination": game["termination"],
            "opening": game["opening"],
            "time_control": game["time_control"],
        },
        "position": {
            "fen": move["fen_before"],
            "board": board_text(board),
            "material": material_text(board),
            "side_to_move": move["side"],
            "move_number": move["move_no"],
            "phase": move["phase"],
            "is_your_move": bool(move["is_player"]),
        },
        "played": {
            "san": move["san"],
            "judgment": move["judgment"],
            # brilliant = a sound sacrifice you were not already winning
            # without; great = the move that had to be found.
            "special": move["special"],
            "material_change_over_the_forced_line_pawns": move["material_swing"],
            "win_pct_the_second_best_move_would_have_cost": (
                round(move["alt_winp_gap"], 1)
                if move["alt_winp_gap"] is not None else None),
            "eval_before_pawns": pawns(move["eval_before"]),
            "eval_after_pawns": pawns(move["eval_after"]),
            "mate_before": move["mate_before"],
            "mate_after": move["mate_after"],
            "win_pct_before": round(move["winp_before"], 1),
            "win_pct_after": round(move["winp_after"], 1),
            "win_pct_lost": round(move["winp_loss"], 1),
            "clock_left_secs": move["clock_secs"],
            "seconds_spent_on_this_move": seconds_spent(
                conn, game_uuid, ply, move["clock_secs"], game["base_seconds"],
                game["increment"]),
            **features,
        },
        "engine_preferred": {
            "san": move["best_san"],
            "line": numbered_line(board, best_line),
        },
        "what_happens_after_your_move": numbered_line(after, refutation_san),
    }


def _moments(conn, engine, limit, game, game_uuid, is_player, cap):
    """Blunders and mistakes for one side, worst first, in board order."""
    rows = conn.execute(
        """SELECT * FROM moves WHERE game_uuid = ? AND is_player = ?
           AND judgment IN ('blunder','mistake')
           ORDER BY winp_loss DESC LIMIT ?""", (game_uuid, is_player, cap)).fetchall()
    out = []
    for move in sorted(rows, key=lambda r: r["ply"]):
        board = chess.Board(move["fen_before"])
        played = chess.Move.from_uci(move["uci"])
        after = board.copy(stack=False)
        after.push(played)
        refutation_san, refutation_moves = pv_line(engine, after, limit)
        colour = chess.WHITE if move["side"] == "white" else chess.BLACK
        out.append({
            "move_number": move["move_no"],
            "side": move["side"],
            "played": move["san"],
            "judgment": move["judgment"],
            "engine_preferred": move["best_san"],
            "win_pct_lost": round(move["winp_loss"], 1),
            "phase": move["phase"],
            "seconds_spent_on_this_move": seconds_spent(
                conn, game_uuid, move["ply"], move["clock_secs"],
                game["base_seconds"], game["increment"]),
            "what_happens_after": numbered_line(after, refutation_san),
            **error_features(board, played, refutation_moves, colour),
        })
    return out


def game_context(conn, engine, limit, game_uuid, max_moments=8):
    game = conn.execute("SELECT * FROM games WHERE uuid = ?", (game_uuid,)).fetchone()
    if not game:
        raise ExplainError("game not found")

    moments = _moments(conn, engine, limit, game, game_uuid, 1, max_moments)
    # The opponent's errors were previously withheld, which meant a game decided
    # entirely by their blunder looked, to the model, like a game with no cause.
    opponent = _moments(conn, engine, limit, game, game_uuid, 0, max_moments)

    good = [dict(r) for r in conn.execute(
        """SELECT move_no, side, san, special, material_swing, alt_winp_gap
           FROM moves WHERE game_uuid = ? AND is_player = 1
           AND special IS NOT NULL ORDER BY ply""", (game_uuid,))]
    for row in good:
        row["why"] = ("a sound sacrifice" if row["special"] == "brilliant"
                      else "the only move that held the position")
        row["material_change_pawns"] = row.pop("material_swing")
        row["win_pct_the_alternative_would_have_cost"] = (
            round(row.pop("alt_winp_gap"), 1)
            if row["alt_winp_gap"] is not None else None)

    shape = conn.execute(
        """SELECT COUNT(*) plies, SUM(is_player = 1) your_moves,
                  GROUP_CONCAT(DISTINCT phase) phases
           FROM moves WHERE game_uuid = ?""", (game_uuid,)).fetchone()

    times = [m["seconds_spent_on_this_move"] for m in moments
             if m["seconds_spent_on_this_move"] is not None]
    phases = {}
    for m in moments:
        phases[m["phase"]] = phases.get(m["phase"], 0) + 1
    patterns = {
        "you_made_no_significant_errors": not moments,
        "your_brilliant_moves": sum(1 for g in good if g["special"] == "brilliant"),
        "your_great_moves": sum(1 for g in good if g["special"] == "great"),
        "your_error_count": len(moments),
        "opponent_error_count": len(opponent),
        "errors_by_phase": phases,
        "errors_that_lost_material": sum(1 for m in moments if m["lost_material"]),
        "errors_where_the_piece_you_moved_was_taken_next":
            sum(1 for m in moments if m["piece_you_moved_is_captured_next"]),
        "errors_leaving_a_piece_undefended_and_attacked":
            sum(1 for m in moments if m["your_undefended_attacked_pieces"]),
        "errors_that_gave_check": sum(1 for m in moments if m["gave_check"]),
        "checks_that_hung_the_checking_piece": sum(
            1 for m in moments
            if m["gave_check"] and m["piece_you_moved_is_captured_next"]),
        "errors_played_in_under_10_seconds": sum(1 for t in times if t < 10),
        "median_seconds_on_these_errors": (
            round(sorted(times)[len(times) // 2], 1) if times else None),
    }

    return {
        "you_played": game["color"],
        "your_result": game["result"],
        "termination": game["termination"],
        "opening": game["opening"],
        "time_control": game["time_control"],
        # A four-move game has no middlegame. Without this the model writes
        # about phases the game never reached.
        "game_length_your_moves": shape["your_moves"] or 0,
        "phases_this_game_reached": sorted(
            (shape["phases"] or "").split(",")) if shape["phases"] else [],
        "your_accuracy_pct": (round(game["my_accuracy"], 1)
                              if game["my_accuracy"] is not None else None),
        "opponent_accuracy_pct": (round(game["opp_accuracy"], 1)
                                  if game["opp_accuracy"] is not None else None),
        "your_avg_centipawn_loss": (round(game["my_acpl"])
                                    if game["my_acpl"] is not None else None),
        "patterns_across_these_errors": patterns,
        "your_significant_errors": moments,
        "your_best_moves": good,
        "opponent_significant_errors": opponent,
    }


def fingerprint(context):
    return hashlib.sha256(
        json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# the model call
# --------------------------------------------------------------------------- #

def resolve_provider(explicit=None):
    """anthropic | local. Falls back to local when no Anthropic key is present."""
    choice = (explicit or os.environ.get("CHESS_REVIEW_LLM_PROVIDER") or "").lower()
    if choice in ("anthropic", "local"):
        return choice
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("CHESS_REVIEW_LLM_BASE_URL"):
        return "local"
    return "anthropic"


def _user_prompt(context):
    return USER_PREAMBLE + "\n\n" + json.dumps(context, indent=2, default=str)


def ask_model(system, context, provider=None, model=None, max_tokens=1500,
              effort=None, base_url=None, api_key=None):
    """Route to whichever backend is configured. Returns (text, meta)."""
    return ask_chat(system, [{"role": "user", "content": _user_prompt(context)}],
                    provider=provider, model=model, max_tokens=max_tokens,
                    effort=effort, base_url=base_url, api_key=api_key)


def ask_chat(system, messages, provider=None, model=None, max_tokens=1500,
             effort=None, base_url=None, api_key=None):
    """Multi-turn variant. `messages` is a list of {role, content} dicts."""
    provider = resolve_provider(provider)
    if provider == "local":
        return _ask_local(system, messages, model=model, max_tokens=max_tokens,
                          base_url=base_url, api_key=api_key)
    return _ask_anthropic(system, messages, model=model, max_tokens=max_tokens,
                          effort=effort)


# -- local OpenAI-compatible server (mlx_lm.server, llama.cpp, LM Studio, …) -- #

def local_base(base_url=None):
    """Normalise the configured base URL to something we can append paths to.

    People paste whatever their server printed, so accept the full endpoint as
    well as the base and strip the parts we add back ourselves.
    """
    base = (base_url or os.environ.get("CHESS_REVIEW_LLM_BASE_URL")
            or "http://127.0.0.1:8080/v1").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


def local_key(api_key=None):
    return api_key or os.environ.get("CHESS_REVIEW_LLM_API_KEY") or None


def _ask_local(system, messages, model=None, max_tokens=1500, base_url=None,
               api_key=None):
    """Talk to a local OpenAI-compatible chat endpoint.

    `mlx_lm.server`, llama.cpp's server, LM Studio and Ollama all speak this
    shape, so one code path covers them. Plain HTTP rather than a vendor SDK:
    there is no MLX client library, and requests is already a dependency.
    """
    import requests

    base = local_base(base_url)
    model = model or os.environ.get("CHESS_REVIEW_LLM_MODEL") or "local-model"
    timeout = float(os.environ.get("CHESS_REVIEW_LLM_TIMEOUT", "180"))
    url = f"{base}/chat/completions"

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        # Low but non-zero: the task is narration of fixed facts, so there is
        # nothing to gain from sampling diversity.
        "temperature": 0.3,
        "stream": False,
        "messages": [{"role": "system", "content": system}] + messages,
    }
    headers = {"Content-Type": "application/json"}
    key = local_key(api_key)
    if key:
        # Bearer is what OpenAI-compatible servers expect; a few also read
        # x-api-key, so send both rather than guessing wrong.
        headers["Authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectionError as exc:
        raise ExplainError(
            f"could not reach the local model server at {base}. Is it running? "
            f"For MLX: mlx_lm.server --model <model> --port "
            f"{base.rsplit(':', 1)[-1].split('/')[0]}"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise ExplainError(
            f"the local model server did not respond within {timeout:.0f}s"
        ) from exc

    if resp.status_code == 404:
        raise ExplainError(
            f"404 from {url}. The base URL is probably wrong: it should be the "
            f"part before /chat/completions, and most servers want /v1 on the "
            f"end (http://host:port/v1). Use Test connection in Settings to see "
            f"which paths that server answers on.")
    if resp.status_code in (401, 403):
        raise ExplainError(
            f"{resp.status_code} from {url} — the server rejected the API key. "
            f"Set it in Settings, or in CHESS_REVIEW_LLM_API_KEY.")
    if resp.status_code != 200:
        raise ExplainError(f"{url} returned {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, AttributeError) as exc:
        raise ExplainError(
            f"unexpected response from the local model server: {resp.text[:300]}"
        ) from exc
    if not text:
        raise ExplainError("the local model returned an empty response")

    usage = data.get("usage") or {}
    return text, {
        "model": f"local/{data.get('model') or model}",
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


def probe_local(base_url=None, api_key=None, timeout=10):
    """Find which path a local server actually answers on.

    A 404 almost always means the base URL is off by a path segment, so rather
    than making the user guess, try the plausible variants and report what each
    one said.
    """
    import requests

    given = local_base(base_url)
    key = local_key(api_key)
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["x-api-key"] = key

    candidates, seen = [], set()
    for base in (given, given + "/v1",
                 given[:-3].rstrip("/") if given.endswith("/v1") else given):
        if base and base not in seen:
            seen.add(base)
            candidates.append(base)

    results, models = [], []
    for base in candidates:
        url = f"{base}/models"
        entry = {"url": url}
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            entry["status"] = resp.status_code
            if resp.status_code == 200:
                try:
                    data = resp.json().get("data") or []
                    found = [m.get("id") for m in data if isinstance(m, dict)]
                    entry["models"] = found[:20]
                    if not models:
                        models = found
                except ValueError:
                    entry["note"] = "responded, but not JSON"
            elif resp.status_code in (401, 403):
                entry["note"] = "reachable, but the API key was rejected"
        except requests.exceptions.RequestException as exc:
            entry["status"] = None
            entry["note"] = type(exc).__name__
        results.append(entry)

    working = next((r for r in results if r.get("status") == 200), None)
    return {
        "configured_base": given,
        "api_key_set": bool(key),
        "tried": results,
        "working_base": working["url"][: -len("/models")] if working else None,
        "models": models,
    }


# -- Anthropic ------------------------------------------------------------- #

def _ask_anthropic(system, messages, model=None, max_tokens=1500, effort=None):
    try:
        import anthropic
    except ImportError as exc:
        raise ExplainError(
            "The anthropic package is not installed. Run: "
            "pip install -r requirements.txt") from exc
    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        raise ExplainError(f"could not create the Anthropic client: {exc}") from exc

    model = model or os.environ.get("CHESS_REVIEW_LLM_MODEL") or DEFAULT_MODEL
    effort = effort or os.environ.get("CHESS_REVIEW_LLM_EFFORT", "medium")

    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            # Opus 5's safety classifiers can decline a request; this re-runs a
            # declined one on Anthropic's recommended fallback automatically.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=messages,
        )
    except anthropic.AuthenticationError as exc:
        raise ExplainError(
            "Anthropic rejected the credentials. Set ANTHROPIC_API_KEY in the "
            "environment (or in .env for Docker)."
        ) from exc
    except anthropic.NotFoundError as exc:
        raise ExplainError(f"unknown model {model!r}") from exc
    except anthropic.RateLimitError as exc:
        raise ExplainError("rate limited by the Anthropic API, try again shortly") from exc
    except anthropic.APIStatusError as exc:
        raise ExplainError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ExplainError(f"could not reach the Anthropic API: {exc}") from exc
    except TypeError as exc:
        # The SDK raises a bare TypeError, not AuthenticationError, when it
        # cannot resolve any credential at all — so it needs its own arm.
        if "authentication" in str(exc).lower():
            raise ExplainError(
                "No Anthropic credentials found. Set ANTHROPIC_API_KEY, or "
                "switch to a local model with CHESS_REVIEW_LLM_PROVIDER=local "
                "and CHESS_REVIEW_LLM_BASE_URL=http://127.0.0.1:8080/v1"
            ) from exc
        raise
    except Exception as exc:  # never let a raw SDK error reach the UI
        raise ExplainError(f"explanation failed: {exc}") from exc

    # Always check the stop reason before touching content: on a refusal the
    # content list can be empty and indexing it would raise.
    if response.stop_reason == "refusal":
        raise ExplainError("the model declined to answer this one")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise ExplainError("the model returned an empty response")
    usage = response.usage
    return text, {
        "model": response.model,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }


# --------------------------------------------------------------------------- #
# public entry points
# --------------------------------------------------------------------------- #

def _cached(conn, game_uuid, ply, kind, fp):
    row = conn.execute(
        "SELECT text, model, fingerprint FROM explanations "
        "WHERE game_uuid = ? AND ply = ? AND kind = ?", (game_uuid, ply, kind)
    ).fetchone()
    if row and row["fingerprint"] == fp:
        return {"text": row["text"], "model": row["model"], "cached": True}
    return None


def _store(conn, game_uuid, ply, kind, fp, text, meta):
    conn.execute(
        "INSERT OR REPLACE INTO explanations(game_uuid, ply, kind, fingerprint, "
        "model, text, created_at, input_tokens, output_tokens) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (game_uuid, ply, kind, fp, meta.get("model"), text, int(time.time()),
         meta.get("input_tokens"), meta.get("output_tokens")),
    )
    conn.commit()


def explain_move(conn, game_uuid, ply, depth=16, engine_path=None, threads=1,
                 refresh=False, model=None, provider=None, base_url=None,
                 api_key=None):
    engine = analyze_mod.open_engine(engine_path, threads=threads, hash_mb=128)
    try:
        context = move_context(conn, engine, chess.engine.Limit(depth=depth),
                               game_uuid, ply)
    finally:
        engine.quit()

    fp = fingerprint(context)
    if not refresh:
        hit = _cached(conn, game_uuid, ply, "move", fp)
        if hit:
            return hit
    text, meta = ask_model(get_prompt(conn, "move"), context,
                              provider=provider, model=model,
                              max_tokens=1500, base_url=base_url,
                              api_key=api_key)
    _store(conn, game_uuid, ply, "move", fp, text, meta)
    return {"text": text, "model": meta.get("model"), "cached": False}


def explain_game(conn, game_uuid, depth=16, engine_path=None, threads=1,
                 refresh=False, model=None, provider=None, base_url=None,
                 api_key=None):
    engine = analyze_mod.open_engine(engine_path, threads=threads, hash_mb=128)
    try:
        context = game_context(conn, engine, chess.engine.Limit(depth=depth),
                               game_uuid)
    finally:
        engine.quit()

    clean = context["patterns_across_these_errors"]["you_made_no_significant_errors"]
    if clean:
        # Nothing to criticise, so send nothing criticisable: an empty error list
        # and an all-zero patterns block are an invitation to invent a weakness.
        context.pop("your_significant_errors", None)
        context.pop("patterns_across_these_errors", None)
        context["you_made_no_significant_errors"] = True

    fp = fingerprint(context)
    if not refresh:
        hit = _cached(conn, game_uuid, 0, "game", fp)
        if hit:
            return hit
    text, meta = ask_model(get_prompt(conn, "game_clean" if clean else "game"),
                           context,
                              provider=provider, model=model,
                              max_tokens=2000, base_url=base_url,
                              api_key=api_key)
    _store(conn, game_uuid, 0, "game", fp, text, meta)
    return {"text": text, "model": meta.get("model"), "cached": False}


def preview(conn, game_uuid, ply=None, depth=16, engine_path=None):
    """The exact request that would be sent, without sending it."""
    engine = analyze_mod.open_engine(engine_path, threads=1, hash_mb=128)
    try:
        limit = chess.engine.Limit(depth=depth)
        if ply is None:
            kind, context = "game", game_context(conn, engine, limit, game_uuid)
        else:
            kind, context = "move", move_context(conn, engine, limit, game_uuid,
                                                 int(ply))
    finally:
        engine.quit()
    return {
        "kind": kind,
        "system": get_prompt(conn, kind),
        "user": _user_prompt(context),
        "provider": resolve_provider(),
    }


def stored(conn, game_uuid):
    """Every explanation already written for a game, for the UI to show at once."""
    rows = conn.execute(
        "SELECT ply, kind, text, model FROM explanations WHERE game_uuid = ?",
        (game_uuid,)).fetchall()
    return {f"{r['kind']}:{r['ply']}": dict(r) for r in rows}
