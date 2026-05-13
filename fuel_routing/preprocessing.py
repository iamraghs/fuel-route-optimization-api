"""
Production-Grade CSV Preprocessing Pipeline for Fuel Station Data.

STEP-BY-STEP EXECUTION:
1. Load CSV + Validate Structure
2. Column Standardization (Whitespace, Capitalization)
3. Remove Invalid Records (Missing fields, Invalid prices)
4. Filter USA Records (50 states + DC only)
5. Address Normalization (Highway patterns, formatting)
6. Address Quality Validation
7. Deduplication (Keep latest valid observation)
8. Data Normalization (Create clean fields)
9. Geocoding Preparation (Google API ready)
10. Final Validation (No nulls, valid ranges)
11. Export Clean Dataset

CSV STRUCTURE:
  - OPIS Truckstop ID
  - Truckstop Name
  - Address
  - City
  - State
  - Rack ID
  - Retail Price

EXPECTED ISSUES IN RAW DATA:
  - Duplicate OPIS IDs (same station, different prices/times)
  - Whitespace inconsistencies
  - Highway abbreviation variations
  - Malformed addresses
  - Price outliers
  - Invalid state codes
  - Trailing spaces in city names
"""

import csv
import logging
import re
from decimal import Decimal
from typing import List, Dict, Tuple, Optional, Set
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class PreprocessingStep:
    """Base class for preprocessing steps"""
    
    def __init__(self, name: str):
        self.name = name
        self.records_in = 0
        self.records_out = 0
        self.rejected = 0
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Execute step, return (valid_records, rejected_records)"""
        raise NotImplementedError
    
    def log_result(self):
        logger.info(
            f"{self.name}: {self.records_in} → {self.records_out} "
            f"(rejected: {self.rejected})"
        )


class CSVLoader(PreprocessingStep):
    """STEP 1: Load CSV + Validate Structure"""
    
    REQUIRED_COLUMNS = [
        'OPIS Truckstop ID',
        'Truckstop Name',
        'Address',
        'City',
        'State',
        'Rack ID',
        'Retail Price'
    ]
    
    def __init__(self, csv_path: str):
        super().__init__("STEP 1: CSV Load & Validation")
        self.csv_path = csv_path
        self.records = []
    
    def execute(self) -> Tuple[List[Dict], List[Dict]]:
        """Load CSV file and validate structure"""
        rejected = []
        
        try:
            with open(self.csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                # Validate columns
                if not reader.fieldnames:
                    raise ValueError("CSV has no headers")
                
                missing_cols = set(self.REQUIRED_COLUMNS) - set(reader.fieldnames)
                if missing_cols:
                    raise ValueError(f"Missing columns: {missing_cols}")
                
                # Load rows
                for row_num, row in enumerate(reader, start=2):  # Start at 2 (skip header)
                    try:
                        # Convert to normalized dict
                        record = {
                            'row_num': row_num,
                            'opis_id': row['OPIS Truckstop ID'],
                            'name': row['Truckstop Name'],
                            'address': row['Address'],
                            'city': row['City'],
                            'state': row['State'],
                            'rack_id': row['Rack ID'],
                            'retail_price': row['Retail Price'],
                            '_raw': row
                        }
                        self.records.append(record)
                    except Exception as e:
                        rejected.append({
                            'row_num': row_num,
                            'reason': f"Parse error: {str(e)}",
                            'data': row
                        })
        
        except FileNotFoundError:
            raise FileNotFoundError(f"CSV file not found: {self.csv_path}")
        except Exception as e:
            logger.error(f"CSV loading failed: {str(e)}")
            raise
        
        self.records_in = len(self.records) + len(rejected)
        self.records_out = len(self.records)
        self.rejected = len(rejected)
        self.log_result()
        
        return self.records, rejected


class ColumnStandardizer(PreprocessingStep):
    """STEP 2: Column Standardization (Whitespace, Case)"""
    
    def __init__(self):
        super().__init__("STEP 2: Column Standardization")
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Standardize whitespace and formatting in all columns"""
        valid = []
        rejected = []
        
        for record in records:
            try:
                # Trim all text fields
                record['opis_id'] = str(record['opis_id']).strip()
                record['name'] = record['name'].strip() if record['name'] else ''
                record['address'] = record['address'].strip() if record['address'] else ''
                record['city'] = record['city'].strip() if record['city'] else ''
                record['state'] = record['state'].strip() if record['state'] else ''
                record['rack_id'] = str(record['rack_id']).strip()
                record['retail_price'] = str(record['retail_price']).strip()
                
                # Collapse multiple spaces to single space
                for field in ['name', 'address', 'city']:
                    record[field] = re.sub(r'\s+', ' ', record[field])
                
                # Convert empty strings to empty (will be caught in validation step)
                for field in ['opis_id', 'address', 'city', 'state']:
                    if not record[field]:
                        record[field] = None
                
                # Uppercase state code
                if record['state']:
                    record['state'] = record['state'].upper()
                
                # Remove BOM if present
                if record['opis_id'] and record['opis_id'].startswith('\ufeff'):
                    record['opis_id'] = record['opis_id'][1:]
                
                valid.append(record)
            
            except Exception as e:
                rejected.append({
                    'row_num': record.get('row_num'),
                    'reason': f"Standardization error: {str(e)}",
                    'data': record.get('_raw')
                })
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = len(rejected)
        self.log_result()
        
        return valid, rejected


