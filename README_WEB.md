# 南京市多模式空间可达性分析网站

这是一个面向课程作业展示的 Flask 动态网站。前端使用 Leaflet 展示地图，后端读取本地 OSM GraphML 路网和高德 POI 数据，实时计算等时圈与 POI 可达性指标。

## 项目文件

- `app.py`：Flask 后端，提供 `/`、`/api/health`、`/api/isochrone`、`/api/accessibility`。
- `index.html`：前端页面，支持地图点击选点、模式选择、等时圈展示、POI 表格和雷达图。
- `requirements.txt`：Python 依赖。
- `Dockerfile`：Hugging Face Spaces Docker Space 构建文件。
- `data/`：部署运行所需数据，不能忽略或删除。

## 数据文件

网站默认读取：

- `data/nanjing_walk.graphml`
- `data/nanjing_bike.graphml`
- `data/nanjing_drive.graphml`
- 优先读取 `data/amap_pois_grid.csv`，不存在时回退到 `data/amap_pois.csv`

高德 POI 坐标会先转换到 WGS84，再与 OSM 路网进行空间匹配。距离、缓冲和面积计算在 `EPSG:32650` 中完成，返回给前端前再转回 WGS84。

## 方法说明

等时圈由本地 GraphML 路网、`travel_time` 阻抗和 NetworkX Dijkstra 计算得到。后端根据可达道路和可达节点在投影坐标系下进行缓冲、融合、平滑和简化，生成非重叠时间带 GeoJSON。

POI 可达性分析输出 10、15、25 min 三个阈值下六类设施结果。雷达图使用当前 notebook 的指标口径：

```text
标准化可达性指数 = 当前项对数压缩得分 / 全表最大对数压缩得分 × 100
```

网站仅提供等时圈分析和 POI 可达性评分两类功能。

## 本地运行

```bash
pip install -r requirements.txt
python app.py
```

默认端口为 `7860`，也可以通过环境变量 `PORT` 指定端口。

## Hugging Face Spaces 部署

1. 在 Hugging Face Spaces 创建新 Space。
2. Space SDK 选择 `Docker`。
3. 将本仓库推送到 Space 仓库，确保 `Dockerfile`、`app.py`、`index.html`、`requirements.txt` 和 `data/` 均已提交。
4. 部署完成后，访问 Space 提供的公开 URL。
5. 可先访问 `/api/health` 检查后端是否成功加载路网和 POI 数据。

首次访问或首次构建后启动可能较慢，因为后端需要加载三种 GraphML 路网、构建投影缓存，并将 POI 吸附到三种路网。
