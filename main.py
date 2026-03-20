#!/usr/bin/env python3
import asyncio
import logging
import os
import hashlib
import re
import random
import tempfile
from collections import deque
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChannel
from telethon.sessions import StringSession
from dotenv import load_dotenv
import aiohttp

load_dotenv()

# --- Основные настройки ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
STRING_SESSION = os.getenv("STRING_SESSION", "")

# --- Настройки фильтров ---
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", 0.7))
SIGHTENGINE_USER = os.getenv("SIGHTENGINE_USER", "")
SIGHTENGINE_SECRET = os.getenv("SIGHTENGINE_SECRET", "")
REWRITE_MIN_LENGTH = int(os.getenv("REWRITE_MIN_LENGTH", "20"))
AVAILABLE_STYLES = ["chekhov", "dostoevsky", "tolstoy", "pushkin", "gogol", "bulgakov", "hemingway", "orwell"]
current_rewrite_style = "random"

# --- Каналы ---
SOURCES = os.getenv("RAW_SOURCES", "").split(",")
TARGET_FAVORITES = os.getenv("TARGET_FAVORITES", "me")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")

# --- Списки каналов ---
CHANNELS_FAVORITES_ONLY = os.getenv("CHANNELS_FAVORITES_ONLY", "").split(",")
CHANNELS_FULL_CHECK = os.getenv("CHANNELS_FULL_CHECK", "").split(",")
CHANNELS_CHECK_NO_NSFW = os.getenv("CHANNELS_CHECK_NO_NSFW", "").split(",")
CHANNELS_REWRITE = os.getenv("CHANNELS_REWRITE", "").split(",")

CHANNELS_FAVORITES_ONLY = [c.strip() for c in CHANNELS_FAVORITES_ONLY if c.strip()]
CHANNELS_FULL_CHECK = [c.strip() for c in CHANNELS_FULL_CHECK if c.strip()]
CHANNELS_CHECK_NO_NSFW = [c.strip() for c in CHANNELS_CHECK_NO_NSFW if c.strip()]
CHANNELS_REWRITE = [c.strip() for c in CHANNELS_REWRITE if c.strip()]

