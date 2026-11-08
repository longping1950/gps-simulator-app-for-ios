# SIM-GPS (v2.0) — macOS 專屬 iOS 虛擬定位與皮克敏自動化控制器

`SIM-GPS` 是一款運行於 macOS (Apple Silicon / Intel) 的原生 SwiftUI + 內嵌獨立 Python (`EmbeddedPython`) 應用程式，透過 `pymobiledevice3` 建立 iOS 17 / 18 / 26+ 開發者定位通道（支援 USB 與相同網路下的 Wi-Fi 無線連線），整合地圖選點巡航、路線庫、路標庫、多裝置同步控制與 Pikmin Bloom 畫面辨識自動化守護功能。

---

## ✨ 核心功能亮點

1. **📱 多裝置連線與獨立 60 km/h 平滑返航**
   - 支援 USB 與 Wi-Fi 無線連線，初次連線自動掛載 Developer Disk Image (DDI)。
   - 支援多台 iPhone 同步巡航，單台裝置中斷或觸發保護時可獨立以 `60 km/h` 平滑返回真實起點，不中斷其他裝置行程。
2. **🌸 皮克敏種花效率幾何模型與多段調速**
   - 內建 5 階段隊伍規模（1~9 隻至 40 隻滿編）自動調速、極速種花 `18 km/h`、高速移動 `60 km/h`、跨區趕路 `800 km/h`，以及 `0 ~ 2000 km/h` 無段滑桿。
   - 即時試算單趟行程時間、消耗花瓣數、預估總產花量與 5 分半（330 秒）循環冷卻警告。
3. **📂 路線庫、路標庫與雲端路標共享**
   - **路線庫**：支援單次巡航、無限循環回原點、指定圈數循環；可對任意航點設定 **「⏸️ 抵達停留」**（到達時發送 macOS 通知並自動暫停停留）。
   - **路標庫**：支援匯入通用格式（`.gpx` / `.json` / `.geojson` / `.csv`）在地圖上標記路標（不視為路徑點），每個路標目錄皆可獨立開關顯示。
   - **GitHub 雲端路標庫**：支援於 App 內搜尋雲端路標並一鍵匯入，或將本機路標目錄匯出分享至 `landmarks/`。
4. **🖱️ App 內右鍵快捷選單與防誤觸體驗**
   - 在地圖任意位置按右鍵可直接「新增路徑點」、「新增為抵達停留點」、「新增路標（含子選單選擇現有路標目錄或新建目錄）」或「直接瞬移」。
   - 對已存在的路徑點或路標按右鍵可直接移除或切換停留狀態。
   - 頂部搜尋列以目前地圖中心優先搜尋並保留歷史搜尋紀錄（點擊搜尋框立即展開）；所有文字輸入框皆支援 **連按兩次 Enter 確認**，防止中文輸入法選字誤觸。
5. **🤖 皮克敏自動化守護與溫度通知**
   - 支援低電量自動停止種花並返航（或直接關閉遊戲）、機身過熱 / 系統熱態通知、遊戲閃退自動重啟、金幣達上限自動停種返航。
   - 連線後延遲 4.5 秒並連續雙重採樣 `Sysmontap` CPU 與功率負載確認遊戲是否在前景，避免開著遊戲時誤跳提醒視窗。
6. **🔄 GitHub Releases 版本驗證與小版本免重裝熱更新**
   - 透過 GitHub Releases API 檢查正式發布版本，自動區分 **大更新 (`major`)**、**小更新 (`minor`)** 與 **Bug 修復 (`bugfix`)**，並支援「跳過此版本」。
   - 小更新與 Bug 修復可直接從 GitHub Release 下載 `hotfix.zip` 附件一鍵完成熱更新，無須重新編譯或重裝 `sim-gps.app`。

---

## 🐍 EmbeddedPython 建立與配置方式

為了讓打包出的 `sim-gps.app` 能在任何 Mac 上獨立運行（不依賴使用者電腦是否安裝 Python 或 Homebrew），專案採用可攜式獨立 Python 環境（`python-build-standalone`）放置於專案根目錄的 `EmbeddedPython/` 資料夾中。

> 💡 **為什麼 GitHub 倉庫不包含 `EmbeddedPython/`？**
> 因為 `EmbeddedPython/` 包含完整 Python 3.11 執行檔、動態連結庫（`.dylib`）與所有第三方套件，總大小約 150MB ~ 250MB，已列入 `.gitignore`。首次從 GitHub Clone 專案下來編譯前，請依下列步驟建立 `EmbeddedPython/`。

### 步驟 1：下載獨立可攜式 Python 3.11 (`python-build-standalone`)

請依您的 Mac 處理器架構（Apple Silicon `aarch64` 或 Intel `x86_64`），從 `indygreg/python-build-standalone` 的 Releases 下載對應的 `install_only` 壓縮包，並解壓縮至專案根目錄下的 `EmbeddedPython/`（使路徑結構為 `EmbeddedPython/python/bin/python3`）：

```bash
# 1. 進入專案根目錄
cd /Users/longping-macbookair/Documents/fake-gps

# 2. 建立 EmbeddedPython 目錄
mkdir -p EmbeddedPython

# 3. 將下載的 python-build-standalone (cpython-3.11.x+...-aarch64-apple-darwin-install_only.tar.gz) 解壓縮至 EmbeddedPython/
# 解壓後會自動產生 EmbeddedPython/python/ 資料夾
tar -xzf ~/Downloads/cpython-3.11.*-apple-darwin-install_only.tar.gz -C EmbeddedPython/

# 4. 驗證內嵌 Python 執行檔是否正常運作
./EmbeddedPython/python/bin/python3 --version
```

