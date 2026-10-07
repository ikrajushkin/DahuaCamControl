"""Клиент HTTP API камеры Dahua и вспомогательные классы.

Реализовано по «DAHUA HTTP API V3.52» (см. DAHUA_HTTP_API V3.52 for BITRU.pdf):
- 4.1.4 Get MJPG Stream: /cgi-bin/mjpg/video.cgi?channel=N&subtype=M — live-видео
  (multipart/x-mixed-replace, части — JPEG-кадры);
- 4.4.2 Get a Snapshot:  /cgi-bin/snapshot.cgi?channel=N&type=0 — JPEG-снимок;
- 4.4.3 Subscribe to snapshot (attachFileProc): /cgi-bin/snapManager.cgi?action=attachFileProc
  — подписка на события (для ANPR — код TrafficJunction, раздел 10.1): multipart-ответ
  содержит text/plain части с Events[...].Field=Value и image/jpeg части со снимком авто;
- 10.4.1 Open Strobe:    /cgi-bin/trafficSnap.cgi?action=openStrobe — открыть ворота.

Аутентификация — HTTP Digest (RFC 7616, раздел 3.4 документации Dahua).
"""
from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Tuple

import requests
from requests.auth import HTTPDigestAuth
from PIL import Image, ImageDraw

from config import normalize_plate


# ------------------------- Разбор multipart-потока -------------------------

class MultipartStream:
    """Итератор частей multipart/x-mixed-replace поверх requests-ответа (stream=True).

    Поддерживает части и с Content-Length (mjpg/video.cgi, snapManager), и без него.
    """

    CHUNK = 65536

    def __init__(self, resp: requests.Response):
        ctype = resp.headers.get("Content-Type", "")
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not m:
            raise ValueError(f"boundary не найден в Content-Type: {ctype!r}")
        bnd = m.group(1).encode("latin-1")
        self.delim = bnd if bnd.startswith(b"--") else b"--" + bnd
        self._it = resp.iter_content(self.CHUNK)
        self._buf = bytearray()
        self._eof = False

    def _fill(self) -> bool:
        if self._eof:
            return False
        try:
            chunk = next(self._it)
        except StopIteration:
            self._eof = True
            return False
        if chunk:
            self._buf += chunk
        return True

    def read_line(self, limit: int = 65536) -> bytes:
        """Прочитать строку вместе с \\r\\n. Пустой bytes — конец потока."""
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                line = bytes(self._buf[: i + 1])
                del self._buf[: i + 1]
                return line
            if len(self._buf) >= limit:
                line = bytes(self._buf[:limit])
                del self._buf[:limit]
                return line
            if not self._fill():
                line = bytes(self._buf)
                self._buf.clear()
                return line

    def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if not self._fill():
                body = bytes(self._buf)
                self._buf.clear()
                return body
        body = bytes(self._buf[:n])
        del self._buf[:n]
        return body


    def read_until_delim(self, cap: int = 30 * 1024 * 1024) -> bytes:
        """Прочитать тело части до следующего разделителя (разделитель остаётся в буфере)."""
        while True:
            i = self._buf.find(self.delim)
            if i >= 0:
                body = bytes(self._buf[:i])
                del self._buf[:i]
                return body
            if len(self._buf) > cap or not self._fill():
                body = bytes(self._buf)
                self._buf.clear()
                return body

    def parts(self) -> Generator[Tuple[Dict[str, str], bytes], None, None]:
        """Генератор (headers, body) всех частей потока до финального разделителя."""
        while True:
            line = self.read_line()
            if not line:
                return
            s = line.rstrip(b"\r\n")
            if not s or not s.startswith(self.delim):
                continue  # пропускаем пустые строки/преамбулу до первого разделителя
            if s[len(self.delim):].strip(b" ") == b"--":
                return  # финальный разделитель --boundary--
            headers: Dict[str, str] = {}
            while True:
                h = self.read_line()
                if h in (b"\r\n", b"\n", b""):
                    break
                k, _, v = h.rstrip(b"\r\n").decode("latin-1", "replace").partition(":")
                headers[k.strip().lower()] = v.strip()
            cl = headers.get("content-length", "")
            if cl.isdigit():
                body = self.read_exact(int(cl))
                if self._buf[:2] == b"\r\n":
                    del self._buf[:2]
                elif self._buf[:1] == b"\n":
                    del self._buf[:1]
            else:
                body = self.read_until_delim()
                if body.endswith(b"\r\n"):
                    body = body[:-2]
                elif body.endswith(b"\n"):
                    body = body[:-1]
            yield headers, body


