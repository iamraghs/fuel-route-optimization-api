#!/usr/bin/env python3
"""Comprehensive 180-test-case validator for Fuel Route Optimization API."""
import json
import subprocess
import time
import sys
from datetime import datetime


# ─── SHORT DISTANCE (0–500 Miles) ─────────────────────────────────
SHORT = [
    {"start": "Miami, FL", "finish": "Orlando, FL"},
    {"start": "Dallas, TX", "finish": "Houston, TX"},
    {"start": "Phoenix, AZ", "finish": "Las Vegas, NV"},
    {"start": "Seattle, WA", "finish": "Portland, OR"},
    {"start": "Chicago, IL", "finish": "Indianapolis, IN"},
    {"start": "Atlanta, GA", "finish": "Charlotte, NC"},
    {"start": "San Diego, CA", "finish": "Los Angeles, CA"},
    {"start": "Kansas City, MO", "finish": "St. Louis, MO"},
    {"start": "Denver, CO", "finish": "Salt Lake City, UT"},
    {"start": "Nashville, TN", "finish": "Memphis, TN"},
    {"start": "Boston, MA", "finish": "New York, NY"},
    {"start": "Cleveland, OH", "finish": "Detroit, MI"},
    {"start": "Boise, ID", "finish": "Spokane, WA"},
    {"start": "Tampa, FL", "finish": "Jacksonville, FL"},
    {"start": "Albuquerque, NM", "finish": "El Paso, TX"},
    {"start": "Oklahoma City, OK", "finish": "Dallas, TX"},
    {"start": "New Orleans, LA", "finish": "Birmingham, AL"},
    {"start": "Minneapolis, MN", "finish": "Madison, WI"},
    {"start": "Philadelphia, PA", "finish": "Washington, DC"},
    {"start": "Richmond, VA", "finish": "Raleigh, NC"},
    {"start": "Louisville, KY", "finish": "Columbus, OH"},
    {"start": "San Francisco, CA", "finish": "Sacramento, CA"},
    {"start": "Tucson, AZ", "finish": "Phoenix, AZ"},
    {"start": "Milwaukee, WI", "finish": "Chicago, IL"},
    {"start": "Austin, TX", "finish": "San Antonio, TX"},
    {"start": "Buffalo, NY", "finish": "Pittsburgh, PA"},
    {"start": "Fresno, CA", "finish": "Reno, NV"},
    {"start": "Charleston, SC", "finish": "Savannah, GA"},
    {"start": "Little Rock, AR", "finish": "Memphis, TN"},
    {"start": "Baton Rouge, LA", "finish": "Houston, TX"},
    {"start": "Des Moines, IA", "finish": "Omaha, NE"},
    {"start": "Cheyenne, WY", "finish": "Denver, CO"},
    {"start": "Sioux Falls, SD", "finish": "Minneapolis, MN"},
    {"start": "Santa Fe, NM", "finish": "Albuquerque, NM"},
    {"start": "Mobile, AL", "finish": "New Orleans, LA"},
    {"start": "Providence, RI", "finish": "Boston, MA"},
    {"start": "Greensboro, NC", "finish": "Atlanta, GA"},
    {"start": "Toledo, OH", "finish": "Cincinnati, OH"},
    {"start": "Grand Rapids, MI", "finish": "Indianapolis, IN"},
    {"start": "Bakersfield, CA", "finish": "Las Vegas, NV"},
    {"start": "Rochester, NY", "finish": "Cleveland, OH"},
    {"start": "Chattanooga, TN", "finish": "Atlanta, GA"},
    {"start": "Wichita, KS", "finish": "Kansas City, MO"},
    {"start": "Bozeman, MT", "finish": "Billings, MT"},
    {"start": "Flagstaff, AZ", "finish": "Albuquerque, NM"},
    {"start": "Lubbock, TX", "finish": "Amarillo, TX"},
    {"start": "Norfolk, VA", "finish": "Richmond, VA"},
    {"start": "Eugene, OR", "finish": "Seattle, WA"},
    {"start": "Scranton, PA", "finish": "Philadelphia, PA"},
    {"start": "Columbia, SC", "finish": "Charlotte, NC"},
]

