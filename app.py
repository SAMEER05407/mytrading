
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
    'profit_target': None,
    'stop_loss': None,
    'asset': None
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

def get_asset_balance(asset):
    """Get actual balance of an asset from Binance"""
    try:
        balance = binance_client.get_asset_balance(asset=asset)
        return float(balance['free']) + float(balance['locked'])
    except Exception as e:
        print(f"Error getting balance for {asset}: {e}")
        return 0.0

def validate_trade_inputs(pair, amount, profit, stop_loss):
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
    
    if stop_loss is not None:
        if stop_loss <= 0:
            errors.append("Stop loss must be > 0")
        
        if stop_loss >= amount:
            errors.append("Stop loss must be less than investment amount")
    
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

def calculate_pnl(pair, buy_price, current_balance):
    """Calculate actual P&L based on current balance and price"""
    try:
        ticker = binance_client.get_symbol_ticker(symbol=pair)
        current_price = float(ticker['price'])
        
        current_value = current_balance * current_price
        invested_value = current_balance * buy_price
        
        pnl = current_value - invested_value
        
        return {
            'current_price': current_price,
            'current_value': current_value,
            'pnl': pnl
        }
    except Exception as e:
        print(f"Error calculating P&L: {e}")
        return None

def monitor_trade():
    """Background thread to monitor price, check balance, and execute sell when profit target is reached or stop-loss triggered"""
    global active_trade
    
    pair = active_trade['pair']
    buy_price = active_trade['buy_price']
    quantity = active_trade['quantity']
    profit_target = active_trade['profit_target']
    stop_loss = active_trade['stop_loss']
    asset = active_trade['asset']
    
    print(f"🔍 Monitoring {pair} - Buy: ${buy_price:.8f}, Target: ${profit_target}, Stop Loss: ${stop_loss}")
    print(f"⏱️ Checking every 2 seconds for immediate execution")
    
    consecutive_errors = 0
    max_errors = 5
    last_balance_check = 0
    balance_check_interval = 10  # Check balance every 10 seconds to detect external sells
    
    while active_trade['running']:
        try:
            current_time = time.time()
            
            # Check balance periodically to detect external sells
            if current_time - last_balance_check >= balance_check_interval:
                current_balance = get_asset_balance(asset)
                last_balance_check = current_time
                
                # If balance is zero or very small, position was sold manually
                if current_balance < (quantity * 0.01):  # Less than 1% of original quantity
                    print(f"⚠️ Position closed externally. Balance: {current_balance}")
                    
                    message = f"⚠️ <b>Trade Closed Externally</b>\n\n"
                    message += f"Detected that {pair} position was sold outside the bot.\n"
                    message += f"Original quantity: {quantity:.8f}\n"
                    message += f"Current balance: {current_balance:.8f}\n\n"
                    message += f"Trade monitoring stopped."
                    
                    send_telegram(message)
                    
                    with trade_lock:
                        active_trade['running'] = False
                        active_trade['pair'] = None
                        active_trade['buy_price'] = None
                        active_trade['quantity'] = None
                        active_trade['profit_target'] = None
                        active_trade['stop_loss'] = None
                        active_trade['asset'] = None
                    break
            else:
                # Use last known balance for P&L calculation
                current_balance = get_asset_balance(asset)
            
            # Calculate actual P&L based on current balance and real-time price
            pnl_data = calculate_pnl(pair, buy_price, current_balance)
            
            if not pnl_data:
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    error_msg = f"⚠️ Failed to fetch price data {max_errors} times. Stopping monitoring."
                    send_telegram(error_msg)
                    with trade_lock:
                        active_trade['running'] = False
                    break
                time.sleep(2)
                continue
            
            consecutive_errors = 0  # Reset on success
            
            current_price = pnl_data['current_price']
            current_pnl = pnl_data['pnl']
            
            print(f"📊 Balance: {current_balance:.8f} {asset} | Price: ${current_price:.8f} | P&L: ${current_pnl:.4f} | Target: ${profit_target:.4f}")
            
            # Check if profit target reached - IMMEDIATE SELL
            if current_pnl >= profit_target:
                print(f"✅ PROFIT TARGET REACHED! Executing immediate sell...")
                print(f"📈 P&L: ${current_pnl:.4f} >= Target: ${profit_target:.4f}")
                
                # Get fresh balance before selling
                final_balance = get_asset_balance(asset)
                
                sell_result = execute_sell_order(pair, final_balance)
                
                if sell_result['success']:
                    sell_price = sell_result['price']
                    actual_profit = (sell_price - buy_price) * final_balance
                    
                    message = f"💰 <b>PROFIT TARGET HIT!</b>\n\n"
                    message += f"Pair: {pair}\n"
                    message += f"Buy Price: ${buy_price:.8f}\n"
                    message += f"Sell Price: ${sell_price:.8f}\n"
                    message += f"Quantity Sold: {final_balance:.8f}\n"
                    message += f"Actual Profit: ${actual_profit:.4f}\n\n"
                    message += f"✅ Trade completed successfully!"
                    
                    send_telegram(message)
                    print(f"✅ Sell executed at ${sell_price:.8f}, Profit: ${actual_profit:.4f}")
                else:
                    error_msg = f"⚠️ Sell order failed: {sell_result['error']}"
                    send_telegram(error_msg)
                    print(f"❌ Sell failed: {sell_result['error']}")
                
                with trade_lock:
                    active_trade['running'] = False
                    active_trade['pair'] = None
                    active_trade['buy_price'] = None
                    active_trade['quantity'] = None
                    active_trade['profit_target'] = None
                    active_trade['stop_loss'] = None
                    active_trade['asset'] = None
                break
            
            # Check if stop-loss triggered - IMMEDIATE SELL (only if stop_loss is set)
            elif stop_loss is not None and current_pnl <= -stop_loss:
                print(f"🛑 STOP LOSS TRIGGERED! Executing immediate sell...")
                print(f"📉 Loss: ${abs(current_pnl):.4f} >= Stop Loss: ${stop_loss:.4f}")
                
                # Get fresh balance before selling
                final_balance = get_asset_balance(asset)
                
                sell_result = execute_sell_order(pair, final_balance)
                
                if sell_result['success']:
                    sell_price = sell_result['price']
                    actual_loss = (sell_price - buy_price) * final_balance
                    
                    message = f"🛑 <b>STOP LOSS TRIGGERED!</b>\n\n"
                    message += f"Pair: {pair}\n"
                    message += f"Buy Price: ${buy_price:.8f}\n"
                    message += f"Sell Price: ${sell_price:.8f}\n"
                    message += f"Quantity Sold: {final_balance:.8f}\n"
                    message += f"Actual Loss: ${actual_loss:.4f}\n\n"
                    message += f"Trade closed to prevent further losses."
                    
                    send_telegram(message)
                    print(f"🛑 Stop loss sell executed at ${sell_price:.8f}, Loss: ${actual_loss:.4f}")
                else:
                    error_msg = f"⚠️ Stop-loss sell order failed: {sell_result['error']}"
                    send_telegram(error_msg)
                    print(f"❌ Stop loss sell failed: {sell_result['error']}")
                
                with trade_lock:
                    active_trade['running'] = False
                    active_trade['pair'] = None
                    active_trade['buy_price'] = None
                    active_trade['quantity'] = None
                    active_trade['profit_target'] = None
                    active_trade['stop_loss'] = None
                    active_trade['asset'] = None
                break
            
            # Sleep for 2 seconds before next check
            time.sleep(2)
            
        except Exception as e:
            print(f"⚠️ Monitoring error: {e}")
            consecutive_errors += 1
            if consecutive_errors >= max_errors:
                error_msg = f"⚠️ Critical monitoring error. Stopping trade monitoring."
                send_telegram(error_msg)
                with trade_lock:
                    active_trade['running'] = False
                break
            time.sleep(2)

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

