"""
WebSocket代理服务器
实现一对多WebSocket代理功能
"""

import asyncio
import websockets
import json
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from app.onebotv11.message_segment import MessageSegmentParser

from ..onebotv11.models import ApiResponse, Event
from ..commands import CommandHandler
from .message_processor import MessageProcessor
from ..utils.reboot import construct_reboot_message

class ProxyServer:
    """WebSocket代理服务器"""

    def __init__(self, config_manager, database_manager, logger, backup_manager=None):
        self.config_manager = config_manager
        self.database_manager = database_manager
        self.logger = logger
        self.backup_manager = backup_manager

        # 连接管理
        self.active_connections = {}  # connection_id -> ProxyConnection
        self.connection_locks = {}     # connection_id -> asyncio.Lock (防止竞态条件)
        self.running = False
        
    async def start(self):
        """启动代理服务器"""
        self.running = True
        self.logger.ws.info("启动WebSocket代理服务器...")
        
        # 获取连接配置
        connections_config = self.config_manager.get_connections_config()
        
        # 为每个连接配置启动代理
        tasks = []
        for connection_id, config in connections_config.items():
            if config.get("enabled", False):
                task = asyncio.create_task(
                    self._start_connection_proxy(connection_id, config)
                )
                tasks.append(task)
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            self.logger.ws.warning("没有启用的连接配置")
            # 保持运行状态
            while self.running:
                await asyncio.sleep(1)
    
    async def _start_connection_proxy(self, connection_id: str, config: Dict[str, Any]):
        """启动单个连接的代理"""
        try:
            # 解析客户端监听地址
            client_endpoint = config["client_endpoint"]
            # 提取主机和端口
            if client_endpoint.startswith("ws://"):
                url_part = client_endpoint[5:]  # 移除 "ws://"
                if "/" in url_part:
                    host_port, path = url_part.split("/", 1)
                else:
                    host_port = url_part
                    path = ""
                
                if ":" in host_port:
                    host, port = host_port.split(":", 1)
                    port = int(port)
                else:
                    host = host_port
                    port = 80
            else:
                raise ValueError(f"不支持的客户端端点格式: {client_endpoint}")
            
            self.logger.ws.info(f"启动连接代理 {connection_id}: {host}:{port}")
            
            # 创建处理器函数
            async def connection_handler(ws):
                # 记录 WebSocket 连接详细信息
                ws_info = {
                    "remote_address": getattr(ws, 'remote_address', None),
                    "path": getattr(ws, 'path', '/'),
                    "host": getattr(ws, 'host', None),
                    "port": getattr(ws, 'port', None),
                    "id": id(ws),  # Python 对象 ID
                }
                self.logger.ws.info(f"[{connection_id}] 收到新的WebSocket连接: {ws_info}")

                # 从WebSocket连接中获取路径
                path = ws.path if hasattr(ws, 'path') else "/"

                # 获取或创建该 connection_id 的锁（防止竞态条件）
                if connection_id not in self.connection_locks:
                    self.connection_locks[connection_id] = asyncio.Lock()

                async with self.connection_locks[connection_id]:
                    if connection_id in self.active_connections:
                        old_conn = self.active_connections[connection_id]
                        old_ws = old_conn.client_ws

                        # 检查旧连接是否真的还活着 - 使用 state 属性
                        old_state = getattr(old_ws, 'state', None) if old_ws else None
                        is_old_alive = old_ws and old_state == 1  # 1 = OPEN 状态

                        if is_old_alive:
                            # 旧连接还活着，拒绝新连接（防止频繁重连）
                            old_ip = getattr(old_ws, 'remote_address', 'unknown')
                            new_ip = getattr(ws, 'remote_address', 'unknown')
                            self.logger.ws.warning(
                                f"[{connection_id}] 已存在活跃连接 (旧:{old_ip} vs 新:{new_ip})，拒绝新连接"
                            )
                            await ws.close(1008, "Connection already exists")
                            return
                        else:
                            # 旧连接已死但还在字典中，清理它
                            self.logger.ws.info(f"[{connection_id}] 清理已断开的旧连接")
                            del self.active_connections[connection_id]

                    return await self._handle_client_connection(ws, path, connection_id, config)

            # 启动WebSocket服务器，移除大小和队列限制
            try:
                async with websockets.serve(
                    connection_handler,
                    host,
                    port,
                    max_size=None,  # 移除消息大小限制
                    max_queue=None,  # 移除队列大小限制
                    ping_interval=300,  # 心跳间隔
                    ping_timeout=60,   # 心跳超时
                    close_timeout=None,   # 关闭超时
                    compression='deflate'  # 启用压缩
                ):
                    self.logger.ws.info(f"连接代理 {connection_id} 已启动在 {client_endpoint}")

                    # 保持运行
                    while self.running:
                        await asyncio.sleep(1)
            except OSError as e:
                if "Address already in use" in str(e) or e.errno == 98 or e.errno == 10048:
                    self.logger.ws.warning(f"连接代理 {connection_id} 端口 {port} 已被占用，跳过启动")
                else:
                    raise

        except Exception as e:
            self.logger.ws.error(f"启动连接代理失败 {connection_id}: {e}")
    
    async def _handle_client_connection(self, client_ws, path, connection_id: str, config: Dict[str, Any]):
        """处理客户端连接"""
        client_ip = client_ws.remote_address
        # self.logger.ws.info(f"[{connection_id}] 新的客户端连接: {client_ip}")
        
        try:
            # 创建代理连接对象
            proxy_connection = ProxyConnection(
                connection_id=connection_id,
                config=config,
                client_ws=client_ws,
                config_manager=self.config_manager,
                database_manager=self.database_manager,
                logger=self.logger,
                backup_manager=self.backup_manager
            )
            
            # 保存活动连接
            self.active_connections[connection_id] = proxy_connection
            
            # 启动代理
            await proxy_connection.start_proxy()
            
        except Exception as e:
            self.logger.ws.error(f"[{connection_id}] 处理客户端连接失败: {e}")
        finally:
            # 清理连接
            if connection_id in self.active_connections:
                del self.active_connections[connection_id]
            self.logger.ws.info(f"[{connection_id}] 客户端连接已关闭: {client_ip}")
    
    
    async def stop(self):
        """停止代理服务器"""
        self.running = False
        self.logger.ws.info("正在停止WebSocket代理服务器...")

        # 关闭所有活动连接
        stop_tasks = []
        for connection in list(self.active_connections.values()):
            try:
                if getattr(connection, "running", True):
                    stop_tasks.append(asyncio.create_task(connection.stop()))
            except Exception as e:
                self.logger.ws.error(f"关闭连接时出错: {e}")

        if stop_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*stop_tasks, return_exceptions=True),
                    timeout=5.0  # 5秒超时
                )
            except asyncio.TimeoutError:
                self.logger.ws.warning("部分连接关闭超时")
            except Exception as e:
                self.logger.ws.error(f"关闭连接任务时出错: {e}")

        self.active_connections.clear()
        self.logger.ws.info("WebSocket代理服务器已停止")


