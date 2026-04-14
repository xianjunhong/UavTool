from PySide6.QtCore import QObject, Signal

from utils.env_setup import configure_runtime_env


def get_overview_count(tif_path: str) -> int:
    configure_runtime_env()
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError("无法打开该 TIF 文件")
    if ds.RasterCount <= 0:
        raise RuntimeError("影像波段为空")

    band = ds.GetRasterBand(1)
    ov_count = band.GetOverviewCount()
    ds = None
    return ov_count


class PyramidBuildWorker(QObject):
    progress = Signal(int, str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, tif_path: str):
        super().__init__()
        self.tif_path = tif_path

    def run(self):
        try:
            configure_runtime_env()
            from osgeo import gdal

            gdal.UseExceptions()
            gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")

            ds = gdal.Open(self.tif_path, gdal.GA_Update)
            if ds is None:
                raise RuntimeError("无法以读写模式打开该 TIF 文件")
            if ds.RasterCount <= 0:
                raise RuntimeError("影像波段为空")

            band = ds.GetRasterBand(1)
            if band.GetOverviewCount() > 0:
                ds = None
                self.progress.emit(100, "该影像已包含金字塔")
                self.finished.emit("该影像已包含金字塔")
                return

            self.progress.emit(0, "开始构建金字塔")

            factors = [2, 4, 8, 16, 32, 64, 128]

            def callback(complete, message, _):
                percent = int(max(0.0, min(1.0, complete)) * 100)
                self.progress.emit(percent, "正在构建金字塔")
                return 1

            ds.BuildOverviews("AVERAGE", factors, callback=callback)
            ds.FlushCache()
            ds = None

            self.progress.emit(100, "金字塔构建完成")
            self.finished.emit("金字塔构建完成")
        except Exception as exc:
            self.failed.emit(str(exc))
