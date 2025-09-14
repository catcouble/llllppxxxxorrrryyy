# Dockerfile

# 1. 使用官方的 Python 镜像作为基础
FROM python:3.10-slim

# 2. 设置工作目录
WORKDIR /app

# 3. 复制 requirements.txt 文件并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 复制你的代理服务器代码
COPY proxy_server.py .

# 5. 暴露你的应用运行的端口 (根据你的代码是 9080)
EXPOSE 9080

# 6. 定义启动命令，使用 uvicorn 运行你的 FastAPI 应用
#    Host 0.0.0.0 是为了让容器外的服务可以访问它
CMD ["uvicorn", "proxy_server:app", "--host", "0.0.0.0", "--port", "9080"]