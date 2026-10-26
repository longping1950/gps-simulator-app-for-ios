import os
import sys

# 1. 必須在任何 pymobiledevice3 子模組 import 前最優先設定，避開 macOS utun 與權限問題
os.environ["PYMOBILEDEVICE3_USERSPACE_TUNNEL"] = "1"

# 確保內嵌 Python (EmbeddedPython) 在任何 Mac 上皆具備正確的 SSL 憑證路徑 (供下載 DDI 與 Apple TSS 簽章使用)
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except Exception:
    pass

import pymobiledevice3.remote.tunnel_service as tunnel_service
tunnel_service.USE_USERSPACE_TUNNEL = True

import asyncio
from datetime import datetime
import json
import math
import random
import re
import signal
import subprocess
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from packaging.version import Version
from pydantic import BaseModel
import urllib.parse
import urllib.request

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import (
    AlreadyMountedError,
    DeveloperModeIsNotEnabledError,
    MuxException,
    NoDeviceConnectedError,
    NotPairedError,
    PairingError,
    RSDRequiredError,
    UserDeniedPairingError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.mobile_image_mounter import (
    CRYPTEX_IMAGE_MIN_VERSION,
    MobileImageMounterService,
    auto_mount,
    fetch_personalized_ddi,
    image_type_for_device,
    uses_personalized_image,
)

# 接收 Swift 傳遞的動態參數：app.py <PORT> <TOKEN> <NOTIFY_PORT>
PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8000
APP_INTERNAL_TOKEN = sys.argv[2] if len(sys.argv) > 2 else None
NOTIFY_PORT = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None

# 自動探測 web 靜態目錄位置
APP_DIR = Path(__file__).resolve().parent
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

# --- 🐞 Debug 錯誤診斷與配套措施紀錄中心 ---
_debug_lock = threading.Lock()
_debug_seq = 0
debug_logs: List[Dict[str, Any]] = []
MAX_DEBUG_LOGS = 100


def _infer_error_solution(error_text: str, stage: str = "") -> str:
    """根據錯誤訊息與發生階段，自動對應具體的排查與配套措施。"""
    lower = f"{error_text} {stage}".lower()
    if any(k in lower for k in ["notpaired", "userdeniedpairing", "pairingerror", "信任", "pair"]):
        return (
            "1. 請將 iPhone 螢幕解鎖並保持亮屏，使用傳輸線連接 Mac。\n"
            "2. 在手機跳出的「要信任這部電腦嗎？」視窗點擊「信任」並輸入手機鎖屏密碼。\n"
            "3. 若未跳出信任視窗，請重新拔插傳輸線或打開 Finder 點擊左側 iPhone 圖示觸發信任。"
        )
    if any(k in lower for k in ["developermode", "開發者模式"]):
        return (
            "1. 請至 iPhone「設定 > 隱私權與安全性 > 開發者模式」將開關打開。\n"
            "2. 依提示重新啟動手機，開機解鎖後在彈出視窗點擊「開啟」並輸入手機密碼。\n"
            "3. 可點擊左上角「❓ 指引」按鈕查看完整圖文步驟。"
        )
    if any(k in lower for k in ["ddi", "developerdiskimage", "tss", "manifest", "ssl", "certificate", "urlopen", "映像檔"]):
        return (
            "1. 首次為新 iPhone 掛載開發者映像檔 (DDI) 時，Mac 必須連上網際網路（需從 GitHub 下載映像檔並向 Apple TSS 伺服器申請簽章）。\n"
            "2. 請確認 Mac 網路連線正常、未開啟阻擋 GitHub 或 Apple 伺服器的 VPN/防火牆，然後重新點擊「連線」。"
        )
    if any(k in lower for k in ["invalidservice", "dtservicehub", "startserviceerror", "dvt"]):
        return (
            "手機上的 DVT 虛擬定位服務尚未啟動（通常因手機重開機後開發者映像檔已卸載，或為全新未掛載過的手機）。\n"
            "配套措施：請先插上 USB 傳輸線再點擊「連線」，系統將自動為此手機下載並掛載 DDI 開發者映像檔。"
        )
    if any(k in lower for k in ["nodeviceconnected", "usbmux", "remotepairing", "userspacetunnelunavailable", "偵測不到"]):
        return (
            "1. 有線連接：請確認使用支援資料傳輸的 USB 線（非純充電線），且手機已解鎖。\n"
            "2. 無線連接：新手機必須先插線成功連線一次（系統會自動為手機開啟 Wi-Fi 無線探索），之後才能在相同 Wi-Fi 網段下無線連線。\n"
            "3. macOS 權限：請確認「系統設定 > 隱私權與安全性 > 區域網路」中已允許本 App 存取。"
        )
    if any(k in lower for k in ["terminated", "incompleteread", "reset", "brokenpipe", "連線中斷", "timeout"]):
        return (
            "與 iPhone 的長連接通道意外中斷（可能因 USB 接觸不良、Wi-Fi 訊號波動或手機進入深層省電）。\n"
            "配套措施：系統已嘗試自動重連；若仍未恢復，請點擊「🔄 掃描」後重新點擊「連線」。"
        )
    return (
        "1. 請確認 iPhone 已解鎖、已開啟「開發者模式」並已信任此 Mac。\n"
        "2. 建議先插上 USB 傳輸線點擊「🔄 掃描」與「連線」，完成初次自動配置後再切換至無線使用。"
    )


def add_debug_log(
    title: str,
    error_msg: str,
    stage: str = "系統執行",
    level: str = "ERROR",
    solution: Optional[str] = None,
    udid: Optional[str] = None,
) -> Dict[str, Any]:
    global _debug_seq
    resolved_solution = solution or _infer_error_solution(f"{title} {error_msg}", stage)
    with _debug_lock:
        _debug_seq += 1
        entry = {
            "id": _debug_seq,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "stage": stage,
            "title": title,
            "error": str(error_msg).strip(),
            "solution": resolved_solution,
            "udid": udid or "",
        }
        debug_logs.insert(0, entry)
        if len(debug_logs) > MAX_DEBUG_LOGS:
            debug_logs.pop()
        return entry


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


async def _ensure_ddi_mounted(provider) -> None:
    """檢查裝置是否已掛載 Developer Disk Image (DDI)，若未掛載則預先下載並自動掛載。"""
    image_type = image_type_for_device(provider)
    async with MobileImageMounterService(lockdown=provider) as mounter:
        if await mounter.is_image_mounted(image_type):
            print(f"✅ [DDI] 裝置已掛載 {image_type} 開發者映像檔，無需重複掛載。")
            return

    print(f"⏳ [DDI] 偵測到手機尚未掛載 {image_type} DDI，準備自動下載與掛載...")
    # 將同步下載 DDI 的步驟放到背景執行緒，避免阻塞 Userspace PyTCP 事件迴圈
    if uses_personalized_image(provider):
        if Version(provider.product_version) >= CRYPTEX_IMAGE_MIN_VERSION:
            from pymobiledevice3.services.cryptexd import fetch_cryptex_ddi
            await asyncio.to_thread(fetch_cryptex_ddi)
        else:
            await asyncio.to_thread(fetch_personalized_ddi)

    try:
        await auto_mount(provider)
        print(f"✅ [DDI] {image_type} 開發者映像檔自動掛載完成！")
    except AlreadyMountedError:
        print(f"✅ [DDI] {image_type} 開發者映像檔已掛載。")


# --- 核心：非同步長連接 Session 服務 ---
class IOSUserspaceGpsService:
    def __init__(self):
        self.cmd_queue: Optional[asyncio.Queue] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.is_connected = False
        self.is_connecting = False
        self.is_running = False
        self.user_requested_stop = False
        self.current_udid: Optional[str] = None
        self.connect_stage: str = ""
        self.last_error: Optional[str] = None
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
                while not self.cmd_queue.empty():
                    try:
                        self.cmd_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                udid, init_lat, init_lon = payload
                self.current_udid = udid
                self.user_requested_stop = False
                await self._run_dvt_session(udid, init_lat, init_lon)
            elif action == "SHUTDOWN":
                break

    async def _run_sim_loop(self, sim: LocationSimulation, init_lat: float, init_lon: float):
        self.sim = sim
        await sim.set(init_lat, init_lon)
        self.is_connected = True
        self.is_running = True
        self.is_connecting = False
        self.connect_stage = "已連線"
        print(f"✅ [Userspace] 會話已就緒！起點座標鎖定: {init_lat:.6f}, {init_lon:.6f}")

        while self.is_running:
            try:
                action, payload = await asyncio.wait_for(self.cmd_queue.get(), timeout=1.0)
                if action == "SET":
                    lat, lon = payload
                    await sim.set(lat, lon)
                elif action == "STOP":
                    self.user_requested_stop = True
                    break
                elif action == "START":
                    await self.cmd_queue.put(("START", payload))
                    break
            except asyncio.TimeoutError:
                continue

        print("🛑 [Userspace] 正在清理定位模擬...")
        try:
            await asyncio.wait_for(sim.clear(), timeout=2.5)
            print("✅ [Userspace] 已恢復真機 GPS 定位。")
        except Exception as e:
            print(f"清理異常: {e}")

    async def _run_dvt_session(self, udid: str, init_lat: float, init_lon: float):
        self.is_connecting = True
        self.last_error = None
        self.connect_stage = "正在檢查裝置與信任狀態..."
        target_serial = None if (not udid or udid == "default") else udid
        ios_major = 17
        ddi_mounted_over_usbmux = False
        was_connected_once = False

        try:
            # Step 1: 嘗試透過 usbmux 連線 Lockdown (檢查信任配對、開發者模式、啟用 Wi-Fi 同步，並高速掛載 DDI)
            print(f"⏳ [1/4] 正在檢查裝置配對與狀態 (UDID: {target_serial or '自動偵測'})...")
            try:
                async with await create_using_usbmux(serial=target_serial, autopair=True) as lockdown:
                    target_serial = lockdown.udid
                    self.current_udid = target_serial
                    ios_version = Version(lockdown.product_version)
                    ios_major = ios_version.major
                    print(f"✅ [1/4] 已連線 Lockdown: {lockdown.display_name or lockdown.product_type} (iOS {lockdown.product_version})")

                    # 檢查開發者模式 (iOS 16+)
                    if lockdown.has_developer_mode and not await lockdown.get_developer_mode_status():
                        self.last_error = (
                            "偵測到 iPhone 尚未開啟「開發者模式」！\n"
                            "請至 iPhone「設定 > 隱私權與安全性 > 開發者模式」開啟並重新啟動手機後再試。"
                        )
                        add_debug_log(
                            title="手機尚未開啟開發者模式",
                            error_msg=f"裝置 {lockdown.display_name or target_serial} (iOS {lockdown.product_version}) DeveloperModeStatus 為 False",
                            stage="Lockdown 檢查",
                            udid=target_serial,
                        )
                        print(f"❌ [Userspace] {self.last_error}")
                        return

                    # 若目前透過 USB 有線連接，自動為該手機啟用 Wi-Fi 無線連線支援，方便日後拔線使用
                    if lockdown.service.mux_device and lockdown.service.mux_device.is_usb:
                        try:
                            await lockdown.set_enable_wifi_connections(True)
                            print("✅ [Wi-Fi] 已自動為此 iPhone 開啟 Wi-Fi 無線探索支援")
                        except Exception as wifi_err:
                            print(f"ℹ️ [Wi-Fi] 設定 Wi-Fi 連接旗標略過: {wifi_err}")

                    # 嘗試直接在原生 usbmux 通道上檢查並高速掛載 DDI (比在 Userspace PyTCP 隧道內上傳快數倍)
                    self.connect_stage = "正在檢查並掛載開發者映像檔 (首次約需 15~30 秒)..."
                    try:
                        await _ensure_ddi_mounted(lockdown)
                        ddi_mounted_over_usbmux = True
                    except RSDRequiredError:
                        print("ℹ️ [DDI] 此 iOS 版本需透過 RSD 隧道安裝 Cryptex1 DDI，將於隧道建立後執行。")
                        ddi_mounted_over_usbmux = False
                    except Exception as ddi_err:
                        add_debug_log(
                            title="透過 usbmux 自動掛載 DDI 失敗",
                            error_msg=f"{type(ddi_err).__name__}: {ddi_err}",
                            stage="DDI 映像檔掛載",
                            level="WARN",
                            udid=target_serial,
                        )
                        raise

                    # 若為 iOS 16 及更舊版本，無需建立 RSD 隧道，直接透過 Lockdown 啟動 DvtProvider
                    if ios_major < 17:
                        self.connect_stage = "正在啟動定位模擬服務..."
                        print("⏳ [iOS < 17] 正在直接透過 Lockdown 掛載 DvtProvider 與 LocationSimulation...")
                        async with DvtProvider(lockdown) as dvt, LocationSimulation(dvt) as sim:
                            was_connected_once = True
                            await self._run_sim_loop(sim, init_lat, init_lon)
                        return

            except (NoDeviceConnectedError, MuxException) as mux_err:
                print(f"ℹ️ [usbmux] 未透過 usbmux 直接連線 ({mux_err})，嘗試使用 RSD 遠端配對通道...")

            # Step 2: iOS 17+ 建立免 root 的 RSD 隧道
            self.connect_stage = "正在建立 RSD 安全隧道..."
            print(f"⏳ [2/4] 正在與裝置 ({target_serial or '自動'}) 建立 RSD 隧道...")
            async with PreferredRsdTunnel(serial=target_serial, autopair=True) as rsd:
                if rsd.udid:
                    self.current_udid = rsd.udid
                print(f"✅ [2/4] RSD 隧道建立成功！裝置: {rsd.product_type} (iOS {rsd.product_version})")

                # Step 3: 若方才尚未透過 usbmux 掛載 DDI (如 iOS 27+ 或純 Bonjour 連線)，在 RSD 隧道上確保 DDI 已掛載
                if not ddi_mounted_over_usbmux:
                    self.connect_stage = "正在透過隧道掛載開發者映像檔 (首次約需 15~30 秒)..."
                    await _ensure_ddi_mounted(rsd)

                # Step 4: 掛載 DvtProvider 與 LocationSimulation
                self.connect_stage = "正在啟動定位模擬服務..."
                print("⏳ [4/4] 正在掛載 DvtProvider 與 LocationSimulation...")
                async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as sim:
                    was_connected_once = True
                    await self._run_sim_loop(sim, init_lat, init_lon)

        except (NotPairedError, UserDeniedPairingError, PairingError) as e:
            self.last_error = (
                "尚未完成手機信任配對！\n"
                "請將 iPhone 解鎖、使用傳輸線連接 Mac，並在手機跳出的視窗點選「信任這部電腦」並輸入手機密碼。"
                f"\n({type(e).__name__})"
            )
            add_debug_log(
                title="手機尚未完成信任配對",
                error_msg=f"{type(e).__name__}: {e}",
                stage="裝置信任配對",
                udid=target_serial,
            )
            print(f"❌ [Userspace] {self.last_error}")
        except DeveloperModeIsNotEnabledError as e:
            self.last_error = (
                "偵測到 iPhone 尚未開啟「開發者模式」！\n"
                "請至 iPhone「設定 > 隱私權與安全性 > 開發者模式」開啟並重新啟動手機。"
            )
            add_debug_log(
                title="開發者模式未啟用",
                error_msg=f"{type(e).__name__}: {e}",
                stage="開發者模式檢查",
                udid=target_serial,
            )
            print(f"❌ [Userspace] {self.last_error}")
        except NoDeviceConnectedError as e:
            self.last_error = (
                "偵測不到 iPhone 裝置！\n"
                "首次使用請先插上 USB 傳輸線，解鎖手機並點選「信任這部電腦」；完成初次有線連線後即可支援相同 Wi-Fi 無線連接。"
            )
            add_debug_log(
                title="找不到可用的 iPhone 裝置",
                error_msg=f"{type(e).__name__}: {e}",
                stage="裝置探索",
                udid=target_serial,
            )
            print(f"❌ [Userspace] {self.last_error}")
        except Exception as e:
            self.last_error = (
                f"無法與 iPhone 建立或維持定位通道 ({type(e).__name__}: {e})。\n"
                "請確認手機已解鎖、已開啟開發者模式，且首次連線時 Mac 具備網路連線以自動下載開發者映像檔。"
            )
            stage_label = "DVT 定位維持中斷" if was_connected_once else (self.connect_stage or "建立連線通道")
            add_debug_log(
                title="連線通道異常中斷" if was_connected_once else "建立連線或掛載服務失敗",
                error_msg=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}",
                stage=stage_label,
                udid=target_serial,
            )
            print(f"❌ [Userspace] DVT 運行異常: {type(e).__name__}: {e}")

            # 若原本已連線成功卻在模擬途中意外斷線（非使用者主動停止），嘗試自動重連一次鎖定當前座標
            if was_connected_once and not self.user_requested_stop:
                send_macos_notification("SIM-GPS 警報", "裝置連線中斷", "與 iPhone 連線意外中斷，正在嘗試自動重連...", sound_name="Basso")
                cur_lat = state.get("current_lat") or init_lat
                cur_lon = state.get("current_lon") or init_lon
                await asyncio.sleep(1.5)
                if not self.user_requested_stop:
                    print(f"🔄 [Auto-Reconnect] 正在嘗試自動重連 {target_serial}...")
                    await self.cmd_queue.put(("START", (target_serial or "default", cur_lat, cur_lon)))
        finally:
            self.sim = None
            self.is_connected = False
            self.is_running = False
            self.is_connecting = False
            self.connect_stage = ""
            if state["is_stopping"]:
                state["is_stopping"] = False
                state["is_returning_home"] = False
            if not self.cmd_queue or self.cmd_queue.empty():
                state["is_simulating"] = False

    def connect(self, udid: str, lat: float, lon: float):
        self.start_worker()
        self.is_connecting = True
        self.user_requested_stop = False
        self.last_error = None
        asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("START", (udid, lat, lon))), self.loop)

    def set_location(self, lat: float, lon: float):
        if self.is_connected and self.loop:
            asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("SET", (lat, lon))), self.loop)

    def stop(self):
        self.user_requested_stop = True
        if (self.is_connected or self.is_running) and self.loop:
            asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("STOP", None)), self.loop)

