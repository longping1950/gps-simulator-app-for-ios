"""
gps_tunnel.py
iOS 裝置探索、DDI 開發者映像檔掛載狀態檢測、RemotePairing 自動配對、RSD/DVT 長連接定位通道與自動化控制介面
"""

import asyncio
from contextlib import AsyncExitStack, suppress
import platform
import threading
import time
import traceback
from typing import Any, Dict, List, Optional
from packaging.version import Version

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import (
    AlreadyMountedError,
    DeveloperModeIsNotEnabledError,
    InvalidServiceError,
    MuxException,
    NoDeviceConnectedError,
    NotPairedError,
    PairingError,
    PyMobileDevice3Exception,
    RemotePairingCompletedError,
    RSDRequiredError,
    StartServiceError,
    UserDeniedPairingError,
    UserspaceTunnelUnavailableError,
)
from pymobiledevice3.lockdown import LockdownClient, create_using_usbmux
from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
import pymobiledevice3.remote.tunnel_service as tunnel_service
from pymobiledevice3.remote.tunnel_service import (
    CoreDeviceTunnelProxy,
    RemotePairingLockdownService,
    get_remote_pairing_tunnel_services,
)
from pymobiledevice3.remote.userspace_tunnel import (
    UserspaceDialPlane,
    UserspaceTun,
    _lifecycle_lock,
)
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap
from pymobiledevice3.services.mobile_image_mounter import (
    CRYPTEX_IMAGE_MIN_VERSION,
    MobileImageMounterService,
    auto_mount,
    fetch_personalized_ddi,
    image_type_for_device,
    uses_personalized_image,
)

from debug_center import add_debug_log, send_macos_notification

tunnel_service.USE_USERSPACE_TUNNEL = True


async def _bootstrap_remotepairing_over_usb(lockdown: LockdownClient) -> None:
    if Version(lockdown.product_version).major < 17:
        return
    service = await RemotePairingLockdownService.create(lockdown)
    try:
        try:
            await asyncio.wait_for(service.connect(autopair=True), timeout=8.0)
        except RemotePairingCompletedError:
            await service.close()
            service = await RemotePairingLockdownService.create(lockdown)
            await asyncio.wait_for(service.connect(autopair=False), timeout=5.0)
        print(f"✅ [RemotePairing] 已為 {lockdown.udid} 自動建立免確認無線 RemotePairing 憑證")
    except Exception as e:
        print(f"ℹ️ [RemotePairing] 自動建立無線憑證略過 ({type(e).__name__}: {e})")
    finally:
        with suppress(Exception):
            await service.close()


async def check_device_ddi_status(provider) -> tuple[Optional[bool], Optional[bool]]:
    """
    快速檢測 iOS 裝置的「開發者模式」與「DDI 開發者映像檔是否已掛載」
    :returns: (dev_mode_enabled, ddi_mounted)
    """
    dev_mode_enabled: Optional[bool] = True
    ddi_mounted: Optional[bool] = None
    try:
        if getattr(provider, "has_developer_mode", False):
            dev_mode_enabled = bool(await asyncio.wait_for(provider.get_developer_mode_status(), timeout=1.0))
            if not dev_mode_enabled:
                return False, False
    except Exception:
        dev_mode_enabled = None

    try:
        image_type = image_type_for_device(provider)
        async with MobileImageMounterService(lockdown=provider) as mounter:
            ddi_mounted = bool(await asyncio.wait_for(mounter.is_image_mounted(image_type), timeout=1.2))
    except Exception:
        ddi_mounted = None

    return dev_mode_enabled, ddi_mounted


