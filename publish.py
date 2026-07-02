#!/usr/bin/env python3
"""產生加密的儀表板資料 docs/data.enc,給 GitHub Pages 前端解密顯示。

收集三種資料:
  - Bitfinex 帳戶狀態(與本機儀表板相同,重用 dashboard.summary)
  - bot_run.log:這一輪機器人的輸出(由 workflow 產生)
  - 既有 docs/data.enc 裡的歷史執行紀錄(解密後保留最近 MAX_RUNS 筆)

加密:PBKDF2-HMAC-SHA256(300k 次) 衍生金鑰 + AES-256-GCM。
瀏覽器端用 WebCrypto 以相同參數解密,密碼不會出現在任何伺服器上。

環境變數:BFX_API_KEY / BFX_API_SECRET / DASHBOARD_PASSWORD / BOT_EXIT / DRY_RUN
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import bot
from dashboard import summary

DOCS = bot.SCRIPT_DIR / "docs"
ENC_PATH = DOCS / "data.enc"
LOG_PATH = bot.SCRIPT_DIR / "bot_run.log"
PBKDF2_ITERS = 300_000
MAX_RUNS = 50
MAX_LOG_CHARS = 8000


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERS)
    return kdf.derive(password.encode())


def encrypt(password: str, payload: dict) -> str:
    salt, iv = os.urandom(16), os.urandom(12)
    plaintext = json.dumps(payload, ensure_ascii=False).encode()
    ct = AESGCM(_derive_key(password, salt)).encrypt(iv, plaintext, None)
    return json.dumps({
        "v": 1,
        "kdf_iters": PBKDF2_ITERS,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
    })


def decrypt(password: str, text: str) -> dict:
    blob = json.loads(text)
    salt, iv, ct = (base64.b64decode(blob[k]) for k in ("salt", "iv", "ct"))
    plaintext = AESGCM(_derive_key(password, salt)).decrypt(iv, ct, None)
    return json.loads(plaintext.decode())


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    bot.load_dotenv(bot.SCRIPT_DIR / ".env")

    password = os.environ.get("DASHBOARD_PASSWORD", "").strip().lstrip("﻿")
    key = os.environ.get("BFX_API_KEY", "")
    secret = os.environ.get("BFX_API_SECRET", "")
    if not password:
        print("未設定 DASHBOARD_PASSWORD,跳過儀表板發佈")
        return 0
    if not key or not secret:
        print("錯誤: 缺少 BFX_API_KEY / BFX_API_SECRET")
        return 1

    # 保留舊的執行紀錄
    runs: list[dict] = []
    if ENC_PATH.exists():
        try:
            runs = decrypt(password, ENC_PATH.read_text(encoding="utf-8")).get("runs", [])
        except Exception as exc:  # noqa: BLE001 — 密碼換過或檔案損毀時重新開始
            print(f"無法解密既有資料,歷史紀錄重新開始: {exc}")

    # 加入這一輪的 log
    if LOG_PATH.exists():
        runs.insert(0, {
            "ts_ms": int(time.time() * 1000),
            "ok": os.environ.get("BOT_EXIT", "0") == "0",
            "dry_run": os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
            "log": LOG_PATH.read_text(encoding="utf-8-sig", errors="replace")[-MAX_LOG_CHARS:],
        })
        runs = runs[:MAX_RUNS]

    # 帳戶即時狀態
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
    }
    DOCS.mkdir(exist_ok=True)
    ENC_PATH.write_text(encrypt(password, payload), encoding="utf-8")
    print(f"已寫入 {ENC_PATH}(幣別: {', '.join(currencies) or '無'},執行紀錄 {len(runs)} 筆)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
