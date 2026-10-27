"""Смоук PassDialog на ЖИВОЙ камере: только чтение списка (запись не выполняется).

Загружает модальное окно пропусков с реальной камерой из config.json и проверяет,
что список пропусков подгружается (recordFinder). Запуск:
    python smoke_pass_dialog_live.py --config dist\\config.json
"""
import os
import sys
import time

import config as config_mod
from config import load_config
import gui
from dahua_client import DahuaCamera

path = sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv \
    else os.path.join(config_mod._data_dir(), "config.json")
cfg = load_config(path)
cam_cfg = [c for c in cfg["cameras"] if config_mod.is_camera_configured(c)][0]
cam = DahuaCamera(cam_cfg, name=str(cam_cfg.get("name") or "Камера"))

root = gui.tk.Tk()
root.withdraw()
dlg = gui.PassDialog(root, [cam])
end = time.time() + 15
while time.time() < end:
    root.update()
    time.sleep(0.05)

n = dlg.lst.size()
status = dlg.lbl_status.cget("text")
print(f"записей в списке: {n}; статус: {status}")
assert n > 0 and "записей" in status, (n, status)
# выбор записи — период действия подтягивается из пропуска камеры
dlg.lst.selection_set(0)
dlg._on_select()
assert dlg.ent_begin.get() and dlg.ent_cancel.get(), "период из записи не подтянулся"
ok, b, c, err = __import__("dahua_client").validate_pass_period(
    dlg.ent_begin.get(), dlg.ent_cancel.get())
print(f"период первой записи: {dlg.ent_begin.get()} .. {dlg.ent_cancel.get()}"
      + ("" if ok else f" (не период: {err})"))
dlg._close()
root.destroy()
print("OK PassDialog на живой камере (список и период действия загружены)")