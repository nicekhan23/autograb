# autograb.py
import os
import asyncio
import logging
import re
import datetime
import traceback
from collections import deque
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
logging_level = logging.DEBUG  # в проде ставь INFO/WARNING
BUFFER_EXPIRY_SECONDS = 30  # время жизни "последнего вопроса" в сек
PARSE_WORKERS = 2  # пул для парсинга (не блокируем event loop)

# ---- Логирование (неблокирующее) ----
log_queue = Queue(-1)
queue_handler = QueueHandler(log_queue)

# файловый логер в отдельном потоке (QueueListener)
file_handler = RotatingFileHandler('auto_orders_debug.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(file_formatter)

logger = logging.getLogger()
logger.setLevel(logging_level)

# добавляем только очередь (основной поток быстро ставит записи в очередь)
logger.addHandler(queue_handler)

# listener будет писать в файл и консоль в отдельном потоке
listener = QueueListener(log_queue, file_handler, console_handler)
listener.start()

# снизим Telethon-логгинг в проде (очень много вывода влияет на производительность)
logging.getLogger('telethon').setLevel(logging.WARNING if logging_level != logging.DEBUG else logging.DEBUG)

# ---- Globals / state ----
processed_orders = set()        # oid, которые уже обработали
processed_msg_ids = set()       # message.id, которые уже обработаны (предотвращение дублей)
current_state = None            # None / "waiting_tons" / "waiting_price"
current_order = {}              # {"id":..., "tons":..., "price":...}
last_clicked_order_id = None

# вместо большого буфера — храним только последние подходящие вопросы (и их timestamp)
last_tons_event = None   # (event, datetime)
last_price_event = None  # (event, datetime)

# ThreadPool для CPU-bound parse
parse_executor = ThreadPoolExecutor(max_workers=PARSE_WORKERS)

# Telethon client
client = TelegramClient("auto_truck_orders", API_ID, API_HASH)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def safe_repr(x):
    try:
        return repr(x)
    except Exception:
        return "<unreprable>"


def log_debug_event(event, note=""):
    """Пишем развёрнутую отладочную информацию."""
    try:
        logging.debug("---- EVENT START %s ----", note)
        try:
            s = event.stringify()
            logging.debug("event.stringify():\n%s", s)
        except Exception as e:
            logging.debug("event.stringify() failed: %s", e)

        try:
            msg = event.message
            logging.debug("message id: %s", getattr(msg, 'id', None))
            logging.debug("message peer_id: %s", getattr(msg, 'peer_id', None))
            logging.debug("message sender_id: %s", getattr(msg, 'sender_id', None))
            logging.debug("message raw_text repr: %s", safe_repr(getattr(event, 'raw_text', None)))
        except Exception:
            logging.debug("cannot access message fields:\n%s", traceback.format_exc())
        try:
            if getattr(event, 'buttons', None):
                logging.debug("Buttons present: True")
                for r_i, row in enumerate(event.buttons):
                    for b_i, btn in enumerate(row):
                        try:
                            logging.debug(
                                "Button row %d col %d: text=%s, __repr__=%s",
                                r_i, b_i, safe_repr(getattr(btn, 'text', None)), safe_repr(btn)
                            )
                        except Exception:
                            logging.debug("Button repr error:\n%s", traceback.format_exc())
            else:
                logging.debug("Buttons present: False")
        except Exception:
            logging.debug("Error reading buttons:\n%s", traceback.format_exc())

        logging.debug("---- EVENT END ----")
    except Exception:
        logging.debug("log_debug_event failed:\n%s", traceback.format_exc())


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
        logging.exception("Ошибка парсинга в parse_order_block_sync")
        return None


async def parse_order_block(block_text: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(parse_executor, parse_order_block_sync, block_text)


async def respond_tons(event, order):
    """Отправляем ответ на вопрос про тонны (без смены state)."""
    answer = str(int(order.get('tons', 0)))
    logging.info("✍️ Ответ (тонны): %s (order=%s)", answer, order.get('id'))
    try:
        await event.respond(answer)
        logging.debug("Sent respond() for tons; answer=%s", answer)
    except Exception:
        logging.exception("Ошибка при отправке ответа тонн")


async def respond_price(event, order):
    """Отправляем ответ на вопрос про цену (без смены state)."""
    answer = str(int(order.get('price', 0)))
    logging.info("✍️ Ответ (цена): %s (order=%s)", answer, order.get('id'))
    try:
        await event.respond(answer)
        logging.debug("Sent respond() for price; answer=%s", answer)
    except Exception:
        logging.exception("Ошибка при отправке ответа цены")


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
    Универсальный handler: логируем, сохраняем последние вопросы в quick slots,
    и реагируем на уведомления о новом списке заказов.
    """
    global last_tons_event, last_price_event

    log_debug_event(event, note="incoming from BOT (generic)")

    raw = event.raw_text or ""
    text_lower = raw.lower()

    # защита от повторной обработки одного сообщения
    msg_id = getattr(event.message, "id", None)
    if msg_id and msg_id in processed_msg_ids:
        logging.debug("Message id %s уже обработан, пропускаю.", msg_id)
        return
    if msg_id:
        processed_msg_ids.add(msg_id)

    # обновление последних вопросов
    try:
        if RE_IS_TONS_QUESTION.search(text_lower):
            last_tons_event = (event, now_utc())
            logging.debug("Сохранил last_tons_event (msg_id=%s)", msg_id)
        if RE_IS_PRICE_QUESTION.search(text_lower):
            last_price_event = (event, now_utc())
            logging.debug("Сохранил last_price_event (msg_id=%s)", msg_id)
    except Exception:
        logging.exception("Ошибка при определении вопроса")

    # --- Если уведомление о новом заказе / отмене ---
    try:
        if (('размещен новый заказ' in text_lower and 'смотрите список заказов' in text_lower)
                or ('отменено' in text_lower and 'заказ в статусе' in text_lower)):
            if current_state is not None:
                logging.info("⏸️ Уже обрабатываю заказ — пропускаю уведомление")
                return
            logging.info("🆕 Новый заказ (уведомление). Отправляю 'Список текущих заказов'...")
            try:
                await client.send_message(BOT, "👷‍♂️ Список текущих заказов")
                logging.debug("send_message(... 'Список текущих заказов') отправлен")
            except Exception:
                logging.exception("Ошибка при отправке команды 'Список текущих заказов'")
            return

        # --- получили сообщение со списком заказов (один или несколько блоков) ---
        if 'номер заказа' in text_lower and 'всего тонн' in text_lower:
            if current_state is not None:
                logging.info("⏸️ Уже обрабатываю заказ — игнорирую список")
                return

            # разбиваем на блоки
            blocks = RE_ORDER_SPLIT.split(raw)
            for block in blocks:
                if not block.strip():
                    continue
                if 'Номер заказа:' not in block:
                    block = 'Номер заказа:' + block

                # быстрые проверки текста (чтобы избежать парсинга)
                if RE_HAS_OFFERS.search(block):
                    logging.info("⏭️ Пропускаю — уже есть предложения в блоке")
                    continue

                if RE_HAS_NO_OFFERS.search(block) is None:
                    logging.info("⏭️ Пропускаю — в блоке нет 'Нет предложений'")
                    continue

                # парсим блок в пуле исполнителей (не блокируем event loop)
                try:
                    data = await parse_order_block(block)
                except Exception:
                    logging.exception("parse_order_block failed")
                    data = None

                if not data:
                    logging.debug("Не удалось распарсить заказ в блоке: %s", safe_repr(block[:200]))
                    continue

                oid = data.get('id')
                logging.info("📦 Обнаружен заказ #%s — %s т, %s тг/т", oid, data['tons'], data['price'])

                if oid in processed_orders:
                    logging.info("⏭️ Заказ #%s уже в processed_orders", oid)
                    continue

                if data['tons'] < MIN_TONS or data['price'] < MIN_PRICE:
                    logging.info("⏩ Заказ #%s не проходит фильтр (tons/price)", oid)
                    continue

                # нажимаем кнопку "Возьму" — асинхронно, не дожидаясь RPC
                if getattr(event, 'buttons', None):
                    clicked = False
                    for r_i, row in enumerate(event.buttons):
                        for c_i, btn in enumerate(row):
                            btn_text = getattr(btn, 'text', '') or ''
                            logging.debug("Пробую кнопку row=%d col=%d text=%s", r_i, c_i, safe_repr(btn_text))
                            if 'возьму' in btn_text.lower():
                                try:
                                    # асинхронный клик: не блокирует loop, Telethon выполнит RPC независимо
                                    asyncio.create_task(btn.click())
                                    logging.info("🚚 (async) Нажал 'Возьму' на заказ #%s (row=%d col=%d)", oid, r_i, c_i)
                                except Exception:
                                    logging.exception("Ошибка при асинхронном клике по кнопке 'Возьму'")
                                    continue

                                # пометить заказ как обработанный и установить state
                                processed_orders.add(oid)
                                current_order.clear()
                                current_order.update(data)
                                current_state = "waiting_tons"
                                last_clicked_order_id = oid
                                clicked = True

                                # сразу после клика — попробуем ответить на последний вопрос, если он уже есть
                                prune_last_questions()
                                if current_state == "waiting_tons" and last_tons_event:
                                    # ответим (не дожидаясь)
                                    try:
                                        asyncio.create_task(respond_tons(last_tons_event[0], current_order))
                                        current_state = "waiting_price"
                                    except Exception:
                                        logging.exception("Не удалось ответить на last_tons_event")
                                elif current_state == "waiting_price" and last_price_event:
                                    try:
                                        asyncio.create_task(respond_price(last_price_event[0], current_order))
                                        current_state = None
                                        current_order = {}
                                    except Exception:
                                        logging.exception("Не удалось ответить на last_price_event")

                                # возвращаем — обрабатываем только первый подходящий заказ в этом сообщении
                                return
                    if not clicked:
                        logging.warning("⚠️ Кнопка 'Возьму' не найдена в структуре кнопок")
                else:
                    logging.warning("⚠️ В сообщении со списком заказов нет кнопок")
    except Exception:
        logging.exception("Unexpected error in generic_handler")


@client.on(events.NewMessage(from_users=BOT, pattern=RE_IS_TONS_QUESTION))
async def tons_question_handler(event):
    """Отдельный handler для вопросов про тонны — моментальная реакция, если мы в нужном состоянии."""
    global current_state, current_order
    log_debug_event(event, note="tons handler")

    if current_state == "waiting_tons" and current_order:
        try:
            await respond_tons(event, current_order)
            current_state = "waiting_price"
        except Exception:
            logging.exception("Ошибка в tons_question_handler")


@client.on(events.NewMessage(from_users=BOT, pattern=RE_IS_PRICE_QUESTION))
async def price_question_handler(event):
    """Отдельный handler для вопросов про цену."""
    global current_state, current_order
    log_debug_event(event, note="price handler")

    if current_state == "waiting_price" and current_order:
        try:
            await respond_price(event, current_order)
            current_state = None
            current_order = {}
        except Exception:
            logging.exception("Ошибка в price_question_handler")


async def main():
    try:
        await client.start()
        logging.info("🤖 Auto orders bot started (debug=%s)", logging_level == logging.DEBUG)
        await client.run_until_disconnected()
    finally:
        # остановим listener логов корректно при завершении
        try:
            listener.stop()
        except Exception:
            pass


if __name__ == "__main__":
    # чтобы graceful shutdown на Ctrl+C в Windows/Linux
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Interrupted by user, exiting...")
    except Exception:
        logging.exception("Fatal error in main")
