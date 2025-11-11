# Binance Spot Auto Trading Bot

## Overview
A secure Telegram-based trading bot that executes trades on Binance Spot markets. All trading operations are performed via Telegram commands, while the Flask web server provides only status information.

**Status:** ✅ Fully functional and running on port 5000

**Last Updated:** November 11, 2025

## Features
- **Telegram Bot Interface:** All trading commands via Telegram
- **Spot Trading Only:** Works exclusively with Binance Spot markets (not futures)
- **Web Status Page:** Shows "Server Alive ✅" and server IP address
- **Automated Profit Monitoring:** Continuously monitors price every 5 seconds until profit target is reached
- **Telegram Notifications:** Sends updates for buy/sell actions and errors
- **Single Trade Execution:** Only one trade can run at a time to prevent conflicts
- **Thread-Safe:** Uses threading.Lock to protect trade state from race conditions
- **Security:** All sensitive credentials stored as environment variables

## Project Architecture

### Core Files
- **app.py** - Main Flask application with Telegram bot handlers and trading logic
- **templates/index.html** - Simple web status page (Server Alive + IP only)
- **requirements.txt** - Python package dependencies

### Key Components

#### 1. Flask Web Server (Port 5000)
- Displays "Server Alive ✅" status
- Shows server IP address
- NO trading functionality on web UI

#### 2. Telegram Bot
- **Command Handlers:**
  - `/start` or `/help` - Show welcome message and command list
  - `/trade <pair> <amount> <profit>` - Start a new trade
  - `/status` - Check active trade status
- Runs in separate thread alongside Flask server

#### 3. Trading Engine
- Validates inputs before execution
- Places market BUY orders via Binance API
- Monitors price in background thread
- Executes market SELL when profit target reached
- Thread-safe state management with locks

#### 4. Telegram Integration
- Sends formatted HTML messages for trade events
- Silent error handling to avoid spam
- Uses pyTelegramBotAPI

## Environment Variables Required

Set these in your Replit Secrets or environment:

- `BINANCE_API_KEY` - Binance API key (ensure withdrawal permissions are disabled)
- `BINANCE_SECRET_KEY` - Binance API secret key
- `TELEGRAM_TOKEN` - Telegram bot token
- `TELEGRAM_CHAT_ID` - Telegram chat ID for notifications (optional, bot responds to any chat)

## Telegram Bot Commands

### `/start` or `/help`
Shows welcome message with all available commands and rules.

### `/trade <pair> <amount> <profit>`
Starts a new trade.

**Example:**
```
/trade BTCUSDT 20 0.2
```

**Parameters:**
- `pair` - Trading pair (must end with USDT), e.g., BTCUSDT, ETHUSDT
- `amount` - Amount in USD (minimum $5)
- `profit` - Profit target in USD (must be > 0)

### `/status`
Check the current active trade status including:
- Trading pair
- Buy price and current price
- Quantity purchased
- Current profit vs target profit
- Progress percentage

## Trading Logic Flow

1. User sends `/trade` command to Telegram bot
2. System validates:
   - Pair ends with USDT and exists on Binance
   - Amount ≥ $5 USD
   - Profit target > 0
   - No other trade is currently running
3. If valid, execute market BUY order
4. Send Telegram confirmation with buy price and target
5. Background thread monitors price every 5 seconds
6. When `(current_price - buy_price) * quantity >= profit_target`:
   - Execute market SELL order
   - Send Telegram profit confirmation
   - Release trade lock
7. Bot becomes idle, ready for next trade

## Validation Rules

- **Pair:** Must end with USDT (e.g., BTCUSDT, ETHUSDT)
- **Amount:** Minimum $5 USD
- **Profit Target:** Must be greater than 0
- **Concurrent Trades:** Only 1 trade allowed at a time (enforced with threading.Lock)

## Error Handling

- Invalid inputs trigger Telegram warning with example format
- Binance API errors are retried silently to avoid spam
- If trade already running, new attempts are rejected with Telegram notification
- Thread-safe state management prevents race conditions

## Dependencies

```
flask - Web framework
python-binance - Binance API client
pyTelegramBotAPI - Telegram bot integration
requests - HTTP library for IP lookup
```

## Running the Application

The application runs automatically via the configured workflow:
```bash
python app.py
```

On startup:
- Telegram bot handlers are initialized (if TELEGRAM_TOKEN is set)
- Telegram bot starts polling in background thread
- Flask server starts on port 5000
- Displays: **"Server Alive ✅"**

Access web UI: `http://0.0.0.0:5000`

## Security Best Practices

1. ✅ API keys stored in environment variables (not hardcoded)
2. ✅ Binance API key should have withdrawal permissions DISABLED
3. ✅ Input validation prevents invalid trades
4. ✅ Silent retry mechanism for API errors
5. ✅ No sensitive data logged or exposed
6. ✅ Thread-safe state management with locks

## How to Use

1. **Set up environment variables:**
   - Add `BINANCE_API_KEY` and `BINANCE_SECRET_KEY` to Replit Secrets
   - Add `TELEGRAM_TOKEN` from BotFather
   - (Optional) Add `TELEGRAM_CHAT_ID` for automatic chat targeting

2. **Start the bot:**
   - Bot automatically starts when you run the Repl
   - Flask server runs on port 5000
   - Telegram bot polls for messages in background

3. **Start trading:**
   - Open your Telegram bot
   - Send `/help` to see commands
   - Send `/trade BTCUSDT 20 0.2` to start a trade
   - Use `/status` to check progress

4. **Monitor:**
   - All updates sent via Telegram
   - Web UI shows server status only
   - Console logs show monitoring activity

## Recent Changes

**November 11, 2025:**
- Refactored bot to use Telegram-only interface
- Removed web form for trading
- Web UI now only shows "Server Alive ✅" and IP address
- Added Telegram bot command handlers (/start, /help, /trade, /status)
- Implemented threading.Lock for thread-safe trade state management
- Telegram bot runs in separate thread alongside Flask
- Updated documentation to reflect new architecture

## Notes

- This is a development server; for production use a WSGI server like Gunicorn
- The bot monitors prices every 5 seconds to balance responsiveness with API rate limits
- All Telegram messages use HTML formatting for better readability
- Telegram bot handlers are only initialized if TELEGRAM_TOKEN is set
- Flask server always runs, even if Telegram token is missing (shows web status only)
