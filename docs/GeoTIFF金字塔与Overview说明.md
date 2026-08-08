# GeoTIFF 金字塔与 Overview 说明

本文介绍 Overview 和影像金字塔的基本概念、金字塔在 GeoTIFF 中的存储方式，以及 UavTool 对金字塔的构建和使用流程。

## 1. Overview 是什么

Overview 可以理解为原始影像的低分辨率缩略层。

假设原始影像尺寸为 `12000 × 8000`，不同 Overview 的尺寸大致如下：

| 层级 | 尺寸示例 | 相对于原图的像素数量 |
| --- | ---: | ---: |
| 原始影像 | 12000 × 8000 | 100% |
| 4 倍 Overview | 3000 × 2000 | 6.25% |
| 8 倍 Overview | 1500 × 1000 | 1.56% |
| 16 倍 Overview | 750 × 500 | 0.39% |
| 32 倍 Overview | 约 375 × 250 | 约 0.10% |
| 64 倍 Overview | 约 188 × 125 | 约 0.025% |

这里的“4 倍”不是将影像放大四倍，而是将宽度和高度分别缩小到原来的 `1/4`。

一个 Overview 表示一个缩略层。GDAL 的 `GetOverviewCount()` 只统计缩略层，不统计原始影像。因此，UavTool 当前构建完成后，该方法通常返回 `5`，实际包含：

```text
1 个原始分辨率层 + 5 个 Overview = 6 个分辨率层
```

## 2. 影像金字塔是什么

由多个不同分辨率的 Overview 组成的多尺度影像结构称为影像金字塔。

```text
原始影像          12000 × 8000
  ├─ 4 倍 Overview      3000 × 2000
  ├─ 8 倍 Overview      1500 × 1000
  ├─ 16 倍 Overview      750 × 500
  ├─ 32 倍 Overview      约 375 × 250
  └─ 64 倍 Overview      约 188 × 125
```

影像分辨率逐级降低，结构类似一座金字塔，因此称为影像金字塔。

### 2.1 为什么需要影像金字塔

当一幅遥感影像包含数亿甚至数十亿个像素时，如果每次查看全图都读取原始分辨率数据，会产生大量磁盘读取、内存占用和图像缩放计算。

构建金字塔后，软件可以根据当前显示比例选择合适的分辨率：

- 查看整幅影像时，读取尺寸最小的 Overview。
- 使用中等缩放比例时，读取中间层 Overview。
- 放大查看细节时，读取高分辨率 Overview 或原始影像。
- 平移影像时，只读取当前窗口覆盖的局部范围。

其原理与在线地图相似：查看全国范围时使用低分辨率地图，放大到街道后才读取高分辨率数据。

金字塔不会提高影像精度，它的作用是使用额外的磁盘空间换取更快的浏览和缩放速度。

## 3. 金字塔数据在 GeoTIFF 中如何存储

GeoTIFF 的原始像素通常按条带或瓦片存储。构建内部金字塔后，同一个 TIF 文件中会增加多组低分辨率影像数据。

其逻辑结构大致如下：

```text
image.tif
├─ 坐标系、仿射变换等 GeoTIFF 元数据
├─ 原始影像数据
├─ 4 倍 Overview
├─ 8 倍 Overview
├─ 16 倍 Overview
├─ 32 倍 Overview
└─ 64 倍 Overview
```

在 TIFF 内部，这些缩略层表现为额外的 TIFF 图像目录（IFD）和相应的像素数据块。它们不是在每个原始像素中增加额外字段，而是保存为独立的低分辨率影像层。

### 3.1 内部金字塔

UavTool 以更新模式打开影像：

```python
ds = gdal.Open(tif_path, gdal.GA_Update)
```

然后调用：

```python
ds.BuildOverviews(
    "AVERAGE",
    [4, 8, 16, 32, 64],
)
```

因此，生成的 Overview 通常直接写入原始 TIF，属于内部金字塔。构建完成后，一般不会在旁边生成单独的 `.ovr` 文件。

内部金字塔具有以下特点：

- 原始 TIF 文件会增大。
- TIF 和金字塔不会因移动文件而分离。
- 构建时要求文件具有写入权限。
- 构建过程会修改原始 TIF，重要影像建议提前备份。

