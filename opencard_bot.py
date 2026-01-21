import os
import requests
import asyncio
import base64
import io
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Настройка логов
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

# Идеальный промпт для ретро-стиля
RETRO_PROMPT = (
    "Vintage Soviet New Year postcard, 1960s USSR style, gouache painting, "
    "soft artistic brushstrokes, nostalgic winter scene, masterpiece, "
    "offset printing texture, slightly faded colors, authentic retro illustration."
)

async def generate_retro_background():
    """Генерирует художественный фон в Leonardo AI"""
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "prompt": RETRO_PROMPT,
        "modelId": "e71a1c2f-4f21-42d1-9b7a-91d01747c304", # Leonardo Illustration model
        "width": 768,
        "height": 1024,
        "num_images": 1,
        "alchemy": True,
        "presetStyle": "ILLUSTRATION"
    }
    try:
        r = requests.post(url, json=payload, headers=headers).json()
        gen_id = r['sdGenerationJob']['generationId']
        for _ in range(40):
            await asyncio.sleep(3)
            res = requests.get(f"{url}/{gen_id}", headers=headers).json()
            images = res.get("generations_by_pk", {}).get("generated_images", [])
            if images:
                return images[0]['url']
    except Exception as e:
        logger.error(f"Leonardo Error: {e}")
    return None

async def runpod_face_swap(target_url, source_b64):
    """Заменяет лицо через RunPod с поддержкой S3 и ссылок"""
    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
    
    # Передаем ссылку на Leonardo напрямую воркеру
    payload = {
        "input": {
            "source_image": source_b64,
            "target_image": target_url,
            "face_restore": True,
            "upscale": 1 # Важно: UPSCALE=1 в коде и в ENV для стабильности
        }
    }
    
    try:
        # Запуск задачи
        run = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        job_id = run.get("id")
        if not job_id: return None

        # Опрос результата
        for _ in range(60):
            await asyncio.sleep(3)
            status_res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}", headers=headers).json()
            if status_res.get("status") == "COMPLETED":
                output = status_res.get("output")
                
                # Обработка S3 ссылки или Base64
                if isinstance(output, dict):
                    res_url = output.get("image_url") or output.get("image")
                    if res_url and res_url.startswith("http"):
                        return requests.get(res_url).content
                    if res_url:
                        return base64.b64decode(res_url.split(",")[-1])
                return base64.b64decode(output.split(",")[-1])
                
            if status_res.get("status") in ["FAILED", "CANCELLED"]:
                logger.error(f"RunPod failed: {status_res}")
                return None
    except Exception as e:
        logger.error(f"RunPod Error: {e}")
    return None

def apply_final_styling(image_bytes):
    """Добавляет рамку и аутентичную надпись"""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(25, 25, 25, 80), fill='#F4F1EA') # Кремовый фон бумаги
    draw = ImageDraw.Draw(img)
    
    # Попытка загрузить красивый шрифт
    try:
        font = ImageFont.truetype("font.ttf", 55)
    except:
        font = ImageFont.load_default()
        
    w, h = img.size
    # Красный классический цвет для надписи
    draw.text((w/2, h-45), "С Новым Годом!", font=font, fill="#C21807", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎄 Пришлите мне ваше фото, и я превращу его в настоящую советскую открытку!")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("🪄 Начинаю колдовать над открыткой...")
    try:
        # Получаем фото пользователя
        file = await update.message.photo[-1].get_file()
        user_img_bytes = await file.download_as_bytearray()
        user_b64 = base64.b64encode(user_img_bytes).decode('utf-8')

        # 1. Генерация фона
        await m.edit_text("🎨 Рисую фон в стиле ретро...")
        bg_url = await generate_retro_background()
        if not bg_url: raise Exception("Background generation failed")

        # 2. Face Swap
        await m.edit_text("👤 Вписываю лицо в картину...")
        swapped_bytes = await runpod_face_swap(bg_url, user_b64)
        if not swapped_bytes: raise Exception("Face swap failed")

        # 3. Финальный дизайн
        final_card = apply_final_styling(swapped_bytes)
        
        await update.message.reply_photo(final_card, caption="✨ Ваша уникальная ретро-открытка готова!")
        await m.delete()

    except Exception as e:
        logger.error(f"General Error: {e}")
        await m.edit_text("❌ Произошла ошибка. Попробуйте другое фото или проверьте настройки RunPod.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
