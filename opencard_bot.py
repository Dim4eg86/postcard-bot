import os, requests, asyncio, io, logging, json
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")

TRANSFORM_PROMPT = (
    "A group of people wearing vintage Soviet 1970s winter coats and fur hats, "
    "standing in a snowy retro park, magical winter evening, "
    "USSR postcard style, gouache painting, soft artistic brushstrokes, "
    "nostalgic atmosphere, masterpiece illustration, highly detailed faces."
)

async def get_init_image_id(image_bytes):
    """Загрузка изображения через Presigned URL"""
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    try:
        payload = {"extension": "jpg"}
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/init-image", json=payload, headers=headers).json()
        
        upload_data = r.get('uploadInitImage')
        if not upload_data:
            logger.error(f"Init Image Error: {r}")
            return None
            
        image_id = upload_data.get('id')
        upload_url = upload_data.get('url')
        fields = json.loads(upload_data.get('fields'))

        files = {'file': image_bytes}
        response = requests.post(upload_url, data=fields, files=files)
        
        if response.status_code in [200, 204]:
            logger.info(f"Photo uploaded. ID: {image_id}")
            # ВАЖНО: Даем Leonardo 5 секунд на индексацию файла в облаке
            await asyncio.sleep(5)
            return image_id
    except Exception as e:
        logger.error(f"Upload Exception: {e}")
    return None

async def generate_transformed_image(init_image_id):
    """Генерация Image-to-Image с исправленной передачей ID"""
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "prompt": TRANSFORM_PROMPT,
        "width": 768,
        "height": 1024,
        "num_images": 1,
        "init_generation_image_id": init_image_id,
        "init_strength": 0.48, 
        "promptMagic": True
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers).json()
        gen_data = r.get('sdGenerationJob') or r.get('generation_job')
        
        if not gen_data:
            # Если всё еще пишет invalid ID, попробуем подождать еще немного и повторить один раз
            if "invalid init" in str(r).lower():
                logger.warning("Still invalid ID, waiting 5 more seconds...")
                await asyncio.sleep(5)
                r = requests.post(url, json=payload, headers=headers).json()
                gen_data = r.get('sdGenerationJob') or r.get('generation_job')

        if not gen_data:
            logger.error(f"Generation Start Error: {r}")
            return None
            
        gen_id = gen_data.get('generationId')
        
        for _ in range(60):
            await asyncio.sleep(4)
            status_res = requests.get(f"{url}/{gen_id}", headers=headers).json()
            data = status_res.get("generations_by_pk") or status_res.get("generations", [{}])[0]
            images = data.get("generated_images", [])
            
            if images:
                return images[0]['url']
            if data.get("status") == "FAILED":
                return None
    except Exception as e:
        logger.error(f"Gen Exception: {e}")
    return None

def finalize_card(image_bytes):
    """Рамка и текст"""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(25, 25, 25, 110), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("font.ttf", 55)
    except:
        font = ImageFont.load_default()
        
    w, h = img.size
    draw.text((w/2, h-55), "С Новым Годом!", font=font, fill="#8B0000", anchor="mm")
    
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("❄️ Начинаю превращение...")
    try:
        photo_file = await update.message.photo[-1].get_file()
        img_bytes = await photo_file.download_as_bytearray()
        
        init_id = await get_init_image_id(img_bytes)
        if not init_id:
            return await m.edit_text("❌ Ошибка загрузки файла.")

        await m.edit_text("🎨 Нейросеть рисует образ (около минуты)...")
        res_url = await generate_transformed_image(init_id)
        if not res_url:
            return await m.edit_text("❌ Leonardo отклонил запрос. Попробуйте другое фото.")
            
        final_bytes = finalize_card(requests.get(res_url).content)
        await update.message.reply_photo(final_bytes, caption="🎄 Готово!")
        await m.delete()

    except Exception as e:
        logger.error(f"Handler error: {e}")
        await m.edit_text("❌ Произошла ошибка.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Пришлите фото!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
