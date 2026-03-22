import asyncio
import logging
import os
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
STRING_SESSION = os.getenv("STRING_SESSION", "")
SOURCES = os.getenv("RAW_SOURCES", "").split(",")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")
TARGET_FAVORITES = os.getenv("TARGET_FAVORITES", "me")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

async def main():
    # Как в статье: выбираем тип сессии
    if STRING_SESSION:
        log.info("🔐 Использую STRING_SESSION из переменных окружения")
        client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    else:
        log.info("📁 Использую файл сессии main_user.session")
        client = TelegramClient("main_user", API_ID, API_HASH)
    
    log.info("🚀 Запуск бота...")
    await client.start()
    
    me = await client.get_me()
    log.info(f"✅ Авторизован как: {me.first_name} (@{me.username})")
    
    # Загружаем каналы
    source_chats = []
    log.info("--- Загрузка каналов-источников ---")
    for s in SOURCES:
        if s.strip():
            try:
                chat = await client.get_entity(s.strip())
                source_chats.append(chat)
                log.info(f"✅ Добавлен источник: {chat.title}")
            except Exception as e:
                log.error(f"❌ Не удалось добавить {s.strip()}: {e}")
    
    favorites = await client.get_entity(TARGET_FAVORITES)
    log.info(f"🎯 Избранное: {favorites.id}")
    
    channel = None
    if TARGET_CHANNEL:
        try:
            channel = await client.get_entity(TARGET_CHANNEL)
            log.info(f"🎯 Канал для публикации: {channel.title}")
        except Exception as e:
            log.error(f"❌ Не удалось получить канал: {e}")
    
    @client.on(events.NewMessage(chats=source_chats))
    async def handler(event):
        try:
            log.info(f"📥 Новое сообщение из {event.chat.title}")
            original_text = event.message.text or ""
            
            # Сохраняем в Избранное
            await client.send_message(favorites, original_text, file=event.message.media, link_preview=False)
            log.info("   ✅ Сохранено в Избранное")
            
            # Если есть канал — публикуем
            if channel:
                await client.send_message(channel, original_text, file=event.message.media, link_preview=False)
                log.info("   ✅ Опубликовано в канал")
            
        except Exception as e:
            log.error(f"Ошибка: {e}")
    
    log.info("🚀 Бот запущен и готов к работе!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
