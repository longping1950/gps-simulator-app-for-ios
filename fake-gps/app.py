"""
app.py
iOS 虛擬定位控制器主服務 (模組化架構：整合多裝置同時控制、獨立 60km/h 返航、pikmin_model, pikmin_automation, debug_center, gps_tunnel, map_layers)
"""

import os
import sys
from pathlib import Path

# 1. 必須在任何 pymobiledevice3 子模組 import 前最優先設定，避開 macOS utun 與權限問題
os.environ["PYMOBILEDEVICE3_USERSPACE_TUNNEL"] = "1"

# 將當前目錄與熱更新目錄加入 sys.path，確保無論從 Xcode .app bundle 或熱更新啟動都能載入最新子模組
APP_DIR = Path(__file__).resolve().parent
HOTFIX_CODE_DIR = Path.home() / "Library" / "Application Support" / "fake-gps" / "hotfix" / "fake-gps"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if HOTFIX_CODE_DIR.exists() and str(HOTFIX_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(HOTFIX_CODE_DIR))

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except Exception:
    pass

import asyncio
import json
import math
import random
import re
import signal
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import urllib.request

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from debug_center import (
    add_debug_log,
    clear_all_debug_logs,
    configure_notify_port,
    get_debug_logs_snapshot,
    get_debug_summary,
    send_macos_notification,
)
from github_sync import (
    check_github_version,
    get_local_version_info,
    perform_github_hot_update,
    search_cloud_landmarks,
    upload_landmark_category_to_github,
)
from gps_tunnel import MultiDeviceGpsManager, scan_ios_devices_async
from map_layers import DEFAULT_MAP_LAYER_ID, get_map_layers
from pikmin_automation import DEFAULT_AUTOMATION_CONFIG, PikminAutomationManager
from pikmin_model import (
    PIKMIN_TIERS,
    SPEED_MODES,
    T_COOLDOWN_WARN_SEC,
    V_HARD_CAP_KMH,
    calculate_planting_efficiency,
    compute_pikmin_stats,
)

# 返程統一時速：60 km/h
RETURN_HOME_SPEED_KMH = 60.0

# 記錄啟動時的父進程 PID (Swift 宿主 App)
PARENT_PID = os.getppid()

# 接收 Swift 傳遞的動態參數：app.py <PORT> <TOKEN> <NOTIFY_PORT>
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
APP_INTERNAL_TOKEN = sys.argv[2] if len(sys.argv) > 2 else None
NOTIFY_PORT = int(sys.argv[3]) if len(sys.argv[3:4]) and sys.argv[3].isdigit() else None
configure_notify_port(NOTIFY_PORT)

# 自動探測 web 靜態目錄位置 (優先選取修改時間最新之 index.html，支援熱更新與 Xcode 增量編譯)
def _resolve_latest_index_file() -> Path:
    candidates = [
        Path.home() / "Library" / "Application Support" / "fake-gps" / "hotfix" / "web" / "index.html",
        Path.home() / "Documents" / "fake-gps" / "web" / "index.html",
        APP_DIR.parent / "web" / "index.html",
        APP_DIR / "web" / "index.html",
        APP_DIR / "index.html",
    ]
    existing = [p for p in candidates if p.exists()]
    if existing:
        return max(existing, key=lambda p: p.stat().st_mtime)
    return APP_DIR / "web" / "index.html"


WEB_DIR = _resolve_latest_index_file().parent

# 使用者資料安全目錄: ~/Library/Application Support/fake-gps/config.json
CONFIG_DIR = Path.home() / "Library" / "Application Support" / "fake-gps"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.json"

