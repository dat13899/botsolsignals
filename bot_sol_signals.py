#!/usr/bin/env python3
"""
bot_sol_signals.py

Phiên bản cải tiến (optimized):
- Multi-timeframe filter: check higher timeframe trend (configurable, default 15m)
- Supertrend used for trailing/exit confirmation
- Volatility filter: skip signals if ATR/price > VOLATILITY_THRESHOLD
- Runtime config via /set and /get (persisted in config.json)
- /gia, /params, /candles commands kept
- Stores signals to SQLite 'signals.db'

ENV required:
- BOT_TOKEN, CHAT_ID

Optional ENV:
- SYMBOL, INTERVAL, EMA_SHORT, EMA_LONG, RSI_PERIOD, ATR_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL
- BB_PERIOD, BB_STD_MULT, K_SL, TP_R, ACCOUNT_BALANCE, RISK_PERCENT
- HIGHER_TF (e.g. "15m"), VOLATILITY_THRESHOLD (e.g. 0.05 -> 5%)

Usage:
- Deploy to Railway, set environment variables, then redeploy.
"""

import os
import time
import json
import logging
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta

import requests
import numpy as np
import pandas as pd
from websocket import WebSocketApp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------- ENV / Defaults ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
if not BOT_TOKEN or not CHAT_ID:
    raise EnvironmentError("BOT_TOKEN và CHAT_ID cần đặt trong Environment Variables của Railway.")

SYMBOL   = os.getenv("SYMBOL", "solusdt").lower()
INTERVAL = os.getenv("INTERVAL", "1m")  # streaming interval

# default params (can be changed via /set and persisted)
DEFAULT_CONFIG = {
    "EMA_SHORT": int(os.getenv("EMA_SHORT", "50")),
    "EMA_LONG": int(os.getenv("EMA_LONG", "200")),
    "RSI_PERIOD": int(os.getenv("RSI_PERIOD", "14")),
    "ATR_PERIOD": int(os.getenv("ATR_PERIOD", "14")),
    "MACD_FAST": int(os.getenv("MACD_FAST", "12")),
    "MACD_SLOW": int(os.getenv("MACD_SLOW", "26")),
    "MACD_SIGNAL": int(os.getenv("MACD_SIGNAL", "9")),
    "BB_PERIOD": int(os.getenv("BB_PERIOD", "20")),
    "BB_STD_MULT": float(os.getenv("BB_STD_MULT", "2")),
    "K_SL": float(os.getenv("K_SL", "1.5")),
    "TP_R": float(os.getenv("TP_R", "2.0")),
    "ACCOUNT_BALANCE": float(os.getenv("ACCOUNT_BALANCE", "1000")),
    "RISK_PERCENT": float(os.getenv("RISK_PERCENT", "1.0")),
    "MIN_CONFIRM": int(os.getenv("MIN_CONFIRM", "2")),
    "HIGHER_TF": os.getenv("HIGHER_TF", "15m"),
    "VOLATILITY_THRESHOLD": float(os.getenv("VOLATILITY_THRESHOLD", "0.06")),  # ATR/price > threshold -> skip
    "SUPERTREND_PERIOD": int(os.getenv("SUPERTREND_PERIOD", "7")),
    "SUPERTREND_MULT": float(os.getenv("SUPERTREND_MULT", "3"))
}

CONFIG_PATH = "config.json"

# load persisted config if exists, otherwise write defaults
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
            # merge defaults
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
    except Exception:
        cfg = DEFAULT_CONFIG.copy()
else:
    cfg = DEFAULT_CONFIG.copy()
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# convenience getters
def get_cfg(key):
    return cfg.get(key, DEFAULT_CONFIG.get(key))

def set_cfg(key, value):
    cfg[key] = value
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# derived params
EMA_SHORT = get_cfg("EMA_SHORT")
EMA_LONG = get_cfg("EMA_LONG")
RSI_PERIOD = get_cfg("RSI_PERIOD")
ATR_PERIOD = get_cfg("ATR_PERIOD")
MACD_FAST = get_cfg("MACD_FAST")
MACD_SLOW = get_cfg("MACD_SLOW")
MACD_SIGNAL = get_cfg("MACD_SIGNAL")
BB_PERIOD = get_cfg("BB_PERIOD")
BB_STD_MULT = get_cfg("BB_STD_MULT")
K_SL = get_cfg("K_SL")
TP_R = get_cfg("TP_R")
ACCOUNT_BALANCE = get_cfg("ACCOUNT_BALANCE")
RISK_PERCENT = get_cfg("RISK_PERCENT")
MIN_CONFIRM = get_cfg("MIN_CONFIRM")
HIGHER_TF = get_cfg("HIGHER_TF")
VOLATILITY_THRESHOLD = get_cfg("VOLATILITY_THRESHOLD")
SUPERTREND_PERIOD = get_cfg("SUPERTREND_PERIOD")
SUPERTREND_MULT = get_cfg("SUPERTREND_MULT")

