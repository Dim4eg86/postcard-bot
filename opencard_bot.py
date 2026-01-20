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
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    message TEXT,
                    status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

# --- ЛОГИКА НЕЙРОСЕТЕЙ ---
async def generate_template_leonardo(theme, style, scene, count, gender, orientation):
    subj = "Couple" if count == "couple" else ("Family" if count == "family" else ("Man" if gender == "man" else "Woman"))
    style_p = {"ussr": "Soviet 1970s gouache postcard", "vintage": "Oil painting", "modern": "Digital art"}.get(style)
    prompt = f"{style_p}, {subj} in {scene}, theme {THEMES.get(theme, 'Holiday')}, festive, high detail, sharp focus."
    
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": prompt,
        "modelId": "6bef9f1b-7448-4db4-b43d-8b3bc778c04a",
        "width": 768 if orientation == "vertical" else 1024,
        "height": 1024 if orientation == "vertical" else 768,
        "num_images": 1
    }
    try:
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/generations", json=payload, headers=headers)
        if r.status_code != 200:
            logger.error(f"Leonardo Error: {r.text}")
            return None
        gid = r.json().get("sdGenerationJob", {}).get("generationId")
        for _ in range(45):
            await asyncio.sleep(4)
            st = requests.get(f"https://cloud.leonardo.ai/api/rest/v1/generations/{gid}", headers=headers).json()
            job = st.get("generations_by_pk") or (st.get("generations")[0] if st.get("generations") else None)
            if job and job.get("status") == "COMPLETE":
                return job.get("generated_images")[0].get("url")
    except Exception as e:
        logger.error(f"Leonardo exception: {e}")
    return None

