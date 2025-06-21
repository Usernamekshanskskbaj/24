import asyncio
import logging
import random
import time
from datetime import datetime
from typing import List, Optional, Set, Callable, Any, Dict
import os

logger = logging.getLogger(__name__)

try:
    from telethon import TelegramClient, events
    from telethon.errors import ChannelPrivateError, ChatWriteForbiddenError, FloodWaitError, UserNotParticipantError
    from telethon.tl.types import Channel, Chat, MessageMediaPhoto, MessageMediaDocument
    from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
    from telethon.tl.functions.messages import SendReactionRequest, GetAvailableReactionsRequest, GetDiscussionMessageRequest, GetRepliesRequest
    from telethon.tl.types import ReactionEmoji, ReactionCustomEmoji
    import g4f
except ImportError as e:
    logger.error(f"Ошибка импорта библиотек: {e}")
    raise

# Глобальные переменные
masslooking_active = False
shared_client: Optional[TelegramClient] = None
settings = {}
processed_channels: Set[str] = set()  # ИСПРАВЛЕНИЕ: только каналы с фактически выполненными действиями
masslooking_progress = {'current_channel': '', 'processed_count': 0}
statistics = {
    'comments_sent': 0,
    'reactions_set': 0,
    'channels_processed': 0,  # ИСПРАВЛЕНИЕ: только каналы с фактически выполненными действиями
    'errors': 0,
    'flood_waits': 0,
    'total_flood_wait_time': 0
}

# Круговая обработка каналов
channel_processing_queue = {}  # ИСПРАВЛЕНИЕ: сохраняем только ID сообщений, не объекты
current_channel_iterator = None
channels_in_rotation = []

# Флаг для отслеживания первого действия подписки
first_subscription_made = False

# Отслеживание новых постов
new_post_tracking_active = False
tracked_channels = {}  # {username: {'entity_id': id, 'last_message_id': id}}

# Настройки FloodWait
FLOOD_WAIT_SETTINGS = {
    'max_retries': 5,
    'max_wait_time': 7200,  # максимальное время ожидания (2 часа)
    'enable_exponential_backoff': True,
    'check_interval': 10,  # интервал проверки состояния во время ожидания
    'backoff_multiplier': 1.5  # множитель для экспоненциального backoff
}

# Положительные реакции для Telegram
DEFAULT_POSITIVE_REACTIONS = [
    '👍', '❤️', '🔥', '🥰', '👏', '😍', '🤩', '💯', '⭐',
    '🎉', '🙏', '💪', '👌', '✨', '🌟', '🚀'
]

def check_bot_running() -> bool:
    """Проверка состояния работы бота"""
    try:
        import bot_interface
        return bot_interface.bot_data.get('is_running', True)
    except:
        return masslooking_active

async def check_subscription_status(entity, username: str) -> bool:
    """Проверка статуса подписки на канал"""
    try:
        # Получаем информацию о канале
        full_channel = await get_full_channel_safe(entity)
        if not full_channel:
            logger.warning(f"Не удалось получить информацию о канале {username} для проверки подписки")
            return False
        
        # Проверяем, подписаны ли мы на канал
        # Если канал приватный и мы не можем получить информацию - значит не подписаны
        if hasattr(full_channel.full_chat, 'participants_count'):
            logger.debug(f"Подписка на канал {username} активна")
            return True
        else:
            logger.warning(f"Нет подписки на канал {username}")
            return False
            
    except ChannelPrivateError:
        logger.warning(f"Канал {username} приватный или мы не подписаны")
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки подписки на канал {username}: {e}")
        return False

async def apply_subscription_delay(username: str, action_type: str = "подписки"):
    """Применение задержки с учетом флага первого действия"""
    global first_subscription_made
    
    # Если это первая подписка - не применяем задержку
    if not first_subscription_made:
        logger.info(f"Первая подписка на канал {username} - задержка не применяется")
        first_subscription_made = True
        return True
    
    # Для всех последующих подписок применяем задержку
    delay_range = settings.get('delay_range', (20, 1000))
    if delay_range == (0, 0):
        return True
    
    try:
        # Валидация диапазона задержки
        if not isinstance(delay_range, (list, tuple)) or len(delay_range) != 2:
            logger.warning(f"Некорректный диапазон задержки {delay_range}, используем дефолтный (20, 1000)")
            delay_range = (20, 1000)
        
        min_delay, max_delay = delay_range
        if not isinstance(min_delay, (int, float)) or not isinstance(max_delay, (int, float)):
            logger.warning(f"Некорректные типы задержки {delay_range}, используем дефолтный (20, 1000)")
            delay_range = (20, 1000)
            min_delay, max_delay = delay_range
        
        if min_delay < 0 or max_delay < 0 or min_delay > max_delay:
            logger.warning(f"Некорректные значения задержки {delay_range}, используем дефолтный (20, 1000)")
            delay_range = (20, 1000)
            min_delay, max_delay = delay_range
        
        subscription_delay = random.uniform(min_delay, max_delay)
        logger.info(f"Ожидание {subscription_delay:.1f} секунд перед {action_type} на канал {username}")
        
        # Разбиваем задержку на части с проверкой состояния
        delay_chunks = int(subscription_delay)
        remaining_delay = subscription_delay - delay_chunks
        
        for _ in range(delay_chunks):
            if not check_bot_running():
                logger.info(f"Остановка запрошена во время задержки {action_type}")
                return False
            
            # Проверяем обновление настроек
            try:
                import bot_interface
                new_settings = bot_interface.get_bot_settings()
                if new_settings != settings:
                    settings.update(new_settings)
                    logger.debug(f"Настройки обновлены во время задержки {action_type}")
            except Exception as e:
                logger.debug(f"Не удалось обновить настройки во время задержки: {e}")
            
            await asyncio.sleep(1)
        
        if remaining_delay > 0:
            if not check_bot_running():
                logger.info(f"Остановка запрошена во время задержки {action_type}")
                return False
            await asyncio.sleep(remaining_delay)
        
        logger.info(f"Задержка {action_type} завершена для канала {username}")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при обработке задержки {action_type}: {e}")
        await asyncio.sleep(20)  # Используем минимальную безопасную задержку
        return True

async def ensure_subscription(username: str) -> bool:
    """Обеспечение подписки на канал с проверкой и переподпиской при необходимости"""
    try:
        # Получаем сущность канала
        entity = await get_entity_safe(username)
        if not entity:
            logger.error(f"Не удалось получить сущность канала {username}")
            return False
        
        # Проверяем текущий статус подписки
        is_subscribed = await check_subscription_status(entity, username)
        
        if not is_subscribed:
            logger.info(f"Обнаружена отписка от канала {username}, подписываемся заново")
            
            # Добавляем задержку перед переподпиской
            delay_success = await apply_subscription_delay(username, "переподписки")
            if not delay_success:
                return False
            
            # Подписываемся заново
            join_result = await join_channel_safe(entity)
            if join_result is None:
                logger.error(f"Не удалось переподписаться на канал {username}")
                return False
            
            logger.info(f"Успешно переподписались на канал {username}")
            return True
        else:
            logger.debug(f"Подписка на канал {username} активна")
            return True
            
    except Exception as e:
        logger.error(f"Ошибка обеспечения подписки на канал {username}: {e}")
        return False

async def smart_wait(wait_time: int, operation_name: str = "operation") -> bool:
    """Умное ожидание с возможностью прерывания и экспоненциальным backoff"""
    original_wait_time = wait_time
    
    # Ограничиваем максимальное время ожидания
    if wait_time > FLOOD_WAIT_SETTINGS['max_wait_time']:
        wait_time = FLOOD_WAIT_SETTINGS['max_wait_time']
        logger.warning(f"FloodWait для {operation_name}: {original_wait_time}с ограничен до {wait_time}с")
    
    logger.info(f"Ожидание FloodWait для {operation_name}: {wait_time} секунд")
    
    # Обновляем статистику
    statistics['flood_waits'] += 1
    statistics['total_flood_wait_time'] += wait_time
    
    # Разбиваем ожидание на части для регулярной проверки состояния
    check_interval = FLOOD_WAIT_SETTINGS['check_interval']
    chunks = wait_time // check_interval
    remainder = wait_time % check_interval
    
    # Ожидаем по частям с проверкой состояния
    for i in range(chunks):
        if not check_bot_running():
            logger.info(f"Остановка запрошена во время FloodWait для {operation_name}")
            return False
        
        progress = (i + 1) * check_interval
        remaining = wait_time - progress
        logger.debug(f"FloodWait {operation_name}: прошло {progress}с, осталось {remaining}с")
        
        await asyncio.sleep(check_interval)
    
    # Ожидаем оставшееся время
    if remainder > 0:
        if not check_bot_running():
            return False
        await asyncio.sleep(remainder)
    
    logger.info(f"FloodWait для {operation_name} завершен")
    return True