*(備註：若您本機已裝有 `conda` 或 `uv`，也可使用 `conda create --prefix ./EmbeddedPython/python python=3.11 -y` 建立相同結構的獨立環境，但跨電腦分發時建議使用 `python-build-standalone` 的 `install_only` 版本以避免寫死本機絕對路徑。)*

### 步驟 2：安裝專案所需的 Python 套件至 `EmbeddedPython`

確認 `./EmbeddedPython/python/bin/python3` 可執行後，使用內嵌 Python 的 `pip` 安裝後端與 iOS 定位通道所需的相依套件：

```bash
# 升級 pip 與基礎打包工具
./EmbeddedPython/python/bin/python3 -m pip install --upgrade pip setuptools

# 安裝核心定位通道、Web 後端與影像處理套件
./EmbeddedPython/python/bin/python3 -m pip install \
    pymobiledevice3 \
    fastapi \
    "uvicorn[standard]" \
    pydantic \
    pillow \
    requests \
    certifi \
    psutil \
    aiofiles
```

### 步驟 3：清除 macOS 隔離屬性與對 `.dylib` / `.so` 重新簽章（Xcode 打包必做）

macOS 與 Xcode 在打包 App 時，會驗證 `EmbeddedPython` 內所有執行檔與二進位擴充模組（如 `cryptography`、`PIL`、`qh3`、`pydantic_core`）的 Code Signature。建立或更新套件後，請執行以下指令避免 Xcode 編譯或啟動時報錯：

```bash
# 1. 移除隔離屬性 (Quarantine attribute)
xattr -cr ./EmbeddedPython

# 2. 對所有 .dylib 與 .so 動態庫以及 python3 執行檔進行 Ad-hoc 本地簽章
find ./EmbeddedPython -type f \( -name "*.dylib" -o -name "*.so" \) -exec codesign --force --sign - {} \;
codesign --force --sign - ./EmbeddedPython/python/bin/python3
codesign --force --sign - ./EmbeddedPython/python/bin/python3.11
```

### 步驟 4：確認目錄結構

完成後，請確認 `EmbeddedPython` 的目錄結構如下（`MyApp.swift` 啟動時會自動尋找 `Contents/Resources/EmbeddedPython/python/bin/python3`）：

```text
fake-gps/
├── EmbeddedPython/
│   └── python/
│       ├── bin/
│       │   ├── python3
│       │   └── python3.11
│       ├── include/
│       ├── lib/
│       │   ├── libpython3.11.dylib
│       │   └── python3.11/
│       │       └── site-packages/
│       │           ├── pymobiledevice3/
│       │           ├── fastapi/
│       │           ├── uvicorn/
│       │           ├── PIL/
│       │           └── ...
│       └── share/
├── fake-gps/               # Swift 主程式與 Python 後端模組
├── web/                    # 前端地圖與控制介面 (index.html)
├── landmarks/              # 雲端與本機共享路標庫索引
├── build_release.py        # 一鍵打包 GitHub Release 熱更新附件腳本
└── fake-gps.xcodeproj/     # Xcode 專案檔 (設定輸出為 sim-gps.app)
```

---

## 🛠️ Xcode 編譯與匯出 `sim-gps.app`

本專案已在 Xcode Build Settings 中將 `PRODUCT_NAME` 設定為 **`sim-gps`**（顯示名稱為 `SIM-GPS`），因此無論是直接執行或 Archive 匯出，產生的應用程式檔名皆為 **`sim-gps.app`**：

1. 使用 Xcode 開啟 `fake-gps.xcodeproj`。
2. 在上方 Scheme 選擇 **`fake-gps` ➔ `My Mac`**。
3. 點擊 **`Product` ➔ `Build` (`Cmd + B`)** 或 **`Run` (`Cmd + R`)** 即可編譯並啟動 `sim-gps.app`。
4. 若要匯出獨立應用程式檔：
   - 點擊選單 **`Product` ➔ `Archive`**。
   - 在彈出的 Organizer 視窗點選 **`Distribute App` ➔ `Custom` ➔ `Copy App`**，匯出後即為獨立的 **`sim-gps.app`**。

---

## 🚀 發布 GitHub Release 與熱更新包 (`hotfix.zip`)

當您修改了 Python 後端（`fake-gps/*.py`）或前端介面（`web/index.html`）並希望發布更新時：

1. **一鍵打包 `hotfix.zip`**：
   ```bash
   # 語法：python3 build_release.py [新版本號] [major|minor|bugfix] "[更新標題]"
   python3 build_release.py 2.0.1 bugfix "修復小問題與介面優化"
   ```
   執行後會自動更新 `version.json` 並在 `dist/` 目錄下產出 `dist/hotfix.zip` 與 `dist/hotfix-v2.0.1.zip`。
2. **前往 GitHub 建立 Release**：
   - 在 GitHub 倉庫點選 **Releases ➔ Draft a new release**。
   - 建立 Tag（例如 `v2.0.1`），填寫標題與更新內容。
   - 將 `dist/hotfix.zip` 拖曳至下方 **Attach binaries** 區塊上傳，點擊 **Publish release**。
   - 使用者在 App 左上角點擊版本按鈕（或啟動 App 時）即可自動偵測並一鍵完成熱更新。
