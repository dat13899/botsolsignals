"""Walk-forward optimizer for TREND mode.

Performs k-fold chronological splits (rolling windows) on the dataset.
Ranks parameter combos by median validation expectancy across folds,
then by median win rate and total trades.
"""
from __future__ import annotations
import itertools, json, argparse, statistics
import pandas as pd
from typing import List, Dict
from backtest import fetch_klines, Backtester
from bot_sol_signals import set_cfg, get_cfg

PARAM_GRID = {
    "EMA_SHORT": [12, 16, 20],
    "EMA_LONG": [48, 60, 80],
    "K_SL": [1.0, 1.2],
    "TP_R": [0.8, 1.0, 1.2],
    "RSI_BUY_CAP": [60, 65],
    "RSI_SELL_FLOOR": [20, 25],
    "PRICE_ABOVE_LONG_PCT": [0.00, 0.05],
    "PRICE_BELOW_LONG_PCT": [0.00, 0.05],
}
PT_GRID = {
    "ENABLE_PARTIAL_TP": [True, False],
    "PARTIAL_AT_R": [0.8, 1.0],
    "PARTIAL_SIZE_FRACTION": [0.4, 0.6],
    "PARTIAL_MOVE_BE": [True],
}
KEEP_ORIG = list(PARAM_GRID.keys()) + list(PT_GRID.keys()) + ["HIGHER_TF", "TRADE_SIDE", "STRATEGY_MODE"]


def evaluate_dataset(df: pd.DataFrame) -> Dict[str, float]:
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
        "payoff": payoff,
    }


def walkforward_splits(df: pd.DataFrame, k: int) -> List[pd.DataFrame]:
    n = len(df)
    size = n // k
    splits = []
    for i in range(k):
        start = i * size
        end = n if i == k-1 else (i+1) * size
        part = df.iloc[start:end].reset_index(drop=True)
        if len(part) > 200:  # avoid tiny pieces
            splits.append(part)
    return splits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="SOLUSDT")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--min_trades_per_fold", type=int, default=3)
    ap.add_argument("--apply_best", action="store_true")
    ap.add_argument("--final_eval", action="store_true")
    ap.add_argument("--require_positive", action="store_true")
    ap.add_argument("--only_side", choices=["BOTH","BUY","SELL"], default="BOTH")
    ap.add_argument("--include_partial", action="store_true", help="Include partial TP parameters in the grid search")
    args = ap.parse_args()

    print(f"Fetching {args.limit} candles...")
    full = fetch_klines(args.symbol, args.interval, args.limit)

    original = {k:get_cfg(k) for k in KEEP_ORIG}
    # Force TREND and disable HTF during grid search to reduce latency
    set_cfg("STRATEGY_MODE", "TREND")
    orig_htf = original.get("HIGHER_TF")
    set_cfg("HIGHER_TF", "0")

    splits = walkforward_splits(full, args.folds)
    print(f"Folds: {len(splits)}")

    base_items = list(PARAM_GRID.items())
    if args.include_partial:
        base_items += list(PT_GRID.items())
    combos = list(itertools.product(*[v for _,v in base_items]))
    combo_keys = [k for k,_ in base_items]
    results: List[dict] = []
    best = None

    for combo in combos:
        params = dict(zip(combo_keys, combo))
        fold_stats = []
        for side in ([args.only_side] if args.only_side != "BOTH" else ["BOTH","BUY","SELL"]):
            set_cfg("TRADE_SIDE", side)
            for k,v in params.items():
                set_cfg(k, v)
            per_fold = []
            valid = True
            for dfp in splits:
                s = evaluate_dataset(dfp)
                if s["trades"] < args.min_trades_per_fold:
                    valid = False
                    break
                per_fold.append(s)
            if not valid or not per_fold:
                continue
            med_expect = statistics.median(x["expectancy"] for x in per_fold)
            med_win = statistics.median(x["win_rate"] for x in per_fold)
            sum_trades = sum(x["trades"] for x in per_fold)
            rec = {**params, "TRADE_SIDE": side, "median_expectancy": med_expect, "median_win_rate": med_win, "sum_trades": sum_trades}
            results.append(rec)
            if args.require_positive:
                def key(r):
                    return (r["median_expectancy"] >= 0, r["median_expectancy"], r["median_win_rate"], r["sum_trades"])
                if best is None or key(rec) > key(best):
                    best = rec
            else:
                if best is None or (med_expect, med_win, sum_trades) > (best.get("median_expectancy", -1e9), best.get("median_win_rate", 0.0), best.get("sum_trades", 0)):
                    best = rec

    # Restore
    for k,v in original.items():
        set_cfg(k, v)
    set_cfg("HIGHER_TF", orig_htf)

    if not results:
        print("No viable parameter sets found.")
        return

    results.sort(key=lambda r: (r["median_expectancy"], r["median_win_rate"], r["sum_trades"]), reverse=True)
    with open("optimize_walkforward_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("Saved walk-forward results -> optimize_walkforward_results.json")

    best_path = "best_walkforward_params.json"
    with open(best_path, "w", encoding="utf-8") as f:
        json.dump(best, f, indent=2)
    print("Best:")
    print(json.dumps(best, indent=2))
    print(f"Best params saved -> {best_path}")

    if best and args.apply_best:
        set_cfg("STRATEGY_MODE", "TREND")
        for k in PARAM_GRID.keys():
            set_cfg(k, best[k])
        set_cfg("TRADE_SIDE", best.get("TRADE_SIDE", "BOTH"))
        print("Applied best walk-forward params to config.json")
        if args.final_eval:
            # Evaluate on full dataset (with original HTF)
            set_cfg("HIGHER_TF", orig_htf)
            stats = evaluate_dataset(full)
            print("Final evaluation on full dataset:")
            print(json.dumps(stats, indent=2))

if __name__ == "__main__":
    main()