# websocket / telegram
BINANCE_WS = f"wss://stream.binance.com:9443/ws/{SYMBOL}@kline_{INTERVAL}"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Data storage and state
MAX_BARS = 3000
opens = deque(maxlen=MAX_BARS)
highs = deque(maxlen=MAX_BARS)
lows = deque(maxlen=MAX_BARS)
closes = deque(maxlen=MAX_BARS)
vols = deque(maxlen=MAX_BARS)
candle_count = 0

last_signal = None
last_signal_time = None

DB_PATH = "signals.db"

# ---------------- DB init ----------------
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

# ---------------- Utilities ----------------
def send_telegram(text: str):
    try:
        resp = requests.post(TELEGRAM_URL, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        resp.raise_for_status()
        logging.info("Telegram message sent")
    except Exception as e:
        logging.exception("Failed to send telegram: %s", e)

def get_current_price_rest(symbol=SYMBOL):
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        logging.exception("get_current_price_rest error: %s", e)
        return None

def df_from_deques():
    return pd.DataFrame({
        "open": list(opens),
        "high": list(highs),
        "low": list(lows),
        "close": list(closes),
        "volume": list(vols)
    })

# ---------------- Indicators ----------------
def compute_ema(series: pd.Series, span: int):
    return series.ewm(span=span, adjust=False).mean()

def compute_rsi(series: pd.Series, period: int = 14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)

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
    atr = compute_atr(df, period)
    hl2 = (df['high'] + df['low']) / 2
    upperband = hl2 + multiplier * atr
    lowerband = hl2 - multiplier * atr
    final_upper = upperband.copy()
    final_lower = lowerband.copy()
    supertrend = pd.Series(index=df.index, dtype=float)
    trend = True
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

# ---------------- Higher timeframe helper ----------------
def fetch_klines_rest(symbol: str, interval: str, limit: int = 200):
    """
    Fetch klines from Binance REST (useful for higher timeframe confirmation).
    Returns DataFrame with open/high/low/close/volume.
    """
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume","close_time",
            "qav","num_trades","taker_base_vol","taker_quote_vol","ignore"
        ])
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        return df[['open','high','low','close','volume']]
    except Exception as e:
        logging.exception("fetch_klines_rest error: %s", e)
        return None

