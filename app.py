from flask import Flask, render_template
from binance.client import Client
from binance.exceptions import BinanceAPIException
import telebot
import threading
import time
import os
import requests

app = Flask(__name__)

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY', '')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

binance_client = None
telegram_bot = None

if BINANCE_API_KEY and BINANCE_SECRET_KEY:
    binance_client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

if TELEGRAM_TOKEN:
    telegram_bot = telebot.TeleBot(TELEGRAM_TOKEN)

active_trade = {
    'running': False,
    'pair': None,
    'buy_price': None,
    'quantity': None,
    'profit_target': None
}

trade_lock = threading.Lock()

def send_telegram(message, chat_id=None):
    """Send Telegram message safely"""
    if telegram_bot:
        target_chat = chat_id or TELEGRAM_CHAT_ID
        if target_chat:
            try:
                telegram_bot.send_message(target_chat, message, parse_mode='HTML')
            except Exception as e:
                print(f"Telegram error: {e}")

def get_server_ip():
    """Get public IP address"""
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=5)
        return response.json()['ip']
    except:
        return 'Unable to fetch IP'

def validate_trade_inputs(pair, amount, profit):
    """Validate trading inputs"""
    errors = []
    
    if not pair.endswith('USDT'):
        errors.append("Pair must end with USDT")
    
    if binance_client:
        try:
            info = binance_client.get_symbol_info(pair)
            if not info:
                errors.append(f"Pair {pair} not found on Binance")
        except BinanceAPIException:
            errors.append(f"Pair {pair} not found on Binance")
    
    if amount < 5:
        errors.append("Amount must be ≥ $5")
    
    if profit <= 0:
        errors.append("Profit must be > 0")
    
    return errors

