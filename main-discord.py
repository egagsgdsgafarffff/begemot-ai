# discord_bot.py
import discord
from discord.ui import View, Button
from discord.ext import commands
import os
import base64
from openai import OpenAI
from dotenv import load_dotenv
from collections import defaultdict, deque
import datetime
import asyncio
from io import BytesIO
from PyPDF2 import PdfReader

# Загружаем переменные окружения из .env файла 
load_dotenv(override=True)

# Получаем токены из переменных окружения
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# --- ДЛЯ ОТЛАДКИ: Проверяем, какой токен читается ---
if DISCORD_BOT_TOKEN:
    print(f"Прочитан токен Discord: '{DISCORD_BOT_TOKEN[:7]}...{DISCORD_BOT_TOKEN[-7:]}'")
else:
    print("Токен Discord не найден в .env файле.")
# ----------------------------------------------------

# Проверяем, что токены были загружены
if not DISCORD_BOT_TOKEN or not OPENAI_API_KEY:
    print("Ошибка: Убедитесь, что вы создали .env файл и указали в нем DISCORD_BOT_TOKEN и OPENAI_API_KEY")
    exit()

# Инициализируем OpenAI клиент
client = OpenAI(api_key=OPENAI_API_KEY)

# --- НАСТРОЙКА МОДЕЛИ OPENAI ---
OPENAI_MODEL = "gpt-5-mini-2025-08-07" # Изменена модель

# --- НАСТРОЙКА ПАМЯТИ ---
# Количество сообщений, которые бот будет помнить для каждого пользователя
MEMORY_SIZE = 10

# Задаем необходимые разрешения для бота
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='b.', intents=intents)

# Словарь для хранения истории сообщений каждого пользователя
# Ключ - ID пользователя, значение - deque с историей
user_memory = defaultdict(lambda: deque(maxlen=MEMORY_SIZE))

# Словарь для хранения "продолжений" длинных ответов
continuations = {}

# --- НОВЫЕ НАСТРОЙКИ РАЗМЕРА ФАЙЛОВ И СИМВОЛОВ ---
MAX_FILE_SIZE_MB = 10  # Максимальный размер файла в МБ
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_TEXT_LENGTH = 30000  # Максимальное количество символов

# --- СПИСОК ПОДДЕРЖИВАЕМЫХ ТЕКСТОВЫХ ФАЙЛОВ ---
SUPPORTED_TEXT_EXTENSIONS = [
    '.txt', '.md', '.py', '.csv', '.json', '.xml', '.yaml', '.yml',
    '.toml', '.log', '.tsv', '.sql', '.html', '.js', '.css', '.env', '.ts', '.svelte'
]

@bot.event
async def on_ready():
    print(f'Бот успешно запущен как {bot.user}')
    print(f"Используемая модель OpenAI: {OPENAI_MODEL}")
    print(f'Размер памяти: {MEMORY_SIZE} сообщений')
    print(f'Максимальный размер файла: {MAX_FILE_SIZE_MB} МБ')
    print(f'Максимальная длина текста: {MAX_TEXT_LENGTH} символов')
    print('------')

