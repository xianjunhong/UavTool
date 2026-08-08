# GeoTIFF 像素坐标转经纬度

## 1. 这个转换解决什么问题

用户在 GeoTIFF 影像上点击一个位置时，程序最先得到的是像素坐标，例如：

```text
(1000, 800)
```

它只表示该位置位于影像的第 1000 列、第 800 行，不能单独说明它在现实世界中的位置。

要把它转换成无人机航点使用的经纬度，需要经过以下过程：

```text
像素坐标 (pixel_x, pixel_y)
        │
        │ GeoTransform
        ▼
影像坐标参考系统中的坐标 (X, Y)
        │
        │ CRS 坐标转换
        ▼
WGS84 经纬度 (longitude, latitude)
```

一句话总结：

> GeoTransform 负责把像素放到地图上，CRS 负责解释地图坐标并将其转换到地球上的经纬度。

---

## 2. 三种坐标分别是什么

### 2.1 像素坐标

像素坐标描述一个点在当前影像中的位置：

```text
pixel_x：从左向右的列位置
pixel_y：从上向下的行位置
```

影像左上角通常记为：

```text
(0, 0)
```

像素坐标只对当前影像有效。影像被裁剪、缩放或重新采样后，同一个地面位置的像素坐标可能发生变化。

严格来说，整数像素坐标 `(1000, 800)` 通常表示该像素的左上角；像素中心是：

```text
(1000.5, 800.5)
```

鼠标在图像上的点击位置可以是小数，因此也可以表达像素内部的位置。

### 2.2 投影坐标

地球表面是曲面，地图投影按照一定的数学规则，将一部分地球表面展开到二维平面。

展开后的位置通常使用：

```text
(X, Y)
```

表示，单位通常是米。例如：

```text
(500200, 3449840, WGS 84 / UTM zone 50N)
```

投影坐标便于计算地面距离、面积、缓冲区和像素分辨率，但坐标数字必须和对应的坐标参考系统一起使用。

单独的：

```text
(500200, 3449840)
```

不能唯一确定地球上的位置。完整表达应当包含 CRS：

```text
(500200, 3449840, EPSG:32650)
```

### 2.3 经纬度

经纬度使用角度描述地球椭球上的位置：

```text
longitude：经度，表示东西方向
latitude：纬度，表示南北方向
```

无人机航点和 GPS 常使用 WGS84 经纬度：

```text
(longitude, latitude, EPSG:4326)
```

经纬度单位是度，不是米。

---

## 3. CRS、WGS84 和 UTM 50N

### 3.1 CRS

CRS 是 Coordinate Reference System 的缩写，中文为“坐标参考系统”。

它是一套坐标说明规则，主要定义：

- 使用什么地球椭球和大地基准；
- 坐标原点和坐标轴方向；
- 坐标单位是度还是米；
- 是否使用地图投影；
- 坐标数字如何对应到地球上的位置。

CRS 大致可以分为两类：

```text
CRS
├─ 地理坐标参考系统：经度、纬度，单位通常是度
└─ 投影坐标参考系统：X、Y，单位通常是米
```

EPSG 编号可以理解为标准 CRS 的唯一编号。

### 3.2 WGS84

WGS84 是全球常用的大地坐标基准。常用的 WGS84 二维经纬度 CRS 为：

```text
EPSG:4326
```

它输出：

```text
(经度, 纬度)
```

### 3.3 UTM 50N

UTM 是一种地图投影系统。它将地球按经度划分为 60 个分区，每个分区宽约 6°。

`UTM 50N` 表示：

- `50`：第 50 个经度分区，大致覆盖东经 114°～120°；
- `N`：北半球，不是北纬 50°。

基于 WGS84 的 UTM 50N 完整名称和编号是：

```text
WGS 84 / UTM zone 50N
EPSG:32650
```

该坐标系输出平面 X、Y，单位为米。

---

## 4. GeoTIFF 必须提供的信息

