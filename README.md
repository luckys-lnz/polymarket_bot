# Polymarket Crypto Market Bot

This project scans Polymarket crypto price markets, estimates fair value with a binary-options model, and either:

- logs signals in read-only mode
- simulates trades in paper mode
- places live limit orders on Polymarket

It is not a general-purpose Polymarket bot. The code is specifically built around crypto price questions that can be parsed into a strike, direction, and expiry.

## What This Repo Does

The strategy combines:

- live spot prices from Binance
- implied volatility from Deribit DVOL and Binance options
- a Black-Scholes-style binary pricer with skew and jump-risk adjustments
- Polymarket market discovery and execution
- optional Telegram approval before live orders are submitted

When the model thinks the YES token is underpriced, it buys YES. When the YES token is overpriced, it buys NO. Position sizing uses a capped fractional Kelly formula.

## Operating Modes

### 1. Paper trader

`paper_trader.py` runs the full signal pipeline against live market data, but never submits orders. It records simulated entries to `paper_trades.json`, updates mark-to-market P&L, and prints a summary.

This is the safest way to evaluate whether the strategy is finding sensible trades.

### 2. Dashboard

`dashboard.py` is a Rich terminal UI for `paper_trades.json`. It shows:

- bankroll, deployed capital, unrealised and realised P&L
- current open positions
- historical paper entries
- signal context: fair value vs market price

### 3. Live bot

`polymarket_bot.py` connects to Polymarket, evaluates markets, and submits limit orders through `py-clob-client`.

Live mode is not exposed as a CLI flag right now. The entrypoint currently runs:

```python
asyncio.run(bot.run(trading_enabled=False))
```

To enable real trading you have to change that flag to `True` in `polymarket_bot.py`.

## What Markets It Tries To Trade

The parser in `strategy.py` is aimed at crypto markets for these assets:

- BTC
- ETH
- SOL
- XRP
- BNB

Supported question shapes are:

- price targets: `Will BTC be above $90,000 by April?`
- downside targets: `Will Bitcoin dip to $65,000 in March?`
- less-than markets: `Will the price of Ethereum be less than $2,000 on April 1?`
- range markets: `Will BTC be between $76,000 and $78,000 on March 20?`
- up/down percentage markets: `Will SOL be up 10% this week?`

If a Polymarket question does not match one of those formats, the strategy ignores it.

## How The Strategy Works

At a high level:

1. Pull active Polymarket markets above a minimum volume threshold.
2. Parse crypto markets into asset, strike, expiry, and direction.
3. Read live spot from Binance.
4. Refresh implied volatility from Deribit and Binance options.
5. Price the market as a binary option.
6. Compare model fair value to the Polymarket YES price.
7. If edge exceeds `min_edge`, buy YES or buy NO.
8. Size the order with capped half-Kelly.

Important filters in the current implementation:

- low-volume markets are ignored
- very short-dated markets are ignored
- tiny near-zero-price signals are ignored
- extreme tail outcomes needing more than roughly 3 sigma are ignored

## Project Layout

- `polymarket_bot.py`: live bot, Polymarket API clients, order manager, read-only demo mode
- `strategy.py`: market parsing, spot/IV feeds, fair-value model, signal generation
- `paper_trader.py`: paper trading loop and trade ledger persisted to JSON
- `dashboard.py`: terminal dashboard for paper-trade output
- `notifier.py`: Telegram approval workflow for live signals
- `get_creds.py`: helper to derive Polymarket API credentials from your private key
- `debug_markets.py`: quick scan of likely crypto markets in top-volume Polymarket listings
- `debug_weekly.py`: regex coverage checker against live crypto market questions
- `test_telegram.py`: sends a test approval request to your Telegram bot
- `paper_trades.json`: current paper-trade ledger used by the dashboard

## Setup

### Requirements

- Python 3.11+ is a safe target for this codebase
- internet access to Polymarket, Binance, and Deribit
- a Polymarket wallet and CLOB API credentials for live mode

Install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the repo root.

For live trading:

```env
PRIVATE_KEY=your_polygon_wallet_private_key
API_KEY=your_polymarket_api_key
API_SECRET=your_polymarket_api_secret
API_PASSPHRASE=your_polymarket_api_passphrase
```

