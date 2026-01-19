import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, PreCheckoutQueryHandler
import logging
import psycopg
from psycopg.rows import dict_row
from datetime import datetime
from yookassa import Configuration, Payment
import uuid
import asyncio
import base64
from PIL import Image, ImageEnhance
import numpy as np
import io

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
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

# Справочники
THEMES = {"new_year": "🎄 Новогодняя", "christmas": "✨ Рождество", "winter": "❄️ Зима"}
STYLES = {"ussr": "СССР", "vintage": "Винтаж", "modern": "Модерн"}
SCENES = {"night_street": "🌙 Улица", "pine_forest": "🌲 Лес", "winter_fair": "🎪 Ярмарка"}

# --- Работа с БД ---
def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
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

# --- Логика генерации ---

def get_mvp_prompt(theme, style, scene, orientation, gender="man"):
    subject = "A handsome man" if gender == "man" else "A beautiful woman"
    style_desc = {
        "ussr": "Vintage Soviet postcard style, 1980s, hand-painted gouache, nostalgic colors.",
        "vintage": "Early 1900s Russian empire postcard, sepia and muted colors.",
        "modern": "Digital artistic illustration, vibrant winter colors."
    }.get(style, "Vintage illustration.")
    
    return f"{style_desc} {subject} in traditional winter clothes, facing camera, centered clear face. Scene: {scene} with {theme} elements. Artistic painted style, high quality."

async def generate_template_leonardo(theme, style, scene, orientation, gender):
    prompt = get_mvp_prompt(theme, style, scene, orientation, gender)
    width, height = (768, 1024) if orientation == "vertical" else (1024, 768)
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a", "width": width, "height": height, "num_images": 1}
    
    try:
        response = requests.post(f"{LEONARDO_API_URL}/generations", json=payload, headers=headers)
        gen_id = response.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(40):
            await asyncio.sleep(3)
            res = requests.get(f"{LEONARDO_API_URL}/generations/{gen_id}", headers=headers)
            data = res.json().get("generations_by_pk", {})
            if data.get("status") == "COMPLETE":
                img_url = data.get("generated_images")[0].get("url")
                return img_url
    except Exception as e:
        logger.error(f"Leonardo error: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        # Загружаем шаблон и в b64
        template_data = base64.b64encode(requests.get(template_url).content).decode('utf-8')
        payload = {
            "input": {
                "source_image": user_photo_b64,
                "target_image": template_data,
                "face_restore_model": "CodeFormer",
                "face_restore_visibility": 1,
                "codeformer_fidelity": 0.5,
                "upscale": 1
            }
        }
        res = requests.post(f"{RUNPOD_API_URL}/run", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}, json=payload)
        job_id = res.json().get("id")
        for _ in range(60):
            await asyncio.sleep(2)
            status = requests.get(f"{RUNPOD_API_URL}/status/{job_id}", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}).json()
            if status.get("status") == "COMPLETED":
                return base64.b64decode(status.get("output") if isinstance(status.get("output"), str) else status.get("output").get("image"))
    except Exception as e:
        logger.error(f"RunPod error: {e}")
    return None

def apply_post_processing(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    # Зернистость для стиля
    img_array = np.array(img)
    noise = np.random.normal(0, 10, img_array.shape).astype('uint8')
    img_array = np.clip(img_array + noise, 0, 255).astype('uint8')
    img = Image.fromarray(img_array)
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

# --- Обработчики команд ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, username))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            credits = cur.fetchone()['credits']
            conn.commit()

    text = f"Привет, {update.effective_user.first_name}! 🎄\n\nЯ создаю уникальные ретро-открытки с твоим лицом.\nТвой баланс: {credits} открыток."
    keyboard = [
        [InlineKeyboardButton("🎨 Создать открытку", callback_data="select_gender")],
        [InlineKeyboardButton("💰 Пополнить баланс", callback_data="show_pricing")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def select_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("👨 Мужчина", callback_data="g_man"), InlineKeyboardButton("👩 Женщина", callback_data="g_woman")]]
    await query.edit_message_text("Кто будет на фото?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['gender'] = "man" if query.data == "g_man" else "woman"
    keyboard = [[InlineKeyboardButton(v, callback_data=f"theme_{k}")] for k, v in THEMES.items()]
    await query.edit_message_text("Выбери тему:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['theme'] = query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    await query.edit_message_text("Выбери стиль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['style'] = query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"scene_{k}")] for k, v in SCENES.items()]
    await query.edit_message_text("Место действия:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['scene'] = query.data.split('_')[1]
    context.user_data['orientation'] = "vertical"
    await query.edit_message_text("Отлично! Теперь пришли фото лица (селфи) крупным планом.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            user = cur.fetchone()
            if not user or user['credits'] <= 0:
                await update.message.reply_text("У вас закончились открытки. Пополните баланс!", 
                                              reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Купить", callback_data="show_pricing")]]))
                return

    status_msg = await update.message.reply_text("⏳ Начинаю работу... Это займет около минуты.")
    
    try:
        # Скачиваем фото пользователя
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        user_photo_b64 = base64.b64encode(photo_bytes).decode('utf-8')

        # 1. Leonardo
        template_url = await generate_template_leonardo(
            context.user_data.get('theme', 'new_year'),
            context.user_data.get('style', 'ussr'),
            context.user_data.get('scene', 'night_street'),
            'vertical',
            context.user_data.get('gender', 'man')
        )

        # 2. RunPod
        result_bytes = await faceswap_runpod(template_url, user_photo_b64)
        
        # 3. Post-process
        final_photo = apply_post_processing(result_bytes)

        # Списание баланса
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET credits = credits - 1, total_generated = total_generated + 1 WHERE user_id = %s", (user_id,))
                conn.commit()

        await update.message.reply_photo(photo=final_photo, caption="Твоя открытка готова! ✨")
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Error in handle_photo: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуйте другое фото.")

# --- Система оплаты (сохранена из оригинала) ---

async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("1 открытка — 99₽", callback_data="buy_1")],
        [InlineKeyboardButton("5 открыток — 390₽", callback_data="buy_5")],
        [InlineKeyboardButton("10 открыток — 690₽", callback_data="buy_10")]
    ]
    await query.edit_message_text("Выберите пакет открыток:", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    amount = int(query.data.split('_')[1])
    prices = {1: 99, 5: 390, 10: 690}
    price = prices[amount]
    
    # Здесь логика создания платежа ЮKassa (как в твоем коде)
    await query.message.reply_text(f"Оплата пакета на {amount} шт. через ЮKassa: {price}₽. (Тут должна быть ссылка на оплату)")

# --- Админ-панель (сохранена) ---

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) as total_users, sum(total_generated) as total_gen FROM users")
            res = cur.fetchone()
            await update.message.reply_text(f"Статистика:\nЮзеров: {res['total_users']}\nВсего генераций: {res['total_gen']}")

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CallbackQueryHandler(select_gender, pattern="^select_gender$"))
    app.add_handler(CallbackQueryHandler(handle_gender, pattern="^g_"))
    app.add_handler(CallbackQueryHandler(handle_theme, pattern="^theme_"))
    app.add_handler(CallbackQueryHandler(handle_style, pattern="^style_"))
    app.add_handler(CallbackQueryHandler(handle_scene, pattern="^scene_"))
    app.add_handler(CallbackQueryHandler(show_pricing, pattern="^show_pricing$"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    app.run_polling()

if __name__ == "__main__":
    main()
