# -*- coding: utf-8 -*-
"""Смоук: виджеты поиска (SearchEntry + фильтрация в SidePanel/LogPanel) в скрытом окне."""
import sys
import tkinter as tk

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"c:\Proj\DahuaCamControl")

import gui

root = tk.Tk()
root.withdraw()

# --- SidePanel: список номеров + поиск ---
side = gui.SidePanel(root, None)
side.update_camera_list(["A123BB77", "C737XH777", "X999YY197"])
assert side.lst.size() == 3, side.lst.size()
side.search._var.set("777")
root.update_idletasks()
assert side.search.query() == "777"
assert side.lst.size() == 1 and side.lst.get(0) == "C737XH777", side.lst.get(0, "end")
assert "найдено 1 из 3" in side.lbl_count.cget("text"), side.lbl_count.cget("text")
side.search._var.set("а1")  # кириллица/раскладка → нормализация
root.update_idletasks()
assert side.lst.size() == 1 and side.lst.get(0) == "A123BB77", side.lst.get(0, "end")
side.search._var.set("а")   # 1 символ — без фильтра
root.update_idletasks()
assert side.lst.size() == 3, side.lst.size()
side.update_camera_list(None, "HTTP 403")
assert "недоступен" in side.lbl_count.cget("text")

# --- LogPanel: история + поиск ---
log = gui.LogPanel(root, None)
log.log("номер A123BB77 — разрешено: ЕСТЬ в справочнике камеры")
log.log("номер C737XH777 — не разрешено: НЕТ в справочнике камеры")
log.log("Отправка команды открытия ворот (камера 1)…")
assert len(log.txt.get("1.0", "end-1c").splitlines()) == 3, log.txt.get("1.0", "end")
log.search._var.set("а123")
root.update_idletasks()
shown = log.txt.get("1.0", "end-1c")
assert len(shown.splitlines()) == 1 and "A123BB77" in shown, shown
log.search._var.set("открытия")   # слово из журнала, без учёта регистра
root.update_idletasks()
shown = log.txt.get("1.0", "end-1c")
assert "Отправка команды открытия" in shown and "номер" not in shown, shown
log.search._var.set("")
root.update_idletasks()
assert len(log.txt.get("1.0", "end-1c").splitlines()) == 3

# --- SearchEntry: подсказка ---
e = gui.SearchEntry(root, lambda: None, width=10)
assert e.query() == "" and e.get() == gui.SEARCH_HINT
e._focus_in()
e._var.set(" 77 ")
assert e.query() == "77"
e._var.set("   ")   # пусто/пробелы → при потере фокуса возвращается подсказка
e._focus_out()
assert e.get() == gui.SEARCH_HINT and e.query() == ""

root.destroy()
print("OK смоук GUI: поиск в справочнике и истории (фильтр со 2-го символа)")