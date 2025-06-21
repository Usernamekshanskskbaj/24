import asyncio
import logging
import time
import random
import threading
import os
from datetime import datetime
from typing import List, Tuple, Set, Optional
import re

logger = logging.getLogger(__name__)

try:
    from seleniumbase import Driver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.keys import Keys
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from telethon import TelegramClient
    from telethon.errors import ChannelPrivateError, ChannelInvalidError, FloodWaitError
    from telethon.tl.functions.channels import GetFullChannelRequest
    import g4f
except ImportError as e:
    logger.error(f"Ошибка импорта библиотек: {e}")
    raise

# Глобальные переменные
search_active = False
found_channels: Set[str] = set()
driver = None
current_settings = {}
shared_telethon_client = None
search_progress = {'current_keyword': '', 'current_topic': ''}
sent_to_queue_count = 0  # ИСПРАВЛЕНИЕ: переименовываем для ясности - это каналы отправленные в очередь

# Переменные для контроля flood wait
last_api_call_time = 0
api_call_interval = 1.0
flood_wait_delays = {}

async def handle_flood_wait(func, *args, **kwargs):
    """Универсальная обертка для обработки FloodWaitError"""
    max_retries = 3
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            global last_api_call_time, api_call_interval
            current_time = time.time()
            time_since_last_call = current_time - last_api_call_time
            
            if time_since_last_call < api_call_interval:
                wait_time = api_call_interval - time_since_last_call
                await asyncio.sleep(wait_time)
            
            last_api_call_time = time.time()
            
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            
            return result
            
        except FloodWaitError as e:
            retry_count += 1
            wait_seconds = e.seconds
            
            logger.warning(f"FloodWaitError: необходимо подождать {wait_seconds} секунд. Попытка {retry_count}/{max_retries}")
            
            api_call_interval = min(api_call_interval * 1.5, 10.0)
           
            if retry_count >= max_retries:
                logger.error(f"Достигнуто максимальное количество попыток для обработки FloodWait. Последнее ожидание: {wait_seconds}")
                raise
            
            try:
                import bot_interface
                if not bot_interface.bot_data.get('is_running', True):
                    logger.info("Остановка запрошена во время FloodWait, прерываем ожидание")
                    raise asyncio.CancelledError("Остановка запрошена")
            except ImportError:
                pass
            
            remaining_wait = wait_seconds
            while remaining_wait > 0:
                check_interval = min(10, remaining_wait)
                await asyncio.sleep(check_interval)
                remaining_wait -= check_interval
                
                try:
                    import bot_interface
                    if not bot_interface.bot_data.get('is_running', True):
                        logger.info("Остановка запрошена во время FloodWait, прерываем ожидание")
                        raise asyncio.CancelledError("Остановка запрошена")
                except ImportError:
                    pass
                
                if remaining_wait > 0:
                    logger.info(f"FloodWait: осталось ждать {remaining_wait} секунд")
            
            logger.info(f"Ожидание FloodWait завершено, повторяем попытку {retry_count + 1}")
            
        except Exception as e:
            raise e
    
    raise Exception(f"Не удалось выполнить операцию после {max_retries} попыток")

def setup_driver_threaded():
    """Настройка и инициализация веб-драйвера в отдельном потоке"""
    logger.info("Инициализация веб-драйвера в отдельном потоке...")
    
    try:
        driver = Driver(uc=True, headless=False)
        driver.set_window_size(600, 1200)
        
        desktop_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        driver.execute_cdp_cmd('Network.setUserAgentOverride', {"userAgent": desktop_user_agent})
        
        driver.get("about:blank")
        logger.info("Веб-драйвер успешно инициализирован в отдельном потоке")
        
        return driver
    except Exception as e:
        logger.error(f"Ошибка инициализации драйвера в потоке: {e}")
        return None

def wait_and_find_element(driver, selectors, timeout=5):
    """Поиск элемента по нескольким селекторам"""
    if isinstance(selectors, str):
        selectors = [selectors]
    
    for selector in selectors:
        try:
            if selector.startswith('//'):
                element = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
            else:
                element = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
            return element
        except TimeoutException:
            continue
        except Exception as e:
            logger.warning(f"Ошибка поиска элемента {selector}: {e}")
            continue
    
    return None

def wait_and_click_element(driver, selectors, timeout=5):
    """Клик по элементу с ожиданием его доступности"""
    element = wait_and_find_element(driver, selectors, timeout)
    if element:
        try:
            if isinstance(selectors, str):
                selectors = [selectors]
            
            for selector in selectors:
                try:
                    if selector.startswith('//'):
                        clickable_element = driver.find_element(By.XPATH, selector)
                    else:
                        clickable_element = driver.find_element(By.CSS_SELECTOR, selector)
                    
                    if clickable_element.is_displayed() and clickable_element.is_enabled():
                        clickable_element.click()
                        return True
                    else:
                        time.sleep(0.1)
                        if clickable_element.is_displayed() and clickable_element.is_enabled():
                            clickable_element.click()
                            return True
                except:
                    continue
        except Exception as e:
            logger.warning(f"Ошибка клика по элементу: {e}")
    
    return False

