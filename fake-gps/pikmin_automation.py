"""
pikmin_automation.py
皮克敏 (Pikmin Bloom) 自動化守護與視覺分析模組

包含：
1. 電池電量與實體溫度監控 (低電量自動停止種花並返航、過熱通知與保護)
2. 遊戲 Process 存活與「前景 (Foreground)」狀態偵測 (閃退自動暫停巡航、自動重啟 App)
3. 嚴格的前景保護：僅於確認皮克敏處於手機前景時，才執行每分鐘截圖分析與自動點擊
4. 基於使用者 3 張參考圖 (`主頁.PNG`, `地圖（顯示花朵）.PNG`, `選花畫面（結束種花）.PNG`) 的視覺狀態機：
   - 若停在子畫面 -> 自動點左下角返回 `<` 直到回到 `主頁` -> 再點正下方中央羅盤 `🧭` 回到 `地圖（顯示花朵）`
   - 結合原生 Vision OCR 偵測頂部橫幅「種花已達金幣上限 / 60枚金幣」或「種植花朵總數 >= 15,000」
   - 達標時自動點擊右側花瓣鈕 `🌼` -> 點擊橘色停止種花鈕 `⏹` -> 啟動平滑返航回原點
"""

import base64
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.request

from PIL import Image, ImageChops, ImageStat

from debug_center import add_debug_log, send_macos_notification

# 皮克敏 Bundle ID 與常見執行檔名稱
PIKMIN_BUNDLE_ID = "com.nianticlabs.pikmin"
PIKMIN_PROCESS_NAMES = ("PikminBloom", "pikmin", "com.nianticlabs.pikmin")

# 依據 3 張參考圖 (1179 x 2556) 實測的正規化點擊座標 (0.0 ~ 1.0) 與 HID 座標 (0 ~ 65535)
TAP_COORDS = {
    # 左下角返回 `<` 按鈕 (地圖、選花畫面、內層子畫面通用)
    "bottom_left_back": (0.096, 0.916),
    # 主頁正下方中央羅盤 `🧭` 按鈕 (從主頁進入種花地圖)
    "home_bottom_center_map": (0.500, 0.916),
    # 地圖/主頁右側花瓣罐 `🌼` 按鈕 (打開選花/結束種花抽屜)
    "right_flower_drawer": (0.862, 0.723),
    # 選花畫面中央偏左的橘色停止種花 `⏹` 按鈕
    "orange_stop_planting": (0.401, 0.575),
}

DEFAULT_AUTOMATION_CONFIG: Dict[str, Any] = {
    "low_battery_stop": False,
    "low_battery_threshold": 20,
    "overheat_notify": True,
    "overheat_threshold_c": 40.0,
    "overheat_auto_pause": False,
    "crash_auto_pause": True,
    "crash_auto_restart": False,
    "screen_analysis_enabled": False,
    "auto_navigate_to_map": True,
    "coin_limit_auto_stop": False,
    "coin_limit_keywords": "上限,已達,已滿,今日上限,獲得上限",
    "flower_limit_target": 15000,
}


