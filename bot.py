import logging
import os
import re
import asyncio
import json
import subprocess
import tempfile
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

# ══════════════════════════════════════════
# НАСТРОЙКИ — заполни свои ключи
# ══════════════════════════════════════════
TELEGRAM_TOKEN = "8670605699:AAGTOjwqEUTCnKQoYepCSiruKjfu2SXdkrA"       # от @BotFather
GEMINI_API_KEY = "AIzaSyBvoswe93AJYGNoh29WOQYLUVZSO3J7ZO8"       # от aistudio.google.com

# ══════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ
# ══════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ══════════════════════════════════════════
# ПРОМПТЫ ДЛЯ АГЕНТА
# ══════════════════════════════════════════

SYSTEM_PROMPT = """
Ты — профессиональный контент-агент для видеографа в нише "ИИ + видеопроизводство".
Язык: только русский. Тон: живой, конкретный, без воды.
"""

PROMPTS = {
    "analyze": """
Проанализируй этот контент из Instagram по структуре:

1. ХУК (0-10 сек)
   — Что зацепило внимание
   — Какой триггер использован (страх/любопытство/провокация/обещание)

2. СТРУКТУРА
   — Как построен контент по блокам

3. ВОВЛЕЧЕНИЕ
   — Что заставляет досматривать
   — Какие эмоции включает

4. CTA
   — Как и куда ведут аудиторию

5. ПОЧЕМУ ЗАШЛО
   — Гипотеза в 2-3 предложениях

6. ЧТО БЕРУ СЕБЕ
   — Конкретные паттерны для адаптации (не копируем, берём принципы)

Контент для анализа:
""",

    "hooks": """
Напиши 7 вариантов хуков для темы. По одному в каждом стиле:

1. ПРОВОКАЦИЯ — задевает убеждения
2. ОБЕЩАНИЕ — конкретный результат за время
3. ЛИЧНАЯ ИСТОРИЯ — начало с личного момента
4. ВОПРОС-ТРИГГЕР — вопрос который больно игнорировать
5. ШОК-ФАКТ — цифра или факт который удивляет
6. ПРОТИВ ТЕЧЕНИЯ — говоришь то чего не ожидают
7. СТРАХ — что теряет человек если не посмотрит

Формат: [Стиль]: "текст хука" → Почему работает: ...

В конце укажи какой хук самый сильный для этой темы и почему.

Тема:
""",

    "reels": """
Напиши сценарий для Instagram Reels / YouTube Shorts.

Структура 30 сек:
— 0-3 сек: хук (текст на экране + что говоришь)
— 3-20 сек: суть (1 мысль максимально плотно)
— 20-30 сек: вывод + CTA

Формат каждого блока:
ВИЗУАЛ: [что на экране]
ТЕКСТ НА ЭКРАНЕ: [оверлей]
ГОЛОС: [дословно что говоришь]
МОНТАЖ: [пометка для монтажёра]

Тема:
""",

    "youtube": """
Напиши полный сценарий YouTube-видео (7-10 минут).

Структура:
[ХУК — 0:00-0:30] — дословно что говоришь
[ОБЕЩАНИЕ — 0:30-1:00] — что зритель получит
[ИСТОРИЯ — 1:00-2:30] — личный опыт, цифры
[ОСНОВНОЙ КОНТЕНТ — 2:30-...] — блоки с таймкодами
[ПЕРЕЛОМНЫЙ МОМЕНТ] — главный инсайт
[CTA — последние 30-60 сек] — живым языком

Стиль: от первого лица, короткие предложения.
[пауза] — для пауз, [улыбка] — для эмоций.

Тема:
""",

    "texts": """
Напиши все тексты для публикации:

1. ЗАГОЛОВОК YOUTUBE (3 варианта, до 60 символов)

2. YouTube SEO-ОПИСАНИЕ
   — Первые 2 строки с ключевым словом
   — Основной текст 300-400 символов с таймкодами
   — 15-20 хэштегов

3. INSTAGRAM CAPTION
   — Хук-строка (первые 125 символов)
   — Основной текст 150-200 символов
   — Вопрос для комментариев
   — 8-10 хэштегов

4. TELEGRAM АНОНС (300-400 символов + ссылка-заглушка)

Тема:
""",

    "telegram": """
Напиши все форматы постов для Telegram:

1. АНОНС YOUTUBE-ВИДЕО (300-400 символов)
2. АНОНС REELS (150-200 символов)
3. САМОСТОЯТЕЛЬНЫЙ ПОСТ-ИНСАЙТ (500-800 символов)
4. ТЕКСТ ДЛЯ КРУЖКА — дословно что сказать голосом (15-30 сек)

Стиль: живой, как пишет человек. Эмодзи: 1-2 максимум.

Тема:
""",

    "plan": """
Составь контент-план на неделю (7 дней).

Для каждого дня:
День X — [ТЕМА]
Формат: YouTube Long / Reels / Telegram пост
Платформы: ...
Хук (одна строка): ...
Почему зайдёт: ...

Ниша: видеограф который осваивает ИИ-инструменты для создания контента.
Чередуй типы: обучение / личная история / провокация / сравнение / список / за кулисами.

Период или пожелания:
"""
}

# ══════════════════════════════════════════
# СКАЧИВАНИЕ КОНТЕНТА ИЗ INSTAGRAM
# ══════════════════════════════════════════

