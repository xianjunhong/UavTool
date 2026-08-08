import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    from ui.pages import ExportKmzDialog
except ImportError as exc:
    raise unittest.SkipTest(f"当前 Python 环境未安装可用的 PySide6: {exc}") from exc


class ExportKmzDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dialog = ExportKmzDialog()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()

    def _select_drone(self, drone_type: str):
        index = self.dialog.drone_combo.findData(drone_type)
        self.assertGreaterEqual(index, 0)
        self.dialog.drone_combo.setCurrentIndex(index)

    def test_parameters_are_locked_until_model_is_selected(self):
        self.assertIsNone(self.dialog.drone_combo.currentData())
        self.assertFalse(self.dialog.parameter_group.isEnabled())
        self.assertFalse(self.dialog.ok_button.isEnabled())

        with self.assertRaisesRegex(ValueError, "请先选择无人机型号"):
            self.dialog.payload()

    def test_m3t_hides_gimbal_yaw_and_uses_m3t_ranges(self):
        self._select_drone("M3T")

        self.assertTrue(self.dialog.parameter_group.isEnabled())
        self.assertTrue(self.dialog.ok_button.isEnabled())
        self.assertTrue(self.dialog.yaw_row.isHidden())
        self.assertFalse(self.dialog.image_format_combo.isHidden())
        self.assertEqual(self.dialog.pitch_spin.minimum(), -90.0)
        self.assertEqual(self.dialog.pitch_spin.maximum(), 35.0)

        payload = self.dialog.payload()
        self.assertEqual(payload["config"].drone_type, "M3T")
        self.assertEqual(payload["config"].image_format, "wide,ir")
        self.assertEqual(payload["pitch"], -90.0)
        self.assertIsNone(payload["yaw"])

    def test_fixed_heading_enables_aircraft_heading_angle(self):
        self._select_drone("M3T")
        fixed_index = self.dialog.heading_mode_combo.findData("fixed")
        self.dialog.heading_mode_combo.setCurrentIndex(fixed_index)
        self.dialog.heading_angle_spin.setValue(-85.0)

        self.assertTrue(self.dialog.heading_angle_spin.isEnabled())
        payload = self.dialog.payload()
        self.assertEqual(payload["config"].waypoint_heading_mode, "fixed")
        self.assertEqual(payload["config"].waypoint_heading_angle, -85.0)
        self.assertIsNone(payload["yaw"])

    def test_m300_exposes_optional_gimbal_yaw(self):
        self._select_drone("M300")

        self.assertFalse(self.dialog.yaw_row.isHidden())
        self.assertTrue(self.dialog.image_format_combo.isHidden())
        self.assertEqual(self.dialog.pitch_spin.minimum(), -120.0)
        self.assertEqual(self.dialog.pitch_spin.maximum(), 45.0)

        self.dialog.yaw_check.setChecked(True)
        self.dialog.yaw_spin.setValue(30.0)
        payload = self.dialog.payload()
        self.assertEqual(payload["config"].drone_type, "M300")
        self.assertEqual(payload["yaw"], 30.0)

    def test_unchecked_optional_angles_are_not_exported(self):
        self._select_drone("M300")
        self.dialog.pitch_check.setChecked(False)
        self.dialog.yaw_check.setChecked(False)

        payload = self.dialog.payload()
        self.assertIsNone(payload["pitch"])
        self.assertIsNone(payload["yaw"])


if __name__ == "__main__":
    unittest.main()