def navigate_to_channel_search(driver):
    """Навигация к странице поиска каналов"""
    try:
        logger.info("Переход на tgstat.ru...")
        driver.get("https://tgstat.ru/")
        time.sleep(1)
        
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем навигацию")
                return False
        except:
            pass
        
        logger.info("Открываем меню...")
        menu_selectors = [
            'a.d-flex.d-lg-none.nav-user',
            '.nav-user',
            '[data-toggle="collapse"]',
            'i.uil-bars'
        ]
        
        if not wait_and_click_element(driver, menu_selectors, 3):
            logger.warning("Не удалось найти кнопку меню, возможно меню уже открыто")
        
        time.sleep(0.5)
        
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем навигацию")
                return False
        except:
            pass
        
        logger.info("Открываем каталог...")
        catalog_selectors = [
            '#topnav-catalog',
            'a[id="topnav-catalog"]',
            '.nav-link.dropdown-toggle',
            '//a[contains(text(), "Каталог")]'
        ]
        
        if not wait_and_click_element(driver, catalog_selectors, 3):
            logger.error("Не удалось найти кнопку каталога")
            return False
        
        time.sleep(0.3)
        
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем навигацию")
                return False
        except:
            pass
        
        logger.info("Переходим к поиску каналов...")
        search_selectors = [
            'a[href="/channels/search"]',
            '//a[contains(text(), "Поиск каналов")]',
            '.dropdown-item[href="/channels/search"]'
        ]
        
        if not wait_and_click_element(driver, search_selectors, 3):
            logger.error("Не удалось найти кнопку поиска каналов")
            return False
        
        time.sleep(1)
        logger.info("Успешно перешли на страницу поиска каналов")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка навигации: {e}")
        return False

def search_channels_sync(driver, keyword: str, topic: str, first_search: bool = False):
    """Функция поиска каналов"""
    try:
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем поиск")
                return []
        except:
            pass
        
        logger.info(f"Поиск каналов по ключевому слову: '{keyword}', тема: '{topic}'")
        
        keyword_input = wait_and_find_element(driver, [
            'input[name="q"]',
            '#q',
            '.form-control',
            'input[placeholder*="канал"]',
            'input.form-control'
        ])
        
        if keyword_input:
            keyword_input.clear()
            time.sleep(0.1)
            keyword_input.send_keys(keyword)
            logger.info(f"Введено ключевое слово: {keyword}")
        else:
            logger.error("Не удалось найти поле ввода ключевого слова")
            return []
        
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем поиск")
                return []
        except:
            pass
        
        topic_input = wait_and_find_element(driver, [
            '.select2-search__field',
            'input[role="searchbox"]',
            '.select2-search input'
        ])
        
        if topic_input:
            topic_input.clear()
            time.sleep(0.1)
            topic_input.send_keys(topic)
            time.sleep(0.3)
            topic_input.send_keys(Keys.ENTER)
            logger.info(f"Введена тема: {topic}")
        else:
            logger.error("Не удалось найти поле ввода темы")
            return []
        
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем поиск")
                return []
        except:
            pass
        
        if first_search:
            description_checkbox = wait_and_find_element(driver, [
                '#inabout',
                'input[name="inAbout"]',
                '.custom-control-input[name="inAbout"]'
            ])
            
            if description_checkbox and not description_checkbox.is_selected():
                driver.execute_script("arguments[0].click();", description_checkbox)
                logger.info("Отмечен поиск в описании")
            
            channel_type_select = wait_and_find_element(driver, [
                '#channeltype',
                'select[name="channelType"]',
                '.custom-select[name="channelType"]'
            ])
            
            if channel_type_select:
                driver.execute_script("arguments[0].value = 'public';", channel_type_select)
                logger.info("Выбран тип канала: публичный")
        
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем поиск")
                return []
        except:
            pass
        
        search_button = wait_and_find_element(driver, [
            '#search-form-submit-btn',
            'button[type="button"].btn-primary',
            '.btn.btn-primary.w-100'
        ])
        
        if search_button:
            driver.execute_script("arguments[0].click();", search_button)
            logger.info("Нажата кнопка поиска")
        else:
            logger.error("Не удалось найти кнопку поиска")
            return []
       
        wait_time = 0
        max_wait = 10
        while wait_time < max_wait:
            try:
                import bot_interface
                if not bot_interface.bot_data['is_running']:
                    logger.info("Остановка запрошена, прерываем ожидание результатов")
                    return []
            except:
                pass
            
            time.sleep(1)
            wait_time += 1
            
            try:
                results = driver.find_elements(By.CSS_SELECTOR, '.card.peer-item-row, .peer-item-row')
                if results:
                    break
            except:
                continue
        
        channels = extract_channel_usernames_sync(driver)
        logger.info(f"Найдено каналов: {len(channels)}")
        
        return channels
        
    except Exception as e:
        logger.error(f"Ошибка поиска каналов: {e}")
        return []

