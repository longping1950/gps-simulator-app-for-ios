import os
import sys

# 1. 必須在任何 pymobiledevice3 子模組 import 前最優先設定，避開 macOS utun 與權限問題
os.environ["PYMOBILEDEVICE3_USERSPACE_TUNNEL"] = "1"
import pymobiledevice3.remote.tunnel_service as tunnel_service
tunnel_service.USE_USERSPACE_TUNNEL = True

import asyncio
import json
import math
import random
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import urllib.request

from pymobiledevice3.remote.userspace_tunnel import establish_userspace_rsd
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

# 接收 Swift 傳遞的動態參數：app.py <PORT> <TOKEN>
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
APP_INTERNAL_TOKEN = sys.argv[2] if len(sys.argv) > 2 else None
NOTIFY_PORT = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None

# 自動探測 web 靜態目錄位置
APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
if not (WEB_DIR / "index.html").exists():
    if (APP_DIR / "index.html").exists():
        WEB_DIR = APP_DIR

# 使用者資料安全目錄: ~/Library/Application Support/fake-gps/config.json
CONFIG_DIR = Path.home() / "Library" / "Application Support" / "fake-gps"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.json"

default_config = {
    "speed_kmh": 18.0,
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
                {"lat": 22.6055, "lng": 120.3060}
            ]
        }
    ]
}

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    save_config(default_config)
    return default_config

def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入 config.json 失敗: {e}")

cfg_data = load_config()

# --- 系統原生通知輔助 ---
def send_macos_notification(title: str, subtitle: str, message: str, sound_name: str = "Glass"):
    if not NOTIFY_PORT:
        return
    def _post():
        try:
            url = f"http://127.0.0.1:{NOTIFY_PORT}/notify"
            payload = json.dumps({
                "title": title,
                "subtitle": subtitle,
                "message": message
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1.0) as _:
                pass
        except Exception as e:
            print(f"[Notify] 發送原生通知失敗: {e}")
    threading.Thread(target=_post, daemon=True).start()

# --- 核心：非同步長連接 Session 服務 ---
class IOSUserspaceGpsService:
    def __init__(self):
        self.cmd_queue: Optional[asyncio.Queue] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.is_connected = False
        self.is_running = False
        self.current_udid: Optional[str] = None
        self.sim = None

    def start_worker(self):
        if self.worker_thread is not None and self.worker_thread.is_alive():
            return

        def _worker_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.cmd_queue = asyncio.Queue()
            try:
                self.loop.run_until_complete(self._event_dispatcher())
            finally:
                self.loop.close()

        self.worker_thread = threading.Thread(target=_worker_loop, daemon=True)
        self.worker_thread.start()

    async def _event_dispatcher(self):
        while True:
            action, payload = await self.cmd_queue.get()
            if action == "START":
                udid, init_lat, init_lon = payload
                self.current_udid = udid
                await self._run_dvt_session(init_lat, init_lon)
            elif action == "SHUTDOWN":
                break

    async def _run_dvt_session(self, init_lat: float, init_lon: float):
        print(f"⏳ [Userspace] 正在與裝置建立 RSD 隧道...")
        try:
            rsd = establish_userspace_rsd()
            if asyncio.iscoroutine(rsd):
                rsd = await rsd
            print(f"✅ [Userspace] RSD 建立成功！Provider: {type(rsd).__name__}")
        except Exception as e:
            print(f"❌ [Userspace] 建立 RSD 隧道失敗: {e}")
            self.is_connected = False
            return

        print("⏳ [Userspace] 正在掛載 DvtProvider 與 LocationSimulation...")
        try:
            async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as sim:
                self.sim = sim
                await sim.set(init_lat, init_lon)
                self.is_connected = True
                self.is_running = True
                print(f"✅ [Userspace] 會話已就緒！起點座標鎖定: {init_lat:.6f}, {init_lon:.6f}")

                while self.is_running:
                    try:
                        action, payload = await asyncio.wait_for(self.cmd_queue.get(), timeout=1.0)
                        if action == "SET":
                            lat, lon = payload
                            await sim.set(lat, lon)
                        elif action == "STOP":
                            break
                    except asyncio.TimeoutError:
                        continue

                print("🛑 [Userspace] 正在清理定位模擬...")
                try:
                    await sim.clear()
                    print("✅ [Userspace] 已恢復真機 GPS 定位。")
                except Exception as e:
                    print(f"清理異常: {e}")

        except Exception as e:
            print(f"❌ [Userspace] DVT 運行異常 (連線中斷): {e}")
            send_macos_notification("SIM-GPS 警報", "裝置連線中斷", "與 iPhone 連線中斷，已終止模擬！", sound_name="Basso")
        finally:
            self.sim = None
            self.is_connected = False
            self.is_running = False

    def connect(self, udid: str, lat: float, lon: float):
        self.start_worker()
        asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("START", (udid, lat, lon))), self.loop)

    def set_location(self, lat: float, lon: float):
        if self.is_connected and self.loop:
            asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("SET", (lat, lon))), self.loop)

    def stop(self):
        if self.is_connected and self.loop:
            asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("STOP", None)), self.loop)

