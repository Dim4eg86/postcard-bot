import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import logging
import psycopg
from psycopg.rows import dict_row
import uuid
import asyncio
import base64
from PIL import Image, ImageDraw, ImageFont, ImageOps
import io

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация из Railway
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "vzsuz6ygs5m4ly")
ADMIN_ID = int(os.getenv("ADMIN_ID", "610820340"))

CONGRATS_TEXTS = {
    "new_year": "С Новым Годом и Рождеством!",
    "winter": "Снежного счастья и мирного неба!",
    "mar_8": "С праздником 8 Марта!"
}

def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, credits INT DEFAULT 1, username TEXT)")
            conn.commit()

# --- ФУНКЦИИ НЕЙРОСЕТЕЙ ---

async def generate_leonardo(theme):
    prompt = f"Vintage Soviet postcard style illustration, {theme}, winter, artistic realism, high quality."
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "width": 768, "height": 1024, "alchemy": True, "num_images": 1}
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        gen_id = r.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(30):
            await asyncio.sleep(3)
            res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers).json()
            images = res.get("generations_by_pk", {}).get("generated_images", [])
            if images: return images[0].get("url")
        return None
    except Exception as e:
        logger.error(f"Leonardo error: {e}")
        return None

async def swap_face(target_url, user_b64):
    try:
        t_resp = requests.get(target_url, timeout=20)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
        # Облегченный запрос
        payload = {
            "input": {
                "source_image": user_b64,
                "target_image": t_b64,
                "face_restorer_name": "None",
                "upscale": 1
            }
        }
        
        run_res = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        job_id = run_res.get("id")
        if not job_id: return None

        for _ in range(60):
            await asyncio.sleep(3)
            status_res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}", headers=headers).json()
            if status_res.get("status") == "COMPLETED":
                output = status_res.get("output")
                img_data = output.get("image") if isinstance(output, dict) else output
                if img_data:
                    if "," in img_data: img_data = img_data.split(",")[1]
                    return base64.b64decode(img_data)
            if status_res.get("status") in ["FAILED", "CANCELLED"]:
                logger.error(f"RunPod job failed: {status_res}")
                return None
        return None
    except Exception as e:
        logger.error(f"Swap error: {e}")
        return None

def add_text_to_image(image_bytes, theme_key):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(20, 20, 20, 70), fill='white')
    draw = ImageDraw.Draw(img)
    text = CONGRATS_TEXTS.get(theme_key, "С Праздником!")
    try:
        font = ImageFont.truetype("font.ttf", 45)
    except:
        font = ImageFont.load_default()
    w, h = img.size
    draw.text((w/2, h-40), text, font=font, fill="black", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()

# --- ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    init_db()
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (uid, update.effective_user.username))
            conn.commit()
    await update.message.reply_text("👋 Пришлите фото (селфи), чтобы создать ретро-открытку!")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ Начинаю работу...")
    try:
        photo_file = await update.message.photo[-1].get_file()
        user_b64 = base64.b64encode(await photo_file.download_as_bytearray()).decode('utf-8')
        
        # 1. Генерация основы
        await m.edit_text("🎨 Рисую фон (Leonardo)...")
        bg_url = await generate_leonardo("New Year celebration")
        if not bg_url: raise Exception("Leonardo failed")
        
        # 2. Замена лица
        await m.edit_text("👤 Накладываю лицо (RunPod)...")
        swapped_bytes = await swap_face(bg_url, user_b64)
        if not swapped_bytes: raise Exception("Swap failed")
        
        # 3. Финальный штрих
        final_img = add_text_to_image(swapped_bytes, "new_year")
        await update.message.reply_photo(final_img, caption="✨ Ваша открытка готова!")
        await m.delete()
        
    except Exception as e:
        logger.error(f"General error: {e}")
        await m.edit_text("❌ Произошла ошибка. Проверьте переменные в RunPod и попробуйте снова.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
