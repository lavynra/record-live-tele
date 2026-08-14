"""
================================================================
TIKTOK LIVE RECORDER -- Console Edition
================================================================
Tools CLI untuk merekam siaran TikTok LIVE langsung dari sumber
stream-nya (bukan screen-recording). Karena rekaman diambil dari
feed video mentah, hasil akhirnya berupa video + audio murni --
tanpa overlay komentar, gift, atau elemen antarmuka lainnya.

Autentikasi memakai cookie browser (cookie.txt) agar tools dapat
mengakses TikTok selayaknya sesi yang sudah login.

Kebutuhan sistem : Python 3.8+, FFmpeg (harus tersedia di PATH)
Dependensi pip   : requests

Catatan:
Tools ini memanfaatkan endpoint internal TikTok yang tidak
didokumentasikan secara resmi. Endpoint tersebut sewaktu-waktu
bisa berubah mengikuti kebijakan TikTok, sehingga pembaruan kode
mungkin diperlukan di kemudian hari. Gunakan tools ini secara
bertanggung jawab dan hormati hak cipta para kreator.
================================================================
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    print("Modul 'requests' belum terpasang.")
    print("Jalankan terlebih dahulu : pip install requests")
    sys.exit(1)

# ================================================================
# KONFIGURASI GLOBAL
# ================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORD_DIR = os.path.join(BASE_DIR, "record")
COOKIE_PATH = os.path.join(BASE_DIR, "cookie.txt")

WIB = timezone(timedelta(hours=7))
WIDTH = 68

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

ROOM_CHECK_URL = "https://www.tiktok.com/api-live/user/room/"
ROOM_INFO_URL = "https://webcast.tiktok.com/webcast/room/info/"
QUALITY_PRIORITY = ["FULL_HD1", "HD1", "SD1", "SD2", "origin"]

USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,24}$")
ANSI_RE = re.compile(r"\033\[[0-9;]*m")

QUOTES = [
    "Momen terbaik sering datang tanpa aba-aba. Selalu siap merekamnya.",
    "Konten hebat bukan soal keberuntungan, tapi soal siapa yang lebih dulu menekan rekam.",
    "Setiap detik live yang tersimpan adalah bahan baku untuk cerita esok hari.",
    "Yang lain menonton, kamu mengarsipkan. Di situ letak bedanya.",
    "Layar akan gelap, tapi rekaman ini akan tetap menyala.",
    "Alat secanggih apa pun cuma alat. Kamu yang menentukan jadi apa hasilnya.",
    "Selesai bukan berarti berhenti. Ini cuma jeda sebelum proyek berikutnya.",
    "Sampai jumpa di sesi rekam berikutnya. Dunia konten tidak pernah tidur.",
]

# ================================================================
# KONSOL & TAMPILAN (CLI STYLING)
# ================================================================

def setup_console():
    """Menyiapkan console Windows agar mendukung UTF-8 dan ANSI."""
    if os.name == "nt":
        os.system("chcp 65001 >nul")
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


class C:
    """Kode warna ANSI. Otomatis nonaktif jika output bukan terminal."""
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    WHITE = "\033[97m"

    @staticmethod
    def wrap(text, code):
        if not sys.stdout.isatty():
            return text
        return f"{code}{text}{C.RESET}"


def visible_len(text):
    return len(ANSI_RE.sub("", text))


def center(text, width=WIDTH):
    return text.center(width)


def print_banner():
    top = "╔" + "═" * (WIDTH - 2) + "╗"
    bottom = "╚" + "═" * (WIDTH - 2) + "╝"
    blank = "║" + " " * (WIDTH - 2) + "║"
    print()
    print(C.wrap(top, C.CYAN))
    print(C.wrap(blank, C.CYAN))
    title = center("T I K T O K   L I V E   R E C O R D E R", WIDTH - 2)
    print(C.wrap("║", C.CYAN) + C.wrap(title, C.BOLD + C.WHITE) + C.wrap("║", C.CYAN))
    subtitle = center("Console Edition", WIDTH - 2)
    print(C.wrap("║", C.CYAN) + C.wrap(subtitle, C.DIM) + C.wrap("║", C.CYAN))
    print(C.wrap(blank, C.CYAN))
    print(C.wrap(bottom, C.CYAN))
    print()


def print_section(title):
    label = f" {title} "
    pad_total = max(WIDTH - len(label), 0)
    left = pad_total // 2
    right = pad_total - left
    print()
    print(C.wrap("─" * left + label + "─" * right, C.CYAN))


def print_box(rows):
    inner_width = WIDTH - 2
    label_width = 14
    print(C.wrap("┌" + "─" * inner_width + "┐", C.DIM))
    for label, value in rows:
        value = str(value)
        prefix = f" {label:<{label_width}}: "
        value_width = max(inner_width - len(prefix), 8)
        if visible_len(value) <= value_width:
            segments = [value]
        else:
            segments = textwrap.wrap(
                value, value_width, break_long_words=True, break_on_hyphens=False
            ) or [""]
        for i, seg in enumerate(segments):
            text = (prefix + seg) if i == 0 else (" " * len(prefix) + seg)
            pad = max(inner_width - visible_len(text), 0)
            print(C.wrap("│", C.DIM) + text + " " * pad + C.wrap("│", C.DIM))
    print(C.wrap("└" + "─" * inner_width + "┘", C.DIM))


def status_tag(is_live):
    if is_live:
        return C.wrap("● LIVE", C.GREEN + C.BOLD)
    return C.wrap("○ OFFLINE", C.RED)


def print_error(message):
    print(C.wrap(f" ✗ {message}", C.RED))


def print_success(message):
    print(C.wrap(f" ✓ {message}", C.GREEN))


def print_warning(message):
    print(C.wrap(f" ⚠ {message}", C.YELLOW))


def print_info(message):
    print(f" › {message}")


class Spinner:
    """Spinner sederhana berbasis unicode braille untuk proses tunggu."""
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, message):
        self.message = message
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop.set()
        if self._thread:
            self._thread.join()
        clear_len = len(self.message) + 6
        sys.stdout.write("\r" + " " * clear_len + "\r")
        sys.stdout.flush()
        return False

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r {frame} {self.message}")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1

# ================================================================
# COOKIE LOADER
# ================================================================

class CookieError(Exception):
    pass


def _parse_json_cookies(content):
    data = json.loads(content)
    cookies = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item.get("name") and "value" in item:
                cookies[item["name"]] = item["value"]
    elif isinstance(data, dict):
        cookies = {str(k): str(v) for k, v in data.items()}
    return cookies


def _parse_netscape_cookies(content):
    cookies = {}
    for raw_line in content.splitlines():
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue
        parts = entry.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5], parts[6]
        if name:
            cookies[name] = value
    return cookies


def _parse_header_cookies(content):
    cookies = {}
    single_line = " ".join(content.splitlines())
    for part in single_line.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            name, value = name.strip(), value.strip()
            if name:
                cookies[name] = value
    return cookies


def load_cookies(path):
    if not os.path.isfile(path):
        raise CookieError(f"File cookie tidak ditemukan di: {path}")
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        content = f.read().strip()
    if not content:
        raise CookieError("File cookie.txt kosong.")
    cookies = {}
    stripped = content.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            cookies = _parse_json_cookies(content)
        except (json.JSONDecodeError, ValueError):
            cookies = {}
    if not cookies and "\t" in content:
        cookies = _parse_netscape_cookies(content)
    if not cookies:
        cookies = _parse_header_cookies(content)
    if not cookies:
        raise CookieError(
            "Format cookie.txt tidak dikenali atau tidak berisi cookie yang valid."
        )
    return cookies


def build_session(cookies):
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://www.tiktok.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    })
    session.cookies.update(cookies)
    return session

# ================================================================
# USERNAME
# ================================================================

def normalize_username(raw):
    raw = raw.strip()
    raw = re.sub(r"^https?://(www\.)?tiktok\.com/", "", raw, flags=re.IGNORECASE)
    raw = raw.split("/")[0]
    raw = raw.lstrip("@")
    return raw.strip()


def is_valid_username(username):
    return bool(USERNAME_RE.match(username))

# ================================================================
# PENGECEKAN STATUS LIVE
# ================================================================

class LiveCheckError(Exception):
    pass


def resolve_room(session, username):
    params = {"aid": "1988", "sourceType": "54", "uniqueId": username}
    headers = {"Referer": f"https://www.tiktok.com/@{username}/live"}
    try:
        resp = session.get(ROOM_CHECK_URL, params=params, headers=headers, timeout=12)
    except requests.RequestException as exc:
        raise LiveCheckError(f"Gagal terhubung ke TikTok: {exc}") from exc
    if resp.status_code != 200:
        raise LiveCheckError(f"TikTok merespons dengan status HTTP {resp.status_code}.")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise LiveCheckError(
            "Respons TikTok tidak dapat dibaca. Cookie mungkin sudah kedaluwarsa."
        ) from exc
    data = payload.get("data") or {}
    user_data = data.get("user") or {}
    room_id = (
        data.get("roomId")
        or data.get("room_id")
        or user_data.get("roomId")
        or user_data.get("room_id")
    )
    if not room_id and os.environ.get("DEBUG_LIVE") == "1":
        print()
        print_warning("DEBUG resolve_room: roomId tidak ditemukan di respons.")
        print(f"  HTTP status    : {resp.status_code}")
        print(f"  Key di payload : {list(payload.keys())}")
        print(f"  Key di data    : {list(data.keys())}")
        dump_path = os.path.join(BASE_DIR, "debug_payload.json")
        try:
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"  Payload lengkap disimpan ke: {dump_path}")
        except Exception as exc:
            print(f"  Gagal menyimpan payload lengkap: {exc}")
    return room_id, data


def fetch_room_info(session, room_id):
    params = {"aid": "1988", "room_id": room_id}
    try:
        resp = session.get(ROOM_INFO_URL, params=params, timeout=12)
    except requests.RequestException as exc:
        raise LiveCheckError(f"Gagal mengambil detail room: {exc}") from exc
    if resp.status_code != 200:
        raise LiveCheckError(
            f"TikTok merespons dengan status HTTP {resp.status_code} saat mengambil detail room."
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise LiveCheckError("Detail room tidak dapat dibaca.") from exc
    return payload.get("data") or {}


def pick_stream_url(info):
    stream_obj = info.get("stream_url") or info.get("streamUrl") or {}
    flv_map = stream_obj.get("flv_pull_url") or stream_obj.get("flvPullUrl")
    if isinstance(flv_map, dict) and flv_map:
        for quality in QUALITY_PRIORITY:
            if quality in flv_map:
                return flv_map[quality]
        return next(iter(flv_map.values()))
    if isinstance(flv_map, str) and flv_map:
        return flv_map
    hls_url = stream_obj.get("hls_pull_url") or stream_obj.get("hlsPullUrl")
    if hls_url:
        return hls_url
    rtmp_map = stream_obj.get("rtmp_pull_url") or stream_obj.get("rtmpPullUrl")
    if isinstance(rtmp_map, dict) and rtmp_map:
        return next(iter(rtmp_map.values()))
    if isinstance(rtmp_map, str) and rtmp_map:
        return rtmp_map
    return None


# Urutan prioritas kualitas dari liveRoom.hevcStreamData / streamData.
# "_60" = varian 60fps eksplisit, diprioritaskan dulu.
LIVEROOM_QUALITY_PRIORITY = ["uhd_60", "hd_60", "origin", "hd", "sd", "ld"]


def pick_stream_url_from_liveroom(live_room):
    """Coba ambil stream URL langsung dari liveRoom (bisa berisi varian 60fps)."""
    if not isinstance(live_room, dict):
        return None, None
    for block_key in ("hevcStreamData", "streamData"):
        block = live_room.get(block_key) or {}
        pull = block.get("pull_data") or block.get("pullData") or {}
        raw = pull.get("stream_data") or pull.get("streamData")
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        data_map = parsed.get("data") or {}
        for quality in LIVEROOM_QUALITY_PRIORITY:
            entry = data_map.get(quality)
            if isinstance(entry, dict):
                main = entry.get("main") or {}
                url = main.get("flv") or main.get("hls")
                if url:
                    return url, quality
    return None, None


def check_live(session, username):
    room_id, room_data = resolve_room(session, username)
    if not room_id:
        return {"found": False, "is_live": False}
    info = fetch_room_info(session, room_id)
    status_raw = info.get("status", room_data.get("status"))
    try:
        status_val = int(status_raw)
    except (TypeError, ValueError):
        status_val = None
    is_live = status_val == 2
    if not is_live and os.environ.get("DEBUG_LIVE") == "1":
        print()
        print_warning(f"DEBUG check_live: roomId={room_id} ditemukan, tapi status={status_raw!r} (bukan 2).")
        print(f"  Isi info : {json.dumps(info, ensure_ascii=False)[:800]}")
    owner = info.get("owner") or {}
    stats = info.get("liveRoomStats") or room_data.get("liveRoomStats") or {}
    result = {
        "found": True,
        "is_live": is_live,
        "room_id": room_id,
        "title": info.get("title") or room_data.get("title") or "-",
        "nickname": owner.get("nickname") or username,
        "viewers": stats.get("userCount"),
    }
    if is_live:
        live_room = room_data.get("liveRoom") or {}
        stream_url, quality = pick_stream_url_from_liveroom(live_room)
        if not stream_url:
            stream_url = pick_stream_url(info)
            quality = None
        if not stream_url:
            raise LiveCheckError(
                "Status live terdeteksi, tetapi URL stream tidak ditemukan "
                "(kemungkinan TikTok mengubah struktur respons API)."
            )
        result["stream_url"] = stream_url
        result["quality"] = quality
    return result

# ================================================================
# PEREKAMAN (FFMPEG)
# ================================================================

def find_ffmpeg():
    return shutil.which("ffmpeg")


def format_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_output_path(username):
    now = datetime.now(WIB)
    stamp = now.strftime("%d-%m-%Y_%H.%M")
    filename = f"{username}_{stamp}WIB.mp4"
    path = os.path.join(RECORD_DIR, filename)
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(path):
        path = f"{base}_{counter}{ext}"
        counter += 1
    return path


def graceful_stop(process):
    """Menghentikan ffmpeg secara halus (finalize file) sebelum paksa."""
    try:
        if process.stdin and not process.stdin.closed:
            process.stdin.write(b"q")
            process.stdin.flush()
            process.wait(timeout=10)
            return
    except Exception:
        pass
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def record_stream(stream_url, output_path, duration_seconds=None, stop_event=None):
    is_hls = ".m3u8" in stream_url.lower()
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-loglevel", "warning", "-stats",
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-thread_queue_size", "1024",
        "-user_agent", USER_AGENT,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_on_http_error", "4xx,5xx",
        "-reconnect_delay_max", "5",
        "-i", stream_url,
        "-avoid_negative_ts", "make_zero",
        "-c", "copy",
    ]
    if is_hls:
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof"]
    if duration_seconds:
        cmd += ["-t", str(int(duration_seconds))]
    cmd.append(output_path)

    process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        while True:
            try:
                process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if stop_event is not None and stop_event.is_set():
                    graceful_stop(process)
                    break
    except KeyboardInterrupt:
        print()
        print_warning("Menghentikan rekaman secara manual...")
        graceful_stop(process)
        raise
    return process.returncode


def monitor_live_status(session, username, stop_event, poll_interval=20):
    """Safety-net: mendeteksi live berakhir bila koneksi stream tidak putus sendiri."""
    misses = 0
    while not stop_event.is_set():
        if stop_event.wait(poll_interval):
            return
        try:
            result = check_live(session, username)
        except LiveCheckError:
            continue
        if not result.get("is_live"):
            misses += 1
            if misses >= 2:
                stop_event.set()
                return
        else:
            misses = 0


def run_recording(session, username, stream_url, duration_minutes, mode_label):
    os.makedirs(RECORD_DIR, exist_ok=True)
    output_path = build_output_path(username)
    duration_seconds = int(duration_minutes * 60) if duration_minutes else None

    print()
    print(C.wrap("─" * WIDTH, C.DIM))
    print(f" {C.wrap('● MEREKAM', C.RED + C.BOLD)} {username} · Mode: {mode_label}")
    print(f" Output : {output_path}")
    print(" Tekan Ctrl+C kapan saja untuk menghentikan rekaman lebih awal.")
    print(C.wrap("─" * WIDTH, C.DIM))
    print()

    stop_event = threading.Event()
    if duration_seconds is None:
        monitor = threading.Thread(
            target=monitor_live_status,
            args=(session, username, stop_event),
            daemon=True,
        )
        monitor.start()

    start_time = time.time()
    interrupted = False
    returncode = None
    try:
        returncode = record_stream(stream_url, output_path, duration_seconds, stop_event)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop_event.set()

    elapsed = time.time() - start_time
    exists = os.path.exists(output_path)
    size_mb = (os.path.getsize(output_path) / (1024 * 1024)) if exists else 0.0

    if interrupted:
        status_text = C.wrap("Dihentikan manual (Ctrl+C)", C.YELLOW)
    elif not exists or size_mb <= 0:
        status_text = C.wrap("Gagal - tidak ada data terekam", C.RED)
    elif returncode not in (0, None):
        status_text = C.wrap("Selesai dengan peringatan", C.YELLOW)
    else:
        status_text = C.wrap("Selesai", C.GREEN)

    print()
    print_box([
        ("Status", status_text),
        ("File", os.path.basename(output_path) if exists else "-"),
        ("Lokasi", output_path if exists else "-"),
        ("Durasi rekam", format_duration(elapsed)),
        ("Ukuran file", f"{size_mb:.2f} MB"),
    ])
    print()

# ================================================================
# MENU & PROMPT
# ================================================================

def print_username_prompt():
    print_section("MASUKKAN USERNAME TIKTOK")
    print()
    print(" Masukkan username TikTok yang ingin direkam (tanpa tanda @).")
    print(C.wrap(" Ketik 'exit' kapan saja untuk keluar dari program.", C.DIM))
    print()


def ask_username():
    print_username_prompt()
    while True:
        raw = input(" Username : ").strip()
        if raw.lower() in {"exit", "keluar", "quit"}:
            return None
        username = normalize_username(raw)
        if not username:
            print_error("Username tidak boleh kosong.")
            continue
        if not is_valid_username(username):
            print_error("Format username tidak valid (huruf, angka, titik, underscore).")
            continue
        return username


def print_menu():
    print_section("PILIH DURASI REKAM")
    print()
    options = [
        ("1", "Rekam sampai live selesai"),
        ("2", "Rekam selama 30 menit"),
        ("3", "Rekam dengan durasi custom (menit)"),
        ("4", "Kembali ke menu awal"),
        ("5", "Keluar dari program"),
    ]
    for num, label in options:
        print(f" [{num}] {label}")
    print()


def ask_menu_choice():
    while True:
        raw = input(" Pilih opsi (1-5) : ").strip()
        if raw in {"1", "2", "3", "4", "5"}:
            return raw
        print_error("Pilihan tidak valid. Masukkan angka 1 sampai 5.")


def ask_custom_duration():
    while True:
        raw = input(" Durasi rekam dalam menit (atau 'batal') : ").strip()
        if raw.lower() in {"batal", "cancel", "back"}:
            return None
        if raw == "":
            print_error("Durasi tidak boleh kosong.")
            continue
        try:
            minutes = float(raw.replace(",", "."))
        except ValueError:
            print_error("Masukkan angka yang valid, contoh: 45")
            continue
        if minutes <= 0:
            print_error("Durasi harus lebih besar dari 0 menit.")
            continue
        return minutes


def show_exit_message():
    quote = random.choice(QUOTES)
    top = "╔" + "═" * (WIDTH - 2) + "╗"
    bottom = "╚" + "═" * (WIDTH - 2) + "╝"
    blank = "║" + " " * (WIDTH - 2) + "║"
    print()
    print(C.wrap(top, C.CYAN))
    print(C.wrap(blank, C.CYAN))
    for row in textwrap.wrap(quote, WIDTH - 6):
        content = f" {row}".ljust(WIDTH - 2)
        print(C.wrap("║", C.CYAN) + content + C.wrap("║", C.CYAN))
    print(C.wrap(blank, C.CYAN))
    print(C.wrap(bottom, C.CYAN))
    print()
    print(center("Terima kasih telah menggunakan TikTok Live Recorder."))
    print()


def print_ffmpeg_missing():
    print_error("FFmpeg tidak ditemukan di PATH sistem.")
    print()
    print(" Tools ini membutuhkan FFmpeg untuk melakukan proses rekam.")
    print(" Silakan install terlebih dahulu, lalu jalankan ulang tools ini.")
    print()
    print(" Windows (winget) : winget install ffmpeg")
    print(" Manual           : https://ffmpeg.org/download.html")
    print()
    print(" Setelah instalasi, pastikan perintah berikut berhasil dijalankan:")
    print(" ffmpeg -version")
    print()


def print_cookie_help(message):
    print_error(message)
    print()
    print(" Pastikan file 'cookie.txt' berada di folder yang sama dengan")
    print(" script ini, dan berisi cookie akun TikTok yang sudah login.")
    print()
    print(" Cara mendapatkan cookie:")
    print(" 1. Login ke tiktok.com melalui browser.")
    print(" 2. Gunakan ekstensi seperti 'Get cookies.txt LOCALLY'.")
    print(" 3. Export cookie untuk domain tiktok.com sebagai cookie.txt,")
    print("    lalu simpan berdampingan dengan script ini.")
    print()

# ================================================================
# ALUR UTAMA
# ================================================================

def main():
    setup_console()
    print_banner()

    if find_ffmpeg() is None:
        print_ffmpeg_missing()
        sys.exit(1)

    try:
        cookies = load_cookies(COOKIE_PATH)
    except CookieError as exc:
        print_cookie_help(str(exc))
        sys.exit(1)

    session = build_session(cookies)
    print_success(f"Cookie berhasil dimuat ({len(cookies)} entri).")
    os.makedirs(RECORD_DIR, exist_ok=True)

    while True:
        username = ask_username()
        if username is None:
            show_exit_message()
            return

        try:
            with Spinner(f"Memeriksa status live untuk @{username} ..."):
                status = check_live(session, username)
        except LiveCheckError as exc:
            print()
            print_error(str(exc))
            continue

        if not status.get("is_live"):
            print()
            print_box([
                ("Username", f"@{username}"),
                ("Status", status_tag(False)),
            ])
            print_info("Akun ini sedang tidak live. Coba username lain atau cek lagi nanti.")
            continue

        viewers = status.get("viewers")
        viewers_display = f"{viewers:,}" if isinstance(viewers, int) else "-"
        quality = status.get("quality")
        print()
        print_box([
            ("Username", f"@{username}"),
            ("Nama", status.get("nickname", "-")),
            ("Status", status_tag(True)),
            ("Judul", status.get("title", "-")),
            ("Penonton", viewers_display),
            ("Kualitas", quality or "tidak diketahui"),
        ])

        stream_url = status["stream_url"]

        while True:
            print_menu()
            choice = ask_menu_choice()
            if choice == "1":
                run_recording(session, username, stream_url, None, "Sampai live selesai")
                break
            elif choice == "2":
                run_recording(session, username, stream_url, 30, "30 menit")
                break
            elif choice == "3":
                minutes = ask_custom_duration()
                if minutes is None:
                    continue
                run_recording(session, username, stream_url, minutes, f"Custom ({minutes:g} menit)")
                break
            elif choice == "4":
                break
            elif choice == "5":
                show_exit_message()
                return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print_warning("Program dihentikan oleh pengguna.")
        sys.exit(0)
    except Exception as exc:
        print()
        print_error(f"Terjadi kesalahan tak terduga: {exc}")
        sys.exit(1)