def extract_channel_usernames_sync(driver) -> List[str]:
    """Функция извлечения юзернеймов каналов"""
    usernames = []
    
    try:
        try:
            import bot_interface
            if not bot_interface.bot_data['is_running']:
                logger.info("Остановка запрошена, прерываем извлечение каналов")
                return usernames
        except:
            pass
        
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.card.peer-item-row, .peer-item-row'))
        )
        
        channel_cards = driver.find_elements(By.CSS_SELECTOR, '.card.peer-item-row, .peer-item-row')
        
        if not channel_cards:
            logger.warning("Не найдено карточек каналов")
            return usernames
        
        for card in channel_cards:
            try:
                try:
                    import bot_interface
                    if not bot_interface.bot_data['is_running']:
                        logger.info("Остановка запрошена, прерываем обработку каналов")
                        break
                except:
                    pass
                
                link_elements = card.find_elements(By.CSS_SELECTOR, 'a[href*="/channel/@"]')
                
                for link in link_elements:
                    href = link.get_attribute('href')
                    if href and '/channel/@' in href:
                        match = re.search(r'/channel/(@[^/]+)', href)
                        if match:
                            username = match.group(1)
                            if username not in usernames:
                                usernames.append(username)
                                logger.info(f"Найден канал: {username}")
                                break
                
            except Exception as e:
                logger.warning(f"Ошибка обработки карточки канала: {e}")
                continue
        
        return usernames
        
    except TimeoutException:
        logger.warning("Результаты поиска не загрузились за отведенное время")
        return usernames
    except Exception as e:
        logger.error(f"Ошибка извлечения юзернеймов: {e}")
        return usernames

# ИСПРАВЛЕНИЕ: Функция для проверки наличия текстовых постов в канале
async def has_textual_posts(client: TelegramClient, username: str) -> bool:
    """
    Проверяет наличие текстовых постов в канале (включая посты с медиа и текстом)
    """
    try:
        if not username.startswith('@'):
            username = '@' + username
        
        async def get_entity_safe():
            return await client.get_entity(username)
        
        entity = await handle_flood_wait(get_entity_safe)
        
        # Проверяем первые 20 сообщений на наличие текста
        text_posts_found = 0
        total_checked = 0
        
        async for message in client.iter_messages(entity, limit=20):
            total_checked += 1
            
            # Проверяем наличие текста в сообщении
            has_text = False
            
            if hasattr(message, 'message') and message.message:
                text = message.message.strip()
                if text:
                    has_text = True
            elif hasattr(message, 'text') and message.text:
                text = message.text.strip()
                if text:
                    has_text = True
            
            # Если есть медиа, проверяем подпись
            if not has_text and hasattr(message, 'media') and message.media:
                if hasattr(message, 'message') and message.message:
                    caption = message.message.strip()
                    if caption:
                        has_text = True
            
            if has_text:
                text_posts_found += 1
                
                # Если нашли хотя бы 3 текстовых поста из первых 10 - канал подходит
                if text_posts_found >= 3 and total_checked <= 10:
                    logger.info(f"Канал {username} содержит достаточно текстовых постов: {text_posts_found} из {total_checked}")
                    return True
        
        # Оцениваем соотношение текстовых постов
        text_ratio = text_posts_found / total_checked if total_checked > 0 else 0
        
        logger.info(f"Канал {username}: найдено {text_posts_found} текстовых постов из {total_checked} проверенных (соотношение: {text_ratio:.2f})")
        
        # Канал подходит если хотя бы 30% постов содержат текст
        return text_ratio >= 0.3
        
    except (ChannelPrivateError, ChannelInvalidError):
        logger.warning(f"Канал {username} недоступен")
        return False
    except FloodWaitError as e:
        logger.error(f"FloodWaitError при проверке канала {username}: {e.seconds} секунд")
        raise
    except Exception as e:
        logger.warning(f"Ошибка проверки текстовых постов канала {username}: {e}")
        return False

async def check_channel_comments_available(client: TelegramClient, username: str) -> bool:
    """Проверка доступности комментариев в канале с использованием GetFullChannelRequest и обработкой FloodWait"""
    try:
        if not username.startswith('@'):
            username = '@' + username
        
        async def get_entity_safe():
            return await client.get_entity(username)
        
        async def get_full_channel_safe(entity):
            return await client(GetFullChannelRequest(entity))
        
        entity = await handle_flood_wait(get_entity_safe)
        full_channel = await handle_flood_wait(get_full_channel_safe, entity)
        
        if hasattr(full_channel.full_chat, 'linked_chat_id') and full_channel.full_chat.linked_chat_id:
            logger.info(f"Канал {username} имеет группу обсуждений (linked_chat_id: {full_channel.full_chat.linked_chat_id})")
            return True
        else:
            logger.info(f"Канал {username} не имеет группы обсуждений")
            return False
        
    except (ChannelPrivateError, ChannelInvalidError):
        logger.warning(f"Канал {username} недоступен")
        return False
    except FloodWaitError as e:
        logger.error(f"FloodWaitError при проверке канала {username}: {e.seconds} секунд")
        raise
    except Exception as e:
        logger.warning(f"Ошибка проверки канала {username}: {e}")
        return False

