from __future__ import annotations

import ast
import json
import math
import time
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from shapely.geometry import Point
from shapely.ops import unary_union


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

CRS_WGS84 = "EPSG:4326"
CRS_PROJECTED = "EPSG:32650"

GRAPH_PATHS = {
    "walk": DATA_DIR / "nanjing_walk.graphml",
    "bike": DATA_DIR / "nanjing_bike.graphml",
    "drive": DATA_DIR / "nanjing_drive.graphml",
}
POI_GRID_PATH = DATA_DIR / "amap_pois_grid.csv"
POI_FALLBACK_PATH = DATA_DIR / "amap_pois.csv"
POI_PATH = POI_GRID_PATH if POI_GRID_PATH.exists() else POI_FALLBACK_PATH

ISO_MINUTES = [5, 15, 25, 35, 45, 55, 65]
ACCESS_MINUTES = [10, 15, 25]
SERVICE_CATEGORIES = ["餐饮", "医院", "学校", "公园", "超市", "地铁接入点"]

MODE_LABELS = {"walk": "步行", "bike": "骑行", "drive": "驾车"}
MODE_COLORS = {"walk": "#2A9D8F", "bike": "#E9C46A", "drive": "#E76F51"}
ISO_COLORS = ["#2DC4B2", "#3BB3C3", "#669EC4", "#8B88B6", "#A2719B", "#AA5E79", "#A64B4B"]
ISO_LINE_WEIGHTS = {"walk": 1.5, "bike": 0.7, "drive": 0.5}

ISO_BUFFER_SETTINGS = {
    "walk": {"edge_m": 45, "node_m": 35, "origin_m": 70, "smooth_m": 20, "simplify_m": 12},
    "bike": {"edge_m": 70, "node_m": 55, "origin_m": 95, "smooth_m": 30, "simplify_m": 18},
    "drive": {"edge_m": 110, "node_m": 85, "origin_m": 140, "smooth_m": 45, "simplify_m": 30},
}

WALK_SPEED_BY_HIGHWAY = {
    "footway": 4.8,
    "pedestrian": 4.5,
    "path": 4.6,
    "steps": 2.8,
    "living_street": 4.2,
    "residential": 4.8,
    "service": 4.5,
    "cycleway": 4.8,
}
BIKE_SPEED_BY_HIGHWAY = {
    "cycleway": 17.0,
    "residential": 15.0,
    "living_street": 12.0,
    "tertiary": 16.0,
    "secondary": 15.0,
    "primary": 13.0,
    "service": 12.0,
    "path": 10.0,
    "footway": 8.0,
}
DRIVE_SPEED_BY_HIGHWAY = {
    "motorway": 80,
    "motorway_link": 55,
    "trunk": 65,
    "trunk_link": 50,
    "primary": 50,
    "primary_link": 40,
    "secondary": 40,
    "secondary_link": 35,
    "tertiary": 35,
    "tertiary_link": 30,
    "residential": 25,
    "living_street": 18,
    "unclassified": 30,
    "service": 18,
    "road": 30,
}

POI_BASE_WEIGHTS = {
    "餐饮": 0.75,
    "医院": 1.00,
    "学校": 0.85,
    "公园": 0.70,
    "超市": 0.80,
    "地铁接入点": 0.90,
}

graphs: dict[str, nx.MultiDiGraph] = {}
edge_proj_gdfs: dict[str, gpd.GeoDataFrame] = {}
node_proj_gdfs: dict[str, gpd.GeoDataFrame] = {}
poi_gdf: gpd.GeoDataFrame | None = None
poi_snapped_by_mode: dict[str, gpd.GeoDataFrame] = {}
startup_summary: dict[str, object] = {}


def normalize_osm_value(value) -> str:
    """将 GraphML 中可能出现的列表、空值或字符串统一转为便于判断的文本。"""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value)
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple)) and parsed:
                return str(parsed[0])
        except Exception:
            pass
    return text


