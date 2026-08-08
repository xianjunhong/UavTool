import copy
import os
import sys
import time

from PySide6.QtCore import QThread
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from logic.kmz_export import MissionConfig, export_waypoints_to_kmz
from logic.crop import CropWorker, crop_tif_with_polygon, normalize_polygon_pixels
from logic.height_extraction import HeightExtractionWorker
from logic.plot_grid import (
    column_dividers_from_quadrilateral,
    plots_from_dividers,
)
from logic.polygon_io import load_polygons_from_vector, save_polygons_to_shapefile
from logic.pyramid_builder import PyramidBuildWorker, get_overview_count, get_tif_profile
from logic.registration import RegistrationWorker, load_registration_parameters
from logic.shapefile_merge import ShapefileMergeWorker
from logic.waypoint_logic import format_waypoint
from ui.crop_viewer import CropViewer
from ui.registration_viewer import RegistrationViewer
from ui.viewer import UavViewer


MAX_PIXELS_WITHOUT_OVERVIEW = 120_000_000


def _resolve_window_icon_path() -> str:
    candidates = []

    # Source run: ui/pages.py -> project root/uav_icon.ico
    candidates.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "uav_icon.ico")))

    if getattr(sys, "frozen", False):
        # Frozen run: data files are extracted/collected under _MEIPASS.
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.insert(0, os.path.join(meipass, "uav_icon.ico"))

        # Fallback: icon placed next to executable.
        candidates.append(os.path.join(os.path.dirname(sys.executable), "uav_icon.ico"))

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return ""


def _check_large_image_without_pyramid(tif_path: str) -> str:
    """Return empty string if import is allowed, otherwise return block reason."""
    width, height, ov_count = get_tif_profile(tif_path)
    if ov_count > 0:
        return ""

    if width * height > MAX_PIXELS_WITHOUT_OVERVIEW:
        return (
            "该影像分辨率过大且未构建金字塔，当前页面不允许直接导入，"
            "请先到“金字塔构建”页构建后再导入。\n"
            f"当前尺寸: {width} x {height}"
        )

    return ""


def _setup_rgb_band_combo(combo: QComboBox):
    combo.addItem("自动(推荐)", None)
    combo.addItem("RGB: 1,2,3", (1, 2, 3))
    combo.addItem("MS: 3,2,1", (3, 2, 1))


def _selected_rgb_bands(combo: QComboBox):
    return combo.currentData()


class ColumnPlotDialog(QDialog):
    def __init__(self, default_prefix: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("整列生成小区")
        self.resize(460, 260)

        root = QVBoxLayout(self)
        tip = QLabel(
            "请先在影像上用4个顶点框住一整列小区。程序会沿四边形的长方向"
            "自动切分；若方向不符合预期，可选择“短方向”。"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        form = QFormLayout()

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 500)
        self.count_spin.setValue(10)

        self.prefix_edit = QLineEdit(default_prefix)

        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 999999)
        self.start_spin.setValue(1)

        self.padding_spin = QSpinBox()
        self.padding_spin.setRange(1, 6)
        self.padding_spin.setValue(2)

        self.axis_combo = QComboBox()
        self.axis_combo.addItem("沿长方向（推荐）", "long")
        self.axis_combo.addItem("沿短方向", "short")

        self.start_end_combo = QComboBox()
        self.start_end_combo.addItem("从 A 端开始", "a")
        self.start_end_combo.addItem("从 B 端开始", "b")

        form.addRow("小区数量", self.count_spin)
        form.addRow("名称前缀", self.prefix_edit)
        form.addRow("起始编号", self.start_spin)
        form.addRow("编号位数", self.padding_spin)
        form.addRow("切分方向", self.axis_combo)
        form.addRow("编号起点", self.start_end_combo)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def payload(self):
        return {
            "count": self.count_spin.value(),
            "prefix": self.prefix_edit.text().strip(),
            "start": self.start_spin.value(),
            "padding": self.padding_spin.value(),
            "axis": self.axis_combo.currentData(),
            "start_end": self.start_end_combo.currentData(),
        }


class ColumnRenameDialog(QDialog):
    def __init__(
        self,
        prefix: str,
        start: int,
        padding: int,
        plot_count: int,
        start_end: str,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("整列重新命名")
        self.resize(420, 220)

        root = QVBoxLayout(self)
        direction_text = "A 端" if start_end == "a" else "B 端"
        tip = QLabel(
            f"当前整列共有 {plot_count} 个小区，编号从{direction_text}开始。"
            "修改后会整列统一更新名称和 Shapefile 命名字段。"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        form = QFormLayout()
        self.prefix_edit = QLineEdit(str(prefix))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 999999)
        self.start_spin.setValue(int(start))
        self.padding_spin = QSpinBox()
        self.padding_spin.setRange(1, 6)
        self.padding_spin.setValue(int(padding))
        form.addRow("名称前缀", self.prefix_edit)
        form.addRow("起始编号", self.start_spin)
        form.addRow("编号位数", self.padding_spin)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def payload(self):
        return {
            "prefix": self.prefix_edit.text().strip(),
            "start": self.start_spin.value(),
            "padding": self.padding_spin.value(),
        }


DRONE_EXPORT_PROFILES = {
    "M300": {
        "pitch_range": (-120.0, 45.0),
        "pitch_default": -90.0,
        "gimbal_yaw_supported": True,
        "note": "M300：可设置飞机航向、云台俯仰和云台偏航。",
    },
    "M3T": {
        "pitch_range": (-90.0, 35.0),
        "pitch_default": -90.0,
        "gimbal_yaw_supported": False,
        "note": (
            "M3T：云台水平偏航不可独立控制。需要改变相机水平方向时，"
            "请选择“固定航向”并设置飞机航向角。"
        ),
    },
}


class ExportKmzDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出航线参数")
        self.resize(620, 560)
        self._current_profile = None

        root = QVBoxLayout(self)

        instruction = QLabel("请先选择无人机型号，系统再开放该机型支持的航线参数。")
        instruction.setWordWrap(True)
        root.addWidget(instruction)

        model_group = QGroupBox("1. 选择机型")
        model_form = QFormLayout(model_group)
        self.drone_combo = QComboBox()
        self.drone_combo.addItem("请选择无人机型号", None)
        self.drone_combo.addItem("M300", "M300")
        self.drone_combo.addItem("M3T", "M3T")
        model_form.addRow("无人机型号", self.drone_combo)
        root.addWidget(model_group)

        self.profile_note = QLabel("尚未选择机型")
        self.profile_note.setWordWrap(True)
        self.profile_note.setStyleSheet(
            "QLabel { padding: 7px; border: 1px solid #c7cfdb; "
            "border-radius: 4px; background: #f5f7fa; color: #475467; }"
        )
        root.addWidget(self.profile_note)

        self.parameter_group = QGroupBox("2. 设置该机型参数")
        self.parameter_form = QFormLayout(self.parameter_group)

        self.takeoff_spin = QDoubleSpinBox()
        self.takeoff_spin.setRange(1.2, 1500.0)
        self.takeoff_spin.setValue(20.0)
        self.takeoff_spin.setSuffix(" m")

        self.rth_height_spin = QDoubleSpinBox()
        self.rth_height_spin.setRange(2.0, 1500.0)
        self.rth_height_spin.setValue(50.0)
        self.rth_height_spin.setSuffix(" m")

        self.trans_speed_spin = QDoubleSpinBox()
        self.trans_speed_spin.setRange(0.0, 15.0)
        self.trans_speed_spin.setValue(15.0)
        self.trans_speed_spin.setSuffix(" m/s")

        self.auto_speed_spin = QDoubleSpinBox()
        self.auto_speed_spin.setRange(1.0, 15.0)
        self.auto_speed_spin.setValue(5.0)
        self.auto_speed_spin.setSuffix(" m/s")

        self.execute_height_spin = QDoubleSpinBox()
        self.execute_height_spin.setRange(0.5, 1500.0)
        self.execute_height_spin.setValue(3.0)
        self.execute_height_spin.setSuffix(" m")

        self.heading_mode_combo = QComboBox()
        self.heading_mode_combo.addItem("跟随航线", "followWayline")
        self.heading_mode_combo.addItem("固定航向", "fixed")

        self.heading_angle_spin = QDoubleSpinBox()
        self.heading_angle_spin.setRange(-180.0, 180.0)
        self.heading_angle_spin.setValue(0.0)
        self.heading_angle_spin.setSuffix("°")

        self.pitch_check = QCheckBox("设置")
        self.pitch_spin = QDoubleSpinBox()
        self.pitch_spin.setSuffix("°")
        self.pitch_row = QWidget()
        pitch_layout = QHBoxLayout(self.pitch_row)
        pitch_layout.setContentsMargins(0, 0, 0, 0)
        pitch_layout.addWidget(self.pitch_check)
        pitch_layout.addWidget(self.pitch_spin, 1)

        self.yaw_check = QCheckBox("设置")
        self.yaw_spin = QDoubleSpinBox()
        self.yaw_spin.setRange(-180.0, 180.0)
        self.yaw_spin.setValue(0.0)
        self.yaw_spin.setSuffix("°")
        self.yaw_row = QWidget()
        yaw_layout = QHBoxLayout(self.yaw_row)
        yaw_layout.setContentsMargins(0, 0, 0, 0)
        yaw_layout.addWidget(self.yaw_check)
        yaw_layout.addWidget(self.yaw_spin, 1)

        self.image_format_combo = QComboBox()
        self.image_format_combo.addItem("广角 + 红外", "wide,ir")
        self.image_format_combo.addItem("仅广角", "wide")
        self.image_format_combo.addItem("仅红外", "ir")
        self.image_format_combo.addItem("仅长焦", "zoom")
        self.image_format_combo.addItem("广角 + 长焦 + 红外", "wide,zoom,ir")

        self.parameter_form.addRow("起飞安全高度", self.takeoff_spin)
        self.parameter_form.addRow("返航高度", self.rth_height_spin)
        self.parameter_form.addRow("首段过渡速度", self.trans_speed_spin)
        self.parameter_form.addRow("自动飞行速度", self.auto_speed_spin)
        self.parameter_form.addRow("执行高度", self.execute_height_spin)
        self.parameter_form.addRow("飞机航向模式", self.heading_mode_combo)
        self.parameter_form.addRow("飞机航向角", self.heading_angle_spin)
        self.parameter_form.addRow("云台俯仰角", self.pitch_row)
        self.parameter_form.addRow("云台偏航角", self.yaw_row)
        self.parameter_form.addRow("M3T 拍照镜头", self.image_format_combo)
        root.addWidget(self.parameter_group)

        output_group = QGroupBox("3. 输出文件")
        output_layout = QHBoxLayout(output_group)
        default_name = f"route_{time.strftime('%Y%m%d_%H%M%S')}.kmz"
        default_path = os.path.join(os.getcwd(), default_name)
        self.output_edit = QLineEdit(default_path)
        browse_btn = QPushButton("选择...")
        browse_btn.clicked.connect(self.choose_output)
        output_layout.addWidget(self.output_edit, 1)
        output_layout.addWidget(browse_btn)
        root.addWidget(output_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        root.addWidget(buttons)

        self.drone_combo.currentIndexChanged.connect(self._apply_selected_drone)
        self.heading_mode_combo.currentIndexChanged.connect(self._sync_heading_angle)
        self.pitch_check.toggled.connect(self._sync_optional_controls)
        self.yaw_check.toggled.connect(self._sync_optional_controls)
        self._apply_selected_drone()

    def _apply_selected_drone(self):
        drone_type = self.drone_combo.currentData()
        self._current_profile = DRONE_EXPORT_PROFILES.get(drone_type)
        selected = self._current_profile is not None
        self.parameter_group.setEnabled(selected)
        self.ok_button.setEnabled(selected)

        if not selected:
            self.profile_note.setText("尚未选择机型")
            self.parameter_form.setRowVisible(self.yaw_row, False)
            self.parameter_form.setRowVisible(self.image_format_combo, False)
            self.heading_angle_spin.setEnabled(False)
            return

        pitch_minimum, pitch_maximum = self._current_profile["pitch_range"]
        self.pitch_spin.setRange(pitch_minimum, pitch_maximum)
        self.pitch_spin.setValue(self._current_profile["pitch_default"])
        self.pitch_check.setChecked(True)

        yaw_supported = self._current_profile["gimbal_yaw_supported"]
        self.parameter_form.setRowVisible(self.yaw_row, yaw_supported)
        self.yaw_check.setChecked(False)
        self.yaw_spin.setValue(0.0)

        is_m3t = drone_type == "M3T"
        self.parameter_form.setRowVisible(self.image_format_combo, is_m3t)
        self.image_format_combo.setCurrentIndex(0)

        self.heading_mode_combo.setCurrentIndex(0)
        self.heading_angle_spin.setValue(0.0)
        self.profile_note.setText(self._current_profile["note"])
        self._sync_heading_angle()
        self._sync_optional_controls()

    def _sync_heading_angle(self):
        enabled = (
            self._current_profile is not None
            and self.heading_mode_combo.currentData() == "fixed"
        )
        self.heading_angle_spin.setEnabled(enabled)

    def _sync_optional_controls(self):
        selected = self._current_profile is not None
        self.pitch_spin.setEnabled(selected and self.pitch_check.isChecked())
        yaw_supported = bool(
            self._current_profile
            and self._current_profile["gimbal_yaw_supported"]
        )
        self.yaw_spin.setEnabled(
            selected and yaw_supported and self.yaw_check.isChecked()
        )

    def choose_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出 KMZ",
            self.output_edit.text(),
            "KMZ 文件 (*.kmz)",
        )
        if path:
            if not path.lower().endswith(".kmz"):
                path += ".kmz"
            self.output_edit.setText(path)

    def payload(self):
        drone_type = self.drone_combo.currentData()
        if drone_type not in DRONE_EXPORT_PROFILES:
            raise ValueError("请先选择无人机型号")

        heading_mode = self.heading_mode_combo.currentData()
        config = MissionConfig(
            drone_type=drone_type,
            takeoff_security_height=self.takeoff_spin.value(),
            global_rth_height=self.rth_height_spin.value(),
            global_transitional_speed=self.trans_speed_spin.value(),
            auto_flight_speed=self.auto_speed_spin.value(),
            execute_height=self.execute_height_spin.value(),
            waypoint_heading_mode=heading_mode,
            waypoint_heading_angle=(
                self.heading_angle_spin.value()
                if heading_mode == "fixed"
                else None
            ),
            image_format=(
                self.image_format_combo.currentData()
                if drone_type == "M3T"
                else None
            ),
        )
        return {
            "config": config,
            "pitch": self.pitch_spin.value() if self.pitch_check.isChecked() else None,
            "yaw": (
                self.yaw_spin.value()
                if (
                    self._current_profile["gimbal_yaw_supported"]
                    and self.yaw_check.isChecked()
                )
                else None
            ),
            "output_path": self.output_edit.text().strip(),
        }


class DrawRoutePage(QWidget):
    def __init__(self):
        super().__init__()

        self.viewer = UavViewer()
        self.coord_list = QListWidget()
        self.import_btn = QPushButton("导入 TIF")
        self.export_btn = QPushButton("导出航线")
        self.rgb_combo = QComboBox()
        _setup_rgb_band_combo(self.rgb_combo)
        self.rotation_spin = QDoubleSpinBox()
        self.rotation_spin.setRange(-180.0, 180.0)
        self.rotation_spin.setValue(0.0)
        self.rotation_spin.setSingleStep(1.0)
        self.rotation_spin.setSuffix("°")

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        head.addWidget(self.import_btn)
        head.addWidget(self.export_btn)
        head.addWidget(QLabel("显示波段"))
        head.addWidget(self.rgb_combo)
        head.addWidget(QLabel("旋转"))
        head.addWidget(self.rotation_spin)
        head.addStretch(1)
        root.addLayout(head)

        body = QHBoxLayout()
        body.addWidget(self.viewer, 8)

        right_panel = QVBoxLayout()
        right_panel.addWidget(QLabel("航点经纬度"))
        right_panel.addWidget(self.coord_list)

        right = QWidget()
        right.setLayout(right_panel)
        body.addWidget(right, 2)

        root.addLayout(body)

    def _bind_events(self):
        self.import_btn.clicked.connect(self.handle_import)
        self.export_btn.clicked.connect(self.handle_export)
        self.rgb_combo.currentIndexChanged.connect(
            lambda _: self.viewer.set_display_rgb_bands(_selected_rgb_bands(self.rgb_combo))
        )
        self.rotation_spin.valueChanged.connect(self.viewer.set_display_rotation)
        self.coord_list.itemDoubleClicked.connect(self.on_list_item_double_clicked)
        self.viewer.on_waypoint_added = self.on_waypoint_added
        self.viewer.on_waypoint_removed = self.on_waypoint_removed
        self.viewer.on_waypoints_reindexed = self.on_waypoints_reindexed

    def clear_state(self):
        self.viewer.unload_image()
        self.coord_list.clear()
        self.rotation_spin.setValue(0.0)
        self.rgb_combo.setCurrentIndex(0)

    def handle_import(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择 TIF 影像", "", "GeoTIFF (*.tif *.tiff)")
        if not file_path:
            return

        try:
            block_reason = _check_large_image_without_pyramid(file_path)
            if block_reason:
                QMessageBox.warning(self, "导入失败", block_reason)
                return
            self.viewer.set_display_rgb_bands(_selected_rgb_bands(self.rgb_combo))
            self.viewer.load_tif(file_path)
            self.coord_list.clear()
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))

    def on_waypoint_added(self, index: int, lon: float, lat: float):
        self._append_coord_row(index, lon, lat)

    def on_waypoint_removed(self, remove_idx: int):
        item = self.coord_list.takeItem(remove_idx)
        del item

    def on_waypoints_reindexed(self, pairs):
        self.coord_list.clear()
        for index, lon, lat in pairs:
            self._append_coord_row(index, lon, lat)

    def on_list_item_double_clicked(self, item):
        row = self.coord_list.row(item)
        self.viewer.focus_waypoint(row)

    def handle_export(self):
        points = self.viewer.get_lon_lat_points()
        if not points:
            QMessageBox.warning(self, "导出失败", "当前没有航点可导出")
            return

        dialog = ExportKmzDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return

        payload = dialog.payload()
        output_path = payload["output_path"]
        if not output_path:
            QMessageBox.warning(self, "导出失败", "请先选择导出文件路径")
            return

        try:
            export_waypoints_to_kmz(
                points,
                output_path,
                payload["config"],
                pitch=payload["pitch"],
                yaw=payload["yaw"],
            )
            QMessageBox.information(self, "导出成功", f"已生成 KMZ:\n{output_path}")
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    def _append_coord_row(self, index: int, lon: float, lat: float):
        item = QListWidgetItem()
        text_label = QLabel(format_waypoint(index, lon, lat))
        delete_btn = QPushButton("x")
        delete_btn.setFixedWidth(26)
        delete_btn.setFixedHeight(22)

        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(6, 2, 6, 2)
        row_layout.setSpacing(8)
        row_layout.addWidget(text_label)
        row_layout.addStretch(1)
        row_layout.addWidget(delete_btn)

        item.setSizeHint(row_widget.sizeHint())
        self.coord_list.addItem(item)
        self.coord_list.setItemWidget(item, row_widget)

        delete_btn.clicked.connect(lambda _, it=item: self._delete_row_item(it))

    def _delete_row_item(self, item: QListWidgetItem):
        row = self.coord_list.row(item)
        if row < 0:
            return
        self.viewer.remove_waypoint_by_index(row)