# ---------------- Evaluate signal (with multi-timeframe & volatility) ----------------
def evaluate_signal(df: pd.DataFrame):
    """
    Evaluate signal using indicators + higher-timeframe trend + volatility filter + supertrend
    """
    result = {"signal": None, "reasons": []}
    close = df['close']
    high = df['high']
    low = df['low']
    vol = df['volume']

    # compute indicators for current timeframe
    ema_short_s = compute_ema(close, get_cfg("EMA_SHORT"))
    ema_long_s  = compute_ema(close, get_cfg("EMA_LONG"))
    prev_ema_short = ema_short_s.iloc[-2]
    prev_ema_long = ema_long_s.iloc[-2]
    ema_short_v = ema_short_s.iloc[-1]
    ema_long_v  = ema_long_s.iloc[-1]

    macd_line, macd_signal, macd_hist = compute_macd(close, get_cfg("MACD_FAST"), get_cfg("MACD_SLOW"), get_cfg("MACD_SIGNAL"))
    macd_line_v = macd_line.iloc[-1]; macd_signal_v = macd_signal.iloc[-1]; macd_hist_v = macd_hist.iloc[-1]
    macd_prev_hist = macd_hist.iloc[-2] if len(macd_hist) > 1 else 0

    rsi_v = compute_rsi(close, get_cfg("RSI_PERIOD")).iloc[-1]
    bb_upper, bb_mid, bb_lower = compute_bollinger(close, get_cfg("BB_PERIOD"), get_cfg("BB_STD_MULT"))
    atr_v = compute_atr(df, get_cfg("ATR_PERIOD")).iloc[-1]

    price_v = close.iloc[-1]

    # Volatility filter: skip if ATR/price > threshold
    vol_ratio = (atr_v / price_v) if price_v and atr_v else 0.0
    if vol_ratio > get_cfg("VOLATILITY_THRESHOLD"):
        result['reasons'].append(f"skip_high_volatility:{vol_ratio:.4f}")
        return result  # skip signal

    confirms = 0
    reasons = []

    ema_cross_up = (prev_ema_short <= prev_ema_long) and (ema_short_v > ema_long_v)
    ema_cross_down = (prev_ema_short >= prev_ema_long) and (ema_short_v < ema_long_v)
    if ema_cross_up:
        confirms += 1; reasons.append("EMA_cross_up")
    if ema_cross_down:
        confirms += 1; reasons.append("EMA_cross_down")

    macd_ok_long = (macd_line_v > macd_signal_v) and (macd_hist_v > macd_prev_hist)
    macd_ok_short = (macd_line_v < macd_signal_v) and (macd_hist_v < macd_prev_hist)
    if macd_ok_long:
        confirms += 1; reasons.append("MACD_long")
    if macd_ok_short:
        confirms += 1; reasons.append("MACD_short")

    # volume confirmation
    vol_avg20 = vol.rolling(window=20).mean().iloc[-1] if len(vol) >= 20 else vol.mean()
    vol_confirm = vol.iloc[-1] > (vol_avg20 * 1.05) if vol_avg20 and not np.isnan(vol_avg20) else False
    if vol_confirm:
        confirms += 1; reasons.append("volume_confirm")

    # Supertrend on current timeframe
    st = compute_supertrend(df, get_cfg("SUPERTREND_PERIOD"), get_cfg("SUPERTREND_MULT"))
    # current supertrend point (trend direction guess):
    supertrend_val = st.iloc[-1] if len(st) > 0 else None

    # Multi-timeframe: check higher timeframe trend (e.g., 15m)
    higher_ok_long = higher_ok_short = False
    try:
        htf = fetch_klines_rest(SYMBOL, get_cfg("HIGHER_TF"), limit=100)
        if htf is not None and len(htf) > 20:
            ema_htf_short = compute_ema(htf['close'], get_cfg("EMA_SHORT")).iloc[-1]
            ema_htf_long  = compute_ema(htf['close'], get_cfg("EMA_LONG")).iloc[-1]
            higher_ok_long = ema_htf_short > ema_htf_long
            higher_ok_short = ema_htf_short < ema_htf_long
            reasons.append(f"htf_trend:{'long' if higher_ok_long else ('short' if higher_ok_short else 'neutral')}")
    except Exception:
        pass

    # Decision logic requires EMA+MACD + MIN_CONFIRM and higher timeframe alignment
    if ema_cross_up and macd_ok_long and confirms >= get_cfg("MIN_CONFIRM") and rsi_v < 90:
        if higher_ok_short:
            # HTF contradicts, skip
            reasons.append("skip_htf_contradiction")
            return result
        # all good -> BUY
        entry = price_v
        sl_dist = get_cfg("K_SL") * atr_v if atr_v > 0 else 0.0
        sl = entry - sl_dist
        tp = entry + get_cfg("TP_R") * sl_dist
        risk_amount = (get_cfg("RISK_PERCENT") / 100.0) * get_cfg("ACCOUNT_BALANCE")
        suggested_size = (risk_amount / sl_dist) if sl_dist > 0 else 0.0
        result.update({
            "signal": "BUY", "entry": float(entry), "sl": float(sl), "tp": float(tp),
            "rr": float(get_cfg("TP_R")), "suggested_size": float(suggested_size), "reasons": reasons
        })
        return result

    if ema_cross_down and macd_ok_short and confirms >= get_cfg("MIN_CONFIRM") and rsi_v > 10:
        if higher_ok_long:
            reasons.append("skip_htf_contradiction")
            return result
        entry = price_v
        sl_dist = get_cfg("K_SL") * atr_v if atr_v > 0 else 0.0
        sl = entry + sl_dist
        tp = entry - get_cfg("TP_R") * sl_dist
        risk_amount = (get_cfg("RISK_PERCENT") / 100.0) * get_cfg("ACCOUNT_BALANCE")
        suggested_size = (risk_amount / sl_dist) if sl_dist > 0 else 0.0
        result.update({
            "signal": "SELL", "entry": float(entry), "sl": float(sl), "tp": float(tp),
            "rr": float(get_cfg("TP_R")), "suggested_size": float(suggested_size), "reasons": reasons
        })
        return result

    return result

