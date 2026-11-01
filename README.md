# 股市尋寶

台股盤後選股工具。GitHub Actions 每個交易日晚上自動從 FinMind／Yahoo Finance 抓資料，並發布到 GitHub Pages。

- `index.html`：選股網頁（條件篩選、K 線圖、回測、選股邏輯）
- `taojin_data.py`：盤後資料腳本
- `lists/ftse.txt`：新加坡富時台灣指數成分股（需自行維護，每行一個代號）
- `.github/workflows/update.yml`：自動排程（台灣時間週一～週五 22:30）
- `status.json`：最近一次更新的資料日與檔數

## 手動更新
Actions → 盤後資料更新 → Run workflow
