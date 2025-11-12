

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
    'asset': None,
    'trade_type': 'spot'  # 'spot' or 'futures'
}

active_futures_trade = {
    'running': False,
    'pair': None,
    'entry_price': None,
    'quantity': None,
    'profit_target': None,
    'stop_loss': None,
    'side': None,  # 'LONG' or 'SHORT'
    'leverage': 1,
    'position_amt': None
}

trade_lock = threading.Lock()
futures_lock = threading.Lock()

def send_telegram(message, chat_id=None):
    """Send Telegram message safely"""
    if not telegram_bot:
        print(f"❌ Telegram bot not initialized - TELEGRAM_TOKEN missing or invalid")
        return False
        
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not target_chat:
        print(f"❌ No chat ID provided - TELEGRAM_CHAT_ID not set")
        return False
        
    try:
        result = telegram_bot.send_message(target_chat, message, parse_mode='HTML')
        print(f"✅ Telegram message sent successfully to chat {target_chat}")
        print(f"   Message ID: {result.message_id}")
        return True
    except Exception as e:
        print(f"❌ Telegram send failed: {type(e).__name__}: {e}")
        print(f"   Target Chat: {target_chat}")
        print(f"   Token configured: {'Yes' if TELEGRAM_TOKEN else 'No'}")
        if hasattr(e, 'result'):
            print(f"   API Response: {e.result}")
        return False

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

def get_futures_balance():
    """Get USDT balance in Futures wallet"""
    try:
        account = binance_client.futures_account()
        for asset in account['assets']:
            if asset['asset'] == 'USDT':
                return float(asset['availableBalance'])
        return 0.0
    except Exception as e:
        print(f"Error getting futures balance: {e}")
        return 0.0

def transfer_spot_to_futures(amount):
    """Transfer USDT from Spot to Futures wallet"""
    try:
        result = binance_client.futures_account_transfer(
            asset='USDT',
            amount=amount,
            type=1  # 1 = Spot to Futures, 2 = Futures to Spot
        )
        return {'success': True, 'result': result}
    except Exception as e:
        return {'success': False, 'error': str(e)}

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

