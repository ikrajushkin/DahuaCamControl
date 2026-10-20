"""Клиент HTTP API камеры Dahua и вспомогательные классы.

Реализовано по «DAHUA HTTP API V3.52» (см. DAHUA_HTTP_API V3.52 for BITRU.pdf):
- 4.1.4 Get MJPG Stream: /cgi-bin/mjpg/video.cgi?channel=N&subtype=M — live-видео
  (multipart/x-mixed-replace, части — JPEG-кадры);
- 4.4.2 Get a Snapshot:  /cgi-bin/snapshot.cgi?channel=N&type=0 — JPEG-снимок;
- 4.4.3 Subscribe to snapshot (attachFileProc): /cgi-bin/snapManager.cgi?action=attachFileProc
  — подписка на события (для ANPR — код TrafficJunction, раздел 10.1): multipart-ответ
  содержит text/plain части с Events[...].Field=Value и image/jpeg части со снимком авто;
- 10.4.1 Open Strobe:    /cgi-bin/trafficSnap.cgi?action=openStrobe — открыть ворота;
- 4.2.1 Get/Set Configure: /cgi-bin/configManager.cgi — разрешение потока камеры
  (Encode[…].MainFormat/SubFormat[…].Video.VideoResolution);
- 10.3.4 Find Traffic BlockList/AllowList Record: /cgi-bin/recordFinder.cgi?action=find
  &name=TrafficRedList|TrafficBlackList — чтение справочника номеров (allow/block);
- 10.7.4–10.7.10 Vehicle Manager: /cgi-bin/api/VehicleRegisterDB/* — альтернативное
  чтение справочника (JSON API; на части прошивок слой /cgi-bin/api/* отвечает
  HTTP 400 независимо от прав — тогда используется recordFinder.cgi).

Аутентификация — HTTP Digest (RFC 7616, раздел 3.4 документации Dahua).
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

import requests
from requests.auth import HTTPDigestAuth
from PIL import Image, ImageDraw, ImageFont

from config import ENABLE_DEBUG, applog, normalize_plate


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


def event_image_sizes(ev: Dict[str, str]) -> Tuple[int, int, int]:
    """Ожидаемые размеры снимка события: (полный блоб, сцена, кроп номера).

    attachFileProc отдаёт снимок блобом [JPEG сцены][JPEG кропа номера] (иногда
    двумя частями). Прошивки считают поля по-разному: у одних SceneImage.Length —
    только сцена, у других — весь блоб; надёжны Object.Image.Offset/Length,
    поэтому полный размер = Object.Image.Offset + Object.Image.Length.
    """

    def _int(key: str) -> int:
        try:
            return int(ev.get(key, "") or 0)
        except ValueError:
            return 0

    scene_len = _int("SceneImage.Length")
    obj_len = _int("Object.Image.Length")
    obj_off = _int("Object.Image.Offset")
    total = (obj_off + obj_len) if (obj_off or obj_len) else scene_len
    scene = obj_off if obj_off else max(0, scene_len - obj_len)
    return total, scene, obj_len


class EventPhotoBinder:
    """Привязка снимков attachFileProc к событиям: по размеру блоба, иначе FIFO.

    Снимок события приходит отдельной частью и часто отстаёт от текста (иногда
    идёт после текстов других событий). Привязка «снимок = последний текст» из-за
    этого показывала фото предыдущего проезда; здесь снимок сверяется с ожидаемыми
    размерами события (см. event_image_sizes). Кроп номера отдельной частью — не
    фото события и отбрасывается.
    """

    def __init__(self) -> None:
        self._pending: List[Tuple[int, Dict[str, str]]] = []
        self._recent_obj: List[int] = []
        self._seq = 0

    def add_event(self, ev: Dict[str, str]) -> int:
        """Зарегистрировать текстовое событие и вернуть его id (общий для текста и фото)."""
        self._seq += 1
        self._pending.append((self._seq, ev))
        return self._seq

    def feed_image(self, body: bytes) -> Optional[Tuple[int, Dict[str, str], bytes]]:
        """Снимок -> (id_события, поля, фото); None — кроп номера (не фото);
        (0, {}, body) — снимок без событий (диагностика в AnprWorker)."""
        n = len(body)
        for i, (seq, ev) in enumerate(self._pending):
            total, scene, obj = event_image_sizes(ev)
            if total and n in (total, scene):
                if obj:
                    self._remember_obj(obj)
                del self._pending[i]
                return seq, ev, body
        if self._is_crop(n):
            return None
        if self._pending:
            seq, ev = self._pending.pop(0)
            _total, _scene, obj = event_image_sizes(ev)
            if obj:
                self._remember_obj(obj)
            return seq, ev, body
        return 0, {}, body

    def _remember_obj(self, obj: int) -> None:
        """Запомнить размер кропа привязанного события (кроп может прийти следом)."""
        self._recent_obj.append(obj)
        del self._recent_obj[:-8]

    def _is_crop(self, n: int) -> bool:
        """Размер кропа номера: у события в очереди или только что привязанного."""
        for _seq, ev in self._pending:
            _total, _scene, obj = event_image_sizes(ev)
            if obj and n == obj:
                return True
        return n in self._recent_obj


# ------------------- БД номеров камеры (VehicleRegisterDB) -------------------
# Whitelist/blacklist камеры живут в VehicleRegisterDB: группы типов AllowListDB
# и BlockListDB (разделы 10.7.1–10.7.10 HTTP API V3.52).

def parse_vehicle_groups(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Разбор ответа findGroup: [{'groupID','groupName','groupType','groupSize'}]."""
    out: List[Dict[str, Any]] = []
    for g in resp.get("GroupList") or []:
        if not isinstance(g, dict):
            continue
        out.append({
            "groupID": str(g.get("groupID") or g.get("GroupID") or "").strip(),
            "groupName": str(g.get("groupName") or g.get("GroupName") or "").strip(),
            "groupType": str(g.get("groupType") or g.get("GroupType") or "").strip(),
            "groupSize": int(g.get("groupSize") or g.get("GroupSize") or 0),
        })
    return out


