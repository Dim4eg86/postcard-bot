import os, requests, io, logging, uuid, asyncio
import psycopg
from telegram import Update, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler
from PIL import Image, ImageDraw, ImageFont, ImageOps
import replicate
from yookassa import Configuration, Payment

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены и БД
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
REPLICATE_TOKEN = os.getenv("REPLICATE_API_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") # Railway выдает её автоматически
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

# Настройка БД
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

# Нейросеть FLUX
async def generate_flux_image(image_url):
    try:
        client = replicate.Client(api_token=REPLICATE_TOKEN)
        output = client.run(
            "black-forest-labs/flux-dev",
            input={
                "image": image_url,
                "prompt": "A nostalgic 1970s Soviet New Year postcard, happy couple in winter vintage clothes, snowy background, magical lights, gouache style.",
                "guidance": 3.0,
                "prompt_strength": 0.35, # Тот самый параметр для сходства лиц
                "num_inference_steps": 28
            }
        )
        return output[0]
    except Exception as e:
        logger.error(f"Replicate Error: {e}")
        return None

def finalize_card(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(30, 30, 30, 130), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("font.ttf", 60)
    except:
        font = ImageFont.load_default()
    draw.text((img.size[0]/2, img.size[1]-65), "С Новым Годом!", font=font, fill="#8B0000", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎄 Пришлите фото, и я превращу его в советскую открытку! (99 руб.)")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_id = update.message.photo[-1].file_id

    # Сохраняем в базу ID фото
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, last_photo_id) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET last_photo_id = EXCLUDED.last_photo_id", (user_id, photo_id))
            conn.commit()

    # Выставляем счет
    await update.message.reply_invoice(
        title="Новогодняя открытка",
        description="Генерация открытки нейросетью FLUX",
        payload="postcard_pay",
        provider_token=os.getenv("PAYMENT_TOKEN", ""), # Токен из BotFather
        currency="RUB",
        prices=[LabeledPrice("Оплата", 9900)]
    )

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def success_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    msg = await update.message.reply_text("💎 Оплата принята! Рисую шедевр (около 30 сек)...")
    
    # Берем данные из базы
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_photo_id FROM users WHERE user_id = %s", (user_id,))
            res = cur.fetchone()
            if not res: return
            file_id = res[0]

    file = await context.bot.get_file(file_id)
    gen_url = await generate_flux_image(file.file_path)

    if gen_url:
        img_data = requests.get(gen_url).content
        final_card = finalize_card(img_data)
        await update.message.reply_photo(final_card, caption="🎁 С Новым Годом!")
        await msg.delete()
    else:
        await update.message.reply_text("❌ Ошибка ИИ. Мы вернем деньги в ближайшее время.")

def main():
    init_db() # Инициализируем БД при старте
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, success_payment))
    app.run_polling()

if __name__ == "__main__":
    main()
