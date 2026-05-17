from __future__ import annotations

from pathlib import Path

from openai import AsyncOpenAI


class VoiceTranscriber:
    def __init__(self, api_key: str, model: str, language: str | None = None) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model
        self._language = language

    async def transcribe(self, audio_path: Path) -> str:
        kwargs: dict[str, object] = {"model": self._model}
        if self._language:
            kwargs["language"] = self._language

        with audio_path.open("rb") as audio_file:
            transcript = await self._client.audio.transcriptions.create(
                file=audio_file,
                **kwargs,
            )

        text = getattr(transcript, "text", "").strip()
        if not text:
            raise RuntimeError("Transcription completed, but returned empty text.")
        return text

