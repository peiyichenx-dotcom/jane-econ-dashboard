# Jane 經濟局勢儀表板

手機一眼判讀世界經濟局勢。指標選自 Jane（旺來幫）12 冊文章合輯的觀察框架（詳見 `經濟儀表板_指標藍圖.md`）。

## 架構

- `index.html` — mobile-first PWA 儀表板（總評燈號 → 資金流向 → 資產矩陣 → 六大指標分區）
- `fetch_data.py` — 抓取 25 個指標並運算訊號/總評，輸出 `data.json`（來源：FRED、Yahoo、Stooq、CNN、CoinGecko、multpl）
- `.github/workflows/update.yml` — 每天台北 06:30 自動更新
- 本地測試：`python3 fetch_data.py --mock`

## 手機安裝

開啟網址 → 瀏覽器選單 →「加入主畫面」→ 變成 app 圖示。

## 維護

- 新增指標：在 `fetch_data.py` 加一個 `grab()` ＋ 一個 `add()`，前端自動渲染。
- FOMC 日程每年更新一次（`FOMC_2026` 清單）。
- 免責：整理自 Jane 文章之學習用途，非投資建議。
