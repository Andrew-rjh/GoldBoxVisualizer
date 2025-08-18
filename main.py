import threading
import time
import re
import os
import math
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

# 애니메이션/시각화 파라미터
ANIM_DURATION = 0.6        # 스테이지 완료 후 최종값으로 보간하는 시간(초)
TICK_COUNT = 8             # 눈금 수
HEADROOM_FACTOR = 1.05     # 기준점 최소 여유 (5%)
SCALE_ADJUST_ALPHA = 0.18  # 스케일 보간 속도 (0-1) — 클수록 빠르게 새 스케일에 도달

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

def closer_nice_max(value):
    """value보다 약간 큰(HEADROOM_FACTOR) 값에 대해 타이트한 '보기 좋은' 상한을 반환."""
    if value <= 0:
        return 1.0
    target = value * HEADROOM_FACTOR
    exp = math.floor(math.log10(max(target, 1e-12)))
    mag = 10 ** exp
    # 촘촘한 후보들
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

def log_thread(log_lines, timeline, lock):
    start_time = None
    current_stage = None

    def process_line(raw_line: str):
        nonlocal start_time, current_stage
        line = raw_line.replace("\t", " ").strip()
        if not line:
            return

        now = time.time()
        ts = time.strftime('%H:%M:%S', time.localtime(now))
        new_line = f"{ts} - {line}"

        with lock:
            if not log_lines or log_lines[-1] != new_line:
                log_lines.append(new_line)

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
    """아래가 0(바닥)인 세로 스택: 바는 아래에서 위로 쌓임.
       max_scale은 외부에서 관리되는 스케일(동적 확장된 값)입니다."""
    x, y = pos
    w, h = size
    margin = 12.0
    legend_h = 26.0

    max_bar_h = max(h * 0.9 - legend_h - margin, 10.0)
    bar_w = max((w - margin * 2) / 3.0, 1.0)

    # 바텀 좌표(바닥)
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

    # 눈금 (바닥->위 방향)
    label_x = max(bar_x - 50, x + 6)
    for i in range(0, TICK_COUNT + 1):
        r = i / float(TICK_COUNT)
        ty = bar_bottom - (max_bar_h * r)
        draw_list.add_line(bar_x, ty, bar_x + bar_w, ty, grid_color)
        value = max_scale * r
        draw_list.add_text(label_x, ty - 7, axis_color, format_label(value))

    # 범례
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

    # 촘촘한 수평 눈금
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
        
        # 텍스트 렌더링 (위치 동적 조정 및 그림자 추가)
        imgui.push_font(font)
        text = f"{stage}: {dur:.3f}s"
        text_size = imgui.calc_text_size(text)
        text_y = top + (bar_height - text_size.y) / 2
        
        # 바깥쪽에 표시할 때의 기본 x 위치
        text_x_outside = bar_x + bar_fill_width + 10

        # 텍스트가 패널을 벗어나는지 확인
        if (text_x_outside + text_size.x) > (x + w - margin):
            # 안쪽에 표시할 때의 x 위치
            text_x_inside = bar_x + bar_fill_width - text_size.x - 10
            # 텍스트가 막대그래프의 시작점보다 왼쪽에 그려지는 것을 방지
            text_x = max(bar_x + 5, text_x_inside)
        else:
            text_x = text_x_outside

        # 그림자 효과
        draw_list.add_text(text_x + 1, text_y + 1, shadow_color, text)
        # 실제 텍스트
        draw_list.add_text(text_x, text_y, axis_color, text)
        imgui.pop_font()

