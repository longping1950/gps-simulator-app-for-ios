"""
github_sync.py
GitHub Releases 版本檢查 (區分大更新 / 小更新 / Bug修復、支援跳過版本)、
透過 GitHub Release Assets (hotfix.zip) 執行免重裝熱更新 (不直接從程式碼分支拉取)，
以及雲端路標庫搜尋 / 上傳模組
"""

import base64
import datetime
import http.client
import io
import json
import os
from pathlib import Path
import re
import ssl
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import zipfile

REPO_OWNER = "longping1950"
REPO_NAME = "gps-simulator-app-for-ios"
REPO_BRANCH = "main"
CURRENT_FALLBACK_VERSION = "2.0"

ALLOWED_HOTFIX_FILES = {
    "fake-gps/app.py",
    "fake-gps/gps_tunnel.py",
    "fake-gps/pikmin_automation.py",
    "fake-gps/pikmin_model.py",
    "fake-gps/debug_center.py",
    "fake-gps/map_layers.py",
    "fake-gps/github_sync.py",
    "web/index.html",
    "version.json",
}


def _parse_version_tuple(ver_str: str) -> Tuple[int, int, int]:
    cleaned = re.sub(r"^[vV]\s*", "", str(ver_str or "0.0.0").strip())
    nums = [int(x) for x in re.findall(r"\d+", cleaned)]
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _normalize_version_str(ver_str: str) -> str:
    cleaned = re.sub(r"^[vV]\s*", "", str(ver_str or "").strip())
    return cleaned or CURRENT_FALLBACK_VERSION


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
        "User-Agent": "SIM-GPS-macOS-Updater/2.0",
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


def _https_get_bytes_follow_redirect(
    host: str,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    max_redirects: int = 5,
) -> Tuple[int, bytes]:
    ctx = _make_ssl_context()
    cur_host = host
    cur_path = path
    base_headers = {
        "User-Agent": "SIM-GPS-macOS-Updater/2.0",
        "Accept": "application/octet-stream, application/zip, */*",
    }
    if headers:
        base_headers.update(headers)

    for _ in range(max_redirects + 1):
        conn = http.client.HTTPSConnection(cur_host, timeout=timeout, context=ctx)
        try:
            conn.request("GET", cur_path, headers=base_headers)
            resp = conn.getresponse()
            status = resp.status
            location = resp.getheader("Location")
            data = resp.read()
        finally:
            conn.close()

        if status in (301, 302, 303, 307, 308) and location:
            parsed = urllib.parse.urlsplit(location)
            if parsed.netloc:
                cur_host = parsed.netloc
            cur_path = parsed.path or "/"
            if parsed.query:
                cur_path = f"{cur_path}?{parsed.query}"
            # 重導向至 CDN 時移除 Authorization header 以免被拒絕
            base_headers.pop("Authorization", None)
            continue
        return status, data

    return 500, b""


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
        "User-Agent": "SIM-GPS-macOS-Updater/2.0",
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
        "base_app_version": CURRENT_FALLBACK_VERSION,
        "update_type": "minor",
        "title": f"v{CURRENT_FALLBACK_VERSION}",
        "changelog": [],
    }


def _classify_release_update_type(
    local_ver: str,
    remote_ver: str,
    release_title: str,
    release_body: str,
) -> str:
    text_combined = f"{release_title}\n{release_body}".lower()
    if any(k in text_combined for k in ("[major]", "【大更新】", "[大更新]", "major update")):
        return "major"
    if any(k in text_combined for k in ("[bugfix]", "[patch]", "【bug修復】", "【修復】", "[bug修復]")):
        return "bugfix"
    if any(k in text_combined for k in ("[minor]", "【小更新】", "[小更新]")):
        return "minor"

    l_maj, l_min, l_pat = _parse_version_tuple(local_ver)
    r_maj, r_min, r_pat = _parse_version_tuple(remote_ver)
    if r_maj > l_maj:
        return "major"
    if r_min > l_min:
        return "minor"
    return "bugfix"


