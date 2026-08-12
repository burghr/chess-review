"""Every report as plain data.

The CLI renders these dicts as rich tables, the web API serves them as JSON.
Neither one knows any SQL, and there is exactly one implementation of each
question so the terminal and the browser can never disagree.
"""

import datetime

SCORE = ("(SUM(CASE result WHEN 'win' THEN 1.0 WHEN 'draw' THEN 0.5 ELSE 0 END) "
         "* 100.0 / COUNT(*))")

MOVE_BUCKET = """CASE
    WHEN m.move_no <= 10 THEN '1-10'
    WHEN m.move_no <= 20 THEN '11-20'
    WHEN m.move_no <= 30 THEN '21-30'
    WHEN m.move_no <= 40 THEN '31-40'
    ELSE '41+' END"""

CLOCK_BUCKET = """CASE
    WHEN m.clock_secs * 1.0 / g.base_seconds >= 0.75 THEN '75-100% left'
    WHEN m.clock_secs * 1.0 / g.base_seconds >= 0.50 THEN '50-75% left'
    WHEN m.clock_secs * 1.0 / g.base_seconds >= 0.25 THEN '25-50% left'
    WHEN m.clock_secs * 1.0 / g.base_seconds >= 0.10 THEN '10-25% left'
    ELSE 'under 10% left' END"""

CLOCK_ORDER = ["75-100% left", "50-75% left", "25-50% left", "10-25% left",
               "under 10% left"]

OPP_BUCKET = """CASE
    WHEN opp_rating - my_rating < -150 THEN '150+ below you'
    WHEN opp_rating - my_rating <  -50 THEN '50-150 below'
    WHEN opp_rating - my_rating <=  50 THEN 'within 50'
    WHEN opp_rating - my_rating <= 150 THEN '50-150 above'
    ELSE '150+ above you' END"""

OPP_ORDER = ["150+ below you", "50-150 below", "within 50", "50-150 above",
             "150+ above you"]


class Filters:
    """Shared WHERE-clause builder so every report honours the same flags."""

    def __init__(self, player, time_class=None, since=None, until=None,
                 color=None, rated_only=False, min_games=3):
        self.player = player
        self.time_class = time_class
        self.since = since
        self.until = until
        self.color = color
        self.rated_only = rated_only
        self.min_games = min_games

    def where(self, alias="games", analyzed_only=False):
        clauses = [f"{alias}.player = ?"]
        params = [self.player]
        if self.time_class:
            clauses.append(f"{alias}.time_class = ?")
            params.append(self.time_class)
        if self.color:
            clauses.append(f"{alias}.color = ?")
            params.append(self.color)
        if self.rated_only:
            clauses.append(f"{alias}.rated = 1")
        if self.since:
            clauses.append(f"{alias}.played_at >= ?")
            params.append(to_epoch(self.since))
        if self.until:
            clauses.append(f"{alias}.played_at < ?")
            params.append(to_epoch(self.until) + 86400)
        if analyzed_only:
            clauses.append(f"{alias}.analyzed_at IS NOT NULL")
        return " AND ".join(clauses), params

    def describe(self):
        bits = [self.player]
        if self.time_class:
            bits.append(self.time_class)
        if self.color:
            bits.append(f"as {self.color}")
        if self.rated_only:
            bits.append("rated only")
        if self.since:
            bits.append(f"since {self.since}")
        if self.until:
            bits.append(f"until {self.until}")
        return " | ".join(bits)

    def to_dict(self):
        return {
            "player": self.player, "time_class": self.time_class,
            "color": self.color, "since": self.since, "until": self.until,
            "rated_only": self.rated_only, "min_games": self.min_games,
        }


def to_epoch(datestr):
    d = datetime.datetime.strptime(datestr, "%Y-%m-%d")
    return int(d.replace(tzinfo=datetime.timezone.utc).timestamp())


def _rows(cursor):
    return [dict(r) for r in cursor.fetchall()]


def _order_by(rows, key, order):
    rank = {name: i for i, name in enumerate(order)}
    return sorted(rows, key=lambda r: rank.get(r[key], len(order)))


# --------------------------------------------------------------------------- #

