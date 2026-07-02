#!/usr/bin/env python3
"""本機收益儀表板:即時顯示 Bitfinex 放貸成交狀況與利潤。

用法:
    py dashboard.py     # 啟動後自動開啟 http://localhost:8899

需要 .env(或環境變數)裡的 BFX_API_KEY / BFX_API_SECRET,讀取權限即可。
資料直接從 Bitfinex API 取得,不經過任何第三方伺服器。
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import bot

PORT = 8899

# funding credit/loan 欄位索引 (https://docs.bitfinex.com/reference/rest-auth-funding-credits)
C_AMOUNT, C_RATE, C_PERIOD, C_MTS_OPENING = 5, 11, 12, 13
# ledger 欄位索引 (https://docs.bitfinex.com/reference/rest-auth-ledgers)
L_MTS, L_AMOUNT, L_DESCRIPTION = 3, 5, 8
LEDGER_CATEGORY_FUNDING_PAYMENT = 28


def day_str(ms: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def position_row(entry: list, used: bool) -> dict:
    rate = float(entry[C_RATE] or 0)
    period = int(entry[C_PERIOD] or 0)
    opened = entry[C_MTS_OPENING] or 0
    return {
        "amount": float(entry[C_AMOUNT] or 0),
        "rate_pct": rate * 100,
        "apr_pct": rate * 36500,
        "period": period,
        "opened_ms": opened,
        "expires_ms": opened + period * 86_400_000 if opened else None,
        "used": used,
    }


def funding_payments(client: bot.Bitfinex, currency: str) -> list:
    ledgers = client.auth(f"auth/r/ledgers/{currency}/hist",
                          {"category": LEDGER_CATEGORY_FUNDING_PAYMENT, "limit": 2500})
    if ledgers:
        return ledgers
    # 保險起見:類別篩選沒結果時,改抓全部再用描述過濾
    everything = client.auth(f"auth/r/ledgers/{currency}/hist", {"limit": 2500})
    return [l for l in everything
            if l[L_DESCRIPTION] and "funding payment" in l[L_DESCRIPTION].lower()]


def summary(client: bot.Bitfinex, currency: str) -> dict:
    symbol = f"f{currency}"

    balance = available = 0.0
    for w in client.auth("auth/r/wallets"):
        if w[bot.W_TYPE] == "funding" and w[bot.W_CURRENCY] == currency:
            balance = float(w[bot.W_BALANCE] or 0)
            available = float(w[bot.W_AVAILABLE] or 0)

    offers = [{
        "amount": float(o[bot.O_AMOUNT] or 0),
        "rate_pct": float(o[bot.O_RATE] or 0) * 100,
        "apr_pct": float(o[bot.O_RATE] or 0) * 36500,
        "period": int(o[bot.O_PERIOD] or 0),
        "created_ms": o[bot.O_MTS_CREATED],
    } for o in client.auth(f"auth/r/funding/offers/{symbol}")]

    lent = [position_row(c, used=True) for c in client.auth(f"auth/r/funding/credits/{symbol}")]
    lent += [position_row(l, used=False) for l in client.auth(f"auth/r/funding/loans/{symbol}")]
    lent.sort(key=lambda p: p["rate_pct"], reverse=True)
    lent_total = sum(p["amount"] for p in lent)
    weighted_rate = (sum(p["rate_pct"] * p["amount"] for p in lent) / lent_total) if lent_total else 0.0

    daily = defaultdict(float)
    for entry in funding_payments(client, currency):
        daily[day_str(entry[L_MTS])] += float(entry[L_AMOUNT] or 0)

    now = time.time() * 1000
    today = day_str(now)
    cutoff7 = day_str(now - 7 * 86_400_000)
    cutoff30 = day_str(now - 30 * 86_400_000)
    sum7 = sum(v for d, v in daily.items() if d > cutoff7)
    sum30 = sum(v for d, v in daily.items() if d > cutoff30)
    chart_days = sorted(daily)[-60:]

    return {
        "currency": currency,
        "balance": balance,
        "available": available,
        "lent": {"total": lent_total, "weighted_rate_pct": weighted_rate,
                 "apr_pct": weighted_rate * 365, "items": lent},
        "offers": offers,
        "earnings": {
            "today": daily.get(today, 0.0),
            "d7": sum7,
            "d30": sum30,
            "window_total": sum(daily.values()),
            "est_apr_pct": (sum30 / 30 * 365 / balance * 100) if balance else 0.0,
            "daily": [{"date": d, "amount": daily[d]} for d in chart_days],
        },
    }


class Handler(BaseHTTPRequestHandler):
    client: bot.Bitfinex
    currencies: list[str]

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        try:
            if url.path == "/":
                self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/config":
                self._json(200, {"currencies": self.currencies})
            elif url.path == "/api/summary":
                ccy = parse_qs(url.query).get("ccy", [self.currencies[0]])[0]
                self._json(200, summary(self.client, ccy))
            else:
                self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": str(exc)})

    def log_message(self, *args) -> None:  # 安靜模式
        pass


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>綠葉放貸儀表板 🍃</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root { --green: #16a34a; --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: "Segoe UI", "Microsoft JhengHei", sans-serif; background: var(--bg); color: var(--text); }
  header { display: flex; align-items: center; gap: 12px; padding: 16px 24px; border-bottom: 1px solid #334155; }
  h1 { font-size: 20px; margin: 0; flex: 1; }
  select, button { background: var(--card); color: var(--text); border: 1px solid #334155; border-radius: 8px; padding: 8px 12px; font-size: 14px; cursor: pointer; }
  main { max-width: 1100px; margin: 0 auto; padding: 24px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
  .card { background: var(--card); border-radius: 12px; padding: 16px; }
  .card .label { color: var(--muted); font-size: 13px; }
  .card .value { font-size: 22px; font-weight: 600; margin-top: 6px; }
  .card .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .green { color: var(--green); }
  section { margin-top: 28px; }
  h2 { font-size: 16px; color: var(--muted); font-weight: 600; }
  table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 12px; overflow: hidden; }
  th, td { padding: 10px 14px; text-align: right; font-size: 14px; }
  th { background: #263548; color: var(--muted); font-weight: 500; }
  th:first-child, td:first-child { text-align: left; }
  tr + tr td { border-top: 1px solid #2c3d52; }
  .empty { color: var(--muted); padding: 18px; text-align: center; }
  #chartWrap { background: var(--card); border-radius: 12px; padding: 16px; height: 280px; }
  #error { color: #f87171; margin-top: 12px; white-space: pre-wrap; }
  .badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #14532d; color: #86efac; }
  .badge.idle { background: #334155; color: var(--muted); }
  footer { color: var(--muted); font-size: 12px; text-align: center; padding: 20px; }
</style>
</head>
<body>
<header>
  <h1>🍃 綠葉放貸儀表板</h1>
  <select id="ccy"></select>
  <button onclick="load()">重新整理</button>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="label">Funding 錢包總額</div><div class="value" id="balance">–</div><div class="sub" id="available"></div></div>
    <div class="card"><div class="label">放貸中(已成交)</div><div class="value green" id="lentTotal">–</div><div class="sub" id="lentRate"></div></div>
    <div class="card"><div class="label">今日利息</div><div class="value green" id="today">–</div><div class="sub" id="d7"></div></div>
    <div class="card"><div class="label">近 30 日利息</div><div class="value green" id="d30">–</div><div class="sub" id="estApr"></div></div>
  </div>
  <div id="error"></div>
  <section>
    <h2>每日利息(近 60 天)</h2>
    <div id="chartWrap"><canvas id="chart"></canvas></div>
  </section>
  <section>
    <h2>放貸中部位</h2>
    <div id="lentTable"></div>
  </section>
  <section>
    <h2>掛單中(等待成交)</h2>
    <div id="offerTable"></div>
  </section>
</main>
<footer>資料直接來自 Bitfinex API,每 60 秒自動更新 · 更新時間 <span id="updated">–</span></footer>
<script>
let chart;
const fmt = (n, d = 2) => Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const ts = ms => ms ? new Date(ms).toLocaleString("zh-TW", { hour12: false }) : "–";

function table(headers, rows) {
  if (!rows.length) return '<div class="card empty">目前沒有資料</div>';
  return `<table><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr>` +
    rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("") + "</table>";
}

async function load() {
  const ccy = document.getElementById("ccy").value;
  const err = document.getElementById("error");
  err.textContent = "";
  let d;
  try {
    const r = await fetch("/api/summary?ccy=" + encodeURIComponent(ccy));
    d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
  } catch (e) { err.textContent = "讀取失敗:" + e.message; return; }

  document.getElementById("balance").textContent = fmt(d.balance) + " " + d.currency;
  document.getElementById("available").textContent = "閒置可掛出:" + fmt(d.available);
  document.getElementById("lentTotal").textContent = fmt(d.lent.total);
  document.getElementById("lentRate").textContent =
    d.lent.total ? `加權 ${fmt(d.lent.weighted_rate_pct, 4)}%/日 · 年化 ${fmt(d.lent.apr_pct, 1)}%` : "";
  document.getElementById("today").textContent = "+" + fmt(d.earnings.today, 4);
  document.getElementById("d7").textContent = "近 7 日:+" + fmt(d.earnings.d7, 2);
  document.getElementById("d30").textContent = "+" + fmt(d.earnings.d30, 2);
  document.getElementById("estApr").textContent = "估算年化 " + fmt(d.earnings.est_apr_pct, 1) + "%";

  document.getElementById("lentTable").innerHTML = table(
    ["狀態", "金額", "日利率", "年化", "天數", "開始", "到期"],
    d.lent.items.map(p => [
      p.used ? '<span class="badge">生息中</span>' : '<span class="badge idle">已提供</span>',
      fmt(p.amount), fmt(p.rate_pct, 4) + "%", fmt(p.apr_pct, 1) + "%",
      p.period, ts(p.opened_ms), ts(p.expires_ms),
    ]));

  document.getElementById("offerTable").innerHTML = table(
    ["掛單時間", "金額", "日利率", "年化", "天數"],
    d.offers.map(o => [ts(o.created_ms), fmt(o.amount), fmt(o.rate_pct, 4) + "%",
                       fmt(o.apr_pct, 1) + "%", o.period]));

  const labels = d.earnings.daily.map(x => x.date.slice(5));
  const values = d.earnings.daily.map(x => x.amount);
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById("chart"), {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: "#16a34a", borderRadius: 3 }] },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#94a3b8", maxTicksLimit: 15 }, grid: { display: false } },
        y: { ticks: { color: "#94a3b8" }, grid: { color: "#2c3d52" } },
      },
    },
  });
  document.getElementById("updated").textContent = new Date().toLocaleTimeString("zh-TW", { hour12: false });
}

async function init() {
  const cfg = await (await fetch("/api/config")).json();
  const sel = document.getElementById("ccy");
  sel.innerHTML = cfg.currencies.map(c => `<option>${c}</option>`).join("");
  sel.onchange = load;
  await load();
  setInterval(load, 60_000);
}
init();
</script>
</body>
</html>
"""


def main() -> int:
    import os
    import sys

    bot.load_dotenv(bot.SCRIPT_DIR / ".env")
    key = os.environ.get("BFX_API_KEY", "")
    secret = os.environ.get("BFX_API_SECRET", "")
    if not key or not secret:
        print("錯誤: 請在 .env 或環境變數設定 BFX_API_KEY / BFX_API_SECRET")
        return 1

    config = json.loads((bot.SCRIPT_DIR / "config.json").read_text(encoding="utf-8"))
    Handler.client = bot.Bitfinex(key, secret)
    Handler.currencies = list(config.get("currencies", {}))

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"儀表板已啟動: {url} (Ctrl+C 結束)")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
