import websocket
import requests
import json
import time
import os
import hmac
import hashlib
import logging
import threading
from dotenv import load_dotenv

# ================= ENV =================
load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET_KEY")

if not API_KEY or not API_SECRET:
    raise Exception("API keys missing")

BASE_URL = "https://api.india.delta.exchange"
WS_URL = "wss://socket.india.delta.exchange"

# ================= CONFIG =================
SYMBOLS = ["GOOGLXUSD", "AMZNXUSD", "TSLAXUSD", "METAXUSD", "NVDAXUSD", "AAPLXUSD"]

LOT_SIZE = {
    "GOOGLXUSD": 30,
    "AMZNXUSD": 50,
    "TSLAXUSD": 25,
    "METAXUSD": 15,
    "NVDAXUSD": 50,
    "AAPLXUSD": 35
}

DROP_PERCENT = 3
TP_PERCENT = 1.5
TRADE_COOLDOWN = 20
ORDER_REFRESH_INTERVAL = 8
REQUEST_RETRY = 3

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ================= STATE =================
prices = {}
positions = {}
product_ids = {}
open_orders_cache = {}
last_trade = {}
last_trigger_price = {}
active_order_flag = {}
positions_ready = False

lock = threading.Lock()

for s in SYMBOLS:
    prices[s] = None
    positions[s] = None
    open_orders_cache[s] = []
    last_trade[s] = 0
    last_trigger_price[s] = None
    active_order_flag[s] = False

