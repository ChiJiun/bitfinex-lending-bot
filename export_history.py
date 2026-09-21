#!/usr/bin/env python3
"""把 Bitfinex 放貸的完整歷史匯出成 CSV,存進 history/ 供日後調整策略。

匯出四份:
  funding_trades.csv    我們實際成交的每一筆放貸(利率、金額、天數)—— 調參最主要的依據
  funding_offers.csv    掛單歷史,含被取消的單 —— 可看出「掛了什麼價 vs 成交了什麼價」
  funding_credits.csv   放貸部位(已被借走的),含開始與最後付息時間
  interest_payments.csv 每一筆入帳利息

可重複執行:會讀取既有 CSV、以 ID 去重後合併,所以之後定期跑就能持續累積,
不會因為 API 只回傳最近 N 筆而遺失更早的紀錄。

用法:
    py export_history.py            # 匯出 config.json 裡所有幣別(含未啟用的)
    py export_history.py USD UST    # 只匯出指定幣別
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
from pathlib import Path

import bot

HISTORY_DIR = bot.SCRIPT_DIR / "history"
PAGE_LIMIT = 500          # 每頁筆數(Bitfinex 對多數 hist 端點的上限)
LEDGER_LIMIT = 2500
MAX_PAGES = 200           # 保險:避免 API 行為異常時無限迴圈


def paginate(client: bot.Bitfinex, path: str, mts_index: int,
             limit: int = PAGE_LIMIT, extra: dict | None = None) -> list[list]:
    """往回翻頁抓完整歷史(以最舊一筆的時間當作下一頁的 end)。"""
    rows: list[list] = []
    seen: set = set()
    end = None
    for _ in range(MAX_PAGES):
        body = dict(extra or {}, limit=limit)
        if end is not None:
            body["end"] = end
        page = client.auth(path, body)
        if not page:
            break
        fresh = [r for r in page if r[0] not in seen]
        for r in fresh:
            seen.add(r[0])
        rows.extend(fresh)
        oldest = min(r[mts_index] for r in page)
        if len(page) < limit or (end is not None and oldest >= end):
            break
        end = oldest - 1          # 下一頁從更早的地方開始
        time.sleep(0.3)           # 避開 rate limit
    return rows


def merge_csv(path: Path, header: list[str], rows: list[dict]) -> int:
    """與既有檔案以第一欄(id)去重合併,依時間排序後寫回。回傳總筆數。"""
    combined: dict[str, dict] = {}
    if path.exists():
        with io.open(path, encoding="utf-8", newline="") as f:
            for old in csv.DictReader(f):
                combined[old[header[0]]] = old
    for row in rows:
        combined[str(row[header[0]])] = {k: row.get(k, "") for k in header}
    ordered = sorted(combined.values(), key=lambda r: str(r.get(header[1], "")))
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(ordered)
    return len(ordered)


def iso(ms) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(ms) / 1000)) if ms else ""


def export_currency(client: bot.Bitfinex, currency: str) -> dict:
    symbol = f"f{currency}"
    stats: dict[str, int] = {}

    # 1. 我們實際成交的放貸
    # [ID, CURRENCY, MTS_CREATE, OFFER_ID, AMOUNT, RATE, PERIOD, MAKER]
    trades = paginate(client, f"auth/r/funding/trades/{symbol}/hist", 2)
    stats["trades"] = merge_csv(
        HISTORY_DIR / f"{currency}_funding_trades.csv",
        ["id", "time_utc", "mts", "amount", "daily_rate_pct", "apr_pct", "period_days", "offer_id"],
        [{
            "id": t[0], "time_utc": iso(t[2]), "mts": t[2],
            "amount": abs(float(t[4])),
            "daily_rate_pct": round(float(t[5]) * 100, 6),
            "apr_pct": round(float(t[5]) * 36500, 4),
            "period_days": t[6], "offer_id": t[3],
        } for t in trades])

    # 2. 掛單歷史(含已取消) — 看得出掛了什麼價、成交率如何
    # [ID, SYMBOL, MTS_CREATED, MTS_UPDATED, AMOUNT, AMOUNT_ORIG, TYPE, ..., STATUS(10), ..., RATE(14), PERIOD(15)]
    offers = paginate(client, f"auth/r/funding/offers/{symbol}/hist", 2)
    stats["offers"] = merge_csv(
        HISTORY_DIR / f"{currency}_funding_offers.csv",
        ["id", "created_utc", "mts_created", "updated_utc", "amount_orig", "amount_remaining",
         "daily_rate_pct", "apr_pct", "period_days", "type", "status"],
        [{
            "id": o[0], "created_utc": iso(o[2]), "mts_created": o[2], "updated_utc": iso(o[3]),
            "amount_orig": abs(float(o[5] or 0)), "amount_remaining": abs(float(o[4] or 0)),
            "daily_rate_pct": round(float(o[14] or 0) * 100, 6),
            "apr_pct": round(float(o[14] or 0) * 36500, 4),
            "period_days": o[15], "type": o[6], "status": o[10],
        } for o in offers])

    # 3. 放貸部位(已被借走的 credit)
    # [ID, SYMBOL, SIDE, MTS_CREATE, MTS_UPDATE, AMOUNT(5), ..., RATE(11), PERIOD(12), MTS_OPENING(13), MTS_LAST_PAYOUT(14)]
    credits = paginate(client, f"auth/r/funding/credits/{symbol}/hist", 3)
    stats["credits"] = merge_csv(
        HISTORY_DIR / f"{currency}_funding_credits.csv",
        ["id", "opened_utc", "mts_opening", "amount", "daily_rate_pct", "apr_pct",
         "period_days", "last_payout_utc", "status", "position_pair"],
        [{
            "id": c[0], "opened_utc": iso(c[13]), "mts_opening": c[13],
            "amount": abs(float(c[5] or 0)),
            "daily_rate_pct": round(float(c[11] or 0) * 100, 6),
            "apr_pct": round(float(c[11] or 0) * 36500, 4),
            "period_days": c[12], "last_payout_utc": iso(c[14]),
            "status": c[7], "position_pair": c[21] if len(c) > 21 else "",
        } for c in credits])

    # 4. 入帳利息(ledger category 28 = funding payment)
    # [ID, CURRENCY, _, MTS(3), _, AMOUNT(5), BALANCE(6), _, DESCRIPTION(8)]
    ledger = paginate(client, f"auth/r/ledgers/{currency}/hist", 3,
                      limit=LEDGER_LIMIT, extra={"category": 28})
    stats["interest"] = merge_csv(
        HISTORY_DIR / f"{currency}_interest_payments.csv",
        ["id", "time_utc", "mts", "amount", "wallet_balance", "description"],
        [{
            "id": l[0], "time_utc": iso(l[3]), "mts": l[3],
            "amount": float(l[5] or 0), "wallet_balance": float(l[6] or 0),
            "description": (l[8] or "").replace("\n", " "),
        } for l in ledger])
    return stats


def summarise(currency: str) -> dict:
    """從匯出的 CSV 算一份摘要,方便日後快速看出策略表現。"""
    out: dict = {"currency": currency}
    trades_path = HISTORY_DIR / f"{currency}_funding_trades.csv"
    if trades_path.exists():
        with io.open(trades_path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            amounts = [float(r["amount"]) for r in rows]
            rates = [float(r["daily_rate_pct"]) for r in rows]
            total = sum(amounts)
            out["fills"] = {
                "count": len(rows),
                "first": rows[0]["time_utc"], "last": rows[-1]["time_utc"],
                "total_amount": round(total, 2),
                "weighted_daily_rate_pct": round(sum(a * r for a, r in zip(amounts, rates)) / total, 6),
                "weighted_apr_pct": round(sum(a * r for a, r in zip(amounts, rates)) / total * 365, 3),
                "min_apr_pct": round(min(rates) * 365, 3),
                "max_apr_pct": round(max(rates) * 365, 3),
            }
    interest_path = HISTORY_DIR / f"{currency}_interest_payments.csv"
    if interest_path.exists():
        with io.open(interest_path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            out["interest"] = {
                "count": len(rows),
                "first": rows[0]["time_utc"], "last": rows[-1]["time_utc"],
                "total": round(sum(float(r["amount"]) for r in rows), 6),
            }
    offers_path = HISTORY_DIR / f"{currency}_funding_offers.csv"
    if offers_path.exists():
        with io.open(offers_path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        executed = sum(1 for r in rows if str(r["status"]).startswith("EXECUTED"))
        out["offers"] = {"count": len(rows), "executed": executed,
                         "cancelled": sum(1 for r in rows if "CANCELED" in str(r["status"])),
                         "fill_rate_pct": round(executed / len(rows) * 100, 1) if rows else 0}
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bot.load_dotenv(bot.SCRIPT_DIR / ".env")
    import os
    key, secret = os.environ.get("BFX_API_KEY", ""), os.environ.get("BFX_API_SECRET", "")
    if not key or not secret:
        print("錯誤: 請設定 BFX_API_KEY / BFX_API_SECRET")
        return 1

    config = json.loads((bot.SCRIPT_DIR / "config.json").read_text(encoding="utf-8"))
    currencies = sys.argv[1:] or list(config.get("currencies", {}))
    client = bot.Bitfinex(key, secret)
    HISTORY_DIR.mkdir(exist_ok=True)

    summaries = []
    for ccy in currencies:
        print(f"\n=== 匯出 {ccy} ===")
        try:
            stats = export_currency(client, ccy)
        except Exception as exc:  # noqa: BLE001 — 單一幣別失敗不該中斷其他幣別
            print(f"  失敗: {exc}")
            continue
        for name, count in stats.items():
            print(f"  {name:10s} 累計 {count} 筆")
        summaries.append(summarise(ccy))

    summary_path = HISTORY_DIR / "summary.json"
    summary_path.write_text(json.dumps(
        {"exported_utc": iso(time.time() * 1000), "currencies": summaries},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n摘要已寫入 {summary_path}")
    for s in summaries:
        if "fills" in s:
            f = s["fills"]
            print(f"  {s['currency']}: {f['count']} 筆成交, 加權年化 {f['weighted_apr_pct']}%"
                  f" (區間 {f['min_apr_pct']}~{f['max_apr_pct']}%), {f['first']} ~ {f['last']}")
        if "interest" in s:
            print(f"  {s['currency']}: 累計利息 {s['interest']['total']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
