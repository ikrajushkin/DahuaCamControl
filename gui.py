"""Графический интерфейс (tkinter): панели камер с видео, кнопками открытия ворот,
фото последнего автомобиля, крупным номером и меткой наличия в справочнике.

Раскладка: [ Панель камеры 1 | Панель камеры 2 (опц.) | Журнал + справочник ]
"""
from __future__ import annotations

import io
import queue
import threading
import tkinter as tk
from datetime import datetime
from typing import Any, Dict, List, Optional

from PIL import Image, ImageTk

import dahua_client as dahua_client_mod
from config import PlateBook, is_camera_configured, log_jsonl
from dahua_client import (ANPRResult, DahuaCamera, AnprWorker, VideoWorker,
                          fit_image, make_placeholder)

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
PHOTO_W, PHOTO_H = 320, 180  # уменьшенный вдвое относительно видео формат

F_TITLE = ("Segoe UI", 12, "bold")
F_BUTTON = ("Segoe UI", 13, "bold")
F_PLATE = ("Consolas", 26, "bold")
F_SMALL = ("Segoe UI", 9)
F_LOG = ("Consolas", 9)


class CameraPanel(tk.Frame):
    """Панель одной камеры: видео, кнопка открытия ворот, фото авто + номер + метка."""

    def __init__(self, master: tk.Misc, app: "App", cam_cfg: Dict[str, Any], index: int):
        super().__init__(master, bg=PANEL_BG, padx=10, pady=8)
        self.app = app
        self.cam_cfg = cam_cfg
        self.index = index
        self.name = str(cam_cfg.get("name") or f"Камера {index + 1}")
        self._video_photo: Optional[ImageTk.PhotoImage] = None
        self._photo_photo: Optional[ImageTk.PhotoImage] = None
        self._last: Optional[ANPRResult] = None
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
        self.btn_add = tk.Button(
            row, text="+ в справочник", command=self._on_add, state="disabled",
            bg=ENTRY_BG, fg=FG, activebackground="#2a2a2a", activeforeground=FG,
            relief="flat", font=F_SMALL, cursor="hand2")
        self.btn_add.pack(side="right")

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
        """Отобразить JPEG-кадр live-видео."""
        try:
            img = Image.open(io.BytesIO(jpeg))
            self._set_image(self.lbl_video, fit_image(img, VIDEO_W, VIDEO_H), "_video_photo")
        except Exception:
            pass

    def show_anpr(self, res: ANPRResult, allowed: bool) -> None:
        """Отобразить результат распознавания: фото, номер, метку справочника."""
        self._last = res
        if res.image:
            try:
                img = Image.open(io.BytesIO(res.image))
                self._set_image(self.lbl_photo, fit_image(img, PHOTO_W, PHOTO_H), "_photo_photo")
            except Exception:
                pass
        self.lbl_plate.config(text=res.plate or "—", fg=(FG if res.plate else MUTED))
        if res.plate:
            if allowed:
                self.lbl_wh.config(text="✓ ЕСТЬ в справочнике разрешённых", fg=GREEN)
                self.btn_add.config(state="disabled")
            else:
                self.lbl_wh.config(text="✗ НЕТ в справочнике разрешённых", fg=RED)
                self.btn_add.config(state="normal")
        ts = datetime.fromtimestamp(res.ts).strftime("%H:%M:%S")
        parts = [f"{ts} · {res.camera_name}"]
        if res.code:
            parts.append(res.code)
        if res.action:
            parts.append(res.action)
        if res.group_id:
            parts.append(f"группа {res.group_id}")
        self.lbl_meta.config(text="  ·  ".join(parts))

    def set_status(self, text: str) -> None:
        self.lbl_status.config(text=text)

    def set_busy(self, busy: bool) -> None:
        self.btn_open.config(state="disabled" if busy else "normal",
                             text="ОТКРЫВАЮ…" if busy else "ОТКРЫТЬ ВОРОТА")

    def _set_image(self, label: tk.Label, img: Image.Image, attr: str) -> None:
        photo = ImageTk.PhotoImage(img)
        setattr(self, attr, photo)  # держим ссылку, иначе GC удалит изображение
        label.config(image=photo, width=photo.width(), height=photo.height())

    def _on_open(self) -> None:
        self.app.open_gate(self.index)

    def _on_add(self) -> None:
        if self._last and self._last.plate:
            self.app.add_plate(self._last.plate, self.index)


