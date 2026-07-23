"""
Geocode all stations in the database with (0,0) coordinates.

Uses Google Geocoding API to resolve addresses to lat/lng coordinates.
Updates FuelStation records in-place.

Usage:
    source venv/bin/activate
    python geocode_stations.py --dry-run          # Preview without geocoding
    python geocode_stations.py --batch 100        # Geocode first 100 stations
    python geocode_stations.py                    # Geocode all (may take ~30 min)
"""
import os, sys, time, json
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()
from django.contrib.gis.geos import Point
from django.db import transaction
from fuel_routing.models import FuelStation
from fuel_routing.constants import GOOGLE_API_KEY
import requests

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
RATE_LIMIT_SLEEP = 0.05  # 50ms between calls (20/sec, well under Google's 50/sec free tier)


def build_query(station):
    """Build geocoding query from station fields."""
    parts = []
    if station.address:
        parts.append(station.address)
    if station.city:
        parts.append(station.city)
    if station.state:
        parts.append(station.state)
    parts.append("USA")
    return ", ".join(parts)


def geocode_station(station):
    """Geocode a single station. Returns (lat, lng) or (None, None)."""
    query = build_query(station)
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"address": query, "key": GOOGLE_API_KEY, "region": "us"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "OK" and data.get("results"):
            loc = data["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
        elif data.get("status") == "ZERO_RESULTS":
            return None, None
        elif data.get("status") == "OVER_QUERY_LIMIT":
            print(f"  OVER LIMIT. Sleeping 60s...")
            time.sleep(60)
            return geocode_station(station)  # Retry
        else:
            return None, None
    except Exception as e:
        print(f"  Error: {e}")
        return None, None


def main():
    dry_run = "--dry-run" in sys.argv
    batch_size = None
    for arg in sys.argv:
        if arg.startswith("--batch="):
            batch_size = int(arg.split("=")[1])

    stations = FuelStation.objects.filter(is_active=True, latitude=0).order_by("opis_id")
    total = stations.count()

    if batch_size:
        stations = stations[:batch_size]

    print(f"Stations to geocode: {min(batch_size or total, total)}/{total}")
    if dry_run:
        print("DRY RUN — no API calls will be made")
        for s in stations[:5]:
            print(f"  OPIS {s.opis_id}: {build_query(s)}")
        print("  ...")
        return

    succeeded = 0
    failed = 0
    skipped = 0

    for i, station in enumerate(stations, 1):
        if i > 1:
            time.sleep(RATE_LIMIT_SLEEP)

        lat, lng = geocode_station(station)
        if lat is not None:
            station.latitude = lat
            station.longitude = lng
            station.location_point = Point(lng, lat, srid=4326)
            station.save(update_fields=["latitude", "longitude", "location_point"])
            succeeded += 1
            if i % 50 == 0 or i == 1:
                print(f"  [{i}/{total}] OPIS {station.opis_id}: ({lat:.4f}, {lng:.4f}) — {station.city}, {station.state}")
        else:
            failed += 1
            if failed <= 5:  # Show first 5 failures
                print(f"  [{i}/{total}] OPIS {station.opis_id}: NOT FOUND — {build_query(station)}")

    print(f"\nDone: {succeeded} geocoded, {failed} failed, {skipped} skipped")
    remaining = FuelStation.objects.filter(is_active=True, latitude=0).count()
    print(f"Remaining without coordinates: {remaining}")


if __name__ == "__main__":
    main()