# ─── MEDIUM DISTANCE (500–1500 Miles) ───────────────────────────────
MEDIUM = [
    {"start": "Los Angeles, CA", "finish": "Denver, CO"},
    {"start": "Chicago, IL", "finish": "Dallas, TX"},
    {"start": "Seattle, WA", "finish": "Boise, ID"},
    {"start": "Phoenix, AZ", "finish": "Houston, TX"},
    {"start": "Miami, FL", "finish": "Nashville, TN"},
    {"start": "Boston, MA", "finish": "Atlanta, GA"},
    {"start": "San Diego, CA", "finish": "Albuquerque, NM"},
    {"start": "Portland, OR", "finish": "Salt Lake City, UT"},
    {"start": "Detroit, MI", "finish": "New Orleans, LA"},
    {"start": "Kansas City, MO", "finish": "Denver, CO"},
    {"start": "Charlotte, NC", "finish": "Dallas, TX"},
    {"start": "Las Vegas, NV", "finish": "Omaha, NE"},
    {"start": "Indianapolis, IN", "finish": "Houston, TX"},
    {"start": "Philadelphia, PA", "finish": "Chicago, IL"},
    {"start": "Austin, TX", "finish": "Phoenix, AZ"},
    {"start": "Tampa, FL", "finish": "Washington, DC"},
    {"start": "Cleveland, OH", "finish": "Dallas, TX"},
    {"start": "New York, NY", "finish": "Nashville, TN"},
    {"start": "Minneapolis, MN", "finish": "Nashville, TN"},
    {"start": "Boise, ID", "finish": "Phoenix, AZ"},
    {"start": "Atlanta, GA", "finish": "Philadelphia, PA"},
    {"start": "San Francisco, CA", "finish": "Seattle, WA"},
    {"start": "Salt Lake City, UT", "finish": "Dallas, TX"},
    {"start": "Memphis, TN", "finish": "Denver, CO"},
    {"start": "Raleigh, NC", "finish": "Chicago, IL"},
    {"start": "Jacksonville, FL", "finish": "Houston, TX"},
    {"start": "Louisville, KY", "finish": "Dallas, TX"},
    {"start": "Birmingham, AL", "finish": "Kansas City, MO"},
    {"start": "Pittsburgh, PA", "finish": "Atlanta, GA"},
    {"start": "New Orleans, LA", "finish": "Phoenix, AZ"},
    {"start": "Milwaukee, WI", "finish": "Charlotte, NC"},
    {"start": "Buffalo, NY", "finish": "Nashville, TN"},
    {"start": "Tucson, AZ", "finish": "Oklahoma City, OK"},
    {"start": "Richmond, VA", "finish": "Miami, FL"},
    {"start": "Columbus, OH", "finish": "New Orleans, LA"},
    {"start": "St. Louis, MO", "finish": "Phoenix, AZ"},
    {"start": "Savannah, GA", "finish": "Dallas, TX"},
    {"start": "Albuquerque, NM", "finish": "Omaha, NE"},
    {"start": "Cincinnati, OH", "finish": "Houston, TX"},
    {"start": "Portland, ME", "finish": "Charlotte, NC"},
    {"start": "Reno, NV", "finish": "Denver, CO"},
    {"start": "Little Rock, AR", "finish": "Phoenix, AZ"},
    {"start": "Spokane, WA", "finish": "Salt Lake City, UT"},
    {"start": "Santa Fe, NM", "finish": "Dallas, TX"},
    {"start": "Charleston, SC", "finish": "Chicago, IL"},
    {"start": "Oklahoma City, OK", "finish": "Las Vegas, NV"},
    {"start": "Billings, MT", "finish": "Denver, CO"},
    {"start": "Amarillo, TX", "finish": "Atlanta, GA"},
    {"start": "Fargo, ND", "finish": "Chicago, IL"},
    {"start": "Knoxville, TN", "finish": "Dallas, TX"},
]