gps_service = IOSUserspaceGpsService()
gps_service.start_worker()

def calculate_distance(lat1, lon1, lat2, lon2):
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def get_real_location():
    try:
        res = subprocess.run(
            ["CoreLocationCLI", "-format", "%latitude,%longitude"],
            capture_output=True, text=True, check=True
        )
        coords = re.findall(r"[-+]?\d*\.\d+|\d+", res.stdout.strip())
        if len(coords) >= 2:
            return float(coords[0]), float(coords[1])
    except Exception:
        pass
    return 22.549088, 120.354109

init_lat, init_lon = get_real_location()

state = {
    "real_lat": init_lat,
    "real_lon": init_lon,
    "current_lat": init_lat,
    "current_lon": init_lon,
    "is_simulating": False,
    "is_stopping": False,
    "speed_kmh": cfg_data.get("speed_kmh", 18.0),
    "random_level": cfg_data.get("random_level", 0),
    "loop_mode": cfg_data.get("loop_mode", "none"),
    "loop_count": cfg_data.get("loop_count", 1),
    "pause_remaining": 0,
    "is_cruising": False,
    "cruise_waypoints": [],
    "original_waypoints": [],
    "current_loop_idx": 1,
    "target_loop_count": 1,
    "target_remaining_dist": 0.0,
    "total_remaining_dist": 0.0,
    "eta_seconds": 0,
    "lock": threading.Lock()
}

def scan_ios_devices() -> List[Dict]:
    devices = []
    try:
        cmd = [sys.executable, "-m", "pymobiledevice3", "usbmux", "list"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2.5)
        if res.returncode == 0 and res.stdout:
            try:
                raw_json = json.loads(res.stdout)
                for item in raw_json:
                    udid = item.get("SerialNumber") or item.get("UniqueDeviceID")
                    conn_type = item.get("ConnectionType", "USB")
                    prod_type = item.get("ProductType", "iPhone")
                    if udid:
                        devices.append({
                            "udid": udid,
                            "connection": conn_type,
                            "model": prod_type,
                            "name": f"iOS 裝置 ({conn_type})"
                        })
            except Exception:
                lines = res.stdout.strip().splitlines()
                for line in lines:
                    match = re.search(r"([0-9a-fA-F-]{24,40})", line)
                    if match:
                        u = match.group(1)
                        conn = "Network (Wi-Fi)" if "network" in line.lower() else "USB"
                        devices.append({
                            "udid": u,
                            "connection": conn,
                            "model": "iPhone",
                            "name": f"iOS 裝置 ({conn})"
                        })
    except Exception:
        pass

    if not devices:
        devices.append({
            "udid": "default",
            "connection": "Auto (預設通道)",
            "model": "iPhone",
            "name": "本機偵測裝置 (預設)"
        })
    return devices