def parse_maxspeed_kmh(value) -> float | None:
    text = normalize_osm_value(value).lower().strip()
    if not text or any(word in text for word in ["walk", "signals", "none"]):
        return None
    nums = []
    for token in text.replace(";", " ").replace(",", " ").split():
        cleaned = "".join(ch for ch in token if ch.isdigit() or ch == ".")
        if cleaned:
            try:
                nums.append(float(cleaned))
            except ValueError:
                pass
    if not nums:
        return None
    speed = max(nums)
    return speed * 1.60934 if "mph" in text else speed


def infer_speed_kmh(edge_data: dict, mode: str) -> float:
    highway = normalize_osm_value(edge_data.get("highway", ""))
    if mode == "walk":
        return WALK_SPEED_BY_HIGHWAY.get(highway, 4.8)
    if mode == "bike":
        return BIKE_SPEED_BY_HIGHWAY.get(highway, 14.0)
    maxspeed = parse_maxspeed_kmh(edge_data.get("maxspeed"))
    if maxspeed is not None and 5 <= maxspeed <= 130:
        return maxspeed
    return DRIVE_SPEED_BY_HIGHWAY.get(highway, 30)


def numeric_length(value) -> float:
    text = normalize_osm_value(value)
    try:
        return float(text)
    except (TypeError, ValueError):
        return float("nan")


def add_travel_time_weights(graph: nx.MultiDiGraph, mode: str) -> dict[str, float]:
    """按 notebook 中的分模式速度表，为每条边计算 travel_time 秒。"""
    speeds = []
    bad_edges = 0
    for _, _, _, data in graph.edges(keys=True, data=True):
        length = numeric_length(data.get("length"))
        if not np.isfinite(length) or length <= 0:
            data["travel_time"] = np.inf
            data["speed_kmh"] = np.nan
            bad_edges += 1
            continue
        speed = infer_speed_kmh(data, mode)
        data["speed_kmh"] = float(speed)
        data["travel_time"] = float(length) / (float(speed) / 3.6)
        speeds.append(float(speed))
    return {
        "bad_edges": int(bad_edges),
        "mean_speed_kmh": round(float(np.mean(speeds)), 2) if speeds else 0.0,
        "min_speed_kmh": round(float(np.min(speeds)), 2) if speeds else 0.0,
        "max_speed_kmh": round(float(np.max(speeds)), 2) if speeds else 0.0,
    }


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """高德坐标转 WGS84，用于与 OSM 路网保持一致。"""

    def out_of_china(x, y):
        return not (72.004 <= x <= 137.8347 and 0.8293 <= y <= 55.8271)

    def transform_lat(x, y):
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return ret

    def transform_lon(x, y):
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    if out_of_china(lon, lat):
        return lon, lat
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = transform_lat(lon - 105.0, lat - 35.0)
    dlon = transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrt_magic * math.cos(radlat) * math.pi)
    return lon * 2 - (lon + dlon), lat * 2 - (lat + dlat)


def point_gdf(lon: float, lat: float, crs: str = CRS_WGS84) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"id": [0]}, geometry=[Point(lon, lat)], crs=crs)


def projected_point_distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    a = point_gdf(lon1, lat1).to_crs(CRS_PROJECTED).geometry.iloc[0]
    b = point_gdf(lon2, lat2).to_crs(CRS_PROJECTED).geometry.iloc[0]
    return float(a.distance(b))


def nearest_node_with_distance(graph: nx.MultiDiGraph, lon: float, lat: float):
    node = ox.distance.nearest_nodes(graph, X=lon, Y=lat)
    data = graph.nodes[node]
    dist_m = projected_point_distance_m(lon, lat, float(data["x"]), float(data["y"]))
    return node, dist_m


def load_pois() -> gpd.GeoDataFrame:
    required_cols = ["POI名称", "所在区县", "详细地址", "经度", "纬度", "POI类型", "分析类别"]
    df = pd.read_csv(POI_PATH, encoding="utf-8-sig")
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"POI 文件缺少字段：{missing}")
    df["经度"] = pd.to_numeric(df["经度"], errors="coerce")
    df["纬度"] = pd.to_numeric(df["纬度"], errors="coerce")
    df = df.dropna(subset=["经度", "纬度"]).copy()
    df["分析类别"] = df["分析类别"].replace({"\u5730\u94c1\u7ad9": "地铁接入点"})
    converted = df.apply(lambda row: gcj02_to_wgs84(float(row["经度"]), float(row["纬度"])), axis=1)
    df["经度_wgs84"] = [xy[0] for xy in converted]
    df["纬度_wgs84"] = [xy[1] for xy in converted]
    return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["经度_wgs84"], df["纬度_wgs84"]), crs=CRS_WGS84)