class PyramidBuildPage(QWidget):
    def __init__(self):
        super().__init__()

        self.file_edit = QLineEdit()
        self.file_edit.setPlaceholderText("请选择一个 TIF 文件")
        self.browse_btn = QPushButton("选择 TIF")
        self.build_btn = QPushButton("构建金字塔")
        self.status_label = QLabel("等待选择文件")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._has_overview = False

        self._worker_thread = None
        self._worker = None

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QVBoxLayout(self)

        file_row = QHBoxLayout()
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(self.browse_btn)

        action_row = QHBoxLayout()
        action_row.addWidget(self.build_btn)
        action_row.addStretch(1)

        root.addLayout(file_row)
        root.addLayout(action_row)
        root.addWidget(self.status_label)
        root.addWidget(self.progress_bar)
        root.addStretch(1)

    def _bind_events(self):
        self.browse_btn.clicked.connect(self.choose_file)
        self.build_btn.clicked.connect(self.start_build)

    def clear_state(self):
        self.file_edit.clear()
        self.status_label.setText("等待选择文件")
        self.progress_bar.setValue(0)
        self.build_btn.setEnabled(True)
        self._has_overview = False

    def choose_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择 TIF 影像", "", "GeoTIFF (*.tif *.tiff)")
        if not file_path:
            return

        self.file_edit.setText(file_path)
        try:
            ov_count = get_overview_count(file_path)
            if ov_count > 0:
                self._has_overview = True
                self.status_label.setText(f"当前文件已包含金字塔，层级数: {ov_count}")
                self.build_btn.setEnabled(False)
            else:
                self._has_overview = False
                self.status_label.setText("当前文件没有金字塔，可执行构建")
                self.build_btn.setEnabled(True)
        except Exception as exc:
            self._has_overview = False
            self.build_btn.setEnabled(False)
            self.status_label.setText("文件检测失败")
            QMessageBox.warning(self, "检测失败", str(exc))

    def start_build(self):
        tif_path = self.file_edit.text().strip()
        if not tif_path:
            QMessageBox.warning(self, "提示", "请先选择 TIF 文件")
            return

        try:
            ov_count = get_overview_count(tif_path)
            if ov_count > 0:
                self._has_overview = True
                self.status_label.setText(f"当前文件已包含金字塔，层级数: {ov_count}")
                self.build_btn.setEnabled(False)
                QMessageBox.information(self, "提示", "该影像已包含金字塔，无需重复构建")
                return
        except Exception as exc:
            QMessageBox.warning(self, "检测失败", str(exc))
            return

        self.progress_bar.setValue(0)
        self.status_label.setText("准备构建...")
        self.build_btn.setEnabled(False)

        self._worker_thread = QThread(self)
        self._worker = PyramidBuildWorker(tif_path)
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.on_progress)
        self._worker.finished.connect(self.on_finished)
        self._worker.failed.connect(self.on_failed)

        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def on_progress(self, percent: int, message: str):
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)

    def on_finished(self, message: str):
        self.progress_bar.setValue(100)
        self.status_label.setText(message)
        self._has_overview = True
        self.build_btn.setEnabled(False)
        QMessageBox.information(self, "完成", message)

    def on_failed(self, error_message: str):
        self.status_label.setText("构建失败")
        self.build_btn.setEnabled(True)
        QMessageBox.warning(self, "构建失败", error_message)


