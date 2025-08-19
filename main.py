#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Boot Profiler (config-driven, stage order by index, marker-based transitions)

- Config file (JSON or YAML) controls:
  * log_file
  * start_pattern (power-on)
  * stages: [{index, name, (optional) pattern}]
  * markers: [{pattern, target, when: "start"|"end"}]
  * colors: {StageName: [r,g,b] 0..255 or 0..1 or "#RRGGBB"}
  * anim_duration, tick_count, headroom_factor, scale_adjust_alpha

Usage:
  python3 main.py --config ./config.json
"""

import argparse
import json
import math
import os
import re
import time
import threading 
from collections import deque

import glfw
import imgui
from imgui.integrations.glfw import GlfwRenderer

# ------------------------------- Config Loader --------------------------------

def parse_color(value):
    """Accepts:
       - [r,g,b] as floats 0..1 or ints 0..255
       - "#RRGGBB"
       - "r,g,b" string
       Returns (r,g,b) floats 0..1
    """
    if value is None:
        return (1.0, 1.0, 1.0)
    if isinstance(value, (list, tuple)):
        if len(value) < 3:
            raise ValueError("color list must have 3 elements")
        mx = max(value)
        if mx > 1.0:
            return (float(value[0]) / 255.0, float(value[1]) / 255.0, float(value[2]) / 255.0)
        return (float(value[0]), float(value[1]), float(value[2]))
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("#") and len(v) == 7:
            r = int(v[1:3], 16) / 255.0
            g = int(v[3:5], 16) / 255.0
            b = int(v[5:7], 16) / 255.0
            return (r, g, b)
        if "," in v:
            parts = [float(x) for x in v.split(",")]
            return parse_color(parts)
    raise ValueError(f"Unsupported color format: {value}")

def load_config(path):
    """Loads JSON (default) or YAML (if pyyaml is installed).
       Supports 'index' on stages and explicit 'markers'.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        raise

    data = None
    # Try JSON, fallback to YAML
    try:
        data = json.loads(text)
    except Exception:
        try:
            import yaml  # type: ignore
            data = yaml.safe_load(text)
        except Exception as e:
            raise RuntimeError("Config is not valid JSON and PyYAML not available for YAML.") from e

    cfg = {}
    cfg["log_file"] = data.get("log_file", "boot.log")
    cfg["anim_duration"] = float(data.get("anim_duration", 0.6))
    cfg["tick_count"] = int(data.get("tick_count", 8))
    cfg["headroom_factor"] = float(data.get("headroom_factor", 1.05))
    cfg["scale_adjust_alpha"] = float(data.get("scale_adjust_alpha", 0.18))

    start_pat = data.get("start_pattern") or data.get("Start") or data.get("start")
    if not start_pat:
        raise ValueError("start_pattern is required in config")
    cfg["start_pattern"] = re.compile(start_pat)

    # stages [{index, name, (optional) pattern}]
    stages_raw = data.get("stages")
    if not stages_raw or not isinstance(stages_raw, list):
        raise ValueError("stages must be a list of {index?, name, (pattern?)} objects")

    collected = []
    seen_idxs = set()
    auto_idx = 0
    for item in stages_raw:
        if not isinstance(item, dict) or "name" not in item:
            raise ValueError("each stage must be object with at least 'name'")
        idx = item.get("index")
        if idx is None:
            while auto_idx in seen_idxs:
                auto_idx += 1
            idx = auto_idx
            auto_idx += 1
        else:
            idx = int(idx)
        if idx in seen_idxs:
            raise ValueError(f"duplicate stage index: {idx}")
        seen_idxs.add(idx)
        name = item["name"]
        pat = item.get("pattern")  # optional, used for fallback start marker
        collected.append((idx, name, pat))

    collected.sort(key=lambda x: x[0])
    stages = [name for _, name, _ in collected]
    # Optional per-stage patterns (fallback)
    stage_patterns = {}
    for _, name, pat in collected:
        if pat:
            stage_patterns[name] = re.compile(pat)

    # colors
    colors_raw = data.get("colors", {})
    colors = {}
    for i, s in enumerate(stages):
        if s in colors_raw:
            try:
                colors[s] = parse_color(colors_raw[s])
            except Exception:
                colors[s] = (1.0, 1.0, 1.0)
        else:
            # auto color if missing
            import colorsys
            hue = (i / max(len(stages), 1)) * 0.7
            r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.9)
            colors[s] = (r, g, b)

    # markers (preferred)
    markers = []
    markers_raw = data.get("markers", [])
    for m in markers_raw:
        if not isinstance(m, dict):
            raise ValueError("markers items must be objects")
        pat = m.get("pattern")
        target = m.get("target")
        when = (m.get("when") or "start").lower()
        if when not in ("start", "end"):
            raise ValueError("marker 'when' must be 'start' or 'end'")
        if not pat or not target:
            raise ValueError("each marker must have 'pattern' and 'target'")
        if target not in stages:
            raise ValueError(f"marker target '{target}' not in stages")
        markers.append({
            "regex": re.compile(pat),
            "target": target,
            "when": when
        })

    # If no markers provided, synthesize "start" markers from stage_patterns (fallback)
    if not markers and stage_patterns:
        for s, rx in stage_patterns.items():
            markers.append({"regex": rx, "target": s, "when": "start"})

    cfg["stages"] = stages
    cfg["patterns"] = stage_patterns  # optional fallback
    cfg["colors"] = colors
    cfg["markers"] = markers
    return cfg

