import math
from typing import Sequence, Tuple


Point = Tuple[float, float]


def _lerp(p1: Point, p2: Point, t: float) -> Point:
    return (
        p1[0] + (p2[0] - p1[0]) * t,
        p1[1] + (p2[1] - p1[1]) * t,
    )


def _edge_length(p1: Point, p2: Point) -> float:
    return math.hypot(p2[0] - p1[0], p2[1] - p1[1])


def _order_convex_quadrilateral(points: Sequence[Point]) -> list[Point]:
    if len(points) != 4:
        raise ValueError("整列边界必须恰好包含4个顶点")

    pts = [(float(x), float(y)) for x, y in points]
    for i, p1 in enumerate(pts):
        for p2 in pts[i + 1 :]:
            if _edge_length(p1, p2) <= 1e-6:
                raise ValueError("整列边界包含重复顶点")

    center_x = sum(p[0] for p in pts) / 4.0
    center_y = sum(p[1] for p in pts) / 4.0
    ordered = sorted(pts, key=lambda p: math.atan2(p[1] - center_y, p[0] - center_x))

    cross_sign = 0
    for i in range(4):
        a = ordered[i]
        b = ordered[(i + 1) % 4]
        c = ordered[(i + 2) % 4]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) <= 1e-8:
            raise ValueError("整列边界存在共线顶点，请重新框选")
        sign = 1 if cross > 0 else -1
        if cross_sign == 0:
            cross_sign = sign
        elif sign != cross_sign:
            raise ValueError("整列边界必须是凸四边形")

    return ordered


def column_dividers_from_quadrilateral(
    points: Sequence[Point],
    plot_count: int,
    axis: str = "long",
) -> list[tuple[Point, Point]]:
    """Create shared divider endpoints for plots inside a quadrilateral.

    ``axis="long"`` divides the boundary along its longer pair of opposite
    edges, which matches the usual layout of multiple plots stacked in one
    long column. ``axis="short"`` provides the perpendicular fallback.
    """

    count = int(plot_count)
    if count < 1:
        raise ValueError("小区数量必须大于0")

    ordered = _order_convex_quadrilateral(points)
    p0, p1, p2, p3 = ordered

    pair_01_23 = _edge_length(p0, p1) + _edge_length(p2, p3)
    pair_12_30 = _edge_length(p1, p2) + _edge_length(p3, p0)

    mode = str(axis or "").strip().lower()
    if mode not in ("long", "short"):
        raise ValueError("切分方向仅支持 long 或 short")

    use_pair_12_30 = pair_12_30 >= pair_01_23
    if mode == "short":
        use_pair_12_30 = not use_pair_12_30

    if use_pair_12_30:
        rail_left_start, rail_left_end = p0, p3
        rail_right_start, rail_right_end = p1, p2
    else:
        rail_left_start, rail_left_end = p0, p1
        rail_right_start, rail_right_end = p3, p2

    dividers = []
    for index in range(count + 1):
        t = index / count
        dividers.append(
            (
                _lerp(rail_left_start, rail_left_end, t),
                _lerp(rail_right_start, rail_right_end, t),
            )
        )

    return dividers


def plots_from_dividers(
    dividers: Sequence[tuple[Point, Point]],
) -> list[list[Point]]:
    normalized = [
        (
            (float(left[0]), float(left[1])),
            (float(right[0]), float(right[1])),
        )
        for left, right in dividers
    ]
    if len(normalized) < 2:
        raise ValueError("整列至少需要两条边界线")

    plots = []
    for index in range(len(normalized) - 1):
        left_top, right_top = normalized[index]
        left_bottom, right_bottom = normalized[index + 1]
        plots.append([left_top, right_top, right_bottom, left_bottom])
    return plots


def redistribute_column_dividers(
    dividers: Sequence[tuple[Point, Point]],
) -> list[tuple[Point, Point]]:
    normalized = [
        (
            (float(left[0]), float(left[1])),
            (float(right[0]), float(right[1])),
        )
        for left, right in dividers
    ]
    if len(normalized) < 2:
        raise ValueError("整列至少需要两条边界线")

    count = len(normalized) - 1
    left_start, right_start = normalized[0]
    left_end, right_end = normalized[-1]
    return [
        (
            _lerp(left_start, left_end, index / count),
            _lerp(right_start, right_end, index / count),
        )
        for index in range(count + 1)
    ]


def split_column_quadrilateral(
    points: Sequence[Point],
    plot_count: int,
    axis: str = "long",
) -> list[list[Point]]:
    """Split a convex quadrilateral into adjacent plot quadrilaterals."""

    return plots_from_dividers(
        column_dividers_from_quadrilateral(points, plot_count, axis=axis)
    )
