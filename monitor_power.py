import os
import sys
from pathlib import Path

# 若使用者以系統預設 python3 執行且缺少 pymobiledevice3，自動切換至專案內建的 EmbeddedPython
EMBEDDED_PYTHON = Path(__file__).resolve().parent / "EmbeddedPython" / "python" / "bin" / "python3"
try:
    import pymobiledevice3  # noqa: F401
except ImportError:
    if EMBEDDED_PYTHON.exists() and Path(sys.executable).resolve() != EMBEDDED_PYTHON.resolve():
        os.execv(str(EMBEDDED_PYTHON), [str(EMBEDDED_PYTHON), *sys.argv])
    else:
        print(f"❌ 找不到 pymobiledevice3，請使用：{EMBEDDED_PYTHON} {sys.argv[0]}")
        sys.exit(1)

import argparse
import asyncio
from collections import deque
from contextlib import suppress
import dataclasses
from datetime import datetime
import statistics
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.animation as animation

# 載入專案內的 ResilientRsdTunnel 與電池/功耗解析輔助函數
APP_MODULE_DIR = Path(__file__).resolve().parent / "fake-gps"
if str(APP_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(APP_MODULE_DIR))

from gps_tunnel import (  # noqa: E402
    THERMAL_STATE_LABELS,
    ResilientRsdTunnel,
    query_battery_from_provider,
)
from pymobiledevice3 import usbmux  # noqa: E402
from pymobiledevice3.lockdown import create_using_usbmux  # noqa: E402
from pymobiledevice3.services.dvt.instruments.condition_inducer import ConditionInducer  # noqa: E402
from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo  # noqa: E402
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider  # noqa: E402
from pymobiledevice3.services.dvt.instruments.energy_monitor import EnergyMonitor  # noqa: E402
from pymobiledevice3.services.dvt.instruments.graphics import Graphics  # noqa: E402
from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap  # noqa: E402
from pymobiledevice3.services.notification_proxy import RemoteNotificationProxyService  # noqa: E402


FPS_CAP_PROFILE_MAP = {
    "critical": "ThermalCritical",
    "serious": "ThermalSerious",
    "fair": "ThermalFair",
    "gpu-min": "GPUPerformanceStateMin",
}

# 全域執行緒安全繪圖佇列（保留最近 60 筆區間數據，5 秒一筆約為 5 分鐘時間長度）
MAX_PLOT_POINTS = 60
data_lock = threading.Lock()
plot_data = {
    "times": deque(maxlen=MAX_PLOT_POINTS),
    "load_w": deque(maxlen=MAX_PLOT_POINTS),
    "inst_ma": deque(maxlen=MAX_PLOT_POINTS),
    "fps": deque(maxlen=MAX_PLOT_POINTS),
    "gpu": deque(maxlen=MAX_PLOT_POINTS),
    "pik_cpu": deque(maxlen=MAX_PLOT_POINTS),
    "sys_cpu": deque(maxlen=MAX_PLOT_POINTS),
    "thermal": deque(maxlen=MAX_PLOT_POINTS),
}
latest_status_title = "正在連線取得裝置資訊..."
shutdown_flag = threading.Event()


def avg_or_none(values: List[float], digits: int = 1) -> Optional[float]:
    if not values:
        return None
    return round(statistics.mean(values), digits)


def fmt_val(val: Optional[float], unit: str = "", digits: int = 1, signed: bool = False) -> str:
    if val is None:
        return "--"
    if digits == 0:
        num_str = f"{int(round(val)):+d}" if signed else f"{int(round(val)):d}"
    else:
        fmt = f"{{:+0.{digits}f}}" if signed else f"{{:0.{digits}f}}"
        num_str = fmt.format(val)
    return f"{num_str}{unit}"


async def find_pikmin_process(dvt: DvtProvider) -> Tuple[Optional[int], bool, str]:
    try:
        async with DeviceInfo(dvt) as dev_info:
            procs = await asyncio.wait_for(dev_info.proclist(), timeout=2.5)
        for p in procs:
            name = str(p.get("name") or "")
            bundle = str(p.get("bundleIdentifier") or "")
            if "pikmin" in name.lower() or "com.nianticlabs.pikmin" in bundle.lower():
                pid = int(p["pid"])
                fg = bool(p.get("foregroundRunning", p.get("foreground", False)))
                return pid, fg, ("🟢前景" if fg else "🟡背景")
        return None, False, "🔴未開啟"
    except Exception:
        return None, False, "⚪未知"