def get_effective_speed() -> float:
    base_kmh = state["speed_kmh"]
    if base_kmh >= 500.0 or state["random_level"] == 0:
        return base_kmh / 3.6

    if state["pause_remaining"] > 0:
        state["pause_remaining"] -= 1
        return 0.0

    pause_chance = state["random_level"] * 0.02
    if random.random() < pause_chance:
        state["pause_remaining"] = random.randint(1, 3)
        return 0.0

    max_reduction = state["random_level"] * 0.05
    speed_factor = 1.0 - random.uniform(0.0, max_reduction)
    eff_kmh = max(1.0, base_kmh * speed_factor)
    return eff_kmh / 3.6

def update_eta_calculation():
    with state["lock"]:
        wps = state["cruise_waypoints"]
        if not wps:
            state["total_remaining_dist"] = 0.0
            state["eta_seconds"] = 0
            return

        cur_lat = state["current_lat"]
        cur_lon = state["current_lon"]

        single_lap_dist = 0.0
        prev_lat, prev_lon = cur_lat, cur_lon
        for wp in wps:
            single_lap_dist += calculate_distance(prev_lat, prev_lon, wp["lat"], wp["lng"])
            prev_lat, prev_lon = wp["lat"], wp["lng"]

        mode = state["loop_mode"]
        if mode != "none" and len(state["original_waypoints"]) >= 2:
            last = state["original_waypoints"][-1]
            first = state["original_waypoints"][0]
            single_lap_dist += calculate_distance(last["lat"], last["lng"], first["lat"], first["lng"])

        total_dist = single_lap_dist
        if mode == "custom":
            rem_laps = max(1, state["target_loop_count"] - state["current_loop_idx"] + 1)
            total_dist = single_lap_dist * rem_laps

        state["total_remaining_dist"] = total_dist
        spd_mps = (state["speed_kmh"] or 18.0) / 3.6
        state["eta_seconds"] = int(total_dist / spd_mps) if spd_mps > 0 else 0

def backend_cruise_worker():
    while True:
        try:
            if state["is_cruising"] and gps_service.is_connected and not state["is_stopping"]:
                with state["lock"]:
                    wps = state["cruise_waypoints"]
                    if not wps:
                        state["is_cruising"] = False
                        send_macos_notification("SIM-GPS", "巡航完成", "所有航點已到達完畢！", "Glass")
                        continue
                    target = wps[0]

                cur_lat = state["current_lat"]
                cur_lon = state["current_lon"]
                dist = calculate_distance(cur_lat, cur_lon, target["lat"], target["lng"])
                step_meters = get_effective_speed()

                state["target_remaining_dist"] = round(dist, 1)

                if step_meters > 0.0:
                    if dist <= step_meters:
                        next_lat, next_lon = target["lat"], target["lng"]
                        arrived = True
                    else:
                        ratio = step_meters / dist
                        next_lat = cur_lat + (target["lat"] - cur_lat) * ratio
                        next_lon = cur_lon + (target["lng"] - cur_lon) * ratio
                        arrived = False

                    state["current_lat"] = next_lat
                    state["current_lon"] = next_lon
                    gps_service.set_location(next_lat, next_lon)

                    if arrived:
                        with state["lock"]:
                            mode = state["loop_mode"]
                            if mode == "infinite":
                                first = state["cruise_waypoints"].pop(0)
                                state["cruise_waypoints"].append(first)
                            elif mode == "custom":
                                first = state["cruise_waypoints"].pop(0)
                                state["cruise_waypoints"].append(first)
                                orig_first = state["original_waypoints"][0]
                                if (state["cruise_waypoints"][0]["lat"] == orig_first["lat"] and
                                    state["cruise_waypoints"][0]["lng"] == orig_first["lng"]):
                                    state["current_loop_idx"] += 1
                                    if state["current_loop_idx"] > state["target_loop_count"]:
                                        state["is_cruising"] = False
                                        state["cruise_waypoints"] = []
                                        send_macos_notification("SIM-GPS", "巡航完成", f"已完成指定的 {state['target_loop_count']} 圈巡航！", "Glass")
                            else:
                                state["cruise_waypoints"].pop(0)
                                if len(state["cruise_waypoints"]) == 0:
                                    state["is_cruising"] = False
                                    send_macos_notification("SIM-GPS", "巡航完成", "單次巡航任務已結束！", "Glass")

                update_eta_calculation()

        except Exception as e:
            print(f"[Cruise Worker] 異常: {e}")

        time.sleep(1.0)

