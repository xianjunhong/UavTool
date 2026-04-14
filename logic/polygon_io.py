import os
from typing import Dict, List, Sequence, Tuple

from utils.env_setup import configure_runtime_env


def _set_traditional_axis_order(srs):
	# GDAL3 默认可能使用 authority axis order，这里统一为传统 GIS 顺序 (x, y)。
	try:
		from osgeo import osr
		if srs is not None and hasattr(srs, "SetAxisMappingStrategy"):
			srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
	except Exception:
		pass


def _cleanup_shapefile_sidecars(shp_path: str):
	base, _ = os.path.splitext(shp_path)
	for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".fix"]:
		p = base + ext
		if os.path.exists(p):
			os.remove(p)


def save_polygons_to_shapefile(
	shp_path: str,
	polygons: Sequence[Dict],
	target_wkt: str,
):
	if not polygons:
		raise ValueError("没有可保存的小区多边形")

	if not shp_path.lower().endswith(".shp"):
		shp_path += ".shp"

	out_dir = os.path.dirname(shp_path)
	if out_dir:
		os.makedirs(out_dir, exist_ok=True)

	configure_runtime_env()
	from osgeo import ogr, osr

	ogr.UseExceptions()

	driver = ogr.GetDriverByName("ESRI Shapefile")
	if driver is None:
		raise RuntimeError("未找到 ESRI Shapefile 驱动")

	_cleanup_shapefile_sidecars(shp_path)

	ds = driver.CreateDataSource(shp_path)
	if ds is None:
		raise RuntimeError("创建 Shapefile 失败")

	srs = None
	if target_wkt:
		srs = osr.SpatialReference()
		srs.ImportFromWkt(target_wkt)
		_set_traditional_axis_order(srs)

	layer = ds.CreateLayer("plots", srs=srs, geom_type=ogr.wkbPolygon)
	layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))

	defn = layer.GetLayerDefn()

	for idx, poly in enumerate(polygons, start=1):
		name = str(poly.get("name") or f"plot_{idx}")
		coords = list(poly.get("geo_points") or [])
		if len(coords) < 3:
			continue

		ring = ogr.Geometry(ogr.wkbLinearRing)
		for x, y in coords:
			ring.AddPoint(float(x), float(y))
		x0, y0 = coords[0]
		x1, y1 = coords[-1]
		if abs(float(x0) - float(x1)) > 1e-10 or abs(float(y0) - float(y1)) > 1e-10:
			ring.AddPoint(float(x0), float(y0))

		geom = ogr.Geometry(ogr.wkbPolygon)
		geom.AddGeometry(ring)

		feat = ogr.Feature(defn)
		feat.SetField("name", name)
		feat.SetGeometry(geom)
		layer.CreateFeature(feat)
		feat = None

	layer = None
	ds = None

	if not os.path.exists(shp_path):
		raise RuntimeError("Shapefile 保存失败：未生成 .shp 文件")

	return shp_path


def load_polygons_from_vector(vector_path: str, target_wkt: str = "") -> List[Dict]:
	configure_runtime_env()
	from osgeo import ogr, osr

	ogr.UseExceptions()

	ds = ogr.Open(vector_path)
	if ds is None:
		raise RuntimeError("无法打开矢量文件")

	layer = ds.GetLayer(0)
	if layer is None:
		raise RuntimeError("矢量文件中不存在图层")

	src_srs = layer.GetSpatialRef()
	_set_traditional_axis_order(src_srs)
	dst_srs = None
	coord_tx = None
	if target_wkt:
		dst_srs = osr.SpatialReference()
		dst_srs.ImportFromWkt(target_wkt)
		_set_traditional_axis_order(dst_srs)
	if src_srs is not None and dst_srs is not None and not src_srs.IsSame(dst_srs):
		coord_tx = osr.CoordinateTransformation(src_srs, dst_srs)

	out: List[Dict] = []

	for feat in layer:
		geom = feat.GetGeometryRef()
		if geom is None:
			continue

		name = ""
		name_idx = feat.GetFieldIndex("name")
		if name_idx >= 0:
			name = str(feat.GetField("name") or "")

		polygons = []
		gtype = geom.GetGeometryType()
		if gtype in (ogr.wkbPolygon, ogr.wkbPolygon25D):
			polygons = [geom]
		elif gtype in (ogr.wkbMultiPolygon, ogr.wkbMultiPolygon25D):
			polygons = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
		else:
			continue

		for pidx, poly in enumerate(polygons, start=1):
			ring = poly.GetGeometryRef(0)
			if ring is None:
				continue

			pts: List[Tuple[float, float]] = []
			pt_count = ring.GetPointCount()
			for i in range(pt_count):
				x, y, _ = ring.GetPoint(i)
				if coord_tx is not None:
					x, y, _ = coord_tx.TransformPoint(x, y)
				pts.append((float(x), float(y)))

			if len(pts) >= 2:
				x0, y0 = pts[0]
				x1, y1 = pts[-1]
				if abs(x0 - x1) < 1e-10 and abs(y0 - y1) < 1e-10:
					pts = pts[:-1]

			if len(pts) < 3:
				continue

			poly_name = name or f"plot_{len(out) + 1}"
			if len(polygons) > 1:
				poly_name = f"{poly_name}_{pidx}"

			out.append({"name": poly_name, "geo_points": pts})

	layer = None
	ds = None
	return out
