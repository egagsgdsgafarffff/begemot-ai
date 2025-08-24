import asyncio
import logging
import os
import re
import base64
from io import BytesIO
from typing import List, Deque, Dict, Tuple

from collections import defaultdict, deque
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from PyPDF2 import PdfReader

# =========================
# Загрузка конфигурации
# =========================
load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

if TELEGRAM_BOT_TOKEN:
    print(f"Прочитан токен Telegram: '{TELEGRAM_BOT_TOKEN[:7]}...{TELEGRAM_BOT_TOKEN[-7:]}'")
else:
    print("Токен Telegram не найден в .env файле.")
if OPENAI_API_KEY:
    print(f"Прочитан ключ OpenAI: '{OPENAI_API_KEY[:5]}...{OPENAI_API_KEY[-5:]}'")
else:
    print("Ключ OpenAI не найден в .env файле.")

if not TELEGRAM_BOT_TOKEN or not OPENAI_API_KEY:
    print("Ошибка: Убедитесь, что вы создали .env файл и указали в нем TELEGRAM_BOT_TOKEN и OPENAI_API_KEY")
    raise SystemExit(1)

# =========================
# Константы и настройки
# =========================
OPENAI_MODEL = "gpt-5-mini-2025-08-07"

MEMORY_SIZE = 10

MAX_DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 1

MAX_FILE_SIZE_MB = 25
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TEXT_LENGTH = 75_000

SUPPORTED_TEXT_EXTENSIONS = [
    '.txt', '.md', '.py', '.csv', '.json', '.xml', '.yaml', '.yml',
    '.toml', '.log', '.tsv', '.sql', '.html', '.js', '.css', '.env', '.ts', '.svelte'
]

# Telegram форматирование: строгое MarkdownV2
PARSE_MODE = "MarkdownV2"
TG_MESSAGE_LIMIT = 4000  # немного меньше 4096 для запаса

# =========================
# OpenAI клиент (поддержка нового и старого SDK)
# =========================
openai_client = None
use_legacy_openai = False
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception:
    try:
        import openai as openai_legacy
        openai_legacy.api_key = OPENAI_API_KEY
        use_legacy_openai = True
    except Exception:
        openai_legacy = None
        use_legacy_openai = False

# =========================
# Инициализация бота
# =========================
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

user_memory: Dict[int, Deque[dict]] = defaultdict(lambda: deque(maxlen=MEMORY_SIZE))
continuations: Dict[Tuple[int, int], str] = {}

# =========================
# MarkdownV2 утилиты
# =========================
MDV2_SPECIAL = r'[_*[\]()~`>#+\-=|{}.!]'

def escape_markdown_v2(text: str) -> str:
    return re.sub(MDV2_SPECIAL, lambda m: '\\' + m.group(0), text or "")

def chunk_text(text: str, limit: int = TG_MESSAGE_LIMIT) -> List[str]:
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        chunk = text[i:i+limit]
        chunks.append(chunk)
        i += limit
    return chunks

async def safe_reply(message: Message, text: str):
    return await message.reply(text, parse_mode=PARSE_MODE, disable_web_page_preview=True)

# =========================
# Загрузка файлов с повторами
# =========================
async def download_file_with_retry(bot: Bot, file_path: str, max_retries: int = MAX_DOWNLOAD_RETRIES, delay: int = DOWNLOAD_RETRY_DELAY) -> BytesIO:
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            logging.info(f"Попытка загрузки файла {attempt + 1}/{max_retries + 1}")
            return await bot.download_file(file_path)
        except asyncio.TimeoutError as e:
            last_exc = e
            if attempt < max_retries:
                logging.warning(f"Тайм-аут при загрузке файла (попытка {attempt + 1}/{max_retries + 1}). Повтор через {delay} сек...")
                await asyncio.sleep(delay)
            else:
                logging.error(f"Не удалось загрузить файл после {max_retries + 1} попыток.")
                raise
        except Exception as e:
            logging.error(f"Ошибка при загрузке файла: {e}")
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Неизвестная ошибка загрузки файла")

# =========================
# Хендлеры
# =========================
@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    text = (
        f"Привет, {escape_markdown_v2(message.from_user.full_name)}\\! Я бот с искусственным интеллектом\\.\n"
        f"Команды:\n"
        f"• Просто задай вопрос для общения\n"
        f"Я могу распознавать изображения и файлы \\(PDF, TXT, MD и другие\\) \\- просто отправь их мне\\!\n"
        f"• Ограничения файлов: Максимум {MAX_FILE_SIZE_MB} MB и {MAX_TEXT_LENGTH // 1000}к символов\\."
    )
    await message.answer(text, parse_mode=PARSE_MODE, disable_web_page_preview=True)

