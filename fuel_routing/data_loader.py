"""
CSV Data Loader for Fuel Pricing Data.

Efficiently loads truck stop data from CSV with geocoding and validation.
Implements batch processing and duplicate handling for production use.
"""

import csv
import logging
from decimal import Decimal
from typing import Tuple, Optional
import requests
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
from django.conf import settings
from .models import TruckStop
import time

logger = logging.getLogger(__name__)

# Cache for geocoding to avoid redundant API calls
_geocoding_cache = {}

# Pre-loaded major US city coordinates to avoid API calls
MAJOR_CITY_COORDS = {
    "new york,ny": (40.7128, -74.0060),
    "los angeles,ca": (34.0522, -118.2437),
    "chicago,il": (41.8781, -87.6298),
    "houston,tx": (29.7604, -95.3698),
    "phoenix,az": (33.4484, -112.0742),
    "philadelphia,pa": (39.9526, -75.1652),
    "san antonio,tx": (29.4241, -98.4936),
    "san diego,ca": (32.7157, -117.1611),
    "dallas,tx": (32.7767, -96.7970),
    "san jose,ca": (37.3382, -121.8863),
    "austin,tx": (30.2672, -97.7431),
    "jacksonville,fl": (30.3322, -81.6557),
    "fort worth,tx": (32.7555, -97.3308),
    "columbus,oh": (39.9612, -82.9988),
    "indianapolis,in": (39.7684, -86.1581),
    "charlotte,nc": (35.2271, -80.8431),
    "detroit,mi": (42.3314, -83.0458),
    "memphis,tn": (35.1495, -90.0490),
    "boston,ma": (42.3601, -71.0589),
    "seattle,wa": (47.6062, -122.3321),
    "denver,co": (39.7392, -104.9903),
    "minneapolis,mn": (44.9778, -93.2650),
    "portland,or": (45.5152, -122.6784),
    "las vegas,nv": (36.1699, -115.1398),
}