@bot.event
async def on_message(message):
    # Обрабатываем команды
    await bot.process_commands(message)

    # Игнорируем сообщения от самого бота и команды
    if message.author == bot.user or message.content.startswith('b.'):
        return

    # Бот должен реагировать только если это личное сообщение или его упомянули на сервере
    if message.guild is not None and not bot.user.mentioned_in(message):
        return

    # Получаем текст запроса, убирая упоминание, если оно есть
    if message.guild is not None:
        prompt = message.content.replace(f'<@!{bot.user.id}>', '').replace(f'<@{bot.user.id}>', '').strip()
    else:
        prompt = message.content.strip()

    # Проверяем есть ли изображения
    images = []
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith('image/'):
            data = await attachment.read()
            base64_image = base64.b64encode(data).decode('utf-8')
            images.append(base64_image)

    # Обработка файлов (PDF, TXT, MD и другие поддерживаемые)
    file_contents = []
    for attachment in message.attachments:
        # Проверяем расширение файла и размер
        if (any(attachment.filename.lower().endswith(ext) for ext in SUPPORTED_TEXT_EXTENSIONS + ['.pdf']) and
            attachment.size < MAX_FILE_SIZE_BYTES):
            try:
                # Скачиваем вложение
                data = await attachment.read()

                # Обработка текстовых файлов
                if any(attachment.filename.lower().endswith(ext) for ext in SUPPORTED_TEXT_EXTENSIONS):
                    text = data.decode('utf-8', errors='ignore')
                    if len(text) > MAX_TEXT_LENGTH:  # Ограничение длины
                        text = text[:MAX_TEXT_LENGTH] + f"... [текст обрезан, максимум {MAX_TEXT_LENGTH} символов]"
                    file_contents.append(f"Содержимое файла {attachment.filename}:\n{text}")

                # Обработка PDF
                elif attachment.filename.lower().endswith('.pdf'):
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
                        file_contents.append(f"Содержимое файла {attachment.filename}:\n{text}")
                    except Exception as e:
                        await message.channel.send(f"❌ Ошибка чтения PDF: {str(e)}")
                        return
            except Exception as e:
                print(f"Ошибка обработки файла: {e}")
                await message.channel.send(f"❌ Ошибка обработки файла: {attachment.filename}")

    # Добавляем содержимое файлов к промпту
    if file_contents:
        if prompt:
            prompt += "\n" + "\n".join(file_contents)
        else:
            prompt = "\n".join(file_contents)

    if not prompt and not images:
        await message.channel.send("Привет! Отправь текст или изображение.")
        return

    try:
        user_id = message.author.id

        # --- Логирование запроса ---
        log_source = f"Сервер: {message.guild.name} | Канал: {message.channel.name}" if message.guild else "Личные сообщения"
        print(f"\n--- Новый запрос от {message.author.name} ({log_source}) ---")
        print(f"Запрос: {prompt}")
        print(f"Изображений: {len(images)}")
        print(f"Файлов: {len(file_contents)}")
        print(f"Размер памяти пользователя: {len(user_memory[user_id])}")
        print(f"--------------------------------------------------")

        # Показываем, что бот "печатает"
        async with message.channel.typing():
            # Формируем сообщения для OpenAI (системное + история)
            messages = [
                {"role": "system", "content": "Вы Begemot AI от создателя Вексдор. Отвечайте максимально кратко, четко, без лишних слов. Один вопрос - одно короткое предложение."} # Обновлённый системный промпт
            ]
            # Добавляем историю пользователя
            messages.extend(list(user_memory[user_id]))

            # Формируем мультимодальный контент для текущего запроса
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
            if content:  # Только если есть контент
                messages.append({
                    "role": "user",
                    "content": content
                })
            else:
                await message.channel.send("❌ Запрос должен содержать текст или изображение!")
                return

            # Отправляем запрос в OpenAI
            # Убран параметр temperature
            response = client.chat.completions.create(
                model=OPENAI_MODEL, # Используем новую модель
                messages=messages
            )
            answer = response.choices[0].message.content

            # --- ИСПРАВЛЕНИЕ ПАМЯТИ ---
            # Добавляем в память текстовый запрос пользователя (даже если были изображения)
            if prompt:
                user_memory[user_id].append({"role": "user", "content": prompt})
            elif images:
                # Если был только изображение, добавляем специальную метку
                user_memory[user_id].append({"role": "user", "content": "[Пользователь отправил изображение]"})

            # Добавляем ответ бота в память
            user_memory[user_id].append({"role": "assistant", "content": answer})

            # --- Логирование ответа ---
            print(f"Ответ ИИ: {answer}")
            print(f"Обновленный размер памяти: {len(user_memory[user_id])}")

            # Отправляем ответ в Discord (с разбивкой, если нужно)
            if len(answer) <= 2000:
                await message.channel.send(answer)
            else:
                # Отправляем первую часть и кнопку
                view = View(timeout=None)
                button = Button(label="Продолжить", style=discord.ButtonStyle.primary, custom_id="continue_response")
                view.add_item(button)
                sent_message = await message.channel.send(answer[:2000], view=view)
                continuations[sent_message.id] = answer[2000:]

    except Exception as e:
        print(f"Произошла ошибка: {e}")
        await message.channel.send("Извините, произошла ошибка при обработке вашего запроса.")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component and interaction.data["custom_id"] == "continue_response":
        message_id = interaction.message.id
        if message_id in continuations:
            remaining_text = continuations.pop(message_id)

            # Отключаем кнопку на старом сообщении
            try:
                view = View.from_message(interaction.message)
                for item in view.children:
                    if isinstance(item, Button) and item.custom_id == "continue_response":
                        item.disabled = True
                await interaction.message.edit(view=view)
            except Exception as e:
                print(f"Не удалось обновить старое сообщение: {e}")

            # Отправляем следующую часть
            if len(remaining_text) <= 2000:
                await interaction.response.send_message(remaining_text)
            else:
                new_view = View(timeout=None)
                new_button = Button(label="Продолжить", style=discord.ButtonStyle.primary, custom_id="continue_response")
                new_view.add_item(new_button)
                await interaction.response.send_message(remaining_text[:2000], view=new_view)
                new_message = await interaction.original_response()
                continuations[new_message.id] = remaining_text[2000:]
        else:
            await interaction.response.edit_message(content=interaction.message.content, view=None)

# Запускаем бота
bot.run(DISCORD_BOT_TOKEN)
