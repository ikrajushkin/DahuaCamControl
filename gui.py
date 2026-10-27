"""Графический интерфейс (tkinter): панели камер с видео, кнопками открытия ворот,
фото последнего автомобиля, крупным номером и меткой наличия в справочнике.

Раскладка: [ Панель камеры 1 | Панель камеры 2 (опц.) | Справочник ]
           [ Журнал (под панелями камер)                           ]
"""
from __future__ import annotations

import io
import queue
import threading
import time
import tkinter as tk
import tkinter.ttk as ttk
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageTk

import dahua_client as dahua_client_mod
from config import is_camera_configured, log_jsonl, plate_decision
from dahua_client import (ANPRResult, DahuaCamera, AnprWorker, VideoWorker,
                          PASS_OWNER_DEFAULT, WhitelistSyncWorker, fit_image,
                          make_placeholder, normalize_plate, validate_pass_input,
                          validate_pass_period)

# ------------------------------- Оформление -------------------------------


BG = "#0e0e0e"
PANEL_BG = "#161616"
FG = "#e8e8e8"
MUTED = "#8a8a8a"
GREEN = "#2ecc71"
RED = "#e74c3c"
ACCENT = "#2f81f7"
ENTRY_BG = "#1f1f1f"

VIDEO_W, VIDEO_H = 640, 360
PHOTO_SCALE_DEFAULT = 0.5  # фото авто — вдвое меньше видео (настраивается video.photo_scale)
PHOTO_SCALE = PHOTO_SCALE_DEFAULT
PHOTO_W, PHOTO_H = 320, 180  # итоговое фото: video.width/height × photo_scale

F_TITLE = ("Segoe UI", 12, "bold")
F_BUTTON = ("Segoe UI", 13, "bold")
F_PLATE = ("Consolas", 26, "bold")
F_SMALL = ("Segoe UI", 9)
F_LOG = ("Consolas", 9)

# Ширина справочника справа — «чуть больше» максимальной длины номера
# (нормализованный номер — до 9 знаков, например «A123BC777»).
PLATE_MAX_CHARS = 9


def normalize_photo_scale(value: Any) -> float:
    """Масштаб фото авто относительно видео (video.photo_scale): 0.5 — вдвое меньше,
    1.0 — в размер видео. Значения вне диапазона 0.25..1.0 приводятся к допустимым."""
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return PHOTO_SCALE_DEFAULT
    return min(1.0, max(0.25, scale))


def photo_size(vw: int, vh: int, scale: float) -> Tuple[int, int]:
    """Итоговый размер фото под видео vw×vh при масштабе scale (не меньше 1 px)."""
    return max(1, round(vw * scale)), max(1, round(vh * scale))


def anpr_display_action(shown_event_id: int, res_event_id: int, has_image: bool) -> str:
    """Что делать с фото-панелью для результата ANPR: 'wait' | 'photo' | 'skip'.

    «wait» — текст нового события: сразу плейсхолдер «снимок проезда…», чтобы под
    новым номером никогда не осталось фото предыдущего авто; «photo» — снимок
    текущего события, показать; «skip» — поздний снимок уже заменённого события
    (или снимок без события) — не затирать актуальные данные.
    """
    if has_image:
        return "photo" if res_event_id == shown_event_id else "skip"
    return "wait"


SEARCH_MIN_CHARS = 2  # фильтрация поиска — со 2-го символа запроса
SEARCH_HINT = "поиск…"


def search_filter(query: str, items: List[str]) -> List[str]:
    """Отфильтровать строки по вхождению запроса в текст (без учёта регистра).

    Фильтрация начинается со 2-го символа: пустой запрос, пробелы или один
    символ — показаны все строки. Дополнительно учитывается «нормализованный»
    запрос (кириллица/раскладка в номере): «а123» находит «A123BB77».
    """
    q = (query or "").strip()
    if len(q) < SEARCH_MIN_CHARS:
        return list(items)
    qq = q.casefold()
    nq = normalize_plate(q).casefold()
    out: List[str] = []
    for s in items:
        t = (s or "").casefold()
        if qq in t or (nq and nq in t):
            out.append(s)
    return out