class SidePanel(tk.Frame):
    """Правая колонка: журнал событий и управление справочником номеров."""

    def __init__(self, master: tk.Misc, app: "App", book: PlateBook):
        super().__init__(master, bg=PANEL_BG, padx=10, pady=8)
        self.app = app
        self.book = book

        tk.Label(self, text="Справочник разрешённых номеров", bg=PANEL_BG, fg=FG,
                 font=F_TITLE).pack(anchor="w")
        self.lst = tk.Listbox(self, bg=ENTRY_BG, fg=FG, selectbackground=ACCENT,
                              relief="flat", font=("Consolas", 11), height=8)
        self.lst.pack(fill="both", expand=True, pady=(6, 4))
        row = tk.Frame(self, bg=PANEL_BG)
        row.pack(fill="x")
        self.ent = tk.Entry(row, bg=ENTRY_BG, fg=FG, insertbackground=FG,
                            relief="flat", font=("Consolas", 11))
        self.ent.pack(side="left", fill="x", expand=True, ipady=4)
        self.ent.bind("<Return>", lambda _e: self._add())
        tk.Button(row, text="＋", width=3, command=self._add, bg=ACCENT, fg="white",
                  activebackground="#1f6fd0", activeforeground="white", relief="flat",
                  font=("Segoe UI", 11, "bold"), cursor="hand2").pack(side="left", padx=(4, 0))
        tk.Button(self, text="Удалить выбранный", command=self._del, bg=ENTRY_BG, fg=FG,
                  activebackground="#2a2a2a", activeforeground=FG, relief="flat",
                  font=F_SMALL, cursor="hand2").pack(fill="x", pady=(4, 8))

        tk.Label(self, text="Журнал", bg=PANEL_BG, fg=FG, font=F_TITLE).pack(anchor="w")
        self.txt = tk.Text(self, bg=ENTRY_BG, fg="#c8c8c8", relief="flat",
                           font=F_LOG, height=14, wrap="word", state="disabled")
        self.txt.pack(fill="both", expand=True, pady=(6, 0))

    def refresh_book(self) -> None:
        self.lst.delete(0, "end")
        for p in self.book.items():
            self.lst.insert("end", p)

    def _add(self) -> None:
        plate = self.ent.get().strip()
        if not plate:
            return
        self.ent.delete(0, "end")
        self.app.add_plate(plate, None)

    def _del(self) -> None:
        sel = self.lst.curselection()
        if not sel:
            return
        plate = self.lst.get(sel[0])
        self.app.remove_plate(plate)

    def log(self, text: str) -> None:
        line = datetime.now().strftime("%H:%M:%S") + "  " + text
        self.txt.config(state="normal")
        self.txt.insert("end", line + "\n")
        # ограничиваем журнал последними 400 строками
        if int(self.txt.index("end-1c").split(".")[0]) > 400:
            self.txt.delete("1.0", "200.0")
        self.txt.see("end")
        self.txt.config(state="disabled")


