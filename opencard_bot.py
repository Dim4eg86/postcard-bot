import os, requests, asyncio, io, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from PIL import Image, ImageDraw, ImageFont, ImageOps

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")

TRANSFORM_PROMPT = (
    "People wearing vintage Soviet 1970s winter coats and fur hats, "
    "snowing background, magical winter evening, USSR postcard style, gouache painting, "
    "retro aesthetic, nostalgic atmosphere, clear faces, highly detailed."
)

async def get_init_image_id(image_bytes):
    """Исправленная загрузка: получение Presigned URL и загрузка файла"""
    auth_header = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    
    try:
        # Шаг 1: Запрос Presigned URL
        payload = {"extension": "jpg"}
        r = requests.post("https://cloud.leonardo.ai/api/rest/v1/init-image", json=payload, headers=auth_header).json()
        
        data = r.get('uploadInitImage')
        if not data:
            logger.error(f"Failed to get presigned URL: {r}")
            return None
            
        upload_url = data.get('url')
        image_id = data.get('id')
        fields = eval(data.get('fields')) # Преобразуем строку полей в словарь

        # Шаг 2: Загрузка бинарных данных (S3 upload)
        files = {'file': image_bytes}
        upload_req = requests.post(upload_url, data=fields, files=files)
        
        if upload_req.status_code == 204 or upload_req.status_code == 200:
            logger.info(f"Image successfully uploaded. ID: {image_id}")
            return image_id
    except Exception as e:
        logger.error(f"Detailed upload error: {e}")
    return None

async def generate_transformed_image(init_image_id):
    url = "https://cloud.leonardo.ai/api/rest/v1/generations"
    headers = {"Authorization": f"Bearer {LEONARDO_API_KEY}", "Content-Type": "application/json"}
    
    payload = {
        "prompt": TRANSFORM_PROMPT,
        "width": 768,
        "height": 1024,
        "modelId": "6b777458-2498-421d-9db1-13c21a952680", # Phoenix
        "num_images": 1,
        "init_generation_image_id": init_image_id,
        "init_strength": 0.5, # Сохраняем баланс сходства
        "alchemy": True,
        "photoReal": False, # Нам нужен стиль рисунка, а не фото
    }
    
    try:
        r = requests.post(url, json=payload, headers=headers).json()
        gen_id = r.get('sdGenerationJob', {}).get('generationId')
        if not gen_id: return None
        
        for _ in range(50):
            await asyncio.sleep(4)
            res = requests.get(f"{url}/{gen_id}", headers=headers).json()
            images = res.get("generations_by_pk", {}).get("generated_images", [])
            if images: return images[0]['url']
    except Exception as e:
        logger.error(f"Gen error: {e}")
    return None

def finalize_card(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.expand(img, border=(25, 25, 25, 95), fill='#FDFBF5')
    draw = ImageDraw.Draw(img)
    try: font = ImageFont.truetype("font.ttf", 55)
    except: font = ImageFont.load_default()
    draw.text((img.size[0]/2, img.size[1]-50), "С Новым Годом!", font=font, fill="#A52A2A", anchor="mm")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=95)
    return out.getvalue()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = await update.message.reply_text("🧣 Начинаю переодевание в зимнее...")
    try:
        photo = await update.message.photo[-1].get_file()
        img_bytes = await photo.download_as_bytearray()
        
        # 1. Загрузка через Presigned URL
        init_id = await get_init_image_id(img_bytes)
        if not init_id: return await m.edit_text("❌ Ошибка загрузки фото.")

        # 2. Генерация Image-to-Image
        await m.edit_text("🎨 Нейросеть рисует ваш новый образ...")
        res_url = await generate_transformed_image(init_id)
        if not res_url: return await m.edit_text("❌ Ошибка генерации.")
            
        # 3. Финализация
        final_img = finalize_card(requests.get(res_url).content)
        await update.message.reply_photo(final_img, caption="🎉 Ваша ретро-открытка готова!")
        await m.delete()

    except Exception as e:
        logger.error(f"Handler error: {e}")
        await m.edit_text("❌ Произошла ошибка.")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", lambda u,c: u.message.reply_text("Пришлите фото!")))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.run_polling()

if __name__ == "__main__":
    main()
