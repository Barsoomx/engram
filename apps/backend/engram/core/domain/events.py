from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DomainEvent(BaseModel): ...


class ClientLogEvent(DomainEvent):
    client_id: int
    event: str
    referrer_type: str | None = None
    referrer_id: str | int | UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ClientRegisteredEvent(DomainEvent):
    client_id: int
    brand_id: int | None = None


class TransactionStatusChangedEvent(DomainEvent):
    new_status: int
    prev_status: int | str
    transaction_token: UUID
    status_token: str
    confirm_amount: Decimal
    account_from: str
    account_to: str
    updated_at: datetime


class IdentityFinishedEvent(DomainEvent):
    identity_pk: int


class IdentityStartedEvent(DomainEvent):
    identity_pk: int
    brand_id: int | None = None


class CallCenterStageCompletedEvent(DomainEvent):
    call_result_id: int


class CardLimitChangedEvent(DomainEvent):
    card_id: int
