"""
早柚协议 WebSocket 适配器
自动在早柚协议和 OneBot v11 协议之间转换
使用 msgspec 进行类型安全的序列化
"""

import json
import msgspec
from typing import Any, Optional

from ..sakoya.models import MessageReceive, MessageSend, SakowaConverter


class SakoyaWebSocketAdapter:
    """
    早柚协议 WebSocket 适配器
    包装原始 WebSocket 连接，自动进行协议转换
    """

    # 需要直接透传的消息类型（不转换）
    PASSTHROUGH_ACTIONS = {
        'get_login_info',
        'get_status',
        'get_version_info',
        'lifecycle',
        '_connect',  # Yunzai 连接消息
    }

    def __init__(self, ws, bot_id: str, logger):
        """
        初始化适配器

        Args:
            ws: 原始 WebSocket 连接
            bot_id: 早柚协议的 bot_id (从路径 /ws/{bot_id} 提取)
            logger: 日志记录器
        """
        self._ws = ws
        self._bot_id = str(bot_id)  # 强制转为字符串
        self._logger = logger
        self._remote_address = getattr(ws, 'remote_address', None)
        self._path = getattr(ws, 'path', None)

        # 消息历史缓存（用于引用消息补全）
        # 格式: {message_id: message_segments}
        # 最多缓存 100 条消息
        from collections import OrderedDict
        self._message_cache = OrderedDict()
        self._max_cache_size = 100

        # self._logger.ws.info(f"[Sakoya] 早柚协议适配器已启动 (bot_id={bot_id})")

    async def _send_bytes(self, data: bytes):
        """发送字节格式的消息"""
        import websockets.exceptions
        try:
            # websockets 库的 send 方法支持 bytes
            if len(data) < 1000:
                # 仅调试时打印详细日志，避免日志爆炸
                self._logger.ws.debug(f"[Sakoya] 发送数据: {data.decode('utf-8', errors='replace')}")
            await self._ws.send(data)
        except websockets.exceptions.ConnectionClosed as e:
            # 连接已关闭，记录日志并重新抛出异常
            self._logger.ws.warning(f"[Sakoya] 发送字节时连接已关闭: code={e.code}, reason={e.reason}")
            raise
        except Exception as e:
            self._logger.ws.error(f"[Sakoya] 发送字节失败: {e}")
            # 如果发送字节失败，尝试发送字符串
            try:
                await self._ws.send(data.decode('utf-8'))
            except websockets.exceptions.ConnectionClosed as e2:
                # 连接已关闭，记录日志并重新抛出异常
                self._logger.ws.warning(f"[Sakoya] 回退到字符串时连接已关闭: code={e2.code}, reason={e2.reason}")
                raise
            except Exception as e2:
                self._logger.ws.error(f"[Sakoya] 回退到字符串也失败: {e2}")
                raise

    async def _process_reply_message(self, event: dict):
        """
        处理引用消息，从被引用消息中提取图片并追加到当前消息中
        参考 Koishi 早柚适配器的实现

        使用消息历史缓存来获取被引用消息的内容，避免调用 API 导致消息流混乱

        Args:
            event: OneBot v11 消息事件（会被原地修改）
        """
        try:
            # 先将当前消息添加到缓存中
            message_id = str(event.get("message_id", ""))
            message_segments = event.get("message", [])

            if message_id:
                self._message_cache[message_id] = message_segments
                # 限制缓存大小
                if len(self._message_cache) > self._max_cache_size:
                    self._message_cache.popitem(last=False)  # 删除最旧的消息

            # 检查消息中是否有 reply 消息段
            reply_id = None
            for seg in message_segments:
                if isinstance(seg, dict) and seg.get("type") == "reply":
                    reply_id = str(seg.get("data", {}).get("id", ""))
                    break

            if not reply_id:
                # 没有引用消息，直接返回
                return

            self._logger.ws.debug(f"[Sakoya] 检测到引用消息，reply_id={reply_id}")

            # 从缓存中查找被引用的消息
            if reply_id in self._message_cache:
                reply_message_segments = self._message_cache[reply_id]
                self._logger.ws.debug(f"[Sakoya] 从缓存中找到被引用消息，包含 {len(reply_message_segments)} 个消息段")

                # 收集被引用消息中的图片
                image_segments = []
                for seg in reply_message_segments:
                    if isinstance(seg, dict) and seg.get("type") == "image":
                        seg_data = seg.get("data", {})
                        img_url = seg_data.get("url", "")

                        # 直接使用图片 URL（不下载转 base64）
                        if img_url:
                            # 创建图片消息段（URL 格式）
                            new_seg = {
                                "type": "image",
                                "data": {
                                    "url": img_url
                                }
                            }
                            image_segments.append(new_seg)
                            self._logger.ws.debug(f"[Sakoya] 从被引用消息中提取图片 URL: {img_url[:100]}")
                        else:
                            # 没有 URL，使用原始消息段
                            image_segments.append(seg)
                            self._logger.ws.debug(f"[Sakoya] 从被引用消息中提取图片（无 URL）")

                # 如果找到了图片，重新组织消息段：移除 reply，将图片放在最前面
                if image_segments:
                    # 移除 reply 消息段
                    new_message = []
                    for seg in message_segments:
                        if not (isinstance(seg, dict) and seg.get("type") == "reply"):
                            new_message.append(seg)

                    # 将图片插入到最前面
                    event["message"] = image_segments + new_message
                    self._logger.ws.debug(f"[Sakoya] 已将 {len(image_segments)} 张图片插入到消息最前面，移除了 reply 消息段")
            else:
                self._logger.ws.debug(f"[Sakoya] 缓存中未找到被引用消息 (reply_id={reply_id})，可能消息太旧或缓存已满")

        except Exception as e:
            self._logger.ws.error(f"[Sakoya] 处理引用消息失败: {e}")
            import traceback
            self._logger.ws.error(f"[Sakoya] 错误堆栈: {traceback.format_exc()}")

    @property
    def remote_address(self):
        """获取远程地址"""
        return self._remote_address

    @property
    def path(self):
        """获取路径"""
        return self._path

    async def send(self, message: Any):
        """
        发送消息（OneBot v11 格式 -> 早柚协议格式）
        早柚协议使用字节格式传输

        Args:
            message: OneBot v11 格式的消息（dict 或 str）
        """
        try:
            # 解析消息
            if isinstance(message, str):
                try:
                    message_data = json.loads(message)
                except json.JSONDecodeError as e:
                    # 非 JSON 消息，直接发送（转为字节）
                    self._logger.ws.debug(f"[Sakoya] 非 JSON 消息，转为字节发送: {message[:200]}")
                    await self._send_bytes(message.encode('utf-8'))
                    return
            else:
                message_data = message

            # 检查是否是 API 响应（直接透传）
            if "echo" in message_data or "retcode" in message_data or "status" in message_data:
                self._logger.ws.debug(f"[Sakoya] 检测到 API 响应，直接透传")
                json_str = json.dumps(message_data, ensure_ascii=False)
                await self._send_bytes(json_str.encode('utf-8'))
                return

            # 优先检查是否是消息事件（post_type）
            post_type = message_data.get("post_type", "")

            # 检查是否是需要跳过的元事件
            if post_type == "meta_event":
                self._logger.ws.debug(f"[Sakoya] 检测到元事件，跳过")
                return

            # 检查是否是消息事件
            if post_type == "message":
                # 引用消息补全逻辑（参考 Koishi 早柚适配器）
                # 检查消息中是否有 reply 消息段，如果有，调用 get_msg API 获取被引用消息的内容
                # 然后将被引用消息中的图片追加到当前消息中
                await self._process_reply_message(message_data)

                # onebot_event_to_sakoya 现在返回 msgspec.json.encode 的 bytes
                sakoya_bytes = SakowaConverter.onebot_event_to_sakoya(message_data, self._bot_id)
                if sakoya_bytes:
                    await self._send_bytes(sakoya_bytes)
                    return
                else:
                    self._logger.ws.warning(f"[Sakoya] 消息事件转换失败")
                    # 转换失败，直接发送原始消息
                    json_str = json.dumps(message_data, ensure_ascii=False)
                    await self._send_bytes(json_str.encode('utf-8'))
                    return

            # 如果不是消息事件，检查是否是 API 调用
            action = message_data.get("action", "")

            # 检查是否是需要透传的 API 类型（不转换）
            if action in self.PASSTHROUGH_ACTIONS:
                self._logger.ws.debug(f"[Sakoya] 检测到透传 API 类型: {action}，直接发送 OneBot 格式")
                json_str = json.dumps(message_data, ensure_ascii=False)
                await self._send_bytes(json_str.encode('utf-8'))
                return

            # 检查是否是发送消息相关的 API
            is_send_message = "send" in action and "_msg" in action

            if not is_send_message:
                # 非发送消息的 API，直接透传
                self._logger.ws.debug(f"[Sakoya] 非发送消息 API ({action})，直接透传")
                json_str = json.dumps(message_data, ensure_ascii=False)
                await self._send_bytes(json_str.encode('utf-8'))
                return

            # 处理发送消息 API（send_private_msg、send_group_msg 等）
            # 将 OneBot v11 发送消息 API 转换为早柚协议
            sakoya_bytes = SakowaConverter.onebot_to_sakoya(message_data)

            if sakoya_bytes:
                await self._send_bytes(sakoya_bytes)
            else:
                # 转换失败或不需要转换，直接发送
                self._logger.ws.warning(f"[Sakoya] API 转换返回 None，直接发送原始消息")
                json_str = json.dumps(message_data, ensure_ascii=False)
                await self._send_bytes(json_str.encode('utf-8'))

        except Exception as e:
            self._logger.ws.error(f"[Sakoya] 发送消息转换失败: {e}")
            import traceback
            self._logger.ws.error(f"[Sakoya] 错误堆栈: {traceback.format_exc()}")
            # 失败时尝试直接发送
            try:
                if isinstance(message, str):
                    await self._send_bytes(message.encode('utf-8'))
                else:
                    json_str = json.dumps(message, ensure_ascii=False)
                    await self._send_bytes(json_str.encode('utf-8'))
                self._logger.ws.error(f"[Sakoya] <<< send() 异常恢复 - 已尝试直接发送")
            except Exception as e2:
                self._logger.ws.error(f"[Sakoya] 直接发送也失败: {e2}")

    async def recv(self):
        """
        接收消息（早柚协议格式 -> OneBot v11 格式）
        使用 msgspec.json.decode 确保类型安全

        Returns:
            OneBot v11 格式的消息（str）
        """
        try:
            # 接收原始消息
            raw_message = await self._ws.recv()

            # 尝试使用 msgspec 解析为 JSON
            try:
                # 使用 msgspec.json.decode 解析，指定类型为 MessageSend
                message = msgspec.json.decode(raw_message, type=MessageSend)

                # MessageSend 转换为 OneBot v11 API 调用
                try:
                    onebot_api = SakowaConverter.sakoya_send_to_onebot_api(message)
                except Exception as conv_error:
                    self._logger.ws.error(f"[Sakoya] 转换失败: {conv_error}")
                    import traceback
                    self._logger.ws.error(f"[Sakoya] 转换错误堆栈: {traceback.format_exc()}")
                    raise

                # 返回 OneBot v11 API 调用的 JSON 字符串
                result = json.dumps(onebot_api, ensure_ascii=False)
                return result

            except msgspec.DecodeError as decode_error:
                # msgspec 解析失败，尝试作为普通 JSON
                try:
                    message_data = json.loads(raw_message)

                    # 检查是否是早柚协议的 MessageReceive
                    if "bot_id" in message_data and "content" in message_data:
                        # 早柚协议消息，转换为 OneBot v11
                        sakoya_message = MessageReceive(**message_data)
                        onebot_event = SakowaConverter.sakoya_to_onebot(sakoya_message)

                        # 返回 OneBot v11 格式的 JSON 字符串
                        return json.dumps(onebot_event, ensure_ascii=False)

                    # 不是早柚协议，直接返回
                    return raw_message

                except json.JSONDecodeError:
                    # 不是 JSON，直接返回
                    return raw_message

        except websockets.exceptions.ConnectionClosedOK as e:
            # 正常关闭，只记录 debug 级别
            self._logger.ws.debug(f"[Sakoya] 连接已正常关闭: {e}")
            raise
        except websockets.exceptions.ConnectionClosed as e:
            # 异常关闭，记录 warning 级别
            self._logger.ws.warning(f"[Sakoya] 连接异常关闭: {e}")
            raise
        except Exception as e:
            self._logger.ws.error(f"[Sakoya] 接收消息转换失败: {e}")
            import traceback
            self._logger.ws.error(f"[Sakoya] 错误堆栈: {traceback.format_exc()}")
            raise

    async def close(self, *args, **kwargs):
        """关闭连接"""
        return await self._ws.close(*args, **kwargs)

    def __aiter__(self):
        """支持 async for 迭代"""
        return self

    async def __anext__(self):
        """异步迭代器的下一个元素"""
        try:
            message = await self.recv()
            return message
        except Exception:
            raise StopAsyncIteration

    def __getattr__(self, name):
        """转发其他属性到原始 WebSocket"""
        return getattr(self._ws, name)


def is_sakoya_path(path: str) -> bool:
    """
    检查路径是否是早柚协议路径

    早柚协议路径格式: /ws/{bot_id}

    Args:
        path: WebSocket 路径

    Returns:
        是否是早柚协议路径
    """
    if not path:
        return False

    # 标准化路径
    if not path.startswith('/'):
        path = '/' + path

    # 检查是否匹配 /ws/{bot_id} 格式
    parts = path.split('/')
    if len(parts) >= 3 and parts[1] == 'ws':
        return True

    return False


def extract_bot_id_from_path(path: str) -> Optional[str]:
    """
    从早柚协议路径中提取 bot_id

    Args:
        path: WebSocket 路径 (例如: /ws/NoneBot2)

    Returns:
        bot_id 或 None
    """
    if not path:
        return None

    # 标准化路径
    if not path.startswith('/'):
        path = '/' + path

    # 检查是否匹配 /ws/{bot_id} 格式
    parts = path.split('/')
    if len(parts) >= 3 and parts[1] == 'ws':
        return parts[2]

    return None
