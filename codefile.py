import os
import time
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.clob_types import OrderType

load_dotenv()

# === CONFIGURATION ===
TARGET_PROXY_ADDRESS = "0x1f0a343513aa6060488fabe96960e6d1e177f7aa".lower()  # The trader to copy
PRIVATE_KEY = os.getenv("private_key")
API_KEY = os.getenv("api_key")
API_SECRET = os.getenv("secret_key")
PASSPHRASE = os.getenv("passphrase")
YOUR_PROXY_ADDRESS = os.getenv("YOUR_WALLET_ADDRESS")  # Your Polymarket proxy/funder address

SLIPPAGE_TOLERANCE = 0.05  # 5% slippage allowed (e.g., for buy: pay up to price * 1.05)
POLL_INTERVAL = 60  # Check for new trades every 60 seconds (safe for rate limits)

host = "https://clob.polymarket.com"
chain_id = 137

creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=PASSPHRASE)

client = ClobClient(
    host=host,
    chain_id=chain_id,
    key=PRIVATE_KEY,          # Must be the exported Polymarket signing key
    creds=creds,              # Your ApiCreds from .env (can be empty strings initially)
    signature_type=2,         # Almost certainly 1
    funder=YOUR_PROXY_ADDRESS # Your Polymarket proxy address (holds USDC)
)

# This line is REQUIRED — it derives/fixes API creds using your private key
client.set_api_creds(client.create_or_derive_api_creds())


print("Copy-trading bot started.")
print(f"Monitoring trades from: {TARGET_PROXY_ADDRESS}")
print(f"Your address: {client.get_address()}")

# Track the latest trade timestamp we've seen
last_timestamp = 0

while True:
    try:
        # Fetch recent activity (trades only) for the target address
        activity_url = "https://data-api.polymarket.com/activity"
        params = {
            "user": TARGET_PROXY_ADDRESS,
            "type": "TRADE",          # Only trades
            "limit": 20,              # Get a few to be safe
            "order": "desc"           # Most recent first
        }
        response = requests.get(activity_url, params=params)
        response.raise_for_status()
        activities = response.json()

        if not activities:
            print("No trades found yet for this address.")
            time.sleep(POLL_INTERVAL)
            continue

        for trade in activities:
            ts = trade["timestamp"]
            if ts <= last_timestamp:
                continue  # Already processed

            print(f"\nNew trade detected! {trade['side']} {trade['size']} shares @ ${trade['price']} ({trade.get('outcome', '')})")

            token_id = trade["asset"]
            condition_id = trade["conditionId"]

            # Get market details to confirm token_id maps to correct outcome (Yes/No)
            market = client.get_market(condition_id=condition_id)
            outcome_token = None
            for t in market["tokens"]:
                if t["token_id"] == token_id:
                    outcome_token = t
                    break

            if not outcome_token:
                print("Could not map token_id to outcome – skipping.")
                continue

            side = BUY if trade["side"] == "BUY" else SELL
            original_price = float(trade["price"])
            size_shares = float(trade["size"])

            # === Safer fixed-risk sizing (since we can't dynamically fetch balances easily) ===
            # We cap each copied trade at 5% of your known ~$950 balance → max ~$47.50 per trade
            # === Safer sizing with $1 minimum order value ===
            trade_usdc_value = size_shares * original_price
            min_order_usd = 1.0  # Polymarket minimum
            max_risk_per_trade_usd = 30  # Your 5% cap

            if trade_usdc_value < min_order_usd:
                print(f"Trade too small (${trade_usdc_value:.2f} < $1 min) — skipping copy.")
                if ts > last_timestamp:
                    last_timestamp = ts
                continue  # Skip to next trade

            copy_usdc_value = min(trade_usdc_value, max_risk_per_trade_usd)
            copy_size = copy_usdc_value / original_price

            print(f"Copying ${copy_usdc_value:.2f} worth → {copy_size:.4f} shares (capped at ${max_risk_per_trade_usd})")

            # === Place limit order with 5% slippage tolerance ===
            if side == BUY:
                limit_price = original_price * (1 + SLIPPAGE_TOLERANCE)
            else:
                limit_price = original_price * (1 - SLIPPAGE_TOLERANCE)

            limit_price = round(limit_price, 4)

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=copy_size,
                side=side
            )

            print(f"Placing limit {side} order at ${limit_price} (original ${original_price})")
            signed_order = client.create_order(order_args)
            post_response = client.post_order(signed_order)
            print("Order submitted:", post_response)

            # Update last seen timestamp to the newest one
            if ts > last_timestamp:
                last_timestamp = ts

        # Small delay if no new trades
        time.sleep(POLL_INTERVAL)

    except Exception as e:
        print(f"Error: {e}")
        time.sleep(POLL_INTERVAL)  # Back off on error
