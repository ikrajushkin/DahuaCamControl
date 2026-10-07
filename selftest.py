"""Офлайн-проверки компонентов без реальной камеры (запуск: python selftest.py).

Проверяются: нормализация номеров и справочник, multipart-парсер (MJPEG и
attachFileProc с бинарями), извлечение номера из события, вписывание изображений.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

from PIL import Image

import dahua_client as dc
from config import PlateBook, normalize_plate


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


def test_plate_book() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "allowed_plates.json")
        book = PlateBook(path)
        assert book.add("а123вв77")
        assert not book.add("А123ВВ 77")  # дубликат после нормализации
        assert book.contains("a123bb77")  # найден в латинице
        assert not book.contains("O123PP77")
        assert book.remove("А123ВВ77")
        assert not book.remove("X000XX00")
        book2 = PlateBook(path)  # перечитывание с диска
        assert book2.items() == []
    print("OK PlateBook (add/contains/remove/reload)")


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


def test_config_roundtrip() -> None:
    from config import load_config, save_config, DEFAULT_CONFIG
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "config.json")
        save_config(DEFAULT_CONFIG, path)
        cfg = load_config(path)
        assert cfg["cameras"][0]["channel"] == 1
        assert len(cfg["cameras"]) == 2
    print("OK load_config/save_config")


def main() -> int:
    tests = [test_normalize, test_plate_book, test_mjpeg_parsing,
             test_mjpeg_parsing_no_length, test_anpr_stream_parsing,
             test_extract_plate_variants, test_images, test_config_roundtrip]
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
