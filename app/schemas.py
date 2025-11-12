from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


PositionState = Literal["long", "short", "flat"]


class Signal(BaseModel):
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    position: PositionState = Field()
    bar_index: Optional[int] = None
    entry_time: Optional[datetime] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    limit_price: Optional[float] = None
    reserve_capital: float = Field(default=0)
    capital_leverage_percent: float = Field(default=100)
    instrument: Instrument = Field()
    
    @field_validator('entry_time', mode='after')
    @classmethod
    def parse_entry_time(cls, v: datetime | None) -> datetime | None:
        """Ensure entry_time has timezone info"""
        if isinstance(v, datetime):
            # Assume local timezone if no timezone info
            if not v.tzinfo:
                return v.replace(tzinfo=datetime.now().astimezone().tzinfo)
        
        return v

class Instrument(BaseModel):
    ticker: str
    class_code: str

    def __str__(self) -> str:
        return f"{self.ticker}@{self.class_code}"

    @classmethod
    def from_string(cls, value: str) -> "Instrument":
        parts = value.split("@", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError("Invalid instrument format, expected 'ticker@class'")
        return cls(ticker=parts[0], class_code=parts[1])

    @model_validator(mode="before")
    @classmethod
    def parse_from_str(cls, value):
        if isinstance(value, str):
            # Convert string input to a dict acceptable by the model
            obj = cls.from_string(value)
            return {"ticker": obj.ticker, "class_code": obj.class_code}
        return value
