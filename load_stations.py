"""
Load new stations from fuel_prices_cleaned.csv into the database.

DESIGN:
  - CSV is the source for adding NEW stations only.
  - Existing station data (coordinates, prices) is NEVER overwritten.
  - Admin panel changes are preserved across runs.
  - Use --refresh-prices to force-reload all prices from CSV.

Usage:
    source venv/bin/activate
    python load_stations.py                        # Insert new stations + their prices only
    python load_stations.py --dry-run              # Preview without inserting
    python load_stations.py --refresh-prices       # Force-refresh ALL prices from CSV
"""
import os, sys, csv
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from django.contrib.gis.geos import Point
from fuel_routing.models import FuelStation, FuelPrice, PriceVersion


def main():
    dry_run = "--dry-run" in sys.argv
    refresh_prices = "--refresh-prices" in sys.argv

    with open('fuel_prices_cleaned.csv') as f:
        rows = list(csv.DictReader(f))

    existing_ids = set(FuelStation.objects.values_list('opis_id', flat=True))
    csv_ids = set(int(r['opis_id']) for r in rows)
    new_ids = csv_ids - existing_ids

    if dry_run:
        print(f"CSV rows: {len(rows)}")
        print(f"Unique OPIS IDs in CSV: {len(csv_ids)}")
        print(f"New stations to insert: {len(new_ids)}")
        print(f"Existing stations to skip: {len(existing_ids & csv_ids)}")
        if refresh_prices:
            print(f"⚠️  --refresh-prices: ALL existing prices will be DELETED and re-inserted from CSV")
            print(f"   Admin panel price changes will be LOST")
        else:
            print(f"Prices to insert for NEW stations only: {sum(1 for r in rows if int(r['opis_id']) in new_ids)}")
            print(f"Existing prices preserved: YES")
        return

    # Get or create active price version
    pv, _ = PriceVersion.objects.get_or_create(
        version_number=1,
        defaults={
            'version_hash': 'v2-fixed-pipeline',
            'is_active': True,
            'is_published': True,
            'expires_at': timezone.now() + timedelta(days=365),
            'total_stations': len(set(int(r['opis_id']) for r in rows)),
            'total_records': len(rows),
        }
    )

    # --- INSERT NEW STATIONS ONLY ---
    new_rows = [r for r in rows if int(r['opis_id']) in new_ids]
    new_stations = []
    for row in new_rows:
        oid = int(row['opis_id'])
        new_stations.append(FuelStation(
            opis_id=oid, name=row['name'],
            address=row['normalized_address'], city=row['city'],
            state=row['state'], rack_id=int(row['rack_id']),
            latitude=0.0, longitude=0.0,
            location_point=Point(0, 0, srid=4326),
            is_active=True, quality_score=100,
        ))

    if new_stations:
        FuelStation.objects.bulk_create(new_stations, batch_size=200)
        print(f"Inserted {len(new_stations)} new stations")

        # Insert prices only for new stations
        now = timezone.now()
        new_prices = [
            FuelPrice(version=pv, opis_id=int(r['opis_id']),
                      price_per_gallon=Decimal(r['retail_price']),
                      fuel_type='DIESEL',
                      observed_at=now, effective_from=now,
                      is_current=True, is_available=True)
            for r in new_rows
        ]
        FuelPrice.objects.bulk_create(new_prices, batch_size=500)
        print(f"Inserted {len(new_prices)} price records for new stations")

        # Update version stats
        total_prices = FuelPrice.objects.filter(version=pv).count()
        total_stations = FuelStation.objects.count()
        PriceVersion.objects.filter(id=pv.id).update(
            total_stations=total_stations,
            total_records=total_prices,
        )
    else:
        print("No new stations to insert")

    # --- OPTIONAL: REFRESH ALL PRICES FROM CSV ---
    if refresh_prices:
        old_count = FuelPrice.objects.filter(version=pv).count()
        FuelPrice.objects.filter(version=pv).delete()
        print(f"Removed {old_count} old prices (--refresh-prices)")

        now = timezone.now()
        FuelPrice.objects.bulk_create([
            FuelPrice(version=pv, opis_id=int(r['opis_id']),
                      price_per_gallon=Decimal(r['retail_price']),
                      fuel_type='DIESEL',
                      observed_at=now, effective_from=now,
                      is_current=True, is_available=True)
            for r in rows
        ], batch_size=500)
        print(f"Inserted {len(rows)} prices from CSV (--refresh-prices)")

        PriceVersion.objects.filter(id=pv.id).update(
            total_stations=len(csv_ids),
            total_records=len(rows),
        )

    # Summary
    n = FuelStation.objects.filter(is_active=True).count()
    m = FuelPrice.objects.filter(version=pv).count()
    print(f"DB now: {n} stations, {m} prices (active version {pv.id})")
    if new_stations:
        print(f"Run `python geocode_stations.py` to geocode new stations at (0,0)")


if __name__ == '__main__':
    main()
