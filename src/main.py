from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, load_settings
from .transcriber import VoiceTranscriber


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram_business_voice_transcriber")
logging.getLogger("httpx").setLevel(logging.WARNING)


class TelegramApiError(RuntimeError):
    def __init__(self, description: str, retry_after: int | None = None) -> None:
        super().__init__(description)
        self.retry_after = retry_after


class TelegramBusinessBotClient:
    def __init__(self, token: str) -> None:
        self._api_url = f"https://api.telegram.org/bot{token}"
        self._file_url = f"https://api.telegram.org/file/bot{token}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(90.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict[str, Any]:
        return await self._request("getMe")

    async def delete_webhook(self) -> None:
        await self._request("deleteWebhook", {"drop_pending_updates": False})

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["business_connection", "business_message"],
        }
        if offset is not None:
            payload["offset"] = offset

        return await self._request("getUpdates", payload)

    async def download_file(self, file_id: str, target: Path) -> Path:
        file_info = await self._request("getFile", {"file_id": file_id})
        file_path = file_info.get("file_path")
        if not file_path:
            raise RuntimeError("Telegram did not return file_path for voice message.")

        response = await self._client.get(f"{self._file_url}/{file_path}")
        response.raise_for_status()
        target.write_bytes(response.content)
        return target

    async def send_business_message(
        self,
        *,
        business_connection_id: str,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None,
    ) -> None:
        payload: dict[str, Any] = {
            "business_connection_id": business_connection_id,
            "chat_id": chat_id,
            "text": text,
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }

        await self._request("sendMessage", payload)

    async def _request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        response = await self._client.post(f"{self._api_url}/{method}", json=payload or {})
        data = response.json()
        if data.get("ok"):
            return data.get("result")

        parameters = data.get("parameters") or {}
        raise TelegramApiError(
            data.get("description", f"Telegram API method {method} failed."),
            retry_after=parameters.get("retry_after"),
        )


class TelegramBusinessVoiceTranscriberApp:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._settings.download_dir.mkdir(parents=True, exist_ok=True)

        self._telegram = TelegramBusinessBotClient(self._settings.telegram_bot_token)
        self._transcriber = VoiceTranscriber(
            model=self._settings.whisper_model,
            language=self._settings.whisper_language,
            device=self._settings.whisper_device,
            compute_type=self._settings.whisper_compute_type,
            beam_size=self._settings.whisper_beam_size,
        )
        self._semaphore = asyncio.Semaphore(
            self._settings.max_parallel_transcriptions
        )
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        await self._telegram.delete_webhook()
        bot = await self._telegram.get_me()
        logger.info("Started as @%s", bot.get("username") or bot.get("id"))
        logger.info("Monitoring Telegram Business voice messages in private chats")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

        poller = asyncio.create_task(self._poll(stop_event))
        await stop_event.wait()
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._telegram.close()

    async def _poll(self, stop_event: asyncio.Event) -> None:
        offset: int | None = None
        while not stop_event.is_set():
            try:
                updates = await self._telegram.get_updates(
                    offset,
                    self._settings.polling_timeout,
                )
            except TelegramApiError as exc:
                if exc.retry_after:
                    logger.warning("Telegram flood wait: sleeping %s seconds", exc.retry_after)
                    await asyncio.sleep(exc.retry_after)
                else:
                    logger.exception("Telegram polling failed")
                    await asyncio.sleep(5)
                continue
            except httpx.HTTPError:
                logger.exception("Telegram network request failed")
                await asyncio.sleep(5)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("business_message")
                if not message or not self._is_voice(message):
                    continue

                task = asyncio.create_task(self._process_voice(message))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _process_voice(self, message: dict[str, Any]) -> None:
        async with self._semaphore:
            audio_path: Path | None = None
            business_connection_id = message["business_connection_id"]
            chat_id = message["chat"]["id"]
            message_id = message["message_id"]

            try:
                audio_path = await self._download_voice(message)
                logger.info("Transcribing voice message chat=%s id=%s", chat_id, message_id)
                text = await self._transcriber.transcribe(audio_path)
                await self._send_transcript(
                    business_connection_id=business_connection_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                )
                logger.info("Transcript sent chat=%s id=%s", chat_id, message_id)
            except TelegramApiError as exc:
                if exc.retry_after:
                    logger.warning("Telegram flood wait: sleeping %s seconds", exc.retry_after)
                    await asyncio.sleep(exc.retry_after)
                else:
                    logger.exception("Telegram API failed for chat=%s id=%s", chat_id, message_id)
            except Exception:
                logger.exception("Failed to process voice message chat=%s id=%s", chat_id, message_id)
            finally:
                if audio_path and audio_path.exists():
                    audio_path.unlink(missing_ok=True)

    async def _download_voice(self, message: dict[str, Any]) -> Path:
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        voice = message["voice"]
        target = self._settings.download_dir / f"voice_{chat_id}_{message_id}.ogg"
        return await self._telegram.download_file(voice["file_id"], target)

    async def _send_transcript(
        self,
        *,
        business_connection_id: str,
        chat_id: int,
        message_id: int,
        text: str,
    ) -> None:
        prefix = self._settings.transcript_prefix
        message = f"{prefix}\n{text}" if prefix else text
        reply_to = message_id if self._settings.reply_to_voice else None

        await self._telegram.send_business_message(
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            text=message,
            reply_to_message_id=reply_to,
        )

    @staticmethod
    def _is_voice(message: dict[str, Any]) -> bool:
        return bool(message.get("voice"))


async def main() -> None:
    settings = load_settings()
    app = TelegramBusinessVoiceTranscriberApp(settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
