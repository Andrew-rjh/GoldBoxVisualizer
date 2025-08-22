#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Boot Profiler with live Settings panel (evdev-only toggle)

- Insert 또는 (NumLock off) 키패드 0: 토글 가능한 'Settings' 창
  * 창은 다른 컴포넌트보다 항상 위에 렌더되고 자유 이동 가능
  * 다시 누르면 닫힘
- 기존 Insert/Del/PgUp/PgDn 위치 이동 핫키 제거
- 주요 시각화/애니 파라미터를 실시간 조정 가능
- Settings 저장/불러오기/기본값 로드 지원 (settings_ui.json)
"""
import argparse
import json
import math
import os
import re
import time
import threading
import queue
import random
from collections import deque
from datetime import datetime

import glfw
import imgui
from imgui.integrations.glfw import GlfwRenderer

# evdev (global hotkeys). optional dependency

try:
    from evdev import InputDevice, list_devices, ecodes
except Exception:
    InputDevice = None
    list_devices = None
    ecodes = None

# ------------------------------- Config Loader --------------------------------

def parse_color(value):
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
    cfg["window_title"] = data.get("window_title", "Boot Profiler")
    cfg["log_file"] = data.get("log_file", "boot.log")
    cfg["anim_duration"] = float(data.get("anim_duration", 0.6))
    cfg["tick_count"] = int(data.get("tick_count", 8))
    cfg["headroom_factor"] = float(data.get("headroom_factor", 1.05))
    cfg["scale_adjust_alpha"] = float(data.get("scale_adjust_alpha", 0.18))

    start_pat = data.get("start_pattern") or data.get("Start") or data.get("start")
    if not start_pat:
        raise ValueError("start_pattern is required in config")
    cfg["start_pattern"] = re.compile(start_pat)

    # stages
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
        pat = item.get("pattern")
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
            import colorsys
            hue = (i / max(len(stages), 1)) * 0.7
            r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.9)
            colors[s] = (r, g, b)

    # markers
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

    if not markers and stage_patterns:
        for s, rx in stage_patterns.items():
            markers.append({"regex": rx, "target": s, "when": "start"})

    # optional F8 settings
    cfg["f8_vertical_seconds"] = float(data.get("f8_vertical_seconds")) if data.get("f8_vertical_seconds") is not None else None
    cfg["f8_horizontal_seconds"] = float(data.get("f8_horizontal_seconds")) if data.get("f8_horizontal_seconds") is not None else None
    cfg["f8_anim_duration"] = float(data.get("f8_anim_duration", 0.5))

    # optional label animation duration
    cfg["label_anim_duration"] = float(data.get("label_anim_duration", 0.30))

    cfg["stages"] = stages
    cfg["patterns"] = stage_patterns
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

WINDOW_TITLE = CFG["window_title"]
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

# F8 settings (None or float seconds)
F8_VERTICAL_SECONDS = CFG.get("f8_vertical_seconds")
F8_HORIZONTAL_SECONDS = CFG.get("f8_horizontal_seconds")
F8_ANIM_DURATION = CFG.get("f8_anim_duration", 0.5)

# Label animation duration
LABEL_ANIM_DURATION = CFG.get("label_anim_duration", 0.30)

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
                time.sleep(0.01)
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

def format_label(val: float) -> str:
    decimals = 1 if val >= 10 else 2 if val >= 1 else 3
    s = f"{val:.{decimals}f}"
    sign = ""
    if s.startswith("-"):
        sign, s = "-", s[1:]
    if "." in s:
        i, f = s.split(".")
        f = f.rstrip("0")
        if f == "":
            f = "0"
        s = f"{sign}{i}.{f}"
    else:
        s = f"{sign}{s}.0"
    return s + "s"

def fmt_hms_hundredths(ts):
    if not ts:
        return "-"
    dt = datetime.fromtimestamp(ts)
    return f"{dt.strftime('%H:%M:%S')}.{dt.microsecond // 10000:02d}"

def ease_out_cubic(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3

# ------------------------------- Timeline / Parser ----------------------------

def compute_durations(timeline):
    durations = {}
    now = time.time()
    for stage in STAGES:
        start = timeline[stage].get("start")
        end = timeline[stage].get("end")
        try:
            if start and end:
                durations[stage] = max(0.0, end - start)
            elif start and not end:
                durations[stage] = max(0.0, now - start)
            else:
                durations[stage] = 0.0
        except Exception:
            durations[stage] = 0.0
    return durations

def log_thread(log_lines, timeline, lock, control_q):
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

        # start - 이제 AUTO_RESET 신호를 GUI로 보냄
        if START_RE.search(line):
            # send control message to GUI to reset+start (GUI will set first stage start)
            try:
                control_q.put("AUTO_RESET")
            except Exception:
                pass
            return

        # markers
        for m in MARKERS:
            if m["regex"].search(line):
                target = m["target"]; when = m["when"]
                with lock:
                    if when == "start":
                        if timeline[target].get("start") is None:
                            timeline[target]["start"] = now
                        idx = STAGES.index(target)
                        if idx - 1 >= 0:
                            prev = STAGES[idx - 1]
                            if timeline[prev].get("start") and timeline[prev].get("end") is None:
                                timeline[prev]["end"] = now
                    elif when == "end":
                        timeline[target]["end"] = now
                return

        # fallback
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

# ------------------------------- Test Sequence Writer -------------------------

_TEST_MESSAGES = [
    "NOTICE:  Reset status: Power-On Reset",
    "Starting kernel ...",
    "Welcome to Auto Linux BSP 42.0 (kirkstone)!",
    "Rootfs start2",
    "Rootfs start3",
    "Rootfs start4",
    "s32g399ardb3 login:"
]

def _random_gaps(n: int, total_sec: float):
    if n <= 0:
        return []
    min_gap = 0.3
    min_total = n * min_gap
    if total_sec < min_total:
        total_sec = min_total
    extra = total_sec - min_total
    weights = [random.expovariate(1.0) for _ in range(n)]
    s = sum(weights) or 1.0
    scale = extra / s if s > 0 else 0.0
    return [w * scale + min_gap for w in weights]

def spawn_test_sequence(log_path: str, total_sec: float = 5.0):
    def writer():
        gaps = _random_gaps(len(_TEST_MESSAGES) - 1, total_sec)
        try:
            with open(log_path, "a", encoding="utf-8", errors="ignore") as f:
                t0 = time.localtime()
                f.write(f"[{time.strftime('%H:%M:%S', t0)}] {_TEST_MESSAGES[0]}\n")
                f.flush()
                os.fsync(f.fileno())
                for gap, msg in zip(gaps, _TEST_MESSAGES[1:]):
                    time.sleep(gap)
                    f.write(f"[{time.strftime('%H:%M:%S', time.localtime())}] {msg}\n")
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as e:
            print("[test_seq] write error:", e)
    t = threading.Thread(target=writer, daemon=True)
    t.start()

# ------------------------------- Global Hotkeys (evdev) -----------------------

# evdev 전용: Insert/KP0/F8 만 처리
HOTKEY_MAP = {}
if ecodes is not None:
    HOTKEY_MAP = {
        ecodes.KEY_INSERT: "TOGGLE_SETTINGS",
        ecodes.KEY_KP0:    "TOGGLE_SETTINGS",  # NumLock off의 Insert 역할
        ecodes.KEY_F10:    "RESET",
        ecodes.KEY_F2:     "SPAWN_SEQ",
        ecodes.KEY_F8:     "SET_F8_SCALES",
    }

def _device_worker(dev: "InputDevice", q: "queue.Queue[str]"):
    try:
        for e in dev.read_loop():
            if e.type == ecodes.EV_KEY and e.value == 1:
                act = HOTKEY_MAP.get(e.code)
                if act:
                    q.put(act)
    except Exception:
        pass
    finally:
        try:
            dev.close()
        except Exception:
            pass

def start_hotkey_queue() -> "tuple[queue.Queue[str], list]":
    q: "queue.Queue[str]" = queue.Queue()
    workers = []

    if ecodes is None or list_devices is None:
        print("[hotkeys] evdev not available. Install python3-evdev or pip install evdev to enable global keys.")
        return q, workers

    for path in list_devices():
        try:
            dev = InputDevice(path)
            caps = dev.capabilities().get(ecodes.EV_KEY, [])
            supported = set()
            if isinstance(caps, dict):
                for v in caps.values():
                    supported.update(v if isinstance(v, list) else [v])
            else:
                supported.update(caps)
            if any(k in supported for k in HOTKEY_MAP.keys()):
                t = threading.Thread(target=_device_worker, args=(dev, q), daemon=True)
                t.start()
                workers.append((dev, t))
            else:
                dev.close()
        except Exception:
            pass

    if not workers:
        print("[hotkeys] No suitable input device found or permission denied. Try sudo or add user to 'input' group.")
    else:
        for dev, _ in workers:
            try:
                print(f"[hotkeys] Listening: {dev.path} - {dev.name}")
            except Exception:
                pass
    return q, workers

# ---------- 개선된 attach_window_hotkeys (콜백 체이닝 + 디버그) ----------
def attach_window_hotkeys(window, q: "queue.Queue[str]"):
    """
    Attach a GLFW key callback that pushes the same action strings into q.
    This implementation chains to any previous callback returned by glfw.set_key_callback.
    """
    if window is None:
        return

    GLWF_KEY_MAP = {
        glfw.KEY_INSERT: "TOGGLE_SETTINGS",
        glfw.KEY_KP_0:   "TOGGLE_SETTINGS",
        glfw.KEY_F10:    "RESET",
        glfw.KEY_F2:     "SPAWN_SEQ",
        glfw.KEY_F8:     "SET_F8_SCALES",
    }

    # define our callback which will call the previous callback if present
    def _key_cb(window_handle, key, scancode, action, mods):
        try:
            # debug line (remove or guard behind a verbose flag in production)
            # print(f"[kb] key={key} action={action} mods={mods}")
            if action == glfw.PRESS:
                act = GLWF_KEY_MAP.get(key)
                if act:
                    try:
                        q.put(act)
                        # quick debug:
                        print(f"[hotkeys] queued action: {act}")
                    except Exception:
                        pass
        except Exception as e:
            print("[hotkeys] callback error:", e)
        # Note: we intentionally don't return value; GLFW callbacks don't use return

    try:
        # set_key_callback returns the previous callback (or None)
        prev_cb = glfw.set_key_callback(window, _key_cb)
    except Exception:
        prev_cb = None

    # if there was a previous callback (e.g. GlfwRenderer), chain it by wrapping
    if prev_cb is not None:
        # create a wrapper that calls both our handler and the previous one
        def _chained_cb(window_handle, key, scancode, action, mods):
            try:
                _key_cb(window_handle, key, scancode, action, mods)
            except Exception:
                pass
            try:
                prev_cb(window_handle, key, scancode, action, mods)
            except Exception:
                pass
        try:
            glfw.set_key_callback(window, _chained_cb)
        except Exception:
            # if chaining fails, at least our handler is installed (prev_cb already replaced)
            pass
# ------------------------------------------------------------------


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

def draw_vertical_stack(draw_list, pos, size, displayed, max_scale, settings,
                        *, legend_side="left", legend_align="center",
                        legend_reverse=True):
    """
    Draw stacked vertical bar with legend and per-segment percent.
    Returns geometry dict: {"bar_top","bar_bottom","bar_x","bar_w","last_tip_x","last_tip_y"}
    """
    x, y = pos
    w, h = size

    # ---- 튜닝 노브 (settings) ----
    MARGIN = 12.0
    LEGEND_BOX = float(settings.get("legend_box", 16.0))
    LEGEND_GAP = float(settings.get("legend_gap", 8.0))
    LEGEND_ROW_PAD = 3.0
    LEGEND_RIGHT_PAD = float(settings.get("legend_right_pad", 2.0))
    LEGEND_BAR_GAP = float(settings.get("legend_bar_gap", 120.0))
    TICK_LABEL_LEFT = 46.0
    PCT_GAP = 8.0
    PCT_TOP_OFFSET = 0.0
    BAR_WIDTH_RATIO = float(settings.get("bar_width_ratio", 0.33))
    SHOW_PCT = bool(settings.get("show_segment_pct", True))
    PCT_DIGITS = int(settings.get("pct_digits", 1))
    LEGEND_TEXT_RATIO = float(settings.get("legend_text_ratio", 0.08))  # 화면 폭 비율
    # -------------------

    axis_color   = imgui.get_color_u32_rgba(1, 1, 1, 1)
    grid_color   = imgui.get_color_u32_rgba(0.6, 0.6, 0.6, 1)
    shadow_color = imgui.get_color_u32_rgba(0, 0, 0, 0.8)

    # 레전드 폭 상한/하한
    MAX_LEGEND_TEXT_PX = min(160.0, max(40.0, w * LEGEND_TEXT_RATIO))

    try:
        text_widths = [imgui.calc_text_size(s).x for s in STAGES]
        max_text_w = max(text_widths) if text_widths else 60.0
    except Exception:
        max_text_w = 60.0
    max_text_w = min(max_text_w, MAX_LEGEND_TEXT_PX)

    legend_w = LEGEND_BOX + LEGEND_GAP + max_text_w + LEGEND_RIGHT_PAD

    # 바 영역
    if legend_side == "left":
        legend_x = x + MARGIN
        bars_area_x = legend_x + legend_w + LEGEND_BAR_GAP
        bars_area_w = max(w - (bars_area_x - x) - MARGIN, 1.0)
        label_x_min = legend_x + legend_w + 6.0
    else:
        bars_area_x = x + MARGIN
        bars_area_w = max(w - (MARGIN * 2 + legend_w + LEGEND_BAR_GAP), 1.0)
        legend_x = bars_area_x + bars_area_w + LEGEND_BAR_GAP
        label_x_min = x + MARGIN + 6.0

    bars_area_h = max(h - MARGIN * 2, 10.0)

    # 바 크기/위치 (패널 정중앙 정렬)
    legend_h_buf = 26.0
    max_bar_h = max(h * 0.9 - legend_h_buf - MARGIN, 10.0)
    bar_w = max(bars_area_w * BAR_WIDTH_RATIO, 1.0)
    bar_x = x + (w - bar_w) * 0.5
    extra_bottom = max((h - (max_bar_h + legend_h_buf + MARGIN)) / 2.0, MARGIN / 2.0)
    bar_bottom = y + max_bar_h + extra_bottom
    bar_top = bar_bottom - max_bar_h

    # 외곽선
    draw_list.add_rect(bar_x, bar_top, bar_x + bar_w, bar_bottom, axis_color)

    # 합(퍼센트용)
    total_for_ratio = max(sum(displayed.values()), 1e-9)

    # 스택 (아래→위)
    current_y = bar_bottom
    panel_right = x + w - MARGIN
    last_seg_top = None

    for idx, stage in enumerate(STAGES):
        dur = displayed.get(stage, 0.0)
        ratio = (dur / max_scale) if max_scale > 0 else 0.0
        height = max_bar_h * min(max(ratio, 0.0), 1.0)
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)

        seg_top = current_y - height
        seg_bottom = current_y
        seg_cy = (seg_top + seg_bottom) * 0.5

        draw_list.add_rect_filled(bar_x, seg_top, bar_x + bar_w, seg_bottom, color)

        # 퍼센트 라벨(바 바로 옆)
        if SHOW_PCT and dur > 0.0 and total_for_ratio > 0.0:
            pct = (dur / total_for_ratio) * 100.0
            pct_text = f"{pct:.{PCT_DIGITS}f}%"
            try:
                tsize = imgui.calc_text_size(pct_text)
                tw, th = float(tsize.x), float(tsize.y)
            except Exception:
                tw, th = 36.0, 14.0
            tx = min(bar_x + bar_w + PCT_GAP, panel_right - tw)
            ty = seg_cy - th * 0.5 + PCT_TOP_OFFSET
            draw_list.add_text(tx + 1, ty + 1, shadow_color, pct_text)
            draw_list.add_text(tx, ty, axis_color, pct_text)

        if idx == len(STAGES) - 1:
            last_seg_top = seg_top

        current_y = seg_top

    # 눈금/라벨
    label_x = max(bar_x - TICK_LABEL_LEFT, label_x_min)
    for i in range(0, TICK_COUNT + 1):
        r = i / float(TICK_COUNT)
        ty = bar_bottom - (max_bar_h * r)
        draw_list.add_line(bar_x, ty, bar_x + bar_w, ty, grid_color)
        value = max_scale * r
        draw_list.add_text(label_x, max(ty - 7.0, y + 2.0), axis_color, format_label(value))

    # 레전드
    try:
        base_line = imgui.get_font_size()
    except Exception:
        base_line = 14.0
    line_h = max(LEGEND_BOX, base_line) + LEGEND_ROW_PAD * 2.0
    legend_total_h = line_h * len(STAGES)

    if legend_align == "top":
        legend_y = y + MARGIN
    elif legend_align == "bottom":
        legend_y = bar_bottom - legend_total_h
    else:
        legend_y = bar_top + (max_bar_h - legend_total_h) / 2.0
    legend_y = max(y + MARGIN, legend_y)

    order = list(STAGES)[::-1] if legend_reverse else list(STAGES)
    lx = legend_x; ly = legend_y
    for stage in order:
        c = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(lx, ly, lx + LEGEND_BOX, ly + LEGEND_BOX, c)
        draw_list.add_rect(lx, ly, lx + LEGEND_BOX, ly + LEGEND_BOX, axis_color)
        try:
            ts = imgui.calc_text_size(stage); ty = ly + (LEGEND_BOX - ts.y) / 2.0
        except Exception:
            ty = ly
        draw_list.add_text(lx + LEGEND_BOX + LEGEND_GAP, ty, axis_color, stage)
        ly += line_h

    # 반환
    return {
        "bar_top": float(bar_top),
        "bar_bottom": float(bar_bottom),
        "bar_x": float(bar_x),
        "bar_w": float(bar_w),
        "last_tip_x": float(bar_x + bar_w),
        "last_tip_y": float(last_seg_top) if last_seg_top is not None else None,
    }

def draw_horizontal_bars(draw_list, pos, size, displayed, max_scale, font,
                         label_anim, now_ts: float, label_anim_duration: float, settings=None):
    """
    Draw horizontal bars for Durations panel.
    settings: dict allowing overrides for margin, row_gap, label_gap, inside_pad_x
    """
    x, y = pos
    w, h = size

    # Use settings if provided, otherwise default values
    margin = float(settings.get("dur_margin", 12.0)) if settings else 12.0
    row_gap = float(settings.get("dur_row_gap", 6.0)) if settings else 6.0
    label_gap = float(settings.get("dur_label_gap", 10.0)) if settings else 10.0
    inside_pad_x = float(settings.get("dur_inside_pad_x", 8.0)) if settings else 8.0

    bar_x = x + margin
    bar_y = y + margin
    bar_w = max(w - margin * 2, 1.0)
    bar_h = max(h - margin * 2, 1.0)

    axis_color = imgui.get_color_u32_rgba(1, 1, 1, 1)
    shadow_color = imgui.get_color_u32_rgba(0, 0, 0, 0.8)
    grid_color = imgui.get_color_u32_rgba(0.6, 0.6, 0.6, 1)

    draw_list.add_rect(bar_x, bar_y, bar_x + bar_w, bar_y + bar_h, axis_color)

    # 세로 그리드 + 상단 눈금 라벨
    label_offset = 29
    for i in range(1, TICK_COUNT + 1):
        r = i / float(TICK_COUNT)
        tx = bar_x + bar_w * r
        draw_list.add_line(tx, bar_y, tx, bar_y + bar_h, grid_color)
        value = max_scale * r
        draw_list.add_text(tx - 15, max(bar_y - label_offset, y + 2) - 5, axis_color, format_label(value))

    bar_count = max(len(STAGES), 1)
    bar_height = bar_h / bar_count
    panel_right = x + w - margin

    for i, stage in enumerate(STAGES):
        dur = displayed.get(stage, 0.0)
        ratio = (dur / max_scale) if max_scale > 0 else 0
        bar_fill_width = bar_w * min(max(ratio, 0.0), 1.0)

        top = bar_y + (bar_count - 1 - i) * bar_height
        rect_top = top + row_gap
        rect_bottom = top + bar_height - row_gap

        # 바 채우기
        color = imgui.get_color_u32_rgba(*COLORS[stage], 1)
        draw_list.add_rect_filled(bar_x, rect_top, bar_x + bar_fill_width, rect_bottom, color)

        # 폰트(볼드) 푸시
        pushed = False
        if font is not None:
            imgui.push_font(font)
            pushed = True

        # 라벨
        text = f"{stage}: {dur:.3f}s"
        text_size = imgui.calc_text_size(text)
        text_y = top + (bar_height - text_size.y) / 2

        # outside/inside
        outside_x = bar_x + bar_fill_width + label_gap
        outside_x = min(outside_x, panel_right - text_size.x)

        inside_right = bar_x + bar_fill_width - inside_pad_x
        inside_x = inside_right - text_size.x
        inside_x = max(inside_x, bar_x + 5.0)

        fits_inside = bar_fill_width >= (text_size.x + inside_pad_x * 2)
        target = 1.0 if fits_inside else 0.0

        st = label_anim.get(stage)
        if st is None:
            st = {"alpha": 0.0, "active": False, "t0": 0.0, "from": 0.0, "to": 0.0, "target": 0.0}
            label_anim[stage] = st

        if st["target"] != target:
            st["target"] = target
            st["from"] = st["alpha"]
            st["to"] = target
            st["t0"] = now_ts
            st["active"] = True

        if st["active"]:
            t = (now_ts - st["t0"]) / max(label_anim_duration, 1e-9)
            if t >= 1.0:
                st["alpha"] = st["to"]
                st["active"] = False
            else:
                st["alpha"] = st["from"] + (st["to"] - st["from"]) * ease_out_cubic(t)

        alpha = st["alpha"]
        text_x = outside_x * (1.0 - alpha) + inside_x * alpha

        draw_list.add_text(text_x + 1, text_y + 1, shadow_color, text)
        draw_list.add_text(text_x, text_y, axis_color, text)

        if pushed:
            imgui.pop_font()

# ------------------------------- GUI Thread -----------------------------------

def gui_thread(log_lines, timeline, lock, hotkey_q: "queue.Queue[str]", hotkey_workers: list):
    global TICK_COUNT, ANIM_DURATION, LABEL_ANIM_DURATION, HEADROOM_FACTOR, SCALE_ADJUST_ALPHA
    global F8_VERTICAL_SECONDS, F8_HORIZONTAL_SECONDS, F8_ANIM_DURATION

    if not glfw.init():
        raise RuntimeError("glfw.init() failed")

    primary_monitor = glfw.get_primary_monitor()
    workarea = glfw.get_monitor_workarea(primary_monitor)
    screen_width, screen_height = workarea[2], workarea[3]
    initial_width, initial_height = int(screen_width / 2), int(screen_height / 2)

    window = glfw.create_window(initial_width, initial_height, WINDOW_TITLE, None, None)
    glfw.make_context_current(window)
    try:
        glfw.set_input_mode(window, glfw.STICKY_KEYS, glfw.TRUE)
    except Exception:
        pass

    # evdev 기반 워커가 없으면 윈도우 키콜백을 폴백으로 붙임
    try:
        if not hotkey_workers:
            attach_window_hotkeys(window, hotkey_q)
            print("[hotkeys] glfw key callback attached (evdev unavailable)")
        else:
            print("[hotkeys] evdev workers present; glfw callback not attached")
    except Exception:
        pass

    # ImGui
    imgui.create_context()
    io = imgui.get_io()

    # Fonts (한글 포함 glyph range 시도)
    regular_path = "fonts/SCDream5.otf"
    bold_path    = "fonts/SCDream9.otf"

    try:
        io.fonts.clear()
    except Exception:
        pass

    # 한글 glyph ranges 가능하면 얻기
    glyph_ranges = None
    try:
        glyph_ranges = io.fonts.get_glyph_ranges_korean()
    except Exception:
        glyph_ranges = None

    font_regular = None
    font_large_bold = None

    try:
        if os.path.exists(regular_path):
            # add_font_from_file_ttf signature may accept glyph_ranges as keyword
            try:
                font_regular = io.fonts.add_font_from_file_ttf(regular_path, 16, glyph_ranges=glyph_ranges)
            except TypeError:
                # fallback if signature differs
                font_regular = io.fonts.add_font_from_file_ttf(regular_path, 16)
        else:
            print("기본 폰트 파일 없음:", os.path.abspath(regular_path))
    except Exception as e:
        print("SCDream5.otf 로드 실패:", e)

    try:
        if os.path.exists(bold_path):
            try:
                font_large_bold = io.fonts.add_font_from_file_ttf(bold_path, 20, glyph_ranges=glyph_ranges)
            except TypeError:
                font_large_bold = io.fonts.add_font_from_file_ttf(bold_path, 20)
    except Exception as e:
        print("SCDream9.otf 로드 실패:", e)

    if font_regular is None:
        font_regular = io.fonts.add_font_default()

    impl = GlfwRenderer(window)
    try:
        impl.refresh_font_texture()
    except Exception:
        pass

    try:
        if not hotkey_workers:
            attach_window_hotkeys(window, hotkey_q)
            # 포커스 요청(테스트용)
            try:
                glfw.focus_window(window)
            except Exception:
                pass
            print("[hotkeys] glfw key callback attached (evdev unavailable)")
        else:
            print("[hotkeys] evdev workers present; glfw callback not attached")
    except Exception:
        pass

    no_resize_no_move_flags = imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE

    # 상태 변수
    displayed = {s: 0.0 for s in STAGES}
    anim_state = {s: {"running": False, "start_t": 0.0, "from": 0.0, "to": 0.0} for s in STAGES}

    # Label animation state per-stage
    label_anim = {
        s: {"alpha": 0.0, "active": False, "t0": 0.0, "from": 0.0, "to": 0.0, "target": 0.0}
        for s in STAGES
    }

    left_scale = 5.0
    right_scale = 5.0
    displayed_left_scale = float(left_scale)
    displayed_right_scale = float(right_scale)

    displayed_total = 0.0

    highlight_color = (42/255, 74/255, 122/255, 1.0)
    final_row_color = (0.9, 0.9, 0.4, 1.0)

    # F8 축 애니메이션 상태
    f8_v_anim = {"active": False, "t0": 0.0, "from": float(displayed_left_scale), "dur": float(F8_ANIM_DURATION)}
    f8_h_anim = {"active": False, "t0": 0.0, "from": float(displayed_right_scale), "dur": float(F8_ANIM_DURATION)}

    # Total 페이드인
    total_anim = {"active": False, "t0": 0.0, "dur": 0.45, "alpha": 0.0}
    prev_all_done = False

    # Settings (실시간 조정 가능한 값) + Defaults/Persistence
    settings = {
        "legend_bar_gap": 120.0,
        "legend_right_pad": 2.0,
        "legend_gap": 8.0,
        "legend_box": 16.0,
        "legend_text_ratio": 0.08,    # 레전드 텍스트 폭 비율 상한
        "bar_width_ratio": 0.33,      # 막대 폭 비율 (0.1~0.6 추천)
        "show_segment_pct": True,
        "pct_digits": 1,
        "total_x_pull": 120.0,        # Total 텍스트를 오른쪽에서 왼쪽으로 당기는 픽셀
        "total_anchor_gap": 10.0,     # 텍스트-선 간격
        "bend_offset": 24.0,          # 꺾임 x 오프셋
        "f8_unbounded": True,         # ⬅️ F8 스케일: DragFloat 모드 기본 켬
        "legend_side": "left",        # 왼쪽/오른쪽 선택 가능 ("left" / "right")
        # Durations layout settings (사용자 조정 가능)
        "dur_margin": 12.0,
        "dur_row_gap": 6.0,
        "dur_label_gap": 10.0,
        "dur_inside_pad_x": 8.0,
    }
    DEFAULT_SETTINGS = dict(settings)
    DEFAULT_GLOBALS = {
        "TICK_COUNT": int(TICK_COUNT),
        "ANIM_DURATION": float(ANIM_DURATION),
        "LABEL_ANIM_DURATION": float(LABEL_ANIM_DURATION),
        "HEADROOM_FACTOR": float(HEADROOM_FACTOR),
        "SCALE_ADJUST_ALPHA": float(SCALE_ADJUST_ALPHA),
        "F8_VERTICAL_SECONDS": F8_VERTICAL_SECONDS if F8_VERTICAL_SECONDS is None else float(F8_VERTICAL_SECONDS),
        "F8_HORIZONTAL_SECONDS": F8_HORIZONTAL_SECONDS if F8_HORIZONTAL_SECONDS is None else float(F8_HORIZONTAL_SECONDS),
        "F8_ANIM_DURATION": float(F8_ANIM_DURATION),
    }
    SETTINGS_FILE = "settings_ui.json"

    # --- Persistence helpers ---
    def _save_settings_to_file(path: str):
        payload = {
            "settings": settings,
            "globals": {
                "TICK_COUNT": int(TICK_COUNT),
                "ANIM_DURATION": float(ANIM_DURATION),
                "LABEL_ANIM_DURATION": float(LABEL_ANIM_DURATION),
                "HEADROOM_FACTOR": float(HEADROOM_FACTOR),
                "SCALE_ADJUST_ALPHA": float(SCALE_ADJUST_ALPHA),
                "F8_VERTICAL_SECONDS": F8_VERTICAL_SECONDS,
                "F8_HORIZONTAL_SECONDS": F8_HORIZONTAL_SECONDS,
                "F8_ANIM_DURATION": float(F8_ANIM_DURATION),
            }
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"[settings] saved -> {os.path.abspath(path)}")
            return True
        except Exception as e:
            print("[settings] save error:", e)
            return False

    def _load_settings_from_file(path: str):
        # need to assign to globals -> declare
        nonlocal f8_v_anim, f8_h_anim
        global TICK_COUNT, ANIM_DURATION, LABEL_ANIM_DURATION, HEADROOM_FACTOR, SCALE_ADJUST_ALPHA
        global F8_VERTICAL_SECONDS, F8_HORIZONTAL_SECONDS, F8_ANIM_DURATION
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except FileNotFoundError:
            print(f"[settings] not found: {path}")
            return False
        except Exception as e:
            print("[settings] load error:", e)
            return False

        # settings merge
        s = payload.get("settings", {})
        if isinstance(s, dict):
            for k, v in s.items():
                settings[k] = v

        # globals apply
        g = payload.get("globals", {})

        if "TICK_COUNT" in g:
            try:
                TICK_COUNT = int(g["TICK_COUNT"])
            except Exception:
                pass
        if "ANIM_DURATION" in g:
            try:
                ANIM_DURATION = float(g["ANIM_DURATION"])
            except Exception:
                pass
        if "LABEL_ANIM_DURATION" in g:
            try:
                LABEL_ANIM_DURATION = float(g["LABEL_ANIM_DURATION"])
            except Exception:
                pass
        if "HEADROOM_FACTOR" in g:
            try:
                HEADROOM_FACTOR = float(g["HEADROOM_FACTOR"])
            except Exception:
                pass
        if "SCALE_ADJUST_ALPHA" in g:
            try:
                SCALE_ADJUST_ALPHA = float(g["SCALE_ADJUST_ALPHA"])
            except Exception:
                pass

        # F8 presets
        if "F8_VERTICAL_SECONDS" in g:
            F8_VERTICAL_SECONDS = g["F8_VERTICAL_SECONDS"]
            if F8_VERTICAL_SECONDS is not None:
                try:
                    F8_VERTICAL_SECONDS = float(F8_VERTICAL_SECONDS)
                except Exception:
                    pass
        if "F8_HORIZONTAL_SECONDS" in g:
            F8_HORIZONTAL_SECONDS = g["F8_HORIZONTAL_SECONDS"]
            if F8_HORIZONTAL_SECONDS is not None:
                try:
                    F8_HORIZONTAL_SECONDS = float(F8_HORIZONTAL_SECONDS)
                except Exception:
                    pass
        if "F8_ANIM_DURATION" in g:
            try:
                F8_ANIM_DURATION = float(g["F8_ANIM_DURATION"])
            except Exception:
                pass

        # reflect new F8 anim durations in anim state holders
        try:
            f8_v_anim["dur"] = float(F8_ANIM_DURATION)
            f8_h_anim["dur"] = float(F8_ANIM_DURATION)
        except Exception:
            pass

        print(f"[settings] loaded <- {os.path.abspath(path)}")
        return True

    def _load_defaults():
        nonlocal f8_v_anim, f8_h_anim
        global TICK_COUNT, ANIM_DURATION, LABEL_ANIM_DURATION, HEADROOM_FACTOR, SCALE_ADJUST_ALPHA
        global F8_VERTICAL_SECONDS, F8_HORIZONTAL_SECONDS, F8_ANIM_DURATION
        # settings
        for k, v in DEFAULT_SETTINGS.items():
            settings[k] = v
        # globals
        TICK_COUNT = int(DEFAULT_GLOBALS["TICK_COUNT"])
        ANIM_DURATION = float(DEFAULT_GLOBALS["ANIM_DURATION"])
        LABEL_ANIM_DURATION = float(DEFAULT_GLOBALS["LABEL_ANIM_DURATION"])
        HEADROOM_FACTOR = float(DEFAULT_GLOBALS["HEADROOM_FACTOR"])
        SCALE_ADJUST_ALPHA = float(DEFAULT_GLOBALS["SCALE_ADJUST_ALPHA"])
        # F8
        F8_VERTICAL_SECONDS = DEFAULT_GLOBALS["F8_VERTICAL_SECONDS"]
        F8_HORIZONTAL_SECONDS = DEFAULT_GLOBALS["F8_HORIZONTAL_SECONDS"]
        F8_ANIM_DURATION = float(DEFAULT_GLOBALS["F8_ANIM_DURATION"])
        f8_v_anim["dur"] = float(F8_ANIM_DURATION)
        f8_h_anim["dur"] = float(F8_ANIM_DURATION)
        print("[settings] loaded defaults.")

    # 실행 초기에 1회 자동 로드
    if os.path.exists(SETTINGS_FILE):
        _load_settings_from_file(SETTINGS_FILE)

    # Settings window toggle (evdev 전용)
    settings_visible = False
    want_focus_settings = False

    # 핫키 적용 함수들
    def _apply_f8_scales():
        nonlocal left_scale, right_scale
        now_local = time.time()
        if F8_VERTICAL_SECONDS is not None:
            try:
                left_scale = float(F8_VERTICAL_SECONDS)
                f8_v_anim.update({"active": True, "t0": now_local, "from": float(displayed_left_scale), "dur": float(F8_ANIM_DURATION)})
                print(f"[F8] vertical exact -> {left_scale:.1f}s (anim {F8_ANIM_DURATION}s)")
            except Exception as e:
                print("[F8] failed to set vertical scale:", e)
        if F8_HORIZONTAL_SECONDS is not None:
            try:
                right_scale = float(F8_HORIZONTAL_SECONDS)
                f8_h_anim.update({"active": True, "t0": now_local, "from": float(displayed_right_scale), "dur": float(F8_ANIM_DURATION)})
                print(f"[F8] horizontal exact -> {right_scale:.1f}s (anim {F8_ANIM_DURATION}s)")
            except Exception as e:
                print("[F8] failed to set horizontal scale:", e)

    # CollapsingHeader 기본 오픈 플래그(버전 호환)
    TREE_NODE_DEFAULT_OPEN = getattr(imgui, "TREE_NODE_DEFAULT_OPEN", 1 << 5)

    while not glfw.window_should_close(window):
        glfw.poll_events()
        impl.process_inputs()
        imgui.new_frame()

        if font_regular is not None:
            imgui.push_font(font_regular)

        # process global hotkey queue (non-blocking, evdev-only)
        try:
            while True:
                action = hotkey_q.get_nowait()
                if action == "RESET":
                    # --- 타임라인/로그 초기화 (manual F10) ---
                    with lock:
                        for stage in STAGES:
                            timeline[stage]["start"] = None
                            timeline[stage]["end"] = None
                        log_lines.clear()
                    for s in STAGES:
                        displayed[s] = 0.0
                        anim_state[s]["running"] = False

                    # --- F8 프리셋으로 스케일 초기화(0.5 step, 범위 Clamp) ---
                    def _q05_in_range(x, lo, hi):
                        if x is None:
                            return None
                        try:
                            v = round(float(x) * 2.0) / 2.0  # 0.5 단위
                            if lo is not None: v = max(lo, v)
                            if hi is not None: v = min(hi, v)
                            return v
                        except Exception:
                            return None

                    _ls = _q05_in_range(F8_VERTICAL_SECONDS,   1.0, 60.0)
                    _rs = _q05_in_range(F8_HORIZONTAL_SECONDS, 1.0, 40.0)

                    # F8 값이 없으면 기존 스케일 유지 (이전처럼 고정 5.0 사용 안 함)
                    left_scale  = float(_ls if _ls is not None else left_scale)
                    right_scale = float(_rs if _rs is not None else right_scale)

                    # 표시 스케일도 바로 동기화
                    displayed_left_scale = float(left_scale)
                    displayed_right_scale = float(right_scale)
                    displayed_total = 0.0

                    # 애니메이션/라벨 상태 리셋
                    f8_v_anim.update({"active": False, "t0": 0.0, "from": float(left_scale),  "dur": float(F8_ANIM_DURATION)})
                    f8_h_anim.update({"active": False, "t0": 0.0, "from": float(right_scale), "dur": float(F8_ANIM_DURATION)})
                    for s in STAGES:
                        label_anim[s].update({"alpha": 0.0, "active": False, "t0": 0.0, "from": 0.0, "to": 0.0, "target": 0.0})
                    total_anim.update({"active": False, "t0": 0.0, "alpha": 0.0})
                    prev_all_done = False

                    print(f"[reset] scales <- F8 presets | left={left_scale:.1f}s, right={right_scale:.1f}s")
                elif action == "AUTO_RESET":
                    # AUTO_RESET: start-pattern 감지로 자동 초기화 + 첫 스테이지 시작
                    # (단, 방금 감지된 로그 라인(마지막 항목)은 보존한다)
                    with lock:
                        # 보존할 마지막 로그 라인 캡처
                        preserved = None
                        try:
                            if log_lines:
                                preserved = log_lines[-1]
                        except Exception:
                            preserved = None

                        # 타임라인 초기화
                        for stage in STAGES:
                            timeline[stage]["start"] = None
                            timeline[stage]["end"] = None

                        # 로그는 초기화하되, 마지막(감지된) 라인은 복원
                        log_lines.clear()
                        if preserved is not None:
                            log_lines.append(preserved)

                    # displayed / anim 초기화
                    for s in STAGES:
                        displayed[s] = 0.0
                        anim_state[s]["running"] = False

                    # 0.5 단위 양자화 헬퍼 (범위 클램프 포함)
                    def _q05_in_range(x, lo, hi):
                        if x is None:
                            return None
                        try:
                            v = round(float(x) * 2.0) / 2.0  # 0.5 단위
                            if lo is not None: v = max(lo, v)
                            if hi is not None: v = min(hi, v)
                            return v
                        except Exception:
                            return None

                    _ls = _q05_in_range(F8_VERTICAL_SECONDS,   1.0, 60.0)
                    _rs = _q05_in_range(F8_HORIZONTAL_SECONDS, 1.0, 40.0)

                    left_scale  = float(_ls if _ls is not None else left_scale)
                    right_scale = float(_rs if _rs is not None else right_scale)

                    displayed_left_scale = float(left_scale)
                    displayed_right_scale = float(right_scale)
                    displayed_total = 0.0

                    f8_v_anim.update({"active": False, "t0": 0.0, "from": float(left_scale),  "dur": float(F8_ANIM_DURATION)})
                    f8_h_anim.update({"active": False, "t0": 0.0, "from": float(right_scale), "dur": float(F8_ANIM_DURATION)})
                    for s in STAGES:
                        label_anim[s].update({"alpha": 0.0, "active": False, "t0": 0.0, "from": 0.0, "to": 0.0, "target": 0.0})
                    total_anim.update({"active": False, "t0": 0.0, "alpha": 0.0})
                    prev_all_done = False

                    # 자동 시작: 첫 스테이지 start 바로 세팅
                    with lock:
                        if STAGES:
                            timeline[STAGES[0]]["start"] = time.time()

                    print(f"[auto] start-pattern detected -> reset & start | left={left_scale:.1f}s, right={right_scale:.1f}s")
                elif action == "SPAWN_SEQ":
                    print("[hotkeys] F2 pressed -> spawn test sequence")
                    spawn_test_sequence(LOG_FILE, total_sec=5.0)
                elif action == "SET_F8_SCALES":
                    _apply_f8_scales()
                elif action == "TOGGLE_SETTINGS":
                    settings_visible = not settings_visible
                    if settings_visible:
                        want_focus_settings = True
                    print(f"[hotkeys] Settings {'shown' if settings_visible else 'hidden'} (evdev/glfw)")
        except queue.Empty:
            pass

        now = time.time()
        width, height = glfw.get_framebuffer_size(window)
        half_w = width / 2
        half_h = height / 2

        with lock:
            timeline_copy = {s: dict(timeline[s]) for s in STAGES}

        # 완료 여부
        all_done = all(timeline_copy[s].get("start") and timeline_copy[s].get("end") for s in STAGES)

        # targets
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

        # update displayed
        for s in STAGES:
            start = timeline_copy[s].get("start")
            end = timeline_copy[s].get("end")
            if start is None:
                displayed[s] = 0.0
                anim_state[s]["running"] = False
            elif end is None:
                displayed[s] = targets[s]
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

        # dynamic scales
        durations_now = compute_durations(timeline_copy)
        target_total = sum(durations_now.values())
        needed_left = max(sum(displayed.values()), target_total, 1e-9)
        needed_right = max(max(displayed.values()) if displayed else 1.0, 1e-9)

        if needed_left > left_scale * 0.999999:
            left_scale = closer_nice_max(needed_left)
        if needed_right > right_scale * 0.999999:
            right_scale = closer_nice_max(needed_right)

        # scale smoothing
        if f8_v_anim["active"]:
            t = (now - f8_v_anim["t0"]) / max(f8_v_anim["dur"], 1e-9)
            if t >= 1.0:
                displayed_left_scale = float(left_scale)
                f8_v_anim["active"] = False
            else:
                displayed_left_scale = f8_v_anim["from"] + (left_scale - f8_v_anim["from"]) * ease_out_cubic(t)
        else:
            displayed_left_scale += (left_scale - displayed_left_scale) * SCALE_ADJUST_ALPHA

        if f8_h_anim["active"]:
            t = (now - f8_h_anim["t0"]) / max(f8_h_anim["dur"], 1e-9)
            if t >= 1.0:
                displayed_right_scale = float(right_scale)
                f8_h_anim["active"] = False
            else:
                displayed_right_scale = f8_h_anim["from"] + (right_scale - f8_h_anim["from"]) * ease_out_cubic(t)
        else:
            displayed_right_scale += (right_scale - displayed_right_scale) * SCALE_ADJUST_ALPHA

        # animated total number
        needed_total = sum(displayed.values())
        displayed_total += (needed_total - displayed_total) * SCALE_ADJUST_ALPHA

        # --- UI ---
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND, *highlight_color)
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND_ACTIVE, *highlight_color)
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND_COLLAPSED, *highlight_color)

        # Log (top-left)  <-- swapped: Log left
        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Log", False)
        with lock:
            lines_snapshot = list(log_lines)
        imgui.begin_child("log_child", 0, 0, border=True)
        imgui.push_style_var(imgui.STYLE_ITEM_SPACING, (0, 0))
        for line in lines_snapshot:
            imgui.text_unformatted(line)
        if lines_snapshot:
            try:
                imgui.set_scroll_here_y(1.0)
            except Exception:
                try:
                    imgui.set_scroll_here_y()
                except Exception:
                    pass
        imgui.pop_style_var()
        imgui.end_child()
        imgui.end()

        # Summary (top-right) <-- swapped: Summary right
        imgui.set_next_window_position(half_w, 0)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Summary", False)
        imgui.text("Subject Profile result")
        if imgui.begin_table("summary_table", 5):
            imgui.table_setup_column("Stage")
            imgui.table_setup_column("Start")
            imgui.table_setup_column("End")
            imgui.table_setup_column("Duration")
            imgui.table_setup_column("Ratio")
            imgui.table_headers_row()
            total_for_ratio = sum(displayed.values()) or 1.0

            for stage in STAGES:
                sstart = timeline_copy[stage].get("start")
                send   = timeline_copy[stage].get("end")
                if sstart and send:
                    dur = max(0.0, send - sstart)
                elif sstart and not send:
                    dur = max(0.0, now - sstart)
                else:
                    dur = 0.0
                ratio = (dur / total_for_ratio) * 100.0

                imgui.table_next_row()
                imgui.table_next_column(); imgui.text(stage)
                imgui.table_next_column(); imgui.text(fmt_hms_hundredths(sstart))
                imgui.table_next_column(); imgui.text(fmt_hms_hundredths(send))
                imgui.table_next_column(); imgui.text(f"{dur:.2f}s")
                imgui.table_next_column(); imgui.text(f"{ratio:.1f}%")

            # TOTAL
            if all_done:
                total_dur = sum((timeline_copy[s]["end"] - timeline_copy[s]["start"]) for s in STAGES)
                starts = [timeline_copy[s]["start"] for s in STAGES if timeline_copy[s].get("start")]
                ends   = [timeline_copy[s]["end"]   for s in STAGES if timeline_copy[s].get("end")]
                first_ts = min(starts) if starts else None
                last_ts  = max(ends)   if ends   else None

                imgui.table_next_row()
                imgui.push_style_color(imgui.COLOR_TEXT, *final_row_color)
                imgui.table_next_column(); imgui.text("TOTAL")
                imgui.table_next_column(); imgui.text(fmt_hms_hundredths(first_ts))
                imgui.table_next_column(); imgui.text(fmt_hms_hundredths(last_ts))
                imgui.table_next_column(); imgui.text(f"{total_dur:.2f}s")
                imgui.table_next_column(); imgui.text("100.0%")
                imgui.pop_style_color()
            imgui.end_table()
        imgui.end()

        # Total (bottom-left)
        imgui.set_next_window_position(0, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Total", False, no_resize_no_move_flags)
        avail_w, avail_h = get_content_region_avail_safe()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()

        panel_x, panel_y = pos
        panel_w, panel_h = float(avail_w), float(avail_h)
        pad = 8.0

        # 1) 스택 먼저 그려서 기하 정보 획득 (settings 반영)
        geom = draw_vertical_stack(
            draw_list, pos, (avail_w, avail_h),
            displayed, displayed_left_scale, settings,
            legend_side=settings.get("legend_side", "left"),
            legend_align="center",
            legend_reverse=True
        )

        # 2) Total 페이드인 트리거/리셋
        now_ts = time.time()
        if all_done and not prev_all_done:
            total_anim.update({"active": True, "t0": now_ts})
            prev_all_done = True
        elif not all_done:
            total_anim.update({"active": False, "alpha": 0.0})
            prev_all_done = False

        # 3) 알파 업데이트
        alpha = total_anim["alpha"]
        if total_anim["active"]:
            t = (now_ts - total_anim["t0"]) / max(total_anim["dur"], 1e-9)
            if t >= 1.0:
                alpha = 1.0
                total_anim["active"] = False
            else:
                alpha = ease_out_cubic(max(0.0, min(1.0, t)))
            total_anim["alpha"] = alpha

        # 4) 완료 상태에서만 Total + 연결선 표시
        if alpha > 0.0:
            total_text = f"Total: {displayed_total:.2f}s"
            try:
                text_size = imgui.calc_text_size(total_text, False, -1)
                text_w, text_h = float(text_size.x), float(text_size.y)
            except Exception:
                text_w, text_h = (len(total_text) * 8.0), 16.0

            TOTAL_X_PULL = float(settings.get("total_x_pull", 120.0))
            TOTAL_ANCHOR_GAP = float(settings.get("total_anchor_gap", 10.0))
            BEND_OFFSET = float(settings.get("bend_offset", 24.0))

            # 오른쪽 정렬 기준에서 왼쪽으로 끌어오기
            tx = max(panel_x + pad, panel_x + panel_w - pad - text_w - TOTAL_X_PULL)
            # bar_top보다 약간 아래
            ty = max(panel_y + pad, geom["bar_top"] + 8.0)
            anchor_x = tx - TOTAL_ANCHOR_GAP
            anchor_y = ty + text_h * 0.5

            if geom.get("last_tip_y") is not None:
                tip_x = geom["last_tip_x"]
                tip_y = geom["last_tip_y"]
                bend_x = min(tip_x + BEND_OFFSET, anchor_x - 6.0)
                bend_y = anchor_y

                line_col = imgui.get_color_u32_rgba(1, 1, 1, 0.6 * alpha)
                dot_col  = imgui.get_color_u32_rgba(1, 1, 1, 0.85 * alpha)

                draw_list.add_line(tip_x,  tip_y, bend_x, bend_y, line_col, 1.6)
                draw_list.add_line(bend_x, bend_y, anchor_x, anchor_y, line_col, 1.6)
                try:
                    draw_list.add_circle_filled(tip_x, tip_y, 2.0, dot_col)
                except Exception:
                    pass

            bold_pushed = False
            if font_large_bold is not None:
                imgui.push_font(font_large_bold)
                bold_pushed = True

            draw_list.add_text(tx + 1, ty + 1, imgui.get_color_u32_rgba(0, 0, 0, 0.7 * alpha), total_text)
            draw_list.add_text(tx, ty, imgui.get_color_u32_rgba(1, 1, 1, alpha), total_text)

            if bold_pushed:
                imgui.pop_font()

        imgui.end()

        # Durations (bottom-right)
        imgui.set_next_window_position(half_w, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Durations", False, no_resize_no_move_flags)
        avail_w, avail_h = get_content_region_avail_safe()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        draw_horizontal_bars(
            draw_list, pos, (avail_w, avail_h),
            displayed, displayed_right_scale,
            font_large_bold,
            label_anim, time.time(), LABEL_ANIM_DURATION,
            settings=settings
        )
        imgui.end()

        # -------------------- Settings (항상 마지막에 그려 최상위 유지) --------------------
        if settings_visible:
            if want_focus_settings:
                imgui.set_next_window_focus()
                # 처음 표시될 때 안전한 위치/크기 지정 (COND_APPEARING 없는 환경 고려)
                imgui.set_next_window_position(20, 20)
                imgui.set_next_window_size(420, 0)
                want_focus_settings = False

            flags = imgui.WINDOW_ALWAYS_AUTO_RESIZE | imgui.WINDOW_NO_COLLAPSE
            # X(닫기 버튼) 제거: closable=False 로 호출
            imgui.begin("Settings", False, flags)

            # ---- (버전 호환) 섹션 시작/종료 래퍼 ----
            TREE_NODE_DEFAULT_OPEN = getattr(imgui, "TREE_NODE_DEFAULT_OPEN", 1 << 5)

            def begin_section(label: str, default_open: bool = True):
                fl = TREE_NODE_DEFAULT_OPEN if default_open else 0
                if hasattr(imgui, "tree_node_ex"):
                    # 신버전: bool 하나 반환
                    opened = bool(imgui.tree_node_ex(label, fl))
                    return opened, True   # True => 나중에 tree_pop 필요
                else:
                    # 구버전: collapsing_header 가 (expanded, visible) 튜플을 반환할 수 있음
                    try:
                        res = imgui.collapsing_header(label, flags=fl)
                    except TypeError:
                        res = imgui.collapsing_header(label)
                    opened = res[0] if isinstance(res, tuple) else bool(res)
                    return opened, False  # False => tree_pop 불필요

            def end_section(need_pop: bool):
                if need_pop and hasattr(imgui, "tree_pop"):
                    imgui.tree_pop()
            # --------------------------------------

            # Layout
            opened, pop = begin_section("Layout / Legend / Bar", True)
            if opened:
                changed, v = imgui.slider_float("Legend-Bar Gap (px)", settings["legend_bar_gap"], 0.0, 240.0, "%.0f")
                if changed: settings["legend_bar_gap"] = v
                changed, v = imgui.slider_float("Legend Right Pad (px)", settings["legend_right_pad"], 0.0, 30.0, "%.0f")
                if changed: settings["legend_right_pad"] = v
                changed, v = imgui.slider_float("Legend Text Max Ratio", settings["legend_text_ratio"], 0.02, 0.20, "%.2f")
                if changed: settings["legend_text_ratio"] = v
                changed, v = imgui.slider_float("Legend Box (px)", settings["legend_box"], 10.0, 28.0, "%.0f")
                if changed: settings["legend_box"] = v
                changed, v = imgui.slider_float("Legend Gap (px)", settings["legend_gap"], 0.0, 24.0, "%.0f")
                if changed: settings["legend_gap"] = v
                changed, v = imgui.slider_float("Bar Width Ratio", settings["bar_width_ratio"], 0.10, 0.60, "%.2f")
                if changed: settings["bar_width_ratio"] = v

                # Legend 위치 선택 (left / right)
                # radio_button은 클릭되면 True를 반환하므로 이를 통해 선택 토글
                if imgui.radio_button("Legend: Left", settings.get("legend_side", "left") == "left"):
                    settings["legend_side"] = "left"
                imgui.same_line()
                if imgui.radio_button("Legend: Right", settings.get("legend_side", "left") == "right"):
                    settings["legend_side"] = "right"

                end_section(pop)

            # Percent labels
            opened, pop = begin_section("Segment % Label", True)
            if opened:
                changed, v = imgui.checkbox("Show segment %", settings["show_segment_pct"])
                if changed: settings["show_segment_pct"] = v
                changed, v = imgui.slider_int("Percent digits", settings["pct_digits"], 0, 2)
                if changed: settings["pct_digits"] = int(v)
                end_section(pop)

            # Ticks / Animation
            opened, pop = begin_section("Ticks / Animation", True)
            if opened:
                changed, v = imgui.slider_int("Tick count", int(TICK_COUNT), 3, 12)
                if changed: TICK_COUNT = int(v)
                changed, v = imgui.slider_float("Stage anim duration (s)", float(ANIM_DURATION), 0.05, 2.0, "%.2f")
                if changed: ANIM_DURATION = float(v)
                changed, v = imgui.slider_float("Label anim duration (s)", float(LABEL_ANIM_DURATION), 0.05, 1.0, "%.2f")
                if changed: LABEL_ANIM_DURATION = float(v)
                changed, v = imgui.slider_float("Headroom factor", float(HEADROOM_FACTOR), 1.00, 1.50, "%.2f")
                if changed: HEADROOM_FACTOR = float(v)
                changed, v = imgui.slider_float("Scale smoothing alpha", float(SCALE_ADJUST_ALPHA), 0.01, 0.50, "%.2f")
                if changed: SCALE_ADJUST_ALPHA = float(v)
                end_section(pop)

            # Total label & connector
            opened, pop = begin_section("Total Label / Connector", True)
            if opened:
                changed, v = imgui.slider_float("Total X Pull (px)", settings["total_x_pull"], 0.0, 300.0, "%.0f")
                if changed: settings["total_x_pull"] = v
                changed, v = imgui.slider_float("Total-Anchor Gap (px)", settings["total_anchor_gap"], 4.0, 40.0, "%.0f")
                if changed: settings["total_anchor_gap"] = v
                changed, v = imgui.slider_float("Bend Offset (px)", settings["bend_offset"], 8.0, 80.0, "%.0f")
                if changed: settings["bend_offset"] = v
                end_section(pop)

            # Durations Bar Layout
            opened, pop = begin_section("Durations Bar Layout", True)
            if opened:
                changed, v = imgui.slider_float("Durations Margin (px)", settings["dur_margin"], 0.0, 48.0, "%.0f")
                if changed: settings["dur_margin"] = v
                changed, v = imgui.slider_float("Row Gap (px)", settings["dur_row_gap"], 0.0, 24.0, "%.0f")
                if changed: settings["dur_row_gap"] = v
                changed, v = imgui.slider_float("Label Gap (px)", settings["dur_label_gap"], 0.0, 64.0, "%.0f")
                if changed: settings["dur_label_gap"] = v
                changed, v = imgui.slider_float("Inside Pad X (px)", settings["dur_inside_pad_x"], 0.0, 32.0, "%.0f")
                if changed: settings["dur_inside_pad_x"] = v
                imgui.text("These values control bar margin, spacing and label placement in Durations panel.")
                end_section(pop)

            # F8 preset scales (0.5-step)
            opened, pop = begin_section("F8 Scales (Exact Seconds)", True)
            if opened:
                # 0.5 단위 양자화 헬퍼
                def _q05(x: float) -> float:
                    try:
                        return round(float(x) * 2.0) / 2.0
                    except Exception:
                        return 0.0

                # 모드 선택: 제한 없는 DragFloat vs 범위 지정 Slider
                changed, settings["f8_unbounded"] = imgui.checkbox(
                    "Unbounded (use DragFloat)", settings.get("f8_unbounded", False)
                )

                if settings["f8_unbounded"]:
                    # ---- DragFloat: 제한 해제, 0.5 단위로 강제 양자화 ----
                    curL = F8_VERTICAL_SECONDS if isinstance(F8_VERTICAL_SECONDS, (int, float)) else float(displayed_left_scale)
                    curR = F8_HORIZONTAL_SECONDS if isinstance(F8_HORIZONTAL_SECONDS, (int, float)) else float(displayed_right_scale)

                    ch, curL = imgui.drag_float("Left scale seconds", float(curL),
                                                change_speed=0.3,  # 민감도 낮춤
                                                min_value=0.0, max_value=0.0,
                                                format="%.1f")
                    if ch:
                        F8_VERTICAL_SECONDS = max(0.0, _q05(curL))

                    ch, curR = imgui.drag_float("Right scale seconds", float(curR),
                                                change_speed=0.12,  # 민감도 낮춤
                                                min_value=0.0, max_value=0.0,
                                                format="%.1f")
                    if ch:
                        F8_HORIZONTAL_SECONDS = max(0.0, _q05(curR))
                else:
                    # ---- SliderFloat: 좌 1~60, 우 1~40, 0.5 단위로 양자화 ----
                    lval = F8_VERTICAL_SECONDS if isinstance(F8_VERTICAL_SECONDS, (int, float)) else float(displayed_left_scale)
                    rval = F8_HORIZONTAL_SECONDS if isinstance(F8_HORIZONTAL_SECONDS, (int, float)) else float(displayed_right_scale)

                    ch, lval = imgui.slider_float("Left scale seconds", float(lval), 1.0, 60.0, "%.1f")
                    if ch:
                        lval = _q05(lval)
                        F8_VERTICAL_SECONDS = min(60.0, max(1.0, lval))

                    ch, rval = imgui.slider_float("Right scale seconds", float(rval), 1.0, 40.0, "%.1f")
                    if ch:
                        rval = _q05(rval)
                        F8_HORIZONTAL_SECONDS = min(40.0, max(1.0, rval))

                # 애니메이션 시간은 자유(스텝 제한 없음)
                changed, v = imgui.slider_float("F8 anim duration (s)", float(F8_ANIM_DURATION), 0.05, 2.0, "%.2f")
                if changed:
                    F8_ANIM_DURATION = float(v)
                    f8_v_anim["dur"] = float(F8_ANIM_DURATION)
                    f8_h_anim["dur"] = float(F8_ANIM_DURATION)

                if imgui.button("Apply now"):
                    _apply_f8_scales()
                imgui.same_line()
                if hasattr(imgui, "text_disabled"):
                    imgui.text_disabled("(apply F8 preset scales)")
                else:
                    imgui.text("(apply F8 preset scales)")
                end_section(pop)


            # Persistence
            opened, pop = begin_section("Persistence / Presets", True)
            if opened:
                imgui.text("settings_ui.json 에 저장/불러오기")
                if imgui.button("Save"):
                    _save_settings_to_file(SETTINGS_FILE)
                imgui.same_line()
                if imgui.button("Load"):
                    _load_settings_from_file(SETTINGS_FILE)
                imgui.same_line()
                if imgui.button("Load defaults"):
                    _load_defaults()
                end_section(pop)

            imgui.end()
        # ----------------------------------------------------------------------------

        imgui.pop_style_color(3)

        if font_regular is not None:
            imgui.pop_font()
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

    # start hotkey monitor
    hotkey_q, workers = start_hotkey_queue()

    # pass hotkey_q/control_q to log_thread so it can emit AUTO_RESET on start-pattern
    t = threading.Thread(target=log_thread, args=(log_lines, timeline, lock, hotkey_q), daemon=True)
    t.start()

    gui_thread(log_lines, timeline, lock, hotkey_q, workers)

if __name__ == "__main__":
    main()