# ─── LONG DISTANCE (1500–3500+ Miles) ─────────────────────────────
LONG = [
    {"start": "Los Angeles, CA", "finish": "New York, NY"},
    {"start": "Seattle, WA", "finish": "Miami, FL"},
    {"start": "San Diego, CA", "finish": "Nashville, TN"},
    {"start": "Phoenix, AZ", "finish": "New York, NY"},
    {"start": "Portland, OR", "finish": "Oklahoma City, OK"},
    {"start": "Miami, FL", "finish": "Los Angeles, CA"},
    {"start": "Boston, MA", "finish": "Phoenix, AZ"},
    {"start": "Chicago, IL", "finish": "Seattle, WA"},
    {"start": "Houston, TX", "finish": "Portland, OR"},
    {"start": "Dallas, TX", "finish": "Boston, MA"},
    {"start": "Atlanta, GA", "finish": "San Francisco, CA"},
    {"start": "Las Vegas, NV", "finish": "Philadelphia, PA"},
    {"start": "Denver, CO", "finish": "Miami, FL"},
    {"start": "Salt Lake City, UT", "finish": "New York, NY"},
    {"start": "Kansas City, MO", "finish": "Seattle, WA"},
    {"start": "Phoenix, AZ", "finish": "Portland, ME"},
    {"start": "San Francisco, CA", "finish": "Atlanta, GA"},
    {"start": "Detroit, MI", "finish": "Las Vegas, NV"},
    {"start": "Minneapolis, MN", "finish": "San Diego, CA"},
    {"start": "Charlotte, NC", "finish": "Phoenix, AZ"},
    {"start": "Seattle, WA", "finish": "Houston, TX"},
    {"start": "New Orleans, LA", "finish": "Portland, OR"},
    {"start": "Boston, MA", "finish": "Dallas, TX"},
    {"start": "Chicago, IL", "finish": "Los Angeles, CA"},
    {"start": "Philadelphia, PA", "finish": "Salt Lake City, UT"},
    {"start": "Miami, FL", "finish": "Seattle, WA"},
    {"start": "Boise, ID", "finish": "Atlanta, GA"},
    {"start": "Albuquerque, NM", "finish": "Boston, MA"},
    {"start": "Las Vegas, NV", "finish": "Miami, FL"},
    {"start": "San Diego, CA", "finish": "New York, NY"},
    {"start": "Houston, TX", "finish": "Seattle, WA"},
    {"start": "Nashville, TN", "finish": "San Francisco, CA"},
    {"start": "Phoenix, AZ", "finish": "Chicago, IL"},
    {"start": "Dallas, TX", "finish": "Portland, ME"},
    {"start": "Los Angeles, CA", "finish": "Miami, FL"},
    {"start": "Portland, OR", "finish": "Charlotte, NC"},
    {"start": "New York, NY", "finish": "Phoenix, AZ"},
    {"start": "Seattle, WA", "finish": "Atlanta, GA"},
    {"start": "Denver, CO", "finish": "Boston, MA"},
    {"start": "San Francisco, CA", "finish": "Philadelphia, PA"},
    {"start": "Minneapolis, MN", "finish": "Los Angeles, CA"},
    {"start": "Salt Lake City, UT", "finish": "Miami, FL"},
    {"start": "Chicago, IL", "finish": "Portland, OR"},
    {"start": "Atlanta, GA", "finish": "Seattle, WA"},
    {"start": "Las Vegas, NV", "finish": "Boston, MA"},
    {"start": "Detroit, MI", "finish": "Phoenix, AZ"},
    {"start": "Charlotte, NC", "finish": "San Diego, CA"},
    {"start": "Houston, TX", "finish": "New York, NY"},
    {"start": "Miami, FL", "finish": "San Francisco, CA"},
    {"start": "Seattle, WA", "finish": "Orlando, FL"},
]

