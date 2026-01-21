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

# Промпт для трансформации (Image-to-Image)
TRANSFORM_PROMPT = (
    "A masterpiece vintage Soviet New Year postcard from 1970s. "
    "People in the image wearing authentic USSR winter coats and fur hats, "
    "standing in a snowy retro city park, magical evening light, "
    "gouache painting style, soft artistic textures, nostalgic atmosphere, "
    "highly detailed faces, festive illustration."
)

async def get_init_image_id(image_bytes):
    """Загрузка изображения в Leonardo через Presigned URL"""
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    
    try:
        # 1. Получаем URL для загрузки
        payload = {"extension": "jpg"}
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/init-image", json=payload, headers=headers).json()
        
        upload_data = r.get('uploadInitImage')
        if not upload_data:
            logger.error(f"Leonardo Response Error: {r}")
            return None
            
        image_id = upload_data.get('id')
        upload_url = upload_data.get('url')
        # Безопасно парсим поля для S3
        fields = json.loads(upload_data.get('fields'))

        # 2. Сама загрузка файла в хранилище Leonardo
        files = {'file': image_bytes}
        response = requests.post(upload_url, data=fields, files=files)
        
        if response.status_code in [200, 204]:
            logger.info(f"Successfully uploaded image to Leonardo. ID: {image_id}")
            return image_id
        else:
            logger.error(f"S3 Upload failed: {response.status_code}")
    except Exception as e:
        logger.error(f"Error in get_init_image_id: {e}")
    return None

async def generate_transformed_image(init_image_id):
    """Создание новой картинки на основе загруженной"""
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "prompt": TRANSFORM_PROMPT,
        "width": 768,
        "height": 1024,
        "modelId": "6b777458-2498-421d-9db1-13c21a952680", # Leonardo Phoenix
        "num_images": 1,
        "init_generation_image_id": init_image_id,
        "init_strength": 0.52, # Баланс: сохраняем лица, но меняем одежду/фон
        "alchemy": True,
        "presetStyle": "ILLUSTRATION"
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers).json()
        gen_id = r.get('sdGenerationJob', {}).get('generationId')
        
        if not gen_id:
            logger.error(f"Generation failed to start: {r}")
            return None
            
        for _ in range(60): # Ожидание до 4 минут
            await asyncio.sleep(4)
            status_res = requests.get(f"{url}/{gen_id}", headers=headers).json()
            
            gen = status_res.get("generations_by_pk") or status_res.get("generations", [{}])[0]
            images = gen.get("generated_images", [])
            
            if images:
                return images[0]['url']
            if gen.get("status") == "FAILED":
                return None
    except Exception as e:
        logger.error(f"Error in generate_transformed_image: {e}")
    return None

def finalize_card(image_bytes):
    """Добавление ретро-рамки и надписи"""
    img = Image.open(io.BytesIO(image_bytes))
    # Делаем рамку снизу побольше для текста
    img = ImageOps.expand(img, border=(25, 25, 25, 100), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("font.ttf", 55)
    except:
        font = ImageFont.load_default()
        
    w, h = img.size
    # Красивый бордовый цвет для текста
    draw.text((w/2, h-50), "С Новым Годом!", font=font, fill="#8B0000", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("🪄 Магия начинается: готовлю ваш ретро-образ...")
    try:
        # Качаем фото
        photo = await update.message.photo[-1].get_file()
        img_bytes = await photo.download_as_bytearray()
        
        # Загружаем в Leonardo
        await m.edit_text("📤 Загрузка в нейросеть...")
        init_id = await get_init_image_id(img_bytes)
        if not init_id:
            return await m.edit_text("❌ Ошибка загрузки. Попробуйте другое фото.")

        # Генерируем
        await m.edit_text("🎨 Рисую открытку (это может занять минуту)...")
        res_url = await generate_transformed_image(init_id)
        if not res_url:
            return await m.edit_text("❌ Нейросеть не смогла обработать фото.")
            
        # Завершаем оформление
        await m.edit_text("✨ Накладываю финальные штрихи...")
        final_bytes = finalize_card(requests.get(res_url).content)
        
        await update.message.reply_photo(final_bytes, caption="🎄 Ваша персональная советская открытка готова!")
        await m.delete()

    except Exception as e:
        logger.error(f"General error: {e}")
        await m.edit_text("❌ Что-то пошло не так. Попробуйте еще раз.")

def main():
    # Настройка приложения с автоматической обработкой конфликтов
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Пришлите фото (групповое или селфи) для создания открытки!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    
    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True) # Очищаем старые сообщения при запуске

if __name__ == "__main__":
    main()