class FuelPricingDataLoader:
    """
    Loads fuel pricing data from CSV with intelligent geocoding.
    
    Features:
    - Batch processing for performance
    - Geocoding caching to minimize API calls
    - Duplicate detection and handling
    - Validation and error reporting
    - Progress logging
    """
    
    # Expected CSV columns
    REQUIRED_COLUMNS = [
        'OPIS Truckstop ID', 'Truckstop Name', 'Address',
        'City', 'State', 'Rack ID', 'Retail Price'
    ]
    
    # Geocoding service configuration
    GEOCODE_TIMEOUT = 10  # seconds
    
    def __init__(self, csv_file_path: str, batch_size: int = 50):
        """
        Initialize data loader.
        
        Args:
            csv_file_path: Path to CSV file
            batch_size: Number of records to process before DB commit
        """
        self.csv_file_path = csv_file_path
        self.batch_size = batch_size
        self.geocoder = None
        self.stats = {
            'total_records': 0,
            'loaded': 0,
            'skipped': 0,
            'geocoded': 0,
            'geocode_cached': 0,
            'geocode_failed': 0,
            'duplicates': 0,
            'errors': []
        }
    
    def _initialize_geocoder(self):
        """Initialize geopy geocoder for address-to-coordinates conversion."""
        if not self.geocoder:
            try:
                self.geocoder = Nominatim(user_agent="spotter_ai_fuel_router")
            except Exception as e:
                logger.warning(f"Could not initialize Nominatim geocoder: {e}")
                self.geocoder = None
    
    def _geocode_address(self, address: str, city: str, state: str) -> Optional[Tuple[float, float]]:
        """
        Convert address to coordinates with intelligent caching.
        
        Args:
            address: Street address
            city: City name
            state: State abbreviation
            
        Returns:
            Tuple of (latitude, longitude) or None
        """
        # Try to use Google Maps Geocoding API if available
        google_key = settings.GOOGLE_MAPS_API_KEY
        if google_key:
            return self._geocode_with_google(address, city, state, google_key)
        
        # Fall back to Nominatim (OpenStreetMap)
        return self._geocode_with_nominatim(address, city, state)
    
    def _geocode_with_google(self, address: str, city: str, state: str,
                            api_key: str) -> Optional[Tuple[float, float]]:
        """Geocode using Google Maps API."""
        cache_key = f"{address}|{city}|{state}".lower()
        
        # Check cache
        if cache_key in _geocoding_cache:
            self.stats['geocode_cached'] += 1
            return _geocoding_cache[cache_key]
        
        try:
            full_address = f"{address}, {city}, {state}, USA"
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                'address': full_address,
                'key': api_key,
                'region': 'us'
            }
            
            response = requests.get(url, params=params, timeout=self.GEOCODE_TIMEOUT)
            response.raise_for_status()
            
            data = response.json()
            if data.get('results'):
                location = data['results'][0]['geometry']['location']
                coords = (location['lat'], location['lng'])
                _geocoding_cache[cache_key] = coords
                self.stats['geocoded'] += 1
                return coords
        except Exception as e:
            logger.debug(f"Google geocoding failed for {full_address}: {e}")
            self.stats['geocode_failed'] += 1
        
        return None
    
    def _geocode_with_nominatim(self, address: str, city: str,
                               state: str) -> Optional[Tuple[float, float]]:
        """Geocode using Nominatim (OpenStreetMap) with rate limiting."""
        self._initialize_geocoder()
        
        if not self.geocoder:
            return None
        
        cache_key = f"{address}|{city}|{state}".lower()
        
        # Check cache
        if cache_key in _geocoding_cache:
            self.stats['geocode_cached'] += 1
            return _geocoding_cache[cache_key]
        
        # Check major city database first
        city_key = f"{city},{state}".lower()
        if city_key in MAJOR_CITY_COORDS:
            coords = MAJOR_CITY_COORDS[city_key]
            _geocoding_cache[cache_key] = coords
            self.stats['geocode_cached'] += 1
            return coords
        
        # Rate limiting: Add delay to avoid 429 errors
        time.sleep(1.5)  # 1.5 second delay between Nominatim requests
        
        try:
            full_address = f"{address}, {city}, {state}, USA"
            location = self.geocoder.geocode(full_address, timeout=self.GEOCODE_TIMEOUT)
            
            if location:
                coords = (location.latitude, location.longitude)
                _geocoding_cache[cache_key] = coords
                self.stats['geocoded'] += 1
                return coords
        except (GeocoderTimedOut, GeocoderServiceError) as e:
            logger.debug(f"Nominatim geocoding failed for {full_address}: {e}")
            self.stats['geocode_failed'] += 1
        except Exception as e:
            logger.debug(f"Unexpected geocoding error: {e}")
            self.stats['geocode_failed'] += 1
        
        # Last resort: Try city-only geocoding with major city database
        city_state_key = f"{city},{state}".lower()
        if city_state_key in MAJOR_CITY_COORDS:
            coords = MAJOR_CITY_COORDS[city_state_key]
            _geocoding_cache[cache_key] = coords
            return coords
        
        return None
    
    def load_from_csv(self, skip_geocoding: bool = False) -> dict:
        """
        Load fuel pricing data from CSV file with optimized bulk inserts.
        
        Args:
            skip_geocoding: If True, load only records with coordinates in CSV.
                          If False, attempt to geocode all addresses.
            
        Returns:
            Dictionary with loading statistics
        """
        logger.info(f"Starting data load from {self.csv_file_path}")
        
        try:
            with open(self.csv_file_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                
                # Validate CSV structure
                if not reader.fieldnames:
                    raise ValueError("CSV file is empty")
                
                missing_columns = set(self.REQUIRED_COLUMNS) - set(reader.fieldnames)
                if missing_columns:
                    raise ValueError(f"Missing columns: {missing_columns}")
                
                batch = []
                for row_num, row in enumerate(reader, 1):
                    self.stats['total_records'] += 1
                    
                    try:
                        truck_stop = self._process_row(row, skip_geocoding)
                        if truck_stop:
                            batch.append(truck_stop)
                        
                        # Batch insert with larger batch size
                        if len(batch) >= self.batch_size:
                            self._save_batch_fast(batch)
                            batch = []
                            
                            # Progress update every 5 batches
                            if self.stats['loaded'] % (self.batch_size * 5) == 0:
                                logger.info(f"Progress: {self.stats['loaded']} records loaded...")
                    
                    except Exception as e:
                        error_msg = f"Row {row_num}: {str(e)}"
                        logger.warning(error_msg)
                        self.stats['errors'].append(error_msg)
                        self.stats['skipped'] += 1
                        continue
                
                # Save remaining batch
                if batch:
                    self._save_batch_fast(batch)
        
        except FileNotFoundError:
            logger.error(f"CSV file not found: {self.csv_file_path}")
            self.stats['errors'].append(f"File not found: {self.csv_file_path}")
        except Exception as e:
            logger.error(f"Error loading CSV: {str(e)}")
            self.stats['errors'].append(str(e))
        
        logger.info(f"Data load complete. Stats: {self.stats}")
        return self.stats
    
    def _save_batch_fast(self, batch: list):
        """
        Save batch using optimized raw SQL for maximum speed.
        
        This is 10-50x faster than using Django ORM.
        """
        if not batch:
            return
        
        try:
            # Use raw SQL for bulk insert - much faster than ORM
            from django.db import connection
            
            cursor = connection.cursor()
            
            # Prepare SQL with proper escaping
            sql_rows = []
            for truck_stop in batch:
                sql_rows.append((
                    truck_stop.opis_id,
                    truck_stop.name,
                    truck_stop.address,
                    truck_stop.city,
                    truck_stop.state,
                    truck_stop.latitude,
                    truck_stop.longitude,
                    truck_stop.rack_id,
                    float(truck_stop.retail_price),
                    True  # is_active
                ))
            
            # Use executemany for batch insert
            cursor.executemany("""
                INSERT OR IGNORE INTO fuel_routing_truckstop 
                (opis_id, name, address, city, state, latitude, longitude, 
                 rack_id, retail_price, is_active, created_at, updated_at, last_price_update)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))
            """, sql_rows)
            
            connection.commit()
            self.stats['loaded'] += len(batch)
            logger.info(f"Loaded batch of {len(batch)} records. Total: {self.stats['loaded']}")
        
        except Exception as e:
            logger.error(f"Error saving batch: {str(e)}")
            self.stats['errors'].append(f"Batch save error: {str(e)}")
            # Fall back to individual saves
            for truck_stop in batch:
                try:
                    truck_stop.save()
                    self.stats['loaded'] += 1
                except Exception as e2:
                    logger.warning(f"Could not save {truck_stop.name}: {str(e2)}")
                    self.stats['skipped'] += 1
    
    def _process_row(self, row: dict, skip_geocoding: bool) -> Optional[TruckStop]:
        """
        Process a single CSV row and return TruckStop object if valid.
        
        Args:
            row: Dictionary representing CSV row
            skip_geocoding: Skip geocoding if True
            
        Returns:
            TruckStop object or None if invalid
        """
        # Extract and validate fields
        try:
            opis_id = int(row['OPIS Truckstop ID'].strip())
            name = row['Truckstop Name'].strip()
            address = row['Address'].strip()
            city = row['City'].strip()
            state = row['State'].strip().upper()
            rack_id = int(row['Rack ID'].strip())
            retail_price = Decimal(row['Retail Price'].strip())
        except (KeyError, ValueError) as e:
            raise ValueError(f"Invalid field values: {e}")
        
        # Check for duplicates
        if TruckStop.objects.filter(opis_id=opis_id).exists():
            self.stats['duplicates'] += 1
            return None
        
        # Attempt geocoding
        coords = None
        if not skip_geocoding:
            coords = self._geocode_address(address, city, state)
        
        if not coords:
            # Try to get approximate coordinates using city centroid
            coords = self._get_city_centroid(city, state)
        
        if not coords:
            raise ValueError(f"Could not determine coordinates for {city}, {state}")
        
        # Create and validate model
        truck_stop = TruckStop(
            opis_id=opis_id,
            name=name,
            address=address,
            city=city,
            state=state,
            latitude=coords[0],
            longitude=coords[1],
            rack_id=rack_id,
            retail_price=retail_price,
            is_active=True
        )
        
        return truck_stop
    
    def _get_city_centroid(self, city: str, state: str) -> Optional[Tuple[float, float]]:
        """
        Get approximate center coordinates for a city.
        
        Falls back to state centroid if city not found.
        Uses pre-loaded database or API.
        """
        # Try major city database first
        city_key = f"{city},{state}".lower()
        if city_key in MAJOR_CITY_COORDS:
            return MAJOR_CITY_COORDS[city_key]
        
        # Try geocoding with city name only
        try:
            return self._geocode_address("", city, state)
        except:
            pass
        
        # Fallback to state centroids
        STATE_CENTROIDS = {
            'AL': (32.8067, -86.7113), 'AK': (64.2008, -152.2782), 'AZ': (33.7298, -111.4312),
            'AR': (34.9697, -92.3731), 'CA': (36.1160, -119.6816), 'CO': (39.0598, -105.3111),
            'CT': (41.5978, -72.7554), 'DE': (39.0582, -75.7244), 'FL': (27.9947, -81.7603),
            'GA': (33.0406, -83.6431), 'HI': (21.0943, -157.4983), 'ID': (44.2405, -114.4787),
            'IL': (40.3495, -88.9861), 'IN': (39.8494, -86.2604), 'IA': (42.0115, -93.2105),
            'KS': (38.5266, -96.7265), 'KY': (37.6681, -84.6701), 'LA': (31.1695, -91.8749),
            'ME': (44.6939, -69.3819), 'MD': (39.0458, -76.6413), 'MA': (42.2352, -71.0275),
            'MI': (43.3266, -84.5361), 'MN': (45.6945, -93.9196), 'MS': (32.7416, -89.6787),
            'MO': (38.4561, -92.2884), 'MT': (47.0527, -109.6333), 'NE': (41.4925, -99.9018),
            'NV': (38.8026, -116.4194), 'NH': (43.4525, -71.3096), 'NJ': (40.2989, -74.5501),
            'NM': (34.8405, -106.2371), 'NY': (42.1657, -74.9481), 'NC': (35.6301, -79.8064),
            'ND': (47.5289, -99.7840), 'OH': (40.3888, -82.7649), 'OK': (35.5653, -96.9289),
            'OR': (43.8041, -120.5542), 'PA': (40.5908, -77.2098), 'RI': (41.6809, -71.5118),
            'SC': (34.0007, -81.1637), 'SD': (44.2998, -99.4388), 'TN': (35.7478, -86.6923),
            'TX': (31.9686, -99.9018), 'UT': (39.3210, -111.0937), 'VT': (44.0459, -72.7107),
            'VA': (37.7693, -78.1694), 'WA': (47.4009, -121.4905), 'WV': (38.4912, -82.9006),
            'WI': (44.2685, -89.6165), 'WY': (42.7559, -107.3025)
        }
        
        return STATE_CENTROIDS.get(state.upper())
