import asyncio
import logging
import os
import re
import base64
import datetime
from io import BytesIO
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from dotenv import load_dotenv
import openai
from collections import defaultdict, deque
from PyPDF2 import PdfReader
# Принудительно загружаем переменные из .env, чтобы переопределить системные
load_dotenv(override=True)
# Получаем токены
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
# --- ДЛЯ ОТЛАДКИ: Проверяем, какие токены читаются ---
if TELEGRAM_BOT_TOKEN:
    print(f"Прочитан токен Telegram: '{TELEGRAM_BOT_TOKEN[:7]}...{TELEGRAM_BOT_TOKEN[-7:]}'")
else:
    print("Токен Telegram не найден в .env файле.")
if OPENAI_API_KEY:
    print(f"Прочитан ключ OpenAI: '{OPENAI_API_KEY[:5]}...{OPENAI_API_KEY[-5:]}'")
else:
    print("Ключ OpenAI не найден в .env файле.")
# ----------------------------------------------------
# Проверяем, что токены были загружены
if not TELEGRAM_BOT_TOKEN or not OPENAI_API_KEY:
    print("Ошибка: Убедитесь, что вы создали .env файл и указали в нем TELEGRAM_BOT_TOKEN и OPENAI_API_KEY")
    exit()
# --- НАСТРОЙКА МОДЕЛИ OPENAI ---
OPENAI_MODEL = "gpt-5-mini-2025-08-07" # Изменена модель
# --- НАСТРОЙКА ПАМЯТИ ---
# Количество сообщений, которые бот будет помнить для каждого пользователя
MEMORY_SIZE = 10
# --- НАСТРОЙКИ ПОВТОРНЫХ ПОПЫТОК ---
MAX_DOWNLOAD_RETRIES = 3 # Максимальное количество попыток загрузки файла
DOWNLOAD_RETRY_DELAY = 1  # Задержка между попытками в секундах
# --- НОВЫЕ НАСТРОЙКИ РАЗМЕРА ФАЙЛОВ И СИМВОЛОВ ---
MAX_FILE_SIZE_MB = 25  # Максимальный размер файла в МБ
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TEXT_LENGTH = 75000  # Максимальное количество символов
# --- СПИСОК ПОДДЕРЖИВАЕМЫХ ТЕКСТОВЫХ ФАЙЛОВ ---
SUPPORTED_TEXT_EXTENSIONS = [
    '.txt', '.md', '.py', '.csv', '.json', '.xml', '.yaml', '.yml',
    '.toml', '.log', '.tsv', '.sql', '.html', '.js', '.css', '.env', '.ts', '.svelte'
]
# Настраиваем OpenAI API
openai.api_key = OPENAI_API_KEY
# Инициализируем бота и диспетчер
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
# Словарь для хранения истории сообщений каждого пользователя
# Ключ - ID пользователя, значение - deque с историей
user_memory = defaultdict(lambda: deque(maxlen=MEMORY_SIZE))
# Словарь для хранения "продолжений" длинных ответов
continuations = {}
# --- Вспомогательная функция для загрузки файла с повторными попытками ---
async def download_file_with_retry(bot, file_path, max_retries=MAX_DOWNLOAD_RETRIES, delay=DOWNLOAD_RETRY_DELAY):
    """Загружает файл с повторными попытками при тайм-ауте."""
    for attempt in range(max_retries + 1):
        try:
            logging.info(f"Попытка загрузки файла {attempt + 1}/{max_retries + 1}")
            return await bot.download_file(file_path)
        except asyncio.TimeoutError as e:
            if attempt < max_retries:
                logging.warning(f"Тайм-аут при загрузке файла (попытка {attempt + 1}/{max_retries + 1}). Повтор через {delay}сек...")
                await asyncio.sleep(delay)
                # Можно увеличивать задержку экспоненциально, если нужно
                # delay *= 2 
            else:
                logging.error(f"Не удалось загрузить файл после {max_retries + 1} попыток.")
                raise e # Перевыбрасываем исключение, если попытки исчерпаны
        except Exception as e: # Ловим и другие возможные ошибки
             logging.error(f"Ошибка при загрузке файла: {e}")
             raise e