gps_service = IOSUserspaceGpsService()
gps_service.start_worker()


# 註冊系統終止信號 (SIGTERM / SIGINT)，確保按 Cmd+Q 或關閉程式時主動清除虛擬定位
def _graceful_shutdown_handler(signum, frame):
    print(f"🛑 收到系統退出信號 ({signum})，正在安全還原 iPhone 真實定位...")
    try:
        if gps_service.is_connected:
            gps_service.stop()
            time.sleep(0.6)
    except Exception:
        pass
    sys.exit(0)

signal.signal(signal.SIGTERM, _graceful_shutdown_handler)
signal.signal(signal.SIGINT, _graceful_shutdown_handler)


def calculate_distance(lat1, lon1, lat2, lon2):
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    return r * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def clamp_step_to_speed_limit(
    prev_lat: float,
    prev_lon: float,
    cand_lat: float,
    cand_lon: float,
    max_speed_kmh: float,
    dt: float,
) -> tuple[float, float, float]:
    """
    🎯【嚴格速度上限鎖 (Hard Velocity Clamp)】：
    確保加入任何真實 GPS 微幅抖動 (Jitter) 後，相鄰兩點間的實際球面距離除以 dt 絕對不會超過設定的 speed_kmh！
    回傳: (clamped_lat, clamped_lon, actual_distance_meters)
    """
    max_step_meters = max(0.01, (max_speed_kmh / 3.6) * dt)
    actual_dist = calculate_distance(prev_lat, prev_lon, cand_lat, cand_lon)
    # 預留 0.5% 浮點數安全邊界，確保瞬時時速絕不超標
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
    """
    在移動目標點上疊加微幅真實 GPS 訊號漂移，並透過 clamp_step_to_speed_limit 強制鎖定不超速。
    """
    if random_level <= 0 or max_speed_kmh >= 500.0:
        clat, clon, _ = clamp_step_to_speed_limit(prev_lat, prev_lon, base_lat, base_lon, max_speed_kmh, dt)
        return clat, clon

    max_step_meters = (max_speed_kmh / 3.6) * dt
    base_dist = calculate_distance(prev_lat, prev_lon, base_lat, base_lon)
    # 抖動半徑隨 random_level 調整 (約 0.05m ~ 0.35m)，且絕不超過可用的速度餘裕 (max_step_meters - base_dist)
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
    "anchor_lat": init_lat,
    "anchor_lon": init_lon,
    "is_simulating": False,
    "is_stopping": False,
    "is_returning_home": False,
    "speed_kmh": cfg_data.get("speed_kmh", 18.0),
    "random_level": cfg_data.get("random_level", 0),
    "loop_mode": cfg_data.get("loop_mode", "none"),
    "loop_count": cfg_data.get("loop_count", 1),
    "pause_remaining": 0.0,
    "actual_speed_kmh": 0.0,
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