class RegistrationPage(QWidget):
    def __init__(self):
        super().__init__()

        self.src_path_edit = QLineEdit()
        self.src_path_edit.setReadOnly(True)
        self.target_path_edit = QLineEdit()
        self.target_path_edit.setReadOnly(True)
        self.src_rotation_spin = QDoubleSpinBox()
        self.src_rotation_spin.setRange(-180.0, 180.0)
        self.src_rotation_spin.setValue(0.0)
        self.src_rotation_spin.setSingleStep(1.0)
        self.src_rotation_spin.setSuffix("°")
        self.src_rgb_combo = QComboBox()
        _setup_rgb_band_combo(self.src_rgb_combo)
        self.target_rotation_spin = QDoubleSpinBox()
        self.target_rotation_spin.setRange(-180.0, 180.0)
        self.target_rotation_spin.setValue(0.0)
        self.target_rotation_spin.setSingleStep(1.0)
        self.target_rotation_spin.setSuffix("°")
        self.target_rgb_combo = QComboBox()
        _setup_rgb_band_combo(self.target_rgb_combo)

        self.btn_choose_src = QPushButton("选择 src 图像 / 参数 CSV")
        self.btn_choose_target = QPushButton("选择 target 图像")
        self.btn_clear_src = QPushButton("清空 src 点")
        self.btn_clear_target = QPushButton("清空 target 点")
        self.btn_align = QPushButton("图像对齐")
        self.export_params_check = QCheckBox("同时导出参数 CSV")
        self.export_params_check.setChecked(True)
        self.export_params_check.setToolTip(
            "正常控制点配准完成后，在输出 TIF 旁保存可复用于 DEM 的参数文件"
        )

        self.src_count_label = QLabel("src 点: 0")
        self.target_count_label = QLabel("target 点: 0")
        self.status_label = QLabel("请选择 src 与 target 图像，然后按顺序选点")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.src_viewer = RegistrationViewer(QColor(255, 0, 0))
        self.target_viewer = RegistrationViewer(QColor(0, 170, 255))

        self.src_tif_path = ""
        self.target_tif_path = ""
        self.registration_parameter_path = ""
        self._worker_thread = None
        self._worker = None

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QVBoxLayout(self)

        top_src = QHBoxLayout()
        top_src.addWidget(self.btn_choose_src)
        top_src.addWidget(self.src_path_edit, 1)
        top_src.addWidget(QLabel("波段"))
        top_src.addWidget(self.src_rgb_combo)
        top_src.addWidget(QLabel("旋转"))
        top_src.addWidget(self.src_rotation_spin)

        top_tgt = QHBoxLayout()
        top_tgt.addWidget(self.btn_choose_target)
        top_tgt.addWidget(self.target_path_edit, 1)
        top_tgt.addWidget(QLabel("波段"))
        top_tgt.addWidget(self.target_rgb_combo)
        top_tgt.addWidget(QLabel("旋转"))
        top_tgt.addWidget(self.target_rotation_spin)

        actions = QHBoxLayout()
        actions.addWidget(self.btn_clear_src)
        actions.addWidget(self.btn_clear_target)
        actions.addWidget(self.btn_align)
        actions.addWidget(self.export_params_check)
        actions.addStretch(1)
        actions.addWidget(self.src_count_label)
        actions.addWidget(self.target_count_label)

        viewers = QHBoxLayout()
        src_box = QGroupBox("src 图像")
        src_layout = QVBoxLayout(src_box)
        src_layout.addWidget(self.src_viewer)

        tgt_box = QGroupBox("target 图像")
        tgt_layout = QVBoxLayout(tgt_box)
        tgt_layout.addWidget(self.target_viewer)

        viewers.addWidget(src_box, 1)
        viewers.addWidget(tgt_box, 1)

        root.addLayout(top_src)
        root.addLayout(top_tgt)
        root.addLayout(actions)
        root.addWidget(self.status_label)
        root.addWidget(self.progress_bar)
        root.addLayout(viewers)

    def _bind_events(self):
        self.btn_choose_src.clicked.connect(self.choose_src)
        self.btn_choose_target.clicked.connect(self.choose_target)
        self.btn_clear_src.clicked.connect(self.src_viewer.clear_points)
        self.btn_clear_target.clicked.connect(self.target_viewer.clear_points)
        self.btn_align.clicked.connect(self.align_images)
        self.src_rgb_combo.currentIndexChanged.connect(
            lambda _: self.src_viewer.set_display_rgb_bands(_selected_rgb_bands(self.src_rgb_combo))
        )
        self.target_rgb_combo.currentIndexChanged.connect(
            lambda _: self.target_viewer.set_display_rgb_bands(_selected_rgb_bands(self.target_rgb_combo))
        )
        self.src_rotation_spin.valueChanged.connect(self.src_viewer.set_display_rotation)
        self.target_rotation_spin.valueChanged.connect(self.target_viewer.set_display_rotation)

        self.src_viewer.on_points_changed = self.on_src_points_changed
        self.target_viewer.on_points_changed = self.on_target_points_changed

    def clear_state(self):
        self.src_viewer.unload_image()
        self.target_viewer.unload_image()
        self.src_tif_path = ""
        self.target_tif_path = ""
        self.registration_parameter_path = ""
        self.src_path_edit.clear()
        self.target_path_edit.clear()
        self.src_count_label.setText("src 点: 0")
        self.target_count_label.setText("target 点: 0")
        self.status_label.setText("请选择 src 与 target 图像，然后按顺序选点")
        self.progress_bar.setValue(0)
        self._update_registration_mode_controls()

    def _update_registration_mode_controls(self):
        using_parameters = bool(self.registration_parameter_path)
        self.btn_clear_src.setEnabled(not using_parameters)
        self.btn_clear_target.setEnabled(not using_parameters)
        self.src_rgb_combo.setEnabled(not using_parameters)
        self.src_rotation_spin.setEnabled(not using_parameters)
        self.target_rgb_combo.setEnabled(not using_parameters)
        self.target_rotation_spin.setEnabled(not using_parameters)
        self.export_params_check.setEnabled(not using_parameters)
        self.btn_align.setText("应用参数并对齐" if using_parameters else "图像对齐")

    def choose_src(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 src 图像或对齐参数",
            "",
            "支持的文件 (*.tif *.tiff *.csv);;GeoTIFF (*.tif *.tiff);;对齐参数 CSV (*.csv)",
        )
        if not path:
            return

        if path.lower().endswith(".csv"):
            try:
                parameters = load_registration_parameters(path)
            except Exception as exc:
                QMessageBox.warning(self, "参数加载失败", str(exc))
                return

            self.src_viewer.unload_image()
            self.target_viewer.unload_image()
            self.src_tif_path = ""
            self.registration_parameter_path = path
            self.src_path_edit.setText(path)
            point_count = parameters.get("point_count")
            self.src_count_label.setText(
                f"参数点对: {point_count}" if point_count is not None else "已加载参数"
            )
            self.target_count_label.setText("target: 无需选点")
            original_target = os.path.basename(str(parameters.get("target_image") or ""))
            source_note = f"（原 target: {original_target}）" if original_target else ""
            self.status_label.setText(
                f"已进入参数复用模式{source_note}，请选择同批次的 target/DEM 图像"
            )
            self._update_registration_mode_controls()
            return

        was_using_parameters = bool(self.registration_parameter_path)
        try:
            block_reason = _check_large_image_without_pyramid(path)
            if block_reason:
                QMessageBox.warning(self, "加载失败", block_reason)
                return
            self.src_viewer.set_display_rgb_bands(_selected_rgb_bands(self.src_rgb_combo))
            self.src_viewer.load_tif(path)
            self.src_tif_path = path
            self.registration_parameter_path = ""
            self.src_path_edit.setText(path)
            self.src_count_label.setText("src 点: 0")
            if was_using_parameters and self.target_tif_path:
                self.target_viewer.unload_image()
                self.target_tif_path = ""
                self.target_path_edit.clear()
                self.target_count_label.setText("target 点: 0")
            self.status_label.setText("已加载 src 图像，请选择 target 并按顺序选点")
            self._update_registration_mode_controls()
        except Exception as exc:
            QMessageBox.warning(self, "加载失败", str(exc))

    def choose_target(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 target 图像", "", "GeoTIFF (*.tif *.tiff)")
        if not path:
            return

        if self.registration_parameter_path:
            try:
                get_tif_profile(path)
            except Exception as exc:
                QMessageBox.warning(self, "加载失败", str(exc))
                return
            self.target_viewer.unload_image()
            self.target_tif_path = path
            self.target_path_edit.setText(path)
            self.target_count_label.setText("target: 无需选点")
            self.status_label.setText("target/DEM 已选择，可以直接应用参数对齐")
            return

        try:
            block_reason = _check_large_image_without_pyramid(path)
            if block_reason:
                QMessageBox.warning(self, "加载失败", block_reason)
                return
            self.target_viewer.set_display_rgb_bands(_selected_rgb_bands(self.target_rgb_combo))
            self.target_viewer.load_tif(path)
            self.target_tif_path = path
            self.target_path_edit.setText(path)
            self.target_count_label.setText("target 点: 0")
        except Exception as exc:
            QMessageBox.warning(self, "加载失败", str(exc))

    def on_src_points_changed(self, count: int):
        self.src_count_label.setText(f"src 点: {count}")

    def on_target_points_changed(self, count: int):
        self.target_count_label.setText(f"target 点: {count}")

    def align_images(self):
        using_parameters = bool(self.registration_parameter_path)
        if not self.target_tif_path:
            QMessageBox.warning(self, "提示", "请先选择 target 图像")
            return
        if not using_parameters and not self.src_tif_path:
            QMessageBox.warning(self, "提示", "请先选择 src 和 target 图像")
            return

        src_points = [] if using_parameters else self.src_viewer.get_points()
        target_points = [] if using_parameters else self.target_viewer.get_points()

        if not using_parameters and len(src_points) != len(target_points):
            QMessageBox.warning(self, "提示", "src 与 target 点数不一致，请按顺序配对")
            return

        if not using_parameters and len(src_points) < 3:
            QMessageBox.warning(self, "提示", "至少需要 3 对点，建议 4 对及以上")
            return

        default_output = os.path.join(
            os.path.dirname(self.target_tif_path),
            os.path.splitext(os.path.basename(self.target_tif_path))[0] + "_aligned.tif",
        )
        output_title = "保存参数对齐后的图像" if using_parameters else "保存配准后图像"
        output_path, _ = QFileDialog.getSaveFileName(
            self,
            output_title,
            default_output,
            "GeoTIFF (*.tif *.tiff)",
        )
        if not output_path:
            return
        exported_parameter_path = ""
        if not using_parameters and self.export_params_check.isChecked():
            exported_parameter_path = os.path.splitext(output_path)[0] + "_params.csv"

        self.progress_bar.setValue(0)
        self.status_label.setText("开始应用对齐参数..." if using_parameters else "开始图像对齐...")
        self.btn_align.setEnabled(False)
        self.btn_choose_src.setEnabled(False)
        self.btn_choose_target.setEnabled(False)
        self.export_params_check.setEnabled(False)

        self._worker_thread = QThread(self)
        self._worker = RegistrationWorker(
            self.src_tif_path,
            self.target_tif_path,
            src_points,
            target_points,
            output_path,
            exported_parameter_csv_path=exported_parameter_path,
            imported_parameter_csv_path=self.registration_parameter_path,
        )
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.on_align_progress)
        self._worker.finished.connect(self.on_align_finished)
        self._worker.failed.connect(self.on_align_failed)

        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def on_align_progress(self, percent: int, message: str):
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)

    def on_align_finished(self, result):
        self.progress_bar.setValue(100)
        self.btn_align.setEnabled(True)
        self.btn_choose_src.setEnabled(True)
        self.btn_choose_target.setEnabled(True)
        self._update_registration_mode_controls()

        lines = [f"输出文件: {result['output_path']}"]
        if result.get("mode") == "parameters":
            lines.append(f"使用参数: {result['parameter_path']}")
        elif result.get("parameter_path"):
            lines.append(f"参数 CSV: {result['parameter_path']}")
        if result.get("point_count") is not None:
            point_label = "原配准点对数" if result.get("mode") == "parameters" else "点对数"
            lines.append(f"{point_label}: {result['point_count']}")
        if result.get("rmse") is not None:
            lines.append(f"训练RMSE(像素): {result['rmse']:.6f}")
        if result.get("max_error") is not None:
            lines.append(f"最大误差(像素): {result['max_error']:.6f}")
        if result.get("determinant") is not None:
            lines.append(f"det(A): {result['determinant']:.6e}")
        if result.get("condition_number") is not None:
            lines.append(f"cond(A): {result['condition_number']:.3e}")
        if result.get("loo_rmse") is not None:
            lines.append(f"留一RMSE(像素): {result['loo_rmse']:.6f}")
        msg = "\n".join(lines)
        if result.get("rmse_note"):
            msg += f"\n\n说明: {result['rmse_note']}"
        self.status_label.setText(
            "参数复用对齐完成" if result.get("mode") == "parameters" else "配准完成"
        )
        QMessageBox.information(self, "图像对齐完成", msg)

    def on_align_failed(self, error_message: str):
        self.btn_align.setEnabled(True)
        self.btn_choose_src.setEnabled(True)
        self.btn_choose_target.setEnabled(True)
        self._update_registration_mode_controls()
        self.status_label.setText("配准失败")
        QMessageBox.warning(self, "图像对齐失败", error_message)


