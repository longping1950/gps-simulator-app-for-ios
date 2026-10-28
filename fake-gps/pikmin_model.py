"""
pikmin_model.py
皮克敏 (Pikmin Bloom) 幾何推土機種花演算法與 7 擇 1 模式配置模組
"""

import math
from typing import Any, Dict

# --- 🌸 底層基準常數 ---
H_CELL_M = 5.0               # 網格南北長度 (公尺)
W_CELL_M = 4.5               # 網格東西寬度 (公尺)
T_SAMPLE_SEC = 2.0           # 位置判定採樣週期 (秒)
T_COOLDOWN_SEC = 300         # 遊戲底層同格重複種植冷卻時間 (5 分鐘 = 300 秒)
T_COOLDOWN_WARN_SEC = 330    # 循環單圈安全警告門檻 (5 分半 = 330 秒，預留緩衝避免冷卻吞花)
V_MAX_PLANTING_KMH = 18.0    # 極速種花上限 (18 km/h)
V_HARD_CAP_KMH = 20.0        # 種花模式滑桿與自動調速絕對上限 (20 km/h，超過斷崖鎖定不產花)
V_SAFE_KMH = 16.5            # 推薦巡航安全上限 (km/h)

# 5 階段皮克敏隊伍規格表（直接整合最佳花瓣率 16.5 km/h 至第 4 階、最高產花量 17.0 km/h 至第 5 階）
PIKMIN_TIERS: Dict[int, Dict[str, Any]] = {
    1: {
        "tier": 1,
        "pikmin_count": 9,
        "label": "1 ～ 9 隻",
        "short_label": "1~9隻",
        "period_sec": 30.0,
        "petals_per_min": 2.0,
        "petals_per_hr": 120,
        "grid_h": 2,
        "grid_w": 1,
        "matrix": "2×1",
        "blade_ew_m": 10.0,
        "blade_ns_m": 4.5,
        "recommended_kmh": 12.0,
        "sweet_spot_desc": "自動調速 12.0 km/h (~60朵/分, 30朵/片)",
    },
    2: {
        "tier": 2,
        "pikmin_count": 19,
        "label": "10 ～ 19 隻",
        "short_label": "10~19隻",
        "period_sec": 20.0,
        "petals_per_min": 3.0,
        "petals_per_hr": 180,
        "grid_h": 3,
        "grid_w": 2,
        "matrix": "3×2",
        "blade_ew_m": 15.0,
        "blade_ns_m": 9.0,
        "recommended_kmh": 14.0,
        "sweet_spot_desc": "自動調速 14.0 km/h (~155朵/分, 52朵/片)",
    },
    3: {
        "tier": 3,
        "pikmin_count": 29,
        "label": "20 ～ 29 隻",
        "short_label": "20~29隻",
        "period_sec": 15.0,
        "petals_per_min": 4.0,
        "petals_per_hr": 240,
        "grid_h": 4,
        "grid_w": 3,
        "matrix": "4×3",
        "blade_ew_m": 20.0,
        "blade_ns_m": 13.5,
        "recommended_kmh": 15.0,
        "sweet_spot_desc": "自動調速 15.0 km/h (~222朵/分, 55.6朵/片)",
    },
    4: {
        "tier": 4,
        "pikmin_count": 39,
        "label": "30 ～ 39 隻",
        "short_label": "30~39隻",
        "period_sec": 12.0,
        "petals_per_min": 5.0,
        "petals_per_hr": 300,
        "grid_h": 5,
        "grid_w": 4,
        "matrix": "5×4",
        "blade_ew_m": 25.0,
        "blade_ns_m": 18.0,
        "recommended_kmh": 16.5,
        "sweet_spot_desc": "最佳花瓣率 16.5 km/h (~306朵/分, 61.1朵/片)",
    },
    5: {
        "tier": 5,
        "pikmin_count": 40,
        "label": "40 隻 (滿編)",
        "short_label": "40滿編",
        "period_sec": 10.0,
        "petals_per_min": 6.0,
        "petals_per_hr": 360,
        "grid_h": 6,
        "grid_w": 5,
        "matrix": "6×5",
        "blade_ew_m": 30.0,
        "blade_ns_m": 22.5,
        "recommended_kmh": 17.0,
        "sweet_spot_desc": "最高產花量 17.0 km/h (~378朵/分, 63.0朵/片)",
    },
}

# 7 擇 1 模式定義：5 階段皮克敏 + 極速種花 18 km/h + 快速移動 800 km/h
SPEED_MODES: Dict[str, Dict[str, Any]] = {
    "tier_1": {"mode": "tier_1", "pikmin_tier": 1, "speed_kmh": 12.0, "max_cap_kmh": 12.0, "label": "1～9隻 (12 km/h)"},
    "tier_2": {"mode": "tier_2", "pikmin_tier": 2, "speed_kmh": 14.0, "max_cap_kmh": 16.5, "label": "10～19隻 (14 km/h)"},
    "tier_3": {"mode": "tier_3", "pikmin_tier": 3, "speed_kmh": 15.0, "max_cap_kmh": 16.5, "label": "20～29隻 (15 km/h)"},
    "tier_4": {"mode": "tier_4", "pikmin_tier": 4, "speed_kmh": 16.5, "max_cap_kmh": 18.0, "label": "30～39隻 (16.5 km/h 最佳花瓣)"},
    "tier_5": {"mode": "tier_5", "pikmin_tier": 5, "speed_kmh": 17.0, "max_cap_kmh": 18.0, "label": "40滿編 (17.0 km/h 最高產花)"},
    "extreme_18": {"mode": "extreme_18", "pikmin_tier": 5, "speed_kmh": 18.0, "max_cap_kmh": 18.0, "label": "⚡ 極速種花 (18.0 km/h)"},
    "fast_800": {"mode": "fast_800", "pikmin_tier": 5, "speed_kmh": 800.0, "max_cap_kmh": 800.0, "label": "✈️ 快速移動 (800 km/h)"},
}