default_config = {
    "speed_kmh": 17.0,
    "pikmin_tier": 5,
    "speed_mode": "tier_5",
    "planting_direction": "EW",
    "active_map_layer": DEFAULT_MAP_LAYER_ID,
    "random_level": 0,
    "loop_mode": "none",
    "loop_count": 1,
    "automation": dict(DEFAULT_AUTOMATION_CONFIG),
    "device_automation": {},
    "saved_routes": [
        {
            "id": "sample_1",
            "name": "範例市區巡航 (3點)",
            "waypoints": [
                {"lat": 22.6047, "lng": 120.3005, "hold": False},
                {"lat": 22.6080, "lng": 120.3025, "hold": False},
                {"lat": 22.6055, "lng": 120.3060, "hold": False},
            ],
        }
    ],
    "landmark_categories": [],
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("pikmin_tier", 5)
                data.setdefault("speed_mode", f"tier_{data.get('pikmin_tier', 5)}")
                data.setdefault("planting_direction", "EW")
                data.setdefault("active_map_layer", DEFAULT_MAP_LAYER_ID)
                data.setdefault("automation", dict(DEFAULT_AUTOMATION_CONFIG))
                data.setdefault("device_automation", {})
                data.setdefault("saved_routes", [])
                data.setdefault("landmark_categories", [])
                return data
        except Exception:
            pass
    save_config(default_config)
    return default_config


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入 config.json 失敗: {e}")


cfg_data = load_config()


def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    return r * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def clamp_step_to_speed_limit(
    prev_lat: float,
    prev_lon: float,
    cand_lat: float,
    cand_lon: float,
    max_speed_kmh: float,
    dt: float,
) -> tuple[float, float, float]:
    max_step_meters = max(0.01, (max_speed_kmh / 3.6) * dt)
    actual_dist = calculate_distance(prev_lat, prev_lon, cand_lat, cand_lon)
    safe_limit = max_step_meters * 0.995
    if actual_dist > safe_limit and actual_dist > 0.0:
        scale = safe_limit / actual_dist
        cand_lat = prev_lat + (cand_lat - prev_lat) * scale
        cand_lon = prev_lon + (cand_lon - prev_lon) * scale
        actual_dist = safe_limit
    return cand_lat, cand_lon, actual_dist


def apply_safe_gps_jitter(
    prev_lat: float,
    prev_lon: float,
    base_lat: float,
    base_lon: float,
    max_speed_kmh: float,
    random_level: int,
    dt: float,
) -> tuple[float, float]:
    if random_level <= 0 or max_speed_kmh >= 500.0:
        clat, clon, _ = clamp_step_to_speed_limit(prev_lat, prev_lon, base_lat, base_lon, max_speed_kmh, dt)
        return clat, clon

    max_step_meters = (max_speed_kmh / 3.6) * dt
    base_dist = calculate_distance(prev_lat, prev_lon, base_lat, base_lon)
    headroom = max(0.0, max_step_meters * 0.98 - base_dist)
    jitter_radius_m = min(0.35 * (random_level / 10.0), headroom * 0.85, base_dist * 0.15 + 0.08)

    if jitter_radius_m > 0.01:
        angle = random.uniform(0.0, 2.0 * math.pi)
        offset_m = random.uniform(0.0, jitter_radius_m)
        lat_deg_per_m = 1.0 / 111111.0
        lon_deg_per_m = 1.0 / max(1.0, 111111.0 * math.cos(math.radians(base_lat)))
        base_lat += math.sin(angle) * offset_m * lat_deg_per_m
        base_lon += math.cos(angle) * offset_m * lon_deg_per_m

    clat, clon, _ = clamp_step_to_speed_limit(prev_lat, prev_lon, base_lat, base_lon, max_speed_kmh, dt)
    return clat, clon


def get_real_location() -> tuple[float, float]:
    try:
        res = subprocess.run(
            ["CoreLocationCLI", "-format", "%latitude,%longitude"],
            capture_output=True,
            text=True,
            check=True,
        )
        coords = re.findall(r"[-+]?\d*\.\d+|\d+", res.stdout.strip())
        if len(coords) >= 2:
            return float(coords[0]), float(coords[1])
    except Exception:
        pass
    return 22.549088, 120.354109


init_lat, init_lon = get_real_location()

state: Dict[str, Any] = {
    "real_lat": init_lat,
    "real_lon": init_lon,
    "current_lat": init_lat,
    "current_lon": init_lon,
    "anchor_lat": init_lat,
    "anchor_lon": init_lon,
    "last_move_ts": 0.0,
    "is_simulating": False,
    "is_stopping": False,
    "is_returning_home": False,
    "speed_kmh": float(cfg_data.get("speed_kmh", 17.0)),
    "pikmin_tier": int(cfg_data.get("pikmin_tier", 5)),
    "speed_mode": cfg_data.get("speed_mode", "tier_5"),
    "planting_direction": cfg_data.get("planting_direction", "EW"),
    "active_map_layer": cfg_data.get("active_map_layer", DEFAULT_MAP_LAYER_ID),
    "random_level": int(cfg_data.get("random_level", 0)),
    "loop_mode": cfg_data.get("loop_mode", "none"),
    "loop_count": int(cfg_data.get("loop_count", 1)),
    "pause_remaining": 0.0,
    "actual_speed_kmh": 0.0,
    "is_cruising": False,
    "is_cruise_paused": False,
    "returning_to_loop_origin": False,
    "cruise_waypoints": [],
    "original_waypoints": [],
    "current_wp_index": 0,
    "current_loop_idx": 1,
    "target_loop_count": 1,
    "target_remaining_dist": 0.0,
    "total_remaining_dist": 0.0,
    "eta_seconds": 0,
    "hold_arrival_seq": 0,
    "hold_arrival_msg": "",
    "lock": threading.Lock(),
}

gps_service = MultiDeviceGpsManager(state_ref=state)
gps_service.start_worker()


def _graceful_shutdown_handler(signum, frame):
    print(f"🛑 收到系統退出信號 ({signum})，正在安全還原所有 iPhone 真實定位...")
    try:
        if gps_service.is_connected:
            gps_service.stop()
            time.sleep(0.6)
    except Exception:
        pass
    os._exit(0)


signal.signal(signal.SIGTERM, _graceful_shutdown_handler)
signal.signal(signal.SIGINT, _graceful_shutdown_handler)


def get_effective_step_meters(dt: float, allow_random_pause: bool = True) -> float:
    base_kmh = max(0.0, min(2000.0, float(state["speed_kmh"])))
    if base_kmh <= 0.0:
        return 0.0

    max_step = (base_kmh / 3.6) * dt
    if base_kmh >= 60.0 or state["random_level"] == 0:
        return max_step

    if allow_random_pause:
        if state["pause_remaining"] > 0.0:
            state["pause_remaining"] = max(0.0, state["pause_remaining"] - dt)
            return 0.0

        pause_chance = state["random_level"] * 0.015 * dt
        if random.random() < pause_chance:
            state["pause_remaining"] = random.uniform(1.0, 2.5)
            return 0.0

    max_reduction = min(0.55, state["random_level"] * 0.05)
    min_factor = max(0.35, 0.95 - max_reduction)
    speed_factor = random.uniform(min_factor, 0.95)
    eff_kmh = max(0.8, base_kmh * speed_factor)
    return min((eff_kmh / 3.6) * dt, max_step * 0.95)


def _sync_global_return_and_cruise_flags() -> None:
    """
    同步多裝置狀態至全域旗標：
    - 只要還有一台裝置正在跟隨行程 (active_following)，行程就持續進行
    - 當所有連線裝置皆進入返航或已斷線 (active_following == 0)，才停止巡航行程
    """
    active_following = gps_service.get_active_following_sessions()
    returning_sessions = gps_service.get_returning_sessions()

    if len(active_following) == 0:
        state["is_cruising"] = False
        state["is_cruise_paused"] = False
        state["returning_to_loop_origin"] = False
        state["current_wp_index"] = 0
        if len(returning_sessions) > 0:
            state["is_returning_home"] = True
            state["is_stopping"] = True
        else:
            state["is_returning_home"] = False
            state["is_stopping"] = False
    else:
        state["is_returning_home"] = False
        state["is_stopping"] = False

    state["is_simulating"] = gps_service.is_connected


def update_eta_calculation() -> None:
    with state["lock"]:
        cur_lat = state["current_lat"]
        cur_lon = state["current_lon"]
        spd_mps = (state["speed_kmh"] or 15.0) / 3.6

        if state["is_stopping"] or state["is_returning_home"]:
            ret_spd_mps = RETURN_HOME_SPEED_KMH / 3.6
            # 取距離最遠的一台返航中裝置計算剩餘時間
            returning_sessions = gps_service.get_returning_sessions()
            if returning_sessions:
                home_dist = max(
                    calculate_distance(
                        s.current_lat or cur_lat,
                        s.current_lon or cur_lon,
                        state["real_lat"],
                        state["real_lon"],
                    )
                    for s in returning_sessions
                )
            else:
                home_dist = calculate_distance(cur_lat, cur_lon, state["real_lat"], state["real_lon"])
            state["target_remaining_dist"] = round(home_dist, 1)
            state["total_remaining_dist"] = home_dist
            state["eta_seconds"] = int(math.ceil(home_dist / ret_spd_mps)) if ret_spd_mps > 0 else 0
            return

        wps = state["cruise_waypoints"]
        idx = state["current_wp_index"]
        if not wps or idx >= len(wps):
            state["target_remaining_dist"] = 0.0
            state["total_remaining_dist"] = 0.0
            state["eta_seconds"] = 0
            return

        mode = state["loop_mode"]
        closing_dist = (
            calculate_distance(wps[-1]["lat"], wps[-1]["lng"], wps[0]["lat"], wps[0]["lng"])
            if (mode != "none" and len(wps) >= 2)
            else 0.0
        )

        if state["returning_to_loop_origin"] and mode != "none" and len(wps) >= 2:
            dist_to_origin = calculate_distance(cur_lat, cur_lon, wps[0]["lat"], wps[0]["lng"])
            state["target_remaining_dist"] = round(dist_to_origin, 1)
            rem_this_lap = dist_to_origin
        else:
            target_wp = wps[idx]
            dist_to_target = calculate_distance(cur_lat, cur_lon, target_wp["lat"], target_wp["lng"])
            state["target_remaining_dist"] = round(dist_to_target, 1)

            rem_this_lap = dist_to_target
            prev_lat, prev_lon = target_wp["lat"], target_wp["lng"]
            for wp in wps[idx + 1:]:
                rem_this_lap += calculate_distance(prev_lat, prev_lon, wp["lat"], wp["lng"])
                prev_lat, prev_lon = wp["lat"], wp["lng"]
            rem_this_lap += closing_dist

        full_lap_dist = closing_dist
        for i in range(len(wps) - 1):
            full_lap_dist += calculate_distance(wps[i]["lat"], wps[i]["lng"], wps[i + 1]["lat"], wps[i + 1]["lng"])

        total_dist = rem_this_lap
        if mode == "custom":
            rem_extra_laps = max(0, state["target_loop_count"] - state["current_loop_idx"])
            total_dist = rem_this_lap + (rem_extra_laps * full_lap_dist)

        state["total_remaining_dist"] = total_dist
        state["eta_seconds"] = int(math.ceil(total_dist / spd_mps)) if spd_mps > 0 else 0


def _internal_pause_cruise() -> None:
    with state["lock"]:
        state["is_cruising"] = False
        if state["cruise_waypoints"] and (
            state["current_wp_index"] < len(state["cruise_waypoints"]) or state["returning_to_loop_origin"]
        ):
            state["is_cruise_paused"] = True
        state["anchor_lat"] = state["current_lat"]
        state["anchor_lon"] = state["current_lon"]


def _internal_trigger_smooth_return(udid: Optional[str] = None) -> None:
    """
    將指定裝置 (或全部裝置) 切換為 60 km/h 獨立平滑返航模式：
    - 若僅單台裝置觸發上限或低電量，只有該裝置進入 60 km/h 返航，其他裝置繼續行程
    - 若所有裝置皆已進入返航或斷線，才自動停止行程
    """
    if not gps_service.is_connected:
        return

    home_lat, home_lon = state["real_lat"], state["real_lon"]
    target_sessions = []
    if udid and udid != "default":
        s = gps_service.get_session(udid)
        if s and s.is_connected:
            target_sessions = [s]
    else:
        target_sessions = gps_service.get_active_following_sessions()

    for sess in target_sessions:
        s_lat = sess.current_lat if sess.current_lat is not None else state["current_lat"]
        s_lon = sess.current_lon if sess.current_lon is not None else state["current_lon"]
        dist = calculate_distance(s_lat, s_lon, home_lat, home_lon)
        if dist < 2.0:
            sess.stop()
        else:
            sess.current_lat = s_lat
            sess.current_lon = s_lon
            sess.is_returning_home = True
            sess.is_stopping = True

    _sync_global_return_and_cruise_flags()
    update_eta_calculation()


# 初始化皮克敏自動化守護管理員
automation_mgr = PikminAutomationManager(
    gps_service=gps_service,
    state_ref=state,
    config_getter=load_config,
    config_saver=save_config,
    stop_cruise_cb=_internal_pause_cruise,
    stop_smooth_cb=_internal_trigger_smooth_return,
    notify_port_getter=lambda: NOTIFY_PORT,
    base_dirs=[APP_DIR, APP_DIR.parent, WEB_DIR],
)
automation_mgr.start_background_worker()


def backend_cruise_worker() -> None:
    TICK_SEC = 0.5
    IDLE_JITTER_INTERVAL = 2.0
    last_idle_jitter_ts = 0.0

    while True:
        try:
            try:
                if os.getppid() != PARENT_PID or os.getppid() == 1:
                    print("🛑 偵測到宿主 App 已結束，自動清理 iPhone 模擬定位並關閉後端...")
                    if gps_service.is_connected:
                        gps_service.stop()
                        time.sleep(0.5)
                    os._exit(0)
            except Exception:
                pass

            if not gps_service.is_connected:
                state["actual_speed_kmh"] = 0.0
                _sync_global_return_and_cruise_flags()
                time.sleep(TICK_SEC)
                continue

            home_lat = state["real_lat"]
            home_lon = state["real_lon"]

            # 1. 優先推進所有處於「獨立 60 km/h 返航模式」的裝置
            returning_sessions = gps_service.get_returning_sessions()
            active_following = gps_service.get_active_following_sessions()

            for sess in returning_sessions:
                s_lat = sess.current_lat if sess.current_lat is not None else state["current_lat"]
                s_lon = sess.current_lon if sess.current_lon is not None else state["current_lon"]
                dist = calculate_distance(s_lat, s_lon, home_lat, home_lon)
                ret_step_m = (RETURN_HOME_SPEED_KMH / 3.6) * TICK_SEC

                if dist <= ret_step_m or dist < 2.0:
                    sess.set_location(home_lat, home_lon, force_returning=True)
                    time.sleep(0.25)
                    dev_udid = sess.current_udid or ""
                    sess.stop()
                    if len(active_following) == 0 and len(gps_service.get_returning_sessions()) == 0:
                        state["current_lat"] = home_lat
                        state["current_lon"] = home_lon
                        state["anchor_lat"] = home_lat
                        state["anchor_lon"] = home_lon
                        state["actual_speed_kmh"] = 0.0
                    send_macos_notification(
                        "SIM-GPS",
                        "已恢復真實定位",
                        f"裝置 {dev_udid[:6]} 已以 60 km/h 平滑返航並還原真實定位。",
                        "Ping",
                    )
                else:
                    ratio = ret_step_m / dist
                    n_lat = s_lat + (home_lat - s_lat) * ratio
                    n_lon = s_lon + (home_lon - s_lon) * ratio
                    sess.set_location(n_lat, n_lon, force_returning=True)
                    # 若目前已無任何其他巡航跟隨裝置，同步更新地圖主座標顯示返航軌跡
                    if len(active_following) == 0:
                        state["current_lat"] = n_lat
                        state["current_lon"] = n_lon
                        state["anchor_lat"] = n_lat
                        state["anchor_lon"] = n_lon
                        state["actual_speed_kmh"] = RETURN_HOME_SPEED_KMH

            _sync_global_return_and_cruise_flags()
            active_following = gps_service.get_active_following_sessions()

            # 若沒有任何正在跟隨行程的裝置，僅更新返航 ETA 後等待下一輪
            if len(active_following) == 0:
                update_eta_calculation()
                time.sleep(TICK_SEC)
                continue

            # 2. 推進所有跟隨中的裝置 (多機同時移動)
            if state["is_cruising"]:
                with state["lock"]:
                    wps = state["cruise_waypoints"]
                    idx = state["current_wp_index"]
                    if not wps or idx >= len(wps):
                        state["is_cruising"] = False
                        state["is_cruise_paused"] = False
                        state["returning_to_loop_origin"] = False
                        state["current_wp_index"] = 0
                        state["actual_speed_kmh"] = 0.0
                        send_macos_notification("SIM-GPS", "巡航完成", "所有航點已到達完畢！", "Glass")
                        time.sleep(TICK_SEC)
                        continue
                    target = wps[0] if state["returning_to_loop_origin"] else wps[idx]

                cur_lat = state["current_lat"]
                cur_lon = state["current_lon"]
                t_lat = target["lat"]
                t_lon = target["lng"]

                dist = calculate_distance(cur_lat, cur_lon, t_lat, t_lon)
                step_meters = get_effective_step_meters(TICK_SEC, allow_random_pause=True)

                if step_meters > 0.0:
                    if dist <= step_meters or dist < 0.8:
                        base_lat, base_lon = t_lat, t_lon
                        arrived = True
                    else:
                        ratio = step_meters / dist
                        base_lat = cur_lat + (t_lat - cur_lat) * ratio
                        base_lon = cur_lon + (t_lon - cur_lon) * ratio
                        arrived = False

                    eff_cap_kmh = max(0.0, min(2000.0, float(state["speed_kmh"])))
                    next_lat, next_lon = apply_safe_gps_jitter(
                        cur_lat, cur_lon, base_lat, base_lon, eff_cap_kmh, state["random_level"], TICK_SEC
                    )
                    actual_step = calculate_distance(cur_lat, cur_lon, next_lat, next_lon)
                    state["actual_speed_kmh"] = round((actual_step / TICK_SEC) * 3.6, 1)

                    state["current_lat"] = next_lat
                    state["current_lon"] = next_lon
                    state["anchor_lat"] = next_lat
                    state["anchor_lon"] = next_lon
                    gps_service.set_location(next_lat, next_lon)

                    if arrived:
                        with state["lock"]:
                            mode = state["loop_mode"]
                            total_wps = len(state["cruise_waypoints"])
                            arrived_idx = 0 if state["returning_to_loop_origin"] else state["current_wp_index"]
                            arrived_wp = (
                                state["cruise_waypoints"][arrived_idx]
                                if 0 <= arrived_idx < total_wps
                                else {}
                            )
                            should_hold = bool(arrived_wp.get("hold", False))
                            wp_custom_name = str(arrived_wp.get("name") or "").strip()
                            arrived_label = (
                                f"點 {arrived_idx + 1} ({wp_custom_name})"
                                if wp_custom_name
                                else f"點 {arrived_idx + 1}"
                            )

                            if state["returning_to_loop_origin"]:
                                state["returning_to_loop_origin"] = False
                                if mode == "infinite":
                                    state["current_loop_idx"] += 1
                                    state["current_wp_index"] = 1 if total_wps >= 2 else 0
                                elif mode == "custom":
                                    if state["current_loop_idx"] < state["target_loop_count"]:
                                        state["current_loop_idx"] += 1
                                        state["current_wp_index"] = 1 if total_wps >= 2 else 0
                                    else:
                                        state["is_cruising"] = False
                                        state["is_cruise_paused"] = False
                                        state["current_wp_index"] = 0
                                        state["actual_speed_kmh"] = 0.0
                                        send_macos_notification(
                                            "SIM-GPS",
                                            "循環巡航完成",
                                            f"已完成指定的 {state['target_loop_count']} 圈循環並返回原點！",
                                            "Glass",
                                        )
                                else:
                                    state["is_cruising"] = False
                                    state["is_cruise_paused"] = False
                                    state["current_wp_index"] = 0
                                    state["actual_speed_kmh"] = 0.0
                            else:
                                next_idx = state["current_wp_index"] + 1
                                if next_idx < total_wps:
                                    state["current_wp_index"] = next_idx
                                else:
                                    if mode in ("infinite", "custom") and total_wps >= 2:
                                        state["returning_to_loop_origin"] = True
                                        state["current_wp_index"] = 0
                                    else:
                                        state["is_cruising"] = False
                                        state["is_cruise_paused"] = False
                                        state["current_wp_index"] = 0
                                        state["actual_speed_kmh"] = 0.0
                                        send_macos_notification("SIM-GPS", "巡航完成", "單次巡航任務已結束！", "Glass")

                            # 🎯 若抵達的航點設有「停留 (hold=True)」，跳通知並暫停停留於該點
                            if should_hold:
                                if state["is_cruising"]:
                                    state["is_cruising"] = False
                                    state["is_cruise_paused"] = True
                                    state["actual_speed_kmh"] = 0.0
                                    state["hold_arrival_seq"] = int(state.get("hold_arrival_seq", 0)) + 1
                                    state["hold_arrival_msg"] = (
                                        f"📍 已抵達設定停留點「{arrived_label}」，行程已自動暫停停留！"
                                        "點擊「▶️ 繼續巡航」即可前往下一航點。"
                                    )
                                    send_macos_notification(
                                        "SIM-GPS 抵達停留點",
                                        f"📍 已抵達「{arrived_label}」並自動停留",
                                        "行程已暫停於此航點，點擊「繼續巡航」即可前往下一點。",
                                        "Glass",
                                    )
                                else:
                                    state["hold_arrival_seq"] = int(state.get("hold_arrival_seq", 0)) + 1
                                    state["hold_arrival_msg"] = f"📍 已抵達終點停留點「{arrived_label}」！"
                                    send_macos_notification(
                                        "SIM-GPS 抵達停留點",
                                        f"📍 已抵達終點停留點「{arrived_label}」",
                                        "此路線巡航已抵達設定停留點並完成行程。",
                                        "Glass",
                                    )
                else:
                    state["actual_speed_kmh"] = 0.0

                update_eta_calculation()

            else:
                state["actual_speed_kmh"] = 0.0
                now = time.monotonic()
                if (
                    state["random_level"] > 0
                    and (now - state["last_move_ts"]) > 2.0
                    and (now - last_idle_jitter_ts) >= IDLE_JITTER_INTERVAL
                ):
                    last_idle_jitter_ts = now
                    anchor_lat = state["anchor_lat"] or state["current_lat"]
                    anchor_lon = state["anchor_lon"] or state["current_lon"]
                    cur_lat = state["current_lat"]
                    cur_lon = state["current_lon"]

                    radius_m = min(1.2, 0.25 + state["random_level"] * 0.09)
                    angle = random.uniform(0.0, 2.0 * math.pi)
                    r = random.uniform(0.05, radius_m)
                    cand_lat = anchor_lat + (r * math.sin(angle)) / 111111.0
                    cand_lon = anchor_lon + (r * math.cos(angle)) / max(1.0, 111111.0 * math.cos(math.radians(anchor_lat)))

                    next_lat, next_lon, _ = clamp_step_to_speed_limit(
                        cur_lat, cur_lon, cand_lat, cand_lon, min(state["speed_kmh"], 3.0), IDLE_JITTER_INTERVAL
                    )
                    state["current_lat"] = next_lat
                    state["current_lon"] = next_lon
                    gps_service.set_location(next_lat, next_lon)

        except Exception as e:
            print(f"[Worker] 背景巡航發生異常: {e}")
            add_debug_log("背景巡航執行緒異常", str(e), stage="巡航控制", level="WARN")

        time.sleep(TICK_SEC)


threading.Thread(target=backend_cruise_worker, daemon=True).start()

app = FastAPI()

images_dir = WEB_DIR / "images"
if images_dir.exists():
    app.mount("/static/images", StaticFiles(directory=str(images_dir)), name="images")


@app.middleware("http")
async def security_guard_middleware(request: Request, call_next):
    path = request.url.path
    if APP_INTERNAL_TOKEN and (path == "/" or path.startswith("/api/")):
        query_token = request.query_params.get("token")
        cookie_token = request.cookies.get("app_token")
        header_token = request.headers.get("X-App-Token")
        if query_token != APP_INTERNAL_TOKEN and cookie_token != APP_INTERNAL_TOKEN and header_token != APP_INTERNAL_TOKEN:
            return Response(content="403 Forbidden: 僅限 SIM-GPS 宿主程式內部存取", status_code=403)

    response = await call_next(request)
    if path == "/" or path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"

    if APP_INTERNAL_TOKEN and request.query_params.get("token") == APP_INTERNAL_TOKEN:
        response.set_cookie(key="app_token", value=APP_INTERNAL_TOKEN, httponly=True)

    return response


# --- API 模型定義 ---

class DeviceActionRequest(BaseModel):
    udid: str
    custom_lat: Optional[float] = None
    custom_lon: Optional[float] = None


class MoveVectorRequest(BaseModel):
    dx: float
    dy: float
    dt: Optional[float] = None


class TeleportRequest(BaseModel):
    target_lat: float
    target_lon: float


class StartCruiseRequest(BaseModel):
    waypoints: List[dict]
    loop_mode: str
    loop_count: int
    resume: Optional[bool] = True
    reset: Optional[bool] = False


class SyncWaypointsRequest(BaseModel):
    waypoints: List[dict]
    removed_index: Optional[int] = None


class SettingsRequest(BaseModel):
    speed_kmh: Optional[float] = None
    pikmin_tier: Optional[int] = None
    speed_mode: Optional[str] = None
    planting_direction: Optional[str] = None
    active_map_layer: Optional[str] = None
    random_level: Optional[int] = None
    loop_mode: Optional[str] = None
    loop_count: Optional[int] = None


class AutomationSettingsRequest(BaseModel):
    udid: Optional[str] = None
    low_battery_stop: Optional[bool] = None
    low_battery_threshold: Optional[int] = None
    low_battery_kill_game: Optional[bool] = None
    overheat_notify: Optional[bool] = None
    overheat_threshold_c: Optional[float] = None
    crash_auto_pause: Optional[bool] = None
    crash_auto_restart: Optional[bool] = None
    screen_analysis_enabled: Optional[bool] = None
    auto_navigate_to_map: Optional[bool] = None
    coin_limit_auto_stop: Optional[bool] = None
    coin_limit_keywords: Optional[str] = None
    flower_limit_target: Optional[int] = None


class RemoteTapRequest(BaseModel):
    udid: Optional[str] = None
    norm_x: float
    norm_y: float
    refresh_screen: Optional[bool] = True


class AutomationTargetRequest(BaseModel):
    udid: Optional[str] = None


class DebugTestBenchRequest(BaseModel):
    test_type: str = "full"
    udid: Optional[str] = None
    params: Optional[Dict[str, Any]] = None


class SaveRouteRequest(BaseModel):
    name: str
    waypoints: List[dict]
    loop_mode: Optional[str] = None
    loop_count: Optional[int] = None


class UpdateRouteRequest(BaseModel):
    id: str
    name: Optional[str] = None
    waypoints: Optional[List[dict]] = None
    loop_mode: Optional[str] = None
    loop_count: Optional[int] = None


class SaveLandmarkCategoryRequest(BaseModel):
    id: Optional[str] = None
    name: str
    landmarks: List[dict]
    enabled: Optional[bool] = True
    color: Optional[str] = "#ec4899"


class ToggleLandmarkCategoryRequest(BaseModel):
    category_id: str
    enabled: bool


class LocationSyncRequest(BaseModel):
    lat: float
    lon: float


class ClientDebugReportRequest(BaseModel):
    title: str
    error: str
    stage: Optional[str] = "前端介面"
    level: Optional[str] = "ERROR"
    solution: Optional[str] = None


@app.get("/")
async def serve_index():
    index_file = _resolve_latest_index_file()
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="找不到前端檔案 web/index.html")
    return FileResponse(str(index_file))


