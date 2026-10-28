"""
app.py
iOS 虛擬定位控制器主服務 (模組化架構：整合 pikmin_model, debug_center, gps_tunnel, map_layers)
"""

import os
import sys
from pathlib import Path

# 1. 必須在任何 pymobiledevice3 子模組 import 前最優先設定，避開 macOS utun 與權限問題
os.environ["PYMOBILEDEVICE3_USERSPACE_TUNNEL"] = "1"

# 將當前目錄加入 sys.path，確保無論從 Xcode .app bundle 或命令列啟動都能載入拆分的子模組
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

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
from gps_tunnel import IOSUserspaceGpsService, scan_ios_devices_async
from map_layers import DEFAULT_MAP_LAYER_ID, get_map_layers
from pikmin_model import (
    PIKMIN_TIERS,
    SPEED_MODES,
    T_COOLDOWN_WARN_SEC,
    V_HARD_CAP_KMH,
    calculate_planting_efficiency,
    compute_pikmin_stats,
)

# 記錄啟動時的父進程 PID (Swift 宿主 App)
PARENT_PID = os.getppid()

# 接收 Swift 傳遞的動態參數：app.py <PORT> <TOKEN> <NOTIFY_PORT>
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
APP_INTERNAL_TOKEN = sys.argv[2] if len(sys.argv) > 2 else None
NOTIFY_PORT = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None
configure_notify_port(NOTIFY_PORT)

# 自動探測 web 靜態目錄位置
WEB_DIR = APP_DIR / "web"
if not (WEB_DIR / "index.html").exists():
    if (APP_DIR.parent / "web" / "index.html").exists():
        WEB_DIR = APP_DIR.parent / "web"
    elif (APP_DIR / "index.html").exists():
        WEB_DIR = APP_DIR

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
    "saved_routes": [
        {
            "id": "sample_1",
            "name": "範例市區巡航 (3點)",
            "waypoints": [
                {"lat": 22.6047, "lng": 120.3005},
                {"lat": 22.6080, "lng": 120.3025},
                {"lat": 22.6055, "lng": 120.3060},
            ],
        }
    ],
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
    "lock": threading.Lock(),
}

gps_service = IOSUserspaceGpsService(state_ref=state)
gps_service.start_worker()


def _graceful_shutdown_handler(signum, frame):
    print(f"🛑 收到系統退出信號 ({signum})，正在安全還原 iPhone 真實定位...")
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
    base_kmh = state["speed_kmh"]
    # 在種花模式 (非 800 km/h 快速移動) 下，自動鎖定絕對不超過 20 km/h 上限
    if state.get("speed_mode") != "fast_800" and base_kmh < 500.0:
        base_kmh = min(base_kmh, V_HARD_CAP_KMH)

    max_step = (base_kmh / 3.6) * dt
    if base_kmh >= 500.0 or state["random_level"] == 0:
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


