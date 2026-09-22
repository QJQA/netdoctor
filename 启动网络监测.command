#!/bin/bash
cd "$(dirname "$0")"

# 检查服务器是否已在运行
if python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',7656)); s.close()" 2>/dev/null; then
  echo "✅ 服务器已在运行"
else
  echo "▶ 启动服务器..."
  nohup python3 server.py --no-open > /tmp/netdoctor_server.log 2>&1 &
  # 等待启动
  for i in 1 2 3 4 5; do
    sleep 1
    if python3 -c "import socket; s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',7656)); s.close()" 2>/dev/null; then
      echo "✅ 服务器已启动"
      break
    fi
  done
fi

echo "▶ 打开浏览器..."
open http://localhost:7656
