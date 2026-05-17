# Telegram Business Voice Transcriber

Бот для Telegram Business: принимает голосовые сообщения из личных чатов, расшифровывает их локально через `faster-whisper` и отправляет текст обратно в тот же чат от имени подключенного бизнес-аккаунта.

Нейросеть работает локально на вашей машине или VPS. Платные API для распознавания речи не используются.

## Как это работает

1. Вы создаете обычного Telegram-бота через `@BotFather`.
2. Подключаете этого бота в настройках Telegram Business вашего аккаунта.
3. Telegram присылает боту события `business_message` из личных чатов.
4. Если сообщение голосовое, приложение скачивает `.ogg` через Bot API.
5. Локальная модель Whisper распознает аудио.
6. Бот отправляет расшифровку обратно в чат через Business-соединение.

Такой вариант не логинится в ваш Telegram-аккаунт через код и не хранит файл сессии аккаунта на сервере. На VPS хранится только токен бота.

## Что понадобится

- Аккаунт Telegram Premium, потому что Telegram Business сейчас доступен Premium-пользователям.
- Бот, созданный через `@BotFather`.
- VPS или локальная машина, где будет работать распознавание.
- Для Docker-запуска: Docker и Docker Compose.

## Настройка Telegram

### 1. Создайте бота

1. Откройте в Telegram `@BotFather`.
2. Отправьте команду `/newbot`.
3. Задайте имя и username бота.
4. Скопируйте токен вида:

```text
123456789:AA....
```

Это значение нужно записать в `.env` как `TELEGRAM_BOT_TOKEN`.

### 2. Подключите бота к Telegram Business

Сначала включите Business Mode у самого бота:

1. Откройте `@BotFather`.
2. Отправьте `/mybots`.
3. Выберите своего бота.
4. Откройте `Bot Settings`.
5. Откройте `Business Mode`.
6. Нажмите `Turn On`.

Если этот пункт не включить, Telegram может написать, что бот не поддерживает Business.

В Telegram откройте:

```text
Настройки -> Telegram Business -> Чат-боты
```

Дальше:

1. Добавьте созданного бота.
2. Разрешите ему читать сообщения.
3. Разрешите ему отвечать на сообщения.
4. Выберите чаты, где он должен работать. Для начала лучше включить только один тестовый личный чат.

После этого бот начнет получать личные сообщения, которые подходят под выбранные вами правила Business.

## Настройка проекта

Создайте `.env`:

```bash
cp .env.example .env
```

На Windows PowerShell:

```powershell
copy .env.example .env
```

Заполните `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:your_botfather_token

WHISPER_MODEL=large-v3-turbo
WHISPER_LANGUAGE=ru
WHISPER_DEVICE=auto
WHISPER_COMPUTE_TYPE=auto
WHISPER_BEAM_SIZE=5

DOWNLOAD_DIR=downloads
MAX_PARALLEL_TRANSCRIPTIONS=1
TRANSCRIPT_PREFIX=Расшифровка:
REPLY_TO_VOICE=true
POLLING_TIMEOUT=30
```

## Локальный запуск

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py -m src.main
```

Linux/macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m src.main
```

При первом распознавании модель `large-v3-turbo` скачается с Hugging Face. Потом она будет использоваться из локального кэша.

## Запуск на VPS через Docker

### 1. Установите Docker

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y git docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

Если ваш пользователь не в группе Docker:

```bash
sudo usermod -aG docker $USER
```

После этого перелогиньтесь в SSH.

### 2. Загрузите проект

```bash
git clone https://github.com/middtho-dev/telegram-voice-transcriber.git
cd telegram-voice-transcriber
cp .env.example .env
nano .env
```

Вставьте `TELEGRAM_BOT_TOKEN` и сохраните файл.

### 3. Запустите

```bash
docker compose up -d --build
```

Посмотреть логи:

```bash
docker compose logs -f telegram-voice-transcriber
```

Остановить:

```bash
docker compose down
```

Перезапустить после изменения `.env`:

```bash
docker compose up -d
```

Модель кэшируется в папке `models/`, чтобы не скачиваться заново после пересборки контейнера.

## Полезные настройки

- `WHISPER_MODEL=large-v3-turbo` - хороший баланс качества и скорости.
- `WHISPER_MODEL=large-v3` - выше качество, но тяжелее.
- `WHISPER_MODEL=medium` - быстрее и легче для слабой VPS.
- `WHISPER_MODEL=small` - если VPS совсем слабая.
- `WHISPER_LANGUAGE=ru` - ожидаемый язык голосовых. Оставьте пустым для автоопределения.
- `WHISPER_DEVICE=auto` - автоматический выбор CPU/GPU.
- `WHISPER_COMPUTE_TYPE=int8` - часто лучший вариант для CPU и слабых VPS.
- `MAX_PARALLEL_TRANSCRIPTIONS=1` - безопасно для VPS без GPU.
- `TRANSCRIPT_PREFIX=` - пустое значение уберет заголовок перед текстом.
- `REPLY_TO_VOICE=false` - отправлять текст обычным сообщением, а не ответом.

Для слабой VPS обычно лучше так:

```env
WHISPER_MODEL=medium
WHISPER_COMPUTE_TYPE=int8
MAX_PARALLEL_TRANSCRIPTIONS=1
```

## Безопасность

- Не публикуйте `.env`.
- Не отправляйте никому `TELEGRAM_BOT_TOKEN`.
- Если токен утек, откройте `@BotFather` и перевыпустите его через `/revoke`.
- Бота можно отключить в любой момент: `Настройки -> Telegram Business -> Чат-боты`.

## Ограничения

- Это работает для личных Business-чатов, а не для чтения всех групп и каналов аккаунта.
- Бот получает только те чаты и сообщения, которые разрешены в настройках Telegram Business.
- Для первого скачивания модели нужен интернет.
- Само распознавание после скачивания модели выполняется локально.