class CameraPanel(tk.Frame):
    """Панель одной камеры: видео, кнопка открытия ворот, фото авто + номер + метка."""

    PHOTO_WAIT_S = 20.0  # через сколько секунд показать «снимок задерживается…»

    def __init__(self, master: tk.Misc, app: "App", cam_cfg: Dict[str, Any], index: int):
        super().__init__(master, bg=PANEL_BG, padx=10, pady=8)
        self.app = app
        self.cam_cfg = cam_cfg
        self.index = index
        self.name = str(cam_cfg.get("name") or f"Камера {index + 1}")
        self._video_photo: Optional[ImageTk.PhotoImage] = None
        self._photo_photo: Optional[ImageTk.PhotoImage] = None
        self._last: Optional[ANPRResult] = None
        # Атомарность «номер + фото»: id отображаемого события и время ожидания снимка.
        self._shown_event_id = 0
        self._photo_wait_ts = 0.0
        # Диагностика потока: реальное разрешение кадров и FPS прихода кадров.
        self._stream_size: Optional[Tuple[int, int]] = None
        self._fps_count = 0
        self._fps_ts = 0.0
        self._fps = 0.0
        self._base_status = ""
        self._last_frame_ts = 0.0
        self._stale_shown = False
        self._build()
        self.show_video_placeholder("подключение…")
        self.show_photo_placeholder("автомобилей пока нет")

    def _build(self) -> None:
        top = tk.Frame(self, bg=PANEL_BG)
        top.pack(fill="x")
        tk.Label(top, text=self.name, bg=PANEL_BG, fg=FG, font=F_TITLE).pack(side="left")
        self.lbl_status = tk.Label(top, text="", bg=PANEL_BG, fg=MUTED, font=F_SMALL)
        self.lbl_status.pack(side="right")

        self.lbl_video = tk.Label(self, bg="#000000", bd=0)
        self.lbl_video.pack(pady=(8, 6))

        self.btn_open = tk.Button(
            self, text="ОТКРЫТЬ ВОРОТА", command=self._on_open,
            bg=ACCENT, fg="white", activebackground="#1f6fd0", activeforeground="white",
            relief="flat", font=F_BUTTON, padx=16, pady=8, cursor="hand2")
        self.btn_open.pack(fill="x")

        row = tk.Frame(self, bg=PANEL_BG)
        row.pack(fill="x", pady=(10, 0))
        self.lbl_plate = tk.Label(row, text="—", bg=PANEL_BG, fg=FG, font=F_PLATE)
        self.lbl_plate.pack(side="left")

        row2 = tk.Frame(self, bg=PANEL_BG)
        row2.pack(fill="x")
        self.lbl_wh = tk.Label(row2, text="ожидание распознавания…", bg=PANEL_BG,
                               fg=MUTED, font=("Segoe UI", 10, "bold"))
        self.lbl_wh.pack(side="left")

        self.lbl_photo = tk.Label(self, bg="#000000", bd=0)
        self.lbl_photo.pack(pady=(8, 0))

        self.lbl_meta = tk.Label(self, text="", bg=PANEL_BG, fg=MUTED, font=F_SMALL,
                                 justify="left", anchor="w")
        self.lbl_meta.pack(fill="x", pady=(4, 0))

    # ------------------------------ отрисовка ------------------------------

    def show_video_placeholder(self, text: str) -> None:
        self._set_image(self.lbl_video, make_placeholder(VIDEO_W, VIDEO_H, text), "_video_photo")

    def show_photo_placeholder(self, text: str) -> None:
        self._set_image(self.lbl_photo, make_placeholder(PHOTO_W, PHOTO_H, text), "_photo_photo")

    def show_frame(self, jpeg: bytes) -> None:
        """Отобразить JPEG-кадр live-видео (быстро: без лишних конвертаций)."""
        try:
            img = Image.open(io.BytesIO(jpeg))
            # FPS прихода кадров — признак «живого» потока (0.0 = поток завис).
            self._fps_count += 1
            now = time.time()
            if self._fps_ts <= 0:
                self._fps_ts = now
            elif now - self._fps_ts >= 2.0:
                self._fps = self._fps_count / (now - self._fps_ts)
                self._fps_count = 0
                self._fps_ts = now
                self._update_stream_status()
            size = (img.width, img.height)
            if size != self._stream_size:
                # Сообщаем реальное разрешение потока камеры (не размер окна).
                self._stream_size = size
                self._update_stream_status()
            self._last_frame_ts = time.time()
            if self._stale_shown:
                self._stale_shown = False
                self._update_stream_status()
            if size == (VIDEO_W, VIDEO_H):
                self._set_image(self.lbl_video, img, "_video_photo")
            else:
                self._set_image(self.lbl_video, fit_image(img, VIDEO_W, VIDEO_H), "_video_photo")
        except Exception:
            pass

    def _update_stream_status(self) -> None:
        """«поток: 704x576 → окно 640x360 · 12 FPS» — видно и разрешение, и «подвис»."""
        parts = []
        if self._stream_size:
            parts.append(f"{self._stream_size[0]}x{self._stream_size[1]}")
            parts.append(f"→ окно {VIDEO_W}x{VIDEO_H}")
        if self._fps:
            parts.append(f"{self._fps:.0f} FPS")
        elif self._stream_size:
            parts.append("кадры не идут")
        text = "поток: " + " · ".join(parts) if parts else ""
        self._set_status_now(text)

    def set_status(self, text: str) -> None:
        self._base_status = text
        if self._stream_size and not self._stale_shown:
            # Не затирать диагностическую строку разрешения/FPS.
            return
        self._set_status_now(text)

    def tick_status(self) -> None:
        """Проверка «живости» потока (вызывается из GUI-опроса очередей)."""
        if self._photo_wait_ts and time.time() - self._photo_wait_ts > self.PHOTO_WAIT_S:
            # Снимок события задерживается — честно показываем это вместо молчания,
            # старое фото под новым номером не возвращается.
            self._photo_wait_ts = 0.0
            self.show_photo_placeholder("снимок задерживается…")
        if not self._stream_size or self._last_frame_ts <= 0:
            return
        stale = (time.time() - self._last_frame_ts) > 5.0
        if stale != self._stale_shown:
            self._stale_shown = stale
            if stale:
                base = f"{self._base_status} · " if self._base_status else ""
                self._set_status_now(f"{base}поток пропал (нет кадров >5с)")
            else:
                self._update_stream_status()

    def _set_status_now(self, text: str) -> None:
        self.lbl_status.config(text=text)

    def show_anpr(self, res: ANPRResult, allowed: bool, mark: str = "",
                  available: bool = True) -> None:
        """Отобразить результат распознавания: фото, номер и метку сверки с камерой.

        Номер и фото атомарны (см. anpr_display_action): под новым номером никогда
        не остаётся фото предыдущего проезда, поздний снимок уже заменённого
        события не затирает актуальные данные.
        """
        action = anpr_display_action(self._shown_event_id, res.event_id, bool(res.image))
        if action == "skip":
            return
        self._last = res
        if action == "wait":
            self._shown_event_id = res.event_id
            self._photo_wait_ts = time.time()
            self.show_photo_placeholder("снимок проезда…")
        else:  # photo: снимок именно текущего отображаемого события
            self._photo_wait_ts = 0.0
            try:
                img = Image.open(io.BytesIO(res.image))
                self._set_image(self.lbl_photo, fit_image(img, PHOTO_W, PHOTO_H), "_photo_photo")
            except Exception:
                pass
        self.lbl_plate.config(text=res.plate or "—", fg=(FG if res.plate else MUTED))
        if res.plate:
            if not available:
                self.lbl_wh.config(text=mark or "справочник камеры недоступен", fg=MUTED)
            elif allowed:
                self.lbl_wh.config(text=f"✓ {mark}", fg=GREEN)
            else:
                self.lbl_wh.config(text=f"✗ {mark}", fg=RED)
        ts = datetime.fromtimestamp(res.ts).strftime("%H:%M:%S")
        parts = [f"{ts} · {res.camera_name}"]
        if res.code:
            parts.append(res.code)
        if res.action:
            parts.append(res.action)
        if res.group_id:
            parts.append(f"группа {res.group_id}")
        if action == "photo" and res.ts > 0:
            # Диагностика: насколько снимок отстал от текста события.
            parts.append(f"снимок +{max(0.0, time.time() - res.ts):.1f}с")
        self.lbl_meta.config(text="  ·  ".join(parts))

    def _on_open(self) -> None:
        self.app.open_gate(self.index)

    def set_busy(self, busy: bool) -> None:
        self.btn_open.config(state="disabled" if busy else "normal",
                             text="ОТКРЫВАЮ…" if busy else "ОТКРЫТЬ ВОРОТА")

    def _set_image(self, label: tk.Label, img: Image.Image, attr: str) -> None:
        photo = ImageTk.PhotoImage(img)
        setattr(self, attr, photo)  # держим ссылку, иначе GC удалит изображение
        label.config(image=photo, width=photo.width(), height=photo.height())


