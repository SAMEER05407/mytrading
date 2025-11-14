
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
    'trade_type': 'spot'
}

active_futures_trade = {
    'running': False,
    'pair': None,
    'entry_price': None,
    'quantity': None,
    'profit_target': None,
    'stop_loss': None,
    'side': None,
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
        return True
    except Exception as e:
        print(f"❌ Telegram send failed: {type(e).__name__}: {e}")
        return False

def get_server_ip():
    """Get public IP address"""
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=5)
        return response.json()['ip']
    except:
        return 'Unable to fetch IP'

def calculate_ema(klines, period):
    """Calculate Exponential Moving Average"""
    closes = [float(k[4]) for k in klines]
    
    if len(closes) < period:
        return None
    
    ema = [sum(closes[:period]) / period]
    multiplier = 2 / (period + 1)
    
    for i in range(period, len(closes)):
        ema_value = (closes[i] - ema[-1]) * multiplier + ema[-1]
        ema.append(ema_value)
    
    return ema[-1]

def calculate_atr(klines, period=14):
    """Calculate Average True Range"""
    if len(klines) < period + 1:
        return None
    
    true_ranges = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i-1][4])
        
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        true_ranges.append(tr)
    
    if len(true_ranges) < period:
        return None
    
    atr = sum(true_ranges[-period:]) / period
    return atr

def check_market_conditions(pair, is_futures=False):
    """Check EMA slope and ATR to filter sideways/low volatility markets"""
    try:
        if is_futures:
            klines = binance_client.futures_klines(symbol=pair, interval='5m', limit=100)
        else:
            klines = binance_client.get_klines(symbol=pair, interval='5m', limit=100)
        
        ema_9 = calculate_ema(klines, 9)
        ema_20 = calculate_ema(klines, 20)
        atr = calculate_atr(klines, 14)
        
        if ema_9 is None or ema_20 is None or atr is None:
            return {
                'valid': False,
                'reason': 'Insufficient data for technical analysis'
            }
        
        current_price = float(klines[-1][4])
        
        ema_slope = ((ema_9 - ema_20) / current_price) * 100
        atr_percent = (atr / current_price) * 100
        
        sideways_threshold = 0.15
        atr_threshold = 0.10
        
        issues = []
        
        if abs(ema_slope) < sideways_threshold:
            issues.append(f"Sideways market detected (EMA slope: {ema_slope:.3f}%)")
        
        if atr_percent < atr_threshold:
            issues.append(f"Low volatility (ATR: {atr_percent:.3f}%)")
        
        if issues:
            return {
                'valid': False,
                'reason': ' | '.join(issues),
                'ema_slope': ema_slope,
                'atr_percent': atr_percent
            }
        
        return {
            'valid': True,
            'ema_slope': ema_slope,
            'atr_percent': atr_percent,
            'trend': 'BULLISH' if ema_slope > 0 else 'BEARISH'
        }
        
    except Exception as e:
        print(f"Error checking market conditions: {e}")
        return {
            'valid': False,
            'reason': f'Error: {str(e)}'
        }

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
            type=1
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
    
    if amount < 10:
        errors.append("Amount must be ≥ $10 for futures trading")
    
    if profit <= 0:
        errors.append("Profit must be > 0")
    
    if stop_loss is not None and stop_loss <= 0:
        errors.append("Stop loss must be > 0")
    
    if leverage < 1 or leverage > 20:
        errors.append("Leverage must be between 1 and 20")
    
    if leverage > 10:
        errors.append("⚠️ Warning: Leverage > 10x is very risky!")
    
    return errors

def get_real_price_from_trades(pair, order_id, is_futures=False):
    """Get real fill price from account trades"""
    try:
        trades = binance_client.futures_account_trades(symbol=pair, limit=20) if is_futures else binance_client.get_my_trades(symbol=pair, limit=20)
        matching = [t for t in trades if t['orderId'] == order_id]
        if matching:
            total_value = sum(float(t['price']) * float(t['qty']) for t in matching)
            total_qty = sum(float(t['qty']) for t in matching)
            if total_qty > 0:
                return total_value / total_qty, total_qty
    except Exception as e:
        print(f"⚠️ Error fetching trades: {e}")
    return None, None

