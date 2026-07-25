# Dootuu Mailer (`@dootuubot`)

Отдельный бот: аккаунты для рассылки → группы → шаблоны сообщений → лог-группа → циклы с паузой.

**Не связан** с vision-ai-bot / магазином.

## Возможности

1. Добавление Telegram-аккаунтов (телефон / код / 2FA) через Telethon  
2. Список групп для рассылки  
3. Редактируемые шаблоны сообщений  
4. Лог-группа: куда ушло, с какого аккаунта  
5. Круг: N сообщений → пауза (по умолчанию 1 час) → снова  

## Env

| Variable | Описание |
|----------|----------|
| `BOT_TOKEN` / `MAILER_BOT_TOKEN` | токен @dootuubot |
| `ADMIN_IDS` | твой числовой id |
| `ADMIN_USERNAMES` | или username без @ |
| `TG_API_ID` / `TG_API_HASH` | https://my.telegram.org |

## Локально

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# заполнить .env
python -m mailer
```

## Railway

- Start command: `python -m mailer`
- Volume (желательно): `/app/data` — чтобы sessions не слетали
