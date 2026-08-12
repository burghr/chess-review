"""Converting engine scores into things a human can act on.

Raw centipawn loss is a bad judge of a move on its own. Dropping 300cp when you
are already up a rook barely matters, while dropping 150cp from a dead-level
position can lose the game. So every move is scored twice: raw centipawn loss
(capped, for the classic ACPL number) and the drop in win probability, which is
what the judgment labels are based on.

The win-probability curve and the accuracy formula are Lichess's, so the numbers
here line up with what you would see on a Lichess analysis board.
"""

import math

# Centipawn values are clamped before any subtraction. Without this, one mate
# score turns an otherwise normal game's ACPL into nonsense.
CP_CLAMP = 1000
MATE_CP = 10000

# Win-probability drop thresholds for judging a move.
BLUNDER = 20.0
MISTAKE = 10.0
INACCURACY = 5.0


def score_to_cp(score, mate_score=MATE_CP):
    """python-chess PovScore -> signed centipawns from that POV."""
    return score.score(mate_score=mate_score)


def clamp_cp(cp):
    return max(-CP_CLAMP, min(CP_CLAMP, cp))


def win_percent(cp):
    """Centipawns -> win probability 0-100 for the side the score belongs to."""
    cp = max(-MATE_CP, min(MATE_CP, cp))
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * cp)) - 1.0)


def move_accuracy(winp_before, winp_after):
    """Lichess's per-move accuracy: 100 means the win% did not move at all."""
    drop = max(0.0, winp_before - winp_after)
    acc = 103.1668 * math.exp(-0.04354 * drop) - 3.1669
    return max(0.0, min(100.0, acc))


def judge(winp_loss, cp_loss):
    """Label a move. Win% drop leads; cp_loss only breaks ties near zero."""
    if winp_loss >= BLUNDER:
        return "blunder"
    if winp_loss >= MISTAKE:
        return "mistake"
    if winp_loss >= INACCURACY:
        return "inaccuracy"
    if cp_loss <= 10:
        return "best"
    return "good"


# Brilliant / Great, following Chess.com's published shape: a brilliant move is
# a sound sacrifice that was not already winning anyway; a great move is the one
# that had to be found. Both require the move to actually be the engine's choice.
SACRIFICE_PAWNS = 1.5      # material genuinely given up over the forced line
ALREADY_WINNING_CP = 200   # above this you were winning without finding it
STILL_FINE_CP = -50        # below this you are worse after it, so it is not sound
ONLY_MOVE_WINP = 10.0      # the alternative would have been a mistake
BRILLIANT_GAP_WINP = 5.0   # and a brilliancy should at least beat the obvious move


def classify_special(judgment, material_swing, eval_before, eval_after,
                     alt_winp_gap):
    """-> 'brilliant' | 'great' | None.

    `material_swing` is pawns gained (negative = given up) over the engine's
    forced continuation, from the mover's point of view. `alt_winp_gap` is how
    much win probability the second-best move would have cost.
    """
    if judgment != "best" or material_swing is None:
        return None
    sound = eval_after is not None and eval_after >= STILL_FINE_CP
    not_already_won = eval_before is not None and eval_before <= ALREADY_WINNING_CP
    gap = alt_winp_gap or 0.0

    if (material_swing <= -SACRIFICE_PAWNS and sound and not_already_won
            and gap >= BRILLIANT_GAP_WINP):
        return "brilliant"
    if gap >= ONLY_MOVE_WINP:
        return "great"
    return None


def phase_of(board, ply):
    """opening / middlegame / endgame for the position before a move.

    Endgame is decided by material rather than move number: queens plus rooks
    plus minors, both sides, weighted Q=4 R=2 B=N=1, out of 24 at the start.
    """
    import chess

    weights = {chess.QUEEN: 4, chess.ROOK: 2, chess.BISHOP: 1, chess.KNIGHT: 1}
    total = 0
    for piece_type, w in weights.items():
        total += w * len(board.pieces(piece_type, chess.WHITE))
        total += w * len(board.pieces(piece_type, chess.BLACK))
    if total <= 6:
        return "endgame"
    if ply <= 20:
        return "opening"
    return "middlegame"


def game_accuracy(move_accuracies):
    """Whole-game accuracy: the mean of the per-move numbers.

    Lichess blends a volatility-weighted mean with a harmonic mean. The plain
    mean is close enough for trend-spotting and much easier to reason about.
    """
    if not move_accuracies:
        return None
    return sum(move_accuracies) / len(move_accuracies)
