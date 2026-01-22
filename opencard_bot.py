import os, requests, asyncio, io, logging, json
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps

# 1. Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# 2. Данные доступа (берутся из переменных окружения Railway)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")

# Промпт для создания советской открытки
TRANSFORM_PROMPT = (
    "Vintage Soviet New Year postcard style, 1970s illustration. "
    "A person in a winter coat and fur hat, snowy background with pines, "
    "magical glow, gouache painting, high quality artistic detail, nostalgic."
)

async def get_init_image_id(image_bytes):
    """Загрузка фото в Leonardo"""
    headers = {
        "Authorization": f"Bearer {LEONARDO_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    try:
        payload = {"extension": "jpg"}
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/init-image", json=payload, headers=headers)
        if r.status_code != 200: return None
            
        upload_data = r.json().get('uploadInitImage')
        if not upload_data: return None
            
        image_id = upload_data.get('id')
        upload_url = upload_data.get('url')
        fields = json.loads(upload_data.get('fields')) if isinstance(upload_data.get('fields'), str) else upload_data.get('fields')

        files = {'file': ('image.jpg', image_bytes, 'image/jpeg')}
        response = requests.post(upload_url, data=fields, files=files)
        
        if response.status_code in [200, 204]:
            logger.info(f"Photo uploaded. ID: {image_id}")
            await asyncio.sleep(10) # Ожидание индексации
            return image_id
    except Exception as e:
        logger.error(f"Upload error: {e}")
    return None

async def generate_transformed_image(init_image_id):
    """Генерация через стабильную модель SD 1.5"""
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
        "init_strength": 0.45,
        "modelId": "6bef9f1b-29cb-40c7-b9df-32b51c1f67d3", # Stable Diffusion 1.5
        "promptMagic": False # Отключаем для стабильности на Free Tier
    }
    
    for attempt in range(5):
        try:
            r = requests.post(url, json=payload, headers=headers).json()
            gen_id = (r.get('sdGenerationJob') or r.get('generation_job') or {}).get('generationId')
            
            if not gen_id:
                if "invalid" in str(r).lower():
                    await asyncio.sleep(15)
                    continue
                return None

            # Опрос статуса
            for _ in range(60):
                await asyncio.sleep(5)
                res = requests.get(f"{url}/{gen_id}", headers=headers).json()
                data = res.get("generations_by_pk") or res.get("generations", [{}])[0]
                
                if data.get("generated_images"):
                    return data["generated_images"][0]['url']
                if data.get("status") == "FAILED":
                    return None
            return None
        except Exception as e:
            logger.error(f"Gen error: {e}")
            break
    return None

def finalize_card(image_bytes):
    """Добавление рамки и текста"""
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(20, 20, 20, 100), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("font.ttf", 50)
    except:
        font = ImageFont.load_default()
    
    draw.text((img.size[0]/2, img.size[1]-50), "С Новым Годом!", font=font, fill="#8B0000", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("❄️ Рисую вашу открытку (около 1 минуты)...")
    try:
        photo = await update.message.photo[-1].get_file()
        img_bytes = await photo.download_as_bytearray()
        
        init_id = await get_init_image_id(img_bytes)
        if not init_id:
            return await m.edit_text("❌ Ошибка загрузки фото.")

        res_url = await generate_transformed_image(init_id)
        if res_url:
            card_data = requests.get(res_url).content
            final_img = finalize_card(card_data)
            await update.message.reply_photo(final_img, caption="🎄 Готово!")
            await m.delete()
        else:
            await m.edit_text("❌ Сервер Leonardo занят. Попробуйте ещё раз через минуту.")
    except Exception as e:
        logger.error(f"Full error: {e}")
        await m.edit_text("❌ Произошла ошибка.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Пришлите фото!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
