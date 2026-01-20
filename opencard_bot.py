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
import io

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "vzsuz6ygs5m4ly")
ADMIN_ID = int(os.getenv("ADMIN_ID", "610820340"))

# Настройки ЮKassa (если есть)
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
if YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

# --- СПРАВОЧНИКИ ---
THEMES = {"new_year": "🎄 Новый Год", "feb_14": "❤️ 14 Февраля", "feb_23": "🎖 23 Февраля", "mar_8": "💐 8 Марта", "winter": "❄️ Зима"}
STYLES = {"ussr": "СССР (Гуашь)", "vintage": "Винтаж (Масло)", "modern": "Модерн"}
SCENES = {"night_street": "🌙 Улица", "pine_forest": "🌲 Лес", "winter_fair": "🎪 Ярмарка"}
PACKAGES = {"1": {"name": "1 шт", "price": 149, "cnt": 1}, "3": {"name": "3 шт", "price": 399, "cnt": 3}}

# --- БАЗА ДАННЫХ ---
def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, credits INT DEFAULT 1)")
            conn.commit()

# --- НЕЙРОСЕТИ ---
async def generate_leonardo(theme, style, scene, count, gender, orientation):
    subj = "Couple" if count == "couple" else ("Man" if gender == "man" else "Woman")
    prompt = f"{style} painting of {subj} in {scene}, theme {theme}, high resolution, detailed faces."
    
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a", # Актуальная Vision XL
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768,
        "alchemy": True,
        "num_images": 1
    }
    
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        if r.status_code != 200:
            logger.error(f"Leonardo Error {r.status_code}: {r.text}")
            return None
            
        gen_id = r.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(40):
            await asyncio.sleep(5)
            status = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers).json()
            # Проверка разных путей в JSON ответе
            data = status.get("generations_by_pk") or (status.get("generations")[0] if status.get("generations") else None)
            if data and data.get("status") == "COMPLETE":
                return data.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo exception: {e}")
    return None

async def swap_face(t_url, u_b64):
    """Оптимизировано под 0 воркеров RunPod (Serverless)"""
    try:
        t_b64 = base64.b64encode(requests.get(t_url).content).decode('utf-8')
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
        payload = {"input": {"source_image": u_b64, "target_image": t_b64, "face_restore_model": "CodeFormer"}}
        
        run = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        job_id = run.get("id")
        
        for _ in range(100): # Ждем до 5 минут для холодного старта
            await asyncio.sleep(3)
            res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}", headers=headers).json()
            if res.get("status") == "COMPLETED":
                out = res.get("output")
                return base64.b64decode(out if isinstance(out, str) else out.get("image"))
            if res.get("status") == "FAILED": return None
    except: return None
    return None

# --- ТЕЛЕГРАМ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (uid,))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            creds = cur.fetchone()['credits']
    kb = [[InlineKeyboardButton("🎨 Создать", callback_data="btn_create")], [InlineKeyboardButton("💰 Баланс", callback_data="btn_buy")]]
    txt = f"Ваш баланс: {creds} 🎫"
    if update.callback_query: await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else: await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))

async def process_steps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "btn_create":
        kb = [[InlineKeyboardButton("📱 Вертикаль", callback_data="o_v"), InlineKeyboardButton("🖼 Горизонт", callback_data="o_h")]]
        await query.edit_message_text("Формат:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("o_"):
        context.user_data['orient'] = "vertical" if data == "o_v" else "horizontal"
        kb = [[InlineKeyboardButton(v, callback_data=f"t_{k}")] for k, v in THEMES.items()]
        await query.edit_message_text("Тема:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("t_"):
        context.user_data['theme'] = data[2:]
        kb = [[InlineKeyboardButton("👤 Один", callback_data="c_s"), InlineKeyboardButton("👩‍❤️‍👨 Пара", callback_data="c_p")]]
        await query.edit_message_text("Людей:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("c_"):
        context.user_data['count'] = "single" if data == "c_s" else "couple"
        kb = [[InlineKeyboardButton(v, callback_data=f"s_{k}")] for k, v in STYLES.items()]
        await query.edit_message_text("Стиль:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("s_"):
        context.user_data['style'] = data[2:]
        kb = [[InlineKeyboardButton(v, callback_data=f"sc_{k}")] for k, v in SCENES.items()]
        await query.edit_message_text("Место:", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("sc_"):
        context.user_data['scene'] = data[3:]
        if context.user_data['count'] == "single":
            kb = [[InlineKeyboardButton("👨 М", callback_data="g_m"), InlineKeyboardButton("👩 Ж", callback_data="g_w")]]
            await query.edit_message_text("Пол:", reply_markup=InlineKeyboardMarkup(kb))
        else:
            context.user_data['gender'] = "mixed"
            await query.edit_message_text("📸 Пришлите фото")
    elif data.startswith("g_"):
        context.user_data['gender'] = "man" if data == "g_m" else "woman"
        await query.edit_message_text("📸 Пришлите фото")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if 'theme' not in context.user_data: return
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            if cur.fetchone()['credits'] <= 0 and uid != ADMIN_ID:
                await update.message.reply_text("Пополните баланс!"); return
            if uid != ADMIN_ID: cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (uid,))
            conn.commit()

    m = await update.message.reply_text("⏳ Начинаю (около 2-3 мин)...")
    try:
        photo = await update.message.photo[-1].get_file()
        u_b64 = base64.b64encode(await photo.download_as_bytearray()).decode('utf-8')
        
        url = await generate_leonardo(context.user_data['theme'], context.user_data['style'], context.user_data['scene'], context.user_data['count'], context.user_data['gender'], context.user_data['orient'])
        if not url: 
            await m.edit_text("Ошибка фона. Возврат 🎫")
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (uid,))
                conn.commit()
            return
            
        await m.edit_text("🔄 Вклеиваю лицо...")
        res = await swap_face(url, u_b64)
        if not res: 
            await m.edit_text("Ошибка вклейки. Возврат 🎫")
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (uid,))
                conn.commit()
            return
            
        await update.message.reply_photo(res, caption="Готово! ✨")
        await m.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(e)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(start, pattern="btn_buy"))
    app.add_handler(CallbackQueryHandler(process_steps, pattern="^(btn_create|o_|t_|c_|s_|sc_|g_)"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
