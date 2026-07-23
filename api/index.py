import os
import time
import hmac
import hashlib
from urllib.parse import urlencode

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- BINANCE DEMO TRADING CREDENTIALS ---
# Set these as Vercel Environment Variables (Project Settings -> Environment Variables).
# Do NOT hardcode them here -- this repo is public.
API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# STRICTLY LOCKED TO BINANCE DEMO TRADING SERVER
# This is Binance's current "Demo Trading" REST base (demo.binance.com account/keys),
# NOT the older testnet.binancefuture.com Futures Testnet -- they use different keys.
BASE_URL = "https://demo-fapi.binance.com"

# Fallback price rounding if a symbol isn't listed below (Binance rejects prices
# that don't match the tick size). Add symbols here as you trade them.
PRICE_DECIMALS = {
    "BTCUSDT": 1,
    "ETHUSDT": 2,
}


def round_price(symbol, price):
    decimals = PRICE_DECIMALS.get(symbol, 2)
    return round(price, decimals)


def signed_request(method, endpoint, params=None):
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query_string = urlencode(params)
    signature = hmac.new(
        API_SECRET.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    params["signature"] = signature
    headers = {"X-MBX-APIKEY": API_KEY}
    url = BASE_URL + endpoint

    if method == "GET":
        resp = requests.get(url, headers=headers, params=params)
    elif method == "POST":
        resp = requests.post(url, headers=headers, params=params)
    elif method == "DELETE":
        resp = requests.delete(url, headers=headers, params=params)
    else:
        raise ValueError(f"Unsupported method: {method}")

    return resp


def get_position(symbol):
    """Returns (position_amt, entry_price) as floats. position_amt > 0 = long, < 0 = short, 0 = flat."""
    resp = signed_request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
    data = resp.json()
    if not isinstance(data, list) or len(data) == 0:
        return 0.0, 0.0
    row = data[0]
    return float(row.get("positionAmt", 0)), float(row.get("entryPrice", 0))


def cancel_open_orders(symbol):
    return signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})


def place_market_order(symbol, side, qty, reduce_only=False):
    params = {
        "symbol": symbol,
        "side": side,
        "type": "MARKET",
        "quantity": qty,
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return signed_request("POST", "/fapi/v1/order", params)


def place_sl_tp(symbol, position_amt, entry_price, sl_dollar, tp_dollar):
    """Places closePosition STOP_MARKET + TAKE_PROFIT_MARKET orders sized off the
    actual open position (not off the qty of any single leg), so a 2nd entry
    that changes the average price/size is handled correctly."""
    if position_amt == 0:
        return {"skipped": "flat"}

    is_long = position_amt > 0
    abs_qty = abs(position_amt)
    price_offset_sl = sl_dollar / abs_qty
    price_offset_tp = tp_dollar / abs_qty

    if is_long:
        stop_price = entry_price - price_offset_sl
        target_price = entry_price + price_offset_tp
        exit_side = "SELL"
    else:
        stop_price = entry_price + price_offset_sl
        target_price = entry_price - price_offset_tp
        exit_side = "BUY"

    stop_price = round_price(symbol, stop_price)
    target_price = round_price(symbol, target_price)

    sl_resp = signed_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": symbol,
            "side": exit_side,
            "type": "STOP_MARKET",
            "stopPrice": stop_price,
            "closePosition": "true",
        },
    )
    tp_resp = signed_request(
        "POST",
        "/fapi/v1/order",
        {
            "symbol": symbol,
            "side": exit_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": target_price,
            "closePosition": "true",
        },
    )
    return {
        "stop_price": stop_price,
        "target_price": target_price,
        "sl_response": sl_resp.json(),
        "tp_response": tp_resp.json(),
    }


@app.route("/", methods=["GET"])
def home():
    return "Binance Futures Testnet Webhook is Live!"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        tv_data = request.json
        print(f"ALERT RECEIVED FROM TV: {tv_data}")

        action = tv_data.get("action", "ENTRY").upper()
        symbol = tv_data.get("symbol")

        if action == "CLOSE":
            cancel_open_orders(symbol)
            position_amt, _ = get_position(symbol)
            if position_amt == 0:
                return jsonify({"status": "success", "action": "CLOSE", "note": "already flat"}), 200
            close_side = "SELL" if position_amt > 0 else "BUY"
            close_resp = place_market_order(symbol, close_side, abs(position_amt), reduce_only=True)
            return jsonify(
                {"status": "success", "action": "CLOSE", "binance_response": close_resp.json()}
            ), close_resp.status_code

        elif action == "ENTRY":
            side = tv_data.get("side").upper()
            qty = tv_data.get("qty")
            sl_dollar = float(tv_data.get("sl_dollar", 50))
            tp_dollar = float(tv_data.get("tp_dollar", 150))

            # Clear any stale SL/TP from a prior leg before placing new ones
            cancel_open_orders(symbol)

            entry_resp = place_market_order(symbol, side, qty)
            if entry_resp.status_code >= 400:
                print(f"ENTRY ORDER FAILED: {entry_resp.status_code} - {entry_resp.text}")
                return jsonify({"status": "error", "binance_response": entry_resp.json()}), entry_resp.status_code

            # Give Binance a moment to update position risk, then read the
            # authoritative average entry price / size and place SL+TP off that.
            time.sleep(0.5)
            position_amt, entry_price = get_position(symbol)
            risk_result = place_sl_tp(symbol, position_amt, entry_price, sl_dollar, tp_dollar)

            return jsonify(
                {
                    "status": "success",
                    "action": "ENTRY",
                    "binance_response": entry_resp.json(),
                    "position_amt": position_amt,
                    "entry_price": entry_price,
                    "risk_orders": risk_result,
                }
            ), 200

        else:
            return jsonify({"error": f"unknown action: {action}"}), 400

    except Exception as e:
        print(f"ERROR: {e}")
        return jsonify({"error": str(e)}), 400
