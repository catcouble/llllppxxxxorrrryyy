#!/bin/bash

# LMArena Proxy 部署脚本
# 作者: AI Assistant
# 功能: 停止旧容器 -> 重新构建镜像 -> 启动新容器

set -e  # 遇到错误立即退出

echo "🚀 开始部署 LMArena Proxy..."

# 1. 停止并删除旧容器
echo "📦 步骤1: 停止旧容器..."
if docker ps -q -f name=lmarena-proxy | grep -q .; then
    echo "   停止容器: lmarena-proxy"
    docker stop lmarena-proxy
    docker rm lmarena-proxy
    echo "   ✅ 旧容器已停止并删除"
else
    echo "   ℹ️  没有找到运行中的 lmarena-proxy 容器"
fi

# 2. 删除旧镜像（可选）
echo "🗑️  步骤2: 清理旧镜像..."
if docker images -q lmarena-proxy | grep -q .; then
    echo "   删除旧镜像: lmarena-proxy"
    docker rmi lmarena-proxy
    echo "   ✅ 旧镜像已删除"
else
    echo "   ℹ️  没有找到 lmarena-proxy 镜像"
fi

# 3. 重新构建镜像
echo "🔨 步骤3: 重新构建 Docker 镜像..."
echo "   构建镜像: lmarena-proxy"
docker build -t lmarena-proxy .
echo "   ✅ 镜像构建完成"

# 4. 启动新容器
echo "🚀 步骤4: 启动新容器..."
docker run -d \
    --name lmarena-proxy \
    --restart unless-stopped \
    -p 9080:9080 \
    lmarena-proxy

echo "   ✅ 容器启动完成"

# 5. 等待服务启动
echo "⏳ 步骤5: 等待服务启动..."
sleep 3

# 6. 检查服务状态
echo "🔍 步骤6: 检查服务状态..."
if docker ps -q -f name=lmarena-proxy | grep -q .; then
    echo "   ✅ 容器运行状态: 正常"
    
    # 测试API接口
    echo "   🔍 测试API接口..."
    if curl -s http://localhost:9080/v1/models > /dev/null; then
        echo "   ✅ API接口响应正常"
        echo "   📊 可用模型数量: $(curl -s http://localhost:9080/v1/models | jq '.data | length')"
    else
        echo "   ⚠️  API接口暂时无响应（可能需要更多时间启动）"
    fi
else
    echo "   ❌ 容器启动失败"
    echo "   📋 查看容器日志:"
    docker logs lmarena-proxy --tail 20
    exit 1
fi

echo ""
echo "🎉 部署完成！"
echo "📋 服务信息:"
echo "   - 容器名称: lmarena-proxy"
echo "   - 端口映射: 9080:9080"
echo "   - API地址: http://localhost:9080"
echo "   - 模型列表: http://localhost:9080/v1/models"
echo "   - 监控面板: http://localhost:9080/monitor"
echo ""
echo "🔧 常用命令:"
echo "   - 查看日志: docker logs lmarena-proxy"
echo "   - 停止服务: docker stop lmarena-proxy"
echo "   - 重启服务: docker restart lmarena-proxy"
echo "   - 进入容器: docker exec -it lmarena-proxy bash" 