# Хендлер на команду /start
@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    await message.answer(f"Привет, {message.from_user.full_name}! Я бот с искусственным интеллектом. \n"
                         f"Команды:\n"
                         f"• Просто задай вопрос для общения\n"
                         f"Я могу распознавать изображения и файлы (PDF, TXT, MD и другие) - просто отправь мне их!\n"
                         f"• Ограничения файлов: Максимум {MAX_FILE_SIZE_MB} MB и {MAX_TEXT_LENGTH // 1000}к символов.")
# Хендлер для обработки текста, фото и документов
@dp.message(F.photo | F.text | F.document)
async def handle_text_and_photo(message: Message):
    # Игнорируем команды
    if message.text and message.text.startswith('/'):
        return
    # Обработка изображений
    images = []
    if message.photo:
        # Берем самое качественное изображение
        photo = message.photo[-1]
        try:
            file = await bot.get_file(photo.file_id)
            # Используем функцию с повторными попытками
            file_data = await download_file_with_retry(bot, file.file_path)
            base64_image = base64.b64encode(file_data.read()).decode('utf-8')
            images.append(base64_image)
        except Exception as e: # Ловим ошибку из download_file_with_retry
            await message.reply(f"❌ Ошибка загрузки изображения: {str(e)}")
            logging.error(f"Ошибка загрузки изображения от пользователя {message.from_user.id}: {e}")
            return # Прекращаем обработку этого сообщения
    # Обработка текста/подписи
    prompt = ""
    if message.caption:
        prompt = message.caption
    elif message.text:
        prompt = message.text
    # Обработка документов (PDF, TXT, MD и другие поддерживаемые)
    file_contents = []
    if message.document:
        document = message.document
        # Проверяем расширение файла и размер
        if (document.file_name and
            any(document.file_name.lower().endswith(ext) for ext in SUPPORTED_TEXT_EXTENSIONS + ['.pdf']) and
            document.file_size < MAX_FILE_SIZE_BYTES):
            try:
                file = await bot.get_file(document.file_id)
                # Используем функцию с повторными попытками
                file_data = await download_file_with_retry(bot, file.file_path)
                data = file_data.read()
                # Текстовые файлы
                if any(document.file_name.lower().endswith(ext) for ext in SUPPORTED_TEXT_EXTENSIONS):
                    text = data.decode('utf-8', errors='ignore')
                    if len(text) > MAX_TEXT_LENGTH:
                        text = text[:MAX_TEXT_LENGTH] + f"... [текст обрезан, максимум {MAX_TEXT_LENGTH} символов]"
                    file_contents.append(f"Содержимое файла {document.file_name}:\n{text}")
                # PDF файлы
                elif document.file_name.lower().endswith('.pdf'):
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
                        file_contents.append(f"Содержимое файла {document.file_name}:\n{text}")
                    except Exception as e:
                        await message.reply(f"❌ Ошибка чтения PDF: {str(e)}")
                        logging.error(f"Ошибка чтения PDF от пользователя {message.from_user.id}: {e}")
                        return # Прекращаем обработку этого сообщения
            except Exception as e: # Ловим ошибку из download_file_with_retry
                logging.error(f"Ошибка обработки файла от пользователя {message.from_user.id}: {e}")
                await message.reply(f"❌ Ошибка обработки файла: {document.file_name} - {str(e)}")
                return # Прекращаем обработку этого сообщения
    # Добавляем содержимое файлов к промпту
    if file_contents:
        if prompt:
            prompt += "\n" + "\n".join(file_contents)
        else:
            prompt = "\n".join(file_contents)
    # Проверяем, в группе ли сообщение
    if message.chat.type in ['group', 'supergroup']:
        bot_info = await bot.get_me()
        # В группе отвечаем только если упомянули бота или ответили на его сообщение
        if (prompt and f"@{bot_info.username}" in prompt) or (message.reply_to_message and message.reply_to_message.from_user.id == bot_info.id):
            if prompt:
                prompt = prompt.replace(f"@{bot_info.username}", "").strip()
        else:
            return # Не отвечаем на сообщения без упоминания в группе
    if not prompt and not images:
        await message.reply("Пожалуйста, задайте вопрос или отправьте изображение/файл.")
        return
    try:
        # Добавляем сообщение пользователя в память
        user_id = message.from_user.id
        if prompt:
            user_memory[user_id].append({"role": "user", "content": prompt})
        # --- Логирование запроса ---
        chat_info = f"Группа: '{message.chat.title}'" if message.chat.type != 'private' else "Личные сообщения"
        logging.info(f"Новый запрос от '{message.from_user.full_name}' ({chat_info}). Запрос: \"{prompt}\". Изображений: {len(images)}. Файлов: {len(file_contents)}. Размер памяти: {len(user_memory[user_id])}")
        # Показываем "печатает..."
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        # Формируем сообщения для OpenAI (системное + история)
        messages = [
            {"role": "system", "content": "Вы Begemot AI от создателя Вексдор. Отвечайте максимально кратко, четко, без лишних слов. Один вопрос - одно короткое предложение."} # Обновлённый системный промпт
        ]
        # Добавляем историю пользователя
        messages.extend(list(user_memory[user_id]))
        # Формируем мультимодальный контент
        content = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        for img in images:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{img}", # Исправлен префикс
                    "detail": "high"
                }
            })
        # Добавляем мультимодальное сообщение
        messages.append({"role": "user", "content": content})
        # Отправляем запрос в OpenAI
        # Убран параметр temperature
        response = openai.chat.completions.create(
            model=OPENAI_MODEL, # Используем новую модель
            messages=messages
        )
        answer = response.choices[0].message.content
        # Добавляем ответ бота в память
        user_memory[user_id].append({"role": "assistant", "content": answer})
        # --- Логирование ответа ---
        logging.info(f"Ответ ИИ: \"{answer[:100]}...\"") # Логируем начало ответа
        # Отправляем ответ
        if len(answer) <= 4096:
            await message.reply(answer)
        else:
            # Создаем кнопку
            builder = InlineKeyboardBuilder()
            builder.add(InlineKeyboardButton(
                text="Продолжить",
                callback_data="continue_response"
            ))
            sent_message = await message.reply(answer[:4096], reply_markup=builder.as_markup())
            # Сохраняем остаток текста, используя ID сообщения и чата как ключ
            continuations[(sent_message.chat.id, sent_message.message_id)] = answer[4096:]
    except openai.NotFoundError as e:
        await message.reply(f"Ошибка: Модель '{OPENAI_MODEL}' не найдена. Проверьте название в файле main.py.")
        logging.error(f"Ошибка модели OpenAI: {e}")
    except Exception as e:
        logging.error(f"Произошла ошибка при обработке запроса от пользователя {message.from_user.id}: {e}", exc_info=True) # exc_info=True для полного трейса
        await message.reply("Извините, произошла ошибка при обработке вашего запроса.")