async def faceswap_runpod(template_url, user_photo_b64):
    """Оптимизировано для Serverless (0 воркеров) с ожиданием прогрева до 5 минут"""
    try:
        t_resp = requests.get(template_url)
        t_b64 = base64.b64encode(t_resp.content).decode('utf-8')
        
        payload = {"input": {"source_image": user_photo_b64, "target_image": t_b64, "face_restore_model": "CodeFormer"}}
        headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}"}
        
        res = requests.post(f"{RUNPOD_API_URL}/run", json=payload, headers=headers).json()
        job_id = res.get("id")
        
        # Ждем до 300 секунд (100 итераций по 3 сек), чтобы сервер успел проснуться
        for i in range(100):
            await asyncio.sleep(3)
            status_res = requests.get(f"{RUNPOD_API_URL}/status/{job_id}", headers=headers).json()
            status = status_res.get("status")
            
            if status == "COMPLETED":
                output = status_res.get("output")
                img_data = output if isinstance(output, str) else output.get("image")
                return base64.b64decode(img_data)
            
            if status == "FAILED":
                logger.error(f"RunPod FAILED: {status_res}")
                return None
                
            if i % 10 == 0:
                logger.info(f"RunPod still working... Job ID: {job_id} Status: {status}")
                
    except Exception as e:
        logger.error(f"RunPod exception: {e}")
    return None

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING", 
                        (user.id, user.username, user.first_name))
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user.id,))
            c = cur.fetchone()['credits']
            conn.commit()

    kb = [[InlineKeyboardButton("🎨 Создать открытку", callback_data="create_postcard")],
          [InlineKeyboardButton("💰 Пополнить баланс", callback_data="show_pricing")],
          [InlineKeyboardButton("💬 Поддержка", callback_data="support")]]
    
    text = f"Привет, {user.first_name}! 🎨\nТвой баланс: {c} открыток."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def create_postcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("📱 Вертикальная", callback_data="orient_vertical"), 
           InlineKeyboardButton("🖼️ Горизонтальная", callback_data="orient_horizontal")]]
    await update.callback_query.edit_message_text("Выберите формат открытки:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['orientation'] = update.callback_query.data.split('_')[1]
    kb = [[InlineKeyboardButton(v, callback_data=f"theme_{k}")] for k, v in THEMES.items()]
    await update.callback_query.edit_message_text("К какому празднику готовим?", reply_markup=InlineKeyboardMarkup(kb))

async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['theme'] = update.callback_query.data.split('_')[1]
    kb = [[InlineKeyboardButton("👤 Один человек", callback_data="cnt_single")],
          [InlineKeyboardButton("👩‍❤️‍👨 Пара (М+Ж)", callback_data="cnt_couple")],
          [InlineKeyboardButton("👨‍👩‍👧‍👦 Семья / Группа", callback_data="cnt_family")]]
    await update.callback_query.edit_message_text("Сколько людей на фото?", reply_markup=InlineKeyboardMarkup(kb))

async def handle_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['count'] = update.callback_query.data.split('_')[1]
    kb = [[InlineKeyboardButton(v, callback_data=f"style_{k}")] for k, v in STYLES.items()]
    await update.callback_query.edit_message_text("Выберите художественный стиль:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['style'] = update.callback_query.data.split('_')[1]
    kb = [[InlineKeyboardButton(v, callback_data=f"scene_{k}")] for k, v in SCENES.items()]
    await update.callback_query.edit_message_text("Выберите место действия:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['scene'] = update.callback_query.data.split('_')[1]
    if context.user_data.get('count') == 'single':
        kb = [[InlineKeyboardButton("👨 Мужчина", callback_data="g_man"), InlineKeyboardButton("👩 Женщина", callback_data="g_woman")]]
        await update.callback_query.edit_message_text("Кто на фото?", reply_markup=InlineKeyboardMarkup(kb))
    else:
        context.user_data['gender'] = 'mixed'
        await update.callback_query.edit_message_text("🎯 Теперь пришлите фото человека (или группы).")

async def handle_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['gender'] = update.callback_query.data.split('_')[1]
    await update.callback_query.edit_message_text("🎯 Почти готово! Теперь пришлите фото лица.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if 'theme' not in context.user_data: return

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
            res = cur.fetchone()
            if (not res or res['credits'] <= 0) and user_id != ADMIN_ID:
                await update.message.reply_text("❌ У вас закончились открытки. Пополните баланс."); return
            if user_id != ADMIN_ID:
                cur.execute("UPDATE users SET credits = credits - 1 WHERE user_id = %s", (user_id,))
            conn.commit()

    msg = await update.message.reply_text("⏳ Магия началась! Генерирую фон (40-60 сек)...")
    
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
            await msg.edit_text("❌ Ошибка Leonardo. Кредит возвращен."); 
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (user_id,))
                conn.commit()
            return

        await msg.edit_text("🔄 Фон готов! Вклеиваю лица (может занять до 2-3 мин, если сервер спит)...")
        final_img = await faceswap_runpod(t_url, u_b64)
        
        if not final_img:
            await msg.edit_text("❌ Ошибка Face Swap. Кредит возвращен.");
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("UPDATE users SET credits = credits + 1 WHERE user_id = %s", (user_id,))
                conn.commit()
            return

        await update.message.reply_photo(photo=final_img, caption="Ваша уникальная открытка готова! ✨")
        await msg.delete()
        context.user_data.clear()
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("⚠️ Ошибка системы. Попробуйте еще раз.")

# --- ПЛАТЕЖИ И ПОДДЕРЖКА ---
async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(f"{v['name']} - {v['price']}₽", callback_data=f"buy_{k}")] for k, v in PACKAGES.items()]
    kb.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_start")])
    await update.callback_query.edit_message_text("Выберите пакет:", reply_markup=InlineKeyboardMarkup(kb))

async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pkg_id = update.callback_query.data.split('_')[1]
    pkg = PACKAGES[pkg_id]
    
    payment = Payment.create({
        "amount": {"value": str(pkg['price']), "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/your_bot_username"},
        "description": f"Покупка: {pkg['name']}",
        "metadata": {"user_id": update.effective_user.id, "count": pkg['count']}
    }, uuid.uuid4())
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO payments (user_id, payment_id, status, amount, count) VALUES (%s, %s, %s, %s, %s)",
                        (update.effective_user.id, payment.id, 'pending', pkg['price'], pkg['count']))
            conn.commit()
            
    kb = [[InlineKeyboardButton("💳 Оплатить", url=payment.confirmation.confirmation_url)],
          [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_p_{payment.id}")]]
    await update.callback_query.edit_message_text(f"Счет на {pkg['price']}₽ создан.", reply_markup=InlineKeyboardMarkup(kb))

async def check_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p_id = update.callback_query.data.split('_')[2]
    try:
        payment = Payment.find_one(p_id)
        if payment.status == 'succeeded':
            uid = int(payment.metadata['user_id'])
            cnt = int(payment.metadata['count'])
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET credits = credits + %s WHERE user_id = %s", (cnt, uid))
                    cur.execute("UPDATE payments SET status = 'succeeded' WHERE payment_id = %s", (p_id,))
                    conn.commit()
            await update.callback_query.edit_message_text(f"✅ Успешно! Вам начислено {cnt} открыток.")
        else:
            await update.callback_query.answer("Оплата еще не поступила.", show_alert=True)
    except Exception:
        await update.callback_query.answer("Ошибка при проверке.")

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_support'] = True
    await update.callback_query.edit_message_text("Напишите ваше сообщение, и админ ответит вам.")

async def handle_support_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_support'): return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO support_tickets (user_id, message) VALUES (%s, %s)", 
                        (update.effective_user.id, update.message.text))
            conn.commit()
    await update.message.reply_text("✅ Сообщение отправлено!")
    context.user_data.pop('waiting_support')

# --- АДМИН-КОМАНДЫ ---
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users")
            u_count = cur.fetchone()[0]
            await update.message.reply_text(f"📊 Статистика:\nВсего пользователей: {u_count}")

# --- ЗАПУСК ---
def main():
    init_database()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", admin_stats))
    
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
    app.add_handler(CallbackQueryHandler(check_payment_callback, pattern="check_p_"))
    app.add_handler(CallbackQueryHandler(support, pattern="support"))
    
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_support_msg))
    
    logger.info("🚀 Бот запущен в режиме Serverless (RunPod 0 workers)!")
    app.run_polling()

if __name__ == "__main__":
    main()
