#!/usr/bin/env python3
import asyncio
import logging
import os
import hashlib
import re
import random
from collections import deque
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerChannel
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

# ===================== КОНФИГУРАЦИЯ =====================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
STRING_SESSION = os.getenv("STRING_SESSION", "")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")
TARGET_FAVORITES = os.getenv("TARGET_FAVORITES", "me")

# Читаем каналы из файла channels.txt
def load_sources():
    sources = []
    try:
        with open("channels.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    sources.append(line)
    except FileNotFoundError:
        logging.error("❌ channels.txt не найден!")
    return sources

SOURCES = load_sources()

# Настройки фильтров
REWRITE_MIN_LENGTH = int(os.getenv("REWRITE_MIN_LENGTH", "20"))
AVAILABLE_STYLES = ["chekhov", "dostoevsky", "tolstoy", "pushkin", "gogol", "bulgakov", "hemingway", "orwell"]
current_rewrite_style = "random"

# Списки каналов с правилами
CHANNELS_FAVORITES_ONLY = os.getenv("CHANNELS_FAVORITES_ONLY", "").split(",")
CHANNELS_FULL_CHECK = os.getenv("CHANNELS_FULL_CHECK", "").split(",")
CHANNELS_REWRITE = os.getenv("CHANNELS_REWRITE", "").split(",")

CHANNELS_FAVORITES_ONLY = [c.strip() for c in CHANNELS_FAVORITES_ONLY if c.strip()]
CHANNELS_FULL_CHECK = [c.strip() for c in CHANNELS_FULL_CHECK if c.strip()]
CHANNELS_REWRITE = [c.strip() for c in CHANNELS_REWRITE if c.strip()]

# Стоп-слова и фразы для очистки
AD_KEYWORDS = ["реклама", "промо", "скидка", "акция", "купить", "оформить", "подпишись", "переходи", "ссылка"]
ALLOWED_WORDS = ["спасибо", "пожалуйста", "спс", "благодарю"]
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
        log.info(f"🔄 DuplicateFilter инициализирован: хранение {max_age_hours}ч")

    def clean_old(self):
        now = datetime.now()
        while self.message_hashes and (now - self.message_hashes[0][2] > self.max_age):
            old = self.message_hashes.popleft()
            if old[0] == 'msg_id':
                self.published_ids.discard(old[1])

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
            for t, h, _ in self.message_hashes:
                if t == 'text' and h == text_hash:
                    return True
        if media_path and os.path.exists(media_path):
            try:
                with open(media_path, 'rb') as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()
                for t, h, _ in self.message_hashes:
                    if t == 'media' and h == file_hash:
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
        urls = re.findall(r'https?://[^\s]+|t\.me/[^\s]+|@\w+', text)
        return len(urls) > 2


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
    if not text:
        return text
    original = text
    text = re.sub(r'https?://[^\s]+', '', text)
    text = re.sub(r't\.me/[^\s]+', '', text)
    text = re.sub(r'@\w+', '', text)
    for phrase in REMOVE_PHRASES:
        text = re.sub(phrase, '', text, flags=re.IGNORECASE)
    text = re.sub(r'(?i)подпишись\s*на\s*канал', '', text)
    text = re.sub(r'(?i)переходи\s*по\s*ссылке', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text and any(w in original for w in ['http', 't.me', '@']):
        return "[Медиафайл]"
    return text


async def rewrite_text_local(text):
    global current_rewrite_style
    if not text or len(text) < REWRITE_MIN_LENGTH:
        return text
    style = random.choice(AVAILABLE_STYLES) if current_rewrite_style == "random" else current_rewrite_style
    style_dicts = {
        "chekhov": {"очень": "весьма", "классно": "недурно", "круто": "превосходно"},
        "dostoevsky": {"очень": "чрезвычайно", "думаю": "размышляю"},
        "tolstoy": {"очень": "весьма", "хорошо": "добро", "плохо": "дурно"},
        "pushkin": {"очень": "зело", "хорошо": "изрядно", "плохо": "худо"},
        "gogol": {"очень": "чрезвычайно", "смешно": "уморительно"},
        "bulgakov": {"очень": "крайне", "странно": "таинственно"},
        "hemingway": {"очень": "", "действительно": ""},
        "orwell": {"хорошо": "правильно", "плохо": "неправильно"}
    }
    for old, new in style_dicts.get(style, {}).items():
        if new:
            text = re.sub(r'\b' + old + r'\b', new, text, flags=re.IGNORECASE)
        else:
            text = re.sub(r'\s*' + old + r'\s*', ' ', text, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', text).strip()


async def publish_album(client, channel, media_list, caption=""):
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

    if not STRING_SESSION:
        log.error("❌ STRING_SESSION не задана!")
        return

    log.info("🔐 Использую STRING_SESSION")
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    log.info(f"✅ Авторизован: {me.first_name} (@{me.username})")

    # Загружаем каналы
    source_chats = []
    log.info("--- Загрузка каналов-источников ---")
    for s in SOURCES:
        if not s:
            continue
        try:
            chat = await get_entity_smart(client, s)
            source_chats.append(chat)
            log.info(f"✅ {chat.title}")
        except Exception as e:
            log.error(f"❌ {s}: {e}")

    # Правила
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
            log.info(f"   ✍️ Рерайт: {chat.title}")
        except Exception:
            pass

    favorites = await client.get_entity(TARGET_FAVORITES)
    log.info(f"🎯 Избранное: {favorites.id}")

    channel = None
    if TARGET_CHANNEL:
        try:
            channel = await get_entity_smart(client, TARGET_CHANNEL)
            log.info(f"🎯 Канал: {channel.title}")
        except Exception as e:
            log.error(f"❌ Канал: {e}")

    @client.on(events.NewMessage(chats=source_chats))
    async def handler(event):
        try:
            if duplicate_filter.is_duplicate(message_id=event.message.id):
                return

            log.info(f"📥 {event.chat.title}")
            original_text = event.message.text or ""

            # Избранное
            await client.send_message(favorites, original_text, file=event.message.media, link_preview=False)
            duplicate_filter.add_message(message_id=event.message.id)

            if not channel or event.chat.id in favorites_only_ids:
                return

            text_for_channel = clean_text(original_text)
            if text_for_channel and (not rewrite_ids or event.chat.id in rewrite_ids) and len(text_for_channel) >= REWRITE_MIN_LENGTH:
                text_for_channel = await rewrite_text_local(text_for_channel)

            should_publish = not content_filter.has_ads(original_text)
            if should_publish:
                if event.message.media:
                    await publish_album(client, channel, [event.message.media], text_for_channel)
                elif text_for_channel:
                    await client.send_message(channel, text_for_channel, link_preview=False)
                log.info("   ✅ Опубликовано")
            else:
                log.info("   🚫 Реклама, пропущено")

            try:
                await client.send_read_acknowledge(event.chat_id, max_id=event.message.id)
            except Exception:
                pass

        except FloodWaitError as e:
            log.warning(f"⏳ Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log.error(f"Ошибка: {e}")

    log.info("🚀 Бот запущен")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