class SearchEntry(tk.Entry):
    """Строка поиска с подсказкой «поиск…»; колбэк — при каждом изменении текста."""

    def __init__(self, master: tk.Misc, on_change, **kw):
        self._var = tk.StringVar()
        super().__init__(master, textvariable=self._var, bg=ENTRY_BG, fg=MUTED,
                         relief="flat", font=F_LOG, insertbackground=FG, **kw)
        self._hint = SEARCH_HINT
        self._on_change = on_change
        self._var.set(self._hint)
        self._var.trace_add("write", lambda *_: self.after_idle(self._fire))
        self.bind("<FocusIn>", self._focus_in)
        self.bind("<FocusOut>", self._focus_out)

    def query(self) -> str:
        """Текущий запрос (подсказка и пробелы вокруг — это пустой запрос)."""
        text = self._var.get()
        return "" if text == self._hint else text.strip()

    def _fire(self) -> None:
        if self.winfo_exists():
            self._on_change()

    def _focus_in(self, _event=None) -> None:
        if self._var.get() == self._hint:
            self._var.set("")
            self.config(fg=FG)

    def _focus_out(self, _event=None) -> None:
        if not self._var.get().strip():
            self._var.set(self._hint)
            self.config(fg=MUTED)


class SidePanel(tk.Frame):
    """Правая колонка: справочник номеров камер с поиском и кнопкой «Изменить».

    «Изменить» открывает модальное окно добавления/корректировки пропусков
    (запись в allow-список камеры). Ширина — чуть больше максимальной длины
    номера (PLATE_MAX_CHARS + 2 знака)."""

    LIST_CHARS = PLATE_MAX_CHARS + 2  # ширина списка в знаках — «чуть больше» номера

    def __init__(self, master: tk.Misc, app: "App"):
        super().__init__(master, bg=PANEL_BG, padx=10, pady=8)
        self.app = app
        self._plates: Optional[List[str]] = None  # None — БД камер недоступна
        self._reason = ""

        self.lst = tk.Listbox(self, bg=ENTRY_BG, fg=FG, selectbackground=ACCENT,
                              relief="flat", font=("Consolas", 11), height=8,
                              width=self.LIST_CHARS)
        # Заголовок переносится точно по ширине списка; шрифт мельче, чтобы
        # длинные слова («Справочник») не рвались посреди слова в узкой колонке.
        w = max(self.lst.winfo_reqwidth(), 80)
        self.lbl_title = tk.Label(self, text="Справочник камер", bg=PANEL_BG, fg=FG,
                                  font=("Segoe UI", 10, "bold"), justify="left",
                                  anchor="w", wraplength=w)
        self.lbl_title.pack(anchor="w")
        # Кнопка управления пропусками — сразу под надписью «Справочник камер».
        self.btn_edit = tk.Button(self, text="Изменить", command=self._on_edit,
                                  bg=ACCENT, fg="white", activebackground="#1f6fd0",
                                  activeforeground="white", relief="flat",
                                  font=("Segoe UI", 9, "bold"), padx=8, pady=2,
                                  cursor="hand2")
        self.btn_edit.pack(anchor="w", pady=(4, 0))
        self.lbl_count = tk.Label(self, text="синхронизация…", bg=PANEL_BG, fg=MUTED,
                                  font=F_SMALL, justify="left", anchor="w", wraplength=w)
        self.lbl_count.pack(anchor="w")
        # Поиск по номеру: вхождение в текст, фильтрация со 2-го символа.
        self.search = SearchEntry(self, self._apply_filter, width=self.LIST_CHARS)
        self.search.pack(anchor="w", pady=(6, 0))
        self.lst.pack(fill="both", expand=True, pady=(4, 0))

    def update_camera_list(self, plates: Optional[List[str]], reason: str = "") -> None:
        """Показать allow-номера БД камер. None — БД недоступна (reason — почему)."""
        self._plates = list(plates) if plates is not None else None
        self._reason = reason
        self._apply_filter()

    def _on_edit(self) -> None:
        """Открыть модальное окно добавления/корректировки пропусков."""
        self.app.open_pass_editor()

    def _apply_filter(self) -> None:
        """Перерисовать список: все номера или только вхождения поискового запроса."""
        self.lst.delete(0, "end")
        if self._plates is None:
            text = "недоступен"
            if self._reason:
                text += f": {self._reason}"
            self.lbl_count.config(text=text, fg=RED)
            return
        shown = search_filter(self.search.query(), self._plates)
        for p in shown:
            self.lst.insert("end", p)
        if len(shown) == len(self._plates):
            self.lbl_count.config(text=f"{len(self._plates)} шт.", fg=MUTED)
        else:
            self.lbl_count.config(text=f"найдено {len(shown)} из {len(self._plates)}", fg=FG)