@app.post("/api/sync-real-location")
async def sync_real_location(req: LocationSyncRequest):
    if not gps_service.is_connected:
        state["real_lat"] = req.lat
        state["real_lon"] = req.lon
        state["current_lat"] = req.lat
        state["current_lon"] = req.lon
        state["anchor_lat"] = req.lat
        state["anchor_lon"] = req.lon
    return {"status": "synced", "lat": state["real_lat"], "lon": state["real_lon"]}


@app.get("/api/init")
async def get_init_data():
    cfg = load_config()
    return {
        "init_lat": state["real_lat"],
        "init_lon": state["real_lon"],
        "speed_kmh": state["speed_kmh"],
        "pikmin_tier": state["pikmin_tier"],
        "speed_mode": state["speed_mode"],
        "speed_modes": SPEED_MODES,
        "planting_direction": state["planting_direction"],
        "pikmin_tiers": PIKMIN_TIERS,
        "cooldown_warn_sec": T_COOLDOWN_WARN_SEC,
        "map_layers": get_map_layers(),
        "active_map_layer": state["active_map_layer"],
        "random_level": state["random_level"],
        "loop_mode": state["loop_mode"],
        "loop_count": state["loop_count"],
        "saved_routes": cfg.get("saved_routes", []),
        "landmark_categories": cfg.get("landmark_categories", []),
        "automation": automation_mgr.get_status_snapshot(include_screenshot=False),
    }


