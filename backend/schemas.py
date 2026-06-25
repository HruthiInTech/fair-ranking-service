"""
Request/response models. All incoming data is validated here before it
ever touches the database layer.
"""
import re
from uuid import UUID
from pydantic import BaseModel, Field, field_validator

USER_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
ALLOWED_CATEGORIES = {"general", "trade", "deposit", "reward", "adjustment"}
MAX_AMOUNT = 1_000_000.0


class TransactionRequest(BaseModel):
    user_id: str = Field(..., description="Stable identifier for the user")
    amount: float = Field(..., description="Positive transaction amount")
    category: str = Field(default="general")
    idempotency_key: str = Field(
        ...,
        description="Client-generated UUID4. Re-sending the same key returns "
        "the original result instead of double-applying the transaction.",
    )

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, v: str) -> str:
        if not USER_ID_PATTERN.match(v):
            raise ValueError(
                "user_id must be 1-64 chars of letters, digits, '_' or '-'"
            )
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):  # NaN / inf guard
            raise ValueError("amount must be a finite number")
        if v <= 0:
            raise ValueError("amount must be strictly positive")
        if v > MAX_AMOUNT:
            raise ValueError(f"amount exceeds maximum allowed ({MAX_AMOUNT})")
        return round(v, 2)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ALLOWED_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(ALLOWED_CATEGORIES)}")
        return v

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, v: str) -> str:
        try:
            UUID(v, version=4)
        except ValueError:
            raise ValueError("idempotency_key must be a valid UUID4 string")
        return v