# ------------------------------- Globals from Config --------------------------

parser = argparse.ArgumentParser(description="Boot Profiler (configurable)")
parser.add_argument("--config", "-c", default="config.json", help="Path to JSON/YAML config")
args = parser.parse_args()

try:
    CFG = load_config(args.config)
except Exception as e:
    print(f"Configuration load error: {e}")
    raise SystemExit(1)

LOG_FILE = CFG["log_file"]
START_RE = CFG["start_pattern"]          # compiled
STAGES = CFG["stages"]                   # ordered by index
PATTERNS = CFG["patterns"] or {}         # optional fallback compiled regex per stage
COLORS = CFG["colors"]                   # name -> (r,g,b)
MARKERS = CFG["markers"] or []           # [{regex, target, when}]

ANIM_DURATION = CFG["anim_duration"]
TICK_COUNT = CFG["tick_count"]
HEADROOM_FACTOR = CFG["headroom_factor"]
SCALE_ADJUST_ALPHA = CFG["scale_adjust_alpha"]

# ------------------------------- File Tail ------------------------------------

def tail_file(path, callback):
    while not os.path.exists(path):
        time.sleep(0.5)
    f = open(path, "r", encoding="utf-8", errors="ignore")
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
                    f = open(path, "r", encoding="utf-8", errors="ignore")
                    f.seek(0, 2)
                continue
            callback(line)
    finally:
        try:
            f.close()
        except Exception:
            pass

# ------------------------------- Math / Labels --------------------------------

def closer_nice_max(value):
    """Return a 'nice' upper bound slightly larger than value * HEADROOM_FACTOR."""
    if value <= 0:
        return 1.0
    target = value * HEADROOM_FACTOR
    exp = math.floor(math.log10(max(target, 1e-12)))
    mag = 10 ** exp
    candidates = [1, 1.1, 1.2, 1.25, 1.5, 2, 2.5, 5]
    for c in candidates:
        cand = c * mag
        if cand >= target - 1e-12:
            return float(cand)
    return float(10 * mag)

def format_label(val):
    if val >= 10:
        return f"{val:.1f}s"
    if val >= 1:
        return f"{val:.2f}s"
    return f"{val:.3f}s"

# ------------------------------- Timeline / Parser ----------------------------

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

def log_thread(log_lines, timeline, lock):
    """Consume LOG_FILE lines; update timeline via markers and start_pattern."""
    def process_line(raw_line: str):
        line = raw_line.replace("\t", " ").strip()
        if not line:
            return

        now = time.time()
        ts = time.strftime('%H:%M:%S', time.localtime(now))
        new_line = f"{ts} - {line}"

        with lock:
            if not log_lines or log_lines[-1] != new_line:
                log_lines.append(new_line)

        # Power-on start pattern -> first stage starts here
        if START_RE.search(line):
            if STAGES:
                first = STAGES[0]
                with lock:
                    if timeline[first].get("start") is None:
                        timeline[first]["start"] = now
            return

        # Preferred path: explicit markers
        for m in MARKERS:
            if m["regex"].search(line):
                target = m["target"]
                when = m["when"]

                with lock:
                    if when == "start":
                        if timeline[target].get("start") is None:
                            timeline[target]["start"] = now
                        # auto-end previous stage if still open
                        idx = STAGES.index(target)
                        if idx - 1 >= 0:
                            prev = STAGES[idx - 1]
                            if timeline[prev].get("start") and timeline[prev].get("end") is None:
                                timeline[prev]["end"] = now
                    elif when == "end":
                        timeline[target]["end"] = now
                return  # one marker per line is enough by default

        # Fallback: stage pattern -> treat as that stage's start; auto-end previous
        for s, rx in PATTERNS.items():
            if rx.search(line):
                with lock:
                    if timeline[s].get("start") is None:
                        timeline[s]["start"] = now
                    idx = STAGES.index(s)
                    if idx - 1 >= 0:
                        prev = STAGES[idx - 1]
                        if timeline[prev].get("start") and timeline[prev].get("end") is None:
                            timeline[prev]["end"] = now
                return

    tail_file(LOG_FILE, process_line)

