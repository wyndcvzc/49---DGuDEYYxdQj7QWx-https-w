# ---- 阶段 1: 构建器 ----
FROM python:3.12-slim as builder

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- 阶段 2: 最终镜像 ----
FROM python:3.12-slim

WORKDIR /app

# 从构建器复制已安装的依赖
COPY --from=builder /install /usr/local

# 复制应用代码
COPY main.py .
COPY static/ ./static/

# 安装运行时依赖
RUN pip install --no-cache-dir -r requirements.txt

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["python", "main.py"]