# ─── COAST-TO-COAST (West Coast → Florida/Maine/Northeast) ─────────
COAST = [
    {"start": "Seattle, WA", "finish": "Key West, FL"},
    {"start": "Bellingham, WA", "finish": "Miami, FL"},
    {"start": "Portland, OR", "finish": "Key Largo, FL"},
    {"start": "San Francisco, CA", "finish": "Bar Harbor, ME"},
    {"start": "Los Angeles, CA", "finish": "Bangor, ME"},
    {"start": "San Diego, CA", "finish": "Portland, ME"},
    {"start": "Eugene, OR", "finish": "Jacksonville, FL"},
    {"start": "Tacoma, WA", "finish": "Orlando, FL"},
    {"start": "Spokane, WA", "finish": "Naples, FL"},
    {"start": "Fresno, CA", "finish": "Presque Isle, ME"},
    {"start": "Sacramento, CA", "finish": "Fort Lauderdale, FL"},
    {"start": "Olympia, WA", "finish": "Tampa, FL"},
    {"start": "Salem, OR", "finish": "Savannah, GA"},
    {"start": "Santa Rosa, CA", "finish": "Portland, ME"},
    {"start": "Medford, OR", "finish": "Miami Beach, FL"},
    {"start": "Reno, NV", "finish": "Bangor, ME"},
    {"start": "Boise, ID", "finish": "Key West, FL"},
    {"start": "Salt Lake City, UT", "finish": "Portland, ME"},
    {"start": "Las Vegas, NV", "finish": "Augusta, ME"},
    {"start": "Phoenix, AZ", "finish": "Burlington, VT"},
    {"start": "Tucson, AZ", "finish": "Portsmouth, NH"},
    {"start": "Albuquerque, NM", "finish": "Portland, ME"},
    {"start": "El Paso, TX", "finish": "Bangor, ME"},
    {"start": "Cheyenne, WY", "finish": "Miami, FL"},
    {"start": "Billings, MT", "finish": "Key West, FL"},
    {"start": "Missoula, MT", "finish": "Jacksonville, FL"},
    {"start": "Helena, MT", "finish": "Fort Lauderdale, FL"},
    {"start": "Great Falls, MT", "finish": "Savannah, GA"},
    {"start": "Yakima, WA", "finish": "Naples, FL"},
    {"start": "Bend, OR", "finish": "Miami, FL"},
]

TEST_CASES = SHORT + MEDIUM + LONG + COAST

API_URL = "http://localhost:8000/route/fuel-optimization/"
VENV_ACTIVATE = ". /home/iamraghs/Documents/Spotter_AI_Assignment/venv/bin/activate && "