async def analyze_channel(channel_id: int) -> Tuple[List[str], List[str]]:
    """Анализ канала для определения тематики и ключевых слов с обработкой FloodWait"""
    try:
        global shared_telethon_client
        if not shared_telethon_client:
            logger.error("Telethon клиент не инициализирован в поисковике")
            return [], []

        if not shared_telethon_client.is_connected():
            try:
                await shared_telethon_client.connect()
            except Exception as e:
                logger.error(f"Не удалось подключить Telethon клиент: {e}")
                return [], []

        async def get_entity_safe():
            if not shared_telethon_client:
                raise Exception("Telethon клиент не инициализирован")
            return await shared_telethon_client.get_entity(channel_id)
        
        async def iter_messages_safe(entity, target_text_posts=30):
            """Получаем именно target_text_posts текстовых постов"""
            if not shared_telethon_client:
                raise Exception("Telethon клиент не инициализирован")
            
            text_messages = []
            total_checked = 0
            
            async for message in shared_telethon_client.iter_messages(entity, limit=1000):
                total_checked += 1
                
                # ИСПРАВЛЕНИЕ: Улучшенная проверка наличия текста
                message_text = ""
                
                if hasattr(message, 'message') and message.message:
                    message_text = message.message.strip()
                elif hasattr(message, 'text') and message.text:
                    message_text = message.text.strip()
                
                # Если основного текста нет, но есть медиа с подписью
                if not message_text and hasattr(message, 'media') and message.media:
                    if hasattr(message, 'message') and message.message:
                        message_text = message.message.strip()
                
                if message_text:
                    text_messages.append(message_text)
                    
                    if len(text_messages) >= target_text_posts:
                        break
                
                if total_checked >= 1000:
                    logger.warning(f"Достигнут лимит проверки 1000 сообщений. Найдено {len(text_messages)} текстовых постов")
                    break
            
            logger.info(f"Проверено {total_checked} сообщений, найдено {len(text_messages)} текстовых постов")
            return text_messages
        
        entity = await handle_flood_wait(get_entity_safe)
        
        channel_info = []
        
        if hasattr(entity, 'title'):
            channel_info.append(f"Название: {entity.title}")
        
        if hasattr(entity, 'about') and entity.about:
            channel_info.append(f"Описание: {entity.about}")
        
        posts_text = await handle_flood_wait(iter_messages_safe, entity, 50)
        
        posts_count = len(posts_text) if posts_text else 0
        logger.info(f"Получено текстовых постов для анализа канала {channel_id}: {posts_count}")
        
        if posts_text:
            posts_text.reverse()
            
            formatted_posts = []
            for post in posts_text:
                formatted_posts.append(f"**Пост:**\n\n{post}\n\n——————————————————")
            
            channel_info.append("\n\n".join(formatted_posts))
            
            logger.info(f"В промт добавлено {len(formatted_posts)} постов")
        else:
            channel_info.append("Посты не найдены")
            logger.info("Посты не найдены для добавления в промт")
        
        full_text = "\n".join(channel_info)
        
        try:
            import bot_interface
            prompts = bot_interface.get_bot_prompts()
            analysis_prompt = prompts.get('analysis_prompt', '')
            if not analysis_prompt:
                raise Exception("Промт для анализа не найден в bot_interface")
        except Exception as e:
            logger.error(f"Ошибка получения промта из bot_interface: {e}")
            analysis_prompt = """Данные канала:

{full_text}

———————————————————————

Ты — профессиональный аналитик Telegram-каналов. Проанализируй название, описание и посты канала и в результате дай точную оценку.

📌 1. Сгенерируй **ТОЧНЫЕ** ключевые слова, которые могли бы встречаться в **названиях других каналов точно по этой же теме**.

📌 2. Определи основную тему или темы канала, строго выбрав их из следующего списка:

{topics}

📤 Формат ответа:

ТЕМЫ: укажи только темы из списка. Если тем несколько, то пиши каждую через запятую.
КЛЮЧЕВЫЕ_СЛОВА: только короткие, точные, релевантные слова, для названия канала по этой теме. Каждое слово через запятую.

Отвечай строго в заданном формате."""
       
        topics_list_text = '["Бизнес и стартапы", "Блоги", "Букмекерство", "Видео и фильмы", "Даркнет", "Дизайн", "Для взрослых", "Еда и кулинария", "Здоровье и медицина", "Игры", "Искусство", "История", "Книги", "Красота и мода", "Криптовалюты", "Лайфхаки", "Маркетинг, PR, реклама", "Музыка", "Наука", "Новости и СМИ", "Образование", "Политика", "Психология", "Путешествия", "Работа и карьера", "Развлечения", "Религия", "Семья и дети", "Спорт", "Технологии", "Фото", "Эзотерика", "Юмор", "Другое"]'
        
        prompt = analysis_prompt
        
        if '{full_text}' in prompt:
            prompt = prompt.replace('{full_text}', full_text)
        else:
            prompt = prompt + f"\n\nДанные канала:\n{full_text}"
        
        if '{topics}' in prompt:
            prompt = prompt.replace('{topics}', topics_list_text)
        
        try:
            response = await g4f.ChatCompletion.create_async(
                model="gemini-2.5-flash",
                messages=[{"role": "user", "content": prompt}]
            )
            
            print("="*50)
            print("ОТВЕТ ОТ ИИ:")
            print("="*50)
            print(response)
            print("="*50)
            
            topics = []
            keywords = []
            
            lines = response.split('\n')
            for line in lines:
                if line.startswith('ТЕМЫ:'):
                    topics_text = line.replace('ТЕМЫ:', '').strip()
                    topics = [topic.strip() for topic in topics_text.split(',') if topic.strip()]
                elif line.startswith('КЛЮЧЕВЫЕ_СЛОВА:'):
                    keywords_text = line.replace('КЛЮЧЕВЫЕ_СЛОВА:', '').strip()
                    keywords = [kw.strip() for kw in keywords_text.split(',') if kw.strip()]
            
            print(f"Исходные темы от ИИ: {topics}")
            print(f"Исходные ключевые слова от ИИ: {keywords}")
            
            valid_topics = [
                "Бизнес и стартапы", "Блоги", "Букмекерство", "Видео и фильмы", "Даркнет", "Дизайн", 
                "Для взрослых", "Еда и кулинария", "Здоровье и медицина", "Игры", "Искусство", "История", 
                "Книги", "Красота и мода", "Криптовалюты", "Лайфхаки", "Маркетинг, PR, реклама", "Музыка", 
                "Наука", "Новости и СМИ", "Образование", "Политика", "Психология", "Путешествия", 
                "Работа и карьера", "Развлечения", "Религия", "Семья и дети", "Спорт", "Технологии", 
                "Фото", "Эзотерика", "Юмор", "Другое"
            ]
            
            filtered_topics = []
            
            topics_text = ', '.join(topics)
            for valid_topic in valid_topics:
                if topics_text.lower() == valid_topic.lower():
                    filtered_topics.append(valid_topic)
                    break
            
            if not filtered_topics:
                for topic in topics:
                    for valid_topic in valid_topics:
                        if topic.lower() == valid_topic.lower():
                            if valid_topic not in filtered_topics:
                                filtered_topics.append(valid_topic)
                            break
            
            unique_keywords = []
            for keyword in keywords:
                keyword_clean = keyword.strip()
                if keyword_clean and keyword_clean not in unique_keywords:
                    unique_keywords.append(keyword_clean)
            
            if not filtered_topics:
                filtered_topics = ['Другое']
                logger.warning("Не найдено подходящих тем, установлена тема 'Другое'")
            if not unique_keywords:
                unique_keywords = ['общее', 'контент']
                logger.warning("Не найдено ключевых слов, установлены дефолтные")
            
            print(f"Отфильтрованные темы: {filtered_topics}")
            print(f"Уникальные ключевые слова: {unique_keywords}")
            
            logger.info(f"Анализ канала завершен. Темы: {filtered_topics}, Ключевые слова: {unique_keywords}")
            return filtered_topics, unique_keywords
            
        except Exception as e:
            logger.error(f"Ошибка анализа с GPT-4: {e}")
            return ['Бизнес и стартапы', 'Маркетинг, PR, реклама'], ['бизнес', 'маркетинг', 'продвижение']
    
    except FloodWaitError as e:
        logger.error(f"FloodWaitError при анализе канала: {e.seconds} секунд")
        raise
    except Exception as e:
        logger.error(f"Ошибка анализа канала: {e}")
        return [], []

