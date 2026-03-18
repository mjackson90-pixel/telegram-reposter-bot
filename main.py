import asyncio
import logging
import os
import hashlib
import re
from collections import deque
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChannel
from dotenv import load_dotenv
import aiohttp
import json
import tempfile

load_dotenv()

# --- Основные настройки ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = "main_user"
SOURCES = os.getenv("RAW_SOURCES", "").split(",")
TARGET_FAVORITES = os.getenv("TARGET_FAVORITES", "me")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")

# --- Настройки фильтров ---
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", 0.7))
SIGHTENGINE_USER = os.getenv("SIGHTENGINE_USER", "")
SIGHTENGINE_SECRET = os.getenv("SIGHTENGINE_SECRET", "")

# --- Настройки AI-рерайта ---
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
REWRITE_STYLE = os.getenv("REWRITE_STYLE", "chekhov")
REWRITE_MIN_LENGTH = int(os.getenv("REWRITE_MIN_LENGTH", "20"))
REWRITE_MAX_LENGTH_RATIO = 1.2  # Максимальное увеличение длины (120% от оригинала)

# --- Списки каналов с разными правилами ---
CHANNELS_FAVORITES_ONLY = os.getenv("CHANNELS_FAVORITES_ONLY", "").split(",")
CHANNELS_FULL_CHECK = os.getenv("CHANNELS_FULL_CHECK", "").split(",")
CHANNELS_CHECK_NO_NSFW = os.getenv("CHANNELS_CHECK_NO_NSFW", "").split(",")
CHANNELS_REWRITE = os.getenv("CHANNELS_REWRITE", "").split(",")

# Очищаем списки от пустых строк
CHANNELS_FAVORITES_ONLY = [c.strip() for c in CHANNELS_FAVORITES_ONLY if c.strip()]
CHANNELS_FULL_CHECK = [c.strip() for c in CHANNELS_FULL_CHECK if c.strip()]
CHANNELS_CHECK_NO_NSFW = [c.strip() for c in CHANNELS_CHECK_NO_NSFW if c.strip()]
CHANNELS_REWRITE = [c.strip() for c in CHANNELS_REWRITE if c.strip()]

# --- Стоп-слова для рекламы ---
AD_KEYWORDS = [
    "реклама", "промо", "скидка", "акция", "купить", "оформить",
    "подпишись", "переходи", "ссылка", "бот", "оферта", "спонсор",
    "реклам", "промокод", "discount", "sale", "buy", "order"
]
ALLOWED_WORDS = ["спасибо", "пожалуйста", "спс", "благодарю"]

# --- Важные паттерны для сохранения ---
IMPORTANT_PATTERNS = [
    # Города России и мира
    r'Москв[аы]|Питер|СПб|Ленинград|Новосибирск|Екатеринбург|Нижний|Казань|Волгоград|Владимир|Сочи|Краснодар|Ростов|Саратов|Воронеж|Пермь',
    # Марки машин
    r'Жигули|Нива|Лада|Мерседес|БМВ|BMW|Ауди|Audi|Тойота|Toyota|Хонда|Honda|Форд|Ford|Шкода|Skoda|Киа|Kia|Хендай|Hyundai',
    # Сленг и ключевые слова
    r'пацан[аы]|мужик[аи]|чувак[аи]|телка|баба|девк[аи]|брат[аы]|краш|кринж|рофл|хайп|флекс|хейт|вайб',
]

# Компилируем паттерны для скорости
COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in IMPORTANT_PATTERNS]

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

async def get_entity_smart(client, entity_input):
    """Умное получение сущности, работающее с разными форматами"""
    entity_input = entity_input.strip()
    
    if entity_input.startswith(('t.me/', 'https://t.me/')):
        return await client.get_entity(entity_input)
    
    if entity_input.startswith('@'):
        return await client.get_entity(entity_input)
    
    if entity_input.lstrip('-').isdigit():
        try:
            id_int = int(entity_input)
            if id_int < 0:
                return await client.get_entity(id_int)
            else:
                return await client.get_entity(PeerChannel(id_int))
        except Exception as e:
            log.error(f"Ошибка при обработке ID {entity_input}: {e}")
            raise
    
    return await client.get_entity(entity_input)

