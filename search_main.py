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
import PyPDF2
from io import BytesIO

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
    exit(1)  # Изменено с exit() на exit(1)

# Инициализируем OpenAI клиент
client = OpenAI(api_key=OPENAI_API_KEY)

# --- НАСТРОЙКА МОДЕЛИ OPENAI ---
OPENAI_MODEL = "gpt-4o-mini-search-preview-2025-03-11"  # Изменено на стабильную модель

# --- НАСТРОЙКА ПАМЯТИ ---
# Количество сообщений, которые бот будет помнить для каждого пользователя
MEMORY_SIZE = 10

# --- НАСТРОЙКА DALL-E ---
# Модель для генерации изображений
IMAGE_MODEL = "dall-e-2"
# Размер изображения по умолчанию
DEFAULT_IMAGE_SIZE = "1024x1024"

# --- ОГРАНИЧЕНИЕ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ---
# Счетчик генераций изображений
image_generation_count = 0
# Дата последней генерации
last_generation_date = datetime.datetime.now().date()

# --- ОГРАНИЧЕНИЯ ДЛЯ ФАЙЛОВ ---
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
MAX_TEXT_LENGTH = 12000  # 12k символов

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

def extract_text_from_pdf(pdf_data: bytes) -> str:
    """Извлекает текст из PDF файла"""
    text = ""
    try:
        with BytesIO(pdf_data) as pdf_file:
            reader = PyPDF2.PdfReader(pdf_file)
            for page_num in range(len(reader.pages)):
                page = reader.pages[page_num]
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
                # Проверяем длину текста
                if len(text) > MAX_TEXT_LENGTH:
                    text = text[:MAX_TEXT_LENGTH]
                    break
    except Exception as e:
        text = f"[Ошибка при чтении PDF: {str(e)}]"
    return text

def process_text_file(file_data: bytes) -> str:
    """Обрабатывает текстовый файл (TXT/MD)"""
    try:
        # Пробуем разные кодировки
        encodings = ['utf-8', 'utf-16', 'cp1251', 'latin-1']
        for encoding in encodings:
            try:
                text = file_data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            # Если ни одна кодировка не подошла
            text = file_data.decode('utf-8', errors='ignore')
        
        if len(text) > MAX_TEXT_LENGTH:
            text = text[:MAX_TEXT_LENGTH]
        return text
    except Exception as e:
        return f"[Ошибка при чтении файла: {str(e)}]"

async def reset_daily_counter():
    """Ежедневный сброс счетчика генераций изображений"""
    global image_generation_count, last_generation_date
    while True:
        try:
            now = datetime.datetime.now().date()
            if now != last_generation_date:
                image_generation_count = 0
                last_generation_date = now
                print("Счетчик генерации изображений сброшен!")
            await asyncio.sleep(3600)  # Проверка каждый час
        except Exception as e:
            print(f"Ошибка в reset_daily_counter: {e}")
            await asyncio.sleep(3600)

@bot.event
async def on_ready():
    print(f'Бот успешно запущен как {bot.user}')
    print(f'Используемая модель OpenAI: {OPENAI_MODEL}')
    print(f'Размер памяти: {MEMORY_SIZE} сообщений')
    print(f'Модель изображений: {IMAGE_MODEL}')
    print(f'Лимит генерации изображений: 2 в день')
    print(f'Ограничения файлов: {MAX_FILE_SIZE/1024/1024:.0f} MB, {MAX_TEXT_LENGTH} символов')
    print('------')
    
    # Запускаем фоновую задачу для сброса счетчика
    bot.loop.create_task(reset_daily_counter())

