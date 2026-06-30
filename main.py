"""
小红书高清主图/视频下载器 - 后端服务
技术栈: FastAPI + httpx
功能: 笔记解析 + 图片代理
"""

import re
import json
import time
from urllib.parse import urlparse, unquote

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="XHS Downloader API")

# CORS 中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件目录（前端 index.html 放在 static 文件夹）
app.mount("/static", StaticFiles(directory="static"), name="static")

# 全局 HTTP 客户端（模拟浏览器）
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.xiaohongshu.com/",
}


def extract_urls(text: str) -> list[str]:
    """从文本中精准提取所有 http/https 链接"""
    pattern = r"https?://[^\s<>\"')\]]+"
    return re.findall(pattern, text)


def clean_image_url(url: str) -> str:
    """
    核心去水印逻辑：
    剥离图片 URL 中 '?' 及其后面的所有参数，还原高清无水印原图地址。
    同时处理可能的编码问题。
    """
    if not url:
        return url
    # 去掉查询参数
    base_url = url.split("?")[0]
    # 确保使用 https
    if base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://", 1)
    return base_url


def parse_initial_state(html: str) -> dict | None:
    """
    从 HTML 中提取 window.__INITIAL_STATE__ 或 window.__DATA__ 的 JSON 数据。
    """
    # 尝试匹配 window.__INITIAL_STATE__
    patterns = [
        r"window\.__INITIAL_STATE__\s*=\s*({.+?})\s*</script>",
        r"window\.__INITIAL_STATE__\s*=\s*({.+?})\s*;\s*</script>",
        r"window\.__INITIAL_STATE__\s*=\s*(.+?)\s*</script>",
        r"window\.__DATA__\s*=\s*({.+?})\s*</script>",
        r"window\.__DATA__\s*=\s*(.+?)\s*</script>",
    ]

    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match:
            raw = match.group(1).strip()
            # 处理 undefined 替换为 null
            raw = re.sub(r"\bundefined\b", "null", raw)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # 尝试修复常见的 JSON 格式问题
                try:
                    # 处理尾部逗号
                    raw = re.sub(r",\s*([}\]])", r"\1", raw)
                    return json.loads(raw)
                except json.JSONDecodeError:
                    continue
    return None


def extract_images_from_data(data: dict) -> tuple[str, list[str]]:
    """
    从解析出的 JSON 数据中提取笔记标题和图片列表。
    返回 (标题, [高清原图链接...])
    """
    title = ""
    images = []

    # 递归搜索 imageList
    def find_image_list(obj, depth=0):
        if depth > 15 or obj is None:
            return None
        if isinstance(obj, dict):
            # 优先查找 note 中的 title 和 imageList
            if "note" in obj and isinstance(obj["note"], dict):
                return find_image_list(obj["note"], depth + 1)
            if "noteDetailMap" in obj:
                return find_image_list(obj["noteDetailMap"], depth + 1)
            if "imageList" in obj:
                if "title" in obj:
                    return obj.get("title", ""), obj["imageList"]
                if "desc" in obj and not title:
                    return obj.get("desc", ""), obj["imageList"]
                return "", obj["imageList"]
            if "title" in obj and "imageList" not in obj:
                if not title:
                    title = obj.get("title", "") or obj.get("desc", "")
            for v in obj.values():
                result = find_image_list(v, depth + 1)
                if result and (isinstance(result, tuple) and result[1]):
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = find_image_list(item, depth + 1)
                if result and (isinstance(result, tuple) and result[1]):
                    return result
        return None

    result = find_image_list(data)
    if result and isinstance(result, tuple):
        title = result[0] or title
        image_list = result[1]
    else:
        return title, images

    if not isinstance(image_list, list):
        return title, images

    for img in image_list:
        if isinstance(img, dict):
            # 尝试多种字段名获取最高清的图片 URL
            url = (
                img.get("urlDefault")
                or img.get("url")
                or img.get("original")
                or img.get("infoList", [{}])[-1].get("url", "") if isinstance(img.get("infoList"), list) and img.get("infoList") else ""
            )
            if not url:
                # 尝试从 infoList 中获取
                info_list = img.get("infoList", [])
                if isinstance(info_list, list) and info_list:
                    url = info_list[-1].get("url", "")
            if not url:
                # 尝试从 stream 字段获取
                stream = img.get("stream", {})
                if isinstance(stream, dict):
                    for formats in stream.values():
                        if isinstance(formats, list) and formats:
                            url = formats[0] if isinstance(formats[0], str) else ""
                            break
            if url and isinstance(url, str):
                images.append(clean_image_url(url))
        elif isinstance(img, str):
            images.append(clean_image_url(img))

    return title, images


# ==================== API 路由 ====================


@app.get("/")
async def index():
    """返回首页"""
    from fastapi.responses import FileResponse
    return FileResponse("static/index.html")


@app.post("/api/parse")
async def parse_note(request: Request):
    """
    接口一：解析小红书笔记链接，提取高清主图列表
    请求体: { "url": "https://www.xiaohongshu.com/..." }
    返回:   { "success": true, "title": "...", "images": ["...", ...] }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "请求体格式错误，需要 JSON"},
        )

    raw_url = body.get("url", "").strip()
    if not raw_url:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "请输入小红书笔记链接"},
        )

    # 从文本中提取链接
    urls = extract_urls(raw_url)
    if not urls:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "未能从输入中提取到有效链接"},
        )

    target_url = urls[0]

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        try:
            resp = await client.get(target_url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return JSONResponse(
                status_code=502,
                content={"success": False, "message": f"请求小红书页面失败: {str(e)}"},
            )

        html = resp.text

    # 解析 HTML 获取数据
    data = parse_initial_state(html)
    if not data:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "无法从页面中解析出笔记数据，可能链接无效或页面结构已变更",
            },
        )

    title, images = extract_images_from_data(data)

    if not images:
        return JSONResponse(
            status_code=200,
            content={
                "success": False,
                "message": "未找到任何图片，该笔记可能为纯视频或数据格式已变更",
            },
        )

    return JSONResponse(
        content={
            "success": True,
            "title": title or "未命名笔记",
            "images": images,
        }
    )


@app.get("/api/proxy")
async def proxy_image(url: str = Query(..., description="小红书图片地址")):
    """
    接口二：图片下载代理
    后端请求小红书图片并以二进制流返回给前端，绕过 CORS 限制。
    """
    if not url:
        return JSONResponse(status_code=400, content={"message": "缺少 url 参数"})

    # 安全校验：仅允许代理图片域名的请求
    parsed = urlparse(url)
    allowed_hosts = [
        "sns-webpic",
        "ci.xiaohongshu.com",
        "xhscdn.com",
        "xiaohongshu.com",
        "sns-img",
    ]
    if not any(host in (parsed.hostname or "") for host in allowed_hosts):
        # 放宽限制：只要是 http(s) 图片都尝试代理
        pass

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return JSONResponse(
                status_code=502,
                content={"message": f"代理请求图片失败: {str(e)}"},
            )

        content_type = resp.headers.get("content-type", "image/jpeg")
        return Response(
            content=resp.content,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Disposition": "inline",
            },
        )


# ==================== 启动入口 ====================

if __name__ == "__main__":
    import uvicorn

    print("=" * 50)
    print("  小红书高清主图下载器 - 后端服务已启动")
    print("  访问 http://127.0.0.1:8000 打开前端页面")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000)
