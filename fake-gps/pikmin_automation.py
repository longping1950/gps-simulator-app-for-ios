"""
pikmin_automation.py
皮克敏 (Pikmin Bloom) 自動化守護、多裝置獨立監控與視覺分析模組

包含：
1. 多裝置電池電量與實體溫度監控 (低電量先停止種花並確認後 60km/h 返航，或低電量不返航直接關閉遊戲)
2. 遊戲 Process 存活與「前景 (Foreground)」狀態偵測 (閃退通知與自動重啟 App，不暫停巡航行程)
3. 嚴格的前景保護：僅於確認皮克敏處於手機前景時，才執行每分鐘截圖分析與自動點擊
4. 基於使用者 3 張參考圖 (`主頁.PNG`, `地圖（顯示花朵）.PNG`, `選花畫面（結束種花）.PNG`) 的正規化比例視覺狀態機 (適配不同螢幕尺寸與比例)：
   - 若停在子畫面 -> 自動點左下角返回 `<` 直到回到 `主頁` -> 再點正下方中央羅盤 `🧭` 回到 `地圖（顯示花朵）`
   - 結合原生 Vision OCR 偵測頂部橫幅標準文字「已達到獲得上限。」或「種植花朵總數 >= 15,000」
   - 達標時自動點擊右側花瓣鈕 `🌼` -> 點擊橘色停止種花鈕 `⏹` -> 二次截圖確認已停止種花 -> 啟動該裝置獨立 60km/h 平滑返航回原點
5. 內建開發者「功能測試與調試介面 (Diagnostic Test Bench)」提供豐富底層參數指標
"""

import base64
import contextlib
from datetime import datetime
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

# 依據 3 張參考圖 (1179 x 2556) 實測的正規化點擊座標 (0.0 ~ 1.0)，自動適配不同 iPhone 螢幕尺寸與比例
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
    "low_battery_kill_game": False,
    "overheat_notify": True,
    "overheat_threshold_c": 40.0,
    "crash_auto_pause": False,
    "crash_auto_restart": False,
    "screen_analysis_enabled": False,
    "auto_navigate_to_map": True,
    "coin_limit_auto_stop": False,
    "coin_limit_keywords": "已達到獲得上限,獲得上限,已達上限",
    "flower_limit_target": 15000,
}


