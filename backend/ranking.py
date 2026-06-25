"""
Scoring model.

final_score = volume_score * consistency_weight * trust_multiplier

- volume_score:      log1p(total_amount)   -> rewards size, but with
                      diminishing returns so one giant transaction can't
                      permanently dominate the board.

- consistency_weight: 0.6 + 0.4 * (active_days / days_since_first_seen)
                      -> rewards users who contribute across many distinct
                      days rather than dumping everything in one sitting.
                      New users (day 0) get full weight so they aren't
                      punished for being new.

- trust_multiplier:   1 / (1 + anomaly_score)
                      -> anomaly_score rises when a user fires several
                      identical-amount transactions in a short window
                      (the classic "spam the leaderboard" pattern) and
                      decays over time (0.9x per new transaction) so a
                      single past burst doesn't permanently blacklist
                      someone. This is the abuse/manipulation defense.
"""
import math
from dataclasses import dataclass

BURST_WINDOW_SECONDS = 60
BURST_AMOUNT_TOLERANCE = 0.01  # amounts within 1 cent count as "identical"
ANOMALY_PENALTY_PER_BURST_HIT = 0.5
ANOMALY_DECAY = 0.9


@dataclass
class ScoreBreakdown:
    volume_score: float
    consistency_weight: float
    trust_multiplier: float
    final_score: float
    anomaly_score: float


def compute_burst_penalty(new_amount: float, new_ts: float, recent: list[tuple[float, float]]) -> float:
    """
    recent: list of (amount, unix_timestamp) for the user's last few
    transactions, most recent first. Returns a penalty contribution for
    *this* transaction based on how many recent identical-amount
    transactions landed inside the burst window.
    """
    hits = 0
    for amount, ts in recent:
        if new_ts - ts > BURST_WINDOW_SECONDS:
            break
        if abs(amount - new_amount) <= BURST_AMOUNT_TOLERANCE:
            hits += 1
    return hits * ANOMALY_PENALTY_PER_BURST_HIT


def update_anomaly_score(old_anomaly_score: float, burst_penalty: float) -> float:
    """Exponential decay + fresh penalty. Bad behavior fades but recent
    bursts hurt immediately."""
    return old_anomaly_score * ANOMALY_DECAY + burst_penalty


def compute_score(
    total_amount: float,
    active_days: int,
    days_since_first_seen: int,
    anomaly_score: float,
) -> ScoreBreakdown:
    volume_score = math.log1p(max(total_amount, 0))

    if days_since_first_seen <= 0:
        consistency_ratio = 1.0
    else:
        consistency_ratio = min(active_days / days_since_first_seen, 1.0)
    consistency_weight = 0.6 + 0.4 * consistency_ratio

    trust_multiplier = 1.0 / (1.0 + max(anomaly_score, 0))

    final_score = volume_score * consistency_weight * trust_multiplier

    return ScoreBreakdown(
        volume_score=round(volume_score, 4),
        consistency_weight=round(consistency_weight, 4),
        trust_multiplier=round(trust_multiplier, 4),
        final_score=round(final_score, 4),
        anomaly_score=round(anomaly_score, 4),
    )
