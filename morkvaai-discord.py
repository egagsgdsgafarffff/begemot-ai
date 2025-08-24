import asyncio
import logging
from typing import Dict, List, Optional
import aiohttp
import discord
from discord.ext import commands
import json
import re # Добавлен импорт re для обработки упоминаний

# Настройки
BOT_TOKEN = ""  # Замените на ваш токен Discord бота
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"  # URL LM Studio API
MODEL_NAME = "qwen/qwen3-4b"  # Имя модели в LM Studio

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Хранилище контекста разговоров для каждого пользователя
user_contexts: Dict[int, List[Dict[str, str]]] = {}

# Хранилище системных промптов для каждого пользователя
user_system_prompts: Dict[int, str] = {}

# Состояние настройки системного промпта
users_setting_system: set = set()

# Системный промпт по умолчанию
DEFAULT_SYSTEM_PROMPT = "Ты самый депрессивный ИИ-ассистент. На всё отвечай максимально депрессивно и создавай депрессивную атмосферу. Тебе уже ничего не хочется в этой жизни."

class LMStudioClient:
    """Клиент для работы с LM Studio API"""
    def __init__(self, base_url: str, model_name: str):
        self.base_url = base_url
        self.model_name = model_name

    async def generate_response(self, messages: List[Dict[str, str]], 
                              max_tokens: int = 1000, 
                              temperature: float = 0.7) -> str:
        """Генерация ответа от локальной модели"""
        # --- Удаление <think> блоков из сообщений перед отправкой в модель ---
        processed_messages = []
        for msg in messages:
            content_without_think = re.sub(r'<think>.*?</think>', '', msg.get("content", ""), flags=re.DOTALL).strip()
            processed_messages.append({"role": msg["role"], "content": content_without_think})
        # ---------------------------------------------------------------------
        
        payload = {
            "model": self.model_name,
            "messages": processed_messages, # Используем обработанные сообщения
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.base_url, 
                    json=payload,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status != 200:
                        logger.error(f"LM Studio API error: {response.status}")
                        return "Извините, произошла ошибка при обращении к ИИ модели."
                    data = await response.json()
                    raw_response = data["choices"][0]["message"]["content"]
                    # --- Удаление <think> блоков из ответа модели ---
                    final_response = re.sub(r'<think>.*?</think>', '', raw_response, flags=re.DOTALL).strip()
                    # --------------------------------------------------
                    return final_response
        except aiohttp.ClientError as e:
            logger.error(f"Ошибка соединения с LM Studio: {e}")
            return "Не удалось подключиться к ИИ модели. Проверьте, что LM Studio запущен."
        except Exception as e:
            logger.error(f"Неожиданная ошибка: {e}")
            return "Произошла неожиданная ошибка."

def get_user_context(user_id: int) -> List[Dict[str, str]]:
    """Получить контекст разговора пользователя"""
    if user_id not in user_contexts:
        # Инициализируем контекст с системным промптом
        system_prompt = get_user_system_prompt(user_id)
        user_contexts[user_id] = [
            {"role": "system", "content": system_prompt}
        ]
    return user_contexts[user_id]

def get_user_system_prompt(user_id: int) -> str:
    """Получить системный промпт пользователя"""
    return user_system_prompts.get(user_id, DEFAULT_SYSTEM_PROMPT)

def set_user_system_prompt(user_id: int, prompt: str):
    """Установить системный промпт для пользователя"""
    user_system_prompts[user_id] = prompt
    # Обновляем контекст с новым системным промптом
    if user_id in user_contexts:
        user_contexts[user_id] = [
            {"role": "system", "content": prompt}
        ]

def add_to_context(user_id: int, role: str, content: str, max_context_length: int = 10):
    """Добавить сообщение в контекст пользователя"""
    # --- Удаление <think> блоков из добавляемого сообщения ---
    content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    # ---------------------------------------------------------
    context = get_user_context(user_id)
    context.append({"role": role, "content": content})
    # Ограничиваем размер контекста
    if len(context) > max_context_length:
        # Удаляем самое старое сообщение (но не системный промпт)
        if len(context) > 1:
            context.pop(1)

def clear_user_context(user_id: int):
    """Очистить контекст пользователя (кроме системного промпта)"""
    system_prompt = get_user_system_prompt(user_id)
    user_contexts[user_id] = [
        {"role": "system", "content": system_prompt}
    ]

# Настройка Discord бота
# intents.message_content = True теперь обязательно для обработки содержимого сообщений
intents = discord.Intents.default()
intents.message_content = True
# Используем упоминание бота как префикс команды
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)

