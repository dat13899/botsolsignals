#!/usr/bin/env python3
"""
bot_sol_signals.py
Bot Telegram gửi tín hiệu MUA/BÁN SOL dựa trên chiến lược EMA + RSI.
- Nguồn giá: Binance WebSocket (kline 1 phút)
- Tín hiệu:
    BUY  khi EMA50 cắt lên EMA200 và RSI < 70
    SELL khi EMA50 cắt xuống EMA200 và RSI > 30
- Biến môi trường cần thiết:
    BOT_TOKEN : token Telegram bot (từ @BotFather)
    CHAT_ID   : id chat hoặc id nhóm nhận tín hiệu
"""

import os
import time
import json
import logging
from collections import deque
from datetime import datetime
import threading

import pandas as pd
import numpy as np
import requests
from websocket import WebSocketApp

# ===== Cấu hình cơ bản =====
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    logging.error("⚠️  Bạn cần set BOT_TOKEN và CHAT_ID trong Environment Variables.")
    raise SystemExit(1)

# Tham số chiến lược (có thể chỉnh bằng ENV nếu muốn)
SYMBOL         = os.getenv("SYMBOL", "solusdt")
INTERVAL       = os.getenv("INTERVAL", "1m")
EMA_SHORT      = int(os.getenv("EMA_SHORT", "50"))
EMA_LONG       = int(os.getenv("EMA_LONG", "200"))
RSI_PERIOD     = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERBOUGHT = int(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD   = int(os.getenv("RSI_OVERSOLD", "30"))

BINANCE_WS = f"wss://stream.binance.com:9443/ws/{SYMBOL}@kline_{INTERVAL}"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Lưu giá đóng để tính indicator
closes = deque(maxlen=1000)
last_signal = None
last_ema_short = None
last_ema_long  = None


def send_telegram(text: str):
    """Gửi message Telegram"""
    try:
        r = requests.post(
            TELEGRAM_URL,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        r.raise_for_status()
        logging.info("✅ Đã gửi tín hiệu Telegram")
    except Exception as e:
        logging.exception("Lỗi gửi Telegram: %s", e)


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Tính RSI với phương pháp EMA"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def on_message(ws, message):
    """Xử lý mỗi khi có dữ liệu mới từ Binance WebSocket"""
    global last_signal, last_ema_short, last_ema_long

    try:
        data = json.loads(message)
        kline = data.get("k", {})
        if not kline.get("x", False):
            # chỉ xử lý khi nến đã đóng
            return

        close_price = float(kline["c"])
        close_time  = kline["T"]
        closes.append(close_price)

        if len(closes) < EMA_LONG + 5:
            return  # chưa đủ dữ liệu tính EMA dài

        s = pd.Series(list(closes))
        ema_short = s.ewm(span=EMA_SHORT, adjust=False).mean().iloc[-1]
        ema_long  = s.ewm(span=EMA_LONG, adjust=False).mean().iloc[-1]
        rsi = compute_rsi(s, RSI_PERIOD).iloc[-1]

        prev_ema_short = last_ema_short
        prev_ema_long  = last_ema_long
        last_ema_short = ema_short
        last_ema_long  = ema_long

        signal = None
        ts = datetime.utcfromtimestamp(close_time/1000).strftime("%Y-%m-%d %H:%M:%S UTC")

        if prev_ema_short is not None and prev_ema_long is not None:
            # Cắt lên: BUY
            if prev_ema_short <= prev_ema_long and ema_short > ema_long and rsi < RSI_OVERBOUGHT:
                signal = "BUY"
            # Cắt xuống: SELL
            elif prev_ema_short >= prev_ema_long and ema_short < ema_long and rsi > RSI_OVERSOLD:
                signal = "SELL"

        if signal and signal != last_signal:
            last_signal = signal
            text = (
                f"*{signal} signal for SOL*\n"
                f"Time: `{ts}`\n"
                f"Price: `{close_price:.4f} USDT`\n"
                f"EMA{EMA_SHORT}: `{ema_short:.4f}`\n"
                f"EMA{EMA_LONG}: `{ema_long:.4f}`\n"
                f"RSI({RSI_PERIOD}): `{rsi:.2f}`\n\n"
                f"_Rule:_ EMA{EMA_SHORT} x EMA{EMA_LONG} + RSI filter\n"
                f"_Lưu ý_: chỉ để tham khảo, không phải khuyến nghị đầu tư."
            )
            send_telegram(text)

    except Exception as e:
        logging.exception("Lỗi on_message: %s", e)


def on_open(ws):
    logging.info("✅ Kết nối Binance WebSocket thành công: %s", BINANCE_WS)


def on_error(ws, error):
    logging.error("❌ WebSocket error: %s", error)


def on_close(ws, code, reason):
    logging.warning("⚠️ WebSocket đóng: %s %s", code, reason)


def run_ws():
    while True:
        try:
            ws = WebSocketApp(
                BINANCE_WS,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            logging.exception("WS loop error: %s", e)
        logging.info("⏳ Reconnect sau 5 giây...")
        time.sleep(5)


if __name__ == "__main__":
    logging.info("🚀 Bot SOL signal khởi động...")
    t = threading.Thread(target=run_ws, daemon=True)
    t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Đã dừng bot.")