要从像素位置计算经纬度，GeoTIFF 至少需要提供两类地理信息。

### 4.1 GeoTransform

GDAL 读取方式：

```python
geo_transform = ds.GetGeoTransform()
```

GeoTransform 包含 6 个参数：

```python
GT = (
    GT[0],  # 左上角地理 X
    GT[1],  # 像素宽度
    GT[2],  # 行变化引起的 X 变化，通常是旋转参数
    GT[3],  # 左上角地理 Y
    GT[4],  # 列变化引起的 Y 变化，通常是旋转参数
    GT[5],  # 像素高度，北向上的影像通常为负数
)
```

转换公式：

```text
X = GT[0] + pixel_x × GT[1] + pixel_y × GT[2]
Y = GT[3] + pixel_x × GT[4] + pixel_y × GT[5]
```

GeoTransform 通常由航测拼接或 GIS 软件在导出 GeoTIFF 时写入，例如 DJI Terra、Pix4D、Metashape、ArcGIS、QGIS 或 GDAL。

### 4.2 Projection / CRS

GDAL 读取方式：

```python
projection_wkt = ds.GetProjection()
```

返回值通常是一段 WKT 文本，用来说明 GeoTransform 计算出的 X、Y：

- 属于哪个坐标参考系统；
- 使用什么投影；
- 坐标单位是米还是度；
- 应当如何转换到 WGS84。

GeoTransform 和 Projection 缺一不可：

- 只有 GeoTransform，不知道 X、Y 属于什么坐标系；
- 只有 Projection，不知道每个像素对应到坐标系中的哪个位置。

---

## 5. 完整计算示例

假设一个 GeoTIFF 保存了以下信息：

```python
CRS = "WGS 84 / UTM zone 50N"  # EPSG:32650

GT = (
    500000,
    0.2,
    0,
    3450000,
    0,
    -0.2,
)
```

这表示：

- 影像左上角 `(0, 0)` 对应 UTM 坐标 `(500000, 3450000)`；
- 每个像素宽 0.2 米；
- 每个像素高 0.2 米；
- 影像没有旋转；
- 图像行号向下增加，而地图 Y 通常向上增加，因此 `GT[5]` 为负。

用户点击像素：

```text
(1000, 800)
```

### 5.1 像素坐标转 UTM 投影坐标

计算 X：

```text
X = 500000 + 1000 × 0.2 + 800 × 0
  = 500200
```

计算 Y：

```text
Y = 3450000 + 1000 × 0 + 800 × (-0.2)
  = 3449840
```

得到：

```text
(500200, 3449840, EPSG:32650)
```

它表示该点在 WGS84 / UTM 50N 平面坐标系中的位置，单位为米。

### 5.2 UTM 投影坐标转 WGS84 经纬度

使用 `pyproj`：

```python
import pyproj

transformer = pyproj.Transformer.from_crs(
    "EPSG:32650",
    "EPSG:4326",
    always_xy=True,
)

longitude, latitude = transformer.transform(500200, 3449840)
```

`pyproj` 根据 EPSG:32650 定义的 UTM 反投影规则，将 X、Y 转换为 WGS84 经度和纬度。

---

## 6. 如果 GeoTIFF 本身就是经纬度坐标系

有些 GeoTIFF 的源 CRS 已经是：

```text
WGS84 / EPSG:4326
```

例如：

```python
GT = (
    116.0,
    0.00001,
    0,
    40.0,
    0,
    -0.00001,
)
```

对于像素 `(1000, 800)`：

```text
longitude = 116.0 + 1000 × 0.00001
          = 116.01°

latitude  = 40.0 + 800 × (-0.00001)
          = 39.992°
```

此时 GeoTransform 的输出已经是 WGS84 经度和纬度。代码仍然可以统一通过 CRS Transformer 处理，Transformer 会完成必要的轴顺序和坐标系转换。

---

## 7. 本项目中的实现

