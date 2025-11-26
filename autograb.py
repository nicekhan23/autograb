import os
import asyncio
import logging
import re
import datetime
import traceback
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
processed_orders = set()
current_state = None  # None / "waiting_tons" / "waiting_price"
current_order = {}    # {"id":..., "tons":..., "price":...}

client = TelegramClient("auto_truck_orders", API_ID, API_HASH)


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
            logging.debug("message raw_text repr: %s", safe_repr(event.raw_text))
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
                            logging.debug("Button row %d col %d: text=%s, __repr__=%s", r_i, b_i, safe_repr(getattr(btn, 'text', None)), safe_repr(btn))
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


@client.on(events.NewMessage(from_users=BOT))
async def handler(event):
    global current_state, current_order

    # немедленно логируем подробности входящего события
    log_debug_event(event, note="incoming from BOT")

    raw = event.raw_text or ""
    text = raw.lower()

    # пытаемся ловить вопросы (состояния)
    try:
        # --- waiting_tons ---
        if current_state == "waiting_tons":
            if re.search(r'сколько\s+тонн|сколько\s+т\.|можете\s+взять', text):
                answer = str(int(current_order.get('tons', 0)))
                logging.info("✍️ Ответ (тонны): %s", answer)
                try:
                    # ответ как reply (reply_to текущего сообщения)
                    await event.respond(answer)
                    logging.debug("Sent respond() for tons; answer=%s", answer)
                except Exception:
                    logging.exception("Ошибка при отправке ответа тонн")
                current_state = "waiting_price"
                return

        # --- waiting_price ---
        if current_state == "waiting_price":
            if re.search(r'цен[ыу]|вашу\s+цену|напишите\s+цену|какая\s+цена', text):
                answer = str(int(current_order.get('price', 0)))
                logging.info("✍️ Ответ (цена): %s", answer)
                try:
                    await event.respond(answer)
                    logging.debug("Sent respond() for price; answer=%s", answer)
                except Exception:
                    logging.exception("Ошибка при отправке ответа цены")
                current_state = None
                current_order = {}
                return

        # --- уведомление о новом заказе / запрос списка ---
        if (('размещен новый заказ' in text and 'смотрите список заказов' in text) or
            ('отменено' in text and 'заказ в статусе' in text)):
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
        if 'номер заказа' in text and 'всего тонн' in text:
            if current_state is not None:
                logging.info("⏸️ Уже обрабатываю заказ — игнорирую список")
                return

            # разбиваем на блоки (учтём регистры)
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
                                try:
                                    await btn.click()
                                    logging.info("🚚 Нажал 'Возьму' на заказ #%s (button row=%d col=%d)", oid, r_i, c_i)
                                except Exception:
                                    logging.exception("Ошибка при клике по кнопке 'Возьму'")
                                clicked = True
                                processed_orders.add(oid)
                                current_order = data
                                current_state = "waiting_tons"
                                # после нажатия бот обычно отправит новый текст с вопросом — мы ждём следующего события
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
