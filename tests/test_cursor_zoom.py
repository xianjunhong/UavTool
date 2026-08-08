import math
import os
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    from ui.crop_viewer import CropViewer
    from ui.registration_viewer import RegistrationViewer
    from ui.viewer import UavViewer
except Exception:
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class CursorCenteredZoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _assert_zoom_keeps_cursor_scene_position(self, viewer):
        viewer.ds = object()
        viewer.scene_obj.setSceneRect(0, 0, 1200, 900)
        viewer.resize(700, 520)
        viewer.fitInView(viewer.scene_obj.sceneRect(), Qt.KeepAspectRatio)
        viewer.rotate(17)
        viewer.centerOn(600, 450)
        viewer.show()
        self.app.processEvents()

        cursor = QPointF(185, 320)
        before = viewer.mapToScene(cursor.toPoint())
        event = QWheelEvent(
            cursor,
            cursor,
            QPoint(),
            QPoint(0, 120),
            Qt.NoButton,
            Qt.NoModifier,
            Qt.ScrollUpdate,
            False,
        )

        viewer.wheelEvent(event)
        after = viewer.mapToScene(cursor.toPoint())
        drift = math.hypot(after.x() - before.x(), after.y() - before.y())
        self.assertLess(drift, 1.5)

    def test_route_viewer_zooms_at_cursor(self):
        self._assert_zoom_keeps_cursor_scene_position(UavViewer())

    def test_registration_viewer_zooms_at_cursor(self):
        from PySide6.QtGui import QColor

        self._assert_zoom_keeps_cursor_scene_position(
            RegistrationViewer(QColor(255, 0, 0))
        )

    def test_crop_viewer_zooms_at_cursor(self):
        self._assert_zoom_keeps_cursor_scene_position(CropViewer())


if __name__ == "__main__":
    unittest.main()
