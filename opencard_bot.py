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
from PIL import Image, ImageEnhance
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
LEONARDO_API_URL = "https://cloud.leonardo.ai/api/rest/v1"
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

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
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
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    message TEXT,
                    status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

def get_or_create_user(user_id, username, first_name):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            user = cur.fetchone()
            if not user:
                cur.execute("INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) RETURNING *", 
                            (user_id, username, first_name))
                user = cur.fetchone()
                conn.commit()
            return user

def use_credit(user_id):
    if user_id == ADMIN_ID: return True
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s AND credits > 0 RETURNING credits", (user_id,))
            res = cur.fetchone()
            conn.commit()
            return res is not None

def add_credits(user_id, amount):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (amount, user_id))
            conn.commit()

# --- ЛОГИКА ГЕНЕРАЦИИ ---
async def generate_template_leonardo(theme, style, scene, count, gender, orientation):
    # Умный промпт
    subj = "A couple" if count == "couple" else ("A family" if count == "family" else ("A man" if gender == "man" else "A woman"))
    style_d = {"ussr": "Soviet 1970s gouache postcard", "vintage": "Vintage oil painting", "modern": "Digital art"}.get(style)
    prompt = f"{style_d}. {subj} at {scene}. Theme: {theme}. Highly detailed, festive atmosphere, nostalgic, clear faces."
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a",
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768
    }
    
    try:
        r = requests.post(f"{LEONARDO_API_URL}/generations", json=payload, headers=headers).json()
        gid = r.get("sdGenerationJob", {}).get("generationId")
        if not gid: return None

        for _ in range(50):
            await asyncio.sleep(4)
            res = requests.get(f"{LEONARDO_API_URL}/generations/{gid}", headers=headers).json()
            job = res.get("generations_by_pk") or (res.get("generations")[0] if res.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo Error: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    try:
        t_resp = requests.get(template_url)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        payload = {"input": {"source_image": user_photo_b64, "target_image": t_b64, "face_restore_model": "CodeFormer"}}
        res = requests.post(f"{RUNPOD_API_URL}/run", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}, json=payload).json()
        jid = res.get("id")
        for _ in range(50):
            await asyncio.sleep(3)
            status = requests.get(f"{RUNPOD_API_URL}/status/{jid}", headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"}).json()
            if status.get("status") == "COMPLETED":
                out = status.get("output")
                return base64.b64decode(out if isinstance(out, str) else out.get("image"))
    except Exception as e:
        logger.error(f"RunPod Error: {e}")
    return None

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_or_create_user(user.id, user.username, user.first_name)
    
    credits = 0
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user.id,))
            res = cur.fetchone()
            credits = res['credits'] if res else 0

    text = f"Привет, {user.first_name}! 🎨\nТвой баланс: {credits} открыток."
    keyboard = [
        [InlineKeyboardButton("🎨 Создать открытку", callback_data="create_postcard")],
        [InlineKeyboardButton("💰 Пополнить баланс", callback_data="show_pricing")],
        [InlineKeyboardButton("💬 Поддержка", callback_data="support")]
    ]
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def create_postcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📱 Вертикальная", callback_data="orient_vertical"), 
                 InlineKeyboardButton("🖼️ Горизонтальная", callback_data="orient_horizontal")]]
    await update.callback_query.edit_message_text("Выберите формат открытки:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['orientation'] = update.callback_query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"theme_{k}")] for k, v in THEMES.items()]
    await update.callback_query.edit_message_text("К какому празднику готовим?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['theme'] = update.callback_query.data.split('_')[1]
    keyboard = [
        [InlineKeyboardButton("👤 Один человек", callback_data="cnt_single")],
        [InlineKeyboardButton("👩‍❤️‍👨 Пара (М+Ж)", callback_data="cnt_couple")],
        [InlineKeyboardButton("👨‍👩‍👧‍👦 Семья / Группа", callback_data="cnt_family")]
    ]
    await update.callback_query.edit_message_text("Сколько людей на фото?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['count'] = update.callback_query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    await update.callback_query.edit_message_text("Выберите художественный стиль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['style'] = update.callback_query.data.split('_')[1]
    keyboard = [[InlineKeyboardButton(v, callback_data=f"scene_{k}")] for k, v in SCENES.items()]
    await update.callback_query.edit_message_text("Выберите место действия:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['scene'] = update.callback_query.data.split('_')[1]
    if context.user_data['count'] == 'single':
        keyboard = [[InlineKeyboardButton("👨 Мужчина", callback_data="g_man"), 
                     InlineKeyboardButton("👩 Женщина", callback_data="g_woman")]]
        await update.callback_query.edit_message_text("Кто на фото?", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        context.user_data['gender'] = 'mixed'
        await update.callback_query.edit_message_text("🎯 Почти готово! Теперь пришлите фото человека (или группы), чьи лица нужно перенести на открытку.")

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = "man" if "man" in update.callback_query.data else "woman"
    await update.callback_query.edit_message_text("🎯 Почти готово! Теперь пришлите фото лица.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not all(k in context.user_data for k in ['theme', 'style', 'scene']): return

    if not use_credit(user_id):
        await update.message.reply_text("❌ У вас закончились открытки. Пополните баланс через меню.")
        return

    msg = await update.message.reply_text("⏳ Магия началась! Создаю фон в Leonardo AI (40-60 сек)...")
    
    try:
        photo = await update.message.photo[-1].get_file()
        p_bytes = await photo.download_as_bytearray()
        u_b64 = base64.b64encode(p_bytes).decode('utf-8')

        t_url = await generate_template_leonardo(
            context.user_data['theme'], context.user_data['style'],
            context.user_data['scene'], context.user_data['count'],
            context.user_data['gender'], context.user_data['orientation']
        )
        
        if not t_url:
            await msg.edit_text("❌ Ошибка Leonardo. Кредит возвращен."); add_credits(user_id, 1); return

        await msg.edit_text("🔄 Фон готов! Теперь переношу лица через RunPod...")
        final_img = await faceswap_runpod(t_url, u_b64)
        
        if not final_img:
            await msg.edit_text("❌ Ошибка Face Swap. Кредит возвращен."); add_credits(user_id, 1); return

        # Легкая коррекция
        img = Image.open(io.BytesIO(final_img))
        img = ImageEnhance.Contrast(img).enhance(1.05)
        
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=95)
        
        await update.message.reply_photo(photo=out.getvalue(), caption="Ваша уникальная праздничная открытка готова! ✨")
        await msg.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("⚠️ Ошибка системы. Попробуйте еще раз.")
        add_credits(user_id, 1)

# --- ПЛАТЕЖИ И ПОДДЕРЖКА ---
async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_start")])
    await update.callback_query.edit_message_text("Выберите пакет:", reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pkg_id = update.callback_query.data.split('_')[1]
    pkg = PACKAGES[pkg_id]
    
    payment = Payment.create({
        "amount": {"value": str(pkg['price']), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": f"https://t.me/{(await context.bot.get_me()).username}"},
        "description": f"Покупка: {pkg['name']}",
        "metadata": {"user_id": update.effective_user.id, "count": pkg['count']}
    }, uuid.uuid4())
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO payments (user_id, payment_id, status, amount, count) VALUES (%s, %s, %s, %s, %s)",
                        (update.effective_user.id, payment.id, 'pending', pkg['price'], pkg['count']))
            conn.commit()
            
    keyboard = [[InlineKeyboardButton("💳 Оплатить", url=payment.confirmation.confirmation_url)],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_p_{payment.id}")]]
    await update.callback_query.edit_message_text(f"Счет на {pkg['price']}₽ готов.", reply_markup=InlineKeyboardMarkup(keyboard))

async def check_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p_id = update.callback_query.data.split('_')[2]
    payment = Payment.find_one(p_id)
    if payment.status == 'succeeded':
        uid = int(payment.metadata['user_id'])
        cnt = int(payment.metadata['count'])
        add_credits(uid, cnt)
        await update.callback_query.edit_message_text(f"✅ Успешно! Вам начислено {cnt} открыток.")
    else:
        await update.callback_query.answer("Оплата еще не поступила.", show_alert=True)

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_support'] = True
    await update.callback_query.edit_message_text("Напишите ваше сообщение, и админ ответит вам в ближайшее время.")

async def handle_support_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_support'): return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO support_tickets (user_id, message) VALUES (%s, %s)", 
                        (update.effective_user.id, update.message.text))
            conn.commit()
    await update.message.reply_text("Сообщение отправлено!")
    context.user_data.pop('waiting_support')

# --- АДМИН-КОМАНДЫ ---
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) as total FROM users")
            count = cur.fetchone()['total']
            await update.message.reply_text(f"Всего пользователей: {count}")

async def admin_add_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        _, uid, amt = update.message.text.split()
        add_credits(int(uid), int(amt))
        await update.message.reply_text(f"Добавлено {amt} кредитов пользователю {uid}")
    except:
        await update.message.reply_text("Формат: /addbalance ID AMOUNT")

# --- ЗАПУСК ---
def main():
    init_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("addbalance", admin_add_credits))
    
    # Callback-и
    app.add_handler(CallbackQueryHandler(start, pattern="back_to_start"))
    app.add_handler(CallbackQueryHandler(create_postcard, pattern="create_postcard"))
    app.add_handler(CallbackQueryHandler(handle_orientation, pattern="orient_"))
    app.add_handler(CallbackQueryHandler(handle_theme, pattern="theme_"))
    app.add_handler(CallbackQueryHandler(handle_count, pattern="cnt_"))
    app.add_handler(CallbackQueryHandler(handle_style, pattern="style_"))
    app.add_handler(CallbackQueryHandler(handle_scene, pattern="scene_"))
    app.add_handler(CallbackQueryHandler(handle_gender, pattern="g_"))
    app.add_handler(CallbackQueryHandler(show_pricing, pattern="show_pricing"))
    app.add_handler(CallbackQueryHandler(buy_package, pattern="buy_"))
    app.add_handler(CallbackQueryHandler(check_payment, pattern="check_p_"))
    app.add_handler(CallbackQueryHandler(support, pattern="support"))
    
    # Сообщения
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_support_msg))
    
    logger.info("🚀 Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
