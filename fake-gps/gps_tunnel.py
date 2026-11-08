"""
gps_tunnel.py
iOS 多裝置並行探索、DDI 狀態檢測、Wi-Fi 優先 RSD/DVT 隧道、DDI 硬體省電鎖幀控制、
即時功耗(W/mA)/熱狀態(Thermal State)感測與個別裝置獨立 60km/h 返航模組
"""

import asyncio
from contextlib import AsyncExitStack, suppress
import errno
import platform
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple
from packaging.version import Version

from pmd_net_addr import Ip6Address, Ip6IfAddr, MacAddress
from pmd_pytcp import stack as pytcp_stack
from pmd_pytcp.lib.interface_layer import InterfaceLayer
from pmd_pytcp.lib.io_backend import register_interface_fd, unregister_interface_fd

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
import pymobiledevice3.remote.userspace_tunnel as ut_mod
from pymobiledevice3.remote.userspace_tunnel import (
    INTERFACE_MTU,
    LOOPBACK_HEADER,
    UserspaceDialPlane,
    UserspaceTun,
    _ETH_HDR_TO_STACK,
    _PEER_MAC,
    _STACK_MAC,
    _eth_header,
    _lifecycle_lock,
    throughput_sysctls,
)
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.dvt.instruments.condition_inducer import ConditionInducer
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.energy_monitor import EnergyMonitor
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

# 熱狀態等級對照表 (對應 iOS ProcessInfo.thermalState / Xcode EnergyMonitor energy.thermalstate.cost)
THERMAL_STATE_LABELS: Dict[int, str] = {
    0: "正常",
    1: "微溫",
    2: "發燙",
    3: "過熱",
}

# --- 🔧 升級 UserspaceTun 以支援多台 iPhone 同時掛載獨立虛擬網卡介面，且確保每個介面的 L2 MAC 與寫入標頭完全一致 ---
_active_userspace_tuns: set[UserspaceTun] = set()
_active_tunnels_by_udid: Dict[str, "ResilientRsdTunnel"] = {}


def _allocate_unique_iface_mac() -> str:
    """
    第一台裝置優先使用標準 _STACK_MAC ("02:00:00:00:00:01")，
    第二台以上同時在線裝置分配獨立 MAC ("02:00:00:00:01:02" 等)，並搭配專屬 _eth_hdr_to_stack 標頭。
    """
    used_macs = {getattr(t, "_iface_mac", _STACK_MAC) for t in _active_userspace_tuns}
    if _STACK_MAC not in used_macs:
        return _STACK_MAC
    for idx in range(2, 250):
        cand = f"02:00:00:00:01:{idx:02x}"
        if cand not in used_macs:
            return cand
    return _STACK_MAC


async def _multi_tun_up(self: UserspaceTun) -> None:
    self._mtu = min(self._mtu, INTERFACE_MTU)
    if not getattr(pytcp_stack, "_pmd3_inited", False):
        sysctls = {"icmp6.default.accept_dad": 0, **throughput_sysctls()}
        pytcp_stack.init(sysctls=sysctls)
        pytcp_stack._pmd3_inited = True  # type: ignore[attr-defined]

    register_interface_fd(self._pend)
    iface_mac = _allocate_unique_iface_mac()
    self._iface_mac = iface_mac  # type: ignore[attr-defined]
    self._eth_hdr_to_stack = _eth_header(iface_mac, _PEER_MAC)  # type: ignore[attr-defined]

    self._ifidx = pytcp_stack.add_interface(
        fd=self._pend.fileno(),
        layer=InterfaceLayer.L2,
        mac_address=MacAddress(iface_mac),
        mtu=self._mtu,
        ip6_support=True,
        ip6_host=Ip6IfAddr(f"{self._addr}/64"),
        ip6_lla_autoconfig=True,
        ip6_gua_autoconfig=False,
        ip4_support=False,
    )
    if not getattr(pytcp_stack, "stack_running", False):
        await pytcp_stack.start()  # type: ignore[misc]

    _active_userspace_tuns.add(self)
    target = Ip6Address(self._addr)
    deadline = asyncio.get_running_loop().time() + 5.0
    while not any(host.address == target for host in pytcp_stack.local_ip6_hosts()):
        if asyncio.get_running_loop().time() > deadline:
            raise PyMobileDevice3Exception(f"Userspace 隧道位址 {self._addr} 未能在 5 秒內完成指派")
        await asyncio.sleep(0)


def _multi_tun_write(self: UserspaceTun, data: bytes) -> None:
    """
    確保寫入 PyTCP L2 socketpair 的目的端 MAC 位址與該介面註冊的 self._iface_mac 一致，
    修正多裝置連線時非預設 MAC 封包被 L2 網卡靜默丟棄導致 RemoteXPCHandshakeTimeoutError 的問題。
    """
    ipv6 = data[len(LOOPBACK_HEADER):] if data[:len(LOOPBACK_HEADER)] == LOOPBACK_HEADER else data
    eth_hdr = getattr(self, "_eth_hdr_to_stack", _ETH_HDR_TO_STACK)
    try:
        self._peer.send(eth_hdr + ipv6)
    except BlockingIOError:
        pass
    except OSError as e:
        if e.errno == errno.ENOBUFS:
            return
        if not self._send_error_logged:
            self._send_error_logged = True


async def _multi_tun_close(self: UserspaceTun) -> None:
    if self._closed:
        return
    self._closed = True
    _active_userspace_tuns.discard(self)

    if len(_active_userspace_tuns) == 0:
        try:
            await pytcp_stack.stop()  # type: ignore[misc]
        except Exception:
            pass
        finally:
            pytcp_stack._pmd3_inited = False  # type: ignore[attr-defined]
    else:
        if self._ifidx is not None:
            with suppress(Exception):
                pytcp_stack.remove_interface(self._ifidx)

    with suppress(Exception):
        unregister_interface_fd(self._pend)
    for s in (self._peer, self._pend):
        with suppress(Exception):
            s.close()


UserspaceTun.up = _multi_tun_up  # type: ignore[assignment]
UserspaceTun.write = _multi_tun_write  # type: ignore[assignment]
UserspaceTun.close = _multi_tun_close  # type: ignore[assignment]


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


