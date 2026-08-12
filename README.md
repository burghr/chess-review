# chess-review

Pulls your Chess.com games into a local SQLite database, runs Stockfish over
every position, and reports the patterns you can actually train: which openings
you score badly in, where in the game you blunder, whether errors track the
clock, and whether you fall apart in game five of a session.

Everything runs locally. The only network call is the public Chess.com API,
which needs no key.

There are two front ends over the same database: a CLI, and a web dashboard that
syncs and analyzes on a schedule so you can leave it running on a box somewhere.

## Setup

```bash
brew install stockfish
cd ~/projects/chess-review
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`./chess-review` is a wrapper that uses the venv, so there is nothing to
activate.

## The web dashboard

```bash
./chess-review serve --user YOURNAME        # http://127.0.0.1:8765
```

Sync and analyze run as background jobs from the header buttons, and a scheduler
fires a sync every hour on its own. Everything the CLI reports is on one of five
tabs: Overview, Mistakes, Openings, Blunders, Games. The Blunders tab draws each
position on a board with your move highlighted in yellow and the engine's in
green, and clicking any game opens a win-probability graph you can scrub to walk
the position move by move.

One worker runs one job at a time. Stockfish already saturates the cores it is
given and SQLite takes one writer, so a queue with a single consumer is the
honest model rather than a pool that fights itself. Cancelling stops after the
current game, and job history lives in the database, so the dashboard still shows
what happened overnight after a restart.

### Docker

```bash
cp .env.example .env      # set CHESS_REVIEW_USER in it
docker compose up -d --build
```

Then open `http://<host>:8765`. Use the `.env` file rather than exporting
variables by hand: `CHESS_REVIEW_USER=you` on its own shell line sets a shell
variable that is never exported, so Compose substitutes an empty string and the
container comes up with no username. Stockfish comes from the Debian package inside
the image; the database lives on the `chess-data` volume.

| Variable | Default | What it does |
| --- | --- | --- |
| `CHESS_REVIEW_USER` | *(empty)* | Chess.com username. Can also be set in the UI. |
| `CHESS_REVIEW_INTERVAL` | `60` | Minutes between automatic syncs. |
| `CHESS_REVIEW_AUTO_SYNC` | `1` | Set `0` to only sync when you press the button. |
| `CHESS_REVIEW_DEPTH` | `14` | Stockfish depth per position. |
| `CHESS_REVIEW_THREADS` | `2` | Engine threads. Leave headroom on a shared box. |
| `CHESS_REVIEW_HASH` | `256` | Engine hash, MB. |
| `CHESS_REVIEW_BATCH` | `200` | Games analyzed per scheduled run. |
| `CHESS_REVIEW_SYNC_ON_START` | `0` | Set `1` to also sync at container start. |
| `CHESS_REVIEW_DB` | `/data/chess.db` | Database path. |

The schedule clock starts from the last scheduled run recorded in the database,
not from process start, so a container that restarts often still honours the
interval instead of re-syncing on every boot. `CHESS_REVIEW_SYNC_ON_START` is the
explicit way to ask for a run at startup.

The first run is the slow one. Analysis is CPU-bound and a large back catalogue
takes hours; it drains `CHESS_REVIEW_BATCH` games per hourly run until it catches
up, after which each run only has a handful of new games to do.

## The CLI

```bash
./chess-review fetch --user YOURNAME        # download every archived game
./chess-review analyze                      # Stockfish over everything new
./chess-review report all                   # the trends
```

`fetch` is incremental. Finished months are marked complete and skipped on later
runs, so a daily `fetch && analyze` only touches new games. Re-fetching never
overwrites analysis you already have.

`analyze` saves after each game, so Ctrl-C loses at most one game's work. A
73-move game takes about 3 seconds at depth 14 on 4 threads. Budget roughly an
hour for 1,000 blitz games.

```bash
./chess-review analyze --limit 50 --depth 16 --threads 8
./chess-review analyze --game 3            # one game, by index from `games`
./chess-review analyze --upgrade --depth 18  # redo anything analyzed shallower
```

### Reports

`report all` runs the lot. Individually:

| Report | Question it answers |
| --- | --- |
| `summary` | Record, score, accuracy, how your games end |
| `openings` | Which openings you score worst in, worst first |
| `mistakes` | Blunder counts vs your opponents', by phase and move number |
| `clock` | Does your blunder rate climb as the clock drops |
| `blunders` | The specific worst moves, with the better move and a FEN |
| `sessions` | Do you get worse the longer you sit down for |
| `opponents` | How you score against stronger and weaker players |
| `trend` | Score, rating, and accuracy month by month |

Every report takes the same filters:

```bash
./chess-review report openings --time-class blitz --color black --min-games 5
./chess-review report mistakes --since 2026-01-01 --rated-only
./chess-review report blunders --phase opening --limit 30
./chess-review report trend --bucket week
```

### One game at a time

```bash
./chess-review games                 # numbered list, most recent first
./chess-review review 1              # move-by-move with ?? / ? / ?! marks
./chess-review report blunders --fens   # FENs to paste into a board
```

