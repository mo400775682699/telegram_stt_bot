import os
import asyncio
import tempfile
import subprocess
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from faster_whisper import WhisperModel

# ========= إعدادات =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8136594767:AAHH0TfYcxNluthFn-VbqCjGGNLnYQjt0lQ")  # ضع التوكن هنا أو كمتغير بيئة
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")  # small / medium / large-v3
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")  # "cuda" لو عندك كرت شاشة مدعوم
LANGUAGE = os.getenv("LANGUAGE", "ar")  # "ar" أو اتركها فارغة None لاكتشاف تلقائي

# ====== تحميل الموديل مرّة واحدة ======
model = WhisperModel(
    WHISPER_MODEL,
    device=WHISPER_DEVICE,
    compute_type="int8" if WHISPER_DEVICE == "cpu" else "float16"
)

# ====== أدوات مساعدة ======
def run_ffmpeg_to_wav(src_path: str, dst_path: str, sample_rate: int = 16000):
    """
    يحوّل أي امتداد صوت/فيديو إلى WAV أحادي 16k باستخدام ffmpeg.
    """
    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-ac", "1",
        "-ar", str(sample_rate),
        "-vn",
        dst_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

async def download_telegram_file(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file):
    """
    ينزّل ملف تيليجرام إلى ملف مؤقت ويرجع المسار.
    """
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tmp:
        await tg_file.download_to_drive(tmp.name)
        return tmp.name

async def transcribe_file_with_progress(update: Update, wav_path: str):
    """
    يشغّل التفريغ ويرسل تقدّم العملية للمستخدم كنسبة مئوية.
    """
    progress_msg = await update.message.reply_text("جارٍ التفريغ… 0%")
    segments, info = model.transcribe(
        wav_path,
        language=None if not LANGUAGE else LANGUAGE,
        vad_filter=True
    )

    duration = getattr(info, "duration", None) or 0.0
    text_parts = []

    last_percent = -1
    last_edit_ts = 0.0
    MIN_EDIT_INTERVAL = 2.0
    STEP = 5

    async def maybe_edit(percent: int, tail_text: str = ""):
        nonlocal last_percent, last_edit_ts
        now = time.time()
        if (percent >= last_percent + STEP) and (now - last_edit_ts >= MIN_EDIT_INTERVAL):
            preview = (tail_text.strip()[:70] + "…") if len(tail_text.strip()) > 70 else tail_text.strip()
            await progress_msg.edit_text(f"جارٍ التفريغ… {percent}%\nآخر جملة: {preview}")
            last_percent = percent
            last_edit_ts = now

    for seg in segments:
        text_parts.append(seg.text.strip())
        if duration > 0:
            pct = int(min(99, (seg.end / duration) * 100))
        else:
            pct = min(99, len(text_parts) * 5)
        await maybe_edit(pct, seg.text)

    await progress_msg.edit_text("تم التفريغ 100% ✅ — أرسل النص الآن…")
    full_text = " ".join(t for t in text_parts if t)
    return full_text.strip()

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, friendly_name: str):
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # 1) تنزيل الملف
    tg_file = await context.bot.get_file(file_id)
    src_path = await download_telegram_file(update, context, tg_file)

    # 2) تحويل إلى WAV 16k mono
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp_wav:
        wav_path = tmp_wav.name

    try:
        run_ffmpeg_to_wav(src_path, wav_path)
    except Exception as e:
        await update.message.reply_text(f"تعذّر تحويل الملف ({friendly_name}). تأكد من تثبيت FFmpeg. تفاصيل: {e}")
        try:
            Path(src_path).unlink(missing_ok=True)
            Path(wav_path).unlink(missing_ok=True)
        except Exception:
            pass
        return

    # 3) تفريغ النص مع التقدم
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        text = await transcribe_file_with_progress(update, wav_path)
    except Exception as e:
        await update.message.reply_text(f"حدث خطأ أثناء التفريغ: {e}")
        text = ""

    # 4) تنظيف الملفات المؤقتة
    try:
        Path(src_path).unlink(missing_ok=True)
        Path(wav_path).unlink(missing_ok=True)
    except Exception:
        pass

    # 5) إرسال النتيجة
    if text:
        MAX_LEN = 4000
        if len(text) <= MAX_LEN:
            await update.message.reply_text(f"النص المستخرج ({friendly_name}):\n\n{text}")
        else:
            await update.message.reply_text(f"النص المستخرج طويل — سأرسله على أجزاء ({friendly_name}):")
            for i in range(0, len(text), MAX_LEN):
                await update.message.reply_text(text[i:i+MAX_LEN])
    else:
        await update.message.reply_text("ما قدرت أستخرج نص من الملف. جرّب تسجيل أوضح أو ملف بصيغة مختلفة.")

# ====== الهاندلرز ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أرسل لي ملاحظة صوتية، رسالة صوتية، أو ملف محاضرة (mp3/mp4/ogg/opus/wav)، وأنا أحوّله إلى نص.\n"
        "لغات مدعومة: العربية وغيرها.\n"
        "نصيحة: استخدم تسجيل واضح وقريب من الميكروفون."
    )

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice
    if not voice:
        return
    await handle_media(update, context, voice.file_id, "Voice Note")

async def audio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    audio = update.message.audio
    if not audio:
        return
    await handle_media(update, context, audio.file_id, f"Audio ({audio.file_name or 'audio'})")

async def video_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vn = update.message.video_note
    if not vn:
        return
    await handle_media(update, context, vn.file_id, "Video Note")

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return
    await handle_media(update, context, doc.file_id, f"Document ({doc.file_name or 'file'})")

def main():
    if TELEGRAM_BOT_TOKEN == "PUT-YOUR-TOKEN-HERE":
        raise RuntimeError("ضع توكن البوت في TELEGRAM_BOT_TOKEN أو كمتغير بيئة.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.AUDIO, audio_handler))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, video_note_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))

    print("Bot is running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()