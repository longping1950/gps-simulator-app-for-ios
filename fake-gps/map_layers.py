"""
map_layers.py
多地圖圖層管理模組（供前端切換標準街道、清爽亮色、暗黑夜視、地形與衛星圖層，方便未來持續擴充）
"""

from typing import Any, Dict, List

DEFAULT_MAP_LAYER_ID = "osm_standard"

MAP_LAYERS: List[Dict[str, Any]] = [
    {
        "id": "osm_standard",
        "name": "🗺️ 標準街道 (OSM)",
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
        "max_zoom": 19,
    },
    {
        "id": "carto_light",
        "name": "☀️ 清爽亮色 (Carto)",
        "url": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
        "attribution": "© OpenStreetMap © CARTO",
        "max_zoom": 20,
    },
    {
        "id": "carto_dark",
        "name": "🌙 深色夜視 (Dark)",
        "url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        "attribution": "© OpenStreetMap © CARTO",
        "max_zoom": 20,
    },
    {
        "id": "esri_satellite",
        "name": "🛰️ 衛星影像 (Esri)",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Tiles © Esri",
        "max_zoom": 19,
    },
    {
        "id": "open_topo",
        "name": "⛰️ 等高地形 (Topo)",
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenTopoMap",
        "max_zoom": 17,
    },
]


def get_map_layers() -> List[Dict[str, Any]]:
    return MAP_LAYERS
