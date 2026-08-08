import csv
import os
import tempfile
import unittest

import numpy as np


try:
    from osgeo import gdal

    from logic.height_extraction import extract_relative_heights

    gdal.UseExceptions()
except Exception:
    gdal = None


@unittest.skipIf(gdal is None, "GDAL/PySide6 runtime is not installed")
class HeightExtractionTests(unittest.TestCase):
    @staticmethod
    def _create_dem(path, values):
        values = np.asarray(values, dtype=np.float32)
        height, width = values.shape
        dataset = gdal.GetDriverByName("GTiff").Create(
            path,
            width,
            height,
            2,
            gdal.GDT_Float32,
        )
        dataset.GetRasterBand(1).WriteArray(values)
        dataset.GetRasterBand(1).SetNoDataValue(0)
        alpha = dataset.GetRasterBand(2)
        alpha.WriteArray(np.full(values.shape, 255, dtype=np.float32))
        alpha.SetColorInterpretation(gdal.GCI_AlphaBand)
        alpha.SetNoDataValue(0)
        dataset.FlushCache()
        dataset = None

    def test_folder_files_are_measured_relative_to_reference_and_saved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reference_path = os.path.join(temp_dir, "ground.tif")
            target_path = os.path.join(temp_dir, "object.tif")
            output_path = os.path.join(temp_dir, "height_results.csv")
            self._create_dem(reference_path, np.full((10, 10), 10.0))
            target_values = np.linspace(10.0, 20.0, 100).reshape(10, 10)
            self._create_dem(target_path, target_values)

            result = extract_relative_heights(
                temp_dir,
                reference_path,
                output_path,
                reference_percentile=50,
                target_percentiles=(95, 99),
            )

            self.assertEqual(result["success_count"], 1)
            self.assertEqual(result["failure_count"], 0)
            self.assertAlmostEqual(result["reference_elevation"], 10.0)
            row = result["rows"][0]
            self.assertEqual(row["file_name"], "object.tif")
            self.assertAlmostEqual(
                row["relative_height_p95"],
                float(np.percentile(target_values, 95)) - 10.0,
                places=5,
            )
            self.assertAlmostEqual(
                row["relative_height_p99"],
                float(np.percentile(target_values, 99)) - 10.0,
                places=5,
            )

            with open(output_path, "r", encoding="utf-8-sig", newline="") as csv_file:
                saved_rows = list(csv.DictReader(csv_file))
            self.assertEqual(len(saved_rows), 1)
            self.assertIn("absolute_p95", saved_rows[0])
            self.assertIn("relative_height_p99", saved_rows[0])

    def test_reference_is_skipped_when_it_is_inside_target_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reference_path = os.path.join(temp_dir, "ground.tif")
            self._create_dem(reference_path, np.full((4, 4), 12.0))

            with self.assertRaisesRegex(RuntimeError, "除基准文件外"):
                extract_relative_heights(
                    temp_dir,
                    reference_path,
                    os.path.join(temp_dir, "height_results.csv"),
                )


if __name__ == "__main__":
    unittest.main()