def get_actually_processed_count():
    """ИСПРАВЛЕНИЕ: Получение реального количества обработанных каналов из масслукера"""
    try:
        import masslooker
        return len(masslooker.processed_channels)
    except Exception as e:
        logger.debug(f"Не удалось получить количество реально обработанных каналов: {e}")
        return 0

async def process_found_channels(channels: List[str]):
    """ИСПРАВЛЕНИЕ: Обработка найденных каналов БЕЗ подсчета как обработанных до фактических действий"""
    global sent_to_queue_count
    
    logger.info(f"Начинаем обработку {len(channels)} найденных каналов")
    
    max_channels = current_settings.get('max_channels', 150)
    
    for channel in channels:
        try:
            if not search_active:
                logger.info("Поиск остановлен, прерываем обработку каналов")
                break
            
            try:
                import bot_interface
                if not bot_interface.bot_data['is_running']:
                    logger.info("Остановка запрошена в bot_interface, прерываем обработку каналов")
                    break
            except:
                pass
            
            # ИСПРАВЛЕНИЕ: проверяем лимит по РЕАЛЬНО обработанным каналам из масслукера
            actual_processed_count = get_actually_processed_count()
            if max_channels != float('inf') and actual_processed_count >= max_channels:
                logger.info(f"Достигнут лимит РЕАЛЬНО обработанных каналов: {max_channels} (обработано с действиями: {actual_processed_count})")
                await stop_search()
                break
            
            # ИСПРАВЛЕНИЕ: Проверяем наличие текстовых постов перед отправкой в очередь
            has_text_posts = await has_textual_posts(shared_telethon_client, channel)
            if not has_text_posts:
                logger.info(f"Канал {channel} не содержит достаточно текстовых постов для комментирования, пропускаем")
                continue
            
            # ИСПРАВЛЕНИЕ: Проверяем доступность комментариев
            has_comments = await check_channel_comments_available(shared_telethon_client, channel)
            if not has_comments:
                logger.info(f"Канал {channel} не поддерживает комментарии, пропускаем")
                continue
            
            # Канал подходит - добавляем в статистику найденных (НЕ обработанных!)
            try:
                import bot_interface
                bot_interface.add_processed_channel_statistics(
                    channel,
                    found_topic='Другое'
                )
                # Добавляем канал в найденные каналы для статистики
                found_channels_list = bot_interface.bot_data['detailed_statistics']['found_channels']
                if channel not in found_channels_list:
                    found_channels_list.append(channel)
                    bot_interface.update_found_channels_statistics(found_channels_list)
            except Exception as e:
                logger.error(f"Ошибка добавления канала {channel} в статистику: {e}")
            
            # ИСПРАВЛЕНИЕ: отправляем в очередь НО НЕ СЧИТАЕМ обработанным
            try:
                import masslooker
                await masslooker.add_channel_to_queue(channel)
                sent_to_queue_count += 1  # ИСПРАВЛЕНИЕ: это только "отправлено в очередь", не "обработано"
                actual_processed = get_actually_processed_count()
                logger.info(f"✅ Канал {channel} отправлен в очередь масслукинга. Отправлено в очередь: {sent_to_queue_count}, реально обработано: {actual_processed}")
                # НЕ обновляем статистику обработанных каналов здесь - это будет делать масслукер после фактических действий!
            except Exception as e:
                logger.error(f"Ошибка добавления канала {channel} в очередь масслукинга: {e}")
            
            await asyncio.sleep(random.uniform(1, 3))
            
        except Exception as e:
            logger.error(f"Ошибка обработки канала {channel}: {e}")
            continue
    
    logger.info("Обработка найденных каналов завершена")

