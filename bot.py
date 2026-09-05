#!/usr/bin/env python3
"""Bitfinex 綠葉放貸自動掛單機器人。

流程(每次執行):
  1. 讀取 config.json 與 API 金鑰(環境變數 BFX_API_KEY / BFX_API_SECRET)
  2. 對每個啟用的幣別:
     a. 抓市場利率(funding ticker:FRR、最佳買/賣利率)
     b. 取消掛太久沒成交的舊掛單,釋放資金
     c. 讀取 funding 錢包可用餘額
     d. 依階梯策略把資金分成多筆、以遞增利率掛出
  3. 任一幣別失敗以非零狀態碼結束,讓 GitHub Actions 顯示紅燈

設 DRY_RUN=1 只會印出將執行的動作,不會真的下單/取消。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sys
import time
from pathlib import Path

import requests

API_BASE = "https://api.bitfinex.com"
SCRIPT_DIR = Path(__file__).resolve().parent

# funding ticker 欄位索引 (https://docs.bitfinex.com/reference/rest-public-ticker)
T_FRR, T_BID, T_ASK = 0, 1, 4
# funding offer 欄位索引 (https://docs.bitfinex.com/reference/rest-auth-funding-offers)
O_ID, O_MTS_CREATED, O_AMOUNT, O_RATE, O_PERIOD = 0, 2, 4, 14, 15
# wallet 欄位索引 (https://docs.bitfinex.com/reference/rest-auth-wallets)
W_TYPE, W_CURRENCY, W_BALANCE, W_AVAILABLE = 0, 1, 2, 4


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()) + f" | {msg}", flush=True)


def load_dotenv(path: Path) -> None:
    """讀取 .env(本機測試用),已存在的環境變數不覆蓋。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class Bitfinex:
    def __init__(self, key: str, secret: str):
        # 環境變數/Secrets 有時會混入 BOM 或空白,一律清掉
        self.key = key.strip().lstrip("﻿")
        self.secret = secret.strip().lstrip("﻿").encode()
        self.session = requests.Session()
        self._last_nonce = 0

    def _nonce(self) -> str:
        nonce = int(time.time() * 1_000_000)
        if nonce <= self._last_nonce:
            nonce = self._last_nonce + 1
        self._last_nonce = nonce
        return str(nonce)

    def public(self, path: str):
        r = self.session.get(f"{API_BASE}/v2/{path}", timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"GET {path} -> HTTP {r.status_code}: {r.text}")
        return r.json()

    def auth(self, path: str, body: dict | None = None):
        raw = json.dumps(body or {})
        nonce = self._nonce()
        payload = f"/api/v2/{path}{nonce}{raw}"
        sig = hmac.new(self.secret, payload.encode(), hashlib.sha384).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "bfx-nonce": nonce,
            "bfx-apikey": self.key,
            "bfx-signature": sig,
        }
        r = self.session.post(f"{API_BASE}/v2/{path}", data=raw, headers=headers, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"POST {path} -> HTTP {r.status_code}: {r.text}")
        return r.json()


def pick_period(rate_pct: float, rules: list[dict]) -> int:
    """依日利率(%)選放貸天數:利率越高鎖越久。"""
    for rule in sorted(rules, key=lambda r: r["min_daily_rate_pct"], reverse=True):
        if rate_pct >= rule["min_daily_rate_pct"]:
            return int(rule["period_days"])
    return 2