async def query_battery_from_lockdown(lockdown: LockdownClient) -> Dict[str, Any]:
    """
    透過 DiagnosticsService 或 Lockdown Battery Domain 讀取 iPhone 電量百分比、充電狀態與實體溫度 (°C)
    """
    bat_level: Optional[int] = None
    is_charging: Optional[bool] = None
    temp_c: Optional[float] = None

    try:
        async with DiagnosticsService(lockdown) as diag:
            info = await asyncio.wait_for(diag.get_battery(), timeout=1.8)
            if isinstance(info, dict):
                cap = info.get("CurrentCapacity")
                if isinstance(cap, (int, float)) and 0 <= cap <= 100:
                    bat_level = int(cap)
                chg = info.get("IsCharging")
                ext = info.get("ExternalConnected")
                if isinstance(chg, bool):
                    is_charging = chg or bool(ext)
                raw_temp = info.get("Temperature")
                if isinstance(raw_temp, (int, float)) and raw_temp > 0:
                    # Apple IOPMPowerSource Temperature 單位為 0.01°C (例如 3580 = 35.8°C)
                    temp_c = round(raw_temp / 100.0, 1) if raw_temp > 200 else round(float(raw_temp), 1)
    except Exception:
        pass

    if bat_level is None:
        try:
            b_dom = await asyncio.wait_for(lockdown.get_value(domain="com.apple.mobile.battery"), timeout=1.0)
            if isinstance(b_dom, dict):
                cap = b_dom.get("BatteryCurrentCapacity")
                if isinstance(cap, (int, float)):
                    bat_level = int(cap)
                chg = b_dom.get("BatteryIsCharging")
                ext = b_dom.get("ExternalConnected")
                if isinstance(chg, bool):
                    is_charging = chg or bool(ext)
        except Exception:
            pass

    return {
        "battery_level": bat_level,
        "is_charging": is_charging,
        "temperature_c": temp_c,
    }


async def _ensure_ddi_mounted(provider, service_ref=None) -> None:
    image_type = image_type_for_device(provider)
    async with MobileImageMounterService(lockdown=provider) as mounter:
        if await mounter.is_image_mounted(image_type):
            print(f"✅ [DDI] 裝置已掛載 {image_type} 開發者映像檔，無需重複掛載。")
            return

    print(f"⏳ [DDI] 偵測到手機尚未掛載 {image_type} DDI，準備自動下載與掛載...")
    if service_ref is not None:
        service_ref.connect_stage = "正在從雲端下載 Apple 開發者映像檔 (首次約需 10~20 秒)..."

    if uses_personalized_image(provider):
        if Version(provider.product_version) >= CRYPTEX_IMAGE_MIN_VERSION:
            from pymobiledevice3.services.cryptexd import fetch_cryptex_ddi
            await asyncio.to_thread(fetch_cryptex_ddi)
        else:
            await asyncio.to_thread(fetch_personalized_ddi)

    if service_ref is not None:
        service_ref.connect_stage = "正在向 Apple 申請專屬簽章並掛載映像檔至手機..."

    try:
        await auto_mount(provider)
        print(f"✅ [DDI] {image_type} 開發者映像檔自動掛載完成！")
    except AlreadyMountedError:
        print(f"✅ [DDI] {image_type} 開發者映像檔已掛載。")


