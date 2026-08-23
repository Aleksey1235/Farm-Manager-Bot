from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _int(name: str, default: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    panel_channel_id: int
    admin_channel_id: int
    report_channel_id: int
    content_proof_channel_id: int
    payment_proof_channel_id: int
    farm_role_id: int
    leadership_role_ids: frozenset[int]
    timezone_name: str
    db_path: Path

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


def load_config() -> Config:
    load_dotenv()
    cfg = Config(
        token=os.getenv("DISCORD_TOKEN", "").strip(),
        guild_id=_int("GUILD_ID"),
        panel_channel_id=_int("PANEL_CHANNEL_ID"),
        admin_channel_id=_int("ADMIN_CHANNEL_ID"),
        report_channel_id=_int("REPORT_CHANNEL_ID"),
        content_proof_channel_id=_int("CONTENT_PROOF_CHANNEL_ID"),
        payment_proof_channel_id=_int("PAYMENT_PROOF_CHANNEL_ID"),
        farm_role_id=_int("FARM_ROLE_ID"),
        leadership_role_ids=_ids("LEADERSHIP_ROLE_IDS"),
        timezone_name=os.getenv("TIMEZONE", "Europe/Moscow").strip(),
        db_path=Path(os.getenv("DB_PATH", "data/farmbot.sqlite3")),
    )
    missing = [
        name for name, value in {
            "DISCORD_TOKEN": cfg.token,
            "GUILD_ID": cfg.guild_id,
            "PANEL_CHANNEL_ID": cfg.panel_channel_id,
            "ADMIN_CHANNEL_ID": cfg.admin_channel_id,
            "REPORT_CHANNEL_ID": cfg.report_channel_id,
            "CONTENT_PROOF_CHANNEL_ID": cfg.content_proof_channel_id,
            "PAYMENT_PROOF_CHANNEL_ID": cfg.payment_proof_channel_id,
            "FARM_ROLE_ID": cfg.farm_role_id,
            "LEADERSHIP_ROLE_IDS": cfg.leadership_role_ids,
        }.items() if not value
    ]
    if missing:
        raise RuntimeError("Не заполнены переменные .env: " + ", ".join(missing))
    _ = cfg.timezone
    return cfg
