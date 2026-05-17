from __future__ import annotations

import asyncio
from pathlib import Path

from faster_whisper import WhisperModel


class VoiceTranscriber:
    def __init__(
        self,
        model: str,
        language: str | None = None,
        device: str = "auto",
        compute_type: str = "auto",
        beam_size: int = 5,
    ) -> None:
        self._language = language
        self._beam_size = beam_size
        self._model = WhisperModel(
            model,
            device=device,
            compute_type=compute_type,
        )

    async def transcribe(self, audio_path: Path) -> str:
        return await asyncio.to_thread(self._transcribe_sync, audio_path)

    def _transcribe_sync(self, audio_path: Path) -> str:
        kwargs: dict[str, object] = {
            "beam_size": self._beam_size,
            "vad_filter": True,
        }
        if self._language:
            kwargs["language"] = self._language

        segments, _ = self._model.transcribe(str(audio_path), **kwargs)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        if not text:
            raise RuntimeError("Transcription completed, but returned empty text.")
        return text