def execute_buy_order(pair, amount_usd):
    """Execute spot buy with accurate fill price"""
    try:
        ticker = binance_client.get_symbol_ticker(symbol=pair)
        current_price = float(ticker['price'])
        
        quantity = amount_usd / current_price
        
        info = binance_client.get_symbol_info(pair)
        step_size = next((float(f['stepSize']) for f in info['filters'] if f['filterType'] == 'LOT_SIZE'), 0)
        
        if step_size > 0:
            precision = len(str(step_size).rstrip('0').split('.')[-1])
            quantity = round(quantity, precision)
        
        order = binance_client.order_market_buy(
            symbol=pair,
            quantity=quantity
        )
        order_id = order['orderId']

        time.sleep(0.5) 
        real_price, real_qty = get_real_price_from_trades(pair, order_id, is_futures=False)

        if real_price and real_qty:
            print(f"✅ BUY FILLED: ${real_price:.8f} x {real_qty:.8f}")
            return {
                'success': True,
                'price': real_price,
                'quantity': real_qty,
                'order': order
            }

        order_status = binance_client.get_order(symbol=pair, orderId=order_id)
        avg_price = float(order_status.get('avgPrice', 0))
        exec_qty = float(order_status.get('executedQty', quantity))

        if avg_price > 0:
            print(f"✅ BUY FILLED (order status): ${avg_price:.8f} x {exec_qty:.8f}")
            return {
                'success': True,
                'price': avg_price,
                'quantity': exec_qty,
                'order': order
            }
        
        print(f"⚠️ Using ticker fallback: ${current_price:.8f}")
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

