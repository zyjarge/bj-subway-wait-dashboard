#!/usr/bin/env python3
"""
北京地铁候车看板 —— 本地服务（站点 / 方向可选，局域网可访问）

后端：直接调用 RuubyPay PIS 实时接口 + 时刻表，做倒算优化
前端：单文件 HTML（无外部依赖），由本服务托管

启动：python3 dashboard/server.py [--port 8791] [--host 0.0.0.0]
"""
import argparse, bisect, json, os, sys, time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rr = _load("rr", os.path.join(ROOT, "ruubypay_pis.py"))
od = _load("od", os.path.join(ROOT, "optimal_departure.py"))

CST = timezone(timedelta(hours=8))
DEFAULT_LOC = "150999351"   # 北神树
DEFAULT_DIR = "未来科学城"   # 方向（终点/方向标识站名过滤，北行）
CACHE = {"station": 0}
TTL = {"station": 600}
_OCACHE = {}   # (kind, loc, dir) -> (ts, obj)；时刻表 30 分、方向表 10 分、事件 2 分


def get_station():
    if time.time() - CACHE["station"] < TTL["station"]:
        return CACHE["station_obj"]
    CACHE["station_obj"] = rr.Station()
    CACHE["station"] = time.time()
    return CACHE["station_obj"]


def _ocached(kind, loc, extra, ttl, fn):
    key = (kind, str(loc), extra)
    hit = _OCACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _OCACHE[key] = (time.time(), val)
    return val


def get_dest_map(st, loc):
    return _ocached("dest", loc, "", 600, lambda: rr.dest_map(st, loc))


def get_schedule(st, loc, dirf):
    """当日时刻表；dirf 过滤方向（终点站名或方向标识站名的子串），空表示全部方向。"""
    def load():
        sched = od.today_schedule(st, loc, None)
        if not dirf:
            return sched
        out = []
        for dloc, ddest, times in sched:
            if dirf in ddest or dirf in st.name(dloc):
                out.append((dloc, ddest, times))
        return out
    return _ocached("tt", loc, dirf or "", 1800, load)


def station_meta(st):
    """线路 -> 站点列表（供前端选择）。"""
    if hasattr(station_meta, "_cache"):
        return station_meta._cache
    lines = []
    for lid, seq in st.seq.items():
        stations = []
        for sid in seq:
            loc = next((x["device_location"] for x in st.acc
                        if str(x["line_id"]) == lid and str(x["station_id"]) == str(sid)), None)
            if loc:
                stations.append({"name": st.name_by_id.get(str(sid), "?"), "loc": str(loc)})
        if stations:
            lines.append({"id": lid, "name": st.line_by_id.get(lid, lid), "stations": stations})
    lines.sort(key=lambda l: l["name"])
    station_meta._cache = lines
    return lines


def station_dirs(st, loc):
    """某站可选方向：方向标识站 + 终点站。"""
    dm = get_dest_map(st, loc)
    out = []
    for dloc, ddest in dm.items():
        out.append({"dirMark": st.name(dloc), "dirMarkLoc": str(dloc),
                    "dest": st.name(ddest) if ddest else "?"})
    return out


