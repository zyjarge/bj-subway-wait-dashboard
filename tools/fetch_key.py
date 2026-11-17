#!/usr/bin/env python3
"""
自助提取 PIS 签名密钥。

本仓库**不包含**任何密钥。密钥需要使用者自行从上游 H5 页面的**公开**前端
构建产物中取得 —— 那是浏览器加载这个网页时任何人都能下载到的 JS 文件。

本脚本自动化这件事：
  1. 抓取 H5 入口 HTML → 找到 manifest / app / vendor 脚本
  2. 从 manifest 里解析 webpack 的 chunk 哈希表与 publicPath
  3. 下载各 chunk，在 CryptoJS HmacSHA1 调用附近寻找 40 位十六进制常量
  4. 打印候选密钥与用法

用法：
    python3 tools/fetch_key.py
    export RUUBYPAY_KEY_HEX=<打印出来的值>
    ./dashboard/start.sh
"""
import os, re, sys, urllib.request

WEB = "https://web.ruubypay.com/web/bj-subway-map-h5-output/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/157.0.0.0 Safari/537.36")


def fetch(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def main():
    print("→ 抓取入口页面 …")
    html = fetch(WEB)
    scripts = re.findall(r'src=([^\s>"]+\.js)', html)
    scripts = [s if s.startswith("http") else WEB + s.lstrip("/") for s in scripts]
    print(f"  找到 {len(scripts)} 个脚本")

    manifest = None
    for s in scripts:
        if "manifest" in s:
            manifest = s
    if not manifest:
        print("✗ 没找到 manifest，无法解析 chunk 列表")
        return 1

    print("→ 解析 webpack chunk 表 …")
    mf = fetch(manifest)
    pub = re.search(r'o\.p\s*=\s*"([^"]+)"', mf)
    public_path = pub.group(1) if pub else "https://static.ruubypay.com/bj-subway-map-h5-output/"
    pairs = re.findall(r'(\d+):\s*"([0-9a-f]{8,})"', mf)
    if not pairs:
        print("✗ manifest 结构已变化，无法解析 chunk 哈希表")
        return 1
    print(f"  publicPath = {public_path}")
    print(f"  chunk 数 = {len(pairs)}")

    hexre = re.compile(r'"([0-9A-Fa-f]{32,64})"')
    hits = []
    for cid, h in pairs:
        url = f"{public_path}static/js/{cid}.{h}.js"
        try:
            src = fetch(url)
        except Exception as e:
            print(f"  · chunk {cid} 下载失败：{e}")
            continue
        # 优先取 HmacSHA1 调用附近的常量
        for m in re.finditer(r'HmacSHA1\((.{0,160}?)\)', src):
            for k in hexre.findall(m.group(1)):
                hits.append((cid, k, "HmacSHA1 附近"))
        if not hits:
            for k in hexre.findall(src):
                hits.append((cid, k, "chunk 内长 hex 常量"))
        if hits:
            break

    if not hits:
        print("✗ 未找到候选密钥（上游可能已更换加密方式）")
        return 1

    print("\n✓ 候选密钥：")
    for cid, k, why in hits[:5]:
        print(f"    {k}    (chunk {cid} · {why})")
    print("\n使用：")
    print(f"    export RUUBYPAY_KEY_HEX={hits[0][1]}")
    print("    ./dashboard/start.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