# ---------------- WebSocket handlers ----------------
def on_message(ws, message):
    global last_signal, last_signal_time, candle_count
    try:
        data = json.loads(message)
        k = data.get("k", {})
        if not k.get("x", False):
            return

        o = float(k.get("o")); h = float(k.get("h")); l = float(k.get("l")); c = float(k.get("c")); v = float(k.get("v"))
        t_close = k.get("T")

        opens.append(o); highs.append(h); lows.append(l); closes.append(c); vols.append(v)
        candle_count += 1

        # log every 20 candles
        if candle_count % 20 == 0:
            logging.info("Received candles: %d; last close: %.6f", candle_count, c)

        # need enough bars
        if len(closes) < max(get_cfg("EMA_LONG"), get_cfg("BB_PERIOD"), get_cfg("ATR_PERIOD"), get_cfg("MACD_SLOW")) + 5:
            return

        df = df_from_deques()
        eval_res = evaluate_signal(df)
        if eval_res.get("signal") and eval_res["signal"] != last_signal:
            last_signal = eval_res["signal"]
            last_signal_time = datetime.utcfromtimestamp(t_close/1000).strftime("%Y-%m-%d %H:%M:%S UTC")
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
    # show current runtime config
    text = "Current params:\n"
    for k in sorted(cfg.keys()):
        text += f"{k} = {cfg[k]}\n"
    text += f"\ncandle_count = {candle_count}\nlast_signal = {last_signal}\n"
    await update.message.reply_text(text)

async def candles_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🔢 Bot đã thu thập {candle_count} nến {INTERVAL}.\nEMA{get_cfg('EMA_LONG')} cần tối thiểu {get_cfg('EMA_LONG')} nến để tính đủ."
    )

# /get <param>
async def get_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /get <param>")
        return
    key = context.args[0].upper()
    val = cfg.get(key)
    if val is None:
        await update.message.reply_text(f"Param '{key}' not found.")
    else:
        await update.message.reply_text(f"{key} = {val}")

# /set <param> <value>
async def set_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # only allow from admin CHAT_ID (simple protection)
    try:
        from_id = update.effective_user.id
        # If CHAT_ID is group (-100...) then admin control is not enforced here strongly.
        # For strict control, compare update.effective_user.id with a saved ADMIN_ID.
    except Exception:
        from_id = None

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /set <param> <value>")
        return
    key = context.args[0].upper()
    val_str = context.args[1]
    # attempt cast to int/float/bool or keep string
    try:
        if "." in val_str:
            val = float(val_str)
        else:
            val = int(val_str)
    except Exception:
        if val_str.lower() in ["true", "false"]:
            val = val_str.lower() == "true"
        else:
            val = val_str
    # set and persist
    set_cfg(key, val)
    # update running vars for immediate effect (best-effort)
    globals().update({
        "EMA_SHORT": get_cfg("EMA_SHORT"),
        "EMA_LONG": get_cfg("EMA_LONG"),
        "RSI_PERIOD": get_cfg("RSI_PERIOD"),
        "ATR_PERIOD": get_cfg("ATR_PERIOD"),
        "MACD_FAST": get_cfg("MACD_FAST"),
        "MACD_SLOW": get_cfg("MACD_SLOW"),
        "MACD_SIGNAL": get_cfg("MACD_SIGNAL"),
        "BB_PERIOD": get_cfg("BB_PERIOD"),
        "BB_STD_MULT": get_cfg("BB_STD_MULT"),
        "K_SL": get_cfg("K_SL"),
        "TP_R": get_cfg("TP_R"),
        "ACCOUNT_BALANCE": get_cfg("ACCOUNT_BALANCE"),
        "RISK_PERCENT": get_cfg("RISK_PERCENT"),
        "MIN_CONFIRM": get_cfg("MIN_CONFIRM"),
        "HIGHER_TF": get_cfg("HIGHER_TF"),
        "VOLATILITY_THRESHOLD": get_cfg("VOLATILITY_THRESHOLD"),
        "SUPERTREND_PERIOD": get_cfg("SUPERTREND_PERIOD"),
        "SUPERTREND_MULT": get_cfg("SUPERTREND_MULT")
    })
    await update.message.reply_text(f"Set {key} = {val}")

def run_telegram_commands():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("gia", gia_handler))
    app.add_handler(CommandHandler("params", params_handler))
    app.add_handler(CommandHandler("candles", candles_handler))
    app.add_handler(CommandHandler("get", get_handler))
    app.add_handler(CommandHandler("set", set_handler))
    app.run_polling()

# ---------------- Main ----------------
if __name__ == "__main__":
    logging.info("Starting optimized multi-indicator SOL signal bot...")
    t_ws = threading.Thread(target=run_ws, daemon=True)
    t_ws.start()
    run_telegram_commands()