def _parse_release_notes_lines(body_str: str) -> List[str]:
    lines = []
    for raw_line in (body_str or "").splitlines():
        s = raw_line.strip()
        if not s or s.startswith("#"):
            continue
        s = re.sub(r"^[-*•]\s*", "", s)
        lines.append(s)
    return lines[:20]


def _find_hotfix_asset_in_release(release_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    assets = release_data.get("assets")
    if not isinstance(assets, list):
        return None
    # 優先尋找名稱含 hotfix 或 patch 的 .zip 附件
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").lower()
        if name.endswith(".zip") and ("hotfix" in name or "patch" in name or "update" in name):
            return asset
    # 若只有一個 .zip 且非完整 .app.zip，也可作為備選
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").lower()
        if name == "hotfix.zip":
            return asset
    return None


def check_github_version(
    app_dir: Path,
    config_dir: Path,
    skipped_version: Optional[str] = None,
    force_manual: bool = False,
) -> Dict[str, Any]:
    """
    透過 GitHub Releases API (/repos/<owner>/<repo>/releases/latest) 檢查最新正式發布版本。
    絕不直接從未發布的分支拉取，確保只有正式建立 Release 時才會通知使用者更新。
    """
    local_info = get_local_version_info(app_dir, config_dir)
    local_ver = _normalize_version_str(local_info.get("version") or CURRENT_FALLBACK_VERSION)

    api_path = f"/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"
    release_data: Optional[Dict[str, Any]] = None
    no_release_yet = False
    fetch_error: Optional[str] = None

    try:
        status, data = _https_request_json("api.github.com", "GET", api_path, timeout=7.0)
        if status == 200 and isinstance(data, dict) and data.get("tag_name"):
            release_data = data
        elif status == 404:
            no_release_yet = True
            fetch_error = "GitHub 倉庫尚未建立任何 Release 發布版本"
        else:
            fetch_error = f"GitHub Releases API 回應狀態碼 {status}"
    except Exception as e:
        fetch_error = str(e)

    default_release_url = "//".join(["https:", f"github.com/{REPO_OWNER}/{REPO_NAME}/releases"])

    if release_data is None:
        return {
            "ok": True if no_release_yet else False,
            "remote_reachable": no_release_yet,
            "no_release_yet": no_release_yet,
            "note": fetch_error,
            "local_version": local_ver,
            "remote_version": local_ver,
            "has_update": False,
            "should_prompt": False,
            "is_skipped": False,
            "update_type": "minor",
            "update_type_label": "目前已是最新版本",
            "title": local_info.get("title", f"v{local_ver}"),
            "release_title": local_info.get("title", f"v{local_ver}"),
            "changelog": local_info.get("changelog", []),
            "hot_update_supported": False,
            "can_hot_update": False,
            "download_url": default_release_url,
            "repo_owner": REPO_OWNER,
            "repo_name": REPO_NAME,
        }

    raw_tag = str(release_data.get("tag_name") or local_ver)
    remote_ver = _normalize_version_str(raw_tag)
    has_update = _parse_version_tuple(remote_ver) > _parse_version_tuple(local_ver)
    is_skipped = bool(skipped_version and _normalize_version_str(skipped_version) == remote_ver)

    release_title = str(release_data.get("name") or f"v{remote_ver} 版本更新").strip()
    release_body = str(release_data.get("body") or "")
    changelog = _parse_release_notes_lines(release_body)

    update_type = _classify_release_update_type(local_ver, remote_ver, release_title, release_body)
    type_labels = {
        "major": "大更新",
        "minor": "小更新",
        "bugfix": "Bug 修復",
    }
    update_type_label = type_labels.get(update_type, "版本更新")

    hotfix_asset = _find_hotfix_asset_in_release(release_data)
    can_hot_update = (update_type != "major") and (hotfix_asset is not None)
    html_url = str(release_data.get("html_url") or default_release_url)

    return {
        "ok": True,
        "remote_reachable": True,
        "no_release_yet": False,
        "local_version": local_ver,
        "remote_version": remote_ver,
        "tag_name": raw_tag,
        "has_update": has_update,
        "should_prompt": has_update and (force_manual or not is_skipped),
        "is_skipped": is_skipped,
        "update_type": update_type,
        "update_type_label": update_type_label,
        "release_date": str(release_data.get("published_at") or "")[:10],
        "title": release_title,
        "release_title": release_title,
        "changelog": changelog,
        "hot_update_supported": can_hot_update,
        "can_hot_update": can_hot_update,
        "hotfix_asset_name": hotfix_asset.get("name") if hotfix_asset else None,
        "hotfix_asset_url": hotfix_asset.get("browser_download_url") if hotfix_asset else None,
        "download_url": html_url,
        "repo_owner": REPO_OWNER,
        "repo_name": REPO_NAME,
    }


def perform_github_hot_update(app_dir: Path, config_dir: Path) -> Dict[str, Any]:
    """
    從 GitHub 最新 Release 的附件 (hotfix.zip / hotfix-vX.Y.Z.zip) 下載並解壓縮套用熱更新：
    絕不直接從程式碼倉庫分支拉取未打包檔案。
    """
    ver_check = check_github_version(app_dir, config_dir, force_manual=True)
    if not ver_check.get("remote_reachable"):
        return {"ok": False, "error": ver_check.get("note") or "無法連線至 GitHub Releases"}

    if ver_check.get("no_release_yet"):
        return {"ok": False, "error": "GitHub 倉庫目前尚未發布任何 Release 版本"}

    if ver_check.get("update_type") == "major":
        return {
            "ok": False,
            "error": "此版本屬於「大版本更新 (Major)」，請點擊前往 GitHub Release 頁面下載完整新版 App。",
        }

    asset_url = ver_check.get("hotfix_asset_url")
    if not asset_url:
        return {
            "ok": False,
            "error": "此 GitHub Release 未包含熱更新套件附件 (hotfix.zip)，請前往 GitHub Release 頁面下載更新。",
        }

    parsed_url = urllib.parse.urlsplit(str(asset_url))
    dl_host = parsed_url.netloc or "github.com"
    dl_path = parsed_url.path or "/"
    if parsed_url.query:
        dl_path = f"{dl_path}?{parsed_url.query}"

    status, zip_bytes = _https_get_bytes_follow_redirect(dl_host, dl_path, timeout=25.0)
    if status != 200 or not zip_bytes:
        return {"ok": False, "error": f"下載 Release 熱更新套件失敗 (HTTP {status})"}

    downloaded: Dict[str, str] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                norm_member = member.lstrip("/")
                # 支援壓縮包內含頂層目錄或直接相對路徑
                matched_rel = None
                if norm_member in ALLOWED_HOTFIX_FILES:
                    matched_rel = norm_member
                else:
                    parts = norm_member.split("/", 1)
                    if len(parts) == 2 and parts[1] in ALLOWED_HOTFIX_FILES:
                        matched_rel = parts[1]
                if matched_rel:
                    raw_bytes = zf.read(member)
                    downloaded[matched_rel] = raw_bytes.decode("utf-8")
    except Exception as e:
        return {"ok": False, "error": f"解析 Release 熱更新壓縮包 (hotfix.zip) 失敗: {e}"}

    if not downloaded:
        return {"ok": False, "error": "hotfix.zip 內未找到有效的熱更新程式檔案"}

    # 確保更新後的 version.json 反映最新 Release 版本號
    remote_ver = str(ver_check.get("remote_version") or CURRENT_FALLBACK_VERSION)
    ver_json_obj = {
        "version": remote_ver,
        "base_app_version": CURRENT_FALLBACK_VERSION,
        "update_type": ver_check.get("update_type", "minor"),
        "update_type_label": ver_check.get("update_type_label", "小更新"),
        "release_date": ver_check.get("release_date") or datetime.datetime.now().strftime("%Y-%m-%d"),
        "title": ver_check.get("release_title") or f"v{remote_ver}",
        "changelog": ver_check.get("changelog") or [],
        "hot_update_supported": True,
        "repo_owner": REPO_OWNER,
        "repo_name": REPO_NAME,
    }
    downloaded["version.json"] = json.dumps(ver_json_obj, ensure_ascii=False, indent=2)

    hotfix_root = config_dir / "hotfix"
    proj_root = get_project_root_dir(app_dir)

    for rel_path, content in downloaded.items():
        # 1. 寫入 ~/Library/Application Support/fake-gps/hotfix/
        hf_dest = hotfix_root / rel_path
        hf_dest.parent.mkdir(parents=True, exist_ok=True)
        with open(hf_dest, "w", encoding="utf-8") as f:
            f.write(content)

        # 2. 更新當前執行中的 App Bundle (Resources/)
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

        # 3. 若本機存在 ~/Documents/fake-gps 專案目錄，同步更新
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
        "new_version": remote_ver,
        "updated_version": remote_ver,
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
    支援關鍵字過濾並依距離目前地圖中心由近到遠排序。
    """
    merged_by_id: Dict[str, Dict[str, Any]] = {}
    remote_loaded = False

    # 1. 先載入本機專案的 landmarks/index.json
    local_idx = _load_local_landmarks_index(app_dir)
    for cat in local_idx.get("categories", []):
        if isinstance(cat, dict) and cat.get("id"):
            merged_by_id[str(cat["id"])] = dict(cat, source="local")

    # 2. 從 GitHub 載入雲端最新 landmarks/index.json
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
                        merged_by_id[cid] = dict(cat, source="github")
    except Exception:
        pass

    q_lower = (query or "").strip().lower()
    results: List[Dict[str, Any]] = []

    for cid, cat in merged_by_id.items():
        cat_name = str(cat.get("name") or "未命名路標目錄")
        cat_desc = str(cat.get("description") or "")
        tags = [str(t) for t in (cat.get("tags") or [])]
        lms = cat.get("landmarks") or []
        if not isinstance(lms, list) or len(lms) == 0:
            continue

        cat_match = (
            not q_lower
            or q_lower in cat_name.lower()
            or q_lower in cat_desc.lower()
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

        desc_summary = cat_desc or (", ".join(tags) if tags else "")
        results.append({
            "id": cid,
            "name": cat_name,
            "description": desc_summary,
            "color": cat.get("color") or "#ec4899",
            "tags": tags,
            "source": cat.get("source", "github"),
            "updated_at": cat.get("updated_at") or "",
            "point_count": len(lms),
            "total_count": len(lms),
            "matched_count": len(matched_points),
            "distance_m": min_dist_m,
            "min_distance_m": min_dist_m,
            "landmarks": lms,
            "matched_landmarks": matched_points[:15],
        })

    if center_lat is not None and center_lon is not None:
        results.sort(key=lambda x: x["distance_m"] if x["distance_m"] is not None else 1e12)

    return {
        "ok": True,
        "remote_loaded": remote_loaded,
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "results": results,
    }


def upload_landmark_category_to_github(
    app_dir: Path,
    category: Dict[str, Any],
    description: str = "",
    github_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    將指定的路標目錄寫入本機 Git 倉庫 `landmarks/index.json` 與 `landmarks/<name>.json`，
    若提供 GitHub Token 則直接透過 GitHub REST API 同步至雲端。
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

    tags = list(category.get("tags") or [])
    if description and description not in tags:
        tags.append(description)

    entry = {
        "id": cat_id,
        "name": cat_name,
        "description": description or category.get("description") or "",
        "color": category.get("color") or "#ec4899",
        "tags": tags,
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

    token = (github_token or os.environ.get("GITHUB_TOKEN") or "").strip()
    api_pushed = False
    api_msg = "已寫入專案 landmarks/index.json，執行 git push 即可同步至 GitHub"

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
        "pushed_to_github": api_pushed,
        "api_pushed": api_pushed,
        "hint": api_msg,
        "api_msg": api_msg,
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
    }
