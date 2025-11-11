import os
import asyncio
import logging
import re
import datetime
from telethon import TelegramClient, events
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# 🔹 Для отслеживания обработанных заказов
processed_orders = set()
waiting_for_tons_input = False
waiting_for_price_input = False
current_order_tons = None
current_order_price = None

# 🔹 Твои данные (из my.telegram.org)
api_id = int(os.getenv("API_ID"))
api_hash = os.getenv("API_HASH")

# 🔹 Username бота с заказами
BOT_USERNAME = os.getenv("BOT_USERNAME")

# 🔹 Условия фильтра
MIN_TONS = int(os.getenv("MIN_TONS", 0))
MIN_PRICE = int(os.getenv("MIN_PRICE", 0))
MIN_ACCEPTABLE_PRICE = 4500  # Минимальная приемлемая цена для перебивания

# --- Настраиваем логирование ---
logging.basicConfig(
    filename='auto_orders.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- Основной клиент ---
client = TelegramClient('auto_truck_orders', api_id, api_hash)


def log(message, level='info'):
    """Пишет сообщение в лог и консоль"""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    log_entry = f"[{timestamp}] {message}"

    if level == 'error':
        logging.error(message)
    else:
        logging.info(message)

    print(log_entry)


def parse_order(text: str):
    """Извлекает данные заказа"""
    order_id_match = re.search(r'Номер заказа:\s*(\d+)', text, re.IGNORECASE)
    tons_match = re.search(r'Всего тонн:\s*([\d.,]+)', text)
    price_match = re.search(r'Максимальная цена за тонну:\s*([\d.,]+)', text)
    
    if not tons_match or not price_match:
        return None, None, None

    order_id = order_id_match.group(1) if order_id_match else None
    tons = float(tons_match.group(1).replace(',', '.'))
    price = float(price_match.group(1).replace(',', '.'))
    return order_id, tons, price


@client.on(events.NewMessage(chats=BOT_USERNAME))
async def handler(event):
    global waiting_for_tons_input, waiting_for_price_input, current_order_tons, current_order_price
    
    text = event.raw_text.lower()

    # 🔹 Если ждём ответа на вопрос о тоннах
    if waiting_for_tons_input and current_order_tons:
        log(f"✍️ Отвечаю: {current_order_tons} тонн")
        await event.respond(str(current_order_tons))
        waiting_for_tons_input = False
        # Now wait for price question
        waiting_for_price_input = True 
        current_order_tons = None
        return 
    
    # 🔹 Если ждём ответа на вопрос о цене
    if waiting_for_price_input and current_order_price:
        log(f"✍️ Отвечаю: {current_order_price} тенге")
        await event.respond(str(current_order_price))
        waiting_for_price_input = False
        current_order_price = None
        await asyncio.sleep(2)
        
        # После взятия заказа возвращаемся к списку
        await event.respond('/start')
        await asyncio.sleep(1)
        # Ищем кнопку "Список текущих заказов"
        async for msg in client.iter_messages(BOT_USERNAME, limit=5):
            if msg.buttons:
                for row in msg.buttons:
                    for button in row:
                        if 'список текущих заказов' in button.text.lower():
                            await button.click()
                            log("🔄 Открываю список заказов для следующего...")
                            return
        return

    # 1️⃣ Уведомление о новом заказе
    if 'размещен новый заказ' in text:
        log("🆕 Новый заказ обнаружен!")
        await asyncio.sleep(1.5)

        if event.buttons:
            for row in event.buttons:
                for button in row:
                    if 'список текущих заказов' in button.text.lower():
                        await button.click()
                        log("📋 Открываю список заказов...")
                        return
        log("⚠️ Кнопка 'Список текущих заказов' не найдена.", 'error')

    # 2️⃣ Пришёл заказ
    elif 'номер заказа' in text and 'всего тонн' in text:
        order_id, tons, price = parse_order(event.raw_text)
        if tons is None or price is None:
            log("⚠️ Не удалось распарсить заказ.", 'error')
            return

        # Проверяем, не обрабатывали ли уже
        if order_id and order_id in processed_orders:
            log(f"⏭️ Заказ #{order_id} уже обработан, пропускаю.")
            return

        log(f"📦 Заказ #{order_id}: {tons} т, {price} тг/т")

        if tons >= MIN_TONS and price >= MIN_PRICE:
            log("✅ Подходит! Нажимаю 'Возьму'...")
            await asyncio.sleep(1.0)

            if event.buttons:
                for row in event.buttons:
                    for button in row:
                        if 'возьму' in button.text.lower():
                            await button.click()
                            log(f"🚚 Нажал 'Возьму' на заказ #{order_id}")
                            
                            # Запоминаем заказ и тоннаж
                            if order_id:
                                processed_orders.add(order_id)
                            waiting_for_tons_input = True
                            current_order_tons = tons
                            current_order_price = price
                            return
                log("⚠️ Кнопка 'Возьму' не найдена.", 'error')
            else:
                log("⚠️ В сообщении нет кнопок.", 'error')
        else:
            log("⏩ Заказ не подходит по условиям.")
        
    # 3️⃣ Конкуренция за заказ (кто-то уже откликнулся)
    elif 'по данному заказу уже есть предложение' in text and 'вы можете взять этот заказ по цене' in text:
        # Ищем цену, по которой можем взять заказ
        price_match = re.search(r'вы можете взять этот заказ по цене\s*([\d.,]+)', text, re.IGNORECASE)
        
        if price_match:
            compete_price = float(price_match.group(1).replace(',', '.').replace(' ', ''))
            log(f"⚔️ Конкуренция! Могу взять заказ по цене {compete_price} тг/т")
            
            if compete_price >= MIN_ACCEPTABLE_PRICE:
                log(f"✅ Цена {compete_price} >= {MIN_ACCEPTABLE_PRICE}, перебиваю!")
                await asyncio.sleep(0.8)
                
                if event.buttons:
                    for row in event.buttons:
                        for button in row:
                            if 'возьму' in button.text.lower():
                                await button.click()
                                log(f"🚚 Перебил конкурента! Взял по цене {compete_price}")
                                
                                # Запоминаем данные для ответа боту
                                waiting_for_tons_input = True
                                # Пытаемся найти тоннаж в этом же сообщении или используем минимальный
                                tons_match = re.search(r'всего тонн:\s*([\d.,]+)', event.raw_text, re.IGNORECASE)
                                current_order_tons = float(tons_match.group(1).replace(',', '.')) if tons_match else MIN_TONS
                                current_order_price = compete_price
                                return
                    log("⚠️ Кнопка 'Возьму' не найдена в конкурентном заказе.", 'error')
            else:
                log(f"⏩ Цена {compete_price} < {MIN_ACCEPTABLE_PRICE}, пропускаю конкурентный заказ.")
        else:
            log("⚠️ Не удалось извлечь цену из конкурентного предложения.", 'error')

async def periodic_check():
    """Проверяет заказы каждый час"""
    while True:
        await asyncio.sleep(3600)  # 1 час
        log("⏰ Автопроверка заказов...")
        await client.send_message(BOT_USERNAME, '/start')
        await asyncio.sleep(2)
        
        # Ищем кнопку "Список текущих заказов"
        async for msg in client.iter_messages(BOT_USERNAME, limit=5):
            if msg.buttons:
                for row in msg.buttons:
                    for button in row:
                        if 'список текущих заказов' in button.text.lower():
                            await button.click()
                            log("📋 Открыл список заказов (авточек)")
                            break

async def main():
    await client.start()
    log("🤖 Авто-принятие заказов запущено. Ожидаем новые заказы...")
    
    # Запускаем периодическую проверку
    asyncio.create_task(periodic_check())
    
    await client.run_until_disconnected()


asyncio.run(main())