async def run_monitor(args: argparse.Namespace) -> None:
    global latest_status_title
    print("================================================================================")
    print(" 📊 iPhone & 皮克敏 (Pikmin Bloom) 即時折線圖與功耗/效能監測工具")
    print("================================================================================")

    mux_devs = await usbmux.list_devices()
    if not mux_devs:
        print("❌ 偵測不到任何已連線的 iOS 裝置！請確認 iPhone 已連接 USB 或處於同一個 Wi-Fi。")
        shutdown_flag.set()
        return

    target_udid = args.udid or mux_devs[0].serial
    dev_name = target_udid
    ios_ver = ""
    with suppress(Exception):
        async with await asyncio.wait_for(create_using_usbmux(serial=target_udid, autopair=False), timeout=2.5) as ld:
            dev_name = ld.all_values.get("DeviceName") or ld.display_name or target_udid
            ios_ver = ld.product_version

    print(f"📱 目標裝置 : {dev_name} (iOS {ios_ver}) [{target_udid}]")
    print(f"⏱️ 統計週期 : 每 {args.interval:.1f} 秒更新一次 (關閉圖表視窗或按 Ctrl+C 可結束)")

    fps_cap_mode = (args.fps_cap or "off").lower()
    chosen_ci_profile = FPS_CAP_PROFILE_MAP.get(fps_cap_mode)

    async with ResilientRsdTunnel(serial=target_udid, autopair=True, prefer_wifi=True) as rsd:
        async with DvtProvider(rsd) as dvt:
            lpm_svc: Optional[RemoteNotificationProxyService] = None

            async with ConditionInducer(dvt) as ci:
                with suppress(Exception):
                    await asyncio.wait_for(ci.clear(), timeout=2.5)
                    await asyncio.sleep(0.4)

                if chosen_ci_profile:
                    try:
                        await asyncio.wait_for(ci.set(chosen_ci_profile), timeout=3.5)
                        await asyncio.sleep(0.3)
                        print(f"✅ 已啟用 DDI 降幀/降載配置：{chosen_ci_profile}")
                    except Exception as e:
                        print(f"⚠️ 無法啟用 {chosen_ci_profile}: {e}")

            if args.low_power:
                try:
                    lpm_svc = RemoteNotificationProxyService(rsd)
                    await asyncio.wait_for(lpm_svc.connect(), timeout=2.5)
                    with suppress(Exception):
                        await lpm_svc.notify_set_state("com.apple.system.lowpowermode", 1)
                    await lpm_svc.notify_post("com.apple.system.lowpowermode")
                    print("✅ 已向系統發送低電量模式通知 (com.apple.system.lowpowermode = 1)")
                except Exception as e:
                    print(f"⚠️ 發送低電量模式通知失敗: {e}")

            pikmin_pid, is_fg, fg_label = await find_pikmin_process(dvt)
            print(f"🌱 皮克敏狀態: {fg_label} (PID={pikmin_pid or '未執行'})")
            print("================================================================================\n")

            bucket: Dict[str, List[float]] = {
                "sys_load_w": [],
                "sys_load_mw": [],
                "batt_power_mw": [],
                "instant_amp_ma": [],
                "avg_amp_ma": [],
                "voltage_mv": [],
                "battery_pct": [],
                "fps": [],
                "gpu_device": [],
                "gpu_renderer": [],
                "gpu_tiler": [],
                "pikmin_cpu": [],
                "sys_cpu": [],
                "power_score": [],
                "mem_mb": [],
                "energy_total": [],
                "energy_cpu": [],
                "energy_gpu": [],
                "energy_display": [],
                "thermal_state": [],
                "induced_thermal": [],
            }
            state_meta: Dict[str, Any] = {
                "is_charging": False,
                "temperature_c": None,
                "pikmin_pid": pikmin_pid,
                "fg_label": fg_label,
            }
            stop_event = asyncio.Event()

            async def task_graphics():
                while not stop_event.is_set():
                    try:
                        async with Graphics(dvt) as gfx:
                            idx = 0
                            async for s in gfx:
                                if stop_event.is_set():
                                    break
                                idx += 1
                                if idx == 1 and s.get("CoreAnimationFramesPerSecond") == 0:
                                    continue
                                if s.get("CoreAnimationFramesPerSecond") is not None:
                                    bucket["fps"].append(float(s["CoreAnimationFramesPerSecond"]))
                                if s.get("Device Utilization %") is not None:
                                    bucket["gpu_device"].append(float(s["Device Utilization %"]))
                                if s.get("Renderer Utilization %") is not None:
                                    bucket["gpu_renderer"].append(float(s["Renderer Utilization %"]))
                                if s.get("Tiler Utilization %") is not None:
                                    bucket["gpu_tiler"].append(float(s["Tiler Utilization %"]))
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        await asyncio.sleep(1.0)

            async def task_sysmon():
                while not stop_event.is_set():
                    try:
                        async with await Sysmontap.create(dvt, interval=1000) as sysmon:
                            async for row in sysmon:
                                if stop_event.is_set():
                                    break
                                if "SystemCPUUsage" in row and isinstance(row["SystemCPUUsage"], dict):
                                    sc = row["SystemCPUUsage"].get("CPU_TotalLoad")
                                    if isinstance(sc, (int, float)):
                                        bucket["sys_cpu"].append(float(sc))
                                if "Processes" in row:
                                    pmap = row["Processes"]
                                    cur_pid = state_meta.get("pikmin_pid")
                                    p_entry = None
                                    if cur_pid:
                                        p_entry = pmap.get(cur_pid) or pmap.get(str(cur_pid))
                                    if p_entry is None:
                                        for _, p_arr in pmap.items():
                                            d_cand = dataclasses.asdict(sysmon.process_attributes_cls(*p_arr))
                                            if "pikmin" in str(d_cand.get("name") or "").lower():
                                                p_entry = p_arr
                                                state_meta["pikmin_pid"] = d_cand.get("pid")
                                                break
                                    if p_entry is not None:
                                        d = dataclasses.asdict(sysmon.process_attributes_cls(*p_entry))
                                        if isinstance(d.get("cpuUsage"), (int, float)):
                                            bucket["pikmin_cpu"].append(float(d["cpuUsage"]))
                                        if isinstance(d.get("powerScore"), (int, float)):
                                            bucket["power_score"].append(float(d["powerScore"]))
                                        if isinstance(d.get("physFootprint"), (int, float)):
                                            bucket["mem_mb"].append(float(d["physFootprint"]) / (1024 * 1024))
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        await asyncio.sleep(1.0)

            async def task_energy():
                while not stop_event.is_set():
                    try:
                        target_pid = int(state_meta.get("pikmin_pid") or 1)
                        async with EnergyMonitor(dvt, [target_pid]) as em:
                            while not stop_event.is_set():
                                sample = await asyncio.wait_for(em._sample_once(), timeout=2.0)
                                if isinstance(sample, dict):
                                    p_data = sample.get(str(target_pid)) or sample.get(target_pid)
                                    if isinstance(p_data, dict):
                                        ts = p_data.get("energy.thermalstate.cost")
                                        ind = p_data.get("energy.inducedthermalstate.cost")
                                        if isinstance(ts, (int, float)):
                                            bucket["thermal_state"].append(float(ts))
                                        if isinstance(ind, (int, float)):
                                            bucket["induced_thermal"].append(float(ind))
                                        ec = p_data.get("energy.cost")
                                        ecpu = p_data.get("energy.cpu.cost")
                                        egpu = p_data.get("energy.gpu.cost")
                                        edisp = p_data.get("energy.display.cost")
                                        if isinstance(ec, (int, float)) and ec > 0:
                                            bucket["energy_total"].append(float(ec))
                                        if isinstance(ecpu, (int, float)) and ecpu > 0:
                                            bucket["energy_cpu"].append(float(ecpu))
                                        if isinstance(egpu, (int, float)) and egpu > 0:
                                            bucket["energy_gpu"].append(float(egpu))
                                        if isinstance(edisp, (int, float)) and edisp > 0:
                                            bucket["energy_display"].append(float(edisp))
                                await asyncio.sleep(1.0)
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        await asyncio.sleep(1.0)

            async def task_battery():
                while not stop_event.is_set():
                    try:
                        b_info = await query_battery_from_provider(rsd, include_raw=False)
                        if b_info.get("battery_level") is None:
                            async with await asyncio.wait_for(
                                create_using_usbmux(serial=target_udid, autopair=False),
                                timeout=2.0,
                            ) as ld:
                                b_info = await query_battery_from_provider(ld, include_raw=False)

                        if b_info.get("battery_level") is not None:
                            bucket["battery_pct"].append(float(b_info["battery_level"]))
                        if b_info.get("is_charging") is not None:
                            state_meta["is_charging"] = bool(b_info["is_charging"])
                        if b_info.get("temperature_c") is not None:
                            state_meta["temperature_c"] = b_info["temperature_c"]
                        if b_info.get("system_load_w") is not None:
                            bucket["sys_load_w"].append(float(b_info["system_load_w"]))
                        if b_info.get("system_load_mw") is not None:
                            bucket["sys_load_mw"].append(float(b_info["system_load_mw"]))
                        if b_info.get("battery_power_mw") is not None:
                            bucket["batt_power_mw"].append(float(b_info["battery_power_mw"]))
                        if b_info.get("instant_amperage_ma") is not None:
                            bucket["instant_amp_ma"].append(float(b_info["instant_amperage_ma"]))
                        if b_info.get("amperage_ma") is not None:
                            bucket["avg_amp_ma"].append(float(b_info["amperage_ma"]))
                        if b_info.get("voltage_mv") is not None:
                            bucket["voltage_mv"].append(float(b_info["voltage_mv"]))
                    except asyncio.CancelledError:
                        break
                    except Exception:
                        pass
                    await asyncio.sleep(1.2)

            workers = [
                asyncio.create_task(task_graphics()),
                asyncio.create_task(task_sysmon()),
                asyncio.create_task(task_energy()),
                asyncio.create_task(task_battery()),
            ]

            window_idx = 0
            try:
                while not shutdown_flag.is_set():
                    t_start = datetime.now()
                    await asyncio.sleep(args.interval)
                    t_end = datetime.now()
                    window_idx += 1

                    p_pid, _, fg_lbl = await find_pikmin_process(dvt)
                    if p_pid:
                        state_meta["pikmin_pid"] = p_pid
                    state_meta["fg_label"] = fg_lbl

                    snap = {k: list(v) for k, v in bucket.items()}
                    for v in bucket.values():
                        v.clear()

                    avg_load_w = avg_or_none(snap["sys_load_w"], 2)
                    avg_load_mw = avg_or_none(snap["sys_load_mw"], 0)
                    avg_batt_mw = avg_or_none(snap["batt_power_mw"], 0)
                    avg_inst_ma = avg_or_none(snap["instant_amp_ma"], 0)
                    avg_volt_mv = avg_or_none(snap["voltage_mv"], 0)
                    avg_bat_pct = avg_or_none(snap["battery_pct"], 0)

                    avg_fps = avg_or_none(snap["fps"], 1)
                    avg_gpu = avg_or_none(snap["gpu_device"], 1)
                    avg_gpu_rend = avg_or_none(snap["gpu_renderer"], 1)
                    avg_gpu_tiler = avg_or_none(snap["gpu_tiler"], 1)

                    avg_pik_cpu = avg_or_none(snap["pikmin_cpu"], 1)
                    avg_sys_cpu = avg_or_none(snap["sys_cpu"], 1)
                    avg_pscore = avg_or_none(snap["power_score"], 1)
                    avg_mem_mb = avg_or_none(snap["mem_mb"], 1)

                    avg_e_tot = avg_or_none(snap["energy_total"], 1)
                    avg_e_cpu = avg_or_none(snap["energy_cpu"], 1)
                    avg_e_gpu = avg_or_none(snap["energy_gpu"], 1)

                    ts_val = int(round(snap["thermal_state"][-1])) if snap["thermal_state"] else 0
                    ind_val = int(round(snap["induced_thermal"][-1])) if snap["induced_thermal"] else -1
                    ts_name = THERMAL_STATE_LABELS.get(ts_val, f"等級{ts_val}")
                    ts_display = f"{ts_name} (Lv.{ts_val})"
                    if state_meta.get("temperature_c") is not None:
                        ts_display += f" / {state_meta['temperature_c']}°C"

                    chg_str = "⚡充電中" if state_meta["is_charging"] else "🔋放電中"

                    # 終端機持續輸出文字 log
                    print(f"[{window_idx:02d}] 🕒 {t_end.strftime('%H:%M:%S')} ({args.interval:.0f}s) | 皮克敏: {state_meta['fg_label']} | 功耗: {fmt_val(avg_load_w, 'W', 2)} | FPS: {fmt_val(avg_fps, '', 1)} | GPU: {fmt_val(avg_gpu, '%', 1)} | CPU: {fmt_val(avg_pik_cpu, '%', 1)}")

                    # 將數據同步至即時圖表佇列
                    with data_lock:
                        plot_data["times"].append(t_end)
                        plot_data["load_w"].append(avg_load_w if avg_load_w is not None else float("nan"))
                        plot_data["inst_ma"].append(avg_inst_ma if avg_inst_ma is not None else float("nan"))
                        plot_data["fps"].append(avg_fps if avg_fps is not None else float("nan"))
                        plot_data["gpu"].append(avg_gpu if avg_gpu is not None else float("nan"))
                        plot_data["pik_cpu"].append(avg_pik_cpu if avg_pik_cpu is not None else float("nan"))
                        plot_data["sys_cpu"].append(avg_sys_cpu if avg_sys_cpu is not None else float("nan"))
                        plot_data["thermal"].append(ts_val)

                        latest_status_title = (
                            f"{dev_name} | 皮克敏: {state_meta['fg_label']} | 電量: {fmt_val(avg_bat_pct, '%', 0)} ({chg_str}) | "
                            f"功耗: {fmt_val(avg_load_w, 'W', 2)} | FPS: {fmt_val(avg_fps, '', 1)} | 熱態: {ts_display}"
                        )
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\n🛑 收到中斷指令，正在清理並恢復手機正常狀態...")
            finally:
                stop_event.set()
                for w in workers:
                    w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

                if chosen_ci_profile:
                    with suppress(Exception):
                        async with ConditionInducer(dvt) as ci:
                            await asyncio.wait_for(ci.clear(), timeout=2.5)
                            await asyncio.sleep(0.4)
                        print("✅ 已自動解除 DDI 降幀模式，恢復手機正常 60 FPS！")

                if lpm_svc is not None:
                    with suppress(Exception):
                        await lpm_svc.notify_set_state("com.apple.system.lowpowermode", 0)
                        await lpm_svc.notify_post("com.apple.system.lowpowermode")
                        await lpm_svc.close()
                    print("✅ 已自動關閉低電量模式通知狀態！")


