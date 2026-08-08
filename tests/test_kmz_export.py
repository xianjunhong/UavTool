import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from logic.kmz_export import MissionConfig, export_waypoints_to_kmz


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _texts(root: ET.Element, name: str):
    return [
        (element.text or "").strip()
        for element in root.iter()
        if _local_name(element.tag) == name
    ]


def _read_members(path: Path):
    with zipfile.ZipFile(path) as kmz:
        return {
            name: ET.fromstring(kmz.read(name))
            for name in ("wpmz/template.kml", "wpmz/waylines.wpml")
        }


class KmzExportTests(unittest.TestCase):
    def test_m3t_export_uses_aircraft_heading_without_gimbal_yaw(self):
        config = MissionConfig(
            drone_type="M3T",
            waypoint_heading_mode="fixed",
            waypoint_heading_angle=-85.0,
            image_format="wide,ir",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "m3t.kmz"
            export_waypoints_to_kmz(
                [(120.0, 30.0), (120.001, 30.001)],
                str(output_path),
                config,
                pitch=-90.0,
                yaw=None,
            )
            members = _read_members(output_path)

        for root in members.values():
            self.assertEqual(_texts(root, "droneEnumValue"), ["77"])
            self.assertNotIn("1", _texts(root, "gimbalYawRotateEnable"))
            self.assertTrue(
                all(value == "-85.0" for value in _texts(root, "waypointHeadingAngle"))
            )

        template = members["wpmz/template.kml"]
        waylines = members["wpmz/waylines.wpml"]
        self.assertEqual(_texts(template, "imageFormat"), ["wide,ir"])
        for unsupported in (
            "returnMode",
            "samplingRate",
            "scanningMode",
            "modelColoringEnable",
        ):
            self.assertEqual(_texts(template, unsupported), [])
        self.assertEqual(_texts(waylines, "waypointHeadingAngleEnable"), ["1", "1"])
        self.assertEqual(_texts(waylines, "waypointGimbalHeadingParam"), [])
        self.assertEqual(_texts(waylines, "waypointWorkType"), [])
        self.assertEqual(_texts(waylines, "isRisky"), [])
        self.assertEqual(_texts(waylines, "gimbalHeadingYawBase"), ["north", "north"])
        self.assertEqual(_texts(waylines, "useGlobalPayloadLensIndex"), ["1", "1"])

    def test_m3t_rejects_independent_gimbal_yaw(self):
        config = MissionConfig(drone_type="M3T")

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "云台偏航不可独立控制"):
                export_waypoints_to_kmz(
                    [(120.0, 30.0)],
                    str(Path(temp_dir) / "invalid.kmz"),
                    config,
                    pitch=None,
                    yaw=0.0,
                )

    def test_model_specific_ranges_are_validated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = str(Path(temp_dir) / "invalid.kmz")
            with self.assertRaisesRegex(ValueError, "自动飞行速度"):
                export_waypoints_to_kmz(
                    [(120.0, 30.0)],
                    output_path,
                    MissionConfig(drone_type="M3T", auto_flight_speed=16.0),
                )
            with self.assertRaisesRegex(ValueError, "M3T 云台俯仰角"):
                export_waypoints_to_kmz(
                    [(120.0, 30.0)],
                    output_path,
                    MissionConfig(drone_type="M3T"),
                    pitch=-100.0,
                )

    def test_m300_can_emit_optional_gimbal_yaw(self):
        config = MissionConfig(drone_type="M300")

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "m300.kmz"
            export_waypoints_to_kmz(
                [(120.0, 30.0)],
                str(output_path),
                config,
                pitch=None,
                yaw=30.0,
            )
            members = _read_members(output_path)

        self.assertIn(
            "1",
            _texts(members["wpmz/waylines.wpml"], "gimbalYawRotateEnable"),
        )


if __name__ == "__main__":
    unittest.main()
