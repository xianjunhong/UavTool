import json
import math
import os
import shutil
import tempfile
from typing import Sequence, Tuple

import numpy as np
from PySide6.QtCore import QObject, Signal

from logic.overview_config import OVERVIEW_FACTORS, OVERVIEW_RESAMPLING
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


def _scale_array_to_byte(arr, nodata=None):
    values = np.asarray(arr)
    valid = np.isfinite(values)
    if nodata is not None:
        valid &= values != nodata
    valid_values = values[valid]
    if valid_values.size == 0:
        return np.zeros(values.shape, dtype=np.uint8)

    lo, hi = np.percentile(valid_values.astype(np.float64), [2.0, 98.0])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.min(valid_values))
        hi = float(np.max(valid_values))
    if hi <= lo:
        out = np.zeros(values.shape, dtype=np.uint8)
        out[valid] = 255 if hi > 0 else 0
        return out

    scaled = (values.astype(np.float64) - lo) * (255.0 / (hi - lo))
    scaled = np.clip(scaled, 0, 255)
    scaled[~valid] = 0
    return scaled.astype(np.uint8)


def _rasterize_pixel_polygon_mask(gdal, polygon_local, width: int, height: int):
    from osgeo import ogr, osr

    mask_ds = gdal.GetDriverByName("MEM").Create(
        "",
        int(width),
        int(height),
        1,
        gdal.GDT_Byte,
    )
    if mask_ds is None:
        raise RuntimeError("无法创建 PNG 掩膜")
    mask_ds.SetGeoTransform((0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
    local_srs = osr.SpatialReference()
    local_srs.ImportFromEPSG(3857)
    mask_ds.SetProjection(local_srs.ExportToWkt())
    mask_ds.GetRasterBand(1).Fill(0)

    vector_ds = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = vector_ds.CreateLayer(
        "mask",
        srs=local_srs,
        geom_type=ogr.wkbPolygon,
    )
    feature = ogr.Feature(layer.GetLayerDefn())
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in polygon_local:
        ring.AddPoint(float(x), float(y))
    if polygon_local[0] != polygon_local[-1]:
        ring.AddPoint(float(polygon_local[0][0]), float(polygon_local[0][1]))
    geometry = ogr.Geometry(ogr.wkbPolygon)
    geometry.AddGeometry(ring)
    feature.SetGeometry(geometry)
    layer.CreateFeature(feature)

    result = gdal.RasterizeLayer(
        mask_ds,
        [1],
        layer,
        burn_values=[255],
        options=["ALL_TOUCHED=TRUE"],
    )
    if result != 0:
        raise RuntimeError("生成 PNG 多边形掩膜失败")

    mask = mask_ds.GetRasterBand(1).ReadAsArray()
    feature = None
    layer = None
    vector_ds = None
    mask_ds = None
    return np.asarray(mask, dtype=np.uint8)


def _rotate_pixel_points(
    polygon_pixels: Sequence[Tuple[float, float]],
    angle_deg: float,
):
    radians = math.radians(float(angle_deg))
    cos_angle = math.cos(radians)
    sin_angle = math.sin(radians)
    return [
        (
            cos_angle * float(px) - sin_angle * float(py),
            sin_angle * float(px) + cos_angle * float(py),
        )
        for px, py in polygon_pixels
    ]


def _read_rotated_pixel_bands(
    gdal,
    ds,
    band_indices,
    polygon_pixels,
    angle_deg: float,
    output_type,
):
    from osgeo import osr

    rotated_polygon = _rotate_pixel_points(polygon_pixels, angle_deg)
    target_min_x = math.floor(min(point[0] for point in rotated_polygon))
    target_min_y = math.floor(min(point[1] for point in rotated_polygon))
    target_max_x = math.ceil(max(point[0] for point in rotated_polygon))
    target_max_y = math.ceil(max(point[1] for point in rotated_polygon))
    target_width = int(target_max_x - target_min_x)
    target_height = int(target_max_y - target_min_y)
    if target_width <= 0 or target_height <= 0:
        raise RuntimeError("旋转后的 PNG 裁剪范围为空")

    # A two-pixel margin supplies valid neighbours for bilinear interpolation
    # along the polygon boundary.
    source_min_x = max(
        0,
        int(math.floor(min(point[0] for point in polygon_pixels))) - 2,
    )
    source_min_y = max(
        0,
        int(math.floor(min(point[1] for point in polygon_pixels))) - 2,
    )
    source_max_x = min(
        ds.RasterXSize,
        int(math.ceil(max(point[0] for point in polygon_pixels))) + 2,
    )
    source_max_y = min(
        ds.RasterYSize,
        int(math.ceil(max(point[1] for point in polygon_pixels))) + 2,
    )
    source_width = source_max_x - source_min_x
    source_height = source_max_y - source_min_y
    if source_width <= 0 or source_height <= 0:
        raise RuntimeError("旋转前的 PNG 裁剪范围为空")

    source_mem = gdal.GetDriverByName("MEM").Create(
        "",
        source_width,
        source_height,
        len(band_indices),
        output_type,
    )
    if source_mem is None:
        raise RuntimeError("无法创建旋转 PNG 的源像素缓存")

    for output_index, source_index in enumerate(band_indices, start=1):
        source_band = ds.GetRasterBand(source_index)
        data = source_band.ReadAsArray(
            source_min_x,
            source_min_y,
            source_width,
            source_height,
        )
        if output_type == gdal.GDT_Byte and source_band.DataType != gdal.GDT_Byte:
            data = _scale_array_to_byte(data, source_band.GetNoDataValue())
        source_mem.GetRasterBand(output_index).WriteArray(data)

    radians = math.radians(float(angle_deg))
    cos_angle = math.cos(radians)
    sin_angle = math.sin(radians)
    source_mem.SetGeoTransform(
        (
            cos_angle * source_min_x - sin_angle * source_min_y,
            cos_angle,
            -sin_angle,
            sin_angle * source_min_x + cos_angle * source_min_y,
            sin_angle,
            cos_angle,
        )
    )

    local_srs = osr.SpatialReference()
    local_srs.ImportFromEPSG(3857)
    local_wkt = local_srs.ExportToWkt()
    source_mem.SetProjection(local_wkt)

    target_mem = gdal.GetDriverByName("MEM").Create(
        "",
        target_width,
        target_height,
        len(band_indices),
        output_type,
    )
    if target_mem is None:
        raise RuntimeError("无法创建旋转 PNG 的输出像素缓存")
    target_mem.SetGeoTransform(
        (target_min_x, 1.0, 0.0, target_min_y, 0.0, 1.0)
    )
    target_mem.SetProjection(local_wkt)

    result = gdal.ReprojectImage(
        source_mem,
        target_mem,
        local_wkt,
        local_wkt,
        gdal.GRA_Bilinear,
    )
    if result != 0:
        raise RuntimeError("按照显示角度旋转 PNG 失败")

    arrays = [
        target_mem.GetRasterBand(index).ReadAsArray()
        for index in range(1, len(band_indices) + 1)
    ]
    polygon_local = [
        (point[0] - target_min_x, point[1] - target_min_y)
        for point in rotated_polygon
    ]
    source_mem = None
    target_mem = None
    return arrays, polygon_local, target_width, target_height


def _crop_png_in_pixel_space(
    ds,
    polygon_pixels: Sequence[Tuple[float, float]],
    output_path: str,
    display_rotation_deg: float = 0.0,
    progress_callback=None,
):
    from osgeo import gdal

    def report(percent: int, message: str):
        if callable(progress_callback):
            progress_callback(percent, message)

    min_x = max(0, int(math.floor(min(p[0] for p in polygon_pixels))))
    min_y = max(0, int(math.floor(min(p[1] for p in polygon_pixels))))
    max_x = min(ds.RasterXSize, int(math.ceil(max(p[0] for p in polygon_pixels))))
    max_y = min(ds.RasterYSize, int(math.ceil(max(p[1] for p in polygon_pixels))))
    width = max_x - min_x
    height = max_y - min_y
    if width <= 0 or height <= 0:
        raise RuntimeError("裁剪范围为空或位于影像之外")

    report(25, "正在读取原始像素")
    alpha_index = None
    data_indices = []
    for band_index in range(1, ds.RasterCount + 1):
        band = ds.GetRasterBand(band_index)
        if band.GetColorInterpretation() == gdal.GCI_AlphaBand:
            alpha_index = band_index
        else:
            data_indices.append(band_index)

    if not data_indices:
        raise RuntimeError("影像不包含可导出的颜色波段")
    if len(data_indices) >= 3:
        data_indices = data_indices[:3]
    else:
        data_indices = data_indices[:1]

    source_type = ds.GetRasterBand(data_indices[0]).DataType
    if source_type in (gdal.GDT_Byte, gdal.GDT_UInt16):
        output_type = source_type
    else:
        output_type = gdal.GDT_Byte

    normalized_rotation = (float(display_rotation_deg) + 180.0) % 360.0 - 180.0
    rotated_arrays = None
    if abs(normalized_rotation) > 1e-9:
        report(35, f"正在按照显示角度旋转 {normalized_rotation:.2f}°")
        rotated_arrays, polygon_local, width, height = _read_rotated_pixel_bands(
            gdal,
            ds,
            data_indices,
            polygon_pixels,
            normalized_rotation,
            output_type,
        )
    else:
        polygon_local = [
            (float(px) - min_x, float(py) - min_y)
            for px, py in polygon_pixels
        ]

    report(45, "正在生成多边形掩膜")
    mask = _rasterize_pixel_polygon_mask(
        gdal,
        polygon_local,
        width,
        height,
    )
    alpha_max = 65535 if output_type == gdal.GDT_UInt16 else 255
    output_alpha = (mask > 0).astype(
        np.uint16 if output_type == gdal.GDT_UInt16 else np.uint8
    ) * alpha_max

    if alpha_index is not None:
        source_alpha_band = ds.GetRasterBand(alpha_index)
        if abs(normalized_rotation) > 1e-9:
            source_alpha = _read_rotated_pixel_bands(
                gdal,
                ds,
                [alpha_index],
                polygon_pixels,
                normalized_rotation,
                output_type,
            )[0][0]
        else:
            source_alpha = source_alpha_band.ReadAsArray(
                min_x,
                min_y,
                width,
                height,
            )
        if output_type == gdal.GDT_Byte and source_alpha.dtype not in (
            np.dtype(np.uint8),
            np.dtype(np.uint16),
        ):
            source_alpha = _scale_array_to_byte(
                source_alpha,
                source_alpha_band.GetNoDataValue(),
            )
        elif output_type == gdal.GDT_Byte and source_alpha.dtype == np.uint16:
            source_alpha = np.rint(source_alpha.astype(np.float64) / 257.0).astype(np.uint8)
        elif output_type == gdal.GDT_UInt16 and source_alpha.dtype == np.uint8:
            source_alpha = source_alpha.astype(np.uint16) * 257
        output_alpha = np.minimum(output_alpha, source_alpha.astype(output_alpha.dtype))

    report(65, "正在写出 PNG")
    mem_ds = gdal.GetDriverByName("MEM").Create(
        "",
        width,
        height,
        len(data_indices) + 1,
        output_type,
    )
    if mem_ds is None:
        raise RuntimeError("无法创建 PNG 输出缓存")

    for output_index, source_index in enumerate(data_indices, start=1):
        source_band = ds.GetRasterBand(source_index)
        if rotated_arrays is not None:
            data = rotated_arrays[output_index - 1]
        else:
            data = source_band.ReadAsArray(min_x, min_y, width, height)
            if output_type == gdal.GDT_Byte and source_band.DataType != gdal.GDT_Byte:
                data = _scale_array_to_byte(data, source_band.GetNoDataValue())
        output_band = mem_ds.GetRasterBand(output_index)
        output_band.WriteArray(data)
        if len(data_indices) == 1:
            output_band.SetColorInterpretation(gdal.GCI_GrayIndex)
        else:
            output_band.SetColorInterpretation(
                [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand][
                    output_index - 1
                ]
            )

    alpha_band = mem_ds.GetRasterBand(len(data_indices) + 1)
    alpha_band.WriteArray(output_alpha)
    alpha_band.SetColorInterpretation(gdal.GCI_AlphaBand)
    mem_ds.FlushCache()

    png_driver = gdal.GetDriverByName("PNG")
    out_ds = png_driver.CreateCopy(output_path, mem_ds, strict=0)
    if out_ds is None:
        raise RuntimeError("创建 PNG 文件失败")
    out_ds.FlushCache()
    out_ds = None
    mem_ds = None
    report(100, "PNG 像素裁剪完成")
    return {
        "output_path": output_path,
        "overview_count": 0,
    }


def crop_tif_with_polygon(
    tif_path: str,
    polygon_pixels: Sequence[Tuple[float, float]],
    output_path: str,
    overwrite: bool = False,
    output_format: str = "tif",
    display_rotation_deg: float = 0.0,
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

    fmt = (output_format or "tif").strip().lower()
    if fmt not in ("tif", "png"):
        raise ValueError("output_format 仅支持 'tif' 或 'png'")
    if overwrite and fmt != "tif":
        raise ValueError("仅 GeoTIFF 模式支持覆盖原图")

    if fmt == "png":
        out_final = output_path
        if not out_final.lower().endswith(".png"):
            out_final += ".png"
        out_dir = os.path.dirname(out_final)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        if os.path.exists(out_final):
            os.remove(out_final)
        try:
            return _crop_png_in_pixel_space(
                ds,
                normalized_pixels,
                out_final,
                display_rotation_deg=display_rotation_deg,
                progress_callback=progress_callback,
            )
        finally:
            ds = None

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

    if overwrite:
        out_final = tif_path
        out_crop_path = os.path.join(temp_dir, "overwrite_result.tif")
    else:
        out_final = output_path
        out_crop_path = output_path

    out_dir = os.path.dirname(out_final)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    if not out_final.lower().endswith(".tif") and not out_final.lower().endswith(".tiff"):
        out_final += ".tif"
        if not overwrite:
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

    out_ds = gdal.Warp(out_crop_path, ds, options=warp_opts)
    if out_ds is None:
        raise RuntimeError("裁剪失败，可能是多边形无效或与影像不相交")

    ov_count = 0
    report(72, "正在构建金字塔")

    def ov_callback(complete, _message, _):
        mapped = 72 + int(max(0.0, min(1.0, complete)) * 23)
        report(mapped, "正在构建金字塔")
        return 1

    gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
    out_ds.BuildOverviews(OVERVIEW_RESAMPLING, OVERVIEW_FACTORS, callback=ov_callback)
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
