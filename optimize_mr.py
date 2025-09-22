"""Mean-Reversion parameter optimizer.

Searches MR-specific parameters for evaluate_signal() when STRATEGY_MODE=MR.
Splits data into train/validation and ranks by validation win rate, then trades, then expectancy.
"""
from __future__ import annotations
import itertools, json, argparse
import pandas as pd
from backtest import fetch_klines, Backtester
from bot_sol_signals import set_cfg, get_cfg

# Grid for Mean Reversion
PARAM_GRID = {
    "EMA_LONG": [48, 60, 80, 120],
    "K_SL": [0.8, 1.0, 1.2],
    "TP_R": [0.5, 0.8, 1.0],
    "MR_DEV_PCT": [0.20, 0.35, 0.50, 0.80],  # percent distance from EMA_LONG
    "MR_RSI_BUY_MAX": [25, 30, 35, 40],
    "MR_RSI_SELL_MIN": [60, 65, 70, 75],
    "MR_TP_TO_EMA": [True, False],
    "MR_USE_BB": [True, False],
    "MR_CONFIRM_CROSSBACK": [True, False],
    "MR_BB_PERIOD": [20, 24],
    "MR_BB_STD": [2.0, 2.5],
}
KEEP_ORIG = list(PARAM_GRID.keys()) + ["TRADE_SIDE", "STRATEGY_MODE", "HIGHER_TF"]


def evaluate_dataset(df: pd.DataFrame):
    bt = Backtester(df, show_progress=False)
    bt.run()
    closed = [t for t in bt.trades if t.result in ("WIN","LOSS")]
    wins = sum(1 for t in closed if t.result=="WIN")
    losses = sum(1 for t in closed if t.result=="LOSS")
    total = len(closed)
    pnl_r = sum(t.pnl_r for t in closed)
    win_rate = wins/total*100 if total else 0.0
    avg_win = (sum(t.pnl_r for t in closed if t.pnl_r>0)/wins) if wins else 0.0
    avg_loss = (sum(-t.pnl_r for t in closed if t.pnl_r<0)/losses) if losses else 0.0
    payoff = (avg_win/avg_loss) if avg_loss else 0.0
    expectancy = (wins/total*avg_win - losses/total*avg_loss) if total else 0.0
    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "pnl_r": pnl_r,
        "expectancy": expectancy,
        "payoff": payoff
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SOLUSDT")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--train_frac", type=float, default=0.6)
    ap.add_argument("--min_train_trades", type=int, default=5)
    ap.add_argument("--min_val_trades", type=int, default=3)
    ap.add_argument("--apply_best", action="store_true")
    ap.add_argument("--final_eval", action="store_true")
    ap.add_argument("--require_positive", action="store_true")
    ap.add_argument("--only_side", choices=["BOTH","BUY","SELL"], default="BOTH")
    ap.add_argument("--min_val_expectancy", type=float, default=-999.0)
    args = ap.parse_args()

    print(f"Fetching {args.limit} candles...")
    full = fetch_klines(args.symbol, args.interval, args.limit)

    # Save & adjust runtime config
    original = {k:get_cfg(k) for k in KEEP_ORIG}
    # Force MR mode and disable HTF during search
    set_cfg("STRATEGY_MODE", "MR")
    orig_htf = original.get("HIGHER_TF")
    set_cfg("HIGHER_TF", "0")

    n = len(full)
    split = int(n*args.train_frac)
    train_df = full.iloc[:split].reset_index(drop=True)
    val_df = full.iloc[split:].reset_index(drop=True)
    print(f"Train candles: {len(train_df)}  Validation candles: {len(val_df)}")

    combos = list(itertools.product(*PARAM_GRID.values()))
    print(f"Total combos: {len(combos)}")
    results: list[dict] = []
    best: dict | None = None

    def run_search(min_train: int, min_val: int):
        nonlocal best
        local_results = []
        for combo in combos:
            params = dict(zip(PARAM_GRID.keys(), combo))
            for side in ([args.only_side] if args.only_side != "BOTH" else ["BOTH","BUY","SELL"]):
                set_cfg("TRADE_SIDE", side)
                for k,v in params.items():
                    set_cfg(k, v)
                train_stats = evaluate_dataset(train_df)
                if train_stats["trades"] < min_train:
                    continue
                val_stats = evaluate_dataset(val_df)
                if val_stats["trades"] < min_val:
                    continue
                if val_stats["expectancy"] < args.min_val_expectancy:
                    continue
                rec = {**params, "TRADE_SIDE": side, **{f"train_{k}":v for k,v in train_stats.items()}, **{f"val_{k}":v for k,v in val_stats.items()}}
                local_results.append(rec)
                # Selection
                if args.require_positive:
                    def key(r):
                        return (r["val_expectancy"] >= 0, r["val_win_rate"], r["val_trades"], r["val_expectancy"])
                    if best is None or key(rec) > key(best):
                        best = rec
                else:
                    if best is None or (rec["val_win_rate"], rec["val_trades"], rec["val_expectancy"]) > (best["val_win_rate"], best["val_trades"], best["val_expectancy"]):
                        best = rec
        return local_results

    # First pass
    results = run_search(args.min_train_trades, args.min_val_trades)
    # Fallback with minimal thresholds
    if not results and (args.min_train_trades > 1 or args.min_val_trades > 1):
        print("No results with current thresholds; retrying with minimal thresholds (1/1)...")
        results = run_search(1, 1)

    # Restore original settings
    for k,v in original.items():
        set_cfg(k, v)
    set_cfg("HIGHER_TF", orig_htf)

    if not results:
        print("No viable parameter sets found.")
        return

    results.sort(key=lambda r: (r["val_win_rate"], r["val_trades"], r["val_expectancy"]), reverse=True)
    out_path = "optimize_mr_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Saved all results -> {out_path}")

    best_path = "best_mr_params.json"
    with open(best_path, "w", encoding="utf-8") as f:
        json.dump(best, f, indent=2)
    print("Best:")
    print(json.dumps(best, indent=2))
    print(f"Best params saved -> {best_path}")

    if args.apply_best and best:
        # Apply MR mode and best params
        set_cfg("STRATEGY_MODE", "MR")
        for k in PARAM_GRID.keys():
            set_cfg(k, best[k])
        set_cfg("TRADE_SIDE", best.get("TRADE_SIDE", "BOTH"))
        print("Applied best MR params to config.json")
        if args.final_eval:
            # Restore HTF setting from original
            set_cfg("HIGHER_TF", orig_htf)
            final_stats = evaluate_dataset(full)
            print("Final evaluation on full dataset:")
            print(json.dumps(final_stats, indent=2))

if __name__ == "__main__":
    main()