def _sanitize_for_json(obj: Any) -> Any:
    """
    遞迴清理 IORegistry / Lockdown / DVT 回傳的字典，將無法以 UTF-8 解碼的二進位 bytes
    (如 ManufacturerData b'\\xe1...') 轉為安全的 hex 字串，避免 FastAPI jsonable_encoder 拋出 UnicodeDecodeError。
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8")
        except Exception:
            preview = bytes(obj[:32]).hex()
            suffix = "..." if len(obj) > 32 else ""
            return f"<bytes len={len(obj)} hex={preview}{suffix}>"
    if isinstance(obj, dict):
        return {str(k): _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize_for_json(x) for x in obj]
    return str(obj)


def _extract_battery_and_power_metrics(info: Dict[str, Any]) -> Dict[str, Any]:
    """
    從 IORegistry IOPMPowerSource 或 AppleSmartBattery 字典解析：
    - battery_level (%)
    - is_charging (bool)
    - temperature_c (°C，向下相容舊版 iOS)
    - instant_amperage_ma (mA，即時電池電流)
    - amperage_ma (mA，平均電流)
    - voltage_mv (mV，電池電壓)
    - battery_power_mw (mW，電池充放電功率)
    - system_load_mw (mW，系統即時總功耗，插電時亦可反映真實耗電負載)
    - system_load_w (W，系統即時總功耗瓦數)
    - thermally_limited_sec (因過熱限制充電秒數)
    """
    out: Dict[str, Any] = {
        "battery_level": None,
        "is_charging": None,
        "temperature_c": None,
        "instant_amperage_ma": None,
        "amperage_ma": None,
        "voltage_mv": None,
        "battery_power_mw": None,
        "system_load_mw": None,
        "system_load_w": None,
        "thermally_limited_sec": None,
    }
    if not isinstance(info, dict):
        return out

    dicts_to_scan: List[Dict[str, Any]] = []

    def _collect_dicts(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            dicts_to_scan.append(node)
            for k, v in node.items():
                if k in ("IOReportLegend", "PortControllerInfo"):
                    continue
                if isinstance(v, (dict, list)):
                    _collect_dicts(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    _collect_dicts(item, depth + 1)

    _collect_dicts(info)

    # 1. 解析電量與充電狀態
    for d in dicts_to_scan:
        if out["battery_level"] is None:
            cap = d.get("CurrentCapacity")
            max_cap = d.get("MaxCapacity")
            if isinstance(cap, (int, float)) and not isinstance(cap, bool):
                if 0 <= cap <= 100:
                    out["battery_level"] = int(cap)
                elif isinstance(max_cap, (int, float)) and max_cap > 0:
                    out["battery_level"] = int(round((cap / max_cap) * 100))

        if out["is_charging"] is None:
            chg = d.get("IsCharging")
            ext = d.get("ExternalConnected")
            if isinstance(chg, bool) or isinstance(ext, bool):
                out["is_charging"] = bool(chg) or bool(ext)

        if out["instant_amperage_ma"] is None:
            ia = d.get("InstantAmperage")
            if isinstance(ia, (int, float)) and not isinstance(ia, bool):
                out["instant_amperage_ma"] = int(ia)

        if out["amperage_ma"] is None:
            amp = d.get("Amperage")
            if isinstance(amp, (int, float)) and not isinstance(amp, bool):
                out["amperage_ma"] = int(amp)

        if out["voltage_mv"] is None:
            volt = d.get("Voltage") or d.get("AppleRawBatteryVoltage")
            if isinstance(volt, (int, float)) and not isinstance(volt, bool) and 2500 <= volt <= 5500:
                out["voltage_mv"] = int(volt)

        if out["battery_power_mw"] is None:
            bp = d.get("BatteryPower")
            if isinstance(bp, (int, float)) and not isinstance(bp, bool):
                out["battery_power_mw"] = round(float(bp), 1)

        if out["system_load_mw"] is None:
            sl = d.get("SystemLoad") or d.get("SystemEffectiveTotalLoad")
            if isinstance(sl, (int, float)) and not isinstance(sl, bool) and sl > 0:
                out["system_load_mw"] = round(float(sl), 1)

        if out["thermally_limited_sec"] is None:
            tl = d.get("TimeChargingThermallyLimited")
            if isinstance(tl, (int, float)) and not isinstance(tl, bool):
                out["thermally_limited_sec"] = int(tl)

    # 計算瓦數 (W)
    if out["system_load_mw"] is not None:
        out["system_load_w"] = round(abs(out["system_load_mw"]) / 1000.0, 2)
    elif out["battery_power_mw"] is not None:
        out["system_load_w"] = round(abs(out["battery_power_mw"]) / 1000.0, 2)
    elif out["instant_amperage_ma"] is not None and out["voltage_mv"] is not None:
        out["system_load_w"] = round(abs(out["instant_amperage_ma"] * out["voltage_mv"]) / 1_000_000.0, 2)

    # 2. 解析舊版 iOS 溫度 (°C)
    candidates: List[Any] = []
    for d in dicts_to_scan:
        for t_key in ("Temperature", "VirtualTemperature", "BatteryTemperature", "ChargerTemperature"):
            if t_key in d and d[t_key] is not None and not isinstance(d[t_key], bool):
                candidates.append(d[t_key])

    for raw_t in candidates:
        if isinstance(raw_t, (int, float)) and raw_t > 0:
            if 1500 <= raw_t <= 6500:
                val = raw_t / 100.0
                if 15.0 <= val <= 65.0:
                    out["temperature_c"] = round(val, 1)
                    break
            elif 200 <= raw_t < 750:
                val = raw_t / 10.0
                if 15.0 <= val <= 65.0:
                    out["temperature_c"] = round(val, 1)
                    break
            elif 15 <= raw_t <= 65 and raw_t != 60:
                # 排除 AppleBTM 的固定 60 秒採樣週期常數
                out["temperature_c"] = round(float(raw_t), 1)
                break

    return out


def _extract_temp_and_battery_from_dict(info: Dict[str, Any]) -> Tuple[Optional[int], Optional[bool], Optional[float]]:
    """向下相容介面：回傳 (battery_level, is_charging, temperature_c)"""
    m = _extract_battery_and_power_metrics(info)
    return m["battery_level"], m["is_charging"], m["temperature_c"]


async def query_battery_from_provider(provider: Any, include_raw: bool = False) -> Dict[str, Any]:
    """
    透過 DiagnosticsService 讀取電池電量、充電狀態、即時功耗 (mW / W)、電流 (mA)、電壓 (mV) 與溫度。
    """
    metrics: Dict[str, Any] = {
        "battery_level": None,
        "is_charging": None,
        "temperature_c": None,
        "instant_amperage_ma": None,
        "amperage_ma": None,
        "voltage_mv": None,
        "battery_power_mw": None,
        "system_load_mw": None,
        "system_load_w": None,
        "thermally_limited_sec": None,
    }
    raw_dump: Dict[str, Any] = {"provider_type": type(provider).__name__}

    # 1. 查詢 IOPMPowerSource (get_battery)
    try:
        async with DiagnosticsService(provider) as diag:
            iopm = await asyncio.wait_for(diag.get_battery(), timeout=2.5)
            if isinstance(iopm, dict):
                if include_raw:
                    # 排除超大型陣列 PortControllerInfo 與 IOReportLegend 以保持診斷報告清晰快速
                    trimmed_iopm = {
                        k: v for k, v in iopm.items() if k not in ("PortControllerInfo", "IOReportLegend")
                    }
                    raw_dump["IOPMPowerSource"] = _sanitize_for_json(trimmed_iopm)
                m1 = _extract_battery_and_power_metrics(iopm)
                for k, v in m1.items():
                    if v is not None and metrics.get(k) is None:
                        metrics[k] = v
    except Exception as e1:
        if include_raw:
            raw_dump["IOPMPowerSource_error"] = f"{type(e1).__name__}: {e1}"

    # 2. 若未取得電量，補充查詢 AppleSmartBattery
    if metrics["battery_level"] is None:
        try:
            async with DiagnosticsService(provider) as diag2:
                smart = await asyncio.wait_for(diag2.ioregistry(ioclass="AppleSmartBattery"), timeout=2.5)
                if isinstance(smart, dict):
                    m2 = _extract_battery_and_power_metrics(smart)
                    for k, v in m2.items():
                        if v is not None and metrics.get(k) is None:
                            metrics[k] = v
        except Exception as e2:
            if include_raw:
                raw_dump["AppleSmartBattery_error"] = f"{type(e2).__name__}: {e2}"

    # 3. Lockdown com.apple.mobile.battery domain 補充查詢
    if (metrics["battery_level"] is None or include_raw) and hasattr(provider, "get_value"):
        try:
            b_dom = await asyncio.wait_for(provider.get_value(domain="com.apple.mobile.battery"), timeout=1.5)
            if isinstance(b_dom, dict):
                if include_raw:
                    raw_dump["lockdown_battery_domain"] = _sanitize_for_json(b_dom)
                cap = b_dom.get("BatteryCurrentCapacity")
                if metrics["battery_level"] is None and isinstance(cap, (int, float)):
                    metrics["battery_level"] = int(cap)
                if metrics["is_charging"] is None:
                    chg = b_dom.get("BatteryIsCharging")
                    ext = b_dom.get("ExternalConnected")
                    if isinstance(chg, bool) or isinstance(ext, bool):
                        metrics["is_charging"] = bool(chg) or bool(ext)
        except Exception as e3:
            if include_raw:
                raw_dump["lockdown_battery_error"] = f"{type(e3).__name__}: {e3}"

    if include_raw:
        clean_raw = _sanitize_for_json(raw_dump)
        metrics["raw_diagnostics"] = clean_raw
        metrics["raw"] = clean_raw
    return metrics


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
    """
    支援「Wi-Fi 優先連線」、多通道自動退避重試與「完整綁定 self.tun / ut_mod._active_tunnel」的 Userspace RSD 隧道，
    徹底解決 HID Tap 呼叫 UserspaceUdp() 時拋出 local_address 錯誤與單一通道握手超時問題。
    """

    def __init__(self, serial: Optional[str] = None, autopair: bool = True, prefer_wifi: bool = True):
        self.serial = serial
        self.autopair = autopair
        self.prefer_wifi = prefer_wifi
        self._stack: Optional[AsyncExitStack] = None
        self._native_tunnel = None
        self.tun: Optional[UserspaceTun] = None
        self.rsd: Optional[RemoteServiceDiscoveryService] = None
        self.transport_used: str = "Unknown"

    def activate_for_udp(self) -> None:
        """在建立 UserspaceUdp (例如 touch_session) 前，將此隧道設為當前活躍隧道以提供 self.tun.addr"""
        if self.tun is not None:
            ut_mod._active_tunnel = self  # type: ignore[assignment]
            ut_mod.USERSPACE_ACTIVE = True

    async def _iter_candidate_providers(self):
        """
        依優先順序產生候選隧道 Provider：
        1. 若開啟 prefer_wifi：先嘗試 Bonjour RemotePairing (Wi-Fi)，再嘗試 usbmux Network CoreDeviceProxy
        2. 預設/USB usbmux CoreDeviceProxy
        3. Bonjour RemotePairing 備援
        """
        resolved_udid = self.serial

        if self.prefer_wifi:
            # 1a. 優先嘗試 Bonjour RemotePairing (在 iOS 17+/26+ Wi-Fi 無線環境下最穩定快速)
            try:
                services = await asyncio.wait_for(
                    get_remote_pairing_tunnel_services(udid=resolved_udid),
                    timeout=3.5,
                )
                if services:
                    for extra in services[1:]:
                        with suppress(Exception):
                            await extra.close()
                    yield "Wi-Fi (RemotePairing)", services[0], None
            except Exception:
                pass

            # 1b. 嘗試 usbmux Network (Wi-Fi) CoreDeviceTunnelProxy
            lockdown_net = None
            try:
                lockdown_net = await asyncio.wait_for(
                    create_using_usbmux(
                        serial=self.serial,
                        autopair=False,
                        connection_type="Network",
                    ),
                    timeout=2.5,
                )
                resolved_udid = self.serial or lockdown_net.udid
                proxy = await asyncio.wait_for(CoreDeviceTunnelProxy.create(lockdown_net), timeout=3.0)
                yield "Wi-Fi (CoreDeviceProxy)", proxy, lockdown_net
            except Exception:
                if lockdown_net is not None:
                    with suppress(Exception):
                        await lockdown_net.close()

        # 2. 嘗試預設/USB usbmux CoreDeviceTunnelProxy
        lockdown_usb = None
        try:
            lockdown_usb = await asyncio.wait_for(
                create_using_usbmux(serial=self.serial, autopair=self.autopair),
                timeout=4.0,
            )
            resolved_udid = self.serial or lockdown_usb.udid
            is_usb = bool(lockdown_usb.service.mux_device and lockdown_usb.service.mux_device.is_usb)
            label = "USB (CoreDeviceProxy)" if is_usb else "Wi-Fi (CoreDeviceProxy)"
            proxy_usb = await asyncio.wait_for(CoreDeviceTunnelProxy.create(lockdown_usb), timeout=3.5)
            yield label, proxy_usb, lockdown_usb
        except Exception:
            if lockdown_usb is not None:
                with suppress(Exception):
                    await lockdown_usb.close()

        # 3. 最後備援嘗試 Bonjour RemotePairing
        if not self.prefer_wifi:
            try:
                services = await asyncio.wait_for(
                    get_remote_pairing_tunnel_services(udid=resolved_udid),
                    timeout=4.0,
                )
                if services:
                    for extra in services[1:]:
                        with suppress(Exception):
                            await extra.close()
                    yield "Wi-Fi (RemotePairing)", services[0], None
            except Exception:
                pass

    async def aopen(self) -> RemoteServiceDiscoveryService:
        async with _lifecycle_lock():
            tunnel_service.USE_USERSPACE_TUNNEL = True
            last_err: Optional[BaseException] = None

            async for transport_label, provider, lockdown in self._iter_candidate_providers():
                stack = AsyncExitStack()
                try:
                    stack.push_async_callback(provider.close)
                    if lockdown is not None:
                        stack.push_async_callback(lockdown.close)
                    tunnel_result = await stack.enter_async_context(provider.start_tcp_tunnel())
                    tun: UserspaceTun = tunnel_result.client.tun
                    tun.set_peer(tunnel_result.address)
                    self.tun = tun
                    dial_plane = await stack.enter_async_context(UserspaceDialPlane(tun, tunnel_result.address))
                    rsd = RemoteServiceDiscoveryService(
                        (tunnel_result.address, tunnel_result.port),
                        open_connection=dial_plane.dial,
                        auxiliary_metadata=tunnel_result.auxiliary_metadata,
                    )
                    stack.push_async_callback(rsd.close)
                    await asyncio.wait_for(rsd.connect(), timeout=10.0)
                    self._stack = stack
                    self.rsd = rsd
                    self.transport_used = transport_label
                    print(f"📶 [Tunnel] 已透過 {transport_label} 成功建立 RSD 通道 ({rsd.udid or self.serial})")
                    ut_mod._active_tunnel = self  # type: ignore[assignment]
                    ut_mod.USERSPACE_ACTIVE = True
                    if rsd.udid:
                        _active_tunnels_by_udid[rsd.udid] = self
                    elif self.serial:
                        _active_tunnels_by_udid[self.serial] = self
                    return rsd
                except BaseException as err:
                    last_err = err
                    print(f"ℹ️ [Tunnel] 候選通道 {transport_label} 握手未完成 ({type(err).__name__}: {err})，嘗試下一通道...")
                    await stack.aclose()
                    self.tun = None

            if platform.system() != "Darwin" and last_err is not None:
                raise last_err

        from pymobiledevice3.remote.native_tunnel import NativeRemotedTunnel

        native = NativeRemotedTunnel(serial=self.serial)
        try:
            rsd = await native.aopen()
        except BaseException:
            await native.aclose()
            if last_err is not None:
                raise last_err
            raise
        self._native_tunnel = native
        self.rsd = rsd
        self.transport_used = "Wi-Fi/Native (remotepairingd)"
        return rsd

    async def aclose(self) -> None:
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
            for k, v in list(_active_tunnels_by_udid.items()):
                if v is self:
                    _active_tunnels_by_udid.pop(k, None)
            self.rsd = None
            self.tun = None
            if ut_mod._active_tunnel is self:
                remaining = next(iter(_active_tunnels_by_udid.values()), None)
                ut_mod._active_tunnel = remaining  # type: ignore[assignment]
                ut_mod.USERSPACE_ACTIVE = remaining is not None
                tunnel_service.USE_USERSPACE_TUNNEL = remaining is not None
            with suppress(Exception):
                await stack.aclose()

    async def __aenter__(self) -> RemoteServiceDiscoveryService:
        return await self.aopen()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.aclose()


class IOSUserspaceGpsService:
    """
    單一 iOS 裝置的獨立定位與自動化控制實例。
    支援：
    - 獨立平滑返航 (60 km/h)
    - 獨立電量、即時功耗 (W / mA) 與系統熱狀態 (Thermal State) 檢測
    - 連線自動切換 DDI 低耗電模式 (ThermalCritical 30FPS 鎖幀降載) 與斷線自動恢復
    - 前景/閃退監控、截圖與 HID 觸控點擊
    """

    def __init__(self, udid: str, state_ref: Dict[str, Any], shared_loop: asyncio.AbstractEventLoop):
        self.udid = udid
        self.target_udid = udid
        self.state_ref = state_ref
        self.loop = shared_loop
        self.cmd_queue: asyncio.Queue = asyncio.Queue()
        self.is_connected = False
        self.is_connecting = False
        self.is_running = False
        self.user_requested_stop = False
        self.current_udid: str = udid
        self.device_name: str = udid
        self.transport_used: str = ""
        self.connect_stage: str = ""
        self.last_error: Optional[str] = None
        self.sim = None
        self.active_provider: Any = None
        self.active_dvt: Optional[DvtProvider] = None
        self.active_tunnel: Optional[ResilientRsdTunnel] = None
        self._dvt_op_lock = asyncio.Lock()

        # DDI 硬體省電模式狀態 (ConditionInducer)
        self.ddi_power_saving_active: bool = False
        self.ddi_power_saving_profile: str = ""

        # 快取最新硬體熱狀態與功耗指標
        self.last_thermal_state: Optional[int] = None
        self.last_thermal_label: str = "正常"
        self.last_system_load_w: Optional[float] = None
        self.last_instant_amperage_ma: Optional[int] = None

        # 個別裝置獨立位置與獨立返航狀態 (60 km/h)
        self.current_lat: float = state_ref.get("current_lat", 22.549088)
        self.current_lon: float = state_ref.get("current_lon", 120.354109)
        self.is_returning_home: bool = False
        self.is_stopping: bool = False
        self.return_remaining_m: float = 0.0
        self.return_eta_seconds: int = 0
        self.prompt_launch_pikmin: bool = False

        # 啟動此裝置專屬的異步控制協程
        asyncio.run_coroutine_threadsafe(self._event_dispatcher(), self.loop)

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

    async def _run_sim_loop(
        self,
        provider: Any,
        dvt: DvtProvider,
        sim: LocationSimulation,
        init_lat: float,
        init_lon: float,
        tunnel_obj: Optional[ResilientRsdTunnel] = None,
    ):
        self.active_provider = provider
        self.active_dvt = dvt
        self.active_tunnel = tunnel_obj
        self.sim = sim
        self.current_lat = init_lat
        self.current_lon = init_lon
        await sim.set(init_lat, init_lon)
        self.is_connected = True
        self.is_running = True
        self.is_connecting = False
        self.connect_stage = "已連線"
        print(f"✅ [Userspace] 裝置 {self.current_udid} ({self.transport_used}) 會話已就緒！座標: {init_lat:.6f}, {init_lon:.6f}")

        # 連線就緒時安全清除任何殘留的 ConditionInducer 測試狀態
        with suppress(Exception):
            async with ConditionInducer(dvt) as ci:
                await asyncio.wait_for(ci.clear(), timeout=2.0)

        try:
            while self.is_running:
                try:
                    action, payload = await asyncio.wait_for(self.cmd_queue.get(), timeout=1.0)
                    if action == "SET":
                        lat, lon = payload
                        self.current_lat = lat
                        self.current_lon = lon
                        await sim.set(lat, lon)
                    elif action == "STOP":
                        self.user_requested_stop = True
                        break
                    elif action == "START":
                        await self.cmd_queue.put(("START", payload))
                        break
                except asyncio.TimeoutError:
                    continue

            print(f"🛑 [Userspace] 正在清理裝置 {self.current_udid} 定位模擬狀態...")
            try:
                await asyncio.wait_for(sim.clear(), timeout=2.5)
                print(f"✅ [Userspace] 裝置 {self.current_udid} 已恢復真機 GPS 定位。")
            except Exception as e:
                print(f"清理異常 ({self.current_udid}): {e}")
        finally:
            self.ddi_power_saving_active = False
            self.ddi_power_saving_profile = ""
            self.is_connected = False
            self.is_running = False
            self.is_returning_home = False
            self.is_stopping = False
            self.sim = None
            self.active_dvt = None
            self.active_provider = None
            self.active_tunnel = None

    async def _run_dvt_session(self, udid: str, init_lat: float, init_lon: float):
        self.is_connecting = True
        self.last_error = None
        self.connect_stage = "正在檢查裝置與 DDI 狀態..."
        target_serial = None if (not udid or udid == "default") else udid
        ios_major = 17
        ddi_already_mounted = False
        was_connected_once = False

        try:
            # 🎯 1. 先嘗試透過 Wi-Fi (Network) 檢查 DDI 是否已掛載；若未掛載才使用 USB 進行首次 DDI 掛載與無線憑證引導
            for conn_pref in ("Network", None):
                try:
                    async with await asyncio.wait_for(
                        create_using_usbmux(serial=target_serial, autopair=True, connection_type=conn_pref),
                        timeout=2.5 if conn_pref == "Network" else 5.0,
                    ) as lockdown:
                        target_serial = lockdown.udid
                        self.current_udid = target_serial
                        self.device_name = lockdown.all_values.get("DeviceName") or lockdown.display_name or target_serial
                        ios_version = Version(lockdown.product_version)
                        ios_major = ios_version.major
                        is_usb_conn = bool(lockdown.service.mux_device and lockdown.service.mux_device.is_usb)

                        if lockdown.has_developer_mode and not await lockdown.get_developer_mode_status():
                            self.last_error = (
                                "偵測到 iPhone 尚未開啟「開發者模式」！\n"
                                "請至 iPhone「設定 > 隱私權與安全性 > 開發者模式」開啟並重新啟動手機後再試。"
                            )
                            add_debug_log(
                                title="手機尚未開啟開發者模式",
                                error_msg=f"裝置 {self.device_name} (iOS {lockdown.product_version}) DeveloperModeStatus 為 False",
                                stage="Lockdown 檢查",
                                udid=target_serial,
                            )
                            return

                        if is_usb_conn:
                            with suppress(Exception):
                                await lockdown.set_enable_wifi_connections(True)
                            await _bootstrap_remotepairing_over_usb(lockdown)

                        _, mounted_now = await check_device_ddi_status(lockdown)
                        if mounted_now:
                            ddi_already_mounted = True
                            print(f"✅ [DDI] 裝置 {self.device_name} 已掛載 DDI (透過 {'USB' if is_usb_conn else 'Wi-Fi'} 確認)")
                            if ios_major < 17:
                                self.transport_used = "Wi-Fi" if not is_usb_conn else "USB"
                                async with DvtProvider(lockdown) as dvt, LocationSimulation(dvt) as sim:
                                    was_connected_once = True
                                    await self._run_sim_loop(lockdown, dvt, sim, init_lat, init_lon)
                                return
                            break
                        elif is_usb_conn:
                            try:
                                await _ensure_ddi_mounted(lockdown, service_ref=self)
                                ddi_already_mounted = True
                                break
                            except RSDRequiredError:
                                print("ℹ️ [DDI] 此 iOS 版本需透過 RSD 隧道安裝 Cryptex1 DDI。")
                                break
                except Exception:
                    continue

            # 🎯 2. 建立 RSD 隧道：即使插著 USB 線，只要 DDI 已掛載就優先嘗試 Wi-Fi 無線連線 (`prefer_wifi=True`)
            self.connect_stage = "正在優先以 Wi-Fi 建立 RSD 安全隧道..."
            async with ResilientRsdTunnel(serial=target_serial, autopair=True, prefer_wifi=True) as tunnel_rsd:
                rsd = tunnel_rsd
                if rsd.udid:
                    self.current_udid = rsd.udid
                tunnel_obj = _active_tunnels_by_udid.get(self.current_udid) or _active_tunnels_by_udid.get(target_serial or "")
                self.transport_used = tunnel_obj.transport_used if tunnel_obj else "Wi-Fi/RSD"

                if not ddi_already_mounted:
                    await _ensure_ddi_mounted(rsd, service_ref=self)

                self.connect_stage = "正在啟動定位模擬服務..."
                async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as sim:
                    was_connected_once = True
                    await self._run_sim_loop(rsd, dvt, sim, init_lat, init_lon, tunnel_obj=tunnel_obj)

        except (NotPairedError, UserDeniedPairingError, PairingError) as e:
            self.is_connected = False
            self.is_running = False
            self.last_error = (
                "尚未完成手機信任配對！\n"
                "請將 iPhone 解鎖、使用傳輸線連接 Mac，並在手機跳出的視窗點選「信任這部電腦」並輸入手機密碼。"
                f"\n({type(e).__name__})"
            )
            add_debug_log("手機尚未完成信任配對", f"{type(e).__name__}: {e}", stage="裝置信任配對", udid=target_serial)
        except DeveloperModeIsNotEnabledError as e:
            self.is_connected = False
            self.is_running = False
            self.last_error = "偵測到 iPhone 尚未開啟「開發者模式」！請至「設定 > 隱私權與安全性 > 開發者模式」開啟。"
            add_debug_log("開發者模式未啟用", f"{type(e).__name__}: {e}", stage="開發者模式檢查", udid=target_serial)
        except NoDeviceConnectedError as e:
            self.is_connected = False
            self.is_running = False
            self.last_error = "偵測不到 iPhone 裝置！首次掛載 DDI 請先插上 USB 傳輸線，完成後即可全程使用 Wi-Fi 連線。"
            add_debug_log("找不到可用的 iPhone 裝置", f"{type(e).__name__}: {e}", stage="裝置探索", udid=target_serial)
        except Exception as e:
            self.is_connected = False
            self.is_running = False
            self.last_error = f"無法與 iPhone 建立或維持定位通道 ({type(e).__name__}: {e})。"
            stage_label = "DVT 定位維持中斷" if was_connected_once else (self.connect_stage or "建立連線通道")
            add_debug_log(
                title="連線通道異常中斷" if was_connected_once else "建立連線或掛載服務失敗",
                error_msg=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}",
                stage=stage_label,
                udid=target_serial,
            )
            if was_connected_once and not self.user_requested_stop:
                send_macos_notification("SIM-GPS 警報", "裝置連線中斷", f"與 {self.device_name} 連線中斷，正在嘗試自動重連...", sound_name="Basso")
                await asyncio.sleep(1.5)
                if not self.user_requested_stop:
                    await self.cmd_queue.put(("START", (target_serial or "default", self.current_lat, self.current_lon)))
        finally:
            self.sim = None
            self.active_dvt = None
            self.active_provider = None
            self.active_tunnel = None
            self.ddi_power_saving_active = False
            self.ddi_power_saving_profile = ""
            self.is_connected = False
            self.is_running = False
            self.is_connecting = False
            self.is_returning_home = False
            self.is_stopping = False
            self.connect_stage = ""

    def connect(self, lat: float, lon: float):
        self.is_connecting = True
        self.is_returning_home = False
        self.is_stopping = False
        self.user_requested_stop = False
        self.last_error = None
        self.current_lat = lat
        self.current_lon = lon
        asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("START", (self.udid, lat, lon))), self.loop)

    def set_location(self, lat: float, lon: float, force_returning: bool = False):
        if self.is_connected and self.loop:
            if self.is_returning_home and not force_returning:
                return
            self.current_lat = lat
            self.current_lon = lon
            asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("SET", (lat, lon))), self.loop)

    def stop(self):
        self.user_requested_stop = True
        self.is_returning_home = False
        self.is_stopping = False
        if (self.is_connected or self.is_running or self.is_connecting) and self.loop:
            asyncio.run_coroutine_threadsafe(self.cmd_queue.put(("STOP", None)), self.loop)

    # --- 🤖 單機 DVT / CoreDevice 自動化與熱狀態/功耗感測方法 ---

    async def _async_query_pikmin_process(self, include_thermal: bool = False) -> Dict[str, Any]:
        if not self.is_connected or self.active_dvt is None:
            return {
                "running": None,
                "foreground": False,
                "foreground_hint": False,
                "pid": None,
                "cpu": 0.0,
                "thermal_state": self.last_thermal_state,
                "thermal_label": self.last_thermal_label,
                "induced_thermal_state": -1,
                "proc_info": None,
            }

        async with self._dvt_op_lock:
            try:
                if self.active_tunnel is not None:
                    self.active_tunnel.activate_for_udp()

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

                thermal_state: Optional[int] = self.last_thermal_state
                induced_thermal_state: int = -1
                energy_snapshot: Dict[str, Any] = {}

                target_pid_for_energy = pikmin_proc.get("pid") if pikmin_proc else 1
                if include_thermal and target_pid_for_energy:
                    try:
                        async with EnergyMonitor(self.active_dvt, [int(target_pid_for_energy)]) as em:
                            sample = await asyncio.wait_for(em._sample_once(), timeout=2.0)
                            if isinstance(sample, dict):
                                p_energy = sample.get(str(target_pid_for_energy)) or sample.get(int(target_pid_for_energy))
                                if isinstance(p_energy, dict):
                                    energy_snapshot = _sanitize_for_json(p_energy)
                                    ts_val = p_energy.get("energy.thermalstate.cost")
                                    ind_val = p_energy.get("energy.inducedthermalstate.cost")
                                    if isinstance(ts_val, (int, float)):
                                        thermal_state = int(ts_val)
                                    if isinstance(ind_val, (int, float)):
                                        induced_thermal_state = int(ind_val)
                    except Exception:
                        pass

                if thermal_state is not None:
                    self.last_thermal_state = thermal_state
                    if self.ddi_power_saving_active and induced_thermal_state >= 1:
                        self.last_thermal_label = "省電降載中"
                    else:
                        self.last_thermal_label = THERMAL_STATE_LABELS.get(thermal_state, f"等級{thermal_state}")

                if pikmin_proc is None:
                    return {
                        "running": False,
                        "foreground": False,
                        "foreground_hint": False,
                        "pid": None,
                        "cpu": 0.0,
                        "thermal_state": thermal_state,
                        "thermal_label": self.last_thermal_label,
                        "induced_thermal_state": induced_thermal_state,
                        "energy_snapshot": energy_snapshot,
                        "proc_info": None,
                    }

                pid = pikmin_proc.get("pid")
                # iOS DeviceInfo.proclist() 的 foregroundRunning 在部分 iOS 18/26 機型可能恆為 False 或 None，
                # 因此只要 native_fg 不為 True，一律透過 Sysmontap 採樣真實 CPU / PowerScore 判定是否在前景運行
                native_fg = pikmin_proc.get("foregroundRunning")
                if native_fg is None:
                    native_fg = pikmin_proc.get("foreground")

                cpu_usage = 0.0
                power_score = 0.0
                sysmon_failed = False
                sysmon_sample: Dict[str, Any] = {}

                if native_fg is not True or include_thermal:
                    try:
                        async with await Sysmontap.create(self.active_dvt, interval=220) as sysmon:
                            sample_idx = 0
                            async for entries in sysmon.iter_processes():
                                sample_idx += 1
                                for item in entries:
                                    if item.get("pid") == pid or "pikmin" in str(item.get("name", "")).lower():
                                        raw_cpu = item.get("cpuUsage")
                                        if isinstance(raw_cpu, (int, float)):
                                            cpu_usage = max(cpu_usage, float(raw_cpu))
                                        raw_ps = item.get("powerScore") or item.get("avgPowerScore")
                                        if isinstance(raw_ps, (int, float)):
                                            power_score = max(power_score, float(raw_ps))
                                        sysmon_sample = {
                                            k: v for k, v in item.items()
                                            if k in ("pid", "name", "cpuUsage", "powerScore", "avgPowerScore", "physFootprint")
                                        }
                                # 第 1 筆為基準點 (cpuUsage 常為 0)，取到第 2~3 筆或已測得前景負載即結束
                                if sample_idx >= 2 and (cpu_usage >= 2.5 or sample_idx >= 3):
                                    break
                    except Exception as sys_err:
                        sysmon_failed = True
                        sysmon_sample = {"sysmontap_error": str(sys_err)}

                if native_fg is True or cpu_usage >= 2.5 or power_score >= 5.0:
                    is_fg = True
                    fg_uncertain = False
                elif sysmon_failed:
                    # 若皮克敏確定正在運行但 Sysmontap 瞬間忙線，標記為 uncertain 避免誤判為不在前景
                    is_fg = True
                    fg_uncertain = True
                else:
                    is_fg = False
                    fg_uncertain = False

                safe_proc = _sanitize_for_json(pikmin_proc) if isinstance(pikmin_proc, dict) else {}
                return {
                    "running": True,
                    "foreground": is_fg,
                    "foreground_hint": is_fg,
                    "foreground_uncertain": fg_uncertain,
                    "pid": pid,
                    "cpu": round(cpu_usage, 1),
                    "thermal_state": thermal_state,
                    "thermal_label": self.last_thermal_label,
                    "induced_thermal_state": induced_thermal_state,
                    "energy_snapshot": energy_snapshot,
                    "proc_info": safe_proc,
                    "sysmon_sample": _sanitize_for_json(sysmon_sample),
                }
            except Exception as e:
                return {
                    "running": None,
                    "foreground": False,
                    "foreground_hint": False,
                    "pid": None,
                    "cpu": 0.0,
                    "thermal_state": self.last_thermal_state,
                    "thermal_label": self.last_thermal_label,
                    "error": str(e),
                }

    def query_pikmin_process_sync(self, include_thermal: bool = False) -> Dict[str, Any]:
        if not self.is_connected or not self.loop:
            return {"running": None, "foreground": False, "foreground_hint": False, "pid": None, "cpu": 0.0}
        fut = asyncio.run_coroutine_threadsafe(self._async_query_pikmin_process(include_thermal=include_thermal), self.loop)
        try:
            return fut.result(timeout=6.5)
        except Exception as e:
            return {"running": None, "foreground": False, "foreground_hint": False, "pid": None, "cpu": 0.0, "error": str(e)}

    async def _async_query_battery_and_pikmin(self, include_raw: bool = False) -> Dict[str, Any]:
        bat_data: Dict[str, Any] = {
            "battery_level": None,
            "is_charging": None,
            "temperature_c": None,
            "instant_amperage_ma": None,
            "amperage_ma": None,
            "voltage_mv": None,
            "battery_power_mw": None,
            "system_load_mw": None,
            "system_load_w": None,
            "thermally_limited_sec": None,
        }
        raw_combined: Dict[str, Any] = {}
        target_udid = self.current_udid if (self.current_udid and self.current_udid != "default") else self.target_udid
        if target_udid == "default":
            target_udid = None

        # 1. 優先透過 usbmux Lockdown (USB 或 Network Wi-Fi) 查詢 DiagnosticsService (IOPMPowerSource)
        for conn_t in (None, "Network"):
            try:
                lockdown = await asyncio.wait_for(
                    create_using_usbmux(serial=target_udid, autopair=False, connection_type=conn_t),
                    timeout=2.2,
                )
                try:
                    mux_bat = await query_battery_from_provider(lockdown, include_raw=include_raw)
                    for k in bat_data:
                        if bat_data.get(k) is None and mux_bat.get(k) is not None:
                            bat_data[k] = mux_bat[k]
                    if include_raw and mux_bat.get("raw_diagnostics"):
                        raw_combined[f"lockdown_{conn_t or 'auto'}"] = mux_bat["raw_diagnostics"]
                    if bat_data.get("battery_level") is not None and bat_data.get("system_load_w") is not None:
                        break
                finally:
                    await lockdown.close()
            except Exception as e_mux:
                if include_raw:
                    raw_combined[f"lockdown_{conn_t or 'auto'}_error"] = f"{type(e_mux).__name__}: {e_mux}"

        # 2. 若透過 usbmux 尚未取得電量，再透過當前 RSD provider 查詢
        if bat_data.get("battery_level") is None and self.active_provider is not None:
            try:
                if self.active_tunnel is not None:
                    self.active_tunnel.activate_for_udp()
                rsd_bat = await query_battery_from_provider(self.active_provider, include_raw=include_raw)
                for k in bat_data:
                    if bat_data.get(k) is None and rsd_bat.get(k) is not None:
                        bat_data[k] = rsd_bat[k]
                if include_raw and rsd_bat.get("raw_diagnostics"):
                    raw_combined["rsd_provider"] = rsd_bat["raw_diagnostics"]
            except Exception as e_rsd:
                if include_raw:
                    raw_combined["rsd_provider_error"] = f"{type(e_rsd).__name__}: {e_rsd}"

        if bat_data.get("system_load_w") is not None:
            self.last_system_load_w = bat_data["system_load_w"]
        if bat_data.get("instant_amperage_ma") is not None:
            self.last_instant_amperage_ma = bat_data["instant_amperage_ma"]

        proc_data = await self._async_query_pikmin_process(include_thermal=True)

        if include_raw:
            raw_combined["dvt_energy_and_thermal"] = {
                "thermal_state": proc_data.get("thermal_state"),
                "thermal_label": proc_data.get("thermal_label"),
                "induced_thermal_state": proc_data.get("induced_thermal_state"),
                "ddi_power_saving_active": self.ddi_power_saving_active,
                "ddi_power_saving_profile": self.ddi_power_saving_profile,
                "energy_snapshot": proc_data.get("energy_snapshot"),
            }
            clean_combined = _sanitize_for_json(raw_combined)
            bat_data["raw_diagnostics"] = clean_combined
            bat_data["raw"] = clean_combined

        return _sanitize_for_json({
            **bat_data,
            "thermal_state": proc_data.get("thermal_state"),
            "thermal_label": proc_data.get("thermal_label") or self.last_thermal_label,
            "induced_thermal_state": proc_data.get("induced_thermal_state", -1),
            "ddi_power_saving_active": self.ddi_power_saving_active,
            "ddi_power_saving_profile": self.ddi_power_saving_profile,
            "pikmin_running": proc_data.get("running"),
            "pikmin_foreground": proc_data.get("foreground"),
            "pikmin_foreground_hint": proc_data.get("foreground_hint"),
            "pikmin_pid": proc_data.get("pid"),
            "pikmin_cpu": proc_data.get("cpu", 0.0),
            "proc_info": proc_data.get("proc_info"),
        })

    def query_battery_and_pikmin_sync(self, include_raw: bool = False) -> Dict[str, Any]:
        if not self.is_connected or not self.loop:
            return {}
        fut = asyncio.run_coroutine_threadsafe(self._async_query_battery_and_pikmin(include_raw=include_raw), self.loop)
        try:
            return fut.result(timeout=10.0)
        except Exception as e:
            return {"error": f"Timeout/Error: {e}"}

    async def _async_capture_screenshot(self) -> Optional[bytes]:
        if not self.is_connected:
            return None
        async with self._dvt_op_lock:
            if self.active_tunnel is not None:
                self.active_tunnel.activate_for_udp()
            if self.active_dvt is not None:
                try:
                    async with Screenshot(self.active_dvt) as shot:
                        data = await asyncio.wait_for(shot.get_screenshot(), timeout=4.0)
                        if data:
                            return bytes(data)
                except Exception as e:
                    print(f"[DVT] Screenshot 擷取異常 ({self.current_udid})，嘗試 ScreenCaptureService: {e}")

            if isinstance(self.active_provider, RemoteServiceDiscoveryService):
                try:
                    from pymobiledevice3.remote.core_device.screen_capture_service import ScreenCaptureService
                    async with ScreenCaptureService(self.active_provider) as cap_svc:
                        res = await asyncio.wait_for(cap_svc.capture_screenshot(), timeout=4.5)
                        if isinstance(res, dict) and res.get("image"):
                            return bytes(res["image"])
                except Exception as e2:
                    print(f"[CoreDevice] ScreenCaptureService 擷取失敗 ({self.current_udid}): {e2}")
        return None

    def capture_screenshot_sync(self) -> Optional[bytes]:
        if not self.is_connected or not self.loop:
            return None
        fut = asyncio.run_coroutine_threadsafe(self._async_capture_screenshot(), self.loop)
        try:
            return fut.result(timeout=7.0)
        except Exception:
            return None

    async def _async_tap_screen(self, norm_x: float, norm_y: float) -> Dict[str, Any]:
        if not self.is_connected or not isinstance(self.active_provider, RemoteServiceDiscoveryService):
            return {"ok": False, "error": "裝置未以 iOS 17+ RSD 通道連線"}

        hid_x = int(round(max(0.0, min(1.0, norm_x)) * 65535))
        hid_y = int(round(max(0.0, min(1.0, norm_y)) * 65535))

        async with self._dvt_op_lock:
            try:
                if self.active_tunnel is not None:
                    self.active_tunnel.activate_for_udp()

                from pymobiledevice3.remote.core_device.hid_service import (
                    DIGITIZER_SURFACE_MAIN_TOUCHSCREEN,
                    TOUCHSCREEN_STATE_CONTACT,
                    TOUCHSCREEN_STATE_RELEASE,
                    touch_session,
                )

                t0 = time.monotonic()
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
                # 給予 DisplayService 0.45s 緩衝釋放媒體串流，避免連續點擊時握手衝突
                await asyncio.sleep(0.45)
                elapsed_ms = int(round((time.monotonic() - t0) * 1000))
                print(f"👆 [HID Tap] ({self.current_udid}) 點擊成功: norm=({norm_x:.3f}, {norm_y:.3f}) -> HID=({hid_x}, {hid_y}) ({elapsed_ms}ms)")
                return {
                    "ok": True,
                    "udid": self.current_udid,
                    "norm_x": round(norm_x, 4),
                    "norm_y": round(norm_y, 4),
                    "hid_x": hid_x,
                    "hid_y": hid_y,
                    "elapsed_ms": elapsed_ms,
                    "stack_addr": getattr(getattr(self.active_tunnel, "tun", None), "addr", None),
                }
            except Exception as e:
                err_str = f"{type(e).__name__}: {e}"
                add_debug_log(
                    title="傳送螢幕模擬點擊 (HID Tap) 失敗",
                    error_msg=err_str,
                    stage="遠端觸控",
                    level="WARN",
                    udid=self.current_udid,
                )
                return {"ok": False, "error": err_str, "hid_x": hid_x, "hid_y": hid_y}

    def tap_screen_sync(self, norm_x: float, norm_y: float) -> bool:
        res = self.tap_screen_detailed_sync(norm_x, norm_y)
        return bool(res.get("ok"))

    def tap_screen_detailed_sync(self, norm_x: float, norm_y: float) -> Dict[str, Any]:
        if not self.is_connected or not self.loop:
            return {"ok": False, "error": "尚未連線至 iOS 裝置"}
        fut = asyncio.run_coroutine_threadsafe(self._async_tap_screen(norm_x, norm_y), self.loop)
        try:
            return fut.result(timeout=12.0)
        except Exception as e:
            return {"ok": False, "error": f"Timeout/Error: {e}"}

    async def _async_launch_pikmin(self, bundle_id: str = "com.nianticlabs.pikmin") -> Dict[str, Any]:
        if not self.is_connected:
            return {"ok": False, "error": "尚未連線"}
        async with self._dvt_op_lock:
            if self.active_tunnel is not None:
                self.active_tunnel.activate_for_udp()
            if self.active_dvt is not None:
                try:
                    async with ProcessControl(self.active_dvt) as proc_ctrl:
                        pid = await asyncio.wait_for(
                            proc_ctrl.launch(bundle_id=bundle_id, kill_existing=False),
                            timeout=5.0,
                        )
                        print(f"🚀 [DVT] 已在裝置 {self.current_udid} 啟動/喚醒 {bundle_id} (PID={pid})")
                        return {"ok": True, "method": "DVT ProcessControl", "pid": pid, "bundle_id": bundle_id}
                except Exception as e:
                    dvt_err = str(e)
            else:
                dvt_err = "No active DVT"

            if isinstance(self.active_provider, RemoteServiceDiscoveryService):
                try:
                    from pymobiledevice3.remote.core_device.app_service import AppServiceService
                    async with AppServiceService(self.active_provider) as app_svc:
                        res = await asyncio.wait_for(
                            app_svc.start_app(bundle_id=bundle_id, kill_existing=False),
                            timeout=5.0,
                        )
                        return {"ok": True, "method": "CoreDevice AppService", "result": _sanitize_for_json(res)}
                except Exception as e2:
                    return {"ok": False, "error": f"DVT: {dvt_err} | AppService: {e2}"}
        return {"ok": False, "error": dvt_err}

    def launch_pikmin_sync(self, bundle_id: str = "com.nianticlabs.pikmin") -> bool:
        return bool(self.launch_pikmin_detailed_sync(bundle_id).get("ok"))

    def launch_pikmin_detailed_sync(self, bundle_id: str = "com.nianticlabs.pikmin") -> Dict[str, Any]:
        if not self.is_connected or not self.loop:
            return {"ok": False, "error": "尚未連線"}
        fut = asyncio.run_coroutine_threadsafe(self._async_launch_pikmin(bundle_id), self.loop)
        try:
            return fut.result(timeout=8.0)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _async_kill_pikmin(self) -> Dict[str, Any]:
        """直接強制關閉皮克敏 Process (用於「低電量不返航，停止種花後直接關閉遊戲」)"""
        proc_info = await self._async_query_pikmin_process()
        pid = proc_info.get("pid")
        if not pid:
            return {"ok": True, "msg": "皮克敏未在執行中，無須關閉"}

        async with self._dvt_op_lock:
            if self.active_dvt is not None:
                try:
                    async with ProcessControl(self.active_dvt) as proc_ctrl:
                        await asyncio.wait_for(proc_ctrl.kill(pid), timeout=4.0)
                        print(f"🛑 [DVT] 已直接關閉裝置 {self.current_udid} 的皮克敏 Process (PID={pid})")
                        return {"ok": True, "method": "DVT ProcessControl.kill", "pid": pid}
                except Exception as e:
                    kill_err = str(e)
            else:
                kill_err = "No DVT"

            if isinstance(self.active_provider, RemoteServiceDiscoveryService):
                try:
                    from pymobiledevice3.remote.core_device.app_service import AppServiceService
                    async with AppServiceService(self.active_provider) as app_svc:
                        await asyncio.wait_for(app_svc.send_signal_to_process(pid, 9), timeout=4.0)
                        return {"ok": True, "method": "CoreDevice AppService SIGKILL", "pid": pid}
                except Exception as e2:
                    return {"ok": False, "error": f"{kill_err} | {e2}", "pid": pid}
        return {"ok": False, "error": kill_err, "pid": pid}

    def kill_pikmin_sync(self) -> Dict[str, Any]:
        if not self.is_connected or not self.loop:
            return {"ok": False, "error": "尚未連線"}
        fut = asyncio.run_coroutine_threadsafe(self._async_kill_pikmin(), self.loop)
        try:
            return fut.result(timeout=6.0)
        except Exception as e:
            return {"ok": False, "error": str(e)}


class MultiDeviceGpsManager:
    """
    管理多台 iOS 裝置同時連線、同步移動、DDI 低耗電模式連動與個別裝置獨立 60 km/h 平滑返航。
    """

    def __init__(self, state_ref: Dict[str, Any]):
        self.state_ref = state_ref
        self.sessions: Dict[str, IOSUserspaceGpsService] = {}
        self.ddi_mounted_udids: set[str] = set()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    def start_worker(self):
        if self.worker_thread is not None and self.worker_thread.is_alive():
            return

        def _loop_runner():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        self.worker_thread = threading.Thread(target=_loop_runner, daemon=True)
        self.worker_thread.start()
        while self.loop is None:
            time.sleep(0.01)

    def get_session(self, udid: Optional[str] = None) -> Optional[IOSUserspaceGpsService]:
        with self.lock:
            if udid and udid != "default":
                if udid in self.sessions:
                    return self.sessions[udid]
                for s in self.sessions.values():
                    if s.current_udid == udid or s.target_udid == udid:
                        return s
                return None
            for s in self.sessions.values():
                if s.is_connected:
                    return s
            return next(iter(self.sessions.values()), None)

    def get_all_sessions(self) -> List[IOSUserspaceGpsService]:
        with self.lock:
            return list(self.sessions.values())

    def get_connected_sessions(self) -> List[IOSUserspaceGpsService]:
        with self.lock:
            return [s for s in self.sessions.values() if s.is_connected]

    def get_active_following_sessions(self) -> List[IOSUserspaceGpsService]:
        """回傳所有「已連線且未處於獨立返航中」的裝置（跟隨主路線移動的隊伍）"""
        with self.lock:
            return [s for s in self.sessions.values() if s.is_connected and not s.is_returning_home]

    def get_returning_sessions(self) -> List[IOSUserspaceGpsService]:
        """回傳所有「正在獨立 60 km/h 平滑返航中」的裝置"""
        with self.lock:
            return [s for s in self.sessions.values() if s.is_connected and s.is_returning_home]

    @property
    def connected_udids(self) -> List[str]:
        with self.lock:
            return [
                (s.current_udid or s.target_udid)
                for s in self.sessions.values()
                if s.is_connected and (s.current_udid or s.target_udid)
            ]

    def get_connected_udids(self) -> List[str]:
        return self.connected_udids

    def is_device_connected(self, udid: Optional[str] = None) -> bool:
        if not udid or udid == "default":
            return self.is_connected
        sess = self.get_session(udid)
        return bool(sess and sess.is_connected)

    @property
    def is_connected(self) -> bool:
        return len(self.get_connected_sessions()) > 0

    @property
    def is_connecting(self) -> bool:
        with self.lock:
            return any(s.is_connecting for s in self.sessions.values())

    @property
    def connect_stage(self) -> str:
        with self.lock:
            for s in self.sessions.values():
                if s.is_connecting and s.connect_stage:
                    return s.connect_stage
        return ""

    @property
    def current_udid(self) -> Optional[str]:
        s = self.get_session()
        return (s.current_udid or s.target_udid) if s else None

    def connect_device(self, udid: str, lat: float, lon: float, prefer_wifi: bool = True) -> IOSUserspaceGpsService:
        self.start_worker()
        with self.lock:
            sess = self.sessions.get(udid)
            if sess is None:
                sess = IOSUserspaceGpsService(udid=udid, state_ref=self.state_ref, shared_loop=self.loop)
                self.sessions[udid] = sess
        sess.connect(lat, lon)
        return sess

    def connect(self, udid: str, lat: float, lon: float, prefer_wifi: bool = True) -> IOSUserspaceGpsService:
        return self.connect_device(udid, lat, lon, prefer_wifi=prefer_wifi)

    def set_location(self, lat: float, lon: float, target_udid: Optional[str] = None) -> None:
        """若指定 target_udid 則只移動該裝置；否則同步移動所有跟隨主路線的裝置"""
        if target_udid:
            sess = self.get_session(target_udid)
            if sess and sess.is_connected:
                sess.set_location(lat, lon)
            return

        for sess in self.get_active_following_sessions():
            sess.set_location(lat, lon)

    def set_all_ddi_power_saving(self, enabled: bool) -> Dict[str, Any]:
        """當使用者在主頁切換「連線自動切低耗電模式」開關時，立即同步套用或解除所有已連線裝置的低電量模式"""
        results = {}
        for sess in self.get_connected_sessions():
            u = sess.current_udid or sess.target_udid
            results[u] = sess.set_ddi_power_saving_sync(enabled)
        return {"ok": True, "enabled": enabled, "devices": results}

    def start_device_return_home(self, udid: str) -> bool:
        sess = self.get_session(udid)
        if not sess or not sess.is_connected:
            return False
        sess.is_returning_home = True
        sess.is_stopping = True
        return True

    def stop_device(self, udid: str) -> None:
        sess = self.get_session(udid)
        if sess:
            sess.stop()

    def stop(self) -> None:
        """停止所有已連線裝置並還原真實定位與正常效能模式"""
        with self.lock:
            all_sess = list(self.sessions.values())
        for s in all_sess:
            s.stop()

    # 相容單機介面 (供預設呼叫或指定 udid 呼叫)
    def query_pikmin_process_sync(self, udid: Optional[str] = None, include_thermal: bool = False) -> Dict[str, Any]:
        sess = self.get_session(udid)
        return sess.query_pikmin_process_sync(include_thermal=include_thermal) if sess else {"running": None}

    def query_battery_and_pikmin_sync(self, udid: Optional[str] = None, include_raw: bool = False) -> Dict[str, Any]:
        sess = self.get_session(udid)
        return sess.query_battery_and_pikmin_sync(include_raw=include_raw) if sess else {}

    def capture_screenshot_sync(self, udid: Optional[str] = None) -> Optional[bytes]:
        sess = self.get_session(udid)
        return sess.capture_screenshot_sync() if sess else None

    def tap_screen_sync(self, norm_x: float, norm_y: float, udid: Optional[str] = None) -> bool:
        sess = self.get_session(udid)
        return sess.tap_screen_sync(norm_x, norm_y) if sess else False

    def tap_screen_detailed_sync(self, norm_x: float, norm_y: float, udid: Optional[str] = None) -> Dict[str, Any]:
        sess = self.get_session(udid)
        return sess.tap_screen_detailed_sync(norm_x, norm_y) if sess else {"ok": False, "error": "找不到連線裝置"}

    def launch_pikmin_sync(self, udid: Optional[str] = None) -> bool:
        sess = self.get_session(udid)
        return sess.launch_pikmin_sync() if sess else False

    def launch_pikmin_detailed_sync(self, udid: Optional[str] = None) -> Dict[str, Any]:
        sess = self.get_session(udid)
        return sess.launch_pikmin_detailed_sync() if sess else {"ok": False, "error": "找不到連線裝置"}

    def kill_pikmin_sync(self, udid: Optional[str] = None) -> Dict[str, Any]:
        sess = self.get_session(udid)
        return sess.kill_pikmin_sync() if sess else {"ok": False, "error": "找不到連線裝置"}


async def _probe_single_usbmux_device(dev, gps_service: MultiDeviceGpsManager) -> Optional[dict]:
    udid = dev.serial
    conn_type = "USB" if dev.is_usb else ("Wi-Fi" if dev.is_network else str(dev.connection_type or "USB/Wi-Fi"))
    is_active = gps_service.is_device_connected(udid)
    sess = gps_service.get_session(udid)

    try:
        async with await asyncio.wait_for(
            create_using_usbmux(serial=udid, autopair=False, connection_type=dev.connection_type),
            timeout=2.2,
        ) as lockdown:
            vals = lockdown.all_values
            name = vals.get("DeviceName") or lockdown.display_name or "iOS Device"
            version = vals.get("ProductVersion", "")

            if dev.is_usb:
                with suppress(Exception):
                    if not await asyncio.wait_for(lockdown.get_enable_wifi_connections(), timeout=1.0):
                        await asyncio.wait_for(lockdown.set_enable_wifi_connections(True), timeout=1.0)
                        print(f"📶 已自動為 USB 裝置 {name} ({udid}) 開啟 Wi-Fi 無線連線功能")

            dev_mode_ok, ddi_mounted = await check_device_ddi_status(lockdown)
            bat_info = await query_battery_from_provider(lockdown)

            if ddi_mounted:
                gps_service.ddi_mounted_udids.add(udid)
            elif udid in gps_service.ddi_mounted_udids:
                ddi_mounted = True

            if is_active:
                ddi_status = "mounted"
            elif dev_mode_ok is False:
                ddi_status = "dev_mode_off"
            elif ddi_mounted is True:
                ddi_status = "mounted"
            elif ddi_mounted is False:
                ddi_status = "unmounted"
            else:
                ddi_status = "unknown"

            return {
                "udid": udid,
                "name": f"{name} ({version})" if version else name,
                "connection": conn_type,
                "has_usb": bool(dev.is_usb),
                "has_wifi": bool(dev.is_network),
                "dev_mode_enabled": dev_mode_ok,
                "ddi_mounted": ddi_mounted,
                "ddi_status": ddi_status,
                "battery_level": bat_info.get("battery_level"),
                "is_charging": bat_info.get("is_charging"),
                "temperature_c": bat_info.get("temperature_c"),
                "system_load_w": bat_info.get("system_load_w") or (sess.last_system_load_w if sess else None),
                "instant_amperage_ma": bat_info.get("instant_amperage_ma") or (sess.last_instant_amperage_ma if sess else None),
                "thermal_state": sess.last_thermal_state if sess else None,
                "thermal_label": sess.last_thermal_label if (sess and sess.is_connected) else None,
                "ddi_power_saving_active": bool(sess.ddi_power_saving_active) if sess else False,
            }
    except Exception:
        known_mounted = is_active or (udid in gps_service.ddi_mounted_udids)
        return {
            "udid": udid,
            "name": f"iOS 裝置 ({udid[:6]}...)",
            "connection": conn_type,
            "has_usb": bool(dev.is_usb),
            "has_wifi": bool(dev.is_network),
            "dev_mode_enabled": None,
            "ddi_mounted": True if known_mounted else None,
            "ddi_status": "mounted" if known_mounted else "unknown",
            "battery_level": None,
            "is_charging": None,
            "temperature_c": None,
            "system_load_w": sess.last_system_load_w if sess else None,
            "instant_amperage_ma": sess.last_instant_amperage_ma if sess else None,
            "thermal_state": sess.last_thermal_state if sess else None,
            "thermal_label": sess.last_thermal_label if (sess and sess.is_connected) else None,
            "ddi_power_saving_active": bool(sess.ddi_power_saving_active) if sess else False,
        }


async def _probe_single_bonjour_service(svc, gps_service: MultiDeviceGpsManager) -> dict:
    udid = svc.udid
    is_active = gps_service.is_device_connected(udid)
    sess = gps_service.get_session(udid)
    dev_mode_ok, ddi_mounted = await check_device_ddi_status(svc)
    bat_info = await query_battery_from_provider(svc)

    if ddi_mounted:
        gps_service.ddi_mounted_udids.add(udid)
    elif udid in gps_service.ddi_mounted_udids:
        ddi_mounted = True

    if is_active:
        ddi_status = "mounted"
    elif dev_mode_ok is False:
        ddi_status = "dev_mode_off"
    elif ddi_mounted is True:
        ddi_status = "mounted"
    elif ddi_mounted is False:
        ddi_status = "unmounted"
    else:
        ddi_status = "unknown"

    with suppress(Exception):
        await svc.close()

    return {
        "udid": udid,
        "name": f"iPhone ({udid[:8]}...)",
        "connection": f"Wi-Fi ({svc.hostname.split('.')[0]})",
        "has_usb": False,
        "has_wifi": True,
        "dev_mode_enabled": dev_mode_ok,
        "ddi_mounted": ddi_mounted,
        "ddi_status": ddi_status,
        "battery_level": bat_info.get("battery_level"),
        "is_charging": bat_info.get("is_charging"),
        "temperature_c": bat_info.get("temperature_c"),
        "system_load_w": bat_info.get("system_load_w") or (sess.last_system_load_w if sess else None),
        "instant_amperage_ma": bat_info.get("instant_amperage_ma") or (sess.last_instant_amperage_ma if sess else None),
        "thermal_state": sess.last_thermal_state if sess else None,
        "thermal_label": sess.last_thermal_label if (sess and sess.is_connected) else None,
        "ddi_power_saving_active": bool(sess.ddi_power_saving_active) if sess else False,
    }


async def scan_ios_devices_async(gps_service: MultiDeviceGpsManager) -> List[dict]:
    """並行探索所有 USB 與 Wi-Fi iOS 裝置，若同時具備 USB 與 Wi-Fi 且已掛載 DDI，標註『Wi-Fi 優先 (USB備援)』"""
    devices_by_udid: Dict[str, dict] = {}

    try:
        mux_devs = await asyncio.wait_for(usbmux.list_devices(), timeout=2.5)
        if mux_devs:
            results = await asyncio.gather(
                *(_probe_single_usbmux_device(d, gps_service) for d in mux_devs),
                return_exceptions=True,
            )
            for item in results:
                if isinstance(item, dict) and "udid" in item:
                    u = item["udid"]
                    if u not in devices_by_udid:
                        devices_by_udid[u] = item
                    else:
                        prev = devices_by_udid[u]
                        prev["has_usb"] = prev.get("has_usb", False) or item.get("has_usb", False)
                        prev["has_wifi"] = prev.get("has_wifi", False) or item.get("has_wifi", False)
                        if prev["has_usb"] and prev["has_wifi"]:
                            prev["connection"] = "Wi-Fi 優先 (USB備援)"
                        if "iOS 裝置" in prev["name"] and "iOS 裝置" not in item["name"]:
                            prev["name"] = item["name"]
                        if item.get("ddi_status") == "mounted":
                            prev["ddi_status"] = "mounted"
                            prev["ddi_mounted"] = True
                        elif prev.get("ddi_status") == "unknown" and item.get("ddi_status") != "unknown":
                            prev["ddi_status"] = item["ddi_status"]
                            prev["ddi_mounted"] = item["ddi_mounted"]
                        for k in ("battery_level", "is_charging", "temperature_c", "system_load_w", "instant_amperage_ma", "thermal_state", "thermal_label"):
                            if prev.get(k) is None and item.get(k) is not None:
                                prev[k] = item[k]
    except Exception as e:
        print(f"[Scan] usbmux 掃描異常: {e}")

    try:
        paired_udids = list(iter_remote_pair_records_by_identifier())
        missing_udids = [u for u in paired_udids if u not in devices_by_udid]
        if missing_udids:
            rp_services = await asyncio.wait_for(get_remote_pairing_tunnel_services(), timeout=2.2)
            to_probe = []
            for svc in rp_services:
                if svc.udid not in devices_by_udid:
                    to_probe.append(svc)
                else:
                    # 標註此裝置同時具備 Wi-Fi RemotePairing 能力
                    devices_by_udid[svc.udid]["has_wifi"] = True
                    if devices_by_udid[svc.udid].get("has_usb"):
                        devices_by_udid[svc.udid]["connection"] = "Wi-Fi 優先 (USB備援)"
                    with suppress(Exception):
                        await svc.close()
            if to_probe:
                rp_results = await asyncio.gather(
                    *(_probe_single_bonjour_service(s, gps_service) for s in to_probe),
                    return_exceptions=True,
                )
                for item in rp_results:
                    if isinstance(item, dict) and "udid" in item:
                        devices_by_udid[item["udid"]] = item
    except Exception as e:
        print(f"[Scan] Bonjour RemotePairing 掃描略過: {e}")

    for s in gps_service.get_connected_sessions():
        c_udid = s.current_udid or s.target_udid
        if c_udid and c_udid not in devices_by_udid:
            devices_by_udid[c_udid] = {
                "udid": c_udid,
                "name": f"{s.device_name} (連線中)",
                "connection": s.transport_used or "Wi-Fi / 已連線",
                "has_usb": False,
                "has_wifi": True,
                "dev_mode_enabled": True,
                "ddi_mounted": True,
                "ddi_status": "mounted",
                "battery_level": None,
                "is_charging": None,
                "temperature_c": None,
                "system_load_w": s.last_system_load_w,
                "instant_amperage_ma": s.last_instant_amperage_ma,
                "thermal_state": s.last_thermal_state,
                "thermal_label": s.last_thermal_label,
                "ddi_power_saving_active": s.ddi_power_saving_active,
            }

    return list(devices_by_udid.values())
