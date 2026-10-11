"""OneBot v11 (aiocqhttp) 群管理 LLM 工具集。

本插件把 OneBot v11 的群管理接口封装成 AstrBot 的 LLM 工具，并支持「分群启用功能」：
每个群可以单独配置哪些功能类别可用，未登记的群按全局策略处理。

所有工具都只作用于「当前会话所在的群」，不接受 LLM 传入的 group_id，
以避免提示词注入导致跨群操作。

语音转文字不注册成 LLM 工具，而是通过 on_llm_request 钩子在请求发出前自动把
语音（含回复引用的语音）转成文字注入上下文，参考 astrbot_plugin_mimo_media 的做法。
"""

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes, Record, Reply
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

PLUGIN_NAME = "astrbot_plugin_group_tools"

FEATURES: dict[str, str] = {
    "card": "群名片与头衔",
    "mute": "禁言管理",
    "essence": "群精华消息",
    "notice": "群公告",
    "album": "群相册",
    "portrait": "群头像",
    "interact": "互动（贴表情/戳一戳）",
    "file": "群文件",
    "stt": "语音自动转文字",
}

# 指令里可以用的中文别名 -> 功能 key
FEATURE_ALIASES: dict[str, str] = {
    "名片": "card",
    "群名片": "card",
    "头衔": "card",
    "禁言": "mute",
    "全体禁言": "mute",
    "被禁言": "mute",
    "精华": "essence",
    "群精华": "essence",
    "公告": "notice",
    "群公告": "notice",
    "相册": "album",
    "群相册": "album",
    "头像": "portrait",
    "群头像": "portrait",
    "互动": "interact",
    "表情": "interact",
    "戳一戳": "interact",
    "文件": "file",
    "群文件": "file",
    "语音": "stt",
    "语音转文字": "stt",
    "自动转文字": "stt",
    "stt": "stt",
}

# 列表类结果最多返回多少条，避免撑爆上下文
LIST_LIMIT = 50

# 递归遍历消息链（回复 / 合并转发）的最大深度
MAX_COMPONENT_DEPTH = 8

# build_main_agent 生成的音频占位符前缀，转写成功后会把这些占位符换成真实文本
_AUDIO_PLACEHOLDER_PREFIXES = (
    "[Audio Attachment:",
    "[Audio Attachment in quoted message:",
)

# 需要转成整数的 OneBot 参数
_INT_KEYS = {
    "group_id",
    "user_id",
    "target_id",
    "message_id",
    "emoji_id",
    "duration",
    "self_id",
}

# 插件实例，供工具调用时读取配置（AstrBot 每个插件只会有一个实例）
_PLUGIN: "GroupToolsPlugin | None" = None


def _s(description: str) -> dict:
    """构造字符串类型的参数 schema。"""
    return {"type": "string", "description": description}


def _i(description: str) -> dict:
    """构造整数类型的参数 schema。"""
    return {"type": "integer", "description": description}


def _b(description: str) -> dict:
    """构造布尔类型的参数 schema。"""
    return {"type": "boolean", "description": description}


