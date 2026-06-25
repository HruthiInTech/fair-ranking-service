# Fair Ranking Transaction Service

A backend service + live frontend demonstrating idempotent transaction
handling, concurrency-safe data updates, and a manipulation-resistant
multi-factor ranking system.

## What makes the ranking "fair"

Most naive leaderboards do `score = sum(amounts)`. That's trivially
gameable — fire enough fake transactions and you win. This service scores
on three independent factors so that **volume alone cannot win the
leaderboard**:

```
final_score = volume_score × consistency_weight × trust_multiplier
```

| Factor | Formula | What it rewards / punishes |
|---|---|---|
| `volume_score` | `log(1 + total_amount)` | Size, but with diminishing returns — one giant transaction can't permanently dominate. |
| `consistency_weight` | `0.6 + 0.4 × (active_days / days_since_first_seen)` | Steady contribution over time, not a single dump. New users get full weight so they aren't punished for being new. |
| `trust_multiplier` | `1 / (1 + anomaly_score)` | Penalizes burst patterns of identical-amount transactions (the classic leaderboard-spam signature). Decays 10% on every new transaction, so a single past burst doesn't permanently blacklist someone. |

**Example from testing:** a user who fired five identical $10 transactions
within a 60-second window ended up with `trust_multiplier ≈ 0.18` and a
final score of `0.71`, while a user who made one clean $50 transaction
(same total volume) scored `trust_multiplier = 1.0` and a final score of
`3.93` — over 5x higher for the same volume, purely because of the burst
pattern. That's the abuse-resistance requirement in action, not just a
claim.

## How duplicate requests are prevented (idempotency)

Every `POST /transaction` requires a client-generated `idempotency_key`
(a UUID4). The `transactions` table has a `UNIQUE` constraint on this
column. If a request retries with the same key (network blip, double
click, client retry logic), the insert collides, the service looks up the
original transaction instead of erroring, and returns it with
`is_duplicate_replay: true` — **the user's balance is never double-counted**,
and the client gets a safe, predictable response either way.

## How concurrent updates are kept consistent

- Every write (`POST /transaction`) runs inside `BEGIN IMMEDIATE ... COMMIT`,
  which makes SQLite take the write lock for the whole critical section
  (duplicate check → read recent transactions → update aggregate stats →
  insert row) as one atomic unit.
- On top of that, a process-level `threading.Lock` serializes the critical
  section, so two requests racing for the same user can't interleave a
  read-modify-write and corrupt `total_amount` or `transaction_count`.
- This means: if two requests for the same user land at the same moment,
  one fully completes (and is visible to `GET /summary` and `GET /ranking`)
  before the other starts.

**At higher scale:** swap SQLite for Postgres and replace the
`threading.Lock` with `SELECT ... FOR UPDATE` on the `user_stats` row — the
`BEGIN IMMEDIATE` pattern here maps directly to that. This is documented as
a known limitation, not hidden.

## API Reference

### `POST /transaction`
```json
{
  "user_id": "alice",
  "amount": 100.0,
  "category": "trade",
  "idempotency_key": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```
- `user_id`: 1–64 chars, letters/digits/`_`/`-` only.
- `amount`: must be `> 0` and `<= 1,000,000`. Rejects `NaN`/`Infinity`.
- `category`: one of `general | trade | deposit | reward | adjustment`.
- `idempotency_key`: required UUID4 string.
- Returns `201` on a new transaction, `200` if it was a duplicate replay.
- Returns `422` on validation failure, `429` if the user exceeds 20
  requests/60s (basic rate limiting — also an abuse-prevention layer).

### `GET /summary/{userId}`
Returns total amount, transaction count, active days, current rank out of
all users, and the full score breakdown (`volume_score`,
`consistency_weight`, `trust_multiplier`, `final_score`) plus a
`trust_status` of `trusted` or `flagged` (multiplier < 0.7). `404` if the
user has no transactions.

### `GET /ranking?limit=20&offset=0`
Returns the leaderboard sorted by `final_score` descending, paginated.
Each row includes `rank`, `user_id`, `total_amount`, `final_score`,
`trust_multiplier`, and `trust_status`.

## Data model (SQLite, `backend/app.db`)

```
transactions(id, user_id, amount, category, idempotency_key UNIQUE, created_at, is_replay)
user_stats(user_id PK, total_amount, transaction_count, first_seen_at,
           last_seen_at, last_active_date, active_days, anomaly_score, version)
```

`user_stats` is a denormalized rolling aggregate — it's updated
transactionally alongside every insert into `transactions`, so reads
(`/summary`, `/ranking`) never have to scan the full transaction log or
risk seeing a half-applied write. `version` is incremented on every update
as an audit/optimistic-concurrency marker.

No mock data is used — everything is real SQLite state, created fresh
(`app.db`) the first time the server runs.

## Running it

### Backend
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```
API will be live at `http://localhost:8000`. Visit `http://localhost:8000/docs`
for interactive Swagger docs (auto-generated by FastAPI).

### Frontend
`frontend/index.html` is a static file with no build step — open it
directly in a browser, or serve it. On load, type your backend's URL into
the **API BASE** field at the top right (defaults to
`http://localhost:8000`, persisted in the browser). It will:
- show a live connection status dot,
- let you submit transactions (with an auto-generated idempotency key you
  can re-send to prove duplicate-prevention works),
- look up any user's summary and score breakdown,
- show a live, auto-refreshing ranking table with a visual trust bar per user.

### Deploying live (for submission)
- **Backend**: Render, Railway, or Fly.io all support a one-click Python
  web service from this repo (`uvicorn main:app --host 0.0.0.0 --port $PORT`).
- **Frontend**: Vercel, Netlify, or GitHub Pages — it's a single static
  HTML file, drag-and-drop deploy works. Just make sure CORS on the
  backend allows the deployed frontend's origin (currently set to `*` for
  the assignment demo; restrict this in real production use).

## Known limitations / trade-offs (documented, not hidden)

- SQLite + in-process locks are appropriate for a single backend
  instance. Multi-instance deployment needs Postgres + row-level locking,
  as noted above.
- The rate limiter and idempotency replay cache are process-local — a
  restart clears the rate-limit window (not the database, which persists).
- Rank is computed by scoring all users on every request. Fine at the
  scale of an assignment/demo; a production system with millions of users
  would maintain a precomputed, periodically-refreshed leaderboard instead.
- Anomaly detection looks only at a user's own last 5 transactions for
  burst detection — simple and explainable, but a more advanced system
  would also compare against cross-user collusion patterns.
