#!/usr/bin/env python3
"""
bot_sol_signals.py

Multi-indicator SOL signal bot:
- Indicators: EMA(50/200), MACD(12,26,9), RSI(14), Bollinger(20,2), ATR(14), Supertrend
- Signal requires confirmation of multiple indicators.
- TP/SL dynamic using ATR; suggested position sizing from ACCOUNT_BALANCE and RISK_PERCENT.
- Stores signals to SQLite 'signals.db'.
- Supports Telegram commands: /gia (current price), /params (show current config).

Environment variables required:
- BOT_TOKEN (Telegram token)
- CHAT_ID   (chat id or group)
Optional / tuning:
- SYMBOL (default solusdt)
- INTERVAL (kline interval, default 1m)
- EMA_SHORT, EMA_LONG, RSI_PERIOD, ATR_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL
- K_SL (ATR multiplier for SL), TP_R (risk:reward ratio)
- ACCOUNT_BALANCE (in USDT, default 1000)
- RISK_PERCENT (percent risk per trade, default 1)
"""

import os
import time
import json
import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime

import requests
import numpy as np
import pandas as pd
from websocket import WebSocketApp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- CONFIG / ENV ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise EnvironmentError("BOT_TOKEN và CHAT_ID cần đặt trong Environment Variables của Railway.")

# symbol & intervals
SYMBOL   = os.getenv("SYMBOL", "solusdt").lower()
INTERVAL = os.getenv("INTERVAL", "1m")

# Indicators params
EMA_SHORT = int(os.getenv("EMA_SHORT", "50"))
EMA_LONG  = int(os.getenv("EMA_LONG", "200"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
MACD_FAST = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("MACD_SIGNAL", "9"))
BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_STD_MULT = float(os.getenv("BB_STD_MULT", "2"))

# Trade sizing & TP/SL
K_SL = float(os.getenv("K_SL", "1.5"))          # SL distance = K_SL * ATR
TP_R = float(os.getenv("TP_R", "2.0"))          # TP = entry + TP_R * SL_distance
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "1000"))  # USDT
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.0"))        # percent per trade

# Other
MIN_CONFIRM = int(os.getenv("MIN_CONFIRM", "2"))  # require at least N indicators confirm
MIN_BARS = max(EMA_LONG, BB_PERIOD, ATR_PERIOD, MACD_SLOW) + 5

BINANCE_WS = f"wss://stream.binance.com:9443/ws/{SYMBOL}@kline_{INTERVAL}"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Data storage for klines
# We'll store lists for open/high/low/close/volume
MAX_BARS = 2000
opens = deque(maxlen=MAX_BARS)
highs = deque(maxlen=MAX_BARS)
lows  = deque(maxlen=MAX_BARS)
closes = deque(maxlen=MAX_BARS)
vols  = deque(maxlen=MAX_BARS)

# Signals & state
last_signal = None  # "BUY" or "SELL" or None
last_signal_time = None

DB_PATH = "signals.db"

