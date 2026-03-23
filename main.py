"""
DeFi Stablecoin Depeg Arbitrage Monitor
Monitors stablecoin prices and detects profitable arbitrage opportunities
Includes a keepalive HTTP server and automatic crash recovery
"""

import os
import requests
import time
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# =============================================================================
# CONFIGURATION — Edit these values to customize behavior
# =============================================================================

# Coins to monitor (CoinGecko IDs)
COINS = {
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI":  "dai",
    "FRAX": "frax",
    "LUSD": "liquity-usd",
}

# DEX Screener token addresses (used as fallback)
DEXSCREENER_ADDRESSES = {
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "DAI":  "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "FRAX": "0x853d955aCEf822Db058eb8505911ED77F175b99e",
    "LUSD": "0x5f98805A4E8be255a32880FDeC7F6728C6568bA0",
}

# Threshold: minimum price gap from $1.00 to consider (as a decimal, 0.003 = 0.3%)
GAP_THRESHOLD = 0.003

# Minimum net profit (USD) required to display as OPPORTUNITY
MIN_PROFIT = 50.0

# Flash loan sizes to calculate profit for (in USD)
LOAN_SIZES = [100_000, 500_000, 1_000_000]

# Aave flash loan fee (0.09% = 0.0009)
AAVE_FEE_RATE = 0.0009

# Estimated gas fee on Arbitrum (USD)
GAS_FEE = 2.0

# How often to check prices (seconds)
POLL_INTERVAL = 30

# File to save opportunities
LOG_FILE = "opportunities_log.txt"

# Port for the keepalive HTTP server.
# Free hosting platforms (Railway, Render, Fly.io) inject a PORT env var.
# In local/Replit dev we default to 8000 to avoid conflicting with other services.
KEEPALIVE_PORT = int(os.environ.get("PORT", 8000))

# =============================================================================
# KEEPALIVE HTTP SERVER — Runs in a background thread
# Replit deployments require an HTTP server to stay alive
# =============================================================================

# Track bot status globally so the health endpoint can report it
_bot_status = {"running": True, "cycles": 0, "last_cycle": None, "opportunities_today": 0}


class KeepaliveHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that returns bot status for health checks."""

    def do_GET(self):
        status = _bot_status
        body = (
            f"Bot is running\n"
            f"Cycles completed: {status['cycles']}\n"
            f"Last cycle: {status['last_cycle'] or 'not yet'}\n"
            f"Opportunities today: {status['opportunities_today']}\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress default access log spam in console
        pass


def start_keepalive_server():
    """Start the keepalive HTTP server in a daemon thread."""
    server = HTTPServer(("0.0.0.0", KEEPALIVE_PORT), KeepaliveHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[Keepalive] HTTP server listening on port {KEEPALIVE_PORT}")


# =============================================================================
# PRICE FETCHING — Two sources with fallback logic
# =============================================================================

def fetch_coingecko_prices(coin_ids: dict) -> dict:
    """
    Fetch USD prices from the CoinGecko free API (no key required).
    Returns a dict mapping coin symbol -> price, or empty dict on failure.
    """
    try:
        ids_str = ",".join(coin_ids.values())
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": ids_str, "vs_currencies": "usd"}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        prices = {}
        for symbol, cg_id in coin_ids.items():
            if cg_id in data and "usd" in data[cg_id]:
                prices[symbol] = float(data[cg_id]["usd"])
        return prices

    except Exception as e:
        print(f"[Warning] CoinGecko fetch failed: {e}")
        return {}


def fetch_dexscreener_price(address: str) -> float | None:
    """
    Fetch USD price for a single token address from DEX Screener free API.
    Returns the price as float, or None on failure.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        # DEX Screener returns a list of pairs; take the first pair's price
        pairs = data.get("pairs", [])
        if not pairs:
            return None

        # Sort by liquidity to get the most reliable price
        pairs_with_liquidity = [
            p for p in pairs if p.get("liquidity", {}).get("usd", 0) > 0
        ]
        if pairs_with_liquidity:
            pairs_sorted = sorted(
                pairs_with_liquidity,
                key=lambda p: p.get("liquidity", {}).get("usd", 0),
                reverse=True,
            )
            price_str = pairs_sorted[0].get("priceUsd")
        else:
            price_str = pairs[0].get("priceUsd")

        if price_str is not None:
            return float(price_str)
        return None

    except Exception as e:
        print(f"[Warning] DEX Screener fetch failed for {address}: {e}")
        return None