# Хендлер для кнопки "Продолжить"
@dp.callback_query(F.data == "continue_response")
async def process_continue_callback(callback_query: types.CallbackQuery):
    message = callback_query.message
    key = (message.chat.id, message.message_id)
    if key in continuations:
        remaining_text = continuations.pop(key)
        # Убираем кнопку со старого сообщения
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=None
        )
        # Отправляем следующую часть
        if len(remaining_text) <= 4096:
            await message.reply(remaining_text)
        else:
            builder = InlineKeyboardBuilder()
            builder.add(InlineKeyboardButton(
                text="Продолжить",
                callback_data="continue_response"
            ))
            new_message = await message.reply(remaining_text[:4096], reply_markup=builder.as_markup())
            continuations[(new_message.chat.id, new_message.message_id)] = remaining_text[4096:]
        # Отвечаем на callback, чтобы убрать "часики" у кнопки
        await callback_query.answer()
    else:
        # Если текста нет, просто убираем кнопку
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=None
        )
        await callback_query.answer(text="Больше текста нет.", show_alert=True)
# Основная функция запуска
async def main() -> None:
    # Включаем логирование
    logging.basicConfig(
        level=logging.INFO, # Можно изменить на DEBUG для более подробной информации
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s (%(filename)s:%(lineno)d)" # Добавлены файл и строка
    )
    print("Бот запускается...")
    print(f"Используемая модель OpenAI: {OPENAI_MODEL}")
    print(f"Размер памяти: {MEMORY_SIZE} сообщений")
    print(f"Максимальное количество повторных попыток загрузки: {MAX_DOWNLOAD_RETRIES}")
    print(f"Максимальный размер файла: {MAX_FILE_SIZE_MB} МБ")
    print(f"Максимальная длина текста: {MAX_TEXT_LENGTH} символов")
    # Запускаем бота
    await dp.start_polling(bot)
if __name__ == "__main__":
    asyncio.run(main())