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

async def generate_and_swap_face(source_image_url):
    """
    Двухэтапная логика:
    1. Генерация качественного ретро-фона с персонажем.
    2. FaceSwap вашего лица на этот фон.
    """
    client = replicate.Client(api_token=REPLICATE_TOKEN)

    # Шаг 1: Генерация базовой открытки (Модель Flux Schnell — быстро и надежно)
    try:
        logger.info("Запуск Шага 1: Генерация фона...")
        base_postcard_output = client.run(
            "black-forest-labs/flux-schnell",
            input={
                "prompt": (
                    "Authentic 1970s Soviet New Year postcard style, gouache painting art. "
                    "A happy man smiling, wearing a vintage thick winter wool coat and a traditional "
                    "fur hat, standing in a magical snowy winter forest with festive lights, "
                    "soft brushstrokes, nostalgic masterpiece."
                ),
                "aspect_ratio": "1:1",
                "output_format": "webp"
            }
        )
        base_postcard_url = base_postcard_output[0]
        logger.info(f"Фон успешно создан: {base_postcard_url}")
    except Exception as e:
        logger.error(f"Ошибка на Шаге 1: {e}")
        return None

    # Шаг 2: FaceSwap (Перенос лица)
    try:
        logger.info("Запуск Шага 2: FaceSwap...")
        faceswap_output = client.run(
            "lucataco/faceswap:9a429845307f79440628bc58b2d5bdce836a57497d4bc807353f478a54160408",
            input={
                "target_image": base_postcard_url,
                "swap_image": source_image_url,
                "face_detector_confidence": 0.6
            }
        )
        return faceswap_output
    except Exception as e:
        logger.error(f"Ошибка на Шаге 2 (FaceSwap): {e}")
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
    status_msg = await update.message.reply_text("⏳ Начинаю волшебство... Шаг 1/2 (создание фона)")
    try:
        file = await context.bot.get_file(file_id)
        source_image_url = file.file_path 

        final_gen_url = await generate_and_swap_face(source_image_url)

        if final_gen_url:
            await status_msg.edit_text("✨ Шаг 2/2: Переношу ваше лицо на открытку...")
            img_data = requests.get(final_gen_url).content
            final_card = finalize_card(img_data)
            await update.message.reply_photo(final_card, caption="🎁 Ваша новогодняя открытка готова!")
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Ошибка генерации. Попробуйте еще раз.")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await status_msg.edit_text("❌ Произошла ошибка на стороне сервера.")

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

    idempotency_key = str(uuid.uuid4())
    payment = Payment.create({
        "amount": {"value": "99.00", "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": "https://t.me/postcard_aibot"},
        "capture": True,
        "description": "Новогодняя открытка"
    }, idempotency_key)

    keyboard = [[InlineKeyboardButton("💳 Оплатить 99 руб.", url=payment.confirmation.confirmation_url)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Чтобы получить открытку, оплатите заказ:", reply_markup=reply_markup)

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("🎄 Пришлите фото, и я сделаю из него ретро-открытку!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
