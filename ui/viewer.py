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
        self.display_rgb_bands = None
        self._display_band_ranges = {}

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

    def set_display_rgb_bands(self, bands):
        self.display_rgb_bands = tuple(bands) if bands is not None else None
        self._display_band_ranges = {}
        if self.ds is None:
            return

        if self.base_item is not None:
            self.scene_obj.removeItem(self.base_item)
        self.base_item = self.create_base_layer()
        self.scene_obj.addItem(self.base_item)
        self.high_res_item.setZValue(5)
        self.refresh_marker_sizes()
        self.update_resolution()

    def _is_standard_rgb_layout(self) -> bool:
        if self.ds is None or self.ds.RasterCount < 3:
            return False
        ci = [self.ds.GetRasterBand(i).GetColorInterpretation() for i in [1, 2, 3]]
        return ci == [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand]

    def _resolve_display_rgb_bands(self):
        if self.ds is None or self.ds.RasterCount <= 0:
            return (1, 1, 1)

        count = self.ds.RasterCount
        if self.display_rgb_bands is not None:
            return tuple(max(1, min(count, int(v))) for v in self.display_rgb_bands)

        if count >= 3 and self._is_standard_rgb_layout():
            return (1, 2, 3)
        if count >= 3:
            return (3, 2, 1)
        return (1, 1, 1)

    def _to_uint8_gray(self, arr):
        return self._to_uint8_gray_with_range(arr, None, None, None)

    def _to_uint8_gray_with_range(self, arr, lo, hi, nodata):
        if arr is None:
            return np.zeros((1, 1), dtype=np.uint8)
        if arr.dtype == np.uint8:
            return arr

        a = np.asarray(arr, dtype=np.float32)
        valid = np.isfinite(a)
        if nodata is not None:
            valid &= ~np.isclose(a, float(nodata), rtol=0.0, atol=1e-6)
        if not np.any(valid):
            return np.zeros(a.shape, dtype=np.uint8)

        if lo is None or hi is None:
            vals = a[valid]
            lo, hi = np.percentile(vals, [2, 98])
            if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
                lo = float(np.min(vals))
                hi = float(np.max(vals))
                if hi <= lo:
                    return np.zeros(a.shape, dtype=np.uint8)

        a = np.nan_to_num(a, nan=lo, posinf=hi, neginf=lo)
        a = np.clip((a - lo) * 255.0 / (hi - lo), 0, 255)
        return a.astype(np.uint8)

    def _band_stretch_range(self, band_id: int):
        if band_id in self._display_band_ranges:
            return self._display_band_ranges[band_id]

        band = self.ds.GetRasterBand(band_id)
        ov_count = band.GetOverviewCount()
        if ov_count > 0:
            src = band.GetOverview(ov_count - 1)
            arr = src.ReadAsArray()
        else:
            target_w = min(2048, self.full_w)
            target_h = max(1, int(round(self.full_h * target_w / max(1, self.full_w))))
            arr = band.ReadAsArray(0, 0, self.full_w, self.full_h, buf_xsize=target_w, buf_ysize=target_h)

        a = np.asarray(arr, dtype=np.float32)
        valid = np.isfinite(a)
        nodata = band.GetNoDataValue()
        if nodata is not None:
            valid &= ~np.isclose(a, float(nodata), rtol=0.0, atol=1e-6)

        if np.any(valid):
            vals = a[valid]
            lo, hi = np.percentile(vals, [2, 98])
            if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
                lo = float(np.min(vals))
                hi = float(np.max(vals))
        else:
            lo, hi = 0.0, 255.0

        if hi <= lo:
            hi = lo + 1.0

        self._display_band_ranges[band_id] = (float(lo), float(hi))
        return self._display_band_ranges[band_id]

    def _read_rgb_uint8(self, x: int, y: int, w: int, h: int, out_w: int, out_h: int, band_ids):
        channels = []
        for band_id in band_ids:
            band = self.ds.GetRasterBand(band_id)
            arr = band.ReadAsArray(x, y, w, h, buf_xsize=out_w, buf_ysize=out_h)
            lo, hi = self._band_stretch_range(band_id)
            nodata = band.GetNoDataValue()
            channels.append(self._to_uint8_gray_with_range(arr, lo, hi, nodata))
        return np.dstack(channels)

    def _to_uint8_rgb(self, arr):
        a = np.asarray(arr)
        if a.dtype == np.uint8:
            return a
        if a.ndim == 3 and a.shape[2] == 3:
            channels = [self._to_uint8_gray(a[:, :, i]) for i in range(3)]
            return np.dstack(channels)
        return self._to_uint8_gray(a)

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
        band_ids = self._resolve_display_rgb_bands()
        channels = []
        for band_id in band_ids:
            b = self.ds.GetRasterBand(band_id).GetOverview(ov_idx)
            arr = b.ReadAsArray()
            lo, hi = self._band_stretch_range(band_id)
            nodata = self.ds.GetRasterBand(band_id).GetNoDataValue()
            channels.append(self._to_uint8_gray_with_range(arr, lo, hi, nodata))
        data = np.dstack(channels)

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
            band_ids = self._resolve_display_rgb_bands()
            rgb = self._read_rgb_uint8(x, y, w, h, target_w, target_h, band_ids)

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