async def download_instagram(url: str) -> dict:
    """Скачивает субтитры и описание из Instagram через yt-dlp"""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [
                "yt-dlp",
                "--write-auto-subs",
                "--sub-lang", "ru,en",
                "--skip-download",
                "--write-description",
                "--no-playlist",
                "-o", f"{tmpdir}/video",
                url
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            content = {}

            # Читаем субтитры
            for ext in [".ru.vtt", ".en.vtt", ".ru.srt", ".en.srt"]:
                sub_path = f"{tmpdir}/video{ext}"
                if os.path.exists(sub_path):
                    with open(sub_path, "r", encoding="utf-8") as f:
                        raw = f.read()
                        # Чистим VTT/SRT теги
                        clean = re.sub(r"<[^>]+>", "", raw)
                        clean = re.sub(r"\d+:\d+:\d+[\.,]\d+ --> \d+:\d+:\d+[\.,]\d+", "", clean)
                        clean = re.sub(r"^\d+$", "", clean, flags=re.MULTILINE)
                        clean = re.sub(r"WEBVTT.*?\n", "", clean)
                        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
                        content["subtitles"] = clean[:3000]
                    break

            # Читаем описание
            desc_path = f"{tmpdir}/video.description"
            if os.path.exists(desc_path):
                with open(desc_path, "r", encoding="utf-8") as f:
                    content["description"] = f.read()[:1000]

            return content

    except subprocess.TimeoutExpired:
        return {"error": "Таймаут при скачивании. Попробуй ещё раз."}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════
# ЗАПРОС К GEMINI
# ══════════════════════════════════════════

async def ask_gemini(prompt: str) -> str:
    try:
        full_prompt = SYSTEM_PROMPT + "\n\n" + prompt
        response = await asyncio.to_thread(model.generate_content, full_prompt)
        return response.text
    except Exception as e:
        return f"Ошибка Gemini: {str(e)}"


# ══════════════════════════════════════════
# ОБРАБОТЧИКИ TELEGRAM
# ══════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """👋 Привет! Я твой контент-агент.

📌 КОМАНДЫ:

🔗 Просто кинь ссылку Instagram → анализ контента

/hooks [тема] → 7 вариантов хуков
/reels [тема] → сценарий для Reels/Shorts
/youtube [тема] → полный сценарий YouTube
/texts [тема] → все описания и тексты
/telegram [тема] → посты для Telegram
/plan → контент-план на неделю

📝 Примеры:
/hooks как я использую ИИ при съёмке
/reels топ-3 ИИ инструмента для видеографа
/plan"""
    await update.message.reply_text(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Если это ссылка Instagram
    if "instagram.com" in text or "instagr.am" in text:
        await update.message.reply_text("🔍 Скачиваю контент из Instagram...")

        content = await download_instagram(text)

        if "error" in content:
            await update.message.reply_text(
                f"❌ Не удалось скачать: {content['error']}\n\n"
                "💡 Попробуй скопировать текст из поста и отправить его с командой:\n"
                "/analyze [вставь текст здесь]"
            )
            return

        if not content:
            await update.message.reply_text(
                "❌ Не нашёл субтитры в этом видео.\n\n"
                "💡 Скопируй текст поста вручную и отправь:\n"
                "/analyze [текст поста]"
            )
            return

        # Собираем контент для анализа
        combined = ""
        if "description" in content:
            combined += f"ОПИСАНИЕ:\n{content['description']}\n\n"
        if "subtitles" in content:
            combined += f"СУБТИТРЫ:\n{content['subtitles']}"

        await update.message.reply_text("🧠 Анализирую...")
        result = await ask_gemini(PROMPTS["analyze"] + combined)
        await update.message.reply_text(result[:4000])

    else:
        await update.message.reply_text(
            "Не понимаю 🤔\n\n"
            "Кинь ссылку Instagram или используй команды.\n"
            "Напиши /start чтобы увидеть все команды."
        )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    content = " ".join(context.args)
    if not content:
        await update.message.reply_text("Напиши текст для анализа:\n/analyze [текст контента]")
        return
    await update.message.reply_text("🧠 Анализирую...")
    result = await ask_gemini(PROMPTS["analyze"] + content)
    await update.message.reply_text(result[:4000])


async def cmd_hooks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/hooks как я использую ИИ при съёмке")
        return
    await update.message.reply_text("✍️ Пишу хуки...")
    result = await ask_gemini(PROMPTS["hooks"] + topic)
    await update.message.reply_text(result[:4000])


async def cmd_reels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/reels топ-3 ИИ инструмента для видеографа")
        return
    await update.message.reply_text("🎬 Пишу сценарий Reels...")
    result = await ask_gemini(PROMPTS["reels"] + topic)
    await update.message.reply_text(result[:4000])


async def cmd_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/youtube как ИИ изменил мою работу видеографа")
        return
    await update.message.reply_text("🎥 Пишу сценарий YouTube...")
    result = await ask_gemini(PROMPTS["youtube"] + topic)
    # Разбиваем на части если длинный
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i:i+4000])


async def cmd_texts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/texts топ-3 ИИ инструмента для видеографа")
        return
    await update.message.reply_text("📝 Пишу все тексты...")
    result = await ask_gemini(PROMPTS["texts"] + topic)
    await update.message.reply_text(result[:4000])


async def cmd_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/telegram как я использую ИИ при съёмке")
        return
    await update.message.reply_text("✈️ Пишу посты для Telegram...")
    result = await ask_gemini(PROMPTS["telegram"] + topic)
    await update.message.reply_text(result[:4000])


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    extra = " ".join(context.args) if context.args else "без пожеланий"
    await update.message.reply_text("📅 Составляю контент-план...")
    result = await ask_gemini(PROMPTS["plan"] + extra)
    await update.message.reply_text(result[:4000])


# ══════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("hooks", cmd_hooks))
    app.add_handler(CommandHandler("reels", cmd_reels))
    app.add_handler(CommandHandler("youtube", cmd_youtube))
    app.add_handler(CommandHandler("texts", cmd_texts))
    app.add_handler(CommandHandler("telegram", cmd_telegram))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
