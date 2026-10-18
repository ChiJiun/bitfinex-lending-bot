#!/usr/bin/env python3
"""Bitfinex 綠葉放貸自動掛單機器人。

每一輪(單次或在 LOOP_MINUTES 內每隔 LOOP_INTERVAL_SECONDS 重複):
  1. 讀取 config.json 與 API 金鑰(環境變數 BFX_API_KEY / BFX_API_SECRET)
  2. 對每個啟用的幣別:
     a. 抓全市場最近幾小時的實際成交,分別整理出 2 天期與 7 天期的成交利率分布
     b. 已掛未成交的單:依「掛了多久」把目標價從高分位往低分位遞減,偏高就撤單重掛
     c. 讀取 funding 錢包可用餘額;零頭不足最小掛單額時,撤回最新一筆掛單併入
     d. 新資金拆成多筆,每筆在 2 天期 / 7 天期兩個市場中挑目標價較高的掛出
  3. 任一幣別失敗以非零狀態碼結束,讓 GitHub Actions 顯示紅燈

掛單天期硬性上限 MAX_PERIOD_DAYS(7 天),config 設更大也會被壓回來,確保資金流動性。
設 DRY_RUN=1 只會印出將執行的動作,不會真的下單/取消。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

API_BASE = "https://api.bitfinex.com"
SCRIPT_DIR = Path(__file__).resolve().parent
MAX_PERIOD_DAYS = 7          # 流動性紅線:任何掛單天期都不得超過一週
MIN_PERIOD_DAYS = 2          # Bitfinex 最短放貸天期
TRADES_LIMIT = 10000         # 公開成交端點單次最多回傳筆數

# funding ticker 欄位索引 (https://docs.bitfinex.com/reference/rest-public-ticker)
T_FRR, T_BID, T_ASK = 0, 1, 4
# 公開 funding 成交欄位索引 (https://docs.bitfinex.com/reference/rest-public-trades)
PT_MTS, PT_AMOUNT, PT_RATE, PT_PERIOD = 1, 2, 3, 4
# funding offer 欄位索引 (https://docs.bitfinex.com/reference/rest-auth-funding-offers)
O_ID, O_MTS_CREATED, O_AMOUNT, O_RATE, O_PERIOD = 0, 2, 4, 14, 15
# wallet 欄位索引 (https://docs.bitfinex.com/reference/rest-auth-wallets)
W_TYPE, W_CURRENCY, W_BALANCE, W_AVAILABLE = 0, 1, 2, 4
# funding credit/loan 欄位索引 (https://docs.bitfinex.com/reference/rest-auth-funding-credits)
C_ID, C_AMOUNT, C_RATE, C_PERIOD, C_RENEW = 0, 5, 11, 12, 18

# 掛單「出生時間」與定價市場。撤單重掛會換新 ID,靠這張表讓新單繼承舊單的年齡,
# 遞減定價才不會每次重掛都從最高分位重來。只存在記憶體裡,長跑模式下跨輪有效。
OFFER_BORN: dict[int, float] = {}
OFFER_MARKET: dict[int, int] = {}


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


# ----------------------------------------------------------------------------
# 純函式:定價、拆單、天期。沒有 I/O,方便回測與測試共用。
# ----------------------------------------------------------------------------
def clamp_period(days) -> int:
    """天期硬性限制在 [MIN_PERIOD_DAYS, MAX_PERIOD_DAYS],config 設錯也不會鎖倉超過一週。"""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = MIN_PERIOD_DAYS
    return max(MIN_PERIOD_DAYS, min(MAX_PERIOD_DAYS, days))


def percentile(sorted_rates: list[float], q: float) -> float:
    """已排序利率序列的第 q 分位(每筆成交一票,不以金額加權,免得單筆巨量主導)。"""
    if not sorted_rates:
        return 0.0
    q = max(0.0, min(100.0, q))
    return sorted_rates[min(len(sorted_rates) - 1, int(len(sorted_rates) * q / 100))]


def group_rates(trades: list, periods: tuple[int, ...]) -> dict[int, list[float]]:
    """把公開成交依天期分組,回傳 {天期: 已排序日利率%}。"""
    out: dict[int, list[float]] = {p: [] for p in periods}
    for t in trades:
        try:
            p = int(t[PT_PERIOD])
        except (TypeError, ValueError, IndexError):
            continue
        if p in out and t[PT_RATE]:
            out[p].append(abs(float(t[PT_RATE])) * 100)
    for p in out:
        out[p].sort()
    return out


def decayed_percentile(start_q: float, floor_q: float, decay_minutes: float, age_minutes: float) -> float:
    """掛單剛送出時用 start_q 分位,隨年齡線性降到 floor_q;decay_minutes<=0 表示不遞減。"""
    if decay_minutes <= 0 or age_minutes <= 0:
        return start_q
    return start_q - (start_q - floor_q) * min(1.0, age_minutes / decay_minutes)


def target_rate(rates: list[float], cfg: dict, age_minutes: float, rung: int = 0) -> float:
    """某市場(已排序成交利率)上,年齡 age_minutes、第 rung 層掛單的目標日利率%。"""
    q = decayed_percentile(float(cfg.get("start_percentile", 90)),
                           float(cfg.get("floor_percentile", 40)),
                           float(cfg.get("decay_minutes", 240)), age_minutes)
    q = min(99.0, q + rung * float(cfg.get("rung_spread_percentile", 0)))
    return max(percentile(rates, q), float(cfg.get("min_daily_rate_pct", 0.0)))


def split_amount(available: float, cfg: dict) -> list[float]:
    """把可用資金等分成多筆,每筆介於 [min_offer_size, max_offer_size],金額截到分為止。

    先用「資金 ÷ 最小額」決定筆數再等分,天然不會產生低於最小額的零頭單;
    金額一律無條件捨去到 0.01,總和才不會因浮點尾差超過餘額被交易所拒單。
    """
    min_size = float(cfg.get("min_offer_size", 150))
    max_size = float(cfg.get("max_offer_size", 0)) or None
    steps = max(1, int(cfg.get("num_ladder_steps", 4)))
    if available < min_size:
        return []
    n = min(steps, int(available // min_size))
    if max_size:
        n = max(n, math.ceil(available / max_size))
    n = max(1, min(n, int(available // min_size)))
    chunk = math.floor(available / n * 100) / 100
    return [chunk] * n if chunk >= min_size else []


def pick_period(rate_pct: float, market: int, spike_rate_pct: float, cfg: dict) -> int:
    """天期:7 天期市場成交的就是 7 天;2 天期市場的單,只有利率高到「近一週尖峰」才鎖滿上限。"""
    max_days = clamp_period(cfg.get("max_period_days", MAX_PERIOD_DAYS))
    if market >= MAX_PERIOD_DAYS:
        return min(max_days, clamp_period(market))
    if spike_rate_pct > 0 and rate_pct >= spike_rate_pct:
        return max_days
    return MIN_PERIOD_DAYS


def plan_new_offers(available: float, bands: dict[int, list[float]], spike_rate_pct: float,
                    cfg: dict) -> list[tuple[float, float, int, int]]:
    """新資金的掛單計畫:[(金額, 日利率%, 天期, 定價市場), ...]。

    每一筆都在有成交紀錄的市場中挑「目前目標價最高」的那個。7 天期市場通常比 2 天期
    高 15~70%,但不是每個時刻都有成交;沒有的時候自然退回 2 天期。
    """
    plan = []
    for rung, amount in enumerate(split_amount(available, cfg)):
        best = None
        for market, rates in bands.items():
            if not rates:
                continue
            rate = target_rate(rates, cfg, 0.0, rung)
            if best is None or rate > best[0]:
                best = (rate, market)
        if best is None:
            break
        rate, market = best
        plan.append((amount, rate, pick_period(rate, market, spike_rate_pct, cfg), market))
    return plan


# ----------------------------------------------------------------------------
# 市場資料
# ----------------------------------------------------------------------------
def market_bands(client: Bitfinex, symbol: str, cfg: dict) -> dict[int, list[float]]:
    """最近 band_hours 小時內、各定價市場(天期)的實際成交利率分布。

    不能用 FRR(含過去鎖倉的長天期高利單,系統性高於當下成交價,掛那裡不會成交),
    也不能用訂單簿最低報價(等於當全市場最便宜的錢)。只有「真的成交過的價」能用。
    取不到就回傳空字典,由呼叫端退回訂單簿定價。
    """
    hours = float(cfg.get("band_hours", 4))
    periods = tuple(sorted({clamp_period(p) for p in cfg.get("markets", [2, 7])}))
    start = int(time.time() * 1000 - hours * 3_600_000)
    try:
        trades = client.public(f"trades/{symbol}/hist?limit={TRADES_LIMIT}&start={start}&sort=-1")
    except Exception as exc:  # noqa: BLE001 — 公開行情失敗不該中斷放貸
        log(f"{symbol}: 取成交紀錄失敗,改用訂單簿 — {exc}")
        return {}
    return group_rates(trades, periods)


_SPIKE_CACHE: dict[str, tuple[float, float]] = {}


def spike_rate(client: Bitfinex, symbol: str, cfg: dict) -> float:
    """「近一週尖峰」門檻:過去 spike_lookback_hours 小時 2 天期每小時收盤價的第 spike_percentile 分位。

    2 天期掛單的利率一旦到這個水位,就把天期拉到上限(預設 7 天)鎖住 ——
    回測顯示尖峰過後 30 天的平均利率只有尖峰時的一半,早鎖早賺。每 15 分鐘更新一次即可。
    """
    q = float(cfg.get("spike_percentile", 90))
    if q >= 100:
        return 0.0
    cached = _SPIKE_CACHE.get(symbol)
    if cached and time.time() - cached[0] < 900:
        return cached[1]
    hours = int(cfg.get("spike_lookback_hours", 168))
    try:
        candles = client.public(f"candles/trade:1h:{symbol}:p2/hist?limit={min(hours, 10000)}")
        closes = sorted(float(c[2]) * 100 for c in candles if c[5])
        thr = percentile(closes, q) if closes else 0.0
    except Exception as exc:  # noqa: BLE001 — 拿不到就不鎖倉
        log(f"{symbol}: 取 K 線失敗,本輪不啟用尖峰鎖倉 — {exc}")
        thr = 0.0
    _SPIKE_CACHE[symbol] = (time.time(), thr)
    return thr


def deployed_amount(client: Bitfinex, symbol: str) -> float:
    """已投入放貸的總額:生息中的部位 + 還掛在市場上未成交的單。"""
    total = 0.0
    for kind in ("credits", "loans"):
        for pos in client.auth(f"auth/r/funding/{kind}/{symbol}"):
            total += abs(float(pos[C_AMOUNT] or 0))
    for offer in client.auth(f"auth/r/funding/offers/{symbol}"):
        total += abs(float(offer[O_AMOUNT] or 0))
    return total


def funding_available(client: Bitfinex, currency: str) -> float:
    for w in client.auth("auth/r/wallets"):
        if w[W_TYPE] == "funding" and w[W_CURRENCY] == currency:
            if w[W_AVAILABLE] is None:
                raise RuntimeError(f"{currency} funding 錢包沒有回傳可用餘額")
            return float(w[W_AVAILABLE])
    return 0.0


# ----------------------------------------------------------------------------
# 下單
# ----------------------------------------------------------------------------
def submit_offer(client: Bitfinex, symbol: str, amount: float, rate_pct: float, period: int,
                 dry_run: bool, born_ms: float | None = None, market: int = 0) -> int | None:
    period = clamp_period(period)  # 最後一道防線:絕不送出超過一週的掛單
    rate = rate_pct / 100  # API 用小數日利率
    if dry_run:
        return None
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
    try:
        new_id = int(resp[4][0][O_ID])
    except (TypeError, IndexError, ValueError):
        new_id = None
    if new_id is not None:
        OFFER_BORN[new_id] = born_ms if born_ms is not None else time.time() * 1000
        if market:
            OFFER_MARKET[new_id] = market
    time.sleep(0.5)  # 避開 rate limit
    return new_id


def run_currency(client: Bitfinex, currency: str, cfg: dict, dry_run: bool,
                 paused: bool = False, verbose: bool = True) -> None:
    symbol = f"f{currency}"
    now_ms = time.time() * 1000
    offers = client.auth(f"auth/r/funding/offers/{symbol}")
    live_ids = {int(o[O_ID]) for o in offers}
    for table in (OFFER_BORN, OFFER_MARKET):  # 已成交/已取消的單不必再記
        for oid in [k for k in table if k not in live_ids]:
            table.pop(oid, None)

    # 暫停:只收回未成交掛單,不掛新單
    if paused:
        for offer in offers:
            log(f"{currency}: 取消掛單 #{offer[O_ID]} — {float(offer[O_AMOUNT]):.2f} @ "
                f"{float(offer[O_RATE]) * 100:.4f}%/日,暫停中,收回未成交掛單")
            if not dry_run:
                client.auth("auth/w/funding/offer/cancel", {"id": offer[O_ID]})
        log(f"{currency}: ⏸ 掛單已暫停 — 本輪收回 {len(offers)} 筆掛單,不再掛新單。"
            f"已成交的放貸會依原本天期(最長 {MAX_PERIOD_DAYS} 天)自然到期回流")
        return

    bands = market_bands(client, symbol, cfg)
    floor_pct = float(cfg.get("min_daily_rate_pct", 0.0))
    spike = spike_rate(client, symbol, cfg)
    if not any(bands.values()):  # 成交紀錄取不到時退回訂單簿最佳報價
        ticker = client.public(f"ticker/{symbol}")
        ask = float(ticker[T_ASK] or 0) * 100
        bands = {MIN_PERIOD_DAYS: [max(ask, floor_pct)]}
        log(f"{currency}: 無成交紀錄,退回訂單簿最佳掛單 {ask:.4f}%/日")
    if verbose:
        desc = ", ".join(
            f"{p}天 {percentile(r, 40):.4f}~{percentile(r, 90):.4f}%/日({len(r)}筆)"
            for p, r in sorted(bands.items()) if r)
        log(f"{currency}: 成交帶 {desc};尖峰鎖倉門檻 {spike:.4f}%/日")

    # 1. 既有掛單:依年齡遞減目標價,偏高就撤單重掛(新單繼承年齡)
    tol = float(cfg.get("reprice_tolerance_pct", 2.0)) / 100
    repriced: set[int] = set()
    for offer in offers:
        oid = int(offer[O_ID])
        born = OFFER_BORN.setdefault(oid, float(offer[O_MTS_CREATED]))
        market = OFFER_MARKET.get(oid, MAX_PERIOD_DAYS if int(offer[O_PERIOD]) >= MAX_PERIOD_DAYS else MIN_PERIOD_DAYS)
        rates = bands.get(market) or bands.get(MIN_PERIOD_DAYS) or []
        if not rates:
            continue
        age_min = (now_ms - born) / 60_000
        target = target_rate(rates, cfg, age_min)
        rate_now = float(offer[O_RATE]) * 100
        if rate_now <= target * (1 + tol):
            continue
        amount = abs(float(offer[O_AMOUNT]))
        period = pick_period(target, market, spike, cfg)
        if amount < float(cfg.get("min_offer_size", 150)):  # 部分成交剩的零頭,收回併入新資金
            log(f"{currency}: 收回 #{oid} — 剩 {amount:.2f} 不足最小額,併入可用資金重掛")
        else:
            log(f"{currency}: 重掛 #{oid} — {amount:.2f} 已掛 {age_min:.0f} 分鐘,"
                f"{rate_now:.4f} → {target:.4f}%/日 (APR {target * 365:.1f}%),{period} 天")
        if not dry_run:
            client.auth("auth/w/funding/offer/cancel", {"id": oid})
            time.sleep(1)  # 等取消後的資金回到可用餘額
            if amount >= float(cfg.get("min_offer_size", 150)):
                submit_offer(client, symbol, amount, target, period, dry_run, born_ms=born, market=market)
        OFFER_BORN.pop(oid, None)
        OFFER_MARKET.pop(oid, None)
        repriced.add(oid)

    # 2. 可用資金;零頭不足最小額時,撤回最新一筆掛單併入(下一輪一起重掛)
    available = funding_available(client, currency) - float(cfg.get("reserve_amount", 0))
    min_size = float(cfg.get("min_offer_size", 150))
    live = [o for o in offers if int(o[O_ID]) not in repriced]
    if 0 < available < min_size and live and bool(cfg.get("consolidate_dust", True)):
        youngest = max(live, key=lambda o: OFFER_BORN.get(int(o[O_ID]), o[O_MTS_CREATED]))
        log(f"{currency}: 零頭 {available:.2f} 不足最小額,撤回掛單 #{youngest[O_ID]}"
            f"({abs(float(youngest[O_AMOUNT])):.2f})併入後一起重掛")
        if not dry_run:
            client.auth("auth/w/funding/offer/cancel", {"id": youngest[O_ID]})
            time.sleep(1)  # 等取消後的資金回到可用餘額
            available = funding_available(client, currency) - float(cfg.get("reserve_amount", 0))
        else:
            available += abs(float(youngest[O_AMOUNT]))
        OFFER_BORN.pop(int(youngest[O_ID]), None)
        OFFER_MARKET.pop(int(youngest[O_ID]), None)

    max_lend = float(cfg.get("max_lend_amount", 0))
    if max_lend > 0 and available >= min_size:  # 總放貸上限:已投入加上這輪要掛的不超過它
        deployed = deployed_amount(client, symbol)
        room = max(0.0, max_lend - deployed)
        if room < available:
            log(f"{currency}: 放貸上限 {max_lend:.2f},已投入 {deployed:.2f},"
                f"本輪最多再掛 {room:.2f}(其餘留在錢包不動)")
            available = room
    if available < min_size:
        if verbose:
            log(f"{currency}: 可掛出資金 {available:.2f},不足最小掛單額 {min_size:.0f},本輪不掛新單"
                f"(掛單中 {len(offers)} 筆)")
        return

    # 3. 新資金:每筆在各市場中挑目標價最高者
    plan = plan_new_offers(available, bands, spike, cfg)
    log(f"{currency}: 可掛出資金 {available:.2f},拆成 {len(plan)} 筆")
    for amount, rate_pct, period, market in plan:
        log(f"{currency}: 掛單 {amount:.2f} @ {rate_pct:.4f}%/日 (APR {rate_pct * 365:.1f}%),"
            f"{period} 天(依 {market} 天期市場定價)")
        submit_offer(client, symbol, amount, rate_pct, period, dry_run, market=market)


# ----------------------------------------------------------------------------
# 主流程(單次 / 長跑)
# ----------------------------------------------------------------------------
def publish_dashboard() -> None:
    """長跑模式下定期把儀表板資料發佈出去(等同 workflow 的 publish + commit 步驟)。"""
    env = dict(os.environ, BOT_EXIT="0")
    subprocess.run([sys.executable, str(SCRIPT_DIR / "publish.py")], check=False, env=env)
    git = lambda *a: subprocess.run(["git", *a], cwd=SCRIPT_DIR, check=False,
                                    capture_output=True, text=True)
    git("add", "-A", "docs")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", "chore: update dashboard data")
    if git("push").returncode != 0:
        git("pull", "--rebase", "origin", "master")
        git("push")


def run_once(client: Bitfinex, config: dict, dry_run: bool, verbose: bool) -> bool:
    paused_all = bool(config.get("paused"))
    if paused_all and verbose:
        log("⏸ 全域暫停(config.json 的 paused=true):只收回掛單,不掛新單")
    failed = False
    for currency, cfg in config.get("currencies", {}).items():
        if not cfg.get("enabled"):
            if verbose:
                log(f"{currency}: 未啟用,跳過")
            continue
        try:
            run_currency(client, currency, cfg, dry_run, paused_all or bool(cfg.get("paused")), verbose)
        except Exception as exc:  # noqa: BLE001 — 單一幣別失敗不該中斷其他幣別
            failed = True
            log(f"{currency}: 失敗 — {exc}")
    return failed


def load_config() -> dict:
    path = Path(os.environ.get("CONFIG_PATH") or SCRIPT_DIR / "config.json")
    return json.loads(path.read_text(encoding="utf-8"))


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

    client = Bitfinex(key, secret)
    config = load_config()
    loop_minutes = float(os.environ.get("LOOP_MINUTES", 0) or 0)
    interval = float(os.environ.get("LOOP_INTERVAL_SECONDS", config.get("loop_interval_seconds", 60)))
    publish_every = float(config.get("publish_every_minutes", 15)) * 60
    publish_in_loop = os.environ.get("PUBLISH_IN_LOOP", "").lower() in ("1", "true", "yes")

    if loop_minutes <= 0:
        return 1 if run_once(client, config, dry_run, verbose=True) else 0

    log(f"長跑模式:接下來 {loop_minutes:.0f} 分鐘內每 {interval:.0f} 秒檢查一次"
        f"(掛單天期上限 {MAX_PERIOD_DAYS} 天)")
    deadline = time.time() + loop_minutes * 60
    last_verbose = last_publish = 0.0
    failed = False
    while True:
        started = time.time()
        verbose = started - last_verbose >= publish_every
        if verbose:
            last_verbose = started
            config = load_config()  # 讓長跑中改 config.json(例如 paused)也能生效
        failed = run_once(client, config, dry_run, verbose)
        if publish_in_loop and started - last_publish >= publish_every:
            last_publish = started
            try:
                publish_dashboard()
            except Exception as exc:  # noqa: BLE001 — 發佈失敗不該中斷放貸
                log(f"發佈儀表板失敗 — {exc}")
        if time.time() + interval > deadline:
            break
        time.sleep(max(1.0, interval - (time.time() - started)))
    log("長跑結束" + ("(最後一輪有錯誤)" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
