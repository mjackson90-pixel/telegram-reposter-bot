import asyncio
from telethon import TelegramClient
import qrcode
from dotenv import load_dotenv
import os
import getpass

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = "main_user"

async def main():
    print("🔄 Создаю клиент...")
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    
    # Проверяем, не авторизованы ли уже
    if await client.is_user_authorized():
        print("✅ Уже авторизован!")
        me = await client.get_me()
        print(f"Аккаунт: {me.first_name} (@{me.username})")
        await client.disconnect()
        return
    
    print("📱 Запрашиваю QR-код для входа...")
    
    try:
        # Пытаемся войти через QR
        qr_login = await client.qr_login()
        
        print("\n" + "="*50)
        print("⚠️ ВАЖНО: ЗАКРОЙТЕ ВСЕ ДРУГИЕ СЕССИИ TELEGRAM НА КОМПЬЮТЕРЕ!")
        print("="*50 + "\n")
        
        print("⏳ Сканируйте QR-код в течение 30 секунд:")
        print("📲 Telegram на телефоне -> Настройки -> Устройства -> Сканировать QR-код")
        print("\n" + "="*50)
        
        # Показываем QR
        qr = qrcode.QRCode(box_size=1, border=1)
        qr.add_data(qr_login.url)
        qr.print_ascii()
        print("="*50 + "\n")
        
        # Сохраняем QR как картинку
        img = qrcode.make(qr_login.url)
        img.save("telegram_qr.png")
        print(f"📸 QR сохранен: telegram_qr.png")
        
        # Ждем сканирования
        await qr_login.wait(30)
        
    except asyncio.TimeoutError:
        print("❌ Время вышло. Запустите заново.")
        await client.disconnect()
        return
        
    except Exception as e:
        # Если требуется пароль (2FA)
        if "SessionPasswordNeededError" in str(e):
            print("\n🔐 Требуется двухфакторная аутентификация")
            password = getpass.getpass("Введите ваш облачный пароль: ")
            
            try:
                await client.sign_in(password=password)
                print("✅ Пароль принят!")
            except Exception as e:
                print(f"❌ Ошибка: {e}")
                await client.disconnect()
                return
        else:
            print(f"❌ Ошибка: {e}")
            await client.disconnect()
            return
    
    # Проверяем результат
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"\n🎉 УСПЕХ! Аккаунт: {me.first_name}")
        print(f"📁 Файл сессии: {SESSION}.session")
    else:
        print("❌ Авторизация не завершена")
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())