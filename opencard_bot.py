import os
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, PreCheckoutQueryHandler
import logging
import psycopg
from psycopg.rows import dict_row
from datetime import datetime
from yookassa import Configuration, Payment
import uuid
import asyncio
import base64
import json
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
import io

# Настройка логирования
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Токены из переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
ADMIN_ID = int(os.getenv("ADMIN_ID", "610820340"))
DATABASE_URL = os.getenv("DATABASE_URL")

# Leonardo API (из env для безопасности)
LEONARDO_API_KEY = os.getenv("LEONARDO_API_KEY")
LEONARDO_API_URL = "https://cloud.leonardo.ai/api/rest/v1"

# RunPod Face Swap API (из env для безопасности)
RUNPOD_API_KEY = os.getenv("RUNPOD_API_KEY")
RUNPOD_ENDPOINT_ID = os.getenv("RUNPOD_ENDPOINT_ID", "vzsuz6ygs5m4ly")
RUNPOD_API_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}"

# Telegraph для загрузки фото
TELEGRAPH_API = "https://telegra.ph"

# Настройка YooKassa
Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY

# Пакеты открыток с ценами
PACKAGES = {
    "test": {"name": "🧪 Тестовый", "count": 1, "price": 5, "description": "Для теста (только для администратора)"},
    "1": {"name": "1 открытка", "count": 1, "price": 149, "description": "Одна красивая открытка"},
    "2": {"name": "2 открытки", "count": 2, "price": 279, "description": "Две открытки со скидкой"},
    "3": {"name": "3 открытки", "count": 3, "price": 399, "description": "Три открытки - выгодно!"},
    "5": {"name": "5 открыток", "count": 5, "price": 599, "description": "Пять открыток - еще выгоднее!"},
    "10": {"name": "10 открыток", "count": 10, "price": 899, "description": "Десять открыток - максимальная выгода!"}
}

# Тематики, стили, сцены
THEMES = {
    "new_year": "🎄 Новогодняя",
    "christmas": "✨ На Рождество", 
    "winter": "❄️ Зима",
    "old_new_year": "🎅 Старый Новый год",
    "baptism": "🕯️ Крещение",
    "congratulations": "💝 Поздравительная"
}

STYLES = {
    "ussr": "СССР",
    "modern": "Русский модерн",
    "cozy": "Гжель",
    "rus": "На Руси",
    "vintage": "Дореволюционная открытка"
}

SCENES = {
    "night_street": "🌙 Ночная улица",
    "snowy_estate": "🏠 Заснеженная усадьба",
    "church_road": "⛪ Дорога к старой церкви",
    "snowy_palace": "❄️ Снежный дворик",
    "pine_forest": "🌲 Сосновый лес",
    "winter_fair": "🎪 Зимняя ярмарка",
    "forest_path": "🪵 Тропа в лесу",
    "epic_winter": "🏔️ Эпический зимний пейзаж",
    "winter_field": "☁️ Зимний простор",
    "ice_river": "🧊 Река во льду"
}

# =============================================================================
# БАЗА ДАННЫХ
# =============================================================================

def get_db_connection():
    """Подключение к PostgreSQL"""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_database():
    """Инициализация таблиц БД"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Таблица пользователей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            first_name VARCHAR(255),
            credits INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица платежей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            package_id VARCHAR(50),
            amount INTEGER,
            status VARCHAR(50),
            payment_id VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица открыток
    cur.execute("""
        CREATE TABLE IF NOT EXISTS postcards (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            theme VARCHAR(50),
            style VARCHAR(50),
            scene VARCHAR(50),
            orientation VARCHAR(20),
            status VARCHAR(50),
            result_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Таблица обращений в поддержку
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id),
            message TEXT,
            status VARCHAR(50) DEFAULT 'open',
            admin_reply TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    cur.close()
    conn.close()

def get_or_create_user(user_id, username=None, first_name=None):
    """Получить или создать пользователя"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    user = cur.fetchone()
    
    if not user:
        cur.execute("""
            INSERT INTO users (user_id, username, first_name, credits)
            VALUES (%s, %s, %s, 0)
            RETURNING *
        """, (user_id, username, first_name))
        user = cur.fetchone()
        conn.commit()
    
    cur.close()
    conn.close()
    return user

def get_user_credits(user_id):
    """Получить баланс пользователя"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT credits FROM users WHERE user_id = %s", (user_id,))
    result = cur.fetchone()
    
    cur.close()
    conn.close()
    return result['credits'] if result else 0

def add_credits(user_id, amount):
    """Добавить кредиты пользователю"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        UPDATE users 
        SET credits = credits + %s, last_active = CURRENT_TIMESTAMP
        WHERE user_id = %s
    """, (amount, user_id))
    
    conn.commit()
    cur.close()
    conn.close()

