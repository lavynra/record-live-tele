"""
================================================================
TIKTOK LIVE RECORDER -- Telegram Bot Edition
================================================================
Menjalankan tools record TikTok Live sebagai bot Telegram, agar
bisa dipakai dari HP/perangkat lain tanpa membuka terminal.

Alur pemakaian (dari sisi user Telegram):
  1. /start           -> salam + menu utama
  2. Record Live TikTok
     -> masukkan username TikTok (tanpa @)
     -> bot cek status live (LIVE / OFFLINE)
     -> pilih durasi rekam
     -> proses rekam berjalan, durasi ditampilkan realtime
     -> selesai -> otomatis diupload ke MediaFire -> link dikirim
  3. Lihat Hasil Record
     -> daftar username, tanggal, jam, dan link MediaFire

Hanya akun Telegram yang ID-nya terdaftar di allowed_users.txt
yang bisa memakai bot ini. Selain itu, bot akan meminta pengguna
menghubungi admin (@mr_quixter) untuk minta akses.

Menjalankan bot:
    python bot.py

Konfigurasi (file teks polos, taruh sejajar dengan bot.py):
    bot_token.txt        -> token bot dari @BotFather
    allowed_users.txt     -> daftar ID Telegram yang diizinkan
    cookie.txt            -> cookie TikTok (dipakai main.py)
    cookie_mediafire.txt  -> cookie MediaFire

Lihat README.md untuk panduan lengkap (termasuk Termux & Windows).
================================================================
"""

import asyncio
import contextlib
import os
import sys
import threading
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

import main as core
import mediafire_uploader as mf
import records_store as store

# ================================================================
# KONFIGURASI
# ================================================================

BOT_TOKEN_PATH = os.path.join(core.BASE_DIR, "bot_token.txt")
ALLOWED_USERS_PATH = os.path.join(core.BASE_DIR, "allowed_users.txt")
ADMIN_CONTACT = "@mr_quixter"

PROGRESS_INTERVAL_SECONDS = 4      # jeda antar update pesan realtime (rekam & upload)
HISTORY_PAGE_SIZE = 5

# State ConversationHandler
MAIN_MENU, ASK_USERNAME, CHOOSE_DURATION, ASK_CUSTOM_DURATION = range(4)

# Diisi saat startup oleh main_bot()
TIKTOK_SESSION = None
MEDIAFIRE_SESSION = None

# chat_id -> dict job yang sedang berjalan (lihat try_start_recording)
ACTIVE_JOBS = {}
_JOBS_GUARD = threading.Lock()


# ================================================================
# KONFIGURASI: TOKEN & WHITELIST
# ================================================================

