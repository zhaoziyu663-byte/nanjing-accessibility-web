# 南京市多模式空间可达性分析网站

本目录提供课程作业网站版本，包含：

- `app.py`：Flask 后端，负责读取本地 OSM GraphML 路网和 POI 数据，计算等时圈与 POI 可达性指标。
- `index.html`：Leaflet 前端页面，支持点击地图设置出发点、选择出行模式、展示等时圈、表格和雷达图。
- `requirements.txt`：运行所需 Python 依赖。

## 运行方式

```bash
pip install -r requirements.txt
python app.py
```

浏览器打开：

```text
http://127.0.0.1:5000
```

## 数据文件

网站默认读取当前工作区的本地数据：

- `data/nanjing_walk.graphml`
- `data/nanjing_bike.graphml`
- `data/nanjing_drive.graphml`
- 优先读取 `data/amap_pois_grid.csv`，不存在时回退到 `data/amap_pois.csv得到。后端根据可达道路和可达节点在投影坐标系下进行缓冲、融合、平滑和简化，生成非重叠时间带 GeoJSON。

POI 可达性分析输出 10、15、25 min 三个阈值下六类设施结果。雷达图使用当前 notebook 的指标口径：

```text
标准化可达性指数 = 当前项对数压缩得分 / 全表最大对数压缩得分 × 100
```

网站仅提供等时圈分析和 POI 可达性评分两类功能。

## 性能提示

首次启动会加载三种 GraphML 路网、构建投影缓存并将 POI 吸附到三种路网，可能需要等待一段时间。每次更换出发点后，后端会重新执行 Dijkstra；同一出发点、同一模式和同一时间阈值的结果会被简单缓存。
