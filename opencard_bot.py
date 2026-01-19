import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import logging
import psycopg
from psycopg.rows import dict_row
from datetime import datetime
from yookassa import Configuration, Payment
import uuid
import asyncio
import base64
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
import io

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
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

# Настройка YooKassa
if YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

# Словари данных
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
    "winter": "❄️ Зима",
    "congratulations": "💝 Поздравление"
}

STYLES = {
    "ussr": "СССР (Гуашь)",
    "vintage": "Винтаж (Масло)",
    "modern": "Модерн",
    "rus": "На Руси"
}

SCENES = {
    "night_street": "🌙 Улица",
    "snowy_estate": "🏠 Усадьба",
    "pine_forest": "🌲 Лес",
    "winter_fair": "🎪 Ярмарка"
}

# =============================================================================
# БАЗА ДАННЫХ (Полностью из твоего файла)
# =============================================================================

def get_db_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_database():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    credits INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    package_id VARCHAR(50),
                    amount INTEGER,
                    status VARCHAR(50),
                    payment_id VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    message TEXT,
                    status VARCHAR(50) DEFAULT 'open',
                    admin_reply TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

def get_or_create_user(user_id, username=None, first_name=None):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            user = cur.fetchone()
            if not user:
                cur.execute("INSERT INTO users (user_id, username, first_name, credits) VALUES (%s, %s, %s, 1) RETURNING *", (user_id, username, first_name))
                user = cur.fetchone()
                conn.commit()
            return user

