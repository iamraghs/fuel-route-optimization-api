"""Route comparison and selection by cost-per-mile efficiency."""
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .optimizer import FuelStopDetail
from .routing import RouteAlternative

logger = logging.getLogger(__name__)


class RouteComparator:
    """Compare multiple routes and select optimal one by cost-per-mile."""

    @staticmethod
    def score_route(
        route: RouteAlternative,
        fuel_stops: List[FuelStopDetail],
        total_fuel_cost: Decimal
    ) -> Dict[str, Any]:
        """Score route on total fuel cost, stop count, and distance."""
        return {
            'total_fuel_cost': float(total_fuel_cost),
            'stop_count': len(fuel_stops),
            'distance_miles': route.distance_miles,
            'duration_hours': route.duration_hours(),
            'cost_per_mile': float(total_fuel_cost) / route.distance_miles if route.distance_miles > 0 else 0,
        }

    @staticmethod
    def select_best_route(
        routes_with_optimizations: List[Tuple[RouteAlternative, List[FuelStopDetail], float]]
    ) -> Tuple[RouteAlternative, List[FuelStopDetail], float]:
        """Select best route by cost-per-mile, then stops, then distance."""
        if not routes_with_optimizations:
            raise ValueError("No routes provided")

        logger.info(f"Evaluating {len(routes_with_optimizations)} routes:")
        for route, stops, cost in routes_with_optimizations:
            cost_per_mile = float(cost) / route.distance_miles if route.distance_miles > 0 else 0
            logger.info(
                f"  {route.route_id}: {route.distance_miles:.1f}mi, "
                f"${cost:.2f} cost (${cost_per_mile:.3f}/mi), {len(stops)} stops"
            )

        best = min(
            routes_with_optimizations,
            key=lambda x: (
                x[2] / x[0].distance_miles,
                len(x[1]),
                x[0].distance_miles
            )
        )

        cost_per_mile = float(best[2]) / best[0].distance_miles
        logger.info(
            f"Selected {best[0].route_id}: {best[0].distance_miles:.1f}mi, "
            f"${best[2]:.2f} cost (${cost_per_mile:.3f}/mi), {len(best[1])} stops"
        )
        return best
