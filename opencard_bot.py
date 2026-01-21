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

# --- 1. НАСТРОЙКИ (Берутся из переменных окружения Railway) ---
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

# --- 2. КОНФИГУРАЦИЯ ТЕКСТА ---
CONGRATS_TEXTS = {
    "new_year": "С Новым Годом и Рождеством!",
    "feb_14": "С Днем Всех Влюбленных!",
    "feb_23": "С Днем Защитника Отечества!",
    "mar_8": "С Международным Женским Днем!",
    "winter": "Снежного счастья и мирного неба!"
}
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

# --- 4. ОФОРМЛЕНИЕ (PILLOW) ---
def draw_card_elements(image_bytes, theme_key):
    img = Image.open(io.BytesIO(image_bytes))
    
    # 1. Создаем мягкое белое паспарту (отступы)
    img = ImageOps.expand(img, border=(35, 35, 35, 85), fill='#FDFDFD')
    
    # 2. Рисуем тонкую изящную рамку
    draw = ImageDraw.Draw(img)
    w, h = img.size
    draw.rectangle([15, 15, w-15, h-15], outline="#C0B9B0", width=1)
    
    text = CONGRATS_TEXTS.get(theme_key, "Поздравляем!")
    
    # 3. Настройка шрифта Lobster (должен быть в корне проекта как font.ttf)
    font_path = "font.ttf"
    try:
        if os.path.exists(font_path):
            font = ImageFont.truetype(font_path, 44)
        else:
            font = ImageFont.load_default()
    except:
        font = ImageFont.load_default()

    # 4. Текст в винтажном серо-коричневом цвете
    draw.text((w/2, h-45), text, font=font, fill="#6D4C41", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

# --- 5. ЛОГИКА ГЕНЕРАЦИИ ---
async def generate_leonardo(theme_key, gender, orientation, count):
    # Промпт для мягкого ретро-реализма (как на референсе)
    subj = "One single person" if count == "single" else "A happy family"
    gender_str = "man" if gender == "man" else "woman"
    
    prompt = (
        f"Nostalgic Soviet era postcard illustration, {subj} ({gender_str if count=='single' else ''}) looking at camera. "
        f"Style of soft pastel drawing on high quality textured paper. "
        f"Muted natural colors, gentle lighting, soft grainy texture, cinematic bokeh background. "
        f"Winter scene, falling snow, realistic facial features, masterpiece, centered."
    )
    
    negative = (
        "caricature, cartoon, 3d, vibrant colors, high contrast, sharp lines, plastic skin, "
        "distorted faces, extra fingers, group of people, crowd, anime"
    )
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "negative_prompt": negative,
        "width": 1024 if orientation == "horizontal" else 768,
        "height": 768 if orientation == "horizontal" else 1024,
        "modelId": "aa77f04e-3e3b-47f4-9049-74e2d3df2f42", # Vision XL для мягкого стиля
        "alchemy": True,
        "presetStyle": "ILLUSTRATION",
        "guidance_scale": 5.5  # Оптимально для реализма без кривизны
    }
    
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        gen_id = r.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(35):
            await asyncio.sleep(4)
            res = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gen_id}", headers=headers).json()
            job = res.get("generations_by_pk") or (res.get("generations")[0] if res.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo error: {e}")
        return None

async def swap_face(target_url, user_b64):
    try:
        t_resp = requests.get(target_url)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
        payload = {
            "input": {
                "source_image": user_b64,
                "target_image": t_b64,
                "face_restorer_name": "CodeFormer",
                "codeformer_fidelity": 0.5, # Сохраняет текстуру рисунка на лице
                "upscale": 1
            }
        }
        
        run_res = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        job_id = run_res.get("id")
        
        for i in range(60):
            await asyncio.sleep(3)
            res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}", headers=headers).json()
            if res.get("status") == "COMPLETED":
                out = res.get("output")
                img_data = out if isinstance(out, str) else (out.get("image") or out.get("result"))
                return base64.b64decode(img_data)
    except Exception as e:
        logger.error(f"FaceSwap error: {e}")
        return None

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
    await update.message.reply_text(f"🎫 Кредитов: {c}\nСоздадим шедевр в стиле ретро?", reply_markup=InlineKeyboardMarkup(kb))

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data
    if d == "go_create":
        kb = [[InlineKeyboardButton("🖼 Горизонтальная", callback_data="o_h"), InlineKeyboardButton("📱 Вертикальная", callback_data="o_v")]]
        await q.edit_message_text("Формат открытки:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("o_"):
        context.user_data['orient'] = "horizontal" if d == "o_h" else "vertical"
        kb = [[InlineKeyboardButton(v, callback_data=f"t_{k}")] for k, v in CONGRATS_TEXTS.items() if k != "winter"]
        kb.append([InlineKeyboardButton("❄️ Зимняя классика", callback_data="t_winter")])
        await q.edit_message_text("Тема праздника:", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("t_"):
        context.user_data['theme'] = d[2:]
        kb = [[InlineKeyboardButton("👤 Один человек", callback_data="c_s"), InlineKeyboardButton("👨‍👩‍ Семья", callback_data="c_g")]]
        await q.edit_message_text("Кто будет на фото?", reply_markup=InlineKeyboardMarkup(kb))
    elif d.startswith("c_"):
        context.user_data['count'] = "single" if d == "c_s" else "couple"
        if context.user_data['count'] == "single":
            kb = [[InlineKeyboardButton("👨 Мужчина", callback_data="g_m"), InlineKeyboardButton("👩 Женщина", callback_data="g_w")]]
            await q.edit_message_text("Ваш пол:", reply_markup=InlineKeyboardMarkup(kb))
        else:
            context.user_data['gender'] = "mixed"
            await q.edit_message_text("📸 Отправьте ваше селфи (лицо должно быть четко видно).")
    elif d.startswith("g_"):
        context.user_data['gender'] = "man" if d == "g_m" else "woman"
        await q.edit_message_text("📸 Отправьте ваше селфи (лицо должно быть четко видно).")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if 'theme' not in context.user_data: return
    
    # Проверка баланса
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (uid,))
            res = cur.fetchone()
            if (not res or res['credits'] <= 0) and uid != ADMIN_ID:
                await update.message.reply_text("🎫 Кредиты закончились. Пополните баланс."); return
            if uid != ADMIN_ID: cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (uid,))
            conn.commit()

    m = await update.message.reply_text("⏳ Магия началась... Рисуем вашу открытку (около 2 мин).")
    try:
        file = await update.message.photo[-1].get_file()
        u_b64 = base64.b64encode(await file.download_as_bytearray()).decode('utf-8')
        
        # 1. Генерация фона
        url = await generate_leonardo(context.user_data['theme'], context.user_data['gender'], context.user_data['orient'], context.user_data['count'])
        if not url: raise Exception("Leonardo failed")
        
        # 2. Замена лица
        swapped = await swap_face(url, u_b64)
        if not swapped: raise Exception("FaceSwap failed")
        
        # 3. Финальная рамка и текст
        final = draw_card_elements(swapped, context.user_data['theme'])
        
        await update.message.reply_photo(final, caption="✨ Ваша ретро-открытка готова!")
        await m.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(f"Process error: {e}")
        await m.edit_text("❌ Ошибка при создании. Кредит возвращен.")
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (uid,))
                conn.commit()

async def go_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    await update.callback_query.edit_message_text("Выберите пакет:", reply_markup=InlineKeyboardMarkup(kb))

async def buy_pkg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pk = PACKAGES[update.callback_query.data.replace("buy_", "")]
    pay = Payment.create({
        "amount": {"value": str(pk['price']), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/your_bot_name"},
        "metadata": {"u": update.effective_user.id, "c": pk['cnt']},
        "capture": True
    }, uuid.uuid4())
    await update.callback_query.edit_message_text(f"Счет на {pk['price']}₽ готов.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оплатить", url=pay.confirmation.confirmation_url)]]))

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(go_pay, pattern="^go_pay$"))
    app.add_handler(CallbackQueryHandler(buy_pkg, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(handle_menu, pattern="^(go_create|o_|t_|c_|g_)"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
