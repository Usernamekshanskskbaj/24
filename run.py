import subprocess
import sys
import os
import logging
from datetime import datetime
import json
import base64
import asyncio
import signal

log_filename = 'run_log.log'

# Проверяем, нужно ли создавать новый файл или дописывать к существующему
file_mode = 'w' if not os.path.exists(log_filename) else 'a'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_filename, mode=file_mode, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Правильное применение nest_asyncio только когда необходимо
try:
    import nest_asyncio
    # Проверяем среду выполнения
    try:
        # Получаем текущий event loop только для проверки
        current_loop = asyncio.get_running_loop()
        # Проверяем, действительно ли мы в Jupyter/Colab
        import sys
        if any(name in sys.modules for name in ['IPython', 'google.colab']):
            nest_asyncio.apply()
            logger.info("nest_asyncio применен для Jupyter/Colab среды")
        else:
            # В обычной среде с запущенным event loop не применяем nest_asyncio
            logger.info("Event loop обнаружен, но среда не требует nest_asyncio")
    except RuntimeError:
        # Нет активного event loop - это нормально для обычного запуска
        logger.info("Запуск в стандартной среде без активного event loop")
        pass
except ImportError:
    # nest_asyncio не установлен, продолжаем без него
    logger.info("nest_asyncio не установлен, продолжаем без него")
    pass

# Настройка логирования для внешних библиотек
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('selenium').setLevel(logging.WARNING)
logging.getLogger('seleniumbase').setLevel(logging.WARNING)