def load_bot_token():
    """Baca token dari baris pertama yang bukan kosong/komentar (awalan '#').
    Sengaja toleran terhadap sisa baris komentar dari bot_token.txt.example
    yang tidak sempat dihapus, dan terhadap BOM UTF-8 dari editor Windows."""
    if not os.path.isfile(BOT_TOKEN_PATH):
        return None
    with open(BOT_TOKEN_PATH, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                return line
    return None


def load_allowed_users():
    """Dibaca ulang setiap kali dipanggil, agar admin bisa edit file tanpa restart bot."""
    if not os.path.isfile(ALLOWED_USERS_PATH):
        return set()
    ids = set()
    with open(ALLOWED_USERS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line.isdigit():
                ids.add(int(line))
    return ids


async def access_control_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler group=-1: jalan lebih dulu dari semua handler lain, memblokir user tak dikenal."""
    user = update.effective_user
    if user is None:
        return
    if user.id in load_allowed_users():
        return

    text = (
        "🚫 Kamu tidak memiliki akses ke bot ini.\n"
        f"Silakan hubungi admin Telegram {ADMIN_CONTACT} untuk meminta akses.\n\n"
        f"ID Telegram kamu: {user.id}"
    )
    if update.callback_query:
        with contextlib.suppress(Exception):
            await update.callback_query.answer()
        with contextlib.suppress(Exception):
            await update.callback_query.message.reply_text(text)
    elif update.message:
        with contextlib.suppress(Exception):
            await update.message.reply_text(text)
    raise ApplicationHandlerStop


# ================================================================
# KEYBOARD & TEKS BANTUAN
# ================================================================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Record Live TikTok", callback_data="menu:record")],
        [InlineKeyboardButton("📁 Lihat Hasil Record", callback_data="menu:history")],
    ])


def cancel_keyboard(callback_data="cancel"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data=callback_data)]])


def duration_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏺ Rekam Sampai Live Selesai", callback_data="dur:full")],
        [InlineKeyboardButton("⏱ Rekam Selama 30 Menit", callback_data="dur:30")],
        [InlineKeyboardButton("🕐 Rekam Durasi Custom (menit)", callback_data="dur:custom")],
        [InlineKeyboardButton("🔙 Kembali ke Menu Awal", callback_data="menu:home")],
    ])


def stop_keyboard(job_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Hentikan Rekam", callback_data=f"stop:{job_id}")]])


async def safe_edit_text(bot, chat_id, message_id, text, reply_markup=None):
    """Edit pesan tanpa melempar error (mis. jika isi persis sama / pesan sudah dihapus)."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            reply_markup=reply_markup, disable_web_page_preview=True,
        )
    except Exception:
        pass


# ================================================================
# MENU UTAMA
# ================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await update.message.reply_text(
        f"Halo, {user.full_name}! 👋\n\n"
        "Selamat datang di TikTok Live Recorder Bot.\n"
        "Silakan pilih menu di bawah ini:",
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU


async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "menu:record":
        context.user_data.pop("record_username", None)
        context.user_data.pop("record_stream_url", None)
        await query.edit_message_text(
            "Masukkan username TikTok yang ingin direkam (tanpa @):",
            reply_markup=cancel_keyboard(),
        )
        return ASK_USERNAME

    if query.data == "menu:history":
        await show_history(update, context, page=0)
        return MAIN_MENU

    # "menu:home" -> tampilkan ulang menu utama
    await query.edit_message_text("Silakan pilih menu:", reply_markup=main_menu_keyboard())
    return MAIN_MENU


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("record_username", None)
    context.user_data.pop("record_stream_url", None)
    await query.edit_message_text("Dibatalkan. Silakan pilih menu:", reply_markup=main_menu_keyboard())
    return MAIN_MENU


async def handle_cancel_to_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Dipakai saat Batal ditekan pada langkah durasi custom -> kembali ke pilihan durasi,
    bukan ke menu awal, supaya user tidak perlu mengulang cek username/live."""
    query = update.callback_query
    await query.answer()
    username = context.user_data.get("record_username")
    if not username:
        await query.edit_message_text("Sesi kedaluwarsa, silakan pilih menu:", reply_markup=main_menu_keyboard())
        return MAIN_MENU
    await query.edit_message_text(
        f"Dibatalkan. @{username} masih LIVE -- pilih durasi rekam:",
        reply_markup=duration_keyboard(),
    )
    return CHOOSE_DURATION


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("record_username", None)
    context.user_data.pop("record_stream_url", None)
    await update.message.reply_text("Dibatalkan. Silakan pilih menu:", reply_markup=main_menu_keyboard())
    return MAIN_MENU


# ================================================================
# LANGKAH: MASUKKAN USERNAME -> CEK STATUS LIVE
# ================================================================

async def handle_username_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    username = core.normalize_username(raw)

    if not username or not core.is_valid_username(username):
        await update.message.reply_text(
            "Format username tidak valid (huruf, angka, titik, underscore, tanpa @).\n"
            "Coba lagi atau tekan Batal.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_USERNAME

    checking_msg = await update.message.reply_text(f"⏳ Memeriksa status live untuk @{username}...")

    try:
        status = await asyncio.to_thread(core.check_live, TIKTOK_SESSION, username)
    except core.LiveCheckError as exc:
        await checking_msg.edit_text(
            f"❌ Gagal memeriksa @{username}: {exc}\n\nCoba username lain atau tekan Batal.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_USERNAME

    if not status.get("is_live"):
        await checking_msg.edit_text(
            f"👤 @{username}\n"
            f"⭕ Status: OFFLINE\n\n"
            f"Akun ini sedang tidak live. Masukkan username lain atau tekan Batal.",
            reply_markup=cancel_keyboard(),
        )
        return ASK_USERNAME

    context.user_data["record_username"] = username
    context.user_data["record_stream_url"] = status["stream_url"]

    viewers = status.get("viewers")
    viewers_display = f"{viewers:,}" if isinstance(viewers, int) else "-"

    await checking_msg.edit_text(
        f"👤 @{username} ({status.get('nickname', '-')})\n"
        f"🔴 Status: LIVE\n"
        f"📝 Judul: {status.get('title', '-')}\n"
        f"👁 Penonton: {viewers_display}\n\n"
        f"Pilih durasi rekam:",
        reply_markup=duration_keyboard(),
    )
    return CHOOSE_DURATION


# ================================================================
# LANGKAH: PILIH DURASI -> MULAI REKAM
# ================================================================

async def handle_duration_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "menu:home":
        context.user_data.pop("record_username", None)
        context.user_data.pop("record_stream_url", None)
        await query.edit_message_text("Silakan pilih menu:", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    if choice == "dur:custom":
        await query.edit_message_text(
            "Masukkan durasi rekam dalam menit (contoh: 45):",
            reply_markup=cancel_keyboard("cancel_to_duration"),
        )
        return ASK_CUSTOM_DURATION

    username = context.user_data.get("record_username")
    stream_url = context.user_data.get("record_stream_url")
    if not username or not stream_url:
        await query.edit_message_text("Sesi kedaluwarsa, silakan mulai lagi.", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    if choice == "dur:full":
        duration_minutes, mode_label = None, "Sampai live selesai"
    elif choice == "dur:30":
        duration_minutes, mode_label = 30, "30 menit"
    else:
        return CHOOSE_DURATION

    started = await try_start_recording(update, context, username, stream_url, duration_minutes, mode_label)
    if not started:
        return MAIN_MENU

    with contextlib.suppress(Exception):
        await query.edit_message_reply_markup(reply_markup=None)
    return ConversationHandler.END


async def handle_custom_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(",", ".")
    try:
        minutes = float(raw)
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Durasi tidak valid. Masukkan angka menit, contoh: 45.",
            reply_markup=cancel_keyboard("cancel_to_duration"),
        )
        return ASK_CUSTOM_DURATION

    username = context.user_data.get("record_username")
    stream_url = context.user_data.get("record_stream_url")
    if not username or not stream_url:
        await update.message.reply_text("Sesi kedaluwarsa, silakan pilih menu:", reply_markup=main_menu_keyboard())
        return MAIN_MENU

    started = await try_start_recording(
        update, context, username, stream_url, minutes, f"Custom ({minutes:g} menit)"
    )
    if not started:
        return MAIN_MENU
    return ConversationHandler.END


# ================================================================
# JOB REKAM: START / STOP / EKSEKUSI DI BACKGROUND
# ================================================================

def _try_register_job(chat_id):
    with _JOBS_GUARD:
        if chat_id in ACTIVE_JOBS:
            return False
        ACTIVE_JOBS[chat_id] = {}
        return True


def _unregister_job(chat_id):
    with _JOBS_GUARD:
        ACTIVE_JOBS.pop(chat_id, None)


async def try_start_recording(update, context, username, stream_url, duration_minutes, mode_label) -> bool:
    chat_id = update.effective_chat.id

    if not _try_register_job(chat_id):
        await update.effective_chat.send_message(
            "⚠️ Kamu masih punya proses rekam yang berjalan.\n"
            "Tunggu sampai selesai, atau tekan 🛑 Hentikan Rekam pada pesan rekam yang sedang berjalan.\n\n"
            "Silakan pilih menu:",
            reply_markup=main_menu_keyboard(),
        )
        return False

    job = ACTIVE_JOBS[chat_id]
    job.update({
        "job_id": f"{chat_id}:{int(time.time())}",
        "stop_event": threading.Event(),
        "ffmpeg_done": False,
        "start_time": time.time(),
        "username": username,
        "mode_label": mode_label,
    })

    requested_by = update.effective_user.full_name
    asyncio.create_task(
        run_recording_job(context.bot, chat_id, job, username, stream_url, duration_minutes, requested_by)
    )
    return True


async def handle_stop_recording(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    job_id = query.data.split(":", 1)[1] if ":" in query.data else ""
    job = ACTIVE_JOBS.get(chat_id)
    if not job or job.get("job_id") != job_id:
        await query.answer("Proses ini sudah tidak berjalan.", show_alert=True)
        return
    job["stop_event"].set()
    await query.answer("Menghentikan rekaman...")


def render_recording_text(username, mode_label, elapsed_seconds):
    elapsed_seconds = int(elapsed_seconds)
    return (
        f"🔴 Merekam @{username}\n"
        f"Mode: {mode_label}\n"
        f"⏱ {core.format_duration(elapsed_seconds)} ({elapsed_seconds} detik)\n\n"
        f"Tekan tombol di bawah untuk menghentikan lebih awal."
    )


async def run_recording_job(bot, chat_id, job, username, stream_url, duration_minutes, requested_by):
    stop_event = job["stop_event"]
    output_path = core.build_output_path(username)
    duration_seconds = int(duration_minutes * 60) if duration_minutes else None
    job["output_path"] = output_path

    result_holder = {}

    def _ffmpeg_worker():
        try:
            result_holder["returncode"] = core.record_stream(
                stream_url, output_path, duration_seconds, stop_event
            )
        except Exception as exc:  # dikirim balik ke event loop lewat result_holder
            result_holder["error"] = exc
        finally:
            stop_event.set()
            job["ffmpeg_done"] = True

    threading.Thread(target=_ffmpeg_worker, daemon=True).start()

    if duration_seconds is None:
        # Safety-net: pantau status live untuk auto-stop jika stream putus tanpa sinyal jelas.
        threading.Thread(
            target=core.monitor_live_status,
            args=(TIKTOK_SESSION, username, stop_event),
            daemon=True,
        ).start()

    msg = await bot.send_message(
        chat_id=chat_id,
        text=render_recording_text(username, job["mode_label"], 0),
        reply_markup=stop_keyboard(job["job_id"]),
    )
    job["message_id"] = msg.message_id

    while not job.get("ffmpeg_done"):
        await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
        elapsed = time.time() - job["start_time"]
        await safe_edit_text(
            bot, chat_id, msg.message_id,
            render_recording_text(username, job["mode_label"], elapsed),
            reply_markup=stop_keyboard(job["job_id"]),
        )

    elapsed = time.time() - job["start_time"]
    error = result_holder.get("error")
    exists = os.path.exists(output_path)
    size_mb = (os.path.getsize(output_path) / (1024 * 1024)) if exists else 0.0

    if error or not exists or size_mb <= 0:
        detail = f"\nDetail: {error}" if error else ""
        await safe_edit_text(
            bot, chat_id, msg.message_id,
            f"❌ Rekam @{username} gagal atau tidak ada data yang berhasil terekam.{detail}",
        )
        _unregister_job(chat_id)
        return

    await safe_edit_text(
        bot, chat_id, msg.message_id,
        f"✅ Rekam @{username} selesai ({core.format_duration(elapsed)}, {size_mb:.1f} MB).\n"
        f"☁️ Mengunggah ke MediaFire...",
    )

    # --- Upload ke MediaFire, dengan progres realtime berjalan paralel ---
    job["uploaded_bytes"] = 0
    job["upload_total"] = int(size_mb * 1024 * 1024) or 1
    upload_done = threading.Event()

    def _progress(uploaded, total):
        job["uploaded_bytes"] = uploaded
        job["upload_total"] = total or 1

    async def _upload_progress_loop():
        while not upload_done.is_set():
            await asyncio.sleep(PROGRESS_INTERVAL_SECONDS)
            uploaded = job.get("uploaded_bytes", 0)
            total = job.get("upload_total", 1)
            pct = min(100, int(uploaded / total * 100)) if total else 0
            await safe_edit_text(
                bot, chat_id, msg.message_id,
                f"☁️ Mengunggah @{username} ke MediaFire...\n"
                f"{pct}% ({uploaded / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB)",
            )

    progress_task = asyncio.create_task(_upload_progress_loop())
    upload_error = None
    link = None
    try:
        link = await asyncio.to_thread(
            mf.upload_file, MEDIAFIRE_SESSION, output_path, mf.DEFAULT_FOLDER_KEY, _progress
        )
    except mf.MediaFireError as exc:
        upload_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - jangan sampai job "menggantung" karena error tak terduga
        upload_error = f"Kesalahan tak terduga: {exc}"
    finally:
        upload_done.set()
        progress_task.cancel()
        with contextlib.suppress(Exception):
            await progress_task

    if upload_error:
        await safe_edit_text(
            bot, chat_id, msg.message_id,
            f"⚠️ Rekam @{username} selesai, tapi upload ke MediaFire gagal.\n"
            f"Error: {upload_error}\n"
            f"File tetap tersimpan di server: {output_path}",
        )
        _unregister_job(chat_id)
        return

    now = datetime.now(core.WIB)
    record = {
        "username": username,
        "date": now.strftime("%d-%m-%Y"),
        "time": now.strftime("%H:%M") + " WIB",
        "duration": core.format_duration(elapsed),
        "size_mb": round(size_mb, 2),
        "mediafire_link": link,
        "requested_by": requested_by,
    }
    store.add_record(record)

    await safe_edit_text(
        bot, chat_id, msg.message_id,
        f"✅ Rekam @{username} selesai!\n\n"
        f"⏱ Durasi: {core.format_duration(elapsed)}\n"
        f"💾 Ukuran: {size_mb:.1f} MB\n"
        f"🔗 Link: {link}",
    )
    _unregister_job(chat_id)


# ================================================================
# LIHAT HASIL RECORD
# ================================================================

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    records = store.list_records()
    total = len(records)
    start = page * HISTORY_PAGE_SIZE
    page_records = records[start:start + HISTORY_PAGE_SIZE]

    if not page_records:
        text = "📁 Belum ada hasil record yang tersimpan."
    else:
        lines = [f"📁 Hasil Record ({total} total):\n"]
        for r in page_records:
            lines.append(
                f"👤 @{r.get('username', '-')}\n"
                f"📅 {r.get('date', '-')}   🕐 {r.get('time', '-')}\n"
                f"🔗 {r.get('mediafire_link', '-')}\n"
            )
        text = "\n".join(lines)

    nav_row = []
    if start > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Sebelumnya", callback_data=f"hist:{page - 1}"))
    if start + HISTORY_PAGE_SIZE < total:
        nav_row.append(InlineKeyboardButton("➡️ Berikutnya", callback_data=f"hist:{page + 1}"))

    buttons = [nav_row] if nav_row else []
    buttons.append([InlineKeyboardButton("🔙 Menu Awal", callback_data="menu:home")])
    keyboard = InlineKeyboardMarkup(buttons)

    query = update.callback_query
    if query:
        await query.edit_message_text(text, reply_markup=keyboard, disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, reply_markup=keyboard, disable_web_page_preview=True)


async def handle_history_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":", 1)[1])
    await show_history(update, context, page=page)
    return MAIN_MENU


# ================================================================
# FALLBACK UNTUK PESAN DI LUAR CONVERSATION
# ================================================================

async def fallback_unhandled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "Ketik /start untuk membuka menu.", reply_markup=main_menu_keyboard()
        )


# ================================================================
# SETUP & STARTUP
# ================================================================

def build_application(token):
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start), CommandHandler("menu", cmd_start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(handle_main_menu, pattern="^menu:"),
                CallbackQueryHandler(handle_history_page, pattern="^hist:"),
            ],
            ASK_USERNAME: [
                CallbackQueryHandler(handle_cancel, pattern="^cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username_input),
            ],
            CHOOSE_DURATION: [
                CallbackQueryHandler(handle_duration_choice, pattern="^(dur:|menu:home)"),
            ],
            ASK_CUSTOM_DURATION: [
                CallbackQueryHandler(handle_cancel_to_duration, pattern="^cancel_to_duration$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_duration),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel), CommandHandler("start", cmd_start)],
        allow_reentry=True,
    )
    # Catatan: PTB akan menampilkan PTBUserWarning soal "per_message=False" karena
    # state di atas mencampur CallbackQueryHandler & MessageHandler dalam satu
    # ConversationHandler. Ini normal/aman untuk pola pemakaian pada bot ini.

    application = Application.builder().token(token).build()
    application.add_handler(TypeHandler(Update, access_control_middleware), group=-1)
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(handle_stop_recording, pattern="^stop:"))
    application.add_handler(MessageHandler(filters.ALL, fallback_unhandled), group=1)
    return application


def main_bot():
    global TIKTOK_SESSION, MEDIAFIRE_SESSION

    core.setup_console()
    print("================================================================")
    print(" TIKTOK LIVE RECORDER -- Telegram Bot Edition")
    print("================================================================")

    if core.find_ffmpeg() is None:
        core.print_ffmpeg_missing()
        sys.exit(1)

    try:
        tiktok_cookies = core.load_cookies(core.COOKIE_PATH)
    except core.CookieError as exc:
        print(f"[TikTok] {exc}")
        print("Pastikan cookie.txt tersedia di folder ini (lihat README.md).")
        sys.exit(1)
    TIKTOK_SESSION = core.build_session(tiktok_cookies)
    print(f"[TikTok] Cookie dimuat ({len(tiktok_cookies)} entri).")

    try:
        mf_cookies = mf.load_mediafire_cookies()
    except mf.MediaFireError as exc:
        print(f"[MediaFire] {exc}")
        print("Pastikan cookie_mediafire.txt tersedia di folder ini (lihat README.md).")
        sys.exit(1)
    MEDIAFIRE_SESSION = mf.build_mediafire_session(mf_cookies)
    print(f"[MediaFire] Cookie dimuat ({len(mf_cookies)} entri).")

    token = load_bot_token()
    if not token:
        print("[Bot] bot_token.txt belum diisi.")
        print("Buat bot lewat @BotFather di Telegram, lalu tempel tokennya ke bot_token.txt.")
        sys.exit(1)

    if not load_allowed_users():
        print("[Bot] PERINGATAN: allowed_users.txt kosong -- belum ada yang bisa memakai bot ini.")
        print("Tambahkan ID Telegram kamu sendiri ke allowed_users.txt sebelum lanjut.")

    os.makedirs(core.RECORD_DIR, exist_ok=True)

    application = build_application(token)
    print("[Bot] Berjalan... tekan Ctrl+C untuk berhenti.")
    print("[Bot] Catatan: menghentikan proses ini (Ctrl+C) akan memutus paksa rekaman")
    print("      yang sedang berjalan. Untuk menghentikan satu rekaman saja, gunakan")
    print("      tombol 'Hentikan Rekam' di chat Telegram.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main_bot()
    except KeyboardInterrupt:
        print()
        print("[Bot] Dihentikan oleh pengguna.")
        sys.exit(0)