async def save_search_progress():
    """Сохранение прогресса поиска в базу данных"""
    try:
        from database import db
        progress_data = [
            ('search_progress', search_progress),
            ('found_channels', list(found_channels)),
            ('sent_to_queue_count', sent_to_queue_count)  # ИСПРАВЛЕНИЕ: переименовываем для ясности
        ]
        
        for key, value in progress_data:
            await db.save_bot_state(key, value)
    except Exception as e:
        logger.error(f"Ошибка сохранения прогресса поиска: {e}")

async def load_search_progress():
    """Загрузка прогресса поиска из базы данных"""
    global search_progress, found_channels, sent_to_queue_count
    try:
        from database import db
        
        saved_progress = await db.load_bot_state('search_progress', {})
        if saved_progress:
            search_progress.update(saved_progress)
        
        saved_channels = await db.load_bot_state('found_channels', [])
        if saved_channels:
            found_channels.update(saved_channels)
        
        # ИСПРАВЛЕНИЕ: загружаем правильный счетчик
        sent_to_queue_count = await db.load_bot_state('sent_to_queue_count', 0)
        
        # Для совместимости со старой версией
        if sent_to_queue_count == 0:
            sent_to_queue_count = await db.load_bot_state('processed_channels_count', 0)
        
        actual_processed = get_actually_processed_count()
        
        logger.info(f"Загружен прогресс поиска: {search_progress}")
        logger.info(f"Загружено найденных каналов: {len(found_channels)}")
        logger.info(f"Отправлено в очередь: {sent_to_queue_count}, реально обработано: {actual_processed}")
    except Exception as e:
        logger.error(f"Ошибка загрузки прогресса поиска: {e}")

