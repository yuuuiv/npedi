#!/bin/bash

echo "======================================"
echo "NPEDI 数据全览 Dashboard"
echo "======================================"
echo ""

# 检查虚拟环境
if [ ! -f ".venv/bin/activate" ]; then
    echo "❌ 虚拟环境不存在，请先创建"
    exit 1
fi

# 激活虚拟环境
echo "✓ 激活虚拟环境..."
source .venv/bin/activate

# 安装依赖
echo "✓ 安装依赖..."
pip install -q -r requirements-dashboard.txt

echo ""
echo "======================================"
echo "🚀 启动 Streamlit Dashboard"
echo "======================================"
echo ""
echo "📍 本地访问: http://localhost:8081"
LOCAL_IP=$(hostname -I | awk '{print $1}')
echo "📍 局域网访问: http://$LOCAL_IP:8081"
echo ""
echo "按 Ctrl+C 停止服务"
echo ""

# 启动 Streamlit
streamlit run dashboard.py --server.port 8081 --server.address 0.0.0.0
