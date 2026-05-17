from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_path: Path
    openai_api_key: str
    openai_transcribe_model: str
    openai_transcribe_language: str | None
    download_dir: Path
    max_parallel_transcriptions: int
    transcript_prefix: str
    reply_to_voice: bool


def load_settings() -> Settings:
    language = os.getenv("OPENAI_TRANSCRIBE_LANGUAGE", "").strip() or None

    return Settings(
        telegram_api_id=int(_required("TELEGRAM_API_ID")),
        telegram_api_hash=_required("TELEGRAM_API_HASH"),
        telegram_session_path=Path(
            os.getenv("TELEGRAM_SESSION_PATH", "data/telegram_user.session")
        ),
        openai_api_key=_required("OPENAI_API_KEY"),
        openai_transcribe_model=os.getenv(
            "OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe"
        ),
        openai_transcribe_language=language,
        download_dir=Path(os.getenv("DOWNLOAD_DIR", "downloads")),
        max_parallel_transcriptions=max(
            1, int(os.getenv("MAX_PARALLEL_TRANSCRIPTIONS", "2"))
        ),
        transcript_prefix=os.getenv("TRANSCRIPT_PREFIX", "Расшифровка:").strip(),
        reply_to_voice=_bool("REPLY_TO_VOICE", True),
    )

