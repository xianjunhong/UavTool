import os
from typing import List, Sequence, Tuple

import numpy as np
from PySide6.QtCore import QObject, Signal

from utils.env_setup import configure_runtime_env


def _to_geo_matrix(gt: Tuple[float, float, float, float, float, float]) -> np.ndarray:
    return np.array(
        [
            [gt[1], gt[2], gt[0]],
            [gt[4], gt[5], gt[3]],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )


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

    t_src = _to_geo_matrix(src_gt)
    a, b, c, d, e, f = params
    linear = np.array([[a, b], [d, e]], dtype=float)
    det = float(np.linalg.det(linear))
    cond = float(np.linalg.cond(linear))

    if abs(det) < 1e-8:
        raise RuntimeError("变换矩阵近似奇异（det≈0），结果会退化为线或点，请重新选点")
    if cond > 1e8:
        raise RuntimeError("控制点几何条件较差（矩阵病态），请选取更分散的对应点后重试")

    t_tgt_to_src = np.array(
        [
            [a, b, c],
            [d, e, f],
            [0.0, 0.0, 1.0],
        ],
        dtype=float,
    )

    t_new = t_src @ t_tgt_to_src
    new_gt = (
        float(t_new[0, 2]),
        float(t_new[0, 0]),
        float(t_new[0, 1]),
        float(t_new[1, 2]),
        float(t_new[1, 0]),
        float(t_new[1, 1]),
    )

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
    factors = [2, 4, 8, 16, 32, 64, 128]

    def ov_callback(complete, _msg, _):
        # Map [0,1] to [70,95]
        mapped = 70 + int(max(0.0, min(1.0, complete)) * 25)
        report(mapped, "正在为对齐结果构建金字塔")
        return 1

    gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
    out_ds.BuildOverviews("AVERAGE", factors, callback=ov_callback)
    out_ds.FlushCache()

    report(100, "配准与金字塔构建完成")

    out_ds = None
    ds_src = None
    ds_tgt = None

    rmse_note = ""
    if len(src_points) == 3:
        rmse_note = "当前仅3对点，仿射模型会精确穿过控制点，RMSE可能接近0，这并不代表全图误差为0。"

    return {
        "output_path": out_path,
        "rmse": rmse,
        "loo_rmse": loo_rmse,
        "max_error": max_err,
        "point_count": len(src_points),
        "determinant": det,
        "condition_number": cond,
        "rmse_note": rmse_note,
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
    ):
        super().__init__()
        self.src_tif_path = src_tif_path
        self.target_tif_path = target_tif_path
        self.src_points = list(src_points)
        self.target_points = list(target_points)
        self.output_tif_path = output_tif_path

    def run(self):
        try:
            result = align_target_to_src_new_tif(
                self.src_tif_path,
                self.target_tif_path,
                self.src_points,
                self.target_points,
                self.output_tif_path,
                progress_callback=lambda p, m: self.progress.emit(p, m),
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