def parse_dofind_response(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Разбор ответа doFind: записи Vehicle из results.candidates."""
    results = resp.get("results") or {}
    out: List[Dict[str, Any]] = []
    for cand in results.get("candidates") or []:
        v = cand.get("Vehicle") if isinstance(cand, dict) else None
        if isinstance(v, dict):
            out.append(v)
    return out


def parse_recordfinder_records(text: str) -> Tuple[int, List[Dict[str, str]]]:
    """Разбор key=value ответа recordFinder.cgi?action=find (раздел 10.3.4).

    Тело вида: totalCount=…, found=…, records[0].PlateNumber=… и т.д.
    Возвращает (totalCount, [{'PlateNumber': …, …}, …]) — по возрастанию индекса.
    """
    total = 0
    recs: Dict[int, Dict[str, str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        m = re.match(r"^records\[(\d+)\]\.(.+)$", key)
        if m:
            recs.setdefault(int(m.group(1)), {})[m.group(2)] = value
        elif key == "totalCount":
            try:
                total = int(value)
            except ValueError:
                pass
    return total, [recs[i] for i in sorted(recs)]


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
    event_id: int = 0  # общий id текста и снимка одного события (см. EventPhotoBinder)

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


_FONT_CACHE: Dict[int, Any] = {}


def _ui_font(size: int) -> Any:
    """Шрифт с кириллицей для подписей на изображениях (встроенный PIL-шрифт её не знает)."""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for name in ("segoeui.ttf", "arial.ttf", "tahoma.ttf"):
        try:
            font = ImageFont.truetype(name, size)  # Windows сам найдёт шрифт в Fonts
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def make_placeholder(width: int, height: int, text: str = "") -> Image.Image:
    """Заглушка (тёмный прямоугольник с текстом), пока видео/фото не получены."""
    canvas = Image.new("RGB", (width, height), (10, 10, 10))
    if text:
        d = ImageDraw.Draw(canvas)
        font = _ui_font(min(18, max(12, height // 14)))
        d.text((width // 2, height // 2), text, fill=(130, 130, 130), anchor="mm",
               font=font)
    return canvas


# ------------------------- Разрешение потока камеры -------------------------

# MJPG-subtype -> группа настроек кодирования камеры (Encode[канал].<группа>[<i>]):
# subtype 0 = основной поток (MainFormat[0]), 1 = Extra Stream 1 (SubFormat[0]),
# 2 = Extra Stream 2 (SubFormat[1]).
_SUBTYPE_TO_FORMAT: Dict[int, Tuple[str, int]] = {
    0: ("MainFormat", 0),
    1: ("SubFormat", 0),
    2: ("SubFormat", 1),
}

# Часто используемые пресеты Dahua -> фактический размер (для логов/подсказок).
RESOLUTION_PRESETS: Dict[str, Tuple[int, int]] = {
    "QCIF": (176, 144), "CIF": (352, 288), "QVGA": (320, 240),
    "VGA": (640, 480), "SVGA": (800, 600), "XVGA": (1024, 768),
    "D1": (704, 576), "960H": (960, 576), "WD1": (960, 576),
    "720P": (1280, 720), "1080N": (1920, 1080), "1080P": (1920, 1080),
    "3MP": (2048, 1536), "4MP": (2560, 1440), "5MP": (2592, 1944),
    "4K": (3840, 2160),
}


def encode_config_name(subtype: int) -> Tuple[str, int]:
    """Имя группы кодирования Dahua для MJPG-subtype (configManager.cgi)."""
    return _SUBTYPE_TO_FORMAT.get(int(subtype), _SUBTYPE_TO_FORMAT[1])


def parse_resolution(value: str) -> Optional[Tuple[int, int]]:
    """'1920x1080' -> (1920, 1080); пресеты Dahua (720P, D1, 960H…) тоже распознаются."""
    s = str(value).strip().upper().replace("×", "X").replace("*", "X")
    m = re.match(r"^(\d{2,5})\s*X\s*(\d{2,5})$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return RESOLUTION_PRESETS.get(s)


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
        self.last_error: str = ""  # причина последней ошибки JSON-запросов (для GUI)
        self._plates_source: Optional[str] = None  # какой канал чтения БД сработал

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
                applog("gate", f"[{self.name}] openStrobe: OK")
                return True, "OK"
            applog("gate", f"[{self.name}] openStrobe: HTTP {r.status_code}: {body[:120]}")
            return False, f"HTTP {r.status_code}: {body[:120]}"
        except requests.RequestException as e:
            applog("gate", f"[{self.name}] openStrobe: ошибка сети: {e}")
            return False, str(e)

    def get_snapshot(self, timeout: float = 6.0) -> Optional[bytes]:
        """JPEG-снимок: snapshot.cgi?channel=N&type=0 (раздел 4.4.2).

        type=0 — снимок «с передней части» (с матрицы): полное разрешение канала,
        независимо от настроек подпотоков. None — камера не ответила/отдала не JPEG.
        """
        try:
            r = self._get("/cgi-bin/snapshot.cgi", {"channel": self.channel, "type": 0},
                          timeout=timeout)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                return r.content
            applog("video", f"[{self.name}] снимок: HTTP {r.status_code}")
        except requests.RequestException as e:
            applog("video", f"[{self.name}] снимок: ошибка сети: {e}")
        return None

    # ----------------- БД номеров камеры (VehicleRegisterDB) -----------------

    def _post_json(self, path: str, payload: Dict[str, Any],
                   timeout: float = 8.0) -> Optional[Dict[str, Any]]:
        """POST с JSON-телом (VehicleRegisterDB). None — сеть/HTTP-ошибка."""
        try:
            r = self.session.post(self.base + path, json=payload, auth=self.auth,
                                  timeout=timeout)
            if r.status_code != 200:
                hint = ""
                if r.status_code == 403:
                    hint = (" — нет прав у учётной записи для БД номеров (ANPR): "
                            "нужен пользователь с правами администратора/ANPR")
                elif r.status_code == 400:
                    hint = (" — JSON API отклоняет запрос (слой /cgi-bin/api/* может быть "
                            "отключён в прошивке)")
                self.last_error = f"HTTP {r.status_code}{hint}"
                applog("camera", f"[{self.name}] {path}: HTTP {r.status_code}: {r.text[:120]}{hint}")
                return None
            self.last_error = ""
            return json.loads(r.text) if r.text.strip() else {}
        except (requests.RequestException, ValueError) as e:
            self.last_error = f"ошибка запроса: {e}"
            applog("camera", f"[{self.name}] {path}: {e}")
            return None

    def get_vehicle_groups(self) -> Optional[List[Dict[str, Any]]]:
        """Список групп БД транспорта (10.7.4 findGroup). None — камера недоступна."""
        resp = self._post_json("/cgi-bin/api/VehicleRegisterDB/findGroup", {"groupID": ""})
        if resp is None:
            return None
        return parse_vehicle_groups(resp)

    def _search_vehicles(self, vehicle_filter: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Поиск записей БД (10.7.8–10.7.10): startFindVehicle -> doFind* -> stopFind.

        vehicle_filter — непустые поля Vehicle как фильтр ({} — все записи).
        None — ошибка запроса; [] — пустой результат.
        """
        # по документации метод называется startFind; часть прошивок зовёт его startFindVehicle
        resp = self._post_json("/cgi-bin/api/VehicleRegisterDB/startFind",
                               {"vehicle": vehicle_filter})
        if resp is None:
            resp = self._post_json("/cgi-bin/api/VehicleRegisterDB/startFindVehicle",
                                   {"vehicle": vehicle_filter})
        if resp is None:
            return None
        token = resp.get("token", resp.get("Token"))
        try:
            total = int(resp.get("totalCount", resp.get("TotalCount")) or 0)
        except (TypeError, ValueError):
            total = 0
        out: List[Dict[str, Any]] = []
        begin, page, pages = 0, 100, 0
        # часть прошивок отдаёт totalCount=0 при непустой БД — пробуем хотя бы одну страницу
        while pages < 1000 and (begin < total or pages == 0):
            pages += 1
            r2 = self._post_json("/cgi-bin/api/VehicleRegisterDB/doFind",
                                 {"condition": {"token": token, "beginNumber": begin,
                                                "count": page}})
            if r2 is None:
                break
            cands = parse_dofind_response(r2)
            if not cands:
                break
            out.extend(cands)
            begin += len(cands)
        if token is not None:
            self._post_json("/cgi-bin/api/VehicleRegisterDB/stopFind", {"token": token})
        return out

    def export_camera_plates(self) -> Optional[Dict[str, Any]]:
        """Снимок БД номеров камеры (ТОЛЬКО ЧТЕНИЕ): {'allow', 'block', 'source'}.

        Два канала чтения, берётся первый успешный (запоминается в self._plates_source):
        'recordFinder' — старый cgi recordFinder.cgi, таблицы TrafficRedList
        (allow) / TrafficBlackList (block), раздел 10.3.4;
        'VehicleRegisterDB' — JSON API, группы AllowListDB/BlockListDB, разделы 10.7.x.
        Номера нормализованы (normalize_plate). None — БД недоступна ни одним каналом.
        """
        order = ["recordFinder", "VehicleRegisterDB"]
        if self._plates_source in order:
            order.remove(self._plates_source)
            order.insert(0, self._plates_source)
        errors = []
        for source in order:
            snap = (self._export_via_recordfinder() if source == "recordFinder"
                    else self._export_via_vehicleregdb())
            if snap is not None:
                self._plates_source = source
                self.last_error = ""
                snap["source"] = source
                return snap
            errors.append(f"{source}: {self.last_error or 'ошибка'}")
        self.last_error = "; ".join(errors)
        return None

    def _find_traffic_records(self, table: str) -> Optional[List[Dict[str, str]]]:
        """Записи таблицы справочника: recordFinder.cgi?action=find (раздел 10.3.4).

        table: 'TrafficRedList' (allow) или 'TrafficBlackList' (block).
        None — ошибка запроса; [] — таблица пуста.
        """
        out: List[Dict[str, str]] = []
        begin, page = 0, 1024
        while True:
            params = {"action": "find", "name": table, "count": page,
                      "condition.QueryCount": page,
                      "condition.QueryResultBegin": begin}
            try:
                r = self._get("/cgi-bin/recordFinder.cgi", params, timeout=10.0)
            except requests.RequestException as e:
                self.last_error = f"ошибка запроса: {e}"
                applog("camera", f"[{self.name}] recordFinder {table}: {e}")
                return None
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}"
                applog("camera", f"[{self.name}] recordFinder.cgi?name={table}: "
                                 f"HTTP {r.status_code}: {r.text[:120]}")
                return None
            total, recs = parse_recordfinder_records(r.text)
            if not recs:
                break
            out.extend(recs)
            begin += len(recs)
            if (total and begin >= total) or len(recs) < page or begin >= 100000:
                break
        self.last_error = ""
        return out

    def _export_via_recordfinder(self) -> Optional[Dict[str, List[str]]]:
        """Снимок allow/block через таблицы TrafficRedList/TrafficBlackList (10.3.4)."""
        recs = self._find_traffic_records("TrafficRedList")
        if recs is None:
            return None
        allow = {normalize_plate(r.get("PlateNumber", "")) for r in recs} - {""}
        # ошибка чтения блок-листа не обнуляет весь снимок: он может быть пуст и по-честному
        block_recs = self._find_traffic_records("TrafficBlackList")
        block = ({normalize_plate(r.get("PlateNumber", "")) for r in block_recs}
                 - {""}) if block_recs is not None else set()
        return {"allow": sorted(allow), "block": sorted(block)}

    def _export_via_vehicleregdb(self) -> Optional[Dict[str, List[str]]]:
        """Снимок allow/block через JSON API VehicleRegisterDB (группы + записи 10.7.x)."""
        groups = self.get_vehicle_groups()
        if groups is None:
            return None

        def _gtype(g: Dict[str, Any]) -> str:
            # прошивки шлют и «AllowListDB», и значения с пробелами/другим регистром
            return str(g.get("groupType") or "").upper().replace(" ", "")

        allow_ids = {g["groupID"] for g in groups
                     if _gtype(g).startswith("ALLOWLIST") and g["groupID"]}
        block_ids = {g["groupID"] for g in groups
                     if _gtype(g).startswith("BLOCKLIST") and g["groupID"]}
        records: Optional[List[Dict[str, Any]]] = self._search_vehicles({})
        if not records:  # пусто/ошибка — часть прошивок ищет только по группам
            records = []
            for gid in sorted(allow_ids | block_ids):
                part = self._search_vehicles({"GroupID": gid})
                if part:
                    records.extend(part)
        allow: Set[str] = set()
        block: Set[str] = set()
        for v in records or []:
            pn = normalize_plate(str(v.get("PlateNumber") or ""))
            if not pn:
                continue
            gid = str(v.get("GroupID") or "").strip()
            if gid in allow_ids:
                allow.add(pn)
            elif gid in block_ids:
                block.add(pn)
        return {"allow": sorted(allow), "block": sorted(block)}

    # -------------------- разрешение потока (configManager) --------------------

    def get_video_resolution(self, subtype: int = 1) -> Optional[str]:
        """Текущее разрешение потока камеры (Encode[…].Video.VideoResolution)."""
        group, idx = encode_config_name(subtype)
        name = f"Encode[{self.channel - 1}].{group}[{idx}].Video.VideoResolution"
        try:
            r = self._get("/cgi-bin/configManager.cgi", {"action": "getConfig", "name": name})
            if r.status_code != 200:
                return None
            m = re.search(r"VideoResolution=(.+)", r.text)
            return m.group(1).strip() if m else None
        except requests.RequestException:
            return None

    def set_video_resolution(self, subtype: int, value: str) -> Tuple[bool, str]:
        """Задать разрешение потока камеры (configManager.cgi?action=setConfig).

        Меняет само КОДИРОВАНИЕ потока на камере (видно всем потребителям потока!),
        а не масштаб картинки в GUI. Для пресетов (например «720P») дополнительно
        пробуется числовая форма «ШИРИНАxВЫСОТА» — её принимает часть прошивок.
        """
        value = str(value).strip()
        if not value:
            return False, "пустое значение разрешения"
        attempts = [value]
        size = parse_resolution(value)
        if size and value.upper() != f"{size[0]}X{size[1]}":
            attempts.append(f"{size[0]}x{size[1]}")
        group, idx = encode_config_name(subtype)
        name = f"Encode[{self.channel - 1}].{group}[{idx}].Video.VideoResolution"
        last = ""
        for attempt in attempts:
            try:
                r = self._get("/cgi-bin/configManager.cgi",
                              {"action": "setConfig", name: attempt})
                body = r.text.strip()
                last = f"HTTP {r.status_code}: {body[:120]}" if body else f"HTTP {r.status_code}"
                if r.status_code == 200 and body.upper() in ("OK", ""):
                    return True, f"{attempt} ({body or 'OK'})"
            except requests.RequestException as e:
                last = str(e)
        return False, last or "неизвестная ошибка"

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
            read_timeout=8.0,  # при замирании потока переподключение не дольше 8 с
        )
        for headers, body in stream.parts():
            if headers.get("content-type", "").startswith("image/") and body[:2] == b"\xff\xd8":
                yield body

    def anpr_stream(self, events: List[str], heartbeat: float = 5.0,
                    read_timeout: float = 30.0
                    ) -> Generator[Tuple[int, Dict[str, str], Optional[bytes]], None, None]:
        """Подписка на события трафика: snapManager.cgi?action=attachFileProc (4.4.3 + 10.1).

        Генерирует тройки (id_события, поля_события, jpeg_или_None). Текстовые и
        бинарные части идут вперемешку, снимок часто отстаёт от текста; привязка
        по размеру блоба (EventPhotoBinder), а не «к последнему тексту». id=0 —
        снимок без события, jpeg=None — текст (фото может прийти следующей частью).
        """
        params: Dict[str, Any] = {
            "action": "attachFileProc",
            "channel": self.channel,
            "heartbeat": int(heartbeat),
            "Flags[0]": "Event",
            "Events": "[" + ",".join(events) + "]",
        }
        stream = self._stream("/cgi-bin/snapManager.cgi", params, read_timeout=read_timeout)
        binder = EventPhotoBinder()
        for headers, body in stream.parts():
            ctype = headers.get("content-type", "")
            if ctype.startswith("text/"):
                for ev in parse_event_text(body):
                    yield binder.add_event(ev), ev, None
            elif ctype.startswith("image/") and body[:2] == b"\xff\xd8":
                bound = binder.feed_image(body)
                if bound is not None:
                    yield bound


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
        applog("worker", text)

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
    """Поток видео.

    mode='snapshot' (по умолчанию): опрос snapshot.cgi — каждый кадр отдельный
    короткий HTTP-запрос, поток не «замерзает», снимок в полном разрешении
    с матрицы (type=0), независимо от зернистости подпотоков.
    mode='stream': MJPEG-поток mjpg/video.cgi (больше FPS, но длинное
    соединение может подолгу не отдавать кадры).
    """

    def __init__(self, camera: DahuaCamera, frame_q: "queue.Queue",
                 status_q: "queue.Queue", subtype: int = 1, monitor=None,
                 set_resolution: Optional[str] = None,
                 mode: str = "snapshot", interval: float = 0.4):
        super().__init__(camera, monitor)
        self.frame_q = frame_q
        self.status_q = status_q
        self.subtype = subtype
        self.set_resolution = (set_resolution or "").strip()
        self.mode = "snapshot" if str(mode or "").strip().lower() == "snapshot" else "stream"
        self.interval = max(0.1, float(interval or 0.4))

    def run(self) -> None:
        if self.mode == "snapshot":
            self._run_snapshot()
        else:
            self._run_stream()

    def _run_stream(self) -> None:
        # Смена разрешения — это настройка КОДИРОВАНИЯ на камере
        # (Encode[…].Video.VideoResolution), а не масштаб картинки в GUI.
        if self.set_resolution:
            try:
                was = self.camera.get_video_resolution(self.subtype)
                ok, msg = self.camera.set_video_resolution(self.subtype, self.set_resolution)
                self._log(f"[{self.camera.name}] разрешение потока: {was or '?'} -> "
                          f"{self.set_resolution} ({'OK' if ok else 'ОШИБКА: ' + msg})")
            except Exception as e:
                self._log(f"[{self.camera.name}] разрешение потока: {e}")
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

    def _run_snapshot(self) -> None:
        """Опрос снимков: неудачный запрос виден сразу и повторяется — без «заморозки»."""
        if self.set_resolution:
            self._log(f"[{self.camera.name}] режим снимков: set_resolution не применяется "
                      f"(снимок берётся с матрицы в полном разрешении)")
        self._log(f"[{self.camera.name}] режим снимков: опрос каждые {self.interval:.2f}с")
        failures = 0
        while not self.stop_evt.is_set():
            t0 = time.time()
            err: Optional[Exception] = None
            try:
                jpeg = self.camera.get_snapshot(timeout=6.0)
            except Exception as e:  # сетевые ошибки get_snapshot глотает сам
                jpeg, err = None, e
            if jpeg:
                failures = 0
                self.attempt = 0
                put_latest(self.frame_q, jpeg)
            else:
                failures += 1
                if err and (failures == 1 or failures % 30 == 0):
                    self._log(f"[{self.camera.name}] снимок: {err}")
                if failures == 3 or failures % 30 == 0:
                    put_latest(self.status_q, ("status", self.camera.name, "video",
                                               f"снимок недоступен (попыток: {failures})"))
            # пауза до следующего опроса (за вычетом времени запроса)
            while not self.stop_evt.is_set():
                rest = self.interval - (time.time() - t0)
                if rest <= 0:
                    break
                time.sleep(min(0.05, rest))


