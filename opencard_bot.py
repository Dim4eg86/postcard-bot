import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import logging
import psycopg
from psycopg.rows import dict_row
from datetime import datetime
from yookassa import Configuration, Payment
import uuid
import asyncio
import base64
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
import io
import numpy as np

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены и настройки
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID", "610820340"))
DATABASE_URL = os.getenv("DATABASE_URL")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
LEONARDO_API_URL = "https://cloud.leonardo.ai/api/rest/v1"
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "vzsuz6ygs5m4ly")
RUNPOD_API_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}"

if YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

# Справочники
THEMES = {
    "new_year": "🎄 Новый Год",
    "feb_14": "❤️ 14 Февраля",
    "feb_23": "🎖 23 Февраля",
    "mar_8": "💐 8 Марта",
    "winter": "❄️ Зима"
}
STYLES = {"ussr": "СССР (Гуашь)", "vintage": "Винтаж (Масло)", "modern": "Модерн"}
SCENES = {"night_street": "🌙 Улица", "pine_forest": "🌲 Лес", "winter_fair": "🎪 Ярмарка"}
PACKAGES = {"1": 149, "3": 399, "5": 599}

# --- БД (PostgreSQL) ---
def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_database():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    credits INTEGER DEFAULT 1,
                    total_generated INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    payment_id TEXT,
                    status TEXT,
                    count INTEGER
                );
            """)
            conn.commit()

def use_credit(user_id):
    if user_id == ADMIN_ID: return True
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s AND credits > 0 RETURNING credits", (user_id,))
            res = cur.fetchone()
            conn.commit()
            return res is not None

def add_credits(user_id, amount):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()

# --- ЛОГИКА НЕЙРОСЕТЕЙ ---

def get_mvp_prompt(theme, style, scene, count, gender):
    if count == "couple": subject = "A romantic couple (man and woman)"
    elif count == "family": subject = "A happy family group with children"
    else: subject = "A handsome man" if gender == "man" else "A beautiful woman"

    style_desc = {
        "ussr": "Soviet 1970s postcard style, gouache painting, nostalgic texture.",
        "vintage": "Classic oil painting, warm rich colors, 19th century aesthetic.",
        "modern": "Digital art illustration, festive colors, clean style."
    }.get(style, "painted illustration")

    theme_map = {
        "new_year": "New Year, spruce branches, snow sparkles",
        "feb_14": "Valentine's day, hearts, romantic pink/red tones",
        "feb_23": "February 23, patriotic military winter aesthetic",
        "mar_8": "March 8, spring flowers, tulips, mimosa",
        "winter": "Winter wonderland, snowy landscape"
    }
    
    return f"{style_desc}. {subject} at {scene}. {theme_map.get(theme)}. Clear faces, artistic masterpiece, NOT a photo."

async def generate_template_leonardo(theme, style, scene, count, gender, orientation):
    prompt = get_mvp_prompt(theme, style, scene, count, gender)
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a",
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768,
        "num_images": 1
    }
    try:
        resp = requests.post(f"{LEONARDO_API_URL}/generations", json=payload, headers=headers).json()
        gen_id = resp.get("sdGenerationJob", {}).get("generationId")
        if not gen_id: return None

        for _ in range(60): # Ждем до 3 минут
            await asyncio.sleep(3)
            res = requests.get(f"{LEONARDO_API_URL}/generations/{gen_id}", headers=headers).json()
            job = res.get("generations_by_pk") or (res.get("generations")[0] if res.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo Error: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        template_bytes = requests.get(template_url).content
        template_b64 = base64.b64encode(template_bytes).decode('utf-8')
        payload = {
            "input": {
                "source_image": user_photo_b64,
                "target_image": template_b64,
                "face_restore_model": "CodeFormer",
                "upscale": 1
            }
        }
        res = requests.post(f"{RUNPOD_API_URL}/run", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}, json=payload).json()
        job_id = res.get("id")
        for _ in range(60):
            await asyncio.sleep(2)
            status = requests.get(f"{RUNPOD_API_URL}/status/{job_id}", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}).json()
            if status.get("status") == "COMPLETED":
                output = status.get("output")
                img_str = output if isinstance(output, str) else output.get("image")
                return base64.b64decode(img_str)
    except Exception as e:
        logger.error(f"RunPod Error: {e}")
    return None

# --- ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (user_id, update.effective_user.username))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            credits = cur.fetchone()['credits']
            conn.commit()

    text = f"Привет! Твой баланс: {credits} открыток."
    keyboard = [[InlineKeyboardButton("🎨 Создать открытку", callback_data="create_postcard")],
                [InlineKeyboardButton("💰 Пополнить баланс", callback_data="show_pricing")]]
    
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def create_postcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📱 Вертикальная", callback_data="orient_vertical"), 
                 InlineKeyboardButton("🖼️ Горизонтальная", callback_data="orient_horizontal")]]
    await update.callback_query.edit_message_text("Выберите формат:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['orientation'] = update.callback_query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"theme_{k}")] for k, v in THEMES.items()]
    await update.callback_query.edit_message_text("Выберите праздник:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['theme'] = update.callback_query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton("👤 Один", callback_data="cnt_single"), 
                 InlineKeyboardButton("👩‍❤️‍👨 Пара", callback_data="cnt_couple"), 
                 InlineKeyboardButton("👨‍👩‍👧‍👦 Группа", callback_data="cnt_family")]]
    await update.callback_query.edit_message_text("Сколько людей на фото?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['count'] = update.callback_query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    await update.callback_query.edit_message_text("Выберите стиль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['style'] = update.callback_query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"scene_{k}")] for k, v in SCENES.items()]
    await update.callback_query.edit_message_text("Выберите место:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['scene'] = update.callback_query.data.split('_')[1]
    if context.user_data['count'] == 'single':
        keyboard = [[InlineKeyboardButton("👨 Мужчина", callback_data="g_man"), InlineKeyboardButton("👩 Женщина", callback_data="g_woman")]]
        await update.callback_query.edit_message_text("Укажите пол:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        context.user_data['gender'] = 'mixed'
        await update.callback_query.edit_message_text("📸 Теперь отправь фото.")

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = "man" if "man" in update.callback_query.data else "woman"
    await update.callback_query.edit_message_text("📸 Теперь отправь фото.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not use_credit(user_id):
        await update.message.reply_text("❌ Нет открыток на балансе.")
        return

    msg = await update.message.reply_text("⏳ Магия началась! Готовлю шаблон (шаг 1/2)...")
    
    try:
        photo_file = await update.message.photo[-1].get_file()
        p_bytes = await photo_file.download_as_bytearray()
        u_b64 = base64.b64encode(p_bytes).decode('utf-8')

        t_url = await generate_template_leonardo(
            context.user_data['theme'], context.user_data['style'],
            context.user_data['scene'], context.user_data['count'],
            context.user_data['gender'], context.user_data['orientation']
        )
        
        if not t_url:
            await msg.edit_text("❌ Ошибка Leonardo (тайм-аут). Кредит возвращен.")
            add_credits(user_id, 1); return

        await msg.edit_text("🔄 Вклеиваю лица (шаг 2/2)...")
        final_bytes = await faceswap_runpod(t_url, u_b64)
        
        if not final_bytes:
            await msg.edit_text("❌ Ошибка Face Swap. Кредит возвращен."); add_credits(user_id, 1); return

        await update.message.reply_photo(photo=final_bytes, caption="Ваша открытка готова! ✨")
        await msg.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(e); await update.message.reply_text("⚠️ Ошибка системы."); add_credits(user_id, 1)

# --- ПЛАТЕЖИ ---
async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"{v} открыток - {PACKAGES[v]}₽", callback_data=f"buy_{v}")] for v in PACKAGES]
    await update.callback_query.edit_message_text("Выберите пакет:", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    count = update.callback_query.data.split('_')[1]
    price = PACKAGES[count]
    payment = Payment.create({
        "amount": {"value": str(price), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/your_bot_user"},
        "metadata": {"user_id": update.effective_user.id, "count": count}
    }, uuid.uuid4())
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO payments (user_id, payment_id, status, count) VALUES (%s, %s, %s, %s)",
                        (update.effective_user.id, payment.id, 'pending', count))
            conn.commit()
    
    keyboard = [[InlineKeyboardButton("💳 Оплатить", url=payment.confirmation.confirmation_url)]]
    await update.callback_query.edit_message_text(f"Счет на {price}₽ создан.", reply_markup=InlineKeyboardMarkup(keyboard))

# --- ЗАПУСК ---
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
