import asyncio
import logging
from typing import Dict, List, Optional # Добавлено Optional
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import json
# --- Добавлено для Exa ---
from exa_py import Exa
# -----------------------

# Настройки
BOT_TOKEN = ""  # Замените на ваш токен бота
# --- Добавлено для Exa ---
EXA_API_KEY = "" # Замените на ваш API-ключ Exa
# -----------------------
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"  # URL LM Studio API
MODEL_NAME = "qwen/qwen3-4b"  # Имя модели в LM Studio

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Добавлено для Exa ---
exa_client: Optional[Exa] = None
# -----------------------

# Состояния для FSM
class ChatStates(StatesGroup):
    waiting_for_message = State()
    in_conversation = State()
    setting_system_prompt = State()
    # --- Добавлено для Exa ---
    waiting_for_search_query = State() # Состояние ожидания запроса для поиска
    # -----------------------

# Хранилище контекста разговоров для каждого пользователя
user_contexts: Dict[int, List[Dict[str, str]]] = {}

# Хранилище системных промптов для каждого пользователя
user_system_prompts: Dict[int, str] = {}

# Системный промпт по умолчанию
DEFAULT_SYSTEM_PROMPT = "Вы полезный ИИ-ассистент, который отвечает на вопросы пользователей дружелюбно и информативно."