def snap_pois_to_graph(graph: nx.MultiDiGraph, points: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if points.empty:
        out = points.copy()
        out["node_id"] = []
        out["snap_dist_m"] = []
        return out
    nodes = ox.distance.nearest_nodes(graph, X=points.geometry.x.to_numpy(), Y=points.geometry.y.to_numpy())
    out = points.copy().reset_index(drop=True)
    out["node_id"] = list(nodes)
    out["node_lon"] = [float(graph.nodes[n]["x"]) for n in out["node_id"]]
    out["node_lat"] = [float(graph.nodes[n]["y"]) for n in out["node_id"]]
    poi_proj = out.to_crs(CRS_PROJECTED).geometry
    node_proj = gpd.GeoSeries(gpd.points_from_xy(out["node_lon"], out["node_lat"]), crs=CRS_WGS84).to_crs(CRS_PROJECTED)
    out["snap_dist_m"] = poi_proj.distance(node_proj).astype(float)
    return out


def load_workspace_data() -> None:
    """启动时一次性加载路网和 POI，避免每个请求重复读取大文件。"""
    global poi_gdf, startup_summary

    missing = [str(path) for path in [*GRAPH_PATHS.values(), POI_PATH] if not path.exists()]
    if missing:
        raise FileNotFoundError("缺少必要数据文件：" + ", ".join(missing))

    start = time.perf_counter()
    graph_rows = []
    for mode, path in GRAPH_PATHS.items():
        graph = ox.load_graphml(path)
        crs_text = str(graph.graph.get("crs", "")).lower()
        if "4326" not in crs_text:
            raise ValueError(f"{MODE_LABELS[mode]}路网 CRS 不是 WGS84：{graph.graph.get('crs')}")

        weight_summary = add_travel_time_weights(graph, mode)
        graphs[mode] = graph

        edges = ox.graph_to_gdfs(graph, nodes=False, fill_edge_geometry=True)
        nodes = ox.graph_to_gdfs(graph, edges=False)
        if edges.crs is None:
            edges = edges.set_crs(CRS_WGS84)
        if nodes.crs is None:
            nodes = nodes.set_crs(CRS_WGS84)
        edge_proj_gdfs[mode] = edges.to_crs(CRS_PROJECTED)
        node_proj_gdfs[mode] = nodes.to_crs(CRS_PROJECTED)

        graph_rows.append(
            {
                "mode": mode,
                "mode_label": MODE_LABELS[mode],
                "nodes": int(graph.number_of_nodes()),
                "edges": int(graph.number_of_edges()),
                **weight_summary,
            }
        )

    poi_gdf = load_pois()
    for mode, graph in graphs.items():
        poi_snapped_by_mode[mode] = snap_pois_to_graph(graph, poi_gdf)

    category_counts = poi_gdf.groupby("分析类别").size().sort_values(ascending=False).to_dict()
    startup_summary = {
        "poi_path": str(POI_PATH.relative_to(BASE_DIR)),
        "poi_count": int(len(poi_gdf)),
        "poi_categories": {str(key): int(value) for key, value in category_counts.items()},
        "graphs": graph_rows,
        "load_seconds": round(time.perf_counter() - start, 2),
    }


def rounded_coord(value: float) -> float:
    """缓存键保留约 1 米精度，减少连续点击造成的重复计算。"""
    return round(float(value), 5)


@lru_cache(maxsize=48)
def travel_times_cached(mode: str, lon: float, lat: float, max_minutes: int):
    graph = graphs[mode]
    origin_node, snap_m = nearest_node_with_distance(graph, lon, lat)
    lengths = nx.single_source_dijkstra_path_length(
        graph,
        origin_node,
        cutoff=int(max_minutes) * 60,
        weight="travel_time",
    )
    return origin_node, float(snap_m), lengths


def assign_time_bucket(seconds: float, thresholds: list[int]) -> int | None:
    for minute in thresholds:
        if seconds <= minute * 60:
            return minute
    return None


def edge_reach_table(mode: str, lengths: dict, thresholds: list[int]) -> gpd.GeoDataFrame:
    edges = edge_proj_gdfs[mode].reset_index().copy()
    reach_seconds = []
    reach_bucket = []
    for row in edges[["u", "v"]].itertuples(index=False):
        sec = min(lengths.get(row.u, np.inf), lengths.get(row.v, np.inf))
        reach_seconds.append(sec)
        reach_bucket.append(assign_time_bucket(sec, thresholds))
    edges["reach_sec"] = reach_seconds
    edges["reach_bucket"] = reach_bucket
    return edges.dropna(subset=["reach_bucket"])


def node_reach_table(mode: str, lengths: dict, thresholds: list[int]) -> gpd.GeoDataFrame:
    nodes = node_proj_gdfs[mode].copy().reset_index()
    if "osmid" in nodes.columns:
        nodes = nodes.rename(columns={"osmid": "node"})
    elif "node" not in nodes.columns:
        nodes = nodes.rename(columns={nodes.columns[0]: "node"})
    nodes["reach_sec"] = nodes["node"].map(lambda node: lengths.get(node, np.inf))
    nodes["reach_bucket"] = nodes["reach_sec"].map(lambda sec: assign_time_bucket(sec, thresholds))
    return nodes.dropna(subset=["reach_bucket"])


def clean_geometry(geom):
    if geom is None or geom.is_empty:
        return geom
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def build_isochrone_polygons(mode: str, lon: float, lat: float, lengths: dict, thresholds: list[int]):
    """用可达道路和可达节点缓冲融合生成本地等时圈。"""
    start = time.perf_counter()
    settings = ISO_BUFFER_SETTINGS[mode]
    origin_proj = point_gdf(lon, lat).to_crs(CRS_PROJECTED).geometry.iloc[0]
    edges_reach = edge_reach_table(mode, lengths, thresholds)
    nodes_reach = node_reach_table(mode, lengths, thresholds)

    cumulative_geom = origin_proj.buffer(settings["origin_m"])
    previous_geom = None
    rows = []

    for minute in thresholds:
        edge_part = edges_reach[edges_reach["reach_bucket"] == minute]
        node_part = nodes_reach[nodes_reach["reach_bucket"] == minute]

        pieces = []
        if len(edge_part) > 0:
            pieces.append(unary_union(edge_part.geometry.buffer(settings["edge_m"])))
        if len(node_part) > 0:
            pieces.append(unary_union(node_part.geometry.buffer(settings["node_m"])))
        if pieces:
            cumulative_geom = cumulative_geom.union(unary_union(pieces))

        geom = cumulative_geom
        if settings["smooth_m"] > 0:
            geom = geom.buffer(settings["smooth_m"]).buffer(-settings["smooth_m"])
        if settings["simplify_m"] > 0:
            geom = geom.simplify(settings["simplify_m"], preserve_topology=True)
        if previous_geom is not None:
            geom = geom.union(previous_geom)
        if not (geom.contains(origin_proj) or geom.touches(origin_proj)):
            geom = geom.union(origin_proj.buffer(settings["origin_m"]))

        geom = clean_geometry(geom)
        rows.append(
            {
                "mode": mode,
                "minutes": int(minute),
                "geometry": geom,
                "reachable_nodes": int((nodes_reach["reach_sec"] <= minute * 60).sum()),
                "reachable_edges": int((edges_reach["reach_sec"] <= minute * 60).sum()),
                "area_km2": float(geom.area / 1_000_000),
            }
        )
        previous_geom = geom
        cumulative_geom = geom

    iso_proj = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS_PROJECTED)
    return iso_proj, round(time.perf_counter() - start, 2)


