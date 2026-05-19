from __future__ import annotations

import asyncio
import logging
import re
import signal
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import Settings, load_settings
from .storage import MessageStorage, extract_attachment_specs
from .transcriber import VoiceTranscriber


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram_business_voice_transcriber")
logging.getLogger("httpx").setLevel(logging.WARNING)

SETTING_STORAGE_ENABLED = "storage_enabled"
SETTING_TRANSCRIPTION_ENABLED = "transcription_enabled"
SETTING_REPLY_TO_VOICE = "reply_to_voice"


class TelegramApiError(RuntimeError):
    def __init__(self, description: str, retry_after: int | None = None) -> None:
        super().__init__(description)
        self.retry_after = retry_after


class TelegramBusinessBotClient:
    def __init__(self, token: str) -> None:
        self._api_url = f"https://api.telegram.org/bot{token}"
        self._file_url = f"https://api.telegram.org/file/bot{token}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def get_me(self) -> dict[str, Any]:
        return await self._request("getMe")

    async def delete_webhook(self) -> None:
        await self._request("deleteWebhook", {"drop_pending_updates": False})

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": [
                "message",
                "callback_query",
                "business_connection",
                "business_message",
                "edited_business_message",
            ],
        }
        if offset is not None:
            payload["offset"] = offset

        return await self._request("getUpdates", payload)

    async def get_file_info(self, file_id: str) -> dict[str, Any]:
        return await self._request("getFile", {"file_id": file_id})

    async def download_file_by_path(self, file_path: str, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        response = await self._client.get(f"{self._file_url}/{file_path}")
        response.raise_for_status()
        target.write_bytes(response.content)
        return target

    async def download_file(self, file_id: str, target: Path) -> Path:
        file_info = await self.get_file_info(file_id)
        file_path = file_info.get("file_path")
        if not file_path:
            raise RuntimeError("Telegram did not return file_path.")
        return await self.download_file_by_path(file_path, target)

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._request("sendMessage", payload)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        await self._request("editMessageText", payload)

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        await self._request("answerCallbackQuery", payload)

    async def send_document(self, *, chat_id: int, path: Path, caption: str) -> None:
        data = {"chat_id": str(chat_id), "caption": caption}
        with path.open("rb") as file:
            response = await self._client.post(
                f"{self._api_url}/sendDocument",
                data=data,
                files={"document": (path.name, file, "application/zip")},
            )
        payload = response.json()
        if payload.get("ok"):
            return

        parameters = payload.get("parameters") or {}
        raise TelegramApiError(
            payload.get("description", "Telegram API method sendDocument failed."),
            retry_after=parameters.get("retry_after"),
        )

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
        self._settings.data_dir.mkdir(parents=True, exist_ok=True)

        self._telegram = TelegramBusinessBotClient(self._settings.telegram_bot_token)
        self._storage = MessageStorage(
            self._settings.database_path,
            self._settings.attachments_dir,
            self._settings.exports_dir,
        )
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
        logger.info("Monitoring Telegram Business messages in private chats")

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
                try:
                    await self._dispatch_update(update)
                except TelegramApiError as exc:
                    if exc.retry_after:
                        logger.warning(
                            "Telegram flood wait while handling update=%s: sleeping %s seconds",
                            update["update_id"],
                            exc.retry_after,
                        )
                        await asyncio.sleep(exc.retry_after)
                    else:
                        logger.exception("Telegram API failed while handling update=%s", update["update_id"])
                except Exception:
                    logger.exception("Failed to handle update=%s", update["update_id"])

    async def _dispatch_update(self, update: dict[str, Any]) -> None:
        if callback_query := update.get("callback_query"):
            await self._handle_callback_query(callback_query)
            return

        if message := update.get("message"):
            await self._handle_regular_message(message)
            return

        business_message = update.get("business_message") or update.get("edited_business_message")
        if business_message:
            task = asyncio.create_task(self._process_business_message(business_message))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _handle_regular_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return

        chat_id = int(chat["id"])
        sender_id = int((message.get("from") or {}).get("id", chat_id))
        text = (message.get("text") or "").strip()
        logger.info("Received private bot message chat=%s from=%s text=%s", chat_id, sender_id, text[:40])

        if not self._is_admin(sender_id):
            await self._telegram.send_message(
                chat_id=chat_id,
                text=(
                    "Панель доступна только администратору.\n"
                    f"Ваш Telegram ID: {sender_id}\n"
                    "Добавьте его в ADMIN_USER_IDS в .env и перезапустите контейнер."
                ),
            )
            return

        if text.startswith("/start") or text.startswith("/menu") or text == "":
            await self._send_menu(chat_id)
        elif text.startswith("/export"):
            await self._send_export(chat_id)
        else:
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Используйте /start, чтобы открыть меню управления.",
            )

    async def _handle_callback_query(self, callback_query: dict[str, Any]) -> None:
        sender = callback_query.get("from") or {}
        sender_id = int(sender.get("id", 0))
        callback_id = callback_query["id"]
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        message_id = int(message.get("message_id", 0))
        data = callback_query.get("data") or ""

        if not self._is_admin(sender_id):
            await self._telegram.answer_callback_query(callback_id, "Нет доступа")
            return

        if data == "menu:main":
            await self._edit_menu(chat_id, message_id)
        elif data == "menu:settings":
            await self._edit_settings(chat_id, message_id)
        elif data == "menu:stats":
            await self._telegram.answer_callback_query(callback_id)
            await self._edit_stats(chat_id, message_id)
        elif data == "menu:export":
            await self._telegram.answer_callback_query(callback_id)
            await self._edit_export_periods(chat_id, message_id)
        elif data.startswith("export:"):
            period_name, since_timestamp = self._export_period(data)
            await self._telegram.answer_callback_query(callback_id, "Готовлю архив")
            await self._send_export(chat_id, period_name, since_timestamp)
        elif data == "toggle:storage":
            self._toggle_bool(SETTING_STORAGE_ENABLED, True)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_settings(chat_id, message_id)
        elif data == "toggle:transcription":
            self._toggle_bool(SETTING_TRANSCRIPTION_ENABLED, True)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_settings(chat_id, message_id)
        elif data == "toggle:reply":
            self._toggle_bool(SETTING_REPLY_TO_VOICE, self._settings.reply_to_voice)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_settings(chat_id, message_id)
        else:
            await self._telegram.answer_callback_query(callback_id)

    async def _process_business_message(self, message: dict[str, Any]) -> None:
        async with self._semaphore:
            business_connection_id = message["business_connection_id"]
            chat_id = message["chat"]["id"]
            message_id = message["message_id"]
            attachments: list[dict[str, Any]] = []

            try:
                if self._storage_enabled():
                    attachments = await self._download_attachments(message)
                    await asyncio.to_thread(self._storage.save_message, message, attachments)
                    logger.info("Archived message chat=%s id=%s", chat_id, message_id)

                if not self._is_voice(message) or not self._transcription_enabled():
                    return

                audio_path = self._find_voice_path(attachments)
                should_delete_audio = False
                if audio_path is None:
                    audio_path = await self._download_voice(message)
                    should_delete_audio = True

                logger.info("Transcribing voice message chat=%s id=%s", chat_id, message_id)
                text = await self._transcriber.transcribe(audio_path)
                await self._send_transcript(
                    business_connection_id=business_connection_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                )
                logger.info("Transcript sent chat=%s id=%s", chat_id, message_id)

                if should_delete_audio and audio_path.exists():
                    audio_path.unlink(missing_ok=True)
            except TelegramApiError as exc:
                if exc.retry_after:
                    logger.warning("Telegram flood wait: sleeping %s seconds", exc.retry_after)
                    await asyncio.sleep(exc.retry_after)
                else:
                    logger.exception("Telegram API failed for chat=%s id=%s", chat_id, message_id)
            except Exception:
                logger.exception("Failed to process message chat=%s id=%s", chat_id, message_id)

    async def _download_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        saved: list[dict[str, Any]] = []
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        for index, spec in enumerate(extract_attachment_specs(message), start=1):
            file_info = await self._telegram.get_file_info(spec["file_id"])
            file_path = file_info.get("file_path")
            if not file_path:
                continue

            suffix = self._attachment_suffix(file_path, spec)
            target = (
                self._settings.attachments_dir
                / str(chat_id)
                / f"{message_id}_{index}_{spec['kind']}{suffix}"
            )
            await self._telegram.download_file_by_path(file_path, target)
            saved.append({**spec, "local_path": str(target)})
        return saved

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
        reply_to = message_id if self._reply_to_voice() else None

        await self._telegram.send_business_message(
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            text=message,
            reply_to_message_id=reply_to,
        )

    async def _send_menu(self, chat_id: int) -> None:
        await self._telegram.send_message(
            chat_id=chat_id,
            text=self._menu_text(),
            reply_markup=self._main_keyboard(),
        )

    async def _edit_menu(self, chat_id: int, message_id: int) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=self._menu_text(),
            reply_markup=self._main_keyboard(),
        )

    async def _edit_settings(self, chat_id: int, message_id: int) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=self._settings_text(),
            reply_markup=self._settings_keyboard(),
        )

    async def _edit_stats(self, chat_id: int, message_id: int) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=self._stats_text(),
            reply_markup=self._main_keyboard(),
        )

    async def _edit_export_periods(self, chat_id: int, message_id: int) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="Выберите период для выгрузки. В архиве будет HTML-страница с вкладками по чатам и встроенным просмотром вложений.",
            reply_markup=self._export_keyboard(),
        )

    async def _send_export(
        self,
        chat_id: int,
        period_name: str = "все время",
        since_timestamp: int | None = None,
    ) -> None:
        try:
            await self._telegram.send_message(
                chat_id=chat_id,
                text=f"Готовлю архив за период: {period_name}. Если сообщений и вложений много, это может занять немного времени.",
            )
            archive_path = await asyncio.to_thread(
                self._storage.create_export_zip,
                period_name=period_name,
                since_timestamp=since_timestamp,
            )
            await self._telegram.send_document(
                chat_id=chat_id,
                path=archive_path,
                caption="Архив сообщений: откройте index.html внутри ZIP для удобного просмотра.",
            )
        except Exception:
            logger.exception("Failed to create or send export")
            await self._telegram.send_message(
                chat_id=chat_id,
                text=(
                    "Не получилось отправить архив. Если он слишком большой, "
                    "заберите его прямо с VPS из папки storage/exports."
                ),
            )

    def _menu_text(self) -> str:
        stats = self._storage.stats()
        return (
            "Панель управления\n\n"
            f"Сообщений в базе: {stats['messages']}\n"
            f"Вложений: {stats['attachments']}\n"
            f"Чатов: {stats['chats']}\n\n"
            "Выберите действие."
        )

    def _settings_text(self) -> str:
        return (
            "Настройки\n\n"
            f"Сохранять сообщения: {self._state_text(self._storage_enabled())}\n"
            f"Расшифровывать голосовые: {self._state_text(self._transcription_enabled())}\n"
            f"Отвечать реплаем: {self._state_text(self._reply_to_voice())}\n\n"
            f"Модель: {self._settings.whisper_model}\n"
            f"Язык: {self._settings.whisper_language or 'auto'}"
        )

    def _stats_text(self) -> str:
        stats = self._storage.stats()
        return (
            "Статистика архива\n\n"
            f"Сообщений: {stats['messages']}\n"
            f"Вложений: {stats['attachments']}\n"
            f"Чатов: {stats['chats']}"
        )

    @staticmethod
    def _main_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "Статистика", "callback_data": "menu:stats"},
                    {"text": "Настройки", "callback_data": "menu:settings"},
                ],
                [{"text": "Выгрузить архив", "callback_data": "menu:export"}],
            ]
        }

    @staticmethod
    def _export_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "24 часа", "callback_data": "export:1d"},
                    {"text": "7 дней", "callback_data": "export:7d"},
                ],
                [
                    {"text": "30 дней", "callback_data": "export:30d"},
                    {"text": "Все время", "callback_data": "export:all"},
                ],
                [{"text": "Назад", "callback_data": "menu:main"}],
            ]
        }

    def _settings_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": f"Хранение: {self._state_text(self._storage_enabled())}",
                        "callback_data": "toggle:storage",
                    }
                ],
                [
                    {
                        "text": f"Расшифровка: {self._state_text(self._transcription_enabled())}",
                        "callback_data": "toggle:transcription",
                    }
                ],
                [
                    {
                        "text": f"Реплай: {self._state_text(self._reply_to_voice())}",
                        "callback_data": "toggle:reply",
                    }
                ],
                [{"text": "Назад", "callback_data": "menu:main"}],
            ]
        }

    def _toggle_bool(self, key: str, default: bool) -> None:
        self._storage.set_bool(key, not self._storage.get_bool(key, default))

    def _storage_enabled(self) -> bool:
        return self._storage.get_bool(SETTING_STORAGE_ENABLED, True)

    def _transcription_enabled(self) -> bool:
        return self._storage.get_bool(SETTING_TRANSCRIPTION_ENABLED, True)

    def _reply_to_voice(self) -> bool:
        return self._storage.get_bool(SETTING_REPLY_TO_VOICE, self._settings.reply_to_voice)

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self._settings.admin_user_ids

    @staticmethod
    def _export_period(data: str) -> tuple[str, int | None]:
        now = int(time.time())
        periods = {
            "export:1d": ("последние 24 часа", now - int(timedelta(days=1).total_seconds())),
            "export:7d": ("последние 7 дней", now - int(timedelta(days=7).total_seconds())),
            "export:30d": ("последние 30 дней", now - int(timedelta(days=30).total_seconds())),
        }
        return periods.get(data, ("все время", None))

    @staticmethod
    def _find_voice_path(attachments: list[dict[str, Any]]) -> Path | None:
        for attachment in attachments:
            if attachment["kind"] == "voice":
                return Path(attachment["local_path"])
        return None

    @staticmethod
    def _attachment_suffix(file_path: str, spec: dict[str, Any]) -> str:
        file_name = spec.get("file_name")
        suffix = Path(file_name or file_path).suffix
        if suffix:
            return _safe_filename(suffix)
        return ".bin"

    @staticmethod
    def _is_voice(message: dict[str, Any]) -> bool:
        return bool(message.get("voice"))

    @staticmethod
    def _state_text(value: bool) -> str:
        return "вкл" if value else "выкл"


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


async def main() -> None:
    settings = load_settings()
    app = TelegramBusinessVoiceTranscriberApp(settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