@app.get("/api/devices")
async def get_device_list():
    devs = await scan_ios_devices_async(gps_service)
    connected_list = gps_service.connected_udids
    returning_list = [
        s.current_udid for s in gps_service.get_returning_sessions() if s.current_udid
    ]
    return {
        "devices": devs,
        "connected_udids": connected_list,
        "returning_udids": returning_list,
        "is_simulating": gps_service.is_connected,
    }


@app.get("/api/status")
async def get_status():
    is_sim = gps_service.is_connected
    connected_list = gps_service.connected_udids
    returning_list = [
        s.current_udid for s in gps_service.get_returning_sessions() if s.current_udid
    ]
    dist = 0.0
    if is_sim and state["real_lat"] and state["current_lat"]:
        dist = calculate_distance(state["real_lat"], state["real_lon"], state["current_lat"], state["current_lon"])

    err_count, latest_err_id = get_debug_summary()
    pikmin_stats = compute_pikmin_stats(
        state["eta_seconds"],
        state["speed_kmh"],
        state["pikmin_tier"],
        direction=state["planting_direction"],
    )

    auto_snap = automation_mgr.get_status_snapshot(include_screenshot=False)
    devices_runtime = auto_snap.get("devices_runtime", {})
    devices_state = {}
    for s in gps_service.get_all_sessions():
        u = s.current_udid or s.target_udid
        if not u:
            continue
        s_dist = calculate_distance(
            state["real_lat"],
            state["real_lon"],
            s.current_lat or state["current_lat"],
            s.current_lon or state["current_lon"],
        )
        rt_u = devices_runtime.get(u, {})
        devices_state[u] = {
            "udid": u,
            "is_connected": s.is_connected,
            "is_connecting": s.is_connecting,
            "connect_stage": s.connect_stage,
            "is_returning_home": s.is_returning_home,
            "return_speed_kmh": RETURN_HOME_SPEED_KMH if s.is_returning_home else 0.0,
            "distance_from_origin": round(s_dist, 1),
            "current_lat": s.current_lat,
            "current_lon": s.current_lon,
            "battery_level": rt_u.get("battery_level"),
            "is_charging": rt_u.get("is_charging"),
            "temperature_c": rt_u.get("temperature_c"),
            "thermal_state": rt_u.get("thermal_state") if rt_u.get("thermal_state") is not None else s.last_thermal_state,
            "thermal_label": rt_u.get("thermal_label") or s.last_thermal_label,
            "system_load_w": rt_u.get("system_load_w") if rt_u.get("system_load_w") is not None else s.last_system_load_w,
            "instant_amperage_ma": rt_u.get("instant_amperage_ma") if rt_u.get("instant_amperage_ma") is not None else s.last_instant_amperage_ma,
            "ddi_power_saving_active": False,
            "pikmin_running": rt_u.get("pikmin_running"),
            "pikmin_foreground": rt_u.get("pikmin_foreground"),
            "pikmin_status_label": rt_u.get("pikmin_status_label"),
            "detected_flowers": rt_u.get("detected_flowers"),
            "detected_coins": rt_u.get("detected_coins"),
            "coin_limit_reached": rt_u.get("coin_limit_reached", False),
        }

    return {
        "is_simulating": is_sim,
        "is_connecting": gps_service.is_connecting,
        "connect_stage": gps_service.connect_stage,
        "is_stopping": state["is_stopping"],
        "is_returning_home": state["is_returning_home"],
        "return_home_speed_kmh": RETURN_HOME_SPEED_KMH,
        "connected_udids": connected_list,
        "returning_udids": returning_list,
        "connected_count": len(connected_list),
        "devices_state": devices_state,
        "real_lat": state["real_lat"],
        "real_lon": state["real_lon"],
        "current_lat": state["current_lat"],
        "current_lon": state["current_lon"],
        "speed_kmh": state["speed_kmh"],
        "pikmin_tier": state["pikmin_tier"],
        "speed_mode": state["speed_mode"],
        "planting_direction": state["planting_direction"],
        "active_map_layer": state["active_map_layer"],
        "pikmin_stats": pikmin_stats,
        "actual_speed_kmh": state["actual_speed_kmh"],
        "random_level": state["random_level"],
        "loop_mode": state["loop_mode"],
        "loop_count": state["loop_count"],
        "is_paused": state["pause_remaining"] > 0.0,
        "distance_from_origin": round(dist, 1),
        "is_cruising": state["is_cruising"],
        "is_cruise_paused": state["is_cruise_paused"],
        "returning_to_loop_origin": state["returning_to_loop_origin"],
        "cruise_waypoints": state["cruise_waypoints"],
        "current_wp_index": state["current_wp_index"],
        "current_loop_idx": state["current_loop_idx"],
        "target_loop_count": state["target_loop_count"],
        "target_remaining_dist": state["target_remaining_dist"],
        "total_remaining_dist": state["total_remaining_dist"],
        "eta_seconds": state["eta_seconds"],
        "hold_arrival_seq": state["hold_arrival_seq"],
        "hold_arrival_msg": state["hold_arrival_msg"],
        "debug_error_count": err_count,
        "latest_debug_id": latest_err_id,
        "automation": auto_snap,
    }


