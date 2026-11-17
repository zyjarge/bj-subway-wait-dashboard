#!/bin/sh
# 北京地铁候车看板 —— 一键启动（局域网可访问）
#   ./start.sh [端口]        默认 8791
set -e
DIR=$(cd "$(dirname "$0")" && pwd)
PORT=${1:-8791}
export PATH="$HOME/.local/bin:$PATH"

# 端口占用检测
if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "端口 $PORT 已被占用，请换一个：./start.sh 8899"
  exit 1
fi

cd "$DIR/.."
if [ -z "$RUUBYPAY_KEY_HEX" ]; then
  echo "提示：未检测到 RUUBYPAY_KEY_HEX，接口调用会被拒绝。"
  echo "      可先运行： python3 tools/fetch_key.py   （自动从公开前端产物提取）"
  echo
fi
setsid nohup python3 dashboard/server.py --port "$PORT" > /tmp/bjsubway-dash.log 2>&1 < /dev/null &
sleep 6
URL="http://127.0.0.1:$PORT/"
LAN=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -m1 '^192\.168\.')
echo "看板已启动： $URL"
[ -n "$LAN" ] && echo "局域网访问： http://$LAN:$PORT/  （手机/同事浏览器直接打开）"
echo "日志：/tmp/bjsubway-dash.log"
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true; fi