### Why was this move bad?

```bash
./chess-review explain 1 --worst      # your worst move in that game
./chess-review explain 1 --ply 15     # a specific half-move
./chess-review explain 1              # a review of the whole game
```

In the web dashboard the same thing is an **Explain this move** button under the
board and a **Review this game** button beside the eval graph.

The important part is what the model is and isn't asked to do. Language models
are weak chess players and will cheerfully invent a tactic that isn't on the
board, so this never asks one to evaluate a position. Stockfish does that, and
the model receives only verified facts: the position, the move played, the
engine's preferred move, the eval before and after, the line the engine wanted,
and the **refutation line** — what the opponent actually does to punish the move.

That last one is the whole feature. "You dropped 44% win probability" is not
something a beginner can act on. "After 8.Qxg7, Black plays 8...Qf6 attacking
your queen and forcing a trade you can't decline" is. Getting it costs one extra
engine analysis of one position, run on demand.

Explanations are cached in the database against a fingerprint of the engine facts
they were built from, so re-opening a move is instant and free, and re-analyzing
at a deeper depth invalidates them.

### Follow-up questions

Under any explanation there is a chat box. It continues from that explanation, so
the model already has the position, the engine's findings and what it just told
you. Each move and each whole-game review keeps its own thread, stored in the
database and reloaded when you come back.

The useful follow-up is "what if I'd played X instead?", and answering that needs
a real evaluation of X. So before your question reaches the model, any legal move
named in it is parsed out and run through Stockfish, and the verified result is
attached to the question. Ask *"what if I had played Qxd6 instead? or Be3?"* and
the model receives:

```
Qxd6: eval +4.0, material +3, line 8... Qf6 9. Qg3 Qg6 10. Be3 Qxg3 11. hxg3
Be3:  eval -1.9, material  0, line 8... Qf6 9. Qd2 Ne7 10. O-O-O Be5 11. f4
```

so it can tell you Qxd6 wins a piece while Be3 does not, from ground truth rather
than from guessing. Moves that were engine-checked are listed under the answer.

No tool-calling protocol is involved, which matters because small local models
are unreliable at tool use. If you ask about a move without naming it in notation,
the model is instructed to say it would need to check rather than invent a line.

### Brilliant and Great moves