def extract_important_terms(text):
    """Извлекает важные термины из текста для последующего восстановления"""
    if not text:
        return []
    
    terms = []
    for pattern in COMPILED_PATTERNS:
        matches = re.findall(pattern, text)
        terms.extend(matches)
    
    # Убираем дубликаты, но сохраняем регистр оригинала
    unique_terms = []
    seen = set()
    for term in terms:
        term_lower = term.lower()
        if term_lower not in seen:
            seen.add(term_lower)
            unique_terms.append(term)
    
    return unique_terms

def clean_text(text):
    """Удаляет ссылки и призывы подписаться из текста, сохраняя важные термины"""
    if not text:
        return text
    
    original = text
    
    # Сохраняем важные термины ДО очистки
    important_terms = extract_important_terms(text)
    if important_terms:
        log.info(f"   🔍 Найдены важные термины: {', '.join(important_terms[:5])}")
    
    # 1. Удаляем markdown-ссылки с эмодзи и без
    md_link_patterns = [
        r'\[([^\]]+)\]\([^\)]+\)',  # [текст](ссылка)
        r'[🙈🙉🙊👀👉]\s*\[([^\]]+)\]\([^\)]+\)',  # эмодзи [текст](ссылка)
        r'\[([^\]]+)\]\([^\)]+\)\s*[🙈🙉🙊👀👉]',  # [текст](ссылка) эмодзи
    ]
    
    for pattern in md_link_patterns:
        # Заменяем ссылку на её текст (без эмодзи)
        text = re.sub(pattern, r'\1', text)
    
    # 2. Удаляем все URL-ссылки
    url_patterns = [
        r'https?://[^\s]+',
        r't\.me/[^\s]+',
        r't\.me/\+[^\s]+',
        r'@\w+',
    ]
    
    for pattern in url_patterns:
        text = re.sub(pattern, '', text)
    
    # 3. Удаляем призывы подписаться
    subscribe_phrases = [
        r'подпишись\s*на\s*канал',
        r'подписывайся\s*на',
        r'заходи\s*на\s*канал',
        r'вступай\s*в\s*группу',
        r'присоединяйся\s*к',
        r'переходи\s*по\s*ссылке',
        r'ссылка\s*на\s*канал',
        r'больше\s*контента\s*на',
        r'наш\s*канал',
        r'мой\s*канал',
        r'подписаться\s*на',
        r'тык\s*сюда',
        r'жми\s*сюда',
    ]
    
    for phrase in subscribe_phrases:
        text = re.sub(phrase, '', text, flags=re.IGNORECASE)
    
    # 4. Очищаем лишние пробелы, но сохраняем переносы строк
    lines = text.split('\n')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in lines]
    text = '\n'.join([line for line in lines if line])
    
    # Если текст стал пустым после очистки, но был контент
    if not text and original:
        # Проверяем, были ли в оригинале важные термины
        if important_terms:
            return f"[Контент с {', '.join(important_terms[:3])}]"
        return "[Медиафайл]"
    
    return text

def post_edit_rewrite(original, rewritten):
    """Проверяет сохранность ключевых элементов и восстанавливает потерянные"""
    if not original or not rewritten:
        return rewritten
    
    # Извлекаем важные термины из оригинала
    original_terms = extract_important_terms(original)
    if not original_terms:
        return rewritten
    
    # Проверяем, сохранились ли они в переписанном тексте
    missing_terms = []
    for term in original_terms:
        if term.lower() not in rewritten.lower():
            missing_terms.append(term)
    
    if missing_terms:
        log.warning(f"   ⚠️ Потеряны важные термины: {', '.join(missing_terms)}")
        
        # Пытаемся восстановить первый потерянный термин в начале текста
        if missing_terms:
            # Добавляем потерянный термин в начало с восклицанием
            rewritten = f"{missing_terms[0]}! {rewritten}"
            log.info(f"      ✅ Восстановлен термин: {missing_terms[0]}")
    
    return rewritten

