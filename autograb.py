# autograb.py
import os
import asyncio
import logging
import re
import datetime
import traceback
from logging.handlers import RotatingFileHandler, QueueHandler, QueueListener
from queue import Queue
from concurrent.futures import ThreadPoolExecutor

from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

# ---- Конфигурация ----
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT = os.getenv("BOT_USERNAME")
MIN_TONS = int(os.getenv("MIN_TONS", 0))
MIN_PRICE = int(os.getenv("MIN_PRICE", 0))

# ---- Runtime / параметры ----
logging_level = logging.INFO  # Изменил на INFO для чистых логов
BUFFER_EXPIRY_SECONDS = 30
PARSE_WORKERS = 2

# ---- Логирование (неблокирующее) ----
log_queue = Queue(-1)
queue_handler = QueueHandler(log_queue)

file_handler = RotatingFileHandler('auto_orders.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
file_formatter = logging.Formatter('%(asctime)s - %(message)s')  # Упрощенный формат
file_handler.setFormatter(file_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(file_formatter)

logger = logging.getLogger()
logger.setLevel(logging_level)

logger.addHandler(queue_handler)
listener = QueueListener(log_queue, file_handler, console_handler)
listener.start()

# Уменьшаем логирование Telethon до минимума
logging.getLogger('telethon').setLevel(logging.ERROR)

# ---- Globals / state ----
processed_orders = set()
processed_msg_ids = set()
current_state = None
current_order = {}
last_clicked_order_id = None

last_tons_event = None
last_price_event = None

parse_executor = ThreadPoolExecutor(max_workers=PARSE_WORKERS)
client = TelegramClient("auto_truck_orders", API_ID, API_HASH)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


# ---- предкомпилированные regex'ы ----
RE_ORDER_SPLIT = re.compile(r'\n\s*Номер заказа:', flags=re.IGNORECASE)
RE_HAS_NO_OFFERS = re.compile(r'нет\s+предложен', flags=re.IGNORECASE)
RE_HAS_OFFERS = re.compile(r'есть\s+предл', flags=re.IGNORECASE)
RE_OID = re.compile(r'Номер заказа:\s*(\d+)', flags=re.IGNORECASE)
RE_TONS = re.compile(r'Всего тонн:\s*([\d.,]+)', flags=re.IGNORECASE)
RE_PRICE = re.compile(r'Максимальная цена за тонну:\s*([\d.,]+)', flags=re.IGNORECASE)

RE_IS_TONS_QUESTION = re.compile(r'сколько\s+тонн|сколько\s+т\.|можете\s+взять', flags=re.IGNORECASE)
RE_IS_PRICE_QUESTION = re.compile(
    r'(цен[ау]|назовите\s+.*цен|напишите\s+свою\s+цен|укажите\s+.*цен|ваш[ау]\s+цен|какая\s+цен|сколько\s+хотите|сколько\s+возьм[её]те)',
    flags=re.IGNORECASE
)


def parse_order_block_sync(block_text: str):
    """Синхронный парсер одного блока. Вызывается в executor."""
    try:
        oid_m = RE_OID.search(block_text)
        tons_m = RE_TONS.search(block_text)
        price_m = RE_PRICE.search(block_text)

        if not tons_m or not price_m:
            return None

        oid = oid_m.group(1) if oid_m else None
        tons = float(tons_m.group(1).replace(',', '.'))
        price = float(price_m.group(1).replace(',', '.'))
        return {"id": oid, "tons": tons, "price": price}
    except Exception:
        logging.error("Ошибка парсинга заказа")
        return None


async def parse_order_block(block_text: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(parse_executor, parse_order_block_sync, block_text)


async def respond_tons(event, order):
    """Отправляем ответ на вопрос про тонны."""
    answer = str(int(order.get('tons', 0)))
    logging.info(f"📦 Ответ: {answer} тонн (заказ #{order.get('id')})")
    try:
        await event.respond(answer)
    except Exception:
        logging.error("Ошибка при отправке ответа тонн")


async def respond_price(event, order):
    """Отправляем ответ на вопрос про цену."""
    # НЕ снижаем цену, берем максимальную цену за тонну
    answer = str(int(order.get('price', 0)))
    logging.info(f"💰 Ответ: {answer} тг/т (заказ #{order.get('id')})")
    try:
        await event.respond(answer)
    except Exception:
        logging.error("Ошибка при отправке ответа цены")


def prune_last_questions():
    """Удаляем устаревшие записи last_tons_event / last_price_event."""
    global last_tons_event, last_price_event
    cutoff = now_utc() - datetime.timedelta(seconds=BUFFER_EXPIRY_SECONDS)
    if last_tons_event and last_tons_event[1] < cutoff:
        last_tons_event = None
    if last_price_event and last_price_event[1] < cutoff:
        last_price_event = None


@client.on(events.NewMessage(from_users=BOT))
async def generic_handler(event):
    """
    Универсальный handler: логируем, сохраняем последние вопросы,
    и реагируем на уведомления о новом списке заказов.
    """
    global last_tons_event, last_price_event, current_state, current_order, last_clicked_order_id, processed_orders, processed_msg_ids

    raw = event.raw_text or ""
    text_lower = raw.lower()

    # защита от повторной обработки одного сообщения
    msg_id = getattr(event.message, "id", None)
    if msg_id and msg_id in processed_msg_ids:
        return
    if msg_id:
        processed_msg_ids.add(msg_id)

    # обновление последних вопросов
    try:
        if RE_IS_TONS_QUESTION.search(text_lower):
            last_tons_event = (event, now_utc())
        if RE_IS_PRICE_QUESTION.search(text_lower):
            last_price_event = (event, now_utc())
    except Exception:
        pass

    # --- Если уведомление о новом заказе / отмене ---
    try:
        if (('размещен новый заказ' in text_lower and 'смотрите список заказов' in text_lower)
                or ('отменено' in text_lower and 'заказ в статусе' in text_lower)):
            logging.info("🔔 Уведомление: %s", raw[:100])
            
            if current_state is not None:
                logging.info("⏸️ Пропускаю — уже обрабатываю заказ")
                return
                
            try:
                await client.send_message(BOT, "👷‍♂️ Список текущих заказов")
                logging.info("📋 Запросил список заказов")
            except Exception:
                logging.error("Ошибка при отправке команды")
            return

        # --- получили сообщение со списком заказов ---
        if 'номер заказа' in text_lower and 'всего тонн' in text_lower:
            logging.info("📄 Получен список заказов")
            
            if current_state is not None:
                logging.info("⏸️ Пропускаю — уже обрабатываю заказ")
                return

            blocks = RE_ORDER_SPLIT.split(raw)
            for block in blocks:
                if not block.strip():
                    continue
                if 'Номер заказа:' not in block:
                    block = 'Номер заказа:' + block

                # быстрые проверки текста
                if RE_HAS_OFFERS.search(block):
                    continue

                if RE_HAS_NO_OFFERS.search(block) is None:
                    continue

                data = await parse_order_block(block)
                if not data:
                    continue

                oid = data.get('id')
                logging.info(f"🔍 Проверяю заказ #{oid}: {data['tons']} т, {data['price']} тг/т")

                if oid in processed_orders:
                    logging.info(f"⏭️ Пропускаю #{oid} — уже обработан")
                    continue

                if data['tons'] < MIN_TONS or data['price'] < MIN_PRICE:
                    logging.info(f"⏩ Пропускаю #{oid} — не проходит фильтр")
                    continue

                # нажимаем кнопку "Возьму"
                if getattr(event, 'buttons', None):
                    clicked = False
                    for row in event.buttons:
                        for btn in row:
                            btn_text = getattr(btn, 'text', '') or ''
                            if 'возьму' in btn_text.lower():
                                try:
                                    asyncio.create_task(btn.click())
                                    logging.info(f"✅ Нажал 'Возьму' на заказ #{oid}")
                                    
                                    processed_orders.add(oid)
                                    current_order.clear()
                                    current_order.update(data)
                                    current_state = "waiting_tons"
                                    last_clicked_order_id = oid
                                    clicked = True

                                    # сразу после клика — попробуем ответить на последний вопрос
                                    prune_last_questions()
                                    if current_state == "waiting_tons" and last_tons_event:
                                        try:
                                            asyncio.create_task(respond_tons(last_tons_event[0], current_order))
                                            current_state = "waiting_price"
                                        except Exception:
                                            logging.error("Не удалось ответить на вопрос про тонны")
                                    elif current_state == "waiting_price" and last_price_event:
                                        try:
                                            asyncio.create_task(respond_price(last_price_event[0], current_order))
                                            current_state = None
                                            current_order = {}
                                        except Exception:
                                            logging.error("Не удалось ответить на вопрос про цену")

                                except Exception:
                                    logging.error("Ошибка при клике по кнопке 'Возьму'")
                                    continue

                                return
                    if not clicked:
                        logging.warning("⚠️ Не нашел кнопку 'Возьму'")
    except Exception:
        logging.error("Ошибка в generic_handler")


@client.on(events.NewMessage(from_users=BOT, pattern=RE_IS_TONS_QUESTION))
async def tons_question_handler(event):
    """Отдельный handler для вопросов про тонны."""
    global current_state, current_order
    
    if current_state == "waiting_tons" and current_order:
        try:
            await respond_tons(event, current_order)
            current_state = "waiting_price"
        except Exception:
            logging.error("Ошибка в tons_question_handler")


@client.on(events.NewMessage(from_users=BOT, pattern=RE_IS_PRICE_QUESTION))
async def price_question_handler(event):
    """Отдельный handler для вопросов про цену."""
    global current_state, current_order
    
    if current_state == "waiting_price" and current_order:
        try:
            await respond_price(event, current_order)
            current_state = None
            current_order = {}
        except Exception:
            logging.error("Ошибка в price_question_handler")


async def main():
    try:
        await client.start()
        logging.info("🤖 Бот запущен")
        logging.info(f"Фильтр: мин. {MIN_TONS} тонн, мин. {MIN_PRICE} тг/т")
        await client.run_until_disconnected()
    finally:
        try:
            listener.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Остановлен пользователем")
    except Exception:
        logging.error("Критическая ошибка в main")