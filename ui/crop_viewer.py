from typing import List, Tuple

import numpy as np
from PySide6.QtCore import QPoint, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
)

from utils.env_setup import configure_runtime_env

configure_runtime_env()
from osgeo import gdal


gdal.UseExceptions()


class VertexMarker(QGraphicsItem):
    def __init__(self, x: float, y: float):
        super().__init__()
        self.radius = 5
        self.setPos(x, y)
        self.setZValue(30)
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)

    def boundingRect(self):
        r = self.radius
        return QRectF(-r - 2, -r - 2, (r + 2) * 2, (r + 2) * 2)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        r = self.radius
        painter.setBrush(QColor(255, 180, 0))
        painter.setPen(QPen(Qt.black, 1.5))
        painter.drawEllipse(-r, -r, r * 2, r * 2)


class CropViewer(QGraphicsView):
    def __init__(self):
        super().__init__()

        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.NoDrag)

        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)

        self.ds = None
        self.full_w = 0
        self.full_h = 0

        self.base_item = None
        self.high_res_item = QGraphicsPixmapItem()
        self.scene_obj.addItem(self.high_res_item)

        self.vertices: List[Tuple[float, float]] = []
        self.markers: List[VertexMarker] = []
        self.polygon_item = QGraphicsPathItem()
        self.polygon_item.setPen(QPen(QColor(255, 220, 0), 2))
        self.polygon_item.setBrush(QColor(255, 220, 0, 40))
        self.polygon_item.setZValue(25)
        self.scene_obj.addItem(self.polygon_item)

        self.saved_polygon_items: List[QGraphicsPathItem] = []
        self.saved_label_items: List[QGraphicsSimpleTextItem] = []
        self.saved_polygons_pixels = []

        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.timeout.connect(self.update_resolution)

        self._press_button = Qt.NoButton
        self._press_pos = QPoint()
        self._last_pan_pos = QPoint()
        self._dragging = False

        self.on_polygon_changed = None
        self.display_rotation_deg = 0.0

    def has_image(self) -> bool:
        return self.ds is not None

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
        center_scene = self.mapToScene(self.viewport().rect().center())
        if abs(delta) > 1e-9:
            self.rotate(delta)
        self.centerOn(center_scene)
        self.update_resolution()

    def reset_view(self):
        self.resetTransform()
        self.scene_obj.clear()
        self.base_item = None
        self.high_res_item = QGraphicsPixmapItem()
        self.scene_obj.addItem(self.high_res_item)

        self.vertices = []
        self.markers = []

        self.polygon_item = QGraphicsPathItem()
        self.polygon_item.setPen(QPen(QColor(255, 220, 0), 2))
        self.polygon_item.setBrush(QColor(255, 220, 0, 40))
        self.polygon_item.setZValue(25)
        self.scene_obj.addItem(self.polygon_item)

        self.saved_polygon_items = []
        self.saved_label_items = []
        self.saved_polygons_pixels = []

        self._notify_polygon_changed()

    def load_tif(self, tif_path: str):
        ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError("无法打开该 TIF 文件")
        if ds.RasterCount < 1:
            raise RuntimeError("影像无可读波段")

        self.reset_view()
        self.ds = ds
        self.full_w = ds.RasterXSize
        self.full_h = ds.RasterYSize

        self.base_item = self.create_base_layer()
        self.scene_obj.addItem(self.base_item)
        self.scene_obj.addItem(self.high_res_item)
        self._rebuild_saved_overlay()
        self.scene_obj.addItem(self.polygon_item)
        self.scene_obj.setSceneRect(0, 0, self.full_w, self.full_h)

        self._apply_base_view_transform()
        self.update_resolution()

    def _read_rgb(self, x: int, y: int, w: int, h: int, out_w: int, out_h: int):
        if self.ds.RasterCount >= 3:
            arr = np.dstack(
                [
                    self.ds.GetRasterBand(i).ReadAsArray(
                        x,
                        y,
                        w,
                        h,
                        buf_xsize=out_w,
                        buf_ysize=out_h,
                    )
                    for i in [1, 2, 3]
                ]
            )
        else:
            gray = self.ds.GetRasterBand(1).ReadAsArray(
                x,
                y,
                w,
                h,
                buf_xsize=out_w,
                buf_ysize=out_h,
            )
            arr = np.dstack([gray, gray, gray])
        return arr.astype(np.uint8, copy=False)

    def create_base_layer(self):
        band = self.ds.GetRasterBand(1)
        ov_count = band.GetOverviewCount()

        if ov_count > 0:
            ov_idx = ov_count - 1
            if self.ds.RasterCount >= 3:
                bands = [self.ds.GetRasterBand(i).GetOverview(ov_idx) for i in [1, 2, 3]]
                data = np.dstack([b.ReadAsArray() for b in bands]).astype(np.uint8, copy=False)
            else:
                b = self.ds.GetRasterBand(1).GetOverview(ov_idx)
                g = b.ReadAsArray().astype(np.uint8, copy=False)
                data = np.dstack([g, g, g])
            h, w, _ = data.shape
            qimg = QImage(data.data, w, h, w * 3, QImage.Format_RGB888)
            pix = QPixmap.fromImage(qimg.copy())
            item = QGraphicsPixmapItem(pix)
            item.setScale(self.full_w / w)
            item.setZValue(0)
            return item

        target_w = 2048
        ratio = self.full_h / max(1, self.full_w)
        target_h = max(1, int(target_w * ratio))
        data = self._read_rgb(0, 0, self.full_w, self.full_h, target_w, target_h)
        qimg = QImage(data.data, target_w, target_h, target_w * 3, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg.copy())
        item = QGraphicsPixmapItem(pix)
        item.setScale(self.full_w / target_w)
        item.setZValue(0)
        return item

    def wheelEvent(self, event):
        if self.ds is None:
            return
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)
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
        btn = self._press_button

        self._press_button = Qt.NoButton
        self._dragging = False
        self.unsetCursor()

        if was_dragging:
            self.update_timer.start(60)
            event.accept()
            return

        if btn == Qt.LeftButton:
            self.add_vertex(release_scene.x(), release_scene.y())
            event.accept()
            return

        if btn == Qt.RightButton:
            self.remove_last_vertex()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def update_resolution(self):
        if self.ds is None:
            return

        viewport_rect = self.viewport().rect()
        t = self.transform()
        view_scale = max(1e-6, (t.m11() ** 2 + t.m21() ** 2) ** 0.5)

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
            data = self._read_rgb(x, y, w, h, target_w, target_h)
            qimg = QImage(data.data, target_w, target_h, target_w * 3, QImage.Format_RGB888)
            self.high_res_item.setPixmap(QPixmap.fromImage(qimg.copy()))
            self.high_res_item.setPos(x, y)
            self.high_res_item.setScale(w / target_w)
            self.high_res_item.setZValue(5)
        except Exception as exc:
            print(f"动态加载失败: {exc}")

    def add_vertex(self, px_x: float, px_y: float):
        if not (0 <= px_x < self.full_w and 0 <= px_y < self.full_h):
            return
        self.vertices.append((px_x, px_y))
        marker = VertexMarker(px_x, px_y)
        self.scene_obj.addItem(marker)
        self.markers.append(marker)
        self._refresh_polygon_path()
        self._notify_polygon_changed()

    def remove_last_vertex(self):
        if not self.vertices:
            return
        self.vertices.pop()
        marker = self.markers.pop()
        self.scene_obj.removeItem(marker)
        self._refresh_polygon_path()
        self._notify_polygon_changed()

    def clear_polygon(self):
        while self.markers:
            marker = self.markers.pop()
            self.scene_obj.removeItem(marker)
        self.vertices = []
        self._refresh_polygon_path()
        self._notify_polygon_changed()

    def _refresh_polygon_path(self):
        path = QPainterPath()
        if not self.vertices:
            self.polygon_item.setPath(path)
            return

        x0, y0 = self.vertices[0]
        path.moveTo(x0, y0)
        for x, y in self.vertices[1:]:
            path.lineTo(x, y)
        if len(self.vertices) >= 3:
            path.closeSubpath()

        self.polygon_item.setPath(path)

    def get_polygon_pixels(self):
        return list(self.vertices)

    def set_polygon_pixels(self, vertices: List[Tuple[float, float]]):
        self.clear_polygon()
        for px_x, px_y in vertices:
            if not (0 <= px_x < self.full_w and 0 <= px_y < self.full_h):
                continue
            self.vertices.append((float(px_x), float(px_y)))
            marker = VertexMarker(px_x, px_y)
            self.scene_obj.addItem(marker)
            self.markers.append(marker)
        self._refresh_polygon_path()
        self._notify_polygon_changed()

    def set_saved_polygons(self, polygons_pixels):
        self.saved_polygons_pixels = list(polygons_pixels)
        self._rebuild_saved_overlay()

    def _rebuild_saved_overlay(self):
        for item in self.saved_polygon_items:
            self.scene_obj.removeItem(item)
        for item in self.saved_label_items:
            self.scene_obj.removeItem(item)
        self.saved_polygon_items = []
        self.saved_label_items = []

        for entry in self.saved_polygons_pixels:
            name = str(entry.get("name") or "")
            vertices = list(entry.get("pixels") or [])
            if len(vertices) < 3:
                continue

            path = QPainterPath()
            x0, y0 = vertices[0]
            path.moveTo(x0, y0)
            for x, y in vertices[1:]:
                path.lineTo(x, y)
            path.closeSubpath()

            poly_item = QGraphicsPathItem(path)
            poly_item.setPen(QPen(QColor(255, 120, 0), 2))
            poly_item.setBrush(QColor(255, 120, 0, 35))
            poly_item.setZValue(18)
            self.scene_obj.addItem(poly_item)
            self.saved_polygon_items.append(poly_item)

            cx, cy = self._polygon_center(vertices)
            label_item = QGraphicsSimpleTextItem(name)
            font = QFont("Arial", 10, QFont.Bold)
            label_item.setFont(font)
            label_item.setBrush(QColor(20, 20, 20))
            label_item.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
            br = label_item.boundingRect()
            label_item.setPos(cx - br.width() / 2, cy - br.height() / 2)
            label_item.setZValue(19)
            self.scene_obj.addItem(label_item)
            self.saved_label_items.append(label_item)

    def _polygon_center(self, vertices: List[Tuple[float, float]]):
        x_sum = 0.0
        y_sum = 0.0
        for x, y in vertices:
            x_sum += float(x)
            y_sum += float(y)
        n = max(1, len(vertices))
        return x_sum / n, y_sum / n

    def pixel_to_geo(self, px: float, py: float):
        if self.ds is None:
            raise RuntimeError("当前未加载影像")
        gt = self.ds.GetGeoTransform()
        gx = gt[0] + px * gt[1] + py * gt[2]
        gy = gt[3] + px * gt[4] + py * gt[5]
        return gx, gy

    def geo_to_pixel(self, gx: float, gy: float):
        if self.ds is None:
            raise RuntimeError("当前未加载影像")
        inv_ret = gdal.InvGeoTransform(self.ds.GetGeoTransform())
        if inv_ret is None:
            raise RuntimeError("影像地理变换不可逆，无法坐标转换")
        if isinstance(inv_ret, tuple) and len(inv_ret) == 2 and isinstance(inv_ret[0], int):
            ok, inv_gt = inv_ret
            if not ok:
                raise RuntimeError("影像地理变换不可逆，无法坐标转换")
        else:
            inv_gt = inv_ret
        px = inv_gt[0] + gx * inv_gt[1] + gy * inv_gt[2]
        py = inv_gt[3] + gx * inv_gt[4] + gy * inv_gt[5]
        return px, py

    def projection_wkt(self) -> str:
        if self.ds is None:
            return ""
        return self.ds.GetProjection() or ""

    def _notify_polygon_changed(self):
        if callable(self.on_polygon_changed):
            self.on_polygon_changed(len(self.vertices))