_PARAMS: dict[str, dict] = {
    # ---------------- 群名片与头衔 ----------------
    "set_group_card": {
        "type": "object",
        "properties": {
            "user_id": _s("目标成员的 QQ 号。"),
            "card": _s("新的群名片。传空字符串表示取消该成员的群名片。"),
        },
        "required": ["user_id", "card"],
    },
    "set_group_special_title": {
        "type": "object",
        "properties": {
            "user_id": _s("目标成员的 QQ 号。"),
            "special_title": _s("新的群头衔。传空字符串表示取消群头衔。"),
            "duration": _i("头衔有效期（秒），不填由协议端决定。"),
        },
        "required": ["user_id", "special_title"],
    },
    # ---------------- 禁言管理 ----------------
    "set_group_ban": {
        "type": "object",
        "properties": {
            "user_id": _s("要被禁言的成员 QQ 号。"),
            "duration": _i("禁言时长（秒）。传 0 表示解除禁言。"),
        },
        "required": ["user_id", "duration"],
    },
    "set_group_whole_ban": {
        "type": "object",
        "properties": {
            "enable": _b("true 表示开启全体禁言，false 表示解除全体禁言。"),
        },
        "required": ["enable"],
    },
    "get_group_shut_list": {"type": "object", "properties": {}},
    # ---------------- 群精华消息 ----------------
    "get_essence_msg_list": {"type": "object", "properties": {}},
    "set_essence_msg": {
        "type": "object",
        "properties": {
            "message_id": _s("要设为精华的消息 ID。不填则使用当前这条消息。"),
        },
    },
    "delete_essence_msg": {
        "type": "object",
        "properties": {
            "message_id": _s("要移出精华的消息 ID。不填则使用当前这条消息。"),
        },
    },
    # ---------------- 群公告 ----------------
    "send_group_notice": {
        "type": "object",
        "properties": {
            "content": _s("公告正文文本。"),
            "image": _s("公告配图，支持 http://、file://、base64://，可留空。"),
            "pinned": _b("是否置顶该公告。"),
        },
        "required": ["content"],
    },
    "get_group_notice": {"type": "object", "properties": {}},
    "delete_group_notice": {
        "type": "object",
        "properties": {
            "notice_id": _s("要删除的公告 ID，可先调用获取群公告拿到。"),
        },
        "required": ["notice_id"],
    },
    # ---------------- 群相册 ----------------
    "create_group_album": {
        "type": "object",
        "properties": {
            "name": _s("相册名称。"),
            "desc": _s("相册描述，可留空。"),
        },
        "required": ["name"],
    },
    "get_group_album_list": {"type": "object", "properties": {}},
    "delete_group_album": {
        "type": "object",
        "properties": {"album_id": _s("要删除的相册 ID。")},
        "required": ["album_id"],
    },
    "upload_group_album": {
        "type": "object",
        "properties": {
            "album_id": _s("目标相册 ID。"),
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要上传的图片路径或 URL 列表。",
            },
        },
        "required": ["album_id", "files"],
    },
    "get_group_album_media_list": {
        "type": "object",
        "properties": {
            "album_id": _s("相册 ID。"),
            "attach_info": _s("翻页游标，取上一次返回的 next_attach_info，可留空。"),
        },
        "required": ["album_id"],
    },
    # ---------------- 群头像 ----------------
    "set_group_portrait": {
        "type": "object",
        "properties": {
            "file": _s("群头像图片，支持 http://、file://、base64://。"),
        },
        "required": ["file"],
    },
    # ---------------- 互动 ----------------
    "set_msg_emoji_like": {
        "type": "object",
        "properties": {
            "emoji_id": _i("表情 ID（QQ 官方表情 ID 表）。"),
            "message_id": _s("要贴表情的消息 ID。不填则使用当前这条消息。"),
            "set": _b("true 贴表情，false 取消贴表情。默认 true。"),
        },
        "required": ["emoji_id"],
    },
    "send_poke": {
        "type": "object",
        "properties": {
            "user_id": _s("要戳的成员 QQ 号。不填则戳当前消息的发送者。"),
        },
    },
    # ---------------- 群文件 ----------------
    "upload_group_file": {
        "type": "object",
        "properties": {
            "file": _s("要上传的本地文件路径或 URL。"),
            "name": _s("上传后的文件名，不填则使用原文件名。"),
            "folder_id": _s("目标文件夹 ID，不填则上传到根目录。"),
        },
        "required": ["file"],
    },
    "set_group_file_forever": {
        "type": "object",
        "properties": {"file_id": _s("群文件 ID。")},
        "required": ["file_id"],
    },
    "delete_group_file": {
        "type": "object",
        "properties": {"file_id": _s("要删除的群文件 ID。")},
        "required": ["file_id"],
    },
    "move_group_file": {
        "type": "object",
        "properties": {
            "file_id": _s("要移动的群文件 ID。"),
            "parent_directory": _s("文件当前所在文件夹 ID，根目录传 /。"),
            "target_directory": _s("目标文件夹 ID，根目录传 /。"),
        },
        "required": ["file_id", "parent_directory", "target_directory"],
    },
    "create_group_file_folder": {
        "type": "object",
        "properties": {"name": _s("新建文件夹的名称。")},
        "required": ["name"],
    },
    "delete_group_file_folder": {
        "type": "object",
        "properties": {"folder_id": _s("要删除的文件夹 ID。")},
        "required": ["folder_id"],
    },
    "get_group_file_system_info": {"type": "object", "properties": {}},
    "get_group_root_files": {"type": "object", "properties": {}},
    "get_group_files_by_folder": {
        "type": "object",
        "properties": {"folder_id": _s("文件夹 ID。")},
        "required": ["folder_id"],
    },
    "rename_group_file_folder": {
        "type": "object",
        "properties": {
            "folder_id": _s("文件夹 ID。"),
            "new_folder_name": _s("新的文件夹名。"),
        },
        "required": ["folder_id", "new_folder_name"],
    },
    "rename_group_file": {
        "type": "object",
        "properties": {
            "file_id": _s("群文件 ID。"),
            "current_parent_directory": _s("文件当前所在文件夹 ID，根目录传 /。"),
            "new_name": _s("新的文件名。"),
        },
        "required": ["file_id", "current_parent_directory", "new_name"],
    },
}


def _dump(value: Any, limit: int = 4000) -> str:
    """把结果序列化成便于 LLM 阅读的紧凑 JSON 文本。"""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return text[:limit] + f"…（已截断，原长 {len(text)} 字符）"
    return text


def _unwrap(result: Any) -> Any:
    """兼容 OneBot 的 {status, retcode, data} 包装，取出真正的 data。

    不同协议端返回形式不一致：有的直接给 data，有的给完整信封。
    """
    if not isinstance(result, dict):
        return result
    if "retcode" in result or "status" in result:
        return result.get("data")
    if "data" in result:
        return result["data"]
    return result