async def scan_ios_devices_async() -> List[Dict]:
    """直接透過 usbmux 協定掃描裝置，避免 subprocess 超時或因單一未配對/休眠裝置導致整體列表失敗。"""
    devices: List[Dict] = []
    try:
        mux_devs = await usbmux.list_devices()
        by_udid: Dict[str, usbmux.MuxDevice] = {}
        for d in mux_devs:
            existing = by_udid.get(d.serial)
            if existing is None or (d.is_usb and not existing.is_usb):
                by_udid[d.serial] = d

        for udid, mux_dev in by_udid.items():
            conn_type = "USB" if mux_dev.is_usb else "Network (Wi-Fi)"
            model_name = "iPhone"
            display_label = f"iOS 裝置 ({conn_type})"

            try:
                lockdown = await asyncio.wait_for(
                    create_using_usbmux(
                        serial=udid,
                        autopair=False,
                        connection_type=mux_dev.connection_type,
                    ),
                    timeout=1.2,
                )
                try:
                    prod_type = lockdown.display_name or lockdown.product_type or "iPhone"
                    prod_ver = lockdown.product_version or ""
                    dev_name = lockdown.all_values.get("DeviceName")
                    model_name = prod_type
                    if dev_name:
                        display_label = f"{dev_name} ({prod_type})"
                    elif prod_ver:
                        display_label = f"{prod_type} (iOS {prod_ver})"
                    else:
                        display_label = prod_type
                finally:
                    await lockdown.close()
            except Exception:
                pass

            devices.append({
                "udid": udid,
                "connection": conn_type,
                "model": model_name,
                "name": display_label,
            })
    except Exception as e:
        print(f"[Scan] usbmux 掃描發生異常: {e}")
        add_debug_log("掃描 iOS 裝置異常", str(e), stage="裝置掃描", level="WARN")

    if not devices:
        devices.append({
            "udid": "default",
            "connection": "Auto (預設通道)",
            "model": "iPhone",
            "name": "本機偵測裝置 (預設)"
        })
    return devices


