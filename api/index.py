import os
import time
import hmac
import hashlib
from datetime import datetime, timezone
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

# Quantity precision per symbol (Binance LOT_SIZE stepSize). Pine's str.tostring()
# can send quantities with tiny floating-point noise (e.g. 0.010000000000000002)
# which Binance rejects outright with a 400 -- so we always re-round server-side
# before an order ever goes out, regardless of what Pine sent.
QTY_DECIMALS = {
    "BTCUSDT": 3,
    "ETHUSDT": 3,
}

# Safety ceiling per entry leg. This is intentionally small -- meant to catch a
# fat-fingered Pine input (e.g. 1 instead of 0.01) before it ever reaches
# Binance, not to be a real position-sizing limit. Raise these deliberately if
# you actually want to trade bigger size.
MAX_QTY = {
    "BTCUSDT": 0.05,
    "ETHUSDT": 0.5,
}


def round_price(symbol, price):
    decimals = PRICE_DECIMALS.get(symbol, 2)
    return round(price, decimals)


def format_qty(symbol, qty):
    """Returns a clean fixed-decimal string, e.g. '0.010', never scientific
    notation or long floating-point tails."""
    decimals = QTY_DECIMALS.get(symbol, 3)
    return f"{round(float(qty), decimals):.{decimals}f}"


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
        "quantity": format_qty(symbol, qty),
    }
    if reduce_only:
        params["reduceOnly"] = "true"
    return signed_request("POST", "/fapi/v1/order", params)


def place_protective_orders(symbol, position_amt, entry_price, sl_dollar, tp_dollar):
    """Places a full-size closePosition STOP_MARKET (the $75 stop), plus a
    half-size TAKE_PROFIT_MARKET with an exact quantity (NOT closePosition).
    That second order executes automatically on Binance's own engine the
    instant price hits it -- it doesn't wait for a TradingView alert. Once it
    fires, Pine detects the same event on its next bar close and sends a
    PARTIAL_TP notification so we can move the remaining stop up to lock it."""
    if position_amt == 0:
        return {"skipped": "flat"}

    is_long = position_amt > 0
    abs_qty = abs(position_amt)
    half_qty = format_qty(symbol, abs_qty / 2)
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
            "quantity": half_qty,
            "reduceOnly": "true",
        },
    )
    return {
        "stop_price": stop_price,
        "target_price": target_price,
        "half_qty": half_qty,
        "sl_response": sl_resp.json(),
        "tp_response": tp_resp.json(),
    }


def get_income_history(symbol, start_time_ms, end_time_ms):
    """Pages through Binance's income ledger (realized PnL, commission,
    funding fees) between two timestamps. Capped at 10 pages (10,000
    records) as a sanity limit -- plenty for a multi-day report."""
    all_records = []
    cursor = start_time_ms
    for _ in range(10):
        resp = signed_request(
            "GET",
            "/fapi/v1/income",
            {"symbol": symbol, "startTime": cursor, "endTime": end_time_ms, "limit": 1000},
        )
        page = resp.json()
        if not isinstance(page, list) or len(page) == 0:
            break
        all_records.extend(page)
        if len(page) < 1000:
            break
        cursor = page[-1]["time"] + 1
    return all_records