def summary(conn, f):
    where, params = f.where()
    totals = conn.execute(
        f"""SELECT COUNT(*) games,
                   SUM(result='win') wins, SUM(result='loss') losses,
                   SUM(result='draw') draws, {SCORE} score,
                   MIN(played_at) first_at, MAX(played_at) last_at,
                   AVG(my_acpl) acpl, AVG(my_accuracy) accuracy,
                   AVG(opp_acpl) opp_acpl,
                   SUM(analyzed_at IS NOT NULL) analyzed
            FROM games WHERE {where}""",
        params,
    ).fetchone()
    totals = dict(totals)

    blunders_per_game = conn.execute(
        f"""SELECT COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT g.uuid), 0) v
            FROM moves m JOIN games g ON g.uuid = m.game_uuid
            WHERE {f.where(alias='g', analyzed_only=True)[0]}
              AND m.is_player = 1 AND m.judgment = 'blunder'""",
        f.where(alias="g", analyzed_only=True)[1],
    ).fetchone()
    totals["blunders_per_game"] = blunders_per_game["v"] if blunders_per_game else None

    def group(col):
        return _rows(conn.execute(
            f"""SELECT {col} k, COUNT(*) games, SUM(result='win') wins,
                       SUM(result='loss') losses, SUM(result='draw') draws,
                       {SCORE} score, AVG(my_acpl) acpl, AVG(my_accuracy) accuracy
                FROM games WHERE {where} GROUP BY {col} ORDER BY games DESC""",
            params,
        ))

    endings = _rows(conn.execute(
        f"""SELECT result, COALESCE(termination,'unknown') termination, COUNT(*) games
            FROM games WHERE {where}
            GROUP BY result, termination ORDER BY result, games DESC""",
        params,
    ))
    per_result = {}
    for row in endings:
        per_result[row["result"]] = per_result.get(row["result"], 0) + row["games"]
    for row in endings:
        row["share"] = row["games"] * 100.0 / per_result[row["result"]]

    ratings = _rows(conn.execute(
        f"""SELECT time_class, MIN(my_rating) low, MAX(my_rating) high,
                   COUNT(*) games FROM games
            WHERE {where} AND rated = 1 AND my_rating IS NOT NULL
            GROUP BY time_class ORDER BY games DESC""",
        params,
    ))
    for row in ratings:
        latest = conn.execute(
            f"""SELECT my_rating FROM games WHERE {where} AND rated = 1
                AND time_class = ? AND my_rating IS NOT NULL
                ORDER BY played_at DESC LIMIT 1""",
            params + [row["time_class"]],
        ).fetchone()
        row["latest"] = latest["my_rating"] if latest else None

    return {
        "totals": totals,
        "by_color": group("color"),
        "by_time_class": group("time_class"),
        "endings": endings,
        "ratings": ratings,
    }


def rating_series(conn, f, bucket="day"):
    """Daily average rating per time class, for the trend line."""
    where, params = f.where()
    fmt = {"day": "%Y-%m-%d", "week": "%Y-%m-%d", "month": "%Y-%m"}[bucket]
    rows = _rows(conn.execute(
        f"""SELECT time_class, strftime('{fmt}', played_at, 'unixepoch') date,
                   AVG(my_rating) rating, COUNT(*) games
            FROM games WHERE {where} AND rated = 1 AND my_rating IS NOT NULL
            GROUP BY time_class, date ORDER BY date""",
        params,
    ))
    series = {}
    for row in rows:
        series.setdefault(row["time_class"], []).append(
            {"date": row["date"], "rating": row["rating"], "games": row["games"]}
        )
    return [{"name": k, "points": v} for k, v in
            sorted(series.items(), key=lambda kv: -len(kv[1]))]


def openings(conn, f, by="opening", limit=25, worst_first=True):
    where, params = f.where()
    col = "eco" if by == "eco" else "opening"
    order = "score ASC, games DESC" if worst_first else "games DESC"
    return _rows(conn.execute(
        f"""SELECT COALESCE({col},'(unknown)') opening, color, COUNT(*) games,
                   SUM(result='win') wins, SUM(result='loss') losses,
                   SUM(result='draw') draws, {SCORE} score, AVG(my_acpl) acpl
            FROM games WHERE {where}
            GROUP BY opening, color HAVING games >= ?
            ORDER BY {order} LIMIT ?""",
        params + [f.min_games, limit],
    ))


