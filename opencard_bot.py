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
from PIL import Image, ImageEnhance, ImageFilter
import io
import numpy as np

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
ADMIN_ID = 610820340  # Твой ID захардкожен для гарантии
DATABASE_URL = os.getenv("DATABASE_URL")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
LEONARDO_API_URL = "https://cloud.leonardo.ai/api/rest/v1"
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "vzsuz6ygs5m4ly")
RUNPOD_API_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}"

if YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

# Справочники
THEMES = {"new_year": "🎄 Новогодняя", "christmas": "✨ Рождество", "winter": "❄️ Зима"}
STYLES = {"ussr": "СССР", "vintage": "Винтаж", "modern": "Модерн"}
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
                    total_generated INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    payment_id TEXT,
                    amount INTEGER,
                    status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

# --- ЛОГИКА НЕЙРОСЕТЕЙ ---

def get_mvp_prompt(theme, style, scene, gender="man"):
    subject = "A handsome man" if gender == "man" else "A beautiful woman"
    style_desc = {
        "ussr": "Vintage Soviet postcard style, 1980s, hand-painted gouache, nostalgic colors.",
        "vintage": "Early 1900s Russian empire postcard style, sepia and muted colors.",
        "modern": "Digital artistic illustration, vibrant winter colors, clean folk art style."
    }.get(style, "Vintage illustration.")
    return f"{style_desc} {subject} in traditional winter clothes, facing camera, centered clear face, shoulders visible. Scene: {scene} with {theme} elements. Artistic painted style."

async def generate_template_leonardo(theme, style, scene, gender):
    prompt = get_mvp_prompt(theme, style, scene, gender)
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a",
        "width": 768, "height": 1024, "num_images": 1
    }
    try:
        resp = requests.post(f"{LEONARDO_API_URL}/generations", json=payload, headers=headers).json()
        gen_id = resp.get("sdGenerationJob", {}).get("generationId") or resp.get("generationId")
        if not gen_id:
            logger.error(f"Leonardo error: {resp}")
            return None

        for _ in range(40):
            await asyncio.sleep(3)
            res = requests.get(f"{LEONARDO_API_URL}/generations/{gen_id}", headers=headers).json()
            
            # Расширенная проверка структуры ответа Leonardo
            job = res.get("generations_by_pk") or (res.get("generations")[0] if res.get("generations") else None)
            
            if job and job.get("status") == "COMPLETE":
                images = job.get("generated_images", [])
                if images:
                    return images[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo API Exception: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        template_bytes = requests.get(template_url).content
        template_b64 = base64.b64encode(template_bytes).decode('utf-8')
        payload = {
            "input": {
                "source_image": user_photo_b64,
                "target_image": template_b64,
                "face_restore_model": "CodeFormer",
                "face_restore_visibility": 1,
                "codeformer_fidelity": 0.5,
                "upscale": 1
            }
        }
        res = requests.post(f"{RUNPOD_API_URL}/run", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}, json=payload).json()
        job_id = res.get("id")
        for _ in range(60):
            await asyncio.sleep(2)
            status = requests.get(f"{RUNPOD_API_URL}/status/{job_id}", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}).json()
            if status.get("status") == "COMPLETED":
                output = status.get("output")
                img_str = output if isinstance(output, str) else output.get("image")
                return base64.b64decode(img_str)
    except Exception as e:
        logger.error(f"RunPod error: {e}")
    return None

def apply_styling(img_bytes):
    img = Image.open(io.BytesIO(img_bytes)).convert('RGB')
    img_array = np.array(img)
    # Зернистость для аутентичности
    noise = np.random.normal(0, 10, img_array.shape).astype('uint8')
    img_array = np.clip(img_array + noise, 0, 255).astype('uint8')
    img = Image.fromarray(img_array)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