class LMStudioClient:
    """Клиент для работы с LM Studio API"""
    def __init__(self, base_url: str, model_name: str):
        self.base_url = base_url
        self.model_name = model_name

    async def generate_response(self, messages: List[Dict[str, str]],
                              max_tokens: int = 1000,
                              temperature: float = 0.7) -> str:
        """Генерация ответа от локальной модели"""
        payload = {
            "model": self.model_name,
            "messages": messages,
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
                    return data["choices"][0]["message"]["content"]
        except aiohttp.ClientError as e:
            logger.error(f"Ошибка соединения с LM Studio: {e}")
            return "Не удалось подключиться к ИИ модели. Проверьте, что LM Studio запущен."
        except Exception as e:
            logger.error(f"Неожиданная ошибка: {e}")
            return "Произошла неожиданная ошибка."

# Инициализация бота и клиента
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
lm_client = LMStudioClient(LM_STUDIO_URL, MODEL_NAME)

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
    context = get_user_context(user_id)
    context.append({"role": role, "content": content})
    # Ограничиваем размер контекста
    # --- Исправлено: удаляем самое старое сообщение *после* системного промпта ---
    if len(context) > max_context_length:
         if len(context) > 1: # Убедимся, что есть что удалять после system
            context.pop(1) # Удаляем второй элемент (первый после system)
    # -----------------------

def clear_user_context(user_id: int):
    """Очистить контекст пользователя (кроме системного промпта)"""
    system_prompt = get_user_system_prompt(user_id)
    user_contexts[user_id] = [
        {"role": "system", "content": system_prompt}
    ]

def escape_markdown(text: str) -> str:
    """Экранирование специальных символов для Markdown"""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

# --- Добавлено для Exa ---
async def search_with_exa(query: str, num_results: int = 5) -> List[Dict]:
    """Выполняет поиск через Exa и возвращает список результатов."""
    if not exa_client:
        logger.error("Exa client не инициализирован.")
        return []
    try:
        # Используем run_in_executor для асинхронного вызова синхронного метода Exa
        loop = asyncio.get_event_loop()
        search_response = await loop.run_in_executor(None, exa_client.search, query, {"num_results": num_results})
        results = []
        for result in search_response.results:
            results.append({
                "title": result.title,
                "url": result.url,
                "text": result.text # Или result.highlights, если доступно
            })
        return results
    except Exception as e:
        logger.error(f"Ошибка при поиске через Exa: {e}")
        return []

@dp.message(Command("search"))
async def search_command(message: types.Message, state: FSMContext):
    """Команда начала поиска через Exa"""
    prompt_text = (
        "🔍 Поиск в интернете через Exa\n"
        "Введите поисковый запрос или отправьте /cancel для отмены:"
    )
    await message.answer(prompt_text)
    await state.set_state(ChatStates.waiting_for_search_query)

@dp.message(F.text, ChatStates.waiting_for_search_query)
async def handle_search_query(message: types.Message, state: FSMContext):
    """Обработка ввода поискового запроса"""
    query = message.text.strip()
    if not query:
        await message.answer("Пожалуйста, введите поисковый запрос или /cancel для отмены.")
        return

    await message.answer(f"Ищу информацию по запросу: `{escape_markdown(query)}`...", parse_mode="MarkdownV2")
    # Отправляем индикатор набора текста
    await bot.send_chat_action(message.chat.id, "typing")

    try:
        results = await search_with_exa(query)
        if not results:
             await message.answer("По вашему запросу ничего не найдено или произошла ошибка.")
             await state.set_state(ChatStates.in_conversation) # Возвращаемся в основное состояние
             return

        response_text = f"🔍 Результаты поиска для: *{escape_markdown(query)}*\n\n"
        for i, result in enumerate(results[:5]): # Показываем до 5 результатов
            title = escape_markdown(result.get('title', 'Без названия')[:200]) # Ограничиваем длину и экранируем
            url = escape_markdown(result.get('url', 'URL не найден')) # Экранируем URL
            snippet = escape_markdown(result.get('text', 'Нет описания')[:400] + "...") # Ограничиваем длину и экранируем

            response_text += f"{i+1}\\. [{title}]({url})\n{snippet}\n\n"

        # Telegram может иметь ограничения на длину сообщения, разбиваем при необходимости
        if len(response_text) > 4096:
             chunks = [response_text[i:i+4096] for i in range(0, len(response_text), 4096)]
             for chunk in chunks:
                 await message.answer(chunk, parse_mode="MarkdownV2")
        else:
             await message.answer(response_text, parse_mode="MarkdownV2")

    except Exception as e:
        logger.error(f"Ошибка в команде search: {e}")
        await message.answer("Произошла ошибка при выполнении поиска.")

    await state.set_state(ChatStates.in_conversation) # Возвращаемся в основное состояние

# -----------------------

@dp.message(Command("start"))
async def start_command(message: types.Message, state: FSMContext):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    clear_user_context(user_id)
    welcome_text = (
        "🤖 Привет! Я бот с интеграцией локальной ИИ модели через LM Studio.\n"
        "Доступные команды:\n"
        "• /start - начать заново\n"
        "• /clear - очистить историю разговора\n"
        "• /system - настроить системный промпт\n"
        "• /show_system - показать текущий системный промпт\n"
        "• /reset_system - сбросить системный промпт к умолчанию\n"
        "• /search - поискать в интернете через Exa\n" # Добавлено
        "• /status - проверить статус подключения к модели\n"
        "• /help - показать справку\n"
        "Просто напишите мне сообщение, и я отвечу с помощью локальной ИИ модели!"
    )
    await message.answer(welcome_text)
    await state.set_state(ChatStates.in_conversation)

@dp.message(Command("system"))
async def system_prompt_command(message: types.Message, state: FSMContext):
    """Команда настройки системного промпта"""
    prompt_text = (
        "🔧 Настройка системного промпта\n"
        "Системный промпт определяет поведение и роль ИИ-ассистента.\n"
        "Примеры системных промптов:\n"
        "• \"Вы опытный программист, который помогает с кодом\"\n"
        "• \"Вы креативный писатель, создающий интересные истории\"\n"
        "• \"Вы строгий учитель, который объясняет сложные темы простыми словами\"\n"
        "Введите новый системный промпт или отправьте /cancel для отмены:"
    )
    await message.answer(prompt_text)
    await state.set_state(ChatStates.setting_system_prompt)

@dp.message(Command("show_system"))
async def show_system_prompt_command(message: types.Message):
    """Показать текущий системный промпт"""
    user_id = message.from_user.id
    current_prompt = get_user_system_prompt(user_id)
    # Экранируем специальные символы для Markdown
    escaped_prompt = escape_markdown(current_prompt)
    response_text = (
        "📋 Текущий системный промпт:\n"
        f"`{escaped_prompt}`\n"
        "Используйте /system для изменения или /reset\\_system для сброса к умолчанию\\."
    )
    await message.answer(response_text, parse_mode="MarkdownV2")

@dp.message(Command("reset_system"))
async def reset_system_prompt_command(message: types.Message):
    """Сбросить системный промпт к умолчанию"""
    user_id = message.from_user.id
    set_user_system_prompt(user_id, DEFAULT_SYSTEM_PROMPT)
    escaped_prompt = escape_markdown(DEFAULT_SYSTEM_PROMPT)
    response_text = (
        "🔄 Системный промпт сброшен к умолчанию\\!\n"
        f"Новый промпт: `{escaped_prompt}`"
    )
    await message.answer(response_text, parse_mode="MarkdownV2")

@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    """Отмена текущего действия"""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Нет активных действий для отмены.")
        return
    await state.clear()
    await state.set_state(ChatStates.in_conversation)
    await message.answer("❌ Действие отменено.")

@dp.message(F.text, ChatStates.setting_system_prompt)
async def handle_system_prompt_input(message: types.Message, state: FSMContext):
    """Обработка ввода системного промпта"""
    user_id = message.from_user.id
    new_prompt = message.text.strip()
    if len(new_prompt) < 10:
        await message.answer(
            "⚠️ Системный промпт слишком короткий. "
            "Пожалуйста, введите более детальное описание (минимум 10 символов)."
        )
        return
    if len(new_prompt) > 1000:
        await message.answer(
            "⚠️ Системный промпт слишком длинный. "
            "Пожалуйста, сократите его до 1000 символов или меньше."
        )
        return
    # Устанавливаем новый системный промпт
    set_user_system_prompt(user_id, new_prompt)
    escaped_prompt = escape_markdown(new_prompt)
    response_text = (
        "✅ Системный промпт успешно обновлен\\!\n"
        f"Новый промпт:\n`{escaped_prompt}`\n"
        "Теперь ИИ будет вести себя согласно новым инструкциям\\."
    )
    await message.answer(response_text, parse_mode="MarkdownV2")
    await state.set_state(ChatStates.in_conversation)

@dp.message(Command("clear"))
async def clear_command(message: types.Message):
    """Обработчик команды очистки контекста"""
    user_id = message.from_user.id
    clear_user_context(user_id)
    await message.answer("🗑️ История разговора очищена!")

@dp.message(Command("status"))
async def status_command(message: types.Message):
    """Проверка статуса подключения к LM Studio"""
    try:
        # Отправляем тестовый запрос
        test_messages = [{"role": "user", "content": "test"}]
        # Отправляем индикатор набора текста
        await bot.send_chat_action(message.chat.id, "typing")
        response = await lm_client.generate_response(test_messages)
        if "ошибка" not in response.lower() and "не удалось" not in response.lower():
            await message.answer("✅ Подключение к LM Studio активно!")
        else:
            await message.answer("❌ Проблемы с подключением к LM Studio")
    except Exception as e:
        logger.error(f"Ошибка проверки статуса: {e}")
        await message.answer("❌ Не удалось проверить статус подключения")

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Справка по использованию бота"""
    help_text = (
        "📖 Справка по боту:\n"
        "Этот бот использует локальную ИИ модель через LM Studio для генерации ответов.\n"
        "🔧 Настройка:\n"
        "1. Запустите LM Studio\n"
        "2. Загрузите модель\n"
        "3. Запустите локальный сервер (обычно localhost:1234)\n"
        "⚡ Команды:\n"
        "/start - перезапуск\n"
        "/clear - очистить историю\n"
        "/system - настроить системный промпт\n"
        "/show_system - показать текущий промпт\n"
        "/reset_system - сбросить промпт к умолчанию\n"
        "/search - поискать в интернете через Exa\n" # Добавлено
        "/status - статус подключения\n"
        "/help - эта справка\n"
        "/cancel - отменить текущее действие\n"
        "🎭 Системный промпт:\n"
        "Определяет роль и поведение ИИ-ассистента. Например:\n"
        "• Учитель, программист, писатель\n"
        "• Стиль общения (формальный/неформальный)\n"
        "• Специализация по темам"
    )
    await message.answer(help_text)

@dp.message(F.text, ChatStates.in_conversation)
async def handle_message(message: types.Message):
    """Обработка обычных сообщений"""
    user_id = message.from_user.id
    user_message = message.text
    # Отправляем индикатор набора текста
    await bot.send_chat_action(message.chat.id, "typing")
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
        await message.answer(ai_response)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")
        await message.answer("Извините, произошла ошибка при отправке ответа.")

@dp.message()
async def handle_other_messages(message: types.Message, state: FSMContext):
    """Обработка других типов сообщений"""
    # Проверим текущее состояние, чтобы корректно обработать сообщения вне ожидаемых состояний
    current_state = await state.get_state()
    if current_state == ChatStates.waiting_for_search_query:
         # Если пользователь отправил текст, когда мы ждали запрос для поиска
         await handle_search_query(message, state)
    elif current_state == ChatStates.setting_system_prompt:
         # Если пользователь отправил текст, когда мы ждали системный промпт
         await handle_system_prompt_input(message, state)
    elif current_state == ChatStates.in_conversation:
        await message.answer("Я работаю только с текстовыми сообщениями. Напишите /start для начала.")
    else:
        # Если состояние не определено или не обрабатывается
        await message.answer("Я работаю только с текстовыми сообщениями. Напишите /start для начала.")


async def main():
    """Основная функция запуска бота"""
    global exa_client # --- Добавлено для инициализации Exa ---
    logger.info("Запуск бота...")

    # --- Добавлено для инициализации Exa ---
    global EXA_API_KEY
    if not EXA_API_KEY or EXA_API_KEY == "YOUR_EXA_API_KEY":
        logger.error("Необходимо указать EXA_API_KEY!")
        return
    try:
        exa_client = Exa(EXA_API_KEY)
        logger.info("Exa клиент инициализирован.")
    except Exception as e:
        logger.error(f"Ошибка инициализации Exa клиента: {e}")
        return
    # -----------------------

    # Проверяем токен
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("Необходимо указать BOT_TOKEN!")
        return
    try:
        # Удаляем веб-хуки и запускаем polling
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
    finally:
        await bot.session.close()

if __name__ == "__main__":

    asyncio.run(main())
