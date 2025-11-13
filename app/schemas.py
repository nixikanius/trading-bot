from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Signal(BaseModel):
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    position: Literal["long", "short", "flat"] = Field()
    bar_index: Optional[int] = None
    entry_time: Optional[datetime] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    limit_price: Optional[float] = None
    reserve_capital: float = Field(default=0)
    capital_leverage_percent: float = Field(default=100)
    instrument: str = Field()
    
    @field_validator('entry_time', mode='after')
    @classmethod
    def parse_entry_time(cls, v: datetime | None) -> datetime | None:
        """Ensure entry_time has timezone info"""
        if isinstance(v, datetime):
            # Assume local timezone if no timezone info
            if not v.tzinfo:
                return v.replace(tzinfo=datetime.now().astimezone().tzinfo)
        
        return v