Following [Chess.com's published criteria](https://support.chess.com/en/articles/8572705-how-are-moves-classified-what-is-a-blunder-or-brilliant-etc):
**brilliant** (`!!`) is a sound sacrifice you were not already winning without,
**great** (`!`) is the move that had to be found. Both require the move to be the
engine's own choice, so neither can land on a mistake.

Detecting them needs two things the analysis now asks for: the second-best move
(to know whether the alternative was just as good) and the material balance after
the forced continuation (to know whether material was genuinely given up). Asking
Stockfish for two lines instead of one costs well under double, because the
search shares work, and the continuation comes back for free in the same result.

The material window is four plies deliberately. Over a longer window a lost
position keeps shedding pieces and every king move starts to look like a
sacrifice.

Both classifications go to the model too: single moves carry `special` with the
material change and what the second-best move would have cost, and whole-game
reviews get a `your_best_moves` list. The prompts are told a special move is good
and must not be criticised.

**These need re-analysis** — the classification happens during `analyze`, so
existing games have nothing until you re-run:

```bash
./chess-review analyze --reanalyze
```

In Docker, either press **Re-analyze every game** in Settings, or:

```bash
docker compose exec chess-review python -m chessreview.cli analyze --reanalyze
```

### Editing the prompts

Each request is a system prompt plus a user message. The user message is a fixed
one-line preamble followed by the engine's findings as JSON:

```
system:  <the editable prompt>
user:    Here is the verified engine analysis. Explain it.

         { "position": {...}, "played": {...},
           "engine_preferred": {...},
           "what_happens_after_your_move": "8... Qf6 9. Qxf6 Nxf6 ..." }
```

Both system prompts are editable — one for a single move, one for a whole-game
review — in the dashboard under Settings → Prompts, or from the CLI:

```bash
./chess-review prompt show
./chess-review prompt set --kind move --file my-prompt.txt
./chess-review prompt reset --kind move
```

They live in the database, so the CLI and the dashboard always agree. Saving an
empty prompt resets it to the built-in default.

The **Show request** button next to Explain prints the exact system prompt and
user message for the move you're looking at, without calling the model, so you
can see what a prompt change did before spending anything on it.

The payload also carries derived, engine-checked facts about *what kind* of error
each move was: how much material it cost over the refutation line, whether the
piece you moved was captured on the very next move, which of your pieces were
left attacked and undefended, whether it gave check, and how many seconds you
spent on it. A `patterns_across_these_errors` block totals those up.

Without that, a whole-game review can only observe "these moves were not the
engine's choice" — true of every error by definition — and the advice comes out
circular ("next time, play the engine's move"). The default game prompt forbids
any advice that depends on having an engine, because you can't consult one while
playing.

The JSON payload itself is generated, not editable, and that is deliberate: it is
what keeps the model narrating verified analysis instead of playing chess. If you
strip the "the engine is ground truth" and "do not calculate your own variations"
rules out of the prompt, expect confident invented tactics — especially from a
smaller local model.

### Which model writes the explanation

Two backends, picked with `CHESS_REVIEW_LLM_PROVIDER`:

| Value | Backend |
| --- | --- |
| `anthropic` | The Claude API via the official SDK. Needs `ANTHROPIC_API_KEY`. |
| `local` | Any OpenAI-compatible chat endpoint: `mlx_lm.server`, llama.cpp's server, LM Studio, Ollama. |
| *(unset)* | Anthropic if `ANTHROPIC_API_KEY` is set, otherwise local if a base URL is. |

```bash
# Anthropic
export ANTHROPIC_API_KEY=sk-ant-...
./chess-review explain 1 --worst

# Local, e.g. MLX on Apple Silicon
mlx_lm.server --model mlx-community/Qwen3-8B-4bit --port 8080
export CHESS_REVIEW_LLM_PROVIDER=local
export CHESS_REVIEW_LLM_BASE_URL=http://127.0.0.1:8080/v1
./chess-review explain 1 --worst
```

If your server needs an API key, set `CHESS_REVIEW_LLM_API_KEY` (sent as both
`Authorization: Bearer` and `x-api-key`, since servers differ on which they read).

All of it is also settable in the dashboard's Settings dialog, which outranks the
environment. The saved key is never sent back to the browser — the config API
returns only `llm_api_key_set: true`.

**Getting a 404?** The base URL is the part *before* `/chat/completions`, and
most servers want `/v1` on the end. Settings has a **Test connection** button
that tries the likely variants, reports which one answers, and lists the models
it found. `401` or `403` there means the URL is right and the key is wrong.

Defaults to `claude-opus-5`; override with `CHESS_REVIEW_LLM_MODEL`. A move
explanation is roughly 2K input and 300 output tokens, so about two cents on
Opus and well under one on Sonnet or Haiku. A local model is free and private,
and blunter — but since it only has to put Stockfish's findings into words
rather than find them, a small local model does a serviceable job.

**MLX only runs on Apple Silicon.** If the container is on another machine, the
local server has to be reachable over the network: point `CHESS_REVIEW_LLM_BASE_URL`
at your Mac's LAN address rather than localhost, and start the server with
`--host 0.0.0.0`.

### Games from anywhere else

```bash
./chess-review import mygame.pgn --user YOURNAME --time-class otb
```

The username has to match the `White` or `Black` tag in the PGN. Imported games
land in the same tables and appear in the same reports.

## How a move gets judged

Each position is evaluated once. A move's cost is the eval of the position it
came from minus the eval of the position it led to, both from the mover's point
of view. That is half the engine work of scoring "my move" and "the best move"
separately, and gives the same number.

Raw centipawn loss is a bad judge on its own, because dropping 300 centipawns
when you are up a rook does not matter and dropping 150 from a level position
can lose the game. So the labels come from the drop in win probability instead:
20+ points is a blunder, 10 a mistake, 5 an inaccuracy. The win-probability
curve and the accuracy formula are Lichess's, so the numbers line up with a
Lichess analysis board rather than with Chess.com's Game Review, which uses its
own scale and will always read a few points different.

The known soft spot: in a position that is already completely winning, throwing
away a forced mate barely moves the win probability, so it gets labeled `good`.
The `cp_loss` column still records it if you want to query for that case
directly.

## The database

One SQLite file, `chess.db`, three tables worth knowing. `games` has a row per
game with result, opening, ratings, ACPL and accuracy. `moves` has a row per
half-move with the eval before and after, the engine's preferred move, the
centipawn and win-probability loss, the judgment, the phase, the clock reading,
and the FEN. `jobs` records every sync and analysis run. Query it directly when a
report does not cover what you want:

```sql
-- openings where you personally blunder inside the first 12 moves
SELECT g.opening, COUNT(*) blunders, COUNT(DISTINCT g.uuid) games
FROM moves m JOIN games g ON g.uuid = m.game_uuid
WHERE m.is_player = 1 AND m.judgment = 'blunder' AND m.move_no <= 12
GROUP BY g.opening ORDER BY blunders DESC;
```

## Gotchas

Chess.com sits behind Cloudflare and returns 403 to any request whose
User-Agent contains `python-requests`, including the default one. `fetch.py`
sends its own string. If you edit it, do not put the library name back in.

Variants are skipped by default. `fetch --include-variants` stores chess960 and
the rest, but the phase and opening logic assumes standard chess.

Reports that read the `moves` table only count analyzed games, so a fresh fetch
shows a full `summary` and an empty `mistakes` until `analyze` catches up.

The CLI and the web API share one implementation of every report, in `stats.py`.
`reports.py` only formats terminal tables and `web/app.py` only serves JSON, so
adding a question means adding it once.
