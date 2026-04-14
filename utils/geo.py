import pyproj


def build_transformer(proj_wkt: str):
    if not proj_wkt:
        raise RuntimeError("影像缺少投影信息，无法转换到 WGS84")

    src_crs = pyproj.CRS.from_wkt(proj_wkt)
    target_crs = pyproj.CRS.from_epsg(4326)
    return pyproj.Transformer.from_crs(src_crs, target_crs, always_xy=True)


def pixel_to_lon_lat(transform, transformer, px_x: float, px_y: float):
    geo_x = transform[0] + px_x * transform[1] + px_y * transform[2]
    geo_y = transform[3] + px_x * transform[4] + px_y * transform[5]
    lon, lat = transformer.transform(geo_x, geo_y)
    return lon, lat