### 3.2 外部金字塔

当影像以只读模式打开，或者驱动不支持内部 Overview 时，GDAL 也可能将金字塔保存为与原图同名的 `.ovr` 文件：

```text
image.tif
image.tif.ovr
```

这种方式称为外部金字塔。UavTool 当前的金字塔构建功能采用可写模式打开 GeoTIFF，主要生成内部金字塔。

### 3.3 Overview 压缩

UavTool 在构建前设置：

```python
gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")
```

因此 Overview 使用 DEFLATE 压缩，以减少新增的存储空间。

当前 `4、8、16、32、64` 这组层级如果完全不压缩，所有 Overview 的额外像素数量之和约为原图的 8.33%；原来的 7 层配置约为三分之一。实际文件增长量取决于：

- 波段数量。
- 像素数据类型。
- 影像内容和纹理复杂程度。
- DEFLATE 的实际压缩率。
- 原始影像采用条带还是瓦片存储。

## 4. Overview 像素如何生成

UavTool 使用 `AVERAGE` 重采样算法构建金字塔。

例如，2 倍 Overview 中的一个像素，大致由原始影像对应的 `2 × 2` 像素求平均得到：

```text
原始像素：

10  20
30  40

Overview 像素：

(10 + 20 + 30 + 40) / 4 = 25
```

`AVERAGE` 适合普通航空影像、卫星影像等连续值遥感数据。

如果处理的是土地类别、语义分割标签或离散掩膜，则通常应该使用 `NEAREST`，避免类别编号被平均后产生无效的新类别。

## 5. 如何检查影像是否已有金字塔

UavTool 通过第一个波段的 Overview 数量判断影像是否包含金字塔：

```python
from osgeo import gdal

gdal.UseExceptions()

tif_path = r"D:\data\image.tif"
ds = gdal.Open(tif_path, gdal.GA_ReadOnly)

if ds is None:
    raise RuntimeError("无法打开 TIF")

if ds.RasterCount <= 0:
    raise RuntimeError("影像波段为空")

band = ds.GetRasterBand(1)
overview_count = band.GetOverviewCount()

if overview_count > 0:
    print(f"影像已有金字塔，共 {overview_count} 级")
else:
    print("影像没有金字塔")

ds = None
```

如果需要查看每一级 Overview 的尺寸：

```python
band = ds.GetRasterBand(1)
overview_count = band.GetOverviewCount()

for index in range(overview_count):
    overview = band.GetOverview(index)
    print(
        f"第 {index + 1} 级："
        f"{overview.XSize} × {overview.YSize}"
    )
```

项目中的检查逻辑位于：

```text
logic/pyramid_builder.py
```

## 6. 如何构建 4～64 倍的 5 级轻量 Overview

UavTool 使用以下层级：

```python
factors = [4, 8, 16, 32, 64]
```

这组轻量层级不包含占用最大的 2 倍 Overview。其未压缩像素量约为
原图的 8.33%，而原来的连续 7 层约为 33.33%，可以明显降低金字塔带来的
额外磁盘占用。代价是接近原始分辨率缩放时，需要从原图读取更多数据。

完整的基本构建示例如下：

```python
from osgeo import gdal

gdal.UseExceptions()

tif_path = r"D:\data\image.tif"

gdal.SetConfigOption("COMPRESS_OVERVIEW", "DEFLATE")

ds = gdal.Open(tif_path, gdal.GA_Update)
if ds is None:
    raise RuntimeError("无法以读写方式打开 TIF")

factors = [4, 8, 16, 32, 64]

result = ds.BuildOverviews(
    "AVERAGE",
    factors,
)

if result != 0:
    raise RuntimeError("金字塔构建失败")

ds.FlushCache()
ds = None

print("5 级轻量金字塔构建完成")
```

构建完成后，可以重新打开影像进行验证：

```python
ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
count = ds.GetRasterBand(1).GetOverviewCount()
print(f"实际生成 {count} 级 Overview")
ds = None
```

UavTool 在后台线程中执行构建操作，并通过进度条显示 GDAL 返回的处理进度，避免长时间阻塞用户界面。

项目中的构建逻辑位于：

```text
logic/pyramid_builder.py
```

## 7. UavTool 如何使用金字塔

