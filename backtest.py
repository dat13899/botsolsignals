"""Simple backtest runner for evaluate_signal() logic.

Usage:
  python backtest.py --symbol SOLUSDT --interval 1m --limit 1000

It will:
  1. Fetch historical klines from Binance REST.
  2. Walk forward candle by candle, calling evaluate_signal() once enough data.
  3. Simulate trades using returned entry/SL/TP (1 position at a time).
  4. Produce stats: #BUY, #SELL, wins, losses, win rate, PnL (R and % balance).

Assumptions:
  - No fees / slippage.
  - TP/SL are limit hits within following candles (checks intra-candle high/low reach).
  - Only one open trade at a time; new signal ignored until trade closed.
"""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass
from typing import Optional, List

import pandas as pd
import requests

from bot_sol_signals import (
	evaluate_signal, get_cfg, SYMBOL as DEFAULT_SYMBOL
)

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"


def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
	"""Fetch klines with progress bar. Handles >1000 by batching (Binance limit 1000 per call).

	Progress hiển thị dạng: Fetched X/Y (Z%) ...
	"""
	remaining = limit
	all_frames: List[pd.DataFrame] = []
	batch_size = 1000
	last_end_time = None  # use endTime pagination
	col_names = [
		"open_time","open","high","low","close","volume","close_time",
		"qav","num_trades","taker_base_vol","taker_quote_vol","ignore"
	]
	fetched = 0
	while remaining > 0:
		this_limit = batch_size if remaining > batch_size else remaining
		params = {"symbol": symbol.upper(), "interval": interval, "limit": this_limit}
		if last_end_time is not None:
			params["endTime"] = last_end_time - 1  # paginate backwards slightly overlap avoidance
		r = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
		r.raise_for_status()
		data = r.json()
		if not data:
			break
		df_part = pd.DataFrame(data, columns=col_names)
		float_cols = ["open","high","low","close","volume"]
		df_part[float_cols] = df_part[float_cols].astype(float)
		all_frames.append(df_part)
		fetched += len(df_part)
		remaining = limit - fetched
		last_end_time = int(df_part.iloc[0].open_time)  # because we will go backwards
		pct = min(100.0, fetched/limit*100)
		print(f"Fetched {fetched}/{limit} ({pct:.1f}%)", end="\r", flush=True)
		if len(df_part) < this_limit:
			# no more historical candles available
			break
	# merge & sort ascending by open_time
	if not all_frames:
		return pd.DataFrame(columns=["open","high","low","close","volume","open_time","close_time"])
	merged = pd.concat(all_frames, ignore_index=True)
	merged = merged.sort_values("open_time")
	print(" " * 60, end="\r")  # clear line
	print(f"Fetched {min(fetched, limit)} candles.")
	return merged[["open","high","low","close","volume","open_time","close_time"]].tail(limit)


@dataclass
class Trade:
	side: str  # BUY or SELL
	entry: float
	sl: float
	tp: float
	rr: float
	open_index: int  # candle index where opened
	close_index: Optional[int] = None
	exit_price: Optional[float] = None
	result: Optional[str] = None  # WIN / LOSS / UNRESOLVED
	entry_fee: float = 0.0
	exit_fee: float = 0.0
	pnl_r: float = 0.0  # realized R including fees
	breakeven_applied: bool = False
	initial_risk_dist: float = 0.0
	mfe_r: float = 0.0
	# partial TP tracking
	partial_filled: bool = False
	realized_r_from_partial: float = 0.0

	def is_open(self) -> bool:
		return self.close_index is None


