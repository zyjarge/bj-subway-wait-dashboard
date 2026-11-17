#!/usr/bin/env python3
"""
地铁候车最小化：求"最佳离开工位时刻"。

数据源：RuubyPay PIS（时刻表 + 实时 ETA）
        https://web.ruubypay.com/web/bj-subway-map-h5-output/

模型
----
目标：最小化 E[候车时间]（并给出"错过目标班"的概率）
变量：
  W       步行时间均值（工位→站台）
  sigma_w 步行时间波动（正态标准差）
  sigma_s 时刻表偏差（看实时 ETA 时可视为 0；只看纸质时刻表时非 0）
  b       缓冲：故意提前到站的分钟数
策略：
  对齐型：选定目标车次 T，离开工位时刻 d = T − W − b
  随机型：在时间窗内随机时刻离开工位
"""
import argparse, json, os, random, statistics, sys
from datetime import datetime, timedelta, timezone
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("rr", os.path.join(_HERE, "ruubypay_pis.py"))
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

CST = timezone(timedelta(hours=8))


def today_schedule(st, loc, dest=None, weekday=None):
    """返回 [(方向标识站, 终点站名, [今天各班到站时间戳])]。"""
    if weekday is None:
        weekday = 1 if datetime.now(CST).weekday() < 5 else 0
    tt = rr.call("/marketingpis/appSides/getStationTimetable", {"cityCode": "1101", "deviceLocation": str(loc)})
    dm = rr.dest_map(st, loc)
    day0 = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for item in tt or []:
        for L in item.get("locations") or []:
            dloc = L.get("directDeviceLocation")
            ddest = st.name(dm.get(dloc)) if dm.get(dloc) else "?"
            if dest and dest not in ddest:
                continue
            for d in L.get("direction") or []:
                if d.get("isWeekDay") != weekday:
                    continue
                times = set()
                for t in d.get("timetables") or []:
                    tod = datetime.fromtimestamp(t["arriveTime"], CST)
                    times.add(int((day0 + timedelta(hours=tod.hour, minutes=tod.minute,
                                                   seconds=tod.second)).timestamp()))
                if times:
                    out.append((dloc, ddest, sorted(times)))
    return out


def fmt(ts):
    return datetime.fromtimestamp(int(ts), CST).strftime("%H:%M:%S")


def sim_aligned(sched, W, b, sw, ss, n=40000, seed=7):
    """对齐型：每次随机抽一个目标车次 T，d = T − W − b；车次实际到站 T+δ。"""
    rng = random.Random(seed)
    waits, miss = [], 0
    for _ in range(n):
        T = rng.choice(sched)
        d = T - (W + b) * 60
        eps = rng.gauss(0, sw * 60)          # 步行偏差
        delta = rng.gauss(0, ss * 60)       # 车次偏差（看实时 ETA 则 ss=0）
        arr = d + W * 60 + eps               # 实际到站时刻 = 离开工位 + 实际步行
        Treal = T + delta                     # 目标车实际到站
        if arr <= Treal + 20:                # 赶上
            waits.append((Treal - arr) / 60)
        else:                                 # 错过 → 等下一班
            nxt = next((x for x in sched if x > Treal), None)
            if nxt is None:
                continue
            miss += 1
            waits.append((nxt - arr) / 60)
    return statistics.mean(waits), miss / n


def sim_random(sched, W, sw, w0, w1, n=40000, seed=7):
    rng = random.Random(seed)
    waits = []
    for _ in range(n):
        d = rng.uniform(w0, w1)
        arr = d + rng.gauss(0, sw * 60)
        nxt = next((x for x in sched if x >= arr - 20), None)
        if nxt is None:
            continue
        waits.append((nxt - arr) / 60)
    return statistics.mean(waits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("location", help="device_location")
    ap.add_argument("--walk", type=float, default=20, help="步行时间均值（分钟）")
    ap.add_argument("--dest", help="方向终点站名过滤")
    ap.add_argument("--sigma-walk", type=float, default=3.0, help="步行波动 σ（分钟）")
    ap.add_argument("--sigma-sched", type=float, default=0.0, help="时刻表偏差 σ（分钟），看实时 ETA 填 0")
    ap.add_argument("--start", default="17:00")
    ap.add_argument("--end", default="20:00")
    ap.add_argument("--table", action="store_true", help="打印逐班建议出发时刻表")
    a = ap.parse_args()

    st = rr.Station()
    scheds = today_schedule(st, a.location, a.dest)
    if not scheds:
        print("未找到匹配时刻表"); return
    now = datetime.now(CST)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    h0, m0 = map(int, a.start.split(":"))
    h1, m1 = map(int, a.end.split(":"))
    w0 = int((day0 + timedelta(hours=h0, minutes=m0)).timestamp())
    w1 = int((day0 + timedelta(hours=h1, minutes=m1)).timestamp())

    for dloc, ddest, times in scheds:
        win = [t for t in times if w0 <= t <= w1]
        if not win:
            continue
        gaps = [(win[i+1] - win[i]) / 60 for i in range(len(win)-1)]
        avg_gap = statistics.mean(gaps)
        print(f"\n{'='*74}")
        print(f"站点 {st.name(a.location)}（{st.line(a.location)}）｜方向 {st.name(dloc)} → 终点 {ddest}")
        print(f"步行 W={a.walk} 分｜步行波动 σ_w={a.sigma_walk} 分｜时刻表偏差 σ_s={a.sigma_sched} 分")
        print(f"窗口 {a.start}–{a.end}：{len(win)} 班，间隔 {min(gaps):.1f}~{max(gaps):.1f} 分（均值 {avg_gap:.2f}）")

        base = sim_random(win, a.walk, a.sigma_walk, w0, w1)
        print(f"\n【基线】窗口内随机时刻离开工位 → 期望候车 {base:.2f} 分")
        print("\n【对齐型】不同缓冲 b 的效果：")
        print(f"   {'b(分)':>6}{'期望候车':>10}{'错过概率':>11}{'期望总损失':>12}")
        best = None
        for b in (0, 1, 2, 3, 4, 5, 6, 8):
            m, miss = sim_aligned(win, a.walk, b, a.sigma_walk, a.sigma_sched)
            loss = m + miss * avg_gap
            print(f"   {b:>6}{m:>10.2f}{miss*100:>10.1f}%{loss:>12.2f}")
            if best is None or loss < best[1]:
                best = (b, loss, m, miss)
        print(f"   → 推荐缓冲 b={best[0]} 分：期望候车 {best[2]:.2f} 分，错车率 {best[3]*100:.1f}%，"
              f"相对随机出发{'节省' if base>best[2] else '多花'} {abs(base-best[2]):.2f} 分")

        if a.table:
            print(f"\n【建议离开工位的时刻】（按推荐缓冲 b={best[0]} 分）")
            print(f"   {'到站时刻':<11}{'离开工位':<12}{'备注'}")
            for t in win:
                leave = t - (a.walk + best[0]) * 60
                note = ""
                if leave < now.timestamp():
                    note = "（已过）"
                elif leave < now.timestamp() + 900:
                    note = "← 最近可赶"
                print(f"   {fmt(t):<11}{fmt(leave):<12}{note}")


if __name__ == "__main__":
    main()