# ================= SIGNATURE =================
def generate_signature(method, path, query="", body=""):
    timestamp = str(int(time.time()))
    message = method + timestamp + path + query + body
    signature = hmac.new(
        API_SECRET.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature, timestamp


# ================= SAFE REQUEST =================
def send_request(method, endpoint, query="", body_dict=None):

    body = ""
    if body_dict:
        body = json.dumps(body_dict, separators=(',', ':'))

    for attempt in range(REQUEST_RETRY):
        try:
            signature, timestamp = generate_signature(method, endpoint, query, body)

            headers = {
                "api-key": API_KEY,
                "timestamp": timestamp,
                "signature": signature,
                "Content-Type": "application/json"
            }

            url = BASE_URL + endpoint + query

            if method == "GET":
                r = requests.get(url, headers=headers, timeout=10)
            elif method == "POST":
                r = requests.post(url, headers=headers, data=body, timeout=10)
            else:
                return None

            return r

        except Exception as e:
            logging.error(f"Request error {attempt+1}: {e}")
            time.sleep(1)

    return None


# ================= LOAD PRODUCTS =================
def load_products():
    logging.info("Loading product IDs...")
    for sym in SYMBOLS:
        r = requests.get(f"{BASE_URL}/v2/products/{sym}")
        product_ids[sym] = r.json()["result"]["id"]
    logging.info("Products Loaded")


# ================= SYNC POSITIONS =================
def sync_positions():
    global positions_ready

    r = send_request("GET", "/v2/positions/margined")

    if not r or r.status_code != 200:
        return

    data = r.json().get("result", [])

    for s in SYMBOLS:
        positions[s] = None

    for pos in data:
        for sym in SYMBOLS:
            if product_ids[sym] == pos["product_id"]:
                positions[sym] = pos

    positions_ready = True


# ================= OPEN ORDER CACHE =================
def fetch_open_orders_loop():
    while True:
        try:
            r = send_request("GET", "/v2/orders", "?state=open")

            if r and r.status_code == 200:
                data = r.json().get("result", [])

                for sym in SYMBOLS:
                    open_orders_cache[sym] = [
                        o for o in data
                        if o["product_id"] == product_ids[sym]
                        and o["side"] == "sell"
                        and o["reduce_only"] is True
                        and o["state"] in ["open", "pending"]
                    ]

        except Exception as e:
            logging.error(f"Order cache error: {e}")

        time.sleep(ORDER_REFRESH_INTERVAL)


# ================= TP ORDER =================
def place_reduce_only_tp(sym, size):

    live_price = prices.get(sym)
    if not live_price:
        return

    tp_price = round(live_price * (1 + TP_PERCENT / 100), 4)

    # prevent duplicate TP at same price
    for order in open_orders_cache.get(sym, []):
        if float(order.get("limit_price", 0)) == tp_price:
            return

    body_data = {
        "product_id": product_ids[sym],
        "size": size,
        "side": "sell",
        "order_type": "limit_order",
        "limit_price": str(tp_price),
        "time_in_force": "gtc",
        "reduce_only": True
    }

    r = send_request("POST", "/v2/orders", body_dict=body_data)

    if r and r.status_code == 200 and r.json().get("success"):
        logging.info(f"{sym} TP placed at {tp_price}")


# ================= PLACE LONG =================
def place_long(sym):

    lot = LOT_SIZE[sym]
    entry = prices.get(sym)

    if not entry:
        return False

    body_data = {
        "product_id": product_ids[sym],
        "size": lot,
        "side": "buy",
        "order_type": "market_order",
        "time_in_force": "ioc",
        "reduce_only": False
    }

    r = send_request("POST", "/v2/orders", body_dict=body_data)

    if r and r.status_code == 200 and r.json().get("success"):

        logging.info(f"{sym} LONG executed")

        time.sleep(1)
        sync_positions()

        pos = positions.get(sym)
        if pos:
            size = abs(float(pos["size"]))
            place_reduce_only_tp(sym, size)

        return True

    return False


# ================= TRADE LOGIC =================
def trade_logic(sym):

    with lock:

        if not positions_ready:
            return

        if prices[sym] is None:
            return

        if active_order_flag[sym]:
            return

        if time.time() - last_trade[sym] < TRADE_COOLDOWN:
            return

        pos = positions.get(sym)

        # FIRST ENTRY
        if not pos or float(pos.get("size", 0)) == 0:

            if open_orders_cache[sym]:
                return

            active_order_flag[sym] = True
            if place_long(sym):
                last_trade[sym] = time.time()
            active_order_flag[sym] = False
            return

        # LADDER ENTRY
        lowest_sell = None
        prices_list = [
            float(o["limit_price"])
            for o in open_orders_cache.get(sym, [])
            if o.get("limit_price")
        ]

        if prices_list:
            lowest_sell = min(prices_list)

        if not lowest_sell:
            return

        trigger = lowest_sell * (1 - DROP_PERCENT / 100)

        if last_trigger_price[sym] == trigger:
            return

        if prices[sym] <= trigger:

            active_order_flag[sym] = True

            if place_long(sym):
                last_trade[sym] = time.time()
                last_trigger_price[sym] = trigger

            active_order_flag[sym] = False


# ================= DASHBOARD =================
def dashboard_loop():
    while True:
        time.sleep(5)
        os.system("clear")

        print("=" * 100)
        print("🔥 DELTA LONG ENGINE - PRODUCTION DASHBOARD 🔥")
        print("=" * 100)

        total_pnl = 0
        total_exposure = 0

        for sym in SYMBOLS:

            price = prices.get(sym)
            pos = positions.get(sym)

            print(f"\n{sym}")
            print("-" * 70)

            print(f"Mark Price        : {price}")

            # Calculate lowest reduce-only sell
            lowest_sell = None
            sell_prices = [
                float(o["limit_price"])
                for o in open_orders_cache.get(sym, [])
                if o.get("limit_price")
            ]

            if sell_prices:
                lowest_sell = min(sell_prices)

            trigger = None
            if lowest_sell:
                trigger = round(lowest_sell * (1 - DROP_PERCENT / 100), 4)

            live_tp = None
            if price:
                live_tp = round(price * (1 + TP_PERCENT / 100), 4)

            print(f"Lowest SELL TP    : {lowest_sell}")
            print(f"Next Trigger      : {trigger}")
            print(f"Live TP (new)     : {live_tp}")

            if not pos or not price:
                print("Position          : None")
                continue

            size = float(pos["size"])
            entry = float(pos["entry_price"])

            pnl = (price - entry) * abs(size)
            exposure = abs(size) * price

            total_pnl += pnl
            total_exposure += exposure

            print(f"Direction         : LONG")
            print(f"Position Size     : {size}")
            print(f"Entry Price       : {entry}")
            print(f"Unrealized PnL    : {round(pnl,4)}")
            print(f"Current Exposure  : {round(exposure,4)}")

        print("\n" + "=" * 100)
        print("PORTFOLIO SUMMARY")
        print("=" * 100)
        print(f"Total Unrealized PnL : {round(total_pnl,4)}")
        print(f"Total Exposure       : {round(total_exposure,4)}")
        print("=" * 100)


# ================= WEBSOCKET =================
def on_open(ws):

    signature, timestamp = generate_signature("GET", "/live")

    ws.send(json.dumps({
        "type": "auth",
        "payload": {
            "api-key": API_KEY,
            "signature": signature,
            "timestamp": timestamp
        }
    }))

    time.sleep(1)

    ws.send(json.dumps({
        "type": "subscribe",
        "payload": {
            "channels": [
                {"name": "v2/ticker", "symbols": SYMBOLS},
                {"name": "positions"}
            ]
        }
    }))


def on_message(ws, message):

    data = json.loads(message)

    if "symbol" in data and "mark_price" in data:
        sym = data["symbol"]
        if sym in SYMBOLS:
            prices[sym] = float(data["mark_price"])
            trade_logic(sym)
        return

    if data.get("type") == "positions":
        for p in data.get("result", []):
            for sym in SYMBOLS:
                if product_ids[sym] == p["product_id"]:
                    positions[sym] = p


def start_ws():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message
            )
            ws.run_forever()
        except Exception as e:
            logging.error(f"WS error: {e}")
            time.sleep(5)


# ================= MAIN =================
if __name__ == "__main__":

    load_products()
    sync_positions()

    threading.Thread(target=fetch_open_orders_loop, daemon=True).start()
    threading.Thread(target=dashboard_loop, daemon=True).start()

    start_ws()