# --- ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username", (user_id, update.effective_user.username))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            credits = cur.fetchone()['credits']
            conn.commit()
    
    msg = f"Привет! Твой баланс: {credits} открыток."
    if user_id == ADMIN_ID: msg = "👑 Привет, Админ! Твои генерации бесплатны."
    
    keyboard = [[InlineKeyboardButton("🎨 Создать открытку", callback_data="select_gender")],
                [InlineKeyboardButton("💰 Пополнить баланс", callback_data="show_pricing")]]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def select_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("👨 Мужчина", callback_data="g_man"), InlineKeyboardButton("👩 Женщина", callback_data="g_woman")]]
    await query.edit_message_text("Кто будет на открытке?\n(Выбор пола улучшает качество)", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['gender'] = "man" if query.data == "g_man" else "woman"
    keyboard = [[InlineKeyboardButton(v, callback_data=f"theme_{k}")] for k, v in THEMES.items()]
    await query.edit_message_text("Выберите тему:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['theme'] = query.data.split('_', 1)[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    await query.edit_message_text("Выберите стиль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['style'] = query.data.split('_', 1)[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"scene_{k}")] for k, v in SCENES.items()]
    await query.edit_message_text("Выберите место:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['scene'] = query.data.split('_', 1)[1]
    await query.edit_message_text("🎯 Отлично! Теперь пришли мне фото (селфи).")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_free = (user_id == ADMIN_ID)

    if not is_free:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
                res = cur.fetchone()
                if not res or res['credits'] <= 0:
                    await update.message.reply_text("Баланс: 0. Купите открытки!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Купить", callback_data="show_pricing")]]))
                    return

    msg = await update.message.reply_text("⏳ Магия в процессе... (60-80 сек)")
    
    try:
        photo = await update.message.photo[-1].get_file()
        p_bytes = await photo.download_as_bytearray()
        u_b64 = base64.b64encode(p_bytes).decode('utf-8')

        # 1. Leonardo
        t_url = await generate_template_leonardo(
            context.user_data.get('theme', 'new_year'),
            context.user_data.get('style', 'ussr'),
            context.user_data.get('scene', 'night_street'),
            context.user_data.get('gender', 'man')
        )
        
        if not t_url:
            await update.message.reply_text("Ошибка Leonardo. Попробуйте еще раз или выберите другой стиль.")
            return

        # 2. FaceSwap
        swapped = await faceswap_runpod(t_url, u_b64)
        
        # 3. Styling
        final = apply_styling(swapped)

        if not is_free:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET credits = credits - 1, total_generated = total_generated + 1 WHERE user_id = %s", (user_id,))
                    conn.commit()

        await update.message.reply_photo(photo=final, caption="Ваша ретро-открытка готова! 🎄")
        await msg.delete()
    except Exception as e:
        logger.error(f"Critical error: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуйте другое фото.")

# --- ПЛАТЕЖИ (ЮKassa) ---

async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("1 открытка — 99₽", callback_data="buy_1")],
        [InlineKeyboardButton("5 открыток — 390₽", callback_data="buy_5")],
        [InlineKeyboardButton("10 открыток — 690₽", callback_data="buy_10")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_start")]
    ]
    await query.edit_message_text("Выберите пакет:", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    prices = {"buy_1": (1, 99), "buy_5": (5, 390), "buy_10": (10, 690)}
    count, price = prices[query.data]
    
    payment = Payment.create({
        "amount": {"value": f"{price}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await context.bot.get_me()).username}"},
        "capture": True,
        "description": f"Пополнение баланса: {count} открыток",
        "metadata": {"user_id": user_id, "count": count}
    }, uuid.uuid4())

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO payments (user_id, payment_id, amount, status) VALUES (%s, %s, %s, %s)",
                        (user_id, payment.id, price, "pending"))
            conn.commit()

    keyboard = [[InlineKeyboardButton("💳 Оплатить", url=payment.confirmation.confirmation_url)],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_p_{payment.id}")]]
    await query.message.reply_text(f"Счет на {price}₽ создан!", reply_markup=InlineKeyboardMarkup(keyboard))

async def check_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    payment_id = query.data.replace("check_p_", "")
    payment = Payment.find_one(payment_id)
    if payment.status == "succeeded":
        user_id = int(payment.metadata['user_id'])
        count = int(payment.metadata['count'])
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM payments WHERE payment_id = %s", (payment_id,))
                if cur.fetchone()['status'] != 'succeeded':
                    cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (count, user_id))
                    cur.execute("UPDATE payments SET status = 'succeeded' WHERE payment_id = %s", (payment_id,))
                    conn.commit()
                    await query.message.reply_text("✨ Оплата прошла! Удачи в творчестве.")
    else:
        await query.answer("Оплата еще не поступила.", show_alert=True)

# --- ЗАПУСК ---

def main():
    init_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(start, pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(select_gender, pattern="^select_gender$"))
    app.add_handler(CallbackQueryHandler(handle_gender, pattern="^g_"))
    app.add_handler(CallbackQueryHandler(handle_theme, pattern="^theme_"))
    app.add_handler(CallbackQueryHandler(handle_style, pattern="^style_"))
    app.add_handler(CallbackQueryHandler(handle_scene, pattern="^scene_"))
    app.add_handler(CallbackQueryHandler(show_pricing, pattern="^show_pricing$"))
    app.add_handler(CallbackQueryHandler(buy_package, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(check_payment_callback, pattern="^check_p_"))
    
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    logger.info("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