def gui_thread(log_lines, timeline, lock):
    if not glfw.init():
        raise RuntimeError("glfw.init() failed")

    # 창 크기를 화면의 1/4로 설정
    primary_monitor = glfw.get_primary_monitor()
    workarea = glfw.get_monitor_workarea(primary_monitor)
    screen_width, screen_height = workarea[2], workarea[3]
    initial_width, initial_height = int(screen_width / 2), int(screen_height / 2)
    
    window = glfw.create_window(initial_width, initial_height, "Boot Profiler", None, None)
    glfw.make_context_current(window)

    imgui.create_context()
    impl = GlfwRenderer(window)
    
    # 폰트 로드
    io = imgui.get_io()
    font_path = "fonts/DejaVuSans.ttf"
    font_bold_path = "fonts/DejaVuSans-Bold.ttf"
    font_large_bold = None
    try:
        # 기본 폰트
        io.fonts.add_font_from_file_ttf(font_path, 16)
        # Durations 패널을 위한 크고 굵은 폰트
        font_large_bold = io.fonts.add_font_from_file_ttf(font_bold_path, 20)
        impl.refresh_font_texture()
    except Exception as e:
        print(f"폰트 로드 실패: {e}. 기본 폰트로 계속합니다.")
        font_large_bold = io.fonts.add_font_default()


    no_resize_no_move_flags = imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE

    # 표시값 + 애니 상태
    displayed = {s: 0.0 for s in STAGES}
    anim_state = {s: {"running": False, "start_t": 0.0, "from": 0.0, "to": 0.0} for s in STAGES}

    # 동적 스케일 상태 (좌/우 별도)
    left_scale = 1.0
    right_scale = 1.0
    displayed_left_scale = float(left_scale)
    displayed_right_scale = float(right_scale)

    # 강조 색상 정의
    highlight_color = (42/255, 74/255, 122/255, 1.0)

    while not glfw.window_should_close(window):
        glfw.poll_events()
        impl.process_inputs()
        imgui.new_frame()

        # --- 단축키 처리 ---
        # 창 위치 제어
        monitor = glfw.get_primary_monitor()
        workarea = glfw.get_monitor_workarea(monitor)
        win_width, win_height = glfw.get_window_size(window)
        
        if imgui.is_key_pressed(glfw.KEY_INSERT, repeat=False):
            glfw.set_window_pos(window, workarea[0], workarea[1])
        if imgui.is_key_pressed(glfw.KEY_PAGE_UP, repeat=False):
            glfw.set_window_pos(window, workarea[0] + workarea[2] - win_width, workarea[1])
        if imgui.is_key_pressed(glfw.KEY_DELETE, repeat=False):
            glfw.set_window_pos(window, workarea[0], workarea[1] + workarea[3] - win_height)
        if imgui.is_key_pressed(glfw.KEY_PAGE_DOWN, repeat=False):
            glfw.set_window_pos(window, workarea[0] + workarea[2] - win_width, workarea[1] + workarea[3] - win_height)

        # 초기화 기능
        if imgui.is_key_pressed(glfw.KEY_F10, repeat=False):
            with lock:
                # timeline과 log_lines는 참조를 공유하므로 직접 수정
                for stage in STAGES:
                    timeline[stage]["start"] = None
                    timeline[stage]["end"] = None
                log_lines.clear()

            # GUI 상태 초기화
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

        # targets 계산: 진행중이면 now-start, 완료면 end-start
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

        # update displayed (애니/실시간)
        for s in STAGES:
            start = timeline_copy[s].get("start")
            end = timeline_copy[s].get("end")
            if start is None:
                displayed[s] = 0.0
                anim_state[s]["running"] = False
            elif end is None:
                displayed[s] = targets[s]  # 실시간으로 증가
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

        # 필요한 스케일(좌: 전체 합 기준, 우: 단일 최대값 기준) 계산
        target_total = sum(compute_durations(timeline_copy).values())
        needed_left = max(sum(displayed.values()), target_total, 1e-9)
        needed_right = max(max(displayed.values()) if displayed else 1.0, 1e-9)

        # 스케일이 초과되면 즉시 새로운 'closer_nice_max' 계산하여 확장
        if needed_left > left_scale * 0.999999:
            left_scale = closer_nice_max(needed_left)
        if needed_right > right_scale * 0.999999:
            right_scale = closer_nice_max(needed_right)

        # 스케일은 부드럽게 보간해서 표시
        displayed_left_scale += (left_scale - displayed_left_scale) * SCALE_ADJUST_ALPHA
        displayed_right_scale += (right_scale - displayed_right_scale) * SCALE_ADJUST_ALPHA

        # --- UI 렌더링 ---
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND, *highlight_color)
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND_ACTIVE, *highlight_color)
        imgui.push_style_color(imgui.COLOR_TITLE_BACKGROUND_COLLAPSED, *highlight_color)

        # Summary (상단 좌)
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
                sstart = timeline_copy[stage].get("start")
                send = timeline_copy[stage].get("end")
                dur = (send - sstart) if (sstart and send) else (now - sstart if sstart else 0.0)
                ratio = (dur / (sum(displayed.values()) if sum(displayed.values()) > 0 else 1.0)) * 100 if True else 0
                imgui.table_next_row()
                imgui.table_next_column(); imgui.text(stage)
                imgui.table_next_column(); imgui.text(time.strftime('%H:%M:%S', time.localtime(sstart)) if sstart else "-")
                imgui.table_next_column(); imgui.text(time.strftime('%H:%M:%S', time.localtime(send)) if send else "-")
                imgui.table_next_column(); imgui.text(f"{dur:.3f}s")
                imgui.table_next_column(); imgui.text(f"{ratio:.1f}%")
            imgui.end_table()
        imgui.end()

        # Total (좌 하단)
        imgui.set_next_window_position(0, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Total", False, no_resize_no_move_flags)
        avail_w, avail_h = get_content_region_avail_safe()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        draw_vertical_stack(draw_list, pos, (avail_w, avail_h), displayed, displayed_left_scale)
        imgui.end()

        # Log (우 상단)
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

        # Durations (우 하단)
        imgui.set_next_window_position(half_w, half_h)
        imgui.set_next_window_size(half_w, half_h)
        imgui.begin("Durations", False, no_resize_no_move_flags)
        avail_w, avail_h = get_content_region_avail_safe()
        draw_list = imgui.get_window_draw_list()
        pos = imgui.get_cursor_screen_pos()
        draw_horizontal_bars(draw_list, pos, (avail_w, avail_h), displayed, displayed_right_scale, font_large_bold)
        imgui.end()

        imgui.pop_style_color(3)

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