def _human_size(size: Any) -> str:
    """把字节数格式化成人类可读的大小。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return str(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return str(size)


def _fmt_time(value: Any) -> str:
    """把 Unix 秒级时间戳格式化成可读时间。"""
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return str(value)
    if timestamp <= 0:
        return "0"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(value)


def _fmt_files(data: Any) -> str:
    """格式化群文件目录列表（get_group_root_files / get_group_files_by_folder）。"""
    if not isinstance(data, dict):
        return _dump(data, 3000)
    folders = data.get("folders") if isinstance(data.get("folders"), list) else []
    files = data.get("files") if isinstance(data.get("files"), list) else []
    if not folders and not files:
        return "该目录下没有文件夹和文件。"

    lines: list[str] = [f"文件夹共 {len(folders)} 个："]
    for folder in folders[:LIST_LIMIT]:
        lines.append(
            f"- folder_id={folder.get('folder_id')} 名称={folder.get('folder_name')} "
            f"文件数={folder.get('total_file_count')} 创建者={folder.get('creator_name')}",
        )
    if len(folders) > LIST_LIMIT:
        lines.append(f"（文件夹已截断，仅显示前 {LIST_LIMIT} 个）")

    lines.append(f"文件共 {len(files)} 个：")
    for item in files[:LIST_LIMIT]:
        dead_time = item.get("dead_time")
        lines.append(
            f"- file_id={item.get('file_id')} 名称={item.get('file_name')} "
            f"大小={_human_size(item.get('file_size'))} busid={item.get('busid')} "
            f"上传者={item.get('uploader_name') or item.get('uploader')} "
            f"下载次数={item.get('download_times')} "
            f"永久文件={'是' if not dead_time else '否'}",
        )
    if len(files) > LIST_LIMIT:
        lines.append(f"（文件已截断，仅显示前 {LIST_LIMIT} 个）")
    return "\n".join(lines)


def _fmt_essence(data: Any) -> str:
    """格式化群精华消息列表。"""
    if not isinstance(data, list):
        return _dump(data, 3000)
    if not data:
        return "当前群没有精华消息。"
    lines: list[str] = [f"共 {len(data)} 条精华消息："]
    for item in data[:LIST_LIMIT]:
        lines.append(
            f"- message_id={item.get('message_id')} "
            f"发送者={item.get('sender_nick')}({item.get('sender_id')}) "
            f"发送时间={_fmt_time(item.get('sender_time'))} "
            f"设为精华的人={item.get('operator_nick')}({item.get('operator_id')}) "
            f"操作时间={_fmt_time(item.get('operator_time'))}",
        )
    if len(data) > LIST_LIMIT:
        lines.append(f"（已截断，仅显示前 {LIST_LIMIT} 条）")
    return "\n".join(lines)


def _fmt_notices(data: Any) -> str:
    """格式化群公告列表。"""
    if not isinstance(data, list):
        return _dump(data, 3000)
    if not data:
        return "当前群没有群公告。"
    lines: list[str] = [f"共 {len(data)} 条群公告："]
    for item in data[:LIST_LIMIT]:
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        text = str(message.get("text") or "").strip().replace("\n", " ")
        images = message.get("images")
        lines.append(
            f"- notice_id={item.get('notice_id')} 发布者={item.get('sender_id')} "
            f"发布时间={_fmt_time(item.get('publish_time'))} "
            f"图片数={len(images) if isinstance(images, list) else 0}\n"
            f"  内容：{text[:200] or '（无文本）'}",
        )
    if len(data) > LIST_LIMIT:
        lines.append(f"（已截断，仅显示前 {LIST_LIMIT} 条）")
    return "\n".join(lines)


def _fmt_albums(data: Any) -> str:
    """格式化群相册列表。"""
    if not isinstance(data, list):
        return _dump(data, 3000)
    if not data:
        return "当前群没有相册。"
    lines: list[str] = [f"共 {len(data)} 个相册："]
    for item in data[:LIST_LIMIT]:
        lines.append(
            f"- album_id={item.get('album_id')} 名称={item.get('name')} "
            f"图片数={item.get('upload_number')} 描述={item.get('desc') or '无'}",
        )
    if len(data) > LIST_LIMIT:
        lines.append(f"（已截断，仅显示前 {LIST_LIMIT} 个）")
    return "\n".join(lines)


def _fmt_album_media(data: Any) -> str:
    """格式化群相册媒体列表。"""
    if not isinstance(data, dict):
        return _dump(data, 3000)
    album = data.get("album") if isinstance(data.get("album"), dict) else {}
    media_list = (
        data.get("media_list") if isinstance(data.get("media_list"), list) else []
    )
    lines: list[str] = [
        f"相册：{album.get('name') or '未知'}（album_id={album.get('album_id')}）",
        f"媒体共 {len(media_list)} 项：",
    ]
    for index, item in enumerate(media_list[:LIST_LIMIT]):
        image = item.get("image") if isinstance(item.get("image"), dict) else {}
        default_url = (
            image.get("default_url")
            if isinstance(image.get("default_url"), dict)
            else {}
        )
        lines.append(
            f"- [{index}] type={item.get('type')} 上传者={item.get('uploader')} "
            f"上传时间={_fmt_time(item.get('upload_time'))} 描述={item.get('desc') or '无'} "
            f"链接={default_url.get('url') or '无'}",
        )
    if len(media_list) > LIST_LIMIT:
        lines.append(f"（已截断，仅显示前 {LIST_LIMIT} 项）")
    if data.get("next_has_more"):
        lines.append(f"还有更多，翻页请传 attach_info={data.get('next_attach_info')}")
    return "\n".join(lines)


def _fmt_file_system(data: Any) -> str:
    """格式化群文件系统信息。"""
    if not isinstance(data, dict):
        return _dump(data, 3000)
    return (
        f"文件数量：{data.get('file_count')} / 上限 {data.get('limit_count')}\n"
        f"已用空间：{_human_size(data.get('used_space'))}\n"
        f"总空间：{_human_size(data.get('total_space'))}"
    )


def _format_result(formatter: str, action: str, result: Any) -> str:
    """把 OneBot 返回值转换成给 LLM 的文本。"""
    data = _unwrap(result)
    if formatter == "ok":
        if data in (None, {}, []):
            return f"{action} 调用成功。"
        return f"{action} 调用成功。返回：{_dump(data, 600)}"
    if formatter == "files":
        return _fmt_files(data)
    if formatter == "essence":
        return _fmt_essence(data)
    if formatter == "notices":
        return _fmt_notices(data)
    if formatter == "albums":
        return _fmt_albums(data)
    if formatter == "album_media":
        return _fmt_album_media(data)
    if formatter == "file_system":
        return _fmt_file_system(data)
    return _dump(data, 4000)


def _resolve_call_action(event: AstrMessageEvent) -> Any | None:
    """取出 OneBot 客户端的 call_action。

    aiocqhttp 的 CQHttp 既支持 `bot.api.call_action(...)`，也通过 __getattr__
    代理支持 `bot.call_action(...)`，这里两种都兼容。

    Args:
        event: 当前消息事件。

    Returns:
        可调用的 call_action，取不到时返回 None。
    """
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", None)
    call_action = getattr(api, "call_action", None)
    if callable(call_action):
        return call_action
    call_action = getattr(bot, "call_action", None)
    return call_action if callable(call_action) else None


def _walk_components(
    components: list[Any] | None,
    *,
    reply_id: str | int | None = None,
    in_forward: bool = False,
    depth: int = 0,
) -> Iterator[tuple[Any, str | int | None, bool]]:
    """递归遍历消息链，展开回复引用与合并转发里的消息段。

    Args:
        components: 当前层的消息段列表。
        reply_id: 若当前层来自被引用的消息，这里是那条消息的 message_id。
        in_forward: 当前层是否位于合并转发内部。
        depth: 当前递归深度，超过 MAX_COMPONENT_DEPTH 就停止。

    Yields:
        (消息段, 所属被引用消息的 message_id, 是否在合并转发内)。
    """
    if not isinstance(components, list) or depth > MAX_COMPONENT_DEPTH:
        return
    for component in components:
        yield component, reply_id, in_forward
        if isinstance(component, Reply) and component.chain:
            yield from _walk_components(
                component.chain,
                reply_id=component.id,
                in_forward=in_forward,
                depth=depth + 1,
            )
        elif isinstance(component, Node):
            yield from _walk_components(
                component.content,
                reply_id=reply_id,
                in_forward=True,
                depth=depth + 1,
            )
        elif isinstance(component, Nodes):
            for node in component.nodes:
                yield node, reply_id, True
                yield from _walk_components(
                    node.content,
                    reply_id=reply_id,
                    in_forward=True,
                    depth=depth + 1,
                )


@dataclass
class _GroupTool(FunctionTool[AstrAgentContext]):
    """所有群管理工具的基类，只负责声明元信息，实际执行走 _execute_tool。"""

    feature: str = ""
    action: str = ""
    write: bool = False
    formatter: str = "json"
    optional_keys: tuple[str, ...] = ()
    group_scoped: bool = True
    default_message_id: bool = False
    default_user_to_sender: bool = False

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        return await _execute_tool(self, context, kwargs)


async def _execute_tool(
    tool: _GroupTool,
    context: ContextWrapper[AstrAgentContext],
    kwargs: dict[str, Any],
) -> ToolExecResult:
    """统一的工具执行入口：平台检查 -> 分群开关检查 -> 权限检查 -> 调用协议端。"""
    plugin = _PLUGIN
    if plugin is None:
        return "error: 插件尚未初始化，请稍后重试。"

    event = context.context.event

    if event.get_platform_name() != "aiocqhttp":
        return "error: 当前会话不是 OneBot v11 (aiocqhttp) 平台，无法调用群管理接口。"

    call_action = _resolve_call_action(event)
    if call_action is None:
        return "error: 无法获取 OneBot 客户端实例（call_action 不可用）。"

    group_id = str(event.get_group_id() or "").strip()
    if tool.group_scoped and not group_id:
        return "error: 该功能只能在群聊中使用。"

    if group_id:
        allowed, reason = plugin.check_feature(group_id, tool.feature)
        if not allowed:
            return f"error: {reason}"

    if tool.write and plugin.require_admin_for_write and not event.is_admin():
        return (
            "error: 该操作需要 AstrBot 管理员权限。"
            f"你的 QQ 号：{event.get_sender_id()}。请让管理员在 WebUI 主配置的 admins_id 中加入该 QQ 号。"
        )

    params: dict[str, Any] = {
        key: value for key, value in kwargs.items() if value is not None
    }
    for key in tool.optional_keys:
        if params.get(key) == "":
            params.pop(key, None)

    if tool.default_message_id and not params.get("message_id"):
        params["message_id"] = event.message_obj.message_id
    if tool.default_user_to_sender and not params.get("user_id"):
        params["user_id"] = event.get_sender_id()
    if group_id:
        params["group_id"] = group_id

    # 多账号场景需要带上 self_id 做路由，否则会串号
    self_id = getattr(event.message_obj, "self_id", None)
    if self_id:
        params.setdefault("self_id", self_id)

    for key, value in list(params.items()):
        if key in _INT_KEYS and isinstance(value, str):
            stripped = value.strip()
            if stripped.lstrip("-").isdigit():
                params[key] = int(stripped)

    try:
        result = await call_action(tool.action, **params)
    except Exception as exc:  # noqa: BLE001 - 协议端错误统一转成 LLM 可读文本
        logger.warning(f"[{PLUGIN_NAME}] 调用 {tool.action} 失败，参数={params}：{exc}")
        return f"error: 调用 {tool.action} 失败：{exc}"

    return _format_result(tool.formatter, tool.action, result)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------


@dataclass
class SetGroupCardTool(_GroupTool):
    name: str = "onebot_set_group_card"
    description: str = (
        "设置指定群成员的群名片（群昵称）。只能在当前群聊中使用。"
        "card 传空字符串表示取消该成员的群名片。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["set_group_card"])
    feature: str = "card"
    action: str = "set_group_card"
    write: bool = True
    formatter: str = "ok"


@dataclass
class SetGroupSpecialTitleTool(_GroupTool):
    name: str = "onebot_set_group_special_title"
    description: str = (
        "设置指定群成员的群头衔（专属头衔）。只能在当前群聊中使用，需要机器人是群主或管理员。"
        "special_title 传空字符串表示取消群头衔。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["set_group_special_title"])
    feature: str = "card"
    action: str = "set_group_special_title"
    write: bool = True
    formatter: str = "ok"
    optional_keys: tuple[str, ...] = ("duration",)


@dataclass
class SetGroupBanTool(_GroupTool):
    name: str = "onebot_set_group_ban"
    description: str = (
        "禁言当前群里的某个成员。duration 是禁言秒数，传 0 表示解除禁言。"
        "常见时长：60（1 分钟）、600（10 分钟）、3600（1 小时）、86400（1 天）。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["set_group_ban"])
    feature: str = "mute"
    action: str = "set_group_ban"
    write: bool = True
    formatter: str = "ok"


@dataclass
class SetGroupWholeBanTool(_GroupTool):
    name: str = "onebot_set_group_whole_ban"
    description: str = (
        "开启或解除当前群的全体禁言。enable=true 开启，enable=false 解除。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["set_group_whole_ban"])
    feature: str = "mute"
    action: str = "set_group_whole_ban"
    write: bool = True
    formatter: str = "ok"


@dataclass
class GetGroupShutListTool(_GroupTool):
    name: str = "onebot_get_group_shut_list"
    description: str = "获取当前群里被禁言的成员列表。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["get_group_shut_list"])
    feature: str = "mute"
    action: str = "get_group_shut_list"
    formatter: str = "json"


@dataclass
class GetEssenceMsgListTool(_GroupTool):
    name: str = "onebot_get_essence_msgs"
    description: str = (
        "获取当前群的精华消息列表，返回每条精华消息的 message_id、发送者与操作者。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["get_essence_msg_list"])
    feature: str = "essence"
    action: str = "get_essence_msg_list"
    formatter: str = "essence"


@dataclass
class SetEssenceMsgTool(_GroupTool):
    name: str = "onebot_set_essence_msg"
    description: str = (
        "把当前群的一条消息设为精华消息。不传 message_id 时使用当前这条消息。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["set_essence_msg"])
    feature: str = "essence"
    action: str = "set_essence_msg"
    write: bool = True
    formatter: str = "ok"
    optional_keys: tuple[str, ...] = ("message_id",)
    default_message_id: bool = True


@dataclass
class DeleteEssenceMsgTool(_GroupTool):
    name: str = "onebot_delete_essence_msg"
    description: str = (
        "把当前群的一条消息移出精华。不传 message_id 时使用当前这条消息。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["delete_essence_msg"])
    feature: str = "essence"
    action: str = "delete_essence_msg"
    write: bool = True
    formatter: str = "ok"
    optional_keys: tuple[str, ...] = ("message_id",)
    default_message_id: bool = True


@dataclass
class SendGroupNoticeTool(_GroupTool):
    name: str = "onebot_send_group_notice"
    description: str = (
        "在当前群发送一条群公告。content 是公告正文，image 可选（图片地址）。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["send_group_notice"])
    feature: str = "notice"
    action: str = "_send_group_notice"
    write: bool = True
    formatter: str = "ok"
    optional_keys: tuple[str, ...] = ("image", "pinned")


@dataclass
class GetGroupNoticeTool(_GroupTool):
    name: str = "onebot_get_group_notice"
    description: str = "获取当前群的群公告列表，返回 notice_id、发布时间与正文。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["get_group_notice"])
    feature: str = "notice"
    action: str = "_get_group_notice"
    formatter: str = "notices"


@dataclass
class DeleteGroupNoticeTool(_GroupTool):
    name: str = "onebot_delete_group_notice"
    description: str = "删除当前群的一条群公告。notice_id 可通过获取群公告得到。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["delete_group_notice"])
    feature: str = "notice"
    action: str = "_delete_group_notice"
    write: bool = True
    formatter: str = "ok"


@dataclass
class CreateGroupAlbumTool(_GroupTool):
    name: str = "onebot_create_group_album"
    description: str = "在当前群创建一个相册，返回新建相册的 album_id。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["create_group_album"])
    feature: str = "album"
    action: str = "create_group_album"
    write: bool = True
    formatter: str = "ok"
    optional_keys: tuple[str, ...] = ("desc",)


@dataclass
class GetGroupAlbumListTool(_GroupTool):
    name: str = "onebot_get_group_album_list"
    description: str = "获取当前群的相册列表，返回每个相册的 album_id、名称与图片数量。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["get_group_album_list"])
    feature: str = "album"
    action: str = "get_group_album_list"
    formatter: str = "albums"


@dataclass
class DeleteGroupAlbumTool(_GroupTool):
    name: str = "onebot_delete_group_album"
    description: str = "删除当前群的一个相册。album_id 可通过获取群相册列表得到。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["delete_group_album"])
    feature: str = "album"
    action: str = "delete_group_album"
    write: bool = True
    formatter: str = "ok"


@dataclass
class UploadGroupAlbumTool(_GroupTool):
    name: str = "onebot_upload_group_album"
    description: str = (
        "向当前群的指定相册上传图片。files 是图片路径或 URL 的列表。"
        "需要机器人是该相册的创建者或有上传权限。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["upload_group_album"])
    feature: str = "album"
    action: str = "upload_group_album"
    write: bool = True
    formatter: str = "ok"


@dataclass
class GetGroupAlbumMediaListTool(_GroupTool):
    name: str = "onebot_get_group_album_media"
    description: str = "获取当前群某个相册里的媒体列表。翻页时把上一次返回的 next_attach_info 作为 attach_info 传入。"
    parameters: dict = Field(
        default_factory=lambda: _PARAMS["get_group_album_media_list"]
    )
    feature: str = "album"
    action: str = "get_group_album_media_list"
    formatter: str = "album_media"
    optional_keys: tuple[str, ...] = ("attach_info",)


@dataclass
class SetGroupPortraitTool(_GroupTool):
    name: str = "onebot_set_group_portrait"
    description: str = (
        "设置当前群的群头像。file 支持 http://、file://、base64:// 三种形式。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["set_group_portrait"])
    feature: str = "portrait"
    action: str = "set_group_portrait"
    write: bool = True
    formatter: str = "ok"


@dataclass
class SetMsgEmojiLikeTool(_GroupTool):
    name: str = "onebot_set_msg_emoji_like"
    description: str = (
        "给一条消息贴表情回应（贴表情）。emoji_id 是 QQ 官方表情 ID。"
        "不传 message_id 时对当前这条消息贴表情；set=false 表示取消。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["set_msg_emoji_like"])
    feature: str = "interact"
    action: str = "set_msg_emoji_like"
    write: bool = True
    formatter: str = "ok"
    group_scoped: bool = False
    optional_keys: tuple[str, ...] = ("message_id", "set")
    default_message_id: bool = True


@dataclass
class SendPokeTool(_GroupTool):
    name: str = "onebot_send_poke"
    description: str = "戳一戳某个成员（双击头像）。不传 user_id 时戳当前消息的发送者。群聊和私聊都可用。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["send_poke"])
    feature: str = "interact"
    action: str = "send_poke"
    write: bool = True
    formatter: str = "ok"
    group_scoped: bool = False
    optional_keys: tuple[str, ...] = ("user_id",)
    default_user_to_sender: bool = True


@dataclass
class UploadGroupFileTool(_GroupTool):
    name: str = "onebot_upload_group_file"
    description: str = (
        "上传文件到当前群的群文件。file 是协议端所在机器上的文件路径或 URL，"
        "folder_id 可选（不传则上传到根目录）。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["upload_group_file"])
    feature: str = "file"
    action: str = "upload_group_file"
    write: bool = True
    formatter: str = "ok"
    optional_keys: tuple[str, ...] = ("name", "folder_id")


@dataclass
class SetGroupFileForeverTool(_GroupTool):
    name: str = "onebot_set_group_file_forever"
    description: str = (
        "把当前群的一个群文件转为永久文件。file_id 可通过获取群文件列表得到。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["set_group_file_forever"])
    feature: str = "file"
    action: str = "set_group_file_forever"
    write: bool = True
    formatter: str = "ok"


@dataclass
class DeleteGroupFileTool(_GroupTool):
    name: str = "onebot_delete_group_file"
    description: str = "删除当前群的一个群文件。file_id 可通过获取群文件列表得到。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["delete_group_file"])
    feature: str = "file"
    action: str = "delete_group_file"
    write: bool = True
    formatter: str = "ok"


@dataclass
class MoveGroupFileTool(_GroupTool):
    name: str = "onebot_move_group_file"
    description: str = (
        "把当前群的一个群文件移动到另一个文件夹。"
        "parent_directory 是文件当前所在文件夹 ID，target_directory 是目标文件夹 ID，根目录传 /。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["move_group_file"])
    feature: str = "file"
    action: str = "move_group_file"
    write: bool = True
    formatter: str = "ok"


@dataclass
class CreateGroupFileFolderTool(_GroupTool):
    name: str = "onebot_create_group_file_folder"
    description: str = "在当前群的群文件里新建一个文件夹，返回新建文件夹的 folder_id。"
    parameters: dict = Field(
        default_factory=lambda: _PARAMS["create_group_file_folder"]
    )
    feature: str = "file"
    action: str = "create_group_file_folder"
    write: bool = True
    formatter: str = "ok"


@dataclass
class DeleteGroupFileFolderTool(_GroupTool):
    name: str = "onebot_delete_group_file_folder"
    description: str = (
        "删除当前群的一个群文件文件夹。folder_id 可通过获取群文件列表得到。"
    )
    parameters: dict = Field(
        default_factory=lambda: _PARAMS["delete_group_file_folder"]
    )
    feature: str = "file"
    action: str = "delete_group_folder"
    write: bool = True
    formatter: str = "ok"


@dataclass
class GetGroupFileSystemInfoTool(_GroupTool):
    name: str = "onebot_get_group_file_system_info"
    description: str = (
        "获取当前群群文件系统的信息：文件数量、数量上限、已用空间、总空间。"
    )
    parameters: dict = Field(
        default_factory=lambda: _PARAMS["get_group_file_system_info"]
    )
    feature: str = "file"
    action: str = "get_group_file_system_info"
    formatter: str = "file_system"


@dataclass
class GetGroupRootFilesTool(_GroupTool):
    name: str = "onebot_get_group_root_files"
    description: str = (
        "获取当前群群文件根目录下的文件夹和文件列表，返回 folder_id / file_id。"
    )
    parameters: dict = Field(default_factory=lambda: _PARAMS["get_group_root_files"])
    feature: str = "file"
    action: str = "get_group_root_files"
    formatter: str = "files"


@dataclass
class GetGroupFilesByFolderTool(_GroupTool):
    name: str = "onebot_get_group_files_by_folder"
    description: str = "获取当前群指定群文件文件夹下的文件夹和文件列表。"
    parameters: dict = Field(
        default_factory=lambda: _PARAMS["get_group_files_by_folder"]
    )
    feature: str = "file"
    action: str = "get_group_files_by_folder"
    formatter: str = "files"


@dataclass
class RenameGroupFileFolderTool(_GroupTool):
    name: str = "onebot_rename_group_file_folder"
    description: str = "重命名当前群的一个群文件文件夹。"
    parameters: dict = Field(
        default_factory=lambda: _PARAMS["rename_group_file_folder"]
    )
    feature: str = "file"
    action: str = "rename_group_file_folder"
    write: bool = True
    formatter: str = "ok"


@dataclass
class RenameGroupFileTool(_GroupTool):
    name: str = "onebot_rename_group_file"
    description: str = "重命名当前群的一个群文件。current_parent_directory 是文件所在文件夹 ID，根目录传 /。"
    parameters: dict = Field(default_factory=lambda: _PARAMS["rename_group_file"])
    feature: str = "file"
    action: str = "rename_group_file"
    write: bool = True
    formatter: str = "ok"


_TOOL_CLASSES: tuple[type[_GroupTool], ...] = (
    SetGroupCardTool,
    SetGroupSpecialTitleTool,
    SetGroupBanTool,
    SetGroupWholeBanTool,
    GetGroupShutListTool,
    GetEssenceMsgListTool,
    SetEssenceMsgTool,
    DeleteEssenceMsgTool,
    SendGroupNoticeTool,
    GetGroupNoticeTool,
    DeleteGroupNoticeTool,
    CreateGroupAlbumTool,
    GetGroupAlbumListTool,
    DeleteGroupAlbumTool,
    UploadGroupAlbumTool,
    GetGroupAlbumMediaListTool,
    SetGroupPortraitTool,
    SetMsgEmojiLikeTool,
    SendPokeTool,
    UploadGroupFileTool,
    SetGroupFileForeverTool,
    DeleteGroupFileTool,
    MoveGroupFileTool,
    CreateGroupFileFolderTool,
    DeleteGroupFileFolderTool,
    GetGroupFileSystemInfoTool,
    GetGroupRootFilesTool,
    GetGroupFilesByFolderTool,
    RenameGroupFileFolderTool,
    RenameGroupFileTool,
)


class GroupToolsPlugin(Star):
    """OneBot v11 群管理工具集（分群启用功能）。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        global _PLUGIN
        _PLUGIN = self

        self.config: AstrBotConfig | dict = config if config is not None else {}

        if self._config_bool("enable_llm_tools", True):
            self.context.add_llm_tools(*(tool_cls() for tool_cls in _TOOL_CLASSES))
            self.logger.info(
                f"[{PLUGIN_NAME}] 已注册 {len(_TOOL_CLASSES)} 个群管理 LLM 工具。",
            )
        else:
            self.logger.info(f"[{PLUGIN_NAME}] 已关闭 LLM 工具注册。")

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------

    def _config_bool(self, key: str, default: bool) -> bool:
        """读取布尔配置项。"""
        value = self.config.get(key, default)
        return bool(default if value is None else value)

    @property
    def require_admin_for_write(self) -> bool:
        """写操作是否只允许 AstrBot 管理员。"""
        return self._config_bool("require_admin_for_write", True)

    @property
    def unconfigured_group_policy(self) -> str:
        """未登记群聊的策略：allow 或 deny。"""
        policy = str(self.config.get("unconfigured_group_policy") or "allow").strip()
        return policy if policy in {"allow", "deny"} else "allow"

    def _group_entries(self) -> list:
        """返回配置中的分群开关列表，必要时初始化。"""
        groups = self.config.get("groups")
        if not isinstance(groups, list):
            groups = []
            self.config["groups"] = groups
        return groups

    def _find_group_entry(self, group_id: str) -> dict | None:
        """按群号查找分群配置项。"""
        for entry in self._group_entries():
            if (
                isinstance(entry, dict)
                and str(entry.get("group_id", "")).strip() == group_id
            ):
                return entry
        return None

    def _ensure_group_entry(self, group_id: str) -> dict:
        """按群号取出分群配置项，不存在则新建。"""
        entry = self._find_group_entry(group_id)
        if entry is None:
            entry = {"__template_key": "group", "group_id": group_id, "enabled": True}
            for key in FEATURES:
                entry[key] = True
            self._group_entries().append(entry)
        return entry

    def check_feature(self, group_id: str, feature: str) -> tuple[bool, str]:
        """检查某个群是否启用了某个功能类别。

        Args:
            group_id: 群号。
            feature: 功能类别 key，见 FEATURES。

        Returns:
            (是否允许, 不允许时的原因)。允许时原因为空字符串。
        """
        label = FEATURES.get(feature, feature)
        entry = self._find_group_entry(group_id)
        if entry is None:
            if self.unconfigured_group_policy == "allow":
                return True, ""
            return False, f"功能「{label}」未在当前群启用（该群未在插件配置中登记）。"
        if not bool(entry.get("enabled", True)):
            return False, "本插件在当前群已被禁用。"
        if not bool(entry.get(feature, True)):
            return False, f"功能「{label}」未在当前群启用。"
        return True, ""

    # ------------------------------------------------------------------
    # 语音自动转文字
    # 参考 astrbot_plugin_mimo_media 的 llonebot_stt 模式：不注册 LLM 工具，
    # 而是在 LLM 请求发出前调用协议端 voice_msg_to_text，把转写结果注入上下文。
    # ------------------------------------------------------------------

    @filter.on_llm_request(priority=10)
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """把本次请求里的语音（含回复引用的语音）转成文字注入上下文。

        Args:
            event: 当前消息事件。
            req: 即将发给大模型的请求对象，会被就地修改。
        """
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            if not self._config_bool("enable_auto_stt", True):
                return

            group_id = str(event.get_group_id() or "").strip()
            if group_id:
                allowed, _ = self.check_feature(group_id, "stt")
                if not allowed:
                    return

            call_action = _resolve_call_action(event)
            if call_action is None:
                return

            targets = self._collect_audio_targets(event)
            if not targets:
                return

            transcriptions = await self._transcribe_audio(call_action, targets)
            if not transcriptions:
                req.extra_user_content_parts.append(
                    TextPart(text="[语音转文字失败，已跳过]"),
                )
                return

            self._inject_transcriptions(req, transcriptions)
        except Exception as exc:  # noqa: BLE001 - 钩子异常不能影响正常对话
            self.logger.warning(f"[{PLUGIN_NAME}] 语音转文字失败：{exc}")

    @staticmethod
    def _collect_audio_targets(
        event: AstrMessageEvent,
    ) -> list[tuple[str | int, bool]]:
        """收集本次请求里需要转写的语音。

        Args:
            event: 当前消息事件。

        Returns:
            (message_id, 是否来自回复引用) 列表，按消息链顺序排列。
        """
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        direct_message_id = None
        if isinstance(raw_message, dict):
            direct_message_id = raw_message.get("message_id") or raw_message.get("id")
        if direct_message_id is None:
            direct_message_id = getattr(message_obj, "message_id", None)

        targets: list[tuple[str | int, bool]] = []
        for component, reply_id, in_forward in _walk_components(
            getattr(message_obj, "message", []) or [],
        ):
            # 合并转发里的语音拿不到可靠的 message_id，跳过
            if not isinstance(component, Record) or in_forward:
                continue
            message_id = reply_id or direct_message_id
            if message_id is None or not str(message_id).strip():
                continue
            targets.append((message_id, reply_id is not None))
        return targets

    async def _transcribe_audio(
        self,
        call_action: Any,
        targets: list[tuple[str | int, bool]],
    ) -> list[tuple[str, bool]]:
        """逐个调用协议端 voice_msg_to_text 转写。

        Args:
            call_action: OneBot 客户端的 call_action 方法。
            targets: _collect_audio_targets 的返回值。

        Returns:
            (转写文本, 是否来自回复引用) 列表，按去重后的顺序排列。
        """
        transcriptions: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for message_id, is_reply in targets:
            key = str(message_id)
            if key in seen:
                continue
            seen.add(key)

            api_message_id: str | int = message_id
            if isinstance(message_id, str) and message_id.isdigit():
                api_message_id = int(message_id)

            try:
                result = await call_action(
                    action="voice_msg_to_text",
                    message_id=api_message_id,
                )
            except Exception as exc:  # noqa: BLE001 - 单条语音失败不影响其它语音
                self.logger.warning(
                    f"[{PLUGIN_NAME}] voice_msg_to_text 失败，message_id={message_id}：{exc}",
                )
                continue

            data = _unwrap(result)
            text = data.get("text") if isinstance(data, dict) else None
            if isinstance(text, str) and text.strip():
                transcriptions.append((text.strip(), is_reply))
            else:
                self.logger.warning(
                    f"[{PLUGIN_NAME}] voice_msg_to_text 返回空文本，message_id={message_id}",
                )
        return transcriptions

    @staticmethod
    def _inject_transcriptions(
        req: ProviderRequest,
        transcriptions: list[tuple[str, bool]],
    ) -> None:
        """把转写结果注入请求，并清理 AstrBot 生成的音频占位符。

        回复语音时替换被引用消息里的 `[Empty Text]`，直接发语音时追加一段文本。

        Args:
            req: 即将发给大模型的请求对象。
            transcriptions: _transcribe_audio 的返回值。
        """
        for text, is_reply in transcriptions:
            if is_reply:
                for part in req.extra_user_content_parts:
                    if not isinstance(part, TextPart):
                        continue
                    if "<Quoted Message>" not in part.text:
                        continue
                    if "[Empty Text]" not in part.text:
                        continue
                    part.text = part.text.replace("[Empty Text]", text, 1)
                    break
                else:
                    req.extra_user_content_parts.append(TextPart(text=text))
            else:
                req.extra_user_content_parts.append(TextPart(text=text))

        # 已经拿到文字，就不必再把原始音频文件塞给模型
        req.audio_urls = []
        req.extra_user_content_parts = [
            part
            for part in req.extra_user_content_parts
            if not (
                isinstance(part, TextPart)
                and part.text.startswith(_AUDIO_PLACEHOLDER_PREFIXES)
            )
        ]

    # ------------------------------------------------------------------
    # 指令：/群管
    # ------------------------------------------------------------------

    @filter.command_group("群管", alias={"群管理", "grouptools"})
    def group_admin(self):
        """群管理工具集的分群开关管理。"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @group_admin.command("状态", alias={"status"})
    async def status(self, event: AstrMessageEvent):
        """查看当前群各功能类别的启用状态。"""
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            yield event.plain_result("该指令只能在群聊中使用。")
            return
        entry = self._find_group_entry(group_id)
        lines = [f"群 {group_id} 的功能开关："]
        if entry is None:
            lines.append(f"（未登记，按策略「{self.unconfigured_group_policy}」处理）")
        else:
            lines.append(
                f"插件总开关：{'开' if bool(entry.get('enabled', True)) else '关'}"
            )
        for key, label in FEATURES.items():
            if entry is None:
                state = "开" if self.unconfigured_group_policy == "allow" else "关"
            else:
                state = "开" if bool(entry.get(key, True)) else "关"
            lines.append(f"- {label}（{key}）：{state}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @group_admin.command("开启", alias={"enable"})
    async def enable_feature(self, event: AstrMessageEvent, feature: str):
        """开启当前群的某个功能，用法：/群管 开启 群文件。"""
        await self._toggle_feature(event, feature, True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @group_admin.command("关闭", alias={"disable"})
    async def disable_feature(self, event: AstrMessageEvent, feature: str):
        """关闭当前群的某个功能，用法：/群管 关闭 群文件。"""
        await self._toggle_feature(event, feature, False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @group_admin.command("开启全部")
    async def enable_all(self, event: AstrMessageEvent):
        """开启当前群的全部功能。"""
        await self._toggle_all(event, True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @group_admin.command("关闭全部")
    async def disable_all(self, event: AstrMessageEvent):
        """关闭当前群的全部功能。"""
        await self._toggle_all(event, False)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @group_admin.command("功能", alias={"features", "列表"})
    async def list_features(self, event: AstrMessageEvent):
        """列出可用的功能类别及其 key。"""
        lines = ["可用功能类别（用 key 或中文名都可以）："]
        lines.extend(f"- {label}（{key}）" for key, label in FEATURES.items())
        yield event.plain_result("\n".join(lines))

    async def _toggle_feature(
        self,
        event: AstrMessageEvent,
        feature: str,
        enabled: bool,
    ) -> None:
        """切换当前群某个功能类别的开关并落盘。"""
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            await event.send(event.plain_result("该指令只能在群聊中使用。"))
            return

        key = self._resolve_feature(feature)
        if key is None:
            await event.send(
                event.plain_result(
                    f"未知功能「{feature}」。可用：{'、'.join(FEATURES.values())}",
                ),
            )
            return

        entry = self._ensure_group_entry(group_id)
        entry[key] = enabled
        await self._save_config()
        state = "已开启" if enabled else "已关闭"
        await event.send(
            event.plain_result(f"群 {group_id} 的「{FEATURES[key]}」{state}。"),
        )

    async def _toggle_all(self, event: AstrMessageEvent, enabled: bool) -> None:
        """切换当前群全部功能类别的开关并落盘。"""
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            await event.send(event.plain_result("该指令只能在群聊中使用。"))
            return

        entry = self._ensure_group_entry(group_id)
        entry["enabled"] = True
        for key in FEATURES:
            entry[key] = enabled
        await self._save_config()
        state = "已开启" if enabled else "已关闭"
        await event.send(event.plain_result(f"群 {group_id} 的全部功能{state}。"))

    @staticmethod
    def _resolve_feature(feature: str) -> str | None:
        """把用户输入的功能名解析成功能 key。"""
        text = (feature or "").strip()
        if text in FEATURES:
            return text
        return FEATURE_ALIASES.get(text)

    async def _save_config(self) -> None:
        """把内存中的配置写回配置文件。"""
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            self.logger.warning(
                f"[{PLUGIN_NAME}] 当前配置对象不支持保存，改动仅在内存中生效。"
            )
            return
        try:
            await asyncio.to_thread(save)
        except Exception as exc:  # noqa: BLE001 - 保存失败不应该让指令崩溃
            self.logger.error(f"[{PLUGIN_NAME}] 保存配置失败：{exc}")