class ImageCropPage(QWidget):
    def __init__(self):
        super().__init__()

        self.tif_path_edit = QLineEdit()
        self.tif_path_edit.setReadOnly(True)
        self.output_path_edit = QLineEdit()
        self.output_path_edit.setReadOnly(True)

        self.btn_choose_tif = QPushButton("选择 TIF")
        self.btn_choose_output = QPushButton("选择输出")
        self.btn_clear_polygon = QPushButton("清空多边形")
        self.btn_crop = QPushButton("执行裁剪")
        self.overwrite_check = QCheckBox("在原图上裁剪（覆盖原文件）")
        self.rgb_combo = QComboBox()
        _setup_rgb_band_combo(self.rgb_combo)
        self.rotation_spin = QDoubleSpinBox()
        self.rotation_spin.setRange(-180.0, 180.0)
        self.rotation_spin.setValue(0.0)
        self.rotation_spin.setSingleStep(1.0)
        self.rotation_spin.setSuffix("°")

        self.vertex_count_label = QLabel("顶点数: 0")
        self.status_label = QLabel("左键绘制多边形顶点，右键撤销最后一个点")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.viewer = CropViewer()

        self.tif_path = ""
        self._worker_thread = None
        self._worker = None

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QVBoxLayout(self)

        row1 = QHBoxLayout()
        row1.addWidget(self.btn_choose_tif)
        row1.addWidget(self.tif_path_edit, 1)
        row1.addWidget(QLabel("波段"))
        row1.addWidget(self.rgb_combo)
        row1.addWidget(QLabel("旋转"))
        row1.addWidget(self.rotation_spin)

        row2 = QHBoxLayout()
        row2.addWidget(self.btn_choose_output)
        row2.addWidget(self.output_path_edit, 1)

        row3 = QHBoxLayout()
        row3.addWidget(self.overwrite_check)
        row3.addWidget(self.btn_clear_polygon)
        row3.addWidget(self.btn_crop)
        row3.addStretch(1)
        row3.addWidget(self.vertex_count_label)

        root.addLayout(row1)
        root.addLayout(row2)
        root.addLayout(row3)
        root.addWidget(self.status_label)
        root.addWidget(self.progress_bar)
        root.addWidget(self.viewer)

    def _bind_events(self):
        self.btn_choose_tif.clicked.connect(self.choose_tif)
        self.btn_choose_output.clicked.connect(self.choose_output)
        self.btn_clear_polygon.clicked.connect(self.viewer.clear_polygon)
        self.btn_crop.clicked.connect(self.start_crop)
        self.overwrite_check.toggled.connect(self.on_overwrite_toggled)
        self.rgb_combo.currentIndexChanged.connect(
            lambda _: self.viewer.set_display_rgb_bands(_selected_rgb_bands(self.rgb_combo))
        )
        self.rotation_spin.valueChanged.connect(self.viewer.set_display_rotation)
        self.viewer.on_polygon_changed = self.on_polygon_changed

    def clear_state(self):
        self.viewer.unload_image()
        self.tif_path = ""
        self.tif_path_edit.clear()
        self.output_path_edit.clear()
        self.status_label.setText("左键绘制多边形顶点，右键撤销最后一个点")
        self.progress_bar.setValue(0)
        self.vertex_count_label.setText("顶点数: 0")
        self.overwrite_check.setChecked(False)
        self.rotation_spin.setValue(0.0)
        self.rgb_combo.setCurrentIndex(0)

    def choose_tif(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择待裁剪影像", "", "GeoTIFF (*.tif *.tiff)")
        if not path:
            return
        try:
            block_reason = _check_large_image_without_pyramid(path)
            if block_reason:
                QMessageBox.warning(self, "加载失败", block_reason)
                return
            self.viewer.set_display_rgb_bands(_selected_rgb_bands(self.rgb_combo))
            self.viewer.load_tif(path)
            self.viewer.set_saved_polygons([])
            self.tif_path = path
            self.tif_path_edit.setText(path)
            if not self.overwrite_check.isChecked():
                default_output = os.path.join(
                    os.path.dirname(path),
                    os.path.splitext(os.path.basename(path))[0] + "_crop.tif",
                )
                self.output_path_edit.setText(default_output)
            self.status_label.setText("影像已加载，开始绘制裁剪多边形")
            self.progress_bar.setValue(0)
        except Exception as exc:
            QMessageBox.warning(self, "加载失败", str(exc))

    def choose_output(self):
        if self.overwrite_check.isChecked():
            return
        default_path = self.output_path_edit.text().strip() or "crop_output.tif"
        path, _ = QFileDialog.getSaveFileName(self, "选择输出裁剪图像", default_path, "GeoTIFF (*.tif *.tiff)")
        if path:
            self.output_path_edit.setText(path)

    def on_overwrite_toggled(self, checked: bool):
        self.btn_choose_output.setEnabled(not checked)
        self.output_path_edit.setEnabled(not checked)
        if checked:
            self.status_label.setText("当前模式: 覆盖原图")
        else:
            self.status_label.setText("当前模式: 输出新图")

    def on_polygon_changed(self, count: int):
        self.vertex_count_label.setText(f"顶点数: {count}")

    def start_crop(self):
        if not self.tif_path:
            QMessageBox.warning(self, "提示", "请先选择待裁剪影像")
            return

        polygon = self.viewer.get_polygon_pixels()
        if len(polygon) < 3:
            QMessageBox.warning(self, "提示", "请至少绘制 3 个顶点")
            return

        overwrite = self.overwrite_check.isChecked()
        output_path = self.output_path_edit.text().strip()
        if not overwrite and not output_path:
            QMessageBox.warning(self, "提示", "请先设置输出路径")
            return

        self.btn_crop.setEnabled(False)
        self.btn_choose_tif.setEnabled(False)
        self.btn_choose_output.setEnabled(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("开始裁剪...")

        self._worker_thread = QThread(self)
        self._worker = CropWorker(
            self.tif_path,
            polygon,
            output_path,
            overwrite,
        )
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.on_crop_progress)
        self._worker.finished.connect(self.on_crop_finished)
        self._worker.failed.connect(self.on_crop_failed)

        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)

        self._worker_thread.start()

    def on_crop_progress(self, percent: int, message: str):
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)

    def on_crop_finished(self, result):
        self.btn_crop.setEnabled(True)
        self.btn_choose_tif.setEnabled(True)
        self.btn_choose_output.setEnabled(not self.overwrite_check.isChecked())
        self.progress_bar.setValue(100)
        self.status_label.setText("裁剪完成")

        if self.overwrite_check.isChecked():
            try:
                self.viewer.load_tif(self.tif_path)
                self.viewer.set_saved_polygons([])
            except Exception:
                pass

        msg = (
            f"输出文件: {result['output_path']}\n"
            f"金字塔层级: {result['overview_count']}"
        )
        QMessageBox.information(self, "裁剪完成", msg)

    def on_crop_failed(self, error_message: str):
        self.btn_crop.setEnabled(True)
        self.btn_choose_tif.setEnabled(True)
        self.btn_choose_output.setEnabled(not self.overwrite_check.isChecked())
        self.status_label.setText("裁剪失败")
        QMessageBox.warning(self, "裁剪失败", error_message)