def get_user_credits(user_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            res = cur.fetchone()
            return res['credits'] if res else 0

def add_credits(user_id, amount):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()

def use_credit(user_id):
    if user_id == ADMIN_ID: return True
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s AND credits > 0 RETURNING credits", (user_id,))
            res = cur.fetchone()
            conn.commit()
            return res is not None

# =============================================================================
# ЛОГИКА ГЕНЕРАЦИИ (Объединение твоего стиля и новых праздников)
# =============================================================================

def get_mvp_prompt(theme, style, scene, count, gender):
    # Определение субъектов
    if count == "couple": subject = "A romantic couple (man and woman)"
    elif count == "family": subject = "A happy family group with children"
    else: subject = "A handsome man" if gender == "man" else "A beautiful woman"

    # Стилизация под твои описания
    style_desc = {
        "ussr": "Soviet-style postcard from 1980s. Hand-painted gouache illustration, warm nostalgic colors, ornate decorative border.",
        "vintage": "Pre-revolutionary Russian vintage postcard early 1900s. Oil painting, warm lighting, aged paper texture.",
        "modern": "Contemporary folk art style, clean lines, vibrant festive colors.",
        "rus": "Ancient Rus historical style, traditional Slavic patterns, warm earth tones."
    }.get(style, "painted illustration")

    # Сцены из твоего файла
    scene_desc = {
        "night_street": f"{subject} on a snow-covered street at night, warm street lamp light.",
        "snowy_estate": f"{subject} in front of a grand Russian manor house covered in snow.",
        "pine_forest": f"{subject} standing in a majestic snowy pine forest.",
        "winter_fair": f"{subject} at a colorful Russian winter fair with decorated stalls."
    }.get(scene, "winter scene")

    # Новые праздники
    theme_desc = {
        "new_year": "New Year theme, spruce branches, snow sparkles.",
        "feb_14": "St. Valentine theme, subtle hearts, romantic atmosphere.",
        "feb_23": "23 February Defender of Fatherland theme, soviet stars, patriotic winter motifs.",
        "mar_8": "8 March theme, spring flowers, tulips, mimosa, bright sun."
    }.get(theme, "festive atmosphere")

    prompt = f"{style_desc}. {scene_desc}. {theme_desc}. Clear faces looking at camera, artistic masterpiece, NOT a photograph."
    return prompt

async def generate_template_leonardo(theme, style, scene, count, gender, orientation):
    prompt = get_mvp_prompt(theme, style, scene, count, gender)
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a", # Vision XL
        "width": 832 if orientation == "vertical" else 1216,
        "height": 1216 if orientation == "vertical" else 832,
        "num_images": 1
    }
    try:
        resp = requests.post(f"{LEONARDO_API_URL}/generations", json=payload, headers=headers).json()
        gen_id = resp.get("sdGenerationJob", {}).get("generationId")
        for _ in range(40):
            await asyncio.sleep(3)
            res = requests.get(f"{LEONARDO_API_URL}/generations/{gen_id}", headers=headers).json()
            job = res.get("generations_by_pk") or (res.get("generations")[0] if res.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo Error: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        template_b64 = base64.b64encode(requests.get(template_url).content).decode('utf-8')
        payload = {
            "input": {
                "source_image": user_photo_b64,
                "target_image": template_b64,
                "face_restore_model": "CodeFormer"
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

# =============================================================================
# ОБРАБОТЧИКИ ТЕЛЕГРАМ
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username, user.first_name)
    credits = get_user_credits(user.id)
    
    keyboard = [
        [InlineKeyboardButton("🎨 Создать открытку", callback_data="create_postcard")],
        [InlineKeyboardButton("💳 Пополнить баланс", callback_data="show_pricing")],
        [InlineKeyboardButton("💬 Поддержка", callback_data="support")]
    ]
    
    text = f"Привет, {user.first_name}! Твой баланс: {credits} открыток.\nВыбирай стиль и создавай шедевры!"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def create_postcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if get_user_credits(update.effective_user.id) <= 0 and update.effective_user.id != ADMIN_ID:
        await query.edit_message_text("❌ Нет открыток на балансе.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Купить", callback_data="show_pricing")]]))
        return
    
    keyboard = [[InlineKeyboardButton("📱 Вертикальная", callback_data="orient_vertical"), 
                 InlineKeyboardButton("🖼️ Горизонтальная", callback_data="orient_horizontal")]]
    await query.edit_message_text("Выберите формат:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['orientation'] = update.callback_query.data.replace("orient_", "")
    keyboard = [[InlineKeyboardButton(v, callback_data=f"theme_{k}")] for k, v in THEMES.items()]
    await update.callback_query.edit_message_text("Выберите праздник:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['theme'] = update.callback_query.data.replace("theme_", "")
    keyboard = [
        [InlineKeyboardButton("👤 Один человек", callback_data="count_single")],
        [InlineKeyboardButton("👩‍❤️‍👨 Пара", callback_data="count_couple")],
        [InlineKeyboardButton("👨‍👩‍👧‍👦 Семья/Группа", callback_data="count_family")]
    ]
    await update.callback_query.edit_message_text("Сколько людей будет на фото?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['count'] = update.callback_query.data.replace("count_", "")
    keyboard = [[InlineKeyboardButton(v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    await update.callback_query.edit_message_text("Выберите стиль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['style'] = update.callback_query.data.replace("style_", "")
    keyboard = [[InlineKeyboardButton(v, callback_data=f"scene_{k}")] for k, v in SCENES.items()]
    await update.callback_query.edit_message_text("Выберите место действия:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['scene'] = update.callback_query.data.replace("scene_", "")
    if context.user_data['count'] == 'single':
        keyboard = [[InlineKeyboardButton("👨 Мужчина", callback_data="g_man"), 
                     InlineKeyboardButton("👩 Женщина", callback_data="g_woman")]]
        await update.callback_query.edit_message_text("Укажите пол для лучшего качества:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        context.user_data['gender'] = 'mixed'
        await update.callback_query.edit_message_text("📸 Отлично! Пришлите фото.")

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = "man" if update.callback_query.data == "g_man" else "woman"
    await update.callback_query.edit_message_text("📸 Отлично! Пришлите фото.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not all(k in context.user_data for k in ['theme', 'style', 'scene']): return
    
    if not use_credit(user_id):
        await update.message.reply_text("❌ Закончились открытки.")
        return

    msg = await update.message.reply_text("⏳ Магия началась! Готовлю шаблон и меняю лица (около 1 мин)...")
    
    try:
        photo = await update.message.photo[-1].get_file()
        p_bytes = await photo.download_as_bytearray()
        u_b64 = base64.b64encode(p_bytes).decode('utf-8')

        # 1. Leonardo Template
        t_url = await generate_template_leonardo(
            context.user_data['theme'], context.user_data['style'],
            context.user_data['scene'], context.user_data['count'],
            context.user_data['gender'], context.user_data['orientation']
        )
        
        # 2. Face Swap
        final_img = await faceswap_runpod(t_url, u_b64)
        
        # 3. Эффекты (из твоего файла)
        img = Image.open(io.BytesIO(final_img))
        enhancer = ImageEnhance.Contrast(img); img = enhancer.enhance(1.05)
        
        out = io.BytesIO(); img.save(out, format="JPEG", quality=95)
        await update.message.reply_photo(photo=out.getvalue(), caption="Ваша уникальная открытка готова! ✨")
        await msg.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(e)
        add_credits(user_id, 1) # Возврат кредита при ошибке
        await update.message.reply_text("Произошла ошибка. Открытка возвращена на баланс.")

# =============================================================================
# ПЛАТЕЖИ И ПОДДЕРЖКА (Из твоего файла)
# =============================================================================

async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_start")])
    await update.callback_query.edit_message_text("Выберите пакет открыток:", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pkg_id = update.callback_query.data.replace("buy_", "")
    pkg = PACKAGES[pkg_id]
    payment = Payment.create({
        "amount": {"value": str(pkg['price']), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await context.bot.get_me()).username}"},
        "capture": True,
        "description": f"Пополнение: {pkg['name']}",
        "metadata": {"user_id": update.effective_user.id, "count": pkg['count']}
    }, uuid.uuid4())
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO payments (user_id, package_id, amount, status, payment_id) VALUES (%s, %s, %s, %s, %s)",
                        (update.effective_user.id, pkg_id, pkg['price'], 'pending', payment.id))
            conn.commit()
    
    keyboard = [[InlineKeyboardButton("💳 Оплатить", url=payment.confirmation.confirmation_url)],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_p_{payment.id}")]]
    await update.callback_query.edit_message_text(f"Счет на {pkg['price']}₽ создан.", reply_markup=InlineKeyboardMarkup(keyboard))

async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p_id = update.callback_query.data.replace("check_p_", "")
    payment = Payment.find_one(p_id)
    if payment.status == 'succeeded':
        uid = int(payment.metadata['user_id'])
        cnt = int(payment.metadata['count'])
        add_credits(uid, cnt)
        await update.callback_query.edit_message_text(f"✅ Успешно! Начислено {cnt} открыток.")
    else:
        await update.callback_query.answer("Оплата не найдена.", show_alert=True)

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['support'] = True
    await update.callback_query.edit_message_text("Напишите ваше сообщение, я передам его админу.")

async def handle_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('support'): return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO support_tickets (user_id, message) VALUES (%s, %s) RETURNING id", (update.effective_user.id, update.message.text))
            tid = cur.fetchone()['id']
            conn.commit()
    await update.message.reply_text(f"Обращение #{tid} принято.")
    context.user_data.pop('support')

# =============================================================================
# MAIN
# =============================================================================

def main():
    init_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(start, pattern="back_to_start"))
    app.add_handler(CallbackQueryHandler(create_postcard, pattern="create_postcard"))
    app.add_handler(CallbackQueryHandler(handle_orientation, pattern="orient_"))
    app.add_handler(CallbackQueryHandler(handle_theme, pattern="theme_"))
    app.add_handler(CallbackQueryHandler(handle_count, pattern="count_"))
    app.add_handler(CallbackQueryHandler(handle_style, pattern="style_"))
    app.add_handler(CallbackQueryHandler(handle_scene, pattern="scene_"))
    app.add_handler(CallbackQueryHandler(handle_gender, pattern="g_"))
    app.add_handler(CallbackQueryHandler(show_pricing, pattern="show_pricing"))
    app.add_handler(CallbackQueryHandler(buy_package, pattern="buy_"))
    app.add_handler(CallbackQueryHandler(check_payment, pattern="check_p_"))
    app.add_handler(CallbackQueryHandler(support, pattern="support"))
    
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_support))
    
    app.run_polling()

if __name__ == "__main__":
    main()
