#!/usr/bin/env python3
"""產生儀表板資料 docs/data.json,供 GitHub Pages 顯示。

內容:
  - Bitfinex 帳戶狀態(與本機儀表板相同,重用 dashboard.summary)
  - bot_run.log:這一輪機器人的輸出(由 workflow 產生)
  - 既有 docs/data.json 裡的歷史執行紀錄(保留最近 MAX_RUNS 筆)
  - config.json:讓儀表板的設定頁顯示機器人實際在跑的參數
  - DASHBOARD_PASSWORD 的 PBKDF2 雜湊,供設定頁比對密碼

注意:這份檔案是**明文**的,任何人打開 GitHub Pages 都看得到餘額與收益。
這是刻意的選擇(看板公開、只有設定區要密碼)。若要改回加密,見 README。

密碼雜湊用 PBKDF2-HMAC-SHA256(300k 次)+ 隨機 salt。雜湊是公開的,所以
DASHBOARD_PASSWORD 必須夠長夠亂(預設產生 20 字元隨機字串),否則會被離線暴力破解。
而且這道檢查只在瀏覽器端執行,擋不住懂 DevTools 的人 —— 真正的寫入權限
一律來自 GitHub 登入,設定頁本身不會、也不能改動 repo。

環境變數:BFX_API_KEY / BFX_API_SECRET / DASHBOARD_PASSWORD / BOT_EXIT / DRY_RUN
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import bot
from dashboard import summary

DOCS = bot.SCRIPT_DIR / "docs"
DATA_PATH = DOCS / "data.json"
LEGACY_ENC = DOCS / "data.enc"
LOG_PATH = bot.SCRIPT_DIR / "bot_run.log"
PBKDF2_ITERS = 300_000
MAX_RUNS = 50
MAX_LOG_CHARS = 8000


def password_digest(password: str) -> dict:
    """給設定頁比對用的密碼雜湊(salt 每次發佈都換新的)。"""
    salt = os.urandom(16)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERS)
    return {
        "salt": base64.b64encode(salt).decode(),
        "iters": PBKDF2_ITERS,
        "hash": base64.b64encode(kdf.derive(password.encode())).decode(),
    }


def previous_runs() -> list[dict]:
    if not DATA_PATH.exists():
        return []
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8")).get("runs", [])
    except Exception as exc:  # noqa: BLE001 — 檔案損毀就重新開始
        print(f"讀不到既有執行紀錄,重新開始: {exc}")
        return []


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bot.load_dotenv(bot.SCRIPT_DIR / ".env")

    password = os.environ.get("DASHBOARD_PASSWORD", "").strip().lstrip("﻿")
    key = os.environ.get("BFX_API_KEY", "")
    secret = os.environ.get("BFX_API_SECRET", "")
    if not key or not secret:
        print("錯誤: 缺少 BFX_API_KEY / BFX_API_SECRET")
        return 1

    runs = previous_runs()
    if LOG_PATH.exists():
        runs.insert(0, {
            "ts_ms": int(time.time() * 1000),
            "ok": os.environ.get("BOT_EXIT", "0") == "0",
            "dry_run": os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
            "log": LOG_PATH.read_text(encoding="utf-8-sig", errors="replace")[-MAX_LOG_CHARS:],
        })
        runs = runs[:MAX_RUNS]

    config = json.loads((bot.SCRIPT_DIR / "config.json").read_text(encoding="utf-8"))
    client = bot.Bitfinex(key, secret)
    currencies: dict[str, dict] = {}
    for ccy, cfg in config.get("currencies", {}).items():
        if not cfg.get("enabled"):
            continue
        try:
            currencies[ccy] = summary(client, ccy)
        except Exception as exc:  # noqa: BLE001
            currencies[ccy] = {"error": str(exc)}

    payload = {
        "generated_ms": int(time.time() * 1000),
        "currencies": currencies,
        "runs": runs,
        "config": config,
    }
    if password:
        payload["password"] = password_digest(password)
    else:
        print("警告: 未設定 DASHBOARD_PASSWORD,設定頁將無法解鎖")

    DOCS.mkdir(exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if LEGACY_ENC.exists():  # 舊的加密檔已不再使用
        LEGACY_ENC.unlink()
        print(f"已移除舊的 {LEGACY_ENC.name}")
    print(f"已寫入 {DATA_PATH}(幣別: {', '.join(currencies) or '無'},執行紀錄 {len(runs)} 筆)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