class ProxyConnection:
    """单个代理连接"""

    def __init__(self, connection_id, config, client_ws, config_manager, database_manager, logger, backup_manager=None):
        self.connection_id = connection_id
        self.config = config
        self.client_ws = client_ws
        self.config_manager = config_manager
        self.database_manager = database_manager
        self.logger = logger
        self.backup_manager = backup_manager

        self.target_connections = []
        self.target_sakoya_flags = []  # 标记每个目标是否使用早柚协议
        self.echo_cache = {}
        self.running = False
        self.client_headers = None
        self.first_message = None
        self.self_id: int | None = None
        self.reloading = False  # 标记是否正在重新加载目标端点

        # 解析目标端点配置，标记哪些使用早柚协议，哪些被禁用
        for endpoint in self.config.get("target_endpoints", []):
            is_sakoya = isinstance(endpoint, dict) and endpoint.get("sakoya_protocol", False)
            self.target_sakoya_flags.append(is_sakoya)

        self.reconnect_locks = []  # 每个 target_index 一个 Lock
        for endpoint in self.config.get("target_endpoints", []):
            # 检查端点是否被禁用
            is_disabled = isinstance(endpoint, dict) and endpoint.get("disabled", False)
            if not is_disabled:
                self.reconnect_locks.append(asyncio.Lock())
            else:
                self.reconnect_locks.append(None)  # 禁用的端点不需要锁

        # 初始化消息处理器
        self.message_processor = MessageProcessor(config_manager, database_manager, logger)

        # 自身指令处理
        self.command_handler = CommandHandler(config_manager, database_manager, logger, backup_manager)
        # 初始化鉴权管理器（加载持久化数据）
        asyncio.create_task(self.command_handler.auth_manager.initialize())

    async def start_proxy(self):
        """启动代理"""
        self.running = True
        
        try:
            # 等待客户端第一个消息以获取请求头
            self.first_message = await self.client_ws.recv()

            try:
                # 尝试不同的方式获取请求头
                if hasattr(self.client_ws, 'request_headers'):
                    self.client_headers = self.client_ws.request_headers
                elif hasattr(self.client_ws, 'headers'):
                    self.client_headers = self.client_ws.headers
                elif hasattr(self.client_ws, 'request') and hasattr(self.client_ws.request, 'headers'):
                    self.client_headers = self.client_ws.request.headers
                else:
                    self.client_headers = {}
                    self.logger.ws.warning(f"[{self.connection_id}] 无法获取客户端请求头")
            except Exception as e:
                self.logger.ws.warning(f"[{self.connection_id}] 获取客户端请求头失败: {e}")
                self.client_headers = {}
            
            
            # 连接到目标端点
            await self._connect_to_targets()
            
            # 处理第一个消息，其中yunzai需要这个lifecycle消息来注册
            await self._process_client_message(self.first_message)
            
            await self.send_reboot_message()

            # 启动消息转发任务
            tasks = []

            # 客户端到目标的转发任务
            client_to_targets_task = asyncio.create_task(self._forward_client_to_targets())
            tasks.append(client_to_targets_task)

            # 目标到客户端的转发任务
            target_tasks = []
            for idx, target_ws in enumerate(self.target_connections):
                if target_ws:
                    task = asyncio.create_task(
                        self._forward_target_to_client(target_ws, self.list_index2target_index(idx))
                    )
                    target_tasks.append(task)
                    tasks.append(task)

            if tasks:
                # 只等待客户端转发任务，让它作为主要任务
                # 目标端点的转发任务会独立运行，互不干扰
                await client_to_targets_task

                # 客户端断开后，取消所有目标转发任务
                for task in target_tasks:
                    if not task.done():
                        task.cancel()

                # 等待所有目标任务结束
                if target_tasks:
                    await asyncio.gather(*target_tasks, return_exceptions=True)
            else:
                self.logger.ws.warning(f"[{self.connection_id}] 没有转发任务，连接将关闭")
            
        except Exception as e:
            self.logger.ws.error(f"[{self.connection_id}] 代理运行错误: {e}")
        finally:
            await self.stop()


    async def _connect_to_target(self, endpoint, target_index: int):
        """
        连接到目标端点

        Args:
            endpoint: 目标端点配置，可以是字符串或字典
            target_index: 目标索引
        """
        try:

            if target_index > len(self.target_connections) + 1 or target_index == 0:
                raise Exception(f"[{self.connection_id}] 目标ID {target_index} 超出范围!")

            # 兼容字符串和对象格式
            if isinstance(endpoint, str):
                endpoint_url = endpoint
                custom_headers = {}
            else:
                endpoint_url = endpoint.get("url", "")
                custom_headers = endpoint.get("headers", {})

            # 使用客户端请求头连接目标
            extra_headers = {}
            if self.client_headers:
                # 更新相关请求头，其中Nonebot必须x-self-id
                for header_name in ["authorization", "x-self-id", "x-client-role", "user-agent"]:
                    if header_name in self.client_headers:
                        extra_headers[header_name] = self.client_headers[header_name]

            # 添加自定义请求头（优先级更高）
            extra_headers.update(custom_headers)

            # 尝试使用不同的参数名连接，同时配置连接参数
            target_ws = None
            connection_params = {
                'max_size': None,  # 移除消息大小限制
                'max_queue': None,  # 移除队列大小限制
                'ping_interval': 300,  # 心跳间隔
                'ping_timeout': 60,   # 心跳超时
                'close_timeout': None,   # 关闭超时
                'compression': 'deflate'
            }

            connection_attempts = [
                # 尝试 extra_headers 参数
                lambda: websockets.connect(endpoint_url, extra_headers=extra_headers, **connection_params),
                # 尝试 additional_headers 参数
                lambda: websockets.connect(endpoint_url, additional_headers=extra_headers, **connection_params),
                # 不使用额外头部，无法连接Nonebot2
                lambda: websockets.connect(endpoint_url, **connection_params)
            ]

            for attempt in connection_attempts:
                try:
                    target_ws = await attempt()
                    break
                except TypeError:
                    # 参数不支持，尝试下一种方式
                    continue
                except Exception as e:
                    # 其他错误，直接抛出
                    raise e

            if target_index == len(self.target_connections) + 1: # next one to append
                self.target_connections.append(target_ws) # 保证index正确，即使是None也添加
            else:
                self.target_connections[self.target_index2list_index(target_index)] = target_ws
            if target_ws is None:
                raise Exception(f"[{self.connection_id}] 所有连接方式都失败")

            self.logger.ws.info(f"[{self.connection_id}] 已连接到目标: {endpoint_url}")
            return target_ws

        except Exception as e:
            if target_index == len(self.target_connections) + 1:
                self.target_connections.append(None)
            else:
                self.target_connections[self.target_index2list_index(target_index)] = None
            self.logger.ws.error(f"[{self.connection_id}] 连接目标失败 {endpoint_url if isinstance(endpoint, str) else endpoint.get('url', endpoint)}: {e}")
            return None


    async def _connect_to_targets(self):
        """连接到目标端点"""
        target_endpoints = self.config.get("target_endpoints", [])

        # self.logger.ws.info(f"[{self.connection_id}] 开始连接到 {len(target_endpoints)} 个目标端点")

        # 先标记哪些目标需要启动重连（避免在循环中启动任务）
        failed_targets = []
        for idx, endpoint in enumerate(target_endpoints):
            # 检查端点是否被禁用
            is_disabled = isinstance(endpoint, dict) and endpoint.get("disabled", False)
            if is_disabled:
                self.logger.ws.info(f"[{self.connection_id}] 目标端点 {self.list_index2target_index(idx)} 已被禁用，跳过连接")
                # 在 target_connections 中添加占位符，保持索引一致
                if idx >= len(self.target_connections):
                    self.target_connections.append(None)
                else:
                    self.target_connections[idx] = None
                continue

            # 兼容字符串格式和对象格式
            endpoint_url = endpoint if isinstance(endpoint, str) else endpoint.get("url", "")
            is_sakoya = isinstance(endpoint, dict) and endpoint.get("sakoya_protocol", False)

            target_ws = await self._connect_to_target(endpoint, self.list_index2target_index(idx))

            # 如果连接成功，检查是否需要应用早柚协议适配器
            if target_ws:
                if is_sakoya:
                    from .sakoya_adapter import GscoreWebSocketAdapter, extract_bot_id_from_path
                    # 从目标 URL 中提取 bot_id
                    bot_id = "Bot"  # 默认 bot_id
                    try:
                        from urllib.parse import urlparse
                        parsed = urlparse(endpoint_url)
                        bot_id = extract_bot_id_from_path(parsed.path)
                        if not bot_id:
                            self.logger.ws.warning(f"[{self.connection_id}] 无法从路径 {parsed.path} 提取 bot_id，使用默认值 'Bot'")
                            bot_id = "Bot"
                    except Exception as e:
                        self.logger.ws.error(f"[{self.connection_id}] 提取 bot_id 失败: {e}，使用默认值 'Bot'")
                        bot_id = "Bot"

                    target_ws = GscoreWebSocketAdapter(target_ws, bot_id, self.logger)
                    # 更新连接列表中的引用为包装后的适配器
                    self.target_connections[idx] = target_ws
                else:
                    # self.logger.ws.info(f"[{self.connection_id}] 目标 {self.list_index2target_index(idx)} 使用标准 OneBot v11 协议")
                    pass

            # 如果连接失败（返回 None），记录下来稍后启动重连
            if target_ws is None:
                failed_targets.append(self.list_index2target_index(idx))

        # 在所有连接尝试完成后，启动失败目标的重连任务
        for target_index in failed_targets:
            self.logger.ws.warning(f"[{self.connection_id}] 目标 {target_index} 初始连接失败，启动后台重连")
            asyncio.create_task(self._start_reconnect_with_delay(target_index))
    
    async def _forward_client_to_targets(self):
        """转发客户端消息到目标"""
        try:
            async for message in self.client_ws:
                try:
                    await self._process_client_message(message)
                except websockets.exceptions.ConnectionClosed:
                    continue
        except Exception as e:
            self.logger.ws.error(f"[{self.connection_id}] 客户端消息转发错误: {e}")
    
    async def _forward_target_to_client(self, target_ws, target_index):
        """转发目标消息到客户端"""
        try:
            self.logger.ws.debug(f"[{self.connection_id}] 目标 {target_index} 开始接收消息循环...")
            async for message in target_ws:
                self.logger.ws.debug(f"[{self.connection_id}] 目标 {target_index} 收到消息")
                await self._process_target_message(message, target_index)
        except websockets.exceptions.ConnectionClosed as e:
            # 如果正在重新加载，不要尝试重连
            if self.reloading:
                self.logger.ws.info(f"[{self.connection_id}] 目标 {target_index} 连接已关闭（正在重新加载），不进行重连")
                return
            self.logger.ws.warning(f"[{self.connection_id}] 目标 {target_index} 连接被服务器关闭: {e}")
            await self._reconnect_target(target_index)
        except TypeError as e:
            # 如果正在重新加载，不要尝试重连
            if self.reloading:
                self.logger.ws.info(f"[{self.connection_id}] 目标 {target_index} 连接已关闭（正在重新加载），不进行重连")
                return
            self.logger.ws.error(f"[{self.connection_id}] 目标 {target_index} TypeError (ws is None): {e}")
            await self._reconnect_target(target_index) # 如果是None，也挂一个后台重连
        except Exception as e:
            self.logger.ws.error(f"[{self.connection_id}] 目标消息转发错误 {target_index}: {e}")
            import traceback
            self.logger.ws.error(f"[{self.connection_id}] 错误堆栈: {traceback.format_exc()}")

    async def _start_reconnect_with_delay(self, target_index: int):
        """延迟启动重连任务，等待客户端完全初始化"""
        await asyncio.sleep(5)
        await self._reconnect_target(target_index)

    async def _reconnect_target(self, target_index: int):
        self.logger.ws.info(f"[{self.connection_id}] 目标连接 {target_index} 已关闭。将在120秒内持续尝试重新连接。")

        # 立即重连循环（40次，每3秒一次）
        for attempt in range(40):
            # 每次重连前都检查端点是否被禁用
            target_endpoints = self.config.get("target_endpoints", [])
            list_index = self.target_index2list_index(target_index)
            if list_index >= len(target_endpoints):
                self.logger.ws.warning(f"[{self.connection_id}] 目标 {target_index} 的索引超出范围，停止重连")
                return

            endpoint = target_endpoints[list_index]
            is_disabled = isinstance(endpoint, dict) and endpoint.get("disabled", False)
            if is_disabled:
                self.logger.ws.info(f"[{self.connection_id}] 目标端点 {target_index} 已被禁用，停止重连")
                return

            # 检查重连锁（如果锁是None说明端点被禁用了）
            lock = self.reconnect_locks[list_index] if list_index < len(self.reconnect_locks) else None
            if lock is None:
                self.logger.ws.info(f"[{self.connection_id}] 目标端点 {target_index} 的重连锁为空（可能被禁用），停止重连")
                return

            # 获取端点配置
            endpoint = target_endpoints[list_index]
            if not endpoint:
                break

            is_sakoya = endpoint and isinstance(endpoint, dict) and endpoint.get("sakoya_protocol", False)
            endpoint_url = endpoint if isinstance(endpoint, str) else endpoint.get("url", "") if endpoint else ""

            # 检查客户端连接是否还活着
            client_state = getattr(self.client_ws, 'state', None)
            if not self.running or client_state != 1:  # 1 = OPEN 状态
                self.logger.ws.info(f"[{self.connection_id}] 客户端已断开 (running={self.running}, state={client_state})，停止重连目标 {target_index}")
                return

            await asyncio.sleep(3)
            try:
                self.logger.ws.debug(f"[{self.connection_id}] 尝试重连目标 {target_index} (第 {attempt + 1}/40 次)...")
                target_ws = await self._connect_to_target(endpoint, target_index)
                if target_ws is None:
                    self.logger.ws.debug(f"[{self.connection_id}] 重连目标 {target_index} 失败，3秒后重试")
                    continue

                # 检查是否需要应用早柚协议适配器
                if is_sakoya:
                    from .sakoya_adapter import GscoreWebSocketAdapter, extract_bot_id_from_path
                    bot_id = "Bot"  # 默认 bot_id
                    try:
                        from urllib.parse import urlparse
                        parsed = urlparse(endpoint_url)
                        bot_id = extract_bot_id_from_path(parsed.path)
                        if not bot_id:
                            bot_id = "Bot"
                    except Exception:
                        bot_id = "Bot"
                    target_ws = GscoreWebSocketAdapter(target_ws, bot_id, self.logger)

                # 更新目标连接列表
                self.target_connections[list_index] = target_ws

                await self._process_client_message(self.first_message) # 比如yunzai需要使用first Message重新注册

                # 早柚协议连接立即开始转发，不等待5秒
                if is_sakoya:
                    self.logger.ws.info(f"[{self.connection_id}] 目标连接 {target_index} 恢复成功，立即开始转发。")
                    await self._forward_target_to_client(target_ws, target_index)
                else:
                    self.logger.ws.info(f"[{self.connection_id}] 目标连接 {target_index} 恢复成功，5秒后重新开始转发。")
                    await asyncio.sleep(5)
                    await self._forward_target_to_client(target_ws, target_index)
                return  # 重连成功，退出重连函数
            except Exception as e:
                self.logger.ws.warning(f"[{self.connection_id}] 尝试重连目标 {target_index} 失败: {e}")

        # 长期重连循环
        while self.running:
            # 每次重连前都检查端点是否被禁用
            target_endpoints = self.config.get("target_endpoints", [])
            list_index = self.target_index2list_index(target_index)
            if list_index < len(target_endpoints):
                endpoint = target_endpoints[list_index]
                is_disabled = isinstance(endpoint, dict) and endpoint.get("disabled", False)
                if is_disabled:
                    self.logger.ws.info(f"[{self.connection_id}] 目标端点 {target_index} 已被禁用，停止长期重连")
                    break

            # 检查客户端状态
            client_state = getattr(self.client_ws, 'state', None)
            if client_state != 1:  # 1 = OPEN 状态
                break

            # 获取端点配置
            endpoint = target_endpoints[list_index] if list_index < len(target_endpoints) else None
            if not endpoint:
                break

            is_sakoya = endpoint and isinstance(endpoint, dict) and endpoint.get("sakoya_protocol", False)
            endpoint_url = endpoint if isinstance(endpoint, str) else endpoint.get("url", "") if endpoint else ""

            await asyncio.sleep(600) # 10分钟后再试
            try:
                target_ws = await self._connect_to_target(endpoint, target_index)
                if target_ws:
                    # 检查是否需要应用早柚协议适配器
                    if is_sakoya:
                        from .sakoya_adapter import GscoreWebSocketAdapter, extract_bot_id_from_path
                        bot_id = "Bot"  # 默认 bot_id
                        try:
                            from urllib.parse import urlparse
                            parsed = urlparse(endpoint_url)
                            bot_id = extract_bot_id_from_path(parsed.path)
                            if not bot_id:
                                bot_id = "Bot"
                        except Exception:
                            bot_id = "Bot"
                        target_ws = GscoreWebSocketAdapter(target_ws, bot_id, self.logger)

                    # 更新目标连接列表
                    self.target_connections[list_index] = target_ws

                    await self._process_client_message(self.first_message)

                    # 早柚协议连接立即开始转发，不等待5秒
                    if is_sakoya:
                        self.logger.ws.info(f"[{self.connection_id}] 目标连接 {target_index} 恢复成功，立即开始转发。")
                        await self._forward_target_to_client(target_ws, target_index)
                    else:
                        self.logger.ws.info(f"[{self.connection_id}] 目标连接 {target_index} 恢复成功，5秒后重新开始转发。")
                        await asyncio.sleep(5)
                        await self._forward_target_to_client(target_ws, target_index)
                    return  # 重连成功，退出重连函数
            except Exception as e:
                pass

        self.logger.ws.info(f"[{self.connection_id}] 客户端已断开，终止目标 {target_index} 的重连循环")
                        
    async def _process_client_message(self, message: str):
        """处理客户端消息"""
        try:
            # 解析JSON消息
            message_data = json.loads(message)
            if message_data.get("self_id"): # 每次更新，客户端可能会换账号
                if self.self_id and self.self_id != message_data["self_id"]:
                    # 但是，不论是通过头注册还是yunzai的方式都不能支持账号的热切换
                    self.logger.ws.warning("[{}] 客户端账号已切换到 {}，请重启该连接！".format(self.connection_id, message_data['self_id']))
                self.self_id = message_data["self_id"]

            # 消息预处理
            message_data = await self.command_handler.preprocesser(message_data)
            processed_message, parsed_event = await self._preprocess_message(message_data)

            if processed_message:
                if self._check_api_call_succ(parsed_event):
                    # 如果是发送成功
                    data_in_api = parsed_event.data
                    if isinstance(data_in_api, dict): # get list api 不可能是发送
                        message_id = message_data.get("data", {}).get("message_id")
                        await self.database_manager.save_message(
                            await self._construct_msg_from_echo(message_data["echo"], message_id=message_id), "SEND", self.connection_id
                        )
                else:
                    # 记录消息到数据库，注意记录的是处理后的消息，所以统计功能是无视别名的，只需要按key搜索即可
                    # 收到裸消息不会是api response
                    self._log_api_call_fail(parsed_event)
                    await self.database_manager.save_message(
                        processed_message, "RECV", self.connection_id
                    )

                # 本体指令集
                resp_api = await self.command_handler.handle_message(parsed_event)
                if resp_api:
                    processed_message = None # 自身返回时，阻止事件传递给框架 Preprocesser不受影响
                    await self._process_target_message(resp_api, 0) # 自身的index为0，其实并不是连接

                # 转发到所有目标
                processed_json = json.dumps(processed_message, ensure_ascii=False)

                if message_data.get("echo"):
                    # api请求内容，尽可能保证各框架的发送api都使用了echo。
                    # 尝试从各个 target_index 的缓存中查找
                    echo_val = str(message_data["echo"])
                    matched_target_index = None
                    for idx in range(1, len(self.target_connections) + 1):
                        echo_key = f"{idx}_{echo_val}"
                        if echo_key in self.echo_cache:
                            matched_target_index = self.echo_cache.pop(echo_key, {}).get("target_index")
                            break

                    if matched_target_index is not None and matched_target_index > 0 and self.target_connections[self.target_index2list_index(matched_target_index)]:
                        self.logger.ws.debug(f"[{self.connection_id}] 发送API请求到目标 {matched_target_index}: {processed_json[:1000]}")
                        try:
                            await self.target_connections[self.target_index2list_index(matched_target_index)].send(processed_json)
                        except websockets.exceptions.ConnectionClosed:
                            return
                        except Exception as e:
                            self.logger.ws.error(f"[{self.connection_id}] 发送到目标失败: {e}")
                            raise
                else:
                    # 检查是否是需要跳过早柚协议端点的消息类型
                    action = message_data.get("action", "")
                    post_type = message_data.get("post_type", "")

                    # gscore 后端只需要 API 调用和消息事件
                    # 跳过：元事件（heartbeat、lifecycle等）
                    skip_sakoya = (
                        action in ['lifecycle', '_connect', 'get_login_info', 'get_status', 'get_version_info'] or
                        post_type == 'meta_event'  # 只跳过元事件，保留消息事件
                    )

                    if skip_sakoya:
                        self.logger.ws.debug(f"[{self.connection_id}] 消息类型 post_type={post_type}, action={action} 将跳过 gscore 端点")

                    for list_index, target_ws in enumerate(self.target_connections):
                        if target_ws:
                            # 检查端点是否被禁用
                            target_index = self.list_index2target_index(list_index)
                            target_endpoints = self.config.get("target_endpoints", [])
                            if target_index - 1 < len(target_endpoints):
                                endpoint = target_endpoints[target_index - 1]
                                is_disabled = isinstance(endpoint, dict) and endpoint.get("disabled", False)
                                if is_disabled:
                                    continue

                            # 检查是否是早柚协议端点，如果是且消息类型需要跳过
                            is_sakoya = list_index < len(self.target_sakoya_flags) and self.target_sakoya_flags[list_index]

                            if is_sakoya and skip_sakoya:
                                self.logger.ws.debug(
                                    f"[{self.connection_id}] 跳过向早柚协议端点 {self.list_index2target_index(list_index)} "
                                    f"发送消息 (post_type={post_type}, action={action})"
                                )
                                continue

                            try:
                                await target_ws.send(processed_json)
                            except websockets.exceptions.ConnectionClosed:
                                continue # 发送时不再捕捉
                            except Exception as e:
                                self.logger.ws.error(f"[{self.connection_id}] 发送到目标失败: {e}")
                                raise

        except json.JSONDecodeError:
            self.logger.ws.warning(f"[{self.connection_id}] 收到非JSON消息: {message[:1000]}")
        except websockets.exceptions.ConnectionClosed:
            raise
        except Exception as e:
            self.logger.ws.error(f"[{self.connection_id}] 处理客户端消息失败: {e}")
    
    async def _process_target_message(self, message: str | dict, target_index: int):
        """处理目标消息"""
        try:
            # 解析JSON消息
            if isinstance(message, str):
                message_data = json.loads(message)
            else:
                message_data = message

            self.logger.ws.debug(f"[{self.connection_id}] 来自连接 {target_index} 的API响应: {str(message_data)[:1000]}")

            if not self._construct_echo_info(message_data, target_index):
                # 兼容不使用echo回报的框架，不清楚有没有
                message_data_as_recv = await self._construct_data_as_msg(message_data)
                await self.database_manager.save_message(
                    message_data_as_recv, "SEND", self.connection_id
                )

            # 消息后处理
            self.logger.ws.debug(f"[{self.connection_id}] 开始消息后处理，目标 {target_index}")
            processed_message = await self._postprocess_message(message_data, str(self.self_id))
            self.logger.ws.debug(f"[{self.connection_id}] 消息后处理完成，目标 {target_index}，结果: {processed_message is not None}")

            if processed_message:
                # 发送到客户端
                processed_json = json.dumps(processed_message, ensure_ascii=False)
                self.logger.ws.debug(f"[{self.connection_id}] 准备发送到客户端，消息长度: {len(processed_json)}")
                try:
                    await self.client_ws.send(processed_json)
                    self.logger.ws.debug(f"[{self.connection_id}] 成功发送到客户端")
                except websockets.exceptions.ConnectionClosed as e:
                    # 客户端连接已关闭，记录日志并重新抛出以终止连接
                    self.logger.ws.warning(f"[{self.connection_id}] 发送到客户端失败，客户端连接已关闭: code={e.code}, reason={e.reason}")
                    raise
                except Exception as e:
                    # 其他发送错误，记录日志但不中断连接
                    self.logger.ws.error(f"[{self.connection_id}] 发送到客户端失败: {e}")
                    import traceback
                    self.logger.ws.error(f"[{self.connection_id}] 错误堆栈: {traceback.format_exc()}")
                    # 不抛出异常，继续处理其他消息
            else:
                self.logger.ws.debug(f"[{self.connection_id}] 消息后处理返回 None，不发送到客户端")

        except json.JSONDecodeError as e:
            self.logger.ws.warning(f"[{self.connection_id}] 目标 {target_index} 发送非JSON消息: {message[:1000]}, 错误: {e}")
        except websockets.exceptions.ConnectionClosed:
            raise
        except Exception as e:
            self.logger.ws.error(f"[{self.connection_id}] 处理目标消息失败: {e}")
            import traceback
            self.logger.ws.error(f"[{self.connection_id}] 错误堆栈: {traceback.format_exc()}")
    
    async def _preprocess_message(self, message_data: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Event]]:
        """消息预处理"""
        return await self.message_processor.preprocess_client_message(message_data)

    async def _postprocess_message(self, message_data: Dict[str, Any], self_id: str) -> Optional[Dict[str, Any]]:
        """消息后处理"""
        return await self.message_processor.postprocess_target_message(message_data, self_id)
    
    async def send_reboot_message(self):
        api_resp = await construct_reboot_message(str(self.self_id))
        if api_resp:
            await self._process_target_message(api_resp, 0)
    
    def _construct_echo_info(self, message_data, target_index) -> str | None:
        echo = str(message_data.get("echo"))
        if not echo:
            return None

        # 将 target_index 加入键值，防止不同连接的 echo 混淆
        echo_key = f"{target_index}_{echo}"

        echo_info = {
            "data": message_data,
            "create_timestamp": int(datetime.now().timestamp()),
            "target_index": target_index,
            "original_echo": echo
        }

        if echo_key in self.echo_cache:
            self.logger.ws.warning(f"[{self.connection_id}] echo {echo_key} 已存在，将被覆盖")
        self.echo_cache[echo_key] = echo_info
        self.logger.ws.debug(f"[{self.connection_id}] 收到echo {echo_key}，缓存大小 {len(self.echo_cache)}")

        # 当缓存首次达到100个的时候。该函数阻塞。如网络正常不应该有这么多cache。
        if len(self.echo_cache) % 100 == 0:
            self.logger.ws.warning(f"[{self.connection_id}] echo 缓存达到 {len(self.echo_cache)} 个，强制清理过期的echo!")
            now_ts = int(datetime.now().timestamp())
            old_keys = [k for k, v in self.echo_cache.items() if now_ts - v.get("create_timestamp", 0) > 120]
            for k in old_keys:
                del self.echo_cache[k]

        return echo
    
    @staticmethod
    def _check_api_call_succ(event: Event):
        if isinstance(event, ApiResponse):
            return event.status == "ok" and event.retcode == 0
        return False
    
    def _log_api_call_fail(self, event: Event):
        if isinstance(event, ApiResponse):
            if event.status != "ok" or event.retcode != 0:
                # echo 现在包含 target_index 前缀，需要查找匹配的键
                echo_val = str(event.echo)
                echo_info = None
                for idx in range(1, len(self.target_connections) + 1):
                    echo_key = f"{idx}_{echo_val}"
                    if echo_key in self.echo_cache:
                        echo_info = self.echo_cache.get(echo_key, None)
                        break

                if echo_info:
                    # 截断过长的数据（如base64）避免日志爆炸
                    data_str = str(echo_info['data'])
                    if len(data_str) > 200:
                        data_str = data_str[:200] + f"...[total length: {len(data_str)}]"
                    self.logger.ws.warning("[{}] API调用失败: {} -> {}".format(self.connection_id, data_str, event))
                    
    async def _construct_msg_from_echo(self, echo, **kwargs):
        """从api结果中构造模拟收到消息"""
        # echo 现在包含 target_index 前缀，需要查找匹配的键
        echo_val = str(echo)
        echo_info = None
        for idx in range(1, len(self.target_connections) + 1):
            echo_key = f"{idx}_{echo_val}"
            if echo_key in self.echo_cache:
                echo_info = self.echo_cache.get(echo_key, None)
                break

        if echo_info:
            return await self._construct_data_as_msg(echo_info["data"], **kwargs)
        return {}
    
    async def _construct_data_as_msg(self, message_data, **kwargs):
        """将发送api请求转换为消息事件"""
        
        if 'send' not in message_data.get('action'):
            return {}
        params = message_data.get("params", {})
        params.update({"self_id": self.self_id})
        if "sender" not in params:
            params.update({"sender": {"user_id": self.self_id, "nickname": "BS Bot Send"}})
        # 这个 message_sent 不是 napcat 那种修改的 Onebot，而是本框架数据库中的标识
        params.update({"post_type": "message_sent"})
        raw_message = MessageSegmentParser.message2raw_message(params.get("message", []))
        params.update({"raw_message": raw_message})
        
        params.update(kwargs)
        return params
    
    @staticmethod
    def target_index2list_index(target_index):
        return target_index - 1
    
    @staticmethod
    def list_index2target_index(list_index):
        return list_index + 1
    
    async def stop(self):
        """停止代理连接"""
        self.running = False

        # 关闭目标连接
        close_tasks = []
        for target_ws in self.target_connections:
            close_tasks.append(asyncio.create_task(self._close_websocket(target_ws)))

        # 关闭客户端连接
        if self.client_ws:
            close_tasks.append(asyncio.create_task(self._close_websocket(self.client_ws)))

        if close_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*close_tasks, return_exceptions=True),
                    timeout=3.0  # 3秒超时
                )
            except asyncio.TimeoutError:
                self.logger.ws.warning(f"[{self.connection_id}] 关闭连接超时")
            except Exception as e:
                self.logger.ws.error(f"[{self.connection_id}] 关闭连接时出错: {e}")

        self.target_connections.clear()

    async def _close_websocket(self, ws):
        """安全关闭WebSocket连接"""
        try:
            if ws:
                await ws.close()
        except Exception as e:
            self.logger.ws.error(f"[{self.connection_id}] 关闭WebSocket连接时出错: {e}")

    async def reload_targets(self, new_config: dict):
        """
        重新加载目标端点配置

        当配置更新时调用此方法，用于更新目标端点连接

        Args:
            new_config: 新的连接配置
        """
        try:
            self.logger.ws.info(f"[{self.connection_id}] 正在重新加载目标端点配置...")

            # 设置重载标志，防止旧的转发任务尝试重连
            self.reloading = True

            # 更新配置
            self.config = new_config

            # 关闭所有旧的目标连接
            for target_ws in self.target_connections:
                if target_ws:
                    try:
                        await target_ws.close()
                    except Exception as e:
                        self.logger.ws.debug(f"[{self.connection_id}] 关闭旧目标连接时出错: {e}")

            # 清空目标连接列表
            self.target_connections.clear()
            self.target_sakoya_flags.clear()

            # 重建重连锁
            self.reconnect_locks = []
            for endpoint in self.config.get("target_endpoints", []):
                is_disabled = isinstance(endpoint, dict) and endpoint.get("disabled", False)
                if not is_disabled:
                    self.reconnect_locks.append(asyncio.Lock())
                else:
                    self.reconnect_locks.append(None)

            # 重新连接到目标端点
            await self._connect_to_targets()

            # 启动新的转发任务来接收目标消息
            for idx, target_ws in enumerate(self.target_connections):
                if target_ws:
                    target_index = self.list_index2target_index(idx)
                    self.logger.ws.info(f"[{self.connection_id}] 启动目标 {target_index} 的转发任务")
                    asyncio.create_task(self._forward_target_to_client(target_ws, target_index))

            # 清除重载标志
            self.reloading = False

            self.logger.ws.info(f"[{self.connection_id}] 目标端点配置已重新加载")

        except Exception as e:
            self.reloading = False
            self.logger.ws.error(f"[{self.connection_id}] 重新加载目标端点失败: {e}")
            import traceback
            self.logger.ws.error(f"[{self.connection_id}] 错误堆栈: {traceback.format_exc()}")
            