threading.Thread(target=backend_cruise_worker, daemon=True).start()

# --- FastAPI 實例與安全防護 ---
app = FastAPI(title="iOS 虛擬定位控制器 - 原生容器封裝版")

# 掛載靜態資源目錄
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

@app.middleware("http")
async def verify_app_internal_access(request: Request, call_next):
    # 放行本地靜態資源
    if request.url.path == "/" or request.url.path.startswith("/static"):
        return await call_next(request)

    if APP_INTERNAL_TOKEN:
        token = (
            request.query_params.get("token") or
            request.headers.get("X-App-Token") or
            request.cookies.get("app_token")
        )
        if token != APP_INTERNAL_TOKEN:
            return Response(content="403 Forbidden: 僅限原生 App 內部容器存取", status_code=403)

    response = await call_next(request)

    if APP_INTERNAL_TOKEN and request.query_params.get("token") == APP_INTERNAL_TOKEN:
        response.set_cookie(key="app_token", value=APP_INTERNAL_TOKEN, httponly=True)

    return response

# --- API 定義 ---

class DeviceActionRequest(BaseModel):
    udid: str
    custom_lat: Optional[float] = None
    custom_lon: Optional[float] = None

class MoveVectorRequest(BaseModel):
    dx: float
    dy: float

class TeleportRequest(BaseModel):
    target_lat: float
    target_lon: float

class StartCruiseRequest(BaseModel):
    waypoints: List[dict]
    loop_mode: str
    loop_count: int

class SettingsRequest(BaseModel):
    speed_kmh: Optional[float] = None
    random_level: Optional[int] = None
    loop_mode: Optional[str] = None
    loop_count: Optional[int] = None

class SaveRouteRequest(BaseModel):
    name: str
    waypoints: List[dict]

class LocationSyncRequest(BaseModel):
    lat: float
    lon: float

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
    return {"status": "synced", "lat": state["real_lat"], "lon": state["real_lon"]}

@app.get("/api/init")
async def get_init_data():
    cfg = load_config()
    return {
        "init_lat": state["real_lat"],
        "init_lon": state["real_lon"],
        "speed_kmh": state["speed_kmh"],
        "random_level": state["random_level"],
        "loop_mode": state["loop_mode"],
        "loop_count": state["loop_count"],
        "saved_routes": cfg.get("saved_routes", [])
    }

@app.get("/api/devices")
async def get_device_list():
    devs = scan_ios_devices()
    connected_list = [gps_service.current_udid] if gps_service.is_connected and gps_service.current_udid else []
    return {
        "devices": devs,
        "connected_udids": connected_list,
        "is_simulating": gps_service.is_connected
    }

@app.get("/api/status")
async def get_status():
    is_sim = gps_service.is_connected
    connected_list = [gps_service.current_udid] if is_sim and gps_service.current_udid else []
    dist = 0.0
    if is_sim and state["real_lat"] and state["current_lat"]:
        dist = calculate_distance(state["real_lat"], state["real_lon"], state["current_lat"], state["current_lon"])

    return {
        "is_simulating": is_sim,
        "is_stopping": state["is_stopping"],
        "connected_udids": connected_list,
        "connected_count": len(connected_list),
        "real_lat": state["real_lat"],
        "real_lon": state["real_lon"],
        "current_lat": state["current_lat"],
        "current_lon": state["current_lon"],
        "speed_kmh": state["speed_kmh"],
        "random_level": state["random_level"],
        "loop_mode": state["loop_mode"],
        "loop_count": state["loop_count"],
        "is_paused": state["pause_remaining"] > 0,
        "distance_from_origin": round(dist, 1),
        "is_cruising": state["is_cruising"],
        "cruise_waypoints": state["cruise_waypoints"],
        "current_loop_idx": state["current_loop_idx"],
        "target_loop_count": state["target_loop_count"],
        "target_remaining_dist": state["target_remaining_dist"],
        "total_remaining_dist": state["total_remaining_dist"],
        "eta_seconds": state["eta_seconds"]
    }