class ReferenceTemplateMatcher:
    """
    載入根目錄的 3 張參考截圖 (`主頁.PNG`, `地圖（顯示花朵）.PNG`, `選花畫面（結束種花）.PNG`)
    或內嵌的 `ref_templates.json`，對擷取到的手機畫面進行正規化比例區塊特徵比對 (適配不同螢幕大小與長寬比)。
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
        # 針對不同長寬比 (例如 19.5:9 vs 16:9 舊機型) 自動微調上下邊界保護
        aspect = h / max(1.0, float(w))
        ref_aspect = 2556.0 / 1179.0  # ~2.168
        if abs(aspect - ref_aspect) > 0.12:
            # 針對底部按鈕區以距離底部比例進行基準對齊
            if ny1 > 0.65:
                bh1 = (1.0 - ny1) * (ref_aspect / aspect)
                bh2 = (1.0 - ny2) * (ref_aspect / aspect)
                ny1 = max(0.0, min(0.98, 1.0 - bh1))
                ny2 = max(ny1 + 0.02, min(1.0, 1.0 - bh2))
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
        w, h = rgb.size

        # 1. 先檢查是否為黑屏 / 鎖定螢幕
        full_stat = ImageStat.Stat(rgb.resize((64, 64)))
        brightness_mean = round(sum(full_stat.mean) / 3.0, 2)
        brightness_std = round(sum(full_stat.stddev) / 3.0, 2)
        if brightness_mean < 12.0 and brightness_std < 10.0:
            return {
                "state": "lock_screen",
                "label": "🔒 螢幕鎖定 / 黑屏",
                "is_pikmin_screen": False,
                "confidence": 0.99,
                "image_size": {"width": w, "height": h, "aspect_ratio": round(h / max(1.0, float(w)), 3)},
                "brightness": {"mean": brightness_mean, "std": brightness_std},
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
        roi_breakdown: Dict[str, Dict[str, float]] = {}
        for tpl_key, t in self.templates.items():
            roi_breakdown[tpl_key] = {
                rname: round(self._mae(crops[rname], t[rname]), 2)
                for rname in self.ROIS
                if rname in t
            }

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

        common_meta = {
            "image_size": {"width": w, "height": h, "aspect_ratio": round(h / max(1.0, float(w)), 3)},
            "brightness": {"mean": brightness_mean, "std": brightness_std},
            "is_orange_stop_color": is_orange_stop_color,
            "orange_stop_rgb": [round(x, 1) for x in stop_stat.mean[:3]],
            "pink_drawer_rgb": [round(x, 1) for x in drawer_stat.mean[:3]],
            "scores": {k: round(v, 1) for k, v in scores.items()},
            "roi_breakdown": roi_breakdown,
        }

        # 2. 判斷是否為「選花畫面（結束種花）」
        if is_orange_stop_color or scores.get("stop_drawer", 999.0) < 28.0 or (
            ("大家一起" in full_text or "加速" in full_text) and ("種植" in full_text or "花朵" in full_text)
        ):
            return {
                "state": "stop_drawer",
                "label": "🌺 選花畫面 (結束種花面板)",
                "is_pikmin_screen": True,
                "confidence": 0.95,
                **common_meta,
            }

        # 3. 判斷是否為「地圖（顯示花朵）」
        has_flower_banner_text = any(
            kw in (banner_text or full_text)
            for kw in ("種植的花朵", "花朵總數", "目前的獲得數量", "下一枚金幣", "枚金幣", "已達到獲得上限", "獲得上限")
        )
        if has_flower_banner_text or scores.get("flower_map", 999.0) < 30.0:
            return {
                "state": "flower_map",
                "label": "🗺️ 種花地圖 (顯示花朵與金幣)",
                "is_pikmin_screen": True,
                "confidence": 0.94,
                **common_meta,
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
                **common_meta,
            }

        # 5. 檢查左下角是否有皮克敏特徵返回按鈕 `<`
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
                **common_meta,
            }

        return {
            "state": "other_app",
            "label": "📱 非皮克敏前景畫面 (不執行自動點擊)",
            "is_pikmin_screen": False,
            "confidence": 0.80,
            **common_meta,
        }


def _normalize_ocr_text(text: str) -> str:
    """移除空白與標點符號，確保『已達到獲得上限。』穩定比對"""
    return re.sub(r"[\s。，、！!？?：:.\-_]+", "", text or "")


def parse_flower_and_coin_banner(
    ocr_data: Dict[str, Any],
    custom_keywords_csv: str,
    flower_limit_target: int,
    prev_missing_next_coin_streak: int = 0,
) -> Dict[str, Any]:
    """
    從 Vision OCR 結果解析頂部種花橫幅：
    1. 種植的花朵總數 (左側數字)
    2. 目前的獲得數量 (右側金幣數字，僅供顯示，絕不用於 >=60 觸發，避免把『距離下一枚金幣：85 96』誤判為 85 枚金幣！)
    3. 距離下一枚金幣 (右側下方數字)
    4. 嚴格偵測標準文字『已達到獲得上限』(參考實機圖 `已達到獲得上限。.heic`)，且確認橫幅未出現『距離下一枚金幣』時才觸發金幣上限自動結束！
    """
    items = ocr_data.get("items") or []
    banner_texts = ocr_data.get("banner_texts") or []
    banner_str = " ".join(banner_texts) if banner_texts else ocr_data.get("banner_full_text") or ocr_data.get("full_text", "")
    full_str = ocr_data.get("full_text", "") or banner_str

    planted_flowers: Optional[int] = None
    earned_coins: Optional[int] = None
    next_coin_flowers: Optional[int] = None

    left_numbers: List[Tuple[float, int]] = []
    right_numbers: List[Tuple[float, int]] = []

    for it in items:
        cy = float(it.get("y", 0.0))
        cx = float(it.get("x", 0.0))
        txt = str(it.get("text", "")).strip()
        if not (0.08 <= cy <= 0.26):
            continue
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

    # 先從文字找「金幣：XXX」作為距離下一枚金幣的花數
    m_rem = re.search(r"金幣\s*[:：]?\s*(\d{1,4})", banner_str)
    if m_rem:
        next_coin_flowers = int(m_rem.group(1))

    if right_numbers:
        right_sorted = sorted(right_numbers, key=lambda pair: pair[0])
        if len(right_sorted) >= 2:
            # 上方較小的合理數值 (0~60) 為目前獲得金幣，下方為距離下一枚金幣花數
            cand_coin = right_sorted[0][1]
            cand_next = right_sorted[-1][1]
            if next_coin_flowers is None:
                next_coin_flowers = cand_next
            if 0 <= cand_coin <= 60:
                earned_coins = cand_coin
        elif len(right_sorted) == 1:
            val = right_sorted[0][1]
            if ("下一枚" in banner_str or "距離下一" in banner_str) and val > 60:
                if next_coin_flowers is None:
                    next_coin_flowers = val
            elif 0 <= val <= 60:
                earned_coins = val

    # 嚴格比對標準文字「已達到獲得上限」(忽略句號與空白)
    raw_kw_list = [k.strip() for k in (custom_keywords_csv or "").split(",") if k.strip()]
    # 過濾掉過於寬鬆的舊預設單字 (如單獨的「上限」或「已達」)，確保以標準文字「已達到獲得上限」為主
    if not raw_kw_list or set(raw_kw_list) == {"上限", "已達", "已滿", "今日上限", "獲得上限"}:
        raw_kw_list = ["已達到獲得上限", "獲得上限", "已達上限"]

    norm_banner = _normalize_ocr_text(banner_str)
    norm_full = _normalize_ocr_text(full_str)
    has_next_coin_label = ("下一枚" in banner_str) or ("距離下一" in banner_str)

    matched_keyword = None
    if not has_next_coin_label:
        for kw in raw_kw_list:
            nkw = _normalize_ocr_text(kw)
            if nkw and (nkw in norm_banner or nkw in norm_full):
                matched_keyword = kw
                break

    coin_limit_reached = False
    trigger_reason = ""

    if matched_keyword:
        coin_limit_reached = True
        trigger_reason = f"偵測到標準金幣上限文字「{matched_keyword}」"
    elif flower_limit_target > 0 and planted_flowers is not None and planted_flowers >= flower_limit_target:
        coin_limit_reached = True
        trigger_reason = f"單趟種植花朵總數已達 {planted_flowers:,} 朵 (設定上限 {flower_limit_target:,} 朵)"

    return {
        "planted_flowers": planted_flowers,
        "earned_coins": earned_coins,
        "next_coin_flowers": next_coin_flowers,
        "banner_text": banner_str,
        "coin_limit_reached": coin_limit_reached,
        "matched_keyword": matched_keyword,
        "has_next_coin_label": has_next_coin_label,
        "trigger_reason": trigger_reason,
        "missing_next_coin_streak": 0,
    }


class PikminAutomationManager:
    def __init__(
        self,
        gps_service: Any,
        state_ref: Dict[str, Any],
        config_getter: Callable[[], Dict[str, Any]],
        config_saver: Callable[[Dict[str, Any]], None],
        stop_cruise_cb: Callable[[], None],
        stop_smooth_cb: Callable[..., None],
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
        # 若舊設定檔存有舊版寬鬆關鍵字，自動升級為標準文字「已達到獲得上限,獲得上限,已達上限」
        if auto_cfg.get("coin_limit_keywords") == "上限,已達,已滿,今日上限,獲得上限":
            auto_cfg["coin_limit_keywords"] = DEFAULT_AUTOMATION_CONFIG["coin_limit_keywords"]

        self.settings: Dict[str, Any] = {**DEFAULT_AUTOMATION_CONFIG, **auto_cfg}
        self.device_settings: Dict[str, Dict[str, Any]] = {
            udid: {**self.settings, **dcfg}
            for udid, dcfg in (cfg.get("device_automation") or {}).items()
            if isinstance(dcfg, dict)
        }

        self.runtime: Dict[str, Any] = self._make_empty_runtime()
        self.device_runtime: Dict[str, Dict[str, Any]] = {}

        self._worker_started = False

    def _make_empty_runtime(self, udid: Optional[str] = None) -> Dict[str, Any]:
        return {
            "udid": udid,
            "battery_level": None,
            "is_charging": None,
            "temperature_c": None,
            "thermal_state": None,
            "thermal_label": "正常",
            "induced_thermal_state": -1,
            "system_load_w": None,
            "instant_amperage_ma": None,
            "ddi_power_saving_active": False,
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
            "_was_pikmin_running": False,
            "_last_overheat_notify_ts": 0.0,
            "_last_low_bat_action_ts": 0.0,
            "_last_coin_stop_ts": 0.0,
            "_last_screen_check_ts": 0.0,
        }

    def _get_or_create_device_runtime(self, udid: Optional[str]) -> Dict[str, Any]:
        if not udid or udid == "default":
            return self.runtime
        with self.lock:
            if udid not in self.device_runtime:
                self.device_runtime[udid] = self._make_empty_runtime(udid)
            return self.device_runtime[udid]

    def start_background_worker(self) -> None:
        if self._worker_started:
            return
        self._worker_started = True
        threading.Thread(target=self._automation_loop, daemon=True).start()

    def get_settings(self, udid: Optional[str] = None) -> Dict[str, Any]:
        with self.lock:
            if udid and udid != "default" and udid in self.device_settings:
                return {**self.settings, **self.device_settings[udid]}
            return dict(self.settings)

    def update_settings(self, updates: Dict[str, Any], udid: Optional[str] = None) -> Dict[str, Any]:
        target_udid = None if (not udid or udid == "default") else udid
        with self.lock:
            if target_udid:
                cur = {**self.settings, **self.device_settings.get(target_udid, {})}
                for k, v in updates.items():
                    if k in DEFAULT_AUTOMATION_CONFIG and v is not None:
                        cur[k] = v
                self.device_settings[target_udid] = cur
                ret = dict(cur)
            else:
                for k, v in updates.items():
                    if k in DEFAULT_AUTOMATION_CONFIG and v is not None:
                        self.settings[k] = v
                # 同步預設設定至已存在裝置中對應的項目
                for d_udid, d_cfg in list(self.device_settings.items()):
                    for k, v in updates.items():
                        if k in DEFAULT_AUTOMATION_CONFIG and v is not None:
                            d_cfg[k] = v
                ret = dict(self.settings)

            cfg = self.config_getter()
            cfg["automation"] = dict(self.settings)
            cfg["device_automation"] = {k: dict(v) for k, v in self.device_settings.items()}
            self.config_saver(cfg)
            return ret

    def get_status_snapshot(self, include_screenshot: bool = False, udid: Optional[str] = None) -> Dict[str, Any]:
        target_udid = None if (not udid or udid == "default") else udid
        with self.lock:
            active_rt = self.runtime
            if target_udid and target_udid in self.device_runtime:
                active_rt = self.device_runtime[target_udid]
            else:
                conn_list = getattr(self.gps_service, "connected_udids", [])
                if conn_list and conn_list[0] in self.device_runtime:
                    active_rt = self.device_runtime[conn_list[0]]

            snap = {k: v for k, v in active_rt.items() if not k.startswith("_")}
            if not include_screenshot:
                snap.pop("last_screenshot_b64", None)

            eff_settings = (
                {**self.settings, **self.device_settings[target_udid]}
                if (target_udid and target_udid in self.device_settings)
                else dict(self.settings)
            )
            snap["udid"] = target_udid or "default"
            snap["settings"] = eff_settings
            snap["default_settings"] = dict(self.settings)
            snap["device_settings"] = {k: dict(v) for k, v in self.device_settings.items()}
            snap["devices_runtime"] = {
                d_udid: {
                    k: v
                    for k, v in d_rt.items()
                    if not k.startswith("_") and (include_screenshot or k != "last_screenshot_b64")
                }
                for d_udid, d_rt in self.device_runtime.items()
            }
            return snap

    def _call_native_ocr(self, image_path: str) -> Dict[str, Any]:
        port = self.notify_port_getter()
        if not port:
            return {"ok": False, "texts": [], "banner_texts": [], "full_text": "", "banner_full_text": "", "error": "notify_port not configured"}
        t0 = time.monotonic()
        try:
            payload = json.dumps({"action": "ocr", "image_path": image_path}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/ocr",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)
                data["roundtrip_ms"] = int((time.monotonic() - t0) * 1000)
                return data
        except Exception as e:
            print(f"[Automation] 呼叫原生 Vision OCR 失敗: {e}")
            return {
                "ok": False,
                "texts": [],
                "banner_texts": [],
                "full_text": "",
                "banner_full_text": "",
                "error": str(e),
                "roundtrip_ms": int((time.monotonic() - t0) * 1000),
            }

    def _encode_preview_jpeg(self, img: Image.Image) -> str:
        thumb = img.copy()
        thumb.thumbnail((360, 780), Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        thumb.convert("RGB").save(buf, format="JPEG", quality=72)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    def execute_stop_planting_sequence(
        self,
        current_screen_state: str,
        reason: str,
        udid: Optional[str] = None,
    ) -> bool:
        """
        執行遊戲內「停止種花」點擊序列，並二次截圖確認已結束種花（僅於確認皮克敏在前景時呼叫）：
        1. 若在 `flower_map` 或 `home`：先點擊右側花瓣按鈕 `🌼` (0.862, 0.723) 打開選花抽屜
        2. 點擊中央偏左的橘色停止種花按鈕 `⏹` (0.401, 0.575)
        3. 等待 1.4 秒後二次截圖驗證是否已離開停止種花按鈕狀態，若仍在則補點一次確保停止完成
        """
        rt = self._get_or_create_device_runtime(udid)
        try:
            if current_screen_state in ("flower_map", "home", "unknown"):
                rx, ry = TAP_COORDS["right_flower_drawer"]
                self.gps_service.tap_screen_sync(rx, ry, udid=udid)
                time.sleep(1.4)

            sx, sy = TAP_COORDS["orange_stop_planting"]
            ok = self.gps_service.tap_screen_sync(sx, sy, udid=udid)
            time.sleep(1.4)

            # 二次截圖確認停止種花是否成功執行
            verify_png = self.gps_service.capture_screenshot_sync(udid=udid)
            if verify_png:
                with contextlib.suppress(Exception):
                    v_img = Image.open(io.BytesIO(verify_png)).convert("RGB")
                    v_cls = self.matcher.classify_screen(v_img)
                    if v_cls.get("is_orange_stop_color"):
                        # 橘色停止按鈕仍在，補點一次確認停止
                        self.gps_service.tap_screen_sync(sx, sy, udid=udid)
                        time.sleep(1.2)

            if ok:
                msg = f"已自動點擊停止種花並確認完成 ({reason})"
                rt["last_action_msg"] = msg
                self.runtime["last_action_msg"] = msg
            return ok
        except Exception as e:
            add_debug_log("自動點擊停止種花失敗", str(e), stage="自動化守護", level="WARN", udid=udid)
            return False

    def inspect_screen_once(
        self,
        allow_auto_actions: bool = True,
        udid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        對指定裝置 (或第一台連線裝置) 擷取手機畫面並執行參考圖比對 + 原生 Vision OCR。
        ⚠️ 嚴格遵守：只有當皮克敏確認在前景運行時，才會執行自動返回導航或自動點擊停止種花。
        """
        if not udid or udid == "default":
            conn_list = getattr(self.gps_service, "connected_udids", [])
            udid = conn_list[0] if conn_list else getattr(self.gps_service, "current_udid", None)

        if not self.gps_service.is_device_connected(udid) if hasattr(self.gps_service, "is_device_connected") else not self.gps_service.is_connected:
            raise RuntimeError("尚未連線至 iOS 裝置")

        rt = self._get_or_create_device_runtime(udid)
        cfg = self.get_settings(udid)

        proc_info = self.gps_service.query_pikmin_process_sync(udid=udid)
        is_running = bool(proc_info.get("running"))
        is_fg = bool(proc_info.get("foreground"))
        if not is_running:
            rt["pikmin_running"] = False
            rt["pikmin_foreground"] = False
            rt["pikmin_status_label"] = "🔴 未開啟 / 已關閉"
            self.runtime.update({
                "pikmin_running": False,
                "pikmin_foreground": False,
                "pikmin_status_label": "🔴 未開啟 / 已關閉",
            })
            raise RuntimeError("皮克敏目前未開啟，強制禁止執行截圖與點擊操作")

        if not is_fg:
            rt["pikmin_running"] = True
            rt["pikmin_foreground"] = False
            rt["pikmin_status_label"] = "🟡 背景運行中 (強制禁止截圖與點擊)"
            self.runtime.update({
                "pikmin_running": True,
                "pikmin_foreground": False,
                "pikmin_status_label": "🟡 背景運行中 (強制禁止截圖與點擊)",
            })
            raise RuntimeError("皮克敏目前不在手機前景（處於背景或鎖屏），強制禁止執行截圖與點擊操作！")

        png_bytes = self.gps_service.capture_screenshot_sync(udid=udid)
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
                self.gps_service.tap_screen_sync(bx, by, udid=udid)
                rt["last_action_msg"] = f"自動點擊左下角返回 ({nav_steps}/3)..."
                time.sleep(1.2)
                png_bytes = self.gps_service.capture_screenshot_sync(udid=udid)
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
                self.gps_service.tap_screen_sync(mx, my, udid=udid)
                rt["last_action_msg"] = "已從主頁自動點擊下方中央羅盤返回種花地圖"
                time.sleep(1.5)
                png_bytes = self.gps_service.capture_screenshot_sync(udid=udid)
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
            prev_missing_next_coin_streak=0,
        )

        now_str = time.strftime("%H:%M:%S")
        preview_b64 = self._encode_preview_jpeg(img)

        with self.lock:
            rt["pikmin_running"] = True
            rt["pikmin_foreground"] = is_pikmin_fg
            rt["pikmin_status_label"] = (
                "🟢 皮克敏前景運行中" if is_pikmin_fg else "🟡 背景運行中 (不執行截圖與點擊)"
            )
            rt["screen_state"] = state_code
            rt["screen_state_label"] = cls_res["label"]
            if banner_info["planted_flowers"] is not None:
                rt["detected_flowers"] = banner_info["planted_flowers"]
            if banner_info["earned_coins"] is not None:
                rt["detected_coins"] = banner_info["earned_coins"]
            if banner_info["next_coin_flowers"] is not None:
                rt["detected_next_coin_flowers"] = banner_info["next_coin_flowers"]
            rt["coin_limit_reached"] = banner_info["coin_limit_reached"]
            if banner_info["banner_text"]:
                rt["last_ocr_banner_text"] = banner_info["banner_text"]
            rt["last_screenshot_b64"] = preview_b64
            rt["last_check_time"] = now_str

            for k in (
                "pikmin_running",
                "pikmin_foreground",
                "pikmin_status_label",
                "screen_state",
                "screen_state_label",
                "detected_flowers",
                "detected_coins",
                "detected_next_coin_flowers",
                "coin_limit_reached",
                "last_ocr_banner_text",
                "last_screenshot_b64",
                "last_check_time",
                "last_action_msg",
            ):
                self.runtime[k] = rt.get(k)

        if (
            allow_auto_actions
            and cfg.get("coin_limit_auto_stop")
            and is_pikmin_fg
            and state_code in ("flower_map", "stop_drawer")
            and banner_info["coin_limit_reached"]
            and (time.monotonic() - rt.get("_last_coin_stop_ts", 0.0)) > 60.0
        ):
            rt["_last_coin_stop_ts"] = time.monotonic()
            reason = banner_info["trigger_reason"] or "已達到獲得上限"
            self.execute_stop_planting_sequence(state_code, reason, udid=udid)
            try:
                self.stop_smooth_cb(udid=udid)
            except TypeError:
                self.stop_smooth_cb()
            send_macos_notification(
                "SIM-GPS 自動化守護",
                "💰 種花已達上限自動結束",
                f"[{udid[:6] if udid else 'iPhone'}] {reason}！已停止種花並啟動 60km/h 平滑返航。",
                "Glass",
            )
            add_debug_log(
                title="種花已達金幣/花數上限，自動結束並以 60km/h 平滑返航",
                error_msg=f"觸發原因: {reason} | 橫幅辨識文字: {banner_info['banner_text']}",
                stage="畫面辨識守護",
                level="INFO",
                solution="系統已自動為該裝置點擊停止種花按鈕並確認完成，現正以 60 km/h 獨立返回真實起點。",
                udid=udid,
            )

        return self.get_status_snapshot(include_screenshot=True, udid=udid)

    def run_diagnostic_test(
        self,
        test_type: str = "full",
        udid: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        🧪 開發者功能測試與底層調試介面 (Diagnostic Test Bench)
        """
        params = params or {}
        t0 = time.monotonic()
        if not udid or udid == "default":
            conn_list = getattr(self.gps_service, "connected_udids", [])
            udid = conn_list[0] if conn_list else getattr(self.gps_service, "current_udid", None)

        is_conn = (
            self.gps_service.is_device_connected(udid)
            if hasattr(self.gps_service, "is_device_connected")
            else self.gps_service.is_connected
        )
        if not udid or not is_conn:
            return {
                "ok": False,
                "test_type": test_type,
                "udid": udid,
                "error": "尚未連線至任何 iOS 裝置，請先連線裝置後再執行測試",
            }

        result: Dict[str, Any] = {
            "ok": True,
            "test_type": test_type,
            "udid": udid,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        }

        try:
            proc_info: Optional[Dict[str, Any]] = None
            if test_type in ("battery_raw", "full"):
                t_bat = time.monotonic()
                bat_info = self.gps_service.query_battery_and_pikmin_sync(udid=udid, include_raw=True)
                result["battery_diagnostics"] = {
                    "elapsed_ms": int((time.monotonic() - t_bat) * 1000),
                    "battery_level": bat_info.get("battery_level"),
                    "is_charging": bat_info.get("is_charging"),
                    "temperature_c": bat_info.get("temperature_c"),
                    "thermal_state": bat_info.get("thermal_state"),
                    "thermal_label": bat_info.get("thermal_label"),
                    "induced_thermal_state": bat_info.get("induced_thermal_state"),
                    "ddi_power_saving_active": bat_info.get("ddi_power_saving_active"),
                    "system_load_w": bat_info.get("system_load_w"),
                    "system_load_mw": bat_info.get("system_load_mw"),
                    "battery_power_mw": bat_info.get("battery_power_mw"),
                    "instant_amperage_ma": bat_info.get("instant_amperage_ma"),
                    "voltage_mv": bat_info.get("voltage_mv"),
                    "raw_ioregistry": bat_info.get("raw_diagnostics") or bat_info.get("raw"),
                }

            if test_type in ("process_fg", "screenshot_ocr", "full"):
                t_proc = time.monotonic()
                proc_info = self.gps_service.query_pikmin_process_sync(udid=udid)
                if test_type in ("process_fg", "full"):
                    result["process_diagnostics"] = {
                        "elapsed_ms": int((time.monotonic() - t_proc) * 1000),
                        **proc_info,
                    }

            if test_type in ("screenshot_ocr", "full"):
                # 強制要求皮克敏在前景才能執行截圖
                if not proc_info or not proc_info.get("running") or not proc_info.get("foreground"):
                    result["screenshot_ocr"] = {
                        "ok": False,
                        "skipped_not_foreground": True,
                        "pikmin_running": proc_info.get("running") if proc_info else False,
                        "pikmin_foreground": proc_info.get("foreground") if proc_info else False,
                        "error": "皮克敏目前不在手機前景（未開啟或處於背景），強制禁止執行截圖！請先在手機上切回皮克敏畫面。",
                    }
                else:
                    t_shot = time.monotonic()
                    png_bytes = self.gps_service.capture_screenshot_sync(udid=udid)
                    shot_ms = int((time.monotonic() - t_shot) * 1000)
                    if not png_bytes:
                        result["screenshot_ocr"] = {
                            "ok": False,
                            "screenshot_elapsed_ms": shot_ms,
                            "error": "擷取螢幕失敗 (capture_screenshot_sync 回傳 None)",
                        }
                    else:
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                            tmp.write(png_bytes)
                            tmp_path = tmp.name
                        try:
                            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                            ocr_data = self._call_native_ocr(tmp_path)
                            cls_res = self.matcher.classify_screen(img, ocr_data)
                            cfg = self.get_settings(udid)
                            banner_parsed = parse_flower_and_coin_banner(
                                ocr_data,
                                custom_keywords_csv=cfg.get("coin_limit_keywords", ""),
                                flower_limit_target=int(cfg.get("flower_limit_target", 15000)),
                            )
                            preview_b64 = self._encode_preview_jpeg(img)
                        finally:
                            Path(tmp_path).unlink(missing_ok=True)

                        result["screenshot_ocr"] = {
                            "ok": True,
                            "png_bytes_len": len(png_bytes),
                            "screenshot_elapsed_ms": shot_ms,
                            "classification": cls_res,
                            "ocr_raw": {
                                "ok": ocr_data.get("ok"),
                                "elapsed_ms": ocr_data.get("elapsed_ms"),
                                "roundtrip_ms": ocr_data.get("roundtrip_ms"),
                                "banner_texts": ocr_data.get("banner_texts", []),
                                "banner_full_text": ocr_data.get("banner_full_text", ""),
                                "full_text": ocr_data.get("full_text", ""),
                                "item_count": len(ocr_data.get("items") or []),
                                "items": (ocr_data.get("items") or [])[:40],
                                "error": ocr_data.get("error"),
                            },
                            "banner_parsed": banner_parsed,
                            "preview_b64": preview_b64,
                        }

            if test_type == "hid_tap":
                nx = float(params.get("norm_x", 0.5))
                ny = float(params.get("norm_y", 0.5))
                result["hid_tap"] = self.gps_service.tap_screen_detailed_sync(nx, ny, udid=udid)

            elif test_type == "launch_pikmin":
                result["launch_pikmin"] = self.gps_service.launch_pikmin_detailed_sync(udid=udid)

            elif test_type == "kill_pikmin":
                result["kill_pikmin"] = self.gps_service.kill_pikmin_sync(udid=udid)

            elif test_type == "stop_planting":
                ok = self.execute_stop_planting_sequence("flower_map", "調試面板手動測試停止種花", udid=udid)
                result["stop_planting"] = {"ok": ok, "tap_coords": TAP_COORDS}

        except Exception as e:
            result["ok"] = False
            result["error"] = f"{type(e).__name__}: {e}"

        result["total_elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        return result

    def _automation_loop(self) -> None:
        while True:
            try:
                conn_udids = list(getattr(self.gps_service, "connected_udids", []))
                if not conn_udids and self.gps_service.is_connected and getattr(self.gps_service, "current_udid", None):
                    conn_udids = [self.gps_service.current_udid]

                if not conn_udids:
                    with self.lock:
                        self.runtime["pikmin_status_label"] = "⚪ 尚未連線"
                    time.sleep(4.0)
                    continue

                now_mono = time.monotonic()

                for idx, udid in enumerate(conn_udids):
                    if not udid:
                        continue
                    rt = self._get_or_create_device_runtime(udid)
                    cfg = self.get_settings(udid)
                    sess = self.gps_service.get_session(udid) if hasattr(self.gps_service, "get_session") else None
                    dev_label = (getattr(sess, "device_name", None) or udid[:6]) if sess else udid[:6]

                    hw = self.gps_service.query_battery_and_pikmin_sync(udid=udid)
                    bat_level = hw.get("battery_level")
                    is_charging = hw.get("is_charging")
                    temp_c = hw.get("temperature_c")
                    thermal_state = hw.get("thermal_state")
                    thermal_label = hw.get("thermal_label")
                    induced_thermal = hw.get("induced_thermal_state", -1)
                    sys_load_w = hw.get("system_load_w")
                    inst_amp_ma = hw.get("instant_amperage_ma")
                    ddi_ps_active = bool(hw.get("ddi_power_saving_active"))
                    proc_running = hw.get("pikmin_running")
                    proc_fg = hw.get("pikmin_foreground")
                    if proc_fg is None:
                        proc_fg = bool(hw.get("pikmin_foreground_hint"))
                    proc_cpu = hw.get("pikmin_cpu", 0.0)
                    proc_pid = hw.get("pikmin_pid")

                    with self.lock:
                        if bat_level is not None:
                            rt["battery_level"] = bat_level
                        if is_charging is not None:
                            rt["is_charging"] = is_charging
                        if temp_c is not None:
                            rt["temperature_c"] = temp_c
                        if thermal_state is not None:
                            rt["thermal_state"] = thermal_state
                        if thermal_label:
                            rt["thermal_label"] = thermal_label
                        rt["induced_thermal_state"] = induced_thermal
                        if sys_load_w is not None:
                            rt["system_load_w"] = sys_load_w
                        if inst_amp_ma is not None:
                            rt["instant_amperage_ma"] = inst_amp_ma
                        rt["ddi_power_saving_active"] = ddi_ps_active
                        if proc_running is not None:
                            rt["pikmin_running"] = proc_running
                            rt["pikmin_cpu"] = proc_cpu
                            rt["pikmin_pid"] = proc_pid
                            if not proc_running:
                                rt["pikmin_foreground"] = False
                                rt["pikmin_status_label"] = "🔴 皮克敏已關閉"
                            elif not proc_fg:
                                rt["pikmin_foreground"] = False
                                rt["pikmin_status_label"] = "🟡 背景運行中 (不執行截圖與點擊)"
                            else:
                                rt["pikmin_foreground"] = True
                                rt["pikmin_status_label"] = (
                                    f"🟢 前景運行中 (CPU {proc_cpu:.0f}%)" if proc_cpu > 0 else "🟢 前景運行中"
                                )

                        if idx == 0:
                            for k in (
                                "battery_level",
                                "is_charging",
                                "temperature_c",
                                "thermal_state",
                                "thermal_label",
                                "induced_thermal_state",
                                "system_load_w",
                                "instant_amperage_ma",
                                "ddi_power_saving_active",
                                "pikmin_running",
                                "pikmin_foreground",
                                "pikmin_pid",
                                "pikmin_cpu",
                                "pikmin_status_label",
                            ):
                                self.runtime[k] = rt.get(k)

                    # 🔥 1. 機身過熱系統通知 (不暫停行程，支援舊版 °C 與 iOS 26/27 原生 Thermal State 熱狀態)
                    if cfg.get("overheat_notify") and (now_mono - rt.get("_last_overheat_notify_ts", 0.0)) > 180.0:
                        warn_temp = float(cfg.get("overheat_threshold_c", 40.0))
                        if temp_c is not None and temp_c >= warn_temp:
                            rt["_last_overheat_notify_ts"] = now_mono
                            send_macos_notification(
                                "SIM-GPS 溫度警報",
                                f"🔥 [{dev_label}] iPhone 溫度偏高 ({temp_c:.1f}°C)",
                                "建議降低手機螢幕亮度或加強散熱以減少發熱。",
                                "Sosumi",
                            )
                        elif (
                            temp_c is None
                            and (induced_thermal is None or induced_thermal < 1)
                            and thermal_state is not None
                            and thermal_state >= 2
                        ):
                            rt["_last_overheat_notify_ts"] = now_mono
                            send_macos_notification(
                                "SIM-GPS 熱狀態警報",
                                f"🔥 [{dev_label}] iPhone 機身熱狀態達「{thermal_label}」",
                                "偵測到系統熱壓力偏高，建議降低手機螢幕亮度或加強散熱。",
                                "Sosumi",
                            )

                    # 🔋 2. 低電量保護：先執行停止種花並確定停止後，再 60km/h 返航 或 直接關閉遊戲不返航
                    is_dev_returning = bool(sess.is_returning_home) if sess else bool(self.state_ref.get("is_returning_home"))
                    if (
                        bat_level is not None
                        and cfg.get("low_battery_stop")
                        and not is_charging
                        and bat_level <= int(cfg.get("low_battery_threshold", 20))
                        and not is_dev_returning
                        and (now_mono - rt.get("_last_low_bat_action_ts", 0.0)) > 120.0
                    ):
                        rt["_last_low_bat_action_ts"] = now_mono
                        if proc_running and proc_fg:
                            with contextlib.suppress(Exception):
                                self.inspect_screen_once(allow_auto_actions=False, udid=udid)
                                if rt.get("pikmin_foreground"):
                                    self.execute_stop_planting_sequence(
                                        rt.get("screen_state", "flower_map"),
                                        f"低電量 {bat_level}%",
                                        udid=udid,
                                    )

                        if cfg.get("low_battery_kill_game"):
                            self.gps_service.kill_pikmin_sync(udid=udid)
                            if hasattr(self.gps_service, "stop_device"):
                                self.gps_service.stop_device(udid)
                            else:
                                self.gps_service.stop()
                            rt["last_action_msg"] = f"電量低於 {bat_level}%，已停止種花並直接關閉遊戲斷線 (不返航)"
                            send_macos_notification(
                                "SIM-GPS 低電量直接關遊戲",
                                f"🔋 [{dev_label}] 電量僅剩 {bat_level}%",
                                "已完成停止種花並直接關閉皮克敏遊戲與中斷定位！",
                                "Basso",
                            )
                        else:
                            try:
                                self.stop_smooth_cb(udid=udid)
                            except TypeError:
                                self.stop_smooth_cb()
                            rt["last_action_msg"] = f"電量低於 {bat_level}%，已確認停止種花並啟動 60km/h 平滑返航"
                            send_macos_notification(
                                "SIM-GPS 低電量保護",
                                f"🔋 [{dev_label}] 手機電量僅剩 {bat_level}%",
                                "已自動觸發停止種花並以 60 km/h 平滑返回真實起點！",
                                "Basso",
                            )

                    # 💥 3. 遊戲關閉/閃退偵測：僅在「有行程中 (is_cruising)」才執行自動重啟，否則僅提示「XX裝置遊戲已關閉」
                    if proc_running is False and rt.get("_was_pikmin_running", False):
                        rt["_was_pikmin_running"] = False
                        is_cruising_now = bool(self.state_ref.get("is_cruising"))
                        if is_cruising_now and cfg.get("crash_auto_restart"):
                            rt["last_action_msg"] = f"[{dev_label}] 行程中偵測到閃退，正在自動重啟遊戲..."
                            ok = self.gps_service.launch_pikmin_sync(udid=udid)
                            if ok:
                                rt["_was_pikmin_running"] = True
                                rt["last_action_msg"] = f"[{dev_label}] 已自動重新啟動皮克敏 App (未暫停行程)"
                                send_macos_notification(
                                    "SIM-GPS 閃退自動重啟",
                                    f"🔄 [{dev_label}] 行程中偵測到遊戲閃退，已自動重啟",
                                    "巡航行程持續進行中，已在手機端重新喚醒皮克敏。",
                                    "Glass",
                                )
                            else:
                                send_macos_notification(
                                    "SIM-GPS 遊戲已關閉",
                                    f"⚠️ {dev_label} 遊戲已關閉 (自動重啟失敗)",
                                    "請確認手機處於解鎖狀態。",
                                    "Basso",
                                )
                        else:
                            rt["last_action_msg"] = f"📱 {dev_label} 遊戲已關閉"
                            send_macos_notification(
                                "SIM-GPS 遊戲狀態提示",
                                f"📱 {dev_label} 遊戲已關閉",
                                "目前非巡航行程中（或未開啟自動重啟），系統僅提示狀態而不自動重啟。",
                                "Basso",
                            )
                    elif proc_running is True:
                        rt["_was_pikmin_running"] = True

                    # 📸 4. 每 60 秒畫面擷取分析（強制要求該機皮克敏在前景運行時才執行）
                    if (
                        cfg.get("screen_analysis_enabled")
                        and proc_running is True
                        and proc_fg is True
                        and (now_mono - rt.get("_last_screen_check_ts", 0.0)) >= 60.0
                    ):
                        rt["_last_screen_check_ts"] = now_mono
                        with contextlib.suppress(Exception):
                            self.inspect_screen_once(allow_auto_actions=True, udid=udid)

            except Exception as e:
                print(f"[Automation] 背景守護迴圈異常: {e}")

            time.sleep(5.0)