/trade &lt;pair&gt; &lt;amount&gt; &lt;profit&gt; [stop_loss]
Start a new trade with optional stop-loss

<b>Examples:</b>
• /trade BTCUSDT 20 0.5 (without stop loss)
• /trade BTCUSDT 20 0.5 0.3 (with stop loss)

<b>Parameters:</b>
• pair: Trading pair (must end with USDT)
• amount: Investment in USD (minimum $5)
• profit: Profit target in USD (must be > 0)
• stop_loss: (Optional) Maximum loss in USD

/status
Check current trade with real-time P&L

/help
Show this message

<b>Features:</b>
✅ Real balance & P&L checking
✅ Auto-sell on profit target
✅ Optional stop-loss protection
✅ Detects external position closure
✅ Only one trade at a time
"""
        send_telegram(help_text, message.chat.id)

    @telegram_bot.message_handler(commands=['status'])
    def check_status(message):
        """Check active trade status with real balance"""
        with trade_lock:
            if active_trade['running']:
                pair = active_trade['pair']
                buy_price = active_trade['buy_price']
                profit_target = active_trade['profit_target']
                stop_loss = active_trade['stop_loss']
                asset = active_trade['asset']
                
                try:
                    current_balance = get_asset_balance(asset)
                    
                    if current_balance < (active_trade['quantity'] * 0.01):
                        status_msg = "⚠️ Position appears to be closed externally. Bot will stop monitoring shortly."
                        send_telegram(status_msg, message.chat.id)
                        return
                    
                    pnl_data = calculate_pnl(pair, buy_price, current_balance)
                    
                    if pnl_data:
                        current_price = pnl_data['current_price']
                        current_pnl = pnl_data['pnl']
                        
                        status_msg = f"📊 <b>Active Trade Status</b>\n\n"
                        status_msg += f"Pair: {pair}\n"
                        status_msg += f"Buy Price: ${buy_price:.8f}\n"
                        status_msg += f"Current Price: ${current_price:.8f}\n"
                        status_msg += f"Balance: {current_balance:.8f} {asset}\n\n"
                        status_msg += f"<b>P&L: ${current_pnl:.4f}</b>\n"
                        status_msg += f"Target Profit: ${profit_target:.4f}\n"
                        if stop_loss is not None:
                            status_msg += f"Stop Loss: ${stop_loss:.4f}\n\n"
                        else:
                            status_msg += f"Stop Loss: Not Set\n\n"
                        
                        if current_pnl > 0:
                            profit_percent = (current_pnl / profit_target) * 100
                            status_msg += f"Progress: {profit_percent:.1f}% to target 📈"
                        else:
                            if stop_loss is not None:
                                loss_percent = (abs(current_pnl) / stop_loss) * 100
                                status_msg += f"Loss: {loss_percent:.1f}% of stop-loss 📉"
                            else:
                                status_msg += f"Current Loss: ${abs(current_pnl):.4f} (No stop-loss) ⚠️"
                        
                        send_telegram(status_msg, message.chat.id)
                    else:
                        send_telegram("⚠️ Error fetching current data", message.chat.id)
                except Exception as e:
                    send_telegram(f"⚠️ Error: {e}", message.chat.id)
            else:
                send_telegram("✅ No active trade. Use /trade to start one.", message.chat.id)

    @telegram_bot.message_handler(commands=['trade'])
    def start_trade_command(message):
        """Handle /trade command with optional stop-loss"""
        
        if not binance_client:
            send_telegram("⚠️ Binance API keys not configured.", message.chat.id)
            return
        
        try:
            parts = message.text.split()
            
            if len(parts) < 4 or len(parts) > 5:
                error_msg = "⚠️ <b>Invalid format!</b>\n\n"
                error_msg += "<b>Usage:</b>\n/trade &lt;pair&gt; &lt;amount&gt; &lt;profit&gt; [stop_loss]\n\n"
                error_msg += "<b>Examples:</b>\n"
                error_msg += "• /trade BTCUSDT 20 0.5 (without stop loss)\n"
                error_msg += "• /trade BTCUSDT 20 0.5 0.3 (with stop loss)"
                send_telegram(error_msg, message.chat.id)
                return
            
            pair = parts[1].upper().strip()
            amount = float(parts[2])
            profit_target = float(parts[3])
            stop_loss = float(parts[4]) if len(parts) == 5 else None
            
        except (ValueError, IndexError):
            error_msg = "⚠️ <b>Invalid values!</b>\n\n"
            error_msg += "<b>Examples:</b>\n"
            error_msg += "• /trade BTCUSDT 20 0.5\n"
            error_msg += "• /trade BTCUSDT 20 0.5 0.3"
            send_telegram(error_msg, message.chat.id)
            return
        
        errors = validate_trade_inputs(pair, amount, profit_target, stop_loss)
        if errors:
            error_msg = f"⚠️ <b>Validation errors:</b>\n\n"
            error_msg += "\n".join(f"• {e}" for e in errors)
            send_telegram(error_msg, message.chat.id)
            return
        
        with trade_lock:
            if active_trade['running']:
                send_telegram("🚫 Trade already running! Wait for it to finish or sell manually.", message.chat.id)
                return
            
            active_trade['running'] = True
        
        buy_result = execute_buy_order(pair, amount)
        
        if not buy_result['success']:
            error_msg = f"⚠️ Buy order failed: {buy_result['error']}"
            send_telegram(error_msg, message.chat.id)
            
            with trade_lock:
                active_trade['running'] = False
            return
        
        asset = pair.replace('USDT', '')
        
        with trade_lock:
            active_trade['pair'] = pair
            active_trade['buy_price'] = buy_result['price']
            active_trade['quantity'] = buy_result['quantity']
            active_trade['profit_target'] = profit_target
            active_trade['stop_loss'] = stop_loss
            active_trade['asset'] = asset
        
        success_msg = f"✅ <b>Trade Started</b>\n\n"
        success_msg += f"Pair: {pair}\n"
        success_msg += f"Buy Price: ${buy_result['price']:.8f}\n"
        success_msg += f"Quantity: {buy_result['quantity']:.8f} {asset}\n"
        success_msg += f"Investment: ${amount:.2f}\n\n"
        success_msg += f"Target Profit: ${profit_target:.4f} 🎯\n"
        if stop_loss is not None:
            success_msg += f"Stop Loss: ${stop_loss:.4f} 🛑\n\n"
        else:
            success_msg += f"Stop Loss: Not Set ⚠️\n\n"
        success_msg += f"Monitoring with balance verification..."
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
