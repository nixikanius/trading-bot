from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


class BrokerConfig(BaseModel):
    name: str
    config: dict[str, Any]

class AccountConfig(BaseModel):
    broker: BrokerConfig

class ServerConfig(BaseModel):
    log_level: str = "INFO"

class TelegramConfig(BaseModel):
    bot_token: str
    chat_id: int

class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=lambda: ServerConfig())
    telegram: TelegramConfig = Field(default_factory=lambda: TelegramConfig())
    accounts: dict[str, AccountConfig]
    
    @model_validator(mode='before')
    @classmethod
    def validate_accounts(cls, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data, dict) and "accounts" in data:
            if isinstance(data["accounts"], dict):
                accounts_data = {}
                for name, account_data in data["accounts"].items():
                    try:
                        accounts_data[name] = AccountConfig(**account_data)
                    except ValidationError as e:
                        raise ValueError(f"Account '{name}' configuration error: {e}") from e
                
                data["accounts"] = accounts_data
        
        return data


def load_config(path: str | Path) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    return AppConfig(**data)
