"""Simple parameter sweep optimizer.

Usage:
  python optimize.py --symbol SOLUSDT --interval 1m --limit 800 \
      --ema_short 30,50 --ema_long 100,150 --k_sl 1.0,1.5 --tp_r 1.5,2.0 --min_confirm 1,2

Outputs table to stdout and saves best_params.json.

NOTE: Uses single fetched dataset (no walk-forward). Overfitting risk high.
"""
from __future__ import annotations
import argparse, itertools, json
import pandas as pd
from bot_sol_signals import get_cfg, set_cfg, evaluate_signal
from backtest import Backtester, fetch_klines

# We'll temporarily override cfg via set_cfg then restore.

KEEP_KEYS = [
    "EMA_SHORT","EMA_LONG","K_SL","TP_R","MIN_CONFIRM"
]

def parse_list(s: str):
    return [type_cast(x) for x in s.split(',') if x.strip()]

def type_cast(v: str):
    if v.isdigit():
        return int(v)
    try:
        return float(v)
    except Exception:
        return v

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', default='SOLUSDT')
    ap.add_argument('--interval', default='1m')
    ap.add_argument('--limit', type=int, default=800)
    ap.add_argument('--ema_short', default='50')
    ap.add_argument('--ema_long', default='200')
    ap.add_argument('--k_sl', default='1.5')
    ap.add_argument('--tp_r', default='2.0')
    ap.add_argument('--min_confirm', default='2')
    return ap.parse_args()


def main():
    args = parse_args()
    print(f"Fetching data {args.symbol} {args.interval} {args.limit}...")
    df = fetch_klines(args.symbol, args.interval, args.limit)

    grid = {
        'EMA_SHORT': parse_list(args.ema_short),
        'EMA_LONG': parse_list(args.ema_long),
        'K_SL': parse_list(args.k_sl),
        'TP_R': parse_list(args.tp_r),
        'MIN_CONFIRM': parse_list(args.min_confirm)
    }

    original = {k: get_cfg(k) for k in KEEP_KEYS}
    results = []
    combos = list(itertools.product(*grid.values()))
    print(f"Total combos: {len(combos)}")

    for combo in combos:
        params = dict(zip(grid.keys(), combo))
        # apply overrides
        for k,v in params.items():
            set_cfg(k, v)
        bt = Backtester(df)
        bt.run()
        closed = [t for t in bt.trades if t.result in ("WIN","LOSS")]
        pnl_r = sum(t.pnl_r for t in closed)
        wins = sum(1 for t in closed if t.result=='WIN')
        losses = sum(1 for t in closed if t.result=='LOSS')
        total = len(closed)
        win_rate = wins/total*100 if total else 0
        results.append({**params, 'pnl_r': pnl_r, 'win_rate': win_rate, 'trades': total})

    # restore original
    for k,v in original.items():
        set_cfg(k,v)

    dfres = pd.DataFrame(results).sort_values('pnl_r', ascending=False)
    print(dfres.head(20).to_string(index=False))
    if not dfres.empty:
        best = dfres.iloc[0].to_dict()
        with open('best_params.json','w',encoding='utf-8') as f:
            json.dump(best, f, indent=2)
        print('Best params saved to best_params.json')

if __name__ == '__main__':
    main()