# ------------------------------ События ANPR ------------------------------

_EV_RE = re.compile(r"^Events\[(\d+)\]\.(.+)$")


def parse_event_text(body: bytes) -> List[Dict[str, str]]:
    """Разобрать text/plain часть потока событий: Events[i].Field=Value -> список словарей."""
    events: Dict[int, Dict[str, str]] = {}
    for raw in body.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        m = _EV_RE.match(key.strip())
        if not m:
            continue
        events.setdefault(int(m.group(1)), {})[m.group(2).strip()] = val.strip()
    return [events[i] for i in sorted(events)]


def extract_plate(ev: Dict[str, str]) -> str:
    """Достать номер автомобиля из полей события (разные варианты ключей у прошивок)."""
    for key in ("TrafficCar.PlateNumber", "PlateNumber", "Vehicle.PlateNumber"):
        if ev.get(key):
            return ev[key].strip()
    return ""


@dataclass
class ANPRResult:
    """Распознанное событие: номер + снимок автомобиля."""

    camera_name: str
    plate: str
    image: Optional[bytes]
    ts: float
    code: str = ""
    action: str = ""
    group_id: str = ""
    fields: Dict[str, str] = field(default_factory=dict)

    @property
    def plate_norm(self) -> str:
        return normalize_plate(self.plate)


# ---------------------- Утилиты изображений и очередей ----------------------

def put_latest(q: "queue.Queue", item: Any) -> None:
    """Положить элемент в очередь, вытесняя самый старый (для live-видео)."""
    if q.full():
        try:
            q.get_nowait()
        except queue.Empty:
            pass
    q.put_nowait(item)


