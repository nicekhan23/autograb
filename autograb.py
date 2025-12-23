import os
import re
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import KeyboardButtonCallback

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
MIN_TONS = int(os.getenv('MIN_TONS'))
MIN_PRICE_PER_TON = int(os.getenv('MIN_PRICE'))

# Хранение данных о текущем заказе
current_order_data = {}

async def main():
    """Основная функция запуска клиента"""
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    
    try:
        await client.start(phone=PHONE_NUMBER)
        me = await client.get_me()
        logger.info(f"Клиент авторизован как {me.first_name} (@{me.username})")
        
        @client.on(events.NewMessage())
        async def handler(event):
            """Обработчик всех новых сообщений"""
            try:
                message = event.message
                message_text = message.text or ""
                sender = await event.get_sender()
                
                # Логируем полученное сообщение
                logger.info(f"Получено сообщение от {sender.username if sender.username else sender.id}: {message_text[:-1]}")
                
                # Обработка триггерных сообщений
                if ("Размещен новый заказ" in message_text or 
                    "Заказ отменен" in message_text):
                    await click_current_orders(client, event)
                
                # Обработка списка заказов
                elif "Номер заказа:" in message_text and "Всего тонн:" in message_text:
                    await process_order_list(client, event, message)
                
                # Обработка вопроса о тоннаже
                elif "Сколько тонн вы можете взять?" in message_text:
                    await answer_tons_question(client, event)
                
                # Обработка вопроса о цене
                elif "Напишите вашу цену" in message_text or "Напишите свою цену" in message_text:
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
        await asyncio.sleep(1)
        
    except Exception as e:
        logger.error(f"Ошибка при отправке команды: {e}")

async def process_order_list(client, event, message):
    """Анализ списка заказов и нажатие 'Возьму' если условия выполнены"""
    try:
        message_text = message.text or ""
        
        # Парсим данные заказа
        order_data = parse_order_data(message_text)
        
        if not order_data:
            return
        
        # Проверяем условия
        tons = float(order_data.get('tons', 0))
        price_per_ton = float(order_data.get('price_per_ton', 0))
        has_no_offers = "Нет предложений" in message_text
        
        logger.info(f"Найден заказ №{order_data.get('number')}: {tons} т, {price_per_ton} тг/т, Нет предложений: {has_no_offers}")
        
        if (tons >= MIN_TONS and 
            price_per_ton >= MIN_PRICE_PER_TON):
            
            # Сохраняем данные заказа для последующих ответов
            current_order_data[event.chat_id] = order_data
            
            # Ищем inline-кнопку "Возьму"
            if message.buttons:
                for row in message.buttons:
                    for button in row:
                        if isinstance(button, KeyboardButtonCallback) and "Возьму" in button.text:
                            await message.click(data=button.data)
                            logger.info(f"Нажата inline-кнопка 'Возьму' для заказа №{order_data.get('number')}")
                            return
                logger.warning(f"В сообщении есть кнопки, но не найдена inline-кнопка 'Возьму'")
            else:
                # Если нет inline-кнопки, но есть подходящий заказ - логируем
                logger.info(f"Заказ №{order_data.get('number')} подходит, но нет inline-кнопки 'Возьму' для нажатия")
                
        else:
            logger.info(f"Заказ №{order_data.get('number')} не подходит по условиям (нужно: ≥{MIN_TONS}т, ≥{MIN_PRICE_PER_TON}тг/т)")
            
    except Exception as e:
        logger.error(f"Ошибка при обработке списка заказов: {e}", exc_info=True)

async def answer_tons_question(client, event):
    """Ответ на вопрос о количестве тонн"""
    try:
        chat_id = event.chat_id
        message = event.message
        
        if chat_id in current_order_data:
            tons = current_order_data[chat_id].get('tons')
            if tons:
                # Отправляем текстовый ответ
                response = str(int(tons) if tons.is_integer() else tons)
                await client.send_message(chat_id, response)
                logger.info(f"Отправлен текстовый ответ о тоннаже: {response}")
            else:
                logger.warning(f"Не найдены данные о тоннаже для чата {chat_id}")
        else:
            logger.warning(f"Нет данных о текущем заказе для чата {chat_id}")
            
    except Exception as e:
        logger.error(f"Ошибка при ответе на вопрос о тоннаже: {e}")

async def answer_price_question(client, event):
    """Ответ на вопрос о цене"""
    try:
        chat_id = event.chat_id
        
        if chat_id in current_order_data:
            price = current_order_data[chat_id].get('total_price') or current_order_data[chat_id].get('price_per_ton')
            if price:
                # Отправляем текстовый ответ
                response = str(int(price) if price.is_integer() else price)
                await client.send_message(chat_id, response)
                logger.info(f"Отправлен текстовый ответ о цене: {response}")
                
                # Очищаем данные заказа после ответа
                if chat_id in current_order_data:
                    del current_order_data[chat_id]
            else:
                logger.warning(f"Не найдены данные о цене для чата {chat_id}")
        else:
            logger.warning(f"Нет данных о текущем заказе для чата {chat_id}")
            
    except Exception as e:
        logger.error(f"Ошибка при ответе на вопрос о цене: {e}")

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
        
        # Извлекаем общую цену (если есть)
        total_price_match = re.search(r'цена перевозчика:\s*([\d\.]+)', message_text, re.IGNORECASE)
        if total_price_match:
            order_data['total_price'] = float(total_price_match.group(1))
        
        return order_data
    except Exception as e:
        logger.error(f"Ошибка при парсинге данных заказа: {e}")
        return None

if __name__ == "__main__":
    # Создаем папку для логов если ее нет
    os.makedirs('logs', exist_ok=True)
    
    logger.info(f"Запуск бота с session_name: {SESSION_NAME}")
    logger.info(f"Минимальный тоннаж: {MIN_TONS}, минимальная цена за тонну: {MIN_PRICE_PER_TON}")
    asyncio.run(main())