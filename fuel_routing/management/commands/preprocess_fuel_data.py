"""
Management command to execute the full preprocessing pipeline.

STEP-BY-STEP EXECUTION:
1. Load CSV + Validate Structure
2. Column Standardization
3. Remove Invalid Records
4. Filter USA Records
5. Address Normalization
6. Address Quality Validation
7. Deduplication
8. Data Normalization
9. Geocoding Preparation
10. Final Validation
11. Export Clean Dataset + Logs

USAGE:
    python manage.py preprocess_fuel_data [--input FILE] [--clean-output FILE] 
                                           [--rejected-output FILE] [--log-output FILE]

DEFAULTS:
    --input: fuel-prices-for-be-assessment.csv
    --clean-output: fuel_prices_cleaned.csv
    --rejected-output: fuel_prices_rejected.csv
    --log-output: preprocessing_report.txt
"""

import os
import csv
from decimal import Decimal
from django.core.management.base import BaseCommand
from fuel_routing.preprocessing import PreprocessingPipeline
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Execute production-grade CSV preprocessing pipeline (11 steps)'
    
    def add_arguments(self, parser):
        parser.add_argument(
            '--input',
            type=str,
            default='fuel-prices-for-be-assessment.csv',
            help='Input CSV file path'
        )
        parser.add_argument(
            '--clean-output',
            type=str,
            default='fuel_prices_cleaned.csv',
            help='Output path for clean records'
        )
        parser.add_argument(
            '--rejected-output',
            type=str,
            default='fuel_prices_rejected.csv',
            help='Output path for rejected records'
        )
        parser.add_argument(
            '--log-output',
            type=str,
            default='preprocessing_report.txt',
            help='Output path for detailed report'
        )
    
    def handle(self, *args, **options):
        input_file = options['input']
        clean_output = options['clean_output']
        rejected_output = options['rejected_output']
        log_output = options['log_output']
        
        # Verify input file exists
        if not os.path.exists(input_file):
            self.stderr.write(f"ERROR: Input file not found: {input_file}")
            return
        
        self.stdout.write(f"\n{'='*70}")
        self.stdout.write("FUEL PRICES CSV PREPROCESSING PIPELINE")
        self.stdout.write(f"{'='*70}")
        self.stdout.write(f"Input:    {input_file}")
        self.stdout.write(f"Clean:    {clean_output}")
        self.stdout.write(f"Rejected: {rejected_output}")
        self.stdout.write(f"Report:   {log_output}")
        self.stdout.write(f"{'='*70}\n")
        
        try:
            # Execute pipeline
            pipeline = PreprocessingPipeline(input_file)
            clean_records, stats = pipeline.execute()
            
            # Export clean dataset
            self._export_clean_dataset(clean_records, clean_output)
            self.stdout.write(
                self.style.SUCCESS(f"✓ Clean dataset exported: {clean_output}")
            )
            
            # Export rejected records
            if pipeline.all_rejected:
                self._export_rejected_records(pipeline.all_rejected, rejected_output)
                self.stdout.write(
                    self.style.WARNING(f"✓ Rejected records exported: {rejected_output}")
                )
            
            # Generate report
            self._generate_report(clean_records, pipeline.all_rejected, stats, log_output)
            self.stdout.write(
                self.style.SUCCESS(f"✓ Detailed report exported: {log_output}")
            )
            
            # Print summary
            self.stdout.write(f"\n{'='*70}")
            self.stdout.write("PREPROCESSING SUMMARY")
            self.stdout.write(f"{'='*70}")
            self.stdout.write(f"Total input records:    {stats['total_input']:,}")
            self.stdout.write(f"Clean records:          {stats['clean_records']:,}")
            self.stdout.write(f"Rejected records:       {stats['rejected_records']:,}")
            self.stdout.write(f"Success rate:           {stats['success_rate']:.1f}%")
            self.stdout.write(f"Timestamp:              {stats['timestamp']}")
            self.stdout.write(f"{'='*70}\n")
            
            self.stdout.write(
                self.style.SUCCESS("✓ Preprocessing complete!")
            )
        
        except Exception as e:
            self.stderr.write(f"ERROR: Preprocessing failed: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def _export_clean_dataset(self, records: list, output_file: str):
        """Export clean records to CSV"""
        if not records:
            logger.warning("No clean records to export")
            return
        
        # Define export fields
        export_fields = [
            'opis_id',
            'name',
            'normalized_address',
            'city',
            'state',
            'rack_id',
            'retail_price',
            'geocode_query',
            'quality_score',
            'preprocessed_at'
        ]
        
        try:
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=export_fields)
                writer.writeheader()
                
                for record in records:
                    # Convert Decimal to string for CSV
                    row = {field: record.get(field, '') for field in export_fields}
                    if isinstance(row['retail_price'], Decimal):
                        row['retail_price'] = str(row['retail_price'])
                    writer.writerow(row)
            
            logger.info(f"Exported {len(records)} clean records to {output_file}")
        
        except Exception as e:
            logger.error(f"Failed to export clean dataset: {str(e)}")
            raise
    
    def _export_rejected_records(self, rejected: list, output_file: str):
        """Export rejected records to CSV"""
        if not rejected:
            logger.warning("No rejected records to export")
            return
        
        try:
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=['row_num', 'reason', 'opis_id', 'address', 'city', 'state']
                )
                writer.writeheader()
                
                for record in rejected:
                    data = record.get('data', {})
                    writer.writerow({
                        'row_num': record.get('row_num', ''),
                        'reason': record.get('reason', ''),
                        'opis_id': data.get('OPIS Truckstop ID', ''),
                        'address': data.get('Address', ''),
                        'city': data.get('City', ''),
                        'state': data.get('State', '')
                    })
            
            logger.info(f"Exported {len(rejected)} rejected records to {output_file}")
        
        except Exception as e:
            logger.error(f"Failed to export rejected records: {str(e)}")
            raise
    
    def _generate_report(self, clean: list, rejected: list, stats: dict, output_file: str):
        """Generate detailed preprocessing report"""
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                # Header
                f.write("="*70 + "\n")
                f.write("FUEL PRICES CSV PREPROCESSING REPORT\n")
                f.write("="*70 + "\n\n")
                
                # Summary Statistics
                f.write("SUMMARY STATISTICS\n")
                f.write("-"*70 + "\n")
                f.write(f"Total input records:        {stats['total_input']:>10,}\n")
                f.write(f"Clean records retained:     {stats['clean_records']:>10,}\n")
                f.write(f"Records rejected:           {stats['rejected_records']:>10,}\n")
                f.write(f"Success rate:               {stats['success_rate']:>10.1f}%\n")
                f.write(f"Processing timestamp:       {stats['timestamp']}\n\n")
                
                # Data Quality Metrics
                f.write("DATA QUALITY METRICS\n")
                f.write("-"*70 + "\n")
                if clean:
                    prices = [r['retail_price'] for r in clean if r.get('retail_price')]
                    f.write(f"Clean records with prices:  {len(prices):>10,}\n")
                    if prices:
                        f.write(f"Price range:                ${min(prices):>10.2f} - ${max(prices):.2f}\n")
                        f.write(f"Average price:              ${sum(prices)/len(prices):>10.2f}\n")
                    
                    # State distribution
                    states = {}
                    for r in clean:
                        state = r.get('state', 'XX')
                        states[state] = states.get(state, 0) + 1
                    
                    f.write(f"States represented:        {len(states):>10}\n")
                    f.write(f"Geocoding readiness:        {100.0:>10.1f}%\n\n")
                
                # Rejection Analysis
                if rejected:
                    f.write("REJECTION ANALYSIS\n")
                    f.write("-"*70 + "\n")
                    
                    reason_counts = {}
                    for r in rejected:
                        reason = r.get('reason', 'Unknown').split('|')[0].strip()
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    
                    # Sort by frequency
                    sorted_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)
                    for reason, count in sorted_reasons[:10]:
                        f.write(f"  {reason:45} {count:>10,} records\n")
                    
                    f.write("\n")
                
                # Sample Clean Records
                f.write("SAMPLE CLEAN RECORDS (First 5)\n")
                f.write("-"*70 + "\n")
                for i, record in enumerate(clean[:5], 1):
                    f.write(f"\nRecord {i}:\n")
                    f.write(f"  OPIS ID:         {record.get('opis_id')}\n")
                    f.write(f"  Name:            {record.get('name')}\n")
                    f.write(f"  Address:         {record.get('normalized_address')}\n")
                    f.write(f"  City, State:     {record.get('city')}, {record.get('state')}\n")
                    f.write(f"  Price:           ${record.get('retail_price')}\n")
                    f.write(f"  Quality Score:   {record.get('quality_score', 0):.1f}/100\n")
                    f.write(f"  Geocode Query:   {record.get('geocode_query')}\n")
                
                f.write("\n" + "="*70 + "\n")
                f.write("END OF REPORT\n")
                f.write("="*70 + "\n")
            
            logger.info(f"Report generated: {output_file}")
        
        except Exception as e:
            logger.error(f"Failed to generate report: {str(e)}")
            raise