# --- Стоп-слова ---
AD_KEYWORDS = ["реклама", "промо", "скидка", "акция", "купить", "оформить", "подпишись", "переходи", "ссылка", "Лепру"]
ALLOWED_WORDS = ["спасибо", "пожалуйста", "спс", "благодарю"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ===================== ФИЛЬТР ДУБЛИКАТОВ =====================

class DuplicateFilter:
    def __init__(self, max_age_hours=24, max_size=2000):
        self.max_age = timedelta(hours=max_age_hours)
        self.max_size = max_size
        self.message_hashes = deque()
        self.published_ids = set()
        log.info(f"🔄 DuplicateFilter инициализирован: хранение {max_age_hours}ч, макс {max_size} записей")
    
    def clean_old(self):
        now = datetime.now()
        while self.message_hashes and (now - self.message_hashes[0][2] > self.max_age):
            old_hash = self.message_hashes.popleft()
            if old_hash[0] == 'msg_id':
                self.published_ids.discard(old_hash[1])
    
    def add_message(self, message_id=None, text=None, media_path=None):
        now = datetime.now()
        if message_id:
            self.message_hashes.append(('msg_id', message_id, now))
            self.published_ids.add(message_id)
        if text:
            text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
            self.message_hashes.append(('text', text_hash, now))
        if media_path and os.path.exists(media_path):
            try:
                with open(media_path, 'rb') as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()
                self.message_hashes.append(('media', file_hash, now))
            except Exception:
                pass
        while len(self.message_hashes) > self.max_size:
            removed = self.message_hashes.popleft()
            if removed[0] == 'msg_id':
                self.published_ids.discard(removed[1])
    
    def is_duplicate(self, text=None, media_path=None, message_id=None):
        self.clean_old()
        if message_id and message_id in self.published_ids:
            return True
        if text:
            text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
            for hash_type, hash_value, ts in self.message_hashes:
                if hash_type == 'text' and hash_value == text_hash:
                    return True
        if media_path and os.path.exists(media_path):
            try:
                with open(media_path, 'rb') as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()
                for hash_type, hash_value, ts in self.message_hashes:
                    if hash_type == 'media' and hash_value == file_hash:
                        return True
            except Exception:
                pass
        return False

# ===================== ФИЛЬТР КОНТЕНТА =====================

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
                return True
        url_pattern = r'https?://[^\s]+|t\.me/[^\s]+|@\w+'
        urls = re.findall(url_pattern, text)
        if len(urls) > 2:
            return True
        return False
    
    @staticmethod
    async def is_nsfw(image_path):
        if not SIGHTENGINE_USER or not SIGHTENGINE_SECRET:
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
                        return nsfw_score > NSFW_THRESHOLD
        except Exception:
            return False

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

async def get_entity_smart(client, entity_input):
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
        except Exception:
            raise
    return await client.get_entity(entity_input)

def clean_text(text):
    if not text:
        return text
    original = text
    text = re.sub(r'https?://[^\s]+', '', text)
    text = re.sub(r't\.me/[^\s]+', '', text)
    text = re.sub(r'@\w+', '', text)
    subscribe_phrases = [
        r'подпишись\s*на\s*канал',
        r'подписывайся\s*на',
        r'заходи\s*на\s*канал',
        r'вступай\s*в\s*группу',
        r'переходи\s*по\s*ссылке',
        r'ссылка\s*на\s*канал',
    ]
    for phrase in subscribe_phrases:
        text = re.sub(phrase, '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text and original and any(word in original for word in ['http', 't.me', '@']):
        return "[Медиафайл]"
    return text

async def rewrite_text_local(text, style_override=None):
    global current_rewrite_style
    if not text or len(text) < REWRITE_MIN_LENGTH:
        return text
    if style_override:
        style = style_override
    elif current_rewrite_style == "random":
        style = random.choice(AVAILABLE_STYLES)
    else:
        style = current_rewrite_style
    
    style_dicts = {
        "chekhov": {"очень": "весьма", "классно": "недурно", "круто": "превосходно", "плохо": "скверно", "хорошо": "изрядно", "смешно": "забавно", "думаю": "полагаю", "понимаю": "сознаю", "человек": "персона"},
        "dostoevsky": {"очень": "чрезвычайно", "думаю": "размышляю", "понимаю": "постигаю", "чувствую": "ощущаю душой", "страшно": "жутко", "хорошо": "благостно", "плохо": "скверно", "человек": "существо", "жизнь": "существование"},
        "tolstoy": {"очень": "весьма", "хорошо": "добро", "плохо": "дурно", "думаю": "мыслю", "понимаю": "разумею", "человек": "человек", "жизнь": "житие", "смерть": "кончина"},
        "pushkin": {"очень": "зело", "хорошо": "изрядно", "плохо": "худо", "красиво": "прекрасно", "говорит": "молвит", "смотрит": "взирает", "думает": "мыслит", "человек": "отрок", "глаза": "очи", "лицо": "лик"},
        "gogol": {"очень": "чрезвычайно", "хорошо": "славно", "плохо": "пакостно", "смешно": "уморительно", "странно": "диковинно", "человек": "субъект", "голова": "котелок", "думает": "размышляет"},
        "bulgakov": {"очень": "крайне", "хорошо": "превосходно", "плохо": "скверно", "странно": "таинственно", "непонятно": "загадочно", "человек": "гражданин", "черт": "дьявол", "сказал": "изрек"},
        "hemingway": {"очень": "", "действительно": "", "на самом деле": "", "в общем": "", "в принципе": "", "как бы": "", "типа": "", "кажется": ""},
        "orwell": {"хорошо": "правильно", "плохо": "неправильно", "свобода": "порабощение", "правда": "ложь", "война": "мир", "человек": "винтик", "общество": "система"}
    }
    if style in style_dicts:
        for old, new in style_dicts[style].items():
            if new:
                text = re.sub(r'\b' + old + r'\b', new, text, flags=re.IGNORECASE)
            else:
                text = re.sub(r'\s*' + old + r'\s*', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

async def publish_album(client, channel, media_list, caption="", original_message=None):
    if not media_list:
        return False
    if len(media_list) == 1:
        await client.send_message(channel, caption, file=media_list[0], link_preview=False)
        return True
    sent_message = await client.send_file(channel, media_list[0], caption=caption)
    await asyncio.sleep(0.7)
    for i, media in enumerate(media_list[1:], 2):
        await client.send_file(channel, media, reply_to=sent_message.id, caption=f"📸 {i}/{len(media_list)}")
        await asyncio.sleep(0.7)
    return True

# ===================== ОСНОВНАЯ ФУНКЦИЯ =====================

async def main():
    global current_rewrite_style
    
    duplicate_filter = DuplicateFilter()
    content_filter = ContentFilter()
    
    # Создаём клиент с StringSession
    if STRING_SESSION:
        log.info("🔐 Использую STRING_SESSION из переменных окружения")
        session = StringSession(STRING_SESSION)
        client = TelegramClient(session, API_ID, API_HASH)
    else:
        log.info("📁 Использую файл сессии main_user.session")
        client = TelegramClient("main_user", API_ID, API_HASH)
    
    log.info("🚀 Запуск бота...")
    await client.start()
    
    me = await client.get_me()
    log.info(f"✅ Авторизован как: {me.first_name} (@{me.username})")
    
    # --- Загрузка каналов ---
    source_chats = []
    log.info("--- Загрузка каналов-источников ---")
    for s in SOURCES:
        if s.strip():
            try:
                chat = await get_entity_smart(client, s.strip())
                source_chats.append(chat)
                log.info(f"✅ Добавлен источник: {chat.title}")
            except Exception as e:
                log.error(f"❌ Не удалось добавить {s.strip()}: {e}")
    
    # --- Настройка правил ---
    favorites_only_ids = set()
    full_check_ids = set()
    rewrite_ids = set()
    
    for ref in CHANNELS_FAVORITES_ONLY:
        try:
            chat = await get_entity_smart(client, ref)
            favorites_only_ids.add(chat.id)
            log.info(f"   🔹 Только в Избранное: {chat.title}")
        except Exception:
            pass
    
    for ref in CHANNELS_FULL_CHECK:
        try:
            chat = await get_entity_smart(client, ref)
            full_check_ids.add(chat.id)
            log.info(f"   🔸 Полная проверка: {chat.title}")
        except Exception:
            pass
    
    for ref in CHANNELS_REWRITE:
        try:
            chat = await get_entity_smart(client, ref)
            rewrite_ids.add(chat.id)
            log.info(f"   ✍️ Рерайт включен: {chat.title}")
        except Exception:
            pass

    # --- Целевые чаты ---
    favorites = await client.get_entity(TARGET_FAVORITES)
    log.info(f"🎯 Избранное: {favorites.id}")
    
    channel = None
    if TARGET_CHANNEL:
        try:
            channel = await get_entity_smart(client, TARGET_CHANNEL)
            log.info(f"🎯 Канал для публикации: {channel.title}")
        except Exception as e:
            log.error(f"❌ Не удалось получить канал: {e}")

    # --- Обработчик сообщений ---
    @client.on(events.NewMessage(chats=source_chats))
    async def handler(event):
        temp_files = []
        temp_check_file = None
        
        try:
            if duplicate_filter.is_duplicate(message_id=event.message.id):
                return
            
            log.info(f"📥 Новое сообщение из {event.chat.title}")
            original_text = event.message.text or ""
            
            # Сохраняем в Избранное
            await client.send_message(favorites, original_text, file=event.message.media, link_preview=False)
            log.info("   ✅ Сохранено в Избранное")
            duplicate_filter.add_message(message_id=event.message.id)
            
            if not channel:
                return
            
            if event.chat.id in favorites_only_ids:
                log.info("   ℹ️ Канал только для Избранного")
                return
            
            text_for_channel = clean_text(original_text) if original_text else ""
            
            if text_for_channel and (not rewrite_ids or event.chat.id in rewrite_ids) and len(text_for_channel) >= REWRITE_MIN_LENGTH:
                rewritten = await rewrite_text_local(text_for_channel)
                if rewritten != text_for_channel:
                    text_for_channel = rewritten
                    log.info("   ✍️ Текст переписан")
            
            should_publish = True
            skip_reason = []
            
            # Проверка дубликатов медиа
            media_list = []
            if event.message.media:
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                        temp_check_file = tmp.name
                    await client.download_media(event.message, temp_check_file)
                    if duplicate_filter.is_duplicate(media_path=temp_check_file):
                        should_publish = False
                        skip_reason.append("дубликат медиа")
                        log.info("      🚫 Дубликат медиа")
                except Exception as e:
                    log.error(f"      Ошибка проверки медиа: {e}")
            
            # Проверка рекламы
            if should_publish and original_text and content_filter.has_ads(original_text):
                should_publish = False
                skip_reason.append("реклама")
                log.info("      🚫 Обнаружена реклама")
            
            # Сбор медиа для публикации
            if should_publish and event.message.media:
                media_list.append(event.message.media)
            
            # NSFW проверка
            if should_publish and media_list and event.chat.id in full_check_ids:
                for media in media_list:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                        tf = tmp.name
                        temp_files.append(tf)
                    await client.download_media(media, tf)
                    if await content_filter.is_nsfw(tf):
                        should_publish = False
                        skip_reason.append("NSFW")
                        log.info("      🚫 NSFW обнаружен")
                        break
            
            # Публикация
            if should_publish:
                if media_list:
                    await publish_album(client, channel, media_list, text_for_channel, event.message)
                    log.info(f"   ✅ Опубликовано в канал ({len(media_list)} медиа)")
                    if temp_check_file:
                        duplicate_filter.add_message(media_path=temp_check_file)
                else:
                    if text_for_channel:
                        await client.send_message(channel, text_for_channel, link_preview=False)
                        log.info("   ✅ Текст опубликован")
                duplicate_filter.add_message(message_id=event.message.id)
            else:
                log.info(f"   ⏭️ Пропущено: {', '.join(skip_reason)}")
            
        except FloodWaitError as e:
            log.warning(f"⏳ Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log.error(f"Ошибка: {e}")
        finally:
            for tf in temp_files:
                if tf and os.path.exists(tf):
                    try:
                        os.unlink(tf)
                    except:
                        pass
            if temp_check_file and os.path.exists(temp_check_file):
                try:
                    os.unlink(temp_check_file)
                except:
                    pass
    
    log.info("🚀 Бот запущен и готов к работе!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())