def launch_plot_gui(interval_sec: float) -> None:
    """在主執行緒建立 Matplotlib 視窗，X 軸固定顯示最近 5 分鐘"""
    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang HK", "Heiti TC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ((ax_pwr, ax_fps), (ax_cpu, ax_therm)) = plt.subplots(2, 2, figsize=(13, 8))
    fig.canvas.manager.set_window_title("iPhone 功耗與效能即時監控儀表板 (最近 5 分鐘)")

    # 建立 4 個子圖的折線物件
    (line_load_w,) = ax_pwr.plot([], [], color="#d62728", lw=1.8, label="系統總功耗 (W)")
    ax_amp = ax_pwr.twinx()
    (line_inst_ma,) = ax_amp.plot([], [], color="#ff7f0e", lw=1.2, ls="--", label="即時電流 (mA)")

    (line_fps,) = ax_fps.plot([], [], color="#2ca02c", lw=2.0, label="螢幕 FPS")
    ax_fps.axhline(60, color="#888888", ls=":", alpha=0.6, label="60 FPS 基準")
    ax_fps.axhline(30, color="#d62728", ls=":", alpha=0.6, label="30 FPS 鎖幀")

    (line_pik_cpu,) = ax_cpu.plot([], [], color="#1f77b4", lw=1.8, label="皮克敏 CPU (%)")
    (line_sys_cpu,) = ax_cpu.plot([], [], color="#aec7e8", lw=1.2, ls=":", label="全機 CPU (%)")
    (line_gpu,) = ax_cpu.plot([], [], color="#9467bd", lw=1.8, label="GPU 使用率 (%)")

    (line_therm,) = ax_therm.plot([], [], color="#8c564b", lw=2.0, marker="o", markersize=4, label="熱態等級 (Lv.0~3)")
    ax_therm.set_yticks([0, 1, 2, 3])
    ax_therm.set_yticklabels(["0:Nominal", "1:Fair", "2:Serious", "3:Critical"])

    # 基礎樣式配置
    for ax in (ax_pwr, ax_fps, ax_cpu, ax_therm):
        ax.grid(True, linestyle="--", alpha=0.5)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

    ax_pwr.set_title("⚡ 系統功耗 (W) 與電池電流 (mA)")
    ax_pwr.set_ylabel("功耗 (W)", color="#d62728")
    ax_amp.set_ylabel("電流 (mA)", color="#ff7f0e")

    ax_fps.set_title("🎮 畫面 FPS (畫面流暢度)")
    ax_fps.set_ylabel("FPS")
    ax_fps.set_ylim(-2, 70)
    ax_fps.legend(loc="lower left", fontsize=8)

    ax_cpu.set_title("💻 CPU / GPU 負載率 (%)")
    ax_cpu.set_ylabel("使用率 (%)")
    ax_cpu.set_ylim(-5, 120)
    ax_cpu.legend(loc="upper left", fontsize=8)

    ax_therm.set_title("🌡️ 熱態等級 (Thermal State)")
    ax_therm.set_ylabel("熱狀態等級")
    ax_therm.set_ylim(-0.5, 3.5)

    fig.tight_layout(rect=[0, 0.03, 1, 0.94])

    def update_frame(_):
        with data_lock:
            if not plot_data["times"]:
                return line_load_w, line_fps, line_gpu, line_therm

            xs = list(plot_data["times"])
            load_w_vals = list(plot_data["load_w"])
            inst_ma_vals = list(plot_data["inst_ma"])
            fps_vals = list(plot_data["fps"])
            gpu_vals = list(plot_data["gpu"])
            pik_cpu_vals = list(plot_data["pik_cpu"])
            sys_cpu_vals = list(plot_data["sys_cpu"])
            therm_vals = list(plot_data["thermal"])
            cur_title = latest_status_title

        fig.suptitle(cur_title, fontsize=12, fontweight="bold")

        # 1. 計算固定的 5 分鐘時間窗口
        now = xs[-1]
        t_min = now - timedelta(minutes=5)
        t_max = now

        # 2. 更新功耗與電流
        line_load_w.set_data(xs, load_w_vals)
        line_inst_ma.set_data(xs, inst_ma_vals)
        ax_pwr.set_xlim(t_min, t_max)
        
        valid_w = [v for v in load_w_vals if v == v]
        if valid_w:
            ax_pwr.set_ylim(max(0.0, min(valid_w) - 0.5), max(valid_w) + 1.0)
        valid_ma = [v for v in inst_ma_vals if v == v]
        if valid_ma:
            ax_amp.set_ylim(min(valid_ma) - 100, max(valid_ma) + 100)

        # 3. 更新 FPS
        line_fps.set_data(xs, fps_vals)
        ax_fps.set_xlim(t_min, t_max)

        # 4. 更新 CPU / GPU
        line_pik_cpu.set_data(xs, pik_cpu_vals)
        line_sys_cpu.set_data(xs, sys_cpu_vals)
        line_gpu.set_data(xs, gpu_vals)
        ax_cpu.set_xlim(t_min, t_max)

        # 5. 更新熱態
        line_therm.set_data(xs, therm_vals)
        ax_therm.set_xlim(t_min, t_max)

        fig.autofmt_xdate()
        return line_load_w, line_inst_ma, line_fps, line_gpu, line_therm

    # 每秒刷新一次視窗
    _ = animation.FuncAnimation(fig, update_frame, interval=1000, cache_frame_data=False)

    def on_close(_):
        print("\n🛑 圖表視窗已關閉，正在終止監測...")
        shutdown_flag.set()

    fig.canvas.mpl_connect("close_event", on_close)

    try:
        plt.show()
    except KeyboardInterrupt:
        shutdown_flag.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="iPhone & 皮克敏即時折線圖視窗與熱態/功耗/FPS 監測工具"
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=5.0,
        help="幾秒計算並顯示一次平均值 (預設: 5.0 秒)",
    )
    parser.add_argument(
        "--fps-cap", "--drop-fps", "-f",
        dest="fps_cap",
        nargs="?",
        const="critical",
        default="off",
        choices=["off", "critical", "serious", "fair", "gpu-min"],
        help="開啟 DDI 降幀/降載測試功能：不帶值預設為 critical (30FPS 降幀)，亦可指定 serious / fair / gpu-min",
    )
    parser.add_argument(
        "--low-power", "-l",
        action="store_true",
        help="同時發送 iOS 低電量模式通知狀態 (com.apple.system.lowpowermode = 1)",
    )
    parser.add_argument(
        "--udid", "-u",
        type=str,
        default=None,
        help="指定要監測的 iPhone UDID (預設自動選擇第一台裝置)",
    )
    return parser.parse_args()


def main():
    cli_args = parse_args()

    # 於背景執行緒啟動 asyncio 監控任務
    loop = asyncio.new_event_loop()

    def run_async_loop():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_monitor(cli_args))
        finally:
            loop.close()

    bg_thread = threading.Thread(target=run_async_loop, daemon=True)
    bg_thread.start()

    try:
        # 主執行緒運行 GUI 視窗
        launch_plot_gui(cli_args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_flag.set()
        bg_thread.join(timeout=3.0)
        sys.exit(0)


if __name__ == "__main__":
    main()