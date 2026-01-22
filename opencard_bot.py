import os, requests, asyncio, io, logging, json
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")

# Промпт для трансформации
TRANSFORM_PROMPT = (
    "A masterpiece vintage Soviet New Year postcard from 1970s. "
    "People in the image wearing authentic USSR winter coats and fur hats, "
    "standing in a snowy retro city park, magical evening light, "
    "gouache painting style, soft artistic textures, nostalgic atmosphere, "
    "highly detailed faces, festive illustration."
)

async def get_init_image_id(image_bytes):
    """Шаг 1: Загрузка изображения в Leonardo (API v1 style)"""
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    try:
        payload = {"extension": "jpg"}
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/init-image", json=payload, headers=headers)
        
        if r.status_code != 200:
            logger.error(f"Upload Error Stage 1: {r.text}")
            return None
            
        res_json = r.json()
        upload_data = res_json.get('uploadInitImage')
        if not upload_data: return None
            
        image_id = upload_data.get('id')
        upload_url = upload_data.get('url')
        raw_fields = upload_data.get('fields')
        fields = json.loads(raw_fields) if isinstance(raw_fields, str) else raw_fields

        # Загрузка в S3
        files = {'file': ('image.jpg', image_bytes, 'image/jpeg')}
        response = requests.post(upload_url, data=fields, files=files)
        
        if response.status_code in [200, 204]:
            logger.info(f"Photo uploaded. ID: {image_id}")
            await asyncio.sleep(12) # Ждем синхронизации
            return image_id
    except Exception as e:
        logger.error(f"Upload global error: {e}")
    return None

async def generate_transformed_image(init_image_id):
    """Шаг 2: Генерация БЕЗ modelId для обхода ошибки 'model not supported'"""
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # Мы убрали modelId и Alchemy. Leonardo возьмет настройки из вашего профиля.
    payload = {
        "prompt": TRANSFORM_PROMPT,
        "width": 768,
        "height": 1024,
        "num_images": 1,
        "init_generation_image_id": init_image_id,
        "init_strength": 0.50,
        "promptMagic": True
    }
    
    for attempt in range(5):
        try:
            r = requests.post(url, json=payload, headers=headers).json()
            gen_data = r.get('sdGenerationJob') or r.get('generation_job')
            
            if gen_data:
                gen_id = gen_data.get('generationId')
                # Опрос статуса (до 5 минут)
                for _ in range(75):
                    await asyncio.sleep(4)
                    res = requests.get(f"{url}/{gen_id}", headers=headers).json()
                    data = res.get("generations_by_pk") or res.get("generations", [{}])[0]
                    images = data.get("generated_images", [])
                    if images: return images[0]['url']
                    if data.get("status") == "FAILED": return None
                return None

            if "invalid" in str(r).lower():
                wait = (attempt + 1) * 10
                logger.warning(f"Syncing ID... Attempt {attempt+1}")
                await asyncio.sleep(wait)
                continue
            else:
                logger.error(f"Leonardo API Error: {r}")
                return None
        except Exception as e:
            logger.error(f"Gen loop error: {e}")
            break
    return None

def finalize_card(image_bytes):
    """Шаг 3: Наложение рамки и текста"""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(25, 25, 25, 110), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("font.ttf", 55)
    except:
        font = ImageFont.load_default()
    draw.text((img.size[0]/2, img.size[1]-55), "С Новым Годом!", font=font, fill="#8B0000", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("❄️ Магия начинается...")
    try:
        photo = await update.message.photo[-1].get_file()
        img_bytes = await photo.download_as_bytearray()
        
        init_id = await get_init_image_id(img_bytes)
        if not init_id:
            return await m.edit_text("❌ Ошибка загрузки.")

        await m.edit_text("🎨 Рисую ваш ретро-образ...")
        res_url = await generate_transformed_image(init_id)
        
        if res_url:
            image_data = requests.get(res_url).content
            final_card = finalize_card(image_data)
            await update.message.reply_photo(final_card, caption="🎄 Открытка готова!")
            await m.delete()
        else:
            await m.edit_text("❌ Ошибка генерации. Попробуйте еще раз.")
    except Exception as e:
        logger.error(f"Handler error: {e}")
        await m.edit_text("❌ Произошла ошибка.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Пришлите фото!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