@app.post("/api/device/connect")
async def connect_single_device(req: DeviceActionRequest):
    lat = req.custom_lat if req.custom_lat is not None else (state["current_lat"] or state["real_lat"])
    lon = req.custom_lon if req.custom_lon is not None else (state["current_lon"] or state["real_lon"])

    state["current_lat"] = lat
    state["current_lon"] = lon
    state["is_simulating"] = True
    state["is_stopping"] = False

    gps_service.connect(req.udid, lat, lon)

    for _ in range(30):
        if gps_service.is_connected:
            break
        await asyncio.sleep(0.1)

    if not gps_service.is_connected:
        raise HTTPException(status_code=500, detail="無法與 iPhone 建立長連接，請確認已解鎖並信任此電腦")

    return {"status": "connected", "udid": req.udid, "lat": lat, "lon": lon}

@app.post("/api/device/disconnect")
async def disconnect_single_device(req: DeviceActionRequest):
    state["is_cruising"] = False
    gps_service.stop()
    state["is_simulating"] = False
    return {"status": "disconnected", "udid": req.udid, "connected_count": 0}

@app.post("/api/cruise/start")
async def start_backend_cruise(req: StartCruiseRequest):
    if not gps_service.is_connected or state["is_stopping"]:
        raise HTTPException(status_code=400, detail="裝置尚未連線")

    if not req.waypoints:
        raise HTTPException(status_code=400, detail="航點清單不可為空")

    with state["lock"]:
        state["cruise_waypoints"] = [dict(w) for w in req.waypoints]
        state["original_waypoints"] = [dict(w) for w in req.waypoints]
        state["loop_mode"] = req.loop_mode
        state["target_loop_count"] = max(1, req.loop_count)
        state["current_loop_idx"] = 1
        state["is_cruising"] = True

    update_eta_calculation()
    return {"status": "cruising_started"}

@app.post("/api/cruise/stop")
async def stop_backend_cruise():
    with state["lock"]:
        state["is_cruising"] = False
    return {"status": "cruising_stopped"}

@app.post("/api/move")
async def move_by_vector(req: MoveVectorRequest):
    if not gps_service.is_connected or state["is_stopping"]:
        raise HTTPException(status_code=400, detail="裝置尚未連線")

    if state["is_cruising"]:
        state["is_cruising"] = False

    meters_per_sec = get_effective_speed()
    if meters_per_sec == 0.0:
        return {"status": "paused", "current_lat": state["current_lat"], "current_lon": state["current_lon"]}

    delta_x_meters = req.dx * meters_per_sec
    delta_y_meters = req.dy * meters_per_sec

    lat_deg_per_meter = 1.0 / 111111.0
    lon_deg_per_meter = 1.0 / (111111.0 * math.cos(math.radians(state["current_lat"])))

    next_lat = state["current_lat"] + (delta_y_meters * lat_deg_per_meter)
    next_lon = state["current_lon"] + (delta_x_meters * lon_deg_per_meter)

    state["current_lat"] = next_lat
    state["current_lon"] = next_lon

    gps_service.set_location(next_lat, next_lon)
    dist = calculate_distance(state["real_lat"], state["real_lon"], next_lat, next_lon)
    return {"status": "ok", "current_lat": next_lat, "current_lon": next_lon, "distance": round(dist, 1)}

@app.post("/api/teleport")
async def teleport_to(req: TeleportRequest):
    if not gps_service.is_connected or state["is_stopping"]:
        raise HTTPException(status_code=400, detail="裝置尚未連線")

    state["current_lat"] = req.target_lat
    state["current_lon"] = req.target_lon
    gps_service.set_location(req.target_lat, req.target_lon)

    dist_to_real = calculate_distance(state["real_lat"], state["real_lon"], req.target_lat, req.target_lon)
    return {"status": "ok", "current_lat": req.target_lat, "current_lon": req.target_lon, "distance": round(dist_to_real, 1)}

