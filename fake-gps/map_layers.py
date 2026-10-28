"""
map_layers.py
地圖圖層管理模組
目前僅啟用標準 OpenStreetMap 底圖；預留遊戲內機制圖層（如飾品皮克敏分佈、大花朵、蘑菇點位等 Overlay）結構供日後擴充。
"""

from typing import Any, Dict, List

DEFAULT_MAP_LAYER_ID = "osm_standard"

# 基礎底圖（目前僅保留標準街道圖層）
MAP_LAYERS: List[Dict[str, Any]] = [
    {
        "id": "osm_standard",
        "name": "🗺️ 標準街道 (OSM)",
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
        "max_zoom": 19,
    },
]

# 預留：未來遊戲內機制疊加圖層 (如飾品皮克敏洗牌後的新分佈圖層、大花/蘑菇格網圖層)
GAME_OVERLAY_LAYERS: List[Dict[str, Any]] = []


def get_map_layers() -> List[Dict[str, Any]]:
    return MAP_LAYERS