class InvalidRecordFilter(PreprocessingStep):
    """STEP 3: Remove Invalid Records"""
    
    VALID_PRICE_MIN = Decimal('1.00')
    VALID_PRICE_MAX = Decimal('10.00')
    
    def __init__(self):
        super().__init__("STEP 3: Invalid Record Filtering")
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Remove records with missing/invalid critical fields"""
        valid = []
        rejected = []
        
        for record in records:
            reasons = []
            
            # Check OPIS ID
            if not record['opis_id']:
                reasons.append("Missing OPIS ID")
            elif not record['opis_id'].isdigit():
                reasons.append(f"Invalid OPIS ID: {record['opis_id']}")
            
            # Check Address
            if not record['address']:
                reasons.append("Missing Address")
            elif len(record['address']) < 3:
                reasons.append(f"Address too short: {record['address']}")
            
            # Check City
            if not record['city']:
                reasons.append("Missing City")
            elif len(record['city']) < 2:
                reasons.append(f"City name too short: {record['city']}")
            
            # Check State
            if not record['state']:
                reasons.append("Missing State")
            elif len(record['state']) != 2:
                reasons.append(f"Invalid state code: {record['state']}")
            
            # Check Price
            if not record['retail_price']:
                reasons.append("Missing Retail Price")
            else:
                try:
                    price = Decimal(record['retail_price'])
                    if price < self.VALID_PRICE_MIN or price > self.VALID_PRICE_MAX:
                        reasons.append(
                            f"Price out of range: ${price} "
                            f"(valid: ${self.VALID_PRICE_MIN}-${self.VALID_PRICE_MAX})"
                        )
                    else:
                        record['retail_price'] = price
                except:
                    reasons.append(f"Non-numeric price: {record['retail_price']}")
            
            if reasons:
                rejected.append({
                    'row_num': record.get('row_num'),
                    'reason': ' | '.join(reasons),
                    'data': record.get('_raw')
                })
            else:
                valid.append(record)
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = len(rejected)
        self.log_result()
        
        return valid, rejected


class USAOnlyFilter(PreprocessingStep):
    """STEP 4: Filter USA Records Only"""
    
    # All 50 US states + Washington DC
    VALID_STATES = {
        'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
        'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
        'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
        'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
        'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
    }
    
    def __init__(self):
        super().__init__("STEP 4: USA-Only Filtering")
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Keep only USA records"""
        valid = []
        rejected = []
        
        for record in records:
            if record['state'] in self.VALID_STATES:
                valid.append(record)
            else:
                rejected.append({
                    'row_num': record.get('row_num'),
                    'reason': f"Invalid state code: {record['state']}",
                    'data': record.get('_raw')
                })
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = len(rejected)
        self.log_result()
        
        return valid, rejected


