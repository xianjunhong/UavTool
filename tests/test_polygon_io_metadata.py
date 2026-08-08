import os
import tempfile
import unittest


try:
    from osgeo import osr

    from logic.polygon_io import (
        load_polygons_from_vector,
        save_polygons_to_shapefile,
    )
except Exception:
    osr = None


@unittest.skipIf(osr is None, "GDAL/OGR is not installed")
class PolygonMetadataRoundTripTests(unittest.TestCase):
    def test_column_metadata_and_utf8_name_round_trip(self):
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        polygon = {
            "name": "第一列-01",
            "geo_points": [(120.0, 30.0), (120.1, 30.0), (120.1, 30.1), (120.0, 30.1)],
            "column_id": "column_1",
            "column_position": 0,
            "plot_index": 1,
            "column_start_end": "b",
            "column_prefix": "第一列-",
            "column_start": 1,
            "column_padding": 2,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            shp_path = os.path.join(temp_dir, "plots.shp")
            save_polygons_to_shapefile(shp_path, [polygon], srs.ExportToWkt())

            for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                self.assertTrue(os.path.exists(os.path.join(temp_dir, "plots" + extension)))

            loaded = load_polygons_from_vector(shp_path, srs.ExportToWkt())

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], polygon["name"])
        self.assertEqual(loaded[0]["column_id"], "column_1")
        self.assertEqual(loaded[0]["column_position"], 0)
        self.assertEqual(loaded[0]["plot_index"], 1)
        self.assertEqual(loaded[0]["column_start_end"], "b")
        self.assertEqual(loaded[0]["column_prefix"], "第一列-")
        self.assertEqual(loaded[0]["column_start"], 1)
        self.assertEqual(loaded[0]["column_padding"], 2)
