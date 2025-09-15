#!/usr/bin/env python3
"""
bot_sol_signals.py
Bot gửi tín hiệu EMA/RSI và phản hồi lệnh /gia để trả giá hiện tại SOL/USDT.
"""

import os
import time
import json
import logging
from collections import deque
from datetime import datetime
import threading
import requests
import pandas as pd
import numpy as np
from websocket import WebSocketApp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ===== Cấu hình =====
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise EnvironmentError(
        "⚠️ Chưa tìm thấy BOT_TOKEN hoặc CHAT_ID. "
        "Hãy vào Railway > Project > Variables để thêm chúng."
    )

SYMBOL         = os.getenv("SYMBOL", "solusdt")
INTERVAL       = os.getenv("INTERVAL", "1m")
EMA_SHORT      = int(os.getenv("EMA_SHORT", "50"))
EMA_LONG       = int(os.getenv("EMA_LONG", "200"))
RSI_PERIOD     = int(os.getenv("RSI_PERIOD", "14"))
RSI_OVERBOUGHT = int(os.getenv("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD   = int(os.getenv("RSI_OVERSOLD", "30"))

BINANCE_WS = f"wss://stream.binance.com:9443/ws/{SYMBOL}@kline_{INTERVAL}"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

closes = deque(maxlen=1000)
last_signal = None
last_ema_short = None
last_ema_long  = None

# --------- Hàm tiện ích ---------
def send_telegram(text: str):
    """Gửi message Telegram chủ động (không qua handler)."""
    try:
        r = requests.post(
            TELEGRAM_URL,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
        r.raise_for_status()
        logging.info("✅ Đã gửi tin nhắn Telegram")
    except Exception as e:
        logging.exception("Lỗi gửi Telegram: %s", e)


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# --------- WebSocket xử lý tín hiệu ---------
def on_message(ws, message):
    global last_signal, last_ema_short, last_ema_long

    try:
        data = json.loads(message)
        kline = data.get("k", {})
        if not kline.get("x", False):
            return

        close_price = float(kline["c"])
        close_time  = kline["T"]
        closes.append(close_price)

        if len(closes) < EMA_LONG + 5:
            return

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
            if prev_ema_short <= prev_ema_long and ema_short > ema_long and rsi < RSI_OVERBOUGHT:
                signal = "BUY"
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
    send_telegram("🚀 Bot SOL đã kết nối thành công tới Binance WebSocket và sẵn sàng gửi tín hiệu!")


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

# --------- Lệnh Telegram: /gia ---------
async def gia_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Trả về giá hiện tại SOL/USDT khi gõ /gia"""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        price = float(r.json()["price"])
        await update.message.reply_text(
            f"💰 Giá SOL/USDT hiện tại: {price:.4f} USDT"
        )
    except Exception as e:
        logging.exception("Lỗi lấy giá: %s", e)
        await update.message.reply_text("⚠️ Không lấy được giá hiện tại.")


def run_telegram_commands():
    """Chạy bot Telegram để xử lý lệnh /gia"""
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("gia", gia_handler))
    app.run_polling()


if __name__ == "__main__":
    logging.info("🚀 Bot SOL signal khởi động...")

    # Thread 1: WebSocket tín hiệu
    t_ws = threading.Thread(target=run_ws, daemon=True)
    t_ws.start()

    # Thread 2: Bot Telegram cho lệnh /gia
    run_telegram_commands()