# ------------------------------- ImGui Helpers --------------------------------

def get_content_region_avail_safe():
    try:
        avail = imgui.get_content_region_avail()
        if hasattr(avail, "x") and hasattr(avail, "y"):
            return (float(avail.x), float(avail.y))
        return (float(avail[0]), float(avail[1]))
    except Exception:
        try:
            wp = imgui.get_window_position()
            ws = imgui.get_window_size()
            cp = imgui.get_cursor_screen_pos()
            avail_w = ws[0] - (cp[0] - wp[0])
            avail_h = ws[1] - (cp[1] - wp[1])
            return (max(float(avail_w), 1.0), max(float(avail_h), 1.0))
        except Exception:
            return (400.0, 300.0)

def draw_vertical_stack(draw_list, pos, size, displayed, max_scale):
    """Bottom is zero; bars stack upward; max_scale external."""
    x, y = pos
    w, h = size
    margin = 12.0
    legend_h = 26.0

    max_bar_h = max(h * 0.9 - legend_h - margin, 10.0)
    bar_w = max((w - margin * 2) / 3.0, 1.0)

    bar_x = x + (w - bar_w) / 2.0
    bar_bottom = y + max_bar_h + max((h - (max_bar_h + legend_h + margin)) / 2.0, margin / 2.0)

    axis_color = imgui.get_color_u32_rgba(1, 1, 1, 1)
    grid_color = imgui.get_color_u32_rgba(0.6, 0.6, 0.6, 1)

    draw_list.add_rect(bar_x, bar_bottom - max_bar_h, bar_x + bar_w, bar_bottom, axis_color)

    current_y = bar_bottom
    for stage in STAGES:
        dur = displayed.get(stage, 0.0)
        ratio = (dur / max_scale) if max_scale > 0 else 0
        height = max_bar_h * min(max(ratio, 0.0), 1.0)
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(bar_x, current_y - height, bar_x + bar_w, current_y, color)
        current_y -= height

    # ticks & labels
    label_x = max(bar_x - 50, x + 6)
    for i in range(0, TICK_COUNT + 1):
        r = i / float(TICK_COUNT)
        ty = bar_bottom - (max_bar_h * r)
        draw_list.add_line(bar_x, ty, bar_x + bar_w, ty, grid_color)
        value = max_scale * r
        draw_list.add_text(label_x, ty - 7, axis_color, format_label(value))

    # legend
    legend_y = bar_bottom + 6
    legend_total_w = len(STAGES) * 120
    legend_x = x + (w - legend_total_w) / 2.0
    for stage in STAGES:
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(legend_x, legend_y, legend_x + 18, legend_y + 18, color)
        draw_list.add_rect(legend_x, legend_y, legend_x + 18, legend_y + 18, axis_color)
        draw_list.add_text(legend_x + 22, legend_y, axis_color, stage)
        legend_x += 120

def draw_horizontal_bars(draw_list, pos, size, displayed, max_scale, font):
    x, y = pos
    w, h = size
    margin = 12.0
    bar_x = x + margin
    bar_y = y + margin
    bar_w = max(w - margin * 2, 1.0)
    bar_h = max(h - margin * 2, 1.0)

    axis_color = imgui.get_color_u32_rgba(1, 1, 1, 1)
    shadow_color = imgui.get_color_u32_rgba(0, 0, 0, 0.8)
    grid_color = imgui.get_color_u32_rgba(0.6, 0.6, 0.6, 1)

    draw_list.add_rect(bar_x, bar_y, bar_x + bar_w, bar_y + bar_h, axis_color)

    # vertical grid lines
    for i in range(1, TICK_COUNT + 1):
        r = i / float(TICK_COUNT)
        tx = bar_x + bar_w * r
        draw_list.add_line(tx, bar_y, tx, bar_y + bar_h, grid_color)
        value = max_scale * r
        draw_list.add_text(tx - 10, bar_y - 15, axis_color, format_label(value))

    bar_height = bar_h / max(len(STAGES), 1)
    for i, stage in enumerate(STAGES):
        dur = displayed.get(stage, 0.0)
        ratio = (dur / max_scale) if max_scale > 0 else 0
        bar_fill_width = bar_w * min(max(ratio, 0.0), 1.0)
        top = bar_y + i * bar_height
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(bar_x, top + 4, bar_x + bar_fill_width, top + bar_height - 4, color)

        imgui.push_font(font)
        text = f"{stage}: {dur:.3f}s"
        text_size = imgui.calc_text_size(text)
        text_y = top + (bar_height - text_size.y) / 2

        text_x_outside = bar_x + bar_fill_width + 10
        # constrain into panel if overflowing
        if (text_x_outside + text_size.x) > (x + w - margin):
            text_x_inside = bar_x + bar_fill_width - text_size.x - 10
            text_x = max(bar_x + 5, text_x_inside)
        else:
            text_x = text_x_outside

        draw_list.add_text(text_x + 1, text_y + 1, shadow_color, text)
        draw_list.add_text(text_x, text_y, axis_color, text)
        imgui.pop_font()

