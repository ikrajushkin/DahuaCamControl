"""Диагностика БД номеров камеры (ТОЛЬКО ЧТЕНИЕ, ничего не изменяет).

Показывает, что именно камера отдаёт по обоим каналам чтения:
- recordFinder.cgi (раздел 10.3.4): таблицы TrafficRedList (allow) / TrafficBlackList (block);
- JSON API VehicleRegisterDB (разделы 10.7.x): группы AllowListDB/BlockListDB и записи;
плюс итоговый снимок allow/block, который использует GUI для сверки номеров.

Запуск:
    python check_db.py --ip 192.168.84.2 --username admin --password пароль
    python check_db.py                  # берёт первую настроенную камеру из config.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import config as config_mod
from config import applog, load_config
from dahua_client import DahuaCamera


def main() -> int:
    parser = argparse.ArgumentParser(description="Чтение БД номеров камеры (без изменений)")
    parser.add_argument("--config", default=None, help="путь к config.json")
    parser.add_argument("--ip", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--channel", type=int, default=1)
    parser.add_argument("--debug", action="store_true", help="полный debug.log")
    args = parser.parse_args()

    if args.debug:
        config_mod.ENABLE_DEBUG = True

    if args.ip:
        cam_cfg = {"name": "check_db", "ip": args.ip,
                   "username": args.username or "", "password": args.password or "",
                   "channel": args.channel}
    else:
        base = config_mod._data_dir()
        path = args.config or os.path.join(base, "config.json")
        if not os.path.exists(path):
            print(f"Нет {path}: укажите --ip/--username/--password или заполните config.json")
            return 2
        cfg = load_config(path)
        cams = [c for c in cfg["cameras"] if config_mod.is_camera_configured(c)]
        if not cams:
            print("В config.json нет камер с заполненными ip/username/password")
            return 2
        cam_cfg = cams[0]

    cam = DahuaCamera(cam_cfg, name=str(cam_cfg.get("name") or "Камера"))
    print(f"Камера: {cam.name} ({cam_cfg['ip']}), только чтение БД номеров\n")

    # 1. Старый cgi: recordFinder.cgi, таблицы справочника (раздел 10.3.4)
    print("== recordFinder.cgi (таблицы справочника, раздел 10.3.4) ==")
    rf_ok = False
    for kind, table in (("allow", "TrafficRedList"), ("block", "TrafficBlackList")):
        recs = cam._find_traffic_records(table)
        if recs is None:
            print(f"  {table} ({kind}): ОШИБКА чтения — подробности в debug.log")
            continue
        rf_ok = True
        print(f"  {table} ({kind}): записей {len(recs)}")
        for r in recs[:10]:
            print(f"    PlateNumber={r.get('PlateNumber')!r} "
                  f"MasterOfCar={r.get('MasterOfCar')!r} RecNo={r.get('RecNo')!r}")
        if len(recs) > 10:
            print(f"    … ещё {len(recs) - 10}")
    if not rf_ok:
        print("  recordFinder.cgi недоступен — пробуем JSON API ниже")

    # 2. JSON API: группы БД (10.7.4 findGroup) и записи (10.7.8–10.7.10)
    print("\n== VehicleRegisterDB (JSON API, разделы 10.7.x) ==")
    groups = cam.get_vehicle_groups()
    if groups is None:
        print("  findGroup: камера не ответила (сеть/HTTP/JSON) — подробности в debug.log")
    elif not groups:
        print("  findGroup: (пусто) — у прошивки нет групп AllowListDB/BlockListDB")
    else:
        for g in groups:
            print(f"  groupID={g['groupID']!r} groupType={g['groupType']!r} "
                  f"groupSize={g['groupSize']} name={g['groupName']!r}")
        records = cam._search_vehicles({})
        if records is None:
            print("  Записи (startFind/doFind): ОШИБКА поиска — подробности в debug.log")
        else:
            print(f"  Записи (startFind/doFind): найдено {len(records)}")
            for v in records[:10]:
                print(f"    GroupID={v.get('GroupID')!r} PlateNumber={v.get('PlateNumber')!r} "
                      f"UID={v.get('UID')!r}")

    # 3. Итоговый снимок, который видит GUI
    print("\n== Снимок для GUI (export_camera_plates) ==")
    snap = cam.export_camera_plates()
    if snap is None:
        print("ОШИБКА: снимок не собран ни одним каналом")
        return 1
    print(f"источник: {snap.get('source')}")
    print(json.dumps({k: v for k, v in snap.items() if k != "source"},
                     ensure_ascii=False, indent=2))
    print("\nГотово. Никакие данные камеры не изменялись.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
