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
  # <--- đọc biến môi trường từ file .env

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
# Optional imports (allow backtest without these dependencies working)
try:
    from websocket import WebSocketApp  # type: ignore
except Exception:  # pragma: no cover
    WebSocketApp = None  # fallback
try:
    from telegram import Update  # type: ignore
    from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes  # type: ignore
    from telegram.error import Conflict  # type: ignore
except Exception:  # pragma: no cover
    Update = ApplicationBuilder = CommandHandler = ContextTypes = Conflict = None  # type: ignore
from dotenv import load_dotenv

load_dotenv()
# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------- ENV / Defaults ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
# Cho phép import module phục vụ backtest mà không cần BOT_TOKEN/CHAT_ID.
# Khi chạy bot realtime (__main__), nếu thiếu sẽ cảnh báo mạnh.
if not BOT_TOKEN or not CHAT_ID:
    logging.warning("Thiếu BOT_TOKEN hoặc CHAT_ID (chế độ headless). Các hàm gửi Telegram sẽ bị bỏ qua.")

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
    "SUPERTREND_MULT": float(os.getenv("SUPERTREND_MULT", "3")),
    # Backtest / risk extensions
    "FEE_RATE": float(os.getenv("FEE_RATE", "0.0004")),          # 0.04% mỗi chiều mặc định
    "SLIPPAGE_BPS": float(os.getenv("SLIPPAGE_BPS", "2")),       # basis points (2 = 0.02%) điều chỉnh giá khớp
    "MAX_CONCURRENT_TRADES": int(os.getenv("MAX_CONCURRENT_TRADES", "1")),
    # Aggressiveness: 1.0 = nguyên bản, >1 nới lỏng dần điều kiện để ra tín hiệu nhiều hơn
    # Gợi ý: 1.5 (hơi tăng), 2.0 (tăng rõ), 2.5–3 (rất nhiều tín hiệu nhưng nhiễu cao)
    "SIGNAL_AGGRESSIVENESS": float(os.getenv("SIGNAL_AGGRESSIVENESS", "1.0"))
    ,"SIGNAL_SCORE_THRESHOLD": float(os.getenv("SIGNAL_SCORE_THRESHOLD", "3.0"))  # điểm tối thiểu để chấp nhận tín hiệu
    ,"MIN_EMA_SEPARATION_PCT": float(os.getenv("MIN_EMA_SEPARATION_PCT", "0.05"))  # % cách biệt tối thiểu (0.05%=0.0005) giữa EMA_SHORT và EMA_LONG theo giá để tránh cross sát
    ,"ENABLE_BB_FILTER": os.getenv("ENABLE_BB_FILTER", "true").lower() == "true"  # yêu cầu hướng breakout Bollinger đồng pha
    ,"RSI_MIDLINE_FILTER": os.getenv("RSI_MIDLINE_FILTER", "true").lower() == "true"  # BUY ưu tiên RSI>50, SELL RSI<50 (trừ khi aggressiveness cao bỏ qua)
    ,"COOLDOWN_BARS": int(os.getenv("COOLDOWN_BARS", "5"))  # số nến cooldown sau 1 tín hiệu cùng hướng
    # Adaptive risk params (ATR% regimes)
    ,"ATR_PCT_LOOKBACK": int(os.getenv("ATR_PCT_LOOKBACK", "120"))  # số nến dùng tính median atr_pct
    ,"ATR_PCT_LOW": float(os.getenv("ATR_PCT_LOW", "0.10"))       # dưới mức này coi là low vol (10%)
    ,"ATR_PCT_HIGH": float(os.getenv("ATR_PCT_HIGH", "0.25"))      # trên mức này coi là high vol (25%)
    ,"K_SL_LOW": float(os.getenv("K_SL_LOW", "1.0"))
    ,"K_SL_BASE": float(os.getenv("K_SL_BASE", "1.5"))
    ,"K_SL_HIGH": float(os.getenv("K_SL_HIGH", "1.8"))
    ,"TP_R_LOW": float(os.getenv("TP_R_LOW", "1.2"))
    ,"TP_R_BASE": float(os.getenv("TP_R_BASE", "1.5"))
    ,"TP_R_HIGH": float(os.getenv("TP_R_HIGH", "1.0"))  # high vol: giảm RR để chốt nhanh
    ,"VOL_SKIP_MULT": float(os.getenv("VOL_SKIP_MULT", "1.8"))  # nếu atr_pct > ATR_PCT_HIGH * VOL_SKIP_MULT -> bỏ qua hoàn toàn
    ,"RSI_BUY_CAP": float(os.getenv("RSI_BUY_CAP", "70"))
    ,"RSI_SELL_FLOOR": float(os.getenv("RSI_SELL_FLOOR", "30"))
    ,"PRICE_ABOVE_LONG_PCT": float(os.getenv("PRICE_ABOVE_LONG_PCT", "0.15"))  # percent
    ,"PRICE_BELOW_LONG_PCT": float(os.getenv("PRICE_BELOW_LONG_PCT", "0.15"))  # percent
    ,"TRADE_SIDE": os.getenv("TRADE_SIDE", "BOTH").upper()  # BOTH | BUY | SELL
    ,"ENABLE_BREAKEVEN": os.getenv("ENABLE_BREAKEVEN", "false").lower() == "true"
    ,"BREAKEVEN_AT_R": float(os.getenv("BREAKEVEN_AT_R", "0.5"))
    ,"ENABLE_TRAIL": os.getenv("ENABLE_TRAIL", "false").lower() == "true"
    ,"TRAIL_AT_R": float(os.getenv("TRAIL_AT_R", "1.0"))
    ,"TRAIL_DIST_R": float(os.getenv("TRAIL_DIST_R", "1.0"))
    ,"STRATEGY_MODE": os.getenv("STRATEGY_MODE", "TREND").upper()  # TREND | MR (mean reversion)
    ,"MR_DEV_PCT": float(os.getenv("MR_DEV_PCT", "0.5"))  # min % distance from EMA_LONG
    ,"MR_RSI_BUY_MAX": float(os.getenv("MR_RSI_BUY_MAX", "35"))
    ,"MR_RSI_SELL_MIN": float(os.getenv("MR_RSI_SELL_MIN", "65"))
    ,"MR_TP_TO_EMA": os.getenv("MR_TP_TO_EMA", "true").lower() == "true"
    ,"MR_USE_BB": os.getenv("MR_USE_BB", "true").lower() == "true"
    ,"MR_BB_PERIOD": int(os.getenv("MR_BB_PERIOD", "20"))
    ,"MR_BB_STD": float(os.getenv("MR_BB_STD", "2.0"))
    ,"MR_CONFIRM_CROSSBACK": os.getenv("MR_CONFIRM_CROSSBACK", "true").lower() == "true"
    # Breakout mode (Donchian-style)
    ,"DONCHIAN_PERIOD": int(os.getenv("DONCHIAN_PERIOD", "40"))
    ,"BREAKOUT_EMA_FILTER": os.getenv("BREAKOUT_EMA_FILTER", "true").lower() == "true"  # require price relative to EMA_LONG
    # Session/volatility guards
    ,"ACTIVE_START_HOUR": int(os.getenv("ACTIVE_START_HOUR", "0"))   # UTC hour [0,24)
    ,"ACTIVE_END_HOUR": int(os.getenv("ACTIVE_END_HOUR", "24"))      # UTC hour [0,24)
    ,"MIN_VOLATILITY_THRESHOLD": float(os.getenv("MIN_VOLATILITY_THRESHOLD", "0.0"))  # min ATR/price to avoid dead market
    # ADX filters: prefer MR when trend is weak; prefer TREND when trend is strong
    ,"ENABLE_ADX_FILTER": os.getenv("ENABLE_ADX_FILTER", "true").lower() == "true"
    ,"ADX_PERIOD": int(os.getenv("ADX_PERIOD", "14"))
    ,"TREND_MIN_ADX": float(os.getenv("TREND_MIN_ADX", "18"))
    ,"MR_MAX_ADX": float(os.getenv("MR_MAX_ADX", "22"))
    # Partial take-profit
    ,"ENABLE_PARTIAL_TP": os.getenv("ENABLE_PARTIAL_TP", "false").lower() == "true"
    ,"PARTIAL_AT_R": float(os.getenv("PARTIAL_AT_R", "1.0"))
    ,"PARTIAL_SIZE_FRACTION": float(os.getenv("PARTIAL_SIZE_FRACTION", "0.5"))  # 0..1
    ,"PARTIAL_MOVE_BE": os.getenv("PARTIAL_MOVE_BE", "true").lower() == "true"
    # Hybrid auto regime switch (TREND vs MR)
    ,"ENABLE_HYBRID": os.getenv("ENABLE_HYBRID", "false").lower() == "true"
    ,"HYBRID_USE_ADX": os.getenv("HYBRID_USE_ADX", "true").lower() == "true"
    ,"HYBRID_USE_SLOPE": os.getenv("HYBRID_USE_SLOPE", "true").lower() == "true"
    ,"ADX_TREND_MIN": float(os.getenv("ADX_TREND_MIN", "20"))
    ,"ADX_RANGE_MAX": float(os.getenv("ADX_RANGE_MAX", "18"))
    ,"EMA_SLOPE_MIN_PCT": float(os.getenv("EMA_SLOPE_MIN_PCT", "0.05"))  # % change of long EMA over lookback to call a trend
    ,"SLOPE_LOOKBACK": int(os.getenv("SLOPE_LOOKBACK", "30"))
    # Absolute SL/TP override (instrument with low % moves like PAXG)
    ,"USE_ABS_SLTP": os.getenv("USE_ABS_SLTP", "false").lower() == "true"
    ,"ABS_SL_USD": float(os.getenv("ABS_SL_USD", "1.0"))  # absolute dollar distance to SL
    ,"ABS_TP_USD": float(os.getenv("ABS_TP_USD", "1.5"))  # absolute dollar distance to TP
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
FEE_RATE = get_cfg("FEE_RATE")
SLIPPAGE_BPS = get_cfg("SLIPPAGE_BPS")
MAX_CONCURRENT_TRADES = get_cfg("MAX_CONCURRENT_TRADES")
SIGNAL_AGGRESSIVENESS = get_cfg("SIGNAL_AGGRESSIVENESS")
SIGNAL_SCORE_THRESHOLD = get_cfg("SIGNAL_SCORE_THRESHOLD")
MIN_EMA_SEPARATION_PCT = get_cfg("MIN_EMA_SEPARATION_PCT")
ENABLE_BB_FILTER = get_cfg("ENABLE_BB_FILTER")
RSI_MIDLINE_FILTER = get_cfg("RSI_MIDLINE_FILTER")
COOLDOWN_BARS = get_cfg("COOLDOWN_BARS")
ATR_PCT_LOOKBACK = get_cfg("ATR_PCT_LOOKBACK")
ATR_PCT_LOW = get_cfg("ATR_PCT_LOW")
ATR_PCT_HIGH = get_cfg("ATR_PCT_HIGH")
K_SL_LOW = get_cfg("K_SL_LOW")
K_SL_BASE = get_cfg("K_SL_BASE")
K_SL_HIGH = get_cfg("K_SL_HIGH")
TP_R_LOW = get_cfg("TP_R_LOW")
TP_R_BASE = get_cfg("TP_R_BASE")
TP_R_HIGH = get_cfg("TP_R_HIGH")
VOL_SKIP_MULT = get_cfg("VOL_SKIP_MULT")
RSI_BUY_CAP = get_cfg("RSI_BUY_CAP") if get_cfg("RSI_BUY_CAP") is not None else 70
RSI_SELL_FLOOR = get_cfg("RSI_SELL_FLOOR") if get_cfg("RSI_SELL_FLOOR") is not None else 30
PRICE_ABOVE_LONG_PCT = get_cfg("PRICE_ABOVE_LONG_PCT") if get_cfg("PRICE_ABOVE_LONG_PCT") is not None else 0.15
PRICE_BELOW_LONG_PCT = get_cfg("PRICE_BELOW_LONG_PCT") if get_cfg("PRICE_BELOW_LONG_PCT") is not None else 0.15

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
    if not BOT_TOKEN or not CHAT_ID:
        # silent skip in backtest / import context
        return
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

