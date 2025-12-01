import os
import asyncio
import logging
import re
import datetime
import traceback
from collections import deque
from logging.handlers import RotatingFileHandler
from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

# ---- Конфигурация ----
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT = os.getenv("BOT_USERNAME")
MIN_TONS = int(os.getenv("MIN_TONS", 0))
MIN_PRICE = int(os.getenv("MIN_PRICE", 0))

# ---- Режим логирования (для быстрого переключения) ----
logging_level = logging.DEBUG  # поставь INFO/WARNING в проде

# ---- Настройка логирования: файл + ротация + консоль ----
logger = logging.getLogger()
logger.setLevel(logging_level)

log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# rotating file handler (max 10MB per file, keep 5)
file_handler = RotatingFileHandler('auto_orders_debug.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# console
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# Telethon internal logger (очень подробный) — можно оставить или снизить
logging.getLogger('telethon').setLevel(logging_level)

# ---- Runtime state ----
processed_orders = set()           # заказы, по которым уже кликали
current_state = None               # None / "waiting_tons" / "waiting_price"
current_order = {}                 # {"id":..., "tons":..., "price":...}
last_clicked_order_id = None       # id последнего кликнутого заказа

# буфер входящих сообщений для обработки гонок (сообщение, текст_lower, date)
# держим последние N сообщений; при клике ищем уже пришедшие вопросы
message_buffer = deque(maxlen=200)
BUFFER_EXPIRY_SECONDS = 30  # сколько секунд назад ещё считаем сообщение "свежим"

client = TelegramClient("auto_truck_orders", API_ID, API_HASH)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def safe_repr(x):
    try:
        return repr(x)
    except Exception:
        return "<unreprable>"


def log_debug_event(event, note=""):
    """Записываем подробную информацию о событии в debug"""
    try:
        logging.debug("---- EVENT START %s ----", note)
        # stringify даёт развёрнутую инфу Telethon (полезно)
        try:
            s = event.stringify()
            logging.debug("event.stringify():\n%s", s)
        except Exception as e:
            logging.debug("event.stringify() failed: %s", e)

        # message id / sender / peer / raw_text
        try:
            msg = event.message
            logging.debug("message id: %s", getattr(msg, 'id', None))
            logging.debug("message peer_id: %s", getattr(msg, 'peer_id', None))
            logging.debug("message sender_id: %s", getattr(msg, 'sender_id', None))
            logging.debug("message raw_text repr: %s", safe_repr(getattr(event, 'raw_text', None)))
        except Exception:
            logging.debug("cannot access message fields:\n%s", traceback.format_exc())

        # кнопки (если есть) — распечатаем структуру
        try:
            if getattr(event, 'buttons', None):
                logging.debug("Buttons present: True")
                for r_i, row in enumerate(event.buttons):
                    for b_i, btn in enumerate(row):
                        try:
                            # .text может быть None, кнопки могут быть callback/data
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


def parse_order(text):
    oid_m = re.search(r'Номер заказа:\s*(\d+)', text, re.IGNORECASE)
    tons_m = re.search(r'Всего тонн:\s*([\d.,]+)', text, re.IGNORECASE)
    price_m = re.search(r'Максимальная цена за тонну:\s*([\d.,]+)', text, re.IGNORECASE)

    if not tons_m or not price_m:
        return None

    try:
        oid = oid_m.group(1) if oid_m else None
        tons = float(tons_m.group(1).replace(',', '.'))
        price = float(price_m.group(1).replace(',', '.'))
    except Exception:
        logging.exception("Ошибка парсинга чисел в parse_order")
        return None

    return {"id": oid, "tons": tons, "price": price}


def is_tons_question(text_lower: str) -> bool:
    return bool(re.search(r'сколько\s+тонн|сколько\s+т\.|можете\s+взять', text_lower))


def is_price_question(text_lower: str) -> bool:
    return bool(re.search(
        r'(цен[ау]|назовите\s+.*цен|напишите\s+свою\s+цен|укажите\s+.*цен|ваш[ау]\s+цен|какая\s+цен|сколько\s+хотите|сколько\s+возьмёте)',
        text_lower
    ))



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


def buffer_prune():
    """Удаляем старые сообщения из буфера (необязательно — deque maxlen держит размер)."""
    cutoff = now_utc() - datetime.timedelta(seconds=BUFFER_EXPIRY_SECONDS)
    # inplace prune (deque имеет нетривиальную filter, делаем rebuild)
    newbuf = deque((m for m in message_buffer if m[2] >= cutoff), maxlen=message_buffer.maxlen)
    message_buffer.clear()
    message_buffer.extend(newbuf)


async def scan_buffer_and_handle_questions():
    """
    После клика или установки состояния — просканируем буфер на предмет уже пришедших вопросов,
    и если найдём — ответим.
    """
    global current_state, current_order

    buffer_prune()
    if not current_order:
        return

    cutoff = now_utc() - datetime.timedelta(seconds=BUFFER_EXPIRY_SECONDS)
    # проходим по буферу в порядке от старых к новым
    for event, text_lower, date in list(message_buffer):
        if date < cutoff:
            continue
        try:
            if current_state == "waiting_tons" and is_tons_question(text_lower):
                logging.debug("Найден в буфере вопрос про тонны (от %s). Отвечаем.", date)
                await respond_tons(event, current_order)
                current_state = "waiting_price"
                return
            if current_state == "waiting_price" and is_price_question(text_lower):
                logging.debug("Найден в буфере вопрос про цену (от %s). Отвечаем.", date)
                await respond_price(event, current_order)
                current_state = None
                current_order = {}
                return
        except Exception:
            logging.exception("Ошибка при обработке буфера")


@client.on(events.NewMessage(from_users=BOT))
async def handler(event):
    global current_state, current_order, last_clicked_order_id

    # немедленно логируем подробности входящего события
    log_debug_event(event, note="incoming from BOT")

    raw = event.raw_text or ""
    text_lower = raw.lower()

    # добавляем в буфер свежую копию (на случай гонок)
    try:
        msg_date = getattr(event.message, 'date', None) or now_utc()
        # ensure timezone-aware
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=datetime.timezone.utc)
        message_buffer.append((event, text_lower, msg_date))
    except Exception:
        logging.exception("Не удалось добавить событие в message_buffer")

    # пытаемся ловить вопросы (состояния)
    try:
        # --- Если уже в ожидании тонн — при наступлении вопроса ответим ---
        if current_state == "waiting_tons" and is_tons_question(text_lower):
            await respond_tons(event, current_order)
            current_state = "waiting_price"
            return

        # --- Если уже в ожидании цены ---
        if current_state == "waiting_price" and is_price_question(text_lower):
            await respond_price(event, current_order)
            current_state = None
            current_order = {}
            return

        # --- уведомление о новом заказе / запрос списка ---
        if (('размещен новый заказ' in text_lower and 'смотрите список заказов' in text_lower) or
                ('отменено' in text_lower and 'заказ в статусе' in text_lower)):
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

        # --- получили сообщение со списком заказов ---
        if 'номер заказа' in text_lower and 'всего тонн' in text_lower:
            if current_state is not None:
                logging.info("⏸️ Уже обрабатываю заказ — игнорирую список")
                return

            # разбиваем на блоки (учтём регистры) — сохраняем оригинальный raw для парсинга
            blocks = re.split(r'\n\s*Номер заказа:', raw, flags=re.IGNORECASE)
            for block in blocks:
                if not block.strip():
                    continue
                if 'Номер заказа:' not in block:
                    block = 'Номер заказа:' + block

                # если в блоке есть предложения — пропускаем
                if re.search(r'есть\s+предл', block, flags=re.IGNORECASE):
                    logging.info("⏭️ Пропускаю — уже есть предложения в блоке")
                    continue

                if re.search(r'нет\s+предложен', block, flags=re.IGNORECASE) is None:
                    logging.info("⏭️ Пропускаю — в блоке нет 'Нет предложений'")
                    continue

                data = parse_order(block)
                if not data:
                    logging.debug("Не удалось распарсить заказ в блоке: %s", safe_repr(block))
                    continue

                oid = data.get('id')
                logging.info("📦 Обнаружен заказ #%s — %s т, %s тг/т", oid, data['tons'], data['price'])

                if oid in processed_orders:
                    logging.info("⏭️ Заказ #%s уже в processed_orders", oid)
                    continue

                if data['tons'] < MIN_TONS or data['price'] < MIN_PRICE:
                    logging.info("⏩ Заказ #%s не проходит фильтр (tons/price)", oid)
                    continue

                # нажимаем кнопку "Возьму"
                if getattr(event, 'buttons', None):
                    clicked = False
                    for r_i, row in enumerate(event.buttons):
                        for c_i, btn in enumerate(row):
                            btn_text = getattr(btn, 'text', '') or ''
                            logging.debug("Пробую кнопку row=%d col=%d text=%s", r_i, c_i, safe_repr(btn_text))
                            if 'возьму' in btn_text.lower():
                                # защита от двойного клика: повторно не кликаем, если уже обработали этот oid
                                try:
                                    await btn.click()
                                    logging.info("🚚 Нажал 'Возьму' на заказ #%s (button row=%d col=%d)", oid, r_i, c_i)
                                except Exception:
                                    logging.exception("Ошибка при клике по кнопке 'Возьму'")
                                    # даже если клик упал — не помечаем заказ как обработанный
                                    continue

                                # успешно кликнули — отмечаем заказ и выставляем состояние
                                processed_orders.add(oid)
                                current_order = data
                                current_state = "waiting_tons"
                                last_clicked_order_id = oid
                                clicked = True

                                # сразу после клика — сканируем буфер: возможно вопрос уже был пришёл ранее
                                await scan_buffer_and_handle_questions()

                                # ждём следующий event (либо он уже пришёл и был обработан сканом)
                                return
                    if not clicked:
                        logging.warning("⚠️ Кнопка 'Возьму' не найдена в структуре кнопок")
                else:
                    logging.warning("⚠️ В сообщении со списком заказов нет кнопок")
    except Exception:
        logging.exception("Unexpected error in handler")


async def main():
    await client.start()
    logging.info("🤖 Auto orders bot started (debug mode=%s)", logging_level == logging.DEBUG)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
