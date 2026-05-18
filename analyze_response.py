#!/usr/bin/env python3
"""Analyze a specific response for bugs."""
import json
import sys

data = json.load(sys.stdin)
sr = data['selected_route']
ts = data['trip_summary']
stops = data['fuel_stops']
cmp = data['route_comparison']

print('=== FIELD-BY-FIELD ANALYSIS ===\n')

# 1. Distance / MPG = ideal consumption
ideal_cons = sr['distance_miles'] / 10
print(f'Ideal consumption (distance/10): {ideal_cons:.2f} gal')
print(f'Actual consumption: {ts["total_fuel_consumed_gallons"]} gal')
overhead = ts["total_fuel_consumed_gallons"] - ideal_cons
print(f'Detour overhead: {overhead:.2f} gal = {overhead * 10:.1f} mi\n')

# 2. Fuel accounting
print(f'Starting: {ts["starting_fuel_gallons"]} gal')
print(f'Purchased: {ts["fuel_purchased_at_stops"]} gal')
print(f'Available: {ts["total_fuel_available"]} gal')
print(f'Consumed: {ts["total_fuel_consumed_gallons"]} gal')
print(f'Remaining: {ts["fuel_remaining_at_destination"]} gal')
avail = ts['total_fuel_available']
consumed = ts['total_fuel_consumed_gallons']
remaining = ts['fuel_remaining_at_destination']
acct_ok = abs(avail - consumed - remaining) < 0.1
print(f'Accounting ({avail} - {consumed} = {avail - consumed} vs {remaining}): {"OK" if acct_ok else "FAIL"}\n')

# 3. Stop cost math
print('Stop cost verification:')
for s in stops:
    expected = round(s['fuel_price_per_gallon'] * s['gallons_to_buy'], 2)
    actual = s['fuel_cost']
    ok = abs(expected - actual) < 0.005
    print(f'  Stop {s["stop_number"]}: {s["fuel_price_per_gallon"]} * {s["gallons_to_buy"]} = {expected} vs {actual} {"OK" if ok else "FAIL"}')

# 4. Sum of stop costs vs total
stop_sum = sum(s['fuel_cost'] for s in stops)
if len(stops) > 0:
    cost_ok = abs(stop_sum - ts['total_fuel_cost']) < 0.01
    print(f'\nSum of stop costs: {stop_sum} vs total: {ts["total_fuel_cost"]} {"OK" if cost_ok else "FAIL"}')
else:
    print(f'\nSum of stop costs: {stop_sum} vs total: {ts["total_fuel_cost"]} (SKIP - no stops)')

# 5. Sum of gallons purchased
gal_sum = sum(s['gallons_to_buy'] for s in stops)
gal_ok = abs(gal_sum - ts['fuel_purchased_at_stops']) < 0.1
print(f'Sum of gallons: {gal_sum} vs {ts["fuel_purchased_at_stops"]} {"OK" if gal_ok else "FAIL"}')

# 6. Average price
purchased = ts['fuel_purchased_at_stops']
if purchased > 0:
    avg = round(ts['total_fuel_cost'] / purchased, 2)
    avg_ok = abs(avg - ts['average_price_per_gallon']) < 0.01
    print(f'Avg price: {avg} vs {ts["average_price_per_gallon"]} {"OK" if avg_ok else "FAIL"}')
else:
    print(f'Avg price: N/A (no fuel purchased) vs {ts["average_price_per_gallon"]}')

# 7. Route comparison
rb = [c for c in cmp if c['selected']][0]
ra = [c for c in cmp if not c['selected']][0]
sel_ok = abs(rb['estimated_total_fuel_cost'] - sr['estimated_total_fuel_cost']) < 0.01
print(f'\nSelected route cost match: {rb["estimated_total_fuel_cost"]} vs {sr["estimated_total_fuel_cost"]} {"OK" if sel_ok else "FAIL"}')
print(f'Route A: {ra["distance_miles"]} mi, ${ra["estimated_total_fuel_cost"]}, {ra["fuel_stops_required"]} stops')
print(f'Route B: {rb["distance_miles"]} mi, ${rb["estimated_total_fuel_cost"]}, {rb["fuel_stops_required"]} stops')
print(f'Cost/mile A: ${ra["estimated_total_fuel_cost"]/ra["distance_miles"]:.4f}')
print(f'Cost/mile B: ${rb["estimated_total_fuel_cost"]/rb["distance_miles"]:.4f}')

# 8. Tank capacity trace
print('\n=== TANK CAPACITY TRACE ===')
print('Max tank: 50 gal, MPG: 10')
fuel = 50.0
prev_mile = 0.0
for s in stops:
    dist = s['mile_marker'] - prev_mile
    fuel_used = dist / 10
    fuel_before = fuel - fuel_used
    fuel_after = fuel_before + s['gallons_to_buy']
    status = 'OVERFILL!' if fuel_after > 50.01 else 'OK'
    print(f'  Stop {s["stop_number"]} @ {s["mile_marker"]}: +{dist:.1f}mi, used {fuel_used:.2f}, before={fuel_before:.2f}, buy={s["gallons_to_buy"]}, after={fuel_after:.2f} {status}')
    prev_mile = s['mile_marker']
    fuel = fuel_after

final_dist = sr['distance_miles'] - prev_mile
final_used = final_dist / 10
final_remaining = fuel - final_used
print(f'  Final leg: {final_dist:.1f}mi, used {final_used:.2f}, remaining={final_remaining:.2f}')
print(f'  Stated remaining: {remaining}')
print(f'  Match: {"OK" if abs(final_remaining - remaining) < 0.1 else "FAIL"}')
