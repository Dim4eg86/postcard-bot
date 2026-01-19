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
from PIL import Image, ImageEnhance, ImageFilter
import io
import numpy as np

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены
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

# --- База данных ---
def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

# --- Логика обработки изображений ---

def get_mvp_prompt(theme, style, scene, orientation, gender="man"):
    """Формирует промпт с учетом пола для предотвращения искажений"""
    subject = "A handsome man" if gender == "man" else "A beautiful woman"
    style_desc = {
        "ussr": "Vintage Soviet postcard style, 1980s, hand-painted gouache, nostalgic colors.",
        "vintage": "Early 1900s Russian empire postcard, sepia and muted colors, artistic illustration.",
        "modern": "Digital artistic illustration, vibrant winter colors, clean folk art style."
    }.get(style, "Vintage illustration.")
    
    return f"{style_desc} {subject} in traditional winter coat and hat, facing camera, centered clear face. Scene: {scene} with {theme} elements. Artistic painted style, NOT photorealistic."

async def generate_template_leonardo(theme, style, scene, orientation, gender):
    prompt = get_mvp_prompt(theme, style, scene, orientation, gender)
    width, height = (768, 1024) if orientation == "vertical" else (1024, 768)
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt, 
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a", # Vision XL
        "width": width, "height": height, "num_images": 1
    }
    
    try:
        response = requests.post(f"{LEONARDO_API_URL}/generations", json=payload, headers=headers)
        gen_id = response.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(40):
            await asyncio.sleep(3)
            res = requests.get(f"{LEONARDO_API_URL}/generations/{gen_id}", headers=headers)
            data = res.json().get("generations_by_pk", {})
            if data.get("status") == "COMPLETE":
                return data.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo error: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    """Face Swap с использованием восстановителя CodeFormer"""
    try:
        template_data = base64.b64encode(requests.get(template_url).content).decode('utf-8')
        payload = {
            "input": {
                "source_image": user_photo_b64,
                "target_image": template_data,
                "face_restore_model": "CodeFormer", # Улучшает детализацию лица
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
                output = status.get("output")
                img_str = output if isinstance(output, str) else output.get("image")
                return base64.b64decode(img_str)
    except Exception as e:
        logger.error(f"RunPod error: {e}")
    return None

def apply_vintage_style(image_bytes):
    """Добавляет шум и цветокоррекцию для 'склейки' фото с рисунком"""
    img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    
    # 1. Добавляем шум (зернистость)
    img_array = np.array(img)
    noise = np.random.normal(0, 10, img_array.shape).astype('uint8')
    img_array = np.clip(img_array + noise, 0, 255).astype('uint8')
    img = Image.fromarray(img_array)
    
    # 2. Немного приглушаем контраст для винтажного вида
    img = ImageEnhance.Contrast(img).enhance(0.95)
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()

# --- Обработчики команд Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username", (user_id, username))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            credits = cur.fetchone()['credits']
            conn.commit()

    text = f"Привет, {update.effective_user.first_name}! 🎄\nТвой баланс: {credits} открыток."
    keyboard = [
        [InlineKeyboardButton("🎨 Создать открытку", callback_data="select_gender")],
        [InlineKeyboardButton("💰 Пополнить баланс", callback_data="show_pricing")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def select_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("👨 Мужчина", callback_data="g_man"),
         InlineKeyboardButton("👩 Женщина", callback_data="g_woman")]
    ]
    await query.edit_message_text("Шаг 0: Кто на фото?\n(Это поможет избежать искажений лица)", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['gender'] = "man" if query.data == "g_man" else "woman"
    
    keyboard = [[InlineKeyboardButton("📱 Вертикальная", callback_data="orientation_vertical"),
                 InlineKeyboardButton("💻 Горизонтальная", callback_data="orientation_horizontal")]]
    await query.edit_message_text("Шаг 1: Формат открытки:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['orientation'] = query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"theme_{k}")] for k, v in THEMES.items()]
    await query.edit_message_text("Шаг 2: Тема:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['theme'] = query.data.split('_', 1)[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    await query.edit_message_text("Шаг 3: Стиль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['style'] = query.data.split('_', 1)[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"scene_{k}")] for k, v in SCENES.items()]
    await query.edit_message_text("Шаг 4: Место:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['scene'] = query.data.split('_', 1)[1]
    await query.edit_message_text("🎯 Все готово! Теперь пришли мне фото (селфи), где хорошо видно лицо.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if 'gender' not in context.user_data:
        await update.message.reply_text("Пожалуйста, начни сначала через /start")
        return

    # Проверка баланса
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            res = cur.fetchone()
            if not res or res['credits'] <= 0:
                await update.message.reply_text("Баланс пуст. Пополни его!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Купить", callback_data="show_pricing")]]))
                return

    msg = await update.message.reply_text("⏳ Магия началась... (обычно занимает 60 сек)")
    
    try:
        photo = await update.message.photo[-1].get_file()
        p_bytes = await photo.download_as_bytearray()
        u_b64 = base64.b64encode(p_bytes).decode('utf-8')

        # 1. Генерация правильного фона
        t_url = await generate_template_leonardo(
            context.user_data['theme'], context.user_data['style'],
            context.user_data['scene'], context.user_data['orientation'],
            context.user_data['gender']
        )
        
        # 2. Профессиональный FaceSwap
        swapped_bytes = await faceswap_runpod(t_url, u_b64)
        
        # 3. Финальная стилизация (шум + цвет)
        final_photo = apply_vintage_style(swapped_bytes)

        # Списание
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET credits = credits - 1, total_generated = total_generated + 1 WHERE user_id = %s", (user_id,))
                conn.commit()

        await update.message.reply_photo(photo=final_photo, caption="Твоя винтажная открытка готова! ✨")
        await msg.delete()

    except Exception as e:
        logger.error(f"Critical error: {e}")
        await update.message.reply_text("Произошла ошибка в нейросетях. Попробуй еще раз.")

# --- Функции оплаты (ЮKassa) ---

async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("1 открытка — 99₽", callback_data="buy_1")],
        [InlineKeyboardButton("5 открыток — 390₽", callback_data="buy_5")],
        [InlineKeyboardButton("10 открыток — 690₽", callback_data="buy_10")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_start")]
    ]
    await query.edit_message_text("Выбери пакет:", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Здесь твоя логика формирования ссылки на оплату через yookassa.Payment.create...
    await query.answer("Перехожу к оплате...")
    await query.message.reply_text("Ссылка на оплату будет здесь (реализуй создание платежа по своему API).")

# --- Админка и запуск ---

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) as u, sum(total_generated) as g FROM users")
            r = cur.fetchone()
            await update.message.reply_text(f"Стата:\nЮзеров: {r['u']}\nГенераций: {r['g']}")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", admin_stats))
    
    app.add_handler(CallbackQueryHandler(select_gender, pattern="^select_gender$"))
    app.add_handler(CallbackQueryHandler(handle_gender, pattern="^g_"))
    app.add_handler(CallbackQueryHandler(handle_orientation, pattern="^orientation_"))
    app.add_handler(CallbackQueryHandler(handle_theme, pattern="^theme_"))
    app.add_handler(CallbackQueryHandler(handle_style, pattern="^style_"))
    app.add_handler(CallbackQueryHandler(handle_scene, pattern="^scene_"))
    app.add_handler(CallbackQueryHandler(show_pricing, pattern="^show_pricing$"))
    app.add_handler(CallbackQueryHandler(buy_package, pattern="^buy_"))
    
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    logger.info("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