def move_quality(conn, f):
    where, params = f.where(alias="g", analyzed_only=True)
    base = f"FROM moves m JOIN games g ON g.uuid = m.game_uuid WHERE {where}"
    total = conn.execute(f"SELECT COUNT(DISTINCT g.uuid) n {base}", params
                         ).fetchone()["n"]

    by_who = []
    for label, flag in (("You", 1), ("Opponents", 0)):
        r = dict(conn.execute(
            f"""SELECT COUNT(*) moves, SUM(m.judgment='blunder') blunders,
                       SUM(m.judgment='mistake') mistakes,
                       SUM(m.judgment='inaccuracy') inaccuracies,
                       AVG(m.cp_loss) acpl, AVG(m.accuracy) accuracy
                {base} AND m.is_player = ?""",
            params + [flag],
        ).fetchone())
        r["who"] = label
        r["per_game"] = ((r["blunders"] or 0) + (r["mistakes"] or 0)) / total if total else None
        r["blunders_per_game"] = (r["blunders"] or 0) / total if total else None
        by_who.append(r)

    by_phase = _rows(conn.execute(
        f"""SELECT m.phase, COUNT(*) moves, SUM(m.judgment='blunder') blunders,
                   SUM(m.judgment='mistake') mistakes, AVG(m.cp_loss) acpl
            {base} AND m.is_player = 1
            GROUP BY m.phase ORDER BY CASE m.phase
                WHEN 'opening' THEN 1 WHEN 'middlegame' THEN 2 ELSE 3 END""",
        params,
    ))
    by_move = _rows(conn.execute(
        f"""SELECT {MOVE_BUCKET} bucket, COUNT(*) moves,
                   SUM(m.judgment='blunder') blunders,
                   SUM(m.judgment='mistake') mistakes, AVG(m.cp_loss) acpl
            {base} AND m.is_player = 1 GROUP BY bucket ORDER BY MIN(m.move_no)""",
        params,
    ))
    for rows in (by_phase, by_move):
        for row in rows:
            row["blunder_rate"] = (row["blunders"] * 100.0 / row["moves"]
                                   if row["moves"] else 0)
    return {"games": total, "by_who": by_who, "by_phase": by_phase,
            "by_move": by_move}


def clock(conn, f):
    where, params = f.where(alias="g", analyzed_only=True)
    rows = _rows(conn.execute(
        f"""SELECT {CLOCK_BUCKET} bucket, COUNT(*) moves,
                   SUM(m.judgment='blunder') blunders,
                   SUM(m.judgment='mistake') mistakes, AVG(m.cp_loss) acpl
            FROM moves m JOIN games g ON g.uuid = m.game_uuid
            WHERE {where} AND m.is_player = 1 AND m.clock_secs IS NOT NULL
              AND g.base_seconds > 0
            GROUP BY bucket""",
        params,
    ))
    for row in rows:
        row["blunder_rate"] = (row["blunders"] * 100.0 / row["moves"]
                               if row["moves"] else 0)
    return _order_by(rows, "bucket", CLOCK_ORDER)


def blunders(conn, f, limit=20, phase=None):
    where, params = f.where(alias="g", analyzed_only=True)
    extra = ""
    if phase:
        extra = " AND m.phase = ?"
        params = params + [phase]
    return _rows(conn.execute(
        f"""SELECT g.uuid, g.url, g.color, g.result, g.opening, g.played_at,
                   m.move_no, m.side, m.san, m.uci, m.best_san, m.best_uci,
                   m.winp_loss, m.cp_loss, m.phase, m.fen_before
            FROM moves m JOIN games g ON g.uuid = m.game_uuid
            WHERE {where} AND m.is_player = 1 AND m.judgment = 'blunder'{extra}
            ORDER BY m.winp_loss DESC LIMIT ?""",
        params + [limit],
    ))


def sessions(conn, f, gap_minutes=60):
    where, params = f.where()
    rows = _rows(conn.execute(
        f"""WITH g AS (
                SELECT uuid, played_at, result, my_acpl,
                       LAG(played_at) OVER (ORDER BY played_at) prev
                FROM games WHERE {where}
            ), flagged AS (
                SELECT *, CASE WHEN prev IS NULL OR played_at - prev > ?
                               THEN 1 ELSE 0 END AS new_session FROM g
            ), s AS (
                SELECT *, SUM(new_session) OVER (
                    ORDER BY played_at ROWS UNBOUNDED PRECEDING) sid FROM flagged
            ), idx AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY sid ORDER BY played_at) pos FROM s
            )
            SELECT CASE WHEN pos >= 6 THEN '6+' ELSE CAST(pos AS TEXT) END bucket,
                   COUNT(*) games, SUM(result='win') wins,
                   SUM(result='loss') losses, SUM(result='draw') draws,
                   {SCORE} score, AVG(my_acpl) acpl
            FROM idx GROUP BY bucket ORDER BY MIN(pos)""",
        params + [gap_minutes * 60],
    ))
    return rows


