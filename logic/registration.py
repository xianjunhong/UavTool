import csv
import math
import os
from typing import Dict, Sequence, Tuple

import numpy as np
from PySide6.QtCore import QObject, Signal

from logic.overview_config import OVERVIEW_FACTORS, OVERVIEW_RESAMPLING
from utils.env_setup import configure_runtime_env


REGISTRATION_PARAMETER_FORMAT = "UavTool registration parameters"
REGISTRATION_PARAMETER_VERSION = "1"


def _to_geo_matrix(gt: Tuple[float, float, float, float, float, float]) -> np.ndarray:
    return np.array(
        [
            [gt[1], gt[2], gt[0]],
            [gt[4], gt[5], gt[3]],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )


def _to_geotransform(matrix: np.ndarray) -> Tuple[float, float, float, float, float, float]:
    return (
        float(matrix[0, 2]),
        float(matrix[0, 0]),
        float(matrix[0, 1]),
        float(matrix[1, 2]),
        float(matrix[1, 0]),
        float(matrix[1, 1]),
    )


def _affine_parameter_matrix(params: Sequence[float]) -> np.ndarray:
    a, b, c, d, e, f = [float(value) for value in params]
    return np.array(
        [
            [a, b, c],
            [d, e, f],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )


def _linear_transform_is_invertible(linear: np.ndarray) -> bool:
    """Return whether a 2x2 transform is invertible, independent of coordinate units."""
    values = np.asarray(linear, dtype=float)
    if values.shape != (2, 2) or not np.all(np.isfinite(values)):
        return False

    singular_values = np.linalg.svd(values, compute_uv=False)
    if singular_values[0] <= 0.0:
        return False
    relative_tolerance = np.finfo(float).eps * max(values.shape)
    return bool(singular_values[-1] > singular_values[0] * relative_tolerance)


def _world_correction_matrix(
    params: Sequence[float],
    src_geotransform: Sequence[float],
    target_geotransform: Sequence[float],
) -> np.ndarray:
    """Build a transform from the original target map coordinates to corrected map coordinates."""
    target_matrix = _to_geo_matrix(tuple(float(value) for value in target_geotransform))
    if not _linear_transform_is_invertible(target_matrix[:2, :2]):
        raise RuntimeError("target 图像的原始 GeoTransform 不可逆，无法导出可复用参数")

    return (
        _to_geo_matrix(tuple(float(value) for value in src_geotransform))
        @ _affine_parameter_matrix(params)
        @ np.linalg.inv(target_matrix)
    )


def _normalized_csv_path(csv_path: str) -> str:
    output = os.path.abspath(csv_path)
    if not output.lower().endswith(".csv"):
        output += ".csv"
    return output


def save_registration_parameters(
    csv_path: str,
    world_correction: np.ndarray,
    *,
    src_projection_wkt: str,
    target_projection_wkt: str,
    pixel_affine_params: Sequence[float],
    src_geotransform: Sequence[float],
    target_geotransform: Sequence[float],
    metadata: Dict[str, object] = None,
) -> str:
    """Save a reusable map-coordinate correction as a human-readable CSV file."""
    matrix = np.asarray(world_correction, dtype=float)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError("对齐参数矩阵必须是有限的 3x3 矩阵")

    out_path = _normalized_csv_path(csv_path)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    rows = [
        ("format", REGISTRATION_PARAMETER_FORMAT),
        ("version", REGISTRATION_PARAMETER_VERSION),
        ("transform_space", "target_map_coordinates_to_src_map_coordinates"),
        ("matrix_00", repr(float(matrix[0, 0]))),
        ("matrix_01", repr(float(matrix[0, 1]))),
        ("matrix_02", repr(float(matrix[0, 2]))),
        ("matrix_10", repr(float(matrix[1, 0]))),
        ("matrix_11", repr(float(matrix[1, 1]))),
        ("matrix_12", repr(float(matrix[1, 2]))),
        ("src_projection_wkt", src_projection_wkt or ""),
        ("target_projection_wkt", target_projection_wkt or ""),
    ]

    for index, value in enumerate(pixel_affine_params):
        rows.append((f"pixel_affine_{index}", repr(float(value))))
    for index, value in enumerate(src_geotransform):
        rows.append((f"src_geotransform_{index}", repr(float(value))))
    for index, value in enumerate(target_geotransform):
        rows.append((f"target_geotransform_{index}", repr(float(value))))

    for key, value in (metadata or {}).items():
        rows.append((f"metadata_{key}", "" if value is None else str(value)))

    temp_path = out_path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(("key", "value"))
            writer.writerows(rows)
        os.replace(temp_path, out_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return out_path


def _required_float(values: Dict[str, str], key: str) -> float:
    if key not in values:
        raise ValueError(f"参数文件缺少字段: {key}")
    try:
        value = float(values[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数文件字段 {key} 不是有效数字") from exc
    if not math.isfinite(value):
        raise ValueError(f"参数文件字段 {key} 不是有限数字")
    return value


def _optional_float(values: Dict[str, str], key: str):
    if key not in values or not str(values[key]).strip():
        return None
    return _required_float(values, key)


def _optional_int(values: Dict[str, str], key: str):
    value = _optional_float(values, key)
    if value is None:
        return None
    return int(value)


def load_registration_parameters(csv_path: str) -> Dict[str, object]:
    """Load and validate a CSV created by :func:`save_registration_parameters`."""
    path = os.path.abspath(csv_path)
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as csv_file:
            rows = list(csv.reader(csv_file))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"无法读取对齐参数 CSV: {exc}") from exc

    if not rows or len(rows[0]) < 2 or rows[0][0].strip().lower() != "key":
        raise ValueError("不是有效的对齐参数 CSV（缺少 key,value 表头）")

    values = {}
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        if len(row) < 2:
            raise ValueError(f"参数文件字段 {row[0]} 缺少值")
        key = row[0].strip()
        if key in values:
            raise ValueError(f"参数文件包含重复字段: {key}")
        values[key] = row[1]

    if values.get("format") != REGISTRATION_PARAMETER_FORMAT:
        raise ValueError("该 CSV 不是 UavTool 导出的对齐参数文件")
    if values.get("version") != REGISTRATION_PARAMETER_VERSION:
        raise ValueError(f"不支持的对齐参数版本: {values.get('version', '')}")
    if values.get("transform_space") != "target_map_coordinates_to_src_map_coordinates":
        raise ValueError("参数文件的坐标变换类型不受支持")

    matrix = np.array(
        [
            [
                _required_float(values, "matrix_00"),
                _required_float(values, "matrix_01"),
                _required_float(values, "matrix_02"),
            ],
            [
                _required_float(values, "matrix_10"),
                _required_float(values, "matrix_11"),
                _required_float(values, "matrix_12"),
            ],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )
    if not _linear_transform_is_invertible(matrix[:2, :2]):
        raise ValueError("参数文件中的变换矩阵不可逆")

    return {
        "path": path,
        "world_correction": matrix,
        "src_projection_wkt": values.get("src_projection_wkt", ""),
        "target_projection_wkt": values.get("target_projection_wkt", ""),
        "point_count": _optional_int(values, "metadata_point_count"),
        "rmse": _optional_float(values, "metadata_rmse"),
        "loo_rmse": _optional_float(values, "metadata_loo_rmse"),
        "max_error": _optional_float(values, "metadata_max_error"),
        "determinant": _optional_float(values, "metadata_determinant"),
        "condition_number": _optional_float(values, "metadata_condition_number"),
        "source_image": values.get("metadata_source_image", ""),
        "target_image": values.get("metadata_target_image", ""),
    }


def _projections_are_equivalent(first_wkt: str, second_wkt: str, osr_module) -> bool:
    first = (first_wkt or "").strip()
    second = (second_wkt or "").strip()
    if not first or not second:
        return first == second
    if first == second:
        return True

    first_srs = osr_module.SpatialReference()
    second_srs = osr_module.SpatialReference()
    try:
        if first_srs.ImportFromWkt(first) != 0 or second_srs.ImportFromWkt(second) != 0:
            return False
        return bool(first_srs.IsSame(second_srs))
    except Exception:
        return False


def solve_affine(target_points: Sequence[Tuple[float, float]], src_points: Sequence[Tuple[float, float]]) -> np.ndarray:
    n = len(target_points)
    if n < 3:
        raise ValueError("至少需要3对控制点")

    if len(src_points) != n:
        raise ValueError("src 和 target 控制点数量不一致")

    a_mat = np.zeros((2 * n, 6), dtype=float)
    b_vec = np.zeros((2 * n,), dtype=float)

    for i, ((tx, ty), (sx, sy)) in enumerate(zip(target_points, src_points)):
        a_mat[2 * i, 0:3] = [tx, ty, 1.0]
        a_mat[2 * i + 1, 3:6] = [tx, ty, 1.0]
        b_vec[2 * i] = sx
        b_vec[2 * i + 1] = sy

    params, _, rank, _ = np.linalg.lstsq(a_mat, b_vec, rcond=None)
    if rank < 6:
        raise ValueError("控制点分布退化（可能近共线），无法稳定求解仿射变换")
    return params


def affine_rmse(params: np.ndarray, target_points: Sequence[Tuple[float, float]], src_points: Sequence[Tuple[float, float]]):
    a, b, c, d, e, f = params
    errors = []
    for (tx, ty), (sx, sy) in zip(target_points, src_points):
        sx2 = a * tx + b * ty + c
        sy2 = d * tx + e * ty + f
        errors.append(((sx2 - sx) ** 2 + (sy2 - sy) ** 2) ** 0.5)

    arr = np.array(errors, dtype=float)
    return float(np.sqrt(np.mean(arr ** 2))), float(np.max(arr))


def leave_one_out_rmse(target_points: Sequence[Tuple[float, float]], src_points: Sequence[Tuple[float, float]]):
    n = len(target_points)
    if n < 4:
        return None

    errs = []
    for i in range(n):
        t_train = [p for j, p in enumerate(target_points) if j != i]
        s_train = [p for j, p in enumerate(src_points) if j != i]
        params = solve_affine(t_train, s_train)

        a, b, c, d, e, f = params
        tx, ty = target_points[i]
        sx, sy = src_points[i]
        sx2 = a * tx + b * ty + c
        sy2 = d * tx + e * ty + f
        errs.append(((sx2 - sx) ** 2 + (sy2 - sy) ** 2) ** 0.5)

    arr = np.array(errs, dtype=float)
    return float(np.sqrt(np.mean(arr ** 2)))


def align_target_to_src_new_tif(
    src_tif_path: str,
    target_tif_path: str,
    src_points: Sequence[Tuple[float, float]],
    target_points: Sequence[Tuple[float, float]],
    output_tif_path: str,
    progress_callback=None,
    parameter_csv_path: str = "",
):
    def report(percent: int, message: str):
        if callable(progress_callback):
            progress_callback(percent, message)

    report(5, "开始配准")

    if len(src_points) != len(target_points):
        raise ValueError("src 与 target 点数必须一致")
    if len(src_points) < 3:
        raise ValueError("至少需要3对点")

    params = solve_affine(target_points, src_points)
    rmse, max_err = affine_rmse(params, target_points, src_points)
    loo_rmse = leave_one_out_rmse(target_points, src_points)
    report(20, "已完成仿射求解")

    configure_runtime_env()
    from osgeo import gdal

    gdal.UseExceptions()

    ds_src = gdal.Open(src_tif_path, gdal.GA_ReadOnly)
    ds_tgt = gdal.Open(target_tif_path, gdal.GA_ReadOnly)
    if ds_src is None or ds_tgt is None:
        raise RuntimeError("无法打开 src 或 target 图像")

    report(35, "已打开源图像")

    src_gt = ds_src.GetGeoTransform()
    src_proj = ds_src.GetProjection()
    target_gt = ds_tgt.GetGeoTransform()
    target_proj = ds_tgt.GetProjection()

    t_src = _to_geo_matrix(src_gt)
    a, b, c, d, e, f = params
    linear = np.array([[a, b], [d, e]], dtype=float)
    det = float(np.linalg.det(linear))
    cond = float(np.linalg.cond(linear))

    if abs(det) < 1e-8:
        raise RuntimeError("变换矩阵近似奇异（det≈0），结果会退化为线或点，请重新选点")
    if cond > 1e8:
        raise RuntimeError("控制点几何条件较差（矩阵病态），请选取更分散的对应点后重试")

    t_tgt_to_src = _affine_parameter_matrix(params)

    t_new = t_src @ t_tgt_to_src
    new_gt = _to_geotransform(t_new)
    world_correction = _world_correction_matrix(params, src_gt, target_gt)

    out_path = output_tif_path
    if not out_path.lower().endswith(".tif") and not out_path.lower().endswith(".tiff"):
        out_path += ".tif"

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    if os.path.exists(out_path):
        os.remove(out_path)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.CreateCopy(out_path, ds_tgt, strict=0)
    if out_ds is None:
        raise RuntimeError("创建输出 TIF 失败")

    report(55, "已复制 target 到新文件")

    out_ds.SetGeoTransform(new_gt)
    if src_proj:
        out_ds.SetProjection(src_proj)

    report(65, "已写入新坐标")

    # Build pyramid for aligned output so it can be loaded smoothly later.
    def ov_callback(complete, _msg, _):
        # Map [0,1] to [70,95]
        mapped = 70 + int(max(0.0, min(1.0, complete)) * 25)
        report(mapped, "正在为对齐结果构建金字塔")
        return 1

    gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
    out_ds.BuildOverviews(OVERVIEW_RESAMPLING, OVERVIEW_FACTORS, callback=ov_callback)
    out_ds.FlushCache()

    out_ds = None
    ds_src = None
    ds_tgt = None

    rmse_note = ""
    if len(src_points) == 3:
        rmse_note = "当前仅3对点，仿射模型会精确穿过控制点，RMSE可能接近0，这并不代表全图误差为0。"

    saved_parameter_path = ""
    if parameter_csv_path:
        report(97, "正在保存可复用对齐参数")
        saved_parameter_path = save_registration_parameters(
            parameter_csv_path,
            world_correction,
            src_projection_wkt=src_proj,
            target_projection_wkt=target_proj,
            pixel_affine_params=params,
            src_geotransform=src_gt,
            target_geotransform=target_gt,
            metadata={
                "source_image": os.path.abspath(src_tif_path),
                "target_image": os.path.abspath(target_tif_path),
                "point_count": len(src_points),
                "rmse": rmse,
                "loo_rmse": loo_rmse,
                "max_error": max_err,
                "determinant": det,
                "condition_number": cond,
            },
        )

    report(100, "配准、金字塔与参数保存完成" if saved_parameter_path else "配准与金字塔构建完成")

    return {
        "mode": "control_points",
        "output_path": out_path,
        "parameter_path": saved_parameter_path,
        "rmse": rmse,
        "loo_rmse": loo_rmse,
        "max_error": max_err,
        "point_count": len(src_points),
        "determinant": det,
        "condition_number": cond,
        "rmse_note": rmse_note,
    }


def apply_registration_parameters_to_tif(
    parameter_csv_path: str,
    target_tif_path: str,
    output_tif_path: str,
    progress_callback=None,
):
    """Apply a saved map-coordinate correction to another raster without loading src."""

    def report(percent: int, message: str):
        if callable(progress_callback):
            progress_callback(percent, message)

    report(5, "正在读取对齐参数")
    parameters = load_registration_parameters(parameter_csv_path)

    configure_runtime_env()
    from osgeo import gdal, osr

    gdal.UseExceptions()
    ds_tgt = gdal.Open(target_tif_path, gdal.GA_ReadOnly)
    if ds_tgt is None:
        raise RuntimeError("无法打开 target 图像")

    report(20, "正在校验 target 图像")
    target_gt = ds_tgt.GetGeoTransform()
    target_proj = ds_tgt.GetProjection()
    expected_target_proj = parameters["target_projection_wkt"]
    if not _projections_are_equivalent(expected_target_proj, target_proj, osr):
        ds_tgt = None
        raise RuntimeError(
            "当前 target 的坐标系与参数文件记录的原 target 坐标系不一致，"
            "不能安全复用该参数"
        )

    target_matrix = _to_geo_matrix(target_gt)
    if not _linear_transform_is_invertible(target_matrix[:2, :2]):
        ds_tgt = None
        raise RuntimeError("当前 target 图像的 GeoTransform 不可逆")

    corrected_matrix = parameters["world_correction"] @ target_matrix
    new_gt = _to_geotransform(corrected_matrix)
    output_projection = parameters["src_projection_wkt"] or target_proj

    out_path = output_tif_path
    if not out_path.lower().endswith(".tif") and not out_path.lower().endswith(".tiff"):
        out_path += ".tif"
    out_path = os.path.abspath(out_path)

    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.CreateCopy(out_path, ds_tgt, strict=0)
    if out_ds is None:
        ds_tgt = None
        raise RuntimeError("创建输出 TIF 失败")

    report(50, "已复制 target 到新文件")
    out_ds.SetGeoTransform(new_gt)
    if output_projection:
        out_ds.SetProjection(output_projection)
    report(65, "已应用对齐参数")

    def ov_callback(complete, _msg, _):
        mapped = 70 + int(max(0.0, min(1.0, complete)) * 25)
        report(mapped, "正在为对齐结果构建金字塔")
        return 1

    gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
    out_ds.BuildOverviews(OVERVIEW_RESAMPLING, OVERVIEW_FACTORS, callback=ov_callback)
    out_ds.FlushCache()

    out_ds = None
    ds_tgt = None
    report(100, "已应用参数并完成金字塔构建")

    return {
        "mode": "parameters",
        "output_path": out_path,
        "parameter_path": parameters["path"],
        "point_count": parameters["point_count"],
        "rmse": parameters["rmse"],
        "loo_rmse": parameters["loo_rmse"],
        "max_error": parameters["max_error"],
        "determinant": parameters["determinant"],
        "condition_number": parameters["condition_number"],
        "rmse_note": "",
    }


class RegistrationWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        src_tif_path: str,
        target_tif_path: str,
        src_points: Sequence[Tuple[float, float]],
        target_points: Sequence[Tuple[float, float]],
        output_tif_path: str,
        exported_parameter_csv_path: str = "",
        imported_parameter_csv_path: str = "",
    ):
        super().__init__()
        self.src_tif_path = src_tif_path
        self.target_tif_path = target_tif_path
        self.src_points = list(src_points)
        self.target_points = list(target_points)
        self.output_tif_path = output_tif_path
        self.exported_parameter_csv_path = exported_parameter_csv_path
        self.imported_parameter_csv_path = imported_parameter_csv_path

    def run(self):
        try:
            if self.imported_parameter_csv_path:
                result = apply_registration_parameters_to_tif(
                    self.imported_parameter_csv_path,
                    self.target_tif_path,
                    self.output_tif_path,
                    progress_callback=lambda p, m: self.progress.emit(p, m),
                )
            else:
                result = align_target_to_src_new_tif(
                    self.src_tif_path,
                    self.target_tif_path,
                    self.src_points,
                    self.target_points,
                    self.output_tif_path,
                    progress_callback=lambda p, m: self.progress.emit(p, m),
                    parameter_csv_path=self.exported_parameter_csv_path,
                )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
