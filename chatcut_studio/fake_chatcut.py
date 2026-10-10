"""本地假 ChatCut MCP 服务器：没有 ChatCut 账号时，用来测试「API 模型直连」这条链路。

    python3 -m chatcut_studio.fake_chatcut          # 监听 http://127.0.0.1:8766/mcp

然后在设置页把 ChatCut MCP 地址改成 http://127.0.0.1:8766/mcp、认证选「不认证」。
工具名是模仿的，真实 ChatCut 的工具名以连接后列出的为准。
"""
from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("fake-chatcut")
STATE: dict = {"aspect_ratio": "16:9", "clips": [], "captions": [], "exports": 0}


@server.tool()
def get_project() -> dict:
    """读取当前项目设置和时间线概况"""
    return {"aspect_ratio": STATE["aspect_ratio"], "clips": len(STATE["clips"]), "captions": len(STATE["captions"])}


@server.tool()
def get_timeline() -> dict:
    """读取完整时间线"""
    return {"clips": STATE["clips"], "captions": STATE["captions"]}


@server.tool()
def update_project_settings(aspect_ratio: str = "9:16", resolution: str = "1080x1920", fps: int = 30) -> str:
    """修改项目画幅、分辨率和帧率"""
    STATE["aspect_ratio"] = aspect_ratio
    return f"项目已设为 {aspect_ratio} {resolution} {fps}fps"


@server.tool()
def add_clip(asset: str, start: float, duration: float, motion: str = "none") -> str:
    """把一个素材放到主视频轨"""
    STATE["clips"].append({"asset": asset, "start": start, "duration": duration, "motion": motion})
    return f"已添加 {asset}（{start}s 起，{duration}s）"


@server.tool()
def add_caption(text: str, start: float, duration: float, style: str = "") -> str:
    """添加一条字幕"""
    STATE["captions"].append({"text": text, "start": start, "duration": duration, "style": style})
    return f"已添加字幕「{text}」"


@server.tool()
def export_video(format: str = "mp4", resolution: str = "1080x1920") -> str:
    """导出成片（真实 ChatCut 里会消耗渲染资源）"""
    STATE["exports"] += 1
    return f"已导出 {format} {resolution}"


if __name__ == "__main__":
    import sys

    if "--stdio" in sys.argv:          # 模拟以 stdio 方式登记的本机 ChatCut Desktop
        server.run("stdio")
        raise SystemExit
    import uvicorn

    uvicorn.run(server.streamable_http_app(), host="127.0.0.1", port=int(os.environ.get("FAKE_CHATCUT_PORT", "8766")))