async def rewrite_text_ai(text, style="chekhov", original_length=None):
    """Переписывает текст в стиле классика с сохранением структуры и имён"""
    if not text or len(text) < REWRITE_MIN_LENGTH:
        return text
    
    original_length = original_length or len(text)
    
    # Сохраняем оригинальную структуру
    paragraphs = text.split('\n\n')
    log.info(f"   📐 Оригинальная структура: {len(paragraphs)} абзацев")
    
    # Извлекаем важные термины для проверки
    important_terms = extract_important_terms(text)
    if important_terms:
        log.info(f"   🔍 Важные термины: {', '.join(important_terms[:5])}")
    
    style_prompts = {
        "chekhov": """Перепиши этот текст в стиле Антона Чехова, СОХРАНЯЯ:

1) ВСЕ ИМЕНА СОБСТВЕННЫЕ (названия городов, имена людей, бренды, марки машин) - ОБЯЗАТЕЛЬНО
2) КЛЮЧЕВЫЕ РЕАЛИИ (специфические термины, сленг) - ОБЯЗАТЕЛЬНО
3) СТРУКТУРУ АБЗАЦЕВ (\\n\\n между абзацами)

Стиль:
- Кратко (на 20-30% короче оригинала)
- С тонкой иронией
- Живым разговорным языком
- Без канцеляризмов

Оригинал (сохрани ВСЕ города, марки машин, имена и ключевые слова):
{text}

Ответ должен быть ТОЛЬКО переписанным текстом с ТОЧНО ТАКИМИ ЖЕ именами собственными.""",
        
        "gogol": """Перепиши этот текст в стиле Николая Гоголя, СОХРАНЯЯ:

1) ВСЕ ИМЕНА СОБСТВЕННЫЕ (названия городов, имена людей, бренды, марки машин) - ОБЯЗАТЕЛЬНО
2) КЛЮЧЕВЫЕ РЕАЛИИ (специфические термины, сленг) - ОБЯЗАТЕЛЬНО
3) СТРУКТУРУ АБЗАЦЕВ (\\n\\n между абзацами)

Стиль:
- С юмором и гротеском
- Неожиданные сравнения
- Живой, сочный язык

Оригинал (сохрани ВСЕ города, марки машин, имена и ключевые слова):
{text}

Ответ должен быть ТОЛЬКО переписанным текстом с ТОЧНО ТАКИМИ ЖЕ именами собственными.""",
        
        "hemingway": """Перепиши этот текст в стиле Эрнеста Хемингуэя, СОХРАНЯЯ:

1) ВСЕ ИМЕНА СОБСТВЕННЫЕ (названия городов, имена людей, бренды, марки машин) - ОБЯЗАТЕЛЬНО
2) КЛЮЧЕВЫЕ РЕАЛИИ (специфические термины, сленг) - ОБЯЗАТЕЛЬНО
3) СТРУКТУРУ АБЗАЦЕВ (\\n\\n между абзацами)

Стиль:
- Максимально коротко
- Сухо, фактографично
- Минимум прилагательных
- Короткие предложения

Оригинал (сохрани ВСЕ города, марки машин, имена и ключевые слова):
{text}

Ответ должен быть ТОЛЬКО переписанным текстом с ТОЧНО ТАКИМИ ЖЕ именами собственными.""",
    }
    
    # Добавляем информацию о важных терминах в промпт
    if important_terms:
        terms_hint = f"\n\nВАЖНО: В тексте есть ключевые слова, которые НУЖНО СОХРАНИТЬ: {', '.join(important_terms[:5])}"
    else:
        terms_hint = ""
    
    prompt = style_prompts.get(style, style_prompts["chekhov"]).format(text=text) + terms_hint
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://localhost",
                    "X-Title": "Telegram Reposter Bot"
                },
                json={
                    "model": "openai/gpt-3.5-turbo",
                    "messages": [
                        {"role": "system", "content": "Ты - литературный стилист. Переписываешь тексты в стиле известных писателей. ВАЖНО: всегда сохраняй имена собственные, города, марки машин и ключевые слова из оригинала."},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.7,
                    "max_tokens": 1000
                }
            ) as response:
                result = await response.json()
                if "choices" in result and len(result["choices"]) > 0:
                    rewritten = result["choices"][0]["message"]["content"].strip()
                    rewritten = rewritten.strip('"\'')
                    
                    # Проверяем структуру
                    new_paragraphs = rewritten.split('\n\n')
                    log.info(f"   📐 Новая структура: {len(new_paragraphs)} абзацев")
                    
                    # Проверяем качество рерайта
                    new_length = len(rewritten)
                    length_ratio = new_length / original_length if original_length > 0 else 1
                    
                    if length_ratio > REWRITE_MAX_LENGTH_RATIO:
                        log.warning(f"      ⚠️ Рерайт слишком длинный (в {length_ratio:.1f} раз)")
                        if length_ratio > 1.5:
                            log.info("      ⏭️ Пропускаем рерайт - слишком много воды")
                            return text
                    
                    # Проверяем наличие ссылок
                    if re.search(r't\.me/\S+', rewritten):
                        log.warning("      ⚠️ В переписанном тексте остались ссылки, удаляем...")
                        rewritten = re.sub(r't\.me/\S+', '', rewritten)
                        rewritten = re.sub(r'\s+', ' ', rewritten).strip()
                    
                    # Постредактирование - восстанавливаем потерянные термины
                    rewritten = post_edit_rewrite(text, rewritten)
                    
                    return rewritten
                else:
                    log.error(f"Ошибка OpenRouter: {result}")
                    return text
    except Exception as e:
        log.error(f"Ошибка при рерайте: {e}")
        return text