@app.route("/", methods=["GET"])
def home():
    key_status = f"...{API_KEY[-4:]}" if API_KEY else "NOT SET"
    secret_status = "set" if API_SECRET else "NOT SET"
    return (
        "Binance Demo Trading Webhook is Live! "
        f"| API key ends: {key_status} | API secret: {secret_status}"
    )


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
            sl_dollar = float(tv_data.get("sl_dollar", 75))
            tp_dollar = float(tv_data.get("tp_dollar", 150))

            max_qty = MAX_QTY.get(symbol)
            if max_qty is not None and float(qty) > max_qty:
                msg = f"Rejected: qty {qty} exceeds safety cap of {max_qty} for {symbol}. Raise MAX_QTY in index.py if this is intentional."
                print(f"ENTRY BLOCKED BY SAFETY CAP: {msg}")
                return jsonify({"status": "error", "error": msg}), 400

            # Clear any stale SL/TP from a prior leg before placing new ones
            cancel_open_orders(symbol)

            print(f"PLACING ENTRY: symbol={symbol} side={side} raw_qty={qty} formatted_qty={format_qty(symbol, qty)}")
            entry_resp = place_market_order(symbol, side, qty)
            if entry_resp.status_code >= 400:
                print(f"ENTRY ORDER FAILED: {entry_resp.status_code} - {entry_resp.text}")
                return jsonify({"status": "error", "binance_response": entry_resp.json()}), entry_resp.status_code

            # Give Binance a moment to update position risk, then read the
            # authoritative average entry price / size and place SL+TP off that.
            time.sleep(0.5)
            position_amt, entry_price = get_position(symbol)
            risk_result = place_protective_orders(symbol, position_amt, entry_price, sl_dollar, tp_dollar)

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

        elif action == "PARTIAL_TP":
            # The native half-size TAKE_PROFIT_MARKET order (placed in
            # place_protective_orders at ENTRY time) already executed the
            # actual partial close on Binance. This alert just tells us to
            # now move the remaining stop up to lock that level in.
            target_dollar = float(tv_data.get("target_dollar", 150))
            position_amt, entry_price = get_position(symbol)
            if position_amt == 0:
                return jsonify({"status": "success", "action": "PARTIAL_TP", "note": "already flat"}), 200

            is_long = position_amt > 0
            remaining_qty = abs(position_amt)
            # remaining_qty is ~half of the original size, so target_dollar/2
            # divided by remaining_qty reproduces the exact price level where
            # the ORIGINAL full position would have made target_dollar.
            lock_offset = (target_dollar / 2) / remaining_qty
            lock_price = entry_price + lock_offset if is_long else entry_price - lock_offset
            lock_price = round_price(symbol, lock_price)
            exit_side = "SELL" if is_long else "BUY"

            cancel_open_orders(symbol)
            stop_resp = signed_request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": exit_side,
                    "type": "STOP_MARKET",
                    "stopPrice": lock_price,
                    "closePosition": "true",
                },
            )
            print(f"PARTIAL_TP: locked stop at {lock_price} for remaining_qty={remaining_qty}")
            return jsonify(
                {
                    "status": "success",
                    "action": "PARTIAL_TP",
                    "remaining_qty": remaining_qty,
                    "entry_price": entry_price,
                    "lock_price": lock_price,
                    "stop_response": stop_resp.json(),
                }
            ), 200

        elif action == "TRAIL_STEP":
            # Moves the existing locked stop forward by another increment,
            # based on whatever the CURRENT resting stop price already is --
            # so Binance's own open order is the persisted state, and this
            # webhook doesn't need to remember how many steps happened before.
            step_dollar = float(tv_data.get("step_dollar", 100))
            position_amt, entry_price = get_position(symbol)
            if position_amt == 0:
                return jsonify({"status": "success", "action": "TRAIL_STEP", "note": "already flat"}), 200

            is_long = position_amt > 0
            remaining_qty = abs(position_amt)
            trail_increment = (step_dollar / 2) / remaining_qty

            open_orders_resp = signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol})
            open_orders = open_orders_resp.json()
            stop_orders = [o for o in open_orders if o.get("type") == "STOP_MARKET"]
            base_price = float(stop_orders[0]["stopPrice"]) if stop_orders else entry_price

            new_stop = base_price + trail_increment if is_long else base_price - trail_increment
            new_stop = round_price(symbol, new_stop)
            exit_side = "SELL" if is_long else "BUY"

            cancel_open_orders(symbol)
            stop_resp = signed_request(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": symbol,
                    "side": exit_side,
                    "type": "STOP_MARKET",
                    "stopPrice": new_stop,
                    "closePosition": "true",
                },
            )
            print(f"TRAIL_STEP: moved stop {base_price} -> {new_stop}")
            return jsonify(
                {
                    "status": "success",
                    "action": "TRAIL_STEP",
                    "old_stop": base_price,
                    "new_stop": new_stop,
                    "stop_response": stop_resp.json(),
                }
            ), 200

        else:
            return jsonify({"error": f"unknown action: {action}"}), 400

    except Exception as e:
        print(f"ERROR: {e}")
        return jsonify({"error": str(e)}), 400


