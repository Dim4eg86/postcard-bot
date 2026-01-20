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

# --- БАЗА ДАННЫХ ---
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
            """)
            conn.commit()

# --- ЛОГИКА НЕЙРОСЕТЕЙ ---
async def generate_template_leonardo(theme, style, scene, count, gender, orientation):
    subj = "Couple" if count == "couple" else ("Family" if count == "family" else ("Man" if gender == "man" else "Woman"))
    style_p = {"ussr": "Soviet gouache painting", "vintage": "Vintage oil painting", "modern": "Digital art illustration"}.get(style)
    prompt = f"{style_p}, {subj} in {scene}, theme {THEMES.get(theme, 'Holiday')}, festive atmosphere, high quality, masterpiece."
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    
    # ИСПОЛЬЗУЕМ СТАБИЛЬНУЮ МОДЕЛЬ Leonardo Diffusion XL
    payload = {
        "prompt": prompt,
        "modelId": "b24e92ff-382e-4590-88f0-c1d91fa3f2ec", 
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768,
        "num_images": 1,
        "alchemy": True,
        "presetStyle": "DYNAMIC"
    }
    
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        res_json = r.json()
        if r.status_code != 200:
            logger.error(f"Leonardo API Error: {res_json}")
            return None
            
        gid = res_json.get("sdGenerationJob", {}).get("generationId")
        for _ in range(45):
            await asyncio.sleep(4)
            st = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gid}", headers=headers).json()
            gen = st.get("generations_by_pk") or (st.get("generations")[0] if st.get("generations") else None)
            if gen and gen.get("status") == "COMPLETE":
                return gen.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo exception: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        t_resp = requests.get(template_url)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        payload = {"input": {"source_image": user_photo_b64, "target_image": t_b64, "face_restore_model": "CodeFormer"}}
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
        
        res = requests.post(f"{RUNPOD_API_URL}/run", json=payload, headers=headers).json()
        job_id = res.get("id")
        
        # Ждем до 5 минут для холодного старта (100 итераций по 3 сек)
        for _ in range(100):
            await asyncio.sleep(3)
            s_res = requests.get(f"{RUNPOD_API_URL}/status/{job_id}", headers=headers).json()
            if s_res.get("status") == "COMPLETED":
                out = s_res.get("output")
                img_data = out if isinstance(out, str) else out.get("image")
                return base64.b64decode(img_data)
            if s_res.get("status") == "FAILED":
                logger.error(f"RunPod FAILED: {s_res}")
                return None
    except Exception as e:
        logger.error(f"RunPod exception: {e}")
    return None

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING", (user.id, user.username, user.first_name))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user.id,))
            c = cur.fetchone()['credits']
    kb = [[InlineKeyboardButton("🎨 Создать открытку", callback_data="create_p")],
          [InlineKeyboardButton("💰 Пополнить", callback_data="pricing")]]
    txt = f"Привет, {user.first_name}! Баланс: {c} 🎫"
    if update.callback_query: await update.callback_query.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(kb))
    else: await update.message.reply_text(txt, reply_markup=InlineKeyboardMarkup(kb))

async def create_p(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📱 Вертикаль", callback_data="o_v"), InlineKeyboardButton("🖼️ Горизонт", callback_data="o_h")]]
    await update.callback_query.edit_message_text("Формат:", reply_markup=InlineKeyboardMarkup(kb))

async def h_orient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['orientation'] = "vertical" if update.callback_query.data == "o_v" else "horizontal"
    kb = [[InlineKeyboardButton(v, callback_data=f"t_{k}")] for k, v in THEMES.items()]
    await update.callback_query.edit_message_text("Тема:", reply_markup=InlineKeyboardMarkup(kb))

async def h_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['theme'] = update.callback_query.data.replace("t_", "")
    kb = [[InlineKeyboardButton("👤 Один", callback_data="c_s"), InlineKeyboardButton("👩‍❤️‍👨 Пара", callback_data="c_c")]]
    await update.callback_query.edit_message_text("Людей:", reply_markup=InlineKeyboardMarkup(kb))

async def h_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['count'] = "single" if update.callback_query.data == "c_s" else "couple"
    kb = [[InlineKeyboardButton(v, callback_data=f"s_{k}")] for k, v in STYLES.items()]
    await update.callback_query.edit_message_text("Стиль:", reply_markup=InlineKeyboardMarkup(kb))

async def h_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['style'] = update.callback_query.data.replace("s_", "")
    kb = [[InlineKeyboardButton(v, callback_data=f"sc_{k}")] for k, v in SCENES.items()]
    await update.callback_query.edit_message_text("Место:", reply_markup=InlineKeyboardMarkup(kb))

async def h_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['scene'] = update.callback_query.data.replace("sc_", "")
    if context.user_data['count'] == "single":
        kb = [[InlineKeyboardButton("👨 М", callback_data="g_m"), InlineKeyboardButton("👩 Ж", callback_data="g_w")]]
        await update.callback_query.edit_message_text("Пол:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        context.user_data['gender'] = 'mixed'
        await update.callback_query.edit_message_text("📸 Пришлите фото.")

async def h_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = "man" if update.callback_query.data == "g_m" else "woman"
    await update.callback_query.edit_message_text("📸 Пришлите фото.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if 'theme' not in context.user_data: return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            res = cur.fetchone()
            if (not res or res['credits'] <= 0) and uid != ADMIN_ID:
                await update.message.reply_text("❌ Нет кредитов."); return
            if uid != ADMIN_ID: cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (uid,))
            conn.commit()

    m = await update.message.reply_text("⏳ Генерирую фон...")
    try:
        f = await update.message.photo[-1].get_file()
        u_b64 = base64.b64encode(await f.download_as_bytearray()).decode('utf-8')
        url = await generate_template_leonardo(context.user_data['theme'], context.user_data['style'], context.user_data['scene'], context.user_data['count'], context.user_data['gender'], context.user_data['orientation'])
        
        if not url:
            await m.edit_text("❌ Ошибка Leonardo. Возврат кредита."); 
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (uid,))
                conn.commit()
            return

        await m.edit_text("🔄 Вклеиваю лицо (ждем прогрева RunPod)...")
        res = await faceswap_runpod(url, u_b64)
        if not res:
            await m.edit_text("❌ Ошибка Face Swap. Возврат кредита.");
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (uid,))
                conn.commit()
            return

        await update.message.reply_photo(res, caption="Готово! ✨")
        await m.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(e)

async def pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    await update.callback_query.edit_message_text("Купить:", reply_markup=InlineKeyboardMarkup(kb))

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pk = PACKAGES[update.callback_query.data.replace("buy_", "")]
    pay = Payment.create({"amount": {"value": str(pk['price']), "currency": "RUB"}, "confirmation": {"type": "redirect", "return_url": "https://t.me/bot"}, "metadata": {"u": update.effective_user.id, "c": pk['count']}}, uuid.uuid4())
    kb = [[InlineKeyboardButton("💳 Оплатить", url=pay.confirmation.confirmation_url)]]
    await update.callback_query.edit_message_text(f"К оплате {pk['price']}₽", reply_markup=InlineKeyboardMarkup(kb))

def main():
    init_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(create_p, pattern="^create_p$"))
    app.add_handler(CallbackQueryHandler(h_orient, pattern="^o_"))
    app.add_handler(CallbackQueryHandler(h_theme, pattern="^t_"))
    app.add_handler(CallbackQueryHandler(h_count, pattern="^c_"))
    app.add_handler(CallbackQueryHandler(h_style, pattern="^s_"))
    app.add_handler(CallbackQueryHandler(h_scene, pattern="^sc_"))
    app.add_handler(CallbackQueryHandler(h_gender, pattern="^g_"))
    app.add_handler(CallbackQueryHandler(pricing, pattern="^pricing$"))
    app.add_handler(CallbackQueryHandler(buy, pattern="^buy_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