class ResilientRsdTunnel:
    def __init__(self, serial: Optional[str] = None, autopair: bool = True):
        self.serial = serial
        self.autopair = autopair
        self._stack: Optional[AsyncExitStack] = None
        self._native_tunnel = None
        self.rsd: Optional[RemoteServiceDiscoveryService] = None

    async def _pick_userspace_provider(self):
        lockdown = None
        resolved_udid = self.serial
        try:
            lockdown = await create_using_usbmux(serial=self.serial, autopair=self.autopair)
            resolved_udid = self.serial or lockdown.udid
            return await CoreDeviceTunnelProxy.create(lockdown), lockdown
        except (
            InvalidServiceError,
            StartServiceError,
            NoDeviceConnectedError,
            MuxException,
            PyMobileDevice3Exception,
            OSError,
        ) as e:
            if lockdown is not None:
                with suppress(Exception):
                    await lockdown.close()
            print(f"ℹ️ [Tunnel] CoreDeviceProxy/usbmux 通道無法直接建立 ({type(e).__name__}: {e})，切換至 Bonjour RemotePairing 無線通道...")

        services = await get_remote_pairing_tunnel_services(udid=resolved_udid)
        if not services:
            raise UserspaceTunnelUnavailableError(
                "無法透過 CoreDeviceProxy 或 Bonjour RemotePairing 建立 Userspace 隧道"
            )
        for extra in services[1:]:
            with suppress(Exception):
                await extra.close()
        return services[0], None

    async def aopen(self) -> RemoteServiceDiscoveryService:
        import pymobiledevice3.remote.userspace_tunnel as ut_mod

        async with _lifecycle_lock():
            if ut_mod._active_tunnel is not None:
                with suppress(Exception):
                    await ut_mod._active_tunnel.aclose()

            tunnel_service.USE_USERSPACE_TUNNEL = True
            stack = AsyncExitStack()
            try:
                provider, lockdown = await self._pick_userspace_provider()
                stack.push_async_callback(provider.close)
                if lockdown is not None:
                    stack.push_async_callback(lockdown.close)
                tunnel_result = await stack.enter_async_context(provider.start_tcp_tunnel())
                tun: UserspaceTun = tunnel_result.client.tun
                tun.set_peer(tunnel_result.address)
                dial_plane = await stack.enter_async_context(UserspaceDialPlane(tun, tunnel_result.address))
                rsd = RemoteServiceDiscoveryService(
                    (tunnel_result.address, tunnel_result.port),
                    open_connection=dial_plane.dial,
                    auxiliary_metadata=tunnel_result.auxiliary_metadata,
                )
                stack.push_async_callback(rsd.close)
                await rsd.connect()
                self._stack = stack
                self.rsd = rsd
                ut_mod.USERSPACE_ACTIVE = True
                return rsd
            except BaseException as userspace_err:
                await stack.aclose()
                tunnel_service.USE_USERSPACE_TUNNEL = False
                ut_mod.USERSPACE_ACTIVE = False
                if platform.system() != "Darwin":
                    raise
                print(f"ℹ️ [Tunnel] Userspace 隧道未就緒 ({type(userspace_err).__name__}: {userspace_err})，嘗試 macOS 原生 remotepairingd 通道...")

        from pymobiledevice3.remote.native_tunnel import NativeRemotedTunnel

        native = NativeRemotedTunnel(serial=self.serial)
        try:
            rsd = await native.aopen()
        except BaseException:
            await native.aclose()
            raise
        self._native_tunnel = native
        self.rsd = rsd
        return rsd

    async def aclose(self) -> None:
        import pymobiledevice3.remote.userspace_tunnel as ut_mod

        if self._native_tunnel is not None:
            native, self._native_tunnel = self._native_tunnel, None
            self.rsd = None
            with suppress(Exception):
                await native.aclose()
            return

        async with _lifecycle_lock():
            if self._stack is None:
                return
            stack, self._stack = self._stack, None
            self.rsd = None
            ut_mod.USERSPACE_ACTIVE = False
            tunnel_service.USE_USERSPACE_TUNNEL = False
            with suppress(Exception):
                await stack.aclose()

    async def __aenter__(self) -> RemoteServiceDiscoveryService:
        return await self.aopen()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()


