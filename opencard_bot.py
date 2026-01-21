import os, requests, asyncio, base64, io, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID")

RETRO_PROMPT = "Vintage Soviet New Year postcard, 1960s USSR gouache painting, winter, artistic masterpiece"

async def generate_retro_background():
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    payload = {"prompt": RETRO_PROMPT, "width": 768, "height": 1024, "num_images": 1}
    try:
        r = requests.post(url, json=payload, headers=headers).json()
        gen_id = (r.get('sdGenerationJob') or r.get('generation_job', {})).get('generationId')
        if not gen_id: return None
        for _ in range(30):
            await asyncio.sleep(4)
            res = requests.get(f"{url}/{gen_id}", headers=headers).json()
            g = res.get("generations_by_pk") or res.get("generations", [{}])[0]
            if g.get("generated_images"): return g["generated_images"][0]['url']
    except Exception as e: logger.error(f"Leonardo Error: {e}")
    return None

async def runpod_face_swap(target_url, source_b64):
    # КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Скачиваем фон и конвертируем в Base64 для воркера
    try:
        bg_resp = requests.get(target_url)
        target_b64 = base64.b64encode(bg_resp.content).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to download/encode background: {e}")
        return None

    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "input": {
            "source_image": source_b64,
            "target_image": target_b64, # Теперь передаем Base64, а не URL
            "face_restore": True,
            "upscale": 1
        }
    }
    
    try:
        run = requests.post(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/run", json=payload, headers=headers).json()
        job_id = run.get("id")
        if not job_id: return None

        for _ in range(80):
            await asyncio.sleep(4)
            status_res = requests.get(f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}/status/{job_id}", headers=headers).json()
            status = status_res.get("status")
            
            if status == "COMPLETED":
                output = status_res.get("output")
                img_data = None
                if isinstance(output, dict):
                    img_data = output.get("image_url") or output.get("image")
                else:
                    img_data = output
                
                if img_data and img_data.startswith("http"):
                    return requests.get(img_data).content
                if img_data:
                    return base64.b64decode(img_data.split(",")[-1])
            
            if status in ["FAILED", "CANCELLED"]:
                logger.error(f"RunPod Job Failed: {status_res}")
                return None
    except Exception as e: logger.error(f"RunPod Exception: {e}")
    return None

def finalize_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(30, 30, 30, 90), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    try: font = ImageFont.truetype("font.ttf", 50)
    except: font = ImageFont.load_default()
    draw.text((img.size[0]/2, img.size[1]-45), "С Новым Годом!", font=font, fill="#A52A2A", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("❄️ Рисую вашу открытку...")
    try:
        file = await update.message.photo[-1].get_file()
        user_img = base64.b64encode(await file.download_as_bytearray()).decode('utf-8')
        
        bg_url = await generate_retro_background()
        if not bg_url: return await m.edit_text("❌ Ошибка Leonardo.")

        await m.edit_text("👤 Вписываю лицо (RunPod)...")
        swapped = await runpod_face_swap(bg_url, user_img)
        
        if swapped:
            final = finalize_image(swapped)
            await update.message.reply_photo(final, caption="✨ Ваша ретро-открытка готова!")
            await m.delete()
        else:
            await m.edit_text("❌ Ошибка Face Swap. Проверьте логи.")
    except Exception as e:
        logger.error(f"General Error: {e}")
        await m.edit_text("❌ Ошибка приложения.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Пришлите фото!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
