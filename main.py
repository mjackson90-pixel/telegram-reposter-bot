#!/usr/bin/env python3
"""
Telegram UserBot — сбор контента, фильтрация, рерайт, автоподписка
Объединяет лучшее из двух подходов: твой бот + функции из статьи
"""

import asyncio
import logging
import os
import hashlib
import re
import random
from collections import deque
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, ChannelPrivateError, UsernameNotOccupiedError
from telethon.tl.types import PeerChannel
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

# ===================== КОНФИГУРАЦИЯ =====================

# Основные настройки
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
STRING_SESSION = os.getenv("STRING_SESSION", "")
SOURCES = os.getenv("RAW_SOURCES", "").split(",")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")
TARGET_FAVORITES = os.getenv("TARGET_FAVORITES", "me")

# Настройки фильтров
REWRITE_MIN_LENGTH = int(os.getenv("REWRITE_MIN_LENGTH", "20"))
AVAILABLE_STYLES = ["chekhov", "dostoevsky", "tolstoy", "pushkin", "gogol", "bulgakov", "hemingway", "orwell"]
current_rewrite_style = "random"

# Списки каналов с разными правилами
CHANNELS_FAVORITES_ONLY = os.getenv("CHANNELS_FAVORITES_ONLY", "").split(",")
CHANNELS_FULL_CHECK = os.getenv("CHANNELS_FULL_CHECK", "").split(",")
CHANNELS_REWRITE = os.getenv("CHANNELS_REWRITE", "").split(",")

CHANNELS_FAVORITES_ONLY = [c.strip() for c in CHANNELS_FAVORITES_ONLY if c.strip()]
CHANNELS_FULL_CHECK = [c.strip() for c in CHANNELS_FULL_CHECK if c.strip()]
CHANNELS_REWRITE = [c.strip() for c in CHANNELS_REWRITE if c.strip()]

# Стоп-слова для рекламы
AD_KEYWORDS = ["реклама", "промо", "скидка", "акция", "купить", "оформить", "подпишись", "переходи", "ссылка"]
ALLOWED_WORDS = ["спасибо", "пожалуйста", "спс", "благодарю"]

# Фразы для удаления
REMOVE_PHRASES = [
    r'(?i)лепра',
    r'(?i)BWM',
    r'(?i)Kameraden',
    r'(?i)подпишись\s*на\s*канал',
    r'(?i)переходи\s*по\s*ссылке',
]

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


# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

async def get_entity_smart(client, entity_input):
    """Умное получение сущности по ссылке, имени или ID"""
    entity_input = entity_input.strip()
    if entity_input.startswith(('t.me/', 'https://t.me/')):
        return await client.get_entity(entity_input)
    if entity_input.startswith('@'):
        return await client.get_entity(entity_input)
    if entity_input.lstrip('-').isdigit():
        try:
            return await client.get_entity(int(entity_input))
        except Exception:
            return await client.get_entity(PeerChannel(int(entity_input)))
    return await client.get_entity(entity_input)


async def auto_join_channels(client, channels):
    """Автоматическая подписка на каналы (из статьи)"""
    joined = []
    failed = []
    for channel in channels:
        if not channel.strip():
            continue
        try:
            await client.join_channel(channel)
            joined.append(channel)
            log.info(f"✅ Подписался на {channel}")
        except ChannelPrivateError:
            log.warning(f"⚠️ Канал {channel} приватный, подписка невозможна")
            failed.append(channel)
        except UsernameNotOccupiedError:
            log.warning(f"⚠️ Канал {channel} не найден")
            failed.append(channel)
        except Exception as e:
            log.warning(f"⚠️ Не удалось подписаться на {channel}: {e}")
            failed.append(channel)
        await asyncio.sleep(0.5)  # пауза между подписками
    return joined, failed


def clean_text(text):
    """Очистка текста от ссылок, рекламы и нежелательных фраз"""
    if not text:
        return text
    original = text
    
    # Удаляем ссылки и упоминания
    text = re.sub(r'https?://[^\s]+', '', text)
    text = re.sub(r't\.me/[^\s]+', '', text)
    text = re.sub(r'@\w+', '', text)
    
    # Удаляем нежелательные фразы
    for phrase in REMOVE_PHRASES:
        text = re.sub(phrase, '', text, flags=re.IGNORECASE)
    
    # Удаляем призывы подписаться
    text = re.sub(r'(?i)подпишись\s*на\s*канал', '', text)
    text = re.sub(r'(?i)переходи\s*по\s*ссылке', '', text)
    
    # Очищаем пробелы
    text = re.sub(r'\s+', ' ', text).strip()
    
    if not text and original and any(word in original for word in ['http', 't.me', '@']):
        return "[Медиафайл]"
    return text


async def rewrite_text_local(text, style_override=None):
    """Локальный рерайт текста"""
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
        "chekhov": {"очень": "весьма", "классно": "недурно", "круто": "превосходно", "плохо": "скверно", "хорошо": "изрядно", "смешно": "забавно"},
        "dostoevsky": {"очень": "чрезвычайно", "думаю": "размышляю", "понимаю": "постигаю", "страшно": "жутко", "хорошо": "благостно"},
        "tolstoy": {"очень": "весьма", "хорошо": "добро", "плохо": "дурно", "думаю": "мыслю"},
        "pushkin": {"очень": "зело", "хорошо": "изрядно", "плохо": "худо", "красиво": "прекрасно", "говорит": "молвит", "смотрит": "взирает"},
        "gogol": {"очень": "чрезвычайно", "хорошо": "славно", "плохо": "пакостно", "смешно": "уморительно", "странно": "диковинно"},
        "bulgakov": {"очень": "крайне", "хорошо": "превосходно", "плохо": "скверно", "странно": "таинственно"},
        "hemingway": {"очень": "", "действительно": "", "на самом деле": "", "в общем": "", "в принципе": ""},
        "orwell": {"хорошо": "правильно", "плохо": "неправильно", "свобода": "порабощение", "правда": "ложь"}
    }
    
    if style in style_dicts:
        for old, new in style_dicts[style].items():
            if new:
                text = re.sub(r'\b' + old + r'\b', new, text, flags=re.IGNORECASE)
            else:
                text = re.sub(r'\s*' + old + r'\s*', ' ', text, flags=re.IGNORECASE)
    
    text = re.sub(r'\s+', ' ', text).strip()
    return text


