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

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---
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

# --- СПРАВОЧНИКИ ---
THEMES = {"new_year": "🎄 Новый Год", "feb_14": "❤️ 14 Февраля", "feb_23": "🎖 23 Февраля", "mar_8": "💐 8 Марта", "winter": "❄️ Зима"}
STYLES = {"ussr": "СССР (Гуашь)", "vintage": "Винтаж (Масло)", "modern": "Модерн"}
SCENES = {"night_street": "🌙 Улица", "pine_forest": "🌲 Лес", "winter_fair": "🎪 Ярмарка"}
PACKAGES = {"1": {"name": "1 открытка", "price": 149, "cnt": 1}, "3": {"name": "3 открытки", "price": 399, "cnt": 3}}

# --- БАЗА ДАННЫХ ---
def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, credits INT DEFAULT 1, username TEXT)")
            cur.execute("CREATE TABLE IF NOT EXISTS payments (id SERIAL PRIMARY KEY, user_id BIGINT, payment_id TEXT, status TEXT, amount INT, count INT)")
            conn.commit()

# --- ЛОГИКА НЕЙРОСЕТЕЙ ---
async def generate_leonardo(theme, style, scene, count, gender, orientation):
    subj = "Family with a child" if count == "couple" else ("Man" if gender == "man" else "Woman")
    
    style_configs = {
        "ussr": "Soviet 1950s holiday postcard style, gouache painting",
        "vintage": "19th century vintage postcard, oil painting style, warm colors",
        "modern": "Digital art, cinematic lighting, sharp focus"
    }
    
    current_style = style_configs.get(style, style_configs["vintage"])
    prompt = (
        f"{current_style}. {subj} standing in {scene}, looking at camera. "
        f"Theme: {THEMES.get(theme, theme)}. Snow, 8k, detailed faces."
    )
    
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    
    # ИСПОЛЬЗУЕМ LIGHTNING МОДЕЛЬ - ОНА САМАЯ СТАБИЛЬНАЯ ДЛЯ API
    payload = {
        "prompt": prompt,
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768,
        "num_images": 1,
        "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3", # Leonardo Lightning XL
        "alchemy": True,
        "presetStyle": "DYNAMIC"
    }
    
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        if r.status_code != 200:
            logger.error(f"Leonardo Start Error: {r.text}")
            return None
            
        gen_id = r.json().get("sdGenerationJob", {}).get("generationId")
        
        for _ in range(30): # Lightning работает быстро, 30 итераций хватит
            await asyncio.sleep(3)
            try:
                status_req = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers)
                data = status_req.json()
                job = data.get("generations_by_pk") or (data.get("generations")[0] if data.get("generations") else None)
                
                if job and job.get("status") == "COMPLETE":
                    return job.get("generated_images")[0].get("url")
                if job and job.get("status") == "FAILED": return None
            except: continue
    except Exception as e:
        logger.error(f"Leonardo Global Error: {e}")
    return None

