# 🎨 Soviet Postcard Bot

Telegram бот для создания винтажных открыток в стиле СССР с вашими фотографиями.

## 🚀 Технологии

- **Leonardo Phoenix AI** - генерация художественных шаблонов
- **RunPod Serverless** - замена лиц (face swap)
- **Python-telegram-bot** - Telegram API
- **PostgreSQL** - база данных
- **YooKassa** - приём платежей

## 📋 Установка

### 1. Клонируй репозиторий

```bash
git clone https://github.com/your-username/postcard-bot.git
cd postcard-bot
```

### 2. Установи зависимости

```bash
pip install -r requirements.txt
```

### 3. Настрой переменные окружения

Скопируй `.env.example` в `.env`:

```bash
cp .env.example .env
```

Заполни все значения в `.env`:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
YOOKASSA_SECRET_KEY=your_yookassa_key
YOOKASSA_SHOP_ID=your_shop_id
ADMIN_ID=your_telegram_id
DATABASE_URL=postgresql://...
LEONARDO_API_KEY=your_leonardo_key
RUNPOD_API_KEY=your_runpod_key
RUNPOD_ENDPOINT_ID=your_endpoint_id
```

### 4. Запусти бота

```bash
python opencard_bot.py
```

## 🌐 Деплой на Railway

1. **Создай проект** на [Railway.app](https://railway.app)
2. **Подключи GitHub репозиторий**
3. **Добавь PostgreSQL** из Railway Marketplace
4. **Настрой Environment Variables** (все из `.env`)
5. **Deploy!**

## 🔑 Получение API ключей

### Telegram Bot Token
1. Напиши [@BotFather](https://t.me/BotFather)
2. Команда `/newbot`
3. Скопируй токен

### Leonardo AI
1. Регистрация на [Leonardo.ai](https://leonardo.ai)
2. Settings → API Access
3. Скопируй API Key

### RunPod
1. Регистрация на [RunPod.io](https://runpod.io)
2. The Hub → Serverless → Face Swap 5.2.0
3. Deploy Endpoint
4. Settings → API Keys → Create New

### YooKassa
1. Регистрация в [YooKassa](https://yookassa.ru)
2. Настройки магазина → API ключи
3. Скопируй shopId и secretKey

## 💰 Стоимость

- Leonardo: ~$0.01 за шаблон
- RunPod: ~$0.002 за face swap (serverless)
- **Итого:** ~$0.012 на открытку

## 📊 Структура

```
postcard-bot/
├── opencard_bot.py      # Основной файл бота
├── requirements.txt     # Python зависимости
├── .env.example        # Пример переменных окружения
├── .gitignore          # Игнорируемые файлы
└── README.md           # Документация
```

## ⚠️ Безопасность

**НИКОГДА не коммить .env файл в Git!**

Все секретные ключи должны быть в environment variables.

## 📝 Лицензия

MIT

## 🤝 Поддержка

Вопросы? Telegram: @your_username