def get_effective_step_meters(dt: float, allow_random_pause: bool = True) -> float:
    """
    計算時間步 dt (秒) 內的推進距離 (公尺)。
    當開啟擬真降速 (random_level > 0) 時，最高推進速度限制在 speed_kmh 的 95% 以內，
    預留 5% 餘裕給微幅方向抖動，確保疊加抖動後絕對不會超過 speed_kmh 上限！
    """
    base_kmh = state["speed_kmh"]
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

    # 擬真速度波動：只向下減速 (保留 5% 給微幅抖動，絕不向上超速)
    max_reduction = min(0.55, state["random_level"] * 0.05)
    min_factor = max(0.35, 0.95 - max_reduction)
    speed_factor = random.uniform(min_factor, 0.95)
    eff_kmh = max(0.8, base_kmh * speed_factor)
    return min((eff_kmh / 3.6) * dt, max_step * 0.95)


def update_eta_calculation():
    with state["lock"]:
        cur_lat = state["current_lat"]
        cur_lon = state["current_lon"]
        spd_mps = (state["speed_kmh"] or 18.0) / 3.6

        # 🎯 若正處於「平滑返航回原點」模式，計算回到真實起點的距離與預估時間
        if state["is_stopping"] or state["is_returning_home"]:
            home_dist = calculate_distance(cur_lat, cur_lon, state["real_lat"], state["real_lon"])
            state["target_remaining_dist"] = round(home_dist, 1)
            state["total_remaining_dist"] = home_dist
            state["eta_seconds"] = int(math.ceil(home_dist / spd_mps)) if spd_mps > 0 else 0
            return

        wps = state["cruise_waypoints"]
        if not wps:
            state["total_remaining_dist"] = 0.0
            state["eta_seconds"] = 0
            return

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
        state["eta_seconds"] = int(math.ceil(total_dist / spd_mps)) if spd_mps > 0 else 0