# --- 🤖 皮克敏自動化守護、多機獨立設定與遠端操作 API ---

@app.get("/api/automation/status")
async def get_automation_status(udid: Optional[str] = None):
    return automation_mgr.get_status_snapshot(include_screenshot=True, udid=udid)


@app.post("/api/automation/settings")
async def update_automation_settings(req: AutomationSettingsRequest):
    raw = req.model_dump()
    target_udid = raw.pop("udid", None)
    updates = {k: v for k, v in raw.items() if v is not None}
    updated = automation_mgr.update_settings(updates, udid=target_udid)
    return {
        "status": "ok",
        "udid": target_udid or "default",
        "settings": updated,
        "automation": automation_mgr.get_status_snapshot(include_screenshot=True, udid=target_udid),
    }


@app.post("/api/automation/inspect-now")
async def inspect_screen_now(req: Optional[AutomationTargetRequest] = None):
    target_udid = req.udid if req else None
    try:
        res = await asyncio.to_thread(automation_mgr.inspect_screen_once, True, target_udid)
        return res
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/automation/tap")
async def remote_tap_screen(req: RemoteTapRequest):
    if not gps_service.is_connected:
        raise HTTPException(status_code=400, detail="尚未連線至 iOS 裝置")
    det = await asyncio.to_thread(gps_service.tap_screen_detailed_sync, req.norm_x, req.norm_y, req.udid)
    if not det.get("ok"):
        raise HTTPException(status_code=500, detail=det.get("error") or "傳送模擬點擊 (HID Tap) 失敗")
    if req.refresh_screen:
        await asyncio.sleep(0.8)
        try:
            snap = await asyncio.to_thread(automation_mgr.inspect_screen_once, False, req.udid)
            return {"status": "tapped", "tap_detail": det, "automation": snap}
        except Exception:
            pass
    return {
        "status": "tapped",
        "tap_detail": det,
        "automation": automation_mgr.get_status_snapshot(include_screenshot=True, udid=req.udid),
    }


@app.post("/api/automation/stop-planting")
async def manual_stop_planting_in_game(req: Optional[AutomationTargetRequest] = None):
    target_udid = req.udid if req else None
    if not gps_service.is_connected:
        raise HTTPException(status_code=400, detail="尚未連線至 iOS 裝置")
    try:
        snap = await asyncio.to_thread(automation_mgr.inspect_screen_once, False, target_udid)
        if not snap.get("pikmin_foreground"):
            raise HTTPException(status_code=400, detail="目前手機前景並非皮克敏畫面，為保護手機操作已取消自動點擊")
        ok = await asyncio.to_thread(
            automation_mgr.execute_stop_planting_sequence,
            snap.get("screen_state", "flower_map"),
            "手動觸發結束種花",
            target_udid,
        )
        await asyncio.sleep(0.8)
        snap = await asyncio.to_thread(automation_mgr.inspect_screen_once, False, target_udid)
        return {"status": "stopped" if ok else "failed", "automation": snap}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/automation/launch-pikmin")
async def manual_launch_pikmin(req: Optional[AutomationTargetRequest] = None):
    target_udid = req.udid if req else None
    if not gps_service.is_connected:
        raise HTTPException(status_code=400, detail="尚未連線至 iOS 裝置")
    det = await asyncio.to_thread(gps_service.launch_pikmin_detailed_sync, target_udid)
    if not det.get("ok"):
        raise HTTPException(status_code=500, detail=det.get("error") or "無法喚醒皮克敏 App，請確認手機已解鎖且已安裝 Pikmin Bloom")
    return {"status": "launched", "detail": det}


# --- 🧪 開發者功能測試與底層調試介面 API ---

@app.post("/api/debug/test-bench")
async def run_debug_test_bench(req: DebugTestBenchRequest):
    res = await asyncio.to_thread(
        automation_mgr.run_diagnostic_test,
        req.test_type,
        req.udid,
        req.params,
    )
    return res


@app.get("/api/debug-logs")
async def get_debug_logs():
    return get_debug_logs_snapshot()


@app.post("/api/debug-logs/report")
async def report_client_debug_log(req: ClientDebugReportRequest):
    entry = add_debug_log(
        title=req.title,
        error_msg=req.error,
        stage=req.stage or "前端介面",
        level=req.level or "ERROR",
        solution=req.solution,
    )
    return {"status": "logged", "entry": entry}


@app.delete("/api/debug-logs")
async def clear_debug_logs():
    clear_all_debug_logs()
    return {"status": "cleared", "total_count": 0}


@app.get("/api/geocode")
async def geocode_search(
    q: str,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    west: Optional[float] = None,
    south: Optional[float] = None,
    east: Optional[float] = None,
    north: Optional[float] = None,
):
    query = q.strip()
    if not query:
        return {"results": []}

    center_lat = lat if lat is not None else (state.get("current_lat") or state.get("real_lat"))
    center_lon = lon if lon is not None else (state.get("current_lon") or state.get("real_lon"))

    coord_match = re.match(r"^\s*([-+]?\d+(?:\.\d+)?)\s*[,，\s]\s*([-+]?\d+(?:\.\d+)?)\s*$", query)
    if coord_match:
        lat_val = float(coord_match.group(1))
        lon_val = float(coord_match.group(2))
        if -90.0 <= lat_val <= 90.0 and -180.0 <= lon_val <= 180.0:
            dist_m = (
                round(calculate_distance(center_lat, center_lon, lat_val, lon_val), 1)
                if (center_lat is not None and center_lon is not None)
                else None
            )
            return {
                "results": [
                    {
                        "display_name": f"精確座標 ({lat_val:.6f}, {lon_val:.6f})",
                        "lat": lat_val,
                        "lon": lon_val,
                        "distance_m": dist_m,
                    }
                ]
            }

    def _fetch_nominatim():
        encoded = urllib.parse.quote(query)
        if center_lat is not None and center_lon is not None:
            span_lat = max(0.08, abs((north - south) / 2.0) if (north is not None and south is not None) else 0.12)
            span_lon = max(0.08, abs((east - west) / 2.0) if (east is not None and west is not None) else 0.12)
            vb = f"{center_lon - span_lon:.6f},{center_lat + span_lat:.6f},{center_lon + span_lon:.6f},{center_lat - span_lat:.6f}"
            encoded = f"{encoded}&viewbox={vb}"
        url = f"https://nominatim.openstreetmap.org/search?format=json&limit=5&q={encoded}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "SIM-GPS-macOS-Client/1.6",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            parsed = [
                {
                    "display_name": item.get("display_name", query),
                    "lat": float(item["lat"]),
                    "lon": float(item["lon"]),
                    "distance_m": (
                        round(calculate_distance(center_lat, center_lon, float(item["lat"]), float(item["lon"])), 1)
                        if (center_lat is not None and center_lon is not None)
                        else None
                    ),
                }
                for item in raw
                if "lat" in item and "lon" in item
            ]
            if center_lat is not None and center_lon is not None:
                parsed.sort(key=lambda x: x["distance_m"] if x["distance_m"] is not None else 1e12)
            return parsed

    try:
        results = await asyncio.to_thread(_fetch_nominatim)
        return {"results": results, "center_lat": center_lat, "center_lon": center_lon}
    except Exception as e:
        add_debug_log("地圖地址搜尋失敗", f"{type(e).__name__}: {e}", stage="地圖搜尋", level="WARN")
        raise HTTPException(status_code=502, detail=f"搜尋服務連線失敗: {e}")