def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute Average Directional Index (ADX). Returns a Series aligned with df index."""
    high = df['high']
    low = df['low']
    close = df['close']
    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    # Directional Movements
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # Smoothed averages (Wilder's smoothing via EMA alpha=1/period)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan))

    dx = ( (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) ) * 100
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    return adx

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
        float_cols = ['open','high','low','close','volume']
        df[float_cols] = df[float_cols].astype(float)
        return df[['open','high','low','close','volume']]
    except Exception as e:
        logging.exception("fetch_klines_rest error: %s", e)
        return None

# ---------------- Evaluate signal (with multi-timeframe & volatility) ----------------
_htf_cache = {
    "interval": None,
    "data": None,
    "last_fetch": None
}

def _get_higher_timeframe_df(symbol: str, interval: str):
    """Return cached higher timeframe klines; refresh every 30 seconds or when interval changes."""
    if not interval or interval == "0":
        return None
    import time as _t
    now = _t.time()
    refresh_due = False
    if _htf_cache["interval"] != interval:
        refresh_due = True
    elif _htf_cache["last_fetch"] is None or (now - _htf_cache["last_fetch"]) > 30:
        refresh_due = True
    if refresh_due:
        df = fetch_klines_rest(symbol, interval, limit=120)
        if df is not None and len(df) > 0:
            _htf_cache.update({"interval": interval, "data": df, "last_fetch": now})
    return _htf_cache["data"]

def evaluate_signal(df: pd.DataFrame, idx: int | None = None):
    """Simplified trend-follow evaluate: only EMA cross + MACD alignment + HTF trend + RSI guard + volatility cap.
    Removed scoring, Bollinger, exploratory, adaptive ATR regimes (kept fixed K_SL/TP_R from config)."""
    result = {"signal": None, "reasons": []}
    close = df['close']
    high = df['high']
    low = df['low']
    # Current index (use last row if not provided)
    j = (len(df) - 1) if idx is None else idx
    if j < 1:
        return result
    price_v = close.iat[j]
    # optional session filter by close_time (UTC hour)
    if "close_time" in df.columns:
        try:
            ts = int(df["close_time"].iat[j])
            hr = datetime.utcfromtimestamp(ts/1000).hour
            h_start = int(get_cfg("ACTIVE_START_HOUR") or 0) % 24
            h_end = int(get_cfg("ACTIVE_END_HOUR") or 24) % 24
            if h_start != h_end:  # if equal, treat as always-on
                if h_start < h_end:
                    if not (h_start <= hr < h_end):
                        return result
                else:  # wraps over midnight
                    if not (hr >= h_start or hr < h_end):
                        return result
        except Exception:
            pass

    # Use precomputed indicators if available to speed up backtests
    ema_short = df["ema_s"] if "ema_s" in df.columns else compute_ema(close, get_cfg("EMA_SHORT"))
    ema_long = df["ema_l"] if "ema_l" in df.columns else compute_ema(close, get_cfg("EMA_LONG"))
    if j >= len(ema_long) or j < 1:
        return result
    prev_short = ema_short.iat[j-1]; prev_long = ema_long.iat[j-1]
    short_v = ema_short.iat[j]; long_v = ema_long.iat[j]

    if {"macd_line","macd_signal","macd_hist"}.issubset(df.columns):
        macd_line_v = df["macd_line"].iat[j]; macd_signal_v = df["macd_signal"].iat[j]; macd_hist_v = df["macd_hist"].iat[j]
        macd_prev_hist = df["macd_hist"].iat[j-1] if j > 0 else 0
    else:
        _ml, _ms, _mh = compute_macd(close, get_cfg("MACD_FAST"), get_cfg("MACD_SLOW"), get_cfg("MACD_SIGNAL"))
        if j >= len(_mh):
            return result
        macd_line_v = _ml.iat[j]; macd_signal_v = _ms.iat[j]; macd_hist_v = _mh.iat[j]
        macd_prev_hist = _mh.iat[j-1] if j > 0 else 0
    rsi_v = (df["rsi"].iat[j] if "rsi" in df.columns else compute_rsi(close, get_cfg("RSI_PERIOD")).iat[j])
    atr_v = (df["atr"].iat[j] if "atr" in df.columns else compute_atr(df, get_cfg("ATR_PERIOD")).iat[j])
    # ADX (optional)
    adx_v = None
    try:
        if "adx" in df.columns:
            adx_v = float(df["adx"].iat[j])
        else:
            adx_series = compute_adx(df, int(get_cfg("ADX_PERIOD") or 14))
            adx_v = float(adx_series.iat[j])
    except Exception:
        adx_v = None

    # volatility bounds
    vol_ratio = (atr_v / price_v) if price_v and atr_v else 0
    if vol_ratio > get_cfg("VOLATILITY_THRESHOLD"):
        return result
    min_vol = float(get_cfg("MIN_VOLATILITY_THRESHOLD") or 0.0)
    if min_vol > 0 and vol_ratio < min_vol:
        return result

    # Trade-side filter
    trade_side = str(get_cfg("TRADE_SIDE") or "BOTH").upper()
    allow_buy = trade_side in ("BOTH","BUY")
    allow_sell = trade_side in ("BOTH","SELL")

    mode = str(get_cfg("STRATEGY_MODE") or "TREND").upper()
    # Hybrid regime override
    if bool(get_cfg("ENABLE_HYBRID") or False):
        # compute EMA slope (% change over lookback)
        slope_mode = None
        use_slope = bool(get_cfg("HYBRID_USE_SLOPE") or False)
        use_adx = bool(get_cfg("HYBRID_USE_ADX") or False)
        trend_by_slope = False
        if use_slope:
            lookback = int(get_cfg("SLOPE_LOOKBACK") or 30)
            if j >= lookback and lookback > 0:
                past = ema_long.iat[j - lookback]
                if past > 0:
                    slope_pct = (long_v - past) / past * 100.0
                    if abs(slope_pct) >= float(get_cfg("EMA_SLOPE_MIN_PCT") or 0.05):
                        trend_by_slope = True
        trend_by_adx = False
        range_by_adx = False
        if use_adx and adx_v is not None:
            if adx_v >= float(get_cfg("ADX_TREND_MIN") or 20):
                trend_by_adx = True
            if adx_v <= float(get_cfg("ADX_RANGE_MAX") or 18):
                range_by_adx = True
        # Decide regime
        if trend_by_adx or trend_by_slope:
            mode = "TREND"
        elif range_by_adx and not trend_by_slope:
            mode = "MR"
    if mode == "MR":
        # ADX guard: skip MR when trend is too strong
        if bool(get_cfg("ENABLE_ADX_FILTER") or False) and adx_v is not None:
            if adx_v > float(get_cfg("MR_MAX_ADX") or 22):
                return result
        # Mean-reversion with optional Bollinger crossback confirmation
        dev_pct = (abs(price_v - long_v)/price_v*100) if price_v else 0
        mr_dev_req = float(get_cfg("MR_DEV_PCT") or 0.5)
        mr_tp_to_ema = bool(get_cfg("MR_TP_TO_EMA") or False)
        use_bb = bool(get_cfg("MR_USE_BB") or False)
        bb_period = int(get_cfg("MR_BB_PERIOD") or get_cfg("BB_PERIOD") or 20)
        bb_std = float(get_cfg("MR_BB_STD") or get_cfg("BB_STD_MULT") or 2.0)
        confirm_crossback = bool(get_cfg("MR_CONFIRM_CROSSBACK") or False)
        lower_prev = lower_curr = None
        upper_prev = upper_curr = None
        if use_bb and j >= bb_period + 2:
            up, mid, lowb = compute_bollinger(close, period=bb_period, std_mult=bb_std)
            lower_prev = lowb.iat[j-1]
            lower_curr = lowb.iat[j]
            upper_prev = up.iat[j-1]
            upper_curr = up.iat[j]
        # Conditions
        rsi_buy_ok = rsi_v <= float(get_cfg("MR_RSI_BUY_MAX") or 35)
        rsi_sell_ok = rsi_v >= float(get_cfg("MR_RSI_SELL_MIN") or 65)
        dev_ok = dev_pct >= mr_dev_req
        bb_buy_ok = False
        bb_sell_ok = False
        if use_bb and lower_prev is not None and lower_curr is not None and upper_prev is not None and upper_curr is not None:
            if confirm_crossback:
                bb_buy_ok = (close.iat[j-1] < lower_prev) and (price_v > lower_curr)
                bb_sell_ok = (close.iat[j-1] > upper_prev) and (price_v < upper_curr)
            else:
                bb_buy_ok = price_v < lower_curr
                bb_sell_ok = price_v > upper_curr
        # BUY: prefer BB crossback; else fallback to EMA deviation + RSI extreme
        if allow_buy and price_v < long_v and ( (use_bb and bb_buy_ok) or (dev_ok and rsi_buy_ok) ):
            use_abs = bool(get_cfg("USE_ABS_SLTP") or False)
            if use_abs:
                sl_dist = float(get_cfg("ABS_SL_USD") or 0)
            else:
                sl_dist = get_cfg("K_SL") * atr_v if atr_v > 0 else 0
            if sl_dist <= 0:
                return result
            entry = float(price_v)
            sl = entry - sl_dist
            if mr_tp_to_ema and long_v > entry and not use_abs:
                tp = float(long_v)
                rr = (tp - entry)/sl_dist
            else:
                if use_abs:
                    tp = entry + float(get_cfg("ABS_TP_USD") or 0)
                    rr = (tp - entry)/sl_dist if sl_dist>0 else 0
                else:
                    tp = entry + get_cfg("TP_R") * sl_dist
                    rr = float(get_cfg("TP_R"))
            risk_amount = (get_cfg("RISK_PERCENT")/100.0) * get_cfg("ACCOUNT_BALANCE")
            size = (risk_amount / sl_dist) if sl_dist>0 else 0
            reason = "mr_bb_buy" if use_bb and bb_buy_ok else "mr_dev_rsi_buy"
            result.update({"signal":"BUY","entry":entry,"sl":float(sl),"tp":float(tp),"rr":float(rr),"suggested_size":float(size),"reasons":[reason]})
            return result
        # SELL symmetric
        if allow_sell and price_v > long_v and ( (use_bb and bb_sell_ok) or (dev_ok and rsi_sell_ok) ):
            use_abs = bool(get_cfg("USE_ABS_SLTP") or False)
            if use_abs:
                sl_dist = float(get_cfg("ABS_SL_USD") or 0)
            else:
                sl_dist = get_cfg("K_SL") * atr_v if atr_v > 0 else 0
            if sl_dist <= 0:
                return result
            entry = float(price_v)
            sl = entry + sl_dist
            if mr_tp_to_ema and long_v < entry and not use_abs:
                tp = float(long_v)
                rr = (entry - tp)/sl_dist
            else:
                if use_abs:
                    tp = entry - float(get_cfg("ABS_TP_USD") or 0)
                    rr = (entry - tp)/sl_dist if sl_dist>0 else 0
                else:
                    tp = entry - get_cfg("TP_R") * sl_dist
                    rr = float(get_cfg("TP_R"))
            risk_amount = (get_cfg("RISK_PERCENT")/100.0) * get_cfg("ACCOUNT_BALANCE")
            size = (risk_amount / sl_dist) if sl_dist>0 else 0
            reason = "mr_bb_sell" if use_bb and bb_sell_ok else "mr_dev_rsi_sell"
            result.update({"signal":"SELL","entry":entry,"sl":float(sl),"tp":float(tp),"rr":float(rr),"suggested_size":float(size),"reasons":[reason]})
            return result
    elif mode == "BREAKOUT":
        # Donchian breakout: break above/below N-bar extremes, optional EMA_LONG filter
        n = int(get_cfg("DONCHIAN_PERIOD") or 40)
        if j < n:
            return result
        hh = high.iloc[j-n+1:j+1].max()
        ll = low.iloc[j-n+1:j+1].min()
        use_ema_filter = bool(get_cfg("BREAKOUT_EMA_FILTER") or False)
        go_long = allow_buy and price_v > hh and (not use_ema_filter or price_v > long_v)
        go_short = allow_sell and price_v < ll and (not use_ema_filter or price_v < long_v)
        if go_long:
            use_abs = bool(get_cfg("USE_ABS_SLTP") or False)
            if use_abs:
                sl_dist = float(get_cfg("ABS_SL_USD") or 0)
            else:
                sl_dist = get_cfg("K_SL") * atr_v if atr_v > 0 else 0
            if sl_dist <= 0:
                return result
            entry = float(price_v)
            sl = entry - sl_dist
            if use_abs:
                tp = entry + float(get_cfg("ABS_TP_USD") or 0)
                rr = (tp - entry)/sl_dist if sl_dist>0 else 0
            else:
                tp = entry + get_cfg("TP_R") * sl_dist
                rr = float(get_cfg("TP_R"))
            risk_amount = (get_cfg("RISK_PERCENT")/100.0) * get_cfg("ACCOUNT_BALANCE")
            size = (risk_amount / sl_dist) if sl_dist>0 else 0
            result.update({"signal":"BUY","entry":entry,"sl":float(sl),"tp":float(tp),"rr":float(rr),"suggested_size":float(size),"reasons":["breakout_up"]})
            return result
        if go_short:
            use_abs = bool(get_cfg("USE_ABS_SLTP") or False)
            if use_abs:
                sl_dist = float(get_cfg("ABS_SL_USD") or 0)
            else:
                sl_dist = get_cfg("K_SL") * atr_v if atr_v > 0 else 0
            if sl_dist <= 0:
                return result
            entry = float(price_v)
            sl = entry + sl_dist
            if use_abs:
                tp = entry - float(get_cfg("ABS_TP_USD") or 0)
                rr = (entry - tp)/sl_dist if sl_dist>0 else 0
            else:
                tp = entry - get_cfg("TP_R") * sl_dist
                rr = float(get_cfg("TP_R"))
            risk_amount = (get_cfg("RISK_PERCENT")/100.0) * get_cfg("ACCOUNT_BALANCE")
            size = (risk_amount / sl_dist) if sl_dist>0 else 0
            result.update({"signal":"SELL","entry":entry,"sl":float(sl),"tp":float(tp),"rr":float(rr),"suggested_size":float(size),"reasons":["breakout_down"]})
            return result
    else:
        # TREND mode: EMA cross + MACD alignment + optional HTF trend + RSI guard + price distance
        # ADX guard: require trend strength
        if bool(get_cfg("ENABLE_ADX_FILTER") or False) and adx_v is not None:
            if adx_v < float(get_cfg("TREND_MIN_ADX") or 18):
                return result
        # higher timeframe trend (neutral if disabled)
        higher_ok_long = higher_ok_short = True
        htf_interval = str(get_cfg("HIGHER_TF")) if get_cfg("HIGHER_TF") else ""
        if htf_interval and htf_interval != "0":
            htf = _get_higher_timeframe_df(SYMBOL, htf_interval)
            if htf is not None and len(htf) > 20:
                ema_htf_short = compute_ema(htf['close'], get_cfg("EMA_SHORT")).iloc[-1]
                ema_htf_long = compute_ema(htf['close'], get_cfg("EMA_LONG")).iloc[-1]
                higher_ok_long = ema_htf_short > ema_htf_long
                higher_ok_short = ema_htf_short < ema_htf_long
        # cross detection
        cross_up = prev_short <= prev_long and short_v > long_v
        cross_down = prev_short >= prev_long and short_v < long_v

        macd_long = macd_line_v > macd_signal_v and macd_hist_v > macd_prev_hist
        macd_short = macd_line_v < macd_signal_v and macd_hist_v < macd_prev_hist

        # RSI guards
        if allow_buy and cross_up and macd_long and higher_ok_long and rsi_v < RSI_BUY_CAP:
            # price distance filter
            dist_long_pct = (price_v - long_v)/price_v*100 if price_v else 0  # percent
            if dist_long_pct < PRICE_ABOVE_LONG_PCT:
                return result
            use_abs = bool(get_cfg("USE_ABS_SLTP") or False)
            if use_abs:
                sl_dist = float(get_cfg("ABS_SL_USD") or 0)
            else:
                sl_dist = get_cfg("K_SL") * atr_v if atr_v > 0 else 0
            if sl_dist <= 0:
                return result
            entry = float(price_v)
            sl = entry - sl_dist
            if use_abs:
                tp = entry + float(get_cfg("ABS_TP_USD") or 0)
                rr = (tp - entry)/sl_dist if sl_dist>0 else 0
            else:
                tp = entry + get_cfg("TP_R") * sl_dist
                rr = float(get_cfg("TP_R"))
            risk_amount = (get_cfg("RISK_PERCENT")/100.0) * get_cfg("ACCOUNT_BALANCE")
            size = (risk_amount / sl_dist) if sl_dist>0 else 0
            result.update({"signal":"BUY","entry":float(entry),"sl":float(sl),"tp":float(tp),"rr":float(rr),"suggested_size":float(size),"reasons":["cross_up","macd_long","htf_long"]})
            return result
        if allow_sell and cross_down and macd_short and higher_ok_short and rsi_v > RSI_SELL_FLOOR:
            dist_long_pct = (long_v - price_v)/price_v*100 if price_v else 0  # percent
            if dist_long_pct < PRICE_BELOW_LONG_PCT:
                return result
            use_abs = bool(get_cfg("USE_ABS_SLTP") or False)
            if use_abs:
                sl_dist = float(get_cfg("ABS_SL_USD") or 0)
            else:
                sl_dist = get_cfg("K_SL") * atr_v if atr_v > 0 else 0
            if sl_dist <= 0:
                return result
            entry = float(price_v)
            sl = entry + sl_dist
            if use_abs:
                tp = entry - float(get_cfg("ABS_TP_USD") or 0)
                rr = (entry - tp)/sl_dist if sl_dist>0 else 0
            else:
                tp = entry - get_cfg("TP_R") * sl_dist
                rr = float(get_cfg("TP_R"))
            risk_amount = (get_cfg("RISK_PERCENT")/100.0) * get_cfg("ACCOUNT_BALANCE")
            size = (risk_amount / sl_dist) if sl_dist>0 else 0
            result.update({"signal":"SELL","entry":float(entry),"sl":float(sl),"tp":float(tp),"rr":float(rr),"suggested_size":float(size),"reasons":["cross_down","macd_short","htf_short"]})
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
async def gia_handler(update, context):
    price = get_current_price_rest()
    if price:
        await update.message.reply_text(f"💰 Giá {SYMBOL.upper()} hiện tại: {price:.6f} USDT")
    else:
        await update.message.reply_text("⚠️ Không lấy được giá hiện tại.")

async def params_handler(update, context):
    # show current runtime config
    text = "Current params:\n"
    for k in sorted(cfg.keys()):
        text += f"{k} = {cfg[k]}\n"
    text += f"\ncandle_count = {candle_count}\nlast_signal = {last_signal}\n"
    await update.message.reply_text(text)

async def candles_handler(update, context):
    await update.message.reply_text(
        f"🔢 Bot đã thu thập {candle_count} nến {INTERVAL}.\nEMA{get_cfg('EMA_LONG')} cần tối thiểu {get_cfg('EMA_LONG')} nến để tính đủ."
    )

# /get <param>
async def get_handler(update, context):
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
async def set_handler(update, context):
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
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("gia", gia_handler))
    app.add_handler(CommandHandler("params", params_handler))
    app.add_handler(CommandHandler("candles", candles_handler))
    app.add_handler(CommandHandler("get", get_handler))
    app.add_handler(CommandHandler("set", set_handler))
    try:
        app.run_polling(drop_pending_updates=True)
    except Conflict as e:
        logging.error("Another instance of this bot is running (Conflict). Exiting gracefully: %s", e)
        send_telegram("⚠️ Another instance of the bot is already polling. This instance will stop.")

# ---------------- Main ----------------
if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Không thể chạy bot realtime vì thiếu BOT_TOKEN/CHAT_ID. Chỉ có thể backtest.")
    logging.info("Starting optimized multi-indicator SOL signal bot...")
    t_ws = threading.Thread(target=run_ws, daemon=True)
    t_ws.start()
    run_telegram_commands()
