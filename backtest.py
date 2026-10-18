#!/usr/bin/env python3
"""用 Bitfinex 公開 K 線回測掛單策略,比較實現年化。

資料:fUSD 2 天期與 7 天期 funding 市場的 15 分鐘 K 線(公開端點,不需金鑰),
      第一次執行會下載並快取到 .cache/,之後直接讀快取。
模型:
  - 成交:某天期市場該 15 分鐘的最高成交價 ≥ 掛單利率就視為成交(我們的量相對市場極小)
  - 借方持有時間:依 history/USD_funding_credits.csv 的實際分布抽樣(2 天期中位數 5 小時,
    37% 撐滿 2 天);7 天期的單撐過 2 天後有一半機率持有到期
  - 利息:放貸中每 15 分鐘按日利率計息;年化 = 總利息 ÷ 本金 ÷ 天數 × 365
  - 機器人每 cadence 分鐘才行動一次(240 ≈ 舊版 GitHub 排程的實際頻率,15 ≈ 長跑模式)

用法:
    py backtest.py                 # 比較「舊策略」與「現行策略」在不同執行頻率下的年化
    py backtest.py --grid          # 另外掃一組參數,看預設值在不在平坦的高原上
    py backtest.py --capital 5000  # 換本金規模

所有結果都是模型估計,絕對值偏樂觀(未計 API 失敗、排隊優先權),相對比較才有意義。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import deque
from pathlib import Path

import requests

import bot

CACHE = bot.SCRIPT_DIR / ".cache"
STEP = 900_000  # 15 分鐘
PAGES = 7       # 每頁 10000 根 → 7 頁約 2 年
HOLD_P2 = [0.1, 0.3, 0.6, 0.8, 1.0, 1.2, 1.3, 2.0, 3.2, 4.4, 5.2, 10, 31, 40, 48, 48, 48, 48, 48, 48]


def candles(period: int) -> dict[int, list]:
    """{mts: [mts, open, close, high, low, volume]} — 下載並快取。"""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"fUSD_15m_p{period}.json"
    if path.exists():
        return {r[0]: r for r in json.loads(path.read_text())}
    rows, end = [], None
    for _ in range(PAGES):
        url = f"{bot.API_BASE}/v2/candles/trade:15m:fUSD:p{period}/hist?limit=10000&sort=-1"
        r = requests.get(url + (f"&end={end}" if end else ""), timeout=60)
        if r.status_code == 429:
            time.sleep(65)
            continue
        r.raise_for_status()
        page = r.json()
        if not page:
            break
        rows += page
        end = min(x[0] for x in page) - 1
        print(f"  p{period}: 已下載 {len(rows)} 根,至 {time.strftime('%Y-%m-%d', time.gmtime(end / 1000))}", flush=True)
        if len(page) < 10000:
            break
        time.sleep(7)
    path.write_text(json.dumps(rows))
    return {r[0]: r for r in rows}


class Band:
    """某市場最近 N 小時的成交利率分布(以 K 線的低/收/高近似,成交量加權)。"""

    def __init__(self, market: dict, hours: float):
        self.m, self.n, self.q, self._cache = market, int(hours * 4), deque(), {}

    def push(self, ts: int) -> None:
        c = self.m.get(ts)
        if c and c[5] > 0:
            self.q.append((ts, c))
        while self.q and self.q[0][0] <= ts - self.n * STEP:
            self.q.popleft()
        self._cache = {}

    def rates(self) -> list[float]:
        """展開成「每筆一票」的利率清單,交給 bot.percentile 用。"""
        if "rates" not in self._cache:
            out = []
            for _, c in self.q:
                w = max(1, min(20, int(c[5] / 50_000)))  # 量大的 K 線多幾票,但不讓單根主導
                out += [c[4] * 100] * w + [c[2] * 100] * (2 * w) + [c[3] * 100] * w
            out.sort()
            self._cache["rates"] = out
        return self._cache["rates"]

    def active(self) -> bool:
        return bool(self.q)


class Sim:
    def __init__(self, capital: float, cadence_min: int, strategy, start: int, end: int, seed: int, cfg: dict):
        self.cap, self.cad, self.strat, self.cfg = capital, max(1, cadence_min // 15), strategy, cfg
        self.start, self.end, self.rng = start, end, random.Random(seed)
        self.cash, self.offers, self.credits, self.fills = capital, [], [], []
        self.interest = self.lent_steps = self.offer_steps = 0.0
        markets = {bot.clamp_period(p) for p in cfg.get("markets", [2, 7])}
        self.band = {p: Band(MARKET[p], cfg.get("band_hours", 4)) for p in MARKET if p in markets}
        self.spike_band = Band(MARKET[2], cfg.get("spike_lookback_hours", 168))

    def hold_hours(self, period: int) -> float:
        cap = period * 24
        h = self.rng.choice(HOLD_P2)
        if h >= 48 and period > 2:
            h = cap if self.rng.random() < 0.5 else 48 + self.rng.random() * (cap - 48)
        return min(cap, h)

    def cancel(self, o: dict) -> None:
        self.offers.remove(o)
        self.cash += o["amount"]

    def place(self, amount: float, rate: float, period: int, ts: int, born=None, market=2) -> None:
        if amount < self.cfg.get("min_offer_size", 150) or amount > self.cash + 1e-9:
            return
        self.cash -= amount
        self.offers.append({"rate": rate, "period": bot.clamp_period(period), "amount": amount,
                            "born": born or ts, "market": market})

    def run(self) -> dict:
        ts, i = self.start, 0
        while ts <= self.end:
            for b in list(self.band.values()) + [self.spike_band]:
                b.push(ts)
            for c in list(self.credits):
                if c["until"] <= ts:
                    self.cash += c["amount"]
                    self.credits.remove(c)
            if i % self.cad == 0:
                self.strat(self, ts)
            for o in list(self.offers):
                m = MARKET[o["period"]].get(ts)
                if m and m[5] > 0 and m[3] * 100 >= o["rate"]:
                    hold = self.hold_hours(o["period"])
                    self.credits.append({"rate": o["rate"], "amount": o["amount"],
                                         "until": ts + max(1, int(hold * 4)) * STEP})
                    self.fills.append((o["rate"], o["period"], o["amount"]))
                    self.offers.remove(o)
            self.lent_steps += sum(c["amount"] for c in self.credits)
            self.interest += sum(c["amount"] * c["rate"] / 100 for c in self.credits) / 96
            self.offer_steps += sum(o["amount"] for o in self.offers)
            ts += STEP
            i += 1
        days = (self.end - self.start) / 86_400_000
        steps = (self.end - self.start) / STEP
        amt = sum(a for *_, a in self.fills) or 1
        return {"apr": self.interest / self.cap / days * 365 * 100,
                "util": self.lent_steps / self.cap / steps * 100,
                "wait": self.offer_steps / self.cap / steps * 100,
                "fills": len(self.fills),
                "wavg": sum(r * a for r, _, a in self.fills) / amt,
                "p7": sum(a for _, p, a in self.fills if p >= 7) / amt * 100}


# ---------------------------------------------------------------------------- 策略
def strat_legacy(sim: Sim, ts: int) -> None:
    """2026-09 以前的 bot.py:2 天期成交帶 25~90 分位線性階梯、30/240 分鐘過時重掛、續借頂端。"""
    cfg = sim.cfg
    for o in list(sim.offers):
        age = (ts - o["born"]) / 60_000
        if age >= (240 if o["rate"] >= 0.02 else 30):
            sim.cancel(o)
    b = sim.band[2]
    if not b.active():
        return
    rates = b.rates()
    base, top = max(bot.percentile(rates, 25), 0.018), bot.percentile(rates, 90)
    recent = [r for r, _, _ in sim.fills[-50:] if r >= 0.02]
    if recent and max(recent) > top:
        top = max(recent)
    parts = bot.split_amount(sim.cash, cfg)
    steps = max(1, int(cfg.get("num_ladder_steps", 4)))
    for k, amt in enumerate(parts):
        rung = k * steps // len(parts)
        rate = base + (top - base) * rung / (steps - 1) if top > base and steps > 1 else base
        sim.place(amt, max(rate, 0.018), 7 if rate >= 0.032 else 2, ts)


def strat_current(sim: Sim, ts: int) -> None:
    """現行 bot.py:雙市場取優 + 依年齡遞減定價 + 零頭併單 + 尖峰鎖倉(≤7 天)。"""
    cfg = sim.cfg
    bands = {p: b.rates() for p, b in sim.band.items() if b.active()}
    if not bands:
        return
    spike = bot.percentile(sim.spike_band.rates(), cfg.get("spike_percentile", 90)) if sim.spike_band.active() else 0.0
    tol = cfg.get("reprice_tolerance_pct", 2) / 100
    for o in list(sim.offers):
        rates = bands.get(o["market"]) or next(iter(bands.values()))
        if not rates:
            continue
        target = bot.target_rate(rates, cfg, (ts - o["born"]) / 60_000)
        if o["rate"] > target * (1 + tol):
            sim.cancel(o)
            sim.place(o["amount"], target, bot.pick_period(target, o["market"], spike, cfg), ts, o["born"], o["market"])
    if 0 < sim.cash < cfg.get("min_offer_size", 150) and sim.offers and cfg.get("consolidate_dust", True):
        sim.cancel(max(sim.offers, key=lambda o: o["born"]))
    for amount, rate, period, market in bot.plan_new_offers(sim.cash, bands, spike, cfg):
        sim.place(amount, rate, period, ts, market=market)


def run(strategy, cfg: dict, capital: float, cadence: int, seeds: int = 3, start=None, end=None) -> dict:
    ts_all = sorted(t for t in MARKET[2] if t >= max(min(MARKET[2]), min(MARKET[7])))
    start, end = start or ts_all[4 * 24 * 7], end or ts_all[-1]
    res = [Sim(capital, cadence, strategy, start, end, s, cfg).run() for s in range(seeds)]
    return {k: sum(r[k] for r in res) / len(res) for k in res[0]}


def show(name: str, r: dict) -> None:
    print(f"{name:44s} 年化 {r['apr']:5.2f}%  放貸中 {r['util']:5.1f}%  等成交 {r['wait']:4.1f}%  "
          f"成交 {r['fills']:5.0f} 筆  加權利率 {r['wavg']:.4f}%/日  7天期占 {r['p7']:3.0f}%", flush=True)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=624)
    ap.add_argument("--grid", action="store_true")
    args = ap.parse_args()

    global MARKET
    print("載入 K 線(第一次會下載,約 1 分鐘)…")
    MARKET = {p: candles(p) for p in (2, 7)}
    span = (time.strftime('%Y-%m-%d', time.gmtime(max(min(MARKET[2]), min(MARKET[7])) / 1000)),
            time.strftime('%Y-%m-%d', time.gmtime(max(MARKET[2]) / 1000)))
    print(f"回測區間 {span[0]} → {span[1]},本金 {args.capital:.0f}\n")

    cfg = bot.load_config()["currencies"]["USD"]
    for cadence in (240, 15):
        show(f"舊策略(2 天期階梯)   每 {cadence} 分鐘行動", run(strat_legacy, cfg, args.capital, cadence))
        show(f"現行策略(雙市場遞減) 每 {cadence} 分鐘行動", run(strat_current, cfg, args.capital, cadence))
    if args.grid:
        print("\n參數掃描(每 15 分鐘行動):")
        for sq, fq, dm in ((90, 40, 240), (95, 50, 360), (80, 30, 120), (75, 40, 240), (90, 40, 0)):
            c = dict(cfg, start_percentile=sq, floor_percentile=fq, decay_minutes=dm)
            show(f"起始 {sq} → 最低 {fq},{dm} 分鐘", run(strat_current, c, args.capital, 15))
        for markets in ([2], [7]):
            show(f"只用 {markets[0]} 天期市場", run(strat_current, dict(cfg, markets=markets), args.capital, 15))
        show("不鎖倉(spike_percentile 100)", run(strat_current, dict(cfg, spike_percentile=100), args.capital, 15))
    return 0


MARKET: dict[int, dict[int, list]] = {}

if __name__ == "__main__":
    sys.exit(main())
