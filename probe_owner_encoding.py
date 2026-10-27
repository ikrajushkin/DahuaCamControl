"""Пробник кодировки MasterOfCar: где пробел превращается в «+» — на записи или чтении.

Пишет владельца вручную с %20 (пробел) и %2B (плюс) в URL — без quote_plus —
и печатает СЫРЫЕ строки recordFinder, чтобы понять поведение прошивки:
  - read «AB CD»        -> прошивка хранит как есть; quote_plus на записи ломал пробел
  - read «AB+CD»        -> ответ кодирует пробел как «+»; читать надо unquote_plus
  - read «AB%20CD»      -> ответ перекодирует значения; читать надо unquote
Тестовая запись TSTPRB01 в конце удаляется (остатки прошлых прогонов чистятся).

Запуск:
    python probe_owner_encoding.py --config dist\\config.json
"""
from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import quote

import config as config_mod
from config import load_config
from dahua_client import DahuaCamera, normalize_plate, PASS_TABLE

TEST_PLATE = "TSTPRB01"


def raw_master_of_car(cam: DahuaCamera) -> str:
    """Сырые строки ответа recordFinder только для тестовой записи (repr — видно кодировку)."""
    url = (cam.base + "/cgi-bin/recordFinder.cgi?action=find&name=" + PASS_TABLE +
           "&count=1024&condition.QueryCount=1024&condition.QueryResultBegin=0")
    r = cam.session.get(url, auth=cam.auth, timeout=10)
    idx = ""
    for line in r.text.splitlines():
        if f"PlateNumber={TEST_PLATE}" in line and ".PlateNumber" in line:
            idx = line.split("records[", 1)[1].split("]", 1)[0]
            break
    out = []
    for line in r.text.splitlines():
        if idx and f"records[{idx}]." in line:
            out.append(line)
    return "\n".join(out)


def write_owner(cam: DahuaCamera, rec_no: int, owner: str) -> str:
    """Запись владельца через recordUpdater с владельцем, закодированным через quote()."""
    enc = quote(owner, safe="")  # пробел -> %20, плюс -> %2B (не quote_plus!)
    if rec_no:
        url = (cam.base + f"/cgi-bin/recordUpdater.cgi?action=update&name={PASS_TABLE}"
               f"&RecNo={rec_no}&PlateNumber={TEST_PLATE}&MasterOfCar={enc}")
    else:
        url = (cam.base + f"/cgi-bin/recordUpdater.cgi?action=insert&name={PASS_TABLE}"
               f"&PlateNumber={TEST_PLATE}&MasterOfCar={enc}")
    r = cam.session.get(url, auth=cam.auth, timeout=10)
    return r.text.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Кодировка пробела в MasterOfCar (+ vs %20)")
    parser.add_argument("--config", default=None, help="путь к config.json")
    parser.add_argument("--ip", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--channel", type=int, default=1)
    args = parser.parse_args()

    if args.ip:
        cam_cfg = {"name": "probe_owner_encoding", "ip": args.ip,
                   "username": args.username or "", "password": args.password or "",
                   "channel": args.channel}
    else:
        path = args.config or os.path.join(config_mod._data_dir(), "config.json")
        if not os.path.exists(path):
            print(f"Нет {path}: укажите --ip/--username/--password")
            return 2
        cfg = load_config(path)
        cams = [c for c in cfg["cameras"] if config_mod.is_camera_configured(c)]
        if not cams:
            print("В config.json нет камер с заполненными ip/username/password")
            return 2
        cam_cfg = cams[0]

    cam = DahuaCamera(cam_cfg, name=str(cam_cfg.get("name") or "Камера"))
    print(f"Камера: {cam.name} ({cam_cfg['ip']})\n")

    # подчистить остатки прошлого прогона
    for r in cam.list_passes() or []:
        if normalize_plate(r.get("PlateNumber", "")) == TEST_PLATE:
            cam.remove_pass(int(r.get("RecNo") or 0))

    try:
        print("1) запись владельца 'AB CD' с %20 в URL:")
        print("   ответ:", write_owner(cam, 0, "AB CD"))
        print("   чтение (сырое):")
        for ln in raw_master_of_car(cam).splitlines():
            print("     ", repr(ln))

        rec_no = 0
        for r in cam.list_passes() or []:
            if normalize_plate(r.get("PlateNumber", "")) == TEST_PLATE:
                rec_no = int(r.get("RecNo") or 0)
        print(f"   RecNo={rec_no}")

        print("2) запись владельца 'AB+CD' с %2B в URL:")
        print("   ответ:", write_owner(cam, rec_no, "AB+CD"))
        print("   чтение (сырое):")
        for ln in raw_master_of_car(cam).splitlines():
            print("     ", repr(ln))
    finally:
        for r in cam.list_passes() or []:
            if normalize_plate(r.get("PlateNumber", "")) == TEST_PLATE:
                cam.remove_pass(int(r.get("RecNo") or 0))
        print("\nтестовая запись удалена")
    return 0


if __name__ == "__main__":
    sys.exit(main())