For Telegram approvals:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_numeric_chat_id
```

Notes:

- `paper_trader.py` and `dashboard.py` do not require Polymarket API credentials.
- `polymarket_bot.py` does require the live trading credentials even in read-only demo mode, because it still constructs the order manager and loads wallet-linked positions.

### Deriving Polymarket API Credentials

If you already have `PRIVATE_KEY` set, you can derive the CLOB credentials with:

```bash
python3 get_creds.py
```

Copy the printed values into `.env`.

## Running The Project

### Paper trading

Run one scan:

```bash
python3 paper_trader.py
```

Run continuously:

```bash
python3 paper_trader.py --loop --interval 3600
```

Useful tuning flags:

```bash
python3 paper_trader.py --bankroll 200 --max-order 10 --min-edge 0.05
```

### Dashboard

Render once:

```bash
python3 dashboard.py --bankroll 200
```

Auto-refresh every 30 seconds:

```bash
python3 dashboard.py --live --bankroll 200
```

The dashboard default bankroll is `500`, while the paper trader default bankroll is `200`. Pass the same bankroll to both if you want the summary numbers to line up.

### Web Dashboard (HTMX + Tailwind)

Run the lightweight operator dashboard locally:

```bash
uvicorn web_dashboard:app --host 127.0.0.1 --port 8080 --reload
```

Optional token guard for local/LAN use:

```bash
export DASHBOARD_TOKEN=your_secret_token
uvicorn web_dashboard:app --host 127.0.0.1 --port 8080 --reload
```

When token guard is enabled, open with:

```text
http://127.0.0.1:8080/?token=your_secret_token
```

Then open:

```text
http://127.0.0.1:8080
```

The web dashboard reads bot state from `.bot_state`, `position_registry.json`,
`order_registry.json`, and `trade_analytics_summary.json` and auto-refreshes each panel.

### Profit-Only 8-Day Soak Config

For an 8-day autonomous test where the bot focuses only on money-making behavior,
set the following env values before launch:

```bash
export AUTO_TUNE=1
export AUTO_TUNE_INTERVAL_SECS=600
export AUTO_TUNE_GLOBAL_COOLDOWN_SECS=3600
export AUTO_TUNE_ASSET_COOLDOWN_SECS=21600
export AUTO_TUNE_MIN_CLOSED_GLOBAL=8
export AUTO_TUNE_MIN_CLOSED_ASSET=6
export AUTO_TUNE_WINRATE_DEADBAND=0.08
export AUTO_TUNE_ASSET_MIN_ABS_PNL=1.0
```

Recommended run command for soak:

```bash
python3 main.py --paper --scan-interval 60
```

This configuration keeps auto-tune active, but prevents frequent parameter flips
unless there is enough trade sample and directional performance signal.

### Read-only live bot demo

This runs the live market scanner and prints signals, but does not place orders:

```bash
python3 polymarket_bot.py
```

Again, this still expects the Polymarket wallet and API credentials in `.env`.

### Live trading

Edit `polymarket_bot.py` and change:

```python
asyncio.run(bot.run(trading_enabled=False))
```

to:

```python
asyncio.run(bot.run(trading_enabled=True))
```

Then run:

```bash
python3 polymarket_bot.py
```

If `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are present, signals are sent to Telegram for approval. A signal auto-trades if there is no response within 10 minutes. If Telegram is not configured, the bot auto-trades every qualifying signal immediately.

### Telegram approval test

```bash
python3 test_telegram.py
```

## Data Sources And Execution Path

- Polymarket Gamma API: active market discovery and market snapshots
- Polymarket Data API: wallet positions and activity
- Polymarket CLOB SDK: live order submission and order management
- Polymarket WebSocket: book monitoring
- Binance ticker WebSocket: live spot prices
- Deribit DVOL API: BTC and ETH volatility
- Binance options API: ATM implied volatility for BTC, ETH, SOL, XRP, BNB

## Current Limitations

These are worth knowing before you trust the bot with money:

- Live trading is enabled by editing code, not by a safer CLI/config switch.
- The strategy only trades market questions that match the current regex parser.
- The live bot only buys into signals; there is no automated exit, hedging, or position reduction logic.
- The paper trader keeps its ledger in memory for the current process. Restarting it does not reload prior trades from `paper_trades.json`.
- The paper trader writes and marks positions, but it does not currently resolve/settle trades on its own. Realised P&L only exists if trades are explicitly closed in the ledger.
- The live trading loop scans on a fixed 60-second cadence in code.
- Pricing is model-driven and depends on external market data quality from Binance and Deribit.
- This repo does not include historical backtesting, parameter search, or deployment tooling.

## Safety Notes

- Start with paper trading.
- Run the read-only bot before enabling live execution.
- Keep `max_order_usdc` small until you have observed several days of signals.
- Review Polymarket eligibility and terms before live trading.

## Signature

`luckys-lnz`

The code itself already includes a warning that Polymarket restricts trading for US persons. Treat that as a real operational constraint, not a boilerplate note.
