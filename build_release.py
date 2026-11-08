"""
build_release.py
一鍵打包 GitHub Release 熱更新套件 (hotfix.zip) 工具

使用方式：
1. 直接打包目前 version.json 設定的版本：
   python3 build_release.py

2. 指定新版本號、更新類型 (minor / bugfix / major) 與更新標題打包：
   python3 build_release.py 2.0.1 bugfix "修復小問題与介面優化"
"""

import datetime
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT_DIR = Path(__file__).resolve().parent
VERSION_FILE = ROOT_DIR / "version.json"
DIST_DIR = ROOT_DIR / "dist"

HOTFIX_FILES = [
    "fake-gps/app.py",
    "fake-gps/gps_tunnel.py",
    "fake-gps/pikmin_automation.py",
    "fake-gps/pikmin_model.py",
    "fake-gps/debug_center.py",
    "fake-gps/map_layers.py",
    "fake-gps/github_sync.py",
    "web/index.html",
    "version.json",
]


def main() -> None:
    if VERSION_FILE.exists():
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            ver_data = json.load(f)
    else:
        ver_data = {
            "version": "2.0",
            "base_app_version": "2.0",
            "update_type": "minor",
            "title": "v2.0 正式版",
            "changelog": [],
            "hot_update_supported": True,
            "hot_update_files": HOTFIX_FILES[:-1],
            "repo_owner": "longping1950",
            "repo_name": "gps-simulator-app-for-ios",
        }

    # 若命令列有傳入新版本號，自動更新 version.json
    if len(sys.argv) >= 2 and sys.argv[1].strip():
        new_ver = re.sub(r"^[vV]\s*", "", sys.argv[1].strip())
        ver_data["version"] = new_ver
    if len(sys.argv) >= 3 and sys.argv[2].strip().lower() in ("major", "minor", "bugfix"):
        u_type = sys.argv[2].strip().lower()
        ver_data["update_type"] = u_type
        ver_data["hot_update_supported"] = (u_type != "major")
    if len(sys.argv) >= 4 and sys.argv[3].strip():
        ver_data["title"] = sys.argv[3].strip()

    ver_data["release_date"] = datetime.datetime.now().strftime("%Y-%m-%d")
    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        json.dump(ver_data, f, ensure_ascii=False, indent=2)

    cur_ver = str(ver_data.get("version") or "2.0")
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    zip_standard = DIST_DIR / "hotfix.zip"
    zip_versioned = DIST_DIR / f"hotfix-v{cur_ver}.zip"

    for z_path in (zip_standard, zip_versioned):
        with zipfile.ZipFile(z_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rel_path in HOTFIX_FILES:
                abs_path = ROOT_DIR / rel_path
                if abs_path.exists():
                    zf.write(abs_path, arcname=rel_path)

    u_type = ver_data.get("update_type", "minor")
    tag_prefix = {"major": "[大更新]", "minor": "[小更新]", "bugfix": "[Bug修復]"}.get(u_type, "[小更新]")
    notes_lines = [f"- {line}" for line in (ver_data.get("changelog") or ["功能優化與錯誤修復"])]

    print("=" * 64)
    print(f"✅ 已完成 v{cur_ver} Release 熱更新套件打包！")
    print(f"📦 熱更新附件檔案位置：{zip_standard}")
    print(f"📦 版本備份附件位置：{zip_versioned}")
    print("-" * 64)
    print("🚀 GitHub 建立 Release 步驟 (只需 30 秒)：")
    print("1. 打開您的 GitHub 倉庫頁面，點選右側欄位的「Releases」➔「Draft a new release」(或 Create a new release)")
    print(f"2. 點擊「Choose a tag」，輸入：v{cur_ver} 並點擊「Create new tag: v{cur_ver} on publish」")
    print(f"3. Release title (標題) 填入：{tag_prefix} {ver_data.get('title', f'v{cur_ver} 更新')}")
    print("4. 在下方說明欄 (Describe this release) 貼上更新內容：")
    for line in notes_lines:
        print(f"   {line}")
    print(f"5. 將產生的【dist/hotfix.zip】拖曳到最下方的「Attach binaries by dropping them here」區塊上傳")
    print("6. 點擊綠色按鈕「Publish release」即完成發布！")
    print("=" * 64)


if __name__ == "__main__":
    main()
