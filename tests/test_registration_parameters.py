import os
import tempfile
import unittest

import numpy as np


try:
    from osgeo import gdal, osr

    from logic.registration import (
        align_target_to_src_new_tif,
        apply_registration_parameters_to_tif,
        load_registration_parameters,
        save_registration_parameters,
    )

    gdal.UseExceptions()
except Exception:
    gdal = None


@unittest.skipIf(gdal is None, "GDAL/PySide6 runtime is not installed")
class RegistrationParameterTests(unittest.TestCase):
    @staticmethod
    def _projection(epsg):
        spatial_reference = osr.SpatialReference()
        spatial_reference.ImportFromEPSG(epsg)
        return spatial_reference.ExportToWkt()

    @staticmethod
    def _create_tif(path, width, height, geotransform, projection, value=1):
        dataset = gdal.GetDriverByName("GTiff").Create(
            path,
            width,
            height,
            1,
            gdal.GDT_Float32,
        )
        dataset.SetGeoTransform(geotransform)
        dataset.SetProjection(projection)
        dataset.GetRasterBand(1).WriteArray(
            np.full((height, width), value, dtype=np.float32)
        )
        dataset.FlushCache()
        dataset = None

    def test_csv_round_trip_preserves_matrix_projection_and_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = os.path.join(temp_dir, "alignment.csv")
            correction = np.array(
                [[1.001, 0.002, 5.5], [-0.003, 0.999, -7.25], [0.0, 0.0, 1.0]]
            )
            projection = self._projection(32650)

            saved_path = save_registration_parameters(
                csv_path,
                correction,
                src_projection_wkt=projection,
                target_projection_wkt=projection,
                pixel_affine_params=(1, 0, 2, 0, 1, 3),
                src_geotransform=(1000, 2, 0, 2000, 0, -2),
                target_geotransform=(995, 2, 0, 2006, 0, -2),
                metadata={"point_count": 5, "rmse": 0.25},
            )
            loaded = load_registration_parameters(saved_path)

            np.testing.assert_allclose(loaded["world_correction"], correction)
            self.assertEqual(loaded["src_projection_wkt"], projection)
            self.assertEqual(loaded["target_projection_wkt"], projection)
            self.assertEqual(loaded["point_count"], 5)
            self.assertAlmostEqual(loaded["rmse"], 0.25)

    def test_parameters_apply_to_dem_with_different_resolution(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            projection = self._projection(4326)
            src_path = os.path.join(temp_dir, "src_ortho.tif")
            target_path = os.path.join(temp_dir, "target_ortho.tif")
            aligned_path = os.path.join(temp_dir, "target_ortho_aligned.tif")
            csv_path = os.path.join(temp_dir, "target_ortho_params.csv")
            dem_path = os.path.join(temp_dir, "target_dem.tif")
            aligned_dem_path = os.path.join(temp_dir, "target_dem_aligned.tif")

            self._create_tif(
                src_path,
                64,
                64,
                (117.0, 2.5e-8, 0.0, 34.0, 0.0, -2.5e-8),
                projection,
                value=10,
            )
            self._create_tif(
                target_path,
                64,
                64,
                (117.00001, 2.5e-8, 0.0, 34.00001, 0.0, -2.5e-8),
                projection,
                value=20,
            )

            points = [(5.0, 5.0), (55.0, 8.0), (12.0, 52.0)]
            result = align_target_to_src_new_tif(
                src_path,
                target_path,
                points,
                points,
                aligned_path,
                parameter_csv_path=csv_path,
            )
            self.assertEqual(result["parameter_path"], csv_path)

            self._create_tif(
                dem_path,
                128,
                128,
                (117.00002, 1.0e-7, 0.0, 34.00002, 0.0, -1.0e-7),
                projection,
                value=123.5,
            )
            apply_result = apply_registration_parameters_to_tif(
                csv_path,
                dem_path,
                aligned_dem_path,
            )

            aligned_dem = gdal.Open(aligned_dem_path, gdal.GA_ReadOnly)
            np.testing.assert_allclose(
                aligned_dem.GetGeoTransform(),
                (117.00001, 1.0e-7, 0.0, 34.00001, 0.0, -1.0e-7),
                rtol=0.0,
                atol=1e-12,
            )
            self.assertTrue(
                np.allclose(aligned_dem.GetRasterBand(1).ReadAsArray(), 123.5)
            )
            aligned_dem = None
            self.assertEqual(apply_result["mode"], "parameters")

    def test_parameters_reject_target_with_different_projection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = os.path.join(temp_dir, "alignment.csv")
            projected_wkt = self._projection(32650)
            save_registration_parameters(
                csv_path,
                np.eye(3),
                src_projection_wkt=projected_wkt,
                target_projection_wkt=projected_wkt,
                pixel_affine_params=(1, 0, 0, 0, 1, 0),
                src_geotransform=(0, 1, 0, 0, 0, -1),
                target_geotransform=(0, 1, 0, 0, 0, -1),
            )

            target_path = os.path.join(temp_dir, "wrong_crs.tif")
            self._create_tif(
                target_path,
                32,
                32,
                (0, 1, 0, 0, 0, -1),
                self._projection(4326),
            )

            with self.assertRaisesRegex(RuntimeError, "坐标系.*不一致"):
                apply_registration_parameters_to_tif(
                    csv_path,
                    target_path,
                    os.path.join(temp_dir, "should_not_exist.tif"),
                )


if __name__ == "__main__":
    unittest.main()
