import csv
import os
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QObject, Signal

from dem_height_percentiles import analyze_dem, find_dem_files


def _percentile_label(value: float) -> str:
    number = float(value)
    if number.is_integer():
        return f"p{int(number)}"
    return f"p{str(number).replace('.', '_')}"


def _validate_percentile(value: float, label: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 100.0:
        raise ValueError(f"{label}必须在 0～100 之间")
    return number


def _write_csv(rows, output_csv_path: str, target_percentiles: Sequence[float]):
    output_path = Path(output_csv_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "file_name",
        "file_path",
        "valid_pixel_count",
        "minimum",
        "median",
        "mean",
        "maximum",
        "reference_file",
        "reference_percentile",
        "reference_elevation",
    ]
    for percentile in target_percentiles:
        label = _percentile_label(percentile)
        fieldnames.extend([f"absolute_{label}", f"relative_height_{label}"])

    temp_path = Path(str(output_path) + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_path, output_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return str(output_path)


def extract_relative_heights(
    target_folder: str,
    reference_tif_path: str,
    output_csv_path: str,
    reference_percentile: float = 50.0,
    target_percentiles: Sequence[float] = (95.0, 99.0),
    recursive: bool = False,
    progress_callback=None,
):
    def report(percent: int, message: str):
        if callable(progress_callback):
            progress_callback(percent, message)

    folder = Path(target_folder).resolve()
    reference_path = Path(reference_tif_path).resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"目标文件夹不存在: {folder}")
    if not reference_path.is_file():
        raise FileNotFoundError(f"基准 TIF 不存在: {reference_path}")
    if reference_path.suffix.lower() not in {".tif", ".tiff"}:
        raise ValueError("基准文件必须是 TIF/TIFF")

    reference_percentile = _validate_percentile(
        reference_percentile,
        "基准百分位",
    )
    selected_percentiles = []
    for value in target_percentiles:
        percentile = _validate_percentile(value, "目标百分位")
        if percentile not in selected_percentiles:
            selected_percentiles.append(percentile)
    if not selected_percentiles:
        raise ValueError("至少需要一个目标百分位")

    report(3, "正在读取基准文件")
    reference_label = _percentile_label(reference_percentile)
    reference_result = analyze_dem(
        reference_path,
        percentiles=[reference_percentile],
    )
    reference_elevation = reference_result[reference_label]

    all_tifs = find_dem_files(folder, recursive)
    reference_key = os.path.normcase(os.fspath(reference_path))
    target_tifs = [
        path
        for path in all_tifs
        if os.path.normcase(os.fspath(path.resolve())) != reference_key
    ]
    if not target_tifs:
        raise RuntimeError("目标文件夹中除基准文件外，没有其他 TIF/TIFF")

    rows = []
    failures = []
    total = len(target_tifs)
    for index, tif_path in enumerate(target_tifs, start=1):
        start_percent = 10 + int((index - 1) * 80 / total)
        report(start_percent, f"正在处理 {index}/{total}: {tif_path.name}")
        try:
            target_result = analyze_dem(
                tif_path,
                percentiles=selected_percentiles,
            )
            row = {
                "file_name": target_result["file_name"],
                "file_path": target_result["file_path"],
                "valid_pixel_count": target_result["valid_pixel_count"],
                "minimum": target_result["minimum"],
                "median": target_result["median"],
                "mean": target_result["mean"],
                "maximum": target_result["maximum"],
                "reference_file": reference_path.name,
                "reference_percentile": reference_percentile,
                "reference_elevation": reference_elevation,
            }
            for percentile in selected_percentiles:
                label = _percentile_label(percentile)
                absolute_elevation = target_result[label]
                row[f"absolute_{label}"] = absolute_elevation
                row[f"relative_height_{label}"] = (
                    absolute_elevation - reference_elevation
                )
            rows.append(row)
        except Exception as exc:
            failures.append({"file_path": str(tif_path), "error": str(exc)})

    if not rows:
        details = "; ".join(
            f"{Path(item['file_path']).name}: {item['error']}"
            for item in failures
        )
        raise RuntimeError(f"所有目标 TIF 均处理失败: {details}")

    report(92, "正在保存 CSV")
    saved_path = _write_csv(rows, output_csv_path, selected_percentiles)
    report(100, "高度提取完成")
    return {
        "output_path": saved_path,
        "reference_path": str(reference_path),
        "reference_percentile": reference_percentile,
        "reference_elevation": reference_elevation,
        "target_percentiles": selected_percentiles,
        "success_count": len(rows),
        "failure_count": len(failures),
        "rows": rows,
        "failures": failures,
    }


class HeightExtractionWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        target_folder: str,
        reference_tif_path: str,
        output_csv_path: str,
        reference_percentile: float,
        target_percentiles: Sequence[float],
        recursive: bool = False,
    ):
        super().__init__()
        self.target_folder = target_folder
        self.reference_tif_path = reference_tif_path
        self.output_csv_path = output_csv_path
        self.reference_percentile = reference_percentile
        self.target_percentiles = list(target_percentiles)
        self.recursive = recursive

    def run(self):
        try:
            result = extract_relative_heights(
                self.target_folder,
                self.reference_tif_path,
                self.output_csv_path,
                reference_percentile=self.reference_percentile,
                target_percentiles=self.target_percentiles,
                recursive=self.recursive,
                progress_callback=lambda p, m: self.progress.emit(p, m),
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