@app.post("/api/set-settings")
async def update_settings(req: SettingsRequest):
    cfg = load_config()
    with state["lock"]: # 確保執行緒安全
        if req.speed_kmh is not None:
            state["speed_kmh"] = max(1.0, min(req.speed_kmh, 1000.0))
            cfg["speed_kmh"] = state["speed_kmh"]
        if req.random_level is not None:
            state["random_level"] = max(0, min(req.random_level, 10))
            cfg["random_level"] = state["random_level"]
        if req.loop_mode is not None:
            state["loop_mode"] = req.loop_mode
            cfg["loop_mode"] = state["loop_mode"]
        if req.loop_count is not None:
            state["loop_count"] = max(1, req.loop_count)
            cfg["loop_count"] = state["loop_count"]

    save_config(cfg)
    update_eta_calculation() # 立即觸發 ETA 重新計算！

    return {
        "status": "ok",
        "speed_kmh": state["speed_kmh"],
        "random_level": state["random_level"],
        "loop_mode": state["loop_mode"],
        "loop_count": state["loop_count"],
        "eta_seconds": state["eta_seconds"] # 回傳最新預估秒數
    }

@app.post("/api/save-route")
async def save_new_route(req: SaveRouteRequest):
    cfg = load_config()
    routes = cfg.get("saved_routes", [])
    new_route = {
        "id": f"route_{int(time.time())}",
        "name": req.name.strip() or f"自訂路線 ({len(req.waypoints)}點)",
        "waypoints": req.waypoints
    }
    routes.append(new_route)
    cfg["saved_routes"] = routes
    save_config(cfg)
    return {"status": "ok", "saved_routes": routes}

@app.delete("/api/delete-route/{route_id}")
async def delete_route(route_id: str):
    cfg = load_config()
    routes = [r for r in cfg.get("saved_routes", []) if r.get("id") != route_id]
    cfg["saved_routes"] = routes
    save_config(cfg)
    return {"status": "ok", "saved_routes": routes}

# 🎯【真正平滑折返起點】：以 10-25 步動態插值平滑移動回原位後再停止連線
@app.post("/api/stop-smooth")
async def stop_simulation_smooth():
    if not gps_service.is_connected:
        return {"status": "not_running"}

    state["is_cruising"] = False
    state["is_stopping"] = True

    start_lat = state["current_lat"]
    start_lon = state["current_lon"]
    target_lat = state["real_lat"]
    target_lon = state["real_lon"]

    total_dist = calculate_distance(start_lat, start_lon, target_lat, target_lon)
    if total_dist > 3.0:
        steps = min(30, max(12, int(total_dist / 15)))
        for i in range(1, steps + 1):
            ratio = i / steps
            cur_lat = start_lat + (target_lat - start_lat) * ratio
            cur_lon = start_lon + (target_lon - start_lon) * ratio
            state["current_lat"] = cur_lat
            state["current_lon"] = cur_lon
            gps_service.set_location(cur_lat, cur_lon)
            await asyncio.sleep(0.08)

    state["current_lat"] = target_lat
    state["current_lon"] = target_lon
    gps_service.set_location(target_lat, target_lon)

    try:
        await asyncio.sleep(0.4)
    except asyncio.CancelledError:
        pass

    gps_service.stop()
    state["is_simulating"] = False
    state["is_stopping"] = False
    send_macos_notification("SIM-GPS", "已恢復真實定位", "iPhone 定位已安全平滑還原。", "Ping")

    return {"status": "stopped_smooth"}

@app.post("/api/stop-force")
async def stop_simulation_force():
    state["is_cruising"] = False
    gps_service.stop()
    state["is_simulating"] = False
    state["is_stopping"] = False
    return {"status": "stopped_force"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