class PlotCropPage(QWidget):
    def __init__(self):
        super().__init__()

        self.tif_path_edit = QLineEdit()
        self.tif_path_edit.setReadOnly(True)
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setReadOnly(True)

        self.btn_choose_tif = QPushButton("选择 TIF")
        self.btn_choose_output = QPushButton("选择输出文件夹")
        self.btn_clear_polygon = QPushButton("清空多边形")
        self.btn_crop = QPushButton("执行裁剪")
        self.btn_add_plot = QPushButton("添加当前小区")
        self.btn_generate_column = QPushButton("整列生成小区")
        self.btn_apply_plot = QPushButton("编辑选中小区")
        self.btn_update_plot = QPushButton("完成单区编辑")
        self.btn_update_plot.setEnabled(False)
        self.btn_previous_plot = QPushButton("上一个")
        self.btn_next_plot = QPushButton("下一个")
        self.btn_edit_column = QPushButton("编辑所在整列")
        self.btn_finish_column = QPushButton("完成整列编辑")
        self.btn_redistribute_column = QPushButton("重新等分")
        self.btn_add_divider = QPushButton("增加分隔线")
        self.btn_delete_divider = QPushButton("删除分隔线")
        self.btn_reverse_column = QPushButton("反转编号方向")
        self.btn_rename_column = QPushButton("整列重新命名")
        self.btn_rename_plot = QPushButton("重命名")
        self.btn_remove_plot = QPushButton("删除")
        self.btn_save_plots = QPushButton("保存小区库")
        self.btn_load_plots = QPushButton("加载小区库")
        self.btn_generate_column.setToolTip("先在影像上用4个点框住整列，再点击此按钮")
        self.btn_edit_column.setToolTip("恢复所选小区所属整列的共享分隔线编辑")
        self.btn_redistribute_column.setToolTip("保留整列外框，将内部所有分隔线重新等距排列")
        self.btn_add_divider.setToolTip("在当前高亮小区中间增加一条共享分隔线")
        self.btn_delete_divider.setToolTip("先点击内部黄色分隔线，再删除")
        self.btn_reverse_column.setToolTip("交换 A/B 编号起点，01 将移动到另一端")
        self.btn_rename_column.setToolTip(
            "重新设置当前整列的名称前缀、起始编号和编号位数"
        )
        self.btn_finish_column.setToolTip("确认当前整列；拖动过程已实时保存到内存")
        self.btn_finish_column.setStyleSheet(
            "QPushButton:enabled { font-weight: bold; background: #1f8f55; color: white; }"
        )
        self.export_png_check = QCheckBox("裁剪导出为 PNG（保持绘制方向）")
        self.rgb_combo = QComboBox()
        _setup_rgb_band_combo(self.rgb_combo)
        self.rotation_spin = QDoubleSpinBox()
        self.rotation_spin.setRange(-180.0, 180.0)
        self.rotation_spin.setValue(0.0)
        self.rotation_spin.setSingleStep(1.0)
        self.rotation_spin.setSuffix("°")

        self.vertex_count_label = QLabel("顶点数: 0")
        self.status_label = QLabel("流程: 绘制并添加小区 -> 选择输出文件夹 -> 批量裁剪")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.plot_list = QListWidget()

        self.viewer = CropViewer()

        self.tif_path = ""
        self.plot_polygons = []
        self._running_batch = False
        self._editing_plot_index = -1
        self._single_edit_original = None
        self._loading_edit_polygon = False
        self._column_edit_context = None
        self._next_column_index = 1

        for button in [
            self.btn_finish_column,
            self.btn_redistribute_column,
            self.btn_add_divider,
            self.btn_delete_divider,
            self.btn_reverse_column,
            self.btn_rename_column,
        ]:
            button.setEnabled(False)

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QVBoxLayout(self)

        row1 = QHBoxLayout()
        row1.addWidget(self.btn_choose_tif)
        row1.addWidget(self.tif_path_edit, 1)
        row1.addWidget(QLabel("波段"))
        row1.addWidget(self.rgb_combo)
        row1.addWidget(QLabel("旋转"))
        row1.addWidget(self.rotation_spin)

        row2 = QHBoxLayout()
        row2.addWidget(self.btn_choose_output)
        row2.addWidget(self.output_dir_edit, 1)

        row3 = QHBoxLayout()
        row3.addWidget(self.btn_clear_polygon)
        row3.addWidget(self.btn_crop)
        row3.addWidget(self.export_png_check)
        row3.addStretch(1)
        row3.addWidget(self.vertex_count_label)

        body = QHBoxLayout()
        body.addWidget(self.viewer, 8)

        right_panel = QVBoxLayout()
        right_panel.addWidget(QLabel("小区列表"))
        right_panel.addWidget(self.plot_list, 1)

        single_group = QGroupBox("单区检查与微调")
        single_layout = QVBoxLayout(single_group)
        single_layout.addWidget(self.btn_add_plot)
        single_layout.addWidget(self.btn_apply_plot)
        single_layout.addWidget(self.btn_update_plot)
        nav_row = QHBoxLayout()
        nav_row.addWidget(self.btn_previous_plot)
        nav_row.addWidget(self.btn_next_plot)
        single_layout.addLayout(nav_row)
        right_panel.addWidget(single_group)

        column_group = QGroupBox("整列生成与编辑")
        column_layout = QVBoxLayout(column_group)
        column_layout.addWidget(self.btn_generate_column)
        column_layout.addWidget(self.btn_edit_column)
        column_action_row = QHBoxLayout()
        column_action_row.addWidget(self.btn_redistribute_column)
        column_action_row.addWidget(self.btn_reverse_column)
        column_layout.addLayout(column_action_row)
        divider_action_row = QHBoxLayout()
        divider_action_row.addWidget(self.btn_add_divider)
        divider_action_row.addWidget(self.btn_delete_divider)
        column_layout.addLayout(divider_action_row)
        column_layout.addWidget(self.btn_rename_column)
        column_layout.addWidget(self.btn_finish_column)
        right_panel.addWidget(column_group)

        library_group = QGroupBox("小区库")
        library_layout = QVBoxLayout(library_group)
        rename_row = QHBoxLayout()
        rename_row.addWidget(self.btn_rename_plot)
        rename_row.addWidget(self.btn_remove_plot)
        library_layout.addLayout(rename_row)
        library_layout.addWidget(self.btn_load_plots)
        library_layout.addWidget(self.btn_save_plots)
        right_panel.addWidget(library_group)
        right_panel.addStretch(1)
        right_panel.addWidget(self.progress_bar)
        right_panel.addWidget(self.status_label)

        right_widget = QWidget()
        right_widget.setLayout(right_panel)
        body.addWidget(right_widget, 2)

        root.addLayout(row1)
        root.addLayout(row2)
        root.addLayout(row3)
        root.addLayout(body)

    def _bind_events(self):
        self.btn_choose_tif.clicked.connect(self.choose_tif)
        self.btn_choose_output.clicked.connect(self.choose_output)
        self.btn_clear_polygon.clicked.connect(self.viewer.clear_polygon)
        self.btn_crop.clicked.connect(self.start_crop)
        self.btn_add_plot.clicked.connect(self.add_current_plot)
        self.btn_generate_column.clicked.connect(self.generate_column_plots)
        self.btn_apply_plot.clicked.connect(self.apply_selected_plot)
        self.btn_update_plot.clicked.connect(self.update_selected_plot)
        self.btn_previous_plot.clicked.connect(lambda: self.navigate_plot(-1))
        self.btn_next_plot.clicked.connect(lambda: self.navigate_plot(1))
        self.btn_edit_column.clicked.connect(self.edit_selected_column)
        self.btn_finish_column.clicked.connect(self.finish_column_edit)
        self.btn_redistribute_column.clicked.connect(self.viewer.redistribute_column)
        self.btn_add_divider.clicked.connect(self.viewer.add_column_divider)
        self.btn_delete_divider.clicked.connect(self.delete_selected_divider)
        self.btn_reverse_column.clicked.connect(self.viewer.reverse_column_direction)
        self.btn_rename_column.clicked.connect(self.rename_active_column)
        self.btn_rename_plot.clicked.connect(self.rename_selected_plot)
        self.btn_remove_plot.clicked.connect(self.remove_selected_plot)
        self.btn_save_plots.clicked.connect(self.save_plot_library)
        self.btn_load_plots.clicked.connect(self.load_plot_library)
        self.plot_list.currentRowChanged.connect(self.on_plot_list_selection_changed)
        self.rgb_combo.currentIndexChanged.connect(
            lambda _: self.viewer.set_display_rgb_bands(_selected_rgb_bands(self.rgb_combo))
        )
        self.rotation_spin.valueChanged.connect(self.viewer.set_display_rotation)
        self.viewer.on_polygon_changed = self.on_polygon_changed
        self.viewer.on_polygon_geometry_changed = self.on_polygon_geometry_changed
        self.viewer.on_polygon_finish_requested = self.add_current_plot
        self.viewer.on_saved_polygon_clicked = self.on_map_plot_clicked
        self.viewer.on_edit_cancel_requested = self.cancel_current_edit
        self.viewer.on_column_changed = self.on_column_changed
        self.viewer.on_column_plot_selected = self.on_column_plot_selected
        self.viewer.on_column_cancel_requested = self.cancel_column_edit

    def clear_state(self):
        self.viewer.unload_image()
        self.tif_path = ""
        self.tif_path_edit.clear()
        self.output_dir_edit.clear()
        self.plot_polygons = []
        self.plot_list.clear()
        self._clear_plot_edit_state()
        self._column_edit_context = None
        self._set_column_editing(False)
        self._next_column_index = 1
        self.status_label.setText("流程: 绘制并添加小区 -> 选择输出文件夹 -> 批量裁剪")
        self.progress_bar.setValue(0)
        self.vertex_count_label.setText("顶点数: 0")
        self.export_png_check.setChecked(False)
        self.rotation_spin.setValue(0.0)
        self.rgb_combo.setCurrentIndex(0)

    def choose_tif(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择待裁剪影像", "", "GeoTIFF (*.tif *.tiff)")
        if not path:
            return
        try:
            block_reason = _check_large_image_without_pyramid(path)
            if block_reason:
                QMessageBox.warning(self, "加载失败", block_reason)
                return
            self.viewer.set_display_rgb_bands(_selected_rgb_bands(self.rgb_combo))
            self.viewer.load_tif(path)
            self.tif_path = path
            self.tif_path_edit.setText(path)
            self._clear_plot_edit_state()
            default_output_dir = os.path.join(
                os.path.dirname(path),
                os.path.splitext(os.path.basename(path))[0] + "_plots",
            )
            self.output_dir_edit.setText(default_output_dir)
            self.status_label.setText("影像已加载，开始绘制裁剪多边形")
            self.progress_bar.setValue(0)
            self._sync_saved_polygons_overlay()
        except Exception as exc:
            QMessageBox.warning(self, "加载失败", str(exc))

    def choose_output(self):
        default_path = self.output_dir_edit.text().strip() or os.getcwd()
        path = QFileDialog.getExistingDirectory(self, "选择输出文件夹", default_path)
        if path:
            self.output_dir_edit.setText(path)

    def on_polygon_changed(self, count: int):
        self.vertex_count_label.setText(f"顶点数: {count}")

    def on_polygon_geometry_changed(self, polygon_px):
        if self._loading_edit_polygon:
            return
        idx = self._editing_plot_index
        if idx < 0 or idx >= len(self.plot_polygons) or len(polygon_px) < 3:
            return
        try:
            polygon_px, _ = normalize_polygon_pixels(polygon_px)
            self.plot_polygons[idx]["geo_points"] = [
                self.viewer.pixel_to_geo(px, py)
                for px, py in polygon_px
            ]
        except Exception:
            return
        self._sync_saved_polygons_overlay()
        self.viewer.set_selected_saved_polygon(idx)
        self.status_label.setText(
            f"小区修改已自动保存到内存: {self.plot_polygons[idx]['name']}"
        )

    def add_current_plot(self):
        if not self.viewer.has_image():
            QMessageBox.warning(self, "提示", "请先加载影像")
            return
        if self._editing_plot_index >= 0:
            QMessageBox.warning(self, "提示", "当前正在编辑已有小区，请先点击“完成单区编辑”")
            return

        polygon_px = self.viewer.get_polygon_pixels()
        if len(polygon_px) < 3:
            QMessageBox.warning(self, "提示", "当前多边形至少需要 3 个顶点")
            return

        try:
            polygon_px, _ = normalize_polygon_pixels(polygon_px)
        except Exception as exc:
            QMessageBox.warning(self, "提示", f"当前小区点序无效: {exc}")
            return

        geo_points = []
        for px, py in polygon_px:
            gx, gy = self.viewer.pixel_to_geo(px, py)
            geo_points.append((gx, gy))

        default_name = f"plot_{len(self.plot_polygons) + 1}"
        name, ok = QInputDialog.getText(self, "小区命名", "请输入小区名称", text=default_name)
        if not ok:
            return
        name = name.strip() or default_name

        self.plot_polygons.append({"name": name, "geo_points": geo_points})
        self._refresh_plot_list(select_idx=len(self.plot_polygons) - 1)
        self._sync_saved_polygons_overlay()
        self.viewer.clear_polygon()
        self.status_label.setText(f"已添加小区: {name}")

    def generate_column_plots(self):
        if not self.viewer.has_image():
            QMessageBox.warning(self, "提示", "请先加载影像")
            return
        if self._column_edit_context is not None:
            QMessageBox.warning(self, "提示", "请先完成或取消当前整列编辑")
            return
        if self._editing_plot_index >= 0:
            self.update_selected_plot(silent=True)

        boundary_px = self.viewer.get_polygon_pixels()
        if len(boundary_px) != 4:
            QMessageBox.warning(
                self,
                "整列生成失败",
                "请先用恰好4个顶点框住一整列小区，再点击“整列生成小区”",
            )
            return

        dialog = ColumnPlotDialog(f"column_{self._next_column_index}_", self)
        if dialog.exec() != QDialog.Accepted:
            return

        payload = dialog.payload()
        try:
            dividers = column_dividers_from_quadrilateral(
                boundary_px,
                payload["count"],
                axis=payload["axis"],
            )
        except Exception as exc:
            QMessageBox.warning(self, "整列生成失败", str(exc))
            return

        names = [
            f"{payload['prefix']}{index:0{payload['padding']}d}"
            for index in range(
                payload["start"],
                payload["start"] + payload["count"],
            )
        ]
        existing_names = {str(item.get("name") or "") for item in self.plot_polygons}
        duplicate_names = [name for name in names if name in existing_names]
        if duplicate_names:
            preview = "、".join(duplicate_names[:5])
            if len(duplicate_names) > 5:
                preview += "……"
            QMessageBox.warning(
                self,
                "整列生成失败",
                f"以下名称已存在：{preview}\n请修改名称前缀或起始编号。",
            )
            return

        column_id = self._new_column_id()
        self._column_edit_context = {
            "column_id": column_id,
            "original_plots": copy.deepcopy(self.plot_polygons),
            "insert_index": len(self.plot_polygons),
            "prefix": payload["prefix"],
            "start": payload["start"],
            "padding": payload["padding"],
            "start_end": payload["start_end"],
            "is_new": True,
        }

        spatial_names = [
            (
                f"{payload['prefix']}"
                f"{payload['start'] + (position if payload['start_end'] == 'a' else payload['count'] - 1 - position):0{payload['padding']}d}"
            )
            for position in range(payload["count"])
        ]
        self.viewer.clear_polygon()
        self.viewer.start_column_edit(
            dividers,
            payload["start_end"],
            plot_names=spatial_names,
        )
        self._replace_active_column_items(
            dividers,
            payload["start_end"],
            refresh_ui=True,
        )
        self._set_column_editing(True)
        self._next_column_index += 1
        self.status_label.setText(
            f"已生成 {payload['count']} 个小区并进入整列编辑；"
            "拖动外框、分隔线或列内部，完成后点击“完成整列编辑”"
        )

    def apply_selected_plot(self):
        if self._column_edit_context is not None:
            QMessageBox.warning(self, "提示", "请先完成当前整列编辑")
            return
        idx = self.plot_list.currentRow()
        if idx < 0 or idx >= len(self.plot_polygons):
            QMessageBox.warning(self, "提示", "请先在小区列表中选择一个小区")
            return
        if not self.viewer.has_image():
            QMessageBox.warning(self, "提示", "请先加载影像")
            return

        item = self.plot_polygons[idx]
        try:
            ok, vertices, err = self._geo_points_to_pixels(item["geo_points"])
        except Exception as exc:
            QMessageBox.warning(self, "应用失败", str(exc))
            return

        if not ok:
            QMessageBox.warning(self, "应用失败", err or "该小区不在当前影像范围内")
            return

        if self._editing_plot_index != idx:
            self._single_edit_original = copy.deepcopy(item)
        self._loading_edit_polygon = True
        try:
            self.viewer.set_polygon_pixels(vertices)
        finally:
            self._loading_edit_polygon = False
        self._editing_plot_index = idx
        self.btn_update_plot.setEnabled(True)
        self.viewer.set_selected_saved_polygon(idx)
        cx = sum([p[0] for p in vertices]) / len(vertices)
        cy = sum([p[1] for p in vertices]) / len(vertices)
        self.viewer.centerOn(cx, cy)
        self.viewer.update_resolution()
        self.status_label.setText(
            f"正在编辑小区: {item['name']}；拖动橙色顶点后自动保存，Esc 可取消"
        )

    def update_selected_plot(self, silent: bool = False):
        idx = self._editing_plot_index
        if idx < 0 or idx >= len(self.plot_polygons):
            if not silent:
                QMessageBox.warning(self, "提示", "请先选择并编辑一个小区")
            self._clear_plot_edit_state()
            return

        polygon_px = self.viewer.get_polygon_pixels()
        if len(polygon_px) < 3:
            if not silent:
                QMessageBox.warning(self, "提示", "修改后的小区至少需要3个顶点")
            return

        try:
            polygon_px, _ = normalize_polygon_pixels(polygon_px)
            geo_points = [
                self.viewer.pixel_to_geo(px, py)
                for px, py in polygon_px
            ]
        except Exception as exc:
            if not silent:
                QMessageBox.warning(self, "保存修改失败", str(exc))
            return

        name = self.plot_polygons[idx]["name"]
        self.plot_polygons[idx]["geo_points"] = geo_points
        self._refresh_plot_list(select_idx=idx)
        self._sync_saved_polygons_overlay()
        self.viewer.set_selected_saved_polygon(idx)
        self._loading_edit_polygon = True
        try:
            self.viewer.clear_polygon()
        finally:
            self._loading_edit_polygon = False
        self._clear_plot_edit_state()
        self.status_label.setText(f"已完成小区编辑: {name}")

    def _clear_plot_edit_state(self):
        self._editing_plot_index = -1
        self._single_edit_original = None
        self.btn_update_plot.setEnabled(False)

    def cancel_current_edit(self):
        idx = self._editing_plot_index
        if idx < 0 or idx >= len(self.plot_polygons):
            return
        if self._single_edit_original is not None:
            self.plot_polygons[idx] = copy.deepcopy(self._single_edit_original)
        name = self.plot_polygons[idx]["name"]
        self._loading_edit_polygon = True
        try:
            self.viewer.clear_polygon()
        finally:
            self._loading_edit_polygon = False
        self._clear_plot_edit_state()
        self._refresh_plot_list(select_idx=idx)
        self._sync_saved_polygons_overlay()
        self.viewer.set_selected_saved_polygon(idx)
        self.status_label.setText(f"已取消小区修改: {name}")

    def on_map_plot_clicked(self, plot_index: int):
        if plot_index < 0 or plot_index >= len(self.plot_polygons):
            return
        self.plot_list.setCurrentRow(plot_index)
        self.viewer.set_selected_saved_polygon(plot_index)
        name = self.plot_polygons[plot_index]["name"]
        self.status_label.setText(
            f"已选择小区: {name}；点击右侧“编辑选中小区”后才会进入编辑"
        )

    def on_plot_list_selection_changed(self, plot_index: int):
        if plot_index >= 0:
            self.viewer.set_selected_saved_polygon(plot_index)
            if self._editing_plot_index < 0 and self._column_edit_context is None:
                name = self.plot_polygons[plot_index]["name"]
                self.status_label.setText(
                    f"已选择小区: {name}；点击“编辑选中小区”进行修改"
                )

    def navigate_plot(self, step: int):
        if not self.plot_polygons:
            return
        if self._column_edit_context is not None:
            current = self.viewer.column_selected_plot_index
            direction = 1 if self.viewer.column_start_end == "a" else -1
            target = current + int(step) * direction
            target = max(0, min(target, len(self.viewer.column_dividers) - 2))
            self.viewer.set_selected_column_plot(target, notify=True)
            return

        current = self.plot_list.currentRow()
        if current < 0:
            current = 0
        target = max(0, min(current + int(step), len(self.plot_polygons) - 1))
        if target == current and self._editing_plot_index == target:
            return
        if self._editing_plot_index >= 0:
            self.update_selected_plot(silent=True)
        self.plot_list.setCurrentRow(target)

    def _new_column_id(self):
        existing = {
            str(item.get("column_id") or "")
            for item in self.plot_polygons
        }
        candidate_index = self._next_column_index
        while f"column_{candidate_index}" in existing:
            candidate_index += 1
        self._next_column_index = candidate_index
        return f"column_{candidate_index}"

    def _replace_active_column_items(
        self,
        dividers,
        start_end: str,
        refresh_ui: bool,
    ):
        context = self._column_edit_context
        if context is None:
            return

        column_id = context["column_id"]
        context["start_end"] = "b" if start_end == "b" else "a"
        remaining = [
            item
            for item in self.plot_polygons
            if str(item.get("column_id") or "") != column_id
        ]
        insert_index = min(context["insert_index"], len(remaining))
        plot_pixels = plots_from_dividers(dividers)
        plot_count = len(plot_pixels)
        generated = []
        spatial_names = []

        for position, polygon_px in enumerate(plot_pixels):
            offset = position if context["start_end"] == "a" else plot_count - 1 - position
            plot_index = int(context["start"]) + offset
            name = (
                f"{context['prefix']}"
                f"{plot_index:0{int(context['padding'])}d}"
            )
            spatial_names.append(name)
            geo_points = [
                self.viewer.pixel_to_geo(px, py)
                for px, py in polygon_px
            ]
            generated.append(
                {
                    "name": name,
                    "geo_points": geo_points,
                    "column_id": column_id,
                    "column_position": position,
                    "plot_index": plot_index,
                    "column_start_end": context["start_end"],
                    "column_prefix": context["prefix"],
                    "column_start": int(context["start"]),
                    "column_padding": int(context["padding"]),
                }
            )

        self.viewer.set_column_plot_names(spatial_names)
        generated.sort(key=lambda item: int(item["plot_index"]))
        self.plot_polygons = (
            remaining[:insert_index]
            + generated
            + remaining[insert_index:]
        )

        if refresh_ui:
            selected_global = self._global_index_for_column_position(
                column_id,
                self.viewer.column_selected_plot_index,
            )
            self._refresh_plot_list(select_idx=selected_global)
            self._sync_saved_polygons_overlay()
            if selected_global >= 0:
                self.viewer.set_selected_saved_polygon(selected_global)

    def _global_index_for_column_position(self, column_id: str, position: int):
        for index, item in enumerate(self.plot_polygons):
            if (
                str(item.get("column_id") or "") == column_id
                and int(item.get("column_position", -1)) == int(position)
            ):
                return index
        return -1

    def _apply_active_column_naming(
        self,
        prefix: str,
        start: int,
        padding: int,
    ):
        context = self._column_edit_context
        if context is None:
            raise ValueError("当前没有正在编辑的整列")

        prefix = str(prefix).strip()
        start = int(start)
        padding = int(padding)
        plot_count = max(0, len(self.viewer.column_dividers) - 1)
        proposed_names = {
            f"{prefix}{index:0{padding}d}"
            for index in range(start, start + plot_count)
        }
        other_names = {
            str(item.get("name") or "")
            for item in self.plot_polygons
            if str(item.get("column_id") or "") != context["column_id"]
        }
        duplicates = sorted(proposed_names & other_names)
        if duplicates:
            preview = "、".join(duplicates[:5])
            if len(duplicates) > 5:
                preview += "……"
            raise ValueError(f"新名称与其他小区重复：{preview}")

        context["prefix"] = prefix
        context["start"] = start
        context["padding"] = padding
        self._replace_active_column_items(
            self.viewer.get_column_dividers(),
            self.viewer.column_start_end,
            refresh_ui=True,
        )
        return sorted(proposed_names)

    def rename_active_column(self):
        context = self._column_edit_context
        if context is None:
            QMessageBox.warning(self, "提示", "请先点击“编辑所在整列”")
            return

        plot_count = max(0, len(self.viewer.column_dividers) - 1)
        dialog = ColumnRenameDialog(
            context["prefix"],
            context["start"],
            context["padding"],
            plot_count,
            self.viewer.column_start_end,
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        payload = dialog.payload()
        try:
            self._apply_active_column_naming(
                payload["prefix"],
                payload["start"],
                payload["padding"],
            )
        except Exception as exc:
            QMessageBox.warning(self, "整列重命名失败", str(exc))
            return

        first_number = int(payload["start"])
        last_number = first_number + max(0, plot_count - 1)
        self.status_label.setText(
            f"整列名称已更新为 {payload['prefix']}"
            f"{first_number:0{payload['padding']}d} 至 "
            f"{payload['prefix']}{last_number:0{payload['padding']}d}；"
            "完成整列编辑后请保存小区库"
        )

    def on_column_changed(self, dividers, start_end: str, final: bool):
        if self._column_edit_context is None:
            return
        try:
            self._replace_active_column_items(
                dividers,
                start_end,
                refresh_ui=bool(final),
            )
        except Exception as exc:
            self.status_label.setText(f"整列自动保存失败: {exc}")
            return
        if final:
            self.status_label.setText(
                f"整列修改已自动保存到内存，共 {len(dividers) - 1} 个小区"
            )

    def on_column_plot_selected(self, local_index: int):
        context = self._column_edit_context
        if context is None:
            return
        global_index = self._global_index_for_column_position(
            context["column_id"],
            local_index,
        )
        if global_index >= 0:
            self.plot_list.setCurrentRow(global_index)
            self.viewer.set_selected_saved_polygon(global_index)

    def _set_column_editing(self, active: bool):
        for button in [
            self.btn_finish_column,
            self.btn_redistribute_column,
            self.btn_add_divider,
            self.btn_delete_divider,
            self.btn_reverse_column,
            self.btn_rename_column,
        ]:
            button.setEnabled(active)
        for button in [
            self.btn_add_plot,
            self.btn_generate_column,
            self.btn_apply_plot,
            self.btn_edit_column,
            self.btn_rename_plot,
            self.btn_remove_plot,
            self.btn_load_plots,
            self.btn_save_plots,
            self.btn_crop,
            self.btn_choose_tif,
            self.btn_clear_polygon,
        ]:
            button.setEnabled(not active)

    def finish_column_edit(self, silent: bool = False):
        context = self._column_edit_context
        if context is None:
            if not silent:
                QMessageBox.warning(self, "提示", "当前没有正在编辑的整列")
            return
        column_id = context["column_id"]
        plot_count = sum(
            1
            for item in self.plot_polygons
            if str(item.get("column_id") or "") == column_id
        )
        self.viewer.stop_column_edit()
        self._column_edit_context = None
        self._set_column_editing(False)
        self._sync_saved_polygons_overlay()
        self.status_label.setText(f"已完成整列编辑，共 {plot_count} 个小区")

    def cancel_column_edit(self):
        context = self._column_edit_context
        if context is None:
            return
        self.plot_polygons = copy.deepcopy(context["original_plots"])
        self.viewer.stop_column_edit()
        self._column_edit_context = None
        self._set_column_editing(False)
        self._refresh_plot_list()
        self._sync_saved_polygons_overlay()
        self.status_label.setText("已取消整列编辑并恢复修改前状态")

    def delete_selected_divider(self):
        if not self.viewer.delete_selected_column_divider():
            QMessageBox.information(
                self,
                "删除分隔线",
                "请先在影像上点击一条内部黄色分隔线，再执行删除。",
            )

    def edit_selected_column(self):
        if self._column_edit_context is not None:
            return
        idx = self.plot_list.currentRow()
        if idx < 0 or idx >= len(self.plot_polygons):
            QMessageBox.warning(self, "提示", "请先选择一个小区")
            return
        item = self.plot_polygons[idx]
        column_id = str(item.get("column_id") or "")
        if not column_id:
            QMessageBox.warning(
                self,
                "无法编辑整列",
                "该小区没有整列元数据。请使用“整列生成小区”创建，"
                "或加载由新版本保存的 Shapefile。",
            )
            return

        column_entries = [
            entry
            for entry in self.plot_polygons
            if str(entry.get("column_id") or "") == column_id
        ]
        try:
            column_entries.sort(key=lambda entry: int(entry["column_position"]))
            polygon_pixels = []
            for entry in column_entries:
                ok, pixels, err = self._geo_points_to_pixels(entry["geo_points"])
                if not ok or len(pixels) != 4:
                    raise ValueError(err or "列内小区不是四边形")
                polygon_pixels.append(pixels)

            dividers = [(polygon_pixels[0][0], polygon_pixels[0][1])]
            for pixels in polygon_pixels:
                dividers.append((pixels[3], pixels[2]))
        except Exception as exc:
            QMessageBox.warning(self, "无法编辑整列", str(exc))
            return

        if self._editing_plot_index >= 0:
            self.update_selected_plot(silent=True)
        first = column_entries[0]
        indices = [
            index
            for index, entry in enumerate(self.plot_polygons)
            if str(entry.get("column_id") or "") == column_id
        ]
        self._column_edit_context = {
            "column_id": column_id,
            "original_plots": copy.deepcopy(self.plot_polygons),
            "insert_index": min(indices),
            "prefix": str(first.get("column_prefix") or f"{column_id}_"),
            "start": int(first.get("column_start", 1)),
            "padding": int(first.get("column_padding", 2)),
            "start_end": str(first.get("column_start_end") or "a"),
            "is_new": False,
        }
        self.viewer.start_column_edit(
            dividers,
            self._column_edit_context["start_end"],
            plot_names=[str(entry.get("name") or "") for entry in column_entries],
        )
        local_position = int(item.get("column_position", 0))
        self.viewer.set_selected_column_plot(local_position, notify=True)
        self._set_column_editing(True)
        self.status_label.setText(f"正在编辑整列: {column_id}")

    def rename_selected_plot(self):
        idx = self.plot_list.currentRow()
        if idx < 0 or idx >= len(self.plot_polygons):
            QMessageBox.warning(self, "提示", "请先选择要重命名的小区")
            return

        old_name = self.plot_polygons[idx]["name"]
        name, ok = QInputDialog.getText(self, "重命名小区", "请输入新的小区名称", text=old_name)
        if not ok:
            return

        name = name.strip()
        if not name:
            QMessageBox.warning(self, "提示", "名称不能为空")
            return

        self.plot_polygons[idx]["name"] = name
        self._refresh_plot_list(select_idx=idx)
        self._sync_saved_polygons_overlay()
        self.status_label.setText(f"小区已重命名为: {name}")

    def remove_selected_plot(self):
        idx = self.plot_list.currentRow()
        if idx < 0 or idx >= len(self.plot_polygons):
            QMessageBox.warning(self, "提示", "请先选择要删除的小区")
            return

        name = self.plot_polygons[idx]["name"]
        del self.plot_polygons[idx]
        if self._editing_plot_index == idx:
            self.viewer.clear_polygon()
            self._clear_plot_edit_state()
        elif self._editing_plot_index > idx:
            self._editing_plot_index -= 1
        next_idx = min(idx, len(self.plot_polygons) - 1)
        self._refresh_plot_list(select_idx=next_idx)
        self._sync_saved_polygons_overlay()
        self.status_label.setText(f"已删除小区: {name}")

    def save_plot_library(self):
        if not self.plot_polygons:
            QMessageBox.warning(self, "提示", "当前没有可保存的小区")
            return
        if not self.viewer.has_image():
            QMessageBox.warning(self, "提示", "请先加载影像以确定坐标系")
            return

        default_path = os.path.join(os.path.dirname(self.tif_path or os.getcwd()), "plots.shp")
        shp_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存小区库",
            default_path,
            "Shapefile (*.shp)",
        )
        if not shp_path:
            return

        try:
            out_path = save_polygons_to_shapefile(
                shp_path,
                self.plot_polygons,
                self.viewer.projection_wkt(),
            )
            self.status_label.setText(f"小区库已保存: {out_path}")
            QMessageBox.information(self, "保存成功", f"已保存小区库:\n{out_path}")
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))

    def load_plot_library(self):
        if not self.viewer.has_image():
            QMessageBox.warning(self, "提示", "请先加载目标影像，再加载小区库")
            return

        vector_path, _ = QFileDialog.getOpenFileName(
            self,
            "加载小区库",
            os.path.dirname(self.tif_path or os.getcwd()),
            "矢量文件 (*.shp *.geojson *.json *.gpkg);;所有文件 (*.*)",
        )
        if not vector_path:
            return

        try:
            loaded = load_polygons_from_vector(vector_path, self.viewer.projection_wkt())
            if not loaded:
                QMessageBox.warning(self, "加载失败", "未读取到有效多边形")
                return
            self.plot_polygons = loaded
            self._clear_plot_edit_state()
            self._next_column_index = self._infer_next_column_index()
            self._refresh_plot_list(select_idx=0)
            visible_count = self._sync_saved_polygons_overlay()
            self.status_label.setText(
                f"已加载 {len(loaded)} 个小区，图中可见 {visible_count} 个"
            )
            QMessageBox.information(
                self,
                "加载成功",
                f"已加载 {len(loaded)} 个小区，图中可见 {visible_count} 个\n"
                "单击地图或列表只会选择；需要修改时请点击“编辑选中小区”。",
            )
        except Exception as exc:
            QMessageBox.warning(self, "加载失败", str(exc))

    def _refresh_plot_list(self, select_idx: int = -1):
        self.plot_list.clear()
        for idx, item in enumerate(self.plot_polygons, start=1):
            self.plot_list.addItem(f"{idx}. {item['name']}")
        if self.plot_polygons and select_idx >= 0:
            self.plot_list.setCurrentRow(select_idx)

    def _sync_saved_polygons_overlay(self):
        if not self.viewer.has_image():
            self.viewer.set_saved_polygons([])
            return 0

        overlay = []
        for index, item in enumerate(self.plot_polygons):
            ok, pixels, _ = self._geo_points_to_pixels(item["geo_points"])
            if ok:
                overlay.append(
                    {
                        "index": index,
                        "name": item["name"],
                        "pixels": pixels,
                    }
                )

        self.viewer.set_saved_polygons(overlay)
        return len(overlay)

    def _infer_next_column_index(self):
        maximum = 0
        for item in self.plot_polygons:
            column_id = str(item.get("column_id") or "")
            if not column_id.startswith("column_"):
                continue
            try:
                maximum = max(maximum, int(column_id.split("_", 1)[1]))
            except Exception:
                continue
        return maximum + 1

    def _geo_points_to_pixels(self, geo_points):
        pixels = []
        for gx, gy in geo_points:
            px, py = self.viewer.geo_to_pixel(gx, gy)
            pixels.append((px, py))

        try:
            pixels, _ = normalize_polygon_pixels(pixels)
        except Exception:
            return False, [], "小区点序无效，无法构成有效多边形"

        if len(pixels) < 3:
            return False, [], "顶点不足，无法构成多边形"

        for px, py in pixels:
            if not (0 <= px < self.viewer.full_w and 0 <= py < self.viewer.full_h):
                return False, [], "该小区超出当前影像范围，无法应用"

        return True, pixels, ""

    def start_crop(self):
        if not self.tif_path:
            QMessageBox.warning(self, "提示", "请先选择待裁剪影像")
            return

        if not self.plot_polygons:
            QMessageBox.warning(self, "提示", "请先添加至少一个小区")
            return

        output_dir = self.output_dir_edit.text().strip()
        if not output_dir:
            QMessageBox.warning(self, "提示", "请先选择输出文件夹")
            return
        os.makedirs(output_dir, exist_ok=True)

        def safe_name(name: str) -> str:
            bad = '\\/:*?"<>|'
            out = []
            for ch in (name or ""):
                out.append("_" if ch in bad else ch)
            cleaned = "".join(out).strip().strip(".")
            return cleaned or "plot"

        export_png = self.export_png_check.isChecked()
        out_ext = "png" if export_png else "tif"

        self._running_batch = True
        self._set_batch_running(True)
        self.progress_bar.setValue(0)

        total = len(self.plot_polygons)
        ok_count = 0
        fail_items = []
        used_names = {}

        for i, item in enumerate(self.plot_polygons):
            if not self._running_batch:
                fail_items.append((item.get("name", f"plot_{i + 1}"), "任务已中止"))
                continue

            name = str(item.get("name") or f"plot_{i + 1}")
            base = safe_name(name)
            suffix = used_names.get(base, 0)
            used_names[base] = suffix + 1
            out_base = base if suffix == 0 else f"{base}_{suffix + 1}"
            out_path = os.path.join(output_dir, f"{out_base}.{out_ext}")

            vertices = []
            valid = True
            for gx, gy in item["geo_points"]:
                try:
                    px, py = self.viewer.geo_to_pixel(gx, gy)
                except Exception as exc:
                    valid = False
                    fail_items.append((name, f"坐标转换失败: {exc}"))
                    break
                vertices.append((px, py))

            if not valid or len(vertices) < 3:
                if valid:
                    fail_items.append((name, "顶点不足或无效"))
                continue

            self.status_label.setText(f"正在裁剪: {name} ({i + 1}/{total})")
            QApplication.processEvents()

            try:
                crop_tif_with_polygon(
                    self.tif_path,
                    vertices,
                    out_path,
                    overwrite=False,
                    output_format=out_ext,
                    display_rotation_deg=(
                        self.viewer.display_rotation_deg if export_png else 0.0
                    ),
                    progress_callback=lambda p, m, idx=i: self._on_single_crop_progress(idx, total, p, m),
                )
                ok_count += 1
            except Exception as exc:
                fail_items.append((name, str(exc)))

        self._set_batch_running(False)
        self._running_batch = False
        self.progress_bar.setValue(100)

        if not fail_items:
            self.status_label.setText(f"批量裁剪完成，共 {ok_count} 个")
            QMessageBox.information(
                self,
                "小区裁剪完成",
                f"成功裁剪 {ok_count}/{total} 个小区\n输出格式: {out_ext.upper()}\n输出目录:\n{output_dir}",
            )
            return

        self.status_label.setText(f"批量裁剪完成，成功 {ok_count}/{total}")
        detail = "\n".join([f"- {n}: {e}" for n, e in fail_items[:8]])
        if len(fail_items) > 8:
            detail += f"\n... 其余 {len(fail_items) - 8} 个失败项已省略"
        QMessageBox.warning(
            self,
            "小区裁剪部分失败",
            f"成功 {ok_count}/{total} 个\n输出格式: {out_ext.upper()}\n输出目录:\n{output_dir}\n\n失败详情:\n{detail}",
        )

    def _on_single_crop_progress(self, idx: int, total: int, percent: int, _message: str):
        global_percent = int(((idx + max(0, min(100, percent)) / 100.0) / max(1, total)) * 100)
        self.progress_bar.setValue(global_percent)
        QApplication.processEvents()

    def _set_batch_running(self, running: bool):
        self.btn_crop.setEnabled(not running)
        self.btn_choose_tif.setEnabled(not running)
        self.btn_choose_output.setEnabled(not running)
        self.btn_add_plot.setEnabled(not running)
        self.btn_generate_column.setEnabled(not running)
        self.btn_apply_plot.setEnabled(not running)
        self.btn_update_plot.setEnabled(not running and self._editing_plot_index >= 0)
        self.btn_previous_plot.setEnabled(not running)
        self.btn_next_plot.setEnabled(not running)
        self.btn_edit_column.setEnabled(not running)
        self.btn_finish_column.setEnabled(False)
        self.btn_redistribute_column.setEnabled(False)
        self.btn_add_divider.setEnabled(False)
        self.btn_delete_divider.setEnabled(False)
        self.btn_reverse_column.setEnabled(False)
        self.btn_rename_column.setEnabled(False)
        self.btn_rename_plot.setEnabled(not running)
        self.btn_remove_plot.setEnabled(not running)
        self.btn_load_plots.setEnabled(not running)
        self.btn_save_plots.setEnabled(not running)
        self.btn_clear_polygon.setEnabled(not running)

    def closeEvent(self, event):
        self._running_batch = False
        super().closeEvent(event)


