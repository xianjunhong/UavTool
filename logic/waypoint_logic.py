from dataclasses import dataclass
from typing import List


@dataclass
class Waypoint:
    px_x: float
    px_y: float
    lon: float
    lat: float
    marker: object


def format_waypoint(index: int, lon: float, lat: float) -> str:
    return f"{index}. lon={lon:.8f}, lat={lat:.8f}"


def nearest_waypoint_index(waypoints: List[Waypoint], px_x: float, px_y: float, threshold: float = 22.0) -> int:
    remove_idx = -1
    min_dist2 = float("inf")
    threshold2 = threshold * threshold

    for i, wp in enumerate(waypoints):
        dx = wp.px_x - px_x
        dy = wp.px_y - px_y
        dist2 = dx * dx + dy * dy
        if dist2 < min_dist2 and dist2 <= threshold2:
            min_dist2 = dist2
            remove_idx = i

    return remove_idx
