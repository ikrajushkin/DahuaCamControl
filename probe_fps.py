"""Временная диагностика: реальная частота MJPEG-кадров и скорость snapshot.cgi.

Показывает, что камера реально отдаёт по HTTP (mjpg/video.cgi), что кодирует
(Encode[…].Video.FPS/VideoResolution) и сколько времени занимает один снимок
(snapshot.cgi — режим video.mode=snapshot).

Запуск:  python probe_fps.py [секунд_на_замер_потока]
Результат: stdout (по кадрам — размеры, паузы между кадрами).
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dahua_client as dc
from probe_anpr_stream import _load_cfg


def measure_mjpeg(cam: "dc.DahuaCamera", subtype: int, wall: float) -> dict:
    """Замер MJPEG в отдельном потоке; зависший поток бросаем по join-таймауту."""
    out: dict = {"stamps": [], "sizes": [], "err": None}

    def run() -> None:
        t0 = time.time()
        try:
            for jpeg in cam.video_frames(subtype=subtype):
                out["stamps"].append(time.time() - t0)
                out["sizes"].append(len(jpeg))
                if time.time() - t0 >= wall:
                    break
        except Exception as e:  # noqa: BLE001 - диагностика
            out["err"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(wall + 30.0)  # запас на медленную сборку кадра
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # вывод в файл/pipe
    except AttributeError:
        pass
    wall = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    cfg, cams, cfg_path = _load_cfg()
    print(f"конфиг: {cfg_path}", flush=True)
    for cam_cfg in cams:
        ip = str(cam_cfg.get("ip"))
        cam = dc.DahuaCamera(cam_cfg, name=str(cam_cfg.get("name") or ip))
        print(f"\n== {ip} ({cam_cfg.get('name')})", flush=True)
        # 1) что камера КОДИРУЕТ: FPS и разрешение энкодера
        for group, idx, label in (("MainFormat", 0, "subtype=0"),
                                  ("SubFormat", 0, "subtype=1")):
            try:
                r = cam._get("/cgi-bin/configManager.cgi",
                             {"action": "getConfig",
                              "name": f"Encode[0].{group}[{idx}].Video"})
                kv = {}
                for line in r.text.splitlines():
                    k, _, v = line.partition("=")
                    if k.strip():
                        kv[k.strip()] = v.strip()
                fps = next((v for k, v in kv.items() if k.endswith(".FPS")), "?")
                res = next((v for k, v in kv.items()
                            if k.endswith(".VideoResolution")), "?")
                print(f"  энкодер {label}: FPS={fps} разрешение={res}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  энкодер {label}: ошибка {e}", flush=True)
        # 2) что камера реально ОТДАЁТ по MJPEG
        for st in (1, 0):
            print(f"  MJPEG subtype={st}:", flush=True)
            o = measure_mjpeg(cam, st, wall)
            stamps, sizes = o["stamps"], o["sizes"]
            if stamps:
                fps = len(stamps) / max(stamps[-1], 0.001)
                gaps = [stamps[i] - stamps[i - 1] for i in range(1, len(stamps))]
                print(f"    кадров: {len(stamps)} за {stamps[-1]:.1f}с -> {fps:.2f} FPS",
                      flush=True)
                print(f"    первый кадр через {stamps[0]:.2f}с, размеры "
                      f"{min(sizes) // 1024}..{max(sizes) // 1024} КБ", flush=True)
                if gaps:
                    print("    паузы между кадрами, с: "
                          + " ".join(f"{g:.2f}" for g in gaps[:12]), flush=True)
            else:
                print(f"    кадров НЕТ за {wall:.0f}с"
                      + (f" (ошибка: {o['err']})" if o["err"] else ""), flush=True)
            if o["err"] and stamps:
                print(f"    завершено ошибкой: {o['err']}", flush=True)
        # 3) сколько стоит один snapshot.cgi (режим video.mode=snapshot)
        for i in range(3):
            t0 = time.time()
            j = cam.get_snapshot(15.0)
            print(f"  snapshot #{i + 1}: {len(j) if j else 0} байт / "
                  f"{time.time() - t0:.2f}с", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())