"""使用标定板 DEM 高程作为基准，批量计算其他 DEM 的相对高度。

计算方式：
    相对高度 = 目标 DEM 的指定百分位高程 - 标定板基准高程

修改下方“参数区”后直接运行：
    python dem_relative_height.py
"""

import csv
import os
from pathlib import Path

from dem_height_percentiles import analyze_dem, find_dem_files


# ============================== 参数区 ==============================
# DEM 小图所在文件夹，标定板 TIF 也放在这个文件夹中。
INPUT_FOLDER = r"C:\Users\Frank\Desktop\多次测试高度\dems"

# 标定板文件名，只填写文件名，不需要填写完整路径。
# 标定板与其他文件应来自同一次 DEM/DSM 重建。
CALIBRATION_FILE_NAME = "groud.tif"

# 标定板使用哪个百分位作为基准高程。
# 50 代表中位数，适合表面较平整的标定板；也可以改成 25、75 等。
CALIBRATION_PERCENTILE = 50

# 每个目标文件提取哪些百分位，并分别减去标定板基准。
TARGET_PERCENTILES = [95, 99]

# 是否搜索 INPUT_FOLDER 的所有子文件夹。
SEARCH_RECURSIVELY = False

# 如果标定板 TIF 本身位于 INPUT_FOLDER 中，是否跳过它。
SKIP_CALIBRATION_FILE = True

# CSV 文件名，程序会自动保存到 INPUT_FOLDER 中。
OUTPUT_CSV_FILE_NAME = "dem_relative_heights.csv"

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


def calculate_relative_heights(
    calibration_tif,
    input_folder,
    calibration_percentile=50,
    target_percentiles=(95, 99),
    recursive=False,
    skip_calibration_file=True,
):
    calibration_percentile = _validate_percentile(
        calibration_percentile,
        "标定板百分位",
    )
    target_percentiles = [
        _validate_percentile(value, "目标百分位")
        for value in target_percentiles
    ]
    if not target_percentiles:
        raise ValueError("TARGET_PERCENTILES 不能为空")

    calibration_path = Path(calibration_tif).resolve()
    if not calibration_path.is_file():
        raise FileNotFoundError(f"标定板 TIF 不存在: {calibration_path}")

    calibration_label = _percentile_label(calibration_percentile)
    calibration_result = analyze_dem(
        calibration_path,
        percentiles=[calibration_percentile],
    )
    calibration_elevation = calibration_result[calibration_label]

    tif_files = find_dem_files(input_folder, recursive)
    results = []
    failures = []

    for tif_path in tif_files:
        resolved_path = tif_path.resolve()
        if skip_calibration_file and os.path.normcase(resolved_path) == os.path.normcase(
            calibration_path
        ):
            continue

        try:
            target_result = analyze_dem(
                resolved_path,
                percentiles=target_percentiles,
            )
            row = {
                "file_name": target_result["file_name"],
                "file_path": target_result["file_path"],
                "valid_pixel_count": target_result["valid_pixel_count"],
                "minimum": target_result["minimum"],
                "median": target_result["median"],
                "mean": target_result["mean"],
                "maximum": target_result["maximum"],
                "calibration_file": calibration_path.name,
                "calibration_percentile": calibration_percentile,
                "calibration_elevation": calibration_elevation,
            }

            for percentile in target_percentiles:
                label = _percentile_label(percentile)
                absolute_elevation = target_result[label]
                row[f"absolute_{label}"] = absolute_elevation
                row[f"relative_height_{label}"] = (
                    absolute_elevation - calibration_elevation
                )
            results.append(row)
        except Exception as exc:
            failures.append((str(resolved_path), str(exc)))

    if not results:
        if failures:
            details = "; ".join(f"{path}: {error}" for path, error in failures)
            raise RuntimeError(f"所有目标 DEM 均计算失败：{details}")
        raise RuntimeError(
            "没有可计算的目标 DEM；如果文件夹中只有标定板文件，"
            "请把其他 DEM 放入该文件夹或修改 INPUT_FOLDER"
        )

    return {
        "calibration": calibration_result,
        "calibration_percentile": calibration_percentile,
        "calibration_elevation": calibration_elevation,
        "target_percentiles": target_percentiles,
        "results": results,
        "failures": failures,
    }


def save_relative_height_csv(calculation, output_csv):
    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    target_labels = [
        _percentile_label(value)
        for value in calculation["target_percentiles"]
    ]
    fieldnames = [
        "file_name",
        "file_path",
        "valid_pixel_count",
        "minimum",
        "median",
        "mean",
        "maximum",
        "calibration_file",
        "calibration_percentile",
        "calibration_elevation",
    ]
    for label in target_labels:
        fieldnames.extend([f"absolute_{label}", f"relative_height_{label}"])

    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(calculation["results"])
    return output_path


def _format(value):
    return f"{float(value):.{DECIMAL_PLACES}f}"


def main():
    input_folder_path = Path(INPUT_FOLDER).resolve()
    calibration_tif_path = input_folder_path / CALIBRATION_FILE_NAME
    output_csv_path = input_folder_path / OUTPUT_CSV_FILE_NAME

    calculation = calculate_relative_heights(
        calibration_tif=calibration_tif_path,
        input_folder=input_folder_path,
        calibration_percentile=CALIBRATION_PERCENTILE,
        target_percentiles=TARGET_PERCENTILES,
        recursive=SEARCH_RECURSIVELY,
        skip_calibration_file=SKIP_CALIBRATION_FILE,
    )

    calibration_label = _percentile_label(
        calculation["calibration_percentile"]
    ).upper()
    print(
        f"标定板: {calculation['calibration']['file_path']}\n"
        f"标定板 {calibration_label} 高程: "
        f"{_format(calculation['calibration_elevation'])}\n"
    )

    for row in calculation["results"]:
        parts = [row["file_name"]]
        for percentile in calculation["target_percentiles"]:
            label = _percentile_label(percentile)
            parts.append(
                f"{label.upper()}绝对高程={_format(row[f'absolute_{label}'])}"
            )
            parts.append(
                f"{label.upper()}相对高度="
                f"{_format(row[f'relative_height_{label}'])}"
            )
        print(" | ".join(parts))

    saved_path = save_relative_height_csv(calculation, output_csv_path)
    print(f"\nCSV 已保存: {saved_path}")
    print(
        f"成功 {len(calculation['results'])} 个，"
        f"失败 {len(calculation['failures'])} 个。"
    )
    for path, error in calculation["failures"]:
        print(f"[失败] {path}: {error}")


if __name__ == "__main__":
    main()
