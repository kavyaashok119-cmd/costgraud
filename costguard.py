#!/usr/bin/env python3
"""
CostGuard: FAST Azure Infrastructure Cost Guardrail CLI for Terraform plans.
"""

import argparse
import json
import sqlite3
import sys
import time
import requests

SKIPPED_TYPES = {
    "azurerm_resource_group",
    "azurerm_virtual_network",
    "azurerm_subnet",
    "azurerm_network_security_group",
}

AZURE_PRICING_API_URL = "https://prices.azure.com/api/retail/prices"
HOURS_PER_MONTH = 730

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "CAD": "CA$",
    "AUD": "A$",
    "JPY": "¥",
}


def normalize_location(loc: str | None) -> str:
    """Normalize location to lowercase without spaces (e.g. 'East US' -> 'eastus')."""
    if not loc:
        return ""
    return loc.lower().replace(" ", "")


def init_db(db_path: str = "pricing_cache.db") -> sqlite3.Connection:
    """Initialize SQLite pricing cache."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pricing_cache (
                sku TEXT NOT NULL,
                region TEXT NOT NULL,
                currency TEXT NOT NULL,
                hourly_rate REAL NOT NULL,
                cached_at INTEGER NOT NULL,
                PRIMARY KEY (sku, region, currency)
            );
            """
        )
    return conn


def clear_cache(conn: sqlite3.Connection) -> None:
    """Clear all entries from the pricing cache."""
    with conn:
        conn.execute("DELETE FROM pricing_cache;")


def get_cached_price(conn: sqlite3.Connection, sku: str, region: str, currency: str) -> float | None:
    """Retrieve hourly rate from SQLite cache."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT hourly_rate FROM pricing_cache WHERE sku = ? AND region = ? AND currency = ?;",
        (sku, region, currency),
    )
    row = cursor.fetchone()
    if row is not None:
        return float(row[0])
    return None


def save_cached_price(conn: sqlite3.Connection, sku: str, region: str, currency: str, rate: float) -> None:
    """Save hourly rate to SQLite cache."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO pricing_cache (sku, region, currency, hourly_rate, cached_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (sku, region, currency, rate, int(time.time())),
        )


def fetch_azure_price(sku: str, region: str, currency: str = "USD", is_windows: bool = False) -> float | None:
    """
    Fetch hourly consumption price for a VM SKU from Azure Retail Prices API.
    Rejects Spot and Low Priority meters.
    """
    filter_expr = (
        f"serviceName eq 'Virtual Machines' and "
        f"armRegionName eq '{region}' and "
        f"armSkuName eq '{sku}' and "
        f"priceType eq 'Consumption'"
    )
    params = {
        "$filter": filter_expr,
        "currencyCode": currency,
    }

    try:
        resp = requests.get(AZURE_PRICING_API_URL, params=params, timeout=15)
        if resp.status_code != 200:
            print(f"[WARN] Azure pricing API unavailable. Skipping SKU: {sku}", file=sys.stderr)
            return None
        data = resp.json()
    except (requests.RequestException, Exception):
        print(f"[WARN] Azure pricing API unavailable. Skipping SKU: {sku}", file=sys.stderr)
        return None

    items = data.get("Items", [])
    if not items:
        print(f"[WARN] SKU '{sku}' not found. Skipping.", file=sys.stderr)
        return None

    # Filter out Spot and Low Priority meters
    valid_items = []
    for item in items:
        meter_name = item.get("meterName", "") or ""
        sku_name = item.get("skuName", "") or ""
        product_name = item.get("productName", "") or ""
        desc = f"{meter_name} {sku_name} {product_name}".lower()

        if "spot" in desc or "low priority" in desc:
            continue
        valid_items.append(item)

    if not valid_items:
        print(f"[WARN] SKU '{sku}' not found. Skipping.", file=sys.stderr)
        return None

    # Prefer OS-specific meter if known (Linux vs Windows)
    os_matched = []
    for item in valid_items:
        product_name = (item.get("productName", "") or "").lower()
        if is_windows:
            if "windows" in product_name:
                os_matched.append(item)
        else:
            if "windows" not in product_name:
                os_matched.append(item)

    candidates = os_matched if os_matched else valid_items

    try:
        return float(candidates[0]["retailPrice"])
    except (KeyError, IndexError, ValueError, TypeError):
        print(f"[WARN] SKU '{sku}' not found. Skipping.", file=sys.stderr)
        return None


