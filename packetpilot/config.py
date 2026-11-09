"""Runtime configuration, read from environment variables.

Everything has a safe local default so the project runs with zero setup:
SQLite database, local file storage, in-process background processing and
the deterministic (no-API-key) extractor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/packetpilot.db")
    )
    storage_backend: str = field(default_factory=lambda: os.getenv("PACKETPILOT_STORAGE", "db"))  # db | fs
    storage_dir: Path = field(
        default_factory=lambda: Path(os.getenv("PACKETPILOT_STORAGE_DIR", "./data/uploads"))
    )
    redis_url: str | None = field(default_factory=lambda: os.getenv("REDIS_URL") or None)
    queue_name: str = field(default_factory=lambda: os.getenv("PACKETPILOT_QUEUE", "packetpilot"))
    # "sync" runs processing inside the request (tests / CLI); "background" uses
    # FastAPI background tasks when no Redis is configured.
    process_mode: str = field(default_factory=lambda: os.getenv("PACKETPILOT_PROCESS_MODE", "background"))

    # Extraction: "rules", "llm", "hybrid" or "auto" (hybrid if an API key exists, else rules)
    extractor: str = field(default_factory=lambda: os.getenv("PACKETPILOT_EXTRACTOR", "auto"))
    llm_provider: str = field(default_factory=lambda: os.getenv("PACKETPILOT_LLM_PROVIDER", "auto"))
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)
    openai_model: str = field(default_factory=lambda: os.getenv("PACKETPILOT_OPENAI_MODEL", "gpt-4.1-mini"))
    gemini_api_key: str | None = field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None
    )
    gemini_model: str = field(default_factory=lambda: os.getenv("PACKETPILOT_GEMINI_MODEL", "gemini-2.5-flash"))

    # Trust policy
    auto_verify_threshold: float = field(
        default_factory=lambda: float(os.getenv("PACKETPILOT_AUTO_VERIFY_THRESHOLD", "0.90"))
    )
    low_confidence_threshold: float = field(
        default_factory=lambda: float(os.getenv("PACKETPILOT_LOW_CONFIDENCE_THRESHOLD", "0.75"))
    )
    money_tolerance: float = field(default_factory=lambda: float(os.getenv("PACKETPILOT_MONEY_TOLERANCE", "0.02")))
    loss_run_max_age_days: int = field(default_factory=lambda: int(os.getenv("PACKETPILOT_LOSS_RUN_MAX_AGE_DAYS", "90")))
    sla_hours: float = field(default_factory=lambda: float(os.getenv("PACKETPILOT_SLA_HOURS", "24")))

    ocr_enabled: bool = field(default_factory=lambda: _bool("PACKETPILOT_OCR", True))
    api_key: str | None = field(default_factory=lambda: os.getenv("PACKETPILOT_API_KEY") or None)


def get_settings() -> Settings:
    return Settings()