@dp.message(F.photo | F.text | F.document)
async def handle_text_and_media(message: Message):
    # Игнорируем команды
    if message.text and message.text.startswith('/'):
        return

    images: List[str] = []
    if message.photo:
        photo = message.photo[-1]
        try:
            file = await bot.get_file(photo.file_id)
            file_data = await download_file_with_retry(bot, file.file_path)
            base64_image = base64.b64encode(file_data.read()).decode('utf-8')
            images.append(base64_image)
        except Exception as e:
            err = f"❌ Ошибка загрузки изображения: {str(e)}"
            await safe_reply(message, escape_markdown_v2(err))
            logging.error(f"Ошибка загрузки изображения от пользователя {message.from_user.id}: {e}")
            return

    # Текст/подпись
    prompt = ""
    if message.caption:
        prompt = message.caption
    elif message.text:
        prompt = message.text

    # Документы
    file_contents: List[str] = []
    if message.document:
        document = message.document
        fname = document.file_name or "document"
        fname_lower = fname.lower()
        is_supported = any(fname_lower.endswith(ext) for ext in SUPPORTED_TEXT_EXTENSIONS + ['.pdf'])
        if is_supported and document.file_size < MAX_FILE_SIZE_BYTES:
            try:
                file = await bot.get_file(document.file_id)
                file_data = await download_file_with_retry(bot, file.file_path)
                data = file_data.read()

                if any(fname_lower.endswith(ext) for ext in SUPPORTED_TEXT_EXTENSIONS):
                    text = data.decode('utf-8', errors='ignore')
                    if len(text) > MAX_TEXT_LENGTH:
                        text = text[:MAX_TEXT_LENGTH] + f"... [текст обрезан, максимум {MAX_TEXT_LENGTH} символов]"
                    file_contents.append(f"Содержимое файла {fname}:\n{text}")
                elif fname_lower.endswith('.pdf'):
                    try:
                        pdf_file = BytesIO(data)
                        reader = PdfReader(pdf_file)
                        text = ""
                        for page in reader.pages:
                            page_text = page.extract_text()
                            if page_text:
                                text += page_text + "\n"
                        if len(text) > MAX_TEXT_LENGTH:
                            text = text[:MAX_TEXT_LENGTH] + f"... [текст обрезан, максимум {MAX_TEXT_LENGTH} символов]"
                        file_contents.append(f"Содержимое файла {fname}:\n{text}")
                    except Exception as e:
                        err = f"❌ Ошибка чтения PDF: {str(e)}"
                        await safe_reply(message, escape_markdown_v2(err))
                        logging.error(f"Ошибка чтения PDF от пользователя {message.from_user.id}: {e}")
                        return
            except Exception as e:
                logging.error(f"Ошибка обработки файла от пользователя {message.from_user.id}: {e}")
                msg = f"❌ Ошибка обработки файла: {fname} - {str(e)}"
                await safe_reply(message, escape_markdown_v2(msg))
                return
        else:
            msg = "❌ Неподдерживаемый тип или размер файла превышает лимит."
            await safe_reply(message, escape_markdown_v2(msg))
            return

    # Собираем промпт
    if file_contents:
        joined = "\n".join(file_contents)
        prompt = f"{prompt}\n{joined}" if prompt else joined

    # В группах отвечаем только при упоминании/реплае
    if message.chat.type in ['group', 'supergroup']:
        bot_info = await bot.get_me()
        mentioned = False
        if prompt and f"@{bot_info.username}" in prompt:
            mentioned = True
            prompt = prompt.replace(f"@{bot_info.username}", "").strip()
        replied_to_bot = bool(message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == bot_info.id)
        if not mentioned and not replied_to_bot:
            return

    if not prompt and not images:
        await safe_reply(message, escape_markdown_v2("Пожалуйста, задайте вопрос или отправьте изображение/файл."))
        return

    try:
        user_id = message.from_user.id
        if prompt:
            user_memory[user_id].append({"role": "user", "content": prompt})

        chat_info = f"Группа: '{message.chat.title}'" if message.chat.type != 'private' else "Личные сообщения"
        logging.info(
            f"Новый запрос от '{message.from_user.full_name}' ({chat_info}). "
            f"Запрос: \"{prompt}\". Изображений: {len(images)}. Файлов: {len(file_contents)}. "
            f"Размер памяти: {len(user_memory[user_id])}"
        )

        # Печатает...
        try:
            await bot.send_chat_action(chat_id=message.chat.id, action=types.ChatActions.TYPING)
        except Exception:
            pass

        # Формируем сообщения для модели
        messages_payload = [
            {"role": "system", "content": "Вы Begemot AI от создателя Вексдор. Отвечайте максимально кратко, четко, без лишних слов. Один вопрос - одно короткое предложение."}
        ]
        messages_payload.extend(list(user_memory[user_id]))

        content = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        for img in images:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{img}",
                    "detail": "high"
                }
            })
        messages_payload.append({"role": "user", "content": content})

        # Вызов модели
        answer = ""
        if openai_client and not use_legacy_openai:
            resp = openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages_payload
            )
            answer = (resp.choices[0].message.content or "").strip()
        else:
            if not use_legacy_openai:
                raise RuntimeError("OpenAI SDK не инициализирован. Установите пакет 'openai'.")
            resp = openai_legacy.ChatCompletion.create(
                model=OPENAI_MODEL,
                messages=messages_payload
            )
            answer = (resp.choices[0].message["content"] or "").strip()

        user_memory[user_id].append({"role": "assistant", "content": answer})
        logging.info(f"Ответ ИИ: \"{answer[:200]}...\"")

        escaped = escape_markdown_v2(answer)
        if len(escaped) <= TG_MESSAGE_LIMIT:
            await message.reply(escaped, parse_mode=PARSE_MODE, disable_web_page_preview=True)
        else:
            builder = InlineKeyboardBuilder()
            builder.add(InlineKeyboardButton(
                text="Продолжить",
                callback_data="continue_response"
            ))
            chunks = chunk_text(escaped, TG_MESSAGE_LIMIT)
            sent_message = await message.reply(
                chunks[0],
                parse_mode=PARSE_MODE,
                reply_markup=builder.as_markup(),
                disable_web_page_preview=True
            )
            remaining = "".join(chunks[1:])
            continuations[(sent_message.chat.id, sent_message.message_id)] = remaining

    except Exception as e:
        err_text = str(e)
        if "model" in err_text.lower() and "not found" in err_text.lower():
            msg = f"Ошибка: Модель '{OPENAI_MODEL}' не найдена. Проверьте название модели."
            await safe_reply(message, escape_markdown_v2(msg))
            logging.error(f"Ошибка модели OpenAI: {e}")
        else:
            logging.error(f"Произошла ошибка при обработке запроса от пользователя {message.from_user.id}: {e}", exc_info=True)
            await safe_reply(message, escape_markdown_v2("Извините, произошла ошибка при обработке вашего запроса."))

