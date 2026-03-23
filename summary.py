"""
DeFi Arbitrage Opportunity Summary
Reads opportunities_log.txt and shows statistics for today and overall.
Run with: python summary.py
"""

import os
import re
from datetime import datetime, date
from collections import defaultdict

LOG_FILE = "opportunities_log.txt"


def parse_log_file(filepath: str) -> list:
    """
    Parse the opportunities log file and return a list of opportunity records.
    Each record is a dict with: timestamp, symbol, price, gap_pct, profits
    """
    if not os.path.exists(filepath):
        print(f"Log file '{filepath}' not found. No opportunities have been logged yet.")
        return []

    records = []
    current = {}

    try:
        with open(filepath, "r") as f:
            lines = f.readlines()

        for line in lines:
            line = line.strip()

            # Detect start of a new record
            if line.startswith("OPPORTUNITY LOGGED:"):
                # Save previous record if it exists
                if current:
                    records.append(current)
                ts_str = line.replace("OPPORTUNITY LOGGED:", "").strip()
                try:
                    current = {
                        "timestamp": datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S"),
                        "symbol": None,
                        "price": None,
                        "gap_pct": None,
                        "profits": {},
                    }
                except ValueError:
                    current = {}
                continue

            if not current:
                continue

            # Parse coin symbol
            if line.startswith("Coin:"):
                current["symbol"] = line.replace("Coin:", "").strip()

            # Parse price
            elif line.startswith("Price:"):
                price_str = line.replace("Price:", "").replace("$", "").strip()
                try:
                    current["price"] = float(price_str)
                except ValueError:
                    pass

            # Parse gap percentage
            elif line.startswith("Gap:"):
                gap_str = line.replace("Gap:", "").replace("%", "").strip()
                try:
                    current["gap_pct"] = float(gap_str)
                except ValueError:
                    pass

            # Parse profit lines (e.g., "  $100k loan -> profit: $289")
            elif "loan -> profit:" in line:
                # Extract loan size label and profit value
                match = re.search(r'\$(\d+[kM])\s+loan\s+->\s+profit:\s+\$([0-9,\-]+)', line)
                if match:
                    loan_label = match.group(1)
                    profit_str = match.group(2).replace(",", "")
                    try:
                        current["profits"][loan_label] = float(profit_str)
                    except ValueError:
                        pass

        # Save the last record
        if current and current.get("symbol"):
            records.append(current)

    except Exception as e:
        print(f"[Error] Could not read log file: {e}")
        return []

    return records


def show_summary():
    """
    Display statistics from the opportunities log.
    """
    records = parse_log_file(LOG_FILE)

    if not records:
        return

    today = date.today()
    today_records = [r for r in records if r["timestamp"].date() == today]

    print("\n========================================")
    print("   DeFi Arbitrage Opportunity Summary   ")
    print("========================================")
    print(f"Report generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # --- Today's stats ---
    print(f"--- Today ({today.strftime('%Y-%m-%d')}) ---")
    if not today_records:
        print("No opportunities found today.")
    else:
        print(f"Opportunities found: {len(today_records)}")

        # Coin breakdown
        coin_counts = defaultdict(int)
        for r in today_records:
            coin_counts[r["symbol"]] += 1
        print("By coin:")
        for coin, count in sorted(coin_counts.items(), key=lambda x: -x[1]):
            print(f"  {coin:<6}: {count} time(s)")

        # Best opportunity today (highest $1M loan profit)
        best_today = max(
            today_records,
            key=lambda r: r["profits"].get("1M", r["profits"].get("$1M", 0)),
        )
        print(f"\nBest opportunity today:")
        print(f"  Coin:  {best_today['symbol']}")
        print(f"  Price: ${best_today['price']:.4f}")
        print(f"  Gap:   {best_today['gap_pct']:+.2f}%")
        print(f"  Time:  {best_today['timestamp'].strftime('%H:%M:%S')}")
        for label, profit in best_today["profits"].items():
            print(f"  ${label} loan -> ${profit:,.0f}")

        # Average gap today
        gaps = [abs(r["gap_pct"]) for r in today_records if r["gap_pct"] is not None]
        if gaps:
            avg_gap = sum(gaps) / len(gaps)
            print(f"\nAverage gap today: {avg_gap:.3f}%")

    print()

    # --- All-time stats ---
    print(f"--- All Time ---")
    print(f"Total opportunities logged: {len(records)}")

    if records:
        # Date range
        earliest = min(r["timestamp"] for r in records)
        latest = max(r["timestamp"] for r in records)
        print(f"Date range: {earliest.strftime('%Y-%m-%d')} to {latest.strftime('%Y-%m-%d')}")

        # Best ever opportunity
        best_ever = max(
            records,
            key=lambda r: r["profits"].get("1M", r["profits"].get("$1M", 0)),
        )
        print(f"\nBest opportunity ever:")
        print(f"  Coin:  {best_ever['symbol']}")
        print(f"  Price: ${best_ever['price']:.4f}")
        print(f"  Gap:   {best_ever['gap_pct']:+.2f}%")
        print(f"  Time:  {best_ever['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}")
        for label, profit in best_ever["profits"].items():
            print(f"  ${label} loan -> ${profit:,.0f}")

        # All-time average gap
        all_gaps = [abs(r["gap_pct"]) for r in records if r["gap_pct"] is not None]
        if all_gaps:
            avg_gap_all = sum(all_gaps) / len(all_gaps)
            print(f"\nAll-time average gap: {avg_gap_all:.3f}%")

        # Most active coins
        all_coin_counts = defaultdict(int)
        for r in records:
            all_coin_counts[r["symbol"]] += 1
        print("\nMost triggered coins (all time):")
        for coin, count in sorted(all_coin_counts.items(), key=lambda x: -x[1]):
            print(f"  {coin:<6}: {count} opportunity(ies)")

        # Opportunities per day breakdown
        per_day = defaultdict(int)
        for r in records:
            per_day[r["timestamp"].date()] += 1
        print(f"\nOpportunities per day:")
        for day in sorted(per_day.keys()):
            print(f"  {day}: {per_day[day]}")

    print("========================================\n")


if __name__ == "__main__":
    show_summary()