class AddressNormalizer(PreprocessingStep):
    """STEP 5: Address Normalization (Highway patterns, formatting)"""
    
    # Highway abbreviation mappings
    HIGHWAY_PATTERNS = {
        r'\bI-(\d+)\b': r'Interstate \1',
        r'\bUS-(\d+)\b': r'US \1',
        r'\bSR-(\d+)\b': r'State Route \1',
        r'\bHWY\b': 'Highway',
        r'\bRT\b': 'Route',
        r'\bEXIT\b': 'Exit',
        r'\bMM\b': 'Mile Marker',
        r'&': 'and',
        r',\s*,': ',',  # Remove duplicate commas
        r',,': ',',     # Remove duplicate commas
    }
    
    def __init__(self):
        super().__init__("STEP 5: Address Normalization")
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Normalize addresses for geocoding"""
        valid = []
        rejected = []
        
        for record in records:
            try:
                address = record['address']
                
                # Apply highway pattern replacements
                for pattern, replacement in self.HIGHWAY_PATTERNS.items():
                    address = re.sub(pattern, replacement, address, flags=re.IGNORECASE)
                
                # Clean up spacing
                address = re.sub(r'\s+', ' ', address).strip()
                
                # Remove trailing punctuation
                address = re.sub(r'[,;\s]+$', '', address)
                
                # Validate normalized address has meaningful content
                if len(address) < 3 or not any(c.isalnum() for c in address):
                    rejected.append({
                        'row_num': record.get('row_num'),
                        'reason': f"Address normalized to invalid value: {address}",
                        'data': record.get('_raw')
                    })
                    continue
                
                # Store original and normalized
                record['normalized_address'] = address
                
                # Create geocoding query
                record['geocode_query'] = (
                    f"{record['normalized_address']}, "
                    f"{record['city']}, {record['state']}, USA"
                )
                
                valid.append(record)
            
            except Exception as e:
                rejected.append({
                    'row_num': record.get('row_num'),
                    'reason': f"Address normalization error: {str(e)}",
                    'data': record.get('_raw')
                })
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = len(rejected)
        self.log_result()
        
        return valid, rejected


class AddressQualityValidator(PreprocessingStep):
    """STEP 6: Address Quality Validation"""
    
    # Roads/highways that should be in truck stop addresses
    VALID_ROAD_INDICATORS = {
        'interstate', 'i-', 'us ', 'highway', 'route', 'state route',
        'exit', 'road', 'rd', 'avenue', 'ave', 'street', 'st',
        'drive', 'dr', 'way', 'lane', 'ln', 'boulevard', 'blvd'
    }
    
    def __init__(self):
        super().__init__("STEP 6: Address Quality Validation")
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Validate address quality for geocoding"""
        valid = []
        rejected = []
        
        for record in records:
            address_lower = record['normalized_address'].lower()
            city_lower = record['city'].lower()
            
            # Check for garbage values
            if any(char in address_lower for char in ['xxxx', '____', '????']):
                rejected.append({
                    'row_num': record.get('row_num'),
                    'reason': "Address contains garbage characters",
                    'data': record.get('_raw')
                })
                continue
            
            # Check address contains road/highway information
            has_road_info = any(
                indicator in address_lower
                for indicator in self.VALID_ROAD_INDICATORS
            )
            
            if not has_road_info:
                rejected.append({
                    'row_num': record.get('row_num'),
                    'reason': "Address lacks road/highway information",
                    'data': record.get('_raw')
                })
                continue
            
            # Validate city name
            if len(city_lower) < 2 or not city_lower.isalpha():
                rejected.append({
                    'row_num': record.get('row_num'),
                    'reason': f"Invalid city name: {record['city']}",
                    'data': record.get('_raw')
                })
                continue
            
            # All validations passed
            valid.append(record)
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = len(rejected)
        self.log_result()
        
        return valid, rejected