def use_credit(user_id):
    """Использовать 1 кредит"""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        UPDATE users 
        SET credits = credits - 1, last_active = CURRENT_TIMESTAMP
        WHERE user_id = %s AND credits > 0
        RETURNING credits
    """, (user_id,))
    
    result = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    
    return result is not None

# =============================================================================
# AI ГЕНЕРАЦИЯ: LEONARDO IMAGE-TO-IMAGE
# =============================================================================

async def upload_init_image_leonardo(image_path):
    """Загрузка фото пользователя в Leonardo для Image-to-Image"""
    logger.info("[Leonardo] Uploading user photo...")
    
    try:
        # Шаг 1: Получаем presigned URL для загрузки
        response = requests.post(
            f"{LEONARDO_API_URL}/init-image",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {LEONARDO_API_KEY}",
                "content-type": "application/json"
            },
            json={"extension": "jpg"},
            timeout=30
        )
        
        if response.status_code != 200:
            logger.error(f"[Leonardo] Init image error: {response.status_code}")
            return None
        
        upload_data = response.json()["uploadInitImage"]
        init_image_id = upload_data["id"]
        upload_url = upload_data["url"]
        fields = upload_data["fields"]
        
        # Если fields это строка JSON - парсим
        if isinstance(fields, str):
            fields = json.loads(fields)
        
        logger.info(f"[Leonardo] Upload URL received, ID: {init_image_id}")
        
        # Шаг 2: Загружаем файл
        with open(image_path, 'rb') as f:
            files = {'file': ('photo.jpg', f, 'image/jpeg')}
            upload_response = requests.post(upload_url, data=fields, files=files, timeout=60)
        
        if upload_response.status_code not in [200, 204]:
            logger.error(f"[Leonardo] Upload failed: {upload_response.status_code}")
            return None
        
        logger.info("[Leonardo] Photo uploaded successfully")
        
        # Ждем пока файл обработается
        await asyncio.sleep(15)
        
        return init_image_id
        
    except Exception as e:
        logger.error(f"[Leonardo] Upload exception: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


async def generate_postcard_image_to_image(user_photo_path, theme, style, scene, orientation):
    """
    Image-to-Image генерация: превращаем фото пользователя в винтажную открытку
    с сохранением лиц и композиции
    """
    logger.info(f"[Leonardo I2I] Starting: {theme}/{style}/{scene}")
    
    try:
        # Загружаем фото пользователя
        init_image_id = await upload_init_image_leonardo(user_photo_path)
        
        if not init_image_id:
            logger.error("[Leonardo I2I] Failed to upload photo")
            return None
        
        # Формируем промпт для трансформации
        prompt = get_mvp_prompt(theme, style, scene, orientation)
        
        # Negative prompt для лучшего качества
        negative_prompt = (
            "ugly, distorted face, bad anatomy, deformed, blurry, "
            "low quality, jpeg artifacts, watermark, text, signature, "
            "modern objects, smartphones, cars, changing number of people, "
            "removing people, adding people"
        )
        
        # Запускаем Image-to-Image генерацию
        logger.info("[Leonardo I2I] Starting transformation...")
        
        response = requests.post(
            f"{LEONARDO_API_URL}/generations",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {LEONARDO_API_KEY}",
                "content-type": "application/json"
            },
            json={
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "modelId": "6b645e3a-d64f-4341-a6d8-7a3690fbf042",  # Leonardo Phoenix
                "width": 832 if orientation == "vertical" else 1216,
                "height": 1216 if orientation == "vertical" else 832,
                "num_images": 1,
                "init_image_id": init_image_id,
                "init_strength": 0.55,  # Увеличено для сохранения композиции
                "presetStyle": "ILLUSTRATION",
                "alchemy": True,
                "photoReal": False,
                "controlnets": [{
                    "initImageId": init_image_id,
                    "initImageType": "UPLOADED",
                    "preprocessorId": 67,  # Depth ControlNet
                    "strengthType": "High",
                    "weight": 0.8
                }]
            },
            timeout=60
        )
        
        if response.status_code != 200:
            logger.error(f"[Leonardo I2I] Error: {response.status_code} - {response.text}")
            return None
        
        generation_id = response.json()["sdGenerationJob"]["generationId"]
        logger.info(f"[Leonardo I2I] Generation ID: {generation_id}")
        
        # Polling результата
        for attempt in range(40):
            await asyncio.sleep(3)
            
            status_response = requests.get(
                f"{LEONARDO_API_URL}/generations/{generation_id}",
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {LEONARDO_API_KEY}"
                },
                timeout=30
            )
            
            if status_response.status_code == 200:
                status_data = status_response.json()
                generation = status_data.get("generations_by_pk")
                
                if generation:
                    status = generation.get("status")
                    
                    if status == "COMPLETE":
                        images = generation.get("generated_images", [])
                        if images:
                            image_url = images[0]["url"]
                            logger.info(f"[Leonardo I2I] Transformation complete!")
                            
                            # Скачиваем результат
                            img_response = requests.get(image_url, timeout=30)
                            result_path = f"/tmp/i2i_result_{uuid.uuid4().hex}.png"
                            
                            with open(result_path, 'wb') as f:
                                f.write(img_response.content)
                            
                            return result_path
                    
                    elif status == "FAILED":
                        logger.error(f"[Leonardo I2I] Generation failed")
                        return None
        
        logger.error("[Leonardo I2I] Timeout")
        return None
        
    except Exception as e:
        logger.error(f"[Leonardo I2I] Exception: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


# =============================================================================
# AI ГЕНЕРАЦИЯ: LEONARDO TEXT-TO-IMAGE (старый метод - оставляем как fallback)
# =============================================================================

def get_mvp_prompt(theme, style, scene, orientation):
    """Генерация промпта для Leonardo на основе выбранных параметров"""
    
    # Описания стилей
    style_descriptions = {
        "ussr": "A vintage Soviet-style postcard from 1970s-1980s USSR. Classic hand-painted illustration with warm nostalgic colors (reds, golds, blues, whites), ornate decorative border with snowflakes and stars, slightly aged vintage look.",
        "vintage": "A pre-revolutionary Russian vintage postcard from early 1900s. Sepia-toned illustration with elegant Art Nouveau elements, delicate ornamental borders, aged paper texture with subtle wear.",
        "modern": "A modern Russian artistic postcard with contemporary folk art style. Bold colors, stylized traditional patterns, clean geometric shapes, minimalist yet decorative approach.",
        "cozy": "A traditional Gzhel-style Russian postcard. Distinctive blue and white porcelain aesthetic, flowing floral patterns, folk art motifs, elegant decorative elements.",
        "rus": "An ancient Rus historical postcard depicting medieval Russia. Rich historical detail, traditional Slavic patterns, epic landscape, orthodox iconography influences, warm earth tones."
    }
    
    # Описания сцен - описание СТИЛЯ ТРАНСФОРМАЦИИ для Image-to-Image
    scene_descriptions = {
        "night_street": "Transform into vintage Soviet winter scene. Traditional Russian winter clothing, snowy street at night, warm golden light from windows, street lamps, wooden houses, bare trees with snow, peaceful evening atmosphere.",
        "snowy_estate": "Transform into elegant winter postcard. Classic winter clothing, grand Russian estate with classical architecture, snow-covered garden, ornate gates, frozen fountains, winter trees, aristocratic mood.",
        "church_road": "Transform into spiritual winter scene. Traditional Russian winter clothing, path to Orthodox church with golden onion domes, snow-covered pine trees, pilgrims, serene spiritual atmosphere.",
        "snowy_palace": "Transform into royal winter scene. Fur-trimmed winter coat, Russian palace courtyard, decorative architecture with carved wooden details, snow-covered surfaces, icicles, cozy elegant atmosphere.",
        "pine_forest": "Transform into magical winter forest. Warm winter clothing, tall snow-covered pines, natural corridors, soft diffused light filtering through branches, pristine untouched snow, enchanted mood.",
        "winter_fair": "Transform into festive fair scene. Traditional Russian folk costume, bustling winter fair, colorful decorated stalls, steaming samovars, wooden carousel, festive joyful atmosphere.",
        "forest_path": "Transform into mystical winter path. Winter coat, narrow path through winter forest, snow-laden trees, wooden fence posts, peaceful mystical mood.",
        "epic_winter": "Transform into epic winter panorama. Elegant winter outfit, vast snow-covered fields to horizon, dramatic winter sky, distant forests and villages, sense of Russia's vast beauty.",
        "winter_field": "Transform into serene winter landscape. Warm coat, open winter field under expansive sky, rolling hills with smooth snow, distant tree line, soft winter light, peaceful solitude.",
        "ice_river": "Transform into dramatic winter riverside. Traditional winter clothing, partially frozen river, ice formations, snow-covered banks, bare willow trees, mist rising from water."
    }
    
    # Описания тематик
    theme_descriptions = {
        "new_year": "New Year celebration theme with decorated Christmas tree, festive ornaments, people celebrating, joyful atmosphere, holiday magic in the air.",
        "christmas": "Christmas religious theme with church bells, nativity elements, candles glowing warmly, peaceful spiritual mood, traditional orthodox Christmas imagery.",
        "winter": "Pure winter beauty theme focusing on snow, frost patterns, winter nature, serene cold beauty without specific holiday references.",
        "old_new_year": "Old New Year celebration (January 13-14) with traditional Russian folk elements, carolers, fortune-telling imagery, mystical winter night atmosphere.",
        "baptism": "Epiphany/Baptism theme with ice-hole bathing tradition, orthodox cross, winter river or lake, spiritual transformation symbolism.",
        "congratulations": "General celebratory theme with warm wishes, festive decorations, gift-giving imagery, joyful winter celebration suitable for any occasion."
    }
    
    style_desc = style_descriptions.get(style, style_descriptions["ussr"])
    scene_desc = scene_descriptions.get(scene, scene_descriptions["winter_fair"])
    theme_desc = theme_descriptions.get(theme, theme_descriptions["new_year"])
    
    composition = "vertical portrait composition" if orientation == "vertical" else "horizontal landscape composition"
    
    prompt = f"""{style_desc}

