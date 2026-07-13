from __future__ import annotations

from datetime import datetime


def require_aware(value: datetime, *, field: str = 'as_of') -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f'{field} must be timezone-aware')

    return
