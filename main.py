import hmac, hashlib, json, time, os, requests, logging
from flask import Flask, request, jsonify
from flask_cors import CORS
import traceback
import threading
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

# Use a persistent session for faster API calls
session = requests.Session()

# --- GLOBAL SETTINGS ---
auto_trade_enabled = True
auto_flip_enabled = True 
current_leverage = 200
current_invest_pct = 0.10
last_trade_time = datetime.min
signal_lock = threading.Lock() 
sync_lock = threading.Lock()  # Critical: Prevents TSL crash
profit_stage = 0  # This will now store the ROE value (e.g., 20, 30, 40)

## --- LOSS PROTECTION & PAUSE LOGIC ---
consecutive_losses = 0
pause_until = datetime.min
loss_lock = threading.Lock()

# --- DEBOUNCE & PROFIT CONTROL ---
# (profit_stage is already defined above)

# --- CONSTANTS & NATIVE TRAILING STOP SETTINGS ---
USD_INR_FIXED = 85.0 
trailing_stop_enabled = True 
trail_offset = "6.50" 
HARD_STOP_GAP = "6.50" # Kept at your safe initial distance

# --- DUAL-PATH PROFIT LADDER ---
# trail = Standard market conditions
# v_trail = High volatility (ATR/Range expansion)
STAGES = [
    {"roe": 1.0,  "trail": "6.50", "v_trail": "7.00"}, 
    {"roe": 90.0,  "trail": "1.50", "v_trail": "3.50"}, 
    {"roe": 110.0, "trail": "1.00", "v_trail": "2.50"}, 
    {"roe": 130.0, "trail": "0.50", "v_trail": "1.50"}  
]

# API KEYS
API_KEY = os.getenv("DELTA_API_KEY", "926ZP6oByHTDNtoplpflp64imuDKHk")
API_SECRET = os.getenv("DELTA_API_SECRET", "1v6SbRZTHF2FSUTjkoK8cNwRSv1yDZmHAE8TugqokLRVn1tt21OD3BnwfJ2r")
BASE_URL = 'https://api.india.delta.exchange'

latest_signal = {"action": "WAITING", "time": "--:--"}

# --- HELPER FUNCTIONS ---

def delta_request(method, path, payload=None, retries=3):
    """Robust request handler with Connection Pooling."""
    # Pre-serialize body for speed
    body = json.dumps(payload, separators=(',', ':')) if payload else ""
    
    for attempt in range(retries):
        timestamp = str(int(time.time()))
        signature_data = f"{method.upper()}{timestamp}{path}{body}"
        signature = hmac.new(API_SECRET.encode('utf-8'), signature_data.encode('utf-8'), hashlib.sha256).hexdigest()
        
        headers = {
            'api-key': API_KEY, 
            'signature': signature, 
            'timestamp': timestamp, 
            'Content-Type': 'application/json',
            'Connection': 'keep-alive'
        }
        try:
            # Reduced timeout for scalping
            res = session.request(method, BASE_URL + path, headers=headers, data=body, timeout=5)
            if not res.text or not res.text.strip():
                raise ValueError("Empty response")
            return res.json()
        except Exception as e:
            app.logger.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < retries - 1:
                time.sleep(0.5) # Reduced retry delay for scalping
            else:
                return {"success": False, "error": str(e)}

def get_verified_balance():
    """Retries balance check to wait for margin release after a close."""
    for _ in range(3):
        bal_res = delta_request("GET", "/v2/wallet/balances")
        avail = sum(float(a.get('available_balance', 0)) for a in bal_res.get('result', []) 
                    if a.get('asset_symbol') in ["USD", "USDT"])
        if avail > 0.05: # Threshold to ensure it's not just dust
            return avail
        time.sleep(0.5) # Reduced sleep
    return 0

