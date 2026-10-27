"""Смоук модального окна «Пропуска» (PassDialog) в скрытом Tk-окне.

Проверяет без сети и реальных камер: сборку виджетов, владельца по умолчанию
«Camera control», выбор записи в форму, фильтр поиска, валидацию пустого номера,
добавление через стаб камеры (фоновый поток + очередь UI). Быстрый запуск:
    python smoke_pass_dialog.py
"""
import queue
import threading
import time
from datetime import datetime

import gui
from dahua_client import PASS_OWNER_DEFAULT


class StubCam:
    """Стаб камеры: список пропусков в памяти (интерфейс PassDialog)."""
    name = "Тестовая камера"
    last_error = ""

    def __init__(self):
        self.records = [
            {"PlateNumber": "A123BB77", "MasterOfCar": "Козел 1", "RecNo": "1",
             "BeginTime": "2000-01-01 00:00:00", "CancelTime": "2037-12-31 23:59:59"},
            {"PlateNumber": "C737XH777", "MasterOfCar": "Иванов", "RecNo": "2",
             "BeginTime": "2026-01-01 08:00:00", "CancelTime": "2026-12-31 23:59:59"},
        ]
        self.last_add = None

    def list_passes(self):
        return [dict(r) for r in self.records]

    def add_pass(self, plate, owner="", begin="", cancel=""):
        self.last_add = (plate, owner, begin, cancel)
        self.records.append({"PlateNumber": plate, "MasterOfCar": owner,
                             "RecNo": str(len(self.records) + 1),
                             "BeginTime": begin, "CancelTime": cancel})
        return True, f"добавлен пропуск {plate}", len(self.records)

    def update_pass(self, rec_no, plate, owner="", begin="", cancel=""):
        return True, f"изменён пропуск {plate}"

    def remove_pass(self, rec_no):
        return True, f"удалён пропуск (recNo={rec_no})"

    def export_camera_plates(self):
        return {"allow": sorted(r["PlateNumber"] for r in self.records),
                "block": [], "source": "stub"}


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


root = gui.tk.Tk()
root.withdraw()
cam = StubCam()
changed, log_lines = [], []
dlg = gui.PassDialog(root, [cam], on_changed=lambda n, s: changed.append(n),
                     log=log_lines.append)
pump(root, 0.6)

# список загружен из стаба; новый пропуск — владелец и период по умолчанию
assert dlg.lst.size() == 2, dlg.lst.size()
assert dlg.ent_owner.get() == PASS_OWNER_DEFAULT, dlg.ent_owner.get()
today = datetime.now().strftime("%Y-%m-%d")
assert dlg.ent_begin.get().startswith(today), dlg.ent_begin.get()
assert dlg.ent_cancel.get() == today + " 23:59:59", dlg.ent_cancel.get()
assert dlg.winfo_x() >= 0 and dlg.winfo_y() >= 0  # центрирование в границах экрана
# «Сохранить» без выбора — подсказка выбрать пропуск
dlg._on_save()
assert "выберите" in dlg.lbl_status.cget("text"), dlg.lbl_status.cget("text")
# выбор записи -> форма (номер, владелец, период действия)
dlg.lst.selection_set(0)
dlg._on_select()
assert dlg.ent_plate.get() == "A123BB77", dlg.ent_plate.get()
assert dlg.ent_owner.get() == "Козел 1", dlg.ent_owner.get()
assert dlg.ent_begin.get() == "2000-01-01 00:00:00", dlg.ent_begin.get()
assert dlg.ent_cancel.get() == "2037-12-31 23:59:59", dlg.ent_cancel.get()
# поиск фильтрует и сбрасывается
dlg.search._var.set("иван")
pump(root, 0.3)
assert dlg.lst.size() == 1, dlg.lst.size()
dlg.search._var.set("")
pump(root, 0.3)
assert dlg.lst.size() == 2, dlg.lst.size()
# пустой номер -> ошибка без запроса
dlg.ent_plate.delete(0, "end")
dlg._on_add()
assert "введите номер" in dlg.lbl_status.cget("text"), dlg.lbl_status.cget("text")
# неверная дата -> ошибка без запроса
dlg.ent_plate.insert(0, "е999хх77")
dlg.ent_begin.delete(0, "end")
dlg.ent_begin.insert(0, "вчера")
dlg._on_add()
assert "формат" in dlg.lbl_status.cget("text"), dlg.lbl_status.cget("text")
# окончание раньше начала -> ошибка
dlg._reset_form()
dlg.ent_plate.insert(0, "е999хх77")
dlg.ent_owner.delete(0, "end")
dlg.ent_owner.insert(0, "Новый")
dlg.ent_cancel.delete(0, "end")
dlg.ent_cancel.insert(0, "2020-01-01 00:00:00")
dlg._on_add()
assert "позже" in dlg.lbl_status.cget("text"), dlg.lbl_status.cget("text")
# добавление через стаб: фоновый поток, очередь, обновление списка и колбэка
dlg._reset_form()
dlg.ent_plate.insert(0, "е999хх77")
dlg.ent_owner.delete(0, "end")
dlg.ent_owner.insert(0, "Новый")
dlg._on_add()
pump(root, 1.2)
assert any("добавлен" in ln for ln in log_lines), log_lines
# стаб получил номер (нормализованный), владельца и период из формы
assert cam.last_add[0] == "E999XX77" and cam.last_add[1] == "Новый", cam.last_add
assert cam.last_add[2].startswith(today), cam.last_add  # начало — сегодня
assert cam.last_add[3] == today + " 23:59:59", cam.last_add  # до конца суток
assert dlg.lst.size() == 3, dlg.lst.size()
assert changed == ["Тестовая камера"], changed
dlg._close()
root.destroy()
print("OK PassDialog (центр, период действия, поиск, добавление через стаб)")