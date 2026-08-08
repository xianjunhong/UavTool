import os
import tempfile
import unittest

import numpy as np


try:
    from osgeo import gdal, osr

    from logic.crop import crop_tif_with_polygon
    gdal.UseExceptions()
except Exception:
    gdal = None


@unittest.skipIf(gdal is None, "GDAL is not installed")
class PixelSpacePngCropTests(unittest.TestCase):
    def _create_rotated_rgb_tif(self, path):
        width, height = 100, 80
        ds = gdal.GetDriverByName("GTiff").Create(
            path,
            width,
            height,
            3,
            gdal.GDT_Byte,
        )
        ds.SetGeoTransform((500000.0, 1.0, 0.35, 4000000.0, 0.2, -1.0))
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(32650)
        ds.SetProjection(srs.ExportToWkt())

        yy, xx = np.indices((height, width))
        arrays = [
            xx.astype(np.uint8),
            yy.astype(np.uint8),
            ((xx + yy) % 256).astype(np.uint8),
        ]
        for index, array in enumerate(arrays, start=1):
            ds.GetRasterBand(index).WriteArray(array)
        ds.FlushCache()
        ds = None
        return arrays

    def test_png_keeps_source_pixel_orientation_despite_rotated_geotransform(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tif_path = os.path.join(temp_dir, "rotated.tif")
            png_path = os.path.join(temp_dir, "plot.png")
            source_arrays = self._create_rotated_rgb_tif(tif_path)

            result = crop_tif_with_polygon(
                tif_path,
                [(20, 10), (70, 10), (70, 60), (20, 60)],
                png_path,
                output_format="png",
            )

            out_ds = gdal.Open(png_path, gdal.GA_ReadOnly)
            self.assertIsNotNone(out_ds)
            self.assertEqual((out_ds.RasterXSize, out_ds.RasterYSize), (50, 50))
            self.assertEqual(out_ds.RasterCount, 4)
            for band_index, source in enumerate(source_arrays, start=1):
                actual = out_ds.GetRasterBand(band_index).ReadAsArray()
                np.testing.assert_array_equal(actual, source[10:60, 20:70])
            alpha = out_ds.GetRasterBand(4).ReadAsArray()
            self.assertTrue(np.all(alpha == 255))
            out_ds = None

            self.assertEqual(result["output_path"], png_path)
            self.assertEqual(result["overview_count"], 0)

    def test_slanted_polygon_uses_alpha_mask_without_rotating_pixels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tif_path = os.path.join(temp_dir, "rotated.tif")
            png_path = os.path.join(temp_dir, "slanted.png")
            self._create_rotated_rgb_tif(tif_path)

            crop_tif_with_polygon(
                tif_path,
                [(20, 10), (70, 15), (65, 60), (25, 55)],
                png_path,
                output_format="png",
            )

            out_ds = gdal.Open(png_path, gdal.GA_ReadOnly)
            alpha = out_ds.GetRasterBand(4).ReadAsArray()
            self.assertGreater(np.count_nonzero(alpha == 0), 0)
            self.assertGreater(np.count_nonzero(alpha == 255), 0)
            out_ds = None

    def test_png_applies_view_rotation_to_match_drawn_orientation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tif_path = os.path.join(temp_dir, "rotated.tif")
            png_path = os.path.join(temp_dir, "display_aligned.png")
            self._create_rotated_rgb_tif(tif_path)

            display_rotation = -10.0
            radians = np.deg2rad(-display_rotation)
            cos_angle = float(np.cos(radians))
            sin_angle = float(np.sin(radians))
            polygon = []
            for local_x, local_y in [
                (-20, -10),
                (20, -10),
                (20, 10),
                (-20, 10),
            ]:
                polygon.append(
                    (
                        50 + cos_angle * local_x - sin_angle * local_y,
                        40 + sin_angle * local_x + cos_angle * local_y,
                    )
                )

            crop_tif_with_polygon(
                tif_path,
                polygon,
                png_path,
                output_format="png",
                display_rotation_deg=display_rotation,
            )

            out_ds = gdal.Open(png_path, gdal.GA_ReadOnly)
            self.assertIsNotNone(out_ds)
            self.assertLessEqual(abs(out_ds.RasterXSize - 40), 1)
            self.assertLessEqual(abs(out_ds.RasterYSize - 20), 1)
            alpha = out_ds.GetRasterBand(4).ReadAsArray()
            self.assertLess(np.count_nonzero(alpha == 0), alpha.size * 0.08)
            out_ds = None


if __name__ == "__main__":
    unittest.main()