def set_native_trailing_stop(product_id, size, side, custom_val=None):
    """Function to set the TSL on Delta Exchange."""
    with sync_lock:
        try:
            # Cancel existing orders to prevent overlap
            delta_request("DELETE", f"/v2/orders/all?product_id={product_id}")
            time.sleep(0.3) # Reduced sleep
            
            stop_side = "sell" if side.lower() == "buy" else "buy"
            use_val = custom_val if custom_val else trail_offset
            final_trail = f"-{use_val}" if stop_side == "sell" else use_val
            
            payload = {
                "product_id": product_id,
                "size": str(abs(int(size))),
                "side": stop_side,
                "order_type": "market_order",
                "stop_order_type": "stop_loss_order",
                "trail_amount": final_trail,      
                "reduce_only": True,
                "bracket_stop_trigger_method": "mark_price"
            }
            res = delta_request("POST", "/v2/orders", payload)
            if res.get('success'):
                app.logger.info(f"TSL Set/Updated: {use_val} for {stop_side}")
            return res
        except Exception as e:
            app.logger.error(f"TSL Update Error: {e}")
            return {"success": False}

def calculate_trade_qty(available_usd, mark_price):
    # Using 0.75 safety buffer for 200x leverage
    return int((available_usd * current_invest_pct * current_leverage * 0.75) / mark_price / 0.01)

def execute_emergency_exit():
    """Closes all ETHUSD positions and cancels all pending orders."""
    try:
        # 1. Fetch current position
        pos_res = delta_request("GET", "/v2/positions/margined")
        eth_pos = next((p for p in pos_res.get('result', []) if p.get('product_symbol') == 'ETHUSD'), None)
        
        if eth_pos:
            product_id = eth_pos['product_id']
            size = int(float(eth_pos['size']))
            
            # 2. Cancel ALL open orders first (TSL and Hard TP)
            delta_request("DELETE", f"/v2/orders/all?product_id={product_id}")
            time.sleep(0.2) 
            
            # 3. If a position exists, close it at market
            if size != 0:
                side = "sell" if size > 0 else "buy"
                res = delta_request("POST", "/v2/orders", {
                    "product_id": product_id, 
                    "size": str(abs(size)),
                    "side": side, 
                    "order_type": "market_order", 
                    "reduce_only": True
                })
                if res.get('success'):
                    app.logger.info(f"EMERGENCY EXIT: Closed {size} units and cleared orders.")
                else:
                    app.logger.error(f"EMERGENCY EXIT FAILED: {res.get('error')}")
        else:
            app.logger.info("Emergency Exit called, but no ETHUSD position found.")
            
    except Exception as e:
        app.logger.error(f"Critical Error in Emergency Exit: {e}")

# --- BACKGROUND MONITORING ---

def monitor_profit_and_tighten():
    global profit_stage, latest_signal
    MIN_EXIT_GAP = 10.0 

    while True:
        try:
            pos_res = delta_request("GET", "/v2/positions/margined")
            if pos_res.get('success'):
                eth_pos = next((p for p in pos_res.get('result', []) if p.get('product_symbol') == 'ETHUSD'), None)
                
                if eth_pos and abs(float(eth_pos['size'])) > 0:
                    # --- 1. DATA COLLECTION ---
                    entry_p = float(eth_pos.get('entry_price', 0))
                    mark_p = float(eth_pos.get('mark_price', 0))
                    upnl, margin = float(eth_pos.get('unrealized_pnl', 0)), float(eth_pos.get('margin', 1))
                    roe_pct = (upnl / margin) * 100
                    size, side = abs(int(float(eth_pos['size']))), ("buy" if float(eth_pos['size']) > 0 else "sell")

                    # --- 2. VOLATILITY INDEX (ATR PROXY) ---
                    ticker = session.get(f"{BASE_URL}/v2/tickers/ETHUSD", timeout=2).json()
                    vol_index = float(ticker['result']['quotes']['high_24h']) - float(ticker['result']['quotes']['low_24h'])
                    is_high_vol = vol_index > 65.0 # High Volatility Threshold

                    # --- 3. TP EXIT ALERT & EXECUTION ($10 GAP) ---
                    price_gap = abs(mark_p - entry_p)
                    if price_gap >= MIN_EXIT_GAP:
                        # Log the TP Exit event
                        app.logger.info(f"TP EXIT TRIGGERED: {price_gap:.2f} USD Gap achieved.")
                        latest_signal = {"action": "TP_EXIT", "time": datetime.now().strftime('%H:%M:%S')}
                        
                        # Execute the close
                        execute_emergency_exit()
                        profit_stage = 0
                        continue

                    # --- 4. DUAL LADDER LOGIC ---
                    highest_met_roe = 0
                    current_target_trail = None

                    for stage in STAGES:
                        if roe_pct >= stage["roe"]:
                            highest_met_roe = int(stage["roe"])
                            current_target_trail = stage["v_trail"] if is_high_vol else stage["trail"]

                    if highest_met_roe > profit_stage:
                        profit_stage = highest_met_roe 
                        app.logger.info(f"LADDER UP: {profit_stage}% | Vol: {'HIGH' if is_high_vol else 'LOW'} | TSL: {current_target_trail}")
                        set_native_trailing_stop(eth_pos['product_id'], size, side, current_target_trail)
                
                else:
                    profit_stage = 0 

            time.sleep(1.0) 
        except Exception as e:
            app.logger.error(f"Monitor Loop Error: {e}")
            time.sleep(5)

