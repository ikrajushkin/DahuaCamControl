"""Офлайн-проверки компонентов без реальной камеры (запуск: python selftest.py).

Проверяются: нормализация номеров и решение по БД камеры, multipart-парсер
(MJPEG и attachFileProc с бинарями), извлечение номера из события, изображения.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

from PIL import Image

import dahua_client as dc
from config import normalize_plate, plate_decision


def _jpeg_bytes(w: int = 64, h: int = 32, color=(200, 30, 30)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class FakeResp:
    """Заглушка requests.Response: отдаёт один большой кусок байтов."""

    def __init__(self, data: bytes, ctype: str):
        self.headers = {"Content-Type": ctype}
        self._data = data

    def iter_content(self, chunk_size: int):
        yield self._data


def test_normalize() -> None:
    assert normalize_plate("а 123 вв 77") == "A123BB77"
    assert normalize_plate("A123BB77") == "A123BB77"
    assert normalize_plate("о-123-рр-77") == "O123PP77"
    print("OK normalize_plate")


def test_plate_decision() -> None:
    """Решение по БД камеры: allow/block, недоступная БД — без ложного «НЕТ»."""
    assert plate_decision(True, False, True) == (True, "ЕСТЬ в справочнике камеры")
    assert plate_decision(False, True, True) == (False, "ЗАПРЕЩЁН (блок-лист камеры)")
    assert plate_decision(False, False, True) == (False, "НЕТ в справочнике камеры")
    ok, mark = plate_decision(False, False, cam_available=False)
    assert not ok and mark == "справочник камеры недоступен"
    print("OK plate_decision (только БД камеры, честная метка при недоступности)")


def test_recordfinder_parsing() -> None:
    """Разбор key=value ответа recordFinder.cgi (таблицы TrafficRedList/BlackList)."""
    text = ("totalCount=3\r\nfound=3\r\n"
            "records[0].PlateNumber=A250MX76\r\nrecords[0].RecNo=1\r\n"
            "records[1].PlateNumber=с737хн777\r\nrecords[1].MasterOfCar=Козел 1\r\n"
            "records[2].PlateNumber=001M0C01\r\n")
    total, recs = dc.parse_recordfinder_records(text)
    assert total == 3 and len(recs) == 3
    assert recs[1]["PlateNumber"] == "с737хн777" and recs[1]["MasterOfCar"] == "Козел 1"
    assert normalize_plate(recs[1]["PlateNumber"]) == "C737XH777"
    assert "RecNo" not in recs[2]  # отсутствующие поля не выдумываем
    total, recs = dc.parse_recordfinder_records("found=0\r\n")
    assert total == 0 and recs == []
    total, recs = dc.parse_recordfinder_records("")
    assert total == 0 and recs == []
    print("OK parse_recordfinder_records (таблицы справочника TrafficRedList/BlackList)")


def test_mjpeg_parsing() -> None:
    j1 = _jpeg_bytes(64, 32, (200, 30, 30))
    j2 = _jpeg_bytes(64, 32, (30, 200, 30))
    data = (b"--b\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(j1)).encode()
            + b"\r\n\r\n" + j1 + b"\r\n"
            + b"--b\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(j2)).encode()
            + b"\r\n\r\n" + j2 + b"\r\n--b--\r\n")
    stream = dc.MultipartStream(FakeResp(data, "multipart/x-mixed-replace; boundary=b"))
    parts = list(stream.parts())
    assert len(parts) == 2, f"ожидалось 2 части, получено {len(parts)}"
    assert parts[0][1] == j1 and parts[1][1] == j2
    print("OK MultipartStream (MJPEG с Content-Length)")


def test_mjpeg_parsing_no_length() -> None:
    j = _jpeg_bytes(32, 16, (10, 10, 200))
    data = (b"--bnd\r\nContent-Type: image/jpeg\r\n\r\n" + j + b"\r\n"
            + b"--bnd\r\nContent-Type: image/jpeg\r\n\r\n" + j + b"\r\n--bnd--\r\n")
    stream = dc.MultipartStream(FakeResp(data, 'multipart/x-mixed-replace; boundary="bnd"'))
    parts = list(stream.parts())
    assert len(parts) == 2 and parts[0][1] == j and parts[1][1] == j
    print("OK MultipartStream (части без Content-Length)")


def test_anpr_stream_parsing() -> None:
    """Сценарий attachFileProc: текст события -> снимок -> heartbeat -> снимок."""
    j = _jpeg_bytes(80, 40, (30, 30, 200))
    txt = (b"Events[0].EventBaseInfo.Code=TrafficJunction\r\n"
           b"Events[0].EventBaseInfo.Action=Pulse\r\n"
           b"Events[0].EventBaseInfo.Index=0\r\n"
           b"Events[0].TrafficCar.PlateNumber=A123BB77\r\n"
           b"Events[0].GroupID=42\r\n")
    data = (b"--b\r\nContent-Type: text/plain\r\nContent-Length: "
            + str(len(txt)).encode() + b"\r\n\r\n" + txt + b"\r\n"
            + b"--b\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(j)).encode() + b"\r\n\r\n" + j + b"\r\n"
            + b"--b\r\nContent-Type: text/plain\r\nContent-Length: 10\r\n\r\nHeartbeat\r\n\r\n"
            + b"--b\r\nContent-Type: image/jpeg\r\nContent-Length: "
            + str(len(j)).encode() + b"\r\n\r\n" + j + b"\r\n--b--\r\n")
    stream = dc.MultipartStream(FakeResp(data, "multipart/x-mixed-replace; boundary=b"))
    pairs = []
    pending = {}
    for headers, body in stream.parts():
        ctype = headers.get("content-type", "")
        if ctype.startswith("text/"):
            for ev in dc.parse_event_text(body):
                pending = ev
                pairs.append((ev, None))
        elif ctype.startswith("image/"):
            pairs.append((dict(pending), body))

    # текст + снимок + снимок = 3 пары (heartbeat-часть без полей Events[...] пар не создаёт)
    assert len(pairs) == 3, f"ожидалось 3 пары, получено {len(pairs)}"
    assert pairs[0][1] is None
    plate = dc.extract_plate(pairs[0][0])
    assert plate == "A123BB77"
    assert pairs[1][0].get("TrafficCar.PlateNumber") == "A123BB77"
    assert pairs[1][1] == j
    assert pairs[2][0].get("TrafficCar.PlateNumber") == "A123BB77"
    assert pairs[2][1] == j
    print("OK ANPR-поток attachFileProc (текст + снимок + heartbeat)")


def test_event_image_sizes() -> None:
    """Размеры снимка события: прошивки считают SceneImage.Length по-разному."""
    # камера 1 (9E0A…): SceneImage.Length — сцена, кроп идёт следом
    assert dc.event_image_sizes({"SceneImage.Length": "150801", "Object.Image.Offset": "150801",
                                 "Object.Image.Length": "5471"}) == (156272, 150801, 5471)
    # камера 2 (ITC237…): SceneImage.Length — весь блоб, кроп внутри него
    assert dc.event_image_sizes({"SceneImage.Length": "122741", "Object.Image.Offset": "120444",
                                 "Object.Image.Length": "2297"}) == (122741, 120444, 2297)
    assert dc.event_image_sizes({}) == (0, 0, 0)  # событие без полей снимка
    assert dc.event_image_sizes({"SceneImage.Length": "5000"}) == (5000, 5000, 0)
    print("OK event_image_sizes (поля Scene/Object прошивок камер)")


def test_photo_binder_pairing() -> None:
    """Снимок привязывается к СВОЕМУ событию по размеру блоба, даже когда тексты
    нескольких событий шли подряд (иначе — фото предыдущего проезда)."""
    ev_a = {"TrafficCar.PlateNumber": "A123BB77", "SceneImage.Length": "100",
            "Object.Image.Offset": "90", "Object.Image.Length": "10"}
    ev_b = {"TrafficCar.PlateNumber": "B456CC77", "SceneImage.Length": "200",
            "Object.Image.Offset": "180", "Object.Image.Length": "20"}
    b = dc.EventPhotoBinder()
    id_a, id_b = b.add_event(ev_a), b.add_event(ev_b)
    img_a, img_b = b"s" * 100, b"t" * 200
    assert b.feed_image(img_b) == (id_b, ev_b, img_b)  # сначала пришло фото B — оно к B
    assert b.feed_image(img_a) == (id_a, ev_a, img_a)
    print("OK EventPhotoBinder (привязка снимка по размеру блоба)")


def test_photo_binder_crop_skipped() -> None:
    """Кроп номера отдельной частью — не фото события (иначе в панели растянутый номер)."""
    ev = {"TrafficCar.PlateNumber": "A123BB77", "SceneImage.Length": "300",
          "Object.Image.Offset": "250", "Object.Image.Length": "50"}
    b = dc.EventPhotoBinder()
    eid = b.add_event(ev)
    assert b.feed_image(b"c" * 50) is None  # кроп пришёл раньше сцены
    assert b.feed_image(b"s" * 300) == (eid, ev, b"s" * 300)
    assert b.feed_image(b"c" * 50) is None  # кроп после сцены (память размеров)
    print("OK EventPhotoBinder (кроп номера не считается фото)")


def test_photo_binder_fifo_and_orphan() -> None:
    """Без полей размеров — FIFO (старые прошивки); снимок без событий — с id=0."""
    a = {"TrafficCar.PlateNumber": "A1"}
    b = {"TrafficCar.PlateNumber": "B2"}
    bd = dc.EventPhotoBinder()
    ia, ib = bd.add_event(a), bd.add_event(b)
    assert bd.feed_image(b"1" * 10) == (ia, a, b"1" * 10)
    assert bd.feed_image(b"2" * 10) == (ib, b, b"2" * 10)
    assert bd.feed_image(b"3" * 10) == (0, {}, b"3" * 10)
    print("OK EventPhotoBinder (FIFO без размеров, снимок без события)")


def test_anpr_display_action() -> None:
    """Фото и номер в панели атомарны: старое фото не остаётся под новым номером."""
    import gui as gui_mod
    act = gui_mod.anpr_display_action
    assert act(5, 5, False) == "wait"   # текст события 5: плейсхолдер до снимка
    assert act(5, 5, True) == "photo"   # снимок события 5: показать
    assert act(5, 4, True) == "skip"    # поздний снимок заменённого события 4
    assert act(0, 7, True) == "skip"    # снимок без события: не трогать
    assert act(0, 7, False) == "wait"
    print("OK anpr_display_action (атомарность фото и номера в панели)")


def test_extract_plate_variants() -> None:
    assert dc.extract_plate({"TrafficCar.PlateNumber": "A1"}) == "A1"
    assert dc.extract_plate({"PlateNumber": "B2"}) == "B2"
    assert dc.extract_plate({"Vehicle.PlateNumber": "C3"}) == "C3"
    assert dc.extract_plate({"Other": "X"}) == ""
    print("OK extract_plate (варианты ключей прошивок)")


def test_images() -> None:
    img = Image.new("RGB", (1920, 1080), (50, 100, 50))
    fitted = dc.fit_image(img, 640, 360)
    assert fitted.size == (640, 360)
    ph = dc.make_placeholder(320, 180, "нет данных")
    assert ph.size == (320, 180)
    print("OK fit_image / make_placeholder")


def test_photo_size() -> None:
    """Размер фото под видео: video.photo_scale (0.5 — вдвое, 1.0 — в размер видео)."""
    import gui as gui_mod
    assert gui_mod.photo_size(640, 360, 0.5) == (320, 180)
    assert gui_mod.photo_size(640, 360, 1.0) == (640, 360)
    assert gui_mod.photo_size(640, 360, 0.75) == (480, 270)
    assert gui_mod.photo_size(100, 60, 0.25) == (25, 15)
    assert gui_mod.normalize_photo_scale(0.75) == 0.75
    assert gui_mod.normalize_photo_scale("0.75") == 0.75
    assert gui_mod.normalize_photo_scale(None) == 0.5
    assert gui_mod.normalize_photo_scale("мусор") == 0.5
    assert gui_mod.normalize_photo_scale(5) == 1.0
    assert gui_mod.normalize_photo_scale(-1) == 0.25
    print("OK photo_size / normalize_photo_scale (video.photo_scale)")


def test_config_roundtrip() -> None:
    from config import load_config, save_config, DEFAULT_CONFIG
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")
        save_config(DEFAULT_CONFIG, path)
        cfg = load_config(path)
        assert cfg["cameras"][0]["channel"] == 1
        assert len(cfg["cameras"]) == 2
    print("OK load_config/save_config")


def test_resolution_helpers() -> None:
    """Хелперы разрешения потока: пресеты, форма ШxВ, имена конфиг-групп Dahua."""
    assert dc.parse_resolution("720P") == (1280, 720)
    assert dc.parse_resolution("1920x1080") == (1920, 1080)
    assert dc.parse_resolution("960H") == (960, 576)
    assert dc.parse_resolution("D1") == (704, 576)
    assert dc.parse_resolution("1080P") == (1920, 1080)
    assert dc.parse_resolution("Nonsense") is None
    assert dc.encode_config_name(0) == ("MainFormat", 0)
    assert dc.encode_config_name(1) == ("SubFormat", 0)
    assert dc.encode_config_name(2) == ("SubFormat", 1)
    assert dc.encode_config_name(99) == ("SubFormat", 0)
    print("OK resolution helpers (parse_resolution / encode_config_name)")


def test_snapshot_worker() -> None:
    """VideoWorker в режиме снимков: кадр кладётся в очередь, воркер останавливается."""
    import queue as _q
    import time as _time
    j = _jpeg_bytes(48, 27, (5, 5, 5))
    calls = {"n": 0}

    class FakeCam:
        name = "fake"

        def get_snapshot(self, timeout: float = 6.0):
            calls["n"] += 1
            return j if calls["n"] <= 3 else None

    q: "_q.Queue" = _q.Queue(maxsize=2)
    st: "_q.Queue" = _q.Queue(maxsize=20)
    dc.stop_event.clear()
    w = dc.VideoWorker(FakeCam(), q, st, mode="snapshot", interval=0.1)
    w.start()
    got = None
    deadline = _time.time() + 5
    while _time.time() < deadline:
        try:
            got = q.get(timeout=0.5)
            break
        except _q.Empty:
            if not w.is_alive():
                break
    dc.stop_event.set()
    w.join(timeout=3)
    assert got == j, "кадр из режима снимков не получен"
    assert not w.is_alive(), "воркер не остановился по stop_event"
    print("OK VideoWorker snapshot mode (опрос снимков, остановка)")


def main() -> int:
    tests = [test_normalize, test_plate_decision, test_recordfinder_parsing,
             test_mjpeg_parsing,
             test_mjpeg_parsing_no_length, test_anpr_stream_parsing,
             test_event_image_sizes, test_photo_binder_pairing,
             test_photo_binder_crop_skipped, test_photo_binder_fifo_and_orphan,
             test_anpr_display_action,
             test_extract_plate_variants, test_images, test_photo_size,
             test_config_roundtrip,
             test_resolution_helpers, test_snapshot_worker]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\nИтого: {len(tests) - failed}/{len(tests)} проверок пройдено")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