def execute_buy_order(pair, amount_usd):
    """Execute market buy order"""
    try:
        ticker = binance_client.get_symbol_ticker(symbol=pair)
        current_price = float(ticker['price'])
        
        quantity = amount_usd / current_price
        
        info = binance_client.get_symbol_info(pair)
        step_size = 0.0
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size = float(f['stepSize'])
                break
        
        if step_size > 0:
            precision = len(str(step_size).rstrip('0').split('.')[-1])
            quantity = round(quantity, precision)
        
        order = binance_client.order_market_buy(
            symbol=pair,
            quantity=quantity
        )
        
        fills = order.get('fills', [])
        total_qty = 0
        total_cost = 0
        for fill in fills:
            total_qty += float(fill['qty'])
            total_cost += float(fill['price']) * float(fill['qty'])
        
        avg_price = total_cost / total_qty if total_qty > 0 else current_price
        
        return {
            'success': True,
            'price': avg_price,
            'quantity': total_qty,
            'order': order
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def execute_sell_order(pair, quantity):
    """Execute market sell order"""
    try:
        order = binance_client.order_market_sell(
            symbol=pair,
            quantity=quantity
        )
        
        fills = order.get('fills', [])
        total_qty = 0
        total_cost = 0
        for fill in fills:
            total_qty += float(fill['qty'])
            total_cost += float(fill['price']) * float(fill['qty'])
        
        avg_price = total_cost / total_qty if total_qty > 0 else 0
        
        return {
            'success': True,
            'price': avg_price,
            'order': order
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def monitor_trade():
    """Background thread to monitor price and execute sell when profit target is reached"""
    global active_trade
    
    pair = active_trade['pair']
    buy_price = active_trade['buy_price']
    quantity = active_trade['quantity']
    profit_target = active_trade['profit_target']
    
    print(f"Monitoring {pair} - Buy: ${buy_price:.8f}, Target Profit: ${profit_target}")
    
    while active_trade['running']:
        try:
            ticker = binance_client.get_symbol_ticker(symbol=pair)
            current_price = float(ticker['price'])
            
            current_profit = (current_price - buy_price) * quantity
            
            print(f"Current Price: ${current_price:.8f}, Profit: ${current_profit:.4f}")
            
            if current_profit >= profit_target:
                print(f"Profit target reached! Selling...")
                
                sell_result = execute_sell_order(pair, quantity)
                
                if sell_result['success']:
                    sell_price = sell_result['price']
                    actual_profit = (sell_price - buy_price) * quantity
                    
                    message = f"💰 <b>Profit Target Reached!</b>\n"
                    message += f"Sold at ${sell_price:.8f}\n"
                    message += f"Profit: ${actual_profit:.4f}\n"
                    message += f"Trade Complete. Ready for next one."
                    
                    send_telegram(message)
                    print(message.replace('<b>', '').replace('</b>', ''))
                else:
                    error_msg = f"⚠️ Sell order failed: {sell_result['error']}"
                    send_telegram(error_msg)
                    print(error_msg)
                
                with trade_lock:
                    active_trade['running'] = False
                    active_trade['pair'] = None
                    active_trade['buy_price'] = None
                    active_trade['quantity'] = None
                    active_trade['profit_target'] = None
                break
            
            time.sleep(5)
            
        except Exception as e:
            print(f"Monitoring error: {e}")
            time.sleep(5)

@app.route('/')
def index():
    """Render status page"""
    server_ip = get_server_ip()
    return render_template('index.html', server_ip=server_ip)

def setup_telegram_handlers():
    """Setup Telegram bot handlers"""
    if not telegram_bot:
        return
    
    @telegram_bot.message_handler(commands=['start', 'help'])
    def send_welcome(message):
        """Welcome message and help"""
        help_text = """
🤖 <b>Binance Auto Trading Bot</b>

<b>Commands:</b>

/trade &lt;pair&gt; &lt;amount&gt; &lt;profit&gt;
Start a new trade

Example: /trade BTCUSDT 20 0.2

• pair: Trading pair (must end with USDT)
• amount: Amount in USD (minimum $5)
• profit: Profit target in USD (must be > 0)

/status
Check current trade status

/help
Show this message

<b>Rules:</b>
✅ Only one trade at a time
✅ Spot trading only (no futures)
✅ Bot monitors price every 5 seconds
✅ Auto-sells when profit target reached
"""
        send_telegram(help_text, message.chat.id)

    @telegram_bot.message_handler(commands=['status'])
    def check_status(message):
        """Check active trade status"""
        with trade_lock:
            if active_trade['running']:
                pair = active_trade['pair']
                buy_price = active_trade['buy_price']
                quantity = active_trade['quantity']
                profit_target = active_trade['profit_target']
                
                try:
                    ticker = binance_client.get_symbol_ticker(symbol=pair)
                    current_price = float(ticker['price'])
                    current_profit = (current_price - buy_price) * quantity
                    
                    status_msg = f"📊 <b>Active Trade</b>\n\n"
                    status_msg += f"Pair: {pair}\n"
                    status_msg += f"Buy Price: ${buy_price:.8f}\n"
                    status_msg += f"Current Price: ${current_price:.8f}\n"
                    status_msg += f"Quantity: {quantity:.8f}\n"
                    status_msg += f"Current Profit: ${current_profit:.4f}\n"
                    status_msg += f"Target Profit: ${profit_target:.4f}\n"
                    
                    percent = (current_profit / profit_target) * 100
                    status_msg += f"Progress: {percent:.1f}%"
                    
                    send_telegram(status_msg, message.chat.id)
                except Exception as e:
                    send_telegram(f"⚠️ Error fetching status: {e}", message.chat.id)
            else:
                send_telegram("✅ No active trade. Use /trade to start one.", message.chat.id)

    @telegram_bot.message_handler(commands=['trade'])
    def start_trade_command(message):
        """Handle /trade command"""
        
        if not binance_client:
            send_telegram("⚠️ Binance API keys not configured. Please set environment variables.", message.chat.id)
            return
        
        try:
            parts = message.text.split()
            
            if len(parts) != 4:
                error_msg = "⚠️ <b>Invalid command format!</b>\n\n"
                error_msg += "<b>Usage:</b>\n/trade &lt;pair&gt; &lt;amount&gt; &lt;profit&gt;\n\n"
                error_msg += "<b>Example:</b>\n/trade BTCUSDT 20 0.2"
                send_telegram(error_msg, message.chat.id)
                return
            
            pair = parts[1].upper().strip()
            amount = float(parts[2])
            profit_target = float(parts[3])
            
        except (ValueError, IndexError):
            error_msg = "⚠️ <b>Invalid values!</b>\n\n"
            error_msg += "Make sure amount and profit are numbers.\n\n"
            error_msg += "<b>Example:</b>\n/trade BTCUSDT 20 0.2"
            send_telegram(error_msg, message.chat.id)
            return
        
        errors = validate_trade_inputs(pair, amount, profit_target)
        if errors:
            error_msg = f"⚠️ <b>Invalid command.</b> Example:\n"
            error_msg += f"Pair: BTCUSDT\n"
            error_msg += f"Amount: 20\n"
            error_msg += f"Profit: 0.2\n\n"
            error_msg += f"<b>Errors:</b>\n" + "\n".join(f"• {e}" for e in errors)
            send_telegram(error_msg, message.chat.id)
            return
        
        with trade_lock:
            if active_trade['running']:
                send_telegram("🚫 Trade already running! Wait for it to finish.", message.chat.id)
                return
            
            active_trade['running'] = True
        
        buy_result = execute_buy_order(pair, amount)
        
        if not buy_result['success']:
            error_msg = f"⚠️ Buy order failed: {buy_result['error']}"
            send_telegram(error_msg, message.chat.id)
            
            with trade_lock:
                active_trade['running'] = False
            return
        
        with trade_lock:
            active_trade['pair'] = pair
            active_trade['buy_price'] = buy_result['price']
            active_trade['quantity'] = buy_result['quantity']
            active_trade['profit_target'] = profit_target
        
        success_msg = f"✅ <b>Bought {pair}</b>\n"
        success_msg += f"Price: ${buy_result['price']:.8f}\n"
        success_msg += f"Amount: ${amount:.2f}\n"
        success_msg += f"Target Profit: ${profit_target:.4f}\n\n"
        success_msg += f"Monitoring price every 5 seconds..."
        send_telegram(success_msg, message.chat.id)
        
        monitor_thread = threading.Thread(target=monitor_trade, daemon=True)
        monitor_thread.start()

def run_telegram_bot():
    """Run Telegram bot polling in separate thread"""
    if telegram_bot:
        print("Telegram bot started ✅")
        try:
            telegram_bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Telegram bot error: {e}")

def run_flask_app():
    """Run Flask app"""
    print("Server Alive ✅")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)

if __name__ == '__main__':
    if TELEGRAM_TOKEN:
        setup_telegram_handlers()
        bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
        bot_thread.start()
    else:
        print("⚠️ TELEGRAM_TOKEN not set. Telegram bot disabled.")
    
    run_flask_app()
