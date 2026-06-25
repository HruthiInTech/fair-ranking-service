"""
Run with: uvicorn main:app --reload --port 8000
"""
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import database as db
from ranking import compute_score
from schemas import TransactionRequest

app = FastAPI(title="Fair Ranking Transaction Service", version="1.0.0")

# Wide open for the assignment's demo frontend. Lock this down to your
# actual frontend origin in a real deployment.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

conn = db.get_connection()
db.init_db(conn)

# --- Simple in-memory rate limiter (per-process; use Redis for multi-instance) ---
RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60
_request_log: dict[str, deque] = defaultdict(deque)


def check_rate_limit(user_id: str) -> None:
    now = time.time()
    q = _request_log[user_id]
    while q and now - q[0] > RATE_LIMIT_WINDOW_SECONDS:
        q.popleft()
    if len(q) >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_MAX_REQUESTS} "
            f"transactions per {RATE_LIMIT_WINDOW_SECONDS}s for this user.",
        )
    q.append(now)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _score_for_stats(stats: dict) -> dict:
    now = time.time()
    days_since_first_seen = 0
    if stats.get("first_seen_at"):
        days_since_first_seen = int((now - stats["first_seen_at"]) // 86400)
    breakdown = compute_score(
        total_amount=stats["total_amount"],
        active_days=stats["active_days"],
        days_since_first_seen=days_since_first_seen,
        anomaly_score=stats["anomaly_score"],
    )
    return breakdown.__dict__


@app.post("/transaction", status_code=201)
def post_transaction(req: TransactionRequest):
    check_rate_limit(req.user_id)

    result = db.create_transaction(
        conn, req.user_id, req.amount, req.category, req.idempotency_key
    )
    tx = result["transaction"]
    stats = db.get_user_stats(conn, req.user_id)

    status_code = 200 if result["is_replay"] else 201
    body = {
        "transaction": {
            "id": tx["id"],
            "user_id": tx["user_id"],
            "amount": tx["amount"],
            "category": tx["category"],
            "idempotency_key": tx["idempotency_key"],
            "created_at": _iso(tx["created_at"]),
        },
        "is_duplicate_replay": result["is_replay"],
        "current_summary": {
            "total_amount": round(stats["total_amount"], 2),
            "transaction_count": stats["transaction_count"],
        },
    }
    return JSONResponse(content=body, status_code=status_code)


@app.get("/summary/{user_id}")
def get_summary(user_id: str):
    stats = db.get_user_stats(conn, user_id)
    if stats is None:
        raise HTTPException(status_code=404, detail=f"No transactions found for user '{user_id}'")

    breakdown = _score_for_stats(stats)
    all_stats = db.get_all_user_stats(conn)
    scored = sorted(
        ((s["user_id"], _score_for_stats(s)["final_score"]) for s in all_stats),
        key=lambda x: x[1],
        reverse=True,
    )
    rank = next((i + 1 for i, (uid, _) in enumerate(scored) if uid == user_id), None)

    return {
        "user_id": user_id,
        "total_amount": round(stats["total_amount"], 2),
        "transaction_count": stats["transaction_count"],
        "first_seen_at": _iso(stats["first_seen_at"]),
        "last_seen_at": _iso(stats["last_seen_at"]),
        "active_days": stats["active_days"],
        "rank": rank,
        "out_of": len(scored),
        "score_breakdown": breakdown,
        "trust_status": "flagged" if breakdown["trust_multiplier"] < 0.7 else "trusted",
    }


@app.get("/ranking")
def get_ranking(limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)):
    all_stats = db.get_all_user_stats(conn)
    scored = []
    for s in all_stats:
        breakdown = _score_for_stats(s)
        scored.append(
            {
                "user_id": s["user_id"],
                "total_amount": round(s["total_amount"], 2),
                "transaction_count": s["transaction_count"],
                "final_score": breakdown["final_score"],
                "trust_multiplier": breakdown["trust_multiplier"],
                "trust_status": "flagged" if breakdown["trust_multiplier"] < 0.7 else "trusted",
            }
        )
    scored.sort(key=lambda x: x["final_score"], reverse=True)
    for i, row in enumerate(scored):
        row["rank"] = i + 1

    page = scored[offset: offset + limit]
    return {"total_users": len(scored), "limit": limit, "offset": offset, "ranking": page}


@app.get("/")
def root():
    return {
        "service": "Fair Ranking Transaction Service",
        "endpoints": ["POST /transaction", "GET /summary/{user_id}", "GET /ranking"],
    }