@app.post("/api/device/connect")
async def connect_single_device(req: DeviceActionRequest):
    sess = gps_service.get_session(req.udid)
    if sess and sess.is_connecting:
        raise HTTPException(status_code=409, detail="此裝置正在建立連線或掛載開發者映像檔中，請稍候...")

    # 若已經有其他裝置正在巡航或模擬，新加入的裝置直接同步至目前隊伍所在座標
    if gps_service.is_connected and state.get("current_lat") and state.get("current_lon"):
        lat = state["current_lat"]
        lon = state["current_lon"]
    else:
        lat = req.custom_lat if req.custom_lat is not None else (state["current_lat"] or state["real_lat"])
        lon = req.custom_lon if req.custom_lon is not None else (state["current_lon"] or state["real_lon"])
        state["current_lat"] = lat
        state["current_lon"] = lon
        state["anchor_lat"] = lat
        state["anchor_lon"] = lon

    gps_service.connect(req.udid, lat, lon, prefer_wifi=True)
    sess = gps_service.get_session(req.udid)

    for _ in range(90):
        await asyncio.sleep(0.5)
        if sess and sess.is_connected:
            _sync_global_return_and_cruise_flags()
            dev_label = getattr(sess, "device_name", None) or (sess.current_udid or req.udid)[:6]
            # 連線成功當下不立即跳出「未在前景」視窗，改由前端延後 4.5 秒呼叫 /api/automation/verify-foreground 進行多次確認
            return {
                "status": "connected",
                "udid": sess.current_udid or req.udid,
                "device_name": dev_label,
                "lat": lat,
                "lon": lon,
                "pikmin_running": True,
                "pikmin_foreground": True,
                "prompt_launch_pikmin": False,
                "verify_foreground_after_ms": 4500,
            }
        if sess and not sess.is_connecting and sess.last_error:
            break

    err_msg = (sess.last_error if sess else None) or "建立定位通道逾時，請確認手機已解鎖、信任此 Mac 並開啟開發者模式。"
    gps_service.stop_device(req.udid)
    _sync_global_return_and_cruise_flags()
    raise HTTPException(status_code=500, detail=err_msg)


@app.post("/api/device/disconnect")
async def disconnect_single_device(req: DeviceActionRequest):
    sess = gps_service.get_session(req.udid)
    if not sess or not sess.is_connected:
        gps_service.stop_device(req.udid)
        _sync_global_return_and_cruise_flags()
        return {
            "status": "disconnected",
            "udid": req.udid,
            "connected_udids": gps_service.connected_udids,
        }

    # 第一次點擊「中斷」：若距離真實起點 >= 2m，讓該台裝置獨立進入 60 km/h 平滑返航（其他裝置繼續行程）
    if not sess.is_returning_home:
        s_lat = sess.current_lat if sess.current_lat is not None else state["current_lat"]
        s_lon = sess.current_lon if sess.current_lon is not None else state["current_lon"]
        dist = calculate_distance(s_lat, s_lon, state["real_lat"], state["real_lon"])
        if dist >= 2.0:
            sess.current_lat = s_lat
            sess.current_lon = s_lon
            sess.is_returning_home = True
            sess.is_stopping = True
            _sync_global_return_and_cruise_flags()
            update_eta_calculation()
            ret_eta = int(math.ceil(dist / (RETURN_HOME_SPEED_KMH / 3.6)))
            return {
                "status": "returning_home",
                "udid": req.udid,
                "speed_kmh": RETURN_HOME_SPEED_KMH,
                "eta_seconds": ret_eta,
                "distance_m": round(dist, 1),
                "connected_udids": gps_service.connected_udids,
            }

    # 第二次點擊「🏠 返航中 (再點強制斷線)」或距離 < 2m：立即強制斷開該裝置並還原真實定位
    gps_service.stop_device(req.udid)
    await asyncio.sleep(0.4)
    _sync_global_return_and_cruise_flags()
    update_eta_calculation()
    return {
        "status": "disconnected",
        "udid": req.udid,
        "connected_udids": gps_service.connected_udids,
    }


@app.post("/api/cruise/start")
async def start_backend_cruise(req: StartCruiseRequest):
    if not gps_service.is_connected or not req.waypoints:
        raise HTTPException(status_code=400, detail="Not ready")

    new_wps = [
        {
            "lat": float(w["lat"]),
            "lng": float(w["lng"]),
            "hold": bool(w.get("hold", False)),
            "name": str(w.get("name") or ""),
        }
        for w in req.waypoints
    ]
    with state["lock"]:
        should_resume = (
            bool(req.resume)
            and not bool(req.reset)
            and (state["is_cruise_paused"] or state["is_cruising"])
            and (0 <= state["current_wp_index"] < len(new_wps) or state["returning_to_loop_origin"])
        )
        state["cruise_waypoints"] = new_wps
        state["original_waypoints"] = [dict(w) for w in new_wps]
        state["loop_mode"] = req.loop_mode
        state["target_loop_count"] = max(1, req.loop_count)
        if not should_resume:
            state["current_wp_index"] = 0
            state["current_loop_idx"] = 1
            state["returning_to_loop_origin"] = False
        state["is_cruising"] = True
        state["is_cruise_paused"] = False

    update_eta_calculation()
    return {
        "status": "cruising_resumed" if should_resume else "cruising_started",
        "current_wp_index": state["current_wp_index"],
        "current_loop_idx": state["current_loop_idx"],
        "returning_to_loop_origin": state["returning_to_loop_origin"],
    }


@app.post("/api/cruise/stop")
async def stop_backend_cruise():
    _internal_pause_cruise()
    return {
        "status": "cruising_paused",
        "current_wp_index": state["current_wp_index"],
        "is_cruise_paused": state["is_cruise_paused"],
        "returning_to_loop_origin": state["returning_to_loop_origin"],
    }


@app.post("/api/cruise/sync-waypoints")
async def sync_cruise_waypoints(req: SyncWaypointsRequest):
    with state["lock"]:
        new_wps = [
            {
                "lat": float(w["lat"]),
                "lng": float(w["lng"]),
                "hold": bool(w.get("hold", False)),
                "name": str(w.get("name") or ""),
            }
            for w in req.waypoints
        ]
        if not new_wps:
            state["cruise_waypoints"] = []
            state["original_waypoints"] = []
            state["current_wp_index"] = 0
            state["returning_to_loop_origin"] = False
            state["is_cruising"] = False
            state["is_cruise_paused"] = False
        else:
            prev_len = len(state["cruise_waypoints"])
            if state["returning_to_loop_origin"] and len(new_wps) > prev_len and req.removed_index is None:
                state["returning_to_loop_origin"] = False
                state["current_wp_index"] = prev_len

            if req.removed_index is not None and req.removed_index < state["current_wp_index"]:
                state["current_wp_index"] = max(0, state["current_wp_index"] - 1)
            if state["current_wp_index"] >= len(new_wps):
                state["current_wp_index"] = max(0, len(new_wps) - 1)
            state["cruise_waypoints"] = new_wps
            state["original_waypoints"] = [dict(w) for w in new_wps]

    update_eta_calculation()
    return {
        "status": "synced",
        "current_wp_index": state["current_wp_index"],
        "returning_to_loop_origin": state["returning_to_loop_origin"],
        "total_waypoints": len(state["cruise_waypoints"]),
        "eta_seconds": state["eta_seconds"],
    }


@app.post("/api/stop-smooth")
async def stop_simulation_smooth():
    if not gps_service.is_connected:
        return {"status": "stopped"}
    _internal_trigger_smooth_return(udid=None)
    if not gps_service.is_connected:
        return {"status": "stopped"}
    dist = calculate_distance(state["current_lat"], state["current_lon"], state["real_lat"], state["real_lon"])
    return {
        "status": "returning_home",
        "speed_kmh": RETURN_HOME_SPEED_KMH,
        "eta_seconds": state["eta_seconds"],
        "distance_m": round(dist, 1),
    }


@app.post("/api/stop-force")
async def stop_simulation_force():
    state["is_cruising"] = False
    state["is_cruise_paused"] = False
    state["returning_to_loop_origin"] = False
    state["is_stopping"] = False
    state["is_returning_home"] = False
    gps_service.stop()
    await asyncio.sleep(0.5)
    return {"status": "force_stopped"}


@app.post("/api/move")
async def move_vector(req: MoveVectorRequest):
    if not gps_service.is_connected or len(gps_service.get_active_following_sessions()) == 0:
        raise HTTPException(status_code=400, detail="Not active")

    now = time.monotonic()
    last_ts = state["last_move_ts"]
    elapsed = (now - last_ts) if last_ts > 0.0 else (req.dt or 0.5)
    dt = max(0.05, min(elapsed, 0.65))
    state["last_move_ts"] = now

    eff_cap_kmh = max(0.0, min(2000.0, float(state["speed_kmh"])))
    step_meters = (eff_cap_kmh / 3.6) * dt
    lat_change = (req.dy * step_meters) / 111111.0
    lon_cos = max(0.1, math.cos(math.radians(state["current_lat"])))
    lon_change = (req.dx * step_meters) / (111111.0 * lon_cos)

    cur_lat = state["current_lat"]
    cur_lon = state["current_lon"]
    cand_lat = cur_lat + lat_change
    cand_lon = cur_lon + lon_change

    next_lat, next_lon = apply_safe_gps_jitter(
        cur_lat, cur_lon, cand_lat, cand_lon, eff_cap_kmh, state["random_level"], dt
    )

    state["current_lat"] = next_lat
    state["current_lon"] = next_lon
    state["anchor_lat"] = next_lat
    state["anchor_lon"] = next_lon
    gps_service.set_location(next_lat, next_lon)

    dist = calculate_distance(state["real_lat"], state["real_lon"], next_lat, next_lon)
    return {"current_lat": next_lat, "current_lon": next_lon, "distance": round(dist, 1)}


