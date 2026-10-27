"""Матрица проб записи в справочник камеры: какой action/формат понимает прошивка.

recordUpdater.cgi на камере есть (bare GET -> 501, action=remove -> 400), но
action=create отдаёт 501. Перебираются безвредные варианты (update/remove — только
по несуществующему RecNo=99999999; create — тестовый номер TSTPRB01, который
подчищается в конце прогона).

Запуск:
    python probe_write_api.py --ip 192.168.82.11 --username admin --password пароль
"""
from __future__ import annotations

import argparse
import os
import sys

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
        base = config_mod._data_dir()
        path = args.config or os.path.join(base, "config.json")
        cfg = load_config(path)
        cam_cfg = [c for c in cfg["cameras"] if config_mod.is_camera_configured(c)][0]

    base = f"http://{cam_cfg['ip']}"
    auth = requests.auth.HTTPDigestAuth(cam_cfg["username"], cam_cfg["password"])
    cam = DahuaCamera(cam_cfg, name="probe")

    rec = "99999999"  # несуществующая запись — ничего не изменит
    probes = [
        ("GET", f"/cgi-bin/recordUpdater.cgi?action=create&name=TrafficRedList&table.PlateNumber={TEST_PLATE}&table.MasterOfCar=probe&table.PlateAttribute=Normal", None),
        ("GET", f"/cgi-bin/recordUpdater.cgi?action=insert&name=TrafficRedList&table.PlateNumber={TEST_PLATE}&table.MasterOfCar=probe", None),
        ("GET", f"/cgi-bin/recordUpdater.cgi?action=add&name=TrafficRedList&table.PlateNumber={TEST_PLATE}", None),
        ("GET", f"/cgi-bin/recordUpdater.cgi?action=update&name=TrafficRedList&table.RecNo={rec}&table.PlateNumber=X", None),
        ("GET", f"/cgi-bin/recordUpdater.cgi?action=modify&name=TrafficRedList&table.RecNo={rec}&table.PlateNumber=X", None),
        ("GET", f"/cgi-bin/recordUpdater.cgi?action=remove&name=TrafficRedList&table.RecNo={rec}", None),
        ("GET", f"/cgi-bin/recordUpdater.cgi?action=remove&name=TrafficRedList&RecNo={rec}", None),
        ("GET", f"/cgi-bin/recordUpdater.cgi?action=delete&name=TrafficRedList&table.RecNo={rec}", None),
        ("POST", f"/cgi-bin/recordUpdater.cgi?action=create&name=TrafficRedList&table.PlateNumber={TEST_PLATE}", None),
        ("POST", "/cgi-bin/recordUpdater.cgi", "action=create&name=TrafficRedList&table.PlateNumber=" + TEST_PLATE + "&table.MasterOfCar=probe"),
        ("GET", f"/cgi-bin/recordFinder.cgi?action=create&name=TrafficRedList&table.PlateNumber={TEST_PLATE}", None),
    ]
    for method, path, body in probes:
        try:
            if body is not None:
                r = requests.request(method, base + path, data=body, auth=auth, timeout=6)
            else:
                r = requests.request(method, base + path, auth=auth, timeout=6)
            print(f"{method} {path}\n    -> {r.status_code} {r.text[:80]!r}")
        except requests.RequestException as e:
            print(f"{method} {path}\n    -> ERR {e}")

    # уборка: если какой-то create сработал — удаляем тестовую запись
    recs = cam.list_passes() or []
    hit = [x for x in recs if str(x.get("PlateNumber", "")).upper() == TEST_PLATE]
    if hit:
        rid = int(hit[0].get("RecNo") or 0)
        print(f"\nТестовая запись создана (RecNo={rid}) — пробую удалить…")
        for act, key in (("remove", f"table.RecNo={rid}"), ("remove", f"RecNo={rid}"),
                         ("delete", f"table.RecNo={rid}")):
            r = requests.get(f"{base}/cgi-bin/recordUpdater.cgi",
                             params=f"action={act}&name=TrafficRedList&{key}",
                             auth=auth, timeout=6)
            print(f"    cleanup action={act} {key} -> {r.status_code} {r.text[:60]!r}")
            if r.status_code == 200:
                break
        recs2 = cam.list_passes() or []
        left = [x for x in recs2 if str(x.get("PlateNumber", "")).upper() == TEST_PLATE]
        print(f"    осталось тестовых записей: {len(left)}")
    else:
        print("\nТестовая запись не создана (ни один create не сработал) — уборка не нужна.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