class ReferenceTemplateMatcher:
    """
    載入根目錄的 3 張參考截圖 (`主頁.PNG`, `地圖（顯示花朵）.PNG`, `選花畫面（結束種花）.PNG`)
    或內嵌的 `ref_templates.json`，對擷取到的手機畫面進行快速區塊特徵比對，判斷當前處於哪個遊戲畫面。
    """

    def __init__(self, base_dirs: List[Path]):
        self.templates: Dict[str, Dict[str, Image.Image]] = {}
        self.loaded_files: List[str] = []
        self._load_reference_images(base_dirs)

    # 定義用於比對的關鍵局部區塊 (nx1, ny1, nx2, ny2)，縮小為 48x48 進行平均絕對誤差 (MAE) 比對
    ROIS = {
        "bottom_left": (0.04, 0.885, 0.155, 0.948),      # 左下角按鈕 (主頁選單 ≡ / 地圖綠返回 < / 選花白返回 <)
        "bottom_center": (0.43, 0.885, 0.57, 0.948),     # 下方中央按鈕 (主頁綠羅盤 🧭 / 地圖雙切膠囊)
        "right_flower": (0.795, 0.685, 0.925, 0.760),    # 右側花瓣按鈕
        "orange_stop": (0.335, 0.535, 0.465, 0.615),     # 選花畫面橘色停止種花按鈕
        "pink_drawer": (0.05, 0.475, 0.95, 0.640),       # 選花畫面粉紅抽屜區
        "top_pills": (0.06, 0.065, 0.94, 0.108),         # 主頁頂部三顆白色膠囊按鈕 (飾品一覽/好友/通知)
    }

    def _crop_thumb(self, img: Image.Image, box_norm: Tuple[float, float, float, float]) -> Image.Image:
        w, h = img.size
        nx1, ny1, nx2, ny2 = box_norm
        crop_box = (int(nx1 * w), int(ny1 * h), int(nx2 * w), int(ny2 * h))
        return img.crop(crop_box).convert("RGB").resize((48, 48), Image.Resampling.BILINEAR)

    def _load_reference_images(self, base_dirs: List[Path]) -> None:
        ref_files = {
            "home": "主頁.PNG",
            "flower_map": "地圖（顯示花朵）.PNG",
            "stop_drawer": "選花畫面（結束種花）.PNG",
        }
        for key, filename in ref_files.items():
            for d in base_dirs:
                candidate = d / filename
                if candidate.exists():
                    try:
                        with Image.open(candidate) as im:
                            rgb = im.convert("RGB")
                            self.templates[key] = {
                                roi_name: self._crop_thumb(rgb, roi_box)
                                for roi_name, roi_box in self.ROIS.items()
                            }
                        self.loaded_files.append(filename)
                        break
                    except Exception as e:
                        print(f"[Automation] 載入參考圖 {candidate} 失敗: {e}")

        # 若部分參考圖未在根目錄找到 (例如打包進 .app 後)，則從內嵌的 ref_templates.json 載入
        if len(self.templates) < 3:
            for d in base_dirs:
                tpl_json = d / "ref_templates.json"
                if tpl_json.exists():
                    try:
                        raw = json.loads(tpl_json.read_text(encoding="utf-8"))
                        for key, roi_map in raw.items():
                            if key not in self.templates and isinstance(roi_map, dict):
                                self.templates[key] = {}
                                for rname, b64_str in roi_map.items():
                                    png_data = base64.b64decode(b64_str)
                                    self.templates[key][rname] = Image.open(io.BytesIO(png_data)).convert("RGB")
                                label_name = ref_files.get(key, key)
                                if label_name not in self.loaded_files:
                                    self.loaded_files.append(label_name)
                        break
                    except Exception as e:
                        print(f"[Automation] 載入 ref_templates.json 失敗: {e}")

    @staticmethod
    def _mae(im1: Image.Image, im2: Image.Image) -> float:
        diff = ImageChops.difference(im1, im2)
        stat = ImageStat.Stat(diff)
        return sum(stat.mean) / 3.0

    def classify_screen(self, img: Image.Image, ocr_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        綜合視覺特徵與 OCR 結果判定當前手機畫面狀態：
        回傳 state:
          - 'stop_drawer': 選花畫面（結束種花）
          - 'flower_map': 地圖（顯示花朵）
          - 'home': 皮克敏主頁
          - 'pikmin_subpage': 皮克敏其他內層子畫面（左下角有返回鍵）
          - 'lock_screen': 螢幕鎖定 / 全黑畫面
          - 'other_app': 非皮克敏畫面 (使用者在滑其他 App 或桌面)
        """
        rgb = img.convert("RGB")

        # 1. 先檢查是否為黑屏 / 鎖定螢幕
        full_stat = ImageStat.Stat(rgb.resize((64, 64)))
        if sum(full_stat.mean) / 3.0 < 12.0 and sum(full_stat.stddev) / 3.0 < 10.0:
            return {
                "state": "lock_screen",
                "label": "🔒 螢幕鎖定 / 黑屏",
                "is_pikmin_screen": False,
                "confidence": 0.99,
            }

        crops = {roi_name: self._crop_thumb(rgb, roi_box) for roi_name, roi_box in self.ROIS.items()}

        # 計算橘色停止按鈕與粉紅抽屜的顏色特徵 (即使不同手機解析度也高度穩定)
        stop_stat = ImageStat.Stat(crops["orange_stop"])
        drawer_stat = ImageStat.Stat(crops["pink_drawer"])
        is_orange_stop_color = (
            stop_stat.mean[0] > 215
            and 120 < stop_stat.mean[1] < 205
            and stop_stat.mean[2] < 155
            and drawer_stat.mean[0] > 210
            and drawer_stat.mean[0] - drawer_stat.mean[1] > 65
        )

        # 與 3 張參考圖計算關鍵按鈕區域的相似度 (MAE 越低越相似，0~255)
        scores: Dict[str, float] = {}
        if "stop_drawer" in self.templates:
            t = self.templates["stop_drawer"]
            scores["stop_drawer"] = (
                self._mae(crops["orange_stop"], t["orange_stop"]) * 0.45
                + self._mae(crops["pink_drawer"], t["pink_drawer"]) * 0.30
                + self._mae(crops["bottom_left"], t["bottom_left"]) * 0.25
            )
        if "flower_map" in self.templates:
            t = self.templates["flower_map"]
            scores["flower_map"] = (
                self._mae(crops["bottom_left"], t["bottom_left"]) * 0.35
                + self._mae(crops["bottom_center"], t["bottom_center"]) * 0.40
                + self._mae(crops["right_flower"], t["right_flower"]) * 0.25
            )
        if "home" in self.templates:
            t = self.templates["home"]
            scores["home"] = (
                self._mae(crops["top_pills"], t["top_pills"]) * 0.35
                + self._mae(crops["bottom_center"], t["bottom_center"]) * 0.35
                + self._mae(crops["bottom_left"], t["bottom_left"]) * 0.30
            )

        full_text = (ocr_data or {}).get("full_text", "")
        banner_text = (ocr_data or {}).get("banner_full_text", "")

        # 2. 判斷是否為「選花畫面（結束種花）」
        if is_orange_stop_color or scores.get("stop_drawer", 999.0) < 28.0 or (
            ("大家一起" in full_text or "加速" in full_text) and ("種植" in full_text or "花朵" in full_text)
        ):
            return {
                "state": "stop_drawer",
                "label": "🌺 選花畫面 (結束種花面板)",
                "is_pikmin_screen": True,
                "confidence": 0.95,
                "scores": {k: round(v, 1) for k, v in scores.items()},
            }

        # 3. 判斷是否為「地圖（顯示花朵）」
        has_flower_banner_text = any(
            kw in (banner_text or full_text)
            for kw in ("種植的花朵", "花朵總數", "目前的獲得數量", "下一枚金幣", "枚金幣")
        )
        if has_flower_banner_text or scores.get("flower_map", 999.0) < 30.0:
            return {
                "state": "flower_map",
                "label": "🗺️ 種花地圖 (顯示花朵與金幣)",
                "is_pikmin_screen": True,
                "confidence": 0.94,
                "scores": {k: round(v, 1) for k, v in scores.items()},
            }

        # 4. 判斷是否為「皮克敏主頁」
        has_home_text = sum(
            1 for kw in ("飾品一覽", "好友", "通知", "步數", "探險", "商店", "蘑菇") if kw in full_text
        ) >= 2
        if has_home_text or scores.get("home", 999.0) < 30.0:
            return {
                "state": "home",
                "label": "🏠 皮克敏主頁 (可點下方中央回種花地圖)",
                "is_pikmin_screen": True,
                "confidence": 0.92,
                "scores": {k: round(v, 1) for k, v in scores.items()},
            }

        # 5. 檢查左下角是否有皮克敏特徵返回按鈕 `<` (綠色圓紐或白色圓紐)，或畫面文字含皮克敏特徵字
        bl_mae_green = (
            self._mae(crops["bottom_left"], self.templates["flower_map"]["bottom_left"])
            if "flower_map" in self.templates
            else 999.0
        )
        bl_mae_white = (
            self._mae(crops["bottom_left"], self.templates["stop_drawer"]["bottom_left"])
            if "stop_drawer" in self.templates
            else 999.0
        )
        has_pikmin_sub_text = any(
            kw in full_text for kw in ("皮克敏", "花瓣", "精華", "探險", "明信片", "隊伍", "花苗", "飾品")
        )
        if bl_mae_green < 32.0 or bl_mae_white < 26.0 or has_pikmin_sub_text:
            return {
                "state": "pikmin_subpage",
                "label": "📄 皮克敏內層子畫面 (可點左下返回主頁)",
                "is_pikmin_screen": True,
                "confidence": 0.85,
                "scores": {k: round(v, 1) for k, v in scores.items()},
            }

        return {
            "state": "other_app",
            "label": "📱 非皮克敏前景畫面 (不執行自動點擊)",
            "is_pikmin_screen": False,
            "confidence": 0.80,
            "scores": {k: round(v, 1) for k, v in scores.items()},
        }


def parse_flower_and_coin_banner(
    ocr_data: Dict[str, Any],
    custom_keywords_csv: str,
    flower_limit_target: int,
    prev_missing_next_coin_streak: int = 0,
) -> Dict[str, Any]:
    """
    從 Vision OCR 結果解析頂部種花橫幅：
    1. 種植的花朵總數 (左側數字)
    2. 目前的獲得數量 (右側金幣數字)
    3. 距離下一枚金幣 (右側下方數字，若已滿上限則會消失或顯示上限文字)
    4. 是否已達每日金幣上限 或 單趟花數目標
    """
    items = ocr_data.get("items") or []
    banner_texts = ocr_data.get("banner_texts") or []
    banner_str = " ".join(banner_texts) if banner_texts else ocr_data.get("full_text", "")

    planted_flowers: Optional[int] = None
    earned_coins: Optional[int] = None
    next_coin_flowers: Optional[int] = None

    # 依據座標區分左側 (x < 0.46: 種植的花朵總數) 與右側 (x >= 0.46: 金幣獲得數量與下一枚金幣)
    left_numbers: List[Tuple[float, int]] = []
    right_numbers: List[Tuple[float, int]] = []

    for it in items:
        cy = float(it.get("y", 0.0))
        cx = float(it.get("x", 0.0))
        txt = str(it.get("text", "")).strip()
        if not (0.11 <= cy <= 0.25):
            continue
        # 擷取純數字 (允許逗號如 15,000)
        for m in re.finditer(r"\b\d[\d,]*\b", txt):
            raw_num = m.group(0).replace(",", "")
            if raw_num.isdigit():
                val = int(raw_num)
                if cx < 0.46:
                    left_numbers.append((cy, val))
                else:
                    right_numbers.append((cy, val))

    if left_numbers:
        planted_flowers = max(v for _, v in left_numbers)

    if right_numbers:
        right_sorted = sorted(right_numbers, key=lambda pair: pair[0])
        if len(right_sorted) >= 2:
            earned_coins = right_sorted[0][1]
            next_coin_flowers = right_sorted[-1][1]
        elif len(right_sorted) == 1:
            val = right_sorted[0][1]
            if "下一枚" in banner_str and val > 60:
                next_coin_flowers = val
            else:
                earned_coins = val

    keywords = [k.strip() for k in (custom_keywords_csv or "").split(",") if k.strip()]
    if not keywords:
        keywords = ["上限", "已達", "已滿", "今日上限", "獲得上限"]

    matched_keyword = next((kw for kw in keywords if kw in banner_str), None)

    has_banner_anchor = ("種植" in banner_str or "花朵總數" in banner_str or "目前的獲得" in banner_str)
    has_next_coin_label = ("下一枚" in banner_str or "距離下一" in banner_str)

    if has_banner_anchor and not has_next_coin_label:
        missing_streak = prev_missing_next_coin_streak + 1
    else:
        missing_streak = 0

    coin_limit_reached = False
    trigger_reason = ""

    if matched_keyword:
        coin_limit_reached = True
        trigger_reason = f"偵測到金幣上限文字「{matched_keyword}」"
    elif earned_coins is not None and earned_coins >= 60 and has_banner_anchor:
        coin_limit_reached = True
        trigger_reason = f"偵測到今日獲得金幣已達 {earned_coins} 枚 (滿 60 枚上限)"
    elif missing_streak >= 2:
        coin_limit_reached = True
        trigger_reason = "種花橫幅已不再顯示「距離下一枚金幣」(判定已達今日金幣上限)"
    elif flower_limit_target > 0 and planted_flowers is not None and planted_flowers >= flower_limit_target:
        coin_limit_reached = True
        trigger_reason = f"單趟種植花朵總數已達 {planted_flowers:,} 朵 (設定上限 {flower_limit_target:,} 朵)"

    return {
        "planted_flowers": planted_flowers,
        "earned_coins": earned_coins,
        "next_coin_flowers": next_coin_flowers,
        "banner_text": banner_str,
        "coin_limit_reached": coin_limit_reached,
        "trigger_reason": trigger_reason,
        "missing_next_coin_streak": missing_streak,
    }


class PikminAutomationManager:
    def __init__(
        self,
        gps_service: Any,
        state_ref: Dict[str, Any],
        config_getter: Callable[[], Dict[str, Any]],
        config_saver: Callable[[Dict[str, Any]], None],
        stop_cruise_cb: Callable[[], None],
        stop_smooth_cb: Callable[[], None],
        notify_port_getter: Callable[[], Optional[int]],
        base_dirs: List[Path],
    ):
        self.gps_service = gps_service
        self.state_ref = state_ref
        self.config_getter = config_getter
        self.config_saver = config_saver
        self.stop_cruise_cb = stop_cruise_cb
        self.stop_smooth_cb = stop_smooth_cb
        self.notify_port_getter = notify_port_getter
        self.matcher = ReferenceTemplateMatcher(base_dirs)
        self.lock = threading.Lock()

        cfg = self.config_getter()
        auto_cfg = cfg.get("automation", {})
        self.settings: Dict[str, Any] = {**DEFAULT_AUTOMATION_CONFIG, **auto_cfg}

        self.runtime: Dict[str, Any] = {
            "battery_level": None,
            "is_charging": None,
            "temperature_c": None,
            "pikmin_running": None,
            "pikmin_foreground": None,
            "pikmin_pid": None,
            "pikmin_cpu": 0.0,
            "pikmin_status_label": "⚪ 尚未連線",
            "screen_state": "unknown",
            "screen_state_label": "尚未擷取",
            "detected_flowers": None,
            "detected_coins": None,
            "detected_next_coin_flowers": None,
            "coin_limit_reached": False,
            "last_ocr_banner_text": "",
            "last_screenshot_b64": None,
            "last_check_time": "尚未執行",
            "last_action_msg": "待命",
            "reference_images_loaded": self.matcher.loaded_files,
        }

        self._was_pikmin_running = False
        self._last_overheat_notify_ts = 0.0
        self._last_low_bat_action_ts = 0.0
        self._last_coin_stop_ts = 0.0
        self._last_screen_check_ts = 0.0
        self._missing_next_coin_streak = 0
        self._worker_started = False

    def start_background_worker(self) -> None:
        if self._worker_started:
            return
        self._worker_started = True
        threading.Thread(target=self._automation_loop, daemon=True).start()

    def get_settings(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.settings)

    def update_settings(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            for k, v in updates.items():
                if k in DEFAULT_AUTOMATION_CONFIG and v is not None:
                    self.settings[k] = v
            cfg = self.config_getter()
            cfg["automation"] = dict(self.settings)
            self.config_saver(cfg)
            return dict(self.settings)

    def get_status_snapshot(self, include_screenshot: bool = False) -> Dict[str, Any]:
        with self.lock:
            snap = dict(self.runtime)
            if not include_screenshot:
                snap.pop("last_screenshot_b64", None)
            snap["settings"] = dict(self.settings)
            return snap

    def _call_native_ocr(self, image_path: str) -> Dict[str, Any]:
        port = self.notify_port_getter()
        if not port:
            return {"ok": False, "texts": [], "banner_texts": [], "full_text": "", "banner_full_text": ""}
        try:
            payload = json.dumps({"action": "ocr", "image_path": image_path}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ocr",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except Exception as e:
            print(f"[Automation] 呼叫原生 Vision OCR 失敗: {e}")
            return {"ok": False, "texts": [], "banner_texts": [], "full_text": "", "banner_full_text": ""}

    def _encode_preview_jpeg(self, img: Image.Image) -> str:
        thumb = img.copy()
        thumb.thumbnail((360, 780), Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        thumb.convert("RGB").save(buf, format="JPEG", quality=72)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def execute_stop_planting_sequence(self, current_screen_state: str, reason: str) -> bool:
        """
        執行遊戲內「停止種花」點擊序列（僅於確認皮克敏在前景時呼叫）：
        1. 若在 `flower_map` 或 `home`：先點擊右側花瓣按鈕 `🌼` (0.862, 0.723) 打開選花抽屜
        2. 點擊中央偏左的橘色停止種花按鈕 `⏹` (0.401, 0.575)
        """
        try:
            if current_screen_state in ("flower_map", "home"):
                rx, ry = TAP_COORDS["right_flower_drawer"]
                self.gps_service.tap_screen_sync(rx, ry)
                time.sleep(1.4)

            sx, sy = TAP_COORDS["orange_stop_planting"]
            ok = self.gps_service.tap_screen_sync(sx, sy)
            if ok:
                self.runtime["last_action_msg"] = f"已自動點擊停止種花 ({reason})"
            return ok
        except Exception as e:
            add_debug_log("自動點擊停止種花失敗", str(e), stage="自動化守護", level="WARN")
            return False

    def inspect_screen_once(self, allow_auto_actions: bool = True) -> Dict[str, Any]:
        """
        擷取一張手機畫面並執行參考圖比對 + 原生 Vision OCR。
        ⚠️ 嚴格遵守：只有當皮克敏確認在前景運行時，才會執行自動返回導航或自動點擊停止種花。
        """
        if not self.gps_service.is_connected:
            raise RuntimeError("尚未連線至 iOS 裝置")

        proc_info = self.gps_service.query_pikmin_process_sync()
        if proc_info.get("running") is False:
            self.runtime["pikmin_running"] = False
            self.runtime["pikmin_foreground"] = False
            self.runtime["pikmin_status_label"] = "🔴 未開啟 / 已閃退"
            raise RuntimeError("偵測到皮克敏目前未在背景或前景運行，已略過截圖與點擊")

        png_bytes = self.gps_service.capture_screenshot_sync()
        if not png_bytes:
            raise RuntimeError("無法從裝置擷取螢幕畫面 (請確認已掛載 DDI 映像檔)")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(png_bytes)
            tmp_path = tmp.name

        try:
            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            ocr_data = self._call_native_ocr(tmp_path)
            cls_res = self.matcher.classify_screen(img, ocr_data)
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

        state_code = cls_res["state"]
        is_pikmin_fg = bool(cls_res["is_pikmin_screen"])
        cfg = self.get_settings()

        # 若啟用「非種花畫面自動導航回種花地圖」且確認目前是皮克敏前景畫面
        if (
            allow_auto_actions
            and cfg.get("auto_navigate_to_map")
            and is_pikmin_fg
            and state_code in ("pikmin_subpage", "home")
        ):
            nav_steps = 0
            while state_code == "pikmin_subpage" and nav_steps < 3:
                nav_steps += 1
                bx, by = TAP_COORDS["bottom_left_back"]
                self.gps_service.tap_screen_sync(bx, by)
                self.runtime["last_action_msg"] = f"自動點擊左下角返回 ({nav_steps}/3)..."
                time.sleep(1.2)
                png_bytes = self.gps_service.capture_screenshot_sync()
                if not png_bytes:
                    break
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp2:
                    tmp2.write(png_bytes)
                    tmp2_path = tmp2.name
                try:
                    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                    ocr_data = self._call_native_ocr(tmp2_path)
                    cls_res = self.matcher.classify_screen(img, ocr_data)
                    state_code = cls_res["state"]
                    is_pikmin_fg = bool(cls_res["is_pikmin_screen"])
                finally:
                    Path(tmp2_path).unlink(missing_ok=True)

            if state_code == "home" and is_pikmin_fg:
                mx, my = TAP_COORDS["home_bottom_center_map"]
                self.gps_service.tap_screen_sync(mx, my)
                self.runtime["last_action_msg"] = "已從主頁自動點擊下方中央羅盤返回種花地圖"
                time.sleep(1.5)
                png_bytes = self.gps_service.capture_screenshot_sync()
                if png_bytes:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp3:
                        tmp3.write(png_bytes)
                        tmp3_path = tmp3.name
                    try:
                        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                        ocr_data = self._call_native_ocr(tmp3_path)
                        cls_res = self.matcher.classify_screen(img, ocr_data)
                        state_code = cls_res["state"]
                        is_pikmin_fg = bool(cls_res["is_pikmin_screen"])
                    finally:
                        Path(tmp3_path).unlink(missing_ok=True)

        banner_info = parse_flower_and_coin_banner(
            ocr_data,
            custom_keywords_csv=cfg.get("coin_limit_keywords", ""),
            flower_limit_target=int(cfg.get("flower_limit_target", 15000)),
            prev_missing_next_coin_streak=self._missing_next_coin_streak if state_code in ("flower_map", "stop_drawer") else 0,
        )
        if state_code in ("flower_map", "stop_drawer"):
            self._missing_next_coin_streak = banner_info["missing_next_coin_streak"]

        now_str = time.strftime("%H:%M:%S")
        preview_b64 = self._encode_preview_jpeg(img)

        with self.lock:
            self.runtime["pikmin_running"] = True
            self.runtime["pikmin_foreground"] = is_pikmin_fg
            self.runtime["pikmin_status_label"] = (
                "🟢 皮克敏前景運行中" if is_pikmin_fg else "🟡 背景運行中 (不執行截圖與點擊)"
            )
            self.runtime["screen_state"] = state_code
            self.runtime["screen_state_label"] = cls_res["label"]
            if banner_info["planted_flowers"] is not None:
                self.runtime["detected_flowers"] = banner_info["planted_flowers"]
            if banner_info["earned_coins"] is not None:
                self.runtime["detected_coins"] = banner_info["earned_coins"]
            if banner_info["next_coin_flowers"] is not None:
                self.runtime["detected_next_coin_flowers"] = banner_info["next_coin_flowers"]
            self.runtime["coin_limit_reached"] = banner_info["coin_limit_reached"]
            if banner_info["banner_text"]:
                self.runtime["last_ocr_banner_text"] = banner_info["banner_text"]
            self.runtime["last_screenshot_b64"] = preview_b64
            self.runtime["last_check_time"] = now_str

        if (
            allow_auto_actions
            and cfg.get("coin_limit_auto_stop")
            and is_pikmin_fg
            and state_code in ("flower_map", "stop_drawer")
            and banner_info["coin_limit_reached"]
            and (time.monotonic() - self._last_coin_stop_ts) > 60.0
        ):
            self._last_coin_stop_ts = time.monotonic()
            reason = banner_info["trigger_reason"] or "已達金幣或花數上限"
            self.execute_stop_planting_sequence(state_code, reason)
            self.stop_smooth_cb()
            send_macos_notification(
                "SIM-GPS 自動化守護",
                "💰 種花已達上限自動結束",
                f"{reason}！已自動點擊停止種花並啟動平滑返航。",
                "Glass",
            )
            add_debug_log(
                title="種花已達金幣/花數上限，自動結束並平滑返航",
                error_msg=f"觸發原因: {reason} | 橫幅辨識文字: {banner_info['banner_text']}",
                stage="畫面辨識守護",
                level="INFO",
                solution="系統已自動為您點擊停止種花按鈕，並依設定時速平滑返回真實起點。",
            )

        return self.get_status_snapshot(include_screenshot=True)

    def _automation_loop(self) -> None:
        while True:
            try:
                if not self.gps_service.is_connected:
                    with self.lock:
                        self.runtime["pikmin_status_label"] = "⚪ 尚未連線"
                    self._was_pikmin_running = False
                    time.sleep(5.0)
                    continue

                cfg = self.get_settings()

                hw = self.gps_service.query_battery_and_pikmin_sync()
                bat_level = hw.get("battery_level")
                is_charging = hw.get("is_charging")
                temp_c = hw.get("temperature_c")
                proc_running = hw.get("pikmin_running")
                proc_fg_hint = hw.get("pikmin_foreground_hint")
                proc_cpu = hw.get("pikmin_cpu", 0.0)

                with self.lock:
                    if bat_level is not None:
                        self.runtime["battery_level"] = bat_level
                    if is_charging is not None:
                        self.runtime["is_charging"] = is_charging
                    if temp_c is not None:
                        self.runtime["temperature_c"] = temp_c
                    if proc_running is not None:
                        self.runtime["pikmin_running"] = proc_running
                        self.runtime["pikmin_cpu"] = proc_cpu
                        if not proc_running:
                            self.runtime["pikmin_foreground"] = False
                            self.runtime["pikmin_status_label"] = "🔴 皮克敏未開啟 / 已閃退"
                        elif proc_fg_hint is False:
                            self.runtime["pikmin_foreground"] = False
                            self.runtime["pikmin_status_label"] = "🟡 背景運行中 (不執行截圖與點擊)"
                        else:
                            self.runtime["pikmin_foreground"] = True
                            self.runtime["pikmin_status_label"] = f"🟢 前景運行中 (CPU {proc_cpu:.0f}%)"

                now_mono = time.monotonic()

                # 🔥 過熱通知與自動保護
                if temp_c is not None and cfg.get("overheat_notify"):
                    warn_temp = float(cfg.get("overheat_threshold_c", 40.0))
                    if temp_c >= warn_temp and (now_mono - self._last_overheat_notify_ts) > 180.0:
                        self._last_overheat_notify_ts = now_mono
                        send_macos_notification(
                            "SIM-GPS 溫度警報",
                            f"🔥 iPhone 機身溫度偏高 ({temp_c:.1f}°C)",
                            "建議將手機螢幕亮度調低或暫時關閉螢幕背景種花，以減少 Unity 3D 渲染發熱。",
                            "Sosumi",
                        )
                        if cfg.get("overheat_auto_pause") and temp_c >= 43.0 and self.state_ref.get("is_cruising"):
                            self.stop_cruise_cb()
                            self.runtime["last_action_msg"] = f"溫度達 {temp_c:.1f}°C，已自動暫停巡航降溫"

                # 🔋 低電量自動停止種花並平滑返航
                if (
                    bat_level is not None
                    and cfg.get("low_battery_stop")
                    and not is_charging
                    and bat_level <= int(cfg.get("low_battery_threshold", 20))
                    and not self.state_ref.get("is_returning_home")
                    and (now_mono - self._last_low_bat_action_ts) > 120.0
                ):
                    self._last_low_bat_action_ts = now_mono
                    if proc_running and proc_fg_hint is not False:
                        with contextlib.suppress(Exception):
                            self.inspect_screen_once(allow_auto_actions=False)
                            if self.runtime.get("pikmin_foreground"):
                                self.execute_stop_planting_sequence(
                                    self.runtime.get("screen_state", "flower_map"),
                                    f"低電量 {bat_level}%",
                                )
                    self.stop_smooth_cb()
                    self.runtime["last_action_msg"] = f"電量低於 {bat_level}%，已自動停種並啟動平滑返航"
                    send_macos_notification(
                        "SIM-GPS 低電量保護",
                        f"🔋 手機電量僅剩 {bat_level}%",
                        "已自動觸發停止種花並依設定速度平滑返回真實起點！",
                        "Basso",
                    )

                # 💥 遊戲閃退偵測與自動重啟
                if proc_running is False and (self._was_pikmin_running or self.state_ref.get("is_cruising")):
                    self._was_pikmin_running = False
                    if cfg.get("crash_auto_pause") and self.state_ref.get("is_cruising"):
                        self.stop_cruise_cb()
                        self.runtime["last_action_msg"] = "偵測到皮克敏閃退，已自動暫停巡航"
                        send_macos_notification(
                            "SIM-GPS 閃退警報",
                            "💥 偵測到皮克敏已關閉或閃退",
                            "巡航已自動暫停以保留路線進度，避免空走浪費冷卻。",
                            "Basso",
                        )
                    if cfg.get("crash_auto_restart"):
                        ok = self.gps_service.launch_pikmin_sync()
                        if ok:
                            self.runtime["last_action_msg"] = "已自動重新啟動皮克敏 App"
                            send_macos_notification(
                                "SIM-GPS 自動重啟",
                                "🔄 已重新啟動皮克敏 App",
                                "請確認手機處於解鎖狀態以完成遊戲載入。",
                                "Glass",
                            )
                elif proc_running is True:
                    self._was_pikmin_running = True

                # 📸 每 60 秒畫面擷取分析（僅於開啟開關 且 皮克敏在前景運行時執行）
                if (
                    cfg.get("screen_analysis_enabled")
                    and proc_running is True
                    and proc_fg_hint is not False
                    and (now_mono - self._last_screen_check_ts) >= 60.0
                ):
                    self._last_screen_check_ts = now_mono
                    try:
                        self.inspect_screen_once(allow_auto_actions=True)
                    except Exception as screen_err:
                        print(f"[Automation] 定時畫面分析略過: {screen_err}")

            except Exception as e:
                print(f"[Automation] 巡檢迴圈異常: {e}")

            time.sleep(15.0)
