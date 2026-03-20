import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 30756871
API_HASH = "e21a1956b57657cb4ac463f08e80d7e9"

async def main():
    # Создаём клиент с StringSession (пустой)
    string_session = StringSession()
    client = TelegramClient(string_session, API_ID, API_HASH)
    
    print("🔐 Авторизация...")
    await client.start()
    
    # Получаем строку сессии
    session_string = client.session.save()
    print("\n" + "="*50)
    print("ВАША STRING SESSION:")
    print(session_string)
    print("="*50 + "\n")
    print("Сохраните эту строку! Она нужна для настройки на сервере.")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())