#!/usr/bin/env python3
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
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

# --- Основные настройки ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
STRING_SESSION = os.getenv("STRING_SESSION", "")

# --- Каналы ---
SOURCES = os.getenv("RAW_SOURCES", "").split(",")
TARGET_FAVORITES = os.getenv("TARGET_FAVORITES", "me")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")

# --- Каналы, которые НЕ публикуются в канал (только в Избранное) ---
FAVORITES_ONLY = os.getenv("CHANNELS_FAVORITES_ONLY", "").split(",")
FAVORITES_ONLY = [c.strip() for c in FAVORITES_ONLY if c.strip()]

# --- Стоп-слова (реклама) ---
AD_WORDS = ["реклама", "промо", "скидка", "акция", "купить", "оформить", "подпишись", "переходи", "ссылка"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ===================== ФИЛЬТР ДУБЛИКАТОВ =====================

class DuplicateFilter:
    def __init__(self, max_age_hours=24):
        self.max_age = timedelta(hours=max_age_hours)
        self.text_hashes = deque()  # (hash, timestamp)
    
    def clean_old(self):
        now = datetime.now()
        while self.text_hashes and (now - self.text_hashes[0][1] > self.max_age):
            self.text_hashes.popleft()
    
    def is_duplicate(self, text):
        if not text:
            return False
        self.clean_old()
        text_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
        for h, _ in self.text_hashes:
            if h == text_hash:
                return True
        self.text_hashes.append((text_hash, datetime.now()))
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
            return await client.get_entity(int(entity_input))
        except Exception:
            return await client.get_entity(PeerChannel(int(entity_input)))
    return await client.get_entity(entity_input)

def clean_text(text):
    """Удаляет ссылки и рекламные призывы"""
    if not text:
        return text
    
    # Удаляем ссылки
    text = re.sub(r'https?://[^\s]+', '', text)
    text = re.sub(r't\.me/[^\s]+', '', text)
    text = re.sub(r'@\w+', '', text)
    
    # Удаляем призывы подписаться
    text = re.sub(r'(?i)подпишись\s*на\s*канал', '', text)
    text = re.sub(r'(?i)переходи\s*по\s*ссылке', '', text)
    text = re.sub(r'(?i)вступай\s*в\s*группу', '', text)
    
    # Удаляем упоминания конкретных каналов
    text = re.sub(r'(?i)лепра', '', text)
    text = re.sub(r'(?i)BWM', '', text)
    text = re.sub(r'(?i)Kameraden', '', text)
    
    # Очищаем пробелы
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

def has_ads(text):
    """Проверяет наличие рекламных слов"""
    if not text:
        return False
    text_lower = text.lower()
    for word in AD_WORDS:
        if word in text_lower:
            return True
    return False

# ===================== ОСНОВНАЯ ФУНКЦИЯ =====================

async def main():
    duplicate_filter = DuplicateFilter()
    
    # Создаём клиент
    if STRING_SESSION:
        log.info("🔐 Использую STRING_SESSION")
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    else:
        log.info("📁 Использую файл сессии")
        client = TelegramClient("main_user", API_ID, API_HASH)
    
    log.info("🚀 Запуск...")
    await client.start()
    
    me = await client.get_me()
    log.info(f"✅ Авторизован: {me.first_name}")
    
    # --- Загрузка каналов ---
    source_chats = []
    log.info("--- Загрузка каналов ---")
    for s in SOURCES:
        if s.strip():
            try:
                chat = await get_entity_smart(client, s.strip())
                source_chats.append(chat)
                log.info(f"✅ {chat.title}")
            except Exception as e:
                log.error(f"❌ {s.strip()}: {e}")
    
    # --- Целевые чаты ---
    favorites = await client.get_entity(TARGET_FAVORITES)
    log.info(f"🎯 Избранное: {favorites.id}")
    
    channel = None
    if TARGET_CHANNEL:
        try:
            channel = await get_entity_smart(client, TARGET_CHANNEL)
            log.info(f"🎯 Канал: {channel.title}")
        except Exception as e:
            log.error(f"❌ Не удалось получить канал: {e}")
    
    # --- ID каналов, которые только в Избранное ---
    favorites_only_ids = set()
    for ref in FAVORITES_ONLY:
        try:
            chat = await get_entity_smart(client, ref)
            favorites_only_ids.add(chat.id)
            log.info(f"   🔹 Только в Избранное: {chat.title}")
        except Exception:
            pass
    
    # --- Обработчик ---
    @client.on(events.NewMessage(chats=source_chats))
    async def handler(event):
        try:
            # Пропускаем дубликаты
            text = event.message.text or ""
            if duplicate_filter.is_duplicate(text):
                log.info(f"⏭️ Дубликат: {event.chat.title}")
                return
            
            # Сохраняем в Избранное (всегда)
            await client.send_message(favorites, text, file=event.message.media, link_preview=False)
            log.info(f"✅ Сохранено в Избранное: {event.chat.title}")
            
            # Если нет целевого канала или канал в исключениях — выходим
            if not channel or event.chat.id in favorites_only_ids:
                return
            
            # Очищаем текст
            clean = clean_text(text)
            
            # Проверяем рекламу
            if has_ads(text):
                log.info(f"   🚫 Реклама, пропущено")
                return
            
            # Публикуем
            if event.message.media:
                await client.send_file(channel, event.message.media, caption=clean, link_preview=False)
                log.info(f"✅ Опубликовано в канал (медиа)")
            elif clean:
                await client.send_message(channel, clean, link_preview=False)
                log.info(f"✅ Опубликовано в канал (текст)")
            
        except FloodWaitError as e:
            log.warning(f"⏳ Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log.error(f"Ошибка: {e}")
    
    log.info("🚀 Бот запущен")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
