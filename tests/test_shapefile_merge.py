import os
import tempfile
import unittest


try:
    from osgeo import ogr, osr

    from logic.polygon_io import load_polygons_from_vector, save_polygons_to_shapefile
    from logic.shapefile_merge import merge_shapefiles
except Exception:
    ogr = None
    osr = None


@unittest.skipIf(ogr is None or osr is None, "GDAL/OGR is not installed")
class ShapefileMergeTests(unittest.TestCase):
    def _srs_wkt(self, epsg):
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        if hasattr(srs, "SetAxisMappingStrategy"):
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return srs.ExportToWkt()

    def test_merge_preserves_plot_numbers_and_metadata(self):
        first = {
            "name": "A01",
            "geo_points": [
                (120.0, 30.0),
                (120.01, 30.0),
                (120.01, 30.01),
                (120.0, 30.01),
            ],
            "column_id": "column_1",
            "column_position": 0,
            "plot_index": 1,
            "column_start_end": "a",
            "column_prefix": "A",
            "column_start": 1,
            "column_padding": 2,
        }
        first_next = {
            **first,
            "name": "A02",
            "geo_points": [
                (120.0, 30.01),
                (120.01, 30.01),
                (120.01, 30.02),
                (120.0, 30.02),
            ],
            "column_position": 1,
            "plot_index": 2,
        }
        second = {
            "name": "B07",
            "geo_points": [
                (120.02, 30.0),
                (120.03, 30.0),
                (120.03, 30.01),
                (120.02, 30.01),
            ],
            "column_id": "column_1",
            "column_position": 0,
            "plot_index": 7,
            "column_start_end": "b",
            "column_prefix": "B",
            "column_start": 7,
            "column_padding": 2,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = os.path.join(temp_dir, "first.shp")
            second_path = os.path.join(temp_dir, "second.shp")
            output_path = os.path.join(temp_dir, "merged.shp")
            wkt = self._srs_wkt(4326)

            save_polygons_to_shapefile(first_path, [first, first_next], wkt)
            save_polygons_to_shapefile(second_path, [second], wkt)
            result = merge_shapefiles([first_path, second_path], output_path)
            loaded = load_polygons_from_vector(output_path, wkt)

            self.assertEqual(result["input_count"], 2)
            self.assertEqual(result["plot_count"], 3)
            self.assertEqual(result["column_count"], 2)
            self.assertTrue(os.path.exists(output_path))

        by_name = {item["name"]: item for item in loaded}
        self.assertEqual(set(by_name), {"A01", "A02", "B07"})
        self.assertEqual(by_name["A01"]["plot_index"], 1)
        self.assertEqual(by_name["A01"]["column_id"], "column_1")
        self.assertEqual(by_name["A02"]["plot_index"], 2)
        self.assertEqual(by_name["A02"]["column_id"], "column_1")
        self.assertEqual(by_name["B07"]["plot_index"], 7)
        self.assertEqual(by_name["B07"]["column_id"], "column_2")
        self.assertEqual(by_name["B07"]["column_prefix"], "B")

    def test_rejects_output_that_is_also_an_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = os.path.join(temp_dir, "first.shp")
            second_path = os.path.join(temp_dir, "second.shp")
            wkt = self._srs_wkt(4326)
            polygon = {
                "name": "01",
                "geo_points": [(0, 0), (1, 0), (1, 1), (0, 1)],
            }
            save_polygons_to_shapefile(first_path, [polygon], wkt)
            save_polygons_to_shapefile(second_path, [polygon], wkt)

            with self.assertRaisesRegex(ValueError, "输出文件不能"):
                merge_shapefiles([first_path, second_path], first_path)

    def test_merge_transforms_other_inputs_to_first_crs(self):
        wgs84 = osr.SpatialReference()
        wgs84.ImportFromEPSG(4326)
        web_mercator = osr.SpatialReference()
        web_mercator.ImportFromEPSG(3857)
        if hasattr(wgs84, "SetAxisMappingStrategy"):
            wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            web_mercator.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        to_mercator = osr.CoordinateTransformation(wgs84, web_mercator)
        mercator_points = [
            to_mercator.TransformPoint(lon, lat)[:2]
            for lon, lat in [
                (120.02, 30.0),
                (120.03, 30.0),
                (120.03, 30.01),
                (120.02, 30.01),
            ]
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            first_path = os.path.join(temp_dir, "wgs84.shp")
            second_path = os.path.join(temp_dir, "mercator.shp")
            output_path = os.path.join(temp_dir, "merged.shp")
            save_polygons_to_shapefile(
                first_path,
                [
                    {
                        "name": "A01",
                        "geo_points": [
                            (120.0, 30.0),
                            (120.01, 30.0),
                            (120.01, 30.01),
                            (120.0, 30.01),
                        ],
                    }
                ],
                wgs84.ExportToWkt(),
            )
            save_polygons_to_shapefile(
                second_path,
                [{"name": "B02", "geo_points": mercator_points}],
                web_mercator.ExportToWkt(),
            )

            merge_shapefiles([first_path, second_path], output_path)
            loaded = load_polygons_from_vector(output_path, wgs84.ExportToWkt())

        by_name = {item["name"]: item for item in loaded}
        transformed_x, transformed_y = by_name["B02"]["geo_points"][0]
        self.assertAlmostEqual(transformed_x, 120.02, places=5)
        self.assertAlmostEqual(transformed_y, 30.0, places=5)