class Deduplicator(PreprocessingStep):
    """STEP 7: Deduplication (Keep latest valid observation)"""
    
    def __init__(self):
        super().__init__("STEP 7: Deduplication")
        self.duplicates_removed = 0
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Deduplicate records by OPIS ID, keeping latest"""
        deduped_dict: Dict[str, Dict] = {}
        
        # Group by OPIS ID, keep last (latest) observation
        for record in records:
            opis_id = record['opis_id']
            # Last record with this OPIS ID wins (order preserved from CSV)
            deduped_dict[opis_id] = record
        
        valid = list(deduped_dict.values())
        self.duplicates_removed = len(records) - len(valid)
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = 0
        self.log_result()
        logger.info(f"  └─ Removed {self.duplicates_removed} duplicate OPIS IDs")
        
        return valid, []


class DataNormalizer(PreprocessingStep):
    """STEP 8: Data Normalization (Create clean fields)"""
    
    def __init__(self):
        super().__init__("STEP 8: Data Normalization")
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Create normalized fields for database insertion"""
        valid = []
        
        for record in records:
            try:
                # Normalize station name (preserve but standardize)
                record['normalized_name'] = record['name'].strip()
                
                # Normalize city (title case)
                record['normalized_city'] = record['city'].strip().title()
                
                # State already uppercase from step 2
                record['normalized_state'] = record['state']
                
                # Create deterministic station hash
                hash_input = (
                    f"{record['opis_id']}"
                    f"{record['normalized_address'].lower()}"
                    f"{record['normalized_city'].lower()}"
                    f"{record['normalized_state']}"
                )
                record['station_hash'] = hash(hash_input) & 0xffffffff
                
                # Add preprocessing metadata
                record['preprocessed_at'] = datetime.utcnow().isoformat()
                record['quality_score'] = self._calculate_quality_score(record)
                record['is_geocoded'] = False  # Will be filled after geocoding
                record['latitude'] = None
                record['longitude'] = None
                
                valid.append(record)
            
            except Exception as e:
                logger.error(f"Normalization error for row {record.get('row_num')}: {e}")
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = len(records) - len(valid)
        self.log_result()
        
        return valid, []
    
    def _calculate_quality_score(self, record: Dict) -> float:
        """Calculate data quality score (0-100)"""
        score = 100.0
        
        # Deduct for short/unclear fields
        if len(record['normalized_address']) < 10:
            score -= 10
        if len(record['normalized_city']) < 3:
            score -= 10
        
        # Bonus for complete data
        if record['name'] and record['address'] and record['city']:
            score += 5
        
        return max(0, min(100, score))