@app.post("/api/teleport")
async def teleport_direct(req: TeleportRequest):
    if not gps_service.is_connected or len(gps_service.get_active_following_sessions()) == 0:
        raise HTTPException(status_code=400, detail="Not active")
    state["current_lat"] = req.target_lat
    state["current_lon"] = req.target_lon
    state["anchor_lat"] = req.target_lat
    state["anchor_lon"] = req.target_lon
    state["last_move_ts"] = time.monotonic()
    gps_service.set_location(req.target_lat, req.target_lon)
    dist = calculate_distance(state["real_lat"], state["real_lon"], state["current_lat"], state["current_lon"])
    return {"current_lat": state["current_lat"], "current_lon": state["current_lon"], "distance": round(dist, 1)}


@app.post("/api/set-settings")
async def update_settings(req: SettingsRequest):
    cfg = load_config()
    with state["lock"]:
        if req.speed_mode is not None and req.speed_mode in SPEED_MODES:
            mode_info = SPEED_MODES[req.speed_mode]
            state["speed_mode"] = req.speed_mode
            state["pikmin_tier"] = mode_info["pikmin_tier"]
            state["speed_kmh"] = mode_info["speed_kmh"]
            cfg["speed_mode"] = req.speed_mode
            cfg["pikmin_tier"] = state["pikmin_tier"]
            cfg["speed_kmh"] = state["speed_kmh"]

        if req.pikmin_tier is not None and req.pikmin_tier in PIKMIN_TIERS:
            state["pikmin_tier"] = req.pikmin_tier
            state["speed_mode"] = f"tier_{req.pikmin_tier}"
            cfg["pikmin_tier"] = req.pikmin_tier
            cfg["speed_mode"] = state["speed_mode"]
            if req.speed_kmh is None:
                rec_spd = PIKMIN_TIERS[req.pikmin_tier]["recommended_kmh"]
                state["speed_kmh"] = rec_spd
                cfg["speed_kmh"] = rec_spd

        if req.speed_kmh is not None:
            raw_spd = max(0.0, min(2000.0, float(req.speed_kmh)))
            state["speed_kmh"] = raw_spd
            cfg["speed_kmh"] = raw_spd

        if req.planting_direction is not None and req.planting_direction.upper() in ("EW", "NS"):
            state["planting_direction"] = req.planting_direction.upper()
            cfg["planting_direction"] = state["planting_direction"]

        if req.active_map_layer is not None:
            state["active_map_layer"] = req.active_map_layer
            cfg["active_map_layer"] = req.active_map_layer

        if req.random_level is not None:
            state["random_level"] = max(0, min(10, req.random_level))
            cfg["random_level"] = state["random_level"]

        if req.loop_mode is not None:
            state["loop_mode"] = req.loop_mode
            cfg["loop_mode"] = req.loop_mode

        if req.loop_count is not None:
            state["loop_count"] = max(1, req.loop_count)
            state["target_loop_count"] = state["loop_count"]
            cfg["loop_count"] = state["loop_count"]

    save_config(cfg)
    update_eta_calculation()
    pikmin_stats = compute_pikmin_stats(
        state["eta_seconds"],
        state["speed_kmh"],
        state["pikmin_tier"],
        direction=state["planting_direction"],
    )
    return {
        "status": "updated",
        "speed_kmh": state["speed_kmh"],
        "pikmin_tier": state["pikmin_tier"],
        "speed_mode": state["speed_mode"],
        "planting_direction": state["planting_direction"],
        "active_map_layer": state["active_map_layer"],
        "eta_seconds": state["eta_seconds"],
        "pikmin_stats": pikmin_stats,
    }


@app.post("/api/save-route")
async def save_route(req: SaveRouteRequest):
    cfg = load_config()
    l_mode = req.loop_mode if req.loop_mode in ("none", "infinite", "custom") else state.get("loop_mode", "none")
    l_count = max(1, int(req.loop_count if req.loop_count is not None else state.get("loop_count", 1)))
    clean_wps = [
        {
            "lat": float(w["lat"]),
            "lng": float(w["lng"]),
            "hold": bool(w.get("hold", False)),
            "name": str(w.get("name") or ""),
        }
        for w in req.waypoints
    ]
    new_route = {
        "id": f"route_{int(time.time() * 1000)}",
        "name": req.name,
        "waypoints": clean_wps,
        "loop_mode": l_mode,
        "loop_count": l_count,
    }
    cfg.setdefault("saved_routes", []).append(new_route)
    save_config(cfg)
    return {"status": "saved", "saved_routes": cfg["saved_routes"]}


@app.post("/api/update-route")
async def update_route(req: UpdateRouteRequest):
    cfg = load_config()
    routes = cfg.get("saved_routes", [])
    found = False
    for r in routes:
        if r.get("id") == req.id:
            if req.name is not None:
                r["name"] = req.name
            if req.waypoints is not None:
                r["waypoints"] = [
                    {
                        "lat": float(w["lat"]),
                        "lng": float(w["lng"]),
                        "hold": bool(w.get("hold", False)),
                        "name": str(w.get("name") or ""),
                    }
                    for w in req.waypoints
                ]
            if req.loop_mode in ("none", "infinite", "custom"):
                r["loop_mode"] = req.loop_mode
            if req.loop_count is not None:
                r["loop_count"] = max(1, int(req.loop_count))
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="找不到指定的路線")
    save_config(cfg)
    return {"status": "updated", "saved_routes": cfg["saved_routes"]}


@app.delete("/api/delete-route/{route_id}")
async def delete_route(route_id: str):
    cfg = load_config()
    cfg["saved_routes"] = [r for r in cfg.get("saved_routes", []) if r["id"] != route_id]
    save_config(cfg)
    return {"status": "deleted", "saved_routes": cfg["saved_routes"]}


# --- 📌 路標庫 (Landmark Categories) 與檔案匯出管理 API ---

class AddLandmarkPointRequest(BaseModel):
    category_id: str
    new_category_name: Optional[str] = None
    color: Optional[str] = None
    lat: float
    lng: float
    name: Optional[str] = None
    desc: Optional[str] = ""


class RemoveLandmarkPointRequest(BaseModel):
    category_id: str
    landmark_id: Optional[str] = None
    index: Optional[int] = None


class RenameLandmarkCategoryRequest(BaseModel):
    category_id: str
    name: str


class RenameLandmarkPointRequest(BaseModel):
    category_id: str
    index: int
    name: str


class ExportFileRequest(BaseModel):
    filename: str
    content: str


@app.post("/api/export-file")
async def export_file_to_disk(req: ExportFileRequest):
    safe_name = os.path.basename(req.filename.strip() or f"SIM-GPS-Export-{int(time.time())}.json")
    downloads_dir = Path.home() / "Downloads"
    target_dir = downloads_dir if downloads_dir.exists() else CONFIG_DIR
    dest_path = target_dir / safe_name

    # 若同名檔案已存在，自動加上序號避免覆蓋或寫入失敗
    if dest_path.exists():
        stem = dest_path.stem
        suffix = dest_path.suffix
        for i in range(1, 100):
            cand = target_dir / f"{stem}_{i}{suffix}"
            if not cand.exists():
                dest_path = cand
                break

    try:
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"ok": True, "saved_path": str(dest_path), "filename": dest_path.name}
    except Exception as e:
        fallback_path = CONFIG_DIR / safe_name
        with open(fallback_path, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"ok": True, "saved_path": str(fallback_path), "filename": fallback_path.name, "note": str(e)}


@app.post("/api/landmarks/save-category")
async def save_landmark_category(req: SaveLandmarkCategoryRequest):
    cfg = load_config()
    cats = cfg.setdefault("landmark_categories", [])
    clean_lms = []
    for idx, lm in enumerate(req.landmarks):
        try:
            lat = float(lm.get("lat") if lm.get("lat") is not None else lm.get("latitude"))
            lng = float(
                lm.get("lng")
                if lm.get("lng") is not None
                else (lm.get("lon") if lm.get("lon") is not None else lm.get("longitude"))
            )
            clean_lms.append({
                "id": str(lm.get("id") or f"lm_{int(time.time() * 1000)}_{idx + 1}"),
                "name": str(lm.get("name") or f"路標 {idx + 1}"),
                "lat": lat,
                "lng": lng,
                "desc": str(lm.get("desc") or ""),
            })
        except Exception:
            continue

    cat_id = req.id or f"lmcat_{int(time.time() * 1000)}"
    existing = next((c for c in cats if c.get("id") == cat_id), None)
    if existing is not None:
        existing["name"] = req.name
        existing["landmarks"] = clean_lms
        if req.enabled is not None:
            existing["enabled"] = bool(req.enabled)
        if req.color:
            existing["color"] = req.color
    else:
        cats.append({
            "id": cat_id,
            "name": req.name,
            "enabled": True if req.enabled is None else bool(req.enabled),
            "color": req.color or "#ec4899",
            "landmarks": clean_lms,
        })
    save_config(cfg)
    return {"status": "saved", "category_id": cat_id, "landmark_categories": cfg["landmark_categories"]}


