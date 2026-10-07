"""Конфигурация приложения и справочник разрешённых номеров.

- config.json  — настройки камер и приложения (камера 1 обязательна, камера 2 —
  опциональна: панель появляется только если заполнены её ip/username/password);
- allowed_plates.json — справочник разрешённых номеров (список строк);
- gate_log.jsonl — журнал ANPR-событий и открытий ворот;
- debug.log — диагностический журнал (ошибки/переподключения).
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from typing import Any, Dict, List


def _data_dir() -> str:
    """Каталог данных: папка exe в frozen-режиме, папка исходников иначе."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ------------------------- Диагностический журнал -------------------------

# Полный диагностический режим (включается аргументом --debug в app.py)
ENABLE_DEBUG = False

DEBUG_LOG = os.path.join(_data_dir(), "debug.log")


def applog(source: str, message: str) -> None:
    """Диагностический журнал (debug.log рядом с exe/config.json).

    Потокобезопасно; ошибки записи проглатываются, чтобы журнал не ломал работу.
    Всегда пишутся: запуск, ошибки, подключения воркеров. Полный режим (--debug)
    дополнительно пишет каждые N секунд признаки «потоки живы» и heartbeat.
    """
    try:
        line = "{} [{}] {}".format(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source, message
        )
        with _applog_lock():
            with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def _applog_lock() -> threading.Lock:
    lock = getattr(applog, "_lock", None)
    if lock is None:
        lock = threading.Lock()
        setattr(applog, "_lock", lock)
    return lock


# Транслитерация «похожих» символов кириллических номеров в латиницу.
# Камеры Dahua могут отдавать один и тот же номер и латиницей, и кириллицей.
CYR_TO_LAT = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")

DEFAULT_CONFIG: Dict[str, Any] = {
    "cameras": [
        {"name": "Ворота — камера 1", "ip": "", "username": "", "password": "", "channel": 1},
        {"name": "Ворота — камера 2", "ip": "", "username": "", "password": "", "channel": 1},
    ],
    "video": {"width": 640, "height": 360, "subtype": 1},
    "anpr_events": ["TrafficJunction"],
    "heartbeat": 5,
    "read_timeout": 30,
    "whitelist_file": "allowed_plates.json",
    "log_file": "gate_log.jsonl",
}


def normalize_plate(plate: str) -> str:
    """Нормализация номера для сравнения: верхний регистр, кириллица→латиница, без пробелов и дефисов."""
    if not plate:
        return ""
    return plate.strip().upper().translate(CYR_TO_LAT).replace(" ", "").replace("-", "")


def is_camera_configured(cam: Dict[str, Any]) -> bool:
    """Камера считается настроенной, если заполнены ip, username и password."""
    return all(str(cam.get(k, "")).strip() for k in ("ip", "username", "password"))


def load_config(path: str) -> Dict[str, Any]:
    """Загрузка config.json с дозаполнением отсутствующих ключей из DEFAULT_CONFIG."""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in cfg.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def save_config(cfg: Dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class PlateBook:
    """Справочник разрешённых номеров (JSON-файл со списком строк)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._plates: List[str] = []
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._plates = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("plates", [])
            self._plates = [str(p).strip() for p in data if str(p).strip()]
        except (OSError, json.JSONDecodeError):
            self._plates = []

    def save(self) -> None:
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._plates, f, ensure_ascii=False, indent=2)

    def contains(self, plate: str) -> bool:
        target = normalize_plate(plate)
        with self._lock:
            return any(normalize_plate(p) == target for p in self._plates)

    def add(self, plate: str) -> bool:
        """Добавить номер в справочник. True — если номер был добавлен (раньше отсутствовал)."""
        plate = plate.strip()
        if not plate:
            return False
        with self._lock:
            target = normalize_plate(plate)
            if any(normalize_plate(p) == target for p in self._plates):
                return False
            self._plates.append(plate.upper())
        self.save()
        return True

    def items(self) -> List[str]:
        with self._lock:
            return list(self._plates)

    def remove(self, plate: str) -> bool:
        """Удалить номер из справочника. True — если номер был найден и удалён."""
        target = normalize_plate(plate)
        with self._lock:
            new = [p for p in self._plates if normalize_plate(p) != target]
            removed = len(new) != len(self._plates)
            self._plates = new
        if removed:
            self.save()
        return removed

    def reload(self) -> None:
        """Перечитать справочник с диска (после ручного редактирования файла)."""
        with self._lock:
            self._load()


def log_jsonl(path: str, record: Dict[str, Any]) -> None:
    """Потокобезопасная дозапись события в JSONL-лог."""
    with _jsonl_lock():
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            applog("config", f"не удалось записать лог {path}: {e}")


def _jsonl_lock() -> threading.Lock:
    lock = getattr(log_jsonl, "_lock", None)
    if lock is None:
        lock = threading.Lock()
        setattr(log_jsonl, "_lock", lock)
    return lock
