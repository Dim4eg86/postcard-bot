import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import logging
import psycopg
from psycopg.rows import dict_row
import asyncio
import base64
from PIL import Image, ImageDraw, ImageFont, ImageOps
import io

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ENV
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "vzsuz6ygs5m4ly")
ADMIN_ID = int(os.getenv("ADMIN_ID", "610820340"))

def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, credits INT DEFAULT 1, username TEXT)")
            conn.commit()

async def generate_leonardo():
    prompt = "Nostalgic Soviet era postcard illustration, winter, holiday spirit, high quality art."
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "width": 768, "height": 1024, "alchemy": True}
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        gid = r.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(40):
            await asyncio.sleep(3)
            res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gid}", headers=headers).json()
            imgs = res.get("generations_by_pk", {}).get("generated_images", [])
            if imgs: return imgs[0].get("url")
    except: return None

async def swap_face(target_url, user_b64):
    try:
        t_resp = requests.get(target_url, timeout=20)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
        # Просим воркер НЕ делать апскейл и использовать S3 для вывода
        payload = {
            "input": {
                "source_image": user_b64,
                "target_image": t_b64,
                "face_restore": False,
                "upscale": 1
            }
        }
        
        # Отправка задачи
        r = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        jid = r.get("id")
        if not jid: return None

        # Опрос статуса
        for _ in range(60):
            await asyncio.sleep(4)
            res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{jid}", headers=headers).json()
            if res.get("status") == "COMPLETED":
                output = res.get("output")
                # Если воркер вернул ссылку на S3 вместо base64
                if isinstance(output, dict) and output.get("image_url"):
                    return requests.get(output.get("image_url")).content
                # Если воркер всё же прислал base64
                img_data = output.get("image") if isinstance(output, dict) else output
                if img_data:
                    if "," in img_data: img_data = img_data.split(",")[1]
                    return base64.b64decode(img_data)
            if res.get("status") in ["FAILED", "CANCELLED"]: return None
    except Exception as e:
        logger.error(f"Swap Error: {e}")
        return None

def finalize_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(20, 20, 20, 70), fill='white')
    draw = ImageDraw.Draw(img)
    try: font = ImageFont.truetype("font.ttf", 42)
    except: font = ImageFont.load_default()
    draw.text((img.size[0]/2, img.size[1]-35), "С Новым Годом!", font=font, fill="black", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("⏳ Магия в процессе...")
    try:
        file = await update.message.photo[-1].get_file()
        u_b64 = base64.b64encode(await file.download_as_bytearray()).decode('utf-8')
        
        bg = await generate_leonardo()
        swapped = await swap_face(bg, u_b64)
        
        if swapped:
            final = finalize_image(swapped)
            await update.message.reply_photo(final, caption="✨ Ваша открытка готова!")
            await m.delete()
        else:
            await m.edit_text("❌ Воркер не смог вернуть фото. Проверьте переменную S3 в RunPod.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await m.edit_text("❌ Ошибка.")

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Пришлите фото!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
