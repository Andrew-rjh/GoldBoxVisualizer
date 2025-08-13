import threading
import time
import re
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
    with open(path, "r") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.1)
                continue
            callback(line.rstrip())

def log_thread(log_lines, timeline, lock):
    start_time = None
    stage_order = ["Bootloader", "Kernel", "Rootfs"]
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

        if PATTERNS["Bootloader"].search(line) and start_time is not None:
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
        start = timeline[stage]["start"]
        end = timeline[stage]["end"]
        durations[stage] = end - start if start and end else 0.0
    return durations

def draw_vertical_stack(draw_list, pos, size, durations, total):
    x, y = pos
    w, h = size
    offset = y
    for stage in STAGES:
        dur = durations[stage]
        ratio = dur / total if total > 0 else 0
        height = h * ratio
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(x, offset, x + w, offset + height, color)
        offset += height

def draw_horizontal_bars(draw_list, pos, size, durations, total):
    x, y = pos
    w, h = size
    bar_height = h / len(STAGES)
    for i, stage in enumerate(STAGES):
        dur = durations[stage]
        ratio = dur / total if total > 0 else 0
        width = w * ratio
        top = y + i * bar_height
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(x, top, x + width, top + bar_height - 4, color)
        draw_list.add_text(x + 5, top + 5, imgui.get_color_u32_rgba(1,1,1,1), f"{stage}: {dur:.3f}s")


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

        # Top-left: summary table
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
                dur = durations[stage]
                ratio = (dur / total_time) * 100 if total_time > 0 else 0
                imgui.table_next_row()
                imgui.table_next_column(); imgui.text(stage)
                imgui.table_next_column(); imgui.text(time.strftime('%H:%M:%S', time.localtime(start)) if start else "-")
                imgui.table_next_column(); imgui.text(time.strftime('%H:%M:%S', time.localtime(end)) if end else "-")
                imgui.table_next_column(); imgui.text(f"{dur:.3f}s")
                imgui.table_next_column(); imgui.text(f"{ratio:.1f}%")
            imgui.end_table()
        imgui.end()

        # Bottom-left: vertical stacked bar
        imgui.set_next_window_position(0, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Total", False)
        avail = imgui.get_content_region_avail()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        draw_vertical_stack(draw_list, pos, (avail.x, avail.y), durations, total_time)
        imgui.invisible_button("stack", avail.x, avail.y)
        imgui.end()

        # Top-right: log viewer
        imgui.set_next_window_position(half_w, 0)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Log", False)
        imgui.begin_child("log_child", 0, 0, border=True)
        for line in list(log_lines):
            imgui.text_unformatted(line)
        imgui.end_child()
        imgui.end()

        # Bottom-right: horizontal bars
        imgui.set_next_window_position(half_w, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Durations", False)
        avail = imgui.get_content_region_avail()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        draw_horizontal_bars(draw_list, pos, (avail.x, avail.y), durations, total_time)
        imgui.invisible_button("bars", avail.x, avail.y)
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

