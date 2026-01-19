import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены
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

# Словари
THEMES = {"new_year": "🎄 Новый Год", "feb_14": "❤️ 14 Февраля", "feb_23": "🎖 23 Февраля", "mar_8": "💐 8 Марта", "winter": "❄️ Зима"}
STYLES = {"ussr": "СССР (Гуашь)", "vintage": "Винтаж (Масло)", "modern": "Модерн"}
SCENES = {"night_street": "🌙 Улица", "pine_forest": "🌲 Лес", "winter_fair": "🎪 Ярмарка"}

# --- БД ---
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

# --- НЕЙРОСЕТИ ---

async def generate_template_leonardo(theme, style, scene, count, gender, orientation):
    # Промпт
    subj = "A couple" if count == "couple" else ("A family" if count == "family" else ("A man" if gender == "man" else "A woman"))
    style_d = {"ussr": "Soviet 1970s gouache postcard", "vintage": "19th century oil painting", "modern": "Digital art"}.get(style)
    prompt = f"{style_d}. {subj} at {scene}. Theme: {theme}. High quality, clear faces, festive atmosphere."
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a",
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768
    }
    
    try:
        r = requests.post(f"{LEONARDO_API_URL}/generations", json=payload, headers=headers).json()
        gid = r.get("sdGenerationJob", {}).get("generationId")
        if not gid: return None

        for i in range(50): # Увеличили до 50 попыток
            await asyncio.sleep(4)
            res = requests.get(f"{LEONARDO_API_URL}/generations/{gid}", headers=headers).json()
            job = res.get("generations_by_pk") or (res.get("generations")[0] if res.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
            logger.info(f"Leonardo status: {job.get('status') if job else 'waiting'} (attempt {i})")
    except Exception as e:
        logger.error(f"Leonardo Error: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        t_b64 = base64.b64encode(requests.get(template_url).content).decode('utf-8')
        payload = {"input": {"source_image": user_photo_b64, "target_image": t_b64, "face_restore_model": "CodeFormer"}}
        res = requests.post(f"{RUNPOD_API_URL}/run", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}, json=payload).json()
        jid = res.get("id")
        for i in range(50):
            await asyncio.sleep(3)
            status = requests.get(f"{RUNPOD_API_URL}/status/{jid}", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}).json()
            if status.get("status") == "COMPLETED":
                out = status.get("output")
                return base64.b64decode(out if isinstance(out, str) else out.get("image"))
            logger.info(f"RunPod status: {status.get('status')} (attempt {i})")
    except Exception as e:
        logger.error(f"RunPod Error: {e}")
    return None

# --- HANDLERS ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not use_credit(user_id):
        await update.message.reply_text("❌ Нет кредитов."); return

    msg = await update.message.reply_text("⏳ Шаг 1: Создаю фон в Leonardo...")
    try:
        photo = await update.message.photo[-1].get_file()
        p_bytes = await photo.download_as_bytearray()
        u_b64 = base64.b64encode(p_bytes).decode('utf-8')

        t_url = await generate_template_leonardo(
            context.user_data.get('theme'), context.user_data.get('style'),
            context.user_data.get('scene'), context.user_data.get('count'),
            context.user_data.get('gender'), context.user_data.get('orientation')
        )
        
        if not t_url:
            await msg.edit_text("❌ Leonardo не ответил. Кредит возвращен."); add_credits(user_id, 1); return

        await msg.edit_text("⏳ Шаг 2: Вклеиваю лицо в RunPod...")
        final = await faceswap_runpod(t_url, u_b64)
        
        if not final:
            await msg.edit_text("❌ Ошибка замены лица. Кредит возвращен."); add_credits(user_id, 1); return

        await update.message.reply_photo(photo=final, caption="Готово! ✨")
        await msg.delete()
    except Exception as e:
        logger.error(e); await update.message.reply_text("⚠️ Ошибка системы."); add_credits(user_id, 1)

# Остальные обработчики (start, темы и т.д.) - оставить из предыдущего кода

def main():
    init_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    # ... (регистрация всех CallbackQueryHandler как в прошлом коде)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Добавь сюда CallbackQueryHandler для каждой кнопки (theme_, style_, count_ и т.д.)
    app.run_polling()

if __name__ == "__main__":
    main()
