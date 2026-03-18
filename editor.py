import asyncio
import logging
import os
import hashlib
import re
import pickle
import json
import struct
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from dotenv import load_dotenv
import tempfile
from pathlib import Path
import imagehash
from PIL import Image
import numpy as np
import cv2
from Levenshtein import distance as levenshtein_distance

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION = "editor"
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "")

# --- Файлы состояния ---
PROCESSED_FILE = "editor_processed.pkl"
MEDIA_HASHES_FILE = "media_hashes.json"
TEXT_HASHES_FILE = "text_hashes.json"

# --- Настройки ---
SCAN_INTERVAL = 300  # Сканировать Избранное каждые 5 минут
MAX_POSTS_PER_SCAN = 50  # Максимум постов за раз
SIMILARITY_THRESHOLD = 85  # Порог похожести текста в процентах
PHASH_THRESHOLD = 10  # Порог похожести изображений (меньше = строже)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

class DuplicateDetector:
    """Абсолютная система проверки дубликатов"""
    
    def __init__(self):
        self.text_hashes = {}  # text_hash -> (count, last_seen)
        self.media_hashes = {}  # media_hash -> (message_id, timestamp)
        self.media_phash = {}   # perceptual hash -> message_id
        self.video_fingerprints = {}  # video_fingerprint -> message_id
        self.load()
    
    def load(self):
        """Загружает все базы из файлов"""
        # Загружаем текстовые хэши
        if os.path.exists(TEXT_HASHES_FILE):
            try:
                with open(TEXT_HASHES_FILE, 'r', encoding='utf-8') as f:
                    self.text_hashes = json.load(f)
                log.info(f"📚 Загружено {len(self.text_hashes)} текстовых хэшей")
            except Exception as e:
                log.error(f"Ошибка загрузки текстовых хэшей: {e}")
        
        # Загружаем медиа-хэши
        if os.path.exists(MEDIA_HASHES_FILE):
            try:
                with open(MEDIA_HASHES_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.media_hashes = data.get('hashes', {})
                    self.media_phash = data.get('phash', {})
                    self.video_fingerprints = data.get('video', {})
                log.info(f"🖼️ Загружено {len(self.media_hashes)} медиа-хэшей")
            except Exception as e:
                log.error(f"Ошибка загрузки медиа-хэшей: {e}")
    
    def save(self):
        """Сохраняет все базы в файлы"""
        # Сохраняем текстовые хэши
        try:
            with open(TEXT_HASHES_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.text_hashes, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Ошибка сохранения текстовых хэшей: {e}")
        
        # Сохраняем медиа-хэши
        try:
            with open(MEDIA_HASHES_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'hashes': self.media_hashes,
                    'phash': self.media_phash,
                    'video': self.video_fingerprints
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Ошибка сохранения медиа-хэшей: {e}")
    
    def calculate_text_hash(self, text):
        """Вычисляет хэш текста"""
        if not text:
            return None
        # Нормализуем текст: нижний регистр, убираем лишние пробелы
        normalized = ' '.join(text.lower().split())
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    
    def calculate_text_similarity(self, text1, text2):
        """Вычисляет процент похожести двух текстов"""
        if not text1 or not text2:
            return 0
        
        # Нормализуем
        t1 = ' '.join(text1.lower().split())
        t2 = ' '.join(text2.lower().split())
        
        # Если тексты одинаковые
        if t1 == t2:
            return 100
        
        # Вычисляем расстояние Левенштейна
        distance = levenshtein_distance(t1, t2)
        max_len = max(len(t1), len(t2))
        if max_len == 0:
            return 0
        
        similarity = (1 - distance / max_len) * 100
        return similarity
    
    def is_text_duplicate(self, text):
        """Проверяет, есть ли похожий текст в базе"""
        if not text:
            return False
        
        current_hash = self.calculate_text_hash(text)
        
        # Проверяем точное совпадение
        if current_hash in self.text_hashes:
            log.info(f"   📝 Найдено точное совпадение текста")
            return True
        
        # Проверяем похожие тексты
        for existing_hash, data in self.text_hashes.items():
            # Если у нас есть оригинальный текст в данных
            if 'sample' in data:
                similarity = self.calculate_text_similarity(text, data['sample'])
                if similarity >= SIMILARITY_THRESHOLD:
                    log.info(f"   📝 Найден похожий текст (совпадение: {similarity:.1f}%)")
                    return True
        
        return False
    
    def add_text(self, text, message_id):
        """Добавляет текст в базу"""
        if not text:
            return
        
        text_hash = self.calculate_text_hash(text)
        # Сохраняем хэш и небольшой сэмпл текста для проверки похожести
        sample = text[:200]  # Первые 200 символов
        self.text_hashes[text_hash] = {
            'count': self.text_hashes.get(text_hash, {}).get('count', 0) + 1,
            'last_seen': datetime.now().isoformat(),
            'sample': sample,
            'message_id': message_id
        }
    
    def calculate_image_phash(self, image_path):
        """Вычисляет perceptual hash изображения (устойчив к изменениям)"""
        try:
            from PIL import Image
            import imagehash
            
            img = Image.open(image_path)
            # Уменьшаем размер для ускорения
            img = img.resize((64, 64))
            # Вычисляем perceptual hash
            phash = str(imagehash.phash(img))
            return phash
        except Exception as e:
            log.error(f"Ошибка вычисления phash: {e}")
            return None
    
    def calculate_media_hash(self, file_path):
        """Вычисляет точный хэш файла"""
        try:
            with open(file_path, 'rb') as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception as e:
            log.error(f"Ошибка вычисления хэша: {e}")
            return None
    
    def calculate_video_fingerprint(self, video_path):
        """Вычисляет отпечаток видео (ключевые кадры + длительность)"""
        try:
            import cv2
            import numpy as np
            
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                return None
            
            # Получаем информацию о видео
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            duration = total_frames / fps if fps > 0 else 0
            
            # Извлекаем ключевые кадры (начало, середина, конец)
            key_frames = []
            positions = [0, total_frames//3, 2*total_frames//3, total_frames-1]
            
            for pos in positions:
                if pos >= 0 and pos < total_frames:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
                    ret, frame = cap.read()
                    if ret:
                        # Конвертируем в оттенки серого и уменьшаем
                        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                        small = cv2.resize(gray, (32, 32))
                        key_frames.append(small)
            
            cap.release()
            
            if not key_frames:
                return None
            
            # Создаем хэш из ключевых кадров
            combined = np.concatenate([f.flatten() for f in key_frames])
            video_hash = hashlib.sha256(combined.tobytes()).hexdigest()
            
            return {
                'hash': video_hash,
                'duration': round(duration, 2),
                'fps': round(fps, 2),
                'frames': len(key_frames)
            }
        except Exception as e:
            log.error(f"Ошибка анализа видео: {e}")
            return None
    
    def is_media_duplicate(self, file_path):
        """Проверяет, есть ли такое медиа в базе"""
        if not os.path.exists(file_path):
            return False
        
        # Точный хэш файла
        file_hash = self.calculate_media_hash(file_path)
        if file_hash and file_hash in self.media_hashes:
            log.info(f"   🔍 Найдено точное совпадение медиа")
            return True
        
        # Для изображений проверяем perceptual hash
        if file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
            phash = self.calculate_image_phash(file_path)
            if phash:
                # Проверяем похожие изображения
                for existing_phash, msg_id in self.media_phash.items():
                    # Вычисляем расстояние Хэмминга между perceptual hashes
                    if phash and existing_phash:
                        # Конвертируем в биты для сравнения
                        h1 = imagehash.hex_to_hash(phash)
                        h2 = imagehash.hex_to_hash(existing_phash)
                        distance = h1 - h2
                        
                        if distance <= PHASH_THRESHOLD:
                            log.info(f"   🔍 Найдено похожее изображение (расстояние: {distance})")
                            return True
        
        # Для видео проверяем отпечаток
        if file_path.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')):
            video_fp = self.calculate_video_fingerprint(file_path)
            if video_fp and video_fp['hash'] in self.video_fingerprints:
                log.info(f"   🔍 Найдено похожее видео")
                return True
        
        return False
    
    def add_media(self, file_path, message_id):
        """Добавляет медиа в базу"""
        if not os.path.exists(file_path):
            return
        
        file_hash = self.calculate_media_hash(file_path)
        if file_hash:
            self.media_hashes[file_hash] = {
                'message_id': message_id,
                'timestamp': datetime.now().isoformat()
            }
        
        # Для изображений добавляем perceptual hash
        if file_path.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
            phash = self.calculate_image_phash(file_path)
            if phash:
                self.media_phash[phash] = message_id
        
        # Для видео добавляем отпечаток
        if file_path.lower().endswith(('.mp4', '.avi', '.mov', '.mkv')):
            video_fp = self.calculate_video_fingerprint(file_path)
            if video_fp:
                self.video_fingerprints[video_fp['hash']] = {
                    'message_id': message_id,
                    'duration': video_fp['duration'],
                    'timestamp': datetime.now().isoformat()
                }

async def process_favorites(client, channel, detector):
    """Обрабатывает новые сообщения из Избранного с абсолютной проверкой дубликатов"""
    try:
        favorites = await client.get_entity("me")
        new_posts = []
        
        # Получаем время последнего сканирования из файла
        last_scan_file = "editor_last_scan.txt"
        last_scan = datetime.now(timezone.utc) - timedelta(hours=24)
        
        if os.path.exists(last_scan_file):
            try:
                with open(last_scan_file, 'r') as f:
                    timestamp = f.read().strip()
                    if timestamp:
                        last_scan = datetime.fromisoformat(timestamp)
                        if last_scan.tzinfo is None:
                            last_scan = last_scan.replace(tzinfo=timezone.utc)
            except Exception as e:
                log.error(f"Ошибка загрузки времени: {e}")
        
        log.info(f"🔍 Сканирую Избранное... (с {last_scan.strftime('%Y-%m-%d %H:%M:%S')})")
        
        # Собираем новые посты
        async for message in client.iter_messages(favorites, limit=MAX_POSTS_PER_SCAN):
            msg_time = message.date
            if msg_time.tzinfo is None:
                msg_time = msg_time.replace(tzinfo=timezone.utc)
            
            if msg_time > last_scan:
                new_posts.append(message)
        
        if not new_posts:
            log.info("⏭️ Нет новых постов в Избранном")
            return 0
        
        log.info(f"📨 Найдено {len(new_posts)} новых постов в Избранном")
        
        published = 0
        skipped = 0
        
        for message in reversed(new_posts):  # От старых к новым
            temp_file = None
            try:
                # Получаем текст
                text = message.text or ""
                
                # Проверка текста на дубликаты
                if text:
                    if detector.is_text_duplicate(text):
                        log.info(f"⏭️ Пост {message.id} - дубликат текста")
                        skipped += 1
                        continue
                
                # Проверка медиа на дубликаты
                is_duplicate = False
                media_hash = None
                
                if message.media:
                    # Создаем временный файл
                    temp_file_obj = tempfile.NamedTemporaryFile(delete=False)
                    temp_file = temp_file_obj.name
                    temp_file_obj.close()
                    
                    # Скачиваем медиа
                    await client.download_media(message, temp_file)
                    
                    # Проверяем на дубликаты
                    if detector.is_media_duplicate(temp_file):
                        log.info(f"⏭️ Пост {message.id} - дубликат медиа")
                        is_duplicate = True
                
                if is_duplicate:
                    skipped += 1
                    continue
                
                # Публикуем в канал
                if message.media:
                    await client.send_file(channel, message.media, caption=text or None)
                    log.info(f"✅ Опубликован пост {message.id} (с медиа)")
                else:
                    if text:
                        await client.send_message(channel, text)
                        log.info(f"✅ Опубликован пост {message.id} (текст)")
                    else:
                        log.info(f"⏭️ Пост {message.id} - пустой")
                        skipped += 1
                        continue
                
                # Сохраняем в базу
                if text:
                    detector.add_text(text, message.id)
                
                if temp_file and os.path.exists(temp_file):
                    detector.add_media(temp_file, message.id)
                
                published += 1
                
                # Небольшая пауза между публикациями
                await asyncio.sleep(1)
                
            except Exception as e:
                log.error(f"Ошибка при обработке поста {message.id}: {e}")
                skipped += 1
            finally:
                # Удаляем временный файл
                if temp_file and os.path.exists(temp_file):
                    try:
                        await asyncio.sleep(0.1)
                        os.unlink(temp_file)
                    except Exception as e:
                        log.error(f"Не удалось удалить файл {temp_file}: {e}")
        
        # Сохраняем базы
        detector.save()
        
        # Обновляем время последнего сканирования
        with open(last_scan_file, 'w') as f:
            f.write(datetime.now(timezone.utc).isoformat())
        
        log.info(f"📊 Результаты: опубликовано {published}, пропущено {skipped}")
        return published
        
    except Exception as e:
        log.error(f"Ошибка сканирования: {e}")
        return 0

async def main():
    # Инициализируем детектор дубликатов
    detector = DuplicateDetector()
    
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    
    # Получаем целевой канал
    channel = None
    if TARGET_CHANNEL:
        try:
            channel = await client.get_entity(TARGET_CHANNEL)
            log.info(f"🎯 Канал для публикации: {channel.title}")
        except Exception as e:
            log.error(f"❌ Не удалось получить канал: {e}")
            return
    
    log.info("🚀 Бот-редактор запущен с абсолютной проверкой дубликатов")
    log.info(f"⏱️  Интервал сканирования: {SCAN_INTERVAL} сек")
    log.info(f"📊 В базах: {len(detector.text_hashes)} текстов, {len(detector.media_hashes)} медиа")
    
    # Первое сканирование
    await process_favorites(client, channel, detector)
    
    # Затем по расписанию
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL)
            await process_favorites(client, channel, detector)
        except Exception as e:
            log.error(f"Ошибка в основном цикле: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())