def backend_cruise_worker():
    TICK_SEC = 0.5
    last_tick = time.monotonic()
    last_stationary_jitter = time.monotonic()

    while True:
        try:
            now = time.monotonic()
            dt = max(0.1, min(1.5, now - last_tick))
            last_tick = now

            if not gps_service.is_connected:
                state["actual_speed_kmh"] = 0.0
                time.sleep(TICK_SEC)
                continue

            # 🎯 模式 A：平滑回歸起點 (依照使用者設定的 speed_kmh 返航回原位後結束模擬)
            if state["is_stopping"] or state["is_returning_home"]:
                cur_lat = state["current_lat"]
                cur_lon = state["current_lon"]
                target_lat = state["real_lat"]
                target_lon = state["real_lon"]

                dist = calculate_distance(cur_lat, cur_lon, target_lat, target_lon)
                state["target_remaining_dist"] = round(dist, 1)
                state["total_remaining_dist"] = dist

                # 返航時不觸發隨機紅燈停等，以設定速度穩定平滑返回原點
                step_meters = get_effective_step_meters(dt, allow_random_pause=False)

                if dist <= max(1.2, step_meters):
                    # 已抵達原點：精確對齊原點後斷開並恢復真實定位
                    state["current_lat"] = target_lat
                    state["current_lon"] = target_lon
                    state["anchor_lat"] = target_lat
                    state["anchor_lon"] = target_lon
                    state["target_remaining_dist"] = 0.0
                    state["total_remaining_dist"] = 0.0
                    state["eta_seconds"] = 0
                    state["actual_speed_kmh"] = 0.0
                    gps_service.set_location(target_lat, target_lon)
                    time.sleep(0.4)
                    gps_service.stop()
                    state["is_simulating"] = False
                    state["is_stopping"] = False
                    state["is_returning_home"] = False
                    send_macos_notification("SIM-GPS", "已恢復真實定位", "iPhone 已依設定速度平滑返航並還原真實定位。", "Ping")
                else:
                    ratio = step_meters / dist
                    cand_lat = cur_lat + (target_lat - cur_lat) * ratio
                    cand_lon = cur_lon + (target_lon - cur_lon) * ratio
                    next_lat, next_lon = apply_safe_gps_jitter(
                        cur_lat, cur_lon, cand_lat, cand_lon, state["speed_kmh"], state["random_level"], dt
                    )
                    actual_moved = calculate_distance(cur_lat, cur_lon, next_lat, next_lon)
                    state["actual_speed_kmh"] = round((actual_moved / dt) * 3.6, 1)
                    state["current_lat"] = next_lat
                    state["current_lon"] = next_lon
                    state["anchor_lat"] = next_lat
                    state["anchor_lon"] = next_lon
                    gps_service.set_location(next_lat, next_lon)
                    update_eta_calculation()

            # 🎯 模式 B：沿航點路徑自主巡航
            elif state["is_cruising"]:
                with state["lock"]:
                    wps = state["cruise_waypoints"]
                    if not wps:
                        state["is_cruising"] = False
                        state["actual_speed_kmh"] = 0.0
                        send_macos_notification("SIM-GPS", "巡航完成", "所有航點已到達完畢！", "Glass")
                        time.sleep(TICK_SEC)
                        continue
                    target = wps[0]

                cur_lat = state["current_lat"]
                cur_lon = state["current_lon"]
                dist = calculate_distance(cur_lat, cur_lon, target["lat"], target["lng"])
                step_meters = get_effective_step_meters(dt, allow_random_pause=True)

                state["target_remaining_dist"] = round(dist, 1)

                if step_meters > 0.0:
                    if dist <= step_meters:
                        # 抵達當前航點，仍需確保這一步不超過設定的速度上限
                        next_lat, next_lon, actual_moved = clamp_step_to_speed_limit(
                            cur_lat, cur_lon, target["lat"], target["lng"], state["speed_kmh"], dt
                        )
                        arrived = calculate_distance(next_lat, next_lon, target["lat"], target["lng"]) <= 0.5
                    else:
                        ratio = step_meters / dist
                        cand_lat = cur_lat + (target["lat"] - cur_lat) * ratio
                        cand_lon = cur_lon + (target["lng"] - cur_lon) * ratio
                        next_lat, next_lon = apply_safe_gps_jitter(
                            cur_lat, cur_lon, cand_lat, cand_lon, state["speed_kmh"], state["random_level"], dt
                        )
                        actual_moved = calculate_distance(cur_lat, cur_lon, next_lat, next_lon)
                        arrived = False

                    state["actual_speed_kmh"] = round((actual_moved / dt) * 3.6, 1)
                    state["current_lat"] = next_lat
                    state["current_lon"] = next_lon
                    state["anchor_lat"] = next_lat
                    state["anchor_lon"] = next_lon
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
                                        state["actual_speed_kmh"] = 0.0
                                        send_macos_notification("SIM-GPS", "巡航完成", f"已完成指定的 {state['target_loop_count']} 圈巡航！", "Glass")
                            else:
                                state["cruise_waypoints"].pop(0)
                                if len(state["cruise_waypoints"]) == 0:
                                    state["is_cruising"] = False
                                    state["actual_speed_kmh"] = 0.0
                                    send_macos_notification("SIM-GPS", "巡航完成", "單次巡航任務已結束！", "Glass")
                else:
                    state["actual_speed_kmh"] = 0.0

                update_eta_calculation()

            # 🎯 模式 C：定點駐留微幅漂移 (當 random_level > 0 時，每 2 秒在錨點周圍微幅漂移，且嚴格限制不超速)
            else:
                state["actual_speed_kmh"] = 0.0
                if state["random_level"] > 0 and (now - last_stationary_jitter) >= 2.0:
                    jitter_dt = now - last_stationary_jitter
                    last_stationary_jitter = now
                    anchor_lat = state.get("anchor_lat") or state["current_lat"]
                    anchor_lon = state.get("anchor_lon") or state["current_lon"]
                    cur_lat = state["current_lat"]
                    cur_lon = state["current_lon"]

                    # 在錨點周圍 0.15m ~ 0.55m 圓內產生微幅熱雜訊，且單步移動速度上限不超過 min(0.8 km/h, 設定速度 * 0.4)
                    radius_m = 0.15 + 0.04 * state["random_level"]
                    angle = random.uniform(0.0, 2.0 * math.pi)
                    r = random.uniform(0.0, radius_m)
                    lat_deg_per_m = 1.0 / 111111.0
                    lon_deg_per_m = 1.0 / max(1.0, 111111.0 * math.cos(math.radians(anchor_lat)))
                    cand_lat = anchor_lat + math.sin(angle) * r * lat_deg_per_m
                    cand_lon = anchor_lon + math.cos(angle) * r * lon_deg_per_m

                    safe_stationary_kmh = min(0.8, state["speed_kmh"] * 0.5)
                    next_lat, next_lon, _ = clamp_step_to_speed_limit(
                        cur_lat, cur_lon, cand_lat, cand_lon, safe_stationary_kmh, jitter_dt
                    )
                    state["current_lat"] = next_lat
                    state["current_lon"] = next_lon
                    gps_service.set_location(next_lat, next_lon)

        except Exception as e:
            print(f"[Cruise Worker] 異常: {e}")
            add_debug_log("背景巡航執行緒異常", f"{type(e).__name__}: {e}", stage="巡航推進", level="WARN")

        time.sleep(TICK_SEC)

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
    dt: Optional[float] = None

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
        "random_level": state["random_level"],
        "loop_mode": state["loop_mode"],
        "loop_count": state["loop_count"],
        "saved_routes": cfg.get("saved_routes", [])
    }