def check_and_install_packages():
    """Проверка и установка необходимых библиотек"""
    required_packages = [
        'telethon==1.40.0',
        'python-telegram-bot==20.7',
        'g4f==0.5.3.2',
        'seleniumbase==4.39.2',
        'chromium-driver',
        'aiosqlite==0.19.0',
        'nest-asyncio==1.5.8'
    ]
    
    # Получаем список установленных пакетов
    try:
        result = subprocess.run([sys.executable, '-m', 'pip', 'list'], 
                              capture_output=True, text=True, check=True)
        installed_packages = result.stdout.lower()
    except subprocess.CalledProcessError:
        logger.error("Не удалось получить список установленных пакетов")
        installed_packages = ""
    
    # Проверяем и устанавливаем необходимые пакеты
    for package in required_packages:
        package_name = package.split('==')[0].replace('-', '_').replace('_driver', '')
        
        # Специальная проверка для разных вариантов названий
        package_variants = [
            package_name,
            package.split('==')[0],
            package.split('==')[0].replace('-', '_'),
            package.split('==')[0].replace('_', '-')
        ]
        
        found = any(variant in installed_packages for variant in package_variants)
        
        if not found:
            logger.info(f"Установка библиотеки {package}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
                logger.info(f"Библиотека {package} успешно установлена")
            except subprocess.CalledProcessError as e:
                logger.error(f"Ошибка установки {package}: {e}")
                # Для chromium-driver пытаемся альтернативные варианты
                if 'chromium' in package:
                    alternatives = ['chromium', 'chromedriver-binary', 'chromedriver-autoinstaller']
                    for alt in alternatives:
                        try:
                            logger.info(f"Попытка установки альтернативы {alt}...")
                            subprocess.check_call([sys.executable, "-m", "pip", "install", alt])
                            logger.info(f"Альтернатива {alt} успешно установлена")
                            break
                        except:
                            continue
                else:
                    # Пытаемся установить без версии
                    try:
                        base_package = package.split('==')[0]
                        logger.info(f"Попытка установки {base_package} без указания версии...")
                        subprocess.check_call([sys.executable, "-m", "pip", "install", base_package])
                        logger.info(f"Библиотека {base_package} успешно установлена")
                    except subprocess.CalledProcessError:
                        logger.error(f"Не удалось установить {base_package}")
        else:
            logger.info(f"Библиотека {package_name} уже установлена")

def simple_encrypt(text, key="telegram_mass_looker_2024"):
    """Простое шифрование без использования cryptography"""
    if not text:
        return ""
    
    # Преобразуем ключ в числа
    key_nums = [ord(c) for c in key]
    
    # Шифруем текст
    encrypted = []
    for i, char in enumerate(text):
        key_char = key_nums[i % len(key_nums)]
        encrypted_char = chr((ord(char) + key_char) % 256)
        encrypted.append(encrypted_char)
    
    # Кодируем в base64 для безопасного хранения
    encrypted_text = ''.join(encrypted)
    return base64.b64encode(encrypted_text.encode('latin-1')).decode()

def simple_decrypt(encrypted_text, key="telegram_mass_looker_2024"):
    """Простая расшифровка без использования cryptography"""
    if not encrypted_text:
        return ""
    
    try:
        # Декодируем из base64
        encrypted_bytes = base64.b64decode(encrypted_text.encode())
        encrypted = encrypted_bytes.decode('latin-1')
        
        # Преобразуем ключ в числа
        key_nums = [ord(c) for c in key]
        
        # Расшифровываем текст
        decrypted = []
        for i, char in enumerate(encrypted):
            key_char = key_nums[i % len(key_nums)]
            decrypted_char = chr((ord(char) - key_char) % 256)
            decrypted.append(decrypted_char)
        
        return ''.join(decrypted)
    except Exception:
        return ""

def load_config():
    """Загрузка конфигурации"""
    config_file = 'config.json'
    default_config = {
        'bot_token': '',
        'api_id': '',
        'api_hash': '',
        'phone': '',
        'password': '',
        'session_name': 'user_session'
    }
    
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            # Расшифровываем данные
            for key in ['bot_token', 'api_id', 'api_hash', 'phone', 'password']:
                if key in config and config[key]:
                    config[key] = simple_decrypt(config[key])
            return config
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
            return default_config
    return default_config

def save_config(config):
    """Сохранение конфигурации"""
    config_file = 'config.json'
    # Шифруем данные перед сохранением
    encrypted_config = config.copy()
    for key in ['bot_token', 'api_id', 'api_hash', 'phone', 'password']:
        if key in encrypted_config and encrypted_config[key]:
            encrypted_config[key] = simple_encrypt(encrypted_config[key])
    
    try:
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(encrypted_config, f, indent=2)
        logger.info("Конфигурация сохранена")
    except Exception as e:
        logger.error(f"Ошибка сохранения конфигурации: {e}")

def get_bot_token():
    """Получение токена бота"""
    config = load_config()
    
    if not config.get('bot_token'):
        print("\n" + "="*50)
        print("НАСТРОЙКА ТОКЕНА БОТА")
        print("="*50)
        print("Токен бота не найден в конфигурации.")
        print("Для получения токена:")
        print("1. Напишите @BotFather в Telegram")
        print("2. Отправьте команду /newbot")
        print("3. Следуйте инструкциям")
        print("4. Скопируйте полученный токен")
        print("="*50)
        
        while True:
            try:
                token = input("Введите токен бота: ").strip()
                if token and ':' in token and len(token) > 20:
                    config['bot_token'] = token
                    save_config(config)
                    logger.info("Токен бота сохранен")
                    print("✅ Токен успешно сохранен!")
                    break
                else:
                    print("❌ Неверный формат токена. Токен должен содержать ':' и быть длиннее 20 символов.")
                    print("Пример: 123456789:ABCdefGHIjklMNOpqrSTUvwxyz-1234567890")
            except KeyboardInterrupt:
                print("\n❌ Отменено пользователем")
                sys.exit(1)
            except Exception as e:
                print(f"❌ Ошибка ввода: {e}")
                continue
    else:
        logger.info("Токен бота найден в конфигурации")
    
    return config['bot_token']

async def initialize_telethon_client():
    """Инициализация Telethon клиента управляется bot_interface"""
    logger.info("Инициализация Telethon клиента выполняется в bot_interface")
    return False

async def cleanup_resources():
    """Очистка ресурсов"""
    logger.info("Очистка ресурсов...")
    
    # Закрываем только базу данных
    try:
        from database import close_database
        await close_database()
    except Exception as e:
        logger.error(f"Ошибка закрытия базы данных: {e}")

async def main_async():
    """Асинхронная основная функция с исправлением проблемы event loop"""
    
    # Флаг для обработки завершения
    shutdown_event = asyncio.Event()
    
    def signal_handler(signum, frame):
        logger.info("Получен сигнал завершения")
        shutdown_event.set()
    
    # Устанавливаем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        logger.info("Запуск в единственном event loop")
        
        print("📱 Бот запущен! Найдите его в Telegram и отправьте /start")
        print("⏹️  Для остановки нажмите Ctrl+C")
        print("="*50)
        
        # Импортируем bot_interface
        sys.path.append(os.path.dirname(__file__))
        import bot_interface
        
        # Получаем токен бота
        config = load_config()
        bot_token = config['bot_token']
        
        # ИСПРАВЛЕНИЕ: гарантируем что используем единственный event loop
        current_loop = asyncio.get_event_loop()
        logger.info(f"Используем event loop: {id(current_loop)}")
        
        # Создаем задачу бота в текущем event loop
        bot_task = current_loop.create_task(bot_interface.run_bot(bot_token))
        
        # Ждем сигнал завершения или завершения бота
        done, pending = await asyncio.wait(
            [current_loop.create_task(shutdown_event.wait()), bot_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Отменяем незавершенные задачи
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        logger.info("Основная функция завершена")
        
    except Exception as e:
        logger.error(f"Ошибка в main_async: {e}")
        raise
    finally:
        # Очищаем ресурсы
        await cleanup_resources()

def main():
    """Основная функция запуска"""
    print("🤖 Система нейрокомментинга и массреакшена")
    print("="*50)
    
    logger.info("Запуск системы нейрокомментирования...")
    
    try:
        # Проверка и установка библиотек
        logger.info("Проверка и установка библиотек...")
        check_and_install_packages()
        
        # Получение токена бота
        logger.info("Проверка токена бота...")
        bot_token = get_bot_token()
        
        print("\n✅ Инициализация завершена!")
        print("🚀 Запуск бота...")
        
        # Запускаем асинхронную часть
        asyncio.run(main_async())
        
    except KeyboardInterrupt:
        print("\n⏹️  Остановка системы пользователем")
        logger.info("Остановка системы пользователем")
    except Exception as e:
        print(f"\n❌ Критическая ошибка: {e}")
        logger.error(f"Критическая ошибка: {e}")
        print("Проверьте логи для получения подробной информации")
        sys.exit(1)

if __name__ == "__main__":
    main()