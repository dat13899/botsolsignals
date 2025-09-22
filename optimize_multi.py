"""Multi symbol/interval walk-forward runner.

Runs optimize_walkforward across a grid of symbols and intervals,
then writes a summary JSON sorted by best median expectancy.
"""
from __future__ import annotations
import json, argparse, itertools, subprocess, sys
from pathlib import Path

DEFAULT_SYMBOLS = [
    "SOLUSDT","BTCUSDT","ETHUSDT","SUIUSDT","BNBUSDT","DOGEUSDT"
]
DEFAULT_INTERVALS = ["1m","3m","5m"]


def run_once(symbol: str, interval: str, limit: int, folds: int, min_trades: int, only_side: str, require_positive: bool) -> dict:
    cmd = [sys.executable, "optimize_walkforward.py",
           "--symbol", symbol,
           "--interval", interval,
           "--limit", str(limit),
           "--folds", str(folds),
           "--min_trades_per_fold", str(min_trades),
           "--only_side", only_side]
    if require_positive:
        cmd.append("--require_positive")
    print("Running:", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent))
    out = res.stdout
    best_line = None
    for line in out.splitlines():
        if line.strip().startswith("Best:"):
            best_line = True
            continue
        if best_line:
            try:
                best_json = json.loads(line)
                best_json["SYMBOL"] = symbol
                best_json["INTERVAL"] = interval
                return best_json
            except Exception:
                break
    return {"SYMBOL": symbol, "INTERVAL": interval, "error": res.stderr[:4000], "stdout": out[-4000:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--intervals", nargs="*", default=DEFAULT_INTERVALS)
    ap.add_argument("--limit", type=int, default=6000)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--min_trades_per_fold", type=int, default=3)
    ap.add_argument("--only_side", choices=["BOTH","BUY","SELL"], default="BOTH")
    ap.add_argument("--require_positive", action="store_true")
    args = ap.parse_args()

    results = []
    for sym, tf in itertools.product(args.symbols, args.intervals):
        r = run_once(sym, tf, args.limit, args.folds, args.min_trades_per_fold, args.only_side, args.require_positive)
        results.append(r)

    # Sort by median_expectancy desc, then win rate
    def key(x):
        return (x.get("median_expectancy", -1e9), x.get("median_win_rate", 0.0))
    results.sort(key=key, reverse=True)

    with open("optimize_multi_summary.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("Saved -> optimize_multi_summary.json")
    print(json.dumps(results[:5], indent=2))

if __name__ == "__main__":
    main()
