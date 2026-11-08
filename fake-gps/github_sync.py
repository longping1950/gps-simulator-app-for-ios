"""
github_sync.py
GitHub 版本檢查 (區分大更新 / 小更新 / Bug修復、支援跳過版本)、
小版本免重裝熱更新 (Hot-Update) 與 GitHub 雲端路標庫搜尋 / 上傳模組
"""

import base64
import datetime
import http.client
import json
import os
from pathlib import Path
import re
import ssl
from typing import Any, Dict, List, Optional, Tuple

REPO_OWNER = "longping1950"
REPO_NAME = "gps-simulator-app-for-ios"
REPO_BRANCH = "main"
CURRENT_FALLBACK_VERSION = "1.3.1"


def _parse_version_tuple(ver_str: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", str(ver_str or "0.0.0"))
    if not nums:
        return (0, 0, 0)
    return tuple(int(x) for x in nums[:4])


def _make_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _https_get_text(host: str, path: str, headers: Optional[Dict[str, str]] = None, timeout: float = 7.0) -> Tuple[int, str]:
    ctx = _make_ssl_context()
    conn = http.client.HTTPSConnection(host, timeout=timeout, context=ctx)
    req_headers = {
        "User-Agent": "SIM-GPS-macOS-Updater/1.3",
        "Cache-Control": "no-cache",
    }
    if headers:
        req_headers.update(headers)
    try:
        conn.request("GET", path, headers=req_headers)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        return resp.status, body
    finally:
        conn.close()


def _https_request_json(
    host: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 10.0,
) -> Tuple[int, Dict[str, Any]]:
    ctx = _make_ssl_context()
    conn = http.client.HTTPSConnection(host, timeout=timeout, context=ctx)
    req_headers = {
        "User-Agent": "SIM-GPS-macOS-Updater/1.3",
        "Accept": "application/vnd.github+json",
    }
    if headers:
        req_headers.update(headers)
    body_bytes = None
    if payload is not None:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        conn.request(method, path, body=body_bytes, headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except Exception:
            data = {"raw": raw}
        return resp.status, data
    finally:
        conn.close()


def get_project_root_dir(app_dir: Path) -> Path:
    doc_repo = Path.home() / "Documents" / "fake-gps"
    if (doc_repo / ".git").exists() or (doc_repo / "fake-gps").exists():
        return doc_repo
    if (app_dir.parent / ".git").exists():
        return app_dir.parent
    return app_dir


def get_local_version_info(app_dir: Path, config_dir: Path) -> Dict[str, Any]:
    candidates = [
        config_dir / "hotfix" / "version.json",
        Path.home() / "Documents" / "fake-gps" / "version.json",
        app_dir.parent / "version.json",
        app_dir / "version.json",
    ]
    existing = [p for p in candidates if p.exists()]
    if existing:
        newest = max(existing, key=lambda p: p.stat().st_mtime)
        try:
            with open(newest, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and data.get("version"):
                    return data
        except Exception:
            pass
    return {
        "version": CURRENT_FALLBACK_VERSION,
        "base_app_version": "1.3",
        "update_type": "minor",
        "title": f"v{CURRENT_FALLBACK_VERSION}",
        "changelog": [],
    }


def check_github_version(
    app_dir: Path,
    config_dir: Path,
    skipped_version: Optional[str] = None,
    force_manual: bool = False,
) -> Dict[str, Any]:
    local_info = get_local_version_info(app_dir, config_dir)
    local_ver = str(local_info.get("version") or CURRENT_FALLBACK_VERSION)

    raw_path = f"/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}/version.json?t={int(datetime.datetime.now().timestamp())}"
    remote_info: Optional[Dict[str, Any]] = None
    fetch_error: Optional[str] = None

    try:
        status, text = _https_get_text("raw.githubusercontent.com", raw_path, timeout=6.0)
        if status == 200:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("version"):
                remote_info = parsed
        elif status == 404:
            fetch_error = "GitHub 遠端分支尚無 version.json (首次推送後即可啟用雲端比對)"
        else:
            fetch_error = f"GitHub 回應狀態碼 {status}"
    except Exception as e:
        fetch_error = str(e)

    if remote_info is None:
        # 若遠端尚未推送或離線，回傳本機目前版本資訊
        return {
            "ok": True,
            "remote_reachable": False,
            "note": fetch_error,
            "local_version": local_ver,
            "remote_version": local_ver,
            "has_update": False,
            "should_prompt": False,
            "is_skipped": False,
            "update_type": local_info.get("update_type", "minor"),
            "update_type_label": "目前已是最新版本",
            "title": local_info.get("title", f"v{local_ver}"),
            "changelog": local_info.get("changelog", []),
            "hot_update_supported": bool(local_info.get("hot_update_supported", True)),
            "repo_owner": REPO_OWNER,
            "repo_name": REPO_NAME,
        }

    remote_ver = str(remote_info.get("version") or local_ver)
    has_update = _parse_version_tuple(remote_ver) > _parse_version_tuple(local_ver)
    is_skipped = bool(skipped_version and skipped_version == remote_ver)
    update_type = str(remote_info.get("update_type") or "minor").lower()

    type_labels = {
        "major": "🔴 大更新 (需下載完整新版 App)",
        "minor": "🔵 小更新 (支援免重裝一鍵熱更新)",
        "bugfix": "🟢 Bug 修復 (支援免重裝一鍵熱更新)",
        "patch": "🟢 Bug 修復 (支援免重裝一鍵熱更新)",
    }
    update_type_label = remote_info.get("update_type_label") or type_labels.get(update_type, "🔵 版本更新")
    hot_supported = bool(remote_info.get("hot_update_supported", update_type in ("minor", "bugfix", "patch")))
    if update_type == "major":
        hot_supported = False

    return {
        "ok": True,
        "remote_reachable": True,
        "local_version": local_ver,
        "remote_version": remote_ver,
        "has_update": has_update,
        "should_prompt": has_update and (force_manual or not is_skipped),
        "is_skipped": is_skipped,
        "update_type": update_type,
        "update_type_label": update_type_label,
        "release_date": remote_info.get("release_date", ""),
        "title": remote_info.get("title") or f"發現新版本 v{remote_ver}",
        "changelog": remote_info.get("changelog") or [],
        "hot_update_supported": hot_supported,
        "hot_update_files": remote_info.get("hot_update_files") or [],
        "repo_owner": REPO_OWNER,
        "repo_name": REPO_NAME,
    }


def perform_github_hot_update(app_dir: Path, config_dir: Path) -> Dict[str, Any]:
    """
    從 GitHub raw 下載小更新 / Bug 修復檔案清單，並同步寫入：
    1. ~/Library/Application Support/fake-gps/hotfix/
    2. 當前執行中的 App Bundle Resources 目錄 (app_dir)
    3. 本機開發目錄 ~/Documents/fake-gps/ (若存在)
    """
    ver_check = check_github_version(app_dir, config_dir, force_manual=True)
    if not ver_check.get("remote_reachable"):
        return {"ok": False, "error": ver_check.get("note") or "無法連線至 GitHub 伺服器"}

    if ver_check.get("update_type") == "major" or not ver_check.get("hot_update_supported"):
        return {
            "ok": False,
            "error": "此版本屬於「大更新 (Major)」，涉及底層框架或內嵌執行環境變更，請前往 GitHub 下載完整新版 App。",
        }

    files_to_update: List[str] = ver_check.get("hot_update_files") or [
        "fake-gps/app.py",
        "fake-gps/gps_tunnel.py",
        "fake-gps/pikmin_automation.py",
        "fake-gps/pikmin_model.py",
        "fake-gps/debug_center.py",
        "fake-gps/map_layers.py",
        "fake-gps/github_sync.py",
        "web/index.html",
    ]
    if "version.json" not in files_to_update:
        files_to_update.append("version.json")

    downloaded: Dict[str, str] = {}
    ts = int(datetime.datetime.now().timestamp())

    for rel_path in files_to_update:
        clean_rel = rel_path.lstrip("/")
        url_path = f"/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}/{clean_rel}?t={ts}"
        status, content = _https_get_text("raw.githubusercontent.com", url_path, timeout=10.0)
        if status == 200 and content.strip():
            downloaded[clean_rel] = content
        elif clean_rel != "fake-gps/github_sync.py":
            return {"ok": False, "error": f"下載熱更新檔案 {clean_rel} 失敗 (HTTP {status})"}

    hotfix_root = config_dir / "hotfix"
    proj_root = get_project_root_dir(app_dir)
    updated_paths: List[str] = []

    for rel_path, content in downloaded.items():
        # 1. 寫入 hotfix 安全目錄
        hf_dest = hotfix_root / rel_path
        hf_dest.parent.mkdir(parents=True, exist_ok=True)
        with open(hf_dest, "w", encoding="utf-8") as f:
            f.write(content)
        updated_paths.append(str(hf_dest))

        # 2. 嘗試直接更新當前執行中的 App Bundle (Resources/)
        try:
            if rel_path.startswith("fake-gps/"):
                bundle_dest = app_dir / rel_path.replace("fake-gps/", "", 1)
            else:
                bundle_dest = app_dir / rel_path
            bundle_dest.parent.mkdir(parents=True, exist_ok=True)
            with open(bundle_dest, "w", encoding="utf-8") as f:
                f.write(content)
            if rel_path == "web/index.html":
                with open(app_dir / "index.html", "w", encoding="utf-8") as f2:
                    f2.write(content)
        except Exception:
            pass

        # 3. 若本機存在 ~/Documents/fake-gps 專案目錄，同步更新原始碼
        if proj_root.exists() and (proj_root / "fake-gps").exists():
            try:
                proj_dest = proj_root / rel_path
                proj_dest.parent.mkdir(parents=True, exist_ok=True)
                with open(proj_dest, "w", encoding="utf-8") as f:
                    f.write(content)
                if rel_path == "web/index.html":
                    with open(proj_root / "fake-gps" / "index.html", "w", encoding="utf-8") as f2:
                        f2.write(content)
            except Exception:
                pass

    return {
        "ok": True,
        "updated_version": ver_check.get("remote_version"),
        "updated_files_count": len(downloaded),
        "files": list(downloaded.keys()),
    }


def _load_local_landmarks_index(app_dir: Path) -> Dict[str, Any]:
    proj_root = get_project_root_dir(app_dir)
    candidates = [
        proj_root / "landmarks" / "index.json",
        app_dir.parent / "landmarks" / "index.json",
        app_dir / "landmarks" / "index.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get("categories"), list):
                        return data
            except Exception:
                pass
    return {"updated_at": "", "repo": f"{REPO_OWNER}/{REPO_NAME}", "categories": []}


def search_cloud_landmarks(
    app_dir: Path,
    query: str = "",
    center_lat: Optional[float] = None,
    center_lon: Optional[float] = None,
    calc_dist_fn=None,
) -> Dict[str, Any]:
    """
    從 GitHub 遠端 `landmarks/index.json` 與本機 `landmarks/index.json` 整合搜尋路標庫，
    支援關鍵字過濾（目錄名稱、標籤、單點路標名稱與描述）並依距離地圖中心由近到遠排序。
    """
    merged_by_id: Dict[str, Dict[str, Any]] = {}
    remote_loaded = False

    # 1. 先載入本機專案的 landmarks/index.json
    local_idx = _load_local_landmarks_index(app_dir)
    for cat in local_idx.get("categories", []):
        if isinstance(cat, dict) and cat.get("id"):
            merged_by_id[str(cat["id"])] = dict(cat, source="local_repo")

    # 2. 從 GitHub raw 載入雲端最新 landmarks/index.json
    ts = int(datetime.datetime.now().timestamp())
    raw_path = f"/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}/landmarks/index.json?t={ts}"
    try:
        status, text = _https_get_text("raw.githubusercontent.com", raw_path, timeout=5.5)
        if status == 200 and text.strip():
            remote_data = json.loads(text)
            if isinstance(remote_data, dict) and isinstance(remote_data.get("categories"), list):
                remote_loaded = True
                for cat in remote_data["categories"]:
                    if isinstance(cat, dict) and cat.get("id"):
                        cid = str(cat["id"])
                        merged_by_id[cid] = dict(cat, source="github_cloud")
    except Exception:
        pass

    q_lower = (query or "").strip().lower()
    results: List[Dict[str, Any]] = []

    for cid, cat in merged_by_id.items():
        cat_name = str(cat.get("name") or "未命名路標目錄")
        tags = [str(t) for t in (cat.get("tags") or [])]
        lms = cat.get("landmarks") or []
        if not isinstance(lms, list) or len(lms) == 0:
            continue

        cat_match = (
            not q_lower
            or q_lower in cat_name.lower()
            or any(q_lower in t.lower() for t in tags)
        )
        matched_points = []
        min_dist_m: Optional[float] = None

        for lm in lms:
            if not isinstance(lm, dict):
                continue
            try:
                lat = float(lm.get("lat"))
                lng = float(lm.get("lng") if lm.get("lng") is not None else lm.get("lon"))
            except Exception:
                continue

            pt_name = str(lm.get("name") or "")
            pt_desc = str(lm.get("desc") or "")
            pt_dist = None
            if center_lat is not None and center_lon is not None and calc_dist_fn is not None:
                pt_dist = round(float(calc_dist_fn(center_lat, center_lon, lat, lng)), 1)
                if min_dist_m is None or pt_dist < min_dist_m:
                    min_dist_m = pt_dist

            if cat_match or q_lower in pt_name.lower() or q_lower in pt_desc.lower():
                matched_points.append({
                    "id": lm.get("id") or "",
                    "name": pt_name,
                    "lat": lat,
                    "lng": lng,
                    "desc": pt_desc,
                    "distance_m": pt_dist,
                })

        if not cat_match and len(matched_points) == 0:
            continue

        results.append({
            "id": cid,
            "name": cat_name,
            "color": cat.get("color") or "#ec4899",
            "tags": tags,
            "source": cat.get("source", "github"),
            "updated_at": cat.get("updated_at") or "",
            "total_count": len(lms),
            "matched_count": len(matched_points),
            "min_distance_m": min_dist_m,
            "landmarks": lms,
            "matched_landmarks": matched_points[:15],
        })

    if center_lat is not None and center_lon is not None:
        results.sort(key=lambda x: x["min_distance_m"] if x["min_distance_m"] is not None else 1e12)

    return {
        "ok": True,
        "remote_loaded": remote_loaded,
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "results": results,
    }


def upload_landmark_category_to_github(
    app_dir: Path,
    category: Dict[str, Any],
    github_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    將指定的路標目錄寫入本機 Git 倉庫 `landmarks/index.json` 與 `landmarks/<id>.json`，
    若提供 GitHub Token 則直接透過 GitHub REST API 同步至雲端，同時嘗試透過本機 Git 提交推送。
    """
    proj_root = get_project_root_dir(app_dir)
    landmarks_dir = proj_root / "landmarks"
    landmarks_dir.mkdir(parents=True, exist_ok=True)
    index_path = landmarks_dir / "index.json"

    index_data = _load_local_landmarks_index(app_dir)
    cats: List[Dict[str, Any]] = index_data.setdefault("categories", [])

    cat_id = str(category.get("id") or f"cloud_{int(datetime.datetime.now().timestamp())}")
    cat_name = str(category.get("name") or "分享路標").strip()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    clean_points = []
    for idx, pt in enumerate(category.get("landmarks") or []):
        try:
            lat = float(pt.get("lat"))
            lng = float(pt.get("lng") if pt.get("lng") is not None else pt.get("lon"))
            clean_points.append({
                "id": str(pt.get("id") or f"pt_{idx + 1}"),
                "name": str(pt.get("name") or f"路標 {idx + 1}"),
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "desc": str(pt.get("desc") or ""),
            })
        except Exception:
            continue

    entry = {
        "id": cat_id,
        "name": cat_name,
        "color": category.get("color") or "#ec4899",
        "tags": category.get("tags") or [],
        "updated_at": now_str,
        "landmarks": clean_points,
    }

    existing_idx = next(
        (i for i, c in enumerate(cats) if c.get("id") == cat_id or c.get("name") == cat_name),
        None,
    )
    if existing_idx is not None:
        cats[existing_idx] = entry
    else:
        cats.append(entry)

    index_data["updated_at"] = now_str
    index_data["repo"] = f"{REPO_OWNER}/{REPO_NAME}"

    index_json_text = json.dumps(index_data, ensure_ascii=False, indent=2)
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_json_text)

    safe_file_stem = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", cat_name).strip("_") or cat_id
    single_path = landmarks_dir / f"{safe_file_stem}.json"
    with open(single_path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)

    # 若有設定 GitHub Personal Access Token，直接呼叫 GitHub API 寫入遠端倉庫
    token = (github_token or os.environ.get("GITHUB_TOKEN") or "").strip()
    api_pushed = False
    api_msg = ""

    if token:
        auth_headers = {"Authorization": f"Bearer {token}"}
        api_path = f"/repos/{REPO_OWNER}/{REPO_NAME}/contents/landmarks/index.json"
        st_get, cur_meta = _https_request_json("api.github.com", "GET", api_path, headers=auth_headers)
        sha = cur_meta.get("sha") if st_get == 200 and isinstance(cur_meta, dict) else None
        put_payload: Dict[str, Any] = {
            "message": f"📌 更新雲端路標庫: {cat_name} ({len(clean_points)} 點)",
            "content": base64.b64encode(index_json_text.encode("utf-8")).decode("ascii"),
            "branch": REPO_BRANCH,
        }
        if sha:
            put_payload["sha"] = sha
        st_put, put_res = _https_request_json("api.github.com", "PUT", api_path, payload=put_payload, headers=auth_headers)
        if st_put in (200, 201):
            api_pushed = True
            api_msg = "已透過 GitHub API 直接同步至雲端倉庫！"
        else:
            api_msg = f"GitHub API 回應 ({st_put}): {put_res.get('message', '')}"

    return {
        "ok": True,
        "category_id": cat_id,
        "category_name": cat_name,
        "points_count": len(clean_points),
        "saved_index_path": str(index_path),
        "saved_file_path": str(single_path),
        "api_pushed": api_pushed,
        "api_msg": api_msg,
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
    }
