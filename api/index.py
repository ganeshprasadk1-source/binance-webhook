from flask import Flask, request, jsonify
import requests
import hmac
import hashlib
import time
from urllib.parse import urlencode

app = Flask(__name__)

# --- BINANCE FUTURES TESTNET CREDENTIALS ---
API_KEY = 'v2ZErBRJoPuLBVkUXk47Nmbc5rqvtGqj0UYRW2oST0E4yi3Uvj24n85guomc9G4S'
API_SECRET = 'JLyymvnseCluoKuBkgip6ekfh6DtZn52mRaCPwoIlITD2mo2T0Q9hn44FikIrCL1'

# 🚨 STRICTLY LOCKED TO BINANCE FUTURES DEMO SERVER 🚨
BASE_URL = 'https://testnet.binancefuture.com' 

@app.route('/', methods=['GET'])
def home():
    return "Binance Futures Testnet Webhook is Live!"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # 1. Catch the JSON from TradingView
        tv_data = request.json
        print(f"✅ ALERT RECEIVED FROM TV: {tv_data}")

        symbol = tv_data.get('symbol')
        side = tv_data.get('side').upper()
        qty = tv_data.get('qty')

        # 2. Build the Binance Futures Order (Endpoint is /fapi/v1/order)
        endpoint = '/fapi/v1/order'
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

        # 4. Send to Binance Futures Testnet
        headers = {
            'X-MBX-APIKEY': API_KEY
        }
        
        response = requests.post(url, headers=headers, params=params)
        
        # 5. Log the result to Vercel
        print(f"🔄 FUTURES TESTNET RESPONSE: {response.status_code} - {response.text}")
        
        return jsonify({"status": "success", "binance_response": response.json()}), response.status_code

    except Exception as e:
        print(f"❌ ERROR: {e}")
        return jsonify({"error": str(e)}), 400