def perform_trade_logic(side, signal_price=0):
    with signal_lock: 
        try:
            # 1. Faster Ticker and Position Fetch
            ticker_res = session.get(f"{BASE_URL}/v2/tickers/ETHUSD", timeout=3).json()
            mark_price = float(ticker_res['result']['mark_price'])
            product_id = ticker_res['result']['product_id']
            
            # SLIPPAGE PROTECTION: Skip if market moved > 0.15% from TV signal price
            if signal_price > 0:
                slippage = abs(mark_price - signal_price) / signal_price
                if slippage > 0.0015:
                    app.logger.warning(f"Slippage too high ({slippage:.4f}). Entry blocked.")
                    return

            pos_res = delta_request("GET", "/v2/positions/margined")
            eth_pos = next((p for p in pos_res.get('result', []) if p.get('product_symbol') == 'ETHUSD'), None)
            current_size = int(float(eth_pos['size'])) if eth_pos else 0

            target_side = "BUY" if "BUY" in side else "SELL"

            if (target_side == "BUY" and current_size > 0) or (target_side == "SELL" and current_size < 0):
                app.logger.info(f"Already in {target_side} position. Skipping.")
                return

            # 3. EXECUTE THE FLIP
            if eth_pos and auto_flip_enabled:
                if (target_side == "BUY" and current_size < 0) or (target_side == "SELL" and current_size > 0):
                    app.logger.info(f"Flipping position. Closing {current_size} units.")
                    close_res = delta_request("POST", "/v2/orders", {
                        "product_id": product_id, "size": str(abs(current_size)), 
                        "side": "buy" if current_size < 0 else "sell", 
                        "order_type": "market_order", "reduce_only": True
                    })
                    if close_res.get('success'):
                        time.sleep(0.6) # Faster margin release

            # 4. RE-FETCH BALANCE 
            available = get_verified_balance()
            
            # 5. EXECUTE NEW ENTRY
            qty = calculate_trade_qty(available, mark_price)
            
            if qty > 0:
                app.logger.info(f"Executing {target_side} for {qty} units.")
                res = delta_request("POST", "/v2/orders", {
                    "product_id": product_id, 
                    "size": str(qty), 
                    "side": target_side.lower(), 
                    "order_type": "market_order"
                })
                
                if res.get('success'):
                    # FAST SCALPING FEATURE: Hard Take Profit at 150% ROE
                    # Capture spikes even if TSL doesn't update fast enough
                    tp_price = mark_price * (1 + (1.5 / current_leverage)) if target_side == "BUY" else mark_price * (1 - (1.5 / current_leverage))
                    
                    # Set TSL and Hard TP nearly simultaneously
                    time.sleep(0.4)
                    set_native_trailing_stop(product_id, qty, target_side, HARD_STOP_GAP)
                    
                    delta_request("POST", "/v2/orders", {
                        "product_id": product_id, "size": str(qty),
                        "side": "sell" if target_side == "BUY" else "buy",
                        "order_type": "limit_order", "limit_price": f"{tp_price:.2f}",
                        "reduce_only": True
                    })
                    
            else:
                app.logger.warning(f"Calculated QTY is 0. Available: ${available}")

        except Exception as e: 
            app.logger.error(f"Trade Logic Error: {e}")
            
