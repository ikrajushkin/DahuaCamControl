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
from typing import Any, Dict, List, Tuple


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
    # video.mode: 'snapshot' (по умолчанию) — опрос снимков snapshot.cgi: полное
    # разрешение с матрицы (не зависит от подпотоков) и без «замерзания» потока;
    # 'stream' — MJPEG mjpg/video.cgi (больше FPS, но может подолгу молчать).
    # video.width/video.height — размер окна видео в GUI (масштаб отображения).
    # video.photo_scale — размер фото авто ПОД видео относительно видео:
    #   0.5 — вдвое меньше (по умолчанию), 1.0 — в размер видео.
    # video.set_resolution ('stream') — смена кодирования потока НА КАМЕРЕ.
    "video": {"width": 640, "height": 360, "subtype": 1, "set_resolution": "",
              "mode": "snapshot", "interval": 0.4, "photo_scale": 0.5},
    "anpr_events": ["TrafficJunction"],
    "heartbeat": 5,
    "read_timeout": 30,
    # Периодическое ЧТЕНИЕ БД номеров камеры (разделы 10.7.x API, только чтение):
    # enable=False — не синхронизировать; sync_interval=0 — один раз при запуске.
    "camera_whitelist": {"enable": True, "sync_interval": 300},
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


def plate_decision(cam_allow: bool, cam_block: bool,
                   cam_available: bool = True) -> Tuple[bool, str]:
    """Итог сверки номера по БД камеры (ТОЛЬКО ЧТЕНИЕ): (разрешён, метка).

    Локальный список не используется: решение принимает камера (AllowListDB /
    BlockListDB) или оператор. При недоступной БД камеры решение не принимается.
    """
    if not cam_available:
        return False, "справочник камеры недоступен"
    if cam_allow:
        return True, "ЕСТЬ в справочнике камеры"
    if cam_block:
        return False, "ЗАПРЕЩЁН (блок-лист камеры)"
    return False, "НЕТ в справочнике камеры"


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
