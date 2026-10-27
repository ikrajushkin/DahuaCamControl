"""Подбор формата записи recordUpdater.cgi?action=insert для прошивки камеры.

Осторожно: камера блокирует учётную запись при серии неудачных логинов, поэтому
все запросы идут с креденшелами из config.json и паузами. Каждый вариант — один
запрос; при успехе — проверка чтением, затем УБОРКА тестовой записи TSTPRB01.

Запуск:
    python probe_insert_format.py --config dist\\config.json
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests

import config as config_mod
from config import load_config
from dahua_client import DahuaCamera

TEST_PLATE = "TSTPRB01"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--ip", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    args = parser.parse_args()
    if args.ip:
        cam_cfg = {"name": "probe", "ip": args.ip, "username": args.username or "",
                   "password": args.password or "", "channel": 1}
    else:
        base_dir = config_mod._data_dir()
        path = args.config or os.path.join(base_dir, "config.json")
        cfg = load_config(path)
        cam_cfg = [c for c in cfg["cameras"] if config_mod.is_camera_configured(c)][0]

    base = f"http://{cam_cfg['ip']}"
    auth = requests.auth.HTTPDigestAuth(cam_cfg["username"], cam_cfg["password"])
    cam = DahuaCamera(cam_cfg, name="probe")

    def get(qs: str):
        time.sleep(1.2)  # щадящий темп — чтобы не спровоцировать блокировку
        r = requests.get(base + "/cgi-bin/recordUpdater.cgi", params=qs, auth=auth, timeout=6)
        print(f"{r.status_code} {r.text.replace(chr(10), ' ')[:70]!r} <- {qs}")
        return r

    candidates = [
        "action=insert&name=TrafficRedList&PlateNumber=" + TEST_PLATE,
        "action=insert&name=TrafficRedList&PlateNumber=" + TEST_PLATE + "&MasterOfCar=probe",
        "action=insert&name=TrafficRedList&PlateNumber=" + TEST_PLATE + "&MasterOfCar=probe&PlateAttribute=Normal",
        "action=insert&name=TrafficRedList&PlateNumber=" + TEST_PLATE + "&PlateAttribute=Normal",
        "action=create&name=TrafficRedList&PlateNumber=" + TEST_PLATE,
        "action=insert&name=TrafficRedList&PlateNumber=" + TEST_PLATE + "&MasterOfCar=probe&PlateAttribute=",
    ]
    winner = None
    for qs in candidates:
        r = get(qs)
        if r.status_code == 200 and ("OK" in r.text.upper() or "RECNO=" in r.text.upper()):
            winner = qs
            break

    if winner is None:
        print("\nНи один вариант insert не принят — пробую POST form-body…")
        time.sleep(1.2)
        r = requests.post(base + "/cgi-bin/recordUpdater.cgi",
                          data=f"action=insert&name=TrafficRedList&PlateNumber={TEST_PLATE}&MasterOfCar=probe",
                          auth=auth, timeout=6)
        print(f"{r.status_code} {r.text.replace(chr(10), ' ')[:70]!r} <- POST form insert")
        if r.status_code == 200:
            winner = "POST form-body"

    if winner is None:
        print("Формат записи не найден.")
        return 1
    print(f"\nПОБЕДИЛ ФОРМАТ: {winner}")

    recs = cam.list_passes() or []
    hit = [x for x in recs if str(x.get("PlateNumber", "")).upper() == TEST_PLATE]
    print(f"Чтение назад: {'запись найдена ' + repr(hit[0]) if hit else 'НЕ НАЙДЕНА!'}")
    if hit:
        rid = int(hit[0].get("RecNo") or 0)
        # проверка корректировки на тестовой записи
        r = get(f"action=update&name=TrafficRedList&RecNo={rid}&PlateNumber={TEST_PLATE}&MasterOfCar=probe2&PlateAttribute=Normal")
        ok_upd = r.status_code == 200
        recs = cam.list_passes() or []
        hit2 = [x for x in recs if str(x.get("PlateNumber", "")).upper() == TEST_PLATE]
        print(f"update: {'OK' if ok_upd else 'FAIL'}, владелец после: "
              f"{hit2[0].get('MasterOfCar') if hit2 else None!r}")
        # УБОРКА
        r = get(f"action=remove&name=TrafficRedList&RecNo={rid}")
        recs = cam.list_passes() or []
        left = [x for x in recs if str(x.get("PlateNumber", "")).upper() == TEST_PLATE]
        print(f"remove: {r.status_code}, осталось тестовых: {len(left)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
