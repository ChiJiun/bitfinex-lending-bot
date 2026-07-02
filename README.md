# Bitfinex 綠葉放貸機器人 🍃

自動化 Bitfinex funding(放貸)掛單,部署在 GitHub Actions 上長期免費執行。

## 運作方式

每 30 分鐘(可調)自動執行一次:

1. 抓取市場利率(FRR 與掛單簿最佳利率)
2. 取消掛超過 60 分鐘未成交的舊單,釋放資金
3. 把 funding 錢包可用資金分成多筆,**階梯掛單**:第一筆貼著市場利率(容易成交),後面幾筆利率遞增(等利率波動時吃到高點)
4. 利率飆高時自動拉長放貸天數,鎖住高利率(預設:日利率 ≥ 0.05% 鎖 30 天、≥ 0.1% 鎖 120 天)

## 部署步驟

### 1. 建立 Bitfinex API 金鑰

到 [Bitfinex API Keys](https://setting.bitfinex.com/api) 建立金鑰,權限**只勾**:

- Margin Funding → **Get funding statuses and info**(讀取)
- Margin Funding → **Offer, cancel and close funding**(下單)

⚠️ **不要**勾提領(Withdraw)或交易權限,被盜也偷不走錢。

### 2. 推上 GitHub

用 GitHub Desktop 把這個資料夾發佈成 repo:

- **私人 repo**:程式碼不公開,每月 2000 免費分鐘 → 排程請維持每 30 分鐘一次
- **公開 repo**:分鐘數無上限,可把 [.github/workflows/lending-bot.yml](.github/workflows/lending-bot.yml) 裡的 cron 改成 `*/10 * * * *`。API 金鑰放在 Secrets 不會外洩

### 3. 設定 Secrets

GitHub repo → Settings → Secrets and variables → Actions → New repository secret:

| 名稱 | 內容 |
|---|---|
| `BFX_API_KEY` | API Key |
| `BFX_API_SECRET` | API Secret |

### 4. 測試

repo → Actions → Bitfinex Lending Bot → **Run workflow**,勾選 `dry_run` 先跑一次,確認 log 顯示的掛單計畫合理,再取消勾選正式執行。之後排程就會自動跑。

## 本機測試

```
pip install -r requirements.txt
```

在專案目錄建立 `.env`(已被 .gitignore 排除):

```
BFX_API_KEY=你的key
BFX_API_SECRET=你的secret
DRY_RUN=1
```

執行 `python bot.py`。

## 設定檔 config.json

所有利率單位都是**每日利率 %**(0.02 = 0.02%/日 ≈ 年化 7.3%)。

| 欄位 | 說明 |
|---|---|
| `stale_offer_minutes` | 掛單超過幾分鐘未成交就取消重掛 |
| `enabled` | 是否啟用該幣別(內建 USD、UST,可自行新增) |
| `min_offer_size` | 最小掛單金額(Bitfinex 規定 USD/UST 最少 150) |
| `max_offer_size` | 單筆掛單上限,資金多時會自動拆更多筆 |
| `num_ladder_steps` | 階梯層數(資金分幾筆) |
| `ladder_step_pct` | 每層利率遞增幅度(% 相對值,8 = 每層比基準高 8%) |
| `min_daily_rate_pct` | 最低可接受日利率,低於此不掛 |
| `reserve_amount` | 保留不掛出的金額 |
| `period_rules` | 利率 → 放貸天數對照,利率越高鎖越久 |