@app.get("/api/devices")
async def get_device_list():
    devs = await scan_ios_devices_async()
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

    with _debug_lock:
        err_count = len(debug_logs)
        latest_err_id = debug_logs[0]["id"] if debug_logs else 0

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
        "actual_speed_kmh": state["actual_speed_kmh"],
        "random_level": state["random_level"],
        "loop_mode": state["loop_mode"],
        "loop_count": state["loop_count"],
        "is_paused": state["pause_remaining"] > 0.0,
        "distance_from_origin": round(dist, 1),
        "is_cruising": state["is_cruising"],
        "cruise_waypoints": state["cruise_waypoints"],
        "current_loop_idx": state["current_loop_idx"],
        "target_loop_count": state["target_loop_count"],
        "target_remaining_dist": state["target_remaining_dist"],
        "total_remaining_dist": state["total_remaining_dist"],
        "eta_seconds": state["eta_seconds"],
        "debug_error_count": err_count,
        "latest_debug_id": latest_err_id,
    }

# --- 🐞 Debug 錯誤紀錄 API ---
@app.get("/api/debug-logs")
async def get_debug_logs():
    with _debug_lock:
        return {
            "logs": list(debug_logs),
            "total_count": len(debug_logs),
            "latest_id": debug_logs[0]["id"] if debug_logs else 0,
        }

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
    with _debug_lock:
        debug_logs.clear()
    return {"status": "cleared", "total_count": 0}


