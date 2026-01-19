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

# --- ЛОГИКА ГЕНЕРАЦИИ (LEONARDO + RUNPOD) ---
async def generate_template_leonardo(theme, style, scene, count, gender, orientation):
    subj = "Couple" if count == "couple" else ("Family" if count == "family" else ("Man" if gender == "man" else "Woman"))
    style_prompt = {"ussr": "Soviet 1970s gouache painting postcard", "vintage": "Classic oil painting", "modern": "Digital illustration"}.get(style)
    
    prompt = f"{style_prompt}, {subj} in {scene}, theme {THEMES.get(theme, theme)}, festive atmosphere, highly detailed, sharp faces."
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a", # Vision XL
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768,
        "num_images": 1
    }
    
    try:
        resp = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        if resp.status_code != 200:
            logger.error(f"Leonardo Error: {resp.text}")
            return None
        
        gen_id = resp.json().get("sdGenerationJob", {}).get("generationId")
        if not gen_id: return None

        for _ in range(50):
            await asyncio.sleep(4)
            status_res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers).json()
            job = status_res.get("generations_by_pk") or (status_res.get("generations")[0] if status_res.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo Exception: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        t_resp = requests.get(template_url)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        
        payload = {"input": {"source_image": user_photo_b64, "target_image": t_b64, "face_restore_model": "CodeFormer"}}
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
        
        res = requests.post(f"{RUNPOD_API_URL}/run", json=payload, headers=headers).json()
        job_id = res.get("id")
        
        for _ in range(50):
            await asyncio.sleep(3)
            status = requests.get(f"{RUNPOD_API_URL}/status/{job_id}", headers=headers).json()
            if status.get("status") == "COMPLETED":
                out = status.get("output")
                img_data = out if isinstance(out, str) else out.get("image")
                return base64.b64decode(img_data)
    except Exception as e:
        logger.error(f"RunPod Error: {e}")
    return None

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING", 
                        (user.id, user.username, user.first_name))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user.id,))
            credits = cur.fetchone()['credits']
            conn.commit()

    text = f"Привет, {user.first_name}! 🎨\nТвой баланс: {credits} открыток."
    keyboard = [
        [InlineKeyboardButton("🎨 Создать открытку", callback_data="create_postcard")],
        [InlineKeyboardButton("💰 Купить открытки", callback_data="show_pricing")],
        [InlineKeyboardButton("💬 Поддержка", callback_data="support")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

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
    keyboard = [[InlineKeyboardButton("👤 Один", callback_data="cnt_single")],
                [InlineKeyboardButton("👩‍❤️‍👨 Пара", callback_data="cnt_couple")],
                [InlineKeyboardButton("👨‍👩‍👧‍👦 Группа", callback_data="cnt_family")]]
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
        await update.callback_query.edit_message_text("📸 Отправьте фото (лицо должно быть четко видно).")

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = update.callback_query.data.split('_')[1]
    await update.callback_query.edit_message_text("📸 Теперь отправьте ваше фото.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if 'theme' not in context.user_data: return

    # Проверка и списание кредита
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            user_credits = cur.fetchone()['credits']
            if user_credits <= 0 and user_id != ADMIN_ID:
                await update.message.reply_text("❌ Нет кредитов. Пополните баланс.")
                return
            if user_id != ADMIN_ID:
                cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (user_id,))
            conn.commit()

    msg = await update.message.reply_text("⏳ Шаг 1: Генерирую фон (40-60 сек)...")
    
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
            await msg.edit_text("❌ Ошибка Leonardo. Кредит возвращен."); 
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (user_id,))
                conn.commit()
            return

        await msg.edit_text("⏳ Шаг 2: Вклеиваю лицо...")
        final_img = await faceswap_runpod(t_url, u_b64)
        
        if not final_img:
            await msg.edit_text("❌ Ошибка Face Swap. Кредит возвращен.");
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (user_id,))
                conn.commit()
            return

        await update.message.reply_photo(photo=final_img, caption="Готово! ✨")
        await msg.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("⚠️ Ошибка системы.")

# --- ПЛАТЕЖИ И ПОДДЕРЖКА ---
async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_start")])
    await update.callback_query.edit_message_text("Выберите пакет:", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE
