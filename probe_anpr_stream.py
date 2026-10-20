"""Временная диагностика: сырая лента attachFileProc («текст ↔ JPEG»).

Помогает понять, почему фото в панели отстаёт на один проезд: пишет каждую часть
потока с таймингом и сопоставляет JPEG со событием по SceneImage.Length /
Object.Image.Length из полей события.

Запуск:  python probe_anpr_stream.py [секунд]
Результат: probe_stream.log (дублируется в stdout).
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

import dahua_client as dc
from config import _data_dir, load_config


def _load_cfg():
    """Конфиг пробы: сначала обычный config.json, затем dist/config.json (рабочий)."""
    for path in (os.path.join(_data_dir(), "config.json"),
                 os.path.join(_data_dir(), "dist", "config.json")):
        if not os.path.isfile(path):
            continue
        cfg = load_config(path)
        cams = [c for c in cfg.get("cameras", []) if str(c.get("ip", "")).strip()]
        if cams:
            return cfg, cams, path
    return None, [], ""


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    cfg, cams, cfg_path = _load_cfg()
    if not cams:
        print("нет настроенных камер в config.json / dist/config.json")
        return 1
    cam_cfg = cams[0]
    cam = dc.DahuaCamera(cam_cfg, name=str(cam_cfg.get("name") or "probe"))
    events = list(cfg.get("anpr_events", ["TrafficJunction"]))
    out_path = os.path.join(_data_dir(), "probe_stream.log")
    t_end = time.time() + seconds
    n = 0
    seen = []  # (id, номер, ожидаемые размеры снимков)
    with open(out_path, "w", encoding="utf-8") as f:

        def log(msg: str) -> None:
            line = datetime.now().strftime("%H:%M:%S.%f")[:-3] + " " + msg
            f.write(line + "\n")
            f.flush()
            print(line)

        log(f"подписка: {cam_cfg.get('ip')} события={events} на {seconds:.0f}с")
        try:
            stream = cam._stream(
                "/cgi-bin/snapManager.cgi",
                {"action": "attachFileProc", "channel": cam.channel, "heartbeat": 5,
                 "Flags[0]": "Event", "Events": "[" + ",".join(events) + "]"},
                read_timeout=30.0)
            for headers, body in stream.parts():
                n += 1
                ctype = headers.get("content-type", "?")
                cl = headers.get("content-length", "-")
                if ctype.startswith("text/"):
                    evs = dc.parse_event_text(body)
                    log(f"#{n} TEXT cl={cl} len={len(body)} событий={len(evs)}")
                    for ev in evs:
                        sizes = {ev.get("SceneImage.Length", ""),
                                 ev.get("Object.Image.Length", "")}
                        seen.append((ev.get("EventID", "?"), dc.extract_plate(ev), sizes))
                        log(f"    событие id={ev.get('EventID', '?')} "
                            f"plate={dc.extract_plate(ev)!r} "
                            f"scene_len={ev.get('SceneImage.Length', '-')} "
                            f"obj_len={ev.get('Object.Image.Length', '-')} "
                            f"index={ev.get('IndexInGroup', '-')}"
                            f"/{ev.get('CountInGroup', '-')}")
                elif ctype.startswith("image/") or body[:2] == b"\xff\xd8":
                    want = [f"id={e[0]}({e[1]})" for e in seen if str(len(body)) in e[2]]
                    log(f"#{n} IMAGE cl={cl} len={len(body)} magic={body[:2].hex()} "
                        f"ожидающие={{{', '.join(want) or 'нет'}}} "
                        f"последний_текст=id{seen[-1][0] if seen else '-'}")
                else:
                    log(f"#{n} {ctype} cl={cl} len={len(body)}")
                if time.time() >= t_end:
                    break
        except Exception as e:  # noqa: BLE001 - диагностика
            log(f"ошибка: {e}")
        log(f"готово, частей={n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())