async def publish_album(client, channel, media_list, caption=""):
    """Публикация альбома"""
    if not media_list:
        return False
    if len(media_list) == 1:
        await client.send_message(channel, caption, file=media_list[0], link_preview=False)
        return True
    sent = await client.send_file(channel, media_list[0], caption=caption)
    await asyncio.sleep(0.7)
    for i, media in enumerate(media_list[1:], 2):
        await client.send_file(channel, media, reply_to=sent.id, caption=f"📸 {i}/{len(media_list)}")
        await asyncio.sleep(0.7)
    return True


# ===================== ОСНОВНАЯ ФУНКЦИЯ =====================

async def main():
    global current_rewrite_style
    
    duplicate_filter = DuplicateFilter()
    content_filter = ContentFilter()
    
    # Инициализация клиента
    if STRING_SESSION:
        log.info("🔐 Использую STRING_SESSION")
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    else:
        log.error("❌ STRING_SESSION не задана в переменных окружения!")
        return
    
    log.info("🚀 Запуск бота...")
    await client.start()
    
    me = await client.get_me()
    log.info(f"✅ Авторизован как: {me.first_name} (@{me.username})")
    
    # ========== АВТОПОДПИСКА (из статьи) ==========
    log.info("--- Автоподписка на каналы ---")
    joined, failed = await auto_join_channels(client, SOURCES)
    log.info(f"✅ Подписалось: {len(joined)} | ❌ Не удалось: {len(failed)}")
    
    # ========== ЗАГРУЗКА КАНАЛОВ ==========
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
    
    # ========== НАСТРОЙКА ПРАВИЛ ==========
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
    
    # ========== ЦЕЛЕВЫЕ ЧАТЫ ==========
    favorites = await client.get_entity(TARGET_FAVORITES)
    log.info(f"🎯 Избранное: {favorites.id}")
    
    channel = None
    if TARGET_CHANNEL:
        try:
            channel = await get_entity_smart(client, TARGET_CHANNEL)
            log.info(f"🎯 Канал для публикации: {channel.title}")
        except Exception as e:
            log.error(f"❌ Не удалось получить канал: {e}")
    
    # ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========
    @client.on(events.NewMessage(chats=source_chats))
    async def handler(event):
        try:
            # Пропускаем дубликаты
            if duplicate_filter.is_duplicate(message_id=event.message.id):
                return
            
            log.info(f"📥 Новое сообщение из {event.chat.title}")
            original_text = event.message.text or ""
            
            # 1. СОХРАНЯЕМ В ИЗБРАННОЕ (всегда)
            await client.send_message(favorites, original_text, file=event.message.media, link_preview=False)
            log.info("   ✅ Сохранено в Избранное")
            duplicate_filter.add_message(message_id=event.message.id)
            
            # Если нет целевого канала — выходим
            if not channel:
                return
            
            # Если канал только для Избранного — не публикуем
            if event.chat.id in favorites_only_ids:
                log.info("   ℹ️ Канал только для Избранного")
                return
            
            # 2. ПОДГОТОВКА ТЕКСТА
            text_for_channel = clean_text(original_text) if original_text else ""
            
            # Рерайт (если включен)
            if text_for_channel and (not rewrite_ids or event.chat.id in rewrite_ids) and len(text_for_channel) >= REWRITE_MIN_LENGTH:
                rewritten = await rewrite_text_local(text_for_channel)
                if rewritten != text_for_channel:
                    text_for_channel = rewritten
                    log.info("   ✍️ Текст переписан")
            
            # 3. ФИЛЬТРАЦИЯ
            should_publish = True
            skip_reason = []
            
            # Проверка рекламы
            if should_publish and original_text and content_filter.has_ads(original_text):
                should_publish = False
                skip_reason.append("реклама")
                log.info("   🚫 Обнаружена реклама")
            
            # 4. ПУБЛИКАЦИЯ
            if should_publish:
                if event.message.media:
                    media_list = [event.message.media]
                    await publish_album(client, channel, media_list, text_for_channel)
                    log.info(f"   ✅ Опубликовано в канал (медиа)")
                else:
                    if text_for_channel:
                        await client.send_message(channel, text_for_channel, link_preview=False)
                        log.info("   ✅ Опубликовано в канал (текст)")
                duplicate_filter.add_message(message_id=event.message.id)
            else:
                log.info(f"   ⏭️ Пропущено: {', '.join(skip_reason)}")
            
            # 5. ОТМЕТКА О ПРОЧТЕНИИ (из статьи)
            try:
                await client.send_read_acknowledge(event.chat_id, max_id=event.message.id)
                log.info("   👁️ Отмечено как прочитанное")
            except Exception as e:
                log.debug(f"Не удалось отметить прочитанным: {e}")
            
        except FloodWaitError as e:
            log.warning(f"⏳ Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log.error(f"Ошибка: {e}")
    
    log.info("🚀 Бот запущен и готов к работе!")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