def update_eta_calculation() -> None:
    """
    精確計算當前剩餘距離與 ETA：
    在循環模式 (`loop_mode != 'none'`) 下，每一圈的結束點都是從最後一個航點 `wps[-1]` 回到原點 `wps[0]`。
    """
    with state["lock"]:
        cur_lat = state["current_lat"]
        cur_lon = state["current_lon"]
        spd_mps = (state["speed_kmh"] or 15.0) / 3.6

        if state["is_stopping"] or state["is_returning_home"]:
            home_dist = calculate_distance(cur_lat, cur_lon, state["real_lat"], state["real_lon"])
            state["target_remaining_dist"] = round(home_dist, 1)
            state["total_remaining_dist"] = home_dist
            state["eta_seconds"] = int(math.ceil(home_dist / spd_mps)) if spd_mps > 0 else 0
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
                time.sleep(TICK_SEC)
                continue

            if state["is_stopping"] or state["is_returning_home"]:
                cur_lat = state["current_lat"]
                cur_lon = state["current_lon"]
                home_lat = state["real_lat"]
                home_lon = state["real_lon"]

                dist = calculate_distance(cur_lat, cur_lon, home_lat, home_lon)
                step_meters = get_effective_step_meters(TICK_SEC, allow_random_pause=False)

                if dist <= step_meters or dist < 1.5:
                    state["current_lat"] = home_lat
                    state["current_lon"] = home_lon
                    state["anchor_lat"] = home_lat
                    state["anchor_lon"] = home_lon
                    state["actual_speed_kmh"] = 0.0
                    gps_service.set_location(home_lat, home_lon)
                    time.sleep(0.3)
                    gps_service.stop()
                    state["is_stopping"] = False
                    state["is_returning_home"] = False
                    state["is_cruise_paused"] = False
                    state["returning_to_loop_origin"] = False
                    send_macos_notification("SIM-GPS", "已恢復真實定位", "iPhone 已依設定速度平滑返航並還原真實定位。", "Ping")
                else:
                    ratio = step_meters / dist
                    base_lat = cur_lat + (home_lat - cur_lat) * ratio
                    base_lon = cur_lon + (home_lon - cur_lon) * ratio
                    next_lat, next_lon = apply_safe_gps_jitter(
                        cur_lat, cur_lon, base_lat, base_lon, state["speed_kmh"], state["random_level"], TICK_SEC
                    )
                    actual_step = calculate_distance(cur_lat, cur_lon, next_lat, next_lon)
                    state["actual_speed_kmh"] = round((actual_step / TICK_SEC) * 3.6, 1)
                    state["current_lat"] = next_lat
                    state["current_lon"] = next_lon
                    state["anchor_lat"] = next_lat
                    state["anchor_lon"] = next_lon
                    gps_service.set_location(next_lat, next_lon)
                    update_eta_calculation()

            elif state["is_cruising"]:
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

                    eff_cap_kmh = (
                        min(state["speed_kmh"], V_HARD_CAP_KMH)
                        if state.get("speed_mode") != "fast_800" and state["speed_kmh"] < 500.0
                        else state["speed_kmh"]
                    )
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


class SaveRouteRequest(BaseModel):
    name: str
    waypoints: List[dict]


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
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        fallback = APP_DIR / "index.html"
        if fallback.exists():
            return FileResponse(str(fallback))
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
    }


@app.get("/api/devices")
async def get_device_list():
    devs = await scan_ios_devices_async(gps_service)
    connected_list = [gps_service.current_udid] if gps_service.is_connected and gps_service.current_udid else []
    return {
        "devices": devs,
        "connected_udids": connected_list,
        "is_simulating": gps_service.is_connected,
    }


@app.get("/api/status")
async def get_status():
    is_sim = gps_service.is_connected
    connected_list = [gps_service.current_udid] if is_sim and gps_service.current_udid else []
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

    return {
        "is_simulating": is_sim,
        "is_connecting": gps_service.is_connecting,
        "connect_stage": gps_service.connect_stage,
        "is_stopping": state["is_stopping"],
        "is_returning_home": state["is_returning_home"],
        "connected_udids": connected_list,
        "connected_count": len(connected_list),
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
        "debug_error_count": err_count,
        "latest_debug_id": latest_err_id,
    }


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
async def geocode_search(q: str):
    query = q.strip()
    if not query:
        return {"results": []}

    coord_match = re.match(r"^\s*([-+]?\d+(?:\.\d+)?)\s*[,，\s]\s*([-+]?\d+(?:\.\d+)?)\s*$", query)
    if coord_match:
        lat_val = float(coord_match.group(1))
        lon_val = float(coord_match.group(2))
        if -90.0 <= lat_val <= 90.0 and -180.0 <= lon_val <= 180.0:
            return {
                "results": [
                    {
                        "display_name": f"精確座標 ({lat_val:.6f}, {lon_val:.6f})",
                        "lat": lat_val,
                        "lon": lon_val,
                    }
                ]
            }

    def _fetch_nominatim():
        encoded = urllib.parse.quote(query)
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
            return [
                {
                    "display_name": item.get("display_name", query),
                    "lat": float(item["lat"]),
                    "lon": float(item["lon"]),
                }
                for item in raw
                if "lat" in item and "lon" in item
            ]

    try:
        results = await asyncio.to_thread(_fetch_nominatim)
        return {"results": results}
    except Exception as e:
        add_debug_log("地圖地址搜尋失敗", f"{type(e).__name__}: {e}", stage="地圖搜尋", level="WARN")
        raise HTTPException(status_code=502, detail=f"搜尋服務連線失敗: {e}")