async def search_loop_async():
    """ИСПРАВЛЕНИЕ: Основная функция поиска с правильной проверкой лимитов"""
    global driver, search_active, sent_to_queue_count
    
    logger.info("🔍 Начинаем основной цикл поиска каналов")
    
    await load_search_progress()
    
    logger.info(f"📊 Начальное состояние: search_active={search_active}")
    
    while search_active:
        try:
            try:
                import bot_interface
                bot_is_running = bot_interface.bot_data.get('is_running', True)
                logger.debug(f"🤖 Состояние bot_interface.is_running: {bot_is_running}")
                
                if not bot_is_running:
                    logger.info("Остановка запрошена в bot_interface, завершаем поиск")
                    search_active = False
                    break
            except Exception as e:
                logger.warning(f"⚠️ Не удалось проверить состояние bot_interface (возможно тест): {e}")
            
            if not shared_telethon_client:
                logger.error("❌ Нет Telethon клиента, поиск невозможен")
                search_active = False
                break
            
            if hasattr(shared_telethon_client, 'connected') and not hasattr(shared_telethon_client, 'api_id'):
                logger.info("🧪 Обнаружен мок-клиент, завершаем тестовый поиск")
                search_active = False
                break
            
            max_channels = current_settings.get('max_channels', 150)
            
            # ИСПРАВЛЕНИЕ: проверяем лимит по РЕАЛЬНО обработанным каналам
            actual_processed_count = get_actually_processed_count()
            if max_channels != float('inf') and actual_processed_count >= max_channels:
                logger.info(f"Достигнут лимит РЕАЛЬНО обработанных каналов: {max_channels} (обработано с действиями: {actual_processed_count}). Останавливаем поиск.")
                search_active = False
                break
            
            if not driver:
                logger.info("🚀 Инициализируем веб-драйвер в отдельном потоке...")
                
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(setup_driver_threaded)
                    driver = future.result()
                
                if not driver:
                    logger.error("❌ Не удалось инициализировать драйвер")
                    await asyncio.sleep(60)
                    continue
            
            navigation_success = False
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(navigate_to_channel_search, driver)
                navigation_success = future.result()
            
            if not navigation_success:
                logger.error("Не удалось перейти к поиску каналов")
                await asyncio.sleep(60)
                continue
            
            keywords = current_settings.get('keywords', [])
            topics = current_settings.get('topics', [])
            
            if not keywords or not topics:
                logger.warning("Ключевые слова или темы не настроены")
                await asyncio.sleep(300)
                continue
            
            first_search = True
            
            # ИСПРАВЛЕНИЕ: определяем с какого места продолжить поиск
            start_keyword_index = 0
            start_topic_index = 0
            
            # Если есть сохраненный прогресс, продолжаем с места остановки
            if search_progress.get('current_keyword') and search_progress.get('current_topic'):
                try:
                    start_topic_index = topics.index(search_progress['current_topic'])
                    start_keyword_index = keywords.index(search_progress['current_keyword'])
                    # Переходим к следующему ключевому слову после остановки
                    start_keyword_index += 1
                    if start_keyword_index >= len(keywords):
                        start_keyword_index = 0
                        start_topic_index += 1
                    logger.info(f"Продолжаем поиск с места остановки: тема {start_topic_index}, ключевое слово {start_keyword_index}")
                except ValueError:
                    logger.warning("Сохраненный прогресс содержит несуществующие ключевые слова или темы, начинаем с начала")
                    start_keyword_index = 0
                    start_topic_index = 0
            
            # Перебираем все комбинации тем и ключевых слов
            for topic_idx, topic in enumerate(topics[start_topic_index:], start_topic_index):
                # ИСПРАВЛЕНИЕ: проверяем лимит по реально обработанным каналам
                actual_processed_count = get_actually_processed_count()
                if max_channels != float('inf') and actual_processed_count >= max_channels:
                    logger.info(f"Достигнут лимит РЕАЛЬНО обработанных каналов во время поиска: {max_channels}")
                    search_active = False
                    break
                
                try:
                    import bot_interface
                    if not bot_interface.bot_data['is_running']:
                        logger.info("Остановка запрошена, прерываем цикл по темам")
                        search_active = False
                        break
                except:
                    pass
                
                if not search_active:
                    break
                
                keyword_start = start_keyword_index if topic_idx == start_topic_index else 0
                
                for keyword_idx, keyword in enumerate(keywords[keyword_start:], keyword_start):
                    # ИСПРАВЛЕНИЕ: проверяем лимит по реально обработанным каналам
                    actual_processed_count = get_actually_processed_count()
                    if max_channels != float('inf') and actual_processed_count >= max_channels:
                        logger.info(f"Достигнут лимит РЕАЛЬНО обработанных каналов во время поиска: {max_channels}")
                        search_active = False
                        break
                    
                    try:
                        import bot_interface
                        if not bot_interface.bot_data['is_running']:
                            logger.info("Остановка запрошена, прерываем цикл по ключевым словам")
                            search_active = False
                            break
                    except:
                        pass
                    
                    if not search_active:
                        break
                    
                    try:
                        search_progress['current_keyword'] = keyword
                        search_progress['current_topic'] = topic
                        await save_search_progress()
                        
                        channels = []
                        with concurrent.futures.ThreadPoolExecutor() as executor:
                            future = executor.submit(search_channels_sync, driver, keyword, topic, first_search)
                            channels = future.result()
                        
                        first_search = False
                        
                        if channels:
                            try:
                                await process_found_channels(channels)
                            except FloodWaitError as e:
                                logger.warning(f"FloodWaitError при обработке каналов: {e.seconds} секунд")
                                await asyncio.sleep(e.seconds)
                        
                        # ИСПРАВЛЕНИЕ: проверяем лимит по реально обработанным каналам
                        actual_processed_count = get_actually_processed_count()
                        if max_channels != float('inf') and actual_processed_count >= max_channels:
                            logger.info(f"Достигнут лимит РЕАЛЬНО обработанных каналов после обработки: {max_channels}")
                            search_active = False
                            break
                        
                        await asyncio.sleep(random.uniform(3, 7))
                        
                    except FloodWaitError as e:
                        logger.warning(f"FloodWaitError в цикле поиска: {e.seconds} секунд")
                        await asyncio.sleep(e.seconds)
                        continue
                    except Exception as e:
                        logger.error(f"Ошибка поиска по '{keyword}' и '{topic}': {e}")
                        await asyncio.sleep(30)
                        continue
            
            # ИСПРАВЛЕНИЕ: правильная логика продолжения поиска
            if max_channels == float('inf'):
                # Если лимит ∞, сбрасываем прогресс и начинаем новый цикл
                search_progress['current_keyword'] = ''
                search_progress['current_topic'] = ''
                await save_search_progress()
                
                logger.info("Цикл поиска завершен (лимит ∞), ожидание 30 минут...")
                for wait_second in range(1800):
                    try:
                        import bot_interface
                        if not bot_interface.bot_data['is_running']:
                            logger.info("Остановка запрошена во время ожидания, завершаем поиск")
                            search_active = False
                            break
                    except:
                        pass
                    
                    if not search_active:
                        break
                    await asyncio.sleep(1)
            else:
                # ИСПРАВЛЕНИЕ: для конечного лимита НЕ СБРАСЫВАЕМ прогресс
                actual_processed_count = get_actually_processed_count()
                if actual_processed_count < max_channels:
                    logger.info(f"Реально обработано {actual_processed_count} из {max_channels} каналов. Продолжаем поиск с места остановки...")
                    # НЕ сбрасываем search_progress - продолжаем с места остановки
                    continue
                else:
                    logger.info(f"Лимит каналов достигнут: {actual_processed_count}/{max_channels}")
                    search_active = False
                    break
            
        except FloodWaitError as e:
            logger.warning(f"FloodWaitError в основном цикле: {e.seconds} секунд")
            await asyncio.sleep(e.seconds)
            continue
        except Exception as e:
            logger.error(f"Критическая ошибка в цикле поиска: {e}")
            if driver:
                try:
                    driver.quit()
                except:
                    pass
                driver = None
            await asyncio.sleep(60)
    
    if driver:
        try:
            driver.quit()
            logger.info("Драйвер закрыт при завершении поиска")
        except:
            pass
        driver = None
    
    logger.info("Цикл поиска каналов завершен")

