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

# --- 2. СПРАВОЧНИКИ ---
THEMES = {"new_year": "🎄 Новый Год", "feb_14": "❤️ 14 Февраля", "feb_23": "🎖 23 Февраля", "mar_8": "💐 8 Марта", "winter": "❄️ Зима"}
CONGRATS_TEXTS = {
    "new_year": "С Новым Годом и Рождеством!",
    "feb_14": "С Днем Всех Влюбленных!",
    "feb_23": "С Днем Защитника Отечества!",
    "mar_8": "С Международным Женским Днем!",
    "winter": "Чудесного зимнего настроения!"
}
STYLES = {"ussr": "СССР (Гуашь)", "vintage": "Винтаж (Масло)", "modern": "Модерн"}
SCENES = {"night_street": "🌙 Улица", "pine_forest": "🌲 Лес", "winter_fair": "🎪 Ярмарка"}
PACKAGES = {"1": {"name": "1 открытка", "price": 149, "cnt": 1}, "3": {"name": "3 открытки", "price": 399, "cnt": 3}}

# --- 3. БАЗА ДАННЫХ ---
def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, credits INT DEFAULT 1, username TEXT)")
            cur.execute("CREATE TABLE IF NOT EXISTS payments (id SERIAL PRIMARY KEY, user_id BIGINT, payment_id TEXT, status TEXT, amount INT, count INT)")
            conn.commit()

# --- 4. ОФОРМЛЕНИЕ ОТКРЫТКИ (PILLOW) ---
def draw_card_elements(image_bytes, theme_key):
    img = Image.open(io.BytesIO(image_bytes))
    
    # Добавляем классическую белую рамку как на старых фото
    img = ImageOps.expand(img, border=40, fill='white')
    img = ImageOps.expand(img, border=5, fill='#d4af37') # Золотистый кант
    
    draw = ImageDraw.Draw(img)
    text = CONGRATS_TEXTS.get(theme_key, "Поздравляем!")
    
    # Попытка использовать шрифт. Если файла нет, будет стандартный.
    try:
        # Рекомендуется загрузить файл font.ttf в корень репозитория
        font = ImageFont.truetype("font.ttf", 45)
    except:
        font = ImageFont.load_default()

    w, h = img.size
    # Пишем текст внизу по центру
    draw.text((w/2, h-60), text, font=font, fill="#5d4037", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

# --- 5. НЕЙРОСЕТИ ---
async def generate_leonardo(theme, style, scene, count, gender, orientation):
    subj = "A happy family" if count == "couple" else f"One adult {'man' if gender == 'man' else 'woman'}"
    
    # Промпт максимально приближен к вашим референсам
    prompt = (
        f"Vintage Christmas greeting card style. {subj} standing in {scene}. "
        f"Detailed oil painting, 19th century holiday aesthetic. "
        f"Decorative borders with pine branches, golden bells, and snowflakes in corners. "
        f"Warm glowing windows in background, snowy atmosphere. Masterpiece, canvas texture."
    )
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768,
        "num_images": 1,
        "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3", # Lightning XL
        "alchemy": True,
        "presetStyle": "DYNAMIC"
    }
    
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        gen_id = r.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(40):
            await asyncio.sleep(4)
            res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers).json()
            job = res.get("generations_by_pk") or (res.get("generations")[0] if res.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except: return None

async def swap_face(t_url, u_b64):
    try:
        t_resp = requests.get(t_url)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
        payload = {"input": {"source_image": u_b64, "target_image": t_b64}}
        run_res = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        job_id = run_res.get("id")
        for i in range(60):
            await asyncio.sleep(3)
            res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}", headers=headers).json()
            if res.get("status") == "COMPLETED":
                out = res.get("output")
                img_data = out if isinstance(out, str) else (out.get("image") or out.get("result"))
                return base64.b64decode(img_data)
    except: return None

# --- 6. ОБРАБОТЧИКИ ТЕЛЕГРАМ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (uid, update.effective_user.username))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            res = cur.fetchone()
            c = res['credits'] if res else 0
    kb = [[InlineKeyboardButton("🎨 Создать открытку", callback_data="go_create")], 
          [InlineKeyboardButton("💰 Купить кредиты", callback_data="go_pay")]]
    await update.message.reply_text(f"Баланс: {c} 🎫\nСоздадим праздничную открытку?", reply_markup=InlineKeyboardMarkup(kb))

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data
    if d == "go_create":
        kb = [[InlineKeyboardButton("📱 Портрет", callback_data="o_v"), InlineKeyboardButton("🖼 Альбом", callback_data="o_h")]]
        await q.edit_message_text("Формат открытки:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("o_"):
        context.user_data['orient'] = "vertical" if d == "o_v" else "horizontal"
        kb = [[InlineKeyboardButton(v, callback_data=f"t_{k}")] for k, v in THEMES.items()]
        await q.edit_message_text("Тема поздравления:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("t_"):
        context.user_data['theme'] = d[2:]
        kb = [[InlineKeyboardButton("👤 Один", callback_data="c_s"), InlineKeyboardButton("👨‍👩‍ Семья", callback_data="c_g")]]
        await q.edit_message_text("Кто на фото?", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("c_"):
        context.user_data['count'] = "single" if d == "c_s" else "couple"
        kb = [[InlineKeyboardButton(v, callback_data=f"s_{k}")] for k, v in STYLES.items()]
        await q.edit_message_text("Стиль рисовки:", reply_markup=InlineKeyboardMarkup(kb))
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
            await q.edit_message_text("📸 Пришлите фото лица крупным планом.")
    elif d.startswith("g_"):
        context.user_data['gender'] = "man" if d == "g_m" else "woman"
        await q.edit_message_text("📸 Пришлите фото лица крупным планом.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if 'theme' not in context.user_data: return
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            res = cur.fetchone()
            if (not res or res['credits'] <= 0) and uid != ADMIN_ID:
                await update.message.reply_text("🎫 Кредиты закончились."); return
            if uid != ADMIN_ID: cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (uid,))
            conn.commit()

    m = await update.message.reply_text("⏳ Рисуем вашу открытку (около 2-3 мин)...")
    try:
        file = await update.message.photo[-1].get_file()
        u_b64 = base64.b64encode(await file.download_as_bytearray()).decode('utf-8')
        
        # 1. Генерация фона с элементами декора
        url = await generate_leonardo(context.user_data['theme'], context.user_data['style'], context.user_data['scene'], context.user_data['count'], context.user_data['gender'], context.user_data['orient'])
        
        # 2. Замена лица
        swapped_img = await swap_face(url, u_b64)
        
        # 3. Финальное оформление (рамка и текст)
        final_card = draw_card_elements(swapped_img, context.user_data['theme'])
        
        await update.message.reply_photo(final_card, caption="✨ Ваша праздничная открытка готова!")
        await m.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(e)
        await m.edit_text("Ошибка. Кредит вернули.")
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (uid,))
                conn.commit()

async def go_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    await update.callback_query.edit_message_text("Выберите пакет:", reply_markup=InlineKeyboardMarkup(kb))

async def buy_pkg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pk = PACKAGES[update.callback_query.data.replace("buy_", "")]
    pay = Payment.create({"amount": {"value": str(pk['price']), "currency": "RUB"}, "confirmation": {"type": "redirect", "return_url": "https://t.me/your_bot_name"}, "metadata": {"u": update.effective_user.id, "c": pk['cnt']}, "capture": True}, uuid.uuid4())
    await update.callback_query.edit_message_text(f"Оплата {pk['price']}₽.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оплатить", url=pay.confirmation.confirmation_url)]]))

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
