import os
import re
import logging
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient, events

#08.01.2026

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

load_dotenv()
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
PHONE_NUMBER = os.getenv('PHONE_NUMBER')
SESSION_NAME = os.getenv('SESSION_NAME', 'auto_truck_orders')

MIN_TONS = int(os.getenv('MIN_TONS', 50))
MIN_PRICE_PER_TON = int(os.getenv('MIN_PRICE', 3000))

# Глобальное хранилище заказов
current_order_data = {}

def parse_order_data(message_text):
    """Парсинг данных заказа из текста сообщения"""
    try:
        order_data = {}
        # Номер заказа
        order_match = re.search(r'Номер заказа:\s*(\d+)', message_text)
        if order_match:
            order_data['number'] = order_match.group(1)
        
        # Тоннаж
        tons_match = re.search(r'Всего тонн:\s*([\d\.]+)', message_text)
        if tons_match:
            order_data['tons'] = float(tons_match.group(1))
        
        # Цена (разные варианты написания)
        price_match = re.search(r'(?:цена за тонну|цена|Максимальная цена за тонну):\s*([\d\.]+)', message_text, re.IGNORECASE)
        if price_match:
            order_data['price_per_ton'] = float(price_match.group(1))
        
        return order_data if 'number' in order_data else None
    except Exception as e:
        logger.error(f"Ошибка парсинга: {e}")
        return None

async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    
    try:
        await client.start(phone=PHONE_NUMBER)
        me = await client.get_me()
        logger.info(f"Бот запущен: {me.first_name}")

        @client.on(events.NewMessage())
        async def handler(event):
            # Защита от дублей: обрабатываем только входящие
            if event.out:
                return
                
            message_text = event.message.message or ""
            chat_id = event.chat_id

            # 1. ТРИГГЕРЫ НА ОБНОВЛЕНИЕ СПИСКА
            if any(x in message_text for x in ["Размещен новый заказ", "отменено", "Предложение по заказу"]):
                logger.info("Событие системы: запрашиваю список заказов...")
                await asyncio.sleep(0.5)
                await client.send_message(chat_id, "👷‍♂️ Список текущих заказов")
                return

            # 2. АНАЛИЗ КАРТОЧКИ ЗАКАЗА
            if "Номер заказа:" in message_text and "Всего тонн:" in message_text:
                order_data = parse_order_data(message_text)
                if order_data:
                    order_num = order_data['number']
                    # Сохраняем/обновляем данные
                    current_order_data[order_num] = order_data
                    current_order_data[order_num]['timestamp'] = datetime.now()
                    
                    logger.info(f"Данные заказа №{order_num} сохранены: {order_data['tons']}т / {order_data['price_per_ton']}тг")

                    # Проверка условий для нажатия кнопки
                    if (order_data['tons'] >= MIN_TONS and 
                        order_data['price_per_ton'] >= MIN_PRICE_PER_TON and 
                        "Есть предложение" not in message_text):
                        
                        await find_and_click_button(event, order_num)

            # 3. ВОПРОС О ТОННАХ
            elif "Сколько тонн вы можете взять" in message_text:
                await answer_question(client, chat_id, message_text, 'tons', MIN_TONS)

            # 4. ВОПРОС О ЦЕНЕ
            elif "Напишите вашу цен" in message_text:
                await answer_question(client, chat_id, message_text, 'price_per_ton', MIN_PRICE_PER_TON)

        await client.run_until_disconnected()
    finally:
        await client.disconnect()

async def find_and_click_button(event, order_num):
    """Ищет кнопку 'Возьму' и нажимает её"""
    if event.reply_markup:
        for row in event.reply_markup.rows:
            for button in row.buttons:
                if "Возьму" in getattr(button, 'text', ''):
                    await event.click(button)
                    logger.info(f"Нажата кнопка 'Возьму' для №{order_num}")
                    return True
    return False

async def answer_question(client, chat_id, message_text, key, default_val):
    """Универсальный ответ на вопросы бота"""
    # Пытаемся найти номер заказа в вопросе бота
    order_num_match = re.search(r'(\d+)', message_text)
    order_num = order_num_match.group(1) if order_num_match else None
    
    response = None

    # Ищем в базе по номеру
    if order_num and order_num in current_order_data:
        response = current_order_data[order_num].get(key)
    # Если номер не указан, берем самый свежий заказ из базы
    elif current_order_data:
        latest_order = max(current_order_data.values(), key=lambda x: x['timestamp'])
        response = latest_order.get(key)

    final_val = str(int(response)) if response else str(default_val)
    await client.send_message(chat_id, final_val)
    logger.info(f"Ответ на {key}: {final_val} (Заказ: {order_num if order_num else 'последний'})")

if __name__ == "__main__":
    asyncio.run(main())