from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class AccountConfig:
    api_token: str
    account_id: str
    sandbox_mode: bool


@dataclass(frozen=True)
class ServerConfig:
    log_level: str

@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig
    telegram: TelegramConfig
    accounts: dict[str, AccountConfig]


def load_config(path: str | Path) -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Load server configuration with defaults
    server_data = data.get("server", {})
    server_config = ServerConfig(
        log_level=server_data.get("log_level", "INFO")
    )
    
    # Load telegram configuration
    telegram_data = data.get("telegram", {})
    telegram_config = TelegramConfig(
        bot_token=telegram_data.get("bot_token", ""),
        chat_id=telegram_data.get("chat_id", "")
    )

    # Load accounts configuration
    accounts: dict[str, AccountConfig] = {}
    raw_accounts = data.get("accounts", {}) or {}
    if not isinstance(raw_accounts, dict):
        raise ValueError("Invalid configuration format")

    for name, s in raw_accounts.items():
        accounts[name] = AccountConfig(
            api_token=s["api_token"],
            account_id=s["account_id"],
            sandbox_mode=bool(s.get("sandbox_mode", False)),
        )

    return AppConfig(server=server_config, telegram=telegram_config, accounts=accounts)
