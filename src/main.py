from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.custom.message import Message

from .config import Settings, load_settings
from .transcriber import VoiceTranscriber


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram_voice_transcriber")


class TelegramVoiceTranscriberApp:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._settings.download_dir.mkdir(parents=True, exist_ok=True)
        self._settings.telegram_session_path.parent.mkdir(parents=True, exist_ok=True)

        self._client = TelegramClient(
            str(self._settings.telegram_session_path),
            self._settings.telegram_api_id,
            self._settings.telegram_api_hash,
        )
        self._transcriber = VoiceTranscriber(
            api_key=self._settings.openai_api_key,
            model=self._settings.openai_transcribe_model,
            language=self._settings.openai_transcribe_language,
        )
        self._semaphore = asyncio.Semaphore(
            self._settings.max_parallel_transcriptions
        )
        self._seen: set[tuple[int, int]] = set()

    async def run(self) -> None:
        self._client.add_event_handler(
            self._on_new_message,
            events.NewMessage(),
        )

        await self._client.start()
        me = await self._client.get_me()
        logger.info("Started as %s", getattr(me, "username", None) or me.id)
        logger.info("Monitoring incoming and outgoing voice messages in all chats")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

        await stop_event.wait()
        await self._client.disconnect()

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        message = event.message
        if not self._is_voice(message):
            return

        peer_id = event.chat_id or 0
        key = (peer_id, message.id)
        if key in self._seen:
            return
        self._seen.add(key)

        asyncio.create_task(self._process_voice(event, key))

    async def _process_voice(
        self,
        event: events.NewMessage.Event,
        key: tuple[int, int],
    ) -> None:
        async with self._semaphore:
            audio_path: Path | None = None
            try:
                audio_path = await self._download_voice(event.message, key)
                logger.info("Transcribing voice message chat=%s id=%s", key[0], key[1])
                text = await self._transcriber.transcribe(audio_path)
                await self._send_transcript(event, text)
                logger.info("Transcript sent chat=%s id=%s", key[0], key[1])
            except FloodWaitError as exc:
                logger.warning("Telegram flood wait: sleeping %s seconds", exc.seconds)
                await asyncio.sleep(exc.seconds)
            except Exception:
                logger.exception("Failed to process voice message chat=%s id=%s", *key)
            finally:
                if audio_path and audio_path.exists():
                    audio_path.unlink(missing_ok=True)

    async def _download_voice(self, message: Message, key: tuple[int, int]) -> Path:
        target = self._settings.download_dir / f"voice_{key[0]}_{key[1]}.ogg"
        downloaded = await message.download_media(file=str(target))
        if not downloaded:
            raise RuntimeError("Telegram did not return a downloaded file path.")
        return Path(downloaded)

    async def _send_transcript(
        self,
        event: events.NewMessage.Event,
        text: str,
    ) -> None:
        prefix = self._settings.transcript_prefix
        message = f"{prefix}\n{text}" if prefix else text

        reply_to: int | None = event.message.id if self._settings.reply_to_voice else None
        await self._client.send_message(
            entity=event.chat_id,
            message=message,
            reply_to=reply_to,
        )

    @staticmethod
    def _is_voice(message: Message | Any) -> bool:
        return bool(getattr(message, "voice", None))


async def main() -> None:
    settings = load_settings()
    app = TelegramVoiceTranscriberApp(settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
