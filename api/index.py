from flask import Flask, request, jsonify
import requests
import hmac
import hashlib
import time
from urllib.parse import urlencode

app = Flask(__name__)

# --- BINANCE TESTNET API CREDENTIALS ---
API_KEY = 'tC0y8c5M66fquyCEl66Ofs8Tnvts7plvHnxAugvw2rpMaIgcTGVtKkATvwvSzTUs'
API_SECRET = 'SmPpCEon9C9q2K22k7FMLKxoQxN5WaXMDn9yrCsshpI30NxOXzP7m0df3HWwekzA'

# 🚨 STRICTLY LOCKED TO BINANCE DEMO SERVER 🚨
BASE_URL = 'https://testnet.binance.vision' 

@app.route('/', methods=['GET'])
def home():
    return "Binance Testnet Webhook is Live!"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # 1. Catch the JSON from TradingView
        tv_data = request.json
        print(f"✅ ALERT RECEIVED FROM TV: {tv_data}")

        symbol = tv_data.get('symbol')
        side = tv_data.get('side').upper()
        qty = tv_data.get('qty')

        # 2. Build the Binance Testnet Order
        endpoint = '/api/v3/order'
        url = BASE_URL + endpoint

        params = {
            'symbol': symbol,
            'side': side,
            'type': 'MARKET',
            'quantity': qty,
            'timestamp': int(time.time() * 1000)
        }

        # 3. Create the Signature
        query_string = urlencode(params)
        signature = hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
        params['signature'] = signature

        # 4. Send to Binance Testnet
        headers = {
            'X-MBX-APIKEY': API_KEY
        }
        
        response = requests.post(url, headers=headers, params=params)
        
        # 5. Log the result to Vercel
        print(f"🔄 TESTNET RESPONSE: {response.status_code} - {response.text}")
        
        return jsonify({"status": "success", "binance_response": response.json()}), response.status_code

    except Exception as e:
        print(f"❌ ERROR: {e}")
        return jsonify({"error": str(e)}), 400