def day_fit(st, loc, dirf, walk, buffer_min, g_sec, y_sec):
    """全天「适合离开工位」适宜度。

    核心量：若在时刻 d 离开工位，走到站台（d + walk）时能赶上的下一班车为 T，
    站台候车 = T − (d + walk)：
      候车 ≤ g → 绿（最佳出发窗口）
      候车 ≤ y → 黄（可以等，但别太早走）
      更久    → 红（现在走纯属干等）
      未运营/已收车 → 灰（none）

    返回：
      pattern  60 项：分钟刻度 0..59 的适宜度（对应模拟表盘 1..12 数字位置，
               分钟 40 落在数字 8 的位置）。
      day      全天 0..1440 分钟的色段（minute-of-day），用于 24 小时色带。
      markers  最佳离开时刻（到站时间 − 步行 − 缓冲），minute-of-day。
      nextGood 下一个绿色窗口。
    """
    sched = get_schedule(st, loc, dirf)
    trains = sorted({t for dloc, ddest, times in sched for t in times})
    now = int(time.time())
    day0 = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = int(day0.timestamp())
    day_end = day_start + 24 * 3600
    trains = [t for t in trains if day_start <= t < day_end]

    walk_s = int(walk * 60)
    buf_s = int(buffer_min * 60)

    def level_at(d):
        if not trains:
            return "none"
        a = d + walk_s
        if a < trains[0] - 20:
            return "none"          # 还没开始运营
        idx = bisect.bisect_right(trains, a - 20)
        if idx >= len(trains):
            return "none"          # 已收车
        wait = trains[idx] - a
        if wait <= g_sec:
            return "good"
        if wait <= y_sec:
            return "warn"
        return "bad"

    # ---- 全天每分钟 ----
    lv = [level_at(day_start + m * 60) for m in range(24 * 60)]

    # ---- 压缩成色段 ----
    day = []
    s = 0
    for m in range(1, 24 * 60 + 1):
        if m == 24 * 60 or lv[m] != lv[s]:
            day.append({"start": s, "end": m, "level": lv[s]})
            s = m

    # ---- 分钟刻度模式：每个 minute-of-hour 取"今天下一次出现该分钟"的判定 ----
    now_min = (now - day_start) // 60
    pattern = []
    for mm in range(60):
        cand = now_min + ((mm - now_min) % 60)
        while cand < 24 * 60 and lv[cand] == "none" and cand + 60 < 24 * 60:
            cand += 60
        pattern.append(lv[cand] if cand < 24 * 60 else "none")

    # ---- 最佳离开时刻 ----
    markers = []
    for t in trains:
        lm = t - walk_s - buf_s
        if day_start <= lm < day_end:
            markers.append(int((lm - day_start) / 60))

    # ---- 下一个绿色窗口 ----
    next_good = None
    for m in range(max(0, now_min), 24 * 60):
        if lv[m] == "good":
            next_good = {"at": m, "inMin": m - now_min}
            break

    return {
        "pattern": pattern,
        "day": day,
        "markers": sorted(set(markers)),
        "nextGood": next_good,
        "nowMin": now_min,
        "headway": round((sum(trains[i + 1] - trains[i] for i in range(len(trains) - 1))
                          / max(1, len(trains) - 1)) / 60, 1) if len(trains) > 1 else 0,
        "firstTrain": int((trains[0] - day_start) / 60) if trains else None,
        "lastTrain": int((trains[-1] - day_start) / 60) if trains else None,
    }


def get_events(loc):
    """PIS 限流/客流管控事件（GET，无需签名）。"""
    return _ocached("ev", loc, "", 120, lambda: _fetch_events(loc))


def _fetch_events(loc):
    import gzip
    url = f"https://api.ruubypay.com/PIS/station/normalEvent/v4?deviceLocation={loc}&cityCode=1101"
    req = rr.urllib.request.Request(url)
    req.add_header("User-Agent", rr.UA)
    req.add_header("Referer", rr.WEB)
    req.add_header("Accept-Encoding", "gzip")
    try:
        with rr.urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        data = json.loads(raw.decode("utf-8"))
        evs = (data.get("resData") or {}).get("normalEventVOList") or []
    except Exception:
        evs = []
    return evs


def hm(s):
    try:
        h, m = map(int, str(s).split(":"))
        return h * 60 + m
    except Exception:
        return None