# --- 📍 地名 / 地址 / 座標搜尋代理 API (避免前端 CORS 或 User-Agent 限制) ---
@app.get("/api/geocode")
async def geocode_search(q: str):
    query = q.strip()
    if not query:
        return {"results": []}

    # 1. 優先判斷是否為直接輸入經緯度座標 (如 "25.0339, 121.5644")
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

    # 2. 向 OpenStreetMap Nominatim 查詢地標與地址
    def _fetch_nominatim():
        encoded = urllib.parse.quote(query)
        url = f"https://nominatim.openstreetmap.org/search?format=json&limit=5&q={encoded}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "SIM-GPS-macOS-Client/1.3",
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
    state["is_simulating"] = True
    state["is_stopping"] = False
    state["is_returning_home"] = False

    gps_service.connect(req.udid, lat, lon)

    # 首次連線若需點擊信任、自動下載並掛載 DDI 映像檔約需 15~45 秒，最高等待 60 秒
    for _ in range(600):
        if gps_service.is_connected:
            break
        if not gps_service.is_connecting and gps_service.last_error:
            break
        await asyncio.sleep(0.1)

    if not gps_service.is_connected:
        state["is_simulating"] = False
        err_msg = gps_service.last_error or "無法與 iPhone 建立長連接，請確認已解鎖並信任此電腦（首次連線需自動下載開發者映像檔，請保持網路暢通）"
        raise HTTPException(status_code=500, detail=err_msg)

    return {"status": "connected", "udid": gps_service.current_udid or req.udid, "lat": lat, "lon": lon}

@app.post("/api/device/disconnect")
async def disconnect_single_device(req: DeviceActionRequest):
    state["is_cruising"] = False
    state["is_stopping"] = False
    state["is_returning_home"] = False
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
        state["anchor_lat"] = state["current_lat"]
        state["anchor_lon"] = state["current_lon"]
    return {"status": "cruising_stopped"}

