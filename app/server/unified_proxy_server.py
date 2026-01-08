"""
统一WebSocket代理服务器
实现基于路径的单端口多连接路由功能
支持 OneBot v11 和早柚协议
"""

import asyncio
import websockets
from typing import Dict, Any, Optional
from urllib.parse import urlparse

from .proxy_server import ProxyConnection


class UnifiedProxyServer:
    """统一WebSocket代理服务器 - 单端口多路径路由"""
    
    def __init__(self, config_manager, database_manager, logger):
        self.config_manager = config_manager
        self.database_manager = database_manager
        self.logger = logger
        
        # 路由映射: {port: {path: connection_id}}
        self.route_map = {}
        
        # 连接配置映射: {connection_id: config}
        self.connection_configs = {}
        
        # 活动连接: {connection_id: ProxyConnection}
        self.active_connections = {}
        
        # WebSocket服务器实例: {port: server}
        self.servers = {}
        
        self.running = False
    
    def _parse_endpoint(self, endpoint: str) -> tuple[str, int, str]:
        """
        解析WebSocket端点
        返回: (host, port, path)
        例如: ws://0.0.0.0:5111/bs/yunzai -> ('0.0.0.0', 5111, '/bs/yunzai')
        """
        if not endpoint.startswith("ws://"):
            raise ValueError(f"不支持的端点格式: {endpoint}")
        
        # 移除 ws:// 前缀
        url_part = endpoint[5:]
        
        # 分离 host:port 和 path
        if "/" in url_part:
            host_port, path = url_part.split("/", 1)
            path = "/" + path  # 恢复开头的 /
        else:
            host_port = url_part
            path = "/"
        
        # 分离 host 和 port
        if ":" in host_port:
            host, port_str = host_port.split(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 80
        
        return host, port, path
    
    def _build_route_map(self, connections_config: Dict[str, Dict[str, Any]]):
        """构建路由映射表"""
        self.route_map.clear()
        self.connection_configs.clear()
        
        for connection_id, config in connections_config.items():
            if not config.get("enabled", False):
                continue
            
            client_endpoint = config.get("client_endpoint", "")
            try:
                host, port, path = self._parse_endpoint(client_endpoint)
                
                # 初始化端口映射
                if port not in self.route_map:
                    self.route_map[port] = {
                        'host': host,
                        'paths': {}
                    }
                
                # 检查路径冲突
                if path in self.route_map[port]['paths']:
                    existing_conn_id = self.route_map[port]['paths'][path]
                    self.logger.ws.warning(
                        f"路径冲突: {path} 已被连接 {existing_conn_id} 使用，"
                        f"连接 {connection_id} 将被忽略"
                    )
                    continue
                
                # 添加路由
                self.route_map[port]['paths'][path] = connection_id
                self.connection_configs[connection_id] = config
                
                self.logger.ws.debug(
                    f"添加路由: {host}:{port}{path} -> {connection_id}"
                )
                
            except Exception as e:
                self.logger.ws.error(
                    f"解析连接端点失败 {connection_id}: {client_endpoint}, 错误: {e}"
                )
    
    async def start(self):
        """启动统一代理服务器"""
        self.running = True
        self.logger.ws.info("启动统一WebSocket代理服务器...")
        
        # 获取连接配置
        connections_config = self.config_manager.get_connections_config()
        
        # 构建路由映射
        self._build_route_map(connections_config)
        
        if not self.route_map:
            self.logger.ws.warning("没有启用的连接配置")
            while self.running:
                await asyncio.sleep(1)
            return
        
        # 为每个端口启动WebSocket服务器
        tasks = []
        for port, route_info in self.route_map.items():
            task = asyncio.create_task(
                self._start_port_server(port, route_info)
            )
            tasks.append(task)
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _start_port_server(self, port: int, route_info: Dict[str, Any]):
        """为指定端口启动WebSocket服务器"""
        host = route_info['host']
        paths = route_info['paths']

        try:
            self.logger.ws.info(
                f"在 {host}:{port} 启动WebSocket服务器，"
                f"路由: {list(paths.keys())}"
            )

            # 创建连接处理器（使用闭包捕获 route_map 的引用）
            async def connection_handler(ws):
                """处理WebSocket连接，根据路径路由到对应的连接"""
                # 动态从 route_map 获取路由配置（支持热更新）
                current_route = self.route_map.get(port, {})
                current_paths = current_route.get('paths', {})

                # 尝试多种方式获取路径
                path = "/"
                if hasattr(ws, 'path'):
                    path = ws.path
                elif hasattr(ws, 'request'):
                    # 从 request 对象获取路径
                    req = ws.request
                    if hasattr(req, 'path'):
                        path = req.path
                    elif hasattr(req, 'scope'):
                        # ASGI scope
                        scope = req.scope
                        path = scope.get('path', '/')

                # 确保路径以 / 开头
                if not path.startswith('/'):
                    path = '/' + path

                self.logger.ws.debug(f"收到连接请求，路径: {path}, 可用路由: {list(current_paths.keys())}")

                # 查找对应的连接ID
                connection_id = current_paths.get(path)

                if not connection_id:
                    self.logger.ws.warning(
                        f"未找到路径 {path} 的路由配置，可用路径: {list(current_paths.keys())}"
                    )
                    await ws.close(1008, f"未找到路径 {path} 的路由配置")
                    return

                # 获取连接配置
                config = self.connection_configs.get(connection_id)
                if not config:
                    self.logger.ws.error(f"连接配置不存在: {connection_id}")
                    await ws.close(1011, "连接配置不存在")
                    return

                # 处理连接
                await self._handle_client_connection(ws, path, connection_id, config)

            # 启动WebSocket服务器
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
            ) as server:
                self.servers[port] = server
                self.logger.ws.info(f"WebSocket服务器已在 {host}:{port} 启动")

                # 保持运行，直到端口不再需要路由
                while self.running:
                    await asyncio.sleep(1)
                    # 检查端口是否仍在路由映射中
                    if port not in self.route_map:
                        self.logger.ws.info(f"端口 {port} 已从路由中移除，服务器将停止")
                        break

        except Exception as e:
            self.logger.ws.error(f"启动端口 {port} 的WebSocket服务器失败: {e}")

    async def _handle_client_connection(
        self,
        client_ws,
        path: str,
        connection_id: str,
        config: Dict[str, Any]
    ):
        """处理客户端连接"""
        client_ip = client_ws.remote_address
        # self.logger.ws.info(
        #     f"[{connection_id}] 新的客户端连接: {client_ip}, 路径: {path}"
        # )

        try:
            # 如果已存在连接，先关闭旧连接
            if connection_id in self.active_connections:
                self.logger.ws.warning(
                    f"[{connection_id}] 已存在连接，正在关闭旧连接以替换为新连接"
                )
                try:
                    await self.active_connections[connection_id].stop()
                    await asyncio.sleep(1)  # 防止频繁重启
                except Exception as e:
                    self.logger.ws.error(f"[{connection_id}] 关闭旧连接失败: {e}")

            # 创建代理连接对象
            proxy_connection = ProxyConnection(
                connection_id=connection_id,
                config=config,
                client_ws=client_ws,
                config_manager=self.config_manager,
                database_manager=self.database_manager,
                logger=self.logger
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
            self.logger.ws.info(
                f"[{connection_id}] 客户端连接已关闭: {client_ip}, 路径: {path}"
            )

    async def stop(self):
        """停止统一代理服务器"""
        self.running = False
        self.logger.ws.info("正在停止统一WebSocket代理服务器...")

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
        self.servers.clear()
        self.logger.ws.info("统一WebSocket代理服务器已停止")

    async def reload_routes(self):
        """热重载路由配置（无需重启服务器）"""
        self.logger.ws.info("开始热重载路由配置...")

        # 获取最新的连接配置
        connections_config = self.config_manager.get_connections_config()

        # 记录旧的端口集合
        old_ports = set(self.route_map.keys())

        # 重建路由映射
        self._build_route_map(connections_config)

        # 新的端口集合
        new_ports = set(self.route_map.keys())

        # 找出需要启动的新端口
        ports_to_start = new_ports - old_ports
        # 找出需要停止的旧端口
        ports_to_stop = old_ports - new_ports

        # 启动新端口的服务器
        for port in ports_to_start:
            route_info = self.route_map[port]
            self.logger.ws.info(f"检测到新端口 {port}，正在启动...")
            asyncio.create_task(self._start_port_server(port, route_info))

        # 停止旧端口的服务器
        for port in ports_to_stop:
            self.logger.ws.info(f"端口 {port} 已无路由，服务器将自动停止")
            # 服务器会在下次循环检查时自动退出

        # 更新现有端口的路由信息
        for port in new_ports & old_ports:
            new_paths = set(self.route_map[port]['paths'].keys())
            self.logger.ws.info(
                f"端口 {port} 路由已更新，当前路由: {new_paths}"
            )

        self.logger.ws.info(
            f"路由重载完成: 端口 {ports_to_start} 新增, "
            f"端口 {ports_to_stop} 移除, 共 {len(self.route_map)} 个端口"
        )