def get_price(
    conn: sqlite3.Connection,
    sku: str,
    region: str,
    currency: str,
    is_windows: bool,
    metrics: dict,
) -> float | None:
    """Lookup SQLite first; if missing, query Azure API and save."""
    cached = get_cached_price(conn, sku, region, currency)
    if cached is not None:
        metrics["hits"] += 1
        return cached

    # Call Azure API
    metrics["lookups"] += 1
    price = fetch_azure_price(sku, region, currency, is_windows)
    if price is not None:
        save_cached_price(conn, sku, region, currency, price)
        return price

    return None


def format_cost(amount: float, symbol: str = "$", show_sign: bool = False) -> str:
    """Format cost as currency string."""
    if show_sign:
        if amount >= 0:
            return f"+{symbol}{amount:.2f}"
        else:
            return f"-{symbol}{abs(amount):.2f}"
    else:
        if amount < 0:
            return f"-{symbol}{abs(amount):.2f}"
        return f"{symbol}{amount:.2f}"


def parse_plan(plan_data: dict) -> list[dict]:
    """Parse Terraform plan resource_changes."""
    if not isinstance(plan_data, dict):
        raise ValueError("Plan JSON must be an object")

    resource_changes = plan_data.get("resource_changes")
    if not isinstance(resource_changes, list):
        raise ValueError("Invalid Terraform plan: 'resource_changes' array not found")

    return resource_changes


def extract_sku_and_location(data: dict | None) -> tuple[str | None, str | None]:
    """Extract VM size and normalized location from change block."""
    if not data or not isinstance(data, dict):
        return None, None
    sku = data.get("size") or data.get("vm_size")
    loc = normalize_location(data.get("location"))
    return sku, loc


