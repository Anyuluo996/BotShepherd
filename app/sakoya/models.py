"""
早柚协议 (Sakoya) 数据模型
定义 GenshinUID-core 使用的 WebSocket 协议数据结构
使用 msgspec 以确保类型安全，与官方适配器保持一致
"""

from typing import Any, Dict, List, Literal, Optional
from msgspec import Struct
import msgspec
import json
import sys


class Message(Struct):
    """早柚协议消息段"""
    type: Optional[str] = None
    data: Optional[Any] = None


class Button(Struct):
    """早柚协议按钮"""
    text: str
    data: str
    pressed_text: Optional[str] = None
    style: Literal[0, 1] = 1
    action: Literal[0, 1, 2] = 2
    permisson: Literal[0, 1, 2, 3] = 2
    specify_role_ids: List[str] = []
    specify_user_ids: List[str] = []
    unsupport_tips: str = "您的客户端暂不支持该功能, 请升级后适配..."


class MessageReceive(Struct):
    """早柚协议消息上报"""
    bot_id: str = 'Bot'
    bot_self_id: str = ''
    msg_id: str = ''
    user_type: Literal['group', 'direct', 'channel', 'sub_channel'] = 'group'
    group_id: Optional[str] = None
    user_id: Optional[str] = None
    sender: Dict[str, Any] = {}
    user_pm: int = 3
    content: List[Message] = []


class MessageSend(Struct):
    """早柚协议消息发送"""
    bot_id: str = 'Bot'
    bot_self_id: str = ''
    msg_id: str = ''
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None


