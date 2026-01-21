import os
import requests
import asyncio
import base64
import io
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")

# Промпт для Image-to-Image трансформации
# Важно: промпт описывает, во что превратить ЛЮДЕЙ НА ФОТО и ФОН
TRANSFORM_PROMPT = (
    "A group of people wearing vintage Soviet 1970s winter coats and fur hats, "
    "standing in a snowy retro city street or park, magical winter evening, "
    "USSR postcard style, gouache painting, soft artistic brushstrokes, "
    "authentic retro aesthetic, festive atmosphere, masterpiece illustration, "
    "clear faces, realistic textures, highly detailed."
)

async def upload_init_image_to_leonardo(image_bytes):
    """
    Загружает фото пользователя в Leonardo AI и возвращает его ID.
    Этот ID затем используется для Image-to-Image генерации.
    """
    upload_url = "https://cloud.leonardo.ai/api/rest/v1/init-image"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "accept": "application/json"
    }
    files = {
        'init_image': ('user_photo.jpg', image_bytes, 'image/jpeg')
    }
    
    try:
        response = requests.post(upload_url, headers=headers, files=files)
        response.raise_for_status() # Вызовет ошибку для HTTP 4xx/5xx
        result = response.json()
        
        init_image_id = result.get('init_image_id')
        if init_image_id:
            logger.info(f"Uploaded init image to Leonardo: {init_image_id}")
            return init_image_id
        else:
            logger.error(f"Leonardo init-image upload failed, no ID: {result}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error uploading init image to Leonardo: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in upload_init_image_to_leonardo: {e}")
        return None

async def generate_transformed_image(init_image_id):
    """
    Генерирует новую картинку на основе загруженного init_image ID.
    """
    if not init_image_id:
        return None
        
    generation_url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    
    payload = {
        "prompt": TRANSFORM_PROMPT,
        "width": 768,
        "height": 1024,
        "modelId": "6bef9f19-0ecf-4064-9b14-7e506e97328e", # Leonardo Diffusion (самая стабильная для init_image)
        "num_images": 1,
        "init_generation_image_id": init_image_id, # Используем ID загруженного фото
        "init_strength": 0.55, # 0.45-0.6 сохраняет сходство, но дает трансформацию
        "presetStyle": "ILLUSTRATION", # Подчеркиваем стиль
        "alchemy": True # Включаем алхимию для лучшего качества
    }
    
    try:
        r = requests.post(generation_url, json=payload, headers=headers).json()
        
        gen_data = r.get('sdGenerationJob') or r.get('generation_job')
        if not gen_data:
            logger.error(f"Leonardo Generation API Error: {r}")
            return None
            
        gen_id = gen_data.get('generationId')
        
        for _ in range(60): # Увеличим время ожидания, так как Image-to-Image дольше
            await asyncio.sleep(4)
            res = requests.get(f"{generation_url}/{gen_id}", headers=headers).json()
            
            g_data = res.get("generations_by_pk") or res.get("generations", [{}])[0]
            images = g_data.get("generated_images", [])
            
            if images:
                return images[0]['url']
            if g_data.get("status") == "FAILED":
                logger.error(f"Leonardo generation failed: {res}")
                return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error generating transformed image: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error in generate_transformed_image: {e}")
        return None
    return None

def add_frame_and_text(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(25, 25, 25, 95), fill='#FDFBF5') # Цвет старой бумаги
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("font.ttf", 55)
    except:
        font = ImageFont.load_default()
        
    w, h = img.size
    draw.text((w/2, h-50), "С Новым Годом!", font=font, fill="#A52A2A", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("✨ Готовлю зимний образ...")
    try:
        # 1. Получаем фото пользователя
        photo_file = await update.message.photo[-1].get_file()
        user_photo_bytes = await photo_file.download_as_bytearray()
        
        # 2. Загружаем фото в Leonardo и получаем ID
        await m.edit_text("📤 Загружаю ваше фото в нейросеть...")
        init_image_id = await upload_init_image_to_leonardo(user_photo_bytes)
        if not init_image_id:
            await m.edit_text("❌ Не удалось загрузить фото в Leonardo. Попробуйте еще раз.")
            return

        # 3. Трансформируем фото через Image-to-Image
        await m.edit_text("🎨 Перерисовываю вас в ретро-стиле...")
        transformed_image_url = await generate_transformed_image(init_image_id)
        if not transformed_image_url:
            await m.edit_text("❌ Не удалось создать открытку. Возможно, проблема с Leonardo.")
            return
            
        # 4. Скачиваем результат
        await m.edit_text("📥 Скачиваю готовую открытку...")
        transformed_image_bytes = requests.get(transformed_image_url).content

        # 5. Добавляем рамку и текст
        final_card = add_frame_and_text(transformed_image_bytes)
        
        await update.message.reply_photo(final_card, caption="🎉 Ваша ретро-открытка готова!")
        await m.delete()

    except Exception as e:
        logger.error(f"General Error in handle_photo: {e}")
        await m.edit_text("❌ Произошла ошибка. Попробуйте другое фото или позже.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Отправьте групповое фото или селфи, чтобы создать ретро-открытку!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
