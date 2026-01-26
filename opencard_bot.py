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

# ВАШ ID (уже прописан для бесплатных тестов)
ADMIN_ID = 610820340 

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

def init_db():
    """Инициализация базы данных без удаления данных"""
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
    logger.info("База данных готова к работе.")

async def generate_flux_image(image_url):
    """Генерация стилизованного изображения через FLUX.1-dev"""
    try:
        client = replicate.Client(api_token=REPLICATE_TOKEN)
        # Промпт настроен на глубокую зимнюю ретро-стилизацию
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
                "prompt_strength": 0.48, # Оптимальный баланс между стилем и сходством
                "num_inference_steps": 30,
                "output_format": "jpg"
            }
        )
        return output[0]
    except Exception as e:
        logger.error(f"Ошибка Replicate API: {e}")
        return None

def finalize_card(image_bytes):
    """Добавление рамки и праздничной надписи"""
    img = Image.open(io.BytesIO(image_bytes))
    # Добавляем поля: 30px по бокам и сверху, 130px снизу для текста
    img = ImageOps.expand(img, border=(30, 30, 30, 130), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("font.ttf", 60)
    except:
        font = ImageFont.load_default()
        logger.warning("Файл font.ttf не найден, используется стандартный шрифт.")
    
    w, h = img.size
    draw.text((w/2, h-65), "С Новым Годом!", font=font, fill="#8B0000", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def process_generation(update: Update,
