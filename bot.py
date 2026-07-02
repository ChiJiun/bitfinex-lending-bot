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


def build_ladder(available: float, cfg: dict, base_rate_pct: float) -> list[tuple[float, float, int]]:
    """把可用資金拆成階梯掛單,回傳 [(金額, 日利率%, 天數), ...],利率由低到高。"""
    min_size = float(cfg.get("min_offer_size", 150))
    max_size = float(cfg.get("max_offer_size", 0)) or None
    steps = max(1, int(cfg.get("num_ladder_steps", 4)))
    step_pct = float(cfg.get("ladder_step_pct", 8.0)) / 100.0
    min_rate = float(cfg.get("min_daily_rate_pct", 0.0))

    if available < min_size:
        return []
    n = min(steps, int(available // min_size)) or 1
    if max_size:
        n = max(n, math.ceil(available / max_size))
    chunk = available / n

    offers = []
    for i in range(n):
        rung = i * steps // n  # 0..steps-1,把 n 筆平均映射到階梯上
        rate = max(base_rate_pct * (1 + step_pct * rung), min_rate)
        amount = chunk if i < n - 1 else available - chunk * (n - 1)
        offers.append((round(amount, 6), rate, pick_period(rate, cfg.get("period_rules", []))))
    return offers


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
    base_rate_pct = max((ask or frr) * 100, float(cfg.get("min_daily_rate_pct", 0.0)))
    log(f"{currency}: FRR {frr * 100:.4f}%/日 (APR {frr * 36500:.1f}%),"
        f" 最佳掛單利率 {ask * 100:.4f}%/日,階梯基準 {base_rate_pct:.4f}%/日")

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

    ladder = build_ladder(available, cfg, base_rate_pct)
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
