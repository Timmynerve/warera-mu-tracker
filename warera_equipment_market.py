"""Summarize recent WarEra equipment market sales (prices vs. stat rolls) into a CSV.

Open offers are not readable with an API token, so this uses completed market sales instead.
The token is read from the WARERA_API_TOKEN environment variable.

Also writes the current food, ammo and pill prices to a second CSV.

Usage: python warera_equipment_market.py [--days 7] [--max-per-item 3000] [--out equipment_market_summary.csv]
       [--consumables-out consumables_market.csv]
"""
import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api2.warera.io/trpc/"
PAGE_SIZE = 100
REQUEST_DELAY = 0.35  # stays under the 200 requests/minute token limit
QUALITY_BAND = 0.2  # rolls in the bottom/top 20% of the stat range count as low/high


def _get(url, headers):
    """GET with automatic wait on rate limiting."""
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                wait = int(exc.headers.get("retry-after") or 30) + 1
                print(f"  rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            if exc.code in (401, 403):
                raise SystemExit(f"Access refused ({exc.code}): check WARERA_API_TOKEN")
            if attempt >= 2:
                raise
        except Exception:
            if attempt >= 2:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Request kept failing: {url[:120]}")


def api(endpoint, payload, headers):
    url = API + endpoint + "?input=" + urllib.parse.quote(json.dumps(payload, separators=(",", ":")))
    return _get(url, headers)["result"]["data"]


def parse_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_sales(code, since, max_items, headers):
    """Market sales of one item code, newest first, back to `since` or `max_items`."""
    sales, cursor = [], None
    while len(sales) < max_items:
        page = api("transaction.getPaginatedTransactions",
                   {"transactionType": "itemMarket", "itemCode": code, "limit": PAGE_SIZE,
                    **({"cursor": cursor} if cursor else {})}, headers)
        items = page.get("items", [])
        recent = [t for t in items if parse_date(t["createdAt"]) >= since]
        sales += recent
        cursor = page.get("nextCursor")
        if not cursor or len(recent) < len(items):
            break
        time.sleep(REQUEST_DELAY)
    return sales[:max_items]


def roll_quality(skills, stat_ranges):
    """Average position of each stat within its range: 0 = minimum roll, 1 = maximum roll."""
    positions = [(skills.get(stat, low) - low) / (high - low) for stat, (low, high) in stat_ranges.items()]
    return sum(positions) / len(positions)


def median(values):
    return round(statistics.median(values), 2) if values else None


def summarize(code, item, sales):
    stat_ranges = item["dynamicStats"]
    prices = [t["money"] for t in sales]
    full = [t for t in sales if t["item"]["state"] >= t["item"]["maxState"]]
    quality = {id(t): roll_quality(t["item"]["skills"], stat_ranges) for t in full}
    dates = sorted(parse_date(t["createdAt"]) for t in sales)
    hours = (dates[-1] - dates[0]).total_seconds() / 3600 if dates else 0
    deciles = statistics.quantiles(prices, n=10) if len(prices) >= 2 else [None]
    return {
        "item": code,
        "slot": item["usage"],
        "rarity": item["rarity"],
        "stats": " / ".join(f"{stat} {low}-{high}" for stat, (low, high) in stat_ranges.items()),
        "sales": len(sales),
        "hours_covered": round(hours, 1),
        "sales_per_day": round(len(sales) / max(hours, 1) * 24),
        "median_price": median(prices),
        "p10": round(deciles[0], 2) if deciles[0] is not None else None,
        "p90": round(deciles[-1], 2) if deciles[-1] is not None else None,
        "median_full_state": median([t["money"] for t in full]),
        "median_low_roll": median([t["money"] for t in full if quality[id(t)] <= QUALITY_BAND]),
        "median_high_roll": median([t["money"] for t in full if quality[id(t)] >= 1 - QUALITY_BAND]),
    }


def consumable_rows(items, headers):
    """Food, ammo and pill prices from the order book, with the cost of each effect point."""
    averages = api("itemTrading.getPrices", {}, headers)
    rows = []
    for code, item in items.items():
        stats = item.get("flatStats", {})
        effect = stats.get("healthRegenPercent") or stats.get("percentAttack")
        if not effect:
            continue
        sell_orders = api("tradingOrder.getTopOrders", {"itemCode": code}, headers).get("sellOrders", [])
        price = sell_orders[0]["price"] if sell_orders else averages.get(code)
        kind = "food" if "healthRegenPercent" in stats else ("pill" if "buffDurationHours" in stats else "ammo")
        rows.append({
            "item": code,
            "kind": kind,
            "effect": f"+{effect}% {'max health restored (1 hunger)' if kind == 'food' else 'attack'}"
                      + (f" for {stats['buffDurationHours']}h, then -{effect}% for {stats['debuffDurationHours']}h"
                         if kind == "pill" else " per hit" if kind == "ammo" else ""),
            "best_sell_price": price,
            "average_price": round(averages.get(code, 0), 3),
            "price_per_effect_percent": round(price / effect, 4) if price else None,
        })
        time.sleep(REQUEST_DELAY)
    return rows


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--max-per-item", type=int, default=3000)
    parser.add_argument("--out", default="equipment_market_summary.csv")
    parser.add_argument("--consumables-out", default="consumables_market.csv")
    args = parser.parse_args()

    token = os.environ.get("WARERA_API_TOKEN")
    if not token:
        sys.exit("Set the WARERA_API_TOKEN environment variable first.")
    headers = {"User-Agent": "curl/8.0", "X-API-Key": token}
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    print("Loading game reference data...")
    items = api("gameConfig.getGameConfig", {}, headers)["items"]
    equipment = {code: item for code, item in items.items() if item.get("type") in ("weapon", "equipment")}

    rows = []
    for code, item in equipment.items():
        sales = fetch_sales(code, since, args.max_per_item, headers)
        print(f"  {code}: {len(sales)} sales")
        if sales:
            rows.append(summarize(code, item, sales))

    if not rows:
        sys.exit("No sales found for the requested period.")
    write_csv(args.out, rows)
    print(f"Summary written to {args.out} ({len(rows)} items, {sum(r['sales'] for r in rows)} sales)")

    print("Loading consumable prices...")
    consumables = consumable_rows(items, headers)
    write_csv(args.consumables_out, consumables)
    print(f"Consumables written to {args.consumables_out} ({len(consumables)} items)")


if __name__ == "__main__":
    main()