def validate_response(data, elapsed_ms):
    """Validate all fields in a response. Returns list of issues."""
    issues = []

    # Check for error response
    if 'error' in data:
        return [f"API ERROR: {data.get('error')}: {data.get('detail', '')}"]

    # Required top-level keys
    required = ['selected_route', 'route_comparison', 'fuel_stops', 'trip_summary', 'request']
    for key in required:
        if key not in data:
            issues.append(f"Missing key: {key}")
            return issues  # Can't validate further

    sr = data['selected_route']
    ts = data['trip_summary']
    stops = data['fuel_stops']
    cmp = data['route_comparison']

    # === CHECK 1: Response time ===
    if elapsed_ms > 5000:
        issues.append(f"Slow response: {elapsed_ms}ms")

    # === CHECK 2: selected_route fields ===
    sr_required = ['route_id', 'distance_miles', 'estimated_total_fuel_consumption_gallons',
                   'estimated_total_fuel_cost', 'fuel_stops_required']
    for key in sr_required:
        if key not in sr:
            issues.append(f"selected_route missing: {key}")

    # === CHECK 3: Consumption consistency ===
    sr_consumption = sr.get('estimated_total_fuel_consumption_gallons')
    ts_consumption = ts.get('total_fuel_consumed_gallons')
    if sr_consumption is not None and ts_consumption is not None:
        if abs(sr_consumption - ts_consumption) > 0.1:
            issues.append(f"Consumption mismatch: selected={sr_consumption} summary={ts_consumption}")

    # === CHECK 4: Cost consistency ===
    sr_cost = sr.get('estimated_total_fuel_cost')
    ts_cost = ts.get('total_fuel_cost')
    if sr_cost is not None and ts_cost is not None:
        if abs(sr_cost - ts_cost) > 0.01:
            issues.append(f"Cost mismatch: selected={sr_cost} summary={ts_cost}")

    # === CHECK 5: route_comparison cost matches selected_route ===
    for c in cmp:
        if c.get('selected'):
            cmp_cost = c.get('estimated_total_fuel_cost', 0)
            if sr_cost is not None and abs(cmp_cost - sr_cost) > 0.01:
                issues.append(f"Route comparison cost mismatch: cmp={cmp_cost} selected={sr_cost}")

    # === CHECK 6: Fuel accounting (available - consumed = remaining) ===
    start_fuel = ts.get('starting_fuel_gallons', 50)
    purchased = ts.get('fuel_purchased_at_stops', 0)
    consumed = ts.get('total_fuel_consumed_gallons', 0)
    remaining = ts.get('fuel_remaining_at_destination', 0)
    expected_available = start_fuel + purchased
    actual_available = ts.get('total_fuel_available', 0)
    if abs(expected_available - actual_available) > 0.1:
        issues.append(f"Available mismatch: {start_fuel}+{purchased}={expected_available} != {actual_available}")

    expected_remaining = round(actual_available - consumed, 1)
    if abs(expected_remaining - remaining) > 0.1:
        issues.append(f"Accounting fail: {actual_available}-{consumed}={expected_remaining} != remaining={remaining}")

    # === CHECK 7: Stop cost math (price * gallons = cost) ===
    for s in stops:
        price = s.get('fuel_price_per_gallon', 0)
        gal = s.get('gallons_to_buy', 0)
        cost = s.get('fuel_cost', 0)
        expected_cost = round(price * gal, 2)
        if abs(expected_cost - cost) > 0.015:
            issues.append(f"Stop {s.get('stop_number')} cost: {price}*{gal}={expected_cost} != {cost}")

    # === CHECK 8: Trip summary stops count ===
    if ts.get('total_fuel_stops') != len(stops):
        issues.append(f"Stop count mismatch: summary={ts.get('total_fuel_stops')} stops={len(stops)}")

    # === CHECK 9: Route comparison distance ===
    for c in cmp:
        for r in [data.get('_route_a'), data.get('_route_b')]:
            if r and c.get('route_id') == r.get('route_id'):
                if abs(c.get('distance_miles', 0) - r.get('distance_miles', 0)) > 0.1:
                    issues.append(f"Comparison distance mismatch for {c.get('route_id')}")

    return issues


def run_test(tc):
    """Run a single test case and return results."""
    label = f"{tc['start']} → {tc['finish']}"
    start = time.time()

    try:
        curl_cmd = (
            f"curl -s -X POST {API_URL} "
            f"-H 'Content-Type: application/json' "
            f"-d '{json.dumps(tc)}' --max-time 120"
        )
        full_cmd = f"{VENV_ACTIVATE} {curl_cmd}"
        result = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, timeout=130
        )
        elapsed_ms = int((time.time() - start) * 1000)

        if result.returncode != 0:
            return (label, elapsed_ms, [f"curl failed: {result.stderr[:200]}"], None)

        data = json.loads(result.stdout)
        issues = validate_response(data, elapsed_ms)
        return (label, elapsed_ms, issues, data)

    except subprocess.TimeoutExpired:
        return (label, 999999, ["Request timed out (>130s)"], None)
    except json.JSONDecodeError as e:
        return (label, 999999, [f"JSON parse error: {e}"], None)
    except Exception as e:
        return (label, 999999, [f"Unexpected error: {e}"], None)