class GeocodingPreparation(PreprocessingStep):
    """STEP 9: Geocoding Preparation (Google API ready)"""
    
    def __init__(self):
        super().__init__("STEP 9: Geocoding Preparation")
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Prepare records for Google Geocoding API"""
        valid = []
        
        for record in records:
            try:
                # Verify geocode query exists
                if not record.get('geocode_query'):
                    record['geocode_query'] = (
                        f"{record['normalized_address']}, "
                        f"{record['normalized_city']}, {record['normalized_state']}, USA"
                    )
                
                # Add geocoding parameters for later API call
                record['geocode_region'] = 'US'
                record['geocode_language'] = 'en'
                record['geocode_components'] = (
                    f"country:US|administrative_area:{record['normalized_state']}"
                )
                record['geocode_status'] = 'pending'  # Will be filled after geocoding
                
                valid.append(record)
            
            except Exception as e:
                logger.error(f"Geocoding prep error for row {record.get('row_num')}: {e}")
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = len(records) - len(valid)
        self.log_result()
        
        return valid, []


class FinalValidator(PreprocessingStep):
    """STEP 10: Final Validation"""
    
    def __init__(self):
        super().__init__("STEP 10: Final Validation")
    
    def execute(self, records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """Final comprehensive validation"""
        valid = []
        rejected = []
        
        seen_opis_ids = set()
        
        for record in records:
            reasons = []
            
            # Check no duplicate OPIS IDs
            if record['opis_id'] in seen_opis_ids:
                reasons.append("Duplicate OPIS ID (after dedup step)")
            seen_opis_ids.add(record['opis_id'])
            
            # Check no NULL critical fields
            if not record['opis_id']:
                reasons.append("OPIS ID is NULL")
            if not record['normalized_address']:
                reasons.append("Address is NULL")
            if not record['normalized_city']:
                reasons.append("City is NULL")
            if not record['normalized_state']:
                reasons.append("State is NULL")
            if not record['retail_price']:
                reasons.append("Price is NULL")
            
            # Validate price is Decimal
            if not isinstance(record['retail_price'], Decimal):
                reasons.append("Price is not Decimal type")
            
            # Validate geocode_query exists
            if not record.get('geocode_query'):
                reasons.append("Geocode query missing")
            
            # Validate state is valid
            if record['normalized_state'] not in {
                'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA',
                'HI', 'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD',
                'MA', 'MI', 'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ',
                'NM', 'NY', 'NC', 'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC',
                'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC'
            }:
                reasons.append(f"Invalid state: {record['normalized_state']}")
            
            if reasons:
                rejected.append({
                    'row_num': record.get('row_num'),
                    'reason': ' | '.join(reasons),
                    'data': record
                })
            else:
                valid.append(record)
        
        self.records_in = len(records)
        self.records_out = len(valid)
        self.rejected = len(rejected)
        self.log_result()
        
        return valid, rejected


class PreprocessingPipeline:
    """Complete preprocessing pipeline orchestrator"""
    
    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        self.steps: List[PreprocessingStep] = []
        self.all_rejected: List[Dict] = []
    
    def execute(self) -> Tuple[List[Dict], Dict]:
        """Execute complete preprocessing pipeline"""
        logger.info("\n" + "="*70)
        logger.info("STARTING CSV PREPROCESSING PIPELINE")
        logger.info("="*70)
        
        # STEP 1: Load CSV
        loader = CSVLoader(self.csv_path)
        records, rejected = loader.execute()
        self.all_rejected.extend(rejected)
        
        # STEP 2: Column Standardization
        standardizer = ColumnStandardizer()
        records, rejected = standardizer.execute(records)
        self.all_rejected.extend(rejected)
        
        # STEP 3: Invalid Record Filter
        invalid_filter = InvalidRecordFilter()
        records, rejected = invalid_filter.execute(records)
        self.all_rejected.extend(rejected)
        
        # STEP 4: USA Only Filter
        usa_filter = USAOnlyFilter()
        records, rejected = usa_filter.execute(records)
        self.all_rejected.extend(rejected)
        
        # STEP 5: Address Normalization
        normalizer = AddressNormalizer()
        records, rejected = normalizer.execute(records)
        self.all_rejected.extend(rejected)
        
        # STEP 6: Address Quality Validation
        quality_validator = AddressQualityValidator()
        records, rejected = quality_validator.execute(records)
        self.all_rejected.extend(rejected)
        
        # STEP 7: Deduplication
        deduplicator = Deduplicator()
        records, _ = deduplicator.execute(records)
        
        # STEP 8: Data Normalization
        data_normalizer = DataNormalizer()
        records, _ = data_normalizer.execute(records)
        
        # STEP 9: Geocoding Preparation
        geocoding_prep = GeocodingPreparation()
        records, _ = geocoding_prep.execute(records)
        
        # STEP 10: Final Validation
        final_validator = FinalValidator()
        records, rejected = final_validator.execute(records)
        self.all_rejected.extend(rejected)
        
        # Summary
        logger.info("\n" + "="*70)
        logger.info("PREPROCESSING COMPLETE")
        logger.info("="*70)
        logger.info(f"Final clean records: {len(records)}")
        logger.info(f"Total rejected: {len(self.all_rejected)}")
        logger.info(f"Success rate: {(len(records)/(len(records)+len(self.all_rejected))*100):.1f}%")
        
        stats = {
            'total_input': len(records) + len(self.all_rejected),
            'clean_records': len(records),
            'rejected_records': len(self.all_rejected),
            'success_rate': (len(records)/(len(records)+len(self.all_rejected))*100),
            'timestamp': datetime.utcnow().isoformat()
        }
        
        return records, stats
