"""
================================================================
MEDIAFIRE UPLOADER -- Unofficial Web API Client
================================================================
Modul ini mengunggah file ke MediaFire memakai endpoint web
internal yang dipakai oleh app.mediafire.com -- BUKAN REST API
resmi yang mengharuskan registrasi App ID + API Key. Autentikasi
memakai cookie sesi browser (cookie_mediafire.txt), persis seperti
main.py mengautentikasi ke TikTok lewat cookie.txt.

Alur upload (resumable, mendukung file berukuran besar):
  1. application/get_session_token.php   -> session_token
  2. api/1.5/user/get_action_token.php    -> action_token (khusus upload)
  3. api/1.5/upload/check.php             -> info unit & preemptive quickkey
  4. upload/resumable.php (per unit)      -> unggah tiap potongan file
  5. api/1.5/upload/poll_upload.php       -> tunggu sampai diproses server
  6. Link akhir: mediafire.com/file/<quickkey>/<nama_file>/file

Referensi format response & parameter di atas: dokumentasi resmi
MediaFire Core API (Upload Guide) dan hasil observasi traffic
nyata dari aplikasi web MediaFire.

Catatan penting:
- Endpoint-endpoint ini tidak didokumentasikan sebagai API publik
  resmi (MediaFire mengarahkan pengembang ke Core API dengan App
  ID terdaftar), sehingga sewaktu-waktu bisa berubah tanpa
  pemberitahuan.
- Set DEBUG_UPLOAD=1 (environment variable) untuk mencetak detail
  response mentah tiap tahap apabila terjadi error yang sulit
  dilacak.
================================================================
"""

import hashlib
import json
import mimetypes
import os
import time
import urllib.parse

import requests

from main import CookieError, load_cookies  # parser cookie generik, dipakai ulang

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIAFIRE_COOKIE_PATH = os.path.join(BASE_DIR, "cookie_mediafire.txt")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

SESSION_TOKEN_URL = "https://www.mediafire.com/application/get_session_token.php"
ACTION_TOKEN_URL = "https://www.mediafire.com/api/1.5/user/get_action_token.php"
UPLOAD_CHECK_URL = "https://www.mediafire.com/api/1.5/upload/check.php"
RESUMABLE_UPLOAD_URL = "https://www.mediafireuserupload.com/api/upload/resumable.php"
POLL_UPLOAD_URL = "https://www.mediafire.com/api/1.5/upload/poll_upload.php"

DEFAULT_FOLDER_KEY = "myfiles"          # moniker untuk folder root akun
FALLBACK_UNIT_SIZE = 4 * 1024 * 1024    # 4 MB, dipakai hanya jika server tak memberi unit_size
MAX_CHUNK_RETRY_ROUNDS = 3
DEBUG = os.environ.get("DEBUG_UPLOAD") == "1"


class MediaFireError(Exception):
    pass


def _debug_dump(label, payload):
    if not DEBUG:
        return
    try:
        path = os.path.join(BASE_DIR, f"debug_upload_{label}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG_UPLOAD] {label} -> {path}")
    except Exception as exc:
        print(f"[DEBUG_UPLOAD] gagal menyimpan dump {label}: {exc}")


# ================================================================
# COOKIE & SESSION
# ================================================================

def load_mediafire_cookies(path=MEDIAFIRE_COOKIE_PATH):
    try:
        return load_cookies(path)
    except CookieError as exc:
        raise MediaFireError(str(exc)) from exc


def build_mediafire_session(cookies):
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://app.mediafire.com/",
        "Origin": "https://app.mediafire.com",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    })
    session.cookies.update(cookies)
    return session


