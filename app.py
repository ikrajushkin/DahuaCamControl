"""Точка входа DahuaCamControl.

Запуск:  python app.py [--config путь_к_config.json]
"""
from __future__ import annotations

import argparse
import os
import sys

from config import DEFAULT_CONFIG, PlateBook, load_config, save_config
from gui import App

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    parser = argparse.ArgumentParser(description="Управление воротами Dahua (GUI)")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    args = parser.parse_args()

    if not os.path.exists(args.config):
        save_config(DEFAULT_CONFIG, args.config)
        print(f"Создан шаблон конфигурации: {args.config} — заполните ip/username/password")
    cfg = load_config(args.config)
    book = PlateBook(os.path.join(BASE_DIR, cfg["whitelist_file"]))
    app = App(cfg, book, os.path.join(BASE_DIR, cfg["log_file"]))
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())