import math
from typing import List, Tuple

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QTimer, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QLabel,
)

from logic.plot_grid import plots_from_dividers, redistribute_column_dividers
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


class OverlayBadgeItem(QGraphicsItem):
    def __init__(
        self,
        text: str,
        background: QColor,
        foreground: QColor,
        font_size: int = 12,
        padding_x: int = 7,
        padding_y: int = 4,
    ):
        super().__init__()
        self.text = str(text)
        self.background = QColor(background)
        self.foreground = QColor(foreground)
        self.border = QColor(255, 255, 255, 220)
        self.font = QFont("Microsoft YaHei", font_size, QFont.Black)
        self.padding_x = padding_x
        self.padding_y = padding_y
        self.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)

    def set_text(self, text: str):
        text = str(text)
        if text == self.text:
            return
        self.prepareGeometryChange()
        self.text = text
        self.update()

    def set_colors(self, background: QColor, foreground: QColor):
        self.background = QColor(background)
        self.foreground = QColor(foreground)
        self.update()

    def boundingRect(self):
        metrics = QFontMetricsF(self.font)
        text_rect = metrics.boundingRect(self.text or " ")
        width = max(28.0, text_rect.width() + self.padding_x * 2)
        height = max(24.0, text_rect.height() + self.padding_y * 2)
        return QRectF(-width / 2, -height / 2, width, height)

    def paint(self, painter: QPainter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.boundingRect()
        painter.setPen(QPen(self.border, 1.5))
        painter.setBrush(self.background)
        painter.drawRoundedRect(rect, 7, 7)
        painter.setFont(self.font)
        painter.setPen(self.foreground)
        painter.drawText(rect, Qt.AlignCenter, self.text)


class CropViewer(QGraphicsView):
    def __init__(self):
        super().__init__()

        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)

        self.ds = None
        self.full_w = 0
        self.full_h = 0
        self.alpha_band_index = None

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
        self.saved_polygon_item_indices = []
        self.saved_polygons_pixels = []
        self.selected_saved_polygon_index = -1

        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.timeout.connect(self.update_resolution)

        self._press_button = Qt.NoButton
        self._press_pos = QPoint()
        self._last_pan_pos = QPoint()
        self._dragging = False
        self._drag_vertex_index = -1
        self._skip_release_add_once = False
        self._polygon_history = []
        self._restoring_polygon = False

        self.column_edit_active = False
        self.column_dividers = []
        self.column_start_end = "a"
        self.column_plot_items = []
        self.column_divider_items = []
        self.column_handle_items = []
        self.column_endpoint_labels = []
        self.column_name_items = []
        self.column_direction_items = []
        self.column_plot_names = []
        self.column_selected_plot_index = 0
        self.column_selected_divider_index = -1
        self._column_history = []
        self._column_drag = None
        self._column_hover = None

        self.on_polygon_changed = None
        self.on_polygon_geometry_changed = None
        self.on_polygon_finish_requested = None
        self.on_saved_polygon_clicked = None
        self.on_edit_cancel_requested = None
        self.on_column_changed = None
        self.on_column_plot_selected = None
        self.on_column_cancel_requested = None
        self.display_rotation_deg = 0.0
        self.display_rgb_bands = None
        self._display_band_ranges = {}

        self.magnifier_label = QLabel(self.viewport())
        self.magnifier_label.setFixedSize(168, 168)
        self.magnifier_label.setStyleSheet(
            "QLabel { background: white; border: 2px solid #00b7ff; padding: 2px; }"
        )
        self.magnifier_label.setAlignment(Qt.AlignCenter)
        self.magnifier_label.hide()

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
        self.stop_column_edit()
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
        self.saved_polygon_item_indices = []
        self.saved_polygons_pixels = []
        self.selected_saved_polygon_index = -1
        self._polygon_history = []
        self._drag_vertex_index = -1
        self.magnifier_label.hide()

        self._notify_polygon_changed()

    def unload_image(self):
        self.reset_view()
        self.ds = None
        self.full_w = 0
        self.full_h = 0
        self.alpha_band_index = None
        self._display_band_ranges = {}

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
        self.alpha_band_index = self._detect_alpha_band_index()

        self.base_item = self.create_base_layer()
        self.scene_obj.addItem(self.base_item)
        self.scene_obj.addItem(self.high_res_item)
        self._rebuild_saved_overlay()
        self.scene_obj.addItem(self.polygon_item)
        self.scene_obj.setSceneRect(0, 0, self.full_w, self.full_h)

        self._apply_base_view_transform()
        self.update_resolution()

    def _detect_alpha_band_index(self):
        if self.ds is None:
            return None
        for i in range(1, self.ds.RasterCount + 1):
            band = self.ds.GetRasterBand(i)
            if band.GetColorInterpretation() == gdal.GCI_AlphaBand:
                return i
        return None

    def _read_rgba(self, x: int, y: int, w: int, h: int, out_w: int, out_h: int):
        if self.ds.RasterCount >= 3:
            band_ids = self._resolve_display_rgb_bands()
            rgb = self._read_rgb_uint8(x, y, w, h, out_w, out_h, band_ids)
        else:
            gray = self.ds.GetRasterBand(1).ReadAsArray(
                x,
                y,
                w,
                h,
                buf_xsize=out_w,
                buf_ysize=out_h,
            )
            g = self._to_uint8_gray_with_range(gray, None, None, self.ds.GetRasterBand(1).GetNoDataValue())
            rgb = np.dstack([g, g, g])

        if self.alpha_band_index is not None and self.alpha_band_index <= self.ds.RasterCount:
            alpha = self.ds.GetRasterBand(self.alpha_band_index).ReadAsArray(
                x,
                y,
                w,
                h,
                buf_xsize=out_w,
                buf_ysize=out_h,
            )
        else:
            alpha = np.full((out_h, out_w), 255, dtype=np.uint8)

        alpha = self._to_uint8_gray_with_range(alpha, None, None, None)
        return np.dstack([rgb, alpha])

    def create_base_layer(self):
        band = self.ds.GetRasterBand(1)
        ov_count = band.GetOverviewCount()

        if ov_count > 0:
            ov_idx = ov_count - 1
            if self.ds.RasterCount >= 3:
                band_ids = self._resolve_display_rgb_bands()
                channels = []
                for band_id in band_ids:
                    b = self.ds.GetRasterBand(band_id).GetOverview(ov_idx)
                    arr = b.ReadAsArray()
                    lo, hi = self._band_stretch_range(band_id)
                    nodata = self.ds.GetRasterBand(band_id).GetNoDataValue()
                    channels.append(self._to_uint8_gray_with_range(arr, lo, hi, nodata))
                rgb = np.dstack(channels)
            else:
                b = self.ds.GetRasterBand(1).GetOverview(ov_idx)
                g = self._to_uint8_gray_with_range(b.ReadAsArray(), None, None, self.ds.GetRasterBand(1).GetNoDataValue())
                rgb = np.dstack([g, g, g])

            if self.alpha_band_index is not None and self.alpha_band_index <= self.ds.RasterCount:
                a_band = self.ds.GetRasterBand(self.alpha_band_index)
                a_ov = a_band.GetOverview(ov_idx)
                if a_ov is not None:
                    alpha = self._to_uint8_gray_with_range(a_ov.ReadAsArray(), None, None, a_band.GetNoDataValue())
                else:
                    alpha = self._to_uint8_gray(a_band.ReadAsArray(
                        0,
                        0,
                        self.full_w,
                        self.full_h,
                        buf_xsize=rgb.shape[1],
                        buf_ysize=rgb.shape[0],
                    ))
            else:
                alpha = np.full((rgb.shape[0], rgb.shape[1]), 255, dtype=np.uint8)

            data = np.dstack([rgb, alpha])
            h, w, _ = data.shape
            qimg = QImage(data.data, w, h, w * 4, QImage.Format_RGBA8888)
            pix = QPixmap.fromImage(qimg.copy())
            item = QGraphicsPixmapItem(pix)
            item.setScale(self.full_w / w)
            item.setZValue(0)
            return item

        target_w = 2048
        ratio = self.full_h / max(1, self.full_w)
        target_h = max(1, int(target_w * ratio))
        data = self._read_rgba(0, 0, self.full_w, self.full_h, target_w, target_h)
        qimg = QImage(data.data, target_w, target_h, target_w * 4, QImage.Format_RGBA8888)
        pix = QPixmap.fromImage(qimg.copy())
        item = QGraphicsPixmapItem(pix)
        item.setScale(self.full_w / target_w)
        item.setZValue(0)
        return item

    @staticmethod
    def _copy_dividers(dividers):
        return [
            (
                (float(left[0]), float(left[1])),
                (float(right[0]), float(right[1])),
            )
            for left, right in dividers
        ]

    def get_column_dividers(self):
        return self._copy_dividers(self.column_dividers)

    def start_column_edit(
        self,
        dividers,
        start_end: str = "a",
        plot_names=None,
    ):
        normalized = self._copy_dividers(dividers)
        if len(normalized) < 2:
            raise ValueError("整列至少需要1个小区")
        if not self._column_geometry_is_valid(normalized):
            raise ValueError("整列边界或分隔线无效")

        self.clear_polygon()
        self.stop_column_edit()
        self.column_edit_active = True
        self.column_dividers = normalized
        self.column_start_end = "b" if str(start_end).lower() == "b" else "a"
        self.column_plot_names = [str(name) for name in (plot_names or [])]
        self.column_selected_plot_index = (
            len(normalized) - 2 if self.column_start_end == "b" else 0
        )
        self.column_selected_divider_index = -1
        self._column_history = []
        self._column_drag = None
        self._column_hover = None
        self._refresh_column_editor(force_rebuild=True)
        self.setFocus()

    def stop_column_edit(self):
        self._clear_column_graphics()
        self.column_edit_active = False
        self.column_dividers = []
        self.column_plot_names = []
        self.column_selected_plot_index = 0
        self.column_selected_divider_index = -1
        self._column_history = []
        self._column_drag = None
        self._column_hover = None
        if hasattr(self, "magnifier_label"):
            self.magnifier_label.hide()

    def _clear_column_graphics(self):
        groups = [
            self.column_plot_items,
            self.column_divider_items,
            self.column_handle_items,
            self.column_endpoint_labels,
            self.column_name_items,
            self.column_direction_items,
        ]
        for group in groups:
            for item in group:
                if item.scene() is self.scene_obj:
                    self.scene_obj.removeItem(item)
            group.clear()

    def _make_cosmetic_pen(self, color: QColor, width: float) -> QPen:
        pen = QPen(color, width)
        pen.setCosmetic(True)
        return pen

    def _refresh_column_editor(self, force_rebuild: bool = False):
        if not self.column_edit_active or len(self.column_dividers) < 2:
            return

        plot_count = len(self.column_dividers) - 1
        expected_handles = len(self.column_dividers) * 2
        if (
            force_rebuild
            or len(self.column_plot_items) != plot_count
            or len(self.column_divider_items) != len(self.column_dividers)
            or len(self.column_handle_items) != expected_handles
            or len(self.column_endpoint_labels) != 2
            or len(self.column_name_items) != plot_count
        ):
            self._clear_column_graphics()

            for _ in range(plot_count):
                item = QGraphicsPathItem()
                item.setZValue(35)
                self.scene_obj.addItem(item)
                self.column_plot_items.append(item)

            for _ in self.column_dividers:
                item = QGraphicsLineItem()
                item.setZValue(38)
                self.scene_obj.addItem(item)
                self.column_divider_items.append(item)

            for _ in range(expected_handles):
                item = QGraphicsEllipseItem(-7, -7, 14, 14)
                item.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
                item.setZValue(42)
                self.scene_obj.addItem(item)
                self.column_handle_items.append(item)

            endpoint_a = OverlayBadgeItem(
                "A端",
                QColor(0, 165, 90, 245),
                QColor(255, 255, 255),
                font_size=16,
                padding_x=10,
                padding_y=6,
            )
            endpoint_b = OverlayBadgeItem(
                "B端",
                QColor(25, 105, 225, 245),
                QColor(255, 255, 255),
                font_size=16,
                padding_x=10,
                padding_y=6,
            )
            for item in (endpoint_a, endpoint_b):
                item.setZValue(47)
                self.scene_obj.addItem(item)
                self.column_endpoint_labels.append(item)

            for _ in range(plot_count):
                item = OverlayBadgeItem(
                    "",
                    QColor(15, 15, 15, 205),
                    QColor(255, 255, 255),
                    font_size=12,
                    padding_x=7,
                    padding_y=4,
                )
                item.setZValue(46)
                self.scene_obj.addItem(item)
                self.column_name_items.append(item)

            arrow_line = QGraphicsLineItem()
            arrow_line.setZValue(44)
            self.scene_obj.addItem(arrow_line)
            arrow_head = QGraphicsPathItem()
            arrow_head.setZValue(44)
            self.scene_obj.addItem(arrow_head)
            self.column_direction_items.extend([arrow_line, arrow_head])

        plots = plots_from_dividers(self.column_dividers)
        for index, (item, vertices) in enumerate(zip(self.column_plot_items, plots)):
            path = self._path_from_vertices(vertices)
            item.setPath(path)
            if index == self.column_selected_plot_index:
                item.setPen(self._make_cosmetic_pen(QColor(0, 210, 255, 235), 3))
                item.setBrush(QColor(0, 190, 255, 65))
            else:
                item.setPen(self._make_cosmetic_pen(QColor(255, 75, 35, 235), 2.5))
                item.setBrush(QColor(0, 0, 0, 0))

        for index, (item, vertices) in enumerate(zip(self.column_name_items, plots)):
            name = (
                self.column_plot_names[index]
                if index < len(self.column_plot_names)
                else f"小区 {index + 1}"
            )
            item.set_text(name)
            item.setPos(*self._polygon_center(vertices))
            if index == self.column_selected_plot_index:
                item.set_colors(
                    QColor(0, 115, 165, 235),
                    QColor(255, 255, 255),
                )
            else:
                item.set_colors(
                    QColor(15, 15, 15, 205),
                    QColor(255, 255, 255),
                )

        for index, (item, divider) in enumerate(
            zip(self.column_divider_items, self.column_dividers)
        ):
            left, right = divider
            item.setLine(left[0], left[1], right[0], right[1])
            is_hovered = self._column_hover == ("divider", index)
            is_selected = index == self.column_selected_divider_index
            if is_hovered or is_selected:
                color = QColor(255, 70, 70, 255)
                width = 4
            elif index in (0, len(self.column_dividers) - 1):
                color = QColor(30, 230, 120, 245)
                width = 3
            else:
                color = QColor(255, 220, 0, 235)
                width = 2.5
            item.setPen(self._make_cosmetic_pen(color, width))

        for divider_index, divider in enumerate(self.column_dividers):
            for side, point in enumerate(divider):
                handle_index = divider_index * 2 + side
                item = self.column_handle_items[handle_index]
                item.setPos(point[0], point[1])
                is_hovered = self._column_hover == (
                    "handle",
                    divider_index,
                    side,
                )
                outer = divider_index in (0, len(self.column_dividers) - 1)
                radius = 9 if is_hovered else (8 if outer else 7)
                item.setRect(-radius, -radius, radius * 2, radius * 2)
                item.setPen(
                    self._make_cosmetic_pen(
                        QColor(255, 255, 255) if is_hovered else QColor(30, 30, 30),
                        2,
                    )
                )
                if is_hovered:
                    item.setBrush(QColor(255, 70, 70))
                elif outer:
                    item.setBrush(QColor(30, 230, 120))
                else:
                    item.setBrush(QColor(255, 210, 0))

        self._refresh_column_direction_overlay(plots)

    def _path_from_vertices(self, vertices):
        path = QPainterPath()
        if not vertices:
            return path
        path.moveTo(vertices[0][0], vertices[0][1])
        for x, y in vertices[1:]:
            path.lineTo(x, y)
        path.closeSubpath()
        return path

    def _refresh_column_direction_overlay(self, plots):
        if not self.column_endpoint_labels or len(self.column_direction_items) != 2:
            return

        first_center = self._divider_center(self.column_dividers[0])
        last_center = self._divider_center(self.column_dividers[-1])
        axis_x = last_center[0] - first_center[0]
        axis_y = last_center[1] - first_center[1]
        axis_length = max(1e-9, math.hypot(axis_x, axis_y))
        unit_x, unit_y = axis_x / axis_length, axis_y / axis_length
        perpendicular_x, perpendicular_y = -unit_y, unit_x

        view_scale = max(
            1e-6,
            (self.transform().m11() ** 2 + self.transform().m21() ** 2) ** 0.5,
        )
        half_width = max(
            math.hypot(
                point[0] - center[0],
                point[1] - center[1],
            )
            for divider, center in (
                (self.column_dividers[0], first_center),
                (self.column_dividers[-1], last_center),
            )
            for point in divider
        )
        side_offset = half_width + 52.0 / view_scale
        end_offset = 18.0 / view_scale

        def badge_positions(side_sign):
            return (
                (
                    first_center[0]
                    + perpendicular_x * side_offset * side_sign
                    - unit_x * end_offset,
                    first_center[1]
                    + perpendicular_y * side_offset * side_sign
                    - unit_y * end_offset,
                ),
                (
                    last_center[0]
                    + perpendicular_x * side_offset * side_sign
                    + unit_x * end_offset,
                    last_center[1]
                    + perpendicular_y * side_offset * side_sign
                    + unit_y * end_offset,
                ),
            )

        def boundary_clearance(positions):
            clearances = []
            for x, y in positions:
                clearances.extend([x, y, self.full_w - x, self.full_h - y])
            return min(clearances)

        positive_positions = badge_positions(1.0)
        negative_positions = badge_positions(-1.0)
        if boundary_clearance(negative_positions) > boundary_clearance(positive_positions):
            badge_a_pos, badge_b_pos = negative_positions
        else:
            badge_a_pos, badge_b_pos = positive_positions

        label_a, label_b = self.column_endpoint_labels
        label_a.setPos(*badge_a_pos)
        label_b.setPos(*badge_b_pos)

        if self.column_start_end == "a":
            start_center, end_center = badge_a_pos, badge_b_pos
            label_a.set_text("A端 · 起点")
            label_b.set_text("B端")
        else:
            start_center, end_center = badge_b_pos, badge_a_pos
            label_a.set_text("A端")
            label_b.set_text("B端 · 起点")

        arrow_line, arrow_head = self.column_direction_items
        arrow_line.setLine(
            start_center[0],
            start_center[1],
            end_center[0],
            end_center[1],
        )
        arrow_line.setPen(self._make_cosmetic_pen(QColor(255, 55, 180, 245), 4.5))

        dx = end_center[0] - start_center[0]
        dy = end_center[1] - start_center[1]
        length = max(1e-9, math.hypot(dx, dy))
        ux, uy = dx / length, dy / length
        size = 20.0 / view_scale
        tip_x = start_center[0] + dx * 0.62
        tip_y = start_center[1] + dy * 0.62
        base_x = tip_x - ux * size
        base_y = tip_y - uy * size
        perp_x, perp_y = -uy, ux
        head_path = QPainterPath(QPointF(tip_x, tip_y))
        head_path.lineTo(
            base_x + perp_x * size * 0.55,
            base_y + perp_y * size * 0.55,
        )
        head_path.lineTo(
            base_x - perp_x * size * 0.55,
            base_y - perp_y * size * 0.55,
        )
        head_path.closeSubpath()
        arrow_head.setPath(head_path)
        arrow_head.setPen(self._make_cosmetic_pen(QColor(255, 55, 180), 1))
        arrow_head.setBrush(QColor(255, 55, 180))

    @staticmethod
    def _divider_center(divider):
        left, right = divider
        return ((left[0] + right[0]) * 0.5, (left[1] + right[1]) * 0.5)

    @staticmethod
    def _center_text_item(item, center):
        bounds = item.boundingRect()
        item.setPos(center[0] - bounds.width() / 2, center[1] - bounds.height() / 2)

    def _push_column_history(self):
        snapshot = (self.get_column_dividers(), self.column_start_end)
        if self._column_history and self._column_history[-1] == snapshot:
            return
        self._column_history.append(snapshot)
        if len(self._column_history) > 50:
            self._column_history.pop(0)

    def undo_column_edit(self):
        if not self.column_edit_active or not self._column_history:
            return False
        dividers, start_end = self._column_history.pop()
        self.column_dividers = self._copy_dividers(dividers)
        self.column_start_end = start_end
        self.column_selected_plot_index = min(
            self.column_selected_plot_index,
            len(self.column_dividers) - 2,
        )
        self.column_selected_divider_index = -1
        self._refresh_column_editor(force_rebuild=True)
        self._emit_column_changed(final=True)
        return True

    def redistribute_column(self):
        if not self.column_edit_active:
            return False
        self._push_column_history()
        self.column_dividers = redistribute_column_dividers(self.column_dividers)
        self._refresh_column_editor()
        self._emit_column_changed(final=True)
        return True

    def add_column_divider(self):
        if not self.column_edit_active:
            return False
        plot_index = max(
            0,
            min(self.column_selected_plot_index, len(self.column_dividers) - 2),
        )
        self._push_column_history()
        top = self.column_dividers[plot_index]
        bottom = self.column_dividers[plot_index + 1]
        midpoint = (
            (
                (top[0][0] + bottom[0][0]) * 0.5,
                (top[0][1] + bottom[0][1]) * 0.5,
            ),
            (
                (top[1][0] + bottom[1][0]) * 0.5,
                (top[1][1] + bottom[1][1]) * 0.5,
            ),
        )
        self.column_dividers.insert(plot_index + 1, midpoint)
        self.column_selected_plot_index = plot_index
        self.column_selected_divider_index = plot_index + 1
        self._refresh_column_editor(force_rebuild=True)
        self._emit_column_changed(final=True)
        return True

    def delete_selected_column_divider(self):
        if not self.column_edit_active:
            return False
        index = self.column_selected_divider_index
        if index <= 0 or index >= len(self.column_dividers) - 1:
            return False
        self._push_column_history()
        self.column_dividers.pop(index)
        self.column_selected_divider_index = -1
        self.column_selected_plot_index = min(
            self.column_selected_plot_index,
            len(self.column_dividers) - 2,
        )
        self._refresh_column_editor(force_rebuild=True)
        self._emit_column_changed(final=True)
        return True

    def reverse_column_direction(self):
        if not self.column_edit_active:
            return False
        self._push_column_history()
        self.column_start_end = "b" if self.column_start_end == "a" else "a"
        self.column_selected_plot_index = (
            len(self.column_dividers) - 2 - self.column_selected_plot_index
        )
        self._refresh_column_editor()
        self._emit_column_changed(final=True)
        return True

    def set_selected_column_plot(self, index: int, notify: bool = False):
        if not self.column_edit_active:
            return
        index = max(0, min(int(index), len(self.column_dividers) - 2))
        self.column_selected_plot_index = index
        self._refresh_column_editor()
        if notify and callable(self.on_column_plot_selected):
            self.on_column_plot_selected(index)

    def set_column_plot_names(self, names):
        normalized = [str(name) for name in names]
        if normalized == self.column_plot_names:
            return
        self.column_plot_names = normalized
        self._refresh_column_editor()

    def _emit_column_changed(self, final: bool):
        if callable(self.on_column_changed):
            self.on_column_changed(
                self.get_column_dividers(),
                self.column_start_end,
                bool(final),
            )

    def _column_handle_at(self, view_pos: QPoint):
        nearest = None
        nearest_distance = float("inf")
        for divider_index, divider in enumerate(self.column_dividers):
            for side, point in enumerate(divider):
                point_view = self.mapFromScene(QPointF(point[0], point[1]))
                distance = math.hypot(
                    point_view.x() - view_pos.x(),
                    point_view.y() - view_pos.y(),
                )
                if distance <= 14.0 and distance < nearest_distance:
                    nearest = (divider_index, side)
                    nearest_distance = distance
        return nearest

    def _column_divider_at(self, view_pos: QPoint):
        nearest = -1
        nearest_distance = float("inf")
        for index, (left, right) in enumerate(self.column_dividers):
            p1 = self.mapFromScene(QPointF(left[0], left[1]))
            p2 = self.mapFromScene(QPointF(right[0], right[1]))
            distance = self._distance_point_to_segment(
                view_pos.x(),
                view_pos.y(),
                p1.x(),
                p1.y(),
                p2.x(),
                p2.y(),
            )
            if distance <= 9.0 and distance < nearest_distance:
                nearest = index
                nearest_distance = distance
        return nearest

    def _column_plot_at(self, scene_pos: QPointF):
        for index, vertices in enumerate(plots_from_dividers(self.column_dividers)):
            if self._path_from_vertices(vertices).contains(scene_pos):
                return index
        return -1

    @staticmethod
    def _distance_point_to_segment(px, py, x1, y1, x2, y2):
        dx = x2 - x1
        dy = y2 - y1
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            return math.hypot(px - x1, py - y1)
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    def _begin_column_interaction(self, event):
        view_pos = event.pos()
        scene_pos = self.mapToScene(view_pos)

        handle = self._column_handle_at(view_pos)
        if handle is not None:
            self._push_column_history()
            divider_index, side = handle
            self._column_drag = {
                "kind": "handle",
                "divider": divider_index,
                "side": side,
                "start_scene": scene_pos,
                "origin": self.get_column_dividers(),
            }
            self.setCursor(Qt.SizeAllCursor)
            return True

        divider_index = self._column_divider_at(view_pos)
        if divider_index >= 0:
            self._push_column_history()
            self.column_selected_divider_index = divider_index
            self._column_drag = {
                "kind": "divider",
                "divider": divider_index,
                "start_scene": scene_pos,
                "origin": self.get_column_dividers(),
            }
            self._refresh_column_editor()
            self.setCursor(Qt.SizeAllCursor)
            return True

        plot_index = self._column_plot_at(scene_pos)
        if plot_index >= 0:
            self.column_selected_plot_index = plot_index
            self.column_selected_divider_index = -1
            self._column_drag = {
                "kind": "whole_pending",
                "plot": plot_index,
                "start_scene": scene_pos,
                "start_view": QPoint(view_pos),
                "origin": self.get_column_dividers(),
            }
            self._refresh_column_editor()
            self.setCursor(Qt.OpenHandCursor)
            return True

        return False

    def _update_column_drag(self, event):
        if not self._column_drag:
            return False

        drag = self._column_drag
        scene_pos = self.mapToScene(event.pos())
        origin = drag["origin"]
        start_scene = drag["start_scene"]
        dx = scene_pos.x() - start_scene.x()
        dy = scene_pos.y() - start_scene.y()

        if drag["kind"] == "whole_pending":
            if (event.pos() - drag["start_view"]).manhattanLength() < 5:
                return True
            self._push_column_history()
            drag["kind"] = "whole"
            self.setCursor(Qt.ClosedHandCursor)

        if drag["kind"] == "whole":
            all_points = [point for divider in origin for point in divider]
            min_x = min(p[0] for p in all_points)
            max_x = max(p[0] for p in all_points)
            min_y = min(p[1] for p in all_points)
            max_y = max(p[1] for p in all_points)
            dx = max(-min_x, min(dx, self.full_w - 1 - max_x))
            dy = max(-min_y, min(dy, self.full_h - 1 - max_y))
            self.column_dividers = [
                (
                    (left[0] + dx, left[1] + dy),
                    (right[0] + dx, right[1] + dy),
                )
                for left, right in origin
            ]

        elif drag["kind"] == "divider":
            self.column_dividers = self._copy_dividers(origin)
            index = drag["divider"]
            left, right = origin[index]
            self.column_dividers[index] = (
                self._clamp_scene_point(left[0] + dx, left[1] + dy),
                self._clamp_scene_point(right[0] + dx, right[1] + dy),
            )
            if 0 < index < len(origin) - 1:
                self._constrain_internal_divider(index)

        elif drag["kind"] == "handle":
            self.column_dividers = self._copy_dividers(origin)
            index = drag["divider"]
            side = drag["side"]
            moved = self._clamp_scene_point(scene_pos.x(), scene_pos.y())
            if index in (0, len(origin) - 1):
                self._move_outer_column_corner(origin, index, side, moved)
            else:
                current = list(self.column_dividers[index])
                current[side] = moved
                self.column_dividers[index] = tuple(current)
                self._constrain_internal_divider(index, sides=(side,))

        if self._column_geometry_is_valid(self.column_dividers):
            drag["last_valid"] = self.get_column_dividers()
        else:
            self.column_dividers = self._copy_dividers(
                drag.get("last_valid", origin)
            )

        self._refresh_column_editor()
        self._emit_column_changed(final=False)
        self._show_magnifier(event.pos())
        return True

    def _move_outer_column_corner(self, origin, divider_index, side, moved):
        count = len(origin) - 1
        old_start = origin[0][side]
        old_end = origin[-1][side]
        new_start = moved if divider_index == 0 else old_start
        new_end = moved if divider_index == count else old_end

        for index in range(count + 1):
            t = index / count
            base_old = (
                old_start[0] + (old_end[0] - old_start[0]) * t,
                old_start[1] + (old_end[1] - old_start[1]) * t,
            )
            residual = (
                origin[index][side][0] - base_old[0],
                origin[index][side][1] - base_old[1],
            )
            base_new = (
                new_start[0] + (new_end[0] - new_start[0]) * t,
                new_start[1] + (new_end[1] - new_start[1]) * t,
            )
            new_point = self._clamp_scene_point(
                base_new[0] + residual[0],
                base_new[1] + residual[1],
            )
            current = list(self.column_dividers[index])
            current[side] = new_point
            self.column_dividers[index] = tuple(current)

    def _constrain_internal_divider(self, divider_index, sides=(0, 1)):
        for side in sides:
            previous = self.column_dividers[divider_index - 1][side]
            current = self.column_dividers[divider_index][side]
            following = self.column_dividers[divider_index + 1][side]
            axis_x = following[0] - previous[0]
            axis_y = following[1] - previous[1]
            axis_len_sq = axis_x * axis_x + axis_y * axis_y
            if axis_len_sq <= 1e-12:
                continue
            t = (
                (current[0] - previous[0]) * axis_x
                + (current[1] - previous[1]) * axis_y
            ) / axis_len_sq
            clamped_t = max(0.03, min(0.97, t))
            if abs(clamped_t - t) <= 1e-12:
                continue
            correction_x = (clamped_t - t) * axis_x
            correction_y = (clamped_t - t) * axis_y
            adjusted = self._clamp_scene_point(
                current[0] + correction_x,
                current[1] + correction_y,
            )
            divider = list(self.column_dividers[divider_index])
            divider[side] = adjusted
            self.column_dividers[divider_index] = tuple(divider)

    def _column_geometry_is_valid(self, dividers):
        try:
            plots = plots_from_dividers(dividers)
        except Exception:
            return False
        for vertices in plots:
            area_twice = 0.0
            for index, point in enumerate(vertices):
                next_point = vertices[(index + 1) % len(vertices)]
                area_twice += point[0] * next_point[1] - next_point[0] * point[1]
            if abs(area_twice) <= 1e-6 or self._is_self_intersecting(vertices):
                return False
        return True

    def _finish_column_interaction(self):
        if not self._column_drag:
            return False
        drag = self._column_drag
        self._column_drag = None
        self.unsetCursor()
        self.magnifier_label.hide()

        if drag["kind"] == "whole_pending":
            plot_index = drag["plot"]
            self.set_selected_column_plot(plot_index, notify=True)
            return True

        self._emit_column_changed(final=True)
        return True

    def _clamp_scene_point(self, x, y):
        return (
            max(0.0, min(float(self.full_w - 1), float(x))),
            max(0.0, min(float(self.full_h - 1), float(y))),
        )

    def _update_column_hover(self, view_pos: QPoint):
        hover = None
        handle = self._column_handle_at(view_pos)
        if handle is not None:
            hover = ("handle", handle[0], handle[1])
            self.setCursor(Qt.SizeAllCursor)
        else:
            divider_index = self._column_divider_at(view_pos)
            if divider_index >= 0:
                hover = ("divider", divider_index)
                self.setCursor(Qt.SizeAllCursor)
            elif self._column_plot_at(self.mapToScene(view_pos)) >= 0:
                self.setCursor(Qt.OpenHandCursor)
            else:
                self.unsetCursor()

        if hover != self._column_hover:
            self._column_hover = hover
            self._refresh_column_editor()

    def _show_magnifier(self, view_pos: QPoint):
        if self.viewport().width() <= 0 or self.viewport().height() <= 0:
            return
        sample_size = 72
        half = sample_size // 2
        sample_rect = QRect(
            view_pos.x() - half,
            view_pos.y() - half,
            sample_size,
            sample_size,
        ).intersected(self.viewport().rect())
        if sample_rect.isEmpty():
            return

        self.magnifier_label.hide()
        pixmap = self.viewport().grab(sample_rect)
        enlarged = pixmap.scaled(
            160,
            160,
            Qt.KeepAspectRatio,
            Qt.FastTransformation,
        )
        self.magnifier_label.setPixmap(enlarged)
        x = max(4, self.viewport().width() - self.magnifier_label.width() - 8)
        self.magnifier_label.move(x, 8)
        self.magnifier_label.show()
        self.magnifier_label.raise_()

    def wheelEvent(self, event):
        if self.ds is None:
            return

        cursor_pos = event.position().toPoint()
        scene_pos_before = self.mapToScene(cursor_pos)
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        previous_anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.scale(factor, factor)
        scene_pos_after = self.mapToScene(cursor_pos)
        offset = scene_pos_after - scene_pos_before
        self.translate(offset.x(), offset.y())
        self.setTransformationAnchor(previous_anchor)
        self.update_timer.start(180)
        if self.column_edit_active:
            self._refresh_column_editor()
        event.accept()

    def mousePressEvent(self, event):
        if self.ds is None:
            return
        self.setFocus()

        if self.column_edit_active and event.button() == Qt.LeftButton:
            self._begin_column_interaction(event)
            event.accept()
            return

        # Some platforms may not emit the paired release after double click.
        # Clear stale skip state before handling a new left-button click.
        if event.button() == Qt.LeftButton and self._skip_release_add_once:
            self._skip_release_add_once = False

        if event.button() == Qt.LeftButton:
            vertex_index = self._vertex_index_at(event.pos())
            if vertex_index >= 0:
                self._push_polygon_history()
                self._drag_vertex_index = vertex_index
                self._press_button = Qt.NoButton
                self._dragging = False
                self.setCursor(Qt.SizeAllCursor)
                event.accept()
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

        if self.column_edit_active:
            if self._column_drag:
                self._update_column_drag(event)
            elif self._press_button == Qt.RightButton:
                pass
            else:
                self._update_column_hover(event.pos())
                event.accept()
                return

        if self._drag_vertex_index >= 0:
            scene_pos = self.mapToScene(event.pos())
            px = max(0.0, min(float(self.full_w - 1), scene_pos.x()))
            py = max(0.0, min(float(self.full_h - 1), scene_pos.y()))
            self.vertices[self._drag_vertex_index] = (px, py)
            self.markers[self._drag_vertex_index].setPos(px, py)
            self._refresh_polygon_path()
            self._show_magnifier(event.pos())
            event.accept()
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

        if self.column_edit_active and self._column_drag:
            self._finish_column_interaction()
            event.accept()
            return

        if self._drag_vertex_index >= 0:
            self._drag_vertex_index = -1
            self.unsetCursor()
            self.magnifier_label.hide()
            self._notify_polygon_changed()
            event.accept()
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
            if self._skip_release_add_once:
                self._skip_release_add_once = False
                event.accept()
                return
            saved_index = self._saved_polygon_index_at(release_scene)
            if saved_index >= 0 and callable(self.on_saved_polygon_clicked):
                self.on_saved_polygon_clicked(saved_index)
                event.accept()
                return
            self.add_vertex(release_scene.x(), release_scene.y())
            event.accept()
            return

        if btn == Qt.RightButton:
            self.remove_last_vertex()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.ds is None:
            return

        if self.column_edit_active:
            event.accept()
            return

        if event.button() == Qt.LeftButton:
            # Ignore the paired release-add triggered by Qt on double click.
            self._skip_release_add_once = True
            if callable(self.on_polygon_finish_requested):
                self.on_polygon_finish_requested()
            event.accept()
            return

        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Z and event.modifiers() & Qt.ControlModifier:
            if self.column_edit_active:
                self.undo_column_edit()
            else:
                self.undo_polygon_edit()
            event.accept()
            return

        if event.key() == Qt.Key_Escape:
            if self.column_edit_active and callable(self.on_column_cancel_requested):
                self.on_column_cancel_requested()
            elif callable(self.on_edit_cancel_requested):
                self.on_edit_cancel_requested()
            event.accept()
            return

        super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.magnifier_label.isVisible():
            x = max(4, self.viewport().width() - self.magnifier_label.width() - 8)
            self.magnifier_label.move(x, 8)

    def _vertex_index_at(self, view_pos: QPoint) -> int:
        nearest_index = -1
        nearest_distance = float("inf")
        for index, marker in enumerate(self.markers):
            marker_view_pos = self.mapFromScene(marker.pos())
            dx = marker_view_pos.x() - view_pos.x()
            dy = marker_view_pos.y() - view_pos.y()
            distance = math.hypot(dx, dy)
            if distance <= 12.0 and distance < nearest_distance:
                nearest_index = index
                nearest_distance = distance
        return nearest_index

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
            data = self._read_rgba(x, y, w, h, target_w, target_h)
            qimg = QImage(data.data, target_w, target_h, target_w * 4, QImage.Format_RGBA8888)
            self.high_res_item.setPixmap(QPixmap.fromImage(qimg.copy()))
            self.high_res_item.setPos(x, y)
            self.high_res_item.setScale(w / target_w)
            self.high_res_item.setZValue(5)
        except Exception as exc:
            print(f"动态加载失败: {exc}")

    def add_vertex(self, px_x: float, px_y: float):
        if not (0 <= px_x < self.full_w and 0 <= px_y < self.full_h):
            return
        self._push_polygon_history()
        self.vertices.append((px_x, px_y))
        marker = VertexMarker(px_x, px_y)
        self.scene_obj.addItem(marker)
        self.markers.append(marker)
        self._refresh_polygon_path()
        self._notify_polygon_changed()

    def remove_last_vertex(self):
        if not self.vertices:
            return
        self._push_polygon_history()
        self.vertices.pop()
        marker = self.markers.pop()
        self.scene_obj.removeItem(marker)
        self._refresh_polygon_path()
        self._notify_polygon_changed()

    def clear_polygon(self):
        if self.vertices:
            self._push_polygon_history()
        self._replace_polygon_vertices([])

    def _replace_polygon_vertices(self, vertices):
        self._drag_vertex_index = -1
        while self.markers:
            marker = self.markers.pop()
            self.scene_obj.removeItem(marker)
        self.vertices = []
        for px_x, px_y in vertices:
            if not (0 <= px_x < self.full_w and 0 <= px_y < self.full_h):
                continue
            self.vertices.append((float(px_x), float(px_y)))
            marker = VertexMarker(px_x, px_y)
            self.scene_obj.addItem(marker)
            self.markers.append(marker)
        self._refresh_polygon_path()
        self._notify_polygon_changed()

    def _push_polygon_history(self):
        snapshot = [(float(x), float(y)) for x, y in self.vertices]
        if self._polygon_history and self._polygon_history[-1] == snapshot:
            return
        self._polygon_history.append(snapshot)
        if len(self._polygon_history) > 50:
            self._polygon_history.pop(0)

    def undo_polygon_edit(self):
        if not self._polygon_history:
            return False
        vertices = self._polygon_history.pop()
        self._restoring_polygon = True
        try:
            self._replace_polygon_vertices(vertices)
        finally:
            self._restoring_polygon = False
        if callable(self.on_polygon_geometry_changed):
            self.on_polygon_geometry_changed(list(self.vertices))
        return True

    def _refresh_polygon_path(self):
        path = QPainterPath()
        if not self.vertices:
            self.polygon_item.setPath(path)
            return

        draw_vertices = self._normalize_vertices_for_display(self.vertices)
        if not draw_vertices:
            self.polygon_item.setPath(path)
            return

        x0, y0 = draw_vertices[0]
        path.moveTo(x0, y0)
        for x, y in draw_vertices[1:]:
            path.lineTo(x, y)
        if len(draw_vertices) >= 3:
            path.closeSubpath()

        self.polygon_item.setPath(path)

    def _normalize_vertices_for_display(self, vertices: List[Tuple[float, float]]):
        pts = [(float(x), float(y)) for x, y in vertices]
        if len(pts) < 4:
            return pts
        if not self._is_self_intersecting(pts):
            return pts

        cx = sum([p[0] for p in pts]) / len(pts)
        cy = sum([p[1] for p in pts]) / len(pts)
        fixed = sorted(pts, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
        if self._is_self_intersecting(fixed):
            return pts
        return fixed

    def _is_self_intersecting(self, pts: List[Tuple[float, float]]) -> bool:
        n = len(pts)
        if n < 4:
            return False

        for i in range(n):
            a1 = pts[i]
            a2 = pts[(i + 1) % n]
            for j in range(i + 1, n):
                b1 = pts[j]
                b2 = pts[(j + 1) % n]

                if i == j:
                    continue
                if (i + 1) % n == j:
                    continue
                if i == (j + 1) % n:
                    continue

                if self._segments_intersect(a1, a2, b1, b2):
                    return True
        return False

    def _segments_intersect(self, p1, q1, p2, q2):
        def orient(a, b, c):
            v = (b[1] - a[1]) * (c[0] - b[0]) - (b[0] - a[0]) * (c[1] - b[1])
            if abs(v) < 1e-12:
                return 0
            return 1 if v > 0 else 2

        def on_seg(a, b, c):
            return (
                min(a[0], c[0]) - 1e-12 <= b[0] <= max(a[0], c[0]) + 1e-12
                and min(a[1], c[1]) - 1e-12 <= b[1] <= max(a[1], c[1]) + 1e-12
            )

        o1 = orient(p1, q1, p2)
        o2 = orient(p1, q1, q2)
        o3 = orient(p2, q2, p1)
        o4 = orient(p2, q2, q1)

        if o1 != o2 and o3 != o4:
            return True
        if o1 == 0 and on_seg(p1, p2, q1):
            return True
        if o2 == 0 and on_seg(p1, q2, q1):
            return True
        if o3 == 0 and on_seg(p2, p1, q2):
            return True
        if o4 == 0 and on_seg(p2, q1, q2):
            return True
        return False

    def get_polygon_pixels(self):
        return list(self.vertices)

    def set_polygon_pixels(self, vertices: List[Tuple[float, float]]):
        self._polygon_history = []
        self._restoring_polygon = True
        try:
            self._replace_polygon_vertices(vertices)
        finally:
            self._restoring_polygon = False

    def set_saved_polygons(self, polygons_pixels):
        self.saved_polygons_pixels = list(polygons_pixels)
        self._rebuild_saved_overlay()

    def set_selected_saved_polygon(self, polygon_index: int):
        self.selected_saved_polygon_index = int(polygon_index)
        self._update_saved_overlay_styles()

    def _rebuild_saved_overlay(self):
        for item in self.saved_polygon_items:
            self.scene_obj.removeItem(item)
        for item in self.saved_label_items:
            self.scene_obj.removeItem(item)
        self.saved_polygon_items = []
        self.saved_label_items = []
        self.saved_polygon_item_indices = []

        for fallback_index, entry in enumerate(self.saved_polygons_pixels):
            polygon_index = int(entry.get("index", fallback_index))
            name = str(entry.get("name") or "")
            vertices = list(entry.get("pixels") or [])
            if len(vertices) < 3:
                continue

            draw_vertices = self._normalize_vertices_for_display(vertices)
            if len(draw_vertices) < 3:
                continue

            path = QPainterPath()
            x0, y0 = draw_vertices[0]
            path.moveTo(x0, y0)
            for x, y in draw_vertices[1:]:
                path.lineTo(x, y)
            path.closeSubpath()

            poly_item = QGraphicsPathItem(path)
            poly_item.setPen(QPen(QColor(255, 120, 0), 2))
            poly_item.setBrush(QColor(255, 120, 0, 35))
            poly_item.setZValue(18)
            self.scene_obj.addItem(poly_item)
            self.saved_polygon_items.append(poly_item)
            self.saved_polygon_item_indices.append(polygon_index)

            cx, cy = self._polygon_center(draw_vertices)
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

        self._update_saved_overlay_styles()

    def _update_saved_overlay_styles(self):
        selected = self.selected_saved_polygon_index
        has_selection = selected >= 0
        for polygon_index, poly_item, label_item in zip(
            self.saved_polygon_item_indices,
            self.saved_polygon_items,
            self.saved_label_items,
        ):
            is_selected = polygon_index == selected
            if is_selected:
                poly_item.setPen(self._make_cosmetic_pen(QColor(0, 220, 255, 245), 3))
                poly_item.setBrush(QColor(0, 190, 255, 55))
                label_item.setVisible(True)
                label_item.setBrush(QColor(255, 255, 255))
            elif has_selection:
                poly_item.setPen(self._make_cosmetic_pen(QColor(255, 95, 30, 190), 2))
                poly_item.setBrush(QColor(0, 0, 0, 0))
                label_item.setVisible(False)
            else:
                poly_item.setPen(self._make_cosmetic_pen(QColor(255, 120, 0, 220), 2))
                poly_item.setBrush(QColor(255, 120, 0, 35))
                label_item.setVisible(True)
                label_item.setBrush(QColor(20, 20, 20))

    def _saved_polygon_index_at(self, scene_pos: QPointF) -> int:
        for polygon_index, item in reversed(
            list(zip(self.saved_polygon_item_indices, self.saved_polygon_items))
        ):
            if item.path().contains(scene_pos):
                return polygon_index
        return -1

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
        if (
            not self._restoring_polygon
            and callable(self.on_polygon_geometry_changed)
        ):
            self.on_polygon_geometry_changed(list(self.vertices))