class WhitelistSyncWorker(_ReconnectWorker):
    """Поток: периодическое ЧТЕНИЕ БД номеров камеры (allow/block) -> wl_q.

    В БД камеры ничего не пишется — только снимок для сверки номеров.
    """

    def __init__(self, camera: DahuaCamera, wl_q: "queue.Queue", status_q: "queue.Queue",
                 interval: float = 300.0, monitor=None):
        super().__init__(camera, monitor)
        self.wl_q = wl_q
        self.status_q = status_q
        self.interval = max(0.0, float(interval or 0.0))

    def run(self) -> None:
        first = True
        while not self.stop_evt.is_set():
            t0 = time.time()
            try:
                snap = self.camera.export_camera_plates()
            except Exception as e:
                snap = None
                self._log(f"[{self.camera.name}] справочник камеры: {e}")
            if snap is not None:
                self.attempt = 0
                put_latest(self.wl_q, ("camera_plates", self.camera.name, snap))
                if first:
                    self._log(f"[{self.camera.name}] справочник камеры "
                              f"({snap.get('source', '?')}): разрешённых {len(snap['allow'])}, "
                              f"блок-лист {len(snap['block'])}")
                first = False
                wait = self.interval
            else:
                reason = getattr(self.camera, "last_error", "") or "неизвестная ошибка"
                put_latest(self.status_q, ("status", self.camera.name, "whitelist",
                                           f"БД номеров камеры недоступна: {reason}"))
                put_latest(self.wl_q, ("camera_plates", self.camera.name,
                                       {"ok": False, "reason": reason}))
                # при ошибке повторяем чаще полного интервала — после починки
                # прав/сети справочник подгрузится в течение ~30 секунд
                wait = min(self.interval, 30.0)
            if self.interval <= 0:
                return  # однократная синхронизация при запуске
            while not self.stop_evt.is_set():
                rest = wait - (time.time() - t0)
                if rest <= 0:
                    break
                time.sleep(min(0.5, rest))


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
            ts_by_id: Dict[int, float] = {}
            try:
                self._log(f"[{self.camera.name}] подписка на события: {','.join(self.events)}")
                for ev_id, ev, jpeg in self.camera.anpr_stream(
                        self.events, heartbeat=self.heartbeat,
                        read_timeout=self.read_timeout):
                    self.attempt = 0
                    if jpeg is None:
                        # Текстовая часть: событие с номером (фото может прийти позже,
                        # иногда после текстов других событий — см. EventPhotoBinder).
                        ts_by_id[ev_id] = time.time()
                        plate = extract_plate(ev)
                        if ENABLE_DEBUG and plate:
                            applog("anpr", f"[{self.camera.name}] событие: {ev}")
                        if plate:
                            put_latest(self.anpr_q, ANPRResult(
                                self.camera.name, plate, None, ts_by_id[ev_id],
                                code=ev.get("EventBaseInfo.Code", ""),
                                action=ev.get("EventBaseInfo.Action", ""),
                                group_id=ev.get("GroupID", ""), fields=ev,
                                event_id=ev_id))
                    else:
                        # Снимок своего события (привязка по размеру блоба, не «последнему тексту»).
                        plate = extract_plate(ev)
                        ts = ts_by_id.pop(ev_id, time.time()) if ev_id else time.time()
                        if plate:
                            put_latest(self.anpr_q, ANPRResult(
                                self.camera.name, plate, jpeg, ts,
                                code=ev.get("EventBaseInfo.Code", ""),
                                action=ev.get("EventBaseInfo.Action", ""),
                                group_id=ev.get("GroupID", ""), fields=ev,
                                event_id=ev_id))
                        else:
                            if ENABLE_DEBUG:
                                applog("anpr", f"[{self.camera.name}] снимок без номера (пришлите debug.log для разбора)")
                            put_latest(self.anpr_q, ANPRResult(
                                self.camera.name, "", jpeg, time.time(),
                                fields=ev, event_id=ev_id))
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
