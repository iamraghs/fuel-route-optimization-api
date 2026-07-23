"""
Management command to clean up expired RouteCache entries.

Usage:
    python manage.py cleanup_routecache                    # Dry-run: show what would be deleted
    python manage.py cleanup_routecache --apply            # Actually delete expired entries
    python manage.py cleanup_routecache --days 7           # Delete entries older than 7 days
    python manage.py cleanup_routecache --apply --days 30  # Delete entries expired or older than 30 days
"""
import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from fuel_routing.models import RouteCache

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Clean up expired or stale RouteCache entries"

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help="Actually delete entries (default is dry-run)"
        )
        parser.add_argument(
            '--days',
            type=int,
            default=7,
            help="Delete entries older than this many days (default: 7)"
        )
        parser.add_argument(
            '--quiet',
            action='store_true',
            help="Suppress output"
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        max_age_days = options['days']
        quiet = options['quiet']

        cutoff = timezone.now() - timedelta(days=max_age_days)

        # Expired entries (past expires_at)
        expired = RouteCache.objects.filter(expires_at__lt=timezone.now())

        # Stale valid entries (not accessed in max_age_days)
        stale = RouteCache.objects.filter(
            is_valid=True,
            expires_at__gte=timezone.now(),
            last_accessed_at__lt=cutoff,
        )

        # Invalid entries older than 1 day (keep recent invalids for debugging)
        old_invalid = RouteCache.objects.filter(
            is_valid=False,
            created_at__lt=timezone.now() - timedelta(days=1),
        )

        expired_count = expired.count()
        stale_count = stale.count()
        invalid_count = old_invalid.count()
        total = expired_count + stale_count + invalid_count

        if quiet:
            if apply_changes:
                expired.delete()
                stale.delete()
                old_invalid.delete()
            return

        self.stdout.write(f"RouteCache Cleanup Report (dry-run={not apply_changes})")
        self.stdout.write(f"  Expired entries:      {expired_count}")
        self.stdout.write(f"  Stale valid entries:   {stale_count} (> {max_age_days}d old)")
        self.stdout.write(f"  Old invalid entries:   {invalid_count}")
        self.stdout.write(f"  ───────────────────────────────────")
        self.stdout.write(f"  Total candidates:      {total}")

        if apply_changes and total > 0:
            expired.delete()
            stale.delete()
            old_invalid.delete()
            self.stdout.write(self.style.SUCCESS(f"  Deleted {total} entries"))
        elif not apply_changes and total > 0:
            self.stdout.write(self.style.WARNING(
                "  Run with --apply to delete these entries"
            ))
        else:
            self.stdout.write(self.style.SUCCESS("  Nothing to clean up"))
