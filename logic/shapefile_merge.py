import os
from typing import Callable, Sequence

from PySide6.QtCore import QObject, Signal

from logic.polygon_io import load_polygons_from_vector, save_polygons_to_shapefile
from utils.env_setup import configure_runtime_env


def _normalized_shapefile_path(path: str) -> str:
    normalized = os.path.abspath(os.path.normpath(str(path or "").strip()))
    if not normalized.lower().endswith(".shp"):
        raise ValueError(f"仅支持 Shapefile (.shp): {path}")
    return normalized


def _projection_wkt(shp_path: str) -> str:
    configure_runtime_env()
    from osgeo import ogr

    ogr.UseExceptions()
    ds = ogr.Open(shp_path)
    if ds is None:
        raise RuntimeError(f"无法打开 Shapefile: {shp_path}")

    layer = ds.GetLayer(0)
    if layer is None:
        ds = None
        raise RuntimeError(f"Shapefile 中不存在图层: {shp_path}")

    srs = layer.GetSpatialRef()
    wkt = srs.ExportToWkt() if srs is not None else ""
    layer = None
    ds = None
    return wkt


def merge_shapefiles(
    input_paths: Sequence[str],
    output_path: str,
    progress_callback: Callable[[int, str], None] | None = None,
):
    def report(percent: int, message: str):
        if callable(progress_callback):
            progress_callback(percent, message)

    unique_inputs = []
    seen = set()
    for raw_path in input_paths:
        path = _normalized_shapefile_path(raw_path)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        unique_inputs.append(path)

    if len(unique_inputs) < 2:
        raise ValueError("请至少选择两个不同的 Shapefile")

    for path in unique_inputs:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Shapefile 不存在: {path}")

    output = _normalized_shapefile_path(output_path)
    output_key = os.path.normcase(output)
    if output_key in seen:
        raise ValueError("输出文件不能与任一输入 Shapefile 相同")

    report(2, "正在检查坐标系")
    target_wkt = _projection_wkt(unique_inputs[0])
    for path in unique_inputs[1:]:
        source_wkt = _projection_wkt(path)
        if bool(source_wkt) != bool(target_wkt):
            raise ValueError(
                "输入 Shapefile 的坐标系信息不完整，无法安全合并: "
                f"{os.path.basename(path)}"
            )

    merged_polygons = []
    source_counts = []
    next_column_index = 1
    total = len(unique_inputs)

    for index, path in enumerate(unique_inputs, start=1):
        report(
            5 + int(((index - 1) / total) * 75),
            f"正在读取 {index}/{total}: {os.path.basename(path)}",
        )
        polygons = load_polygons_from_vector(path, target_wkt)
        if not polygons:
            raise ValueError(f"未读取到有效小区多边形: {path}")

        source_column_ids = {}
        for polygon in polygons:
            source_column_id = str(polygon.get("column_id") or "")
            if not source_column_id:
                continue
            if source_column_id not in source_column_ids:
                source_column_ids[source_column_id] = f"column_{next_column_index}"
                next_column_index += 1
            polygon["column_id"] = source_column_ids[source_column_id]

        merged_polygons.extend(polygons)
        source_counts.append(
            {
                "path": path,
                "plot_count": len(polygons),
                "column_count": len(source_column_ids),
            }
        )

    report(85, "正在写入合并后的 Shapefile")
    saved_path = save_polygons_to_shapefile(output, merged_polygons, target_wkt)
    report(100, "Shapefile 合并完成")

    return {
        "output_path": saved_path,
        "input_count": len(unique_inputs),
        "plot_count": len(merged_polygons),
        "column_count": next_column_index - 1,
        "source_counts": source_counts,
    }


class ShapefileMergeWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, input_paths: Sequence[str], output_path: str):
        super().__init__()
        self.input_paths = list(input_paths)
        self.output_path = output_path

    def run(self):
        try:
            result = merge_shapefiles(
                self.input_paths,
                self.output_path,
                progress_callback=self.progress.emit,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