class LogPanel(tk.Frame):
    """Нижняя строка раскладки: журнал событий под панелями камер (видео + фото).

    История распознавания и все события — в этом журнале; над ним поиск:
    вхождение в текст строки, фильтрация со 2-го символа.
    """

    def __init__(self, master: tk.Misc, app: "App"):
        super().__init__(master, bg=PANEL_BG, padx=10, pady=8)
        self.app = app
        self._lines: List[str] = []  # буфер строк (для фильтрации поиском)
        tk.Label(self, text="Журнал", bg=PANEL_BG, fg=FG, font=F_TITLE).pack(anchor="w")
        self.search = SearchEntry(self, self._apply_filter, width=28)
        self.search.pack(anchor="w", pady=(6, 0))
        self.txt = tk.Text(self, bg=ENTRY_BG, fg="#c8c8c8", relief="flat",
                           font=F_LOG, height=10, wrap="word", state="disabled")
        self.txt.pack(fill="both", expand=True, pady=(4, 0))

    def log(self, text: str) -> None:
        """Добавить строку в журнал (буфер ограничен последними 400 строк)."""
        line = datetime.now().strftime("%H:%M:%S") + "  " + text
        self._lines.append(line)
        if len(self._lines) > 400:
            del self._lines[:200]
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Показать журнал целиком или только строки с вхождением запроса."""
        shown = search_filter(self.search.query(), self._lines)
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        if shown:
            self.txt.insert("end", "\n".join(shown) + "\n")
        self.txt.see("end")
        self.txt.config(state="disabled")


class PassDialog(tk.Toplevel):
    """Модальное окно «Пропуска»: добавление и корректировка записей allow-списка.

    Слева — список пропусков (номер + владелец) с поиском, справа — форма
    «Номер»/«Владелец» (владелец по умолчанию «Camera control»). Запись в
    справочник камеры идёт через recordUpdater.cgi в фоне (GUI не блокируется);
    после успеха список перечитывается, справочник справа обновляется on_changed.
    """

    POLL_MS = 100

    def __init__(self, master: tk.Misc, cameras: List[DahuaCamera], on_changed=None,
                 log=None, default_index: int = 0):
        super().__init__(master)
        self.title("Пропуска — справочник камер")
        self.configure(bg=BG)
        self.transient(master)
        self.resizable(False, False)
        self.cameras = list(cameras)
        self.on_changed = on_changed
        self.log = log
        self._q: "queue.Queue" = queue.Queue()
        self._busy = False
        self._records: List[Dict[str, str]] = []  # все записи выбранной камеры
        self._shown: List[Dict[str, str]] = []    # после фильтра поиска
        self._selected: Optional[Dict[str, str]] = None
        self._build()
        self._reset_form()  # новый пропуск: владелец и период — по умолчанию
        self.cmb_cam.current(max(0, min(int(default_index or 0), len(self.cameras) - 1)))
        self._center(master)  # окно — по геометрическому центру главного окна
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()
        self._load()
        self.after(self.POLL_MS, self._poll)

    def _build(self) -> None:
        top = tk.Frame(self, bg=PANEL_BG)
        top.pack(fill="x", padx=12, pady=(10, 8))
        tk.Label(top, text="Камера:", bg=PANEL_BG, fg=FG, font=F_SMALL).pack(side="left")
        self.cmb_cam = ttk.Combobox(top, state="readonly", width=30,
                                    values=[c.name for c in self.cameras])
        self.cmb_cam.pack(side="left", padx=(6, 0))
        self.cmb_cam.bind("<<ComboboxSelected>>", lambda _e: self._load())

        body = tk.Frame(self, bg=PANEL_BG)
        body.pack(fill="both", expand=True, padx=12)

        left = tk.Frame(body, bg=PANEL_BG)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="Пропуска", bg=PANEL_BG, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.search = SearchEntry(left, self._apply_filter, width=32)
        self.search.pack(anchor="w", pady=(4, 0))
        self.lst = tk.Listbox(left, bg=ENTRY_BG, fg=FG, selectbackground=ACCENT,
                              relief="flat", font=("Consolas", 10), height=16,
                              width=40, exportselection=False)
        self.lst.pack(fill="both", expand=True, pady=(4, 0))
        self.lst.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(body, bg=PANEL_BG)
        right.pack(side="left", fill="y", padx=(14, 0))
        tk.Label(right, text="Номер", bg=PANEL_BG, fg=FG, font=F_SMALL).pack(anchor="w")
        self.ent_plate = tk.Entry(right, bg=ENTRY_BG, fg=FG, relief="flat",
                                  font=("Consolas", 13), width=20)
        self.ent_plate.pack(anchor="w", pady=(2, 8))
        tk.Label(right, text="Владелец", bg=PANEL_BG, fg=FG, font=F_SMALL).pack(anchor="w")
        self.ent_owner = tk.Entry(right, bg=ENTRY_BG, fg=FG, relief="flat",
                                  font=("Segoe UI", 11), width=20)
        self.ent_owner.pack(anchor="w", pady=(2, 8))
        self.ent_owner.insert(0, PASS_OWNER_DEFAULT)
        tk.Label(right, text="Действует с", bg=PANEL_BG, fg=FG,
                 font=F_SMALL).pack(anchor="w")
        self.ent_begin = tk.Entry(right, bg=ENTRY_BG, fg=FG, relief="flat",
                                  font=("Consolas", 11), width=20)
        self.ent_begin.pack(anchor="w", pady=(2, 8))
        tk.Label(right, text="Действует по", bg=PANEL_BG, fg=FG,
                 font=F_SMALL).pack(anchor="w")
        self.ent_cancel = tk.Entry(right, bg=ENTRY_BG, fg=FG, relief="flat",
                                   font=("Consolas", 11), width=20)
        self.ent_cancel.pack(anchor="w", pady=(2, 2))
        tk.Label(right, text="формат: ГГГГ-ММ-ДД ЧЧ:ММ[:СС]", bg=PANEL_BG, fg=MUTED,
                 font=F_SMALL).pack(anchor="w", pady=(0, 10))

        self.btn_add = self._btn(right, "Добавить", self._on_add, True)
        self.btn_save = self._btn(right, "Сохранить", self._on_save, True)
        self.btn_clear = self._btn(right, "Очистить", self._reset_form)
        self.btn_close = self._btn(right, "Закрыть", self._close)
        for b in (self.btn_add, self.btn_save, self.btn_clear, self.btn_close):
            b.pack(fill="x", pady=2)

        self.lbl_status = tk.Label(self, text="", bg=PANEL_BG, fg=MUTED,
                                   font=F_SMALL, anchor="w", justify="left")
        self.lbl_status.pack(fill="x", padx=12, pady=(8, 10))

    def _btn(self, master: tk.Misc, text: str, cmd, primary: bool = False) -> tk.Button:
        return tk.Button(master, text=text, command=cmd,
                         bg=(ACCENT if primary else "#2a2a2a"), fg="white",
                         activebackground=("#1f6fd0" if primary else "#3a3a3a"),
                         activeforeground="white", relief="flat",
                         font=("Segoe UI", 10, "bold"), padx=8, pady=4, cursor="hand2")

    def _center(self, master: tk.Misc) -> None:
        """Окно — по геометрическому центру главного окна (с учётом границ экрана)."""
        self.update_idletasks()
        w = max(self.winfo_reqwidth(), self.winfo_width())
        h = max(self.winfo_reqheight(), self.winfo_height())
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 2
        x = max(0, min(x, self.winfo_screenwidth() - w))
        y = max(0, min(y, self.winfo_screenheight() - h))
        self.geometry(f"+{x}+{y}")

    def _cam(self) -> DahuaCamera:
        return self.cameras[self.cmb_cam.current()]

    def _status(self, text: str, color: str = MUTED) -> None:
        self.lbl_status.config(text=text, fg=color)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for b in (self.btn_add, self.btn_save, self.btn_clear):
            b.config(state="disabled" if busy else "normal")
        self.cmb_cam.config(state="disabled" if busy else "readonly")

    def _display(self, rec: Dict[str, str]) -> str:
        return f"{rec.get('PlateNumber', ''):<16} {rec.get('MasterOfCar', '')}".rstrip()

    def _load(self) -> None:
        """Перечитать список пропусков выбранной камеры (в фоне)."""
        if not self.cameras or self._busy:
            return
        idx = self.cmb_cam.current()
        cam = self.cameras[idx]
        self._set_busy(True)
        self._status(f"загрузка пропусков ({cam.name})…")

        def work() -> None:
            recs = cam.list_passes()
            self._q.put(("loaded", idx, cam.name, recs, cam.last_error))

        threading.Thread(target=work, daemon=True).start()

    def _run_op(self, what: str, cam: DahuaCamera, fn) -> None:
        """Запуск операции записи в фоне: GUI не блокируется (очередь + after)."""
        if self._busy:
            return
        self._set_busy(True)
        self._status(f"{what}…")

        def work() -> None:
            try:
                res = fn()
            except Exception as e:  # сеть и т.п. не должны ронять GUI
                res = (False, str(e))
            ok, msg = bool(res[0]), str(res[1])
            snap = None
            if ok:
                # сразу перечитываем БД камеры — правая панель обновится без ожидания
                snap = cam.export_camera_plates()
                if snap is None:
                    snap = {"ok": False, "reason": cam.last_error or "БД недоступна"}
            self._q.put(("op", ok, msg, cam.name, snap))

        threading.Thread(target=work, daemon=True).start()

    def _form(self) -> Tuple[bool, str, str, str, str, str]:
        """Значения формы: (ok, номер, владелец, начало действия, окончание, ошибка)."""
        ok, plate, owner, err = validate_pass_input(self.ent_plate.get(),
                                                    self.ent_owner.get())
        if not ok:
            return False, "", "", "", "", err
        ok, begin, cancel, err = validate_pass_period(self.ent_begin.get(),
                                                      self.ent_cancel.get())
        if not ok:
            return False, "", "", "", "", err
        return True, plate, owner, begin, cancel, ""

    def _on_add(self) -> None:
        """Добавить новый пропуск (номер, владелец, период действия)."""
        ok, plate, owner, begin, cancel, err = self._form()
        if not ok:
            self._status(f"✗ {err}", RED)
            return
        cam = self._cam()
        self._run_op(f"добавление {plate}", cam,
                     lambda: cam.add_pass(plate, owner, begin, cancel))

    def _on_save(self) -> None:
        """Скорректировать выбранный пропуск (номер, владелец, период действия)."""
        rec = self._selected
        if not rec or not str(rec.get("RecNo") or "").strip():
            self._status("✗ выберите пропуск в списке для изменения", RED)
            return
        ok, plate, owner, begin, cancel, err = self._form()
        if not ok:
            self._status(f"✗ {err}", RED)
            return
        cam = self._cam()
        rec_no = int(rec.get("RecNo") or 0)
        self._run_op(f"изменение {plate}", cam,
                     lambda: cam.update_pass(rec_no, plate, owner, begin, cancel))

    def _reset_form(self) -> None:
        """Очистить форму: новый пропуск, владелец и период — значения по умолчанию."""
        self._selected = None
        self.lst.selection_clear(0, "end")
        self.ent_plate.delete(0, "end")
        self.ent_owner.delete(0, "end")
        self.ent_owner.insert(0, PASS_OWNER_DEFAULT)
        # период действия нового пропуска: с текущего момента до конца суток
        now = datetime.now()
        self.ent_begin.delete(0, "end")
        self.ent_begin.insert(0, now.strftime("%Y-%m-%d %H:%M:%S"))
        self.ent_cancel.delete(0, "end")
        self.ent_cancel.insert(0, now.strftime("%Y-%m-%d") + " 23:59:59")
        self.ent_plate.focus_set()

    def _on_select(self, _event=None) -> None:
        """Выбор пропуска в списке — заполнить форму его номером, владельцем и периодом."""
        sel = self.lst.curselection()
        if not sel or sel[0] >= len(self._shown):
            return
        rec = self._shown[sel[0]]
        self._selected = rec
        self.ent_plate.delete(0, "end")
        self.ent_plate.insert(0, rec.get("PlateNumber", ""))
        self.ent_owner.delete(0, "end")
        self.ent_owner.insert(0, rec.get("MasterOfCar", ""))
        self.ent_begin.delete(0, "end")
        self.ent_begin.insert(0, rec.get("BeginTime", ""))
        self.ent_cancel.delete(0, "end")
        self.ent_cancel.insert(0, rec.get("CancelTime", ""))
        self._status(f"выбран пропуск RecNo={rec.get('RecNo', '?')}", MUTED)

    def _apply_filter(self) -> None:
        """Список целиком или только вхождения поискового запроса (со 2-го символа)."""
        items = [self._display(r) for r in self._records]
        counts: Dict[str, int] = {}
        for s in search_filter(self.search.query(), items):
            counts[s] = counts.get(s, 0) + 1
        self._shown = []
        for r, s in zip(self._records, items):
            if counts.get(s, 0) > 0:
                counts[s] -= 1
                self._shown.append(r)
        self.lst.delete(0, "end")
        for r in self._shown:
            self.lst.insert("end", self._display(r))
        if self.search.query() and len(self._shown) != len(self._records):
            self._status(f"найдено {len(self._shown)} из {len(self._records)}", MUTED)

    def _poll(self) -> None:
        try:
            while True:
                try:
                    ev = self._q.get_nowait()
                except queue.Empty:
                    break
                self._handle(ev)
        finally:
            if self.winfo_exists():
                self.after(self.POLL_MS, self._poll)

    def _handle(self, ev: tuple) -> None:
        if ev[0] == "loaded":
            _tag, idx, _cam_name, recs, err = ev
            self._set_busy(False)
            if idx != self.cmb_cam.current():
                return  # пользователь уже переключил камеру
            if recs is None:
                self._records, self._shown = [], []
                self._apply_filter()
                self._status(f"✗ справочник недоступен: {err or 'ошибка запроса'}", RED)
                return
            self._records = sorted(
                recs, key=lambda r: normalize_plate(r.get("PlateNumber", "")))
            self._apply_filter()
            self._status(f"записей: {len(self._records)}", MUTED)
        elif ev[0] == "op":
            _tag, ok, msg, cam_name, snap = ev
            self._set_busy(False)
            self._status(f"{'✓' if ok else '✗'} {msg}", GREEN if ok else RED)
            if self.log:
                self.log(f"[{cam_name}] пропуска: {'✓' if ok else '✗'} {msg}")
            if snap is not None and self.on_changed is not None:
                self.on_changed(cam_name, snap)
            if ok:
                self._reset_form()
                self._load()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class App:
    """Главное окно: сборка панелей, опрос очередей воркеров, действия пользователя."""

    POLL_MS = 40  # период опроса очередей (≈25 FPS максимум)

    def __init__(self, cfg: Dict[str, Any], log_file: str):
        self.cfg = cfg
        self.log_file = log_file
        self.cameras: List[Any] = []       # DahuaCamera
        self.workers: List[Any] = []       # (VideoWorker, AnprWorker, frame_q, anpr_q, status_q, wl_q)
        self.gate_thread: Optional[Any] = None
        self._closing = False
        # Снимки БД номеров камер (ТОЛЬКО ЧТЕНИЕ): name -> {'allow': [...], 'block': [...]}.
        # Локального справочника нет: решение принимает камера или оператор.
        self.camera_plates: Dict[str, Dict[str, Any]] = {}

        self.root = tk.Tk()
        self.root.title("DahuaCamControl — управление воротами")
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.frame: Optional[tk.Frame] = None
        self.panels: List[CameraPanel] = []
        self.side: Optional[SidePanel] = None
        self.log_panel: Optional[LogPanel] = None
        self._ui_q: "queue.Queue" = queue.Queue()  # UI-события из фоновых потоков

        cams = [c for c in cfg["cameras"] if is_camera_configured(c)]
        if not cams:
            tk.Label(self.root, text="Нет настроенных камер.\nЗаполните config.json "
                                     "(ip, username, password камеры 1).",
                     bg=BG, fg=FG, font=F_TITLE, justify="center").pack(expand=True)
            self._fit_window(None)
            return

        # размеры видео/фото берутся из config (video.width/video.height,
        # photo_scale); если окно не влезает в экран, _fit_window уменьшит их
        # и пересоберёт панели
        video = cfg.get("video", {})
        vw = int(video.get("width", VIDEO_W))
        vh = int(video.get("height", VIDEO_H))
        globals()["PHOTO_SCALE"] = normalize_photo_scale(video.get("photo_scale"))
        if vw > 0 and vh > 0:
            globals()["VIDEO_W"], globals()["VIDEO_H"] = vw, vh
            globals()["PHOTO_W"], globals()["PHOTO_H"] = photo_size(vw, vh, PHOTO_SCALE)
        self._build_layout(cams)
        self._fit_window(cams)

        self.log_panel.log(f"Настроено камер: {len(cams)}")
        self._start_workers(cams)
        self.root.after(self.POLL_MS, self._poll)

    def _build_layout(self, cams: List[Dict[str, Any]]) -> None:
        """Собрать панели камер, журнал под ними и справочник справа (повторно — при подгонке под экран).

        Сетка:  row 0: [камера 0 | камера 1 | … | справочник (rowspan=2)]
                row 1: [журнал (columnspan=N)                         ]
        """
        if self.frame is not None:
            self.frame.destroy()
        self.frame = tk.Frame(self.root, bg=BG)
        self.frame.pack(fill="both", expand=True)
        self.panels = []
        n = len(cams)
        for i, cam_cfg in enumerate(cams):
            panel = CameraPanel(self.frame, self, cam_cfg, i)
            panel.grid(row=0, column=i, sticky="nsew", padx=6, pady=6)
            self.panels.append(panel)
        # Журнал — под панелями камер (видео + фото), на всю их ширину.
        self.log_panel = LogPanel(self.frame, self)
        self.log_panel.grid(row=1, column=0, columnspan=n, sticky="nsew", padx=6, pady=6)
        # Справочник — справа от видеопотоков, на всю высоту окна.
        self.side = SidePanel(self.frame, self)
        self.side.grid(row=0, column=n, rowspan=2, sticky="nsew", padx=6, pady=6)
        for i in range(n):
            self.frame.columnconfigure(i, weight=1)
        self.frame.columnconfigure(n, weight=0)  # справочник — фиксированной ширины
        self.frame.rowconfigure(0, weight=1)     # камеры — основная высота
        self.frame.rowconfigure(1, weight=0)     # журнал — по своему содержимому
        # Замораживаем размер панелей (width/height фрейма + pack_propagate(False)):
        # длина текстов меток меняется (FPS раз в 2 с, номера, ошибки), а без
        # заморозки меняется «запрошенный размер» — подрагивают и окно, и раскладка.
        self.frame.update_idletasks()
        for p in [*self.panels, self.log_panel, self.side]:
            p.pack_propagate(False)
            p.config(width=p.winfo_reqwidth(), height=p.winfo_reqheight())

    def _fit_window(self, cams: Optional[List[Dict[str, Any]]]) -> None:
        """Вписать окно в экран, отцентрировать и зафиксировать его размер.

        ВАЖНО: без явного geometry() Tk держит размер окна «по содержимому» —
        любое изменение длины текстов (FPS каждые 2 с, номера, ошибки) меняет
        запрошенный размер, и окно само «прыгает». Явно заданный размер это
        отключает. Если содержимое шире экрана — пропорционально уменьшаем
        видео/фото и пересобираем панели.
        """
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        # рабочая область: запас под рамки окна и панель задач
        max_w = max(640, screen_w - 80)
        max_h = max(480, screen_h - 120)
        for _ in range(5):
            self.root.update_idletasks()
            req_w = self.root.winfo_reqwidth()
            req_h = self.root.winfo_reqheight()
            if not cams or (req_w <= max_w and req_h <= max_h):
                break
            scale = min(max_w / max(req_w, 1), max_h / max(req_h, 1), 1.0)
            if scale >= 0.999:
                break
            vw = int(VIDEO_W * scale)
            vh = int(VIDEO_H * scale)
            if vw < 240 or vh < 135 or (vw, vh) == (VIDEO_W, VIDEO_H):
                break
            globals()["VIDEO_W"], globals()["VIDEO_H"] = vw, vh
            globals()["PHOTO_W"], globals()["PHOTO_H"] = photo_size(vw, vh, PHOTO_SCALE)
            self._build_layout(cams)
        self.root.update_idletasks()
        w = min(self.root.winfo_reqwidth(), max_w)
        h = min(self.root.winfo_reqheight(), max_h)
        x = max(0, (screen_w - w) // 2)
        y = max(0, (screen_h - h) // 2)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(min(w, 800), min(h, 500))

    def run(self) -> None:
        self.root.mainloop()

    # --------------------------- запуск/опрос ---------------------------

    def _thread_log(self, text: str) -> None:
        """Потокобезопасный лог: сообщения воркеров идут через очередь UI-событий."""
        self._ui_q.put(("log", text))

    def _start_workers(self, cams: List[Dict[str, Any]]) -> None:
        vcfg = self.cfg.get("video", {})
        subtype = int(vcfg.get("subtype", 1))
        # video.mode: 'snapshot' (по умолчанию) — опрос снимков snapshot.cgi
        # (полное разрешение, поток не замерзает); 'stream' — MJPEG-поток.
        # video.set_resolution меняет кодирование потока НА КАМЕРЕ
        # (Encode[…].Video.VideoResolution); width/height — лишь размер окна в GUI.
        mode = str(vcfg.get("mode", "snapshot") or "snapshot").strip().lower()
        interval = float(vcfg.get("interval", 0.4) or 0.4)
        set_res = str(vcfg.get("set_resolution", "") or "")
        # Синхронизация БД номеров камеры (whitelist/blacklist камеры):
        # camera_whitelist.enable=False — выключить; sync_interval=0 — один раз при запуске.
        wcfg = self.cfg.get("camera_whitelist", {}) or {}
        wl_enabled = bool(wcfg.get("enable", True))
        wl_interval = float(wcfg.get("sync_interval", 300) or 0)
        for i, cam_cfg in enumerate(cams):
            cam = DahuaCamera(cam_cfg, name=str(cam_cfg.get("name") or f"Камера {i + 1}"))
            self.cameras.append(cam)
            frame_q: "queue.Queue" = queue.Queue(maxsize=2)
            anpr_q: "queue.Queue" = queue.Queue(maxsize=10)
            status_q: "queue.Queue" = queue.Queue(maxsize=20)
            vw = VideoWorker(cam, frame_q, status_q, subtype=subtype,
                             set_resolution=set_res, mode=mode, interval=interval,
                             monitor=self._thread_log)
            aw = AnprWorker(cam, anpr_q, status_q, self.cfg.get("anpr_events", ["TrafficJunction"]),
                            heartbeat=int(self.cfg.get("heartbeat", 5)),
                            read_timeout=float(self.cfg.get("read_timeout", 30)),
                            monitor=self._thread_log)
            vw.start()
            aw.start()
            wl_q = None
            if wl_enabled:
                wl_q = queue.Queue(maxsize=2)
                ww = WhitelistSyncWorker(cam, wl_q, status_q, interval=wl_interval,
                                         monitor=self._thread_log)
                ww.start()
            self.workers.append((vw, aw, frame_q, anpr_q, status_q, wl_q))

    def _poll(self) -> None:
        """Единственная точка обновления tkinter: разбор очередей воркеров."""
        try:
            # UI-события из фоновых потоков (логи, результаты открытия ворот)
            while True:
                try:
                    ev = self._ui_q.get_nowait()
                except queue.Empty:
                    break
                kind = ev[0]
                if kind == "log":
                    self.log_panel.log(ev[1])
                elif kind == "gate_done":
                    self._gate_done(ev[1], ev[2], ev[3])
                elif kind == "camera_plates":
                    # свежий снимок БД после записи пропуска (редактор пропусков)
                    self.camera_plates[ev[1]] = ev[2]
                    self._refresh_side_list()
            for i, (_vw, _aw, frame_q, anpr_q, status_q, wl_q) in enumerate(self.workers):
                panel = self.panels[i]
                panel.tick_status()  # индикация «поток пропал», если кадры не идут
                for _ in range(4):  # не более 4 кадров за опрос — GUI не «залипает»
                    try:
                        panel.show_frame(frame_q.get_nowait())
                    except queue.Empty:
                        break
                while True:
                    try:
                        res = anpr_q.get_nowait()
                    except queue.Empty:
                        break
                    allowed, mark, available = self._plate_verdict(res.plate, panel.name)
                    panel.show_anpr(res, allowed, mark, available)
                    self._log_anpr(res, allowed, mark, available)
                if wl_q is not None:
                    while True:
                        try:
                            _tag, cam_name, snap = wl_q.get_nowait()
                        except queue.Empty:
                            break
                        self.camera_plates[cam_name] = snap
                        self._refresh_side_list()
                while True:
                    try:
                        _tag, source, kind, text = status_q.get_nowait()
                    except queue.Empty:
                        break
                    for p in self.panels:
                        if p.name == source:
                            p.set_status(f"{kind}: {text}")
        finally:
            if not self._closing:
                self.root.after(self.POLL_MS, self._poll)

    def _log_anpr(self, res: ANPRResult, allowed: bool, mark: str = "",
                  available: bool = True) -> None:
        ts = datetime.fromtimestamp(res.ts).strftime("%Y-%m-%d %H:%M:%S")
        if res.plate:
            verdict = "разрешено" if allowed else "не разрешено"
            self.log_panel.log(f"[{res.camera_name}] номер {res.plate} — {verdict}: {mark}")
            log_jsonl(self.log_file, {"ts": ts, "type": "anpr",
                                      "kind": "photo" if res.image else "text",
                                      "camera": res.camera_name,
                                      "plate": res.plate, "allowed": allowed,
                                      "mark": mark, "db_available": available,
                                      "code": res.code, "fields": res.fields})
        else:
            log_jsonl(self.log_file, {"ts": ts, "type": "anpr_image", "camera": res.camera_name})

    def _plate_verdict(self, plate: str, cam_name: str) -> Tuple[bool, str, bool]:
        """Итог сверки номера (разрешён, метка, БД камеры доступна) — ТОЛЬКО ЧТЕНИЕ БД камеры."""
        snap = self.camera_plates.get(cam_name)
        cam_allow = cam_block = False
        cam_available = False
        reason = ""
        if isinstance(snap, dict):
            if snap.get("ok", True):
                cam_available = True
                pn = normalize_plate(plate)
                cam_allow = pn in (snap.get("allow") or [])
                cam_block = pn in (snap.get("block") or [])
            else:
                reason = str(snap.get("reason") or "")
        allowed, mark = plate_decision(cam_allow, cam_block, cam_available=cam_available)
        if not cam_available and reason:
            mark = f"{mark} — {reason}"
        return allowed, mark, cam_available

    def _refresh_side_list(self) -> None:
        """В правой панели — allow-номера первой доступной БД камеры (только чтение)."""
        snap = next((s for s in self.camera_plates.values()
                     if isinstance(s, dict) and s.get("ok", True)), None)
        if snap is not None:
            self.side.update_camera_list(sorted(snap.get("allow") or []))
            return
        reason = next((str(s.get("reason") or "") for s in self.camera_plates.values()
                       if isinstance(s, dict) and s.get("reason")), "")
        self.side.update_camera_list(None, reason)

    # --------------------------- действия ---------------------------

    def open_pass_editor(self) -> None:
        """Модальное окно добавления/корректировки пропусков (запись в БД камеры)."""
        if not self.cameras:
            return
        # по умолчанию — камера, чей справочник сейчас показан справа
        default = 0
        for i, cam in enumerate(self.cameras):
            snap = self.camera_plates.get(cam.name)
            if isinstance(snap, dict) and snap.get("ok", True):
                default = i
                break

        def on_changed(cam_name: str, snap: Dict[str, Any]) -> None:
            # обновление правой панели — через очередь UI-событий главного окна
            self._ui_q.put(("camera_plates", cam_name, snap))

        PassDialog(self.root, self.cameras, on_changed=on_changed,
                   log=self.log_panel.log, default_index=default)

    def open_gate(self, index: int) -> None:
        """Открыть ворота выбранной камеры в отдельном потоке (GUI не блокируется)."""
        if self.gate_thread and self.gate_thread.is_alive():
            return
        if index >= len(self.cameras):
            return
        cam = self.cameras[index]
        panel = self.panels[index]
        plate = panel._last.plate if panel._last else ""
        panel.set_busy(True)
        self.log_panel.log(f"Отправка команды открытия ворот ({cam.name})…")

        def work() -> None:
            ok, msg = cam.open_gate(plate_number=plate)
            self._ui_q.put(("gate_done", index, ok, msg))

        self.gate_thread = threading.Thread(target=work, daemon=True)
        self.gate_thread.start()

    def _gate_done(self, index: int, ok: bool, msg: str) -> None:
        cam = self.cameras[index]
        panel = self.panels[index]
        panel.set_busy(False)
        self.log_panel.log(f"{'✓ Ворота открыты' if ok else '✗ Ошибка открытия'} ({cam.name}): {msg}")
        log_jsonl(self.log_file, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "gate_open", "camera": cam.name, "success": ok, "detail": msg,
            "plate": panel._last.plate if panel._last else ""})

    def _on_close(self) -> None:
        self._closing = True
        try:
            dahua_client_mod.stop_workers()
        except Exception:
            pass
        self.root.destroy()