@app.post("/api/device/connect")
async def connect_single_device(req: DeviceActionRequest):
    if gps_service.is_connecting:
        raise HTTPException(status_code=409, detail="正在建立連線或掛載開發者映像檔中，請稍候...")

    lat = req.custom_lat if req.custom_lat is not None else (state["current_lat"] or state["real_lat"])
    lon = req.custom_lon if req.custom_lon is not None else (state["current_lon"] or state["real_lon"])

    state["current_lat"] = lat
    state["current_lon"] = lon
    state["anchor_lat"] = lat
    state["anchor_lon"] = lon
    state["is_stopping"] = False
    state["is_returning_home"] = False

    gps_service.connect(req.udid, lat, lon)

    for _ in range(90):
        await asyncio.sleep(0.5)
        if gps_service.is_connected:
            state["is_simulating"] = True
            return {"status": "connected", "udid": gps_service.current_udid or req.udid, "lat": lat, "lon": lon}
        if not gps_service.is_connecting and gps_service.last_error:
            break

    err_msg = gps_service.last_error or "建立定位通道逾時，請確認手機已解鎖、信任此 Mac 並開啟開發者模式。"
    gps_service.stop()
    raise HTTPException(status_code=500, detail=err_msg)


@app.post("/api/device/disconnect")
async def disconnect_single_device(req: DeviceActionRequest):
    state["is_cruising"] = False
    state["is_cruise_paused"] = False
    state["returning_to_loop_origin"] = False
    state["is_stopping"] = False
    state["is_returning_home"] = False
    gps_service.stop()
    await asyncio.sleep(0.6)
    return {"status": "disconnected", "udid": req.udid}


@app.post("/api/cruise/start")
async def start_backend_cruise(req: StartCruiseRequest):
    if not gps_service.is_connected or not req.waypoints:
        raise HTTPException(status_code=400, detail="Not ready")

    new_wps = [{"lat": float(w["lat"]), "lng": float(w["lng"])} for w in req.waypoints]
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
    with state["lock"]:
        state["is_cruising"] = False
        if state["cruise_waypoints"] and (
            state["current_wp_index"] < len(state["cruise_waypoints"]) or state["returning_to_loop_origin"]
        ):
            state["is_cruise_paused"] = True
        state["anchor_lat"] = state["current_lat"]
        state["anchor_lon"] = state["current_lon"]
    return {
        "status": "cruising_paused",
        "current_wp_index": state["current_wp_index"],
        "is_cruise_paused": state["is_cruise_paused"],
        "returning_to_loop_origin": state["returning_to_loop_origin"],
    }