def isochrone_bands_geojson(iso_proj: gpd.GeoDataFrame) -> dict:
    """将累计等时圈转成非重叠时间带，前端每个时间带为一个完整图层。"""
    rows = []
    prev_geom = None
    prev_minute = None
    for idx, (_, row) in enumerate(iso_proj.sort_values("minutes").iterrows()):
        geom = row.geometry
        band_geom = geom if prev_geom is None else geom.difference(prev_geom)
        band_geom = clean_geometry(band_geom)
        label = f"{int(row['minutes'])} min" if prev_minute is None else f"{int(prev_minute)}-{int(row['minutes'])} min"
        if band_geom is not None and not band_geom.is_empty:
            rows.append(
                {
                    "minutes": int(row["minutes"]),
                    "label": label,
                    "color": ISO_COLORS[idx],
                    "weight": ISO_LINE_WEIGHTS[row["mode"]],
                    "reachable_nodes": int(row["reachable_nodes"]),
                    "reachable_edges": int(row["reachable_edges"]),
                    "area_km2": round(float(row["area_km2"]), 3),
                    "geometry": band_geom,
                }
            )
        prev_geom = geom
        prev_minute = row["minutes"]

    bands = gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS_PROJECTED).to_crs(CRS_WGS84)
    return json.loads(bands.to_json())