Scene: {scene_desc}

Theme: {theme_desc}

Composition: {composition}.

CRITICAL INSTRUCTIONS:
- Keep the EXACT SAME number of people from the original photo
- Keep the EXACT SAME positions and poses
- Keep all faces recognizable and preserve facial features
- Transform only the style, clothing, and background
- DO NOT add or remove people
- DO NOT change the composition layout

Transform the photo into this vintage postcard style while strictly maintaining the number of people, their positions, and facial identities.

Style requirements: painted illustration aesthetic, vintage postcard look, warm nostalgic atmosphere, ornate decorative border, aged vintage appearance.

IMPORTANT: Hand-painted illustration style, artistic and painterly, NOT a photograph. Preserve exact number of people and all facial identities."""

    return prompt


async def generate_template_leonardo(theme, style, scene, orientation):
    """
    Генерация шаблона открытки через Leonardo Phoenix
    
    Returns:
        str: Path к сохранённому изображению или None
    """
    logger.info(f"[Leonardo] Генерация: {theme}/{style}/{scene}/{orientation}")
    
    # Формируем промпт
    prompt = get_mvp_prompt(theme, style, scene, orientation)
    
    try:
        # Запрос генерации
        response = requests.post(
            f"{LEONARDO_API_URL}/generations",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {LEONARDO_API_KEY}",
                "content-type": "application/json"
            },
            json={
                "prompt": prompt,
                "modelId": "6b645e3a-d64f-4341-a6d8-7a3690fbf042",  # Leonardo Phoenix
                "width": 832 if orientation == "vertical" else 1216,
                "height": 1216 if orientation == "vertical" else 832,
                "num_images": 1,
                "presetStyle": "ILLUSTRATION",
                "alchemy": True
            },
            timeout=60
        )
        
        if response.status_code != 200:
            logger.error(f"[Leonardo] Error: {response.status_code} - {response.text}")
            return None
        
        generation_id = response.json()["sdGenerationJob"]["generationId"]
        logger.info(f"[Leonardo] Generation ID: {generation_id}")
        
        # Polling результата (макс 60 секунд)
        for attempt in range(30):
            await asyncio.sleep(2)
            
            status_response = requests.get(
                f"{LEONARDO_API_URL}/generations/{generation_id}",
                headers={
                    "accept": "application/json",
                    "authorization": f"Bearer {LEONARDO_API_KEY}"
                },
                timeout=30
            )
            
            if status_response.status_code == 200:
                status_data = status_response.json()
                generation = status_data.get("generations_by_pk")
                
                if generation and generation.get("status") == "COMPLETE":
                    images = generation.get("generated_images", [])
                    if images:
                        image_url = images[0]["url"]
                        logger.info(f"[Leonardo] Template ready!")
                        
                        # Скачиваем изображение
                        img_response = requests.get(image_url, timeout=30)
                        template_path = f"/tmp/template_{uuid.uuid4().hex}.png"
                        
                        with open(template_path, 'wb') as f:
                            f.write(img_response.content)
                        
                        return template_path
        
        logger.error("[Leonardo] Timeout waiting for generation")
        return None
        
    except Exception as e:
        logger.error(f"[Leonardo] Exception: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


async def faceswap_runpod(template_path, user_photo_path):
    """
    Face swap через RunPod Serverless API
    
    Returns:
        str: Path к результату или None
    """
    logger.info(f"[RunPod] Face swap starting...")
    
    try:
        # Читаем изображения как base64
        with open(template_path, 'rb') as f:
            template_b64 = base64.b64encode(f.read()).decode('utf-8')
        
        with open(user_photo_path, 'rb') as f:
            user_photo_b64 = base64.b64encode(f.read()).decode('utf-8')
        
        # Формируем запрос для RunPod
        payload = {
            "input": {
                "source_image": user_photo_b64,  # Фото пользователя
                "target_image": template_b64     # Шаблон открытки
            }
        }
        
        # Отправляем запрос
        run_url = f"{RUNPOD_API_URL}/run"
        logger.info(f"[RunPod] POST {run_url}")
        
        response = requests.post(
            run_url,
            headers={
                "Authorization": f"Bearer {RUNPOD_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=120
        )
        
        logger.info(f"[RunPod] Response status: {response.status_code}")
        
        if response.status_code != 200:
            logger.error(f"[RunPod] Error: {response.status_code} - {response.text}")
            return None
        
        result = response.json()
        logger.info(f"[RunPod] Response: {result}")
        
        job_id = result.get("id")
        
        if not job_id:
            logger.error(f"[RunPod] No job ID in response")
            return None
        
        logger.info(f"[RunPod] Job started: {job_id}")
        
        # Polling результата (макс 120 секунд)
        for attempt in range(60):
            await asyncio.sleep(2)
            
            status_url = f"{RUNPOD_API_URL}/status/{job_id}"
            
            status_response = requests.get(
                status_url,
                headers={"Authorization": f"Bearer {RUNPOD_API_KEY}"},
                timeout=30
            )
            
            if status_response.status_code == 200:
                status_data = status_response.json()
                status = status_data.get("status")
                
                logger.info(f"[RunPod] Status: {status}")
                
                if status == "COMPLETED":
                    output = status_data.get("output")
                    
                    if isinstance(output, dict):
                        result_b64 = output.get("image") or output.get("image_url")
                    elif isinstance(output, str):
                        result_b64 = output
                    else:
                        logger.error(f"[RunPod] Unknown output format: {type(output)}")
                        return None
                    
                    if not result_b64:
                        logger.error(f"[RunPod] No image in output")
                        return None
                    
                    # Если это URL, скачиваем
                    if result_b64.startswith('http'):
                        img_response = requests.get(result_b64, timeout=30)
                        img_data = img_response.content
                    else:
                        # Если base64, декодируем
                        img_data = base64.b64decode(result_b64)
                    
                    # Сохраняем результат
                    result_path = f"/tmp/swapped_{uuid.uuid4().hex}.png"
                    with open(result_path, 'wb') as f:
                        f.write(img_data)
                    
                    logger.info(f"[RunPod] Face swap complete!")
                    return result_path
                    
                elif status == "FAILED":
                    error = status_data.get("error", "Unknown error")
                    logger.error(f"[RunPod] Job failed: {error}")
                    return None
        
        logger.error("[RunPod] Timeout waiting for face swap")
        return None
        
    except Exception as e:
        logger.error(f"[RunPod] Exception: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def apply_vintage_effects(img, style):
    """Применение винтажных эффектов"""
    # Конвертируем в RGB если нужно
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    # Лёгкое размытие для смягчения
    img = img.filter(ImageFilter.GaussianBlur(radius=0.5))
    
    # Уменьшение насыщенности для винтажного вида
    enhancer = ImageEnhance.Color(img)
    img = enhancer.enhance(0.85)
    
    # Лёгкое снижение контраста для винтажности
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(0.9)
    
    return img


def add_decorative_frame(img, style):
    """Добавление декоративной рамки"""
    width, height = img.size
    border_size = int(min(width, height) * 0.05)
    
    # Создаём новое изображение с рамкой
    new_size = (width + border_size * 2, height + border_size * 2)
    framed = Image.new('RGB', new_size, '#8B4513')
    
    # Вставляем оригинальное изображение
    framed.paste(img, (border_size, border_size))
    
    return framed


def add_greeting_text(img, theme):
    """Добавление поздравительного текста"""
    draw = ImageDraw.Draw(img)
    width, height = img.size
    
    # Текст по тематике
    greetings = {
        "new_year": "С Новым Годом!",
        "christmas": "С Рождеством!",
        "winter": "Счастливой зимы!",
        "old_new_year": "Со Старым Новым годом!",
        "baptism": "С Крещением!",
        "congratulations": "Поздравляю!"
    }
    
    text = greetings.get(theme, "Поздравляю!")
    
    # Простой текст внизу
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf", int(height * 0.04))
    except:
        font = ImageFont.load_default()
    
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    x = (width - text_width) // 2
    y = height - text_height - int(height * 0.05)
    
    # Тень
    draw.text((x+2, y+2), text, font=font, fill='black')
    # Основной текст
    draw.text((x, y), text, font=font, fill='white')
    
    return img


async def upload_to_telegraph(image_path):
    """Загрузка изображения на Telegraph"""
    try:
        # Telegraph не поддерживает PNG с альфа-каналом
        # Конвертируем в JPEG
        img = Image.open(image_path)
        
        # Конвертируем в RGB
        if img.mode != 'RGB':
            # Создаём белый фон
            if img.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert('RGB')
        
        # Сохраняем как JPEG
        jpeg_path = image_path.replace('.png', '.jpg')
        img.save(jpeg_path, 'JPEG', quality=95, optimize=True)
        
        logger.info(f"[Telegraph] Converted to JPEG: {jpeg_path}")
        
        # Загружаем на Telegraph
        with open(jpeg_path, 'rb') as f:
            files = {'file': ('image.jpg', f, 'image/jpeg')}
            response = requests.post(f"{TELEGRAPH_API}/upload", files=files, timeout=30)
        
        logger.info(f"[Telegraph] Upload response: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"[Telegraph] Response data: {result}")
            
            if result and len(result) > 0:
                image_url = f"{TELEGRAPH_API}{result[0]['src']}"
                logger.info(f"[Telegraph] Upload success: {image_url}")
                
                # Удаляем временный JPEG
                try:
                    os.remove(jpeg_path)
                except:
                    pass
                
                return image_url
        
        logger.error(f"[Telegraph] Upload failed: {response.status_code} - {response.text}")
        return None
        
    except Exception as e:
        logger.error(f"[Telegraph] Exception: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


async def generate_postcard(photo_path, theme, style, scene, orientation="vertical"):
    """
    Полный пайплайн генерации открытки через Image-to-Image
    
    1. Leonardo Image-to-Image → трансформация фото в винтажную открытку
    2. Постобработка (рамка, эффекты, текст)
    3. Конвертация в JPEG
    """
    logger.info(f"[PIPELINE] Starting: {theme}/{style}/{scene}")
    
    try:
        # ШАГ 1: Image-to-Image трансформация через Leonardo
        logger.info("[STEP 1/3] Leonardo Image-to-Image transformation...")
        transformed_path = await generate_postcard_image_to_image(
            user_photo_path=photo_path,
            theme=theme,
            style=style,
            scene=scene,
            orientation=orientation
        )
        
        if not transformed_path:
            logger.error("Leonardo I2I transformation failed")
            return None
        
        logger.info(f"[STEP 1/3] ✓ Transformation complete")
        
        # ШАГ 2: Постобработка
        logger.info("[STEP 2/3] Post-processing...")
        
        img = Image.open(transformed_path)
        
        # Применяем винтажные эффекты
        img = apply_vintage_effects(img, style)
        
        # Добавляем декоративную рамку
        img = add_decorative_frame(img, style)
        
        # Добавляем поздравительный текст
        img = add_greeting_text(img, theme)
        
        logger.info(f"[STEP 2/3] ✓ Post-processing complete")
        
        # ШАГ 3: Конвертируем в JPEG для отправки
        logger.info("[STEP 3/3] Converting to JPEG...")
        
        # Конвертируем в RGB и сохраняем как JPEG
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        jpeg_path = f"/tmp/result_{uuid.uuid4().hex}.jpg"
        img.save(jpeg_path, "JPEG", quality=95, optimize=True)
        
        logger.info(f"[STEP 3/3] ✓ Conversion complete")
        logger.info(f"[PIPELINE] ✓ SUCCESS! File: {jpeg_path}")
        
        # Очистка временных файлов (кроме финального результата)
        try:
            os.remove(transformed_path)
        except:
            pass
        
        return jpeg_path
        
    except Exception as e:
        logger.error(f"[PIPELINE] Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

# =============================================================================
# TELEGRAM BOT HANDLERS
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    get_or_create_user(user.id, user.username, user.first_name)
    
    credits = get_user_credits(user.id)
    
    keyboard = [
        [InlineKeyboardButton("🎨 Создать открытку", callback_data="create_postcard")],
        [InlineKeyboardButton("💳 Купить открытки", callback_data="show_pricing")],
        [InlineKeyboardButton("💰 Мой баланс", callback_data="check_balance")],
        [InlineKeyboardButton("💬 Поддержка", callback_data="support")]
    ]
    
    text = (
        f"Привет, {user.first_name}! ✨\n\n"
        "Создавай душевные открытки в стиле СССР с твоими фото! 🎄\n\n"
        f"💰 Твой баланс: {credits} открыток\n\n"
        "Загрузи фото — и через минуту получи художественную открытку, "
        "которую приятно подарить и хочется сохранить 🎨"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def check_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка баланса"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    credits = get_user_credits(user_id)
    
    keyboard = [
        [InlineKeyboardButton("💳 Купить открытки", callback_data="show_pricing")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_start")]
    ]
    
    text = f"💰 Твой баланс: {credits} открыток\n\n"
    
    if credits == 0:
        text += "У тебя закончились открытки 😢\nКупи новые, чтобы продолжить создавать!"
    else:
        text += "Можешь создавать открытки! 🎨"
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ тарифов"""
    query = update.callback_query
    await query.answer()
    
    keyboard = []
    
    # Добавляем пакеты (кроме тестового для обычных пользователей)
    for package_id, package in PACKAGES.items():
        if package_id == "test" and update.effective_user.id != ADMIN_ID:
            continue
        
        price_per_card = package['price'] / package['count']
        button_text = f"{package['name']} - {package['price']}₽"
        if package['count'] > 1:
            button_text += f" ({price_per_card:.0f}₽/шт)"
        
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"buy_{package_id}")])
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_start")])
    
    text = (
        "💳 Выбери пакет открыток:\n\n"
        "Чем больше пакет, тем выгоднее цена! 🎁"
    )
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def buy_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Покупка пакета"""
    query = update.callback_query
    await query.answer()
    
    package_id = query.data.replace("buy_", "")
    package = PACKAGES.get(package_id)
    
    if not package:
        await query.edit_message_text("❌ Пакет не найден")
        return
    
    user_id = update.effective_user.id
    
    # Создаём платёж в YooKassa
    try:
        payment = Payment.create({
            "amount": {
                "value": str(package['price']),
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/postcard_aibot"
            },
            "capture": True,
            "description": f"{package['name']} для @{update.effective_user.username or update.effective_user.id}"
        })
        
        # Сохраняем платёж в БД
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO payments (user_id, package_id, amount, status, payment_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, package_id, package['price'], 'pending', payment.id))
        conn.commit()
        
        # Сохраняем payment_id для проверки
        context.user_data['pending_payment_id'] = payment.id
        context.user_data['pending_package_id'] = package_id
        
        cur.close()
        conn.close()
        
        # Кнопки оплаты и проверки
        keyboard = [
            [InlineKeyboardButton("💳 Оплатить", url=payment.confirmation.confirmation_url)],
            [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"check_payment_{payment.id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="show_pricing")]
        ]
        
        text = (
            f"💳 Оплата: {package['name']}\n\n"
            f"💰 Сумма: {package['price']}₽\n"
            f"🎁 Получишь: {package['count']} открыток\n\n"
            "1️⃣ Нажми «Оплатить»\n"
            "2️⃣ После оплаты нажми «Проверить оплату»\n\n"
            "Открытки начислятся автоматически! ✨"
        )
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        
    except Exception as e:
        logger.error(f"Ошибка создания платежа: {e}")
        await query.edit_message_text(
            "❌ Ошибка создания платежа. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="show_pricing")]])
        )


async def check_single_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка одного платежа по кнопке"""
    query = update.callback_query
    await query.answer("Проверяю платеж...")
    
    payment_id = query.data.replace("check_payment_", "")
    user_id = update.effective_user.id
    
    try:
        # Получаем статус платежа из YooKassa
        payment = Payment.find_one(payment_id)
        
        if payment.status == 'succeeded':
            # Получаем данные платежа из БД
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM payments 
                WHERE payment_id = %s AND user_id = %s AND status = 'pending'
            """, (payment_id, user_id))
            payment_row = cur.fetchone()
            
            if payment_row:
                package = PACKAGES.get(payment_row['package_id'])
                
                # Начисляем кредиты
                add_credits(user_id, package['count'])
                
                # Обновляем статус платежа
                cur.execute("""
                    UPDATE payments 
                    SET status = 'succeeded' 
                    WHERE payment_id = %s
                """, (payment_id,))
                conn.commit()
                
                cur.close()
                conn.close()
                
                # Успешное сообщение
                keyboard = [[InlineKeyboardButton("🎨 Создать открытку", callback_data="create_postcard")]]
                
                await query.edit_message_text(
                    f"✅ Оплата успешна!\n\n"
                    f"🎁 Начислено: {package['count']} открыток\n"
                    f"💰 Твой баланс: {get_user_credits(user_id)} открыток\n\n"
                    "Теперь можешь создавать открытки! 🎨",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                
                # Очищаем данные
                context.user_data.pop('pending_payment_id', None)
                context.user_data.pop('pending_package_id', None)
            else:
                cur.close()
                conn.close()
                await query.edit_message_text("✅ Оплата уже обработана!")
                
        elif payment.status == 'pending':
            # Платеж еще обрабатывается
            keyboard = [
                [InlineKeyboardButton("🔄 Проверить снова", callback_data=f"check_payment_{payment_id}")],
                [InlineKeyboardButton("◀️ В меню", callback_data="back_to_start")]
            ]
            await query.edit_message_text(
                "⏳ Платеж обрабатывается...\n\n"
                "Попробуй проверить через несколько секунд",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        elif payment.status == 'canceled':
            await query.edit_message_text(
                "❌ Платеж отменён\n\n"
                "Попробуй оплатить снова через /start → 💳 Купить открытки"
            )
        else:
            await query.edit_message_text(
                f"⚠️ Статус платежа: {payment.status}\n\n"
                "Обратись в поддержку через /start → 💬 Поддержка"
            )
            
    except Exception as e:
        logger.error(f"Ошибка проверки платежа: {e}")
        await query.edit_message_text(
            "❌ Ошибка проверки платежа\n\n"
            "Попробуй позже или обратись в поддержку"
        )


async def create_postcard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало создания открытки"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    credits = get_user_credits(user_id)
    
    if credits <= 0:
        keyboard = [[InlineKeyboardButton("💳 Купить открытки", callback_data="show_pricing")]]
        await query.edit_message_text(
            "❌ У тебя нет открыток!\n\n"
            "Купи пакет, чтобы создавать открытки 🎨",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    # Выбор ориентации
    keyboard = [
        [InlineKeyboardButton("📱 Вертикальная", callback_data="orientation_vertical")],
        [InlineKeyboardButton("🖼️ Горизонтальная", callback_data="orientation_horizontal")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back_to_start")]
    ]
    
    text = (
        "🎨 Создание открытки\n\n"
        "Шаг 1/4: Выбери ориентацию:"
    )
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_orientation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора ориентации"""
    query = update.callback_query
    await query.answer()
    
    orientation = query.data.replace("orientation_", "")
    context.user_data['orientation'] = orientation
    
    # Выбор тематики
    keyboard = []
    for theme_id, theme_name in THEMES.items():
        keyboard.append([InlineKeyboardButton(theme_name, callback_data=f"theme_{theme_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="create_postcard")])
    
    text = (
        f"🎨 Создание открытки\n"
        f"Ориентация: {'📱 Вертикальная' if orientation == 'vertical' else '🖼️ Горизонтальная'}\n\n"
        "Шаг 2/4: Выбери тематику:"
    )
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_theme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора тематики"""
    query = update.callback_query
    await query.answer()
    
    theme = query.data.replace("theme_", "")
    context.user_data['theme'] = theme
    
    # Выбор стиля
    keyboard = []
    for style_id, style_name in STYLES.items():
        keyboard.append([InlineKeyboardButton(style_name, callback_data=f"style_{style_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"orientation_{context.user_data['orientation']}")])
    
    text = (
        f"🎨 Создание открытки\n"
        f"Тематика: {THEMES[theme]}\n\n"
        "Шаг 3/4: Выбери стиль:"
    )
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора стиля"""
    query = update.callback_query
    await query.answer()
    
    style = query.data.replace("style_", "")
    context.user_data['style'] = style
    
    # Выбор сцены
    keyboard = []
    for scene_id, scene_name in SCENES.items():
        keyboard.append([InlineKeyboardButton(scene_name, callback_data=f"scene_{scene_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"theme_{context.user_data['theme']}")])
    
    text = (
        f"🎨 Создание открытки\n"
        f"Стиль: {STYLES[style]}\n\n"
        "Шаг 4/4: Выбери сцену:"
    )
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_scene(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора сцены"""
    query = update.callback_query
    await query.answer()
    
    scene = query.data.replace("scene_", "")
    context.user_data['scene'] = scene
    
    text = (
        f"📸 Отлично!\n\n"
        f"Ориентация: {'📱 Вертикальная' if context.user_data['orientation'] == 'vertical' else '🖼️ Горизонтальная'}\n"
        f"Тематика: {THEMES[context.user_data['theme']]}\n"
        f"Стиль: {STYLES[context.user_data['style']]}\n"
        f"Сцена: {SCENES[scene]}\n\n"
        "Теперь отправь своё фото, и я создам открытку! 🎨\n\n"
        "💡 Лучше всего подходят портретные фото с хорошим освещением"
    )
    
    await query.edit_message_text(text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загруженного фото"""
    user_id = update.effective_user.id
    
    # Проверяем что все параметры выбраны
    if not all(key in context.user_data for key in ['orientation', 'theme', 'style', 'scene']):
        await update.message.reply_text(
            "Сначала выбери параметры открытки через /start → 🎨 Создать открытку"
        )
        return
    
    # Проверяем кредиты
    if not use_credit(user_id):
        await update.message.reply_text(
            "❌ У тебя нет открыток!\n\n"
            "Купи пакет через /start → 💳 Купить открытки"
        )
        return
    
    # Сообщение о начале
    status_msg = await update.message.reply_text("⏳ Создаю твою открытку...\n\nЭто займёт 1-2 минуты")
    
    try:
        # Скачиваем фото
        photo_file = await update.message.photo[-1].get_file()
        photo_path = f"/tmp/user_photo_{uuid.uuid4().hex}.jpg"
        await photo_file.download_to_drive(photo_path)
        
        # Генерируем открытку
        result_path = await generate_postcard(
            photo_path=photo_path,
            theme=context.user_data['theme'],
            style=context.user_data['style'],
            scene=context.user_data['scene'],
            orientation=context.user_data['orientation']
        )
        
        # Удаляем временное фото
        try:
            os.remove(photo_path)
        except:
            pass
        
        if result_path:
            # Сохраняем в БД (без URL, так как файл локальный)
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO postcards (user_id, theme, style, scene, orientation, status, result_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id,
                context.user_data['theme'],
                context.user_data['style'],
                context.user_data['scene'],
                context.user_data['orientation'],
                'completed',
                'local_file'  # Помечаем как локальный файл
            ))
            conn.commit()
            cur.close()
            conn.close()
            
            # Удаляем статусное сообщение
            await status_msg.delete()
            
            # Отправляем результат как файл
            credits = get_user_credits(user_id)
            
            keyboard = [
                [InlineKeyboardButton("🎨 Создать ещё", callback_data="create_postcard")],
                [InlineKeyboardButton("◀️ В меню", callback_data="back_to_start")]
            ]
            
            with open(result_path, 'rb') as photo_file:
                await update.message.reply_photo(
                    photo=photo_file,
                    caption=f"✨ Твоя открытка готова!\n\n💰 Осталось открыток: {credits}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            
            # Удаляем файл после отправки
            try:
                os.remove(result_path)
            except:
                pass
            
            # Очищаем данные пользователя
            context.user_data.clear()
        else:
            # Возвращаем кредит если генерация не удалась
            add_credits(user_id, 1)
            credits = get_user_credits(user_id)
            
            await status_msg.edit_text(
                "❌ Не удалось создать открытку\n\n"
                f"💰 Открытка возвращена на баланс: {credits} открыток\n\n"
                "Попробуй ещё раз или обратись в поддержку"
            )
            
    except Exception as e:
        logger.error(f"Ошибка создания открытки: {e}")
        
        # Возвращаем кредит при ошибке
        add_credits(user_id, 1)
        credits = get_user_credits(user_id)
        
        await status_msg.edit_text(
            "❌ Произошла ошибка при создании открытки\n\n"
            f"💰 Открытка возвращена на баланс: {credits} открыток\n\n"
            "Попробуй ещё раз или обратись в поддержку"
        )


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поддержка"""
    query = update.callback_query
    await query.answer()
    
    text = (
        "💬 Поддержка\n\n"
        "Напиши своё сообщение, и я передам его администратору.\n"
        "Он ответит тебе как можно скорее!"
    )
    
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_start")]]
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    context.user_data['support_mode'] = True


async def handle_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщения в поддержку"""
    if not context.user_data.get('support_mode'):
        return
    
    user = update.effective_user
    user_id = user.id
    message_text = update.message.text
    
    # Сохраняем тикет
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO support_tickets (user_id, message)
        VALUES (%s, %s)
        RETURNING id
    """, (user_id, message_text))
    ticket_id = cur.fetchone()['id']
    conn.commit()
    cur.close()
    conn.close()
    
    await update.message.reply_text(
        f"✅ Твоё сообщение отправлено!\n\n"
        f"Номер обращения: #{ticket_id}\n"
        "Администратор ответит тебе в ближайшее время."
    )
    
    # Уведомляем админа
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"💬 Новое обращение #{ticket_id}\n\n"
             f"От: {user.first_name} (@{user.username})\n"
             f"ID: {user_id}\n\n"
             f"Сообщение:\n{message_text}\n\n"
             f"Ответить: /reply_{ticket_id} текст"
    )
    
    context.user_data.pop('support_mode', None)


# =============================================================================
# ADMIN COMMANDS
# =============================================================================

async def admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ответ админа на тикет"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    parts = update.message.text.split(' ', 1)
    if len(parts) < 2:
        await update.message.reply_text("Использование: /reply_ID текст ответа")
        return
    
    ticket_id = parts[0].replace('/reply_', '')
    reply_text = parts[1]
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM support_tickets WHERE id = %s", (ticket_id,))
    ticket = cur.fetchone()
    
    if not ticket:
        await update.message.reply_text("Тикет не найден")
        cur.close()
        conn.close()
        return
    
    cur.execute("""
        UPDATE support_tickets 
        SET admin_reply = %s, status = 'closed'
        WHERE id = %s
    """, (reply_text, ticket_id))
    conn.commit()
    cur.close()
    conn.close()
    
    await context.bot.send_message(
        chat_id=ticket['user_id'],
        text=f"💬 Ответ на обращение #{ticket_id}:\n\n{reply_text}"
    )
    
    await update.message.reply_text(f"✅ Ответ отправлен")


async def check_payment_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручная проверка платежей"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT * FROM payments 
        WHERE status = 'pending' 
        ORDER BY created_at DESC 
        LIMIT 5
    """)
    payments = cur.fetchall()
    
    if not payments:
        await update.message.reply_text("✅ Нет pending платежей")
        cur.close()
        conn.close()
        return
    
    processed = 0
    for payment_row in payments:
        try:
            payment = Payment.find_one(payment_row['payment_id'])
            
            if payment.status == 'succeeded':
                package = PACKAGES.get(payment_row['package_id'])
                add_credits(payment_row['user_id'], package['count'])
                
                cur.execute("""
                    UPDATE payments 
                    SET status = 'succeeded' 
                    WHERE payment_id = %s
                """, (payment_row['payment_id'],))
                conn.commit()
                
                await context.bot.send_message(
                    chat_id=payment_row['user_id'],
                    text=f"✅ Оплата успешна!\n\n"
                         f"🎁 Начислено: {package['count']} открыток\n"
                         f"💰 Баланс: {get_user_credits(payment_row['user_id'])} открыток"
                )
                
                processed += 1
        except Exception as e:
            logger.error(f"Ошибка проверки платежа: {e}")
    
    await update.message.reply_text(f"✅ Обработано: {processed}")
    
    cur.close()
    conn.close()


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) as count FROM users")
    users_count = cur.fetchone()['count']
    
    cur.execute("SELECT COUNT(*) as count FROM payments WHERE status = 'succeeded'")
    payments_count = cur.fetchone()['count']
    
    cur.execute("SELECT SUM(amount) as total FROM payments WHERE status = 'succeeded'")
    total_revenue = cur.fetchone()['total'] or 0
    
    cur.execute("SELECT COUNT(*) as count FROM postcards WHERE status = 'completed'")
    postcards_count = cur.fetchone()['count']
    
    cur.close()
    conn.close()
    
    await update.message.reply_text(
        f"📊 Статистика:\n\n"
        f"👥 Пользователей: {users_count}\n"
        f"💰 Платежей: {payments_count}\n"
        f"💵 Выручка: {total_revenue}₽\n"
        f"🎨 Открыток: {postcards_count}"
    )


async def admin_add_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начислить кредиты"""
    if update.effective_user.id != ADMIN_ID:
        return
    
    try:
        parts = update.message.text.split()
        user_id = int(parts[1])
        amount = int(parts[2])
        
        add_credits(user_id, amount)
        
        await update.message.reply_text(
            f"✅ Начислено {amount} открыток\n"
            f"User: {user_id}\n"
            f"Баланс: {get_user_credits(user_id)}"
        )
        
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🎁 Тебе начислено {amount} открыток!\n\n"
                 f"💰 Баланс: {get_user_credits(user_id)}"
        )
        
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /add_credits USER_ID AMOUNT")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def background_payment_checker(app):
    """Фоновая проверка платежей каждые 5 минут"""
    while True:
        await asyncio.sleep(300)  # 5 минут
        
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            cur.execute("""
                SELECT * FROM payments 
                WHERE status = 'pending' 
                AND created_at > NOW() - INTERVAL '24 hours'
            """)
            payments = cur.fetchall()
            
            for payment_row in payments:
                try:
                    payment = Payment.find_one(payment_row['payment_id'])
                    
                    if payment.status == 'succeeded':
                        package = PACKAGES.get(payment_row['package_id'])
                        add_credits(payment_row['user_id'], package['count'])
                        
                        cur.execute("""
                            UPDATE payments 
                            SET status = 'succeeded' 
                            WHERE payment_id = %s
                        """, (payment_row['payment_id'],))
                        conn.commit()
                        
                        await app.bot.send_message(
                            chat_id=payment_row['user_id'],
                            text=f"✅ Оплата успешна!\n\n"
                                 f"🎁 Начислено: {package['count']} открыток\n"
                                 f"💰 Баланс: {get_user_credits(payment_row['user_id'])} открыток"
                        )
                        
                except Exception as e:
                    logger.error(f"Ошибка проверки платежа: {e}")
            
            cur.close()
            conn.close()
            
        except Exception as e:
            logger.error(f"Ошибка фоновой проверки: {e}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Запуск бота"""
    init_database()
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(start, pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(check_balance, pattern="^check_balance$"))
    app.add_handler(CallbackQueryHandler(show_pricing, pattern="^show_pricing$"))
    app.add_handler(CallbackQueryHandler(buy_package, pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(check_single_payment, pattern="^check_payment_"))
    app.add_handler(CallbackQueryHandler(create_postcard, pattern="^create_postcard$"))
    app.add_handler(CallbackQueryHandler(handle_orientation, pattern="^orientation_"))
    app.add_handler(CallbackQueryHandler(handle_theme, pattern="^theme_"))
    app.add_handler(CallbackQueryHandler(handle_style, pattern="^style_"))
    app.add_handler(CallbackQueryHandler(handle_scene, pattern="^scene_"))
    app.add_handler(CallbackQueryHandler(support, pattern="^support$"))
    
    # Сообщения
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_support_message))
    
    # Админ
    app.add_handler(CommandHandler("reply", admin_reply))
    app.add_handler(CommandHandler("check_payments", check_payment_manual))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("add_credits", admin_add_credits))
    app.add_handler(CommandHandler("addbalance", admin_add_credits))  # Alias
    
    logger.info("🚀 Бот запущен с Leonardo + RunPod Face Swap!")
    
    # Фоновая проверка платежей
    loop = asyncio.get_event_loop()
    loop.create_task(background_payment_checker(app))
    
    app.run_polling()


if __name__ == "__main__":
    main()