def _post_json(session, url, **kwargs):
    try:
        resp = session.post(url, timeout=kwargs.pop("timeout", 30), **kwargs)
    except requests.RequestException as exc:
        raise MediaFireError(f"Gagal terhubung ke MediaFire ({url}): {exc}") from exc
    if resp.status_code != 200:
        raise MediaFireError(f"MediaFire merespons HTTP {resp.status_code} pada {url}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise MediaFireError(f"Respons MediaFire tidak dapat dibaca (bukan JSON) di {url}.") from exc
    return payload


def _unwrap(payload):
    """doupload kadang berupa dict tunggal, kadang list (saat banyak key).
    Beberapa endpoint (terutama upload/resumable.php, di domain berbeda dari
    endpoint lain) kemungkinan TIDAK membungkus hasil dalam "response" --
    ini tidak sempat terverifikasi dari traffic asli, jadi tangani dua-duanya:
    jika "response" tidak ada / bukan dict, anggap payload itu sendiri adalah
    datanya."""
    data = payload.get("response")
    if not isinstance(data, dict):
        data = payload
    doupload = data.get("doupload")
    if isinstance(doupload, list):
        doupload = doupload[0] if doupload else {}
    return data, (doupload or {})


# ================================================================
# TAHAP 1-3: SESSION TOKEN, ACTION TOKEN, UPLOAD CHECK
# ================================================================

def get_session_token(session):
    payload = _post_json(session, SESSION_TOKEN_URL)
    _debug_dump("session_token", payload)
    token = (payload.get("response") or {}).get("session_token")
    if not token:
        raise MediaFireError(
            "Gagal mendapatkan session_token MediaFire. "
            "Cookie di cookie_mediafire.txt mungkin sudah kedaluwarsa atau belum login."
        )
    return token


def get_action_token(session, session_token, token_type="upload", lifespan=1440):
    files = {
        "type": (None, token_type),
        "lifespan": (None, str(lifespan)),
        "response_format": (None, "json"),
        "session_token": (None, session_token),
    }
    payload = _post_json(session, ACTION_TOKEN_URL, files=files)
    _debug_dump("action_token", payload)
    data = payload.get("response") or {}
    token = data.get("action_token")
    if not token:
        raise MediaFireError("Gagal mendapatkan action_token MediaFire untuk upload.")
    return token


def _sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def upload_check(session, action_token, filename, size, file_hash, folder_key=DEFAULT_FOLDER_KEY):
    uploads = json.dumps([{
        "filename": filename,
        "folder_key": folder_key,
        "size": size,
        "hash": file_hash,
        "resumable": "yes",
        "preemptive": "yes",
    }])
    files = {
        "uploads": (None, uploads),
        "response_format": (None, "json"),
        "session_token": (None, action_token),
    }
    payload = _post_json(session, UPLOAD_CHECK_URL, files=files)
    _debug_dump("upload_check", payload)
    data = payload.get("response") or {}
    # Jika request dikirim sebagai batch, sebagian versi API membungkus hasil
    # per-file dalam "upload_checks": [...]. Ratakan supaya konsisten.
    checks = data.get("upload_checks")
    if isinstance(checks, list) and checks:
        merged = dict(data)
        merged.update(checks[0])
        data = merged
    if data.get("result") != "Success":
        raise MediaFireError(f"MediaFire menolak permintaan upload/check: {data}")
    return data


# ================================================================
# TAHAP 4: UPLOAD RESUMABLE (PER UNIT / CHUNK)
# ================================================================

def _decode_bitmap(bitmap):
    """
    Uraikan resumable_upload.bitmap menjadi set index unit yang SUDAH
    berhasil terupload. Mengikuti algoritma resmi dari dokumentasi
    MediaFire (16-bit words, dibaca dari bit paling rendah).
    """
    words = bitmap.get("words") or []
    done = set()
    for word_index, word in enumerate(words):
        try:
            value = int(word)
        except (TypeError, ValueError):
            continue
        for bit in range(16):
            if value & (1 << bit):
                done.add(word_index * 16 + bit)
    return done


def upload_chunks(session, action_token, filepath, filename, total_size, file_hash,
                   unit_size, number_of_units, folder_key=DEFAULT_FOLDER_KEY,
                   progress_callback=None):
    """
    Unggah seluruh unit/chunk file lewat upload/resumable.php.
    Mengembalikan 'upload_key' (doupload.key) yang dipakai untuk poll_upload.
    Nilai upload_key selalu sama di setiap response chunk pada file yang sama.
    """
    params = {
        "folder_key": folder_key,
        "response_format": "json",
        "session_token": action_token,
    }

    def _read_unit(unit_id):
        with open(filepath, "rb") as f:
            f.seek(unit_id * unit_size)
            return f.read(unit_size)

    def _send_unit(unit_id):
        chunk = _read_unit(unit_id)
        unit_hash = hashlib.sha256(chunk).hexdigest()
        headers = {
            "Content-Type": "application/octet-stream",
            "x-filename": filename,
            "x-filesize": str(total_size),
            "x-filehash": file_hash,
            "x-unit-hash": unit_hash,
            "x-unit-id": str(unit_id),
            "x-unit-size": str(len(chunk)),
        }
        try:
            resp = session.post(
                RESUMABLE_UPLOAD_URL, params=params, headers=headers,
                data=chunk, timeout=180,
            )
        except requests.RequestException as exc:
            raise MediaFireError(f"Gagal mengunggah potongan #{unit_id}: {exc}") from exc
        if resp.status_code != 200:
            raise MediaFireError(
                f"MediaFire menolak potongan #{unit_id} (HTTP {resp.status_code})."
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MediaFireError(f"Respons potongan #{unit_id} tidak dapat dibaca.") from exc
        _debug_dump(f"resumable_unit_{unit_id}", payload)
        data, doupload = _unwrap(payload)
        result_code = str(doupload.get("result", "0"))
        if result_code not in ("0", ""):
            raise MediaFireError(f"MediaFire menolak potongan #{unit_id}: kode {result_code}")
        return len(chunk), doupload.get("key"), (data.get("resumable_upload") or {})

    upload_key = None
    uploaded_bytes = 0
    pending = list(range(number_of_units))

    for _round in range(MAX_CHUNK_RETRY_ROUNDS):
        resumable_info = {}
        for unit_id in pending:
            size, key, resumable_info = _send_unit(unit_id)
            upload_key = key or upload_key
            uploaded_bytes = min(uploaded_bytes + size, total_size)
            if progress_callback:
                progress_callback(uploaded_bytes, total_size)

        if resumable_info.get("all_units_ready") == "yes":
            if not upload_key:
                raise MediaFireError(
                    "Semua unit terupload tapi MediaFire tidak memberi upload_key."
                )
            return upload_key

        # Sebagian unit gagal diproses -> cek bitmap, upload ulang yang hilang saja.
        bitmap = resumable_info.get("bitmap") or {}
        done_units = _decode_bitmap(bitmap)
        pending = [u for u in range(number_of_units) if u not in done_units]
        if not pending:
            return upload_key

    raise MediaFireError(
        f"Sebagian potongan gagal diunggah setelah {MAX_CHUNK_RETRY_ROUNDS}x percobaan "
        f"(unit tersisa: {pending})."
    )


# ================================================================
# TAHAP 5: POLL UPLOAD
# ================================================================

def poll_upload(session, action_token, upload_key, interval=2.0, timeout=1800, on_tick=None):
    deadline = time.time() + timeout
    last_doupload = {}
    while time.time() < deadline:
        files = {
            "key": (None, upload_key),
            "response_format": (None, "json"),
            "session_token": (None, action_token),
        }
        payload = _post_json(session, POLL_UPLOAD_URL, files=files)
        _debug_dump("poll_upload", payload)
        _data, doupload = _unwrap(payload)
        last_doupload = doupload
        status = str(doupload.get("status", ""))
        if on_tick:
            on_tick(status, doupload.get("description", ""))
        if status == "99":
            quickkey = doupload.get("quickkey")
            if not quickkey:
                raise MediaFireError(
                    f"Upload selesai diproses tapi MediaFire tidak memberi quickkey: {doupload}"
                )
            return doupload
        time.sleep(interval)
    raise MediaFireError(f"Timeout menunggu MediaFire memproses upload. Status terakhir: {last_doupload}")


# ================================================================
# FUNGSI UTAMA
# ================================================================

def build_share_link(quickkey, filename):
    return f"https://www.mediafire.com/file/{quickkey}/{urllib.parse.quote(filename)}/file"


def upload_file(session, filepath, folder_key=DEFAULT_FOLDER_KEY, progress_callback=None,
                 on_phase_change=None):
    """
    Unggah satu file ke MediaFire dan kembalikan link download-nya.

    progress_callback(uploaded_bytes, total_bytes) dipanggil setiap kali
    sebuah unit selesai diunggah -- cocok dipakai untuk menampilkan
    progres realtime (mis. diedit ke pesan Telegram).

    on_phase_change(phase) dipanggil saat berpindah tahap ("uploading" ->
    "finalizing") -- berguna supaya UI bisa membedakan "masih mengirim data"
    vs "sudah terkirim semua, menunggu MediaFire selesai memproses", karena
    tahap kedua ini bisa memakan waktu tanpa progres byte yang berubah.
    """
    if not os.path.isfile(filepath):
        raise MediaFireError(f"File tidak ditemukan: {filepath}")

    filename = os.path.basename(filepath)
    total_size = os.path.getsize(filepath)
    if total_size <= 0:
        raise MediaFireError("Ukuran file 0 byte, tidak ada yang bisa diunggah.")

    file_hash = _sha256_file(filepath)

    session_token = get_session_token(session)
    action_token = get_action_token(session, session_token)

    check_data = upload_check(session, action_token, filename, total_size, file_hash, folder_key)

    # File (persis sama by hash & nama) sudah pernah ada -> tidak perlu upload ulang.
    if (check_data.get("file_exists") == "yes"
            and check_data.get("different_hash") == "no"
            and check_data.get("duplicate_quickkey")):
        if progress_callback:
            progress_callback(total_size, total_size)
        return build_share_link(check_data["duplicate_quickkey"], filename)

    resumable = check_data.get("resumable_upload") or {}
    unit_size = int(resumable.get("unit_size") or FALLBACK_UNIT_SIZE)
    number_of_units = int(resumable.get("number_of_units") or 1)

    upload_key = upload_chunks(
        session, action_token, filepath, filename, total_size, file_hash,
        unit_size, number_of_units, folder_key, progress_callback,
    )
    if not upload_key:
        raise MediaFireError(
            "Semua potongan terkirim tapi tidak mendapat upload_key dari MediaFire -- "
            "tidak bisa memverifikasi status upload. Set DEBUG_UPLOAD=1 untuk detail respons."
        )

    if on_phase_change:
        on_phase_change("finalizing")

    doupload = poll_upload(session, action_token, upload_key)

    final_quickkey = doupload.get("quickkey")
    final_filename = doupload.get("filename") or filename
    return build_share_link(final_quickkey, final_filename)
