# 北京地铁 · 候车看板

本地单页看板，实时抓取北京地铁 PIS 数据，给出「什么时候离开工位最划算」。

- **全网任意站点、任意方向可选**：顶部下拉依次选 线路 → 站点 → 方向
- **选择自动记忆**：上次选过的线路/站点/方向/步行参数存在浏览器 localStorage，下次打开自动恢复
- **常用收藏**：点「☆ 收藏当前」把"站点+方向"存为常用，以胶囊按钮常驻顶部，一键切换
- **局域网可访问**：默认监听 0.0.0.0，同事/手机用 `http://<本机IP>:8791/` 直接打开
- 默认（未选择时）：北神树（17 号线）· 北行 · 终点未来科学城北

## 启动

本仓库**不包含任何密钥**。签名密钥需自行从上游 H5 页面的公开前端产物中取得（见下）。

```bash
git clone https://github.com/zyjarge/bj-subway-wait-dashboard.git
cd bj-subway-wait-dashboard

python3 tools/fetch_key.py            # 自动提取密钥（公开前端产物，任何人可下）
export RUUBYPAY_KEY_HEX=<上一步打印的值>

./dashboard/start.sh                  # 默认 8791 端口，自动打开浏览器
./dashboard/start.sh 8899             # 换端口
```

停止：`pkill -f "server[.]py --port 8791"`
（注意用 `[.]` 转义，否则 `pgrep -f` 会匹配到 shell 自身命令行把自己杀掉）

### 关于密钥

- 密钥是上游 H5 页面（`web.ruubypay.com/web/bj-subway-map-h5-output/`）前端构建产物里的
  一个 HMAC 常量，任何人打开该网页都能下载到这些 JS 文件
- 本项目只公开**签名算法与业务流程**（见下），不保存密钥值
- 上游更换密钥后，重新运行 `tools/fetch_key.py` 即可
- 也可以手动设置：`export RUUBYPAY_KEY_HEX=<40位十六进制串>`

### 签名算法（公开部分）

```
body  = {业务参数..., "ts": "<unix秒>"}
mac   = HMAC_SHA1(key = Hex.parse(KEY_HEX), msg = normalize(body)).hex().upper()
body += {"mac": mac}
POST  https://api.ruubypay.com/marketingpis/appSides/<接口名>

normalize(obj):
    list  -> "[" + "".join(normalize(x) for x in obj) + "]"
    标量   -> str(obj)
    dict  -> "".join(f"{k}{normalize(v)}||" for k in sorted(keys, key=str.lower))
```

## 看板内容（四个页签）

| 页签 | 区块 |
|---|---|
| 🚇 候车 | 下一班（大字倒计时、到站时刻、最晚离开时刻）；现在就走的判定（模拟时钟 + 适宜度扇形表盘 + 营运时段色带）；建议离开工位的时刻表（点行复制） |
| 📊 卡点分析 | 卡点 vs 随缘走的期望候车对比；缓冲 b 逐档对比表（含最优档高亮与当前档标记） |
| ⚠️ 运行信息 | 限流事件：全网生效数汇总 + 逐条结构化卡片（车站、所属线路、时段、描述，本线路高亮）；首末班车 |
| ⚙️ 设置 | 步行/缓冲/σ、适宜度阈值（绿/黄）参数（带说明，自动记忆）；常用收藏管理；数据源说明 |

页签位置同样记忆在浏览器里，下次打开停留在上次页面。

### 适宜度着色模型（`GET /api/dayfit`）

对每一分钟 d：若此刻离开工位，走到站台（d + walk）时能赶上的下一班车是 T，
则站台候车 = T − (d + walk)：

- 候车 ≤ 绿阈值（默认 2 分）→ 🟢 最佳出发窗口
- 候车 ≤ 黄阈值（默认 5 分）→ 🟡 可以等，但别太早走
- 更久 → 🔴 现在走纯属干等
- 未运营 / 已收车 → ⚪ 无车

表盘 60 个扇形 = 分钟刻度（数字 8~10 的位置对应 40~50 分），白点为当前时刻；
下方色带只显示当日营运时段，白竖线 = 最佳离开时刻（到站时间 − 步行 − 缓冲），
蓝线 = 现在。

## 核心结论（由蒙特卡洛仿真得出）

错一班的代价 = **整整一个发车间隔**（晚高峰 6–8 分钟）。因此：

- 步行很稳（σ≈1 分钟）→ **卡点值得**，期望候车可从 ~2.8 分压到 ~1.6 分
- 步行波动大（σ≥3 分钟）→ **卡点不划算**，错车率 30%+，期望候车反而高于随机出发

规则：**离开工位时刻 = 车次到站时刻 − (步行 + 缓冲)**，缓冲取 1–3 分钟。

## 数据源

RuubyPay PIS（`https://web.ruubypay.com/web/bj-subway-map-h5-output/`）：

- `POST /marketingpis/appSides/getRealtimeStationInfo` — 实时到站
- `POST /marketingpis/appSides/getStationTimetable` — 时刻表
- `POST /marketingpis/appSides/getStationFirstLastTrain` — 首末班车
- `GET /PIS/station/normalEvent/v4` — 限流事件（无需签名）

请求体带 `ts` + `mac`（HMAC-SHA1），密钥从环境变量 `RUUBYPAY_KEY_HEX` 读取，
实现见 [ruubypay_pis.py](ruubypay_pis.py)；密钥提取见 [tools/fetch_key.py](tools/fetch_key.py)。

**注意**：`fullLoadRate` 字段全网恒为 50，是占位默认值，**不能**当作拥挤度；
判断拥挤请看限流事件。

## 文件

- [dashboard/server.py](dashboard/server.py) — 后端（Python 标准库，无第三方依赖）
- [dashboard/index.html](dashboard/index.html) — 前端单页（无外部依赖）
- [dashboard/start.sh](dashboard/start.sh) — 启动脚本
- [ruubypay_pis.py](ruubypay_pis.py) — PIS 接口客户端（签名算法 + 站点索引）
- [optimal_departure.py](optimal_departure.py) — 蒙特卡洛仿真
- [tools/fetch_key.py](tools/fetch_key.py) — 从公开前端产物提取签名密钥