def calculate_planting_efficiency(pikmin_count: int, speed_kmh: float, direction: str = "EW") -> Dict[str, Any]:
    """
    基於遊戲底層幾何推土機模型與實測數據計算皮克敏種花效益
    :param pikmin_count: 皮克敏隊伍數量 (1~40)
    :param speed_kmh: 巡航時速 (km/h)
    :param direction: 預設以標準最佳刀鋒 'EW' 計算
    """
    if pikmin_count <= 9:
        tier_id, t_petal, grid_h, grid_w = 1, 30.0, 2, 1
    elif pikmin_count <= 19:
        tier_id, t_petal, grid_h, grid_w = 2, 20.0, 3, 2
    elif pikmin_count <= 29:
        tier_id, t_petal, grid_h, grid_w = 3, 15.0, 4, 3
    elif pikmin_count <= 39:
        tier_id, t_petal, grid_h, grid_w = 4, 12.0, 5, 4
    else:
        tier_id, t_petal, grid_h, grid_w = 5, 10.0, 6, 5

    petals_per_min = 60.0 / t_petal
    petals_per_hr = int(round(petals_per_min * 60))

    # 超速斷崖處理 (>= 20 km/h)
    if speed_kmh >= V_HARD_CAP_KMH:
        return {
            "status": "OVER_SPEED_DROP",
            "over_speed_limit": True,
            "tier": tier_id,
            "pikmin_tier": f"{grid_h}×{grid_w}",
            "direction": direction.upper(),
            "petal_interval_sec": int(t_petal),
            "petals_per_min": round(petals_per_min, 1),
            "petals_per_hr": petals_per_hr,
            "flowers_per_min": 0,
            "flowers_per_hr": 0,
            "flowers_per_petal": 0.0,
            "step_2s_m": round((speed_kmh / 3.6) * T_SAMPLE_SEC, 2),
            "rows_per_2s": 0.0,
            "blade_cells": grid_h if direction.upper() == "EW" else grid_w,
            "recommended_max_safe_speed": 18.0 if pikmin_count >= 20 else 12.0,
        }

    v_ms = max(0.0, speed_kmh) / 3.6
    if direction.upper() == "EW":
        blade_width = grid_h
        step_len = W_CELL_M
    else:
        blade_width = grid_w
        step_len = H_CELL_M

    step_2s_m = v_ms * T_SAMPLE_SEC
    rows_per_2s = step_2s_m / step_len

    if pikmin_count <= 9:
        eff_rows = min(rows_per_2s, 1.0)
        if speed_kmh > 12.0:
            penalty = max(0.6, 1.0 - (speed_kmh - 12.0) * 0.012)
        elif speed_kmh <= 4.5:
            penalty = 0.81
        elif speed_kmh <= 8.5:
            penalty = 0.71
        else:
            penalty = 1.0
        flowers_per_min = 30.0 * eff_rows * blade_width * penalty
    else:
        # 若介於 18.0 ~ 20.0 km/h 之間，輕微線性衰減
        penalty = 1.0 if speed_kmh <= V_MAX_PLANTING_KMH else max(0.5, 1.0 - (speed_kmh - V_MAX_PLANTING_KMH) * 0.25)
        flowers_per_min = (60.0 * v_ms * blade_width / step_len) * penalty

    flowers_per_petal = flowers_per_min / petals_per_min if petals_per_min > 0 else 0.0

    return {
        "status": "OK",
        "over_speed_limit": False,
        "tier": tier_id,
        "pikmin_tier": f"{grid_h}×{grid_w}",
        "direction": direction.upper(),
        "petal_interval_sec": int(t_petal),
        "petals_per_min": round(petals_per_min, 1),
        "petals_per_hr": petals_per_hr,
        "flowers_per_min": int(round(flowers_per_min)),
        "flowers_per_hr": int(round(flowers_per_min * 60)),
        "flowers_per_petal": round(flowers_per_petal, 1),
        "step_2s_m": round(step_2s_m, 2),
        "rows_per_2s": round(rows_per_2s, 2),
        "blade_cells": blade_width,
        "recommended_max_safe_speed": 18.0 if pikmin_count >= 20 else 12.0,
    }


def compute_pikmin_stats(eta_seconds: int, speed_kmh: float, tier_id: int, direction: str = "EW") -> Dict[str, Any]:
    """結合幾何推土機模型與當前行程時間計算總消耗花瓣與總產花數。"""
    tier = PIKMIN_TIERS.get(tier_id, PIKMIN_TIERS[5])
    eff_data = calculate_planting_efficiency(tier["pikmin_count"], speed_kmh, direction=direction)
    period_sec = tier["period_sec"]

    if eta_seconds <= 0:
        petals_used = 0
        flowers_planted = 0
    else:
        petals_used = int(math.ceil(eta_seconds / period_sec))
        flowers_planted = (
            0
            if eff_data["over_speed_limit"]
            else int(round(eff_data["flowers_per_min"] * (eta_seconds / 60.0)))
        )

    return {
        **tier,
        **eff_data,
        "trip_petals": petals_used,
        "trip_flowers": flowers_planted,
    }