### 7.1 导入 GeoTIFF

查看器加载影像时保存 GeoTransform，并根据 Projection 创建到 WGS84 的转换器：

```python
self.geo_transform = ds.GetGeoTransform()
self.transformer = build_transformer(ds.GetProjection())
```

对应代码：

- [`ui/viewer.py`](../ui/viewer.py)
- [`utils/geo.py`](../utils/geo.py)

### 7.2 获取点击位置的像素坐标

鼠标事件首先得到窗口坐标，随后通过 `mapToScene()` 转换为场景坐标：

```python
release_scene = self.mapToScene(event.pos())
pixel_x = release_scene.x()
pixel_y = release_scene.y()
```

本项目的场景坐标与原始影像像素坐标保持一致，因此 `pixel_x`、`pixel_y` 可以直接参与 GeoTransform 计算。

### 7.3 转换到经纬度

项目核心函数：

```python
def pixel_to_lon_lat(transform, transformer, px_x, px_y):
    geo_x = transform[0] + px_x * transform[1] + px_y * transform[2]
    geo_y = transform[3] + px_x * transform[4] + px_y * transform[5]
    lon, lat = transformer.transform(geo_x, geo_y)
    return lon, lat
```

完整调用链：

```text
鼠标窗口坐标
    ↓ QGraphicsView.mapToScene()
原始影像像素坐标
    ↓ GeoTransform
源 CRS 中的 X、Y
    ↓ pyproj Transformer
WGS84 经度、纬度
```

---

## 8. 如何查看一个 GeoTIFF 的地理参数

可以使用 GDAL 命令：

```powershell
gdalinfo 影像.tif
```

重点查看：

```text
Coordinate System is:
PROJCRS["WGS 84 / UTM zone 50N", ...]

Origin = (500000.000000, 3450000.000000)

Pixel Size = (0.200000, -0.200000)
```

对应关系：

```text
Coordinate System → Projection / CRS
Origin X          → GT[0]
Pixel Size X      → GT[1]
Origin Y          → GT[3]
Pixel Size Y      → GT[5]
```

如果影像包含旋转，`GT[2]` 和 `GT[4]` 也会参与转换。

---

## 9. 常见误区

### 误区一：投影坐标本身是全球唯一坐标

不准确。正确表达是：

```text
(X, Y, CRS)
```

三者结合才能确定位置。同一个地面位置在不同 CRS 中会得到不同的 X、Y。

### 误区二：所有 X、Y 都是经纬度

不正确。

- 地理 CRS 中的 X、Y 可能是经度、纬度，单位是度；
- 投影 CRS 中的 X、Y 通常是平面坐标，单位是米。

必须读取 Projection 才能确定。

### 误区三：只靠图片内容就能计算经纬度

不能。普通 TIFF、PNG 或 JPG 如果没有地理参考信息，仅凭像素 `(1000, 800)` 无法知道现实位置。

至少需要：

```text
GeoTransform + CRS
```

或者其他等价的定位信息，例如地面控制点、RPC、外部世界文件等。

### 误区四：WGS84 与国内互联网地图坐标完全相同

不一定。

- GPS 和无人机航点通常使用 WGS84；
- 高德、腾讯等国内互联网地图常涉及 GCJ-02；
- 百度地图常使用 BD-09。

GCJ-02、BD-09 存在额外偏移，不能仅通过普通 EPSG 坐标转换直接等同于 WGS84。

---

## 10. 最简记忆方式

```text
像素坐标：
这个点在图片的哪里？

GeoTransform：
从图片左上角出发，每移动一个像素，现实坐标变化多少？

CRS：
这些现实坐标数字使用哪套地球和地图规则？

经纬度：
最终这个点在地球上的经度和纬度是多少？
```

最终公式：

```text
像素坐标
  + GeoTransform
  = 源 CRS 坐标

源 CRS 坐标
  + CRS 转换
  = WGS84 经纬度
```
