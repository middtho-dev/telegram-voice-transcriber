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
from .postprocess import add_meaningful_emojis, improve_transcript, parse_replacements
from .storage import MessageStorage, extract_attachment_specs
from .support import generate_vpn_support_reply
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
SETTING_AUTO_DELETE_OWN_VOICE = "auto_delete_own_voice"
SETTING_TRANSCRIPT_PREFIX_ENABLED = "transcript_prefix_enabled"
SETTING_TRANSCRIPT_PREFIX = "transcript_prefix"
SETTING_TRANSCRIPT_CLEANUP_ENABLED = "transcript_cleanup_enabled"
SETTING_PRESERVE_PROFANITY = "preserve_profanity"
SETTING_TRANSCRIPT_REPLACEMENTS = "transcript_replacements"
SETTING_TRANSCRIPT_EMOJIS_ENABLED = "transcript_emojis_enabled"
SETTING_VPN_SUPPORT_ENABLED = "vpn_support_enabled"
SETTING_SUPPORT_LEARNING_ENABLED = "support_learning_enabled"
SETTING_SUPPORT_SERVICE_NAME = "support_service_name"
SETTING_SUPPORT_CONTACT = "support_contact"
SETTING_PENDING_ADMIN_ACTION = "pending_admin_action"


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

    async def delete_business_messages(
        self,
        *,
        business_connection_id: str,
        message_ids: list[int],
    ) -> None:
        await self._request(
            "deleteBusinessMessages",
            {
                "business_connection_id": business_connection_id,
                "message_ids": message_ids,
            },
        )

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
        elif await self._handle_pending_admin_input(chat_id, text):
            return
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
        elif data == "menu:archive":
            await self._telegram.answer_callback_query(callback_id)
            await self._edit_archive(chat_id, message_id)
        elif data == "menu:support":
            await self._telegram.answer_callback_query(callback_id)
            await self._edit_support(chat_id, message_id)
        elif data == "support:edit_service_name":
            self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "edit_service_name")
            await self._telegram.answer_callback_query(callback_id)
            await self._telegram.send_message(chat_id=chat_id, text="✏️ Напишите новое название VPN-сервиса одним сообщением.")
        elif data == "support:edit_contact":
            self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "edit_contact")
            await self._telegram.answer_callback_query(callback_id)
            await self._telegram.send_message(chat_id=chat_id, text="✏️ Напишите контакт поддержки: username, ссылку или короткий текст.")
        elif data == "support:analyze":
            await self._telegram.answer_callback_query(callback_id, "Анализирую")
            result = await asyncio.to_thread(self._storage.analyze_support_knowledge, self._settings.admin_user_ids)
            await self._telegram.send_message(
                chat_id=chat_id,
                text=f"🧠 Анализ завершён.\nПросканировано сообщений: {result['scanned']}\nДобавлено/обновлено знаний: {result['learned']}",
            )
            await self._edit_support(chat_id, message_id)
        elif data == "menu:maintenance":
            await self._telegram.answer_callback_query(callback_id)
            await self._edit_maintenance(chat_id, message_id)
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
        elif data.startswith("cleanup:"):
            await self._telegram.answer_callback_query(callback_id)
            await self._edit_cleanup_confirm(chat_id, message_id, data)
        elif data.startswith("confirm_cleanup:"):
            await self._telegram.answer_callback_query(callback_id, "Очищаю")
            await self._run_cleanup(chat_id, message_id, data.removeprefix("confirm_"))
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
        elif data == "toggle:auto_delete_own_voice":
            self._toggle_bool(SETTING_AUTO_DELETE_OWN_VOICE, False)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_settings(chat_id, message_id)
        elif data == "toggle:transcript_prefix":
            self._toggle_bool(SETTING_TRANSCRIPT_PREFIX_ENABLED, bool(self._settings.transcript_prefix))
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_settings(chat_id, message_id)
        elif data == "settings:edit_transcript_prefix":
            self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "edit_transcript_prefix")
            await self._telegram.answer_callback_query(callback_id)
            await self._telegram.send_message(
                chat_id=chat_id,
                text="✏️ Напишите новый префикс расшифровки. Например: Расшифровка:",
            )
        elif data == "toggle:transcript_cleanup":
            self._toggle_bool(SETTING_TRANSCRIPT_CLEANUP_ENABLED, True)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_settings(chat_id, message_id)
        elif data == "toggle:preserve_profanity":
            self._toggle_bool(SETTING_PRESERVE_PROFANITY, True)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_settings(chat_id, message_id)
        elif data == "toggle:transcript_emojis":
            self._toggle_bool(SETTING_TRANSCRIPT_EMOJIS_ENABLED, False)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_settings(chat_id, message_id)
        elif data == "settings:edit_replacements":
            self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "edit_replacements")
            await self._telegram.answer_callback_query(callback_id)
            await self._telegram.send_message(
                chat_id=chat_id,
                text=(
                    "✏️ Пришлите словарь автозамен, каждая замена с новой строки:\n\n"
                    "ошибка => правильно\n"
                    "серега => Серёга\n"
                    "open ai => OpenAI"
                ),
            )
        elif data == "toggle:vpn_support":
            self._toggle_bool(SETTING_VPN_SUPPORT_ENABLED, False)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_support(chat_id, message_id)
        elif data == "toggle:support_learning":
            self._toggle_bool(SETTING_SUPPORT_LEARNING_ENABLED, False)
            await self._telegram.answer_callback_query(callback_id, "Готово")
            await self._edit_support(chat_id, message_id)
            if self._support_learning_enabled():
                result = await asyncio.to_thread(self._storage.analyze_support_knowledge, self._settings.admin_user_ids)
                await self._telegram.send_message(
                    chat_id=chat_id,
                    text=f"🧠 Обучение включено. Уже проанализировал архив.\nПросканировано: {result['scanned']}\nДобавлено/обновлено: {result['learned']}",
                )
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
                    await self._maybe_learn_support_reply(message)
                    logger.info("Archived message chat=%s id=%s", chat_id, message_id)

                if not self._is_voice(message) or not self._transcription_enabled():
                    await self._maybe_send_support_reply(message)
                    return

                audio_path = self._find_voice_path(attachments)
                should_delete_audio = False
                if audio_path is None:
                    audio_path = await self._download_voice(message)
                    should_delete_audio = True

                await self._maybe_delete_own_voice(message)
                logger.info("Transcribing voice message chat=%s id=%s", chat_id, message_id)
                text = await self._transcriber.transcribe(audio_path)
                text = self._improve_transcript(text)
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

    async def _maybe_send_support_reply(self, message: dict[str, Any]) -> None:
        if not self._vpn_support_enabled():
            return
        if self._is_admin(int((message.get("from") or {}).get("id", 0))):
            return

        text = (message.get("text") or message.get("caption") or "").strip()
        if not text:
            return

        reply = None
        if self._support_learning_enabled():
            reply = await asyncio.to_thread(self._storage.find_learned_reply, text)

        if reply is None:
            reply = generate_vpn_support_reply(
                text,
                service_name=self._support_service_name(),
                support_contact=self._support_contact(),
            )
        if not reply:
            return

        await self._telegram.send_business_message(
            business_connection_id=message["business_connection_id"],
            chat_id=message["chat"]["id"],
            text=reply,
            reply_to_message_id=message["message_id"],
        )
        logger.info(
            "VPN support reply sent chat=%s id=%s",
            message["chat"]["id"],
            message["message_id"],
        )

    async def _maybe_delete_own_voice(self, message: dict[str, Any]) -> None:
        if not self._auto_delete_own_voice():
            return
        if not self._is_admin(int((message.get("from") or {}).get("id", 0))):
            return

        try:
            await self._telegram.delete_business_messages(
                business_connection_id=message["business_connection_id"],
                message_ids=[int(message["message_id"])],
            )
            logger.info(
                "Deleted own voice message chat=%s id=%s",
                message["chat"]["id"],
                message["message_id"],
            )
        except TelegramApiError:
            logger.exception(
                "Failed to delete own voice message chat=%s id=%s. Check Telegram Business delete permissions.",
                message["chat"]["id"],
                message["message_id"],
            )

    async def _maybe_learn_support_reply(self, message: dict[str, Any]) -> None:
        if not self._support_learning_enabled():
            return
        if not self._is_admin(int((message.get("from") or {}).get("id", 0))):
            return

        answer_text = (message.get("text") or message.get("caption") or "").strip()
        reply = message.get("reply_to_message") or {}
        question_text = (reply.get("text") or reply.get("caption") or "").strip()
        if not answer_text or not question_text:
            return

        learned = await asyncio.to_thread(
            self._storage.learn_support_reply,
            question_text=question_text,
            answer_text=answer_text,
            source_chat_id=message["chat"]["id"],
            source_message_id=message["message_id"],
            reply_to_message_id=reply.get("message_id") or 0,
        )
        if learned:
            logger.info(
                "Learned support reply chat=%s id=%s",
                message["chat"]["id"],
                message["message_id"],
            )

    async def _handle_pending_admin_input(self, chat_id: int, text: str) -> bool:
        action = self._storage.get_value(SETTING_PENDING_ADMIN_ACTION)
        if not action:
            return False

        value = text.strip()
        if not value:
            await self._telegram.send_message(chat_id=chat_id, text="Пустое значение не сохранено.")
            return True

        if action == "edit_service_name":
            self._storage.set_value(SETTING_SUPPORT_SERVICE_NAME, value[:80])
            self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "")
            await self._telegram.send_message(chat_id=chat_id, text=f"✅ Название сервиса сохранено: {value[:80]}")
            await self._send_menu(chat_id)
            return True

        if action == "edit_contact":
            self._storage.set_value(SETTING_SUPPORT_CONTACT, value[:120])
            self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "")
            await self._telegram.send_message(chat_id=chat_id, text=f"✅ Контакт поддержки сохранён: {value[:120]}")
            await self._send_menu(chat_id)
            return True

        if action == "edit_transcript_prefix":
            self._storage.set_value(SETTING_TRANSCRIPT_PREFIX, value[:80])
            self._storage.set_bool(SETTING_TRANSCRIPT_PREFIX_ENABLED, True)
            self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "")
            await self._telegram.send_message(chat_id=chat_id, text=f"✅ Префикс расшифровки сохранён: {value[:80]}")
            await self._send_menu(chat_id)
            return True

        if action == "edit_replacements":
            replacements = parse_replacements(value)
            self._storage.set_value(SETTING_TRANSCRIPT_REPLACEMENTS, value[:4000])
            self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "")
            await self._telegram.send_message(
                chat_id=chat_id,
                text=f"✅ Словарь автозамен сохранён. Активных замен: {len(replacements)}",
            )
            await self._send_menu(chat_id)
            return True

        self._storage.set_value(SETTING_PENDING_ADMIN_ACTION, "")
        return False

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
        prefix = self._transcript_prefix()
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

    async def _edit_archive(self, chat_id: int, message_id: int) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=self._archive_text(),
            reply_markup=self._archive_keyboard(),
        )

    async def _edit_support(self, chat_id: int, message_id: int) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=self._support_text(),
            reply_markup=self._support_keyboard(),
        )

    async def _edit_maintenance(self, chat_id: int, message_id: int) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=self._maintenance_text(),
            reply_markup=self._maintenance_keyboard(),
        )

    async def _edit_export_periods(self, chat_id: int, message_id: int) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="Выберите период для выгрузки. В архиве будет HTML-страница с вкладками по чатам и встроенным просмотром вложений.",
            reply_markup=self._export_keyboard(),
        )

    async def _edit_cleanup_confirm(self, chat_id: int, message_id: int, cleanup_id: str) -> None:
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=self._cleanup_confirm_text(cleanup_id),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Да, удалить", "callback_data": f"confirm_{cleanup_id}"}],
                    [{"text": "Назад", "callback_data": "menu:maintenance"}],
                ]
            },
        )

    async def _run_cleanup(self, chat_id: int, message_id: int, cleanup_id: str) -> None:
        result_text = await asyncio.to_thread(self._cleanup_result_text, cleanup_id)
        await self._telegram.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=result_text,
            reply_markup=self._maintenance_keyboard(),
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
        maintenance = self._storage.maintenance_stats()
        return (
            "🧭 Центр управления\n\n"
            f"💬 Сообщений: {stats['messages']}\n"
            f"🎙 Голосовых: {stats['voice_messages']}\n"
            f"📎 Вложений: {stats['attachments']}\n"
            f"👥 Чатов: {stats['chats']}\n"
            f"🧠 Выученных ответов: {stats['learned_replies']}\n"
            f"💾 Вложения занимают: {_human_bytes(maintenance['attachments_bytes'])}\n\n"
            "Выберите раздел ниже."
        )

    def _settings_text(self) -> str:
        return (
            "⚙️ Настройки\n\n"
            f"🗄 Сохранять сообщения: {self._state_text(self._storage_enabled())}\n"
            f"🎙 Расшифровывать голосовые: {self._state_text(self._transcription_enabled())}\n"
            f"↩️ Отвечать реплаем: {self._state_text(self._reply_to_voice())}\n\n"
            f"🧽 Удалять мои voice после расшифровки: {self._state_text(self._auto_delete_own_voice())}\n\n"
            f"🏷 Префикс расшифровки: {self._state_text(self._transcript_prefix_enabled())}\n"
            f"Текст префикса: {self._transcript_prefix_value() or 'пусто'}\n\n"
            f"🪄 Улучшать расшифровку: {self._state_text(self._transcript_cleanup_enabled())}\n"
            f"🤬 Сохранять мат/разговорность: {self._state_text(self._preserve_profanity())}\n"
            f"✨ Подбирать эмодзи: {self._state_text(self._transcript_emojis_enabled())}\n"
            f"🧾 Автозамен: {len(self._transcript_replacements())}\n\n"
            f"🤖 Модель: {self._settings.whisper_model}\n"
            f"🌐 Язык: {self._settings.whisper_language or 'auto'}"
        )

    def _stats_text(self) -> str:
        stats = self._storage.stats()
        return (
            "📊 Статистика\n\n"
            f"💬 Сообщений: {stats['messages']}\n"
            f"🎙 Голосовых: {stats['voice_messages']}\n"
            f"📎 Вложений: {stats['attachments']}\n"
            f"👥 Чатов: {stats['chats']}\n"
            f"🧠 Выученных ответов: {stats['learned_replies']}\n\n"
            f"{self._top_chats_text()}"
        )

    @staticmethod
    def _main_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🗄 Архив", "callback_data": "menu:archive"},
                    {"text": "🛟 Поддержка", "callback_data": "menu:support"},
                ],
                [
                    {"text": "🧹 Обслуживание", "callback_data": "menu:maintenance"},
                    {"text": "📊 Статистика", "callback_data": "menu:stats"},
                ],
                [{"text": "⚙️ Настройки", "callback_data": "menu:settings"}],
            ]
        }

    def _archive_text(self) -> str:
        stats = self._storage.stats()
        maintenance = self._storage.maintenance_stats()
        return (
            "🗄 Архив сообщений\n\n"
            f"💬 Сообщений: {stats['messages']}\n"
            f"📎 Вложений: {stats['attachments']}\n"
            f"👥 Чатов: {stats['chats']}\n"
            f"🧱 База: {_human_bytes(maintenance['database_bytes'])}\n"
            f"💾 Вложения: {_human_bytes(maintenance['attachments_bytes'])}\n"
            f"📦 ZIP-выгрузки: {maintenance['export_files']} файлов, {_human_bytes(maintenance['exports_bytes'])}\n\n"
            "Можно выгрузить HTML-архив за нужный период или перейти к очистке."
        )

    @staticmethod
    def _archive_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "📦 Выгрузить архив", "callback_data": "menu:export"}],
                [{"text": "🧹 Очистка и место", "callback_data": "menu:maintenance"}],
                [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
            ]
        }

    def _support_text(self) -> str:
        stats = self._storage.stats()
        return (
            "🛟 Поддержка VPN\n\n"
            f"🤖 Автоответы: {self._state_text(self._vpn_support_enabled())}\n"
            f"🧠 Обучение на переписке: {self._state_text(self._support_learning_enabled())}\n"
            f"📚 Выученных ответов: {stats['learned_replies']}\n\n"
            f"🏷 Название сервиса: {self._support_service_name()}\n"
            f"☎️ Контакт: {self._support_contact()}\n\n"
            "Когда обучение включено, бот анализирует всю сохранённую переписку: ваши ручные ответы реплаем, последовательности клиент->оператор и типовые инструкции оператора."
        )

    def _support_keyboard(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": f"🤖 VPN-поддержка: {self._state_text(self._vpn_support_enabled())}",
                        "callback_data": "toggle:vpn_support",
                    }
                ],
                [
                    {
                        "text": f"🧠 Обучение: {self._state_text(self._support_learning_enabled())}",
                        "callback_data": "toggle:support_learning",
                    }
                ],
                [{"text": "🔎 Анализировать переписку", "callback_data": "support:analyze"}],
                [
                    {"text": "✏️ Название", "callback_data": "support:edit_service_name"},
                    {"text": "☎️ Контакт", "callback_data": "support:edit_contact"},
                ],
                [{"text": "🧯 Сбросить обучение", "callback_data": "cleanup:learned:all"}],
                [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
            ]
        }

    def _maintenance_text(self) -> str:
        maintenance = self._storage.maintenance_stats()
        return (
            "🧹 Обслуживание\n\n"
            f"🧱 База: {_human_bytes(maintenance['database_bytes'])}\n"
            f"📎 Вложения: {_human_bytes(maintenance['attachments_bytes'])}\n"
            f"📦 ZIP-выгрузки: {maintenance['export_files']} файлов, {_human_bytes(maintenance['exports_bytes'])}\n\n"
            "Очистка удаляет файлы вложений и ZIP-выгрузки, но не удаляет сами сообщения из базы."
        )

    @staticmethod
    def _maintenance_keyboard() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "📎 Вложения старше 30 дней", "callback_data": "cleanup:attachments:30d"}],
                [{"text": "📎 Вложения старше 90 дней", "callback_data": "cleanup:attachments:90d"}],
                [{"text": "🧨 Все вложения", "callback_data": "cleanup:attachments:all"}],
                [{"text": "📦 ZIP старше 30 дней", "callback_data": "cleanup:exports:30d"}],
                [{"text": "🗑 Все ZIP", "callback_data": "cleanup:exports:all"}],
                [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
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
                        "text": f"🗄 Хранение: {self._state_text(self._storage_enabled())}",
                        "callback_data": "toggle:storage",
                    }
                ],
                [
                    {
                        "text": f"🎙 Расшифровка: {self._state_text(self._transcription_enabled())}",
                        "callback_data": "toggle:transcription",
                    }
                ],
                [
                    {
                        "text": f"↩️ Реплай: {self._state_text(self._reply_to_voice())}",
                        "callback_data": "toggle:reply",
                    }
                ],
                [
                    {
                        "text": f"🧽 Удалять мои voice: {self._state_text(self._auto_delete_own_voice())}",
                        "callback_data": "toggle:auto_delete_own_voice",
                    }
                ],
                [
                    {
                        "text": f"🏷 Префикс: {self._state_text(self._transcript_prefix_enabled())}",
                        "callback_data": "toggle:transcript_prefix",
                    }
                ],
                [{"text": "✏️ Изменить префикс", "callback_data": "settings:edit_transcript_prefix"}],
                [
                    {
                        "text": f"🪄 Улучшать текст: {self._state_text(self._transcript_cleanup_enabled())}",
                        "callback_data": "toggle:transcript_cleanup",
                    }
                ],
                [
                    {
                        "text": f"🤬 Мат/разговорность: {self._state_text(self._preserve_profanity())}",
                        "callback_data": "toggle:preserve_profanity",
                    }
                ],
                [
                    {
                        "text": f"✨ Эмодзи по смыслу: {self._state_text(self._transcript_emojis_enabled())}",
                        "callback_data": "toggle:transcript_emojis",
                    }
                ],
                [{"text": "🧾 Словарь автозамен", "callback_data": "settings:edit_replacements"}],
                [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
            ]
        }

    def _top_chats_text(self) -> str:
        chats = self._storage.top_chats()
        if not chats:
            return "Топ чатов: пока пусто"
        lines = ["Топ чатов:"]
        for index, chat in enumerate(chats, start=1):
            lines.append(
                f"{index}. {chat['chat_name']}: {chat['messages']} сообщений, "
                f"{chat['voice_messages']} голосовых"
            )
        return "\n".join(lines)

    @staticmethod
    def _cleanup_confirm_text(cleanup_id: str) -> str:
        labels = {
            "cleanup:attachments:30d": "Удалить вложения старше 30 дней?",
            "cleanup:attachments:90d": "Удалить вложения старше 90 дней?",
            "cleanup:attachments:all": "Удалить все сохраненные вложения?",
            "cleanup:exports:30d": "Удалить ZIP-выгрузки старше 30 дней?",
            "cleanup:exports:all": "Удалить все ZIP-выгрузки?",
            "cleanup:learned:all": "Удалить все выученные ответы поддержки?",
        }
        detail = labels.get(cleanup_id, "Выполнить очистку?")
        return (
            f"{detail}\n\n"
            "Это действие нельзя отменить. Сообщения в SQLite не удаляются, кроме базы выученных ответов при её сбросе."
        )

    def _cleanup_result_text(self, cleanup_id: str) -> str:
        now = int(time.time())
        if cleanup_id == "cleanup:attachments:30d":
            result = self._storage.cleanup_attachments(now - int(timedelta(days=30).total_seconds()))
            return f"Готово. Удалено вложений: {result['files']}, освобождено: {_human_bytes(result['bytes'])}."
        if cleanup_id == "cleanup:attachments:90d":
            result = self._storage.cleanup_attachments(now - int(timedelta(days=90).total_seconds()))
            return f"Готово. Удалено вложений: {result['files']}, освобождено: {_human_bytes(result['bytes'])}."
        if cleanup_id == "cleanup:attachments:all":
            result = self._storage.cleanup_attachments()
            return f"Готово. Удалено вложений: {result['files']}, освобождено: {_human_bytes(result['bytes'])}."
        if cleanup_id == "cleanup:exports:30d":
            result = self._storage.cleanup_exports(now - int(timedelta(days=30).total_seconds()))
            return f"Готово. Удалено ZIP-файлов: {result['files']}, освобождено: {_human_bytes(result['bytes'])}."
        if cleanup_id == "cleanup:exports:all":
            result = self._storage.cleanup_exports()
            return f"Готово. Удалено ZIP-файлов: {result['files']}, освобождено: {_human_bytes(result['bytes'])}."
        if cleanup_id == "cleanup:learned:all":
            count = self._storage.clear_learned_replies()
            return f"Готово. Удалено выученных ответов: {count}."
        return "Неизвестный тип очистки."

    def _toggle_bool(self, key: str, default: bool) -> None:
        self._storage.set_bool(key, not self._storage.get_bool(key, default))

    def _storage_enabled(self) -> bool:
        return self._storage.get_bool(SETTING_STORAGE_ENABLED, True)

    def _transcription_enabled(self) -> bool:
        return self._storage.get_bool(SETTING_TRANSCRIPTION_ENABLED, True)

    def _reply_to_voice(self) -> bool:
        return self._storage.get_bool(SETTING_REPLY_TO_VOICE, self._settings.reply_to_voice)

    def _auto_delete_own_voice(self) -> bool:
        return self._storage.get_bool(SETTING_AUTO_DELETE_OWN_VOICE, False)

    def _transcript_prefix_enabled(self) -> bool:
        return self._storage.get_bool(
            SETTING_TRANSCRIPT_PREFIX_ENABLED,
            bool(self._settings.transcript_prefix),
        )

    def _transcript_prefix_value(self) -> str:
        value = self._storage.get_value(SETTING_TRANSCRIPT_PREFIX)
        if value is None:
            return self._settings.transcript_prefix
        return value.strip()

    def _transcript_prefix(self) -> str:
        if not self._transcript_prefix_enabled():
            return ""
        return self._transcript_prefix_value()

    def _transcript_cleanup_enabled(self) -> bool:
        return self._storage.get_bool(SETTING_TRANSCRIPT_CLEANUP_ENABLED, True)

    def _preserve_profanity(self) -> bool:
        return self._storage.get_bool(SETTING_PRESERVE_PROFANITY, True)

    def _transcript_emojis_enabled(self) -> bool:
        return self._storage.get_bool(SETTING_TRANSCRIPT_EMOJIS_ENABLED, False)

    def _transcript_replacements(self) -> dict[str, str]:
        return parse_replacements(self._storage.get_value(SETTING_TRANSCRIPT_REPLACEMENTS) or "")

    def _improve_transcript(self, text: str) -> str:
        improved = text
        if self._transcript_cleanup_enabled():
            improved = improve_transcript(
                text,
                custom_replacements=self._transcript_replacements(),
                preserve_profanity=self._preserve_profanity(),
            )
        if self._transcript_emojis_enabled():
            return add_meaningful_emojis(improved)
        return improved

    def _vpn_support_enabled(self) -> bool:
        return self._storage.get_bool(SETTING_VPN_SUPPORT_ENABLED, False)

    def _support_learning_enabled(self) -> bool:
        return self._storage.get_bool(SETTING_SUPPORT_LEARNING_ENABLED, False)

    def _support_service_name(self) -> str:
        return self._storage.get_value(SETTING_SUPPORT_SERVICE_NAME) or self._settings.support_service_name

    def _support_contact(self) -> str:
        return self._storage.get_value(SETTING_SUPPORT_CONTACT) or self._settings.support_contact

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


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


async def main() -> None:
    settings = load_settings()
    app = TelegramBusinessVoiceTranscriberApp(settings)
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