def build_state(walk, buffer_min, sigma, loc=DEFAULT_LOC, dirf=DEFAULT_DIR):
    st = get_station()
    loc = str(loc)
    dirf = (dirf or "").strip()
    now = int(time.time())
    now_dt = datetime.now(CST)
    dm = get_dest_map(st, loc)

    def dir_match(dloc, dest_name):
        if not dirf:
            return True
        return dirf in (dest_name or "") or dirf in st.name(dloc)

    # --- 实时到站（只取目标方向）---
    rt = rr.call("/marketingpis/appSides/getRealtimeStationInfo",
                 {"cityCode": "1101", "deviceLocation": loc})
    groups = []
    for item in rt or []:
        for mi in item.get("metroInfo") or []:
            dloc = mi.get("directDeviceLocation")
            dest_name = st.name(dm.get(dloc)) if dm.get(dloc) else "?"
            if not dir_match(dloc, dest_name):
                continue
            trains = []
            for t in mi.get("nextTrains") or []:
                at = int(t["arriveTime"])
                lb = at - int((walk + buffer_min) * 60)
                trains.append({
                    "arriveTime": at,
                    "arriveText": datetime.fromtimestamp(at, CST).strftime("%H:%M:%S"),
                    "countdown": at - now,
                    "leaveBy": lb,
                    "leaveText": datetime.fromtimestamp(lb, CST).strftime("%H:%M:%S"),
                    "missed": lb <= now,     # 按当前参数出发已来不及
                    "loadField": t.get("fullLoadRate"),
                    "loadFlag": t.get("isFullLoadRate"),
                })
            groups.append({"dirMark": st.name(dloc), "dirMarkId": dloc,
                           "dest": dest_name, "trains": trains})

    # --- 当日目标方向到站序列：用于判断"现在走会赶上哪班" ---
    sched = get_schedule(st, loc, dirf)
    all_trains = []
    for dloc, ddest, times in sched:
        for t in times:
            if t >= now - 600:
                all_trains.append(t)
    all_trains = sorted(set(all_trains))

    # --- "现在走会赶上哪班" ---
    arrival_if_leave_now = now + int(walk * 60)
    catch = None
    for t in all_trains:
        if t >= arrival_if_leave_now - 20:
            catch = {"arriveTime": t,
                     "arriveText": datetime.fromtimestamp(t, CST).strftime("%H:%M:%S"),
                     "wait": t - arrival_if_leave_now}
            break

    # --- 未来可赶的班次（展示用）：只列"现在出发还来得及"的 ---
    leave_cut = now + int((walk + buffer_min) * 60) - 20
    catchable = [t for t in all_trains if t >= leave_cut]
    upcoming = []
    for t in catchable[:8]:
        upcoming.append({
            "arriveTime": t,
            "arriveText": datetime.fromtimestamp(t, CST).strftime("%H:%M:%S"),
            "countdown": t - now,
            "leaveText": datetime.fromtimestamp(t - int((walk + buffer_min) * 60), CST).strftime("%H:%M:%S"),
        })

    # --- 今日晚间的时刻表 + 优化 ---
    win = []
    for dloc, ddest, times in sched:
        win = [t for t in times if now - 120 <= t <= now + 6 * 3600]
    table = []
    if win:
        for t in win:
            leave = t - int((walk + buffer_min) * 60)
            table.append({
                "arrive": datetime.fromtimestamp(t, CST).strftime("%H:%M:%S"),
                "leave": datetime.fromtimestamp(leave, CST).strftime("%H:%M:%S"),
                "leaveTs": leave,
                "state": "past" if leave < now else ("next" if leave <= now + 900 else "future"),
            })
    opt = {}
    if len(win) >= 3:
        gaps = [(win[i + 1] - win[i]) / 60 for i in range(len(win) - 1)]
        w0 = now - 3600
        w1 = now + 4 * 3600
        base = od.sim_random(win, walk, sigma, w0, w1)
        sweep = []
        for b in (0, 1, 2, 3, 4, 5, 6, 8):
            m, miss = od.sim_aligned(win, walk, b, sigma, 0.0)
            sweep.append({"b": b, "wait": round(m, 2), "miss": round(miss * 100, 1),
                          "loss": round(m + miss * (sum(gaps) / len(gaps)), 2)})
        best = min(sweep, key=lambda x: x["loss"])
        # 稳定步行(σ=1)时的最优缓冲
        stable = []
        for b in (0, 1, 2, 3):
            m, miss = od.sim_aligned(win, walk, b, 1.0, 0.0)
            stable.append({"b": b, "wait": round(m, 2), "miss": round(miss * 100, 1)})
        opt = {"baseline": round(base, 2), "sweep": sweep, "best": best,
               "headway": round(sum(gaps) / len(gaps), 2),
               "headwayMin": round(min(gaps), 1), "headwayMax": round(max(gaps), 1),
               "stable": stable}

    # --- 限流事件 ---
    evs = get_events(loc)
    now_min = now_dt.hour * 60 + now_dt.minute
    active_raw = []
    for e in evs:
        b, en = hm(e.get("btime")), hm(e.get("etime"))
        if b is not None and en is not None and b <= now_min <= en:
            active_raw.append(e)
    line_id = str((st.by_loc.get(loc) or {}).get("line_id", ""))
    st_name = st.name(loc)
    line_label = st.line(loc)

    # 事件里的 lineId 是另一套不可靠编码（如永安里被标为 17），
    # 按"事件实际所属线路 == 所选线路"或"发生在本站"判断
    _line_of_station = {}
    for l in station_meta(st):
        for s in l["stations"]:
            _line_of_station.setdefault(s["name"], l["name"])

    def ev_brief(e):
        sn = e.get("stationName") or "?"
        lid = str(e.get("lineId") or "")
        return {
            "station": sn,
            "lineName": st.line_by_id.get(lid) or _line_of_station.get(sn, lid or "?"),
            "btime": e.get("btime") or "?",
            "etime": e.get("etime") or "?",
            "desc": e.get("desc") or "",
            "etype": str(e.get("etype") or ""),
        }

    line_events = [ev_brief(e) for e in evs
                   if (ev_brief(e)["lineName"] == line_label)
                   or e.get("stationName") == st_name]

    return {
        "now": now,
        "nowText": now_dt.strftime("%H:%M:%S"),
        "station": {"name": st_name, "line": line_label, "id": loc,
                    "lineId": line_id},
        "dirFilter": dirf,
        "walk": walk, "buffer": buffer_min, "sigma": sigma,
        "groups": groups,
        "upcoming": upcoming,
        "leaveNow": {
            "arriveText": datetime.fromtimestamp(arrival_if_leave_now, CST).strftime("%H:%M:%S"),
            "catchArrive": catch["arriveText"] if catch else None,
            "catchTime": catch["arriveTime"] if catch else None,
            "catchWait": catch["wait"] if catch else None,
        },
        "table": table,
        "opt": opt,
        "events": {
            "total": len(evs),
            "active": [ev_brief(e) for e in active_raw],
            "line": line_events,
        },
        "firstLast": first_last_summary(st, loc, dm, dirf),
    }