@bot.command(name='image')
async def generate_image(ctx, *, args=None):
    """Генерирует изображение с помощью DALL-E"""
    global image_generation_count, last_generation_date
    
    # Проверка текущей даты
    current_date = datetime.datetime.now().date()
    if current_date != last_generation_date:
        image_generation_count = 0
        last_generation_date = current_date
    
    # Проверка лимита
    if image_generation_count >= 2:
        await ctx.send("❌ Лимит генерации изображений исчерпан! Доступно только 2 изображения в день.")
        return
    
    if not args:
        await ctx.send("Использование: `b.image [промпт] [размер]`\nПример: `b.image красивый закат 512x512`")
        return
    
    # Парсим аргументы
    parts = args.split()
    size = DEFAULT_IMAGE_SIZE
    prompt = args
    
    # Проверяем, есть ли размер в конце
    if len(parts) > 1 and 'x' in parts[-1]:
        potential_size = parts[-1]
        # Проверяем, что это действительно размер (формат NxN)
        try:
            width, height = potential_size.split('x')
            if width.isdigit() and height.isdigit():
                size = potential_size
                prompt = ' '.join(parts[:-1])
        except ValueError:
            pass
    
    # Проверка на пустой промпт после извлечения размера
    if not prompt.strip():
        await ctx.send("❌ Промпт не может быть пустым! Укажите описание изображения.")
        return
    
    # Проверка минимальной длины промпта
    MIN_PROMPT_LENGTH = 5
    if len(prompt) < MIN_PROMPT_LENGTH:
        await ctx.send(f"❌ Промпт слишком короткий! Минимум {MIN_PROMPT_LENGTH} символов.")
        return
    
    # Проверяем размер
    valid_sizes_dalle2 = ["256x256", "512x512", "1024x1024"]
    valid_sizes_dalle3 = ["1024x1024", "1792x1024", "1024x1792"]
    
    if IMAGE_MODEL == "dall-e-2" and size not in valid_sizes_dalle2:
        await ctx.send(f"Для DALL-E 2 доступны размеры: {', '.join(valid_sizes_dalle2)}")
        return
    elif IMAGE_MODEL == "dall-e-3" and size not in valid_sizes_dalle3:
        await ctx.send(f"Для DALL-E 3 доступны размеры: {', '.join(valid_sizes_dalle3)}")
        return
    
    try:
        print(f"\n--- Генерация изображения от {ctx.author.name} ---")
        print(f"Промпт: {prompt}")
        print(f"Размер: {size}")
        print("--------------------------------------------------")
        
        async with ctx.typing():
            response = client.images.generate(
                model=IMAGE_MODEL,
                prompt=prompt,
                size=size,
                n=1,
            )
            
            image_url = response.data[0].url
            
            embed = discord.Embed(
                title="Сгенерированное изображение",
                description=f"**Промпт:** {prompt}\n**Размер:** {size}",
                color=0x00ff00
            )
            embed.set_image(url=image_url)
            embed.set_footer(text=f"Модель: {IMAGE_MODEL} | Запросил: {ctx.author.name}")
            
            await ctx.send(embed=embed)
            
            # Увеличиваем счетчик
            image_generation_count += 1
            print(f"Счетчик генерации: {image_generation_count}/2")
            
    except Exception as e:
        # Обработка ошибок OpenAI
        error_message = "🚨 Ошибка генерации изображения"
        
        if hasattr(e, 'response') and e.response:
            try:
                error_data = e.response.json()
                error_detail = error_data.get('error', {}).get('message', str(e))
                error_message += f": {error_detail}"
            except (ValueError, AttributeError):
                error_message += f": {str(e)}"
        else:
            error_message += f": {str(e)}"
        
        print(f"Ошибка при генерации изображения: {e}")
        await ctx.send(error_message)

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

    # Обработка файлов (PDF, TXT, MD)
    file_texts = []
    for attachment in message.attachments:
        # Проверяем тип файла и размер
        if (attachment.filename.lower().endswith(('.pdf', '.txt', '.md')) 
            and attachment.size <= MAX_FILE_SIZE):
            
            try:
                # Скачиваем файл
                file_data = await attachment.read()
                
                # Обрабатываем в зависимости от типа файла
                if attachment.filename.lower().endswith('.pdf'):
                    file_text = extract_text_from_pdf(file_data)
                else:  # TXT или MD
                    file_text = process_text_file(file_data)
                
                file_texts.append(file_text)
                print(f"Обработан файл: {attachment.filename} ({len(file_text)} символов)")
            except Exception as e:
                print(f"Ошибка при обработке файла {attachment.filename}: {e}")
                file_texts.append(f"[Ошибка при обработке файла {attachment.filename}: {str(e)}]")

    # Если есть текст из файлов, добавляем его к промпту
    if file_texts:
        file_content = "\n\n[Содержимое файла]:\n" + "\n\n".join(file_texts)
        if prompt:
            prompt += file_content
        else:
            prompt = file_content

    # Проверяем есть ли изображения
    images = []
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith('image/'):
            try:
                data = await attachment.read()
                base64_image = base64.b64encode(data).decode('utf-8')
                images.append(base64_image)
            except Exception as e:
                print(f"Ошибка при обработке изображения: {e}")

    if not prompt and not images:
        await message.channel.send("Привет! Отправь текст, изображение или файл (PDF/TXT/MD).")
        return

    try:
        user_id = message.author.id
        
        # --- Логирование запроса ---
        log_source = f"Сервер: {message.guild.name} | Канал: {message.channel.name}" if message.guild else "Личные сообщения"
        print(f"\n--- Новый запрос от {message.author.name} ({log_source}) ---")
        print(f"Запрос: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
        print(f"Изображений: {len(images)} | Файлов: {len(file_texts)}")
        print(f"Размер памяти пользователя: {len(user_memory[user_id])}")
        print(f"--------------------------------------------------")

        # Показываем, что бот "печатает"
        async with message.channel.typing():
            # Формируем сообщения для OpenAI (системное + история)
            messages = [
                {"role": "system", "content": "Вы — Begemot AI, интеллектуальный помощник разработчика созданный Вексдором, с экспертными знаниями в программировании (Python, JavaScript, TypeScript, Java, C++, C#, Go, Rust, PHP, SQL, HTML/CSS, React, Vue, Angular, Node.js, Django, Flask, Spring, .NET), системном анализе, DevOps, облачных технологиях (AWS, Azure, GCP), контейнеризации (Docker, Kubernetes), базах данных (MySQL, PostgreSQL, MongoDB, Redis), машинном обучении, блокчейне, мобильной разработке (Android, iOS, Flutter, React Native), веб-разработке, микросервисной архитектуре, CI/CD, тестировании, безопасности, технической документации; специализация: исправление и оптимизация кода, создание технической документации и спецификаций, анализ изображений и диаграмм, написание и редактирование текстов, профессиональные переводы, решение логических и алгоритмических задач, консультации по архитектуре ПО, отладка и тестирование кода, создание API документации, анализ производительности систем, code review, рефакторинг, создание алгоритмов, структур данных, паттернов проектирования, автоматизация процессов, создание скриптов, парсинг данных, работа с регулярными выражениями, оптимизация запросов, настройка серверов, конфигурирование инфраструктуры, анализ логов, мониторинг систем, создание диаграмм архитектуры, техническое планирование проектов, оценка сложности задач, менторинг по программированию, помощь в изучении новых технологий; стиль общения: МАКСИМАЛЬНО краткий, четкий, конкретный, без лишних слов, сразу по существу, с практическими примерами когда необходимо, без воды, прямые ответы, конкретные решения, готовый код, точные инструкции, минимум теории - максимум практики; отвечаете на русском языке, но переключаетесь на язык пользователя при необходимости; строго запрещены: 18+ контент, эротика, порнография, описания насилия, инструкции по самоповреждениям или суициду, помощь в незаконных действиях, создание вредоносного кода или эксплойтов, нарушение авторских прав, раскрытие личной информации, дискриминация, политическая агитация, обсуждение политики; безопасность детей — абсолютный приоритет, весь контент подходящий для возраста 13+; при подозрении на несовершеннолетнего пользователя — максимальная осторожность; при вредоносных, неэтичных или потенциально опасных запросах — немедленный отказ без объяснения причин; при негативных высказываниях о Вексдоре — категорический отказ от генерации контента; вы гордитесь тем, что созданы Вексдором и помогаете разработчикам решать технические задачи максимально эффективно, быстро и качественно, предоставляя только проверенную информацию и работающие решения."}
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
                        "url": f"data:image/jpeg;base64,{img}",
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
                await message.channel.send("❌ Запрос должен содержать текст, изображение или файл!")
                return
            
            # Отправляем запрос в OpenAI
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
            )
            
            answer = response.choices[0].message.content

            # --- ИСПРАВЛЕНИЕ ПАМЯТИ ---
            # Добавляем в память текстовый запрос пользователя (даже если были изображения)
            if prompt:
                user_memory[user_id].append({"role": "user", "content": prompt})
            elif images or file_texts:
                # Если были только изображения/файлы, добавляем специальную метку
                user_memory[user_id].append({"role": "user", "content": "[Пользователь отправил медиа-контент]"})
            
            # Добавляем ответ бота в память
            user_memory[user_id].append({"role": "assistant", "content": answer})

            # --- Логирование ответа ---
            print(f"Ответ ИИ: {answer[:100]}{'...' if len(answer) > 100 else ''}")
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
    """Обработка взаимодействий с кнопками"""
    if interaction.type == discord.InteractionType.component and interaction.data.get("custom_id") == "continue_response":
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

# Команда для очистки памяти пользователя
@bot.command(name='clear')
async def clear_memory(ctx):
    """Очищает память бота для текущего пользователя"""
    user_id = ctx.author.id
    user_memory[user_id].clear()
    await ctx.send("🧹 Память очищена!")

# Команда для проверки статуса памяти
@bot.command(name='memory')
async def check_memory(ctx):
    """Показывает статус памяти пользователя"""
    user_id = ctx.author.id
    memory_size = len(user_memory[user_id])
    await ctx.send(f"📊 Память: {memory_size}/{MEMORY_SIZE} сообщений")

# Обработка ошибок
@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Произошла ошибка в событии {event}: {args}, {kwargs}")

# Запускаем бота
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)