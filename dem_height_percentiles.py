"""批量提取 DEM/DSM GeoTIFF 的高程百分位。

使用方法：
1. 修改下方“参数区”的变量。
2. 运行：python dem_height_percentiles.py
3. 结果会打印到终端，并按 OUTPUT_CSV 配置保存为 CSV。
"""

import csv
import os
from pathlib import Path

import numpy as np

from utils.env_setup import configure_runtime_env


# ============================== 参数区 ==============================

# 可以填写单个 .tif/.tiff，也可以填写包含多个 DEM 的文件夹。
INPUT_PATH = r"C:\Users\Frank\Desktop\多次测试高度\dems"

# 需要提取的高程百分位，可任意增删，例如 [90, 95, 98, 99]。
PERCENTILES = [95, 99]

# 输入为文件夹时，是否搜索所有子文件夹。
SEARCH_RECURSIVELY = False

# 高程所在波段，通常为第 1 波段。
HEIGHT_BAND_INDEX = 1

# 自动使用 GeoTIFF 的 Alpha 波段过滤裁剪区域外的像元。
USE_ALPHA_MASK = True

# 除 GeoTIFF 自带 NoData 外，额外排除的数值。DEM 裁剪结果通常用 0 表示无效。
EXCLUDE_VALUES = [0.0]

# 可选：计算相对地面高度。
# 设为 None：只输出 P95、P99 等绝对高程。
# 设为 5：用全部有效像元的 P5 作为地面高程，并额外输出 P95-P5、P99-P5。
GROUND_PERCENTILE = None

# 结果 CSV。设为空字符串 "" 时只打印，不保存。
OUTPUT_CSV = Path(INPUT_PATH,"dem_height_percentiles.csv")

# 分块读取的行数，避免一次读取整幅大图。
BLOCK_ROWS = 1024

# 输出小数位数。
DECIMAL_PLACES = 4

# ===================================================================


def _validate_percentile(value, label):
    number = float(value)
    if not 0.0 <= number <= 100.0:
        raise ValueError(f"{label} 必须在 0～100 之间，当前值为 {value}")
    return number


def _percentile_label(value):
    number = float(value)
    if number.is_integer():
        return f"p{int(number)}"
    return f"p{str(number).replace('.', '_')}"


def find_dem_files(input_path, recursive=False):
    path = Path(input_path)
    if path.is_file():
        if path.suffix.lower() not in {".tif", ".tiff"}:
            raise ValueError(f"输入文件不是 TIF: {path}")
        return [path]

    if not path.is_dir():
        raise FileNotFoundError(f"输入路径不存在: {path}")

    pattern = "**/*" if recursive else "*"
    files = sorted(
        item
        for item in path.glob(pattern)
        if item.is_file() and item.suffix.lower() in {".tif", ".tiff"}
    )
    if not files:
        raise FileNotFoundError(f"文件夹中没有找到 TIF: {path}")
    return files


def _find_alpha_band(dataset, gdal):
    if not USE_ALPHA_MASK:
        return None
    for index in range(1, dataset.RasterCount + 1):
        band = dataset.GetRasterBand(index)
        if band.GetColorInterpretation() == gdal.GCI_AlphaBand:
            return band
    return None


def _read_valid_heights(dataset, gdal):
    if HEIGHT_BAND_INDEX < 1 or HEIGHT_BAND_INDEX > dataset.RasterCount:
        raise ValueError(
            f"高程波段 {HEIGHT_BAND_INDEX} 超出范围，"
            f"当前影像共有 {dataset.RasterCount} 个波段"
        )

    height_band = dataset.GetRasterBand(HEIGHT_BAND_INDEX)
    alpha_band = _find_alpha_band(dataset, gdal)
    nodata = height_band.GetNoDataValue()
    chunks = []

    for y_offset in range(0, dataset.RasterYSize, BLOCK_ROWS):
        row_count = min(BLOCK_ROWS, dataset.RasterYSize - y_offset)
        heights = height_band.ReadAsArray(
            0,
            y_offset,
            dataset.RasterXSize,
            row_count,
        )
        if heights is None:
            raise RuntimeError(f"读取第 {y_offset} 行附近的高程数据失败")

        heights = np.asarray(heights)
        valid = np.isfinite(heights)

        if nodata is not None:
            if np.isnan(nodata):
                valid &= ~np.isnan(heights)
            else:
                valid &= heights != nodata

        for excluded in EXCLUDE_VALUES:
            valid &= heights != float(excluded)

        if alpha_band is not None:
            alpha = alpha_band.ReadAsArray(
                0,
                y_offset,
                dataset.RasterXSize,
                row_count,
            )
            if alpha is None:
                raise RuntimeError(f"读取第 {y_offset} 行附近的 Alpha 数据失败")
            valid &= np.asarray(alpha) > 0

        if np.any(valid):
            chunks.append(heights[valid].astype(np.float64, copy=False))

    if not chunks:
        raise ValueError("没有可用于统计的有效高程像元")
    return np.concatenate(chunks)