class ShapefileMergePage(QWidget):
    def __init__(self):
        super().__init__()

        self.input_paths = []
        self._worker_thread = None
        self._worker = None
        self._is_running = False

        self.input_list = QListWidget()
        self.input_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.btn_add_files = QPushButton("添加 Shap 文件")
        self.btn_remove_files = QPushButton("移除选中")
        self.btn_clear_files = QPushButton("清空列表")

        self.output_edit = QLineEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText("请选择合并后的 .shp 输出路径")
        self.btn_choose_output = QPushButton("选择输出")
        self.btn_merge = QPushButton("开始合并")

        self.status_label = QLabel("等待选择至少两个 Shapefile")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QVBoxLayout(self)

        description = QLabel(
            "合并“小区裁剪”页生成的多个 Shapefile。"
            "输出会保留 name（小区编号/名称）、plot_idx 及整列编号规则；"
            "其他文件将转换到列表中第一个文件的坐标系。"
        )
        description.setWordWrap(True)
        root.addWidget(description)

        input_group = QGroupBox("待合并 Shapefile")
        input_layout = QVBoxLayout(input_group)
        input_layout.addWidget(self.input_list, 1)

        input_actions = QHBoxLayout()
        input_actions.addWidget(self.btn_add_files)
        input_actions.addWidget(self.btn_remove_files)
        input_actions.addWidget(self.btn_clear_files)
        input_actions.addStretch(1)
        input_layout.addLayout(input_actions)
        root.addWidget(input_group, 1)

        output_group = QGroupBox("输出文件")
        output_layout = QHBoxLayout(output_group)
        output_layout.addWidget(self.output_edit, 1)
        output_layout.addWidget(self.btn_choose_output)
        root.addWidget(output_group)

        action_row = QHBoxLayout()
        action_row.addWidget(self.btn_merge)
        action_row.addStretch(1)
        root.addLayout(action_row)
        root.addWidget(self.status_label)
        root.addWidget(self.progress_bar)

    def _bind_events(self):
        self.btn_add_files.clicked.connect(self.add_files)
        self.btn_remove_files.clicked.connect(self.remove_selected_files)
        self.btn_clear_files.clicked.connect(self.clear_files)
        self.btn_choose_output.clicked.connect(self.choose_output)
        self.btn_merge.clicked.connect(self.start_merge)

    def clear_state(self):
        if self._is_running:
            return
        self.input_paths = []
        self.input_list.clear()
        self.output_edit.clear()
        self.status_label.setText("等待选择至少两个 Shapefile")
        self.progress_bar.setValue(0)
        self._set_running(False)

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要合并的 Shapefile",
            "",
            "Shapefile (*.shp)",
        )
        if not paths:
            return

        existing = {os.path.normcase(os.path.abspath(path)) for path in self.input_paths}
        for path in paths:
            absolute = os.path.abspath(path)
            key = os.path.normcase(absolute)
            if key in existing:
                continue
            existing.add(key)
            self.input_paths.append(absolute)

        self._refresh_input_list()
        if self.input_paths and not self.output_edit.text().strip():
            default_output = os.path.join(
                os.path.dirname(self.input_paths[0]),
                "merged_plots.shp",
            )
            self.output_edit.setText(default_output)

    def remove_selected_files(self):
        rows = sorted(
            {self.input_list.row(item) for item in self.input_list.selectedItems()},
            reverse=True,
        )
        for row in rows:
            if 0 <= row < len(self.input_paths):
                self.input_paths.pop(row)
        self._refresh_input_list()

    def clear_files(self):
        self.input_paths = []
        self.input_list.clear()
        self.status_label.setText("等待选择至少两个 Shapefile")

    def _refresh_input_list(self):
        self.input_list.clear()
        for index, path in enumerate(self.input_paths, start=1):
            self.input_list.addItem(f"{index}. {path}")
        self.status_label.setText(f"已选择 {len(self.input_paths)} 个 Shapefile")

    def choose_output(self):
        default_path = self.output_edit.text().strip()
        if not default_path and self.input_paths:
            default_path = os.path.join(
                os.path.dirname(self.input_paths[0]),
                "merged_plots.shp",
            )
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存合并后的 Shapefile",
            default_path,
            "Shapefile (*.shp)",
        )
        if not path:
            return
        if not path.lower().endswith(".shp"):
            path += ".shp"
        self.output_edit.setText(path)

    def start_merge(self):
        if len(self.input_paths) < 2:
            QMessageBox.warning(self, "提示", "请至少选择两个不同的 Shapefile")
            return

        output_path = self.output_edit.text().strip()
        if not output_path:
            QMessageBox.warning(self, "提示", "请先选择输出文件")
            return

        normalized_inputs = {
            os.path.normcase(os.path.abspath(path)) for path in self.input_paths
        }
        if os.path.normcase(os.path.abspath(output_path)) in normalized_inputs:
            QMessageBox.warning(self, "提示", "输出文件不能与任一输入文件相同")
            return

        if os.path.exists(output_path):
            answer = QMessageBox.question(
                self,
                "确认覆盖",
                f"输出文件已存在，是否覆盖？\n{output_path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.progress_bar.setValue(0)
        self.status_label.setText("准备合并...")
        self._set_running(True)

        self._worker_thread = QThread(self)
        self._worker = ShapefileMergeWorker(self.input_paths, output_path)
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.on_progress)
        self._worker.finished.connect(self.on_finished)
        self._worker.failed.connect(self.on_failed)

        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _set_running(self, running: bool):
        self._is_running = running
        for widget in [
            self.btn_add_files,
            self.btn_remove_files,
            self.btn_clear_files,
            self.btn_choose_output,
            self.btn_merge,
        ]:
            widget.setEnabled(not running)

    def on_progress(self, percent: int, message: str):
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)

    def on_finished(self, result):
        self._set_running(False)
        self.progress_bar.setValue(100)
        self.status_label.setText(
            f"合并完成：{result['plot_count']} 个小区，编号字段已保留"
        )
        QMessageBox.information(
            self,
            "Shapefile 合并完成",
            f"已合并 {result['input_count']} 个 Shapefile\n"
            f"小区总数: {result['plot_count']}\n"
            f"整列分组数: {result['column_count']}\n"
            "已保留 name、plot_idx 及整列编号规则\n"
            f"输出文件:\n{result['output_path']}",
        )

    def on_failed(self, error_message: str):
        self._set_running(False)
        self.status_label.setText("合并失败")
        QMessageBox.warning(self, "Shapefile 合并失败", error_message)


