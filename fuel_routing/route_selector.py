"""Route comparison and selection by total fuel cost efficiency."""
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .optimizer import FuelStopDetail
from .routing import RouteAlternative

logger = logging.getLogger(__name__)


class RouteComparator:
    """Compare multiple routes and select optimal one by total fuel cost."""

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
        """Select best route by total fuel cost, then stops, then distance."""
        if not routes_with_optimizations:
            raise ValueError("No routes provided")

        logger.info(f"Evaluating {len(routes_with_optimizations)} routes:")
        for route, stops, cost in routes_with_optimizations:
            logger.info(
                f"  {route.route_id}: {route.distance_miles:.1f}mi, "
                f"${cost:.2f} cost, {len(stops)} stops"
            )

        best = min(
            routes_with_optimizations,
            key=lambda x: (
                x[2],          # total fuel cost (ascending) — primary: minimize absolute cost
                len(x[1]),     # stop count (ascending)       — secondary: fewer stops
                x[0].distance_miles  # distance (ascending)   — tertiary: shorter route
            )
        )

        logger.info(
            f"Selected {best[0].route_id}: {best[0].distance_miles:.1f}mi, "
            f"${best[2]:.2f} cost, {len(best[1])} stops"
        )
        return best
