# Tom Hougaard Trading Bot

An automated Python trading bot implementing Tom Hougaard's (Trader Tom) price action methodology. Supports paper trading, live trading via Alpaca or ccxt, and backtesting against historical CSV data.

---

## Strategies Implemented

| Strategy | Description |
|---|---|
| **Opening Range Breakout** | Detects the 15-min opening range for DAX/US30 and enters on breakouts within the first 90 minutes |
| **Scenario-Based Swing** | Analyses weekly daily highs to anticipate directional moves (Scenario A & B) |
| **Mind the Gap** | Fades opening session gaps back to the previous session's close |
| **62 EMA Trend Filter** | Filters all breakout trades — only longs above EMA, shorts below |

---

## Risk Management

- **1% risk per trade** — position sized to the stop distance
- **Stop loss** placed beyond the most recent swing high/low
- **Trailing stop** activated at 1.5R profit
- **Max 2 trades per session** per instrument
- **Daily loss limit**: stops trading if down 2% on the day
- **No revenge trading**: skips the next marginal setup after a loss

---

## Project Structure

```
trading_bot/
├── config.yaml               # All configuration parameters
├── main.py                   # Entry point (live, paper, backtest)
├── requirements.txt
├── strategies/
│   ├── breakout.py           # Opening range breakout
│   ├── scenario.py           # Weekly scenario-based swing trades
│   ├── mind_the_gap.py       # Gap fade strategy
│   └── trend_filter.py       # 62-period EMA trend filter
├── risk/
│   └── position_sizer.py     # Position sizing, trailing stops, session tracker
├── broker/
│   └── connector.py          # Alpaca, ccxt, and paper broker connectors
├── utils/
│   ├── logger.py             # Color-coded console + file logging
│   └── database.py           # SQLite trade journal
└── tests/
    └── test_strategies.py    # Unit tests for all strategies
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd trading_bot
pip install -r requirements.txt
```

### 2. Configure `config.yaml`

Open `config.yaml` and set your parameters:

```yaml
broker:
  name: "alpaca"           # "alpaca", "ccxt", or "paper"
  paper_trading: true      # Always start with paper!
  api_key: "YOUR_KEY"
  api_secret: "YOUR_SECRET"
  base_url: "https://paper-api.alpaca.markets"

account:
  initial_balance: 10000.0

risk:
  max_risk_per_trade_pct: 1.0
  daily_loss_limit_pct: 2.0
  max_trades_per_session: 2
```

### 3. Run in paper trading mode (default)

```bash
cd trading_bot
python main.py
```

### 4. Run backtesting

Prepare a CSV file with columns: `timestamp,open,high,low,close,volume`

```bash
# Place CSV in data/ directory, then:
python main.py --backtest --symbol DAX --csv data/DAX_ohlcv.csv
```

Or enable backtesting in `config.yaml`:
```yaml
backtesting:
  enabled: true
  start_date: "2024-01-01"
  end_date: "2024-12-31"
```

### 5. Run tests

```bash
pytest tests/test_strategies.py -v
```

---

## Broker Configuration

### Alpaca (stocks/ETFs/crypto)

```yaml
broker:
  name: "alpaca"
  paper_trading: true
  api_key: "PKXXXXXXXXXXXXXXXX"
  api_secret: "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  base_url: "https://paper-api.alpaca.markets"
```

Install: `pip install alpaca-trade-api`

### ccxt (crypto, CFDs)

```yaml
broker:
  name: "ccxt"
  paper_trading: true       # Uses exchange sandbox mode
  api_key: "YOUR_KEY"
  api_secret: "YOUR_SECRET"
  ccxt_exchange: "binance"  # Any ccxt-supported exchange
```

Install: `pip install ccxt`

### Paper Broker (no account needed)

```yaml
broker:
  paper_trading: true  # Runs local simulation — no API needed
```

---

## Instruments

The bot monitors:

| Instrument | Open (UTC) | Timezone |
|---|---|---|
| DAX | 08:00 | Europe/Berlin |
| US30 (Dow Jones) | 14:30 | America/New_York |

Add or remove instruments in `config.yaml` under the `instruments` list.

---

## Output

### Console (color-coded)
- **Green bold** = LONG signal
- **Red bold** = SHORT signal
- **Cyan** = general signal info

### Log files
- `logs/trades.log` — full trade log
- `logs/error.log` — errors only

### SQLite database
- `trading_bot.db` — all trades with entry, exit, R-multiple, PnL
- Query with any SQLite browser or Python: `sqlite3 trading_bot.db`

### Daily summary (end of session)
```
============================================================
  DAILY SUMMARY — 2024-01-15
============================================================
  Trades taken  : 3
  Wins / Losses : 2 / 1
  Win Rate      : 66.7%
  Total R       : +2.10R
  P&L           : +$190.00 (+1.90%)
  End Balance   : $10,190.00
============================================================
```

---

## Strategy Details

### Opening Range Breakout

Tom Hougaard monitors the first 15 minutes of the DAX (09:00 CET) and US30 (09:30 EST) to identify the opening range. A close above the range high triggers a LONG; below the low triggers a SHORT. The 62 EMA must align, and the breakout candle must not be more than 2x the average candle size (avoids trading on news spikes).

### Scenario-Based Swing Trades

**Scenario A**: If Monday's high is higher than both Tuesday's and Wednesday's highs, the week is showing a distribution pattern. Enter SHORT at Wednesday's close, targeting a continuation lower on Thursday.

**Scenario B**: If Thursday's high is higher than Friday's high, the market failed to extend on Friday. Enter SHORT at Friday's close anticipating a gap-down open on Monday.

### Mind the Gap

On session open, if the gap between the previous close and current open is between the configured `min_gap_ticks` and `max_gap_ticks`:
- Gap UP → SHORT (fade back to previous close as TP)
- Gap DOWN → LONG (fade back to previous close as TP)

### 62 EMA Trend Filter

All breakout trades are filtered through the 62-period EMA:
- Only LONG trades when price > 62 EMA and EMA is sloping up
- Only SHORT trades when price < 62 EMA and EMA is sloping down

---

## Important Disclaimer

**This software is for educational purposes only.** Trading financial instruments involves significant risk. Always start in paper trading mode. Past performance does not guarantee future results. Never risk money you cannot afford to lose.

The strategies implemented are inspired by publicly discussed concepts from Tom Hougaard's educational content and are not guaranteed to be profitable.
