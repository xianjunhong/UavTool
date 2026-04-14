import os
import sys
import time

from PySide6.QtCore import QThread
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
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
    QTabBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from logic.kmz_export import MissionConfig, export_waypoints_to_kmz
from logic.crop import CropWorker, crop_tif_with_polygon, normalize_polygon_pixels
from logic.polygon_io import load_polygons_from_vector, save_polygons_to_shapefile
from logic.pyramid_builder import PyramidBuildWorker, get_overview_count
from logic.registration import RegistrationWorker
from logic.waypoint_logic import format_waypoint
from ui.crop_viewer import CropViewer
from ui.registration_viewer import RegistrationViewer
from ui.viewer import UavViewer


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


class ExportKmzDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出航线参数")
        self.resize(560, 320)

        root = QVBoxLayout(self)
        form = QFormLayout()

        self.drone_combo = QComboBox()
        self.drone_combo.addItems(["M300", "M3T"])

        self.takeoff_spin = QDoubleSpinBox()
        self.takeoff_spin.setRange(1, 500)
        self.takeoff_spin.setValue(20)

        self.trans_speed_spin = QDoubleSpinBox()
        self.trans_speed_spin.setRange(1, 30)
        self.trans_speed_spin.setValue(15)

        self.auto_speed_spin = QDoubleSpinBox()
        self.auto_speed_spin.setRange(0.5, 30)
        self.auto_speed_spin.setValue(5)

        self.execute_height_spin = QDoubleSpinBox()
        self.execute_height_spin.setRange(0.5, 200)
        self.execute_height_spin.setValue(3)

        self.heading_mode_combo = QComboBox()
        self.heading_mode_combo.addItem("跟随航线", "followWayline")
        self.heading_mode_combo.addItem("固定", "fixed")

        self.pitch_spin = QDoubleSpinBox()
        self.pitch_spin.setRange(-120, 30)
        self.pitch_spin.setValue(0)

        self.yaw_spin = QDoubleSpinBox()
        self.yaw_spin.setRange(-180, 180)
        self.yaw_spin.setValue(0)

        default_name = f"route_{time.strftime('%Y%m%d_%H%M%S')}.kmz"
        default_path = os.path.join(os.getcwd(), default_name)
        self.output_edit = QLineEdit(default_path)
        browse_btn = QPushButton("选择...")
        browse_btn.clicked.connect(self.choose_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        output_row.addWidget(browse_btn)

        form.addRow("无人机型号", self.drone_combo)
        form.addRow("起飞安全高度(m)", self.takeoff_spin)
        form.addRow("首段过渡速度(m/s)", self.trans_speed_spin)
        form.addRow("自动飞行速度(m/s)", self.auto_speed_spin)
        form.addRow("执行高度(m)", self.execute_height_spin)
        form.addRow("偏航模式", self.heading_mode_combo)
        form.addRow("云台俯仰角(度)", self.pitch_spin)
        form.addRow("云台偏航角(度)", self.yaw_spin)
        form.addRow("输出文件", output_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        root.addLayout(form)
        root.addWidget(buttons)

    def choose_output(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出 KMZ", self.output_edit.text(), "KMZ 文件 (*.kmz)")
        if path:
            if not path.lower().endswith(".kmz"):
                path += ".kmz"
            self.output_edit.setText(path)

    def payload(self):
        config = MissionConfig(
            drone_type=self.drone_combo.currentText(),
            takeoff_security_height=self.takeoff_spin.value(),
            global_transitional_speed=self.trans_speed_spin.value(),
            auto_flight_speed=self.auto_speed_spin.value(),
            execute_height=self.execute_height_spin.value(),
            waypoint_heading_mode=self.heading_mode_combo.currentData(),
        )
        return {
            "config": config,
            "pitch": self.pitch_spin.value(),
            "yaw": self.yaw_spin.value(),
            "output_path": self.output_edit.text().strip(),
        }


class DrawRoutePage(QWidget):
    def __init__(self):
        super().__init__()

        self.viewer = UavViewer()
        self.coord_list = QListWidget()
        self.import_btn = QPushButton("导入 TIF")
        self.export_btn = QPushButton("导出航线")
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
        self.rotation_spin.valueChanged.connect(self.viewer.set_display_rotation)
        self.coord_list.itemDoubleClicked.connect(self.on_list_item_double_clicked)
        self.viewer.on_waypoint_added = self.on_waypoint_added
        self.viewer.on_waypoint_removed = self.on_waypoint_removed
        self.viewer.on_waypoints_reindexed = self.on_waypoints_reindexed

    def handle_import(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "选择 TIF 影像", "", "GeoTIFF (*.tif *.tiff)")
        if not file_path:
            return

        try:
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
        self.target_rotation_spin = QDoubleSpinBox()
        self.target_rotation_spin.setRange(-180.0, 180.0)
        self.target_rotation_spin.setValue(0.0)
        self.target_rotation_spin.setSingleStep(1.0)
        self.target_rotation_spin.setSuffix("°")

        self.btn_choose_src = QPushButton("选择 src 图像")
        self.btn_choose_target = QPushButton("选择 target 图像")
        self.btn_clear_src = QPushButton("清空 src 点")
        self.btn_clear_target = QPushButton("清空 target 点")
        self.btn_align = QPushButton("图像对齐")

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
        self._worker_thread = None
        self._worker = None

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QVBoxLayout(self)

        top_src = QHBoxLayout()
        top_src.addWidget(self.btn_choose_src)
        top_src.addWidget(self.src_path_edit, 1)
        top_src.addWidget(QLabel("旋转"))
        top_src.addWidget(self.src_rotation_spin)

        top_tgt = QHBoxLayout()
        top_tgt.addWidget(self.btn_choose_target)
        top_tgt.addWidget(self.target_path_edit, 1)
        top_tgt.addWidget(QLabel("旋转"))
        top_tgt.addWidget(self.target_rotation_spin)

        actions = QHBoxLayout()
        actions.addWidget(self.btn_clear_src)
        actions.addWidget(self.btn_clear_target)
        actions.addWidget(self.btn_align)
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
        self.src_rotation_spin.valueChanged.connect(self.src_viewer.set_display_rotation)
        self.target_rotation_spin.valueChanged.connect(self.target_viewer.set_display_rotation)

        self.src_viewer.on_points_changed = self.on_src_points_changed
        self.target_viewer.on_points_changed = self.on_target_points_changed

    def choose_src(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 src 图像", "", "GeoTIFF (*.tif *.tiff)")
        if not path:
            return
        try:
            self.src_viewer.load_tif(path)
            self.src_tif_path = path
            self.src_path_edit.setText(path)
            self.src_count_label.setText("src 点: 0")
        except Exception as exc:
            QMessageBox.warning(self, "加载失败", str(exc))

    def choose_target(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 target 图像", "", "GeoTIFF (*.tif *.tiff)")
        if not path:
            return
        try:
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
        if not self.src_tif_path or not self.target_tif_path:
            QMessageBox.warning(self, "提示", "请先选择 src 和 target 图像")
            return

        src_points = self.src_viewer.get_points()
        target_points = self.target_viewer.get_points()

        if len(src_points) != len(target_points):
            QMessageBox.warning(self, "提示", "src 与 target 点数不一致，请按顺序配对")
            return

        if len(src_points) < 3:
            QMessageBox.warning(self, "提示", "至少需要 3 对点，建议 4 对及以上")
            return

        default_output = os.path.join(
            os.path.dirname(self.target_tif_path),
            os.path.splitext(os.path.basename(self.target_tif_path))[0] + "_aligned.tif",
        )
        output_path, _ = QFileDialog.getSaveFileName(self, "保存配准后图像", default_output, "GeoTIFF (*.tif *.tiff)")
        if not output_path:
            return

        self.progress_bar.setValue(0)
        self.status_label.setText("开始图像对齐...")
        self.btn_align.setEnabled(False)
        self.btn_choose_src.setEnabled(False)
        self.btn_choose_target.setEnabled(False)

        self._worker_thread = QThread(self)
        self._worker = RegistrationWorker(
            self.src_tif_path,
            self.target_tif_path,
            src_points,
            target_points,
            output_path,
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
        msg = (
            f"输出文件: {result['output_path']}\n"
            f"点对数: {result['point_count']}\n"
            f"训练RMSE(像素): {result['rmse']:.6f}\n"
            f"最大误差(像素): {result['max_error']:.6f}\n"
            f"det(A): {result['determinant']:.6e}\n"
            f"cond(A): {result['condition_number']:.3e}"
        )
        if result.get("loo_rmse") is not None:
            msg += f"\n留一RMSE(像素): {result['loo_rmse']:.6f}"
        if result.get("rmse_note"):
            msg += f"\n\n说明: {result['rmse_note']}"
        self.status_label.setText("配准完成")
        QMessageBox.information(self, "图像对齐完成", msg)

    def on_align_failed(self, error_message: str):
        self.btn_align.setEnabled(True)
        self.btn_choose_src.setEnabled(True)
        self.btn_choose_target.setEnabled(True)
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
        self.rotation_spin.valueChanged.connect(self.viewer.set_display_rotation)
        self.viewer.on_polygon_changed = self.on_polygon_changed

    def choose_tif(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择待裁剪影像", "", "GeoTIFF (*.tif *.tiff)")
        if not path:
            return
        try:
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
        self.btn_apply_plot = QPushButton("编辑选中小区")
        self.btn_rename_plot = QPushButton("重命名")
        self.btn_remove_plot = QPushButton("删除")
        self.btn_save_plots = QPushButton("保存小区库")
        self.btn_load_plots = QPushButton("加载小区库")
        self.export_png_check = QCheckBox("裁剪导出为 PNG")
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

        self._init_ui()
        self._bind_events()

    def _init_ui(self):
        root = QVBoxLayout(self)

        row1 = QHBoxLayout()
        row1.addWidget(self.btn_choose_tif)
        row1.addWidget(self.tif_path_edit, 1)
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

        right_panel.addWidget(self.btn_add_plot)
        right_panel.addWidget(self.btn_apply_plot)
        right_panel.addWidget(self.btn_rename_plot)
        right_panel.addWidget(self.btn_remove_plot)
        right_panel.addWidget(self.btn_load_plots)
        right_panel.addWidget(self.btn_save_plots)
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
        self.btn_apply_plot.clicked.connect(self.apply_selected_plot)
        self.btn_rename_plot.clicked.connect(self.rename_selected_plot)
        self.btn_remove_plot.clicked.connect(self.remove_selected_plot)
        self.btn_save_plots.clicked.connect(self.save_plot_library)
        self.btn_load_plots.clicked.connect(self.load_plot_library)
        self.plot_list.itemDoubleClicked.connect(lambda _: self.apply_selected_plot())
        self.rotation_spin.valueChanged.connect(self.viewer.set_display_rotation)
        self.viewer.on_polygon_changed = self.on_polygon_changed
        self.viewer.on_polygon_finish_requested = self.add_current_plot

    def choose_tif(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择待裁剪影像", "", "GeoTIFF (*.tif *.tiff)")
        if not path:
            return
        try:
            self.viewer.load_tif(path)
            self.tif_path = path
            self.tif_path_edit.setText(path)
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

    def add_current_plot(self):
        if not self.viewer.has_image():
            QMessageBox.warning(self, "提示", "请先加载影像")
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

    def apply_selected_plot(self):
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

        self.viewer.set_polygon_pixels(vertices)
        cx = sum([p[0] for p in vertices]) / len(vertices)
        cy = sum([p[1] for p in vertices]) / len(vertices)
        self.viewer.centerOn(cx, cy)
        self.viewer.update_resolution()
        self.status_label.setText(f"已应用并定位小区: {item['name']}")

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
            self._refresh_plot_list(select_idx=0)
            visible_count = self._sync_saved_polygons_overlay()
            applied = False
            if self.plot_polygons:
                try:
                    self.apply_selected_plot()
                    applied = True
                except Exception:
                    applied = False
            self.status_label.setText(
                f"已加载 {len(loaded)} 个小区，图中可见 {visible_count} 个"
            )
            tip = "并已自动应用第一个小区" if applied else ""
            QMessageBox.information(
                self,
                "加载成功",
                f"已加载 {len(loaded)} 个小区，图中可见 {visible_count} 个{tip}",
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
        for item in self.plot_polygons:
            ok, pixels, _ = self._geo_points_to_pixels(item["geo_points"])
            if ok:
                overlay.append({"name": item["name"], "pixels": pixels})

        self.viewer.set_saved_polygons(overlay)
        return len(overlay)

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
        self.btn_apply_plot.setEnabled(not running)
        self.btn_rename_plot.setEnabled(not running)
        self.btn_remove_plot.setEnabled(not running)
        self.btn_load_plots.setEnabled(not running)
        self.btn_save_plots.setEnabled(not running)
        self.btn_clear_polygon.setEnabled(not running)

    def closeEvent(self, event):
        self._running_batch = False
        super().closeEvent(event)


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

        self.stack.addWidget(self.draw_page)
        self.stack.addWidget(self.pyramid_page)
        self.stack.addWidget(self.registration_page)
        self.stack.addWidget(self.image_crop_page)
        self.stack.addWidget(self.plot_crop_page)
        self.stack.setCurrentIndex(0)

        self.tab_bar.currentChanged.connect(self.stack.setCurrentIndex)

        root.addWidget(self.tab_bar, 0)
        root.addWidget(self.stack)
        self.setCentralWidget(container)
