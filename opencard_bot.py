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

# Токены
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
ADMIN_ID = 610820340  # Твой ID для бесплатного доступа
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

def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

# --- УЛУЧШЕННАЯ ЛОГИКА ГЕНЕРАЦИИ ---

def get_mvp_prompt(theme, style, scene, gender="man"):
    subject = "A handsome man" if gender == "man" else "A beautiful woman"
    style_desc = {
        "ussr": "Vintage Soviet postcard style, 1980s, hand-painted gouache, nostalgic colors.",
        "vintage": "Early 1900s Russian empire postcard, sepia and muted colors, artistic illustration.",
        "modern": "Digital artistic illustration, vibrant winter colors, clean folk art style."
    }.get(style, "Vintage illustration.")
    return f"{style_desc} {subject} in traditional winter clothes, facing camera, centered clear face, shoulders visible. Scene: {scene} with {theme} elements. Artistic painted style, NOT a photo."

async def generate_template_leonardo(theme, style, scene, gender):
    prompt = get_mvp_prompt(theme, style, scene, gender)
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a", # Leonardo Vision XL
        "width": 768, "height": 1024, "num_images": 1
    }
    try:
        resp = requests.post(f"{LEONARDO_API_URL}/generations", json=payload, headers=headers).json()
        # Исправлено: пробуем разные ключи ответа
        gen_id = resp.get("sdGenerationJob", {}).get("generationId") or resp.get("generationId")
        if not gen_id: 
            logger.error(f"Leonardo error response: {resp}")
            return None

        for _ in range(40):
            await asyncio.sleep(3)
            res = requests.get(f"{LEONARDO_API_URL}/generations/{gen_id}", headers=headers).json()
            gen_data = res.get("generations_by_pk") or res.get("generations", [{}])[0]
            if gen_data.get("status") == "COMPLETE":
                return gen_data.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo API Crash: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        if not template_url: return None
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
        logger.error(f"RunPod Error: {e}")
    return None

def apply_post_processing(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert('RGB')
    # Добавляем "шум" для винтажности
    img_array = np.array(img)
    noise = np.random.normal(0, 10, img_array.shape).astype('uint8')
    img_array = np.clip(img_array + noise, 0, 255).astype('uint8')
    img = Image.fromarray(img_array)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

# --- ПЛАТЕЖИ (ТВОЯ ЛОГИКА) ---

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    prices = {"buy_1": (1, 99), "buy_5": (5, 390), "buy_10": (10, 690)}
    count, price = prices[query.data]
    
    payment = Payment.create({
        "amount": {"value": f"{price}.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await context.bot.get_me()).username}"},
        "capture": True,
        "description": f"Пополнение: {count} открыток",
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
                cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (count, user_id))
                cur.execute("UPDATE payments SET status = 'succeeded' WHERE payment_id = %s", (payment_id,))
                conn.commit()
        await query.message.reply_text("✨ Оплата прошла! Кредиты зачислены.")
    else:
        await query.answer("Оплата еще не найдена.", show_alert=True)

# --- ОБРАБОТЧИКИ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username", (user_id, update.effective_user.username))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            credits = cur.fetchone()['credits']
            conn.commit()
    
    text = f"Ваш баланс: {credits} открыток."
    if user_id == ADMIN_ID: text = "👑 Бесплатный режим админа активен."
    
    keyboard = [[InlineKeyboardButton("🎨 Создать открытку", callback_data="select_gender")],
                [InlineKeyboardButton("💰 Пополнить баланс", callback_data="show_pricing")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_admin = (user_id == ADMIN_ID)

    if not is_admin:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
                res = cur.fetchone()
                if not res or res['credits'] <= 0:
                    await update.message.reply_text("Баланс 0. Пополните его!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Купить", callback_data="show_pricing")]]))
                    return

    status = await update.message.reply_text("⏳ Генерирую шаблон и вклеиваю лицо...")
    
    try:
        photo = await update.message.photo[-1].get_file()
        p_bytes = await photo.download_as_bytearray()
        u_b64 = base64.b64encode(p_bytes).decode('utf-8')

        t_url = await generate_template_leonardo(context.user_data.get('theme', 'new_year'), context.user_data.get('style', 'ussr'), context.user_data.get('scene', 'night_street'), context.user_data.get('gender', 'man'))
        
        if not t_url:
            await update.message.reply_text("Ошибка Leonardo. Попробуйте снова.")
            return

        res_bytes = await faceswap_runpod(t_url, u_b64)
        final = apply_post_processing(res_bytes)

        if not is_admin:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET credits = credits - 1, total_generated = total_generated + 1 WHERE user_id = %s", (user_id,))
                    conn.commit()

        await update.message.reply_photo(photo=final, caption="Ваша открытка готова! ✨")
        await status.delete()
    except Exception as e:
        logger.error(f"General processing error: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуйте другое фото.")

# (Функции выбора тем/стилей остаются такими же как выше...)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buy_package, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(check_payment_callback, pattern="^check_p_"))
    # ... добавь сюда обработчики тем/стилей g_man, theme_, style_, scene_ ...
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