def handle_remote_update(new_tp, new_sl, new_atr):
    """
    Updates the live TSL on Delta when the TradingView Dashboard shifts.
    Ensures updates don't overwrite the App's profit-protection ladder.
    """
    global profit_stage
    try:
        # 1. Fetch current position to get Product ID and Size
        pos_res = delta_request("GET", "/v2/positions/margined")
        eth_pos = next((p for p in pos_res.get('result', []) if p.get('product_symbol') == 'ETHUSD'), None)
        
        if eth_pos and abs(float(eth_pos['size'])) > 0:
            # SAFETY CHECK: Only update SL from webhook if the App Ladder hasn't kicked in yet.
            # If profit_stage > 0, the app is already managing a tight profit-lock stop.
            if profit_stage == 0:
                product_id = eth_pos['product_id']
                size = abs(int(float(eth_pos['size'])))
                side = "buy" if float(eth_pos['size']) > 0 else "sell"
                
                # CRITICAL: Format ATR to exactly 2 decimals for Delta API
                formatted_atr = f"{float(new_atr):.2f}"
                
                app.logger.info(f"DASHBOARD UPDATE: Moving TSL to {formatted_atr}")
                set_native_trailing_stop(product_id, size, side, custom_val=formatted_atr)
            else:
                app.logger.info(f"DASHBOARD UPDATE IGNORED: App Ladder is active (Stage {profit_stage}%).")
            
            # 2. Update Limit Take Profit if new_tp is provided
            if new_tp:
                formatted_tp = f"{float(new_tp):.2f}"
                # Optional: You could add a delta_request here to move your Limit TP order
                # but usually, the native TSL handles the dynamic exit better.
                pass
                
        else:
            app.logger.info("Update received but no active ETHUSD position to modify.")
            
    except Exception as e:
        app.logger.error(f"Error in Remote Update: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    global latest_signal, profit_stage, last_trade_time, trail_offset, current_tp_price
    try:
        data = request.get_json(silent=True) or {}
        action = data.get("action", "").upper()
        sig_price = float(data.get("price", 0))
        now = datetime.now()
        
        is_priority = any(x in action for x in ["EXT", "FLIP", "EXIT"]) or data.get("manual", False)
        
        if not is_priority and (now - last_trade_time).total_seconds() < 1:
            app.logger.info(f"Flicker ignored: {action}")
            return jsonify({"status": "ignored", "reason": "flicker_protection"}), 200
            
        last_trade_time = now
        latest_signal = {"action": action, "time": now.strftime('%H:%M:%S')}
        
        # --- EXECUTION LOGIC ---
        
        # 1. Take Profit / Emergency Exit
        if action == "EXIT_TP":
            threading.Thread(target=execute_emergency_exit).start()
            app.logger.info("TP Exit triggered from Webhook.")

        # 2. Standard Trades & Flips
        elif action in ["BUY", "SELL", "FLIP_BUY", "FLIP_SELL", "EXT_BUY", "EXT_SELL"]:
            if auto_trade_enabled or data.get("manual", False):
                profit_stage = 0  # Reset ladder
                threading.Thread(target=perform_trade_logic, args=(action, sig_price)).start()
                app.logger.info(f"Trade Thread Started: {action}")
            else:
                app.logger.info(f"Signal received ({action}), but Auto-Trade is OFF.")

        # 3. Dashboard Updates
        elif action == "UPDATE":
            new_tp = data.get("new_tp")
            new_sl = data.get("new_sl")
            new_atr = data.get("new_atr")
            
            if new_atr:
                trail_offset = f"{float(new_atr):.2f}"
            if new_tp:
                # Initialize this at the top of your script as: current_tp_price = "0.00"
                current_tp_price = f"{float(new_tp):.2f}"

            app.logger.info(f"DASHBOARD UPDATE: TP:{new_tp} | Trail:{trail_offset}")
            threading.Thread(target=handle_remote_update, args=(new_tp, new_sl, new_atr)).start()

        return jsonify({"status": "processed", "action": action}), 200

    except Exception as e:
        app.logger.error(f"WEBHOOK ERROR: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500
            
@app.route('/emergency-exit', methods=['POST'])
def emergency_exit_route():
    threading.Thread(target=execute_emergency_exit).start()
    return jsonify({"status": "exit_triggered"}), 200

@app.route('/update-settings', methods=['POST'])
def update_settings():
    # 1. Added trailing_stop_enabled to the global list
    global auto_trade_enabled, auto_flip_enabled, current_leverage, current_invest_pct, trail_offset, trailing_stop_enabled
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data received"}), 400

        # 2. Update all settings
        if "auto_trade" in data: 
            auto_trade_enabled = bool(data["auto_trade"])
        if "auto_flip" in data: 
            auto_flip_enabled = bool(data["auto_flip"])
        if "leverage" in data: 
            current_leverage = int(data["leverage"])
        if "invest_pct" in data: 
            current_invest_pct = float(data["invest_pct"]) / 100.0
        if "trail_value" in data: 
            trail_offset = f"{float(data['trail_value']):.2f}"
            
        # 3. This now actually runs because the 'return' is at the very end
        if "tsl_enable" in data: 
            trailing_stop_enabled = bool(data["tsl_enable"])

        app.logger.info(f"Settings Updated: TSL={trailing_stop_enabled}, AutoTrade={auto_trade_enabled}")
        
        # 4. Single return statement at the bottom
        return jsonify({
            "status": "success", 
            "tsl_active": trailing_stop_enabled,
            "auto_trade": auto_trade_enabled
        })

    except Exception as e:
        app.logger.error(f"Update Settings Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get-app-data', methods=['GET'])
def get_app_data():
    try:
        # 1. Fetch Ticker for Mark Price
        ticker_res = session.get(f"{BASE_URL}/v2/tickers/ETHUSD", timeout=2).json()
        mark_price = float(ticker_res.get('result', {}).get('mark_price', 0))
        
        # 2. Fetch Balances (Fixed Margin Reflection)
        bal_res = delta_request("GET", "/v2/wallet/balances")
        balances = bal_res.get('result', [])
        # Sums all available USD/USDT to show as your 'Margin'
        available_usd = sum(float(a.get('available_balance') or 0) for a in balances 
                            if a.get('asset_symbol') in ["USD", "USDT"])
        
        # 3. Fetch Positions for ROE %
        pos_res = delta_request("GET", "/v2/positions/margined")
        positions = pos_res.get('result', [])
        
        total_upnl_usd = 0.0
        total_margin_in_use = 0.0
        
        for pos in positions:
            if pos.get('product_symbol') == 'ETHUSD':
                total_upnl_usd += float(pos.get('unrealized_pnl') or 0)
                total_margin_in_use += float(pos.get('margin') or 0)

        # --- DELTA MATCHING LOGIC ---
        # Delta shows (uPNL / Margin) * 100. 
        if total_margin_in_use > 0:
            upnl_pct = (total_upnl_usd / total_margin_in_use) * 100
        else:
            upnl_pct = 0.0

        # Sync color with profit status
        pnl_color = "green" if total_upnl_usd > 0 else "red" if total_upnl_usd < 0 else "white"

        qty = calculate_trade_qty(available_usd, mark_price)
        
        return jsonify({
            "available_margin": f"{available_usd:.2f}",
            "available_margin_inr": f"Ã¢ÂÂ¹{available_usd * USD_INR_FIXED:,.2f}",
            "total_upnl": f"{total_upnl_usd:.4f}",
            "total_upnl_pct": f"{upnl_pct:.2f}%", 
            "pnl_color": pnl_color,
            "total_upnl_inr": f"Ã¢ÂÂ¹{total_upnl_usd * USD_INR_FIXED:,.2f}", 
            "quantity": str(max(0, qty)),
            "signal": latest_signal["action"],
            "signal_time": latest_signal.get("time", "--:--"),
            "auto_trade": auto_trade_enabled,
            "profit_stage": f"{profit_stage}%",
            "current_trail": trail_offset 
        })
    except Exception as e:
        app.logger.error(f"App Data Sync Error: {e}")
        return jsonify({"error": "fetch failed", "details": str(e)}), 500

@app.route('/my-ip', methods=['GET'])
def get_my_ip():
    try: return jsonify({"server_public_ip": session.get('https://api.ipify.org?format=json', timeout=5).json().get('ip'), "status": "online"})
    except: return jsonify({"error": "failed"})

# --- START SEQUENCE ---

if __name__ != '__main__':
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
    threading.Thread(target=monitor_profit_and_tighten, daemon=True).start()

if __name__ == "__main__":
    threading.Thread(target=monitor_profit_and_tighten, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