class IOSUserspaceGpsService:
    def __init__(self, state_ref: Dict[str, Any]):
        self.state_ref = state_ref
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
        self.ddi_mounted_udids: set[str] = set()
        self.sim = None
        self.active_provider: Any = None
        self.active_dvt: Optional[DvtProvider] = None
        self._dvt_op_lock: Optional[asyncio.Lock] = None

    def start_worker(self):
        if self.worker_thread is not None and self.worker_thread.is_alive():
            return

        def _worker_loop():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.cmd_queue = asyncio.Queue()
            self._dvt_op_lock = asyncio.Lock()
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
                abort_start = False
                while not self.cmd_queue.empty():
                    try:
                        next_act, _ = self.cmd_queue.get_nowait()
                        if next_act == "STOP":
                            abort_start = True
                    except asyncio.QueueEmpty:
                        break
                if abort_start:
                    self.is_connecting = False
                    continue
                udid, init_lat, init_lon = payload
                self.current_udid = udid
                self.user_requested_stop = False
                await self._run_dvt_session(udid, init_lat, init_lon)
            elif action == "SHUTDOWN":
                break

    async def _run_sim_loop(self, provider: Any, dvt: DvtProvider, sim: LocationSimulation, init_lat: float, init_lon: float):
        self.active_provider = provider
        self.active_dvt = dvt
        self.sim = sim
        await sim.set(init_lat, init_lon)
        self.is_connected = True
        self.is_running = True
        self.is_connecting = False
        self.connect_stage = "已連線"
        if self.current_udid:
            self.ddi_mounted_udids.add(self.current_udid)
        print(f"✅ [Userspace] 會話已就緒！起點座標鎖定: {init_lat:.6f}, {init_lon:.6f}")

        try:
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
        finally:
            self.is_connected = False
            self.is_running = False
            self.sim = None
            self.active_dvt = None
            self.active_provider = None

    async def _run_dvt_session(self, udid: str, init_lat: float, init_lon: float):
        self.is_connecting = True
        self.last_error = None
        self.connect_stage = "正在檢查裝置與信任狀態..."
        target_serial = None if (not udid or udid == "default") else udid
        ios_major = 17
        ddi_mounted_over_usbmux = False
        was_connected_once = False

        try:
            print(f"⏳ [1/4] 正在檢查裝置配對與狀態 (UDID: {target_serial or '自動偵測'})...")
            try:
                async with await create_using_usbmux(serial=target_serial, autopair=True) as lockdown:
                    target_serial = lockdown.udid
                    self.current_udid = target_serial
                    ios_version = Version(lockdown.product_version)
                    ios_major = ios_version.major
                    print(f"✅ [1/4] 已連線 Lockdown: {lockdown.display_name or lockdown.product_type} (iOS {lockdown.product_version})")

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

                    if lockdown.service.mux_device and lockdown.service.mux_device.is_usb:
                        try:
                            await lockdown.set_enable_wifi_connections(True)
                            print("✅ [Wi-Fi] 已自動為此 iPhone 開啟 usbmux Wi-Fi 無線探索支援")
                        except Exception as wifi_err:
                            print(f"ℹ️ [Wi-Fi] 設定 Wi-Fi 連接旗標略過: {wifi_err}")

                        await _bootstrap_remotepairing_over_usb(lockdown)

                    try:
                        await _ensure_ddi_mounted(lockdown, service_ref=self)
                        ddi_mounted_over_usbmux = True
                        if target_serial:
                            self.ddi_mounted_udids.add(target_serial)
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

                    if ios_major < 17:
                        self.connect_stage = "正在啟動定位模擬服務..."
                        print("⏳ [iOS < 17] 正在直接透過 Lockdown 掛載 DvtProvider 與 LocationSimulation...")
                        async with DvtProvider(lockdown) as dvt, LocationSimulation(dvt) as sim:
                            was_connected_once = True
                            await self._run_sim_loop(lockdown, dvt, sim, init_lat, init_lon)
                        return

            except (NoDeviceConnectedError, MuxException) as mux_err:
                print(f"ℹ️ [usbmux] 未透過 usbmux 直接連線 ({mux_err})，嘗試使用無線 RSD 遠端配對通道...")

            self.connect_stage = "正在建立 RSD 安全隧道..."
            print(f"⏳ [2/4] 正在與裝置 ({target_serial or '自動'}) 建立 RSD 隧道...")
            async with ResilientRsdTunnel(serial=target_serial, autopair=True) as rsd:
                if rsd.udid:
                    self.current_udid = rsd.udid
                print(f"✅ [2/4] RSD 隧道建立成功！裝置: {rsd.product_type} (iOS {rsd.product_version})")

                if not ddi_mounted_over_usbmux:
                    await _ensure_ddi_mounted(rsd, service_ref=self)
                if self.current_udid:
                    self.ddi_mounted_udids.add(self.current_udid)

                self.connect_stage = "正在啟動定位模擬服務..."
                print("⏳ [4/4] 正在掛載 DvtProvider 與 LocationSimulation...")
                async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as sim:
                    was_connected_once = True
                    await self._run_sim_loop(rsd, dvt, sim, init_lat, init_lon)

        except (NotPairedError, UserDeniedPairingError, PairingError) as e:
            self.is_connected = False
            self.is_running = False
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
            self.is_connected = False
            self.is_running = False
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
            self.is_connected = False
            self.is_running = False
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
            self.is_connected = False
            self.is_running = False
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

            if was_connected_once and not self.user_requested_stop:
                send_macos_notification("SIM-GPS 警報", "裝置連線中斷", "與 iPhone 連線意外中斷，正在嘗試自動重連...", sound_name="Basso")
                cur_lat = self.state_ref.get("current_lat") or init_lat
                cur_lon = self.state_ref.get("current_lon") or init_lon
                await asyncio.sleep(1.5)
                if not self.user_requested_stop:
                    print(f"🔄 [Auto-Reconnect] 正在嘗試自動重連 {target_serial}...")
                    await self.cmd_queue.put(("START", (target_serial or "default", cur_lat, cur_lon)))
        finally:
            self.sim = None
            self.active_dvt = None
            self.active_provider = None
            self.is_connected = False
            self.is_running = False
            self.is_connecting = False
            self.connect_stage = ""
            if self.state_ref["is_stopping"]:
                self.state_ref["is_stopping"] = False
                self.state_ref["is_returning_home"] = False
            if not self.cmd_queue or self.cmd_queue.empty():
                self.state_ref["is_simulating"] = False

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
        if (self.is_connected or self.is_running or self.is_connecting) and self.loop:
            asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("STOP", None)), self.loop)

    # --- 🤖 皮克敏自動化守護專用 DVT / CoreDevice 同步包裝方法 ---

    async def _async_query_pikmin_process(self) -> Dict[str, Any]:
        if not self.is_connected or self.active_dvt is None:
            return {"running": None, "foreground_hint": None, "pid": None, "cpu": 0.0}

        async with self._dvt_op_lock:
            try:
                async with DeviceInfo(self.active_dvt) as dev_info:
                    procs = await asyncio.wait_for(dev_info.proclist(), timeout=2.5)
                pikmin_proc = None
                for p in procs:
                    name = str(p.get("name") or "")
                    real_app = str(p.get("realAppName") or "")
                    bundle_id = str(p.get("bundleIdentifier") or "")
                    if (
                        "pikmin" in name.lower()
                        or "pikmin" in real_app.lower()
                        or "com.nianticlabs.pikmin" in bundle_id.lower()
                    ):
                        pikmin_proc = p
                        break

                if pikmin_proc is None:
                    return {"running": False, "foreground_hint": False, "pid": None, "cpu": 0.0}

                pid = pikmin_proc.get("pid")
                fg_flag = pikmin_proc.get("foreground")
                cpu_usage = 0.0

                # 若 proclist 未直接提供 foreground 旗標，可透過輕量 Sysmontap 取樣判斷 CPU 是否處於前景渲染活躍狀態
                try:
                    async with await Sysmontap.create(self.active_dvt, interval=250) as sysmon:
                        sample_idx = 0
                        async for entries in sysmon.iter_processes():
                            sample_idx += 1
                            for item in entries:
                                if item.get("pid") == pid or "pikmin" in str(item.get("name", "")).lower():
                                    raw_cpu = item.get("cpuUsage")
                                    if isinstance(raw_cpu, (int, float)):
                                        cpu_usage = float(raw_cpu)
                                    if "foreground" in item and isinstance(item["foreground"], bool):
                                        fg_flag = item["foreground"]
                            if sample_idx >= 2:
                                break
                except Exception:
                    pass

                # Unity 3D 前景渲染時 CPU 通常 > 3%，若明確為 background 或 CPU 極低 (< 0.4%) 則視為背景
                if isinstance(fg_flag, bool):
                    fg_hint = fg_flag
                else:
                    fg_hint = None if cpu_usage == 0.0 else (cpu_usage >= 1.5)

                return {
                    "running": True,
                    "foreground_hint": fg_hint,
                    "pid": pid,
                    "cpu": round(cpu_usage, 1),
                }
            except Exception as e:
                print(f"[DVT] 查詢皮克敏 Process 狀態異常: {e}")
                return {"running": None, "foreground_hint": None, "pid": None, "cpu": 0.0}

    def query_pikmin_process_sync(self) -> Dict[str, Any]:
        if not self.is_connected or not self.loop:
            return {"running": None, "foreground_hint": None, "pid": None, "cpu": 0.0}
        fut = asyncio.run_coroutine_threadsafe(self._async_query_pikmin_process(), self.loop)
        try:
            return fut.result(timeout=5.0)
        except Exception:
            return {"running": None, "foreground_hint": None, "pid": None, "cpu": 0.0}

    async def _async_query_battery_and_pikmin(self) -> Dict[str, Any]:
        bat_data: Dict[str, Any] = {"battery_level": None, "is_charging": None, "temperature_c": None}
        target_udid = self.current_udid if (self.current_udid and self.current_udid != "default") else None
        try:
            async with await asyncio.wait_for(
                create_using_usbmux(serial=target_udid, autopair=False),
                timeout=1.8,
            ) as lockdown:
                bat_data = await query_battery_from_lockdown(lockdown)
        except Exception:
            # 若為純 RemoteXPC RSD 通道，嘗試透過 RSD 建立 DiagnosticsService
            if isinstance(self.active_provider, RemoteServiceDiscoveryService):
                try:
                    async with DiagnosticsService(self.active_provider) as diag:
                        info = await asyncio.wait_for(diag.get_battery(), timeout=1.8)
                        if isinstance(info, dict):
                            cap = info.get("CurrentCapacity")
                            if isinstance(cap, (int, float)):
                                bat_data["battery_level"] = int(cap)
                            bat_data["is_charging"] = bool(info.get("IsCharging") or info.get("ExternalConnected"))
                            raw_t = info.get("Temperature")
                            if isinstance(raw_t, (int, float)) and raw_t > 0:
                                bat_data["temperature_c"] = round(raw_t / 100.0, 1) if raw_t > 200 else round(float(raw_t), 1)
                except Exception:
                    pass

        proc_data = await self._async_query_pikmin_process()
        return {
            **bat_data,
            "pikmin_running": proc_data.get("running"),
            "pikmin_foreground_hint": proc_data.get("foreground_hint"),
            "pikmin_pid": proc_data.get("pid"),
            "pikmin_cpu": proc_data.get("cpu", 0.0),
        }

    def query_battery_and_pikmin_sync(self) -> Dict[str, Any]:
        if not self.is_connected or not self.loop:
            return {}
        fut = asyncio.run_coroutine_threadsafe(self._async_query_battery_and_pikmin(), self.loop)
        try:
            return fut.result(timeout=6.0)
        except Exception:
            return {}

    async def _async_capture_screenshot(self) -> Optional[bytes]:
        if not self.is_connected:
            return None
        async with self._dvt_op_lock:
            # 優先透過已開啟的 DVT 通道擷取螢幕畫面
            if self.active_dvt is not None:
                try:
                    async with Screenshot(self.active_dvt) as shot:
                        data = await asyncio.wait_for(shot.get_screenshot(), timeout=4.0)
                        if data:
                            return bytes(data)
                except Exception as e:
                    print(f"[DVT] Screenshot 擷取異常，嘗試 CoreDevice ScreenCaptureService: {e}")

            # 備援：透過 CoreDevice ScreenCaptureService 擷取
            if isinstance(self.active_provider, RemoteServiceDiscoveryService):
                try:
                    from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
                    async with ScreenCaptureService(self.active_provider) as cap_svc:
                        res = await asyncio.wait_for(cap_svc.capture_screenshot(), timeout=4.5)
                        if isinstance(res, dict) and res.get("image"):
                            return bytes(res["image"])
                except Exception as e2:
                    print(f"[CoreDevice] ScreenCaptureService 擷取失敗: {e2}")
        return None

    def capture_screenshot_sync(self) -> Optional[bytes]:
        if not self.is_connected or not self.loop:
            return None
        fut = asyncio.run_coroutine_threadsafe(self._async_capture_screenshot(), self.loop)
        try:
            return fut.result(timeout=7.0)
        except Exception as e:
            print(f"[Screenshot] 同步等待逾時: {e}")
            return None

    async def _async_tap_screen(self, norm_x: float, norm_y: float) -> bool:
        if not self.is_connected or not isinstance(self.active_provider, RemoteServiceDiscoveryService):
            return False
        hid_x = int(round(max(0.0, min(1.0, norm_x)) * 65535))
        hid_y = int(round(max(0.0, min(1.0, norm_y)) * 65535))

        async with self._dvt_op_lock:
            try:
                from pymobiledevice3.remote.core_device.hid_service import (
                    DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
                    TOUCHSCREEN_STATE_CONTACT,
                    TOUCHSCREEN_STATE_RELEASE,
                    touch_session,
                )
                async with touch_session(self.active_provider) as hid:
                    await hid.send_touchscreen(
                        TOUCHSCREEN_STATE_CONTACT,
                        hid_x,
                        hid_y,
                        service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
                    )
                    await asyncio.sleep(0.06)
                    await hid.send_touchscreen(
                        TOUCHSCREEN_STATE_RELEASE,
                        hid_x,
                        hid_y,
                        service_id=DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
                    )
                print(f"👆 [HID Tap] 已傳送螢幕點擊: norm=({norm_x:.3f}, {norm_y:.3f}) -> HID=({hid_x}, {hid_y})")
                return True
            except Exception as e:
                add_debug_log(
                    title="傳送螢幕模擬點擊 (HID Tap) 失敗",
                    error_msg=f"{type(e).__name__}: {e}",
                    stage="遠端觸控",
                    level="WARN",
                    udid=self.current_udid,
                )
                print(f"❌ [HID Tap] 失敗: {e}")
                return False

    def tap_screen_sync(self, norm_x: float, norm_y: float) -> bool:
        if not self.is_connected or not self.loop:
            return False
        fut = asyncio.run_coroutine_threadsafe(self._async_tap_screen(norm_x, norm_y), self.loop)
        try:
            return bool(fut.result(timeout=12.0))
        except Exception as e:
            print(f"[HID Tap] 同步等待逾時: {e}")
            return False

    async def _async_launch_pikmin(self, bundle_id: str = "com.nianticlabs.pikmin") -> bool:
        if not self.is_connected:
            return False
        async with self._dvt_op_lock:
            # 1. 優先使用 DVT ProcessControl.launch
            if self.active_dvt is not None:
                try:
                    async with ProcessControl(self.active_dvt) as proc_ctrl:
                        pid = await asyncio.wait_for(
                            proc_ctrl.launch(bundle_id=bundle_id, kill_existing=False),
                            timeout=6.0,
                        )
                        print(f"🚀 [DVT] 已成功啟動皮克敏 ({bundle_id}), PID={pid}")
                        return True
                except Exception as e:
                    print(f"[DVT] ProcessControl 啟動皮克敏失敗，改試 AppService: {e}")

            # 2. 備援使用 CoreDevice AppServiceService
            if isinstance(self.active_provider, RemoteServiceDiscoveryService):
                try:
                    from pymobiledevice3.remote.core_device.app_service import AppServiceService
                    async with AppServiceService(self.active_provider) as app_svc:
                        await asyncio.wait_for(
                            app_svc.launch_application(bundle_id, kill_existing=False),
                            timeout=6.0,
                        )
                        print(f"🚀 [AppService] 已成功啟動皮克敏 ({bundle_id})")
                        return True
                except Exception as e2:
                    add_debug_log(
                        title="自動啟動皮克敏 App 失敗",
                        error_msg=f"{type(e2).__name__}: {e2}",
                        stage="自動重啟 App",
                        level="WARN",
                        udid=self.current_udid,
                    )
        return False

    def launch_pikmin_sync(self, bundle_id: str = "com.nianticlabs.pikmin") -> bool:
        if not self.is_connected or not self.loop:
            return False
        fut = asyncio.run_coroutine_threadsafe(self._async_launch_pikmin(bundle_id), self.loop)
        try:
            return bool(fut.result(timeout=8.0))
        except Exception:
            return False


