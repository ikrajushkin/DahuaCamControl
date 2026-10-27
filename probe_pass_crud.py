"""Round-trip проверка записи пропусков в справочник камеры (recordUpdater.cgi).

Проверяет путь записи, который использует редактор пропусков в GUI:
  create (10.3.6) -> чтение через recordFinder -> update (10.3.3) -> remove (10.3.5).
Добавляется ТЕСТОВЫЙ пропуск TSTPRB01, в конце он УДАЛЯЕТСЯ; если тест прервётся
на середине, повторный запуск сначала сам подчищает остатки прошлого прогона.

Запуск:
    python probe_pass_crud.py --config dist\\config.json
    python probe_pass_crud.py --ip 192.168.84.2 --username admin --password пароль
"""
from __future__ import annotations

import argparse
import os
import sys

import config as config_mod
from config import load_config
from dahua_client import DahuaCamera, normalize_plate

TEST_PLATE = "TSTPRB01"  # тестовая запись: создаётся и удаляется в одном прогоне


def find_rec(records, plate: str):
    norm = normalize_plate(plate)
    for r in records or []:
        if normalize_plate(r.get("PlateNumber", "")) == norm:
            return r
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка записи пропусков (с удалением теста)")
    parser.add_argument("--config", default=None, help="путь к config.json")
    parser.add_argument("--ip", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--channel", type=int, default=1)
    args = parser.parse_args()

    if args.ip:
        cam_cfg = {"name": "probe_pass_crud", "ip": args.ip,
                   "username": args.username or "", "password": args.password or "",
                   "channel": args.channel}
    else:
        base = config_mod._data_dir()
        path = args.config or os.path.join(base, "config.json")
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
    print(f"Камера: {cam.name} ({cam_cfg['ip']}) — запись тестового пропуска {TEST_PLATE}\n")

    fails = 0

    def check(cond: bool, text: str) -> bool:
        nonlocal fails
        print(("  OK   " if cond else "  FAIL ") + text)
        if not cond:
            fails += 1
        return cond

    before = cam.list_passes()
    if before is None:
        print(f"Не удалось прочитать справочник: {cam.last_error}")
        return 2
    print(f"Записей до теста: {len(before)}")
    old = find_rec(before, TEST_PLATE)
    if old:
        print(f"Найден остаток прошлого прогона (RecNo={old.get('RecNo')}) — удаляю…")
        cam.remove_pass(int(old.get("RecNo") or 0))

    ok, msg, rec_no = cam.add_pass(TEST_PLATE, "probe owner",
                                  "2026-01-01 08:00:00", "2026-01-02 23:59:59")
    check(ok, f"create: {msg}")
    if not ok:
        return 1

    recs = cam.list_passes() or []
    rec = find_rec(recs, TEST_PLATE)
    check(rec is not None, f"запись видна через recordFinder: {rec}")
    if rec is None:
        return 1
    rid = int(rec.get("RecNo") or rec_no or 0)
    check(rec.get("MasterOfCar") == "probe owner",
          f"владелец сохранён: MasterOfCar={rec.get('MasterOfCar')!r}")
    check(rec.get("BeginTime") == "2026-01-01 08:00:00"
          and rec.get("CancelTime") == "2026-01-02 23:59:59",
          f"период действия сохранён: {rec.get('BeginTime')} .. {rec.get('CancelTime')}")

    ok, msg = cam.update_pass(rid, TEST_PLATE, "probe owner 2",
                              "2026-02-01 09:30:00", "2026-02-02 23:59:59")
    check(ok, f"update: {msg}")
    recs = cam.list_passes() or []
    rec = find_rec(recs, TEST_PLATE)
    check(rec is not None and rec.get("MasterOfCar") == "probe owner 2",
          f"владелец изменён: {rec.get('MasterOfCar') if rec else None!r}")
    check(rec is not None and rec.get("BeginTime") == "2026-02-01 09:30:00"
          and rec.get("CancelTime") == "2026-02-02 23:59:59",
          f"период изменён: {rec.get('BeginTime') if rec else None} .. "
          f"{rec.get('CancelTime') if rec else None}")

    ok, msg = cam.remove_pass(rid)
    check(ok, f"remove: {msg}")
    recs = cam.list_passes() or []
    check(find_rec(recs, TEST_PLATE) is None, "тестовая запись удалена")
    check(len(recs) == len(before),
          f"количество записей как до теста: {len(recs)} == {len(before)}")

    print(f"\nИтого: {'ВСЁ OK' if fails == 0 else f'ошибок: {fails}'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
