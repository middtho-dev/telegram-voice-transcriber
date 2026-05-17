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
    telegram_bot_token: str
    whisper_model: str
    whisper_language: str | None
    whisper_device: str
    whisper_compute_type: str
    whisper_beam_size: int
    download_dir: Path
    max_parallel_transcriptions: int
    transcript_prefix: str
    reply_to_voice: bool
    polling_timeout: int


def load_settings() -> Settings:
    language = os.getenv("WHISPER_LANGUAGE", "").strip() or None

    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        whisper_model=os.getenv("WHISPER_MODEL", "large-v3-turbo"),
        whisper_language=language,
        whisper_device=os.getenv("WHISPER_DEVICE", "auto"),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "auto"),
        whisper_beam_size=max(1, int(os.getenv("WHISPER_BEAM_SIZE", "5"))),
        download_dir=Path(os.getenv("DOWNLOAD_DIR", "downloads")),
        max_parallel_transcriptions=max(
            1, int(os.getenv("MAX_PARALLEL_TRANSCRIPTIONS", "1"))
        ),
        transcript_prefix=os.getenv("TRANSCRIPT_PREFIX", "Расшифровка:").strip(),
        reply_to_voice=_bool("REPLY_TO_VOICE", True),
        polling_timeout=max(1, int(os.getenv("POLLING_TIMEOUT", "30"))),
    )