def gaussian_decay_seconds(seconds: float, cutoff: float) -> float:
    sigma = cutoff / 2.0
    return float(np.exp(-0.5 * (float(seconds) / sigma) ** 2))


def accessibility_table_for_mode(mode: str, lengths: dict) -> pd.DataFrame:
    snapped = poi_snapped_by_mode[mode]
    category_totals = snapped.groupby("分析类别").size().to_dict()
    node_seconds = snapped["node_id"].map(lengths).fillna(np.inf).to_numpy(dtype=float)
    categories = snapped["分析类别"].to_numpy()
    rows = []

    for minutes in ACCESS_MINUTES:
        cutoff = minutes * 60
        for category in SERVICE_CATEGORIES:
            cat_mask = categories == category
            cat_seconds = node_seconds[cat_mask]
            reachable = np.isfinite(cat_seconds) & (cat_seconds <= cutoff)
            raw_count = int(reachable.sum())
            if raw_count:
                decay_sum = float(np.exp(-0.5 * (cat_seconds[reachable] / (cutoff / 2.0)) ** 2).sum())
            else:
                decay_sum = 0.0
            total_count = int(category_totals.get(category, 0))
            coverage = raw_count / total_count * 100 if total_count else 0.0
            base_weight = POI_BASE_WEIGHTS.get(category, 0.7)
            weighted_decay_score = float(base_weight * decay_sum)
            compressed_score = float(base_weight * np.log1p(decay_sum))
            rows.append(
                {
                    "模式": MODE_LABELS[mode],
                    "mode": mode,
                    "时间阈值_min": int(minutes),
                    "类别": category,
                    "类别总数": total_count,
                    "可达POI数量": raw_count,
                    "类别覆盖率_%": coverage,
                    "距离衰减有效数量": decay_sum,
                    "时间衰减得分": weighted_decay_score,
                    "对数压缩得分": compressed_score,
                }
            )
    return pd.DataFrame(rows)


def accessibility_for_origin(lon: float, lat: float, selected_mode: str) -> tuple[pd.DataFrame, dict]:
    """按 notebook 当前口径：先生成全模式表，再用全表最大对数压缩得分统一标准化。"""
    all_tables = []
    snap_rows = {}
    for mode in ["walk", "bike", "drive"]:
        origin_node, snap_m, lengths = travel_times_cached(mode, rounded_coord(lon), rounded_coord(lat), max(ACCESS_MINUTES))
        snap_rows[mode] = {"origin_node": str(origin_node), "snap_distance_m": round(snap_m, 2)}
        all_tables.append(accessibility_table_for_mode(mode, lengths))

    full_df = pd.concat(all_tables, ignore_index=True)
    global_max = float(full_df["对数压缩得分"].max())
    if global_max > 0:
        full_df["标准化可达性指数_0_100"] = full_df["对数压缩得分"] / global_max * 100
    else:
        full_df["标准化可达性指数_0_100"] = 0.0

    selected = full_df[full_df["mode"] == selected_mode].copy()
    return selected, {
        "snap": snap_rows[selected_mode],
        "normalization": "当前出发点下三种模式、三个时间阈值和六类 POI 的全表最大对数压缩得分",
        "global_max_log_score": round(global_max, 4),
    }


