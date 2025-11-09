# Telegram Auto Hatch Posts

> Автоматический репостер Telegram-постов с автогенерацией партнёрских ссылок Яндекс.Маркета.  
> Работает как **userbot** (через твою Telegram-сессию) и автоматически добавляет кнопки с короткими cc-ссылками.  
> Поддерживает картинки, кнопки и безопасную авторизацию в Яндекс через Playwright.

---

## Возможности

- Репост сообщений из заданных каналов в целевой канал.  
- Автоматическая замена ссылок на **Yandex Market** на партнёрские (`market.yandex.ru/cc/...`).  
- Поддержка **разметки текста**, **изображений** и **inline-кнопок**.  
- Сохранение авторизации в **Yandex** через Playwright (`storage_state.json`).  
- Работа от имени **пользовательского аккаунта** (userbot), без отдельного бота BotFather.  

---

## Установка

### 1. Клонируй репозиторий

```bash
git clone https://github.com/<yourname>/telegram-auto-hatch-posts.git
cd telegram-auto-hatch-posts
```

### 2. Создай виртуальное окружение и активируй

```bash
python -m venv .venv
source .venv/bin/activate  # Linux / macOS
.venv\Scripts\activate     # Windows
```

### 3. Установи зависимости

```bash
pip install -r requirements.txt
```

### 4. Установи Playwright-драйверы

```bash
playwright install chromium
```

---

## Авторизация в Telegram

Бот работает **как userbot**, то есть использует **твою личную Telegram-сессию**, а не токен от `@BotFather`.

Первый запуск создаст файл сессии (например, `main_user.session`), и тебе нужно будет авторизоваться:

```bash
python main.py
```

Затем бот сохранит твою сессию, и повторная авторизация не понадобится.  

---

## 🧩 Конфигурация

Создай `.env` на основе примера:

```bash
cp .env.example .env
```

### Пример содержимого

```env
API_ID=1234567
API_HASH=abcdef0123456789abcdef0123456789
SESSION=main_user
RAW_SOURCES=t.me/source_channel
RAW_TARGET=t.me/target_channel
HEADLESS=false
STORAGE_PATH=storage_state.json
```

`API_ID` и `API_HASH` можно получить на [my.telegram.org](https://my.telegram.org).

---

## Запуск вручную

```bash
python main.py
```

Первый запуск откроет Chromium — авторизуйся в **Яндекс**, чтобы бот мог генерировать партнёрские cc-ссылки.  
После успешного входа сессия сохранится в `storage_state.json`.

---

## Деплой на VPS (systemd)

1. Скопируй код проекта на сервер.  
2. Установи зависимости, как выше.  
3. Создай юнит-файл для systemd:

```bash
sudo nano /etc/systemd/system/telegram-auto-hatch.service
```

Вставь туда:

```ini
[Unit]
Description=Telegram Auto Hatch Posts
After=network.target

[Service]
User=root
WorkingDirectory=/root/telegram-auto-hatch-posts
ExecStart=/root/telegram-auto-hatch-posts/.venv/bin/python main.py
Restart=always
RestartSec=5
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
```

4. Активируй и запусти сервис:

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-auto-hatch
sudo systemctl start telegram-auto-hatch
```

Проверить логи:

```bash
journalctl -u telegram-auto-hatch -f
```

Теперь бот будет запускаться автоматически при перезагрузке VPS.

---
