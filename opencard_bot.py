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
from PIL import Image, ImageDraw, ImageFont, ImageOps
import io

# --- 1. НАСТРОЙКИ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "vzsuz6ygs5m4ly")
ADMIN_ID = int(os.getenv("ADMIN_ID", "610820340"))

YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
if YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

CONGRATS_TEXTS = {
    "new_year": "С Новым Годом и Рождеством!",
    "winter": "Снежного счастья и мирного неба!",
    "feb_23": "С Днем Защитника Отечества!",
    "mar_8": "С 8 Марта!"
}

PACKAGES = {"1": {"name": "1 открытка", "price": 149, "cnt": 1}, "3": {"name": "3 открытки", "price": 399, "cnt": 3}}

# --- 2. БАЗА ДАННЫХ ---
def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, credits INT DEFAULT 1, username TEXT)")
            cur.execute("CREATE TABLE IF NOT EXISTS payments (id SERIAL PRIMARY KEY, user_id BIGINT, payment_id TEXT, status TEXT, amount INT, count INT)")
            conn.commit()

# --- 3. ГРАФИКА ---
def draw_card(image_bytes, theme):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(30, 30, 30, 80), fill='#FDFDFD')
    draw = ImageDraw.Draw(img)
    w, h = img.size
    txt = CONGRATS_TEXTS.get(theme, "Поздравляем!")
    try:
        font = ImageFont.truetype("font.ttf", 40)
    except:
        font = ImageFont.load_default()
    draw.text((w/2, h-40), txt, font=font, fill="#6D4C41", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()

# --- 4. НЕЙРОСЕТИ ---
async def generate_base(theme, orient):
    prompt = f"Vintage Soviet postcard style, winter, festive, masterpiece, high quality."
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "width": 1024 if orient == "h" else 768, "height": 768 if orient == "h" else 1024, "alchemy": True}
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        gid = r.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(40):
            await asyncio.sleep(3)
            res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gid}", headers=headers).json()
            job = res.get("generations_by_pk") or (res.get("generations")[0] if res.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except: return None

async def swap_face(target_url, user_b64):
    try:
        t_resp = requests.get(target_url, timeout=20)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
        # Просим воркер НЕ делать апскейл, чтобы избежать ошибки 400
        payload = {"input": {"source_image": user_b64, "target_image": t_b64, "face_restorer_name": "None", "upscale": 1}}
        
        r = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        jid = r.get("id")
        if not jid: return None

        for _ in range(60):
            await asyncio.sleep(3)
            res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{jid}", headers=headers).json()
            if res.get("status") == "COMPLETED":
                out = res.get("output")
                img = out.get("image") if isinstance(out, dict) else out
                if img:
                    if "," in img: img = img.split(",")[1]
                    return base64.b64decode(img)
                return None
            if res.get("status") in ["FAILED", "CANCELLED"]: return None
    except: return None

# --- 5. БОТ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (uid, update.effective_user.username))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            res = cur.fetchone()
            c = res['credits'] if res else 0
    kb = [[InlineKeyboardButton("🎨 Создать", callback_data="start_gen")], [InlineKeyboardButton("💰 Пополнить", callback_data="buy")]]
    await update.message.reply_text(f"🎫 Кредитов: {c}", reply_markup=InlineKeyboardMarkup(kb))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.data == "start_gen":
        kb = [[InlineKeyboardButton("🖼 Гор.", callback_data="o_h"), InlineKeyboardButton("📱 Вер.", callback_data="o_v")]]
        await q.edit_message_text("Формат:", reply_markup=InlineKeyboardMarkup(kb))
    elif q.data.startswith("o_"):
        context.user_data['o'] = q.data[2:]
        kb = [[InlineKeyboardButton(v, callback_data=f"t_{k}")] for k, v in CONGRATS_TEXTS.items()]
        await q.edit_message_text("Тема:", reply_markup=InlineKeyboardMarkup(kb))
    elif q.data.startswith("t_"):
        context.user_data['t'] = q.data[2:]
        await q.edit_message_text("📸 Отправьте селфи.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if 't' not in context.user_data: return
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            res = cur.fetchone()
            if (not res or res['credits'] <= 0) and uid != ADMIN_ID:
                await update.message.reply_text("Нет кредитов."); return
            if uid != ADMIN_ID: cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (uid,))
            conn.commit()

    m = await update.message.reply_text("⏳ Магия (1-2 мин)...")
    try:
        photo = await update.message.photo[-1].get_file()
        u_b64 = base64.b64encode(await photo.download_as_bytearray()).decode('utf-8')
        
        url = await generate_base(context.user_data['t'], context.user_data['o'])
        res_bytes = await swap_face(url, u_b64)
        
        if res_bytes:
            final = draw_card(res_bytes, context.user_data['t'])
            await update.message.reply_photo(final, caption="✨ Готово!")
        else:
            raise Exception("Fail")
    except:
        await m.edit_text("❌ Ошибка. Кредит возвращен.")
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (uid,))
                conn.commit()
    context.user_data.clear()

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