class Backtester:
	def __init__(self, df: pd.DataFrame, show_progress: bool = True):
		self.df_full = df.reset_index(drop=True)
		self.trades: List[Trade] = []
		self.buy_signals = 0
		self.sell_signals = 0
		self.min_bars_needed = max(
			get_cfg("EMA_LONG"),
			get_cfg("BB_PERIOD"),
			get_cfg("ATR_PERIOD"),
			get_cfg("MACD_SLOW")
		) + 5
		self.fee_rate = get_cfg("FEE_RATE") or 0.0
		self.slip_bps = get_cfg("SLIPPAGE_BPS") or 0.0
		self.max_concurrent = int(get_cfg("MAX_CONCURRENT_TRADES") or 1)
		self.show_progress = show_progress
		# Precompute indicators once per dataset for speed (used by evaluate_signal if present)
		try:
			close = self.df_full["close"]
			# EMA short/long
			from bot_sol_signals import compute_ema as _ema, compute_macd as _macd, compute_rsi as _rsi, compute_atr as _atr, compute_adx as _adx
			self.df_full.loc[:, "ema_s"] = _ema(close, get_cfg("EMA_SHORT"))
			self.df_full.loc[:, "ema_l"] = _ema(close, get_cfg("EMA_LONG"))
			macd_line, macd_signal, macd_hist = _macd(close, get_cfg("MACD_FAST"), get_cfg("MACD_SLOW"), get_cfg("MACD_SIGNAL"))
			self.df_full.loc[:, "macd_line"] = macd_line
			self.df_full.loc[:, "macd_signal"] = macd_signal
			self.df_full.loc[:, "macd_hist"] = macd_hist
			self.df_full.loc[:, "rsi"] = _rsi(close, get_cfg("RSI_PERIOD"))
			self.df_full.loc[:, "atr"] = _atr(self.df_full, get_cfg("ATR_PERIOD"))
			self.df_full.loc[:, "adx"] = _adx(self.df_full, int(get_cfg("ADX_PERIOD") or 14))
		except Exception:
			# Precompute is best-effort; evaluate_signal will compute if missing
			pass

	def run(self):
		total = len(self.df_full)
		start_time = time.time()
		last_print = 0.0
		update_every = max(1, total // 300)  # aim ~0.33% resolution
		for i in range(total):
			if i + 1 < self.min_bars_needed:
				continue
			# Evaluate on the full precomputed frame; idx selects the current bar
			signal_res = evaluate_signal(self.df_full, idx=i)
			sig = signal_res.get("signal")
			if sig and self._open_trades_count() < self.max_concurrent:
				if sig == "BUY":
					self.buy_signals += 1
				elif sig == "SELL":
					self.sell_signals += 1
				self._open_trade(sig, signal_res, i)
			self._update_trades(i)
			if self.show_progress and (i % update_every == 0 or time.time() - last_print >= 0.5):
				last_print = time.time()
				processed = i + 1
				pct = processed / total * 100.0
				elapsed = last_print - start_time
				eta = (elapsed / processed) * (total - processed) if processed > 0 else 0
				wins = sum(1 for t in self.trades if t.result == "WIN")
				losses = sum(1 for t in self.trades if t.result == "LOSS")
				open_tr = self._open_trades_count()
				bar_len = 30
				filled = int(bar_len * pct / 100)
				bar = "#" * filled + "-" * (bar_len - filled)
				def fmt_sec(s: float) -> str:
					if s >= 3600:
						return f"{int(s//3600)}h{int((s%3600)//60)}m"
					if s >= 60:
						return f"{int(s//60)}m{int(s%60)}s"
					return f"{int(s)}s"
				eta_txt = fmt_sec(eta)
				print(f"Processing [{bar}] {pct:5.1f}%  {processed}/{total}  ETA {eta_txt}  Trades:{len(self.trades)} (O:{open_tr} W:{wins} L:{losses})", end="\r", flush=True)

		# ensure newline after progress bar
		if self.show_progress:
			print("\n", end="")
		# mark unresolved at end
		for t in self.trades:
			if t.is_open():
				t.close_index = len(self.df_full) - 1
				t.exit_price = self.df_full.iloc[-1].close
				t.result = "UNRESOLVED"
				t.pnl_r = 0.0

	def _open_trades_count(self) -> int:
		return sum(1 for t in self.trades if t.is_open())

	def _apply_slippage(self, price: float, side: str, is_entry: bool) -> float:
		# simple: entry worse, exit worse relative to favorable direction
		slip_frac = (self.slip_bps / 10000.0)
		if slip_frac <= 0:
			return price
		if side == "BUY":
			return price * (1 + slip_frac) if is_entry else price * (1 - slip_frac)
		else:  # SELL
			return price * (1 - slip_frac) if is_entry else price * (1 + slip_frac)

	def _open_trade(self, sig: str, signal_res: dict, idx: int):
		entry = self._apply_slippage(signal_res["entry"], sig, True)
		# Adjust SL/TP relative distances from original entry proportionally
		orig_entry = signal_res["entry"]
		adj_factor = entry / orig_entry if orig_entry else 1.0
		sl = signal_res["sl"] * adj_factor
		tp = signal_res["tp"] * adj_factor
		# compute initial risk distance from original targets (before breakeven/trailing)
		init_risk = (entry - sl) if sig == "BUY" else (sl - entry)
		trade = Trade(
			side=sig,
			entry=entry,
			sl=sl,
			tp=tp,
			rr=signal_res.get("rr", 0.0),
			open_index=idx,
			initial_risk_dist=init_risk if init_risk>0 else 0.0,
		)
		# entry fee (one side)
		trade.entry_fee = self.fee_rate * 1.0  # in R units approximate: fee expressed later when computing pnl
		self.trades.append(trade)

	def _update_trades(self, idx: int):
		if not any(t.is_open() for t in self.trades):
			return
		candle = self.df_full.iloc[idx]
		high = candle.high
		low = candle.low
		close = candle.close
		# Breakeven config
		enable_be = bool(get_cfg("ENABLE_BREAKEVEN") or False)
		be_at = float(get_cfg("BREAKEVEN_AT_R") or 0.0)
		# Trailing config
		enable_trail = bool(get_cfg("ENABLE_TRAIL") or False)
		trail_at = float(get_cfg("TRAIL_AT_R") or 0.0)
		trail_dist = float(get_cfg("TRAIL_DIST_R") or 0.0)
		# Partial TP config
		enable_pt = bool(get_cfg("ENABLE_PARTIAL_TP") or False)
		pt_at = float(get_cfg("PARTIAL_AT_R") or 0.0)
		pt_frac = max(0.0, min(1.0, float(get_cfg("PARTIAL_SIZE_FRACTION") or 0.0)))
		pt_move_be = bool(get_cfg("PARTIAL_MOVE_BE") or False)
		for t in self.trades:
			if not t.is_open():
				continue
			# Compute current R progress for partial/BE/trailing
			risk_dist0 = t.initial_risk_dist if t.initial_risk_dist>0 else (t.entry - t.sl if t.side=="BUY" else t.sl - t.entry)
			curr_r_buy = (candle.high - t.entry)/risk_dist0 if risk_dist0>0 else 0.0
			curr_r_sell = (t.entry - candle.low)/risk_dist0 if risk_dist0>0 else 0.0
			curr_r = curr_r_buy if t.side=="BUY" else curr_r_sell
			# Partial TP: realize pt_frac at pt_at R once, then optionally move SL to entry
			if enable_pt and not t.partial_filled and pt_at > 0 and curr_r >= pt_at:
				# realize partial profit in R units
				partial_r = pt_at * pt_frac
				t.realized_r_from_partial += partial_r
				t.partial_filled = True
				if pt_move_be:
					# move SL to entry
					t.sl = t.entry
			# Apply breakeven before evaluating TP/SL hits
			if enable_be and not t.breakeven_applied and be_at > 0:
				if t.side == "BUY":
					risk_dist = t.entry - t.sl
					if risk_dist > 0 and high >= (t.entry + be_at * risk_dist):
						# move SL to entry (breakeven)
						t.sl = t.entry
						t.breakeven_applied = True
				else:  # SELL
					risk_dist = t.sl - t.entry
					if risk_dist > 0 and low <= (t.entry - be_at * risk_dist):
						t.sl = t.entry
						t.breakeven_applied = True
			# Trailing stop after breakeven logic
			if enable_trail and t.initial_risk_dist > 0 and trail_at > 0 and trail_dist > 0:
				if t.side == "BUY":
					curr_r = max(0.0, (high - t.entry)/t.initial_risk_dist)
					if curr_r > t.mfe_r:
						t.mfe_r = curr_r
					if t.mfe_r >= trail_at:
						candidate_sl = close - trail_dist * t.initial_risk_dist
						if candidate_sl > t.sl:
							t.sl = candidate_sl
				else:
					curr_r = max(0.0, (t.entry - low)/t.initial_risk_dist)
					if curr_r > t.mfe_r:
						t.mfe_r = curr_r
					if t.mfe_r >= trail_at:
						candidate_sl = close + trail_dist * t.initial_risk_dist
						if candidate_sl < t.sl:
							t.sl = candidate_sl
			if t.side == "BUY":
				hit_tp = high >= t.tp
				hit_sl = low <= t.sl
				if hit_tp and hit_sl:
					hit_tp = False  # worst case
				if hit_tp or hit_sl:
					exit_price = t.tp if hit_tp else t.sl
					exit_price = self._apply_slippage(exit_price, t.side, False)
					t.exit_price = exit_price
					t.close_index = idx
					t.result = "WIN" if hit_tp else "LOSS"
					# compute R: (exit-entry)/(entry - SL distance) for BUY
					risk_dist = t.entry - t.sl
					gross_r = (exit_price - t.entry) / risk_dist if risk_dist > 0 else 0.0
					# add any realized partial R
					gross_r += t.realized_r_from_partial
					# fees convert to R: assume fee charged both entry & exit proportional to risk_dist fraction
					fee_r = 0.0
					if risk_dist > 0:
						fee_r = (self.fee_rate * t.entry + self.fee_rate * exit_price) / (risk_dist)
					t.pnl_r = gross_r - fee_r
			else:  # SELL
				hit_tp = low <= t.tp
				hit_sl = high >= t.sl
				if hit_tp and hit_sl:
					hit_tp = False
				if hit_tp or hit_sl:
					exit_price = t.tp if hit_tp else t.sl
					exit_price = self._apply_slippage(exit_price, t.side, False)
					t.exit_price = exit_price
					t.close_index = idx
					t.result = "WIN" if hit_tp else "LOSS"
					risk_dist = t.sl - t.entry
					gross_r = (t.entry - exit_price) / risk_dist if risk_dist > 0 else 0.0
					gross_r += t.realized_r_from_partial
					fee_r = 0.0
					if risk_dist > 0:
						fee_r = (self.fee_rate * t.entry + self.fee_rate * exit_price) / (risk_dist)
					t.pnl_r = gross_r - fee_r

	def report(self, write_csv: bool = True):
		closed = [t for t in self.trades if t.result in ("WIN","LOSS")]
		wins = sum(1 for t in closed if t.result == "WIN")
		losses = sum(1 for t in closed if t.result == "LOSS")
		unresolved = sum(1 for t in self.trades if t.result == "UNRESOLVED")
		total_closed = len(closed)
		win_rate = (wins / total_closed * 100) if total_closed else 0.0
		pnl_r = sum(t.pnl_r for t in closed)
		risk_pct = get_cfg("RISK_PERCENT") / 100.0
		balance_change_pct = pnl_r * risk_pct * 100
		# advanced stats
		equity_curve = []
		cum = 0.0
		for t in closed:
			cum += t.pnl_r
			equity_curve.append(cum)
		max_dd = 0.0
		peak = -1e9
		for v in equity_curve:
			if v > peak:
				peak = v
			dd = peak - v
			if dd > max_dd:
				max_dd = dd
		avg_win = (sum(t.pnl_r for t in closed if t.pnl_r > 0) / wins) if wins else 0.0
		avg_loss = (sum(-t.pnl_r for t in closed if t.pnl_r < 0) / losses) if losses else 0.0
		payoff = (avg_win / avg_loss) if avg_loss else 0.0
		expectancy = (wins/total_closed * avg_win - losses/total_closed * avg_loss) if total_closed else 0.0
		# pseudo Sharpe (mean / std of trade R)
		import math
		r_list = [t.pnl_r for t in closed]
		sharpe_like = 0.0
		if len(r_list) > 1:
			mean_r = sum(r_list)/len(r_list)
			var = sum((x-mean_r)**2 for x in r_list)/(len(r_list)-1)
			std = math.sqrt(var) if var>0 else 0
			if std>0:
				sharpe_like = mean_r/std * (len(r_list)**0.5)

		print("===== Backtest Report =====")
		print(f"Candles processed: {len(self.df_full)}")
		print(f"Signals -> BUY: {self.buy_signals} | SELL: {self.sell_signals}")
		print(f"Trades -> Total: {len(self.trades)} | Closed: {total_closed} | Open unresolved: {unresolved}")
		print(f"Wins: {wins} | Losses: {losses} | Win rate: {win_rate:.2f}%")
		print(f"Net R (after fees): {pnl_r:.2f}R (Risk per trade {risk_pct*100:.2f}% -> Balance change {balance_change_pct:.2f}%)")
		if total_closed:
			print(f"Avg win: {avg_win:.2f}R | Avg loss: -{avg_loss:.2f}R | Payoff: {payoff:.2f}")
			print(f"Expectancy: {expectancy:.2f}R/trade | Max DD: {max_dd:.2f}R | Sharpe-like: {sharpe_like:.2f}")
		print("===========================")

		if write_csv and total_closed:
			import csv
			with open("trades.csv","w", newline="", encoding="utf-8") as f:
				w = csv.writer(f)
				w.writerow(["side","entry","sl","tp","exit","result","pnl_r","open_index","close_index"])
				for t in closed:
					w.writerow([t.side, f"{t.entry:.6f}", f"{t.sl:.6f}", f"{t.tp:.6f}", f"{t.exit_price:.6f}", t.result, f"{t.pnl_r:.4f}", t.open_index, t.close_index])
			print("Saved trades.csv")


def parse_args():
	ap = argparse.ArgumentParser()
	ap.add_argument("--symbol", default=DEFAULT_SYMBOL.upper(), help="Symbol e.g. SOLUSDT")
	ap.add_argument("--interval", default="1m", help="Kline interval (1m,5m,15m,1h,...)")
	ap.add_argument("--limit", type=int, default=1000, help="Number of candles to fetch (can be >1000; will batch)")
	ap.add_argument("--no-progress", action="store_true", help="Disable run-phase progress bar")
	return ap.parse_args()


def main():
	args = parse_args()
	print(f"Fetching {args.limit} candles for {args.symbol} {args.interval} ...")
	df = fetch_klines(args.symbol, args.interval, args.limit)
	print(f"Dataset size: {len(df)}")
	bt = Backtester(df, show_progress=not args.no_progress)
	bt.run()
	bt.report()


if __name__ == "__main__":
	main()

