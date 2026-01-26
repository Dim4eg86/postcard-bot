import os, requests, io, logging, asyncio
import psycopg
from telegram import Update, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler
from PIL import Image, ImageDraw, ImageFont, ImageOps
import replicate
from yookassa import Configuration

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация переменных
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
REPLICATE_TOKEN = os.getenv("REPLICATE_API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN", "") # Токен из BotFather
ADMIN_ID = 123456789  # ЗАМЕНИТЕ ЭТО ЧИСЛО НА ВАШ ID ДЛЯ БЕСПЛАТНЫХ ТЕСТОВ

# Настройка БД с принудительным обновлением структуры
def init_db():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # ВАЖНО: Удаляем старую таблицу, если она была создана неправильно
            cur.execute("DROP TABLE IF EXISTS users CASCADE;") 
            cur.execute("""
                CREATE TABLE users (
                    user_id BIGINT PRIMARY KEY,
                    last_photo_id TEXT,
                    paid BOOLEAN DEFAULT FALSE
                )
            """)
            conn.commit()
    logger.info("Database initialized with correct columns.")

# Генерация через FLUX
async def generate_flux_image(image_url):
    try:
        client = replicate.Client(api_token=REPLICATE_TOKEN)
        output = client.run(
            "black-forest-labs/flux-dev",
            input={
                "image": image_url,
                "prompt": "A nostalgic 1970s Soviet New Year postcard, happy couple in winter vintage clothes, snowy background, magical lighting, gouache art style, masterpiece.",
                "guidance": 3.0,
                "prompt_strength": 0.35, # Баланс сходства лиц
                "num_inference_steps": 28
            }
        )
        return output[0]
    except Exception as e:
        logger.error(f"Replicate Error: {e}")
        return None

# Оформление открытки
def finalize_card(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(30, 30, 30, 130), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("font.ttf", 60)
    except:
        font = ImageFont.load_default()
    
    text = "С Новым Годом!"
    w, h = img.size
    draw.text((w/2, h-65), text, font=font, fill="#8B0000", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎄 Пришлите фото, и я превращу его в новогоднюю открытку! (Цена: 99 руб.)")

async def process_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    msg = await update.message.reply_text("🎨 Нейросеть начала работу. Пожалуйста, подождите около 30 секунд...")
    file = await context.bot.get_file(file_id)
    gen_url = await generate_flux_image(file.file_path)

    if gen_url:
        await msg.edit_text("✨ Почти готово, накладываю поздравление...")
        img_data = requests.get(gen_url).content
        final_card = finalize_card(img_data)
        await update.message.reply_photo(final_card, caption="🎁 Ваша уникальная открытка!")
        await msg.delete()
    else:
        await msg.edit_text("❌ Ошибка генерации. Если вы платили, свяжитесь с поддержкой для возврата.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_id = update.message.photo[-1].file_id

    # Сохраняем фото в базу
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, last_photo_id) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET last_photo_id = EXCLUDED.last_photo_id", (user_id, photo_id))
            conn.commit()

    # Проверка на АДМИНА (бесплатный тест)
    if user_id == ADMIN_ID:
        await update.message.reply_text("👑 Режим администратора: Тестовая генерация БЕСПЛАТНО.")
        await process_generation(update, context, photo_id)
        return

    # Для обычных пользователей - счет
    await update.message.reply_invoice(
        title="Новогодняя открытка",
        description="Генерация открытки через FLUX AI",
        payload="postcard_pay",
        provider_token=PAYMENT_TOKEN,
        currency="RUB",
        prices=[LabeledPrice("Создание", 9900)]
    )

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def success_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_photo_id FROM users WHERE user_id = %s", (user_id,))
            res = cur.fetchone()
            if res:
                await process_generation(update, context, res[0])

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, success_payment))
    
    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