class App:
    """Главное окно: сборка панелей, опрос очередей воркеров, действия пользователя."""

    POLL_MS = 40  # период опроса очередей (≈25 FPS максимум)

    def __init__(self, cfg: Dict[str, Any], book: PlateBook, log_file: str):
        self.cfg = cfg
        self.book = book
        self.log_file = log_file
        self.cameras: List[Any] = []       # DahuaCamera
        self.workers: List[Any] = []       # (VideoWorker, AnprWorker, frame_q, anpr_q, status_q)
        self.gate_thread: Optional[Any] = None
        self._closing = False

        self.root = tk.Tk()
        self.root.title("DahuaCamControl — управление воротами")
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.panels: List[CameraPanel] = []
        self.side: Optional[SidePanel] = None
        self._ui_q: "queue.Queue" = queue.Queue()  # UI-события из фоновых потоков

        cams = [c for c in cfg["cameras"] if is_camera_configured(c)]
        if not cams:
            tk.Label(self.root, text="Нет настроенных камер.\nЗаполните config.json "
                                     "(ip, username, password камеры 1).",
                     bg=BG, fg=FG, font=F_TITLE, justify="center").pack(expand=True)
            return

        self.frame = tk.Frame(self.root, bg=BG)
        self.frame.pack(fill="both", expand=True)
        # размеры видео/фото берутся из config (video.width/video.height)
        video = cfg.get("video", {})
        vw = int(video.get("width", VIDEO_W))
        vh = int(video.get("height", VIDEO_H))
        if vw > 0 and vh > 0:
            globals()["VIDEO_W"], globals()["VIDEO_H"] = vw, vh
            globals()["PHOTO_W"], globals()["PHOTO_H"] = vw // 2, vh // 2
        for i, cam_cfg in enumerate(cams):
            panel = CameraPanel(self.frame, self, cam_cfg, i)
            panel.grid(row=0, column=i, sticky="nsew", padx=6, pady=6)
            self.panels.append(panel)
        self.side = SidePanel(self.frame, self, book)
        self.side.grid(row=0, column=len(cams), sticky="nsew", padx=6, pady=6)
        self.frame.columnconfigure(len(cams), weight=0)
        for i in range(len(cams)):
            self.frame.columnconfigure(i, weight=1)
        self.frame.rowconfigure(0, weight=1)

        self.side.refresh_book()
        self.side.log(f"Настроено камер: {len(cams)}")
        self._start_workers(cams)
        self.root.after(self.POLL_MS, self._poll)

    def run(self) -> None:
        self.root.mainloop()

    # --------------------------- запуск/опрос ---------------------------

    def _thread_log(self, text: str) -> None:
        """Потокобезопасный лог: сообщения воркеров идут через очередь UI-событий."""
        self._ui_q.put(("log", text))

    def _start_workers(self, cams: List[Dict[str, Any]]) -> None:
        vcfg = self.cfg.get("video", {})
        subtype = int(vcfg.get("subtype", 1))
        for i, cam_cfg in enumerate(cams):
            cam = DahuaCamera(cam_cfg, name=str(cam_cfg.get("name") or f"Камера {i + 1}"))
            self.cameras.append(cam)
            frame_q: "queue.Queue" = queue.Queue(maxsize=2)
            anpr_q: "queue.Queue" = queue.Queue(maxsize=10)
            status_q: "queue.Queue" = queue.Queue(maxsize=20)
            vw = VideoWorker(cam, frame_q, status_q, subtype=subtype,
                             monitor=self._thread_log)
            aw = AnprWorker(cam, anpr_q, status_q, self.cfg.get("anpr_events", ["TrafficJunction"]),
                            heartbeat=int(self.cfg.get("heartbeat", 5)),
                            read_timeout=float(self.cfg.get("read_timeout", 30)),
                            monitor=self._thread_log)
            vw.start()
            aw.start()
            self.workers.append((vw, aw, frame_q, anpr_q, status_q))

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
                    self.side.log(ev[1])
                elif kind == "gate_done":
                    self._gate_done(ev[1], ev[2], ev[3])
            for i, (_vw, _aw, frame_q, anpr_q, status_q) in enumerate(self.workers):
                panel = self.panels[i]
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
                    allowed = self.book.contains(res.plate) if res.plate else False
                    panel.show_anpr(res, allowed)
                    self._log_anpr(res, allowed)
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

    def _log_anpr(self, res: ANPRResult, allowed: bool) -> None:
        ts = datetime.fromtimestamp(res.ts).strftime("%Y-%m-%d %H:%M:%S")
        if res.plate:
            mark = "ЕСТЬ в справочнике" if allowed else "НЕТ в справочнике"
            self.side.log(f"[{res.camera_name}] номер {res.plate} — {mark}")
            log_jsonl(self.log_file, {"ts": ts, "type": "anpr", "camera": res.camera_name,
                                      "plate": res.plate, "allowed": allowed,
                                      "code": res.code, "fields": res.fields})
        else:
            log_jsonl(self.log_file, {"ts": ts, "type": "anpr_image", "camera": res.camera_name})

    # --------------------------- действия ---------------------------

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
        self.side.log(f"Отправка команды открытия ворот ({cam.name})…")

        def work() -> None:
            ok, msg = cam.open_gate(plate_number=plate)
            self._ui_q.put(("gate_done", index, ok, msg))

        self.gate_thread = threading.Thread(target=work, daemon=True)
        self.gate_thread.start()

    def _gate_done(self, index: int, ok: bool, msg: str) -> None:
        cam = self.cameras[index]
        panel = self.panels[index]
        panel.set_busy(False)
        self.side.log(f"{'✓ Ворота открыты' if ok else '✗ Ошибка открытия'} ({cam.name}): {msg}")
        log_jsonl(self.log_file, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": "gate_open", "camera": cam.name, "success": ok, "detail": msg,
            "plate": panel._last.plate if panel._last else ""})

    def add_plate(self, plate: str, index: Optional[int]) -> None:
        if self.book.add(plate):
            self.side.refresh_book()
            self.side.log(f"Добавлен номер: {plate}")
            if index is not None and self.panels[index]._last:
                self.panels[index].show_anpr(self.panels[index]._last, True)
        else:
            self.side.log(f"Номер уже есть в справочнике: {plate}")

    def remove_plate(self, plate: str) -> None:
        if self.book.remove(plate):
            self.side.refresh_book()
            self.side.log(f"Удалён номер: {plate}")

    def _on_close(self) -> None:
        self._closing = True
        try:
            dahua_client_mod.stop_workers()
        except Exception:
            pass
        self.root.destroy()
