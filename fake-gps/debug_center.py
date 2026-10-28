"""
debug_center.py
Debug 錯誤診斷紀錄中心與 macOS 原生通知模組
"""

from datetime import datetime
import json
import threading
from typing import Any, Dict, List, Optional
import urllib.request

_debug_lock = threading.Lock()
_debug_seq = 0
debug_logs: List[Dict[str, Any]] = []
MAX_DEBUG_LOGS = 100
NOTIFY_PORT: Optional[int] = None


def configure_notify_port(port: Optional[int]) -> None:
    global NOTIFY_PORT
    NOTIFY_PORT = port


def _infer_error_solution(error_text: str, stage: str = "") -> str:
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
            "2. 無線連接：新手機必須先插線成功連線一次（系統會自動為手機開啟 Wi-Fi 探索並建立 RemotePairing 無線憑證），之後才能在相同 Wi-Fi 網段下無線連線。\n"
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


def get_debug_summary() -> tuple[int, int]:
    with _debug_lock:
        err_count = len(debug_logs)
        latest_err_id = debug_logs[0]["id"] if debug_logs else 0
        return err_count, latest_err_id


def get_debug_logs_snapshot() -> Dict[str, Any]:
    with _debug_lock:
        return {
            "logs": list(debug_logs),
            "total_count": len(debug_logs),
            "latest_id": debug_logs[0]["id"] if debug_logs else 0,
        }


def clear_all_debug_logs() -> None:
    with _debug_lock:
        debug_logs.clear()


def send_macos_notification(title: str, subtitle: str, message: str, sound_name: str = "Glass") -> None:
    if not NOTIFY_PORT:
        return

    def _post():
        try:
            url = f"http://127.0.0.1:{NOTIFY_PORT}/notify"
            payload = json.dumps({
                "title": title,
                "subtitle": subtitle,
                "message": message,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1.0) as _:
                pass
        except Exception as e:
            print(f"[Notify] 發送原生通知失敗: {e}")

    threading.Thread(target=_post, daemon=True).start()