async def swap_face(t_url, u_b64):
    try:
        t_resp = requests.get(t_url)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
        payload = {"input": {"source_image": u_b64, "target_image": t_b64}}
        
        run_res = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        job_id = run_res.get("id")
        if not job_id: return None

        for i in range(100):
            await asyncio.sleep(3)
            res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}", headers=headers).json()
            status = res.get("status")
            if status == "COMPLETED":
                out = res.get("output")
                img_data = out if isinstance(out, str) else (out.get("image") or out.get("result"))
                return base64.b64decode(img_data) if img_data else "FACE_NOT_FOUND"
            if status == "FAILED": return None
    except: return None
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (uid, update.effective_user.username))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            res = cur.fetchone()
            c = res['credits'] if res else 0
    kb = [[InlineKeyboardButton("🎨 Создать открытку", callback_data="go_create")], [InlineKeyboardButton("💰 Пополнить баланс", callback_data="go_pay")]]
    await update.message.reply_text(f"Привет! Баланс: {c} 🎫", reply_markup=InlineKeyboardMarkup(kb))

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data
    if d == "go_create":
        kb = [[InlineKeyboardButton("📱 Портрет", callback_data="o_v"), InlineKeyboardButton("🖼 Альбом", callback_data="o_h")]]
        await q.edit_message_text("Формат:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("o_"):
        context.user_data['orient'] = "vertical" if d == "o_v" else "horizontal"
        kb = [[InlineKeyboardButton(v, callback_data=f"t_{k}")] for k, v in THEMES.items()]
        await q.edit_message_text("Праздник:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("t_"):
        context.user_data['theme'] = d[2:]
        kb = [[InlineKeyboardButton("👤 Один человек", callback_data="c_s"), InlineKeyboardButton("👨‍👩‍ Семья", callback_data="c_g")]]
        await q.edit_message_text("Кто на фото?", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("c_"):
        context.user_data['count'] = "single" if d == "c_s" else "couple"
        kb = [[InlineKeyboardButton(v, callback_data=f"s_{k}")] for k, v in STYLES.items()]
        await q.edit_message_text("Стиль:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("s_"):
        context.user_data['style'] = d[2:]
        kb = [[InlineKeyboardButton(v, callback_data=f"sc_{k}")] for k, v in SCENES.items()]
        await q.edit_message_text("Место:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("sc_"):
        context.user_data['scene'] = d[3:]
        if context.user_data['count'] == "single":
            kb = [[InlineKeyboardButton("👨 Мужчина", callback_data="g_m"), InlineKeyboardButton("👩 Женщина", callback_data="g_w")]]
            await q.edit_message_text("Ваш пол:", reply_markup=InlineKeyboardMarkup(kb))
        else:
            context.user_data['gender'] = "mixed"
            await q.edit_message_text("📸 Пришлите фото.")
    elif d.startswith("g_"):
        context.user_data['gender'] = "man" if d == "g_m" else "woman"
        await q.edit_message_text("📸 Пришлите фото.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if 'theme' not in context.user_data: return
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            res = cur.fetchone()
            if (not res or res['credits'] <= 0) and uid != ADMIN_ID:
                await update.message.reply_text("🎫 Нет кредитов."); return
            if uid != ADMIN_ID: cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (uid,))
            conn.commit()

    m = await update.message.reply_text("⏳ Магия в процессе...")
    try:
        file = await update.message.photo[-1].get_file()
        u_b64 = base64.b64encode(await file.download_as_bytearray()).decode('utf-8')
        
        url = await generate_leonardo(context.user_data['theme'], context.user_data['style'], context.user_data['scene'], context.user_data['count'], context.user_data['gender'], context.user_data['orient'])
        if not url:
            await m.edit_text("❌ Ошибка Leonardo. Кредит возвращен."); await refund(uid); return
            
        res_img = await swap_face(url, u_b64)
        if res_img == "FACE_NOT_FOUND":
            await m.edit_text("❌ Лицо не найдено. Кредит возвращен."); await refund(uid); return
        elif not res_img:
            await m.edit_text("❌ Ошибка замены. Кредит возвращен."); await refund(uid); return
            
        await update.message.reply_photo(res_img, caption="Ваша открытка! ✨")
        await m.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Error: {e}")

async def refund(uid):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (uid,))
            conn.commit()

async def go_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    await update.callback_query.edit_message_text("Пакеты:", reply_markup=InlineKeyboardMarkup(kb))

async def buy_pkg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pk = PACKAGES[update.callback_query.data.replace("buy_", "")]
    pay = Payment.create({"amount": {"value": str(pk['price']), "currency": "RUB"}, "confirmation": {"type": "redirect", "return_url": "https://t.me/opencard_bot"}, "metadata": {"u": update.effective_user.id, "c": pk['cnt']}}, uuid.uuid4())
    kb = [[InlineKeyboardButton("💳 Оплатить", url=pay.confirmation.confirmation_url)]]
    await update.callback_query.edit_message_text(f"Счет на {pk['price']}₽.", reply_markup=InlineKeyboardMarkup(kb))

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(go_pay, pattern="^go_pay$"))
    app.add_handler(CallbackQueryHandler(buy_pkg, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(handle_menu, pattern="^(go_create|o_|t_|c_|s_|sc_|g_)"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
