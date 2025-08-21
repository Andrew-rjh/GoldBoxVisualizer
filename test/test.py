#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F2(글로벌) 감지 시 부팅 로그 시퀀스를 boot.log에 기록.
- 각 줄 간 간격을 무작위 분배하되, 전체 소요시간은 정확히 15초(기본)로 맞춤.
- Wayland/X11 모두에서 동작하도록 evdev로 /dev/input/* 직접 읽기 (보통 sudo 필요).
"""

import os
import time
import random
import threading
import argparse
import signal
import asyncio

from evdev import InputDevice, list_devices, ecodes

MESSAGES = [
    "NOTICE:  Reset status: Power-On Reset",
    "Starting kernel ...",
    "Welcome to Auto Linux BSP 42.0 (kirkstone)!",
    "Rootfs start2",
    "Rootfs start3",
    "Rootfs start4",
    "s32g399ardb3 login:",
]

# ----------------------- 파일 기록 -----------------------

class BootLogWriter:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write_line(self, line: str):
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())

def now_tag() -> str:
    return time.strftime("[%H:%M:%S]")

def random_gaps(n: int, total_sec: float):
    """지수분포 난수 n개를 뽑아 전체 합을 total_sec로 정규화."""
    weights = [random.expovariate(1.0) for _ in range(n)]
    s = sum(weights) or 1.0
    scale = total_sec / s
    return [w * scale for w in weights]

def run_sequence(writer: BootLogWriter, total_sec: float):
    """첫 줄 즉시 출력, 이후 n-1개의 간격 합이 total_sec이 되도록 출력."""
    gaps = random_gaps(len(MESSAGES) - 1, total_sec)
    writer.write_line(f"{now_tag()} {MESSAGES[0]}")
    for i, gap in enumerate(gaps, start=1):
        time.sleep(gap)
        writer.write_line(f"{now_tag()} {MESSAGES[i]}")

def spawn_sequence(writer: BootLogWriter, total_sec: float):
    threading.Thread(target=run_sequence, args=(writer, total_sec), daemon=True).start()

# ----------------------- F2 글로벌 키 감시 -----------------------

async def read_loop(dev: InputDevice, on_press):
    async for event in dev.async_read_loop():
        if event.type == ecodes.EV_KEY and event.code == ecodes.KEY_F2 and event.value == 1:
            on_press()

async def monitor_f2(on_press):
    devices = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
            caps = dev.capabilities().get(ecodes.EV_KEY, [])
            supported = set()
            if isinstance(caps, dict):
                for v in caps.values():
                    if isinstance(v, list):
                        supported.update(v)
                    else:
                        supported.add(v)
            else:
                supported = set(caps)
            if ecodes.KEY_F2 in supported:
                print(f"Listening: {dev.path} - {dev.name}")
                devices.append(dev)
            else:
                dev.close()
        except Exception:
            # 권한/장치 이슈는 무시
            pass

    if not devices:
        print("F2를 지원하는 키보드 장치를 찾지 못했습니다. 권한 문제일 수 있으니 sudo로 실행해 보세요.")
        # 그냥 대기 (장치 없으면 이벤트 없음)
        await asyncio.Event().wait()

    tasks = [asyncio.create_task(read_loop(dev, on_press)) for dev in devices]
    await asyncio.gather(*tasks)

# ----------------------- 메인 -----------------------

def main():
    ap = argparse.ArgumentParser(description="F2를 누를 때마다 boot.log에 15초짜리 랜덤 간격 부팅 로그 시퀀스를 기록합니다.")
    ap.add_argument("--log", default="boot.log", help="로그 파일 경로 (기본: ./boot.log)")
    ap.add_argument("--total", type=float, default=15.0, help="시퀀스 총 시간 초 단위 (기본: 15.0)")
    args = ap.parse_args()

    writer = BootLogWriter(args.log)

    def on_press():
        print(f"{now_tag()} F2 detected -> start sequence ({args.total:.1f}s)")
        spawn_sequence(writer, args.total)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, loop.stop)
        except NotImplementedError:
            pass  # Windows 등

    try:
        loop.run_until_complete(monitor_f2(on_press))
    finally:
        loop.close()

if __name__ == "__main__":
    main()