async def scan_ios_devices_async(gps_service: IOSUserspaceGpsService) -> List[Dict[str, Any]]:
    """
    掃描所有可用 iOS 裝置（USB + Wi-Fi），並檢測每台裝置的 DDI 狀態、電量百分比与溫度
    """
    devices: List[Dict[str, Any]] = []
    seen_udids: set[str] = set()

    try:
        mux_devs = await usbmux.list_devices()
        by_udid: Dict[str, usbmux.MuxDevice] = {}
        for d in mux_devs:
            existing = by_udid.get(d.serial)
            if existing is None or (d.is_usb and not existing.is_usb):
                by_udid[d.serial] = d

        for udid, mux_dev in by_udid.items():
            seen_udids.add(udid)
            conn_type = "USB" if mux_dev.is_usb else "Network (Wi-Fi)"
            model_name = "iPhone"
            display_label = f"iOS 裝置 ({conn_type})"
            dev_mode_enabled: Optional[bool] = None
            ddi_mounted: Optional[bool] = (
                True if (gps_service.is_connected and gps_service.current_udid == udid) or (udid in gps_service.ddi_mounted_udids) else None
            )
            bat_info: Dict[str, Any] = {"battery_level": None, "is_charging": None, "temperature_c": None}

            try:
                lockdown = await asyncio.wait_for(
                    create_using_usbmux(
                        serial=udid,
                        autopair=False,
                        connection_type=mux_dev.connection_type,
                    ),
                    timeout=1.5,
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

                    dm_status, mounted_status = await check_device_ddi_status(lockdown)
                    dev_mode_enabled = dm_status
                    if mounted_status is not None:
                        ddi_mounted = mounted_status
                        if mounted_status:
                            gps_service.ddi_mounted_udids.add(udid)
                        else:
                            gps_service.ddi_mounted_udids.discard(udid)

                    bat_info = await query_battery_from_lockdown(lockdown)
                finally:
                    await lockdown.close()
            except Exception:
                pass

            if dev_mode_enabled is False:
                ddi_status = "dev_mode_off"
                ddi_label = "開發者模式未開"
            elif ddi_mounted is True:
                ddi_status = "mounted"
                ddi_label = "DDI 已掛載"
            elif ddi_mounted is False:
                ddi_status = "unmounted"
                ddi_label = "DDI 未掛載 (連線自動掛載)"
            else:
                ddi_status = "unknown"
                ddi_label = "連線時自動檢查 DDI"

            devices.append({
                "udid": udid,
                "connection": conn_type,
                "model": model_name,
                "name": display_label,
                "dev_mode_enabled": dev_mode_enabled,
                "ddi_mounted": ddi_mounted,
                "ddi_status": ddi_status,
                "ddi_label": ddi_label,
                "battery_level": bat_info.get("battery_level"),
                "is_charging": bat_info.get("is_charging"),
                "temperature_c": bat_info.get("temperature_c"),
            })
    except Exception as e:
        print(f"[Scan] usbmux 掃描發生異常: {e}")
        add_debug_log("掃描 iOS 裝置異常", str(e), stage="裝置掃描", level="WARN")

    try:
        for identifier, _, rec in iter_remote_pair_records_by_identifier():
            if identifier and identifier not in seen_udids:
                seen_udids.add(identifier)
                peer_model = rec.get("peer_model") or "iPhone"
                is_active_ddi = (
                    (gps_service.is_connected and gps_service.current_udid == identifier)
                    or (identifier in gps_service.ddi_mounted_udids)
                )
                devices.append({
                    "udid": identifier,
                    "connection": "Network (Wi-Fi)",
                    "model": peer_model,
                    "name": f"{peer_model} (已配對 Wi-Fi)",
                    "dev_mode_enabled": True if is_active_ddi else None,
                    "ddi_mounted": True if is_active_ddi else None,
                    "ddi_status": "mounted" if is_active_ddi else "unknown",
                    "ddi_label": "DDI 已掛載" if is_active_ddi else "連線時自動檢查 DDI",
                    "battery_level": None,
                    "is_charging": None,
                    "temperature_c": None,
                })
    except Exception:
        pass

    if not devices:
        devices.append({
            "udid": "default",
            "connection": "Auto (預設通道)",
            "model": "iPhone",
            "name": "本機偵測裝置 (預設)",
            "dev_mode_enabled": None,
            "ddi_mounted": None,
            "ddi_status": "unknown",
            "ddi_label": "連線時自動檢查 DDI",
            "battery_level": None,
            "is_charging": None,
            "temperature_c": None,
        })
    return devices
