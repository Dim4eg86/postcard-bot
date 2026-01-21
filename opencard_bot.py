import os, requests, asyncio, io, logging, json
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения (подтягиваются из Railway)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")

# Промпт для трансформации (Image-to-Image)
TRANSFORM_PROMPT = (
    "A masterpiece vintage Soviet New Year postcard from 1970s. "
    "People in the image wearing authentic USSR winter coats and fur hats, "
    "standing in a snowy retro city park, magical evening light, "
    "gouache painting style, soft artistic textures, nostalgic atmosphere, "
    "highly detailed faces, festive illustration."
)

async def get_init_image_id(image_bytes):
    """Шаг 1: Загрузка изображения в Leonardo через Presigned URL"""
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    
    try:
        # 1. Запрашиваем данные для загрузки
        payload = {"extension": "jpg"}
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/init-image", json=payload, headers=headers).json()
        
        upload_data = r.get('uploadInitImage')
        if not upload_data:
            logger.error(f"Leonardo Init Error: {r}")
            return None
            
        image_id = upload_data.get('id')
        upload_url = upload_data.get('url')
        # Парсим поля для S3. Используем json.loads для безопасности
        fields = json.loads(upload_data.get('fields'))

        # 2. Загружаем бинарный файл в хранилище
        files = {'file': image_bytes}
        response = requests.post(upload_url, data=fields, files=files)
        
        if response.status_code in [200, 204]:
            logger.info(f"Successfully uploaded image. ID: {image_id}")
            return image_id
        else:
            logger.error(f"S3 Upload failed with status: {response.status_code}")
    except Exception as e:
        logger.error(f"Error in get_init_image_id: {e}")
    return None

async def generate_transformed_image(init_image_id):
    """Шаг 2: Создание новой картинки на основе загруженной (Image-to-Image)"""
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "prompt": TRANSFORM_PROMPT,
        "width": 768,
        "height": 1024,
        # Используем максимально совместимую модель Leonardo Diffusion
        "modelId": "6bef9f19-0ecf-4064-9b14-7e506e97328e", 
        "num_images": 1,
        "init_generation_image_id": init_image_id,
        "init_strength": 0.50, # 0.5 — золотая середина между сходством и стилизацией
        "promptMagic": True
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers).json()
        
        # Получаем ID генерации (универсальный поиск ключа)
        gen_data = r.get('sdGenerationJob') or r.get('generation_job')
        if not gen_data:
            logger.error(f"Generation failed to start: {r}")
            return None
            
        gen_id = gen_data.get('generationId')
        
        # Опрос готовности результата
        for _ in range(60):
            await asyncio.sleep(4)
            status_res = requests.get(f"{url}/{gen_id}", headers=headers).json()
            
            # Парсим ответ
            data = status_res.get("generations_by_pk") or status_res.get("generations", [{}])[0]
            images = data.get("generated_images", [])
            
            if images:
                return images[0]['url']
            
            if data.get("status") == "FAILED":
                logger.error(f"Leonardo job failed: {status_res}")
                return None
    except Exception as e:
        logger.error(f"Error in generate_transformed_image: {e}")
    return None

def finalize_card(image_bytes):
    """Шаг 3: Наложение рамки и праздничной надписи"""
    img = Image.open(io.BytesIO(image_bytes))
    # Создаем классическую рамку открытки
    img = ImageOps.expand(img, border=(25, 25, 25, 100), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    
    # Пытаемся загрузить шрифт, иначе используем стандартный
    try:
        font = ImageFont.truetype("font.ttf", 55)
    except:
        font = ImageFont.load_default()
        
    w, h = img.size
    # Пишем текст внизу по центру
    draw.text((w/2, h-50), "С Новым Годом!", font=font, fill="#8B0000", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("🪄 Магия начинается: готовлю ваш ретро-образ...")
    try:
        # Скачиваем фото пользователя
        photo_file = await update.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        
        # 1. Загружаем в Leonardo
        await m.edit_text("📤 Загрузка фото в нейросеть...")
        init_id = await get_init_image_id(img_bytes)
        if not init_id:
            return await m.edit_text("❌ Ошибка при передаче фото. Попробуйте еще раз.")

        # 2. Генерируем трансформированное изображение
        await m.edit_text("🎨 Нейросеть перерисовывает вас (это займет около минуты)...")
        res_url = await generate_transformed_image(init_id)
        if not res_url:
            return await m.edit_text("❌ Нейросеть не смогла обработать фото. Попробуйте другое.")
            
        # 3. Финализация (рамка и текст)
        await m.edit_text("✨ Финальные штрихи...")
        final_img_bytes = finalize_card(requests.get(res_url).content)
        
        await update.message.reply_photo(final_img_bytes, caption="🎄 Ваша персональная советская открытка готова!")
        await m.delete()

    except Exception as e:
        logger.error(f"Handler error: {e}")
        await m.edit_text("❌ Произошла ошибка. Попробуйте отправить другое фото.")

def main():
    # Инициализация бота
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Пришлите фото (групповое или селфи) для создания ретро-открытки!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    logger.info("Бот запущен...")
    # drop_pending_updates помогает избежать конфликтов при перезапуске
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