class DuplicateFilter:
    def __init__(self, max_age_hours=24, max_size=1000):
        self.max_age = timedelta(hours=max_age_hours)
        self.max_size = max_size
        self.message_hashes = deque()
        log.info(f"DuplicateFilter инициализирован: хранение {max_age_hours}ч, макс {max_size} записей")
    
    def clean_old(self):
        now = datetime.now()
        while self.message_hashes and (now - self.message_hashes[0][1] > self.max_age):
            self.message_hashes.popleft()
    
    def is_duplicate(self, text):
        if not text:
            return False
        self.clean_old()
        msg_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        for h, _ in self.message_hashes:
            if h == msg_hash:
                return True
        self.message_hashes.append((msg_hash, datetime.now()))
        if len(self.message_hashes) > self.max_size:
            self.message_hashes.popleft()
        return False

class ContentFilter:
    @staticmethod
    def has_ads(text):
        if not text:
            return False
        text_lower = text.lower()
        for word in ALLOWED_WORDS:
            if word in text_lower:
                return False
        for word in AD_KEYWORDS:
            if word in text_lower:
                log.info(f"Обнаружена реклама: слово '{word}'")
                return True
        return False
    
    @staticmethod
    async def is_nsfw(image_path):
        if not SIGHTENGINE_USER or not SIGHTENGINE_SECRET:
            log.warning("Sightengine не настроен, пропускаем NSFW проверку")
            return False
        try:
            async with aiohttp.ClientSession() as session:
                with open(image_path, 'rb') as f:
                    data = aiohttp.FormData()
                    data.add_field('media', f, filename='image.jpg')
                    async with session.post(
                        'https://api.sightengine.com/1.0/check.json',
                        params={
                            'models': 'nudity',
                            'api_user': SIGHTENGINE_USER,
                            'api_secret': SIGHTENGINE_SECRET
                        },
                        data=data
                    ) as response:
                        result = await response.json()
                        nsfw_score = result.get('nudity', {}).get('raw', 0)
                        if nsfw_score > NSFW_THRESHOLD:
                            log.info(f"⚠️ NSFW обнаружен! Счет: {nsfw_score}")
                            return True
                        return False
        except Exception as e:
            log.error(f"Ошибка при NSFW проверке: {e}")
            return False

