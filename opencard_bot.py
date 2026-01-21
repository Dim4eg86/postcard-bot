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

# Конфиг
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

# Промпт, который сам "вытянет" стиль даже на базовой модели
RETRO_PROMPT = (
    "A vintage 1960s Soviet New Year postcard illustration. "
    "Style of gouache painting on old paper, nostalgic winter atmosphere, "
    "artistic brushstrokes, festive USSR holiday scene, masterpiece quality."
)

async def generate_retro_background():
    """Генерация фона БЕЗ указания модели для обхода ошибки API Version"""
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    
    # Передаем только самое необходимое
    payload = {
        "prompt": RETRO_PROMPT,
        "width": 768,
        "height": 1024,
        "num_images": 1
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers).json()
        
        # Получаем ID задачи
        gen_data = r.get('sdGenerationJob') or r.get('generation_job')
        if not gen_data:
            logger.error(f"Leonardo API Error: {r}")
            return None
            
        gen_id = gen_data.get('generationId')
        
        # Опрос готовности
        for _ in range(45):
            await asyncio.sleep(4)
            res = requests.get(f"{url}/{gen_id}", headers=headers).json()
            
            # Парсим результат универсально
            g_data = res.get("generations_by_pk") or res.get("generations", [{}])[0]
            images = g_data.get("generated_images", [])
            
            if images:
                return images[0]['url']
    except Exception as e:
        logger.error(f"Leonardo Exception: {e}")
    return None

async def runpod_face_swap(target_url, source_b64):
    """Face Swap с поддержкой S3 (обход ошибки 400)"""
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
                
                # Поддержка ссылки S3 или Base64
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
    """Накладываем рамку и текст через Pillow"""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(30, 30, 30, 90), fill='#FCFAF2')
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("font.ttf", 52)
    except:
        font = ImageFont.load_default()
        
    w, h = img.size
    draw.text((w/2, h-45), "С Новым Годом!", font=font, fill="#B22222", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("❄️ Рисую вашу открытку...")
    try:
        file = await update.message.photo[-1].get_file()
        user_img_b64 = base64.b64encode(await file.download_as_bytearray()).decode('utf-8')

        # 1. Фон (теперь без ошибок модели)
        bg_url = await generate_retro_background()
        if not bg_url:
            await m.edit_text("❌ Ошибка Leonardo. Пожалуйста, попробуйте еще раз.")
            return

        # 2. Swap
        await m.edit_text("👤 Вписываю вас в ретро-стиль...")
        swapped = await runpod_face_swap(bg_url, user_img_b64)
        if not swapped:
            await m.edit_text("❌ Ошибка Face Swap (RunPod).")
            return

        # 3. Финал
        final = finalize_image(swapped)
        await update.message.reply_photo(final, caption="✨ Ваша советская открытка готова!")
        await m.delete()

    except Exception as e:
        logger.error(f"General Error: {e}")
        await m.edit_text("❌ Произошла ошибка. Попробуйте снова.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Отправьте фото для открытки!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