@app.post("/api/move")
async def move_by_vector(req: MoveVectorRequest):
    if not gps_service.is_connected or state["is_stopping"]:
        raise HTTPException(status_code=400, detail="裝置尚未連線")

    if state["is_cruising"]:
        state["is_cruising"] = False

    dt = max(0.1, min(1.5, req.dt if req.dt is not None else 0.5))
    vec_mag = math.hypot(req.dx, req.dy)
    if vec_mag <= 0.001:
        return {"status": "idle", "current_lat": state["current_lat"], "current_lon": state["current_lon"]}

    # 正規化方向並允許搖桿比例控制 (0 < vec_mag <= 1.0)，且單步絕不超過 speed_kmh 上限
    throttle = min(1.0, vec_mag)
    ux, uy = req.dx / vec_mag, req.dy / vec_mag

    step_meters = get_effective_step_meters(dt, allow_random_pause=False) * throttle
    if step_meters <= 0.0:
        return {"status": "paused", "current_lat": state["current_lat"], "current_lon": state["current_lon"]}

    cur_lat = state["current_lat"]
    cur_lon = state["current_lon"]

    delta_x_meters = ux * step_meters
    delta_y_meters = uy * step_meters

    lat_deg_per_meter = 1.0 / 111111.0
    lon_deg_per_meter = 1.0 / max(1.0, 111111.0 * math.cos(math.radians(cur_lat)))

    cand_lat = cur_lat + (delta_y_meters * lat_deg_per_meter)
    cand_lon = cur_lon + (delta_x_meters * lon_deg_per_meter)

    # 疊加不超速微幅抖動並強制限制在 speed_kmh 內
    next_lat, next_lon = apply_safe_gps_jitter(
        cur_lat, cur_lon, cand_lat, cand_lon, state["speed_kmh"], state["random_level"], dt
    )

    state["current_lat"] = next_lat
    state["current_lon"] = next_lon
    state["anchor_lat"] = next_lat
    state["anchor_lon"] = next_lon

    gps_service.set_location(next_lat, next_lon)
    dist = calculate_distance(state["real_lat"], state["real_lon"], next_lat, next_lon)
    return {"status": "ok", "current_lat": next_lat, "current_lon": next_lon, "distance": round(dist, 1)}

@app.post("/api/teleport")
async def teleport_to(req: TeleportRequest):
    if not gps_service.is_connected or state["is_stopping"]:
        raise HTTPException(status_code=400, detail="裝置尚未連線")

    state["current_lat"] = req.target_lat
    state["current_lon"] = req.target_lon
    state["anchor_lat"] = req.target_lat
    state["anchor_lon"] = req.target_lon
    gps_service.set_location(req.target_lat, req.target_lon)

    dist_to_real = calculate_distance(state["real_lat"], state["real_lon"], req.target_lat, req.target_lon)
    return {"status": "ok", "current_lat": req.target_lat, "current_lon": req.target_lon, "distance": round(dist_to_real, 1)}

@app.post("/api/set-settings")
async def update_settings(req: SettingsRequest):
    cfg = load_config()
    with state["lock"]:
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
    update_eta_calculation()

    return {
        "status": "ok",
        "speed_kmh": state["speed_kmh"],
        "random_level": state["random_level"],
        "loop_mode": state["loop_mode"],
        "loop_count": state["loop_count"],
        "eta_seconds": state["eta_seconds"]
    }

@app.post("/api/save-route")
async def save_new_route(req: SaveRouteRequest):
    cfg = load_config()
    routes = cfg.get("saved_routes", [])
    new_route = {
        "id": f"route_{int(time.time() * 1000)}",
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

# 🎯【真正以設定速度平滑返航回原位】：啟動背景返航任務，按照使用者設定的 speed_kmh 走回真實起點後再結束連線
@app.post("/api/stop-smooth")
async def stop_simulation_smooth():
    if not gps_service.is_connected:
        state["is_simulating"] = False
        state["is_stopping"] = False
        state["is_returning_home"] = False
        return {"status": "not_running"}

    with state["lock"]:
        state["is_cruising"] = False
        start_lat = state["current_lat"]
        start_lon = state["current_lon"]
        target_lat = state["real_lat"]
        target_lon = state["real_lon"]
        total_dist = calculate_distance(start_lat, start_lon, target_lat, target_lon)

        # 若距離起點已經非常近 (<= 1.5m)，直接復位並結束
        if total_dist <= 1.5:
            state["current_lat"] = target_lat
            state["current_lon"] = target_lon
            state["anchor_lat"] = target_lat
            state["anchor_lon"] = target_lon
            gps_service.set_location(target_lat, target_lon)
            await asyncio.sleep(0.3)
            gps_service.stop()
            state["is_simulating"] = False
            state["is_stopping"] = False
            state["is_returning_home"] = False
            send_macos_notification("SIM-GPS", "已恢復真實定位", "iPhone 定位已安全還原。", "Ping")
            return {"status": "stopped_smooth", "distance": 0.0, "eta_seconds": 0}

        # 啟動以設定速度 (speed_kmh) 返航模式，由 backend_cruise_worker 依設定速度推進回原點
        state["is_stopping"] = True
        state["is_returning_home"] = True

    update_eta_calculation()
    return {
        "status": "returning_home",
        "distance": round(total_dist, 1),
        "eta_seconds": state["eta_seconds"],
        "speed_kmh": state["speed_kmh"],
    }

@app.post("/api/stop-force")
async def stop_simulation_force():
    with state["lock"]:
        state["is_cruising"] = False
        state["is_stopping"] = False
        state["is_returning_home"] = False
        state["actual_speed_kmh"] = 0.0
    gps_service.stop()
    state["is_simulating"] = False
    return {"status": "stopped_force"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT)