### 7.1 导入时检查

软件导入 GeoTIFF 时会读取第一个波段的 Overview 数量：

```python
band = ds.GetRasterBand(1)
overview_count = band.GetOverviewCount()
```

在航线绘制页面，影像必须包含 Overview。没有金字塔时，页面会拒绝导入，并提示用户先进入“金字塔构建”页面进行处理。

对于其他影像页面，软件可以在部分情况下从原始影像生成临时低分辨率预览；但对于大幅面影像，提前构建金字塔仍然能够显著改善浏览性能。

### 7.2 初始全图显示

影像刚打开时，软件选择最后一级 Overview：

```python
band = ds.GetRasterBand(1)
overview_index = band.GetOverviewCount() - 1
overview = band.GetOverview(overview_index)
```

最后一级通常是 128 倍 Overview，像素数量最少，因此适合快速显示影像全貌。

软件将该 Overview 映射回原图的场景尺寸，作为始终存在的低分辨率底图。

### 7.3 缩放和平移时动态读取

当用户缩放、旋转或平移影像时，软件会：

1. 计算当前窗口四个角在原图坐标系中的位置。
2. 得到当前可见的原图像素范围 `x、y、w、h`。
3. 根据窗口大小和当前缩放比例计算实际需要显示的尺寸。
4. 只读取当前可见区域，并预留少量边缘缓冲。
5. 将读取结果覆盖到底图之上。

核心读取方式类似：

```python
arr = band.ReadAsArray(
    x,
    y,
    w,
    h,
    buf_xsize=target_w,
    buf_ysize=target_h,
)
```

当 `target_w`、`target_h` 明显小于原始读取范围时，GDAL 通常会自动选择最合适的 Overview，从而避免读取和缩放大量原始像素。

整体流程如下：

```text
用户缩放、旋转或平移
          ↓
计算当前窗口对应的原图范围
          ↓
计算屏幕实际需要的输出尺寸
          ↓
GDAL 从合适的 Overview 或原图读取局部数据
          ↓
执行 NoData 过滤和显示拉伸
          ↓
组合 RGB 或多光谱显示波段
          ↓
更新界面上的局部清晰影像
```

### 7.4 显示层结构

UavTool 的影像查看器主要包含两层：

```text
高层：当前视窗动态读取的局部影像
低层：最后一级 Overview 生成的全图底图
```

当动态局部影像正在加载时，下面的全图底图仍然存在，因此用户平移和缩放时不会看到完全空白的画面。

### 7.5 显示拉伸与波段组合

软件不仅使用金字塔加速读取，还会：

- 根据 NoData 值排除无效像素。
- 使用 2%～98% 分位数进行显示拉伸。
- 支持标准 RGB `1,2,3` 波段组合。
- 支持多光谱 `3,2,1` 波段组合。
- 将处理结果转换为 8 位 RGB 图像显示。

分位数范围优先从最小的 Overview 计算，因为该层数据量小，可以快速估算整幅影像的显示范围。

## 8. 软件中的完整工作流程

```text
选择 GeoTIFF
      ↓
检查第一个波段的 Overview 数量
      ↓
是否已有金字塔？
  ├─ 是：直接进入影像浏览
  └─ 否：进入“金字塔构建”页面
              ↓
      以 GA_Update 模式打开 TIF
              ↓
      构建 4、8、16、32、64 级 Overview
              ↓
      使用 DEFLATE 压缩并写入原始 TIF
              ↓
      重新导入影像
              ↓
      全图使用最小 Overview
              ↓
      缩放和平移时动态读取合适层级和局部区域
```

## 9. 总结

- Overview 是原始影像的一个低分辨率缩略层。
- 多个 Overview 与原始影像共同组成影像金字塔。
- UavTool 构建 `4、8、16、32、64` 共 5 级轻量 Overview。
- Overview 使用 `AVERAGE` 重采样和 DEFLATE 压缩。
- 金字塔通常作为内部 Overview 写入原始 GeoTIFF。
- 全图显示时使用最小的 Overview。
- 缩放和平移时只读取当前可见区域，GDAL根据输出尺寸选择合适的分辨率。
- 金字塔不会提高影像精度，其主要作用是提升大幅面影像的浏览和交互性能。