class SakowaConverter:
    """早柚协议转换器 - 在早柚协议和 OneBot v11 协议之间转换"""

    @staticmethod
    def onebot_event_to_sakoya(event: dict, bot_id: str = "Bot") -> Optional[bytes]:
        """
        将 OneBot v11 的消息事件转换为早柚协议的 MessageReceive 格式
        使用 msgspec.json.encode 确保类型安全

        Args:
            event: OneBot v11 消息事件
            bot_id: 早柚协议的 bot_id

        Returns:
            早柚协议 MessageReceive 的 msgspec 编码字节，如果不是消息事件则返回 None
        """
        post_type = event.get("post_type")

        # 只处理消息事件
        if post_type != "message":
            return None

        message_type = event.get("message_type", "")
        is_group = message_type == "group"

        # 转换消息段
        content = []
        for segment in event.get("message", []):
            if isinstance(segment, dict):
                seg_type = segment.get("type")
                seg_data = segment.get("data", {})

                if seg_type == "text":
                    content.append(Message(type="text", data=str(seg_data.get("text", ""))))
                elif seg_type == "at":
                    # 确保qq号转换为字符串
                    content.append(Message(type="at", data=str(seg_data.get("qq", ""))))
                elif seg_type == "image":
                    # OneBot image 格式: {file: "url" or "base64://...", url: "...", ...}
                    # 早柚协议图片格式: data 直接是字符串 "base64://..." 或 URL
                    # 优先使用 url 字段（如果存在）
                    img_url = seg_data.get("url", "")
                    img_file = seg_data.get("file", "")

                    # 选择最合适的图片源
                    img_source = img_url if img_url else img_file

                    # 早柚协议：data 直接是字符串（base64:// 或 http:// 或文件名）
                    if img_source.startswith("base64://"):
                        # base64 图片：直接传递 base64:// 字符串
                        content.append(Message(type="image", data=img_source))
                    elif img_source.startswith("http"):
                        # URL 图片：直接传递 URL
                        content.append(Message(type="image", data=img_source))
                    else:
                        # 文件名：优先使用 url 字段
                        if img_url:
                            content.append(Message(type="image", data=img_url))
                        else:
                            content.append(Message(type="image", data=img_source))
                elif seg_type == "record":
                    content.append(Message(type="record", data=str(seg_data.get("file", ""))))
                elif seg_type == "reply":
                    content.append(Message(type="reply", data=str(seg_data.get("id", ""))))
                else:
                    # 其他类型转为文本
                    content.append(Message(type="text", data=str(seg_data)))

        # 引用消息补全逻辑（参考 Koishi 早柚适配器）
        # 如果消息中包含引用（reply），尝试从被引用消息中提取图片等元素
        # 这是为了支持评分等功能需要图片上下文的场景
        # 参考：https://github.com/koishijs/koishi-plugin-adapter-satori/blob/master/src/message.ts#L135-152
        reply_data = event.get("reply")
        if reply_data and isinstance(reply_data, dict):
            # OneBot v11 扩展：某些实现（如 NapCat）可能在事件中提供 reply 字段
            # reply 字段包含被引用消息的完整内容
            reply_message = reply_data.get("message", [])
            if reply_message:
                # 遍历被引用消息的消息段，提取图片
                for seg in reply_message:
                    if isinstance(seg, dict):
                        seg_type = seg.get("type")
                        seg_data = seg.get("data", {})
                        if seg_type == "image" and seg_data:
                            img_file = seg_data.get("file", "")
                            # 将引用消息中的图片追加到消息内容中
                            # 这样早柚后端就能获取到完整的上下文
                            if img_file.startswith("base64://"):
                                content.append(Message(type="image", data={
                                    "type": "b64",
                                    "content": img_file[9:]
                                }))
                            elif img_file.startswith("http"):
                                content.append(Message(type="image", data={
                                    "type": "url",
                                    "content": img_file
                                }))
                            else:
                                content.append(Message(type="image", data={
                                    "type": "file",
                                    "content": img_file
                                }))

        # 转换发送者信息
        sender = event.get("sender", {})
        sakoya_sender = {
            "nickname": str(sender.get("nickname", "")),
            "card": str(sender.get("card", "")),
        }

        # 手动构造字典，确保所有 ID 字段都是字符串
        # 不使用 msgspec.Struct，直接用 dict 确保类型
        result = {
            "bot_id": str(bot_id),
            "bot_self_id": str(event.get("self_id", "")),
            "msg_id": str(event.get("message_id", "")),
            "user_type": "group" if is_group else "direct",
            "user_id": str(event.get("user_id", "")),
            "sender": sakoya_sender,
            "user_pm": 3,
            "content": [msg if isinstance(msg, dict) else {"type": msg.type, "data": msg.data} for msg in content]
        }

        # 只在群组消息中添加 group_id
        if is_group:
            result["group_id"] = str(event.get("group_id", ""))

        # 使用标准 json.dumps 编码（与官方适配器一致）
        json_str = json.dumps(result, ensure_ascii=False)
        encoded = json_str.encode('utf-8')
        return encoded

    @staticmethod
    def sakoya_to_onebot(message: MessageReceive) -> dict:
        """
        将早柚协议的 MessageReceive 转换为 OneBot v11 的消息事件格式

        Args:
            message: 早柚协议的 MessageReceive（用户发送的消息）

        Returns:
            OneBot v11 消息事件字典
        """
        # 提取字段
        is_group = message.user_type == 'group'
        user_type = message.user_type
        user_id = message.user_id
        group_id = message.group_id
        sender = message.sender or {}

        # 转换消息段
        message_segments = []
        raw_message_parts = []

        for msg in message.content:
            if msg.type == "text":
                text_content = str(msg.data) if msg.data else ""
                message_segments.append({
                    "type": "text",
                    "data": {"text": text_content}
                })
                raw_message_parts.append(text_content)

            elif msg.type == "at":
                at_id = str(msg.data) if msg.data else ""
                message_segments.append({
                    "type": "at",
                    "data": {"qq": at_id}
                })
                raw_message_parts.append(f"@{at_id}")

            elif msg.type == "image":
                # 早柚协议的 image 是一个 dict: {type: url|file|b64, content: ...}
                if isinstance(msg.data, dict):
                    img_type = msg.data.get("type", "url")
                    img_content = msg.data.get("content", "")

                    if img_type == "url":
                        message_segments.append({
                            "type": "image",
                            "data": {"file": img_content}
                        })
                    elif img_type == "b64":
                        if img_content.startswith("base64://"):
                            message_segments.append({
                                "type": "image",
                                "data": {"file": img_content}
                            })
                        else:
                            message_segments.append({
                                "type": "image",
                                "data": {"file": f"base64://{img_content}"}
                            })
                    elif img_type == "file":
                        message_segments.append({
                            "type": "image",
                            "data": {"file": img_content}
                        })
                raw_message_parts.append("[图片]")

            elif msg.type == "reply":
                reply_id = str(msg.data) if msg.data else ""
                message_segments.append({
                    "type": "reply",
                    "data": {"id": reply_id}
                })
                raw_message_parts.append("[回复]")

            elif msg.type == "record":
                if isinstance(msg.data, str):
                    message_segments.append({
                        "type": "record",
                        "data": {"file": msg.data}
                    })
                raw_message_parts.append("[语音]")

            elif msg.type == "file":
                # 早柚协议 file 格式: {文件名}|{文件base64}
                if isinstance(msg.data, str):
                    parts = msg.data.split("|", 1)
                    if len(parts) == 2:
                        file_name, file_b64 = parts
                        message_segments.append({
                            "type": "file",
                            "data": {"file": f"base64://{file_b64}", "name": file_name}
                        })
                raw_message_parts.append("[文件]")

            elif msg.type == "node":
                # 合并转发消息，展开处理
                if isinstance(msg.data, list):
                    for sub_msg in msg.data:
                        for sub_seg in sub_msg:
                            # 递归处理子消息
                            sub_msg_obj = Message(type=sub_seg.get("type"), data=sub_seg.get("data"))
                            # 简化处理，只提取文本
                            if sub_msg_obj.type == "text":
                                raw_message_parts.append(str(sub_msg_obj.data))

            elif msg.type == "markdown":
                text_content = str(msg.data) if msg.data else ""
                message_segments.append({
                    "type": "text",
                    "data": {"text": text_content}
                })
                raw_message_parts.append(text_content)

            elif msg.type == "buttons":
                # 按钮消息，转换为文本提示
                raw_message_parts.append("[按钮消息]")

            else:
                # 未知类型，保留为文本
                if msg.data:
                    raw_message_parts.append(str(msg.data))

        raw_message = "".join(raw_message_parts)

        # 构造发送者信息
        onebot_sender = {
            "user_id": int(user_id) if user_id and str(user_id).isdigit() else 0,
            "nickname": sender.get("nickname", ""),
            "card": sender.get("card", ""),
            "sex": sender.get("sex", "unknown"),
            "age": sender.get("age", 0),
            "area": sender.get("area", ""),
            "level": sender.get("level", ""),
            "role": sender.get("role", "member"),
            "title": sender.get("title", ""),
        }

        # 构造 OneBot v11 消息事件
        if is_group:
            event = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": int(message.msg_id) if message.msg_id and str(message.msg_id).isdigit() else 0,
                "group_id": int(group_id) if group_id and str(group_id).isdigit() else 0,
                "user_id": int(user_id) if user_id and str(user_id).isdigit() else 0,
                "raw_message": raw_message,
                "message": message_segments,
                "font": 0,
                "sender": onebot_sender,
                "time": 0,
                "self_id": int(message.bot_self_id) if message.bot_self_id and str(message.bot_self_id).isdigit() else 0,
            }
        else:
            event = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": int(message.msg_id) if message.msg_id and str(message.msg_id).isdigit() else 0,
                "user_id": int(user_id) if user_id and str(user_id).isdigit() else 0,
                "raw_message": raw_message,
                "message": message_segments,
                "font": 0,
                "sender": onebot_sender,
                "time": 0,
                "self_id": int(message.bot_self_id) if message.bot_self_id and str(message.bot_self_id).isdigit() else 0,
            }

        return event

    @staticmethod
    def sakoya_send_to_onebot_api(message: MessageSend) -> dict:
        """
        将早柚协议的 MessageSend 转换为 OneBot v11 的发送消息 API 调用

        Args:
            message: 早柚协议的 MessageSend

        Returns:
            OneBot v11 API 调用字典
        """
        import uuid

        # 确定消息类型和目标
        is_group = message.target_type == 'group'

        # 转换消息段
        message_segments = []
        for msg in message.content or []:
            if msg.type == "text":
                message_segments.append({
                    "type": "text",
                    "data": {"text": str(msg.data) if msg.data else ""}
                })

            elif msg.type == "at":
                message_segments.append({
                    "type": "at",
                    "data": {"qq": str(msg.data) if msg.data else ""}
                })

            elif msg.type == "image":
                # 早柚协议的 image 是一个 dict: {type: url|file|b64, content: ...}
                if isinstance(msg.data, dict):
                    img_type = msg.data.get("type", "url")
                    img_content = msg.data.get("content", "")

                    if img_type == "b64":
                        # base64 图片
                        if not img_content.startswith("base64://"):
                            img_content = f"base64://{img_content}"
                        message_segments.append({
                            "type": "image",
                            "data": {"file": img_content}
                        })
                    elif img_type == "url":
                        # URL 图片
                        message_segments.append({
                            "type": "image",
                            "data": {"file": img_content}
                        })
                    elif img_type == "file":
                        # 文件路径
                        message_segments.append({
                            "type": "image",
                            "data": {"file": img_content}
                        })
                else:
                    # 如果 data 不是 dict，可能是字符串（URL 或 base64）
                    img_str = str(msg.data) if msg.data else ""
                    if img_str:
                        message_segments.append({
                            "type": "image",
                            "data": {"file": img_str}
                        })

            elif msg.type == "reply":
                message_segments.append({
                    "type": "reply",
                    "data": {"id": str(msg.data) if msg.data else ""}
                })

            elif msg.type == "record":
                message_segments.append({
                    "type": "record",
                    "data": {"file": str(msg.data) if msg.data else ""}
                })

            elif msg.type == "file":
                # 早柚协议 file 格式: {文件名}|{文件base64}
                if isinstance(msg.data, str):
                    parts = msg.data.split("|", 1)
                    if len(parts) == 2:
                        file_name, file_b64 = parts
                        message_segments.append({
                            "type": "file",
                            "data": {"file": f"base64://{file_b64}", "name": file_name}
                        })

            elif msg.type == "markdown":
                # Markdown 转为文本
                message_segments.append({
                    "type": "text",
                    "data": {"text": str(msg.data) if msg.data else ""}
                })

            elif msg.type.startswith("log_"):
                # log 类型消息，跳过（仅用于日志输出）
                continue

            else:
                # 其他类型转为文本
                if msg.data:
                    message_segments.append({
                        "type": "text",
                        "data": {"text": str(msg.data)}
                    })

        # 如果消息段为空，添加一个空文本段（避免 NapCat 报错）
        if not message_segments:
            message_segments.append({
                "type": "text",
                "data": {"text": ""}
            })

        # 构造 OneBot v11 API 调用
        if is_group:
            api_call = {
                "action": "send_group_msg",
                "params": {
                    "group_id": int(message.target_id) if message.target_id and str(message.target_id).isdigit() else 0,
                    "message": message_segments
                },
                "echo": uuid.uuid4().hex
            }
        else:
            api_call = {
                "action": "send_private_msg",
                "params": {
                    "user_id": int(message.target_id) if message.target_id and str(message.target_id).isdigit() else 0,
                    "message": message_segments
                },
                "echo": uuid.uuid4().hex
            }

        return api_call

    @staticmethod
    def onebot_to_sakoya(message_data: dict) -> Optional[bytes]:
        """
        将 OneBot v11 的发送消息转换为早柚协议的 MessageSend
        使用 msgspec.json.encode 确保类型安全

        OneBot v11 send_msg API:
        - action: send_msg
        - params: {message_type: group/private, group_id/user_id, message: [...]}

        早柚协议 MessageSend:
        - target_type: group/direct
        - target_id: 目标ID
        - content: List[Message]
        """
        params = message_data.get("params", {})
        message_type = params.get("message_type", "")
        message_list = params.get("message", [])

        # 确定目标类型和ID
        if message_type == "group":
            target_type = "group"
            target_id = str(params.get("group_id", ""))
        else:
            target_type = "direct"
            target_id = str(params.get("user_id", ""))

        # 转换消息段
        content = []
        for segment in message_list:
            if isinstance(segment, dict):
                seg_type = segment.get("type")
                seg_data = segment.get("data", {})

                if seg_type == "text":
                    content.append(Message(type="text", data=seg_data.get("text", "")))

                elif seg_type == "at":
                    content.append(Message(type="at", data=seg_data.get("qq", "")))

                elif seg_type == "image":
                    # OneBot v11 的 image 可能是 file 或 url
                    img_file = seg_data.get("file", "")
                    if img_file.startswith("base64://"):
                        content.append(Message(type="image", data={
                            "type": "b64",
                            "content": img_file[9:]
                        }))
                    elif img_file.startswith("http"):
                        content.append(Message(type="image", data={
                            "type": "url",
                            "content": img_file
                        }))
                    else:
                        content.append(Message(type="image", data={
                            "type": "file",
                            "content": img_file
                        }))

                elif seg_type == "record":
                    content.append(Message(type="record", data=seg_data.get("file", "")))

                elif seg_type == "file":
                    file_data = seg_data.get("file", "")
                    file_name = seg_data.get("name", "unknown")
                    if file_data.startswith("base64://"):
                        content.append(Message(type="file", data=f"{file_name}|{file_data[9:]}"))
                    else:
                        # 对于非 base64 文件，作为文本处理
                        content.append(Message(type="text", data=f"[文件: {file_name}]"))

                elif seg_type == "reply":
                    content.append(Message(type="reply", data=seg_data.get("id", "")))

                elif seg_type == "forward" or seg_type == "node":
                    # 合并转发，早柚不建议使用，转为文本提示
                    content.append(Message(type="text", data="[合并转发消息暂不支持]"))

                else:
                    # 其他类型转为文本
                    content.append(Message(type="text", data=str(seg_data)))

        # 从消息数据中获取机器人信息
        self_id = str(message_data.get("self_id", ""))

        # 手动构造字典，确保所有字段都是字符串
        result = {
            "bot_id": "Bot",
            "bot_self_id": self_id,
            "msg_id": "",
            "target_type": target_type,
            "target_id": target_id,
            "content": [msg if isinstance(msg, dict) else {"type": msg.type, "data": msg.data} for msg in content]
        }

        # 使用标准 json.dumps 编码（与官方适配器一致）
        import json
        json_str = json.dumps(result, ensure_ascii=False)
        return json_str.encode('utf-8')
