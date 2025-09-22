# Bot SOL Signals & Backtest

Bot tạo tín hiệu giao dịch cho cặp (mặc định) SOLUSDT dựa trên nhiều indicator (EMA crossover, MACD, RSI, Bollinger, ATR, Supertrend, volume) và lọc đa khung thời gian. Có thêm script backtest để đánh giá hiệu quả cơ bản.

## Cấu hình
Các tham số được lưu trong `config.json` và có thể đặt qua biến môi trường hoặc lệnh Telegram (`/set`).

Các key chính:
- EMA_SHORT / EMA_LONG
- RSI_PERIOD, ATR_PERIOD
- MACD_FAST / MACD_SLOW / MACD_SIGNAL
- BB_PERIOD / BB_STD_MULT
- K_SL (hệ số ATR cho Stop Loss)
- TP_R (Risk:Reward mục tiêu)
- RISK_PERCENT (rủi ro % mỗi lệnh khi tính size)
- HIGHER_TF (khung xác nhận xu hướng, ví dụ 15m) – đặt "0" hoặc rỗng để tắt lọc HTF
- VOLATILITY_THRESHOLD (lọc khi ATR/price quá cao)
- SUPERTREND_PERIOD / SUPERTREND_MULT
 - FEE_RATE (phí mỗi chiều, ví dụ 0.0004 = 0.04%)
 - SLIPPAGE_BPS (basis points trượt giá 1 bps = 0.01%)
 - MAX_CONCURRENT_TRADES (số lệnh tối đa song song trong backtest)
 - SIGNAL_AGGRESSIVENESS (mức "hung hãn" của tín hiệu: 1.0 nguyên bản, >1.0 nới lỏng điều kiện)

### SIGNAL_AGGRESSIVENESS hoạt động thế nào?
Mục tiêu: tăng số lượng tín hiệu khi mặc định quá ít. Khi tăng giá trị này:

1. Giảm yêu cầu `MIN_CONFIRM` (không thấp hơn 1).
2. Cho phép các tổ hợp EMA/MACD yếu hơn (relaxed_long / relaxed_short) đủ điều kiện.
3. Nới lỏng bộ lọc biến động: cho phép ATR/price cao hơn một chút.
4. Nới rộng biên RSI (BUY vẫn chấp nhận RSI cao hơn, SELL chấp nhận RSI thấp hơn).
5. Ở mức >= 2.0 có thể bỏ qua xung đột xu hướng HTF (ghi lý do `ignore_htf_contradiction_aggr`).

Khuyến nghị:
| Giá trị | Mô tả | Dùng khi |
|--------|-------|---------|
| 1.0 | Chuẩn, bảo thủ | Giai đoạn nhiễu cao, ưu tiên chất lượng |
| 1.5 | Tăng nhẹ | Muốn thêm tín hiệu nhưng vẫn giữ lọc HTF |
| 2.0 | Tăng mạnh | Dữ liệu ít tín hiệu, chấp nhận nhiều nhiễu |
| 2.5+ | Rất nhiều tín hiệu | Chỉ dùng để khám phá / tối ưu, rủi ro nhiễu lớn |

LƯU Ý: Tăng aggressiveness thường làm giảm chất lượng và giảm hiệu suất (PnL). Luôn backtest sau khi thay đổi.

## Chạy bot realtime
Yêu cầu biến môi trường BOT_TOKEN và CHAT_ID.

```
python bot_sol_signals.py
```

## Backtest
Backtest tối giản: mô phỏng lần lượt từng cây nến lịch sử, mở 1 vị thế tại tín hiệu (BUY hoặc SELL) và đóng khi chạm TP hoặc SL. Không tính phí, trượt giá, partial fills.

### Lưu ý về Higher Timeframe (HTF)
Trong runtime bot, dữ liệu HTF được cache 30 giây (giảm gọi REST). Khi backtest, việc gọi HTF vẫn diễn ra nhưng có cache mức code (nếu bạn để HIGHER_TF != 0). Muốn bỏ ảnh hưởng HTF để kiểm tra raw strategy: thiết lập `HIGHER_TF = 0` rồi chạy lại.

### Chạy
```
python backtest.py --symbol SOLUSDT --interval 1m --limit 1000
```
Tham số:
- --symbol: mặc định SOLUSDT
- --interval: 1m,5m,15m,1h,... (Binance)
- --limit: số nến (<=1000 mỗi lần gọi)

### Kết quả báo cáo
- Số tín hiệu BUY / SELL
- Số lệnh WIN / LOSS & win rate
- Tổng R (TP = +TP_R R, SL = -1 R)
- Thay đổi % balance giả định (dựa trên RISK_PERCENT mỗi lệnh)
 - Avg win / Avg loss / Payoff
 - Expectancy, Max Drawdown (R), Sharpe-like
 - Xuất file trades.csv (các lệnh đã đóng)

### Giới hạn hiện tại
- Chỉ 1 vị thế tại một thời điểm.
- Nếu cùng nến chạm TP và SL chọn kịch bản xấu (SL) để bảo thủ.
- Không mô phỏng phí, funding, slippage.
- Các lệnh chưa đóng cuối chuỗi được đánh dấu UNRESOLVED (không tính vào win/loss).

## Nâng cấp backtest (gợi ý tương lai)
- Thêm phí giao dịch và slippage.
- Cho phép nhiều vị thế hoặc pyramiding.
- Ghi log chi tiết từng trade ra CSV.
- Walk-forward multi-run với bộ tham số khác nhau (optimization grid search).
- Tính thêm max drawdown, Sharpe, expectancy.
 - (ĐÃ LÀM) Phí, slippage, multi-trade, metrics, optimizer cơ bản.

## Optimizer
Chạy brute-force đơn giản trên một bộ dữ liệu nến:
```
python optimize.py --symbol SOLUSDT --interval 1m --limit 800 \
	--ema_short 30,50 --ema_long 100,150 --k_sl 1.0,1.5 --tp_r 1.5,2.0 --min_confirm 1,2
```
Kết quả in bảng top theo pnl_r và lưu best_params.json.
Lưu ý: chỉ một giai đoạn dữ liệu -> dễ overfit. Nên kiểm tra lại bằng backtest khác hoặc nhiều segment.

## Cảnh báo
Chiến lược chỉ mang tính thử nghiệm. Kết quả backtest không đảm bảo lợi nhuận thực tế. Luôn quản lý rủi ro cẩn thận.