def validate_futures_inputs(pair, amount, profit, stop_loss, leverage):
    """Validate futures trading inputs"""
    errors = []
    
    if not pair.endswith('USDT'):
        errors.append("Pair must end with USDT")
    
    if amount < 5:
        errors.append("Amount must be ≥ $5")
    
    if profit <= 0:
        errors.append("Profit must be > 0")
    
    if stop_loss is not None and stop_loss <= 0:
        errors.append("Stop loss must be > 0")
    
    if leverage < 1 or leverage > 20:
        errors.append("Leverage must be between 1 and 20")
    
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
    """Execute market sell order with proper quantity formatting"""
    try:
        info = binance_client.get_symbol_info(pair)
        step_size = 0.0
        min_qty = 0.0
        
        for f in info['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step_size = float(f['stepSize'])
                min_qty = float(f['minQty'])
                break
        
        if step_size > 0:
            precision = len(str(step_size).rstrip('0').split('.')[-1])
            quantity = float(quantity)
            
            quantity = (quantity // step_size) * step_size
            quantity = round(quantity, precision)
        
        if quantity < min_qty:
            return {
                'success': False,
                'error': f'Quantity {quantity} is below minimum {min_qty}'
            }
        
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

def execute_futures_order(pair, side, amount_usd, leverage):
    """Execute futures market order (LONG or SHORT)"""
    try:
        # Set leverage
        binance_client.futures_change_leverage(symbol=pair, leverage=leverage)
        
        # Get current price
        ticker = binance_client.futures_symbol_ticker(symbol=pair)
        current_price = float(ticker['price'])
        
        # Calculate quantity
        quantity = (amount_usd * leverage) / current_price
        
        # Get precision
        info = binance_client.futures_exchange_info()
        precision = 3
        step_size = 0.0
        for s in info['symbols']:
            if s['symbol'] == pair:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        precision = len(str(step_size).rstrip('0').split('.')[-1])
                        break
                break
        
        quantity = round(quantity, precision)
        
        # Place order
        order = binance_client.futures_create_order(
            symbol=pair,
            side='BUY' if side == 'LONG' else 'SELL',
            type='MARKET',
            quantity=quantity
        )
        
        return {
            'success': True,
            'price': current_price,
            'quantity': quantity,
            'order': order
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def close_futures_position(pair):
    """Close futures position"""
    try:
        # Get current position
        positions = binance_client.futures_position_information(symbol=pair)
        position_amt = 0.0
        
        for pos in positions:
            if pos['symbol'] == pair:
                position_amt = float(pos['positionAmt'])
                break
        
        if position_amt == 0:
            return {'success': False, 'error': 'No open position'}
        
        # Close position
        side = 'SELL' if position_amt > 0 else 'BUY'
        quantity = abs(position_amt)
        
        order = binance_client.futures_create_order(
            symbol=pair,
            side=side,
            type='MARKET',
            quantity=quantity
        )
        
        # Get exit price from order
        exit_price = 0.0
        if 'avgPrice' in order:
            exit_price = float(order['avgPrice'])
        else:
            ticker = binance_client.futures_symbol_ticker(symbol=pair)
            exit_price = float(ticker['price'])
        
        return {
            'success': True,
            'price': exit_price,
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

def calculate_futures_pnl(pair, entry_price, side, quantity):
    """Calculate futures P&L"""
    try:
        ticker = binance_client.futures_symbol_ticker(symbol=pair)
        current_price = float(ticker['price'])
        
        if side == 'LONG':
            pnl = (current_price - entry_price) * quantity
        else:  # SHORT
            pnl = (entry_price - current_price) * quantity
        
        return {
            'current_price': current_price,
            'pnl': pnl
        }
    except Exception as e:
        print(f"Error calculating futures P&L: {e}")
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
    balance_check_interval = 10
    
    while active_trade['running']:
        try:
            current_time = time.time()
            
            if current_time - last_balance_check >= balance_check_interval:
                current_balance = get_asset_balance(asset)
                last_balance_check = current_time
                
                if current_balance < (quantity * 0.01):
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
                current_balance = get_asset_balance(asset)
            
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
            
            consecutive_errors = 0
            
            current_price = pnl_data['current_price']
            current_pnl = pnl_data['pnl']
            
            print(f"📊 Balance: {current_balance:.8f} {asset} | Price: ${current_price:.8f} | P&L: ${current_pnl:.4f} | Target: ${profit_target:.4f}")
            
            if current_pnl >= profit_target:
                print(f"✅ PROFIT TARGET REACHED! Executing immediate sell...")
                print(f"📈 P&L: ${current_pnl:.4f} >= Target: ${profit_target:.4f}")
                
                final_balance = get_asset_balance(asset)
                
                exec_msg = f"⏳ <b>EXECUTING SELL ORDER...</b>\n\n"
                exec_msg += f"Profit Target Reached!\n"
                exec_msg += f"Current P&L: ${current_pnl:.4f}\n"
                exec_msg += f"Selling {final_balance:.8f} {asset}..."
                send_telegram(exec_msg)
                print(f"📤 Telegram notification sent: Executing sell order")
                
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
                    print(f"📤 Telegram notification sent: Profit confirmation")
                else:
                    error_msg = f"⚠️ <b>SELL ORDER FAILED!</b>\n\n"
                    error_msg += f"Error: {sell_result['error']}\n"
                    error_msg += f"Pair: {pair}\n"
                    error_msg += f"Attempted Quantity: {final_balance:.8f}"
                    send_telegram(error_msg)
                    print(f"❌ Sell failed: {sell_result['error']}")
                    print(f"📤 Telegram notification sent: Sell error")
                
                with trade_lock:
                    active_trade['running'] = False
                    active_trade['pair'] = None
                    active_trade['buy_price'] = None
                    active_trade['quantity'] = None
                    active_trade['profit_target'] = None
                    active_trade['stop_loss'] = None
                    active_trade['asset'] = None
                break
            
            elif stop_loss is not None and current_pnl <= -stop_loss:
                print(f"🛑 STOP LOSS TRIGGERED! Executing immediate sell...")
                print(f"📉 Loss: ${abs(current_pnl):.4f} >= Stop Loss: ${stop_loss:.4f}")
                
                final_balance = get_asset_balance(asset)
                
                exec_msg = f"⏳ <b>EXECUTING STOP-LOSS SELL...</b>\n\n"
                exec_msg += f"Stop Loss Triggered!\n"
                exec_msg += f"Current Loss: ${abs(current_pnl):.4f}\n"
                exec_msg += f"Selling {final_balance:.8f} {asset}..."
                send_telegram(exec_msg)
                print(f"📤 Telegram notification sent: Executing stop-loss sell")
                
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
                    print(f"📤 Telegram notification sent: Stop-loss confirmation")
                else:
                    error_msg = f"⚠️ <b>STOP-LOSS SELL FAILED!</b>\n\n"
                    error_msg += f"Error: {sell_result['error']}\n"
                    error_msg += f"Pair: {pair}\n"
                    error_msg += f"Attempted Quantity: {final_balance:.8f}"
                    send_telegram(error_msg)
                    print(f"❌ Stop loss sell failed: {sell_result['error']}")
                    print(f"📤 Telegram notification sent: Stop-loss error")
                
                with trade_lock:
                    active_trade['running'] = False
                    active_trade['pair'] = None
                    active_trade['buy_price'] = None
                    active_trade['quantity'] = None
                    active_trade['profit_target'] = None
                    active_trade['stop_loss'] = None
                    active_trade['asset'] = None
                break
            
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

def monitor_futures_trade():
    """Monitor futures position"""
    global active_futures_trade
    
    pair = active_futures_trade['pair']
    entry_price = active_futures_trade['entry_price']
    quantity = active_futures_trade['quantity']
    profit_target = active_futures_trade['profit_target']
    stop_loss = active_futures_trade['stop_loss']
    side = active_futures_trade['side']
    
    print(f"🔍 Monitoring Futures {pair} {side} - Entry: ${entry_price:.8f}, Target: ${profit_target}, Stop Loss: ${stop_loss}")
    
    consecutive_errors = 0
    max_errors = 5
    
    while active_futures_trade['running']:
        try:
            pnl_data = calculate_futures_pnl(pair, entry_price, side, quantity)
            
            if not pnl_data:
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    error_msg = f"⚠️ Failed to fetch futures data. Stopping monitoring."
                    send_telegram(error_msg)
                    with futures_lock:
                        active_futures_trade['running'] = False
                    break
                time.sleep(2)
                continue
            
            consecutive_errors = 0
            current_price = pnl_data['current_price']
            current_pnl = pnl_data['pnl']
            
            print(f"📊 Futures {side} | Price: ${current_price:.8f} | P&L: ${current_pnl:.4f} | Target: ${profit_target:.4f}")
            
            # Check profit target
            if current_pnl >= profit_target:
                print(f"✅ FUTURES PROFIT TARGET REACHED!")
                
                exec_msg = f"⏳ <b>CLOSING FUTURES POSITION...</b>\n\n"
                exec_msg += f"Profit Target Reached!\n"
                exec_msg += f"Current P&L: ${current_pnl:.4f}"
                send_telegram(exec_msg)
                
                close_result = close_futures_position(pair)
                
                if close_result['success']:
                    exit_price = close_result['price']
                    
                    if side == 'LONG':
                        actual_profit = (exit_price - entry_price) * quantity
                    else:
                        actual_profit = (entry_price - exit_price) * quantity
                    
                    message = f"💰 <b>FUTURES PROFIT TARGET HIT!</b>\n\n"
                    message += f"Pair: {pair}\n"
                    message += f"Side: {side}\n"
                    message += f"Entry Price: ${entry_price:.8f}\n"
                    message += f"Exit Price: ${exit_price:.8f}\n"
                    message += f"Quantity: {quantity:.8f}\n"
                    message += f"Actual Profit: ${actual_profit:.4f}\n\n"
                    message += f"✅ Position closed successfully!"
                    
                    send_telegram(message)
                else:
                    error_msg = f"⚠️ Failed to close position: {close_result['error']}"
                    send_telegram(error_msg)
                
                with futures_lock:
                    active_futures_trade['running'] = False
                    active_futures_trade['pair'] = None
                    active_futures_trade['entry_price'] = None
                    active_futures_trade['quantity'] = None
                    active_futures_trade['profit_target'] = None
                    active_futures_trade['stop_loss'] = None
                    active_futures_trade['side'] = None
                    active_futures_trade['leverage'] = 1
                    active_futures_trade['position_amt'] = None
                break
            
            # Check stop loss
            elif stop_loss is not None and current_pnl <= -stop_loss:
                print(f"🛑 FUTURES STOP LOSS TRIGGERED!")
                
                exec_msg = f"⏳ <b>STOP-LOSS: CLOSING POSITION...</b>\n\n"
                exec_msg += f"Stop Loss Triggered!\n"
                exec_msg += f"Current Loss: ${abs(current_pnl):.4f}"
                send_telegram(exec_msg)
                
                close_result = close_futures_position(pair)
                
                if close_result['success']:
                    exit_price = close_result['price']
                    
                    if side == 'LONG':
                        actual_loss = (exit_price - entry_price) * quantity
                    else:
                        actual_loss = (entry_price - exit_price) * quantity
                    
                    message = f"🛑 <b>FUTURES STOP LOSS TRIGGERED!</b>\n\n"
                    message += f"Pair: {pair}\n"
                    message += f"Side: {side}\n"
                    message += f"Entry Price: ${entry_price:.8f}\n"
                    message += f"Exit Price: ${exit_price:.8f}\n"
                    message += f"Quantity: {quantity:.8f}\n"
                    message += f"Actual Loss: ${actual_loss:.4f}\n\n"
                    message += f"Position closed to prevent further losses."
                    
                    send_telegram(message)
                else:
                    error_msg = f"⚠️ Failed to close position: {close_result['error']}"
                    send_telegram(error_msg)
                
                with futures_lock:
                    active_futures_trade['running'] = False
                    active_futures_trade['pair'] = None
                    active_futures_trade['entry_price'] = None
                    active_futures_trade['quantity'] = None
                    active_futures_trade['profit_target'] = None
                    active_futures_trade['stop_loss'] = None
                    active_futures_trade['side'] = None
                    active_futures_trade['leverage'] = 1
                    active_futures_trade['position_amt'] = None
                break
            
            time.sleep(2)
            
        except Exception as e:
            print(f"⚠️ Futures monitoring error: {e}")
            consecutive_errors += 1
            if consecutive_errors >= max_errors:
                error_msg = f"⚠️ Critical futures monitoring error."
                send_telegram(error_msg)
                with futures_lock:
                    active_futures_trade['running'] = False
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

<b>SPOT Trading Commands:</b>

/trade &lt;pair&gt; &lt;amount&gt; &lt;profit&gt; [stop_loss]
Start a spot trade

<b>Example:</b>
• /trade BTCUSDT 20 0.5
• /trade BTCUSDT 20 0.5 0.3

<b>FUTURES Trading Commands:</b>

/futures &lt;pair&gt; &lt;side&gt; &lt;amount&gt; &lt;profit&gt; &lt;leverage&gt; [stop_loss]

<b>Examples:</b>
• /futures BTCUSDT LONG 20 2 10
• /futures BTCUSDT SHORT 20 2 10 1.5

<b>Parameters:</b>
• side: LONG (buy) or SHORT (sell)
• leverage: 1 to 20x
• Bot will auto-transfer from Spot to Futures

<b>Status Commands:</b>
/status - Check spot trade
/fstatus - Check futures trade

⚠️ <b>WARNING:</b> Futures trading is very risky!
Start with small amounts and low leverage.
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
                        
                        status_msg = f"📊 <b>Active Spot Trade Status</b>\n\n"
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
                send_telegram("✅ No active spot trade. Use /trade to start one.", message.chat.id)

    @telegram_bot.message_handler(commands=['fstatus'])
    def check_futures_status(message):
        """Check active futures trade status"""
        with futures_lock:
            if active_futures_trade['running']:
                pair = active_futures_trade['pair']
                entry_price = active_futures_trade['entry_price']
                quantity = active_futures_trade['quantity']
                profit_target = active_futures_trade['profit_target']
                stop_loss = active_futures_trade['stop_loss']
                side = active_futures_trade['side']
                leverage = active_futures_trade['leverage']
                
                try:
                    pnl_data = calculate_futures_pnl(pair, entry_price, side, quantity)
                    
                    if pnl_data:
                        current_price = pnl_data['current_price']
                        current_pnl = pnl_data['pnl']
                        
                        status_msg = f"📊 <b>Active Futures Trade Status</b>\n\n"
                        status_msg += f"Pair: {pair}\n"
                        status_msg += f"Side: {side}\n"
                        status_msg += f"Leverage: {leverage}x\n"
                        status_msg += f"Entry Price: ${entry_price:.8f}\n"
                        status_msg += f"Current Price: ${current_price:.8f}\n"
                        status_msg += f"Quantity: {quantity:.8f}\n\n"
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
                            status_msg += f"Current Loss: ${abs(current_pnl):.4f} 📉"
                        
                        send_telegram(status_msg, message.chat.id)
                    else:
                        send_telegram("⚠️ Error fetching futures data", message.chat.id)
                except Exception as e:
                    send_telegram(f"⚠️ Error: {e}", message.chat.id)
            else:
                send_telegram("✅ No active futures trade. Use /futures to start one.", message.chat.id)

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
                error_msg += "• /trade BTCUSDT 20 0.5\n"
                error_msg += "• /trade BTCUSDT 20 0.5 0.3"
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
                send_telegram("🚫 Spot trade already running!", message.chat.id)
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
            active_trade['trade_type'] = 'spot'
        
        success_msg = f"✅ <b>Spot Trade Started</b>\n\n"
        success_msg += f"Pair: {pair}\n"
        success_msg += f"Buy Price: ${buy_result['price']:.8f}\n"
        success_msg += f"Quantity: {buy_result['quantity']:.8f} {asset}\n"
        success_msg += f"Investment: ${amount:.2f}\n\n"
        success_msg += f"Target Profit: ${profit_target:.4f} 🎯\n"
        if stop_loss is not None:
            success_msg += f"Stop Loss: ${stop_loss:.4f} 🛑\n\n"
        else:
            success_msg += f"Stop Loss: Not Set ⚠️\n\n"
        success_msg += f"Monitoring started..."
        send_telegram(success_msg, message.chat.id)
        
        monitor_thread = threading.Thread(target=monitor_trade, daemon=True)
        monitor_thread.start()

    @telegram_bot.message_handler(commands=['futures'])
    def start_futures_trade(message):
        """Handle /futures command"""
        
        if not binance_client:
            send_telegram("⚠️ Binance API keys not configured.", message.chat.id)
            return
        
        try:
            parts = message.text.split()
            
            if len(parts) < 6 or len(parts) > 7:
                error_msg = "⚠️ <b>Invalid format!</b>\n\n"
                error_msg += "<b>Usage:</b>\n/futures &lt;pair&gt; &lt;side&gt; &lt;amount&gt; &lt;profit&gt; &lt;leverage&gt; [stop_loss]\n\n"
                error_msg += "<b>Examples:</b>\n"
                error_msg += "• /futures BTCUSDT LONG 20 2 10\n"
                error_msg += "• /futures BTCUSDT SHORT 20 2 10 1.5"
                send_telegram(error_msg, message.chat.id)
                return
            
            pair = parts[1].upper().strip()
            side = parts[2].upper().strip()
            amount = float(parts[3])
            profit_target = float(parts[4])
            leverage = int(parts[5])
            stop_loss = float(parts[6]) if len(parts) == 7 else None
            
            if side not in ['LONG', 'SHORT']:
                raise ValueError("Side must be LONG or SHORT")
            
        except (ValueError, IndexError) as e:
            error_msg = "⚠️ <b>Invalid values!</b>\n\n"
            error_msg += f"Error: {e}\n\n"
            error_msg += "<b>Examples:</b>\n"
            error_msg += "• /futures BTCUSDT LONG 20 2 10\n"
            error_msg += "• /futures BTCUSDT SHORT 20 2 5 1"
            send_telegram(error_msg, message.chat.id)
            return
        
        errors = validate_futures_inputs(pair, amount, profit_target, stop_loss, leverage)
        if errors:
            error_msg = f"⚠️ <b>Validation errors:</b>\n\n"
            error_msg += "\n".join(f"• {e}" for e in errors)
            send_telegram(error_msg, message.chat.id)
            return
        
        with futures_lock:
            if active_futures_trade['running']:
                send_telegram("🚫 Futures trade already running!", message.chat.id)
                return
            
            active_futures_trade['running'] = True
        
        # Check and transfer from Spot to Futures if needed
        futures_balance = get_futures_balance()
        if futures_balance < amount:
            spot_balance = get_asset_balance('USDT')
            needed = amount - futures_balance
            
            if spot_balance < needed:
                error_msg = f"⚠️ Insufficient balance!\n\n"
                error_msg += f"Need: ${needed:.2f}\n"
                error_msg += f"Spot Balance: ${spot_balance:.2f}\n"
                error_msg += f"Futures Balance: ${futures_balance:.2f}"
                send_telegram(error_msg, message.chat.id)
                with futures_lock:
                    active_futures_trade['running'] = False
                return
            
            transfer_msg = f"💸 Transferring ${needed:.2f} from Spot to Futures..."
            send_telegram(transfer_msg, message.chat.id)
            
            transfer_result = transfer_spot_to_futures(needed)
            if not transfer_result['success']:
                error_msg = f"⚠️ Transfer failed: {transfer_result['error']}"
                send_telegram(error_msg, message.chat.id)
                with futures_lock:
                    active_futures_trade['running'] = False
                return
            
            send_telegram("✅ Transfer successful!", message.chat.id)
        
        # Execute futures order
        order_result = execute_futures_order(pair, side, amount, leverage)
        
        if not order_result['success']:
            error_msg = f"⚠️ Futures order failed: {order_result['error']}"
            send_telegram(error_msg, message.chat.id)
            with futures_lock:
                active_futures_trade['running'] = False
            return
        
        with futures_lock:
            active_futures_trade['pair'] = pair
            active_futures_trade['entry_price'] = order_result['price']
            active_futures_trade['quantity'] = order_result['quantity']
            active_futures_trade['profit_target'] = profit_target
            active_futures_trade['stop_loss'] = stop_loss
            active_futures_trade['side'] = side
            active_futures_trade['leverage'] = leverage
        
        success_msg = f"✅ <b>Futures Trade Started</b>\n\n"
        success_msg += f"⚠️ <b>HIGH RISK!</b>\n\n"
        success_msg += f"Pair: {pair}\n"
        success_msg += f"Side: {side}\n"
        success_msg += f"Leverage: {leverage}x\n"
        success_msg += f"Entry Price: ${order_result['price']:.8f}\n"
        success_msg += f"Quantity: {order_result['quantity']:.8f}\n"
        success_msg += f"Margin: ${amount:.2f}\n\n"
        success_msg += f"Target Profit: ${profit_target:.4f} 🎯\n"
        if stop_loss is not None:
            success_msg += f"Stop Loss: ${stop_loss:.4f} 🛑\n\n"
        else:
            success_msg += f"Stop Loss: Not Set ⚠️\n\n"
        success_msg += f"Monitoring started..."
        send_telegram(success_msg, message.chat.id)
        
        monitor_thread = threading.Thread(target=monitor_futures_trade, daemon=True)
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