def fetch_all_prices() -> dict:
    """
    Fetch prices using CoinGecko first, fall back to DEX Screener for any
    coins that CoinGecko failed to return.
    Returns a dict mapping coin symbol -> price.
    """
    prices = fetch_coingecko_prices(COINS)

    # For any coin missing from CoinGecko, try DEX Screener
    for symbol, address in DEXSCREENER_ADDRESSES.items():
        if symbol not in prices:
            dex_price = fetch_dexscreener_price(address)
            if dex_price is not None:
                prices[symbol] = dex_price

    return prices


# =============================================================================
# ARBITRAGE CALCULATION
# =============================================================================

def calculate_profit(price: float, loan_size: float) -> float:
    """
    Calculate net profit for a flash loan arbitrage trade.

    Strategy: buy the depegged stablecoin cheap, redeem at $1.00.
    - Buy `loan_size` USD worth of the stablecoin at current price
    - Redeem the tokens at $1.00 peg
    - Subtract Aave flash loan fee and gas cost

    Returns net profit in USD (can be negative if unprofitable).
    """
    # Number of tokens purchased at the depegged price
    tokens = loan_size / price

    # Revenue: redeem tokens at $1.00
    revenue = tokens * 1.00

    # Flash loan repayment: original loan + Aave fee
    repayment = loan_size * (1 + AAVE_FEE_RATE)

    # Net profit after flash loan repayment and gas
    net_profit = revenue - repayment - GAS_FEE

    return net_profit


# =============================================================================
# LOGGING
# =============================================================================

def log_opportunity(symbol: str, price: float, gap_pct: float, profits: dict):
    """
    Append an opportunity record to the log file with timestamp.
    """
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a") as f:
            f.write(f"\n{'='*50}\n")
            f.write(f"OPPORTUNITY LOGGED: {timestamp}\n")
            f.write(f"Coin:  {symbol}\n")
            f.write(f"Price: ${price:.4f}\n")
            f.write(f"Gap:   {gap_pct:+.2f}%\n")
            for loan_size, profit in profits.items():
                label = f"${loan_size // 1000}k" if loan_size < 1_000_000 else "$1M"
                f.write(f"  {label} loan -> profit: ${profit:,.0f}\n")
            f.write(f"{'='*50}\n")
    except Exception as e:
        print(f"[Warning] Could not write to log file: {e}")


# =============================================================================
# DISPLAY DASHBOARD
# =============================================================================