async def publish_album(client, channel, media_list, caption="", original_message=None):
    """Публикует альбом из нескольких медиа"""
    if not media_list:
        return False
    
    if len(media_list) == 1:
        await client.send_message(
            channel,
            caption,
            file=media_list[0],
            formatting_entities=original_message.entities if original_message else None,
            link_preview=False
        )
        return True
    
    log.info(f"   📸 Публикация альбома из {len(media_list)} фото")
    
    sent_message = await client.send_file(
        channel,
        media_list[0],
        caption=caption,
        formatting_entities=original_message.entities if original_message else None
    )
    
    await asyncio.sleep(0.7)
    
    for i, media in enumerate(media_list[1:], 2):
        await client.send_file(
            channel,
            media,
            reply_to=sent_message.id,
            caption=f"📸 {i}/{len(media_list)}"
        )
        await asyncio.sleep(0.7)
    
    return True

async def main():
    duplicate_filter = DuplicateFilter()
    content_filter = ContentFilter()
    
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    
    source_chats = []
    channel_entities = {}
    
    log.info("--- Загрузка каналов-источников ---")
    for s in SOURCES:
        if s.strip():
            try:
                chat = await get_entity_smart(client, s.strip())
                source_chats.append(chat)
                channel_entities[s.strip()] = chat
                log.info(f"✅ Добавлен источник: {chat.title}")
            except Exception as e:
                log.error(f"❌ Не удалось добавить {s.strip()}: {e}")
    
    log.info("--- Настройка правил для каналов ---")
    
    favorites_only_ids = set()
    for ref in CHANNELS_FAVORITES_ONLY:
        try:
            chat = await get_entity_smart(client, ref)
            favorites_only_ids.add(chat.id)
            log.info(f"   🔹 Только в Избранное: {chat.title}")
        except Exception as e:
            log.warning(f"   ⚠️ Канал для 'только Избранное' не найден: {ref}")
    
    full_check_ids = set()
    for ref in CHANNELS_FULL_CHECK:
        try:
            chat = await get_entity_smart(client, ref)
            full_check_ids.add(chat.id)
            log.info(f"   🔸 Полная проверка: {chat.title}")
        except Exception as e:
            log.warning(f"   ⚠️ Канал для 'полной проверки' не найден: {ref}")
    
    check_no_nsfw_ids = set()
    for ref in CHANNELS_CHECK_NO_NSFW:
        try:
            chat = await get_entity_smart(client, ref)
            check_no_nsfw_ids.add(chat.id)
            log.info(f"   🔹 Проверка без NSFW: {chat.title}")
        except Exception as e:
            log.warning(f"   ⚠️ Канал для 'проверки без NSFW' не найден: {ref}")
    
    rewrite_ids = set()
    for ref in CHANNELS_REWRITE:
        try:
            chat = await get_entity_smart(client, ref)
            rewrite_ids.add(chat.id)
            log.info(f"   ✍️ AI-рерайт включен: {chat.title}")
        except Exception as e:
            log.warning(f"   ⚠️ Канал для 'рерайта' не найден: {ref}")

    favorites = await client.get_entity(TARGET_FAVORITES)
    log.info(f"🎯 Избранное: {favorites.id}")
    
    channel = None
    if TARGET_CHANNEL:
        try:
            channel = await get_entity_smart(client, TARGET_CHANNEL)
            log.info(f"🎯 Канал для публикации: {channel.title}")
        except Exception as e:
            log.error(f"❌ Не удалось получить канал {TARGET_CHANNEL}: {e}")

    @client.on(events.NewMessage(chats=source_chats))
    async def handler(event):
        temp_files = []
        media_list = []
        
        try:
            chat_title = event.chat.title if event.chat else "unknown"
            log.info(f"📥 Новое сообщение из {chat_title}")
            
            original_text = event.message.text or ""
            
            # Всегда сохраняем в Избранное
            await client.send_message(
                favorites,
                original_text,
                file=event.message.media,
                formatting_entities=event.message.entities,
                link_preview=False
            )
            log.info("   ✅ Оригинал сохранён в Избранное")
            
            if not channel:
                return
            
            current_chat_id = event.chat.id
            
            if current_chat_id in favorites_only_ids:
                log.info("   ℹ️ Канал только для Избранного")
                return
            
            text_for_channel = original_text
            original_length = len(original_text) if original_text else 0
            
            if text_for_channel:
                text_for_channel = clean_text(text_for_channel)
                log.info("   🔗 Ссылки и призывы удалены")
            
            # AI-рерайт
            if (text_for_channel and OPENROUTER_API_KEY and 
                (not rewrite_ids or current_chat_id in rewrite_ids) and
                len(text_for_channel) >= REWRITE_MIN_LENGTH):
                
                log.info(f"   ✍️ Применяем AI-рерайт в стиле {REWRITE_STYLE}...")
                rewritten = await rewrite_text_ai(text_for_channel, REWRITE_STYLE, original_length)
                if rewritten and rewritten != text_for_channel:
                    text_for_channel = rewritten
                    log.info("      ✅ Текст переписан")
                else:
                    log.info("      ⏭️ Рерайт не изменил текст")
            
            should_publish = True
            skip_reason = []
            
            if original_text and duplicate_filter.is_duplicate(original_text):
                should_publish = False
                skip_reason.append("дубликат")
            
            if should_publish and original_text and content_filter.has_ads(original_text):
                should_publish = False
                skip_reason.append("реклама")
            
            if should_publish and event.message.media:
                if hasattr(event.message, 'grouped_id') and event.message.grouped_id:
                    log.info(f"   📸 Обнаружен альбом")
                media_list.append(event.message.media)
            
            # NSFW проверка
            if should_publish and media_list and current_chat_id in full_check_ids:
                log.info(f"   🔞 Проверяем {len(media_list)} медиа на NSFW...")
                for idx, media in enumerate(media_list):
                    temp_file = None
                    try:
                        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                            temp_file = tmp.name
                            temp_files.append(temp_file)
                        
                        await client.download_media(media, temp_file)
                        
                        if await content_filter.is_nsfw(temp_file):
                            should_publish = False
                            skip_reason.append(f"NSFW")
                            log.info(f"      🚫 NSFW обнаружен")
                            break
                    except Exception as e:
                        log.error(f"      Ошибка при обработке медиа: {e}")
            
            # Публикация
            if should_publish:
                if media_list:
                    await publish_album(client, channel, media_list, text_for_channel, event.message)
                    log.info(f"   ✅ Опубликовано в канал ({len(media_list)} медиа)")
                else:
                    await client.send_message(
                        channel,
                        text_for_channel,
                        formatting_entities=event.message.entities,
                        link_preview=False
                    )
                    log.info("   ✅ Текст опубликован в канал")
            else:
                log.info(f"   ⏭️ Пропущено: {', '.join(skip_reason)}")
            
        except FloodWaitError as e:
            log.warning(f"⏳ Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log.error(f"Ошибка: {e}", exc_info=True)
        finally:
            for temp_file in temp_files:
                if temp_file and os.path.exists(temp_file):
                    try:
                        await asyncio.sleep(0.1)
                        os.unlink(temp_file)
                    except Exception as e:
                        log.error(f"Не удалось удалить файл {temp_file}: {e}")
    
    log.info("🚀 Бот запущен с улучшенным сохранением имён и удалением ссылок!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())