# Telegram Voice Transcriber

Userbot для Telegram: подключается к вашему личному аккаунту, мониторит входящие и исходящие голосовые сообщения во всех чатах, расшифровывает их через OpenAI и отправляет текст обратно в тот же чат.

Важно: это именно userbot через Telegram MTProto, а не обычный BotFather-бот. Он работает от имени вашего аккаунта.

## Что умеет

- Следит за голосовыми сообщениями во всех доступных чатах аккаунта.
- Обрабатывает и входящие, и ваши исходящие голосовые.
- Скачивает голосовое временно, отправляет в OpenAI Speech-to-Text и удаляет файл.
- Пишет расшифровку в тот же чат, по умолчанию ответом на голосовое.
- Готов к запуску на сервере через Docker Compose.

## Подготовка

1. Получите `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` на [my.telegram.org/apps](https://my.telegram.org/apps).
2. Получите `OPENAI_API_KEY` в кабинете OpenAI.
3. Создайте файл `.env`:

```bash
cp .env.example .env
```

4. Заполните `.env`:

```env
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_telegram_api_hash
TELEGRAM_SESSION_PATH=data/telegram_user.session

OPENAI_API_KEY=sk-your-openai-key
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
OPENAI_TRANSCRIBE_LANGUAGE=ru

DOWNLOAD_DIR=downloads
MAX_PARALLEL_TRANSCRIPTIONS=2
TRANSCRIPT_PREFIX=Расшифровка:
REPLY_TO_VOICE=true
```

## Первый запуск локально

Первый запуск лучше сделать локально или в интерактивной SSH-сессии, потому что Telegram попросит номер телефона, код из Telegram и, если включена, 2FA-пароль.

```bash
python -m venv .venv
. .venv/Scripts/activate
pip install -r requirements.txt
python -m src.main
```

На Windows в PowerShell активация окружения:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m src.main
```

После успешного входа появится файл сессии в `data/telegram_user.session`. Его нужно сохранить на сервере, чтобы повторно не логиниться.

## Запуск на сервере

На сервере должны быть установлены `git`, Docker и Docker Compose.

```bash
git clone https://github.com/middtho-dev/telegram-voice-transcriber.git
cd telegram-voice-transcriber
cp .env.example .env
nano .env
```

Если вы уже авторизовались локально, перенесите папку `data/` на сервер рядом с `docker-compose.yml`.

Запуск:

```bash
docker compose up -d --build
docker compose logs -f telegram-voice-transcriber
```

Остановка:

```bash
docker compose down
```

## Обновления на сервере

Все обновления можно делать прямо на сервере:

```bash
bash scripts/update-server.sh
```

Скрипт подтянет свежий код, пересоберет контейнер и покажет последние логи.

## Полезные настройки

- `OPENAI_TRANSCRIBE_LANGUAGE=ru` помогает модели ожидать русский язык. Можно оставить пустым для автоопределения.
- `MAX_PARALLEL_TRANSCRIPTIONS=2` ограничивает параллельные расшифровки.
- `TRANSCRIPT_PREFIX=` можно сделать пустым, если не нужен заголовок перед текстом.
- `REPLY_TO_VOICE=false` отправит расшифровку обычным сообщением в чат, а не ответом.

## Безопасность

Не коммитьте `.env` и файл `data/telegram_user.session`: сессия дает доступ к вашему Telegram-аккаунту. Они уже добавлены в `.gitignore`.
