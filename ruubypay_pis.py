#!/usr/bin/env python3
"""
北京地铁实时到站数据客户端（RuubyPay PIS 接口）

数据来源：https://web.ruubypay.com/web/bj-subway-map-h5-output/
接口逆向自该站前端构建产物（app.01b4b58228e2296e32ad.js -> chunk c0.js 模块 vLgD），
签名算法已用真实抓包报文逐字节校验通过。

用法：
    python3 ruubypay_pis.py 150999353          # 查某站实时到站（device_location）
    python3 ruubypay_pis.py --list 17           # 列出 17 号线所有站及 device_location
    python3 ruubypay_pis.py --search 次渠北      # 按站名查 device_location
"""
import argparse, hashlib, hmac, json, os, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

API = "https://api.ruubypay.com"
APPCFG = "https://appconfig.ruubypay.com"
WEB = "https://web.ruubypay.com/web/bj-subway-map-h5-output/"

# 签名密钥：不在本仓库中保存，需使用者自行从上游 H5 页面的公开前端产物中取得
# （任何人都能在浏览器里下载到这些 JS）。用 tools/fetch_key.py 可自动提取。
KEY_ENV = "RUUBYPAY_KEY_HEX"
_KEY_HEX = (os.environ.get(KEY_ENV) or "").strip()


def key_hex():
    """返回签名密钥；未配置时给出可操作的报错。"""
    if not _KEY_HEX:
        raise RuntimeError(
            f"未配置签名密钥。请先执行 `python3 tools/fetch_key.py` 取得密钥，"
            f"再 `export {KEY_ENV}=<密钥值>` 后重试。")
    return _KEY_HEX

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/157.0.0.0 Safari/537.36")


def _get(url, binary=False):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    req.add_header("Referer", WEB)
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
    return raw if binary else json.loads(raw.decode("utf-8"))


def normalize(obj):
    """复现前端：key 按小写排序，递归拼接 `${k}${v}||`；数组用 [] 包裹。"""
    if isinstance(obj, list):
        return "[" + "".join(normalize(x) for x in obj) + "]"
    if obj is None or not isinstance(obj, (dict, list)):
        return str(obj)
    return "".join(f"{k}{normalize(obj[k])}||" for k in sorted(obj, key=lambda s: s.lower()))


def call(path, payload=None):
    """POST JSON，body 自动附加 ts + mac 签名。"""
    body = dict(payload or {})
    body["ts"] = str(int(time.time()))
    mac = hmac.new(bytes.fromhex(key_hex()), normalize(body).encode(), hashlib.sha1).hexdigest().upper()
    body["mac"] = mac
    req = urllib.request.Request(API + path, data=json.dumps(body, ensure_ascii=False).encode(), method="POST")
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("User-Agent", UA)
    req.add_header("Referer", WEB)
    req.add_header("Origin", "https://web.ruubypay.com")
    with urllib.request.urlopen(req, timeout=25) as r:
        out = json.loads(r.read().decode("utf-8"))
    if out.get("resCode") not in ("00000000", "0000"):
        raise RuntimeError(f"接口拒绝: resCode={out.get('resCode')} resMessage={out.get('resMessage')}")
    return out.get("resData")


def fmt(ts):
    return datetime.fromtimestamp(int(ts), CST).strftime("%H:%M:%S")


class Station:
    """站点索引：device_location <-> 站名/线路。"""

    def __init__(self):
        self.acc = _get(f"{APPCFG}/stations/acclocation.json")
        self.pis = _get(f"{APPCFG}/stations/pisStationSelect.json")
        mp = _get(f"{APPCFG}/stations/map-h5.json")
        self.name_by_id = {str(s.get("station_id") or s.get("id")): s.get("cn_name")
                           for s in mp.get("stations_data", []) if (s.get("station_id") or s.get("id"))}
        self.line_by_id = {str(l["id"]): l.get("cn_name") for l in self.pis if "id" in l}
        self.seq = {}
        for l in self.pis:
            s = []
            for ss in l.get("show_stations", []):
                if ss.get("line") == l["id"]:
                    s += ss.get("stations", [])
            self.seq[str(l["id"])] = s
        self.by_loc = {str(a["device_location"]): a for a in self.acc}

    def name(self, loc):
        a = self.by_loc.get(str(loc))
        if not a:
            return "?"
        return self.name_by_id.get(str(a["station_id"]), "?")

    def line(self, loc):
        a = self.by_loc.get(str(loc))
        return self.line_by_id.get(str(a["line_id"]), "?") if a else "?"

    def neighbors(self, loc):
        """同线路相邻站（用于判断方向）。"""
        a = self.by_loc.get(str(loc))
        if not a:
            return []
        seq = self.seq.get(str(a["line_id"]), [])
        sid = str(a["station_id"])
        out = []
        if sid in seq:
            i = seq.index(sid)
            for j in (i - 1, i + 1):
                if 0 <= j < len(seq):
                    nid = str(seq[j])
                    out.append((self.name_by_id.get(str(seq[j]), "?"), nid))
        return out


