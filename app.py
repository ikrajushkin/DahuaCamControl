"""Точка входа DahuaCamControl.

Запуск:  python app.py [--config путь_к_config.json] [--debug]

В собранной версии (PyInstaller onefile/onedir) все данные — config.json,
gate_log.jsonl, debug.log — лежат рядом с exe.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

import config as config_mod
from config import DEFAULT_CONFIG, applog, load_config, save_config
from gui import App


def _watchdog(app: "App", interval: float = 60.0) -> None:
    """Полный диагностический режим (--debug): раз в interval пишет состояние потоков.

    Повесьте перед app.run() — использует root.after, в mainloop не требуется поток.
    """
    def tick() -> None:
        if app._closing:
            return
        for i, w in enumerate(app.workers):
            vw, aw = w[0], w[1]
            applog("watchdog", f"[{i}] video alive={vw.is_alive()}, anpr alive={aw.is_alive()}")
        app.root.after(int(interval * 1000), tick)

    app.root.after(int(interval * 1000), tick)


def main() -> int:
    parser = argparse.ArgumentParser(description="Управление воротами Dahua (GUI)")
    parser.add_argument("--config", default=None, help="путь к config.json")
    parser.add_argument("--debug", action="store_true",
                        help="полный диагностический журнал (debug.log)")
    args = parser.parse_args()

    if args.debug:
        config_mod.ENABLE_DEBUG = True

    base = config_mod._data_dir()
    config_path = args.config or os.path.join(base, "config.json")

    if not os.path.exists(config_path):
        save_config(DEFAULT_CONFIG, config_path)
        msg = f"Создан шаблон конфигурации: {config_path} — заполните ip/username/password"
        print(msg)
        applog("app", msg)

    try:
        cfg = load_config(config_path)
        app = App(cfg, os.path.join(base, cfg["log_file"]))
        if args.debug:
            _watchdog(app)
    except Exception:
        # GUI ещё не запущен — ошибки инициализации фиксируем в debug.log и перевыбрасываем.
        import traceback
        applog("app", "ошибка инициализации:\n" + traceback.format_exc())
        raise
    applog("app", "приложение запущено (debug={})".format(bool(args.debug)))

    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())