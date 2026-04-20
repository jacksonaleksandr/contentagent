import logging
import os
import re
import asyncio
import subprocess
import tempfile
import json
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
import anthropic
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# ══════════════════════════════════════════
# НАСТРОЙКИ
# ══════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDS_JSON", "")

# ══════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ
# ══════════════════════════════════════════
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

claude = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# Состояния диалога для добавления референса
(
    REF_VIEWS, REF_LIKES, REF_COMMENTS,
    REF_SAVES, REF_AGE, REF_WHY, REF_CONFIRM
) = range(7)

# Временное хранилище данных пользователя
user_data_store = {}

# ══════════════════════════════════════════
# GOOGLE SHEETS
# ══════════════════════════════════════════

def get_sheet():
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"
            ]
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEETS_ID)

        # Создаём лист если нет
        try:
            worksheet = sh.worksheet("Референсы")
        except:
            worksheet = sh.add_worksheet(title="Референсы", rows=1000, cols=20)
            worksheet.append_row([
                "Дата", "Ссылка", "Просмотры", "Лайки", "Комментарии",
                "Сохранения", "ER%", "Возраст", "Почему понравилось",
                "Тема", "Тип хука", "Структура", "Длина", "Подача",
                "Почему залетело (Claude)", "Паттерны", "Адаптация", "Рейтинг"
            ])
        return worksheet
    except Exception as e:
        logger.error(f"Google Sheets error: {e}")
        return None


def save_to_sheets(data: dict) -> bool:
    try:
        ws = get_sheet()
        if not ws:
            return False

        # Считаем ER
        try:
            views = int(str(data.get("views", "0")).replace("М", "000000")
                       .replace("К", "000").replace("м", "000000").replace("к", "000"))
            likes = int(str(data.get("likes", "0")).replace("М", "000000")
                       .replace("К", "000").replace("м", "000000").replace("к", "000"))
            comments = int(str(data.get("comments", "0")).replace("М", "000000")
                          .replace("К", "000").replace("м", "000000").replace("к", "000"))
            er = round((likes + comments) / views * 100, 2) if views > 0 else 0
        except:
            er = 0

        row = [
            datetime.now().strftime("%d.%m.%Y"),
            data.get("url", ""),
            data.get("views", ""),
            data.get("likes", ""),
            data.get("comments", ""),
            data.get("saves", "не указано"),
            f"{er}%",
            data.get("age", ""),
            data.get("why", ""),
            data.get("analysis_topic", ""),
            data.get("analysis_hook_type", ""),
            data.get("analysis_structure", ""),
            data.get("analysis_length", ""),
            data.get("analysis_delivery", ""),
            data.get("analysis_why_viral", ""),
            data.get("analysis_patterns", ""),
            data.get("analysis_adaptation", ""),
            data.get("rating", ""),
        ]
        ws.append_row(row)
        return True
    except Exception as e:
        logger.error(f"Save error: {e}")
        return False


def get_all_refs() -> str:
    try:
        ws = get_sheet()
        if not ws:
            return ""
        records = ws.get_all_records()
        if not records:
            return "База референсов пока пуста."

        text = f"БАЗА РЕФЕРЕНСОВ ({len(records)} видео):\n\n"
        for i, r in enumerate(records, 1):
            text += f"{i}. {r.get('Тема', '—')} | "
            text += f"👁{r.get('Просмотры', '?')} "
            text += f"❤️{r.get('Лайки', '?')} "
            text += f"ER:{r.get('ER%', '?')} | "
            text += f"Хук: {r.get('Тип хука', '?')} | "
            text += f"{r.get('Почему залетело (Claude)', '')[:80]}...\n"
        return text
    except Exception as e:
        return f"Ошибка чтения базы: {e}"


def export_for_claude() -> str:
    try:
        ws = get_sheet()
        if not ws:
            return ""
        records = ws.get_all_records()
        if not records:
            return "База пуста."

        text = "=== БАЗА РЕФЕРЕНСОВ ДЛЯ CLAUDE ===\n\n"
        for i, r in enumerate(records, 1):
            text += f"--- РЕФЕРЕНС #{i} ---\n"
            for key, val in r.items():
                if val:
                    text += f"{key}: {val}\n"
            text += "\n"
        return text
    except Exception as e:
        return f"Ошибка: {e}"

# ══════════════════════════════════════════
# СКАЧИВАНИЕ И ТРАНСКРИПЦИЯ
# ══════════════════════════════════════════

