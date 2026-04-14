import json
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


def crop_tif_with_polygon(
    tif_path: str,
    polygon_pixels: Sequence[Tuple[float, float]],
    output_path: str,
    overwrite: bool = False,
    progress_callback=None,
):
    if len(polygon_pixels) < 3:
        raise ValueError("多边形至少需要3个点")

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
    for px, py in polygon_pixels:
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

    def warp_callback(complete, _message, _):
        mapped = 15 + int(max(0.0, min(1.0, complete)) * 55)
        report(mapped, "正在执行裁剪")
        return 1

    warp_opts = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=cutline_path,
        cropToCutline=True,
        dstNodata=0,
        multithread=True,
        creationOptions=["TILED=YES", "COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
        callback=warp_callback,
    )

    out_ds = gdal.Warp(out_crop_path, ds, options=warp_opts)
    if out_ds is None:
        raise RuntimeError("裁剪失败，可能是多边形无效或与影像不相交")

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