def parse_request_float(name: str) -> float:
    value = request.args.get(name)
    if value is None:
        raise ValueError(f"缺少参数：{name}")
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"参数 {name} 不是有效数字") from exc


def parse_mode() -> str:
    mode = request.args.get("mode", "walk")
    if mode not in MODE_LABELS:
        raise ValueError("mode 必须为 walk、bike 或 drive")
    return mode


app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "summary": startup_summary})


@app.route("/api/isochrone")
def api_isochrone():
    try:
        lon = parse_request_float("lon")
        lat = parse_request_float("lat")
        mode = parse_mode()
        minutes = int(request.args.get("minutes", max(ISO_MINUTES)))
        if minutes not in ISO_MINUTES:
            raise ValueError("minutes 必须为 5、15、25、35、45、55 或 65")

        thresholds = [minute for minute in ISO_MINUTES if minute <= minutes]
        origin_node, snap_m, lengths = travel_times_cached(mode, rounded_coord(lon), rounded_coord(lat), minutes)
        iso_proj, elapsed = build_isochrone_polygons(mode, lon, lat, lengths, thresholds)
        geojson = isochrone_bands_geojson(iso_proj)
        final_row = iso_proj.sort_values("minutes").iloc[-1]

        return jsonify(
            {
                "mode": mode,
                "mode_label": MODE_LABELS[mode],
                "origin": {"lon": lon, "lat": lat},
                "minutes": minutes,
                "thresholds": thresholds,
                "snap": {"origin_node": str(origin_node), "snap_distance_m": round(snap_m, 2)},
                "stats": {
                    "reachable_nodes": int(final_row["reachable_nodes"]),
                    "reachable_edges": int(final_row["reachable_edges"]),
                    "area_km2": round(float(final_row["area_km2"]), 3),
                    "elapsed_seconds": elapsed,
                },
                "geojson": geojson,
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"等时圈计算失败：{exc}"}), 500


@app.route("/api/accessibility")
def api_accessibility():
    try:
        lon = parse_request_float("lon")
        lat = parse_request_float("lat")
        mode = parse_mode()
        df, meta = accessibility_for_origin(lon, lat, mode)

        score_cols = [
            "类别覆盖率_%",
            "距离衰减有效数量",
            "时间衰减得分",
            "对数压缩得分",
            "标准化可达性指数_0_100",
        ]
        df[score_cols] = df[score_cols].round(2)
        table_cols = [
            "时间阈值_min",
            "类别",
            "类别总数",
            "可达POI数量",
            "类别覆盖率_%",
            "距离衰减有效数量",
            "时间衰减得分",
            "对数压缩得分",
            "标准化可达性指数_0_100",
        ]
        table = json.loads(df[table_cols].to_json(orient="records", force_ascii=False))

        radar = {"labels": SERVICE_CATEGORIES, "datasets": []}
        radar_colors = {10: "#2DC4B2", 15: "#669EC4", 25: "#A64B4B"}
        for minutes in ACCESS_MINUTES:
            sub = df[df["时间阈值_min"] == minutes].set_index("类别").reindex(SERVICE_CATEGORIES)
            radar["datasets"].append(
                {
                    "label": f"{minutes} min",
                    "data": sub["标准化可达性指数_0_100"].fillna(0).round(2).tolist(),
                    "borderColor": radar_colors[minutes],
                    "backgroundColor": radar_colors[minutes] + "33",
                }
            )

        return jsonify(
            {
                "mode": mode,
                "mode_label": MODE_LABELS[mode],
                "origin": {"lon": lon, "lat": lat},
                "table": table,
                "radar": radar,
                "meta": meta,
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"POI 可达性计算失败：{exc}"}), 500


load_workspace_data()

import os


if __name__ == "__main__":
    print("南京多模式空间可达性网站后端已启动")
    print(f"POI 文件：{startup_summary.get('poi_path')}")
    print(f"启动加载耗时：{startup_summary.get('load_seconds')} 秒")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
