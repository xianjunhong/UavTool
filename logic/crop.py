import json
import math
import os
import shutil
import tempfile
from typing import Sequence, Tuple

from PySide6.QtCore import QObject, Signal

from utils.env_setup import configure_runtime_env


def _pixel_to_geo(gt, px: float, py: float):
    geo_x = gt[0] + px * gt[1] + py * gt[2]
    geo_y = gt[3] + px * gt[4] + py * gt[5]
    return geo_x, geo_y


def _is_close(p1: Tuple[float, float], p2: Tuple[float, float], eps: float = 1e-8) -> bool:
    return abs(p1[0] - p2[0]) <= eps and abs(p1[1] - p2[1]) <= eps


def _clean_points(points: Sequence[Tuple[float, float]]) -> list:
    out = []
    for x, y in points:
        p = (float(x), float(y))
        if not out or not _is_close(out[-1], p):
            out.append(p)
    if len(out) >= 2 and _is_close(out[0], out[-1]):
        out.pop()
    return out


def _orientation(a, b, c):
    val = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
    if abs(val) < 1e-12:
        return 0
    return 1 if val > 0 else 2


def _on_segment(a, b, c):
    return (
        min(a[0], c[0]) - 1e-12 <= b[0] <= max(a[0], c[0]) + 1e-12
        and min(a[1], c[1]) - 1e-12 <= b[1] <= max(a[1], c[1]) + 1e-12
    )


def _segments_intersect(p1, q1, p2, q2):
    o1 = _orientation(p1, q1, p2)
    o2 = _orientation(p1, q1, q2)
    o3 = _orientation(p2, q2, p1)
    o4 = _orientation(p2, q2, q1)

    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(p1, p2, q1):
        return True
    if o2 == 0 and _on_segment(p1, q2, q1):
        return True
    if o3 == 0 and _on_segment(p2, p1, q2):
        return True
    if o4 == 0 and _on_segment(p2, q1, q2):
        return True
    return False


def _is_self_intersecting(points: Sequence[Tuple[float, float]]) -> bool:
    n = len(points)
    if n < 4:
        return False

    for i in range(n):
        a1 = points[i]
        a2 = points[(i + 1) % n]
        for j in range(i + 1, n):
            b1 = points[j]
            b2 = points[(j + 1) % n]

            if i == j:
                continue
            if (i + 1) % n == j:
                continue
            if i == (j + 1) % n:
                continue

            if _segments_intersect(a1, a2, b1, b2):
                return True
    return False


def _reorder_by_angle(points: Sequence[Tuple[float, float]]) -> list:
    cx = sum([p[0] for p in points]) / len(points)
    cy = sum([p[1] for p in points]) / len(points)
    return sorted(points, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))


def normalize_polygon_pixels(polygon_pixels: Sequence[Tuple[float, float]]):
    pts = _clean_points(polygon_pixels)
    if len(pts) < 3:
        raise ValueError("多边形至少需要3个不重复点")

    reordered = False
    if _is_self_intersecting(pts):
        fixed = _reorder_by_angle(pts)
        if _is_self_intersecting(fixed):
            raise ValueError("多边形点序存在自相交，请调整点位顺序")
        pts = fixed
        reordered = True

    return pts, reordered