def execute_sell_order(pair, quantity):
    """Execute spot sell with accurate fill price"""
    try:
        info = binance_client.get_symbol_info(pair)
        step_size = next((float(f['stepSize']) for f in info['filters'] if f['filterType'] == 'LOT_SIZE'), 0)
        min_qty = next((float(f['minQty']) for f in info['filters'] if f['filterType'] == 'LOT_SIZE'), 0)
        
        if step_size > 0:
            precision = len(str(step_size).rstrip('0').split('.')[-1])
            quantity = (float(quantity) // step_size) * step_size
            quantity = round(quantity, precision)
        
        if quantity < min_qty:
            return {
                'success': False,
                'error': f'Quantity {quantity} below minimum {min_qty}'
            }
        
        order = binance_client.order_market_sell(
            symbol=pair,
            quantity=quantity
        )
        order_id = order['orderId']

        time.sleep(0.5)
        real_price, real_qty = get_real_price_from_trades(pair, order_id, is_futures=False)

        if real_price and real_qty:
            print(f"✅ SELL FILLED: ${real_price:.8f} x {real_qty:.8f}")
            return {
                'success': True,
                'price': real_price,
                'order': order
            }

        order_status = binance_client.get_order(symbol=pair, orderId=order_id)
        avg_price = float(order_status.get('avgPrice', 0))
        
        if avg_price > 0:
            print(f"✅ SELL FILLED (order status): ${avg_price:.8f}")
            return {
                'success': True,
                'price': avg_price,
                'order': order
            }
        
        ticker = binance_client.get_symbol_ticker(symbol=pair)
        fallback_price = float(ticker['price'])
        print(f"⚠️ Using ticker fallback: ${fallback_price:.8f}")
        return {
            'success': True,
            'price': fallback_price,
            'order': order
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def execute_futures_order(pair, side, amount_usd, leverage, signal_entry_price=None):
    """Execute futures order with accurate fill price"""
    try:
        binance_client.futures_change_leverage(symbol=pair, leverage=leverage)
        
        ticker = binance_client.futures_symbol_ticker(symbol=pair)
        current_price = float(ticker['price'])
        
        info = binance_client.futures_exchange_info()
        precision = 3
        step_size = 0.0
        min_qty = 0.0
        
        for s in info['symbols']:
            if s['symbol'] == pair:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = float(f['stepSize'])
                        min_qty = float(f['minQty'])
                        precision = len(str(step_size).rstrip('0').split('.')[-1])
                        break
                break
        
        quantity = (amount_usd * leverage) / current_price
        
        if step_size > 0:
            quantity = (quantity // step_size) * step_size
            quantity = round(quantity, precision)
        
        if quantity < min_qty:
            quantity = min_qty
        
        if quantity <= 0:
            return {
                'success': False,
                'error': 'Quantity too small'
            }
        
        order = binance_client.futures_create_order(
            symbol=pair,
            side='BUY' if side == 'LONG' else 'SELL',
            type='MARKET',
            quantity=quantity
        )
        order_id = order['orderId']
        
        entry_price = None
        actual_qty = quantity

        for attempt in range(8):
            time.sleep(0.3 if attempt < 3 else 0.5) 
            real_price, real_qty = get_real_price_from_trades(pair, order_id, is_futures=True)
            if real_price and real_qty:
                entry_price = real_price
                actual_qty = real_qty
                print(f"✅ FUTURES {side} OPENED (trades): ${entry_price:.8f} x {actual_qty:.4f}")
                break

        if signal_entry_price:
            entry_price = signal_entry_price
            print(f"ℹ️ Using signal entry price: ${entry_price:.8f}")
        
        if not entry_price:
            order_status = binance_client.futures_get_order(symbol=pair, orderId=order_id)
            avg_price = float(order_status.get('avgPrice', 0))
            if avg_price > 0:
                entry_price = avg_price
                actual_qty = float(order_status.get('executedQty', quantity))
                print(f"✅ FUTURES {side} OPENED (order status): ${entry_price:.8f}")

        if not entry_price:
            entry_price = current_price
            print(f"⚠️ Using ticker fallback: ${entry_price:.8f}")
        
        return {
            'success': True,
            'price': entry_price,
            'quantity': actual_qty,
            'order': order
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def close_futures_position(pair):
    """Close futures position with accurate exit price"""
    try:
        positions = binance_client.futures_position_information(symbol=pair)
        position_amt = next((float(pos['positionAmt']) for pos in positions if pos['symbol'] == pair), 0)
        
        if position_amt == 0:
            return {'success': False, 'error': 'No position'}
        
        side = 'SELL' if position_amt > 0 else 'BUY'
        quantity = abs(position_amt)
        
        order = binance_client.futures_create_order(
            symbol=pair,
            side=side,
            type='MARKET',
            quantity=quantity
        )
        order_id = order['orderId']
        
        exit_price = None
        
        for attempt in range(8):
            time.sleep(0.3 if attempt < 3 else 0.5) 
            real_price, real_qty = get_real_price_from_trades(pair, order_id, is_futures=True)
            if real_price and real_qty:
                exit_price = real_price
                print(f"✅ POSITION CLOSED (trades): ${exit_price:.8f}")
                break

        if not exit_price:
            order_status = binance_client.futures_get_order(symbol=pair, orderId=order_id)
            avg_price = float(order_status.get('avgPrice', 0))
            if avg_price > 0:
                exit_price = avg_price
                print(f"✅ POSITION CLOSED (order status): ${exit_price:.8f}")

        if not exit_price:
            ticker = binance_client.futures_symbol_ticker(symbol=pair)
            exit_price = float(ticker['price'])
            print(f"⚠️ Using ticker fallback: ${exit_price:.8f}")
        
        if exit_price == 0:
            raise Exception("Failed to get exit price")
            
        return {
            'success': True,
            'price': exit_price,
            'quantity': quantity,
            'order': order
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

def calculate_pnl(pair, buy_price, quantity):
    """Calculate spot P&L - FIX #1: quantity-based"""
    try:
        ticker = binance_client.get_symbol_ticker(symbol=pair)
        current_price = float(ticker['price'])
        
        pnl = (current_price - buy_price) * quantity
        
        return {
            'current_price': current_price,
            'pnl': pnl
        }
    except:
        return None

def calculate_futures_pnl(pair, entry_price, side, quantity):
    """Calculate futures P&L with unrealized_pnl"""
    try:
        positions = binance_client.futures_position_information(symbol=pair)
        position_data = next((pos for pos in positions if pos['symbol'] == pair), None)
        
        if not position_data:
            return {
                'current_price': 0,
                'pnl': 0,
                'position_closed': True
            }
        
        position_amt = float(position_data['positionAmt'])
        unrealized_pnl = float(position_data['unRealizedProfit'])
        entry_price_binance = float(position_data['entryPrice'])
        
        if position_amt == 0:
            return {
                'current_price': 0,
                'pnl': 0,
                'position_closed': True
            }
        
        ticker = binance_client.futures_symbol_ticker(symbol=pair)
        current_price = float(ticker['price'])
        
        actual_qty = abs(position_amt)
        
        return {
            'current_price': current_price,
            'pnl': unrealized_pnl,
            'unrealized_pnl': unrealized_pnl,
            'position_amt': position_amt,
            'actual_quantity': actual_qty,
            'entry_price_binance': entry_price_binance,
            'position_closed': False
        }
    except Exception as e:
        print(f"Error calculating futures P&L: {e}")
        return None

def monitor_trade():
    """Monitor spot trade - FIX #1: quantity-based PnL"""
    global active_trade
    
    pair = active_trade['pair']
    buy_price = active_trade['buy_price']
    quantity = active_trade['quantity']
    profit_target = active_trade['profit_target']
    stop_loss = active_trade['stop_loss']
    asset = active_trade['asset']
    
    print(f"🔍 Monitoring {pair} - Buy: ${buy_price:.8f}, Qty: {quantity:.8f}, Target: ${profit_target}, SL: ${stop_loss}")
    
    consecutive_errors = 0
    last_balance_check = 0

    while active_trade['running']:
        try:
            current_time = time.time()
            
            if current_time - last_balance_check >= 10:
                current_balance = get_asset_balance(asset)
                last_balance_check = current_time
                if current_balance < (quantity * 0.01):
                    send_telegram(f"⚠️ Position closed externally. Stopping monitor.")
                    with trade_lock:
                        active_trade['running'] = False
                    break
                 
            pnl_data = calculate_pnl(pair, buy_price, quantity)
            if not pnl_data:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    send_telegram(f"⚠️ Too many errors fetching price data. Stopping monitor.")
                    with trade_lock:
                        active_trade['running'] = False
                    break
                time.sleep(2)
                continue
            
            consecutive_errors = 0
            current_pnl = pnl_data['pnl']
            
            print(f"📊 {pair} | P&L: ${current_pnl:.4f} | Target: ${profit_target:.4f}")
            
            if current_pnl >= profit_target:
                print(f"✅ PROFIT TARGET REACHED!")
                send_telegram(f"⏳ Executing sell order...")
                
                final_balance = get_asset_balance(asset)
                sell_result = execute_sell_order(pair, final_balance)
                
                if sell_result['success']:
                    sell_price = sell_result['price']
                    actual_profit = (sell_price - buy_price) * final_balance
                    
                    message = f"💰 <b>PROFIT HIT!</b>\n\n{pair}\nBuy: ${buy_price:.8f}\nSell: ${sell_price:.8f}\nProfit: ${actual_profit:.4f}"
                    send_telegram(message)
                    
                with trade_lock:
                    active_trade['running'] = False
                    for key in list(active_trade.keys()):
                        if key != 'running':
                            active_trade[key] = None
                break
            
            elif stop_loss is not None and current_pnl <= -stop_loss:
                print(f"🛑 STOP LOSS TRIGGERED!")
                send_telegram(f"⏳ Executing stop-loss sell...")
                
                final_balance = get_asset_balance(asset)
                sell_result = execute_sell_order(pair, final_balance)
                
                if sell_result['success']:
                    sell_price = sell_result['price']
                    actual_loss = (sell_price - buy_price) * final_balance
                    
                    message = f"🛑 <b>STOP LOSS!</b>\n\n{pair}\nBuy: ${buy_price:.8f}\nSell: ${sell_price:.8f}\nLoss: ${actual_loss:.4f}"
                    send_telegram(message)
                
                with trade_lock:
                    active_trade['running'] = False
                    for key in list(active_trade.keys()):
                        if key != 'running':
                            active_trade[key] = None
                break
            
            time.sleep(2)
            
        except Exception as e:
            print(f"⚠️ Monitor error: {e}")
            consecutive_errors += 1
            if consecutive_errors >= 5:
                send_telegram(f"⚠️ Critical error in monitor. Stopping.")
                with trade_lock:
                    active_trade['running'] = False
                break
            time.sleep(2)

def monitor_futures_trade():
    """Monitor futures trade - FIX #2, #3: unrealized_pnl SL, position sync"""
    global active_futures_trade
    
    pair = active_futures_trade['pair']
    entry_price = active_futures_trade['entry_price']
    quantity = active_futures_trade['quantity']
    profit_target = active_futures_trade['profit_target']
    stop_loss = active_futures_trade['stop_loss']
    side = active_futures_trade['side']
    
    print(f"🔍 Monitoring Futures {pair} {side} - Entry: ${entry_price:.8f}, Target: ${profit_target}, SL: ${stop_loss}")
    
    consecutive_errors = 0
    
    while active_futures_trade['running']:
        try:
            pnl_data = calculate_futures_pnl(pair, entry_price, side, quantity)
            
            if not pnl_data:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    send_telegram(f"⚠️ Too many errors fetching futures data. Stopping monitor.")
                    with futures_lock:
                        active_futures_trade['running'] = False
                    break
                time.sleep(2)
                continue
            
            if pnl_data.get('position_closed', False):
                send_telegram(f"⚠️ Position closed externally. Stopping monitor.")
                with futures_lock:
                    active_futures_trade['running'] = False
                break
            
            consecutive_errors = 0
            unrealized_pnl = pnl_data['unrealized_pnl']
            actual_qty = pnl_data.get('actual_quantity', quantity)
            entry_price_binance = pnl_data.get('entry_price_binance', entry_price)
            
            # FIX #3: Position size change → update BOTH quantity AND entry_price
            if abs(actual_qty - quantity) > 0.001 or abs(entry_price_binance - entry_price) > 0.00001:
                print(f"⚠️ Position changed: {quantity:.4f} → {actual_qty:.4f}, Entry: ${entry_price:.8f} → ${entry_price_binance:.8f}")
                with futures_lock:
                    quantity = actual_qty
                    entry_price = entry_price_binance
                    active_futures_trade['quantity'] = actual_qty
                    active_futures_trade['entry_price'] = entry_price_binance
            
            print(f"📊 Futures {side} | P&L: ${unrealized_pnl:.4f} | Qty: {actual_qty:.4f} | Target: ${profit_target:.4f}")
            
            if unrealized_pnl >= profit_target:
                print(f"✅ FUTURES PROFIT TARGET REACHED!")
                send_telegram(f"⏳ Closing futures position...")
                
                close_result = close_futures_position(pair)
                
                if close_result['success']:
                    exit_price = close_result['price']
                    actual_profit = (exit_price - entry_price) * quantity if side == 'LONG' else (entry_price - exit_price) * quantity
                    
                    message = f"💰 <b>FUTURES PROFIT HIT!</b>\n\n{pair} {side}\nEntry: ${entry_price:.8f}\nExit: ${exit_price:.8f}\nProfit: ${actual_profit:.4f}"
                    send_telegram(message)
                
                with futures_lock:
                    active_futures_trade['running'] = False
                    for key in list(active_futures_trade.keys()):
                        if key != 'running':
                            active_futures_trade[key] = None
                break
            
            # FIX #2: EXACT unrealized_pnl check without 0.97 modifier
            elif stop_loss is not None and unrealized_pnl <= -stop_loss:
                print(f"🛑 FUTURES STOP LOSS TRIGGERED!")
                send_telegram(f"⏳ Stop-loss: Closing position...")
                
                close_result = close_futures_position(pair)
                
                if close_result['success']:
                    exit_price = close_result['price']
                    actual_loss = (exit_price - entry_price) * quantity if side == 'LONG' else (entry_price - exit_price) * quantity
                    
                    message = f"🛑 <b>FUTURES STOP LOSS!</b>\n\n{pair} {side}\nEntry: ${entry_price:.8f}\nExit: ${exit_price:.8f}\nLoss: ${actual_loss:.4f}"
                    send_telegram(message)
                
                with futures_lock:
                    active_futures_trade['running'] = False
                    for key in list(active_futures_trade.keys()):
                        if key != 'running':
                            active_futures_trade[key] = None
                break
            
            time.sleep(2)
            
        except Exception as e:
            print(f"⚠️ Futures monitor error: {e}")
            consecutive_errors += 1
            if consecutive_errors >= 5:
                send_telegram(f"⚠️ Critical error in futures monitor. Stopping.")
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
        """Check active trade status"""
        with trade_lock:
            if active_trade['running']:
                pair = active_trade['pair']
                buy_price = active_trade['buy_price']
                quantity = active_trade['quantity']
                asset = active_trade['asset']
                
                try:
                    current_balance = get_asset_balance(asset)
                    
                    if current_balance < (quantity * 0.01): 
                        status_msg = "⚠️ Position appears to be closed externally. Bot will stop monitoring shortly."
                        send_telegram(status_msg, message.chat.id)
                        with trade_lock:
                            active_trade['running'] = False
                        return
                    
                    pnl_data = calculate_pnl(pair, buy_price, quantity)
                    
                    if pnl_data:
                        status_msg = f"📊 <b>Active Spot Trade Status</b>\n\n"
                        status_msg += f"Pair: {pair}\n"
                        status_msg += f"Buy Price: ${buy_price:.8f}\n"
                        status_msg += f"Current Price: ${pnl_data['current_price']:.8f}\n"
                        status_msg += f"Quantity: {quantity:.8f} {asset}\n"
                        status_msg += f"Balance: {current_balance:.8f} {asset}\n\n"
                        status_msg += f"<b>P&L: ${pnl_data['pnl']:.4f}</b>\n"
                        status_msg += f"Target Profit: ${active_trade['profit_target']:.4f}\n"
                        if active_trade['stop_loss'] is not None:
                            status_msg += f"Stop Loss: ${active_trade['stop_loss']:.4f}\n\n"
                        else:
                            status_msg += f"Stop Loss: Not Set\n\n"
                        
                        if pnl_data['pnl'] > 0:
                            profit_percent = (pnl_data['pnl'] / active_trade['profit_target']) * 100 if active_trade['profit_target'] else 0
                            status_msg += f"Progress: {profit_percent:.1f}% to target 📈"
                        else:
                            if active_trade['stop_loss'] is not None:
                                loss_percent = (abs(pnl_data['pnl']) / active_trade['stop_loss']) * 100 if active_trade['stop_loss'] else 0
                                status_msg += f"Loss: {loss_percent:.1f}% of stop-loss 📉"
                            else:
                                status_msg += f"Current Loss: ${abs(pnl_data['pnl']):.4f} (No stop-loss) ⚠️"
                        
                        send_telegram(status_msg, message.chat.id)
                    else:
                        send_telegram("⚠️ Error fetching current data", message.chat.id)
                except Exception as e:
                    send_telegram(f"⚠️ Error checking status: {e}", message.chat.id)
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
                side = active_futures_trade['side']
                
                try:
                    pnl_data = calculate_futures_pnl(pair, entry_price, side, quantity)
                    
                    if pnl_data:
                        status_msg = f"📊 <b>Active Futures Trade Status</b>\n\n"
                        status_msg += f"Pair: {pair}\n"
                        status_msg += f"Side: {side}\n"
                        status_msg += f"Leverage: {active_futures_trade['leverage']}x\n"
                        status_msg += f"Entry Price: ${entry_price:.8f}\n"
                        status_msg += f"Current Price: ${pnl_data['current_price']:.8f}\n"
                        status_msg += f"Quantity: {pnl_data['actual_quantity']:.8f}\n\n"
                        status_msg += f"<b>P&L: ${pnl_data['unrealized_pnl']:.4f}</b>\n"
                        status_msg += f"Target Profit: ${active_futures_trade['profit_target']:.4f}\n"
                        if active_futures_trade['stop_loss'] is not None:
                            status_msg += f"Stop Loss: ${active_futures_trade['stop_loss']:.4f}\n\n"
                        else:
                            status_msg += f"Stop Loss: Not Set\n\n"
                        
                        if pnl_data['unrealized_pnl'] > 0:
                            profit_percent = (pnl_data['unrealized_pnl'] / active_futures_trade['profit_target']) * 100 if active_futures_trade['profit_target'] else 0
                            status_msg += f"Progress: {profit_percent:.1f}% to target 📈"
                        else:
                            status_msg += f"Current Loss: ${abs(pnl_data['unrealized_pnl']):.4f} 📉"
                        
                        send_telegram(status_msg, message.chat.id)
                    else:
                        send_telegram("⚠️ Error fetching futures data", message.chat.id)
                except Exception as e:
                    send_telegram(f"⚠️ Error checking futures status: {e}", message.chat.id)
            else:
                send_telegram("✅ No active futures trade. Use /futures to start one.", message.chat.id)

    @telegram_bot.message_handler(commands=['trade'])
    def start_trade_command(message):
        """Handle /trade command"""
        
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
        
        market_check = check_market_conditions(pair, is_futures=False)
        if not market_check['valid']:
            warning_msg = f"⚠️ <b>Market Condition Warning</b>\n\n"
            warning_msg += f"Pair: {pair}\n"
            warning_msg += f"Issue: {market_check['reason']}\n\n"
            warning_msg += f"<i>Trading in such conditions may increase stop-loss hits.</i>"
            send_telegram(warning_msg, message.chat.id)
            print(f"⚠️ Market filter: {market_check['reason']}")
        else:
            analysis_msg = f"✅ <b>Market Analysis</b>\n\n"
            analysis_msg += f"Trend: {market_check['trend']}\n"
            analysis_msg += f"EMA Slope: {market_check['ema_slope']:.3f}%\n"
            analysis_msg += f"ATR: {market_check['atr_percent']:.3f}%"
            send_telegram(analysis_msg, message.chat.id)
            print(f"✅ Market conditions favorable: {market_check['trend']}")
        
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
        success_msg += f"Monitoring will start in 2 seconds..."
        send_telegram(success_msg, message.chat.id)
        
        # FIX #5: 2-second cooldown before monitoring starts
        time.sleep(2)
        
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
        
        market_check = check_market_conditions(pair, is_futures=True)
        if not market_check['valid']:
            warning_msg = f"⚠️ <b>Market Condition Warning</b>\n\n"
            warning_msg += f"Pair: {pair}\n"
            warning_msg += f"Issue: {market_check['reason']}\n\n"
            warning_msg += f"<i>High leverage in such conditions is very risky!</i>"
            send_telegram(warning_msg, message.chat.id)
            print(f"⚠️ Futures market filter: {market_check['reason']}")
        else:
            analysis_msg = f"✅ <b>Futures Market Analysis</b>\n\n"
            analysis_msg += f"Trend: {market_check['trend']}\n"
            analysis_msg += f"EMA Slope: {market_check['ema_slope']:.3f}%\n"
            analysis_msg += f"ATR: {market_check['atr_percent']:.3f}%\n"
            analysis_msg += f"Leverage: {leverage}x"
            send_telegram(analysis_msg, message.chat.id)
            print(f"✅ Futures market conditions favorable: {market_check['trend']}")
        
        with futures_lock:
            if active_futures_trade['running']:
                send_telegram("🚫 Futures trade already running!", message.chat.id)
                return
            
            active_futures_trade['running'] = True
        
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
        success_msg += f"Monitoring will start in 2 seconds..."
        send_telegram(success_msg, message.chat.id)
        
        # FIX #5: 2-second cooldown before monitoring starts
        time.sleep(2)
        
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