def opponents(conn, f):
    where, params = f.where()
    rows = _rows(conn.execute(
        f"""SELECT {OPP_BUCKET} bucket, COUNT(*) games, SUM(result='win') wins,
                   SUM(result='loss') losses, SUM(result='draw') draws,
                   {SCORE} score, AVG(my_acpl) acpl
            FROM games WHERE {where} AND my_rating IS NOT NULL
              AND opp_rating IS NOT NULL
            GROUP BY bucket""",
        params,
    ))
    return _order_by(rows, "bucket", OPP_ORDER)


def trend(conn, f, bucket="month"):
    where, params = f.where()
    fmt = {"month": "%Y-%m", "week": "%Y-W%W", "day": "%Y-%m-%d"}[bucket]
    return _rows(conn.execute(
        f"""SELECT strftime('{fmt}', played_at, 'unixepoch') bucket, COUNT(*) games,
                   SUM(result='win') wins, SUM(result='loss') losses,
                   SUM(result='draw') draws, {SCORE} score, AVG(my_acpl) acpl,
                   AVG(my_accuracy) accuracy, AVG(my_rating) rating
            FROM games WHERE {where} GROUP BY bucket ORDER BY bucket""",
        params,
    ))


def games(conn, f, limit=50, offset=0, result=None, analyzed=None):
    where, params = f.where()
    if result:
        where += " AND games.result = ?"
        params = params + [result]
    if analyzed is True:
        where += " AND games.analyzed_at IS NOT NULL"
    elif analyzed is False:
        where += " AND games.analyzed_at IS NULL"
    total = conn.execute(f"SELECT COUNT(*) n FROM games WHERE {where}",
                         params).fetchone()["n"]
    rows = _rows(conn.execute(
        f"""SELECT uuid, url, played_at, color, result, termination, opening, eco,
                   time_class, time_control, my_rating, opp_rating, my_acpl,
                   opp_acpl, my_accuracy, analyzed_at, ply_count,
                   white_username, black_username
            FROM games WHERE {where} ORDER BY played_at DESC LIMIT ? OFFSET ?""",
        params + [limit, offset],
    ))
    return {"total": total, "games": rows}


def game_detail(conn, uuid):
    game = conn.execute("SELECT * FROM games WHERE uuid = ?", (uuid,)).fetchone()
    if not game:
        return None
    game = dict(game)
    game.pop("pgn", None)
    moves = _rows(conn.execute(
        """SELECT ply, move_no, side, is_player, san, uci, fen_before, clock_secs,
                  eval_before, eval_after, mate_before, mate_after, best_san,
                  best_uci, cp_loss, winp_before, winp_after, winp_loss, accuracy,
                  judgment, special, material_swing, alt_winp_gap, phase
           FROM moves WHERE game_uuid = ? ORDER BY ply""", (uuid,)))
    return {"game": game, "moves": moves}


def game_pgn(conn, uuid):
    row = conn.execute("SELECT pgn FROM games WHERE uuid = ?", (uuid,)).fetchone()
    return row["pgn"] if row else None


def players(conn):
    return [dict(r) for r in conn.execute(
        """SELECT player, COUNT(*) games, MAX(played_at) last_at,
                  SUM(analyzed_at IS NULL) pending
           FROM games GROUP BY player ORDER BY games DESC""")]


def coverage(conn, player):
    """What the dashboard header needs: how much is fetched and analyzed."""
    row = conn.execute(
        """SELECT COUNT(*) games, SUM(analyzed_at IS NOT NULL) analyzed,
                  SUM(analyzed_at IS NULL) pending, MAX(played_at) last_played,
                  MAX(fetched_at) last_fetched
           FROM games WHERE player = ?""", (player,)).fetchone()
    return dict(row) if row else {}
