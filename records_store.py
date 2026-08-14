"""
================================================================
RECORDS STORE -- Riwayat Hasil Record
================================================================
Penyimpanan riwayat hasil record TikTok Live yang sudah diupload
ke MediaFire. Dipakai oleh bot.py untuk menu "Lihat Hasil Record".

Data disimpan sebagai satu file JSON (records.json) berisi list
of dict, dengan penulisan yang aman untuk multi-thread (lock +
tulis ke file sementara lalu rename atomik).
================================================================
"""

import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDS_PATH = os.path.join(BASE_DIR, "records.json")

_lock = threading.Lock()


def _read_all():
    if not os.path.isfile(RECORDS_PATH):
        return []
    try:
        with open(RECORDS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _write_all(records):
    tmp_path = RECORDS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, RECORDS_PATH)


def add_record(record):
    """
    Tambahkan satu record hasil upload.

    `record` minimal berisi: username, date, time, mediafire_link.
    Field lain (mis. requested_by, duration, size_mb) boleh ikut
    disertakan dan akan ikut tersimpan.
    """
    with _lock:
        records = _read_all()
        records.append(record)
        _write_all(records)


def list_records(limit=None, newest_first=True):
    with _lock:
        records = _read_all()
    if newest_first:
        records = list(reversed(records))
    if limit is not None:
        records = records[:limit]
    return records


def count_records():
    with _lock:
        return len(_read_all())