class HeightExtractionPage(QWidget):
    OUTPUT_FILE_NAME = "height_extraction_results.csv"

    def __init__(self):
        super().__init__()
        self._worker_thread = None
        self._worker = None
        self._is_running = False

        self.target_folder_edit = QLineEdit()
        self.target_folder_edit.setReadOnly(True)
        self.target_folder_edit.setPlaceholderText("请选择包含 DEM 小图的文件夹")
        self.reference_path_edit = QLineEdit()
        self.reference_path_edit.setReadOnly(True)
        self.reference_path_edit.setPlaceholderText("请选择作为高度基准的 TIF")
        self.output_path_edit = QLineEdit()
        self.output_path_edit.setReadOnly(True)

        self.btn_choose_folder = QPushButton("选择目标文件夹")
        self.btn_choose_reference = QPushButton("选择基准文件")
        self.btn_extract = QPushButton("开始提取高度")

        self.reference_percentile_spin = QDoubleSpinBox()
        self.reference_percentile_spin.setRange(0.0, 100.0)
        self.reference_percentile_spin.setDecimals(1)
        self.reference_percentile_spin.setSingleStep(1.0)
        self.reference_percentile_spin.setValue(50.0)
        self.reference_percentile_spin.setSuffix(" %")

        self.target_percentiles_edit = QLineEdit("95, 99")
        self.target_percentiles_edit.setPlaceholderText("例如：95, 99")
        self.recursive_check = QCheckBox("包含子文件夹中的 TIF")

        self.status_label = QLabel("等待选择目标文件夹和基准文件")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self._init_ui()
        self._bind_events()

    @staticmethod
    def _path_row(edit, button):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        layout.addWidget(button)
        return widget

    def _init_ui(self):
        root = QVBoxLayout(self)

        description = QLabel(
            "选择包含 DEM/DSM 小图的文件夹和一个基准 TIF。程序默认使用基准文件的 "
            "P50（中位高程），计算每个目标文件的 P95、P99 绝对高程及其相对高度，"
            "并将 CSV 自动保存到目标文件夹。"
        )
        description.setWordWrap(True)
        description.setStyleSheet(
            "QLabel { padding: 10px; border: 1px solid #c7cfdb; "
            "border-radius: 5px; background: #f5f7fa; color: #344054; }"
        )
        root.addWidget(description)

        input_group = QGroupBox("输入")
        input_form = QFormLayout(input_group)
        input_form.addRow(
            "目标文件夹",
            self._path_row(self.target_folder_edit, self.btn_choose_folder),
        )
        input_form.addRow(
            "基准 TIF",
            self._path_row(self.reference_path_edit, self.btn_choose_reference),
        )
        input_form.addRow("输出 CSV", self.output_path_edit)
        root.addWidget(input_group)

        parameter_group = QGroupBox("统计参数")
        parameter_form = QFormLayout(parameter_group)
        parameter_form.addRow("基准文件百分位", self.reference_percentile_spin)
        parameter_form.addRow("目标文件百分位", self.target_percentiles_edit)
        parameter_form.addRow("搜索范围", self.recursive_check)
        root.addWidget(parameter_group)

        action_row = QHBoxLayout()
        action_row.addWidget(self.btn_extract)
        action_row.addStretch(1)
        root.addLayout(action_row)
        root.addWidget(self.status_label)
        root.addWidget(self.progress_bar)
        root.addStretch(1)

    def _bind_events(self):
        self.btn_choose_folder.clicked.connect(self.choose_target_folder)
        self.btn_choose_reference.clicked.connect(self.choose_reference_file)
        self.btn_extract.clicked.connect(self.start_extraction)

    def clear_state(self):
        if self._is_running:
            return
        self.target_folder_edit.clear()
        self.reference_path_edit.clear()
        self.output_path_edit.clear()
        self.reference_percentile_spin.setValue(50.0)
        self.target_percentiles_edit.setText("95, 99")
        self.recursive_check.setChecked(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("等待选择目标文件夹和基准文件")

    def choose_target_folder(self):
        current = self.target_folder_edit.text().strip()
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择包含 DEM 小图的文件夹",
            current,
        )
        if not folder:
            return
        folder = os.path.abspath(folder)
        self.target_folder_edit.setText(folder)
        self.output_path_edit.setText(
            os.path.join(folder, self.OUTPUT_FILE_NAME)
        )
        self.status_label.setText("目标文件夹已选择，请选择基准 TIF")

    def choose_reference_file(self):
        default_dir = self.target_folder_edit.text().strip()
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择高度基准 TIF",
            default_dir,
            "GeoTIFF (*.tif *.tiff)",
        )
        if not path:
            return
        self.reference_path_edit.setText(os.path.abspath(path))
        self.status_label.setText("输入已准备，可以开始提取高度")

    def _parse_target_percentiles(self):
        text = self.target_percentiles_edit.text().strip().replace("，", ",")
        if not text:
            raise ValueError("请填写至少一个目标百分位")
        values = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = float(part)
            except ValueError as exc:
                raise ValueError(f"无效的目标百分位: {part}") from exc
            if not 0.0 <= value <= 100.0:
                raise ValueError(f"目标百分位必须在 0～100 之间: {part}")
            if value not in values:
                values.append(value)
        if not values:
            raise ValueError("请填写至少一个目标百分位")
        return values

    def start_extraction(self):
        target_folder = self.target_folder_edit.text().strip()
        reference_path = self.reference_path_edit.text().strip()
        output_path = self.output_path_edit.text().strip()
        if not target_folder:
            QMessageBox.warning(self, "提示", "请先选择目标文件夹")
            return
        if not reference_path:
            QMessageBox.warning(self, "提示", "请先选择基准 TIF")
            return
        try:
            target_percentiles = self._parse_target_percentiles()
        except ValueError as exc:
            QMessageBox.warning(self, "参数错误", str(exc))
            return

        if not output_path:
            output_path = os.path.join(target_folder, self.OUTPUT_FILE_NAME)
            self.output_path_edit.setText(output_path)
        if os.path.exists(output_path):
            answer = QMessageBox.question(
                self,
                "确认覆盖",
                f"结果 CSV 已存在，是否覆盖？\n{output_path}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.progress_bar.setValue(0)
        self.status_label.setText("准备提取高度...")
        self._set_running(True)

        self._worker_thread = QThread(self)
        self._worker = HeightExtractionWorker(
            target_folder,
            reference_path,
            output_path,
            self.reference_percentile_spin.value(),
            target_percentiles,
            recursive=self.recursive_check.isChecked(),
        )
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.on_progress)
        self._worker.finished.connect(self.on_finished)
        self._worker.failed.connect(self.on_failed)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _set_running(self, running: bool):
        self._is_running = running
        for widget in [
            self.btn_choose_folder,
            self.btn_choose_reference,
            self.btn_extract,
            self.reference_percentile_spin,
            self.target_percentiles_edit,
            self.recursive_check,
        ]:
            widget.setEnabled(not running)

    def on_progress(self, percent: int, message: str):
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)

    def on_finished(self, result):
        self._set_running(False)
        self.progress_bar.setValue(100)
        self.status_label.setText(
            f"高度提取完成：成功 {result['success_count']} 个，"
            f"失败 {result['failure_count']} 个"
        )
        QMessageBox.information(
            self,
            "高度提取完成",
            f"基准文件: {os.path.basename(result['reference_path'])}\n"
            f"基准高程: {result['reference_elevation']:.4f}\n"
            f"成功: {result['success_count']} 个\n"
            f"失败: {result['failure_count']} 个\n"
            f"CSV 文件:\n{result['output_path']}",
        )

    def on_failed(self, error_message: str):
        self._set_running(False)
        self.status_label.setText("高度提取失败")
        QMessageBox.warning(self, "高度提取失败", error_message)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("UavTool")
        icon_path = _resolve_window_icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))
        self.resize(1440, 900)

        container = QWidget()
        root = QVBoxLayout(container)

        self.tab_bar = QTabBar()
        self.tab_bar.addTab("航线绘制")
        self.tab_bar.addTab("金字塔构建")
        self.tab_bar.addTab("图像配准")
        self.tab_bar.addTab("图像裁剪")
        self.tab_bar.addTab("小区裁剪")
        self.tab_bar.addTab("Shap 合并")
        self.tab_bar.addTab("高度提取")
        self.tab_bar.setCurrentIndex(0)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setDrawBase(True)
        self.tab_bar.setStyleSheet(
            "QTabBar::tab {"
            "padding: 8px 18px;"
            "margin-right: 4px;"
            "border: 1px solid #c7cfdb;"
            "border-bottom: none;"
            "border-top-left-radius: 6px;"
            "border-top-right-radius: 6px;"
            "background: #e7ecf3;"
            "color: #475467;"
            "}"
            "QTabBar::tab:selected {"
            "background: #2f6feb;"
            "color: #ffffff;"
            "font-weight: 700;"
            "border-color: #2f6feb;"
            "}"
            "QTabBar::tab:hover:!selected {"
            "background: #dbe5f3;"
            "}"
        )

        self.stack = QStackedWidget()
        self.draw_page = DrawRoutePage()
        self.pyramid_page = PyramidBuildPage()
        self.registration_page = RegistrationPage()
        self.image_crop_page = ImageCropPage()
        self.plot_crop_page = PlotCropPage()
        self.shapefile_merge_page = ShapefileMergePage()
        self.height_extraction_page = HeightExtractionPage()

        self.stack.addWidget(self.draw_page)
        self.stack.addWidget(self.pyramid_page)
        self.stack.addWidget(self.registration_page)
        self.stack.addWidget(self.image_crop_page)
        self.stack.addWidget(self.plot_crop_page)
        self.stack.addWidget(self.shapefile_merge_page)
        self.stack.addWidget(self.height_extraction_page)
        self.stack.setCurrentIndex(0)

        self._current_index = 0
        self.tab_bar.currentChanged.connect(self.on_tab_changed)

        root.addWidget(self.tab_bar, 0)
        root.addWidget(self.stack)
        self.setCentralWidget(container)

    def on_tab_changed(self, index: int):
        self._clear_page(index)
        self.stack.setCurrentIndex(index)
        self._current_index = index

    def _clear_page(self, index: int):
        if index == 0:
            self.draw_page.clear_state()
            return
        if index == 1:
            self.pyramid_page.clear_state()
            return
        if index == 2:
            self.registration_page.clear_state()
            return
        if index == 3:
            self.image_crop_page.clear_state()
            return
        if index == 4:
            self.plot_crop_page.clear_state()
            return
        if index == 5:
            self.shapefile_merge_page.clear_state()
            return
        if index == 6:
            self.height_extraction_page.clear_state()
            return
