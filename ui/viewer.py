from typing import List

import numpy as np
from PySide6.QtCore import QPoint, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
)

from logic.waypoint_logic import Waypoint, nearest_waypoint_index
from utils.env_setup import configure_runtime_env

configure_runtime_env()

from osgeo import gdal

from utils.geo import build_transformer, pixel_to_lon_lat


gdal.UseExceptions()


class WaypointMarker(QGraphicsItem):
    def __init__(self, x: float, y: float, index: int):
        super().__init__()
        self.px_x = x
        self.px_y = y
        self.index = index
        self.base_radius = 14
        self.radius = 14
        self.font = QFont("Arial", 9, QFont.Bold)
        self.setPos(x, y)
        self.setZValue(30)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.update_index(index)

    def boundingRect(self):
        pad = 3
        r = self.radius
        return QRectF(-r - pad, -r - pad, (r + pad) * 2, (r + pad) * 2)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(255, 0, 0))
        painter.setPen(QPen(Qt.black, 2))
        r = self.radius
        painter.drawEllipse(-r, -r, r * 2, r * 2)

        painter.setPen(QPen(Qt.black, 1))
        painter.setFont(self.font)
        painter.drawText(self.boundingRect(), Qt.AlignCenter, str(self.index))

    def update_index(self, index: int):
        self.index = index
        self.update()

    def update_visual_scale(self, view_scale: float):
        # Use a soft curve so markers shrink at top level but stay readable.
        scale = max(0.05, float(view_scale))
        new_radius = int(round(self.base_radius * (scale ** 0.35)))
        new_radius = max(6, min(14, new_radius))

        if new_radius != self.radius:
            self.prepareGeometryChange()
            self.radius = new_radius

        font_size = max(7, min(10, int(round(new_radius * 0.65))))
        if self.font.pointSize() != font_size:
            self.font.setPointSize(font_size)

        self.update()


