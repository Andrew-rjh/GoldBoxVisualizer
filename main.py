
import threading
import time
import re
import os
from collections import deque

import glfw
import imgui
from imgui.integrations.glfw import GlfwRenderer

LOG_FILE = "boot.log"

PATTERNS = {
    "Start": re.compile(r"NOTICE:  Reset status: Power-On Reset"),
    "Bootloader": re.compile(r"Starting kernel \.\.\."),
    "Kernel": re.compile(r"Welcome to Auto Linux BSP 42\.0 \(kirkstone\)!"),
    "Rootfs": re.compile(r"s32g399ardb3 login:")
}

STAGES = ["Bootloader", "Kernel", "Rootfs"]
COLORS = {
    "Bootloader": (0.2, 0.6, 0.9),
    "Kernel": (0.2, 0.8, 0.2),
    "Rootfs": (0.9, 0.3, 0.3),
}

def tail_file(path, callback):
    while not os.path.exists(path):
        time.sleep(0.5)
    f = open(path, "r")
    try:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                if not os.path.exists(path):
                    f.close()
                    while not os.path.exists(path):
                        time.sleep(0.5)
                    f = open(path, "r")
                    f.seek(0, 2)
                continue
            callback(line.rstrip())
    finally:
        try:
            f.close()
        except Exception:
            pass

def log_thread(log_lines, timeline, lock):
    start_time = None
    current_stage = None

    def process_line(line: str):
        nonlocal start_time, current_stage
        now = time.time()
        log_lines.append(f"{time.strftime('%H:%M:%S', time.localtime(now))} - {line}")

        if PATTERNS["Start"].search(line):
            start_time = now
            current_stage = "Bootloader"
            with lock:
                timeline["Bootloader"]["start"] = now
            return

        if PATTERNS["Bootloader"].search(line) and start_time is not None and current_stage != "Kernel":
            with lock:
                timeline["Bootloader"]["end"] = now
                timeline["Kernel"]["start"] = now
            current_stage = "Kernel"
            return

        if PATTERNS["Kernel"].search(line) and current_stage == "Kernel":
            with lock:
                timeline["Kernel"]["end"] = now
                timeline["Rootfs"]["start"] = now
            current_stage = "Rootfs"
            return

        if PATTERNS["Rootfs"].search(line) and current_stage == "Rootfs":
            with lock:
                timeline["Rootfs"]["end"] = now
            current_stage = None
            return

    tail_file(LOG_FILE, process_line)

def compute_durations(timeline):
    durations = {}
    for stage in STAGES:
        start = timeline[stage].get("start")
        end = timeline[stage].get("end")
        try:
            durations[stage] = (end - start) if (start and end) else 0.0
        except Exception:
            durations[stage] = 0.0
    return durations

def get_content_region_avail_safe():
    try:
        avail = imgui.get_content_region_avail()
        if hasattr(avail, "x") and hasattr(avail, "y"):
            return (avail.x, avail.y)
        return (float(avail[0]), float(avail[1]))
    except Exception:
        try:
            wp = imgui.get_window_position()
            ws = imgui.get_window_size()
            cp = imgui.get_cursor_screen_pos()
            avail_w = ws[0] - (cp[0] - wp[0])
            avail_h = ws[1] - (cp[1] - wp[1])
            return (max(avail_w, 1.0), max(avail_h, 1.0))
        except Exception:
            return (400.0, 300.0)

def draw_vertical_stack(draw_list, pos, size, durations, total):
    x, y = pos
    w, h = size
    offset = y
    for stage in STAGES:
        dur = durations.get(stage, 0.0)
        ratio = (dur / total) if total > 0 else 0
        height = h * ratio
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(x, offset, x + w, offset + height, color)
        offset += height

def draw_horizontal_bars(draw_list, pos, size, durations, total):
    x, y = pos
    w, h = size
    bar_height = h / max(len(STAGES), 1)
    for i, stage in enumerate(STAGES):
        dur = durations.get(stage, 0.0)
        ratio = (dur / total) if total > 0 else 0
        width = w * ratio
        top = y + i * bar_height
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(x, top, x + width, top + bar_height - 4, color)
        try:
            draw_list.add_text(x + 5, top + 5, imgui.get_color_u32_rgba(1,1,1,1), f"{stage}: {dur:.3f}s")
        except TypeError:
            try:
                draw_list.add_text((x + 5, top + 5), f"{stage}: {dur:.3f}s", imgui.get_color_u32_rgba(1,1,1,1))
            except Exception:
                pass

def gui_thread(log_lines, timeline, lock):
    if not glfw.init():
        raise RuntimeError("glfw.init() failed")
    window = glfw.create_window(1200, 800, "Boot Profiler", None, None)
    glfw.make_context_current(window)

    imgui.create_context()
    impl = GlfwRenderer(window)

    while not glfw.window_should_close(window):
        glfw.poll_events()
        impl.process_inputs()
        imgui.new_frame()

        width, height = glfw.get_framebuffer_size(window)
        half_w = width / 2
        half_h = height / 2

        with lock:
            durations = compute_durations(timeline)
        total_time = sum(durations.values())

        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Summary", False)
        imgui.text("Subject Profile result(GUI)")
        if imgui.begin_table("summary_table", 5):
            imgui.table_setup_column("Stage")
            imgui.table_setup_column("Start")
            imgui.table_setup_column("End")
            imgui.table_setup_column("Duration")
            imgui.table_setup_column("Ratio")
            imgui.table_headers_row()
            for stage in STAGES:
                start = timeline[stage]["start"]
                end = timeline[stage]["end"]
                dur = durations.get(stage, 0.0)
                ratio = (dur / total_time) * 100 if total_time > 0 else 0
                imgui.table_next_row()
                imgui.table_next_column(); imgui.text(stage)
                imgui.table_next_column(); imgui.text(time.strftime('%H:%M:%S', time.localtime(start)) if start else "-")
                imgui.table_next_column(); imgui.text(time.strftime('%H:%M:%S', time.localtime(end)) if end else "-")
                imgui.table_next_column(); imgui.text(f"{dur:.3f}s")
                imgui.table_next_column(); imgui.text(f"{ratio:.1f}%")
            imgui.end_table()
        imgui.end()

        imgui.set_next_window_position(0, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Total", False)
        avail_w, avail_h = get_content_region_avail_safe()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        draw_vertical_stack(draw_list, pos, (avail_w, avail_h), durations, total_time)
        imgui.invisible_button("stack", avail_w, avail_h)
        imgui.end()

        imgui.set_next_window_position(half_w, 0)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Log", False)
        imgui.begin_child("log_child", 0, 0, border=True)
        for line in list(log_lines):
            imgui.text_unformatted(line)
        imgui.end_child()
        imgui.end()

        imgui.set_next_window_position(half_w, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Durations", False)
        avail_w, avail_h = get_content_region_avail_safe()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        draw_horizontal_bars(draw_list, pos, (avail_w, avail_h), durations, total_time)
        imgui.invisible_button("bars", avail_w, avail_h)
        imgui.end()

        imgui.render()
        impl.render(imgui.get_draw_data())
        glfw.swap_buffers(window)

    impl.shutdown()
    glfw.terminate()

def main():
    log_lines = deque(maxlen=1000)
    timeline = {stage: {"start": None, "end": None} for stage in STAGES}
    lock = threading.Lock()

    t = threading.Thread(target=log_thread, args=(log_lines, timeline, lock), daemon=True)
    t.start()

    gui_thread(log_lines, timeline, lock)

if __name__ == "__main__":
    main()