async def download_and_transcribe(url: str) -> dict:
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Скачиваем субтитры и описание
            cmd = [
                "yt-dlp",
                "--write-auto-subs",
                "--sub-lang", "ru,en",
                "--skip-download",
                "--write-description",
                "--no-playlist",
                "--write-info-json",
                "-o", f"{tmpdir}/video",
                url
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            content = {"url": url}

            # Субтитры
            for ext in [".ru.vtt", ".en.vtt", ".ru.srt", ".en.srt"]:
                sub_path = f"{tmpdir}/video{ext}"
                if os.path.exists(sub_path):
                    with open(sub_path, "r", encoding="utf-8") as f:
                        raw = f.read()
                        clean = re.sub(r"<[^>]+>", "", raw)
                        clean = re.sub(r"\d+:\d+:\d+[\.,]\d+ --> \d+:\d+:\d+[\.,]\d+", "", clean)
                        clean = re.sub(r"^\d+$", "", clean, flags=re.MULTILINE)
                        clean = re.sub(r"WEBVTT.*?\n", "", clean)
                        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
                        content["subtitles"] = clean[:3000]
                        content["lang"] = "ru" if ".ru." in ext else "en"
                    break

            # Описание
            desc_path = f"{tmpdir}/video.description"
            if os.path.exists(desc_path):
                with open(desc_path, "r", encoding="utf-8") as f:
                    content["description"] = f.read()[:1000]

            # Метаданные (длина видео)
            info_path = f"{tmpdir}/video.info.json"
            if os.path.exists(info_path):
                with open(info_path, "r", encoding="utf-8") as f:
                    info = json.load(f)
                    duration = info.get("duration", 0)
                    content["duration"] = f"{duration} сек" if duration else "неизвестно"

            return content

    except subprocess.TimeoutExpired:
        return {"url": url, "error": "Таймаут"}
    except Exception as e:
        return {"url": url, "error": str(e)}

# ══════════════════════════════════════════
# CLAUDE API
# ══════════════════════════════════════════

SYSTEM = """Ты — профессиональный контент-агент для видеографа в нише "ИИ + видеопроизводство".
Язык: только русский. Тон: живой, конкретный, без воды."""

async def ask_claude(prompt: str) -> str:
    try:
        response = await asyncio.to_thread(
            claude.messages.create,
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        return f"Ошибка Claude: {str(e)}"


async def analyze_video(content: dict, metrics: dict) -> dict:
    """Детальный анализ видео через Claude"""
    text_content = ""
    if content.get("description"):
        text_content += f"ОПИСАНИЕ:\n{content['description']}\n\n"
    if content.get("subtitles"):
        text_content += f"ТРАНСКРИПЦИЯ:\n{content['subtitles']}\n\n"

    metrics_text = f"""
МЕТРИКИ:
Просмотры: {metrics.get('views', '?')}
Лайки: {metrics.get('likes', '?')}
Комментарии: {metrics.get('comments', '?')}
Сохранения: {metrics.get('saves', 'не указано')}
Возраст видео: {metrics.get('age', '?')}
Длина: {content.get('duration', '?')}
Почему понравилось автору: {metrics.get('why', '?')}
"""

    prompt = f"""Проведи детальный анализ этого видео из Instagram.

{metrics_text}

КОНТЕНТ ВИДЕО:
{text_content if text_content else "Текст недоступен — анализируй по метрикам"}

Ответь СТРОГО в формате JSON (без markdown, только чистый JSON):
{{
  "topic": "тема видео в 5-7 словах",
  "hook_type": "тип хука (провокация/обещание/вопрос/факт/история/против течения)",
  "structure": "тип структуры (обучение/история/список/сравнение/за кулисами)",
  "length_assessment": "оценка длины (слишком короткое/оптимально/длинновато)",
  "delivery": "подача (говорит в камеру/закадровый/текст на экране/комбо)",
  "why_viral": "почему залетело — 2-3 предложения",
  "patterns": "ключевые паттерны — через запятую",
  "adaptation": "как адаптировать под нишу видеограф+ИИ — 1-2 предложения",
  "rating": "оценка от 1 до 5 на основе метрик и качества"
}}"""

    result = await ask_claude(prompt)

    try:
        clean = result.strip()
        if "```" in clean:
            clean = re.sub(r"```json|```", "", clean).strip()
        return json.loads(clean)
    except:
        return {
            "topic": "не удалось определить",
            "hook_type": "—", "structure": "—",
            "length_assessment": "—", "delivery": "—",
            "why_viral": result[:200],
            "patterns": "—", "adaptation": "—", "rating": "3"
        }

# ══════════════════════════════════════════
# ПРОМПТЫ ДЛЯ ГЕНЕРАЦИИ
# ══════════════════════════════════════════

async def get_refs_context() -> str:
    refs = get_all_refs()
    if not refs or "пуста" in refs:
        return ""
    return f"\n\nУЧИТЫВАЙ МОЮ БАЗУ РЕФЕРЕНСОВ при генерации:\n{refs}\n\n"


async def generate_hooks(topic: str) -> str:
    refs = await get_refs_context()
    prompt = f"""{refs}Напиши 7 вариантов хуков для темы. По одному в каждом стиле:

1. ПРОВОКАЦИЯ — задевает убеждения
2. ОБЕЩАНИЕ — конкретный результат за время
3. ЛИЧНАЯ ИСТОРИЯ — начало с личного момента
4. ВОПРОС-ТРИГГЕР — вопрос который больно игнорировать
5. ШОК-ФАКТ — цифра или факт который удивляет
6. ПРОТИВ ТЕЧЕНИЯ — говоришь то чего не ожидают
7. СТРАХ — что теряет человек если не посмотрит

Формат: [Стиль]: "текст хука" → Почему работает: ...

В конце укажи какой хук самый сильный и почему.

Тема: {topic}"""
    return await ask_claude(prompt)


async def generate_reels(topic: str) -> str:
    refs = await get_refs_context()
    prompt = f"""{refs}Напиши сценарий для Instagram Reels (30 сек).

— 0-3 сек: хук (текст на экране + что говоришь)
— 3-20 сек: суть (1 мысль максимально плотно)
— 20-30 сек: вывод + CTA

Формат:
ВИЗУАЛ: [что на экране]
ТЕКСТ НА ЭКРАНЕ: [оверлей]
ГОЛОС: [дословно]
МОНТАЖ: [пометка]

Тема: {topic}"""
    return await ask_claude(prompt)


async def generate_youtube(topic: str) -> str:
    refs = await get_refs_context()
    prompt = f"""{refs}Напиши полный сценарий YouTube-видео (7-10 минут).

[ХУК — 0:00-0:30]
[ОБЕЩАНИЕ — 0:30-1:00]
[ИСТОРИЯ — 1:00-2:30]
[ОСНОВНОЙ КОНТЕНТ — 2:30-...] с таймкодами
[ПЕРЕЛОМНЫЙ МОМЕНТ]
[CTA — последние 30-60 сек]

От первого лица, короткие предложения. [пауза] и [улыбка] для эмоций.

Тема: {topic}"""
    return await ask_claude(prompt)


async def generate_texts(topic: str) -> str:
    refs = await get_refs_context()
    prompt = f"""{refs}Напиши все тексты для публикации:

1. ЗАГОЛОВОК YOUTUBE (3 варианта, до 60 символов)
2. YouTube SEO-ОПИСАНИЕ (первые 2 строки + основной текст + 15 хэштегов)
3. INSTAGRAM CAPTION (хук + текст + вопрос + 8 хэштегов)
4. TELEGRAM АНОНС (300-400 символов)

Тема: {topic}"""
    return await ask_claude(prompt)


async def generate_telegram(topic: str) -> str:
    refs = await get_refs_context()
    prompt = f"""{refs}Напиши все форматы для Telegram:

1. АНОНС YOUTUBE (300-400 символов)
2. АНОНС REELS (150-200 символов)
3. ПОСТ-ИНСАЙТ (500-800 символов)
4. ТЕКСТ ДЛЯ КРУЖКА (15-30 сек, дословно)

Стиль: живой. Эмодзи: 1-2 максимум.
Тема: {topic}"""
    return await ask_claude(prompt)


async def generate_plan(extra: str = "") -> str:
    refs = await get_refs_context()
    prompt = f"""{refs}Составь контент-план на неделю (7 дней).

День X — [ТЕМА]
Формат: YouTube / Reels / Telegram
Хук (одна строка): ...
Почему зайдёт: ...

Ниша: видеограф + ИИ. Чередуй: обучение/история/провокация/сравнение/список.
{extra}"""
    return await ask_claude(prompt)


async def analyze_patterns() -> str:
    refs = export_for_claude()
    if "пуста" in refs or not refs:
        return "База референсов пуста. Добавь хотя бы 5-10 видео командой /ref"

    prompt = f"""Проанализируй мою базу референсов и выдай инсайты:

{refs}

1. ТОП-3 ТЕМЫ которые залетают лучше всего
2. КАКИЕ ХУКИ работают лучше (по типам)
3. ОПТИМАЛЬНАЯ ДЛИНА видео в моей нише
4. ОБЩИЕ ПАТТЕРНЫ топовых видео (ER выше среднего)
5. ЧТО ИЗБЕГАТЬ — что не работает
6. РЕКОМЕНДАЦИИ для следующих 3 видео

Будь конкретным, опирайся на цифры из базы."""
    return await ask_claude(prompt)

# ══════════════════════════════════════════
# CONVERSATION HANDLER — добавление референса
# ══════════════════════════════════════════

async def ref_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else ""

    if not url or "instagram.com" not in url:
        await update.message.reply_text(
            "Отправь ссылку так:\n/ref https://www.instagram.com/reel/..."
        )
        return ConversationHandler.END

    user_id = update.effective_user.id
    user_data_store[user_id] = {"url": url}

    await update.message.reply_text("🔍 Скачиваю видео...")

    content = await download_and_transcribe(url)
    user_data_store[user_id]["content"] = content

    if content.get("subtitles"):
        await update.message.reply_text(
            f"✅ Транскрипция готова ({content.get('duration', '?')})\n\n"
            f"Первые слова: «{content['subtitles'][:150]}...»\n\n"
            "👁 Сколько просмотров? (например: 3.2М или 850К)"
        )
    else:
        await update.message.reply_text(
            f"⚠️ Субтитры не найдены, но продолжим по метрикам.\n\n"
            "👁 Сколько просмотров? (например: 3.2М или 850К)"
        )

    return REF_VIEWS


async def ref_views(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data_store[user_id]["views"] = update.message.text
    await update.message.reply_text("❤️ Лайки?")
    return REF_LIKES


async def ref_likes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data_store[user_id]["likes"] = update.message.text
    await update.message.reply_text("💬 Комментарии?")
    return REF_COMMENTS


async def ref_comments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data_store[user_id]["comments"] = update.message.text
    await update.message.reply_text(
        "🔁 Сохранения/репосты?\n(напиши число или «не знаю»)"
    )
    return REF_SAVES


async def ref_saves(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data_store[user_id]["saves"] = update.message.text

    keyboard = [["Сегодня", "На этой неделе"],
                ["Месяц назад", "Больше месяца"]]
    await update.message.reply_text(
        "📅 Когда вышло видео?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return REF_AGE


async def ref_age(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data_store[user_id]["age"] = update.message.text

    keyboard = [
        ["1 — Крутой хук", "2 — Тема залетела"],
        ["3 — Монтаж/подача", "4 — Сценарий/текст"],
        ["5 — Интуиция", "6 — Всё вместе"]
    ]
    await update.message.reply_text(
        "🎯 Почему понравилось? Выбери или напиши своё:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return REF_WHY


async def ref_why(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data_store[user_id]["why"] = update.message.text

    await update.message.reply_text(
        "🧠 Анализирую видео через Claude...",
        reply_markup=ReplyKeyboardRemove()
    )

    data = user_data_store[user_id]
    analysis = await analyze_video(data.get("content", {}), data)

    # Сохраняем анализ
    data["analysis_topic"] = analysis.get("topic", "")
    data["analysis_hook_type"] = analysis.get("hook_type", "")
    data["analysis_structure"] = analysis.get("structure", "")
    data["analysis_length"] = analysis.get("length_assessment", "")
    data["analysis_delivery"] = analysis.get("delivery", "")
    data["analysis_why_viral"] = analysis.get("why_viral", "")
    data["analysis_patterns"] = analysis.get("patterns", "")
    data["analysis_adaptation"] = analysis.get("adaptation", "")
    data["rating"] = analysis.get("rating", "3")

    summary = f"""📊 АНАЛИЗ ГОТОВ

🎯 Тема: {analysis.get('topic', '—')}
🪝 Хук: {analysis.get('hook_type', '—')}
📐 Структура: {analysis.get('structure', '—')}
🎬 Подача: {analysis.get('delivery', '—')}
⭐ Рейтинг: {analysis.get('rating', '—')}/5

💡 Почему залетело:
{analysis.get('why_viral', '—')}

🔑 Паттерны: {analysis.get('patterns', '—')}

✏️ Как адаптировать:
{analysis.get('adaptation', '—')}

Сохранить в базу референсов?"""

    keyboard = [["✅ Да, сохранить", "❌ Отмена"]]
    await update.message.reply_text(
        summary,
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return REF_CONFIRM


async def ref_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if "да" in update.message.text.lower() or "✅" in update.message.text:
        data = user_data_store.get(user_id, {})
        success = await asyncio.to_thread(save_to_sheets, data)

        if success:
            await update.message.reply_text(
                "✅ Сохранено в базу референсов!\n\n"
                "Используй /patterns чтобы увидеть паттерны всей базы.",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await update.message.reply_text(
                "❌ Ошибка сохранения. Проверь настройки Google Sheets.",
                reply_markup=ReplyKeyboardRemove()
            )
    else:
        await update.message.reply_text(
            "Отменено.",
            reply_markup=ReplyKeyboardRemove()
        )

    user_data_store.pop(user_id, None)
    return ConversationHandler.END


async def ref_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data_store.pop(user_id, None)
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ══════════════════════════════════════════
# ОСТАЛЬНЫЕ КОМАНДЫ
# ══════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """👋 Привет! Я твой контент-агент на Claude.

━━━━━━━━━━━━━━━━━━
📥 РЕФЕРЕНСЫ
━━━━━━━━━━━━━━━━━━
/ref [ссылка] — добавить видео в базу
/patterns — анализ паттернов всей базы
/export — выгрузить базу для Claude.ai

━━━━━━━━━━━━━━━━━━
✍️ ГЕНЕРАЦИЯ
━━━━━━━━━━━━━━━━━━
/hooks [тема] — 7 вариантов хуков
/reels [тема] — сценарий Reels
/youtube [тема] — сценарий YouTube
/texts [тема] — описания и хэштеги
/telegram [тема] — посты Telegram
/plan — контент-план на неделю

━━━━━━━━━━━━━━━━━━
📝 Пример:
/ref https://instagram.com/reel/...
/hooks как ИИ меняет работу видеографа"""
    await update.message.reply_text(text)


async def cmd_patterns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Анализирую базу референсов...")
    result = await analyze_patterns()
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i:i+4000])


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Готовлю файл для Claude.ai...")
    content = await asyncio.to_thread(export_for_claude)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        tmp_path = f.name

    with open(tmp_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename="referensy_dlya_claude.txt",
            caption="📎 Загрузи этот файл в Project Instructions в Claude.ai"
        )
    os.unlink(tmp_path)


async def cmd_hooks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/hooks [тема]")
        return
    await update.message.reply_text("✍️ Пишу хуки с учётом твоих референсов...")
    result = await generate_hooks(topic)
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i:i+4000])


async def cmd_reels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/reels [тема]")
        return
    await update.message.reply_text("🎬 Пишу сценарий Reels...")
    result = await generate_reels(topic)
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i:i+4000])


async def cmd_youtube(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/youtube [тема]")
        return
    await update.message.reply_text("🎥 Пишу сценарий YouTube...")
    result = await generate_youtube(topic)
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i:i+4000])


async def cmd_texts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/texts [тема]")
        return
    await update.message.reply_text("📝 Пишу все тексты...")
    result = await generate_texts(topic)
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i:i+4000])


async def cmd_telegram_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args)
    if not topic:
        await update.message.reply_text("Укажи тему:\n/telegram [тема]")
        return
    await update.message.reply_text("✈️ Пишу посты для Telegram...")
    result = await generate_telegram(topic)
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i:i+4000])


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    extra = " ".join(context.args) if context.args else ""
    await update.message.reply_text("📅 Составляю контент-план с учётом референсов...")
    result = await generate_plan(extra)
    for i in range(0, len(result), 4000):
        await update.message.reply_text(result[i:i+4000])

# ══════════════════════════════════════════
# ЗАПУСК
# ══════════════════════════════════════════

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # ConversationHandler для добавления референса
    ref_handler = ConversationHandler(
        entry_points=[CommandHandler("ref", ref_start)],
        states={
            REF_VIEWS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_views)],
            REF_LIKES: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_likes)],
            REF_COMMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_comments)],
            REF_SAVES: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_saves)],
            REF_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_age)],
            REF_WHY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_why)],
            REF_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_confirm)],
        },
        fallbacks=[CommandHandler("cancel", ref_cancel)],
    )

    app.add_handler(ref_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("patterns", cmd_patterns))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("hooks", cmd_hooks))
    app.add_handler(CommandHandler("reels", cmd_reels))
    app.add_handler(CommandHandler("youtube", cmd_youtube))
    app.add_handler(CommandHandler("texts", cmd_texts))
    app.add_handler(CommandHandler("telegram", cmd_telegram_posts))
    app.add_handler(CommandHandler("plan", cmd_plan))

    print("🤖 Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