def main():
    n_short, n_med, n_long, n_coast = len(SHORT), len(MEDIUM), len(LONG), len(COAST)
    total = len(TEST_CASES)
    print(f"Running {total} test cases ({n_short} short, {n_med} medium, {n_long} long, {n_coast} coast-to-coast)...\n")
    print(f"{'#':>3} {'Route':<50} {'Time':>7} {'Cons':>4} {'Cost':>4} {'Acct':>4} {'Math':>4} {'Cmp':>4} {'Stops':>5}  Grp")
    print("-" * 98)

    all_results = []
    total_start = time.time()
    passed = 0
    failed = 0
    slow = 0

    for i, tc in enumerate(TEST_CASES, 1):
        # Determine group label
        if i <= n_short:
            group = "SHORT"
        elif i <= n_short + n_med:
            group = "MED"
        elif i <= n_short + n_med + n_long:
            group = "LONG"
        else:
            group = "COAST"

        label, elapsed_ms, issues, data = run_test(tc)

        # Categorize issues
        cons_fail = any("Consumption" in x for x in issues)
        cost_fail = any("Cost mismatch" in x for x in issues)
        acct_fail = any("Accounting" in x or "Available" in x for x in issues)
        math_fail = any("cost:" in x and "*" in x for x in issues)
        cmp_fail = any("comparison" in x.lower() or "comparison cost" in x for x in issues)
        is_slow = any("Slow" in x for x in issues)

        # Check if there are any real issues (not just slow)
        real_issues = [x for x in issues if "Slow" not in x]

        status = "❌" if real_issues else ("⚠️" if is_slow else "✅")

        if real_issues:
            failed += 1
        else:
            passed += 1
        if is_slow:
            slow += 1

        short_label = label[:49]
        stops_count = len(data.get('fuel_stops', [])) if data else 0

        cons_s = "❌" if cons_fail else "✅"
        cost_s = "❌" if cost_fail else "✅"
        acct_s = "❌" if acct_fail else "✅"
        math_s = "❌" if math_fail else "✅"
        cmp_s = "❌" if cmp_fail else "✅"

        print(f"{i:>3} {short_label:<50} {elapsed_ms:>5}ms {cons_s:>4} {cost_s:>4} {acct_s:>4} {math_s:>4} {cmp_s:>4} {stops_count:>5}  {group}")

        # Print details for failures
        if real_issues:
            for issue in real_issues:
                print(f"     ⚠️  {issue}")

        all_results.append((label, elapsed_ms, issues, data))

        # Small delay between requests to not overwhelm
        if i % 10 == 0:
            print(f"     --- {i}/{len(TEST_CASES)} complete ---")

    total_time = time.time() - total_start

    print("\n" + "=" * 85)
    total_cases = len(TEST_CASES)
    print(f"RESULTS: {passed}/{total_cases} passed | {failed}/{total_cases} failed | {slow}/{total_cases} slow (>5s)")
    print(f"Total time: {total_time:.1f}s | Avg: {total_time/total_cases*1000:.0f}ms per request")
    print()

    # Summary of failures
    if failed > 0:
        print("FAILURE DETAILS:")
        for label, elapsed_ms, issues, data in all_results:
            real_issues = [x for x in issues if "Slow" not in x]
            if real_issues:
                print(f"\n  ❌ {label} ({elapsed_ms}ms):")
                for issue in real_issues:
                    print(f"    - {issue}")

    # Return 1 if any failures
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