# Инициализация клиента LM Studio
lm_client = LMStudioClient(LM_STUDIO_URL, MODEL_NAME)

@bot.event
async def on_ready():
    """Событие готовности бота"""
    logger.info(f'{bot.user} подключился к Discord!')
    print(f'{bot.user} подключился к Discord!')

# Команды теперь вызываются через @ИмяБота m.команда
@bot.command(name='start')
async def start_command(ctx):
    """Команда начала работы"""
    user_id = ctx.author.id
    clear_user_context(user_id)
    embed = discord.Embed(
        title="🤖 Discord бот с ИИ",
        description="Привет! Я бот с интеграцией локальной ИИ модели через LM Studio.",
        color=0x00ff00
    )
    embed.add_field(
        name="📋 Доступные команды:",
        value=(
            "`@ИмяБота m.start` - начать заново\n"
            "`@ИмяБота m.clear` - очистить историю разговора\n"
            "`@ИмяБота m.system` - настроить системный промпт\n"
            "`@ИмяБота m.show_system` - показать текущий системный промпт\n"
            "`@ИмяБота m.reset_system` - сбросить системный промпт к умолчанию\n"
            "`@ИмяБота m.status` - проверить статус подключения к модели\n"
            "`@ИмяБота m.info` - показать справку\n"
            "`@ИмяБота m.cancel` - отменить настройку системного промпта"
        ),
        inline=False
    )
    embed.add_field(
        name="💬 Общение:",
        value="Упомяните меня и напишите сообщение, и я отвечу с помощью локальной ИИ модели!",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command(name='system')
async def system_prompt_command(ctx):
    """Команда настройки системного промпта"""
    user_id = ctx.author.id
    users_setting_system.add(user_id)
    embed = discord.Embed(
        title="🔧 Настройка системного промпта",
        description="Системный промпт определяет поведение и роль ИИ-ассистента.",
        color=0xffa500
    )
    embed.add_field(
        name="📝 Примеры системных промптов:",
        value=(
            "• \"Вы опытный программист, который помогает с кодом\"\n"
            "• \"Вы креативный писатель, создающий интересные истории\"\n"
            "• \"Вы строгий учитель, который объясняет сложные темы простыми словами\""
        ),
        inline=False
    )
    embed.add_field(
        name="⚡ Инструкция:",
        value="Напишите новый системный промпт в следующем сообщении или используйте `@ИмяБота m.cancel` для отмены.",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command(name='show_system')
async def show_system_prompt_command(ctx):
    """Показать текущий системный промпт"""
    user_id = ctx.author.id
    current_prompt = get_user_system_prompt(user_id)
    embed = discord.Embed(
        title="📋 Текущий системный промпт",
        description=f"```{current_prompt}```",
        color=0x0099ff
    )
    embed.add_field(
        name="💡 Подсказка:",
        value="Используйте `@ИмяБота m.system` для изменения или `@ИмяБота m.reset_system` для сброса к умолчанию.",
        inline=False
    )
    await ctx.send(embed=embed)

@bot.command(name='reset_system')
async def reset_system_prompt_command(ctx):
    """Сбросить системный промпт к умолчанию"""
    user_id = ctx.author.id
    set_user_system_prompt(user_id, DEFAULT_SYSTEM_PROMPT)
    embed = discord.Embed(
        title="🔄 Системный промпт сброшен",
        description=f"Системный промпт сброшен к умолчанию:\n```{DEFAULT_SYSTEM_PROMPT}```",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@bot.command(name='cancel')
async def cancel_command(ctx):
    """Отмена настройки системного промпта"""
    user_id = ctx.author.id
    if user_id in users_setting_system:
        users_setting_system.remove(user_id)
        embed = discord.Embed(
            title="❌ Действие отменено",
            description="Настройка системного промпта отменена.",
            color=0xff0000
        )
        await ctx.send(embed=embed)
    else:
        await ctx.send("Нет активных действий для отмены.")

@bot.command(name='clear')
async def clear_command(ctx):
    """Команда очистки контекста"""
    user_id = ctx.author.id
    clear_user_context(user_id)
    embed = discord.Embed(
        title="🗑️ История очищена",
        description="История разговора успешно очищена!",
        color=0x00ff00
    )
    await ctx.send(embed=embed)

@bot.command(name='status')
async def status_command(ctx):
    """Проверка статуса подключения к LM Studio"""
    try:
        # Отправляем тестовый запрос
        test_messages = [{"role": "user", "content": "test"}]
        # Показываем, что бот "думает"
        async with ctx.typing():
            response = await lm_client.generate_response(test_messages)
        if "ошибка" not in response.lower() and "не удалось" not in response.lower():
            embed = discord.Embed(
                title="✅ Статус подключения",
                description="Подключение к LM Studio активно!",
                color=0x00ff00
            )
        else:
            embed = discord.Embed(
                title="❌ Статус подключения", 
                description="Проблемы с подключением к LM Studio",
                color=0xff0000
            )
    except Exception as e:
        logger.error(f"Ошибка проверки статуса: {e}")
        embed = discord.Embed(
            title="❌ Ошибка",
            description="Не удалось проверить статус подключения",
            color=0xff0000
        )
    await ctx.send(embed=embed)

@bot.command(name='info')
async def info_command(ctx):
    """Справка по использованию бота"""
    embed = discord.Embed(
        title="📖 Справка по боту",
        description="Этот бот использует локальную ИИ модель через LM Studio для генерации ответов.",
        color=0x9932cc
    )
    embed.add_field(
        name="🔧 Настройка LM Studio:",
        value=(
            "1. Запустите LM Studio\n"
            "2. Загрузите модель\n"
            "3. Запустите локальный сервер (обычно localhost:1234)"
        ),
        inline=False
    )
    embed.add_field(
        name="⚡ Команды:",
        value=(
            "`@ИмяБота m.start` - перезапуск\n"
            "`@ИмяБота m.clear` - очистить историю\n"
            "`@ИмяБота m.system` - настроить системный промпт\n"
            "`@ИмяБота m.show_system` - показать текущий промпт\n"
            "`@ИмяБота m.reset_system` - сбросить промпт\n"
            "`@ИмяБота m.status` - статус подключения\n"
            "`@ИмяБота m.cancel` - отменить действие"
        ),
        inline=False
    )
    embed.add_field(
        name="🎭 Системный промпт:",
        value=(
            "Определяет роль и поведение ИИ-ассистента:\n"
            "• Роль: учитель, программист, писатель\n"
            "• Стиль: формальный/неформальный\n"
            "• Специализация по темам"
        ),
        inline=False
    )
    await ctx.send(embed=embed)

@bot.event
async def on_message(message):
    """Обработка всех сообщений"""
    # Игнорируем сообщения от самого бота
    if message.author == bot.user:
        return

    # Проверяем, упомянут ли бот
    if bot.user.mentioned_in(message):
        # Проверяем, является ли сообщение командой (начинается с префикса команды, т.е. упоминания)
        ctx = await bot.get_context(message) # <-- Исправлено: добавлен await
        if ctx.valid: # Это команда
             await bot.process_commands(message)
             return

        user_id = message.author.id
        # Проверяем, настраивает ли пользователь системный промпт
        if user_id in users_setting_system:
            await handle_system_prompt_input(message)
            return

        # Обрабатываем обычное сообщение (с упоминанием) как запрос к ИИ
        await handle_ai_message(message)
    # Если бот не упомянут, просто игнорируем сообщение

async def handle_system_prompt_input(message):
    """Обработка ввода системного промпта"""
    user_id = message.author.id
    new_prompt = message.content.strip()
    # Убираем упоминание бота из начала сообщения, если оно там осталось
    if message.mentions and message.mentions[0] == bot.user:
         # Удаляем упоминание из начала строки
         new_prompt = re.sub(rf'<@!?{bot.user.id}>', '', new_prompt, count=1).strip()

    if len(new_prompt) < 10:
        embed = discord.Embed(
            title="⚠️ Слишком короткий промпт",
            description="Системный промпт слишком короткий. Пожалуйста, введите более детальное описание (минимум 10 символов).",
            color=0xffa500
        )
        await message.channel.send(embed=embed)
        return
    if len(new_prompt) > 1000:
        embed = discord.Embed(
            title="⚠️ Слишком длинный промпт",
            description="Системный промпт слишком длинный. Пожалуйста, сократите его до 1000 символов или меньше.",
            color=0xffa500
        )
        await message.channel.send(embed=embed)
        return

    # Устанавливаем новый системный промпт
    set_user_system_prompt(user_id, new_prompt)
    users_setting_system.remove(user_id)
    embed = discord.Embed(
        title="✅ Системный промпт обновлен",
        description=f"Новый системный промпт:\n```{new_prompt}```\nТеперь ИИ будет вести себя согласно новым инструкциям.",
        color=0x00ff00
    )
    await message.channel.send(embed=embed)

async def handle_ai_message(message):
    """Обработка сообщений для ИИ"""
    user_id = message.author.id
    # Получаем текст сообщения без упоминания бота
    user_message = message.content
    if message.mentions and message.mentions[0] == bot.user:
        # Удаляем упоминание из начала строки
        user_message = re.sub(rf'<@!?{bot.user.id}>', '', user_message, count=1).strip()

    if not user_message: # Если после удаления упоминания ничего не осталось
        await message.add_reaction("🤔") # Реагируем, если сообщение пустое
        return

    # Показываем, что бот "печатает"
    async with message.channel.typing():
        # Добавляем сообщение пользователя в контекст
        add_to_context(user_id, "user", user_message)
        # Получаем полный контекст для отправки в модель
        context = get_user_context(user_id)
        # Генерируем ответ
        ai_response = await lm_client.generate_response(context)
        # Добавляем ответ ИИ в контекст
        add_to_context(user_id, "assistant", ai_response)

    # Отправляем ответ пользователю
    try:
        # Если ответ слишком длинный для Discord (лимит 2000 символов)
        if len(ai_response) > 2000:
            # Разбиваем на части
            chunks = [ai_response[i:i+2000] for i in range(0, len(ai_response), 2000)]
            for chunk in chunks:
                await message.channel.send(chunk)
        else:
            await message.channel.send(ai_response)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")
        embed = discord.Embed(
            title="❌ Ошибка",
            description="Произошла ошибка при отправке ответа.",
            color=0xff0000
        )
        await message.channel.send(embed=embed)

async def main():
    """Основная функция запуска бота"""
    logger.info("Запуск Discord бота...")
    # Проверяем токен
    if BOT_TOKEN == "YOUR_DISCORD_BOT_TOKEN":
        logger.error("Необходимо указать BOT_TOKEN!")
        return
    try:
        await bot.start(BOT_TOKEN)
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
    finally:
        await bot.close()

if __name__ == "__main__":

    asyncio.run(main())