def build_ladder(available: float, cfg: dict, base_rate_pct: float,
                 top_rate_pct: float = 0.0) -> list[tuple[float, float, int]]:
    """把可用資金拆成階梯掛單,回傳 [(金額, 日利率%, 天數), ...],利率由低到高。

    給了 top_rate_pct 就把階梯線性鋪在 base..top 之間(貼合實際成交帶);
    否則退回以 ladder_step_pct 逐層遞增。
    """
    min_size = float(cfg.get("min_offer_size", 150))
    max_size = float(cfg.get("max_offer_size", 0)) or None
    steps = max(1, int(cfg.get("num_ladder_steps", 4)))
    step_pct = float(cfg.get("ladder_step_pct", 8.0)) / 100.0
    min_rate = float(cfg.get("min_daily_rate_pct", 0.0))

    if available < min_size:
        return []
    n = min(steps, int(available // min_size))
    if max_size:  # 資金大時拆更多筆,讓每筆不超過單筆上限
        n = max(n, math.ceil(available / max_size))
    # 無論如何每筆都不能低於交易所最小掛單額
    n = max(1, min(n, int(available // min_size)))
    chunk = available / n

    offers = []
    for i in range(n):
        rung = i * steps // n  # 0..steps-1,把 n 筆平均映射到階梯上
        if top_rate_pct > base_rate_pct and steps > 1:
            rate = base_rate_pct + (top_rate_pct - base_rate_pct) * rung / (steps - 1)
        else:
            rate = base_rate_pct * (1 + step_pct * rung)
        rate = max(rate, min_rate)
        amount = chunk if i < n - 1 else available - chunk * (n - 1)
        offers.append((round(amount, 6), rate, pick_period(rate, cfg.get("period_rules", []))))
    return offers


def market_band(client: Bitfinex, symbol: str, low_pct: float, high_pct: float) -> tuple[float, float]:
    """全市場最近成交利率的分位區間,即「現在確實借得出去的價格帶」。

    ticker 的 FRR 是已提供資金的加權平均(含過去鎖倉的長天期高利單),通常
    高於當下成交價,掛在那裡不會成交;ask 則是訂單簿最底層的殺價單,掛在
    那裡等於當全市場最便宜的錢。兩者都不適合當基準。

    階梯直接鋪在這個實際成交帶上,所以市場窄幅時階梯自動收窄(每層都掛得掉),
    行情波動、成交價拉開時階梯自動變寬(高層才有機會吃到尖峰)。
    取不到成交紀錄時回傳 (0, 0),由呼叫端退回訂單簿。
    """
    try:
        trades = client.public(f"trades/{symbol}/hist?limit=1000")
    except Exception as exc:  # noqa: BLE001 — 公開行情失敗不該中斷放貸
        log(f"{symbol}: 取成交紀錄失敗,改用訂單簿 — {exc}")
        return 0.0, 0.0
    rates = sorted(abs(float(t[3])) for t in trades)
    if not rates:
        return 0.0, 0.0
    at = lambda q: rates[min(len(rates) - 1, int(len(rates) * q / 100))]
    return at(low_pct), at(high_pct)


def funding_available(client: Bitfinex, currency: str) -> float:
    for w in client.auth("auth/r/wallets"):
        if w[W_TYPE] == "funding" and w[W_CURRENCY] == currency:
            if w[W_AVAILABLE] is None:
                raise RuntimeError(f"{currency} funding 錢包沒有回傳可用餘額")
            return float(w[W_AVAILABLE])
    return 0.0


def run_currency(client: Bitfinex, currency: str, cfg: dict, stale_minutes: float, dry_run: bool) -> None:
    symbol = f"f{currency}"

    ticker = client.public(f"ticker/{symbol}")
    frr = float(ticker[T_FRR] or 0)
    ask = float(ticker[T_ASK] or 0)

    # 錨定「全市場實際成交價」的成交量加權均價。
    # 不能用 ask(訂單簿最低的殺價單,掛那裡等於當最便宜的錢,只拿到約半價),
    # 也不能用 FRR(已提供資金的加權平均,含過去鎖倉的高利長單,通常高於當下
    # 成交價 — 掛在那裡根本不會成交)。
    low, high = market_band(client, symbol,
                            float(cfg.get("market_anchor_percentile", 25)),
                            float(cfg.get("market_top_percentile", 90)))
    floor_pct = float(cfg.get("min_daily_rate_pct", 0.0))
    if low:
        base_rate_pct, top_rate_pct, anchor = max(low * 100, floor_pct), high * 100, "成交帶"
    else:  # 成交紀錄取不到時退回訂單簿
        base_rate_pct, top_rate_pct, anchor = max(ask * 100, floor_pct), 0.0, "最佳掛單"
    if base_rate_pct <= floor_pct:
        anchor = "利率地板"
    log(f"{currency}: FRR {frr * 100:.4f}%/日 (APR {frr * 36500:.1f}%),"
        f" 最佳掛單 {ask * 100:.4f}%/日,"
        f" 成交帶 {low * 100:.4f}~{high * 100:.4f}%/日,"
        f" 階梯 {base_rate_pct:.4f}~{max(top_rate_pct, base_rate_pct):.4f}%/日"
        f" (APR {base_rate_pct * 365:.1f}~{max(top_rate_pct, base_rate_pct) * 365:.1f}%, 錨定{anchor})")

    # 取消掛超過 stale_minutes 未成交的舊單
    now_ms = time.time() * 1000
    cancelled = 0
    for offer in client.auth(f"auth/r/funding/offers/{symbol}"):
        age_min = (now_ms - offer[O_MTS_CREATED]) / 60_000
        if age_min < stale_minutes:
            continue
        log(f"{currency}: 取消舊掛單 #{offer[O_ID]} — {float(offer[O_AMOUNT]):.2f} @ "
            f"{float(offer[O_RATE]) * 100:.4f}%/日,已掛 {age_min:.0f} 分鐘")
        if not dry_run:
            client.auth("auth/w/funding/offer/cancel", {"id": offer[O_ID]})
        cancelled += 1
    if cancelled and not dry_run:
        time.sleep(2)  # 等取消後的資金回到可用餘額

    available = funding_available(client, currency) - float(cfg.get("reserve_amount", 0))
    if dry_run and cancelled:
        log(f"{currency}: (DRY_RUN 未實際取消,以下餘額不含被舊掛單鎖住的資金)")
    log(f"{currency}: 可掛出資金 {available:.2f}")

    ladder = build_ladder(available, cfg, base_rate_pct, top_rate_pct)
    if not ladder:
        log(f"{currency}: 資金不足最小掛單額 {cfg.get('min_offer_size', 150)},本輪不掛單")
        return

    for amount, rate_pct, period in ladder:
        rate = rate_pct / 100  # API 用小數日利率
        log(f"{currency}: 掛單 {amount:.2f} @ {rate_pct:.4f}%/日 (APR {rate * 36500:.1f}%),{period} 天")
        if dry_run:
            continue
        resp = client.auth("auth/w/funding/offer/submit", {
            "type": "LIMIT",
            "symbol": symbol,
            "amount": f"{amount:.6f}",
            "rate": f"{rate:.9f}",
            "period": period,
        })
        status, text = resp[6], resp[7]
        if status != "SUCCESS":
            raise RuntimeError(f"掛單失敗: {status} — {text}")
        time.sleep(0.5)  # 避開 rate limit


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(SCRIPT_DIR / ".env")

    key = os.environ.get("BFX_API_KEY", "")
    secret = os.environ.get("BFX_API_SECRET", "")
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    if not key or not secret:
        log("錯誤: 請設定 BFX_API_KEY / BFX_API_SECRET 環境變數")
        return 1
    if dry_run:
        log("DRY_RUN 模式:只顯示動作,不會真的下單")

    config = json.loads((SCRIPT_DIR / "config.json").read_text(encoding="utf-8"))
    client = Bitfinex(key, secret)
    stale_minutes = float(config.get("stale_offer_minutes", 60))

    failed = False
    for currency, cfg in config.get("currencies", {}).items():
        if not cfg.get("enabled"):
            log(f"{currency}: 未啟用,跳過")
            continue
        try:
            run_currency(client, currency, cfg, stale_minutes, dry_run)
        except Exception as exc:
            failed = True
            log(f"{currency}: 失敗 — {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