# ------------------------------- GUI Thread -----------------------------------

def gui_thread(log_lines, timeline, lock):
    if not glfw.init():
        raise RuntimeError("glfw.init() failed")

    primary_monitor = glfw.get_primary_monitor()
    workarea = glfw.get_monitor_workarea(primary_monitor)
    screen_width, screen_height = workarea[2], workarea[3]
    initial_width, initial_height = int(screen_width / 2), int(screen_height / 2)

    window = glfw.create_window(initial_width, initial_height, "Boot Profiler", None, None)
    glfw.make_context_current(window)

    imgui.create_context()
    impl = GlfwRenderer(window)

    io = imgui.get_io()
    font_path = "fonts/DejaVuSans.ttf"
    font_bold_path = "fonts/DejaVuSans-Bold.ttf"
    font_large_bold = None
    try:
        io.fonts.add_font_from_file_ttf(font_path, 16)
        font_large_bold = io.fonts.add_font_from_file_ttf(font_bold_path, 20)
        impl.refresh_font_texture()
    except Exception as e:
        print(f"폰트 로드 실패: {e}. 기본 폰트로 계속합니다.")
        font_large_bold = io.fonts.add_font_default()

    no_resize_no_move_flags = imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE

    displayed = {s: 0.0 for s in STAGES}
    anim_state = {s: {"running": False, "start_t": 0.0, "from": 0.0, "to": 0.0} for s in STAGES}

    left_scale = 1.0
    right_scale = 1.0
    displayed_left_scale = float(left_scale)
    displayed_right_scale = float(right_scale)

    highlight_color = (42/255, 74/255, 122/255, 1.0)

    while not glfw.window_should_close(window):
        glfw.poll_events()
        impl.process_inputs()
        imgui.new_frame()

        # hotkeys: move window to corners
        monitor = glfw.get_primary_monitor()
        workarea = glfw.get_monitor_workarea(monitor)
        win_w, win_h = glfw.get_window_size(window)

        if imgui.is_key_pressed(glfw.KEY_INSERT, repeat=False):
            glfw.set_window_pos(window, workarea[0], workarea[1])
        if imgui.is_key_pressed(glfw.KEY_PAGE_UP, repeat=False):
            glfw.set_window_pos(window, workarea[0] + workarea[2] - win_w, workarea[1])
        if imgui.is_key_pressed(glfw.KEY_DELETE, repeat=False):
            glfw.set_window_pos(window, workarea[0], workarea[1] + workarea[3] - win_h)
        if imgui.is_key_pressed(glfw.KEY_PAGE_DOWN, repeat=False):
            glfw.set_window_pos(window, workarea[0] + workarea[2] - win_w, workarea[1] + workarea[3] - win_h)

        # reset (F10)
        if imgui.is_key_pressed(glfw.KEY_F10, repeat=False):
            with lock:
                for stage in STAGES:
                    timeline[stage]["start"] = None
                    timeline[stage]["end"] = None
                log_lines.clear()
            for s in STAGES:
                displayed[s] = 0.0
                anim_state[s]["running"] = False
            left_scale = 1.0
            right_scale = 1.0
            displayed_left_scale = 1.0
            displayed_right_scale = 1.0

        now = time.time()
        width, height = glfw.get_framebuffer_size(window)
        half_w = width / 2
        half_h = height / 2

        with lock:
            timeline_copy = {s: dict(timeline[s]) for s in STAGES}

        # targets: live for running, final for ended
        targets = {}
        for s in STAGES:
            start = timeline_copy[s].get("start")
            end = timeline_copy[s].get("end")
            if start is None:
                targets[s] = 0.0
            elif end is None:
                targets[s] = max(0.0, now - start)
                anim_state[s]["running"] = False
            else:
                targets[s] = max(0.0, end - start)
                if not anim_state[s]["running"] and displayed[s] < targets[s] - 1e-6:
                    anim_state[s]["running"] = True
                    anim_state[s]["start_t"] = now
                    anim_state[s]["from"] = displayed[s]
                    anim_state[s]["to"] = targets[s]

        # update displayed with easing on stage completion
        for s in STAGES:
            start = timeline_copy[s].get("start")
            end = timeline_copy[s].get("end")
            if start is None:
                displayed[s] = 0.0
                anim_state[s]["running"] = False
            elif end is None:
                displayed[s] = targets[s]  # live increase
            else:
                if anim_state[s]["running"]:
                    t0 = anim_state[s]["start_t"]
                    frac = (now - t0) / max(ANIM_DURATION, 1e-9)
                    if frac >= 1.0:
                        displayed[s] = anim_state[s]["to"]
                        anim_state[s]["running"] = False
                    else:
                        eased = 1 - (1 - frac) ** 3
                        displayed[s] = anim_state[s]["from"] + (anim_state[s]["to"] - anim_state[s]["from"]) * eased
                else:
                    displayed[s] = targets[s]

        # dynamic scales (left: sum, right: max)
        target_total = sum(compute_durations(timeline_copy).values())
        needed_left = max(sum(displayed.values()), target_total, 1e-9)
        needed_right = max(max(displayed.values()) if displayed else 1.0, 1e-9)

        if needed_left > left_scale * 0.999999:
            left_scale = closer_nice_max(needed_left)
        if needed_right > right_scale * 0.999999:
            right_scale = closer_nice_max(needed_right)

        displayed_left_scale += (left_scale - displayed_left_scale) * SCALE_ADJUST_ALPHA
        displayed_right_scale += (right_scale - displayed_right_scale) * SCALE_ADJUST_ALPHA

        # --- UI ---
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND, *highlight_color)
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND_ACTIVE, *highlight_color)
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND_COLLAPSED, *highlight_color)

        # Summary (top-left)
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
            total = sum(displayed.values()) or 1.0
            for stage in STAGES:
                sstart = timeline_copy[stage].get("start")
                send = timeline_copy[stage].get("end")
                dur = (send - sstart) if (sstart and send) else (now - sstart if sstart else 0.0)
                ratio = (dur / total) * 100.0
                imgui.table_next_row()
                imgui.table_next_column(); imgui.text(stage)
                imgui.table_next_column(); imgui.text(time.strftime('%H:%M:%S', time.localtime(sstart)) if sstart else "-")
                imgui.table_next_column(); imgui.text(time.strftime('%H:%M:%S', time.localtime(send)) if send else "-")
                imgui.table_next_column(); imgui.text(f"{dur:.3f}s")
                imgui.table_next_column(); imgui.text(f"{ratio:.1f}%")
            imgui.end_table()
        imgui.end()

        # Total (bottom-left)
        imgui.set_next_window_position(0, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Total", False, no_resize_no_move_flags)
        avail_w, avail_h = get_content_region_avail_safe()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        draw_vertical_stack(draw_list, pos, (avail_w, avail_h), displayed, displayed_left_scale)
        imgui.end()

        # Log (top-right)
        imgui.set_next_window_position(half_w, 0)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Log", False)
        with lock:
            lines_snapshot = list(log_lines)
        imgui.begin_child("log_child", 0, 0, border=True)
        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        for line in lines_snapshot:
            imgui.text_unformatted(line)
        imgui.pop_style_var()
        imgui.end_child()
        imgui.end()

        # Durations (bottom-right)
        imgui.set_next_window_position(half_w, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Durations", False, no_resize_no_move_flags)
        avail_w, avail_h = get_content_region_avail_safe()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        # large bold font may be None if load failed; ImGui will fallback
        draw_horizontal_bars(draw_list, pos, (avail_w, avail_h), displayed, displayed_right_scale, font_large_bold)
        imgui.end()

        imgui.pop_style_color(3)

        imgui.render()
        impl.render(imgui.get_draw_data())
        glfw.swap_buffers(window)

    impl.shutdown()
    glfw.terminate()

# ------------------------------- Main -----------------------------------------

def main():
    log_lines = deque(maxlen=1000)
    timeline = {stage: {"start": None, "end": None} for stage in STAGES}
    lock = threading.Lock()

    t = threading.Thread(target=log_thread, args=(log_lines, timeline, lock), daemon=True)
    t.start()

    gui_thread(log_lines, timeline, lock)

if __name__ == "__main__":
    main()
