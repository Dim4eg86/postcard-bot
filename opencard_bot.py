import os
import requests
import asyncio
import base64
import io
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфиг (Railway)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

# Промпт для художественного стиля
RETRO_PROMPT = (
    "Vintage Soviet New Year postcard, 1960s USSR style, gouache painting, "
    "soft artistic brushstrokes, nostalgic winter scene, masterpiece, "
    "offset printing texture, slightly faded colors, authentic retro illustration."
)

async def generate_retro_background():
    """Исправленная генерация фона через Leonardo AI"""
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    payload = {
        "prompt": RETRO_PROMPT,
        "width": 768,
        "height": 1024,
        "num_images": 1,
        "alchemy": True,
        "presetStyle": "ILLUSTRATION",
        "modelId": "e71a1c2f-4f21-42d1-9b7a-91d01747c304" # Illustration model
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers).json()
        # Исправленный поиск ID генерации
        gen_data = r.get('sdGenerationJob') or r.get('generation_job')
        if not gen_data:
            logger.error(f"Unexpected Leonardo response: {r}")
            return None
            
        gen_id = gen_data.get('generationId')
        
        for _ in range(40):
            await asyncio.sleep(4)
            res = requests.get(f"{url}/{gen_id}", headers=headers).json()
            # Универсальный поиск картинок в ответе
            gen_res = res.get("generations_by_pk") or res.get("generations", [{}])[0]
            images = gen_res.get("generated_images", [])
            
            if images:
                return images[0]['url']
                
            if gen_res.get("status") == "FAILED":
                return None
    except Exception as e:
        logger.error(f"Leonardo API Error: {e}")
    return None

async def runpod_face_swap(target_url, source_b64):
    """Face Swap через RunPod с поддержкой S3"""
    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "input": {
            "source_image": source_b64,
            "target_image": target_url,
            "face_restore": True,
            "upscale": 1
        }
    }
    
    try:
        run = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        job_id = run.get("id")
        if not job_id: return None

        for _ in range(60):
            await asyncio.sleep(3)
            status_res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}", headers=headers).json()
            if status_res.get("status") == "COMPLETED":
                output = status_res.get("output")
                
                # Обработка URL от S3 (решает ошибку 400)
                if isinstance(output, dict):
                    res_url = output.get("image_url") or output.get("image")
                    if res_url and res_url.startswith("http"):
                        return requests.get(res_url).content
                    if res_url:
                        return base64.b64decode(res_url.split(",")[-1])
                return base64.b64decode(output.split(",")[-1])
    except Exception as e:
        logger.error(f"RunPod Error: {e}")
    return None

def finalize_image(image_bytes):
    """Оформление открытки"""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(25, 25, 25, 85), fill='#F5F5F0')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("font.ttf", 50)
    except:
        font = ImageFont.load_default()
    draw.text((img.size[0]/2, img.size[1]-45), "С Новым Годом!", font=font, fill="#B22222", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("❄️ Создаю вашу ретро-открытку...")
    try:
        file = await update.message.photo[-1].get_file()
        user_img = base64.b64encode(await file.download_as_bytearray()).decode('utf-8')

        # 1. Фон
        bg_url = await generate_retro_background()
        if not bg_url:
            await m.edit_text("❌ Ошибка Leonardo AI. Попробуйте еще раз через минуту.")
            return

        # 2. Лицо
        swapped = await runpod_face_swap(bg_url, user_img)
        if not swapped:
            await m.edit_text("❌ Ошибка RunPod. Проверьте баланс или настройки S3.")
            return

        # 3. Итог
        final = finalize_image(swapped)
        await update.message.reply_photo(final, caption="✨ С Новым Годом в стиле ретро!")
        await m.delete()

    except Exception as e:
        logger.error(f"General Error: {e}")
        await m.edit_text("❌ Что-то пошло не так. Попробуйте другое фото.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Пришлите фото!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
