import os, requests, io, logging, asyncio, uuid
import psycopg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps
import replicate
from yookassa import Configuration, Payment

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
REPLICATE_TOKEN = os.getenv("REPLICATE_API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# ВАШ ID
ADMIN_ID = 610820340 

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

def init_db():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    last_photo_id TEXT,
                    paid BOOLEAN DEFAULT FALSE
                )
            """)
            conn.commit()
    logger.info("База данных готова.")

async def generate_flux_image(image_url):
    try:
        client = replicate.Client(api_token=REPLICATE_TOKEN)
        output = client.run(
            "black-forest-labs/flux-dev",
            input={
                "image": image_url,
                "prompt": (
                    "Authentic 1970s Soviet New Year postcard art. A man wearing a vintage "
                    "winter wool coat and a traditional fur hat. Magical snowy winter forest "
                    "background with festive lights. Soft gouache painting style, visible "
                    "artistic brushstrokes, nostalgic atmosphere, masterpiece."
                ),
                "guidance": 3.5,
                "prompt_strength": 0.48,
                "num_inference_steps": 30,
                "output_format": "jpg"
            }
        )
        return output[0]
    except Exception as e:
        logger.error(f"Ошибка Replicate API: {e}")
        return None

def finalize_card(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(30, 30, 30, 130), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("font.ttf", 60)
    except:
        font = ImageFont.load_default()
    
    w, h = img.size
    draw.text((w/2, h-65), "С Новым Годом!", font=font, fill="#8B0000", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def process_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    status_msg = await update.message.reply_text("🚀 Нейросеть переодевает вас в зимнее пальто... Пожалуйста, подождите.")
    try:
        file = await context.bot.get_file(file_id)
        gen_url = await generate_flux_image(file.file_path)

        if gen_url:
            await status_msg.edit_text("✨ Рисую рамку и поздравление...")
            img_data = requests.get(gen_url).content
            final_card = finalize_card(img_data)
            await update.message.reply_photo(final_card, caption="🎁 Ваша новогодняя ретро-открытка готова!")
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Не удалось сгенерировать изображение.")
    except Exception as e:
        logger.error(f"Ошибка при обработке фото: {e}")
        await status_msg.edit_text("❌ Произошла ошибка.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_id = update.message.photo[-1].file_id
    
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, last_photo_id) 
                VALUES (%s, %s) 
                ON CONFLICT (user_id) DO UPDATE SET last_photo_id = EXCLUDED.last_photo_id
            """, (user_id, photo_id))
            conn.commit()

    if user_id == ADMIN_ID:
        await process_generation(update, context, photo_id)
        return

    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        await update.message.reply_text("⚠️ Ошибка конфигурации платежей.")
        return

    idempotency_key = str(uuid.uuid4())
    payment = Payment.create({
        "amount": {"value": "99.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/your_bot_name"},
        "capture": True,
        "description": "Создание новогодней открытки"
    }, idempotency_key)

    keyboard = [[InlineKeyboardButton("💳 Оплатить 99 руб.", url=payment.confirmation.confirmation_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Для создания открытки оплатите заказ:", reply_markup=reply_markup)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("👋 Пришлите фото!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