def main() -> None:
    parser = argparse.ArgumentParser(description="CostGuard: Azure Infrastructure Cost Impact Guardrail")
    parser.add_argument("--plan", type=str, default=None, help="Path to Terraform plan JSON file (or '-' for stdin)")
    parser.add_argument("--max-increase", type=float, default=None, help="Maximum allowed net monthly increase")
    parser.add_argument("--currency", type=str, default="USD", help="Billing currency code (default: USD)")
    parser.add_argument("--clear-cache", action="store_true", help="Clear the pricing SQLite cache")

    args = parser.parse_args()

    conn = init_db()

    if args.clear_cache:
        clear_cache(conn)
        if not args.plan:
            print("Pricing cache cleared.")
            sys.exit(0)

    # Determine input source
    plan_raw = None
    if args.plan and args.plan != "-":
        try:
            with open(args.plan, "r", encoding="utf-8") as f:
                plan_raw = f.read()
        except FileNotFoundError:
            sys.stderr.write(f"Error: Plan file '{args.plan}' not found.\n")
            sys.exit(2)
        except Exception as e:
            sys.stderr.write(f"Error reading plan file: {e}\n")
            sys.exit(2)
    elif not sys.stdin.isatty():
        plan_raw = sys.stdin.read()
        if not plan_raw.strip():
            if args.clear_cache:
                print("Pricing cache cleared.")
                sys.exit(0)
            sys.stderr.write("Error: Empty plan input received from stdin.\n")
            sys.exit(2)
    else:
        if args.clear_cache:
            print("Pricing cache cleared.")
            sys.exit(0)
        sys.stderr.write("Error: Missing plan input. Specify --plan <file> or pipe via stdin.\n")
        sys.exit(2)

    try:
        plan_data = json.loads(plan_raw)
        resource_changes = parse_plan(plan_data)
    except Exception as e:
        sys.stderr.write(f"Error: Invalid Terraform JSON input: {e}\n")
        sys.exit(2)

    currency = args.currency.upper()
    currency_sym = CURRENCY_SYMBOLS.get(currency, f"{currency} ")

    metrics = {"hits": 0, "lookups": 0}
    evaluated_resources = []

    for rc in resource_changes:
        address = rc.get("address", "unknown_resource")
        res_type = rc.get("type", "")
        change = rc.get("change", {})
        actions = change.get("actions", [])
        before = change.get("before")
        after = change.get("after")

        # Skip non-billable / ignored resources
        if res_type in SKIPPED_TYPES:
            continue

        is_windows = "windows" in res_type.lower()
        actions_set = set(actions)

        # Classify action
        if "create" in actions_set and "delete" in actions_set:
            action_name = "REPLACE"
        elif "create" in actions_set:
            action_name = "CREATE"
        elif "delete" in actions_set:
            action_name = "DELETE"
        elif "update" in actions_set:
            action_name = "UPDATE"
        elif "no-op" in actions_set or "read" in actions_set:
            action_name = "NO-OP"
        else:
            action_name = "UNKNOWN"

        old_sku, old_loc = extract_sku_and_location(before)
        new_sku, new_loc = extract_sku_and_location(after)

        # If not a VM resource (no SKU found in before or after), skip gracefully
        if not old_sku and not new_sku:
            continue

        old_monthly = 0.0
        new_monthly = 0.0
        display_sku = new_sku or old_sku or ""

        if action_name == "CREATE":
            if not new_sku:
                print(f"[WARN] SKU '{new_sku}' not found. Skipping.", file=sys.stderr)
                continue
            rate = get_price(conn, new_sku, new_loc or "", currency, is_windows, metrics)
            if rate is None:
                continue
            old_monthly = 0.0
            new_monthly = rate * HOURS_PER_MONTH

        elif action_name == "DELETE":
            if not old_sku:
                print(f"[WARN] SKU '{old_sku}' not found. Skipping.", file=sys.stderr)
                continue
            rate = get_price(conn, old_sku, old_loc or "", currency, is_windows, metrics)
            if rate is None:
                continue
            old_monthly = rate * HOURS_PER_MONTH
            new_monthly = 0.0

        elif action_name in ("UPDATE", "REPLACE"):
            # Old rate
            old_rate = None
            if old_sku:
                old_rate = get_price(conn, old_sku, old_loc or "", currency, is_windows, metrics)
            # New rate
            new_rate = None
            if new_sku:
                new_rate = get_price(conn, new_sku, new_loc or "", currency, is_windows, metrics)

            if old_rate is None and new_rate is None:
                continue
            old_monthly = (old_rate or 0.0) * HOURS_PER_MONTH
            new_monthly = (new_rate or 0.0) * HOURS_PER_MONTH

        elif action_name == "NO-OP":
            sku = new_sku or old_sku
            loc = new_loc or old_loc
            if not sku:
                continue
            rate = get_price(conn, sku, loc or "", currency, is_windows, metrics)
            if rate is None:
                continue
            old_monthly = rate * HOURS_PER_MONTH
            new_monthly = rate * HOURS_PER_MONTH

        delta = new_monthly - old_monthly

        evaluated_resources.append({
            "address": address,
            "action": action_name,
            "sku": display_sku,
            "old_cost": old_monthly,
            "new_cost": new_monthly,
            "delta": delta,
        })

    prior_total = sum(r["old_cost"] for r in evaluated_resources)
    projected_total = sum(r["new_cost"] for r in evaluated_resources)
    net_impact = projected_total - prior_total

    # Report Output
    print("=" * 60)
    print("COSTGUARD: Azure Infrastructure Cost Impact Report")
    print("=" * 60)
    print()
    print(f"{'Resource':<33}{'Action':<10}{'SKU':<11}{'Old':<10}{'New':<10}{'Delta'}")
    print("-" * 79)

    for r in evaluated_resources:
        print(r["address"])
        print(f"{'':<33}{r['action']:<10}{r['sku']}")
        old_formatted = format_cost(r["old_cost"], currency_sym)
        new_formatted = format_cost(r["new_cost"], currency_sym)
        delta_formatted = format_cost(r["delta"], currency_sym, show_sign=True)
        print(f"{'':<53}{old_formatted:<10}{new_formatted:<10}{delta_formatted}")
        print()

    print("-" * 79)
    print()
    print(f"{'Prior Monthly Total:':<25}{format_cost(prior_total, currency_sym)}")
    print(f"{'Projected Monthly Total:':<25}{format_cost(projected_total, currency_sym)}")
    print(f"{'Net Monthly Impact:':<25}{format_cost(net_impact, currency_sym, show_sign=True)}")
    print()

    hits_str = f"{metrics['hits']} hit" if metrics["hits"] == 1 else f"{metrics['hits']} hits"
    lookup_str = f"{metrics['lookups']} API lookup" if metrics["lookups"] == 1 else f"{metrics['lookups']} API lookups"
    print(f"Cache: {hits_str}, {lookup_str}")
    print()

    print("POLICY VERDICT:")
    if args.max_increase is not None:
        print(f"Budget Threshold: {format_cost(args.max_increase, currency_sym)}")
        if net_impact <= args.max_increase:
            print("Status: PASSED")
            print()
            print("=" * 60)
            sys.exit(0)
        else:
            print("Status: FAILED")
            print("[CIRCUIT BREAKER] Budget threshold breached. Deployment blocked.")
            print()
            print("=" * 60)
            sys.exit(1)
    else:
        print("Status: PASSED")
        print()
        print("=" * 60)
        sys.exit(0)


if __name__ == "__main__":
    main()