def analyze_dem(tif_path, percentiles=None, ground_percentile=None):
    configure_runtime_env()
    from osgeo import gdal

    gdal.UseExceptions()
    dataset = gdal.Open(os.fspath(tif_path), gdal.GA_ReadOnly)
    if dataset is None:
        raise RuntimeError(f"无法打开 TIF: {tif_path}")

    try:
        values = _read_valid_heights(dataset, gdal)
        selected_percentiles = [
            _validate_percentile(value, "高程百分位")
            for value in (PERCENTILES if percentiles is None else percentiles)
        ]

        result = {
            "file_name": Path(tif_path).name,
            "file_path": str(Path(tif_path).resolve()),
            "width": dataset.RasterXSize,
            "height": dataset.RasterYSize,
            "valid_pixel_count": int(values.size),
            "minimum": float(np.min(values)),
            "maximum": float(np.max(values)),
            "mean": float(np.mean(values)),
            "median": float(np.median(values)),
        }

        for percentile in selected_percentiles:
            result[_percentile_label(percentile)] = float(
                np.percentile(values, percentile)
            )

        selected_ground = (
            GROUND_PERCENTILE
            if ground_percentile is None
            else ground_percentile
        )
        if selected_ground is not None:
            selected_ground = _validate_percentile(
                selected_ground,
                "地面百分位",
            )
            ground_value = float(np.percentile(values, selected_ground))
            ground_label = _percentile_label(selected_ground)
            result["ground_percentile"] = selected_ground
            result["ground_elevation"] = ground_value
            for percentile in selected_percentiles:
                label = _percentile_label(percentile)
                result[f"{label}_minus_{ground_label}"] = (
                    result[label] - ground_value
                )

        return result
    finally:
        dataset = None


def _format_number(value):
    return f"{float(value):.{DECIMAL_PLACES}f}"


def print_result(result, percentile_labels):
    parts = [
        result["file_name"],
        f"有效像元={result['valid_pixel_count']}",
        f"最小值={_format_number(result['minimum'])}",
        f"中位数={_format_number(result['median'])}",
        f"平均值={_format_number(result['mean'])}",
    ]
    for label in percentile_labels:
        parts.append(f"{label.upper()}={_format_number(result[label])}")
    parts.append(f"最大值={_format_number(result['maximum'])}")

    if "ground_elevation" in result:
        parts.append(f"地面高程={_format_number(result['ground_elevation'])}")
        ground_label = _percentile_label(result["ground_percentile"])
        for label in percentile_labels:
            relative_key = f"{label}_minus_{ground_label}"
            parts.append(
                f"{label.upper()}相对高度={_format_number(result[relative_key])}"
            )

    print(" | ".join(parts))


def save_results_csv(results, output_csv):
    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    base_fields = [
        "file_name",
        "file_path",
        "width",
        "height",
        "valid_pixel_count",
        "minimum",
        "median",
        "mean",
    ]
    dynamic_fields = [
        key
        for key in results[0]
        if key not in base_fields and key != "maximum"
    ]
    fieldnames = base_fields + dynamic_fields + ["maximum"]

    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    return output_path


def main():
    selected_percentiles = [
        _validate_percentile(value, "高程百分位")
        for value in PERCENTILES
    ]
    percentile_labels = [
        _percentile_label(value)
        for value in selected_percentiles
    ]
    dem_files = find_dem_files(INPUT_PATH, SEARCH_RECURSIVELY)

    results = []
    failures = []
    for tif_path in dem_files:
        try:
            result = analyze_dem(
                tif_path,
                percentiles=selected_percentiles,
                ground_percentile=GROUND_PERCENTILE,
            )
            results.append(result)
            print_result(result, percentile_labels)
        except Exception as exc:
            failures.append((tif_path, str(exc)))
            print(f"[失败] {tif_path}: {exc}")

    if not results:
        raise RuntimeError("所有 DEM 均统计失败")

    if OUTPUT_CSV:
        saved_path = save_results_csv(results, OUTPUT_CSV)
        print(f"\nCSV 已保存: {saved_path}")

    if failures:
        print(f"\n成功 {len(results)} 个，失败 {len(failures)} 个。")
    else:
        print(f"\n完成，共处理 {len(results)} 个 DEM。")


if __name__ == "__main__":
    main()
