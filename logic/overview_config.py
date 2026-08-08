"""Shared GeoTIFF overview settings.

Lightweight levels keep smooth multi-scale browsing while avoiding the large
2x overview, which accounts for most of the pyramid storage.
"""

OVERVIEW_FACTORS = (4, 8, 16, 32, 64)
OVERVIEW_RESAMPLING = "AVERAGE"
