import asyncio
import logging
import os
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from dotenv import load_dotenv

load_dotenv()

# --- Основные настройки ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = "collector"
SOURCES = os.getenv("COLLECTOR_SOURCES", "").split(",")
TARGET_FAVORITES = "me"

# --- Настройка логирования ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

async def get_entity_smart(client, entity_input):
    """Получение сущности канала по ссылке или ID"""
    entity_input = entity_input.strip()
    
    # Если это ссылка
    if entity_input.startswith(('t.me/', 'https://t.me/')):
        return await client.get_entity(entity_input)
    
    if entity_input.startswith('@'):
        return await client.get_entity(entity_input)
    
    # Если это ID
    if entity_input.lstrip('-').isdigit():
        try:
            return await client.get_entity(int(entity_input))
        except:
            pass
    
    # Пробуем как есть
    return await client.get_entity(entity_input)

async def main():
    log.info("🚀 Запуск бота-сборщика (режим копирования)")
    
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    
    # --- Загрузка каналов-источников ---
    source_chats = []
    log.info("📡 Подключение к каналам-источникам:")
    
    for s in SOURCES:
        if s.strip():
            try:
                chat = await get_entity_smart(client, s.strip())
                source_chats.append(chat)
                log.info(f"  ✅ {chat.title} (ID: {chat.id})")
            except Exception as e:
                log.error(f"  ❌ Не удалось подключиться к {s}: {e}")
    
    if not source_chats:
        log.error("❌ Нет доступных каналов-источников. Завершение работы.")
        return
    
    # --- Целевой чат (Избранное) ---
    favorites = await client.get_entity(TARGET_FAVORITES)
    log.info(f"🎯 Целевой чат: Избранное (ID: {favorites.id})")
    log.info(f"📊 Отслеживается каналов: {len(source_chats)}")
    
    @client.on(events.NewMessage(chats=source_chats))
    async def handler(event):
        try:
            chat_title = event.chat.title if event.chat else "unknown"
            log.info(f"📥 Новое сообщение из {chat_title}")
            
            # КОПИРУЕМ, а не пересылаем
            await client.send_message(
                favorites,
                event.message.text or "",
                file=event.message.media,
                formatting_entities=event.message.entities,
                link_preview=False
            )
            
            log.info(f"  ✅ Скопировано в Избранное (ID: {event.message.id})")
            
        except FloodWaitError as e:
            log.warning(f"⏳ Flood wait {e.seconds}с")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            log.error(f"❌ Ошибка при копировании: {e}")
    
    log.info("✅ Бот-сборщик запущен и ожидает новые сообщения")
    log.info("⏸️  Нажмите Ctrl+C для остановки")
    
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("👋 Бот-сборщик остановлен")
    except Exception as e:
        log.error(f"💥 Критическая ошибка: {e}")