class UavViewer(QGraphicsView):
    def __init__(self):
        super().__init__()

        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.NoDrag)

        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)

        self.ds = None
        self.geo_transform = None
        self.transformer = None
        self.full_w = 0
        self.full_h = 0

        self.base_item = None
        self.high_res_item = QGraphicsPixmapItem()
        self.scene_obj.addItem(self.high_res_item)

        self.waypoints: List[Waypoint] = []

        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.timeout.connect(self.update_resolution)

        self._press_button = Qt.NoButton
        self._press_pos = QPoint()
        self._dragging = False
        self._last_pan_pos = QPoint()

        self.on_waypoint_added = None
        self.on_waypoint_removed = None
        self.on_waypoints_reindexed = None

        self.display_rotation_deg = 0.0

    def _current_view_scale(self) -> float:
        t = self.transform()
        return (t.m11() ** 2 + t.m21() ** 2) ** 0.5

    def _apply_base_view_transform(self):
        self.resetTransform()
        self.fitInView(self.scene_obj.sceneRect(), Qt.KeepAspectRatio)
        if abs(self.display_rotation_deg) > 1e-9:
            self.rotate(self.display_rotation_deg)
        self.centerOn(self.full_w / 2, self.full_h / 2)

    def set_display_rotation(self, angle_deg: float):
        new_angle = float(angle_deg)
        delta = new_angle - self.display_rotation_deg
        self.display_rotation_deg = new_angle
        if self.ds is None:
            return

        # Preserve current zoom level and center when changing display rotation.
        center_scene = self.mapToScene(self.viewport().rect().center())
        if abs(delta) > 1e-9:
            self.rotate(delta)
        self.centerOn(center_scene)
        self.refresh_marker_sizes()
        self.update_resolution()

    def refresh_marker_sizes(self):
        view_scale = self._current_view_scale()
        for wp in self.waypoints:
            wp.marker.update_visual_scale(view_scale)

    def reset_view(self):
        self.resetTransform()
        self.scene_obj.clear()
        self.base_item = None
        self.high_res_item = QGraphicsPixmapItem()
        self.scene_obj.addItem(self.high_res_item)
        self.waypoints = []

    def load_tif(self, tif_path: str):
        ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError("无法打开该 TIF 文件")
        if ds.RasterCount < 3:
            raise RuntimeError("当前仅支持至少 3 波段的 RGB 影像")

        ov_count = ds.GetRasterBand(1).GetOverviewCount()
        if ov_count <= 0:
            raise RuntimeError("该影像不是金字塔格式，请先构建 overviews 后再导入")

        self.reset_view()
        self.ds = ds
        self.full_w = ds.RasterXSize
        self.full_h = ds.RasterYSize
        self.geo_transform = ds.GetGeoTransform()
        self.transformer = build_transformer(ds.GetProjection())

        self.base_item = self.create_base_layer()
        self.scene_obj.addItem(self.base_item)
        self.scene_obj.addItem(self.high_res_item)
        self.scene_obj.setSceneRect(0, 0, self.full_w, self.full_h)

        # Initial view uses top-level zoom (maximum visible extent) and stays centered.
        self._apply_base_view_transform()
        self.refresh_marker_sizes()
        self.update_resolution()

    def create_base_layer(self):
        band = self.ds.GetRasterBand(1)
        ov_idx = band.GetOverviewCount() - 1
        bands = [self.ds.GetRasterBand(i).GetOverview(ov_idx) for i in [1, 2, 3]]
        data = np.dstack([b.ReadAsArray() for b in bands]).astype(np.uint8, copy=False)

        h, w, _ = data.shape
        qimg = QImage(data.data, w, h, w * 3, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg.copy())
        item = QGraphicsPixmapItem(pix)
        item.setScale(self.full_w / w)
        item.setZValue(0)
        return item

    def wheelEvent(self, event):
        if self.ds is None:
            return
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)
        self.refresh_marker_sizes()
        self.update_timer.start(180)

    def mousePressEvent(self, event):
        if self.ds is None:
            return

        self._press_button = event.button()
        self._press_pos = event.pos()
        self._last_pan_pos = event.pos()
        self._dragging = False

        if event.button() in (Qt.LeftButton, Qt.RightButton):
            self.setCursor(Qt.ClosedHandCursor)

        event.accept()

    def mouseMoveEvent(self, event):
        if self.ds is None:
            return

        if self._press_button in (Qt.LeftButton, Qt.RightButton):
            delta = event.pos() - self._press_pos
            if not self._dragging and delta.manhattanLength() >= 6:
                self._dragging = True

            if self._dragging:
                move = event.pos() - self._last_pan_pos
                self._last_pan_pos = event.pos()
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - move.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - move.y())
                self.update_timer.start(120)
                event.accept()
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.ds is None:
            return

        release_scene = self.mapToScene(event.pos())
        was_dragging = self._dragging
        button = self._press_button

        self._press_button = Qt.NoButton
        self._dragging = False
        self.unsetCursor()

        if was_dragging:
            self.update_timer.start(60)
            event.accept()
            return

        if button == Qt.LeftButton:
            self.try_add_waypoint(release_scene.x(), release_scene.y())
            event.accept()
            return

        if button == Qt.RightButton:
            self.try_remove_waypoint(release_scene.x(), release_scene.y())
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def update_resolution(self):
        if self.ds is None:
            return

        viewport_rect = self.viewport().rect()
        view_scale = max(1e-6, self._current_view_scale())
        p1 = self.mapToScene(viewport_rect.topLeft())
        p2 = self.mapToScene(viewport_rect.topRight())
        p3 = self.mapToScene(viewport_rect.bottomLeft())
        p4 = self.mapToScene(viewport_rect.bottomRight())

        min_x = min(p1.x(), p2.x(), p3.x(), p4.x())
        max_x = max(p1.x(), p2.x(), p3.x(), p4.x())
        min_y = min(p1.y(), p2.y(), p3.y(), p4.y())
        max_y = max(p1.y(), p2.y(), p3.y(), p4.y())

        pad = 1.15
        cx = (min_x + max_x) * 0.5
        cy = (min_y + max_y) * 0.5
        read_w = max(1, int((max_x - min_x) * pad))
        read_h = max(1, int((max_y - min_y) * pad))

        x = int(round(cx - read_w / 2))
        y = int(round(cy - read_h / 2))
        x = max(0, min(self.full_w - 1, x))
        y = max(0, min(self.full_h - 1, y))
        w = min(read_w, self.full_w - x)
        h = min(read_h, self.full_h - y)
        if w <= 0 or h <= 0:
            return

        target_w = max(1, int(w * view_scale))
        target_h = max(1, int(h * view_scale))

        try:
            rgb = np.dstack(
                [
                    self.ds.GetRasterBand(i).ReadAsArray(
                        x,
                        y,
                        w,
                        h,
                        buf_xsize=target_w,
                        buf_ysize=target_h,
                    )
                    for i in [1, 2, 3]
                ]
            ).astype(np.uint8, copy=False)

            qimg = QImage(rgb.data, target_w, target_h, target_w * 3, QImage.Format_RGB888)
            self.high_res_item.setPixmap(QPixmap.fromImage(qimg.copy()))
            self.high_res_item.setPos(x, y)
            self.high_res_item.setScale(w / target_w)
            self.high_res_item.setZValue(5)
        except Exception as exc:
            print(f"动态加载失败: {exc}")

    def try_add_waypoint(self, px_x: float, px_y: float):
        if not (0 <= px_x < self.full_w and 0 <= px_y < self.full_h):
            return

        lon, lat = pixel_to_lon_lat(self.geo_transform, self.transformer, px_x, px_y)

        index = len(self.waypoints) + 1
        marker = WaypointMarker(px_x, px_y, index)
        self.scene_obj.addItem(marker)

        waypoint = Waypoint(px_x=px_x, px_y=px_y, lon=lon, lat=lat, marker=marker)
        self.waypoints.append(waypoint)
        marker.update_visual_scale(self._current_view_scale())

        if callable(self.on_waypoint_added):
            self.on_waypoint_added(index, lon, lat)

    def try_remove_waypoint(self, px_x: float, px_y: float):
        if not self.waypoints:
            return

        remove_idx = nearest_waypoint_index(self.waypoints, px_x, px_y)
        if remove_idx < 0:
            return

        self.remove_waypoint_by_index(remove_idx)

    def remove_waypoint_by_index(self, remove_idx: int):
        if remove_idx < 0 or remove_idx >= len(self.waypoints):
            return

        waypoint = self.waypoints.pop(remove_idx)
        self.scene_obj.removeItem(waypoint.marker)

        if callable(self.on_waypoint_removed):
            self.on_waypoint_removed(remove_idx)

        for idx, item in enumerate(self.waypoints, start=1):
            item.marker.update_index(idx)

        if callable(self.on_waypoints_reindexed):
            pairs = [(i + 1, wp.lon, wp.lat) for i, wp in enumerate(self.waypoints)]
            self.on_waypoints_reindexed(pairs)

    def focus_waypoint(self, index: int):
        if index < 0 or index >= len(self.waypoints):
            return
        target = self.waypoints[index]
        self.centerOn(target.px_x, target.px_y)
        self.update_timer.start(80)

    def get_lon_lat_points(self):
        return [(wp.lon, wp.lat) for wp in self.waypoints]