async def start_search(settings: dict, shared_client: Optional[TelegramClient] = None):
    """Запуск поиска каналов с использованием единого клиента"""
    global search_active, current_settings, api_call_interval, shared_telethon_client, sent_to_queue_count
    
    if is_search_really_active():
        logger.warning("Поиск уже запущен")
        return
    
    if search_active:
        logger.info("Сбрасываем заблокированное состояние поиска")
        await reset_search_state()
    
    logger.info("Запуск поиска каналов...")
    current_settings = settings.copy()
    
    # ИСПРАВЛЕНИЕ: НЕ сбрасываем счетчик при продолжении поиска
    # sent_to_queue_count остается как есть для продолжения с места остановки
    
    api_call_interval = 1.0
    
    if shared_client and isinstance(shared_client, TelegramClient):
        shared_telethon_client = shared_client
        logger.info("Используем единый Telethon клиент от bot_interface")
        
        if not shared_telethon_client.is_connected():
            try:
                await shared_telethon_client.connect()
                logger.info("Telethon клиент успешно подключен")
            except Exception as e:
                logger.error(f"Не удалось подключить Telethon клиент: {e}")
                return
    else:
        logger.error("Не передан корректный Telethon клиент")
        return

    search_active = True
    
    try:
        search_task = asyncio.create_task(search_loop_async())
        logger.info("Поиск каналов запущен как фоновая задача в основном event loop")
    except Exception as e:
        logger.error(f"Не удалось запустить поиск: {e}")
        search_active = False

async def stop_search():
    """Остановка поиска каналов"""
    global search_active, driver, shared_telethon_client
    
    logger.info("Остановка поиска каналов...")
    search_active = False
    
    await save_search_progress()
    
    if driver:
        try:
            driver.quit()
            logger.info("Драйвер закрыт")
        except Exception as e:
            logger.error(f"Ошибка закрытия драйвера: {e}")
        driver = None
    
    shared_telethon_client = None
    
    logger.info("Поиск каналов остановлен")

def get_statistics():
    """ИСПРАВЛЕНИЕ: Получение статистики поиска с правильным разделением"""
    actual_processed_count = get_actually_processed_count()
    
    return {
        'found_channels': len(found_channels),
        'search_active': search_active,
        'current_progress': search_progress.copy(),
        'api_call_interval': api_call_interval,
        'flood_wait_delays': flood_wait_delays.copy(),
        'sent_to_queue_count': sent_to_queue_count,  # ИСПРАВЛЕНИЕ: переименовываем для ясности
        'actually_processed_count': actual_processed_count,  # ИСПРАВЛЕНИЕ: добавляем реальный счетчик
        'processed_channels_count': sent_to_queue_count  # Для обратной совместимости
    }

async def reset_search_state():
    """Принудительный сброс состояния поиска для устранения блокировок"""
    global search_active, driver, shared_telethon_client, sent_to_queue_count
    
    logger.info("Принудительный сброс состояния поиска...")
    search_active = False
    # НЕ сбрасываем sent_to_queue_count для сохранения прогресса
    
    if driver:
        try:
            driver.quit()
        except Exception as e:
            logger.debug(f"Ошибка закрытия драйвера при сбросе: {e}")
        driver = None
    
    logger.info("Состояние поиска сброшено")

def is_search_really_active():
    """Проверка реального состояния поиска"""
    global search_active, driver, shared_telethon_client
    
    if search_active and not driver and not shared_telethon_client:
        logger.warning("Обнаружено неконсистентное состояние поиска - сбрасываем флаг")
        search_active = False
        return False
    
    return search_active

async def main():
    """Тестирование модуля"""
    test_settings = {
        'keywords': ['тест', 'пример'],
        'topics': ['Технологии', 'Образование']
    }
    
    await start_search(test_settings)
    
    await asyncio.sleep(120)
    
    await stop_search()

if __name__ == "__main__":
    asyncio.run(main())