@app.post("/api/landmarks/add-point")
async def add_landmark_point(req: AddLandmarkPointRequest):
    cfg = load_config()
    cats = cfg.setdefault("landmark_categories", [])
    target_cat = None

    if req.category_id != "new":
        target_cat = next((c for c in cats if c.get("id") == req.category_id), None)

    if target_cat is None:
        cat_id = f"lmcat_{int(time.time() * 1000)}"
        target_cat = {
            "id": cat_id,
            "name": (req.new_category_name or "自訂路標目錄").strip(),
            "enabled": True,
            "color": req.color or "#ec4899",
            "landmarks": [],
        }
        cats.append(target_cat)

    lms = target_cat.setdefault("landmarks", [])
    pt_name = (req.name or "").strip() or f"路標 {len(lms) + 1}"
    lms.append({
        "id": f"lm_{int(time.time() * 1000)}_{len(lms) + 1}",
        "name": pt_name,
        "lat": float(req.lat),
        "lng": float(req.lng),
        "desc": str(req.desc or ""),
    })
    target_cat["enabled"] = True
    save_config(cfg)
    return {
        "status": "added",
        "category_id": target_cat["id"],
        "category_name": target_cat["name"],
        "landmark_name": pt_name,
        "landmark_categories": cfg["landmark_categories"],
    }


@app.post("/api/landmarks/remove-point")
async def remove_landmark_point(req: RemoveLandmarkPointRequest):
    cfg = load_config()
    cats = cfg.setdefault("landmark_categories", [])
    target_cat = next((c for c in cats if c.get("id") == req.category_id), None)
    if target_cat is None:
        raise HTTPException(status_code=404, detail="找不到指定的路標目錄")

    lms = target_cat.get("landmarks", [])
    if req.index is not None and 0 <= req.index < len(lms):
        lms.pop(req.index)
    elif req.landmark_id:
        target_cat["landmarks"] = [item for item in lms if item.get("id") != req.landmark_id]

    save_config(cfg)
    return {"status": "removed", "landmark_categories": cfg["landmark_categories"]}


@app.post("/api/landmarks/rename-category")
async def rename_landmark_category(req: RenameLandmarkCategoryRequest):
    cfg = load_config()
    cats = cfg.setdefault("landmark_categories", [])
    target_cat = next((c for c in cats if c.get("id") == req.category_id), None)
    if target_cat is None:
        raise HTTPException(status_code=404, detail="找不到指定的路標目錄")
    target_cat["name"] = req.name.strip() or target_cat["name"]
    save_config(cfg)
    return {"status": "renamed", "landmark_categories": cfg["landmark_categories"]}


@app.post("/api/landmarks/rename-point")
async def rename_landmark_point(req: RenameLandmarkPointRequest):
    cfg = load_config()
    cats = cfg.setdefault("landmark_categories", [])
    target_cat = next((c for c in cats if c.get("id") == req.category_id), None)
    if target_cat is None:
        raise HTTPException(status_code=404, detail="找不到指定的路標目錄")
    lms = target_cat.get("landmarks", [])
    if 0 <= req.index < len(lms) and req.name.strip():
        lms[req.index]["name"] = req.name.strip()
    save_config(cfg)
    return {"status": "renamed", "landmark_categories": cfg["landmark_categories"]}


@app.post("/api/landmarks/toggle-category")
async def toggle_landmark_category(req: ToggleLandmarkCategoryRequest):
    cfg = load_config()
    cats = cfg.setdefault("landmark_categories", [])
    for c in cats:
        if req.category_id == "all" or c.get("id") == req.category_id:
            c["enabled"] = bool(req.enabled)
    save_config(cfg)
    return {"status": "updated", "landmark_categories": cfg["landmark_categories"]}


@app.delete("/api/landmarks/delete-category/{category_id}")
async def delete_landmark_category(category_id: str):
    cfg = load_config()
    cfg["landmark_categories"] = [
        c for c in cfg.get("landmark_categories", []) if c.get("id") != category_id
    ]
    save_config(cfg)
    return {"status": "deleted", "landmark_categories": cfg["landmark_categories"]}


# --- 🔄 延後多次確認皮克敏前景狀態、GitHub 版本檢查 / 熱更新 與 雲端路標庫 API ---

class SkipVersionRequest(BaseModel):
    version: str


class UploadLandmarkGitHubRequest(BaseModel):
    category_id: str
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    github_token: Optional[str] = None


@app.post("/api/automation/verify-foreground")
async def verify_pikmin_foreground_delayed(req: Optional[AutomationTargetRequest] = None):
    """
    連線後延遲確認皮克敏是否在前景：
    連續進行 2 次採樣（間隔 2.0 秒），只要任一次偵測到在前景 (或 CPU >= 2.5% / uncertain) 即判定為在前景，
    必須兩次皆明確判定不在前景才回傳 confirmed_not_foreground=True 並發送通知，徹底避免開著遊戲卻誤跳視窗。
    """
    target_udid = req.udid if req else None
    sess = gps_service.get_session(target_udid)
    if not sess or not sess.is_connected:
        return {
            "ok": True,
            "connected": False,
            "pikmin_foreground": True,
            "confirmed_not_foreground": False,
            "prompt_launch_pikmin": False,
        }

    dev_label = getattr(sess, "device_name", None) or (sess.current_udid or target_udid or "")[:6]

    # 第一次採樣 (含 Sysmontap CPU 檢測)
    p1 = await asyncio.to_thread(gps_service.query_pikmin_process_sync, target_udid, True)
    if p1.get("foreground") is True or p1.get("foreground_uncertain") is True or (p1.get("cpu") or 0.0) >= 2.5:
        return {
            "ok": True,
            "connected": True,
            "udid": sess.current_udid or target_udid,
            "device_name": dev_label,
            "pikmin_running": bool(p1.get("running")),
            "pikmin_foreground": True,
            "confirmed_not_foreground": False,
            "prompt_launch_pikmin": False,
            "cpu": p1.get("cpu", 0.0),
        }

    # 若第一次顯示不在前景，等待 2.0 秒後進行第二次確認
    await asyncio.sleep(2.0)
    if not sess.is_connected:
        return {
            "ok": True,
            "connected": False,
            "pikmin_foreground": True,
            "confirmed_not_foreground": False,
            "prompt_launch_pikmin": False,
        }

    p2 = await asyncio.to_thread(gps_service.query_pikmin_process_sync, target_udid, True)
    is_fg = bool(p2.get("foreground") is True or p2.get("foreground_uncertain") is True or (p2.get("cpu") or 0.0) >= 2.5)
    is_running = bool(p2.get("running"))

    if not is_fg:
        send_macos_notification(
            "SIM-GPS 遊戲未在前景",
            f"📱 {dev_label} 尚未在前景開啟皮克敏",
            "已在連線後確認皮克敏未在手機前景，可於控制視窗一鍵自動啟動遊戲。",
            "Glass",
        )

    return {
        "ok": True,
        "connected": True,
        "udid": sess.current_udid or target_udid,
        "device_name": dev_label,
        "pikmin_running": is_running,
        "pikmin_foreground": is_fg,
        "confirmed_not_foreground": not is_fg,
        "prompt_launch_pikmin": not is_fg,
        "cpu": p2.get("cpu", 0.0),
    }


@app.get("/api/version/check")
async def api_check_version(manual: bool = False):
    cfg = load_config()
    skipped = cfg.get("skipped_version")
    res = await asyncio.to_thread(
        check_github_version,
        APP_DIR,
        CONFIG_DIR,
        skipped_version=skipped,
        force_manual=bool(manual),
    )
    return res


@app.post("/api/version/skip")
async def api_skip_version(req: SkipVersionRequest):
    cfg = load_config()
    cfg["skipped_version"] = req.version.strip()
    save_config(cfg)
    return {"ok": True, "skipped_version": cfg["skipped_version"]}


@app.post("/api/version/hot-update")
async def api_perform_hot_update():
    res = await asyncio.to_thread(perform_github_hot_update, APP_DIR, CONFIG_DIR)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "熱更新失敗")
    return res


@app.get("/api/landmarks/cloud-search")
async def api_search_cloud_landmarks(
    q: str = "",
    lat: Optional[float] = None,
    lon: Optional[float] = None,
):
    c_lat = lat if lat is not None else (state.get("current_lat") or state.get("real_lat"))
    c_lon = lon if lon is not None else (state.get("current_lon") or state.get("real_lon"))
    res = await asyncio.to_thread(
        search_cloud_landmarks,
        APP_DIR,
        query=q,
        center_lat=c_lat,
        center_lon=c_lon,
        calc_dist_fn=calculate_distance,
    )
    return res


@app.post("/api/landmarks/github-upload")
async def api_upload_landmark_to_github(req: UploadLandmarkGitHubRequest):
    cfg = load_config()
    cats = cfg.get("landmark_categories", [])
    target = next((c for c in cats if c.get("id") == req.category_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="找不到指定的本機路標目錄")

    cat_copy = dict(target)
    if req.tags is not None:
        cat_copy["tags"] = [t.strip() for t in req.tags if t.strip()]
    if req.github_token:
        cfg["github_token"] = req.github_token.strip()
        save_config(cfg)

    saved_token = req.github_token or cfg.get("github_token")
    res = await asyncio.to_thread(
        upload_landmark_category_to_github,
        APP_DIR,
        cat_copy,
        description=req.description or "",
        github_token=saved_token,
    )
    return res


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