async def handle_flood_wait(func: Callable, *args, operation_name: str = None, max_retries: int = None, **kwargs) -> Any:
    """Универсальная обработка FloodWait для любых функций"""
    if operation_name is None:
        operation_name = func.__name__ if hasattr(func, '__name__') else "operation"
    
    if max_retries is None:
        max_retries = FLOOD_WAIT_SETTINGS['max_retries']
    
    base_delay = 1  # базовая задержка между попытками
    
    for attempt in range(max_retries):
        try:
            # Проверяем состояние перед каждой попыткой
            if not check_bot_running():
                logger.info(f"Остановка запрошена перед выполнением {operation_name}")
                return None
            
            logger.debug(f"Попытка {attempt + 1}/{max_retries} для {operation_name}")
            return await func(*args, **kwargs)
            
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"FloodWait при {operation_name} (попытка {attempt + 1}): {wait_time} секунд")
            
            if attempt < max_retries - 1:
                # Ожидаем FloodWait
                if not await smart_wait(wait_time, operation_name):
                    logger.info(f"Прерываем {operation_name} из-за остановки бота")
                    return None
                
                # Добавляем экспоненциальную задержку после FloodWait
                if FLOOD_WAIT_SETTINGS['enable_exponential_backoff']:
                    extra_delay = base_delay * (FLOOD_WAIT_SETTINGS['backoff_multiplier'] ** attempt)
                    logger.debug(f"Дополнительная задержка после FloodWait: {extra_delay:.1f}с")
                    await asyncio.sleep(extra_delay)
                
                continue
            else:
                logger.error(f"Превышено количество попыток для {operation_name} после FloodWait")
                statistics['errors'] += 1
                return None
                
        except Exception as e:
            logger.error(f"Ошибка при выполнении {operation_name} (попытка {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                # Небольшая задержка перед повтором при обычных ошибках
                await asyncio.sleep(random.uniform(1, 3))
                continue
            else:
                logger.error(f"Превышено количество попыток для {operation_name}")
                statistics['errors'] += 1
                return None
    
    return None

# ИСПРАВЛЕНИЕ: Улучшенная функция извлечения текста из сообщения
def extract_message_text(message) -> str:
    """Извлекает текст из сообщения, включая сообщения с медиа"""
    text = ""
    
    try:
        # Проверяем основной текст сообщения
        if hasattr(message, 'message') and message.message:
            text = str(message.message).strip()
        elif hasattr(message, 'text') and message.text:
            text = str(message.text).strip()
        
        # Если основного текста нет, но есть медиа с подписью
        if not text and hasattr(message, 'media') and message.media:
            # Для медиа-сообщений текст тоже хранится в message
            if hasattr(message, 'message') and message.message:
                text = str(message.message).strip()
        
        return text
    except Exception as e:
        logger.error(f"Ошибка извлечения текста из сообщения: {e}")
        return ""

# ИСПРАВЛЕНИЕ: Улучшенная функция проверки наличия контента в сообщении
def has_commentable_content(message) -> bool:
    """Проверяет, есть ли в сообщении контент для комментирования"""
    try:
        # Проверяем ID сообщения
        if not hasattr(message, 'id') or not message.id or message.id <= 0:
            return False
        
        # Извлекаем текст
        text = extract_message_text(message)
        
        # Если есть текст (включая подписи к медиа)
        if text and len(text.strip()) > 0:
            return True
        
        # Проверяем наличие медиа-контента без текста
        if hasattr(message, 'media') and message.media:
            # Фото, видео, документы и т.д. тоже можно комментировать
            return True
        
        return False
    except Exception as e:
        logger.error(f"Ошибка проверки контента сообщения: {e}")
        return False

async def get_post_comments(message, channel_entity) -> str:
    """Получение комментариев к посту"""
    try:
        if not shared_client:
            logger.warning("Telethon клиент не инициализирован")
            return ""
        
        # Получаем discussion message через GetDiscussionMessageRequest
        discussion_info = await shared_client(GetDiscussionMessageRequest(
            peer=channel_entity,
            msg_id=message.id
        ))
        
        if not discussion_info or not discussion_info.messages:
            return ""
        
        discussion_message = discussion_info.messages[0]
        discussion_group = discussion_message.peer_id
        reply_to_msg_id = discussion_message.id
        
        # Получаем ответы на этот пост (комментарии)
        replies = await shared_client(GetRepliesRequest(
            peer=discussion_group,
            msg_id=reply_to_msg_id,
            offset_date=None,
            offset_id=0,
            offset_peer=None,
            limit=50
        ))
        
        if not replies or not replies.messages:
            return ""
        
        comments = []
        total_length = 0
        max_length = 10000
        
        for msg in replies.messages:
            if msg.message and msg.message.strip():
                # Получаем имя отправителя
                sender_name = "Аноним"
                try:
                    if hasattr(msg, 'from_id') and msg.from_id:
                        sender = await shared_client.get_entity(msg.from_id)
                        if hasattr(sender, 'first_name'):
                            sender_name = sender.first_name
                            if hasattr(sender, 'last_name') and sender.last_name:
                                sender_name += f" {sender.last_name}"
                        elif hasattr(sender, 'title'):
                            sender_name = sender.title
                except:
                    pass
                
                comment_text = f"{sender_name}: {msg.message.strip()}"
                
                # Проверяем лимит длины
                if total_length + len(comment_text) + 2 > max_length:
                    break
                
                comments.append(comment_text)
                total_length += len(comment_text) + 2
        
        return "\n\n".join(comments)
        
    except Exception as e:
        logger.error(f"Ошибка получения комментариев: {e}")
        return ""

async def generate_comment(post_text: str, topics: List[str], message=None, channel_entity=None) -> str:
    """Генерация комментария с помощью GPT-4 с использованием промта из bot_interface"""
    try:
        # Получаем промт из bot_interface
        try:
            import bot_interface
            prompts = bot_interface.get_bot_prompts()
            comment_prompt = prompts.get('comment_prompt', '')
            if not comment_prompt:
                raise Exception("Промт для комментариев не найден в bot_interface")
        except Exception as e:
            logger.error(f"Ошибка получения промта из bot_interface: {e}")
            # Используем fallback промт
            comment_prompt = """Создай короткий, естественный комментарий к посту на русском языке. 

Текст поста: {text_of_the_post}

Требования к комментарию:
- Максимум 2-3 предложения
- Естественный стиль общения
- Положительная или нейтральная тональность
- Без спама и навязчивости
- Соответствует тематике поста
- Выглядит как реальный отзыв пользователя
- Без эмодзи
- Без ссылок
- Без рекламы

Создай комментарий:"""
        
        # Подготавливаем данные для замены плейсхолдеров
        topics_text = ', '.join(topics) if topics else 'общая тематика'
        
        # Получаем комментарии под постом если нужно
        comments_text = ""
        if '{comments}' in comment_prompt and message and channel_entity:
            comments_text = await get_post_comments(message, channel_entity)
        
        # Заменяем плейсхолдеры в промпте
        prompt = comment_prompt
        
        if '{text_of_the_post}' in prompt:
            prompt = prompt.replace('{text_of_the_post}', post_text[:1000])
        else:
            prompt = prompt + f"\n\nТекст поста: {post_text[:1000]}"
        
        if '{topics}' in prompt:
            prompt = prompt.replace('{topics}', topics_text)
        
        if '{comments}' in prompt:
            prompt = prompt.replace('{comments}', comments_text if comments_text else "Комментариев пока нет")
        
        # Генерируем комментарий
        response = g4f.ChatCompletion.create(
            model=g4f.models.gpt_4,
            messages=[{"role": "user", "content": prompt}],
            stream=False
        )
        
        # Очищаем ответ - убираем любые лишние символы
        comment = response.strip()
        
        # Удаляем кавычки в начале и конце если есть
        if comment.startswith('"') and comment.endswith('"'):
            comment = comment[1:-1]
        
        if comment.startswith("'") and comment.endswith("'"):
            comment = comment[1:-1]
        
        logger.info(f"Сгенерирован комментарий: {comment[:50]}...")
        return comment
        
    except Exception as e:
        logger.error(f"Ошибка генерации комментария: {e}")
        # Возвращаем простой комментарий в случае ошибки
        fallback_comments = [
            "Интересно, спасибо за пост!",
            "Полезная информация",
            "Актуальная тема",
            "Хороший материал",
            "Согласен с автором"
        ]
        return random.choice(fallback_comments)

async def get_entity_safe(identifier):
    """Безопасное получение сущности с обработкой FloodWait"""
    async def _get_entity():
        return await shared_client.get_entity(identifier)
    
    return await handle_flood_wait(_get_entity, operation_name=f"get_entity({identifier})")

async def get_full_channel_safe(entity):
    """Безопасное получение полной информации о канале с обработкой FloodWait"""
    async def _get_full_channel():
        return await shared_client(GetFullChannelRequest(entity))
    
    return await handle_flood_wait(_get_full_channel, operation_name=f"get_full_channel({entity.id})")

async def join_channel_safe(entity):
    """Безопасная подписка на канал с обработкой FloodWait"""
    async def _join_channel():
        return await shared_client(JoinChannelRequest(entity))
    
    return await handle_flood_wait(_join_channel, operation_name=f"join_channel({entity.username or entity.id})")

async def leave_channel_safe(entity):
    """Безопасная отписка от канала с обработкой FloodWait"""
    async def _leave_channel():
        return await shared_client(LeaveChannelRequest(entity))
    
    return await handle_flood_wait(_leave_channel, operation_name=f"leave_channel({entity.username or entity.id})")

async def send_message_safe(peer, message, **kwargs):
    """Безопасная отправка сообщения с обработкой FloodWait"""
    async def _send_message():
        return await shared_client.send_message(peer, message, **kwargs)
    
    peer_name = getattr(peer, 'username', None) or getattr(peer, 'id', 'unknown')
    return await handle_flood_wait(_send_message, operation_name=f"send_message_to({peer_name})")

async def send_reaction_safe(peer, msg_id, reaction):
    """Безопасная отправка реакции с обработкой FloodWait и ошибки лимита реакций"""
    async def _send_reaction():
        return await shared_client(SendReactionRequest(
            peer=peer,
            msg_id=msg_id,
            reaction=[ReactionEmoji(emoticon=reaction)]
        ))
    
    peer_name = getattr(peer, 'username', None) or getattr(peer, 'id', 'unknown')
    
    # Особая обработка для реакций с проверкой лимита
    for attempt in range(FLOOD_WAIT_SETTINGS['max_retries']):
        try:
            if not check_bot_running():
                logger.info(f"Остановка запрошена перед отправкой реакции")
                return None
            
            logger.debug(f"Попытка {attempt + 1} отправки реакции к {peer_name}:{msg_id}")
            return await _send_reaction()
            
        except Exception as e:
            error_str = str(e).lower()
            
            # Проверяем ошибку лимита реакций
            if "reactions_uniq_max" in error_str or "reaction emojis" in error_str:
                logger.warning(f"Достигнут лимит уникальных реакций для поста {msg_id}")
                return None  # Прекращаем попытки
            
            # Обычная обработка FloodWait и других ошибок
            if "flood" in error_str:
                try:
                    wait_time = int(''.join(filter(str.isdigit, str(e))))
                    if wait_time > 0:
                        logger.warning(f"FloodWait при отправке реакции: {wait_time} секунд")
                        if not await smart_wait(wait_time, f"send_reaction_to({peer_name}, {msg_id})"):
                            return None
                        continue
                except:
                    pass
            
            logger.error(f"Ошибка отправки реакции (попытка {attempt + 1}): {e}")
            if attempt < FLOOD_WAIT_SETTINGS['max_retries'] - 1:
                await asyncio.sleep(random.uniform(1, 3))
                continue
            else:
                logger.error(f"Не удалось отправить реакцию после {FLOOD_WAIT_SETTINGS['max_retries']} попыток")
                statistics['errors'] += 1
                return None
    
    return None

async def get_discussion_message_safe(peer, msg_id):
    """Безопасное получение discussion message с обработкой FloodWait"""
    async def _get_discussion_message():
        return await shared_client(GetDiscussionMessageRequest(peer=peer, msg_id=msg_id))
    
    peer_name = getattr(peer, 'username', None) or getattr(peer, 'id', 'unknown')
    return await handle_flood_wait(_get_discussion_message, operation_name=f"get_discussion_message({peer_name}, {msg_id})")

async def iter_messages_safe(entity, limit=None):
    """Безопасная итерация по сообщениям с обработкой FloodWait"""
    messages = []
    try:
        async for message in shared_client.iter_messages(entity, limit=limit):
            messages.append(message)
            # Небольшая задержка между получением сообщений
            await asyncio.sleep(0.1)
    except FloodWaitError as e:
        logger.warning(f"FloodWait при получении сообщений: {e.seconds} секунд")
        if await smart_wait(e.seconds, "iter_messages"):
            # Повторяем попытку после ожидания
            try:
                async for message in shared_client.iter_messages(entity, limit=limit):
                    messages.append(message)
                    await asyncio.sleep(0.1)
            except Exception as retry_error:
                logger.error(f"Ошибка при повторной попытке получения сообщений: {retry_error}")
        else:
            logger.info("Прерываем получение сообщений из-за остановки бота")
    except Exception as e:
        logger.error(f"Ошибка при получении сообщений: {e}")
    
    return messages

async def get_channel_available_reactions(entity) -> List[str]:
    """Получение доступных реакций конкретно в канале"""
    try:
        # Получаем полную информацию о канале с защитой от FloodWait
        full_channel = await get_full_channel_safe(entity)
        if not full_channel:
            logger.warning("Не удалось получить информацию о канале")
            return DEFAULT_POSITIVE_REACTIONS
        
        # Проверяем доступные реакции канала
        if hasattr(full_channel.full_chat, 'available_reactions'):
            available_reactions = full_channel.full_chat.available_reactions
            
            if available_reactions and hasattr(available_reactions, 'reactions'):
                channel_reactions = []
                for reaction in available_reactions.reactions:
                    if hasattr(reaction, 'emoticon'):
                        emoji = reaction.emoticon
                        # Добавляем только положительные эмодзи
                        if emoji in DEFAULT_POSITIVE_REACTIONS:
                            channel_reactions.append(emoji)
                
                if channel_reactions:
                    logger.info(f"Найдено {len(channel_reactions)} доступных положительных реакций в канале")
                    return channel_reactions
        
        # Если не удалось получить реакции канала, используем базовый набор
        logger.info("Используем базовый набор положительных реакций")
        return DEFAULT_POSITIVE_REACTIONS
        
    except Exception as e:
        logger.warning(f"Ошибка получения доступных реакций канала: {e}")
        return DEFAULT_POSITIVE_REACTIONS

async def add_reaction_to_post(message, channel_username):
    """Добавление реакции к посту с полной обработкой FloodWait"""
    try:
        # Проверяем состояние is_running
        if not check_bot_running():
            logger.info("Остановка запрошена, прерываем добавление реакции")
            return False
        
        # Получаем информацию о канале
        entity = await get_entity_safe(message.peer_id)
        if not entity:
            logger.error("Не удалось получить информацию о канале для реакции")
            return False
        
        # Получаем доступные реакции в канале
        available_reactions = await get_channel_available_reactions(entity)
        
        if not available_reactions:
            logger.warning("Нет доступных реакций в канале")
            return False
        
        # Выбираем случайную доступную положительную реакцию
        reaction = random.choice(available_reactions)
       
        # Отправляем реакцию с защитой от FloodWait
        result = await send_reaction_safe(message.peer_id, message.id, reaction)
        
        if result is not None:
            logger.info(f"Поставлена реакция {reaction} к посту {message.id}")
            statistics['reactions_set'] += 1
            
            # Обновляем статистику в bot_interface
            try:
                import bot_interface
                bot_interface.update_statistics(reactions=1)
                bot_interface.add_processed_channel_statistics(channel_username, reaction_added=True)
            except:
                pass
            
            return True
        else:
            logger.warning(f"Не удалось поставить реакцию на пост {message.id}")
            return False
            
    except Exception as e:
        logger.error(f"Критическая ошибка добавления реакции: {e}")
        statistics['errors'] += 1
        return False

async def check_post_comments_available(message) -> bool:
    """Улучшенная проверка доступности комментариев под конкретным постом"""
    try:
        # Сначала проверяем атрибут replies в самом сообщении
        if not hasattr(message, 'replies') or not message.replies:
            logger.debug(f"Пост {message.id} не имеет атрибута replies")
            return False
        
        # Получаем информацию о канале с защитой от FloodWait
        entity = await get_entity_safe(message.peer_id)
        if not entity:
            return False
        
        # Получаем полную информацию о канале с защитой от FloodWait
        full_channel = await get_full_channel_safe(entity)
        if not full_channel:
            return False
        
        # Проверяем, есть ли linked_chat_id
        if hasattr(full_channel.full_chat, 'linked_chat_id') and full_channel.full_chat.linked_chat_id:
            # Дополнительно проверяем, действительно ли можно получить discussion message
            try:
                async def _test_discussion_message():
                    return await shared_client(GetDiscussionMessageRequest(
                        peer=message.peer_id,
                        msg_id=message.id
                    ))
                
                test_discussion = await handle_flood_wait(
                    _test_discussion_message,
                    operation_name="test_discussion_message"
                )
                
                if test_discussion and test_discussion.messages:
                    logger.info(f"Пост {message.id} поддерживает комментарии")
                    return True
                else:
                    logger.info(f"Пост {message.id} не поддерживает комментарии (нет discussion message)")
                    return False
                    
            except Exception as e:
                error_str = str(e).lower()
                if "message id used in the peer was invalid" in error_str:
                    logger.info(f"Пост {message.id} не поддерживает комментарии (недействительный ID)")
                    return False
                else:
                    logger.warning(f"Ошибка проверки discussion message для поста {message.id}: {e}")
                    return False
        else:
            logger.info(f"Канал не имеет группы обсуждений")
            return False
        
    except Exception as e:
        logger.warning(f"Ошибка проверки комментариев поста: {e}")
        return False

async def send_comment_to_post(message, comment_text: str, channel_username: str):
    """ИСПРАВЛЕНИЕ: Отправка комментария к посту с правильной обработкой ошибки вступления в группу"""
    try:
        if not shared_client:
            logger.warning("Telethon клиент не инициализирован")
            return False
        
        # Сначала проверяем, поддерживает ли конкретный пост комментарии
        if not hasattr(message, 'replies') or not message.replies:
            logger.info(f"Пост {message.id} в канале {channel_username} не поддерживает комментарии")
            return False
        
        # Получаем информацию о группе обсуждений
        try:
            async def _get_discussion_message():
                return await shared_client(GetDiscussionMessageRequest(
                    peer=message.peer_id,
                    msg_id=message.id
                ))
            
            discussion_info = await handle_flood_wait(
                _get_discussion_message,
                operation_name="get_discussion_info"
            )
            
            if not discussion_info or not discussion_info.messages:
                logger.warning(f"Не удалось получить информацию о группе обсуждений для поста {message.id} в канале {channel_username}")
                return False
            
            discussion_group = discussion_info.messages[0].peer_id
            reply_to_msg_id = discussion_info.messages[0].id
            
        except Exception as e:
            error_str = str(e).lower()
            
            # Проверяем специфичные ошибки
            if "message id used in the peer was invalid" in error_str:
                logger.info(f"Пост {message.id} в канале {channel_username} не доступен для комментирования (недействительный ID)")
                return False
            elif "msg_id invalid" in error_str:
                logger.info(f"Недействительный ID сообщения {message.id} в канале {channel_username}")
                return False
            elif "peer_id_invalid" in error_str:
                logger.warning(f"Недействительный peer_id для канала {channel_username}")
                return False
            else:
                logger.error(f"Ошибка получения информации о группе обсуждений для поста {message.id}: {e}")
                return False
        
        # Функция для отправки комментария
        async def _send_comment():
            return await shared_client.send_message(
                discussion_group,
                message=comment_text,
                reply_to=reply_to_msg_id
            )
        
        # ИСПРАВЛЕНИЕ: Сначала пробуем отправить комментарий один раз
        try:
            comment = await handle_flood_wait(
                _send_comment,
                operation_name="send_comment"
            )
            
            if comment:
                logger.info(f"✅ Комментарий успешно отправлен к посту {message.id} в {channel_username}")
                statistics['comments_sent'] += 1
                
                # Обновляем статистику в bot_interface
                try:
                    import bot_interface
                    bot_interface.update_statistics(comments=1)
                    comment_link = f"https://t.me/{channel_username.replace('@', '')}/{message.id}"
                    post_link = f"https://t.me/{channel_username.replace('@', '')}/{message.id}"
                    bot_interface.add_processed_channel_statistics(channel_username, comment_link=comment_link, post_link=post_link)
                except:
                    pass
                
                return True
                
        except Exception as e:
            error_str = str(e).lower()
            logger.warning(f"Ошибка при отправке комментария в {channel_username}: {e}")
            
            # ИСПРАВЛЕНИЕ: Улучшенная проверка ошибки вступления в группу
            join_required_patterns = [
                "you join the discussion group before commenting",
                "join the discussion group before commenting", 
                "must join the discussion group",
                "need to join the discussion group",
                "must join",
                "need to join"
            ]
            
            requires_join = any(pattern in error_str for pattern in join_required_patterns)
            
            if requires_join:
                logger.info(f"🔄 Требуется вступить в группу обсуждений для {channel_username}")
                
                # Пытаемся вступить в группу обсуждений
                try:
                    # Получаем информацию о канале
                    channel_entity = await handle_flood_wait(
                        lambda: shared_client.get_entity(message.peer_id),
                        operation_name="get_channel_entity_for_join"
                    )
                    
                    if not channel_entity:
                        logger.error(f"❌ Не удалось получить сущность канала для {channel_username}")
                        return False
                    
                    # Получаем полную информацию о канале
                    full_channel = await handle_flood_wait(
                        lambda: shared_client(GetFullChannelRequest(channel=channel_entity)),
                        operation_name="get_full_channel_for_join"
                    )
                    
                    if not full_channel or not hasattr(full_channel.full_chat, 'linked_chat_id') or not full_channel.full_chat.linked_chat_id:
                        logger.error(f"❌ Канал {channel_username} не имеет связанной группы обсуждений")
                        return False
                    
                    # Получаем сущность группы обсуждений
                    discussion_group_entity = await handle_flood_wait(
                        lambda: shared_client.get_entity(full_channel.full_chat.linked_chat_id),
                        operation_name="get_discussion_group_entity"
                    )
                    
                    if not discussion_group_entity:
                        logger.error(f"❌ Не удалось получить сущность группы обсуждений для {channel_username}")
                        return False
                    
                    # Вступаем в группу обсуждений
                    join_result = await handle_flood_wait(
                        lambda: shared_client(JoinChannelRequest(discussion_group_entity)),
                        operation_name="join_discussion_group"
                    )
                    
                    if join_result is None:
                        logger.error(f"❌ Не удалось вступить в группу обсуждений для {channel_username}")
                        return False
                    
                    logger.info(f"✅ Успешно вступили в группу обсуждений для {channel_username}")
                    
                    # Небольшая пауза после вступления
                    await asyncio.sleep(2)
                    
                    # ИСПРАВЛЕНИЕ: Повторяем попытку отправки комментария ТОЛЬКО ОДИН РАЗ
                    try:
                        comment = await handle_flood_wait(
                            _send_comment,
                            operation_name="send_comment_after_join"
                        )
                        
                        if comment:
                            logger.info(f"✅ Комментарий успешно отправлен после вступления в группу {channel_username}")
                            statistics['comments_sent'] += 1
                            
                            # Обновляем статистику в bot_interface
                            try:
                                import bot_interface
                                bot_interface.update_statistics(comments=1)
                                comment_link = f"https://t.me/{channel_username.replace('@', '')}/{message.id}"
                                post_link = f"https://t.me/{channel_username.replace('@', '')}/{message.id}"
                                bot_interface.add_processed_channel_statistics(channel_username, comment_link=comment_link, post_link=post_link)
                            except:
                                pass
                            
                            return True
                        else:
                            logger.error(f"❌ Не удалось отправить комментарий даже после вступления в группу {channel_username}")
                            return False
                            
                    except Exception as retry_error:
                        logger.error(f"❌ Ошибка при повторной отправке комментария после вступления в группу {channel_username}: {retry_error}")
                        return False
                        
                except Exception as join_error:
                    logger.error(f"❌ Критическая ошибка при попытке вступить в группу обсуждений для {channel_username}: {join_error}")
                    return False
            
            # Проверяем другие специфичные ошибки
            elif "message id used in the peer was invalid" in error_str:
                logger.warning(f"❌ Пост {message.id} больше не доступен для комментирования в {channel_username}")
                return False
            elif "chat_write_forbidden" in error_str:
                logger.warning(f"❌ Запрещена запись в группу обсуждений канала {channel_username}")
                return False
            elif "user_banned_in_channel" in error_str:
                logger.warning(f"❌ Пользователь заблокирован в канале/группе {channel_username}")
                return False
            else:
                logger.error(f"❌ Неизвестная ошибка при отправке комментария в {channel_username}: {e}")
                return False
        
        return False
                
    except Exception as e:
        logger.error(f"💥 Критическая ошибка при отправке комментария: {e}")
        return False

# ИСПРАВЛЕНИЕ: Обновленная функция сохранения прогресса (исключаем объекты сообщений)
async def save_masslooking_progress():
    """Сохранение прогресса масслукинга в базу данных (только сериализуемые данные)"""
    try:
        from database import db
        
        # ИСПРАВЛЕНИЕ: подготавливаем данные для сохранения (исключаем entity)
        serializable_queue = {}
        for username, data in channel_processing_queue.items():
            serializable_queue[username] = {
                'entity_id': data.get('entity_id'),
                'entity_username': data.get('entity_username'),
                'message_ids': data.get('message_ids', []),
                'total_posts': data.get('total_posts', 0),
                'posts_processed': data.get('posts_processed', 0),
                'last_processed': data.get('last_processed').isoformat() if data.get('last_processed') else None,
                'actions_performed': data.get('actions_performed', False),
                'found_topic': data.get('found_topic', 'Другое')
            }
        
        # ИСПРАВЛЕНИЕ: подготавливаем данные отслеживаемых каналов (только ID)
        serializable_tracked = {}
        for username, data in tracked_channels.items():
            serializable_tracked[username] = {
                'entity_id': data.get('entity_id'),
                'last_message_id': data.get('last_message_id', 0)
            }
        
        progress_data = [
            ('masslooking_progress', masslooking_progress),
            ('processed_channels', list(processed_channels)),
            ('channel_processing_queue', serializable_queue),  # ИСПРАВЛЕНИЕ: сериализуемая версия
            ('tracked_channels', serializable_tracked)  # ИСПРАВЛЕНИЕ: сериализуемая версия
        ]
        
        for key, value in progress_data:
            await db.save_bot_state(key, value)
            
        logger.debug("Прогресс масслукинга сохранен")
    except Exception as e:
        logger.error(f"Ошибка сохранения прогресса масслукинга: {e}")

# ИСПРАВЛЕНИЕ: Обновленная функция загрузки прогресса
async def load_masslooking_progress():
    """Загрузка прогресса масслукинга из базы данных"""
    global masslooking_progress, processed_channels, channel_processing_queue, tracked_channels
    try:
        from database import db
        
        # Загружаем прогресс масслукинга
        saved_progress = await db.load_bot_state('masslooking_progress', {})
        if saved_progress:
            masslooking_progress.update(saved_progress)
        
        # Загружаем обработанные каналы
        saved_channels = await db.load_bot_state('processed_channels', [])
        if saved_channels:
            processed_channels.update(saved_channels)
        
        # ИСПРАВЛЕНИЕ: загружаем очередь обработки каналов (без entity объектов)
        saved_queue = await db.load_bot_state('channel_processing_queue', {})
        if saved_queue:
            for username, data in saved_queue.items():
                # Восстанавливаем данные, но без entity (будем получать заново)
                channel_processing_queue[username] = {
                    'entity_id': data.get('entity_id'),
                    'entity_username': data.get('entity_username', username),
                    'message_ids': data.get('message_ids', []),
                    'total_posts': data.get('total_posts', 0),
                    'posts_processed': data.get('posts_processed', 0),
                    'last_processed': datetime.fromisoformat(data['last_processed']) if data.get('last_processed') else None,
                    'actions_performed': data.get('actions_performed', False),
                    'found_topic': data.get('found_topic', 'Другое')
                }
        
        # ИСПРАВЛЕНИЕ: загружаем отслеживаемые каналы (только ID)
        saved_tracked = await db.load_bot_state('tracked_channels', {})
        if saved_tracked:
            for username, data in saved_tracked.items():
                tracked_channels[username] = {
                    'entity_id': data.get('entity_id'),
                    'last_message_id': data.get('last_message_id', 0)
                }
        
        logger.info(f"Загружен прогресс масслукинга: {masslooking_progress}")
        logger.info(f"Загружено обработанных каналов: {len(processed_channels)}")
        logger.info(f"Загружено каналов в очереди: {len(channel_processing_queue)}")
        logger.info(f"Загружено отслеживаемых каналов: {len(tracked_channels)}")
    except Exception as e:
        logger.error(f"Ошибка загрузки прогресса масслукинга: {e}")

# ИСПРАВЛЕНИЕ: Функция подготовки канала с сохранением только ID сообщений
async def prepare_channel_for_processing(username: str):
    """ИСПРАВЛЕНИЕ: Подготовка канала к обработке с сохранением только ID сообщений"""
    try:
        # Проверяем, не обрабатывается ли уже этот канал
        if username in channel_processing_queue:
            logger.info(f"Канал {username} уже в очереди обработки")
            return False
        
        # ИСПРАВЛЕНИЕ: проверяем, не был ли канал уже ПОЛНОСТЬЮ обработан
        if username in processed_channels:
            logger.info(f"Канал {username} уже был полностью обработан")
            return False
        
        # Получаем сущность канала БЕЗ подписки
        entity = await get_entity_safe(username)
        if not entity:
            logger.warning(f"Не удалось получить сущность канала {username}")
            return False
        
        # Проверяем доступность комментариев
        full_channel = await get_full_channel_safe(entity)
        if not full_channel:
            logger.warning(f"Не удалось получить полную информацию о канале {username}")
            return False
        
        # Проверяем, есть ли группа обсуждений (комментарии)
        if not (hasattr(full_channel.full_chat, 'linked_chat_id') and full_channel.full_chat.linked_chat_id):
            logger.info(f"Канал {username} не имеет группы обсуждений - комментарии недоступны")
            return False
        
        logger.info(f"Канал {username} имеет группу обсуждений - подходит для нейрокомментинга")
        
        # Добавляем задержку перед подпиской (учитываем флаг первого действия)
        delay_success = await apply_subscription_delay(username, "подписки")
        if not delay_success:
            return False
        
        # Подписываемся на канал
        join_result = await join_channel_safe(entity)
        if join_result is None:
            logger.warning(f"Не удалось вступить в канал {username}")
            return False
        
        logger.info(f"Успешно подписались на канал {username}")
        
        # ИСПРАВЛЕНИЕ: получаем сообщения и сохраняем только их ID
        try:
            posts_range = settings.get('posts_range', (1, 5))
            limit = posts_range[1] if isinstance(posts_range, (list, tuple)) and len(posts_range) >= 2 else 5
            
            logger.info(f"🔍 Получаем до {limit * 3} сообщений из канала {username} для фильтрации")
            
            message_ids = []  # ИСПРАВЛЕНИЕ: сохраняем только ID
            message_count = 0
            valid_message_count = 0
            
            async for message in shared_client.iter_messages(entity, limit=limit * 3):
                message_count += 1
                
                # Проверяем наличие ID и подходящего контента
                if hasattr(message, 'id') and message.id and has_commentable_content(message):
                    message_ids.append(message.id)  # ИСПРАВЛЕНИЕ: сохраняем только ID
                    valid_message_count += 1
                    logger.debug(f"✅ Сообщение {message.id} подходит для комментирования")
                    
                    if len(message_ids) >= limit:  # Достигли нужного количества
                        break
                else:
                    msg_id = getattr(message, 'id', 'NO_ID')
                    logger.debug(f"❌ Сообщение {msg_id} пропущено (нет ID или контента)")
            
            logger.info(f"📊 Статистика получения сообщений из {username}:")
            logger.info(f"  Всего получено: {message_count}")
            logger.info(f"  Валидных для комментирования: {valid_message_count}")
            logger.info(f"  ID добавлено в очередь: {len(message_ids)}")
            
            if not message_ids:
                logger.warning(f"❌ В канале {username} нет подходящих сообщений для комментирования")
                await leave_channel_safe(entity)
                return False
            
            logger.info(f"✅ Найдено {len(message_ids)} подходящих сообщений для обработки в канале {username}")
            
            # ИСПРАВЛЕНИЕ: сохраняем entity отдельно (для восстановления) и список ID
            channel_processing_queue[username] = {
                'entity_id': entity.id,  # ID сущности для повторного получения
                'entity_username': username,  # username для получения entity
                'message_ids': message_ids,  # ИСПРАВЛЕНИЕ: только ID сообщений
                'total_posts': len(message_ids),
                'posts_processed': 0,
                'last_processed': None,
                'actions_performed': False
            }
            
            # Получаем тему канала из статистики
            try:
                import bot_interface
                channel_data = bot_interface.bot_data['detailed_statistics']['processed_channels'].get(username, {})
                found_topic = channel_data.get('found_topic', 'Другое')
                channel_processing_queue[username]['found_topic'] = found_topic
            except Exception as e:
                logger.error(f"Ошибка получения темы канала из статистики: {e}")
                channel_processing_queue[username]['found_topic'] = 'Другое'
            
            logger.info(f"✅ Канал {username} успешно подготовлен для обработки")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка при получении сообщений канала {username}: {e}")
            await leave_channel_safe(entity)
            return False
        
    except Exception as e:
        logger.error(f"❌ Ошибка подготовки канала {username}: {e}")
        return False

async def process_single_post_from_channel(username: str) -> bool:
    """ИСПРАВЛЕНИЕ: Обработка одного поста с получением сообщения по ID"""
    try:
        if username not in channel_processing_queue:
            logger.warning(f"Канал {username} не найден в очереди обработки")
            return False
        
        channel_data = channel_processing_queue[username]
        
        # Проверяем, остались ли необработанные посты
        if channel_data['posts_processed'] >= channel_data['total_posts']:
            logger.info(f"Все посты канала {username} обработаны")
            return False
        
        # Проверяем подписку перед обработкой поста
        subscription_ok = await ensure_subscription(username)
        if not subscription_ok:
            logger.error(f"Не удалось обеспечить подписку на канал {username}")
            return False
        
        # ИСПРАВЛЕНИЕ: получаем ID текущего сообщения и загружаем само сообщение
        current_message_id = channel_data['message_ids'][channel_data['posts_processed']]
        
        # Обновляем прогресс обработки
        masslooking_progress['current_channel'] = username
        masslooking_progress['processed_count'] = channel_data['posts_processed']
        
        logger.info(f"🎯 Получаем сообщение {current_message_id} из канала {username}")
        
        # ИСПРАВЛЕНИЕ: получаем сущность канала заново
        try:
            entity = await get_entity_safe(username)
            if not entity:
                logger.error(f"❌ Не удалось получить сущность канала {username}")
                channel_data['posts_processed'] += 1
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка получения сущности канала {username}: {e}")
            channel_data['posts_processed'] += 1
            return True
        
        # ИСПРАВЛЕНИЕ: получаем конкретное сообщение по ID
        current_post = None
        try:
            # Получаем сообщение по ID через get_messages
            messages = await shared_client.get_messages(entity, ids=current_message_id)
            if messages and len(messages) > 0:
                current_post = messages[0]
                logger.debug(f"✅ Сообщение {current_message_id} успешно получено")
            else:
                logger.warning(f"⚠️ Сообщение {current_message_id} не найдено")
                channel_data['posts_processed'] += 1
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка получения сообщения {current_message_id}: {e}")
            channel_data['posts_processed'] += 1
            return True
        
        # Проверяем валидность полученного сообщения
        if not current_post:
            logger.warning(f"⚠️ Получен пустой объект сообщения {current_message_id} в канале {username}")
            channel_data['posts_processed'] += 1
            return True
        
        if not hasattr(current_post, 'id') or current_post.id != current_message_id:
            logger.warning(f"⚠️ ID сообщения не совпадает: ожидали {current_message_id}, получили {getattr(current_post, 'id', 'NO_ID')}")
            channel_data['posts_processed'] += 1
            return True
       
        # ИСПРАВЛЕНИЕ: проверяем, что сообщение все еще подходит для комментирования
        if not has_commentable_content(current_post):
            logger.info(f"Сообщение {current_message_id} в канале {username} больше не подходит для комментирования")
            channel_data['posts_processed'] += 1
            return True
        
        # Извлекаем текст из сообщения
        post_text = extract_message_text(current_post)
        if not post_text:
            logger.info(f"Пост {current_message_id} в канале {username} не содержит текста для комментирования")
            channel_data['posts_processed'] += 1
            return True
        
        logger.info(f"🎯 Обрабатываем пост {current_message_id} в канале {username} (текст: {len(post_text)} символов)")
        
        # Используем сохраненную тему канала для генерации комментария
        channel_topic = channel_data.get('found_topic', 'Другое')
        
        comment_sent = False
        reaction_added = False
        actions_performed = False
        
        # Генерируем комментарий
        try:
            comment = await generate_comment(post_text, [channel_topic], current_post)
            
            if comment:
                # Проверяем доступность комментариев для конкретного поста
                comments_available = await check_post_comments_available(current_post)
                
                if comments_available:
                    logger.info(f"📝 Отправляем комментарий к посту {current_message_id} в канале {username}")
                    comment_sent = await send_comment_to_post(current_post, comment, username)
                    if comment_sent:
                        logger.info(f"✅ Комментарий успешно отправлен к посту {current_message_id} в канале {username}")
                        actions_performed = True
                        statistics['comments_sent'] += 1
                        
                        # Обновляем статистику в bot_interface
                        try:
                            import bot_interface
                            bot_interface.update_statistics(comments=1)
                            comment_link = f"https://t.me/{username.replace('@', '')}/{current_message_id}"
                            post_link = f"https://t.me/{username.replace('@', '')}/{current_message_id}"
                            bot_interface.add_processed_channel_statistics(username, comment_link=comment_link, post_link=post_link)
                        except Exception as e:
                            logger.debug(f"Ошибка обновления статистики bot_interface: {e}")
                    else:
                        logger.warning(f"❌ Не удалось отправить комментарий к посту {current_message_id} в канале {username}")
                else:
                    logger.info(f"Пост {current_message_id} в канале {username} не поддерживает комментарии")
            else:
                logger.warning(f"Не удалось сгенерировать комментарий для поста {current_message_id} в канале {username}")
        except Exception as e:
            logger.error(f"Ошибка при обработке комментария для поста {current_message_id} в канале {username}: {e}")
        
        # Добавляем реакцию
        try:
            logger.info(f"👍 Добавляем реакцию к посту {current_message_id} в канале {username}")
            reaction_added = await add_reaction_to_post(current_post, username)
            if reaction_added:
                logger.info(f"✅ Реакция добавлена к посту {current_message_id} в канале {username}")
                actions_performed = True
                statistics['reactions_set'] += 1
                
                # Обновляем статистику в bot_interface
                try:
                    import bot_interface
                    bot_interface.update_statistics(reactions=1)
                except Exception as e:
                    logger.debug(f"Ошибка обновления статистики bot_interface: {e}")
            else:
                logger.warning(f"❌ Не удалось добавить реакцию к посту {current_message_id} в канале {username}")
        except Exception as e:
            logger.error(f"Ошибка при добавлении реакции к посту {current_message_id} в канале {username}: {e}")
        
        # Обновляем счетчик обработанных постов
        channel_data['posts_processed'] += 1
        channel_data['last_processed'] = datetime.now()
        
        # Обновляем флаг выполненных действий
        if actions_performed:
            channel_data['actions_performed'] = True
            logger.info(f"📝 Выполнены действия для поста {current_message_id} в канале {username} (комментарий: {comment_sent}, реакция: {reaction_added})")
        else:
            logger.warning(f"⚠️ Не выполнено ни одного действия для поста {current_message_id} в канале {username}")
        
        # Сохраняем прогресс
        await save_masslooking_progress()
        
        return True
        
    except Exception as e:
        logger.error(f"💥 Критическая ошибка обработки поста из канала {username}: {e}")
        statistics['errors'] += 1
        return False

# ИСПРАВЛЕНИЕ: Обновленная функция финализации
async def finalize_channel_processing(username: str):
    """Завершение обработки канала с правильной обработкой entity"""
    try:
        channel_data = channel_processing_queue.get(username)
        if not channel_data:
            logger.warning(f"Данные канала {username} не найдены при финализации")
            return
        
        actions_performed = channel_data.get('actions_performed', False)
        
        if not actions_performed:
            logger.warning(f"В канале {username} не было выполнено ни одного действия (комментарий/реакция)")
        else:
            processed_channels.add(username)
            statistics['channels_processed'] += 1
            logger.info(f"Канал {username} добавлен в список полностью обработанных каналов (выполнены действия)")
            
            # Обновляем статистику только после фактической обработки
            try:
                import bot_interface
                bot_interface.bot_data['detailed_statistics']['processed_channels'][username] = {
                    'processed_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'actions_performed': True
                }
                bot_interface.update_statistics(channels=1)
                await bot_interface.save_bot_state()
            except:
                pass
        
        # Проверяем настройки отслеживания новых постов
        track_new_posts = False
        try:
            import bot_interface
            track_new_posts = bot_interface.bot_data['settings'].get('track_new_posts', False)
        except:
            pass

        # ИСПРАВЛЕНИЕ: получаем сущность канала заново для отписки/отслеживания
        try:
            entity = await get_entity_safe(username)
            if not entity:
                logger.warning(f"Не удалось получить сущность канала {username} для финализации")
            else:
                # Если отслеживание новых постов включено И были выполнены действия, добавляем канал в отслеживаемые
                if track_new_posts and actions_performed:
                    logger.info(f"Добавляем канал {username} в отслеживание новых постов")
                    # Получаем последний ID сообщения
                    try:
                        messages = await iter_messages_safe(entity, limit=1)
                        last_message_id = messages[0].id if messages else 0
                        tracked_channels[username] = {
                            'entity_id': entity.id,
                            'last_message_id': last_message_id
                        }
                        logger.info(f"Канал {username} добавлен в отслеживание (последний пост: {last_message_id})")
                    except Exception as e:
                        logger.error(f"Ошибка добавления канала {username} в отслеживание: {e}")
                else:
                    # Отписываемся от канала если не отслеживаем новые посты ИЛИ не были выполнены действия
                    reason = "track_new_posts = False" if not track_new_posts else "не выполнены действия"
                    logger.info(f"Отписываемся от канала {username} ({reason})")
                    await leave_channel_safe(entity)
        except Exception as e:
            logger.error(f"Ошибка получения entity для финализации канала {username}: {e}")

        # Удаляем канал из очереди обработки
        if username in channel_processing_queue:
            del channel_processing_queue[username]
        
        # Сохраняем прогресс
        await save_masslooking_progress()
        
    except Exception as e:
        logger.error(f"Ошибка при финализации канала {username}: {e}")
        statistics['errors'] += 1

async def check_new_posts_in_tracked_channels():
    """Проверка новых постов в отслеживаемых каналах"""
    global tracked_channels
    
    if not new_post_tracking_active or not tracked_channels:
        return
    
    logger.info(f"Проверяем новые посты в {len(tracked_channels)} отслеживаемых каналах")
    
    for username, channel_data in list(tracked_channels.items()):
        try:
            if not check_bot_running():
                logger.info("Остановка запрошена, прерываем проверку новых постов")
                break
            
            # ИСПРАВЛЕНИЕ: получаем entity заново по ID или username
            entity = None
            try:
                entity_id = channel_data.get('entity_id')
                if entity_id:
                    entity = await get_entity_safe(entity_id)
                if not entity:
                    entity = await get_entity_safe(username)
                
                if not entity:
                    logger.error(f"Не удалось получить entity для отслеживаемого канала {username}")
                    continue
            except Exception as e:
                logger.error(f"Ошибка получения entity для канала {username}: {e}")
                continue
            
            last_known_id = channel_data['last_message_id']
            
            # Получаем новые сообщения
            try:
                new_messages = []
                async for message in shared_client.iter_messages(entity, min_id=last_known_id, limit=10):
                    # ИСПРАВЛЕНИЕ: используем улучшенную функцию проверки контента
                    if has_commentable_content(message):
                        new_messages.append(message)
                
                if new_messages:
                    logger.info(f"Найдено {len(new_messages)} новых постов в канале {username}")
                    
                    # Обрабатываем новые посты
                    for message in reversed(new_messages):  # От старых к новым
                        try:
                            # ИСПРАВЛЕНИЕ: правильное получение ID поста
                            message_id = getattr(message, 'id', None)
                            if message_id is None:
                                logger.warning(f"Не удалось получить ID нового поста в канале {username}")
                                continue
                            
                            # ИСПРАВЛЕНИЕ: используем улучшенную функцию извлечения текста
                            post_text = extract_message_text(message)
                            
                            # Если нет текста, но есть медиа, создаем описание для комментария
                            if not post_text and hasattr(message, 'media') and message.media:
                                if hasattr(message.media, 'photo'):
                                    post_text = "Интересное фото"
                                elif hasattr(message.media, 'document'):
                                    post_text = "Полезный материал"
                                else:
                                    post_text = "Интересный контент"
                            
                            if not post_text:
                                logger.warning(f"Новый пост {message_id} в канале {username} не содержит контента для комментирования")
                                continue
                            
                            # Получаем тему канала
                            channel_topic = 'Другое'
                            try:
                                import bot_interface
                                channel_data_stats = bot_interface.bot_data['detailed_statistics']['processed_channels'].get(username, {})
                                channel_topic = channel_data_stats.get('found_topic', 'Другое')
                            except:
                                pass
                            
                            # Генерируем комментарий
                            comment = await generate_comment(post_text, [channel_topic], message, entity)
                            
                            if comment:
                                # Отправляем комментарий
                                comment_sent = await send_comment_to_post(message, comment, username)
                                if comment_sent:
                                    logger.info(f"Комментарий к новому посту {message_id} отправлен в канал {username}")
                                    statistics['comments_sent'] += 1
                                    
                                    # Обновляем статистику в bot_interface
                                    try:
                                        import bot_interface
                                        bot_interface.update_statistics(comments=1)
                                        comment_link = f"https://t.me/{username.replace('@', '')}/{message_id}"
                                        post_link = f"https://t.me/{username.replace('@', '')}/{message_id}"
                                        bot_interface.add_processed_channel_statistics(username, comment_link=comment_link, post_link=post_link)
                                    except:
                                        pass
                            
                            # Добавляем реакцию
                            reaction_added = await add_reaction_to_post(message, username)
                            if reaction_added:
                                logger.info(f"Реакция к новому посту {message_id} добавлена в канале {username}")
                                statistics['reactions_set'] += 1
                                
                                # Обновляем статистику в bot_interface
                                try:
                                    import bot_interface
                                    bot_interface.update_statistics(reactions=1)
                                except:
                                    pass
                            
                            # Применяем задержку между постами
                            delay_range = settings.get('delay_range', (20, 1000))
                            if delay_range != (0, 0):
                                delay = random.uniform(delay_range[0], delay_range[1])
                                await asyncio.sleep(delay)
                            
                        except Exception as e:
                            logger.error(f"Ошибка обработки нового поста {message_id} в канале {username}: {e}")
                            continue
                    
                    # Обновляем последний ID сообщения
                    tracked_channels[username]['last_message_id'] = new_messages[0].id
                    
                    # Сохраняем прогресс
                    await save_masslooking_progress()
                
            except Exception as e:
                logger.error(f"Ошибка получения новых сообщений для канала {username}: {e}")
                continue
                
        except Exception as e:
            logger.error(f"Ошибка проверки новых постов в канале {username}: {e}")
            continue
    
    logger.info("Проверка новых постов завершена")

async def new_post_tracking_worker():
    """Рабочий процесс отслеживания новых постов"""
    global new_post_tracking_active
    
    logger.info("Запущен worker отслеживания новых постов")
    
    while new_post_tracking_active:
        try:
            if not check_bot_running():
                logger.info("Остановка запрошена, завершаем отслеживание новых постов")
                new_post_tracking_active = False
                break
            
            await check_new_posts_in_tracked_channels()
            
            # Ждем 5 минут между проверками
            for _ in range(300):  # 5 минут
                if not check_bot_running() or not new_post_tracking_active:
                    break
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Ошибка в worker отслеживания новых постов: {e}")
            await asyncio.sleep(60)  # Ждем минуту при ошибке
    
    logger.info("Worker отслеживания новых постов завершен")

async def start_new_post_tracking():
    """Запуск отслеживания новых постов"""
    global new_post_tracking_active
    
    if new_post_tracking_active:
        logger.info("Отслеживание новых постов уже активно")
        return
    
    new_post_tracking_active = True
    logger.info("Запуск отслеживания новых постов")
    
    # Запускаем worker в фоне
    asyncio.create_task(new_post_tracking_worker())
    logger.info("Отслеживание новых постов запущено")

async def stop_new_post_tracking():
    """Остановка отслеживания новых постов"""
    global new_post_tracking_active
    
    new_post_tracking_active = False
    logger.info("Отслеживание новых постов остановлено")

async def masslooking_worker():
    """Рабочий процесс масслукинга с круговой обработкой каналов"""
    global masslooking_active, current_channel_iterator, channels_in_rotation, settings
    
    # Загружаем прогресс при запуске
    await load_masslooking_progress()
    logger.info("Рабочий процесс масслукинга запущен (круговая обработка)")
    
    # Для отслеживания текущей задержки
    current_delay = None
    delay_start_time = None
    
    def validate_delay_range(delay_range):
        """Проверка валидности диапазона задержки"""
        try:
            if not isinstance(delay_range, (list, tuple)) or len(delay_range) != 2:
                return False
            min_delay, max_delay = delay_range
            if not isinstance(min_delay, (int, float)) or not isinstance(max_delay, (int, float)):
                return False
            if min_delay < 0 or max_delay < 0:
                return False
            if min_delay > max_delay:
                return False
            return True
        except Exception:
            return False
    
    def calculate_new_delay(old_delay_range, new_delay_range, elapsed_time, current_delay):
        """Расчет новой задержки с сохранением пропорции"""
        try:
            if not validate_delay_range(old_delay_range) or not validate_delay_range(new_delay_range):
                logger.warning("Некорректный диапазон задержки, используем новый случайный")
                return random.uniform(new_delay_range[0], new_delay_range[1])
            
            # Находим, какую часть от старого диапазона составляла текущая задержка
            old_range = old_delay_range[1] - old_delay_range[0]
            if old_range <= 0:
                return random.uniform(new_delay_range[0], new_delay_range[1])
            
            # Вычисляем оставшееся время
            remaining_delay = max(0, current_delay - elapsed_time)
            
            # Находим пропорцию оставшегося времени относительно старого диапазона
            if current_delay <= 0:
                proportion = 0
            else:
                proportion = remaining_delay / current_delay
            
            # Применяем ту же пропорцию к новому диапазону
            new_range = new_delay_range[1] - new_delay_range[0]
            new_delay = new_delay_range[0] + (new_range * proportion)
            
            # Проверяем границы
            new_delay = max(new_delay_range[0], min(new_delay, new_delay_range[1]))
            
            return new_delay
            
        except Exception as e:
            logger.error(f"Ошибка расчета новой задержки: {e}")
            return random.uniform(new_delay_range[0], new_delay_range[1])
    
    while masslooking_active:
        try:
            # Проверяем состояние is_running
            if not check_bot_running():
                logger.info("Остановка запрошена в bot_interface, завершаем масслукинг")
                masslooking_active = False
                break
            
            # Обновляем настройки в реальном времени
            try:
                import bot_interface
                new_settings = bot_interface.get_bot_settings()
                if new_settings != settings:
                    old_delay_range = settings.get('delay_range', (20, 1000))
                    settings.update(new_settings)
                    logger.info("Настройки масслукинга обновлены в реальном времени")
                    
                    # Если изменился диапазон задержки и есть активная задержка
                    new_delay_range = settings.get('delay_range', (20, 1000))
                    if (old_delay_range != new_delay_range and 
                        current_delay is not None and delay_start_time is not None):
                        try:
                            # Вычисляем оставшееся время задержки
                            elapsed_time = time.time() - delay_start_time
                            
                            # Рассчитываем новую задержку с сохранением пропорции
                            new_delay = calculate_new_delay(
                                old_delay_range,
                                new_delay_range,
                                elapsed_time,
                                current_delay
                            )
                            
                            current_delay = new_delay
                            delay_start_time = time.time()
                            logger.info(f"Задержка обновлена до {current_delay:.1f}с")
                        except Exception as e:
                            logger.error(f"Ошибка при обновлении задержки: {e}")
            except Exception as e:
                logger.warning(f"Не удалось обновить настройки: {e}")
            
            # Обновляем список каналов в ротации
            channels_in_rotation = list(channel_processing_queue.keys())
            
            # Если нет каналов в обработке, ждем новые
            if not channels_in_rotation:
                logger.debug("Нет каналов в обработке, ожидаем...")
                await asyncio.sleep(5)
                continue
            
            # Создаем итератор для круговой обработки, если его нет
            if current_channel_iterator is None:
                current_channel_iterator = iter(channels_in_rotation)
            
            try:
                # Получаем следующий канал из ротации
                current_channel = next(current_channel_iterator)
            except StopIteration:
                # Если итератор закончился, создаем новый (начинаем сначала)
                current_channel_iterator = iter(channels_in_rotation)
                if channels_in_rotation:  # Проверяем, что список не пуст
                    current_channel = next(current_channel_iterator)
                else:
                    continue
            
            # Проверяем, что канал все еще в очереди
            if current_channel not in channel_processing_queue:
                # Канал был удален, обновляем итератор
                current_channel_iterator = None
                continue
            
            logger.info(f"Обрабатываем следующий пост из канала: {current_channel}")
            
            # ИСПРАВЛЕНИЕ: проверяем лимит ПОЛНОСТЬЮ ОБРАБОТАННЫХ каналов (с фактически выполненными действиями)
            max_channels = settings.get('max_channels', 150)
            if max_channels != float('inf') and len(processed_channels) >= max_channels:
                logger.info(f"Достигнут лимит полностью обработанных каналов: {max_channels} (обработано: {len(processed_channels)})")
                # Завершаем все активные каналы
                for channel in list(channel_processing_queue.keys()):
                    await finalize_channel_processing(channel)
                masslooking_active = False
                break
            
            # Обрабатываем один пост из текущего канала
            post_processed = await process_single_post_from_channel(current_channel)
            
            # Проверяем, завершена ли обработка этого канала
            if current_channel in channel_processing_queue:
                channel_data = channel_processing_queue[current_channel]
                if channel_data['posts_processed'] >= channel_data['total_posts']:
                    logger.info(f"Канал {current_channel} полностью обработан")
                    await finalize_channel_processing(current_channel)
                    # Сбрасываем итератор, так как список каналов изменился
                    current_channel_iterator = None
            
            # Задержка между действиями
            delay_range = settings.get('delay_range', (20, 1000))
            if delay_range != (0, 0):
                try:
                    if not validate_delay_range(delay_range):
                        logger.warning(f"Некорректный диапазон задержки {delay_range}, используем дефолтный (20, 1000)")
                        delay_range = (20, 1000)
                    
                    current_delay = random.uniform(delay_range[0], delay_range[1])
                    delay_start_time = time.time()
                    logger.info(f"Задержка {current_delay:.1f} секунд перед следующим действием")
                    
                    # Разбиваем задержку на части с проверкой состояния
                    delay_chunks = int(current_delay)
                    remaining_delay = current_delay - delay_chunks
                    
                    for _ in range(delay_chunks):
                        if not check_bot_running():
                            logger.info("Остановка запрошена во время задержки")
                            masslooking_active = False
                            break
                        
                        # Проверяем обновление настроек
                        try:
                            import bot_interface
                            new_settings = bot_interface.get_bot_settings()
                            if new_settings != settings:
                                new_delay_range = new_settings.get('delay_range', (20, 1000))
                                if new_delay_range != delay_range:
                                    # Рассчитываем новую задержку
                                    elapsed_time = time.time() - delay_start_time
                                    new_delay = calculate_new_delay(
                                        delay_range,
                                        new_delay_range,
                                        elapsed_time,
                                        current_delay
                                    )
                                    
                                    current_delay = new_delay
                                    delay_start_time = time.time()
                                    logger.info(f"Задержка обновлена до {current_delay:.1f}с")
                                    break
                        except Exception as e:
                            logger.warning(f"Не удалось проверить обновление настроек: {e}")
                        
                        await asyncio.sleep(1)
                    
                    if remaining_delay > 0 and masslooking_active:
                        await asyncio.sleep(remaining_delay)
                except Exception as e:
                    logger.error(f"Ошибка при обработке задержки: {e}")
                    await asyncio.sleep(20)  # Используем минимальную безопасную задержку
            
        except Exception as e:
            logger.error(f"Ошибка в рабочем процессе масслукинга: {e}")
            await asyncio.sleep(30)
    
    logger.info("Рабочий процесс масслукинга завершен")

async def add_channel_to_queue(username: str):
    """Добавление канала в очередь обработки с учетом лимита"""
    # ИСПРАВЛЕНИЕ: проверяем лимит только по ПОЛНОСТЬЮ обработанным каналам (с фактически выполненными действиями)
    max_channels = settings.get('max_channels', 150)
    
    if max_channels != float('inf') and len(processed_channels) >= max_channels:
        logger.info(f"Достигнут лимит полностью обработанных каналов ({max_channels}), канал {username} не добавлен в очередь")
        return
    
    if username not in processed_channels and username not in channel_processing_queue:
        # Подготавливаем канал для обработки
        success = await prepare_channel_for_processing(username)
        if success:
            logger.info(f"Канал {username} добавлен в очередь круговой обработки")
            
            # Обновляем статистику очереди в bot_interface
            try:
                import bot_interface
                queue_list = list(channel_processing_queue.keys())
                bot_interface.update_queue_statistics(queue_list)
            except:
                pass
        else:
            logger.warning(f"Не удалось подготовить канал {username} для обработки")
    else:
        logger.info(f"Канал {username} уже в обработке или полностью обработан, пропускаем")

async def start_masslooking(telegram_client: TelegramClient, masslooking_settings: dict):
    """Запуск масслукинга с единым клиентом"""
    global masslooking_active, shared_client, settings, first_subscription_made
    
    if masslooking_active:
        logger.warning("Масслукинг уже запущен")
        return
    
    logger.info("Запуск масслукинга с круговой обработкой каналов...")
    
    # Используем переданный единый клиент
    shared_client = telegram_client
    settings = masslooking_settings.copy()
    masslooking_active = True
    first_subscription_made = False  # Сбрасываем флаг при запуске
    
    logger.info(f"Настройки масслукинга: {settings}")
    logger.info(f"Настройки FloodWait: {FLOOD_WAIT_SETTINGS}")
    
    # Запускаем рабочий процесс
    asyncio.create_task(masslooking_worker())
    
    # Запускаем отслеживание новых постов если включено
    if settings.get('track_new_posts', False):
        await start_new_post_tracking()
    
    logger.info("Масслукинг запущен с круговой обработкой каналов и правильным соблюдением задержек между подписками")

async def stop_masslooking():
    """Остановка масслукинга"""
    global masslooking_active, current_channel_iterator, channel_processing_queue, first_subscription_made
    
    logger.info("Остановка масслукинга...")
    masslooking_active = False
    current_channel_iterator = None
    first_subscription_made = False  # Сбрасываем флаг при остановке
    
    # Останавливаем отслеживание новых постов
    await stop_new_post_tracking()
    
    # Сохраняем прогресс перед остановкой
    await save_masslooking_progress()
    
    logger.info(f"Масслукинг остановлен, в очереди осталось {len(channel_processing_queue)} каналов")

def get_statistics():
    """Получение статистики масслукинга включая FloodWait"""
    avg_flood_wait = 0
    if statistics['flood_waits'] > 0:
        avg_flood_wait = statistics['total_flood_wait_time'] / statistics['flood_waits']
    
    return {
        **statistics,
        'progress': masslooking_progress.copy(),
        'queue_size': len(channel_processing_queue),
        'channels_in_rotation': len(channel_processing_queue),
        'average_flood_wait_time': round(avg_flood_wait, 2),
        'flood_wait_settings': FLOOD_WAIT_SETTINGS.copy(),
        'first_subscription_made': first_subscription_made,
        'tracked_channels_count': len(tracked_channels),
        'new_post_tracking_active': new_post_tracking_active
    }

def reset_statistics():
    """Сброс статистики"""
    global statistics, masslooking_progress, first_subscription_made
    statistics = {
        'comments_sent': 0,
        'reactions_set': 0,
        'channels_processed': 0,
        'errors': 0,
        'flood_waits': 0,
        'total_flood_wait_time': 0
    }
    masslooking_progress = {'current_channel': '', 'processed_count': 0}
    first_subscription_made = False

def update_flood_wait_settings(new_settings: dict):
    """Обновление настроек FloodWait"""
    global FLOOD_WAIT_SETTINGS
    FLOOD_WAIT_SETTINGS.update(new_settings)
    logger.info(f"Настройки FloodWait обновлены: {FLOOD_WAIT_SETTINGS}")

async def main():
    """Тестирование модуля"""
    test_settings = {
        'delay_range': (5, 10),
        'posts_range': (1, 3),
        'max_channels': 5,
        'track_new_posts': False
    }
    
    logger.info("Тестирование модуля masslooker...")
    print("Статистика масслукинга:", get_statistics())

if __name__ == "__main__":
    asyncio.run(main())