@app.route("/report", methods=["GET"])
def report():
    """Visit e.g. /report?symbol=BTCUSDT&days=4 in a browser. Pulls realized
    PnL / commission / funding directly from Binance's own income ledger --
    the real, authoritative numbers, not a reconstruction from trade history.
    Demo Trading keeps this data for the same account as your live trades."""
    try:
        symbol = request.args.get("symbol", "BTCUSDT").upper()
        days = float(request.args.get("days", "4"))
        end_time_ms = int(time.time() * 1000)
        start_time_ms = end_time_ms - int(days * 86400 * 1000)

        records = get_income_history(symbol, start_time_ms, end_time_ms)

        realized = [r for r in records if r.get("incomeType") == "REALIZED_PNL"]
        commission = [r for r in records if r.get("incomeType") == "COMMISSION"]
        funding = [r for r in records if r.get("incomeType") == "FUNDING_FEE"]

        total_realized = sum(float(r["income"]) for r in realized)
        total_commission = sum(float(r["income"]) for r in commission)
        total_funding = sum(float(r["income"]) for r in funding)
        net_pnl = total_realized + total_commission + total_funding

        wins = [r for r in realized if float(r["income"]) > 0]
        losses = [r for r in realized if float(r["income"]) < 0]
        win_rate = (len(wins) / len(realized) * 100) if realized else 0.0

        recent = sorted(realized, key=lambda r: r["time"], reverse=True)[:25]
        rows_html = ""
        for r in recent:
            ts = datetime.fromtimestamp(r["time"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            amt = float(r["income"])
            color = "green" if amt >= 0 else "crimson"
            rows_html += f"<tr><td>{ts}</td><td>{r.get('symbol', '')}</td><td style='color:{color}'>{amt:+.4f} USDT</td></tr>"

        realized_color = "green" if total_realized >= 0 else "crimson"
        net_color = "green" if net_pnl >= 0 else "crimson"

        start_str = datetime.fromtimestamp(start_time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        end_str = datetime.fromtimestamp(end_time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if realized:
            oldest_str = datetime.fromtimestamp(min(r["time"] for r in realized) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            oldest_note = f"Oldest realized-PnL event actually found: {oldest_str}"
        else:
            oldest_note = "No realized-PnL events found in this window."

        html = f"""
        <html><head><title>Performance Report</title></head>
        <body style="font-family: -apple-system, Arial, sans-serif; padding: 24px; max-width: 700px;">
        <h2>Performance Report -- {symbol}</h2>
        <p>Queried range: {start_str} &rarr; {end_str} ({days:g} day(s) requested)</p>
        <p>{oldest_note}</p>
        <table cellpadding="6" style="border-collapse: collapse;">
          <tr><td>Realized PnL</td><td style="color:{realized_color}">{total_realized:+.4f} USDT</td></tr>
          <tr><td>Commission paid</td><td style="color:crimson">{total_commission:.4f} USDT</td></tr>
          <tr><td>Funding fees</td><td>{total_funding:+.4f} USDT</td></tr>
          <tr><td><b>Net PnL</b></td><td style="color:{net_color}"><b>{net_pnl:+.4f} USDT</b></td></tr>
          <tr><td>Realized-PnL events</td><td>{len(realized)} total &mdash; {len(wins)} win / {len(losses)} loss ({win_rate:.1f}% win rate)</td></tr>
        </table>
        <h3>Most recent {len(recent)} realized-PnL events</h3>
        <table border="1" cellpadding="6" style="border-collapse: collapse;">
          <tr><th>Time</th><th>Symbol</th><th>Realized PnL</th></tr>
          {rows_html}
        </table>
        </body></html>
        """
        return html, 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400