def first_last_summary(st, loc, dm, dirf=""):
    try:
        fl = rr.call("/marketingpis/appSides/getStationFirstLastTrain",
                     {"cityCode": "1101", "deviceLocation": str(loc)})
    except Exception:
        return []
    out = []
    for item in fl or []:
        for d in item.get("direction") or []:
            dname = st.name(dm.get(d["directDeviceLocation"]))
            if dirf and dirf not in dname and dirf not in st.name(d["directDeviceLocation"]):
                continue
            out.append({
                "isLast": d.get("isLast"),
                "drive": datetime.fromtimestamp(d["driveTime"], CST).strftime("%H:%M:%S"),
                "dest": st.name(d.get("destDeviceLocation")),
                "destTime": datetime.fromtimestamp(d["destTime"], CST).strftime("%H:%M:%S"),
            })
    return out


HTML = os.path.join(HERE, "index.html")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(HTML, encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/api/meta":
                st = get_station()
                return self._send(200, json.dumps({"lines": station_meta(st)}, ensure_ascii=False))
            if u.path == "/api/dirs":
                st = get_station()
                loc = q.get("loc", [DEFAULT_LOC])[0]
                return self._send(200, json.dumps({"dirs": station_dirs(st, loc)}, ensure_ascii=False))
            if u.path == "/api/dayfit":
                st = get_station()
                loc = q.get("loc", [DEFAULT_LOC])[0]
                dirf = q.get("dir", [DEFAULT_DIR])[0]
                walk = float(q.get("walk", ["20"])[0])
                buf = float(q.get("buffer", ["1"])[0])
                g = float(q.get("g", ["2"])[0]) * 60       # 绿：候车 ≤ g 分钟
                y = float(q.get("y", ["5"])[0]) * 60       # 黄：候车 ≤ y 分钟
                data = day_fit(st, loc, dirf, walk, buf, g, y)
                return self._send(200, json.dumps(data, ensure_ascii=False))
            if u.path == "/api/state":
                walk = float(q.get("walk", ["20"])[0])
                buf = float(q.get("buffer", ["1"])[0])
                sig = float(q.get("sigma", ["3"])[0])
                loc = q.get("loc", [DEFAULT_LOC])[0]
                dirf = q.get("dir", [DEFAULT_DIR])[0]
                return self._send(200, json.dumps(
                    build_state(walk, buf, sig, loc=loc, dirf=dirf), ensure_ascii=False))
            if u.path == "/api/health":
                return self._send(200, json.dumps({"ok": True, "time": datetime.now(CST).isoformat()}))
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if "RUUBYPAY_KEY_HEX" in msg:
                msg = ("未配置签名密钥。请先运行 `python3 tools/fetch_key.py` 取得密钥，"
                       "再 `export RUUBYPAY_KEY_HEX=<值>` 后重启服务。")
            return self._send(500, json.dumps({"error": msg}, ensure_ascii=False))
        self._send(404, json.dumps({"error": "not found"}))


def _lan_ips():
    import socket
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--host", default="0.0.0.0", help="默认 0.0.0.0，局域网内可访问")
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"北京地铁候车看板（站点/方向可选）已启动，端口 {a.port}")
    print(f"  本机：   http://127.0.0.1:{a.port}/")
    for ip in _lan_ips():
        print(f"  局域网： http://{ip}:{a.port}/")
    print("Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
