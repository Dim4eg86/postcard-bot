import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import logging
import psycopg
from psycopg.rows import dict_row
from yookassa import Configuration, Payment
import uuid
import asyncio
import base64
from PIL import Image, ImageEnhance
import io

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID", "610820340"))
DATABASE_URL = os.getenv("DATABASE_URL")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "vzsuz6ygs5m4ly")
RUNPOD_API_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}"

if YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

# --- СПРАВОЧНИКИ ---
PACKAGES = {
    "1": {"name": "1 открытка", "count": 1, "price": 149},
    "3": {"name": "3 открытки", "count": 3, "price": 399},
    "5": {"name": "5 открыток", "count": 5, "price": 599}
}

THEMES = {
    "new_year": "🎄 Новый Год",
    "feb_14": "❤️ 14 Февраля",
    "feb_23": "🎖 23 Февраля",
    "mar_8": "💐 8 Марта",
    "winter": "❄️ Зима"
}

STYLES = {"ussr": "СССР (Гуашь)", "vintage": "Винтаж (Масло)", "modern": "Модерн"}
SCENES = {"night_street": "🌙 Улица", "pine_forest": "🌲 Лес", "winter_fair": "🎪 Ярмарка"}

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_database():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    credits INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    payment_id TEXT,
                    status TEXT,
                    amount INTEGER,
                    count INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    message TEXT,
                    status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

# --- ЛОГИКА ГЕНЕРАЦИИ ---
async def generate_template_leonardo(theme, style, scene, count, gender, orientation):
    subj = "Couple" if count == "couple" else ("Family" if count == "family" else ("Man" if gender == "man" else "Woman"))
    style_p = {"ussr": "Soviet 1970s gouache postcard", "vintage": "Oil painting", "modern": "Digital art"}.get(style)
    prompt = f"{style_p}, {subj} in {scene}, theme {theme}, festive, high detail."
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a",
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768
    }
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        if r.status_code != 200: return None
        gid = r.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(40):
            await asyncio.sleep(4)
            st = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gid}", headers=headers).json()
            job = st.get("generations_by_pk") or (st.get("generations")[0] if st.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except: return None
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        t_b64 = base64.b64encode(requests.get(template_url).content).decode('utf-8')
        p = {"input": {"source_image": user_photo_b64, "target_image": t_b64, "face_restore_model": "CodeFormer"}}
        h = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
        res = requests.post(f"{RUNPOD_API_URL}/run", json=p, headers=h).json()
        jid = res.get("id")
        for _ in range(40):
            await asyncio.sleep(3)
            s = requests.get(f"{RUNPOD_API_URL}/status/{jid}", headers=h).json()
            if s.get("status") == "COMPLETED":
                out = s.get("output")
                return base64.b64decode(out if isinstance(out, str) else out.get("image"))
    except: return None
    return None

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (user.id, user.username, user.first_name))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user.id,))
            c = cur.fetchone()['credits']
    kb = [[InlineKeyboardButton("🎨 Создать", callback_data="create_postcard")], [InlineKeyboardButton("💰 Пополнить", callback_data="show_pricing")]]
    if update.callback_query: await update.callback_query.edit_message_text(f"Баланс: {c} 🎫", reply_markup=InlineKeyboardMarkup(kb))
    else: await update.message.reply_text(f"Привет! Баланс: {c} 🎫", reply_markup=InlineKeyboardMarkup(kb))

async def create_postcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📱 Вертикаль", callback_data="orient_vertical"), InlineKeyboardButton("🖼️ Горизонт", callback_data="orient_horizontal")]]
    await update.callback_query.edit_message_text("Формат:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['orientation'] = update.callback_query.data.split('_')[1]
    kb = [[InlineKeyboardButton(v, callback_data=f"theme_{k}")] for k, v in THEMES.items()]
    await update.callback_query.edit_message_text("Тема:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['theme'] = update.callback_query.data.split('_')[1]
    kb = [[InlineKeyboardButton("👤 Один", callback_data="cnt_single"), InlineKeyboardButton("👩‍❤️‍👨 Пара", callback_data="cnt_couple")]]
    await update.callback_query.edit_message_text("Людей:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['count'] = update.callback_query.data.split('_')[1]
    kb = [[InlineKeyboardButton(v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    await update.callback_query.edit_message_text("Стиль:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['style'] = update.callback_query.data.split('_')[1]
    kb = [[InlineKeyboardButton(v, callback_data=f"scene_{k}")] for k, v in SCENES.items()]
    await update.callback_query.edit_message_text("Место:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['scene'] = update.callback_query.data.split('_')[1]
    if context.user_data.get('count') == 'single':
        kb = [[InlineKeyboardButton("👨 М", callback_data="g_man"), InlineKeyboardButton("👩 Ж", callback_data="g_woman")]]
        await update.callback_query.edit_message_text("Пол:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        context.user_data['gender'] = 'mixed'
        await update.callback_query.edit_message_text("Пришли фото!")

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = update.callback_query.data.split('_')[1]
    await update.callback_query.edit_message_text("Пришли фото!")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if 'theme' not in context.user_data: return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            if cur.fetchone()['credits'] <= 0 and uid != ADMIN_ID:
                await update.message.reply_text("Нет кредитов!"); return
            if uid != ADMIN_ID: cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (uid,))
            conn.commit()

    msg = await update.message.reply_text("⏳ Генерирую...")
    photo = await update.message.photo[-1].get_file()
    u_b64 = base64.b64encode(await photo.download_as_bytearray()).decode('utf-8')

    url = await generate_template_leonardo(context.user_data['theme'], context.user_data['style'], context.user_data['scene'], context.user_data['count'], context.user_data['gender'], context.user_data['orientation'])
    if not url:
        await msg.edit_text("Ошибка Leonardo."); return

    res = await faceswap_runpod(url, u_b64)
    if not res:
        await msg.edit_text("Ошибка Swap."); return

    await update.message.reply_photo(res, caption="Готово!")
    await msg.delete()
    context.user_data.clear()

async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    await update.callback_query.edit_message_text("Купить:", reply_markup=InlineKeyboardMarkup(kb))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pid = update.callback_query.data.split('_')[1]
    pkg = PACKAGES[pid]
    pay = Payment.create({"amount": {"value": str(pkg['price']), "currency": "RUB"}, "confirmation": {"type": "redirect", "return_url": "https://t.me/bot"}, "metadata": {"u": update.effective_user.id, "c": pkg['count']}}, uuid.uuid4())
    kb = [[InlineKeyboardButton("💳 Оплатить", url=pay.confirmation.confirmation_url)]]
    await update.callback_query.edit_message_text(f"Счет на {pkg['price']}₽", reply_markup=InlineKeyboardMarkup(kb))

def main():
    init_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(create_postcard, pattern="create_postcard"))
    app.add_handler(CallbackQueryHandler(handle_orientation, pattern="orient_"))
    app.add_handler(CallbackQueryHandler(handle_theme, pattern="theme_"))
    app.add_handler(CallbackQueryHandler(handle_count, pattern="cnt_"))
    app.add_handler(CallbackQueryHandler(handle_style, pattern="style_"))
    app.add_handler(CallbackQueryHandler(handle_scene, pattern="scene_"))
    app.add_handler(CallbackQueryHandler(handle_gender, pattern="g_"))
    app.add_handler(CallbackQueryHandler(show_pricing, pattern="show_pricing"))
    app.add_handler(CallbackQueryHandler(buy_package, pattern="buy_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
