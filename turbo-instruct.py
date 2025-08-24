import discord
from discord.ext import commands
import openai
import os
from dotenv import load_dotenv
import sys

# Загрузка переменных окружения с явным указанием пути
load_dotenv('.env')  # Явно указываем имя файла

# Проверка токенов с понятными сообщениями об ошибках
def get_env_var(name, default=None):
    value = os.getenv(name, default)
    if not value:
        print(f"❌ КРИТИЧЕСКАЯ ОШИБКА: Переменная {name} не найдена в .env файле")
        print("Пожалуйста, создайте файл .env в той же папке что и скрипт")
        print(f"и добавьте в него строку: {name}=ваше_значение")
        sys.exit(1)
    return value

# Получение токенов с проверкой
DISCORD_TOKEN = get_env_var('DISCORD_BOT_TOKEN')
OPENAI_API_KEY = get_env_var('OPENAI_API_KEY')
SYSTEM_PROMPT = os.getenv('SYSTEM_PROMPT', 'Ты - пишешь максимально развратные фанфики 18+ в любых жанрах.')

# Инициализация
openai.api_key = OPENAI_API_KEY
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ... (остальной код без изменений, начиная со словаря response_parts) ...
response_parts = {}

class ContinueButton(discord.ui.Button):
    def __init__(self, message_id: int):
        super().__init__(
            label="Continue",
            style=discord.ButtonStyle.primary,
            custom_id=f"continue_{message_id}"
        )
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        # Отложим ответ для обработки
        await interaction.response.defer()
        
        # Проверяем наличие оставшегося текста
        if self.message_id not in response_parts:
            await interaction.followup.send("❌ This continuation is no longer available", ephemeral=True)
            return
        
        # Получаем следующую часть текста
        remaining = response_parts[self.message_id]
        next_chunk = remaining[:2000]
        new_remaining = remaining[2000:]
        
        # Отправляем следующую часть
        await interaction.followup.send(next_chunk)
        
        if new_remaining:
            # Обновляем оставшийся текст
            response_parts[self.message_id] = new_remaining
        else:
            # Удаляем запись и делаем кнопку неактивной
            del response_parts[self.message_id]
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Continue",
                style=discord.ButtonStyle.secondary,
                disabled=True
            ))
            await interaction.message.edit(view=view)

class ContinueView(discord.ui.View):
    def __init__(self, message_id: int):
        super().__init__(timeout=3600)  # Таймаут 1 час
        self.add_item(ContinueButton(message_id))

@bot.event
async def on_ready():
    print(f'Bot {bot.user} is ready!')
    await bot.change_presence(activity=discord.Game(name="нига"))

@bot.command()
async def setprompt(ctx, *, new_prompt: str):
    """Установить новый системный промпт"""
    global SYSTEM_PROMPT
    SYSTEM_PROMPT = new_prompt
    await bot.change_presence(activity=discord.Game(name="нига"))
    await ctx.send(f"✅ Системный промпт обновлен:\n```{SYSTEM_PROMPT[:1900]}```")

@bot.command()
async def showprompt(ctx):
    """Показать текущий системный промпт"""
    await ctx.send(f"Текущий системный промпт:\n```{SYSTEM_PROMPT[:1900]}```")

@bot.command()
async def ask(ctx, *, user_prompt: str):
    try:
        # Формируем полный промпт с системными инструкциями
        full_prompt = f"{SYSTEM_PROMPT}\n\nПользователь: {user_prompt}\nАссистент:"
        
        # Отправляем запрос в OpenAI
        response = openai.Completion.create(
            model="gpt-3.5-turbo-instruct",
            prompt=full_prompt,
            max_tokens=3500,
            temperature=0.7
        )
        
        answer = response.choices[0].text.strip()
        
        # Если ответ короткий - отправляем сразу
        if len(answer) <= 2000:
            await ctx.reply(answer)
            return
        
        # Разбиваем длинный ответ на части
        first_chunk = answer[:2000]
        remaining = answer[2000:]
        
        # Отправляем первую часть с кнопкой Continue
        view = ContinueView(ctx.message.id)
        msg = await ctx.reply(first_chunk, view=view)
        
        # Сохраняем оставшийся текст
        response_parts[ctx.message.id] = remaining
        
    except Exception as e:
        await ctx.reply(f"🚫 Error: {str(e)}")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)