def fit_image(img: Image.Image, width: int, height: int) -> Image.Image:
    """Вписать изображение в прямоугольник width×height с сохранением пропорций (letterbox)."""
    img = img.convert("RGB")
    scale = min(width / img.width, height / img.height)
    nw = max(1, round(img.width * scale))
    nh = max(1, round(img.height * scale))
    img = img.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", (width, height), (10, 10, 10))
    canvas.paste(img, ((width - nw) // 2, (height - nh) // 2))
    return canvas


def make_placeholder(width: int, height: int, text: str = "") -> Image.Image:
    """Заглушка (тёмный прямоугольник с текстом), пока видео/фото не получены."""
    canvas = Image.new("RGB", (width, height), (10, 10, 10))
    if text:
        d = ImageDraw.Draw(canvas)
        d.text((width // 2, height // 2), text, fill=(130, 130, 130), anchor="mm")
    return canvas


def _base(cam: Dict[str, Any]) -> str:
    return f"http://{str(cam['ip']).strip()}"


def _auth(cam: Dict[str, Any]) -> HTTPDigestAuth:
    return HTTPDigestAuth(cam["username"], cam["password"])


# ------------------------------ Клиент камеры ------------------------------

class DahuaCamera:
    """HTTP-клиент камеры Dahua (аутентификация HTTP Digest, раздел 3.4 документации)."""

    def __init__(self, cam: Dict[str, Any], name: str = ""):
        self.cam = cam
        self.name = name or str(cam.get("name") or cam.get("ip") or "camera")
        self.channel = int(cam.get("channel", 1) or 1)
        self.base = _base(cam)
        self.auth = _auth(cam)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "DahuaCamControl/1.0"

    # ------------------------- простые CGI-запросы -------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             timeout: float = 6.0) -> requests.Response:
        return self.session.get(self.base + path, params=params, auth=self.auth, timeout=timeout)

    def get_time(self) -> str:
        """Текущее время камеры: global.cgi?action=getCurrentTime."""
        r = self._get("/cgi-bin/global.cgi", {"action": "getCurrentTime"})
        r.raise_for_status()
        return r.text.strip()

    def open_gate(self, plate_number: str = "", open_type: str = "Normal") -> Tuple[bool, str]:
        """Открыть ворота: trafficSnap.cgi?action=openStrobe (раздел 10.4.1).

        Успех — HTTP 200 и тело 'OK'. Возвращает (успех, сообщение).
        """
        try:
            r = self._get(
                "/cgi-bin/trafficSnap.cgi",
                {
                    "action": "openStrobe",
                    "channel": self.channel,
                    "info.openType": open_type,
                    "info.plateNumber": plate_number,
                },
                timeout=6.0,
            )
            body = r.text.strip()
            if r.status_code == 200 and body.upper() == "OK":
                return True, "OK"
            return False, f"HTTP {r.status_code}: {body[:120]}"
        except requests.RequestException as e:
            return False, str(e)

    def get_snapshot(self) -> Optional[bytes]:
        """JPEG-снимок: snapshot.cgi?channel=N&type=0 (раздел 4.4.2)."""
        try:
            r = self._get("/cgi-bin/snapshot.cgi", {"channel": self.channel, "type": 0}, timeout=6.0)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                return r.content
        except requests.RequestException:
            pass
        return None

    # ----------------------- длинные потоковые запросы -----------------------

    def _stream(self, path: str, params: Dict[str, Any],
                read_timeout: float) -> MultipartStream:
        resp = self.session.get(
            self.base + path, params=params, auth=self.auth,
            stream=True, timeout=(6.0, read_timeout),
        )
        resp.raise_for_status()
        return MultipartStream(resp)

    def video_frames(self, subtype: int = 1) -> Generator[bytes, None, None]:
        """JPEG-кадры live-видео: mjpg/video.cgi (раздел 4.1.4). Бесконечный поток."""
        stream = self._stream(
            "/cgi-bin/mjpg/video.cgi",
            {"channel": self.channel, "subtype": subtype},
            read_timeout=15.0,
        )
        for headers, body in stream.parts():
            if headers.get("content-type", "").startswith("image/") and body[:2] == b"\xff\xd8":
                yield body

    def anpr_stream(self, events: List[str], heartbeat: float = 5.0,
                    read_timeout: float = 30.0
                    ) -> Generator[Tuple[Dict[str, str], Optional[bytes]], None, None]:
        """Подписка на события трафика: snapManager.cgi?action=attachFileProc (4.4.3 + 10.1).

        Генерирует пары (поля_события, jpeg_или_None). Текстовые и бинарные части
        идут вперемешку; снимок относится к последнему текстовому событию.
        """
        params: Dict[str, Any] = {
            "action": "attachFileProc",
            "channel": self.channel,
            "heartbeat": int(heartbeat),
            "Flags[0]": "Event",
            "Events": "[" + ",".join(events) + "]",
        }
        stream = self._stream("/cgi-bin/snapManager.cgi", params, read_timeout=read_timeout)
        pending: Dict[str, str] = {}
        for headers, body in stream.parts():
            ctype = headers.get("content-type", "")
            if ctype.startswith("text/"):
                for ev in parse_event_text(body):
                    pending = ev
                    yield ev, None
            elif ctype.startswith("image/"):
                if body[:2] == b"\xff\xd8":
                    yield dict(pending), body


# ----------------------- Фоновые потоки-воркеры -----------------------

stop_event = threading.Event()


def stop_workers() -> None:
    """Сигнал остановки фоновых воркеров (видео/ANPR)."""
    stop_event.set()


class _ReconnectWorker(threading.Thread):
    """Базовый воркер: бесконечный цикл с переподключением и логом в GUI."""

    def __init__(self, camera: DahuaCamera, monitor=None):
        super().__init__(daemon=True, name=f"worker-{camera.name}")
        self.camera = camera
        self.monitor = monitor
        self.attempt = 0
        self.stop_evt: threading.Event = stop_event

    def _log(self, text: str) -> None:
        if self.monitor:
            try:
                self.monitor(text)
            except Exception:
                pass

    def _reconnect_delay(self) -> float:
        self.attempt += 1
        return min(30.0, 2.0 * self.attempt)

    def _sleep(self, delay: float) -> None:
        t0 = time.time()
        while time.time() - t0 < delay and not self.stop_evt.is_set():
            time.sleep(0.2)

    def run(self) -> None:  # pragma: no cover - переопределяется наследниками
        raise NotImplementedError


class VideoWorker(_ReconnectWorker):
    """Поток: качает MJPEG-видео, кладёт JPEG-кадры в вытесняющую очередь."""

    def __init__(self, camera: DahuaCamera, frame_q: "queue.Queue",
                 status_q: "queue.Queue", subtype: int = 1, monitor=None):
        super().__init__(camera, monitor)
        self.frame_q = frame_q
        self.status_q = status_q
        self.subtype = subtype

    def run(self) -> None:
        while not self.stop_evt.is_set():
            try:
                self._log(f"[{self.camera.name}] подключение к видеопотоку…")
                for jpeg in self.camera.video_frames(subtype=self.subtype):
                    self.attempt = 0
                    put_latest(self.frame_q, jpeg)
                if self.stop_evt.is_set():
                    break
                self._log(f"[{self.camera.name}] поток закрыт камерой")
            except Exception as e:
                self._log(f"[{self.camera.name}] видео: {e}")
            if self.stop_evt.is_set():
                break
            delay = self._reconnect_delay()
            put_latest(self.status_q, ("status", self.camera.name, "video",
                                       f"переподключение через {delay:.0f}с"))
            self._sleep(delay)


class AnprWorker(_ReconnectWorker):
    """Поток: подписка attachFileProc, сборка событий (текст+фото) -> anpr_q."""

    def __init__(self, camera: DahuaCamera, anpr_q: "queue.Queue", status_q: "queue.Queue",
                 events: List[str], heartbeat: int = 5, read_timeout: float = 30.0,
                 monitor=None):
        super().__init__(camera, monitor)
        self.anpr_q = anpr_q
        self.status_q = status_q
        self.events = events
        self.heartbeat = heartbeat
        self.read_timeout = read_timeout

    def run(self) -> None:
        while not self.stop_evt.is_set():
            pending_event: Optional[Dict[str, str]] = None
            pending_ts = 0.0
            try:
                self._log(f"[{self.camera.name}] подписка на события: {','.join(self.events)}")
                for ev, jpeg in self.camera.anpr_stream(
                        self.events, heartbeat=self.heartbeat,
                        read_timeout=self.read_timeout):
                    self.attempt = 0
                    if jpeg is None:
                        # Текстовая часть: событие с номером (фото может прийти следующей частью).
                        pending_event = ev
                        pending_ts = time.time()
                        plate = extract_plate(ev)
                        if plate:
                            put_latest(self.anpr_q, ANPRResult(
                                self.camera.name, plate, None, pending_ts,
                                code=ev.get("EventBaseInfo.Code", ""),
                                action=ev.get("EventBaseInfo.Action", ""),
                                group_id=ev.get("GroupID", ""), fields=ev))
                    else:
                        # Бинарная часть: снимок относится к последнему текстовому событию.
                        plate = extract_plate(pending_event or {})
                        if plate:
                            put_latest(self.anpr_q, ANPRResult(
                                self.camera.name, plate, jpeg, pending_ts,
                                code=(pending_event or {}).get("EventBaseInfo.Code", ""),
                                action=(pending_event or {}).get("EventBaseInfo.Action", ""),
                                group_id=(pending_event or {}).get("GroupID", ""),
                                fields=pending_event or {}))
                        else:
                            put_latest(self.anpr_q, ANPRResult(
                                self.camera.name, "", jpeg, time.time(),
                                fields=pending_event or {}))
                if self.stop_evt.is_set():
                    break
                self._log(f"[{self.camera.name}] подписка закрыта камерой")
            except Exception as e:
                self._log(f"[{self.camera.name}] события: {e}")
            if self.stop_evt.is_set():
                break
            delay = self._reconnect_delay()
            put_latest(self.status_q, ("status", self.camera.name, "anpr",
                                       f"переподключение через {delay:.0f}с"))
            self._sleep(delay)