@dp.callback_query(F.data == "continue_response")
async def process_continue_callback(callback_query: types.CallbackQuery):
    message = callback_query.message
    key = (message.chat.id, message.message_id)
    if key in continuations:
        remaining_text = continuations.pop(key)

        # Убираем кнопку
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=None
            )
        except Exception:
            pass

        if len(remaining_text) <= TG_MESSAGE_LIMIT:
            await message.reply(remaining_text, parse_mode=PARSE_MODE, disable_web_page_preview=True)
        else:
            builder = InlineKeyboardBuilder()
            builder.add(InlineKeyboardButton(
                text="Продолжить",
                callback_data="continue_response"
            ))
            chunks = chunk_text(remaining_text, TG_MESSAGE_LIMIT)
            new_message = await message.reply(
                chunks[0],
                parse_mode=PARSE_MODE,
                reply_markup=builder.as_markup(),
                disable_web_page_preview=True
            )
            rest = "".join(chunks[1:])
            continuations[(new_message.chat.id, new_message.message_id)] = rest

        try:
            await callback_query.answer()
        except Exception:
            pass
    else:
        try:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=None
            )
        except Exception:
            pass
        try:
            await callback_query.answer(text="Больше текста нет.", show_alert=True)
        except Exception:
            pass

# =========================
# main
# =========================
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s (%(filename)s:%(lineno)d)"
    )
    print("Бот запускается...")
    print(f"Используемая модель OpenAI: {OPENAI_MODEL}")
    print(f"Размер памяти: {MEMORY_SIZE} сообщений")
    print(f"Максимальное количество повторных попыток загрузки: {MAX_DOWNLOAD_RETRIES}")
    print(f"Максимальный размер файла: {MAX_FILE_SIZE_MB} МБ")
    print(f"Максимальная длина текста: {MAX_TEXT_LENGTH} символов")
    print(f"Режим форматирования: {PARSE_MODE}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
