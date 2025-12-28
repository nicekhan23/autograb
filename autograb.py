import os
import re
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import KeyboardButton, KeyboardButtonCallback

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('auto_orders.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Загрузка данных из .env
load_dotenv()
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
PHONE_NUMBER = os.getenv('PHONE_NUMBER')
SESSION_NAME = os.getenv('SESSION_NAME', 'auto_truck_orders')

# Параметры фильтра заказов
MIN_TONS = int(os.getenv('MIN_TONS', 50))
MIN_PRICE_PER_TON = int(os.getenv('MIN_PRICE', 3000))

# Хранение данных о текущем заказе
current_order_data = {}

async def main():
    """Основная функция запуска клиента"""
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    
    try:
        await client.start(phone=PHONE_NUMBER)
        me = await client.get_me()
        logger.info(f"Клиент авторизован как {me.first_name} (@{me.username})")
        
        # Уникальный обработчик для всех сообщений
        @client.on(events.NewMessage())
        async def handler(event):
            """Обработчик всех новых сообщений"""
            try:
                message = event.message
                message_text = message.message or ""
                sender = await event.get_sender()
                chat_id = event.chat_id
                
                # Пропускаем свои же сообщения
                if sender and sender.id == me.id:
                    return
                
                # Логируем полученное сообщение
                logger.info(f"Получено сообщение от {sender.username if sender.username else sender.id}: {message_text[:-1]}...")
                
                # Обработка триггерных сообщений
                if ("Размещен новый заказ" in message_text or 
                    "отменено" in message_text or
                    "Предложение по заказу" in message_text):
                    await asyncio.sleep(1)  # Задержка перед запросом списка
                    await click_current_orders(client, event)
                
                # Обработка списка заказов
                elif ("Номер заказа:" in message_text and 
                      "Всего тонн:" in message_text and
                      "Описание заказа:" in message_text):
                    await process_order_list(client, event, message)
                
                # Обработка вопроса о тоннаже
                elif "Сколько тонн вы можете взять" in message_text:
                    await answer_tons_question(client, event)
                
                # Обработка вопроса о цене
                elif "Напишите вашу цен" in message_text:
                    await answer_price_question(client, event)
                    
            except Exception as e:
                logger.error(f"Ошибка при обработке сообщения: {e}", exc_info=True)
        
        logger.info("Бот запущен и ожидает сообщений...")
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
    finally:
        await client.disconnect()

async def click_current_orders(client, event):
    """Отправка команды 'Список текущих заказов'"""
    try:
        # Отправляем текстовую команду
        await client.send_message(event.chat_id, "👷‍♂️ Список текущих заказов")
        logger.info("Отправлена текстовая команда: '👷‍♂️ Список текущих заказов'")
        await asyncio.sleep(2)  # Увеличиваем задержку
        
    except Exception as e:
        logger.error(f"Ошибка при отправке команды: {e}")

async def process_order_list(client, event, message):
    """Анализ списка заказов и нажатие 'Возьму' если условия выполнены"""
    try:
        message_text = message.message or ""
        
        # Парсим данные заказа
        order_data = parse_order_data(message_text)
        
        if not order_data:
            return
        
        # Проверяем условия
        tons = float(order_data.get('tons', 0))
        price_per_ton = float(order_data.get('price_per_ton', 0))
        has_no_offers = "Нет предложений" in message_text
        has_offers = "Есть предложение" in message_text or "Есть предлолжение" in message_text
        
        logger.info(f"Найден заказ №{order_data.get('number')}: {tons} т, {price_per_ton} тг/т, Нет предложений: {has_no_offers}")
        
        # Если уже есть предложения - пропускаем
        if has_offers:
            logger.info(f"Заказ №{order_data.get('number')} уже имеет предложения, пропускаем")
            return
            
        # Проверяем условия по тоннажу и цене
        if (tons >= MIN_TONS and price_per_ton >= MIN_PRICE_PER_TON and has_no_offers):
            
            # Сохраняем данные заказа
            current_order_data[event.chat_id] = order_data
            current_order_data[event.chat_id]['processed_at'] = datetime.now()
            
            # Ищем кнопки в сообщении
            button_found = await find_and_click_button(client, message, order_data)
            
            if not button_found:
                # Если не нашли кнопку, пробуем отправить текстовую команду
                await client.send_message(event.chat_id, "Возьму")
                logger.info(f"Отправлена текстовая команда 'Возьму' для заказа №{order_data.get('number')}")
                
        else:
            logger.info(f"Заказ №{order_data.get('number')} не подходит по условиям")
            
    except Exception as e:
        logger.error(f"Ошибка при обработке списка заказов: {e}", exc_info=True)

async def find_and_click_button(client, message, order_data):
    """Поиск и нажатие кнопки 'Возьму'"""
    try:
        # Проверяем reply_markup (inline-кнопки)
        if hasattr(message, 'reply_markup') and message.reply_markup:
            rows = message.reply_markup.rows
            for row in rows:
                for button in row.buttons:
                    button_text = getattr(button, 'text', '')
                    if "Возьму" in button_text:
                        # Нажимаем на кнопку
                        await message.click(data=button.data)
                        logger.info(f"Нажата кнопка 'Возьму' для заказа №{order_data.get('number')}")
                        return True
        
        # Проверяем buttons (устаревший способ)
        if hasattr(message, 'buttons') and message.buttons:
            for row in message.buttons:
                for button in row:
                    button_text = getattr(button, 'text', '')
                    if "Возьму" in button_text:
                        await message.click(data=button.data)
                        logger.info(f"Нажата кнопка 'Возьму' для заказа №{order_data.get('number')}")
                        return True
        
        return False
        
    except Exception as e:
        logger.error(f"Ошибка при поиске/нажатии кнопки: {e}")
        return False

async def answer_tons_question(client, event):
    """Ответ на вопрос о количестве тонн"""
    try:
        chat_id = event.chat_id
        message_text = event.message.message or ""
        
        # Попробуем найти номер заказа в сообщении
        order_number = None
        order_match = re.search(r'заказ[а]?\s*[№#]?\s*(\d+)', message_text, re.IGNORECASE)
        if order_match:
            order_number = order_match.group(1)
        
        # Если есть номер заказа в сообщении, ищем соответствующие данные
        if order_number:
            for chat, order_data in list(current_order_data.items()):
                if order_data.get('number') == order_number:
                    tons = order_data.get('tons')
                    if tons:
                        response = str(int(tons) if tons.is_integer() else tons)
                        await client.send_message(chat_id, response)
                        logger.info(f"Отправлен текстовый ответ о тоннаже для заказа №{order_number}: {response}")
                        return
            
        # Старая логика для обратной совместимости
        if chat_id in current_order_data:
            tons = current_order_data[chat_id].get('tons')
            if tons:
                response = str(int(tons) if tons.is_integer() else tons)
                await client.send_message(chat_id, response)
                logger.info(f"Отправлен текстовый ответ о тоннаже (старая логика): {response}")
                return
            else:
                logger.warning(f"Не найдены данные о тоннаже для чата {chat_id}")
        else:
            logger.info(f"Нет данных о текущем заказе для чата {chat_id}. Поиск по всему хранилищу...")
            # Последняя попытка: берем любой доступный заказ
            for chat, order_data in list(current_order_data.items()):
                tons = order_data.get('tons')
                if tons:
                    response = str(int(tons) if tons.is_integer() else tons)
                    await client.send_message(chat_id, response)
                    logger.info(f"Отправлен текстовый ответ о тоннаже из общего хранилища: {response}")
                    return
        
        # Если ничего не нашли, отправляем минимальный тоннаж по умолчанию
        await client.send_message(chat_id, str(MIN_TONS))
        logger.info(f"Отправлен минимальный тоннаж по умолчанию: {MIN_TONS}")
            
    except Exception as e:
        logger.error(f"Ошибка при ответе на вопрос о тоннаже: {e}")
        try:
            await client.send_message(event.chat_id, str(MIN_TONS))
        except:
            pass

async def answer_price_question(client, event):
    """Ответ на вопрос о цене"""
    try:
        chat_id = event.chat_id
        message_text = event.message.message or ""
        
        # Попробуем найти номер заказа в сообщении
        order_number = None
        order_match = re.search(r'заказ[а]?\s*[№#]?\s*(\d+)', message_text, re.IGNORECASE)
        if order_match:
            order_number = order_match.group(1)
        
        # Если есть номер заказа в сообщении, ищем соответствующие данные
        if order_number:
            for chat, order_data in list(current_order_data.items()):
                if order_data.get('number') == order_number:
                    price = order_data.get('price_per_ton')
                    if price:
                        response = str(int(price) if price.is_integer() else price)
                        await client.send_message(chat_id, response)
                        logger.info(f"Отправлен текстовый ответ о цене для заказа №{order_number}: {response}")
                        
                        # НЕ удаляем данные сразу - они могут понадобиться для других вопросов
                        # Очищаем только если это текущий чат
                        if chat == chat_id:
                            del current_order_data[chat_id]
                        return
            
        # Старая логика для обратной совместимости
        if chat_id in current_order_data:
            price = current_order_data[chat_id].get('price_per_ton')
            if price:
                response = str(int(price) if price.is_integer() else price)
                await client.send_message(chat_id, response)
                logger.info(f"Отправлен текстовый ответ о цене (старая логика): {response}")
                
                # Очищаем данные заказа после ответа только для этого чата
                if chat_id in current_order_data:
                    del current_order_data[chat_id]
                return
            else:
                logger.warning(f"Не найдены данные о цене для чата {chat_id}")
        else:
            logger.info(f"Нет данных о текущем заказе для чата {chat_id}. Поиск по всему хранилищу...")
            # Последняя попытка: берем любой доступный заказ
            for chat, order_data in list(current_order_data.items()):
                price = order_data.get('price_per_ton')
                if price:
                    response = str(int(price) if price.is_integer() else price)
                    await client.send_message(chat_id, response)
                    logger.info(f"Отправлен текстовый ответ о цене из общего хранилища: {response}")
                    
                    # Очищаем только если это текущий чат
                    if chat == chat_id:
                        del current_order_data[chat_id]
                    return
        
        # Если ничего не нашли, отправляем минимальную цену по умолчанию
        await client.send_message(chat_id, str(MIN_PRICE_PER_TON))
        logger.info(f"Отправлена минимальная цена по умолчанию: {MIN_PRICE_PER_TON}")
            
    except Exception as e:
        logger.error(f"Ошибка при ответе на вопрос о цене: {e}")
        try:
            await client.send_message(event.chat_id, str(MIN_PRICE_PER_TON))
        except:
            pass

def parse_order_data(message_text):
    """Парсинг данных заказа из текста сообщения"""
    try:
        order_data = {}
        
        # Извлекаем номер заказа
        order_match = re.search(r'Номер заказа:\s*(\d+)', message_text)
        if order_match:
            order_data['number'] = order_match.group(1)
        
        # Извлекаем тоннаж
        tons_match = re.search(r'Всего тонн:\s*([\d\.]+)\s*т', message_text)
        if tons_match:
            order_data['tons'] = float(tons_match.group(1))
        
        # Извлекаем цену за тонну
        price_match = re.search(r'цена за тонну:\s*([\d\.]+)\s*тг', message_text, re.IGNORECASE)
        if not price_match:
            price_match = re.search(r'цена:\s*([\d\.]+)\s*тг', message_text, re.IGNORECASE)
        if not price_match:
            price_match = re.search(r'Максимальная цена за тонну:\s*([\d\.]+)\s*тг', message_text)
        
        if price_match:
            order_data['price_per_ton'] = float(price_match.group(1))
        
        return order_data
    except Exception as e:
        logger.error(f"Ошибка при парсинге данных заказа: {e}")
        return None

def cleanup_old_orders():
    """Очистка устаревших данных о заказах (старше 5 минут)"""
    try:
        current_time = datetime.now()
        to_delete = []
        
        for chat_id, order_data in current_order_data.items():
            # Если заказ обработан более 5 минут назад, удаляем
            if 'processed_at' in order_data:
                processed_time = order_data['processed_at']
                if (current_time - processed_time).seconds > 300:  # 5 минут
                    to_delete.append(chat_id)
        
        for chat_id in to_delete:
            del current_order_data[chat_id]
            logger.info(f"Очищены устаревшие данные для чата {chat_id}")
            
    except Exception as e:
        logger.error(f"Ошибка при очистке устаревших заказов: {e}")

if __name__ == "__main__":
    # Создаем папку для логов если ее нет
    os.makedirs('logs', exist_ok=True)
    
    logger.info(f"Запуск бота с session_name: {SESSION_NAME}")
    logger.info(f"Минимальный тоннаж: {MIN_TONS}, минимальная цена за тонну: {MIN_PRICE_PER_TON}")
    asyncio.run(main())