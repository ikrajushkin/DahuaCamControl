# -*- coding: utf-8 -*-
"""Зонд соответствия «текст <-> снимок» по официальному пути anpr_stream.

Печатает для каждого кортежа решение anpr_display_action (точно как в GUI):
какой event_id у текста и снимка одного события, и какова реальная задержка
снимка после текста.
"""
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"c:\Proj\DahuaCamControl")

from config import load_config
from dahua_client import DahuaCamera, extract_plate
from gui import anpr_display_action

cfg = load_config(r"c:\Proj\DahuaCamControl\dist\config.json")
cam = [c for c in cfg.get("cameras", []) if str(c.get("ip", "")).strip()][0]
stop = threading.Event()
print(f"[{time.strftime('%H:%M:%S')}] {cam.get('name')} {cam.get('ip')}", flush=True)
c = DahuaCamera(cam, name=str(cam.get("name") or "probe"))
t0 = time.time()
shown = 0
last_text_t = {}
while time.time() - t0 < 480:
    try:
        for ev_id, ev, jpeg in c.anpr_stream(list(cfg.get("anpr_events", ["TrafficJunction"])),
                                             heartbeat=int(cfg.get("heartbeat", 5)),
                                             read_timeout=float(cfg.get("read_timeout", 30))):
            plate = extract_plate(ev)
            now = time.time()
            if jpeg is None:
                action = anpr_display_action(shown, ev_id, False)
                shown = ev_id
                last_text_t[ev_id] = now
                print(f"{time.strftime('%H:%M:%S')} TEXT  id={ev_id} plate={plate!r} "
                      f"-> {action}", flush=True)
            else:
                action = anpr_display_action(shown, ev_id, True)
                dt = now - last_text_t.get(ev_id, now)
                print(f"{time.strftime('%H:%M:%S')} PHOTO id={ev_id} plate={plate!r} "
                      f"байт={len(jpeg)} +{dt:.1f}с -> {action}", flush=True)
    except Exception as e:
        print(f"{time.strftime('%H:%M:%S')} ОШИБКА/переподключение: {e}", flush=True)
        time.sleep(3)
print(f"[{time.strftime('%H:%M:%S')}] зонд завершён", flush=True)