def print_dashboard(prices: dict, opportunities: list):
    """
    Print the formatted dashboard to the console.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\n============ DeFi Arb Monitor ============")
    print(f"Time: {now}")
    print("------------------------------------------")

    for symbol in COINS:
        if symbol not in prices:
            print(f"{symbol:<5}: [price unavailable]")
            continue

        price = prices[symbol]
        gap = price - 1.00
        gap_pct = gap / 1.00 * 100
        gap_str = f"{gap_pct:+.2f}%"

        # Find if this coin has an opportunity
        has_opp = any(o["symbol"] == symbol for o in opportunities)
        status = "[OPPORTUNITY!]" if has_opp else "[NO TRADE]"

        print(f"{symbol:<5}: ${price:.4f}  Gap: {gap_str:<8}  {status}")

    print("------------------------------------------")

    if opportunities:
        for opp in opportunities:
            print("OPPORTUNITY FOUND:")
            print(f"  Coin:      {opp['symbol']}")
            print(f"  Buy at:    ${opp['price']:.4f}")
            print(f"  Redeem at: $1.0000")
            print(f"  Gap:       {abs(opp['gap_pct']):.2f}%")
            for loan_size, profit in opp["profits"].items():
                if loan_size < 1_000_000:
                    label = f"${loan_size // 1000}k"
                else:
                    label = "$1M"
                print(f"  {label:<8} loan  -> profit: ${profit:,.0f} (after fees)")
    else:
        print("No profitable opportunities this cycle.")

    print("==========================================")


# =============================================================================
# MAIN LOOP — with automatic crash recovery
# =============================================================================

def run_monitor():
    """
    Main monitoring loop. Runs indefinitely, fetching prices every POLL_INTERVAL
    seconds and evaluating arbitrage opportunities.
    If an unexpected error escapes the inner handler, it logs it and restarts
    after a 10-second pause — the program never fully exits.
    """
    print("Starting DeFi Stablecoin Depeg Arbitrage Monitor...")
    print(f"Monitoring: {', '.join(COINS.keys())}")
    print(f"Gap threshold: {GAP_THRESHOLD * 100:.2f}%")
    print(f"Min profit threshold: ${MIN_PROFIT:,.0f}")
    print(f"Loan sizes: {[f'${s:,}' for s in LOAN_SIZES]}")
    print(f"Poll interval: {POLL_INTERVAL}s\n")

    # Track opportunities found today for the status page
    today_date = datetime.now().date()

    # Outer restart loop — if anything escapes the inner try/except, restart
    while True:
        try:
            # Inner monitoring loop
            while True:
                try:
                    # Reset daily counter when date changes
                    if datetime.now().date() != today_date:
                        today_date = datetime.now().date()
                        _bot_status["opportunities_today"] = 0

                    # --- Fetch prices from both sources ---
                    prices = fetch_all_prices()

                    if not prices:
                        print("[Warning] No prices fetched this cycle. Retrying next interval.")
                        time.sleep(POLL_INTERVAL)
                        continue

                    # --- Evaluate each coin for opportunities ---
                    opportunities = []

                    for symbol in COINS:
                        if symbol not in prices:
                            continue

                        price = prices[symbol]
                        gap = price - 1.00
                        gap_pct = gap / 1.00 * 100
                        abs_gap = abs(gap)

                        # Only consider if gap exceeds threshold
                        if abs_gap < GAP_THRESHOLD:
                            continue

                        # Only consider depeg below $1.00 (buy cheap, redeem at $1)
                        # If price > $1.00, the arbitrage direction is reversed and more
                        # complex; skip for now as redemption needs protocol-specific logic
                        if price >= 1.00:
                            continue

                        # Calculate profits for each loan size
                        profits = {}
                        any_profitable = False
                        for loan_size in LOAN_SIZES:
                            profit = calculate_profit(price, loan_size)
                            profits[loan_size] = profit
                            if profit >= MIN_PROFIT:
                                any_profitable = True

                        if any_profitable:
                            opportunities.append({
                                "symbol": symbol,
                                "price": price,
                                "gap_pct": gap_pct,
                                "profits": profits,
                            })
                            # Log this opportunity to file
                            log_opportunity(symbol, price, gap_pct, profits)
                            _bot_status["opportunities_today"] += 1

                    # --- Display dashboard ---
                    print_dashboard(prices, opportunities)

                    # --- Update status for health endpoint ---
                    _bot_status["cycles"] += 1
                    _bot_status["last_cycle"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                except Exception as e:
                    # Inner catch: log but keep running
                    print(f"[Error] Unexpected error in monitor cycle: {e}")

                # Wait before next cycle
                time.sleep(POLL_INTERVAL)

        except Exception as e:
            # Outer catch: something seriously wrong — wait 10s then restart the loop
            print(f"[CRITICAL] Monitor loop crashed: {e}")
            print("[CRITICAL] Restarting in 10 seconds...")
            time.sleep(10)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    # Start the keepalive HTTP server in the background first
    start_keepalive_server()

    # Start the monitoring loop (runs forever, never exits)
    run_monitor()