@app.post("/api/cruise/sync-waypoints")
async def sync_cruise_waypoints(req: SyncWaypointsRequest):
    with state["lock"]:
        new_wps = [{"lat": float(w["lat"]), "lng": float(w["lng"])} for w in req.waypoints]
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
    state["is_cruising"] = False
    state["is_cruise_paused"] = False
    if not gps_service.is_connected:
        return {"status": "stopped"}

    dist = calculate_distance(state["current_lat"], state["current_lon"], state["real_lat"], state["real_lon"])
    if dist < 2.0:
        gps_service.stop()
        await asyncio.sleep(0.5)
        return {"status": "stopped"}

    state["is_stopping"] = True
    state["is_returning_home"] = True
    update_eta_calculation()
    return {
        "status": "returning_home",
        "speed_kmh": state["speed_kmh"],
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
    await asyncio.sleep(0.6)
    return {"status": "force_stopped"}


@app.post("/api/move")
async def move_vector(req: MoveVectorRequest):
    if not gps_service.is_connected or state["is_stopping"]:
        raise HTTPException(status_code=400, detail="Not active")

    now = time.monotonic()
    last_ts = state["last_move_ts"]
    elapsed = (now - last_ts) if last_ts > 0.0 else (req.dt or 0.5)
    dt = max(0.05, min(elapsed, 0.65))
    state["last_move_ts"] = now

    eff_cap_kmh = (
        min(state["speed_kmh"], V_HARD_CAP_KMH)
        if state.get("speed_mode") != "fast_800" and state["speed_kmh"] < 500.0
        else state["speed_kmh"]
    )
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
    if not gps_service.is_connected or state["is_stopping"]:
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
            cfg["speed_mode"] = state["speed_mode"]
            cfg["pikmin_tier"] = state["pikmin_tier"]
            cfg["speed_kmh"] = state["speed_kmh"]

        if req.pikmin_tier is not None and req.pikmin_tier in PIKMIN_TIERS:
            state["pikmin_tier"] = req.pikmin_tier
            cfg["pikmin_tier"] = state["pikmin_tier"]

        if req.planting_direction is not None and req.planting_direction.upper() in ("EW", "NS"):
            state["planting_direction"] = req.planting_direction.upper()
            cfg["planting_direction"] = state["planting_direction"]

        if req.active_map_layer is not None:
            state["active_map_layer"] = req.active_map_layer
            cfg["active_map_layer"] = state["active_map_layer"]

        if req.speed_kmh is not None:
            # 若非 800 km/h 快速移動模式，強制鎖定最高上限為 20.0 km/h
            if state["speed_mode"] != "fast_800" and req.speed_kmh < 500.0:
                state["speed_kmh"] = max(1.0, min(float(req.speed_kmh), V_HARD_CAP_KMH))
            else:
                state["speed_kmh"] = max(1.0, min(float(req.speed_kmh), 1000.0))
            cfg["speed_kmh"] = state["speed_kmh"]

        if req.random_level is not None:
            state["random_level"] = max(0, min(req.random_level, 10))
            cfg["random_level"] = state["random_level"]

        if req.loop_mode is not None:
            state["loop_mode"] = req.loop_mode
            cfg["loop_mode"] = state["loop_mode"]
            if req.loop_mode == "none":
                state["returning_to_loop_origin"] = False

        if req.loop_count is not None:
            state["loop_count"] = max(1, req.loop_count)
            cfg["loop_count"] = state["loop_count"]
            state["target_loop_count"] = state["loop_count"]

    save_config(cfg)
    update_eta_calculation()
    return {
        "status": "ok",
        "speed_kmh": state["speed_kmh"],
        "pikmin_tier": state["pikmin_tier"],
        "speed_mode": state["speed_mode"],
        "planting_direction": state["planting_direction"],
        "active_map_layer": state["active_map_layer"],
        "pikmin_stats": compute_pikmin_stats(
            state["eta_seconds"],
            state["speed_kmh"],
            state["pikmin_tier"],
            direction=state["planting_direction"],
        ),
    }


@app.post("/api/save-route")
async def save_custom_route(req: SaveRouteRequest):
    cfg = load_config()
    new_route = {
        "id": f"route_{int(time.time() * 1000)}",
        "name": req.name.strip() or f"自訂路線 ({len(req.waypoints)}點)",
        "waypoints": req.waypoints,
    }
    cfg.setdefault("saved_routes", []).append(new_route)
    save_config(cfg)
    return {"status": "saved", "saved_routes": cfg["saved_routes"]}


@app.delete("/api/delete-route/{route_id}")
async def delete_custom_route(route_id: str):
    cfg = load_config()
    cfg["saved_routes"] = [r for r in cfg.get("saved_routes", []) if r.get("id") != route_id]
    save_config(cfg)
    return {"status": "deleted", "saved_routes": cfg["saved_routes"]}


if __name__ == "__main__":
    import uvicorn

    print(f"🚀 Python 定位核心已啟動: http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