# ---------------- Database ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        symbol TEXT,
        signal TEXT,
        entry REAL,
        sl REAL,
        tp REAL,
        rr REAL,
        suggested_size REAL,
        indicators TEXT
    )
    """)
    conn.commit()
    return conn

db_conn = init_db()

# ---------------- Utilities & Indicators ----------------
def send_telegram(text: str):
    """Send proactive telegram message to configured CHAT_ID"""
    try:
        resp = requests.post(TELEGRAM_URL, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        resp.raise_for_status()
        logging.info("Telegram message sent")
    except Exception as e:
        logging.exception("Failed to send telegram: %s", e)

def get_current_price_rest():
    """Get current price via Binance REST (used by /gia)"""
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL.upper()}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        logging.exception("get_current_price_rest error: %s", e)
        return None

def df_from_deques():
    df = pd.DataFrame({
        "open": list(opens),
        "high": list(highs),
        "low": list(lows),
        "close": list(closes),
        "volume": list(vols)
    })
    return df

def compute_ema(series: pd.Series, span: int):
    return series.ewm(span=span, adjust=False).mean()

def compute_rsi(series: pd.Series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi

def compute_macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=signal, adjust=False).mean()
    macd_hist = macd_line - macd_signal
    return macd_line, macd_signal, macd_hist

def compute_bollinger(series: pd.Series, period=20, std_mult=2.0):
    ma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = ma + std_mult * std
    lower = ma - std_mult * std
    return upper, ma, lower

def compute_atr(df: pd.DataFrame, period=14):
    high_low = df['high'] - df['low']
    high_prev_close = (df['high'] - df['close'].shift()).abs()
    low_prev_close  = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    return atr

def compute_supertrend(df: pd.DataFrame, period=7, multiplier=3):
    # Simple Supertrend implementation (used for trailing)
    atr = compute_atr(df, period)
    hl2 = (df['high'] + df['low']) / 2
    upperband = hl2 + multiplier * atr
    lowerband = hl2 - multiplier * atr
    supertrend = pd.Series(index=df.index)
    trend = True  # True = up
    final_upper = upperband.copy()
    final_lower = lowerband.copy()
    for i in range(len(df)):
        if i == 0:
            supertrend.iat[i] = final_lower.iat[i]
            continue
        if df['close'].iat[i] > final_upper.iat[i-1]:
            trend = True
        elif df['close'].iat[i] < final_lower.iat[i-1]:
            trend = False
        if trend:
            final_lower.iat[i] = max(final_lower.iat[i], final_lower.iat[i-1])
            supertrend.iat[i] = final_lower.iat[i]
        else:
            final_upper.iat[i] = min(final_upper.iat[i], final_upper.iat[i-1])
            supertrend.iat[i] = final_upper.iat[i]
    return supertrend

# ---------------- Evaluate signal ----------------
def evaluate_signal(df: pd.DataFrame):
    """
    Return dict with keys:
    - signal: "BUY"/"SELL"/None
    - entry, sl, tp, rr, suggested_size, reasons(list)
    """
    result = {"signal": None, "reasons": []}
    close = df['close']
    high = df['high']
    low = df['low']
    vol = df['volume']

    # indicators
    ema_short = compute_ema(close, EMA_SHORT).iloc[-1]
    ema_long  = compute_ema(close, EMA_LONG).iloc[-1]
    prev_ema_short = compute_ema(close, EMA_SHORT).iloc[-2]
    prev_ema_long  = compute_ema(close, EMA_LONG).iloc[-2]

    macd_line, macd_signal, macd_hist = compute_macd(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    macd_line_v = macd_line.iloc[-1]; macd_signal_v = macd_signal.iloc[-1]; macd_hist_v = macd_hist.iloc[-1]
    macd_prev_hist = macd_hist.iloc[-2] if len(macd_hist) > 1 else 0

    rsi_v = compute_rsi(close, RSI_PERIOD).iloc[-1]

    bb_upper, bb_mid, bb_lower = compute_bollinger(close, BB_PERIOD, BB_STD_MULT)
    bb_upper_v = bb_upper.iloc[-1]; bb_mid_v = bb_mid.iloc[-1]; bb_lower_v = bb_lower.iloc[-1]

    atr = compute_atr(df, ATR_PERIOD).iloc[-1]

    # volume confirmation: last vol > 1.1 * avg20
    vol_avg20 = vol.rolling(window=20).mean().iloc[-1] if len(vol) >= 20 else vol.mean()
    vol_confirm = vol.iloc[-1] > (vol_avg20 * 1.05) if vol_avg20 and not np.isnan(vol_avg20) else False

    confirms = 0
    reasons = []

    # EMA crossover
    ema_cross_up = (prev_ema_short <= prev_ema_long) and (ema_short > ema_long)
    ema_cross_down = (prev_ema_short >= prev_ema_long) and (ema_short < ema_long)
    if ema_cross_up:
        confirms += 1
        reasons.append("EMA_cross_up")
    if ema_cross_down:
        confirms += 1
        reasons.append("EMA_cross_down")

    # MACD: line > signal and hist increasing
    macd_ok_long = (macd_line_v > macd_signal_v) and (macd_hist_v > macd_prev_hist)
    macd_ok_short = (macd_line_v < macd_signal_v) and (macd_hist_v < macd_prev_hist)
    if macd_ok_long:
        confirms += 1
        reasons.append("MACD_long")
    if macd_ok_short:
        confirms += 1
        reasons.append("MACD_short")

    # RSI filter
    if rsi_v < 70:
        reasons.append("RSI_not_overbought")
    if rsi_v > 30:
        reasons.append("RSI_not_oversold")

    # Bollinger breakout / mean-reversion hint
    price_v = close.iloc[-1]
    if price_v > bb_mid_v:
        reasons.append("price_above_BBmid")
    else:
        reasons.append("price_below_BBmid")

    # Volume
    if vol_confirm:
        confirms += 1
        reasons.append("volume_confirm")

    # Decision rules: require at least MIN_CONFIRM confirms plus EMA+MACD agreement
    # BUY: ema_cross_up and macd_ok_long and confirms >= MIN_CONFIRM
    if ema_cross_up and macd_ok_long and confirms >= MIN_CONFIRM and rsi_v < 85:
        entry = price_v
        sl_distance = K_SL * atr if atr > 0 else 0.0
        sl = entry - sl_distance
        tp = entry + TP_R * sl_distance
        # sizing
        risk_amount = (RISK_PERCENT / 100.0) * ACCOUNT_BALANCE
        suggested_size = (risk_amount / sl_distance) if sl_distance > 0 else 0.0
        result.update({
            "signal": "BUY",
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "rr": float(TP_R),
            "suggested_size": float(suggested_size),
            "reasons": reasons
        })
        return result

    # SELL (short) decision (note: many traders avoid shorting on margin; this is symmetric)
    if ema_cross_down and macd_ok_short and confirms >= MIN_CONFIRM and rsi_v > 15:
        entry = price_v
        sl_distance = K_SL * atr if atr > 0 else 0.0
        sl = entry + sl_distance
        tp = entry - TP_R * sl_distance
        risk_amount = (RISK_PERCENT / 100.0) * ACCOUNT_BALANCE
        suggested_size = (risk_amount / sl_distance) if sl_distance > 0 else 0.0
        result.update({
            "signal": "SELL",
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "rr": float(TP_R),
            "suggested_size": float(suggested_size),
            "reasons": reasons
        })
        return result

    return result

# ---------------- WebSocket handlers ----------------
def on_message(ws, message):
    global last_signal, last_signal_time
    try:
        data = json.loads(message)
        k = data.get("k", {})
        if not k.get("x", False):
            return  # only process closed candles

        o = float(k.get("o"))
        h = float(k.get("h"))
        l = float(k.get("l"))
        c = float(k.get("c"))
        v = float(k.get("v"))
        t_close = k.get("T")

        opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)

        if len(closes) < MIN_BARS:
            return

        df = df_from_deques()
        eval_res = evaluate_signal(df)

        if eval_res.get("signal") and eval_res["signal"] != last_signal:
            last_signal = eval_res["signal"]
            last_signal_time = datetime.utcfromtimestamp(t_close/1000).strftime("%Y-%m-%d %H:%M:%S UTC")
            # prepare message
            text = (
                f"*{eval_res['signal']} signal for {SYMBOL.upper()}*\n"
                f"Time: `{last_signal_time}`\n"
                f"Entry: `{eval_res['entry']:.6f}`\n"
                f"SL: `{eval_res['sl']:.6f}`\n"
                f"TP: `{eval_res['tp']:.6f}`\n"
                f"R:R: `{eval_res['rr']:.2f}`\n"
                f"Suggested size (units): `{eval_res['suggested_size']:.6f}`\n"
                f"Reasons: `{', '.join(eval_res.get('reasons',[]))}`\n\n"
                "_Lưu ý_: Đây là tín hiệu tự động, chỉ để tham khảo. Quản lý rủi ro khi trade._"
            )
            send_telegram(text)
            # save to DB
            try:
                cur = db_conn.cursor()
                cur.execute(
                    "INSERT INTO signals (ts, symbol, signal, entry, sl, tp, rr, suggested_size, indicators) VALUES (?,?,?,?,?,?,?,?,?)",
                    (last_signal_time, SYMBOL.upper(), eval_res['signal'], eval_res['entry'], eval_res['sl'],
                     eval_res['tp'], eval_res['rr'], eval_res['suggested_size'], json.dumps(eval_res.get('reasons',[])))
                )
                db_conn.commit()
            except Exception as e:
                logging.exception("DB insert error: %s", e)

    except Exception as e:
        logging.exception("on_message error: %s", e)

def on_open(ws):
    logging.info("Connected to Binance websocket: %s", BINANCE_WS)
    send_telegram(f"🚀 Bot connected to Binance WS for *{SYMBOL.upper()}* and ready to generate signals.")

def on_error(ws, error):
    logging.error("WebSocket error: %s", error)
    send_telegram(f"⚠️ WebSocket error: {error}")

def on_close(ws, code, reason):
    logging.warning("WebSocket closed: %s %s", code, reason)
    send_telegram(f"⚠️ WebSocket closed: {code} {reason}")

def run_ws():
    while True:
        try:
            ws = WebSocketApp(BINANCE_WS, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            logging.exception("WS reconnect error: %s", e)
        time.sleep(5)

# ---------------- Telegram command handlers ----------------
async def gia_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = get_current_price_rest()
    if price:
        await update.message.reply_text(f"💰 Giá {SYMBOL.upper()} hiện tại: {price:.6f} USDT")
    else:
        await update.message.reply_text("⚠️ Không lấy được giá hiện tại.")

async def params_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"Current params:\n"
        f"SYMBOL={SYMBOL.upper()}, INTERVAL={INTERVAL}\n"
        f"EMA_SHORT={EMA_SHORT}, EMA_LONG={EMA_LONG}\n"
        f"MACD={MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}\n"
        f"RSI={RSI_PERIOD}, ATR={ATR_PERIOD}\n"
        f"K_SL={K_SL}, TP_R={TP_R}\n"
        f"ACCOUNT_BALANCE={ACCOUNT_BALANCE}, RISK%={RISK_PERCENT}\n"
    )
    await update.message.reply_text(text)

def run_telegram_commands():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("gia", gia_handler))
    app.add_handler(CommandHandler("params", params_handler))
    app.run_polling()

# ---------------- Main ----------------
if __name__ == "__main__":
    logging.info("Starting multi-indicator SOL signal bot...")
    t_ws = threading.Thread(target=run_ws, daemon=True)
    t_ws.start()

    # Telegram commands runs in main thread (blocking)
    run_telegram_commands()