def dest_map(st, loc):
    """方向标识站 -> 终点站（取自首末班车接口 destDeviceLocation）。"""
    out = {}
    data = call("/marketingpis/appSides/getStationFirstLastTrain",
                {"cityCode": "1101", "deviceLocation": str(loc)})
    for item in data or []:
        for d in item.get("direction") or []:
            out.setdefault(d["directDeviceLocation"], d.get("destDeviceLocation"))
    return out


def realtime(st, loc, only=None, dest=None):
    """only: 按方向标识站名过滤；dest: 按终点站名过滤。"""
    print(f"\n=== 实时到站：{st.name(loc)}（{st.line(loc)}） device_location={loc} ===")
    now = int(time.time())
    print(f"    本机当前时间 {fmt(now)}")
    data = call("/marketingpis/appSides/getRealtimeStationInfo",
                {"cityCode": "1101", "deviceLocation": str(loc)})
    dm = dest_map(st, loc) if dest else {}
    for item in data or []:
        for ev in item.get("eventInfo") or []:
            print(f"  [事件] {ev.get('eventTitle')} {ev.get('eventAddr')}")
        for mi in item.get("metroInfo") or []:
            dloc = mi.get("directDeviceLocation")
            dname = st.name(dloc)
            ddest = dm.get(dloc)
            ddestname = st.name(ddest) if ddest else "?"
            if only and only not in dname:
                continue
            if dest and dest not in ddestname:
                continue
            nxt = mi.get("nextTrains") or []
            if nxt:
                head = f"  → 方向标识站 {dname}({dloc}) → 终点 {ddestname}" + ("  ★" if dest else "")
                print(head + "：")
                for i, t in enumerate(nxt):
                    eta = int(t["arriveTime"]) - now
                    print(f"       第{i+1}班  到达 {fmt(t['arriveTime'])}  还有 {eta//60}分{eta%60}秒"
                          f"  满载率 {t.get('fullLoadRate')}%  (isFullLoadRate={t.get('isFullLoadRate')})")
                gaps = [int(nxt[i+1]['arriveTime'])-int(nxt[i]['arriveTime']) for i in range(len(nxt)-1)]
                if gaps:
                    print("       车次间隔：" + "，".join(f"{g//60}分{g%60}秒" for g in gaps))
                print(f"       >>> 下一班到达：{fmt(nxt[0]['arriveTime'])}"
                      f"（还有 {int(nxt[0]['arriveTime'])-now} 秒）")
            else:
                print(f"  → 方向标识站 {dname}({dloc}) → 终点 {ddestname}：暂无实时车次")
        if not item.get("metroInfo"):
            print("    （无 metroInfo）")


def first_last(st, loc):
    print(f"\n=== 首末班车：{st.name(loc)} ===")
    data = call("/marketingpis/appSides/getStationFirstLastTrain",
                {"cityCode": "1101", "deviceLocation": str(loc)})
    for item in data or []:
        for d in item.get("direction") or []:
            flag = "末班车" if d.get("isLast") else "首班车"
            print(f"  经 {st.name(d.get('directDeviceLocation'))}：{flag} 发车 {fmt(d['driveTime'])}"
                  f" → 终点 {st.name(d.get('destDeviceLocation'))} {fmt(d['destTime'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("location", nargs="?", help="device_location，例如 150999353")
    ap.add_argument("--list", help="列出某线路的站点（线路 id，如 17）")
    ap.add_argument("--search", help="按站名模糊搜索")
    ap.add_argument("--first-last", action="store_true", help="附带首末班车")
    ap.add_argument("--dir", help="只看该方向（如 十里河），按方向标识站名匹配")
    ap.add_argument("--dest", help="只看终点为该站的方向（如 未来科学城）")
    a = ap.parse_args()
    st = Station()

    if a.list:
        lid = str(a.list)
        for sid in st.seq.get(lid, []):
            loc = next((x["device_location"] for x in st.acc
                        if str(x["line_id"]) == lid and str(x["station_id"]) == str(sid)), None)
            print(f"  {st.name_by_id.get(str(sid),'?'):<12} station_id={sid:<5} device_location={loc}")
        return

    if a.search:
        for x in st.acc:
            nm = st.name(x["device_location"])
            if a.search in nm:
                print(f"  {nm:<12} 线路={st.line(x['device_location']):<8} "
                      f"device_location={x['device_location']} station_id={x['station_id']}")
        return

    if not a.location:
        ap.error("需要 device_location，或使用 --list / --search")
    realtime(st, a.location, a.dir, a.dest)
    if a.first_last:
        first_last(st, a.location)


if __name__ == "__main__":
    main()