def crop_tif_with_polygon(
    tif_path: str,
    polygon_pixels: Sequence[Tuple[float, float]],
    output_path: str,
    overwrite: bool = False,
    output_format: str = "tif",
    progress_callback=None,
):
    normalized_pixels, reordered = normalize_polygon_pixels(polygon_pixels)

    def report(percent: int, message: str):
        if callable(progress_callback):
            progress_callback(percent, message)

    configure_runtime_env()
    from osgeo import gdal

    gdal.UseExceptions()

    report(5, "正在读取影像")
    ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError("无法打开待裁剪影像")

    gt = ds.GetGeoTransform()
    proj = ds.GetProjection()
    if not proj:
        raise RuntimeError("影像缺少投影信息，无法执行地理裁剪")

    ring = []
    for px, py in normalized_pixels:
        gx, gy = _pixel_to_geo(gt, px, py)
        ring.append([gx, gy])
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    temp_dir = tempfile.mkdtemp(prefix="uav_crop_")
    cutline_path = os.path.join(temp_dir, "cutline.geojson")

    cutline = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring],
                },
            }
        ],
    }
    with open(cutline_path, "w", encoding="utf-8") as f:
        json.dump(cutline, f)

    fmt = (output_format or "tif").strip().lower()
    if fmt not in ("tif", "png"):
        raise ValueError("output_format 仅支持 'tif' 或 'png'")

    if overwrite and fmt != "tif":
        raise ValueError("仅 GeoTIFF 模式支持覆盖原图")

    if overwrite:
        out_final = tif_path
        out_crop_path = os.path.join(temp_dir, "overwrite_result.tif")
    else:
        out_final = output_path
        out_crop_path = output_path

    out_dir = os.path.dirname(out_final)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    if fmt == "tif":
        if not out_final.lower().endswith(".tif") and not out_final.lower().endswith(".tiff"):
            out_final += ".tif"
            if not overwrite:
                out_crop_path = out_final
    else:
        if not out_final.lower().endswith(".png"):
            out_final += ".png"
            out_crop_path = out_final

    if os.path.exists(out_crop_path):
        os.remove(out_crop_path)

    report(15, "正在执行裁剪")
    if reordered:
        report(16, "检测到自相交点序，已自动重排为有效多边形")

    def warp_callback(complete, _message, _):
        mapped = 15 + int(max(0.0, min(1.0, complete)) * 55)
        report(mapped, "正在执行裁剪")
        return 1

    if fmt == "tif":
        warp_opts = gdal.WarpOptions(
            format="GTiff",
            cutlineDSName=cutline_path,
            cropToCutline=True,
            dstNodata=0,
            dstAlpha=True,
            multithread=True,
            creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
            callback=warp_callback,
        )
    else:
        warp_opts = gdal.WarpOptions(
            format="PNG",
            cutlineDSName=cutline_path,
            cropToCutline=True,
            dstNodata=0,
            dstAlpha=True,
            callback=warp_callback,
        )

    out_ds = gdal.Warp(out_crop_path, ds, options=warp_opts)
    if out_ds is None:
        raise RuntimeError("裁剪失败，可能是多边形无效或与影像不相交")

    ov_count = 0
    if fmt == "tif":
        report(72, "正在构建金字塔")
        factors = [2, 4, 8, 16, 32, 64, 128]

        def ov_callback(complete, _message, _):
            mapped = 72 + int(max(0.0, min(1.0, complete)) * 23)
            report(mapped, "正在构建金字塔")
            return 1

        gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
        out_ds.BuildOverviews("AVERAGE", factors, callback=ov_callback)
        ov_count = out_ds.GetRasterBand(1).GetOverviewCount()
    out_ds.FlushCache()
    out_ds = None
    ds = None

    if overwrite:
        if os.path.exists(out_final):
            os.remove(out_final)
        os.replace(out_crop_path, out_final)

    report(100, "裁剪完成")
    shutil.rmtree(temp_dir, ignore_errors=True)

    return {
        "output_path": out_final,
        "overview_count": ov_count,
    }


class CropWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        tif_path: str,
        polygon_pixels: Sequence[Tuple[float, float]],
        output_path: str,
        overwrite: bool,
    ):
        super().__init__()
        self.tif_path = tif_path
        self.polygon_pixels = list(polygon_pixels)
        self.output_path = output_path
        self.overwrite = overwrite

    def run(self):
        try:
            result = crop_tif_with_polygon(
                self.tif_path,
                self.polygon_pixels,
                self.output_path,
                overwrite=self.overwrite,
                progress_callback=lambda p, m: self.progress.emit(p, m),
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
