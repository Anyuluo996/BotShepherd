"""
统一 HTTP 和 WebSocket 服务器
使用 aiohttp 同时处理 HTTP（Flask 应用）和 WebSocket 连接
"""

import asyncio
import aiohttp
from aiohttp import web
from aiohttp_wsgi import WSGIHandler
from typing import Dict, Any
import re
from urllib.parse import urlparse


class AiohttpWebSocketAdapter:
    """将 aiohttp WebSocket 适配为 websockets 库的接口"""

    def __init__(self, aiohttp_ws, request):
        self._ws = aiohttp_ws
        self._request = request
        self.remote_address = (request.remote, 0)

    async def recv(self):
        """接收消息"""
        msg = await self._ws.receive()
        if msg.type == aiohttp.WSMsgType.TEXT:
            return msg.data
        elif msg.type == aiohttp.WSMsgType.BINARY:
            return msg.data
        elif msg.type == aiohttp.WSMsgType.CLOSE:
            raise ConnectionClosed(1000, "Connection closed")
        elif msg.type == aiohttp.WSMsgType.ERROR:
            raise ConnectionClosed(1006, "Connection error")
        else:
            raise ConnectionClosed(1002, f"Unexpected message type: {msg.type}")

    async def send(self, message):
        """发送消息"""
        if isinstance(message, str):
            await self._ws.send_str(message)
        elif isinstance(message, bytes):
            await self._ws.send_bytes(message)
        else:
            await self._ws.send_str(str(message))

    async def close(self, code=1000, reason=''):
        """关闭连接"""
        await self._ws.close(code=code, message=reason.encode() if reason else b'')

    @property
    def closed(self):
        """检查连接是否已关闭"""
        return self._ws.closed

    @property
    def request_headers(self):
        """获取请求头"""
        return self._request.headers

    @property
    def headers(self):
        """获取请求头（别名）"""
        return self._request.headers


class ConnectionClosed(Exception):
    """连接关闭异常"""
    def __init__(self, code, reason):
        self.code = code
        self.reason = reason
        super().__init__(f"Connection closed: {code} {reason}")


class UnifiedHttpWsServer:
    """统一的 HTTP 和 WebSocket 服务器"""
    
    def __init__(self, flask_app, config_manager, database_manager, logger, port=5111):
        """
        初始化统一服务器
        
        Args:
            flask_app: Flask 应用实例
            config_manager: 配置管理器
            database_manager: 数据库管理器
            logger: 日志记录器
            port: 监听端口
        """
        self.flask_app = flask_app
        self.config_manager = config_manager
        self.database_manager = database_manager
        self.logger = logger
        self.port = port
        self.running = False
        
        # WebSocket 路由映射 {port: {path: connection_id}}
        self.route_map = {}
        
        # 活跃的 WebSocket 连接 {connection_id: ProxyConnection}
        self.active_connections = {}
        
        # aiohttp 应用
        self.app = None
        self.runner = None
        self.site = None
        
    def _parse_endpoint(self, endpoint: str) -> tuple:
        """解析 WebSocket 端点"""
        try:
            parsed = urlparse(endpoint)
            host = parsed.hostname or '0.0.0.0'
            port = parsed.port or 5111
            path = parsed.path or '/'
            return host, port, path
        except Exception as e:
            self.logger.ws.error(f"解析端点失败: {endpoint}, 错误: {e}")
            return None, None, None
    
    def _build_route_map(self, connections_config: Dict[str, Any]):
        """构建路由映射表"""
        self.route_map = {}
        
        for connection_id, config in connections_config.items():
            if not config.get("enabled", False):
                continue
                
            client_endpoint = config.get("client_endpoint", "")
            if not client_endpoint:
                continue
                
            try:
                host, port, path = self._parse_endpoint(client_endpoint)
                
                if port is None:
                    continue
                
                # 只处理配置的端口（通常是 ws_port）
                ws_port = self.config_manager.get_global_config().get("ws_port", 5111)
                if port != ws_port:
                    continue
                
                # 添加到路由映射
                if port not in self.route_map:
                    self.route_map[port] = {
                        'host': host,
                        'paths': {}
                    }
                
                # 检查路径冲突
                if path in self.route_map[port]['paths']:
                    self.logger.ws.warning(
                        f"路径冲突: {path} 已被 {self.route_map[port]['paths'][path]} 使用，"
                        f"忽略连接 {connection_id}"
                    )
                    continue
                
                self.route_map[port]['paths'][path] = connection_id
                self.logger.ws.info(f"注册 WebSocket 路由: {path} -> {connection_id}")
                
            except Exception as e:
                self.logger.ws.error(
                    f"解析连接端点失败 {connection_id}: {client_endpoint}, 错误: {e}"
                )
    
    async def websocket_handler(self, request):
        """处理 WebSocket 连接"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        path = request.path

        # 查找对应的连接配置
        connection_id = None
        for port, route_info in self.route_map.items():
            if path in route_info['paths']:
                connection_id = route_info['paths'][path]
                break

        if not connection_id:
            self.logger.ws.warning(f"未找到路径 {path} 的路由配置")
            await ws.close()
            return ws

        # 获取连接配置
        connections_config = self.config_manager.get_connections_config()
        config = connections_config.get(connection_id)

        if not config:
            self.logger.ws.error(f"连接配置不存在: {connection_id}")
            await ws.close()
            return ws

        # 创建 WebSocket 适配器
        client_ws_adapter = AiohttpWebSocketAdapter(ws, request)

        # 创建代理连接
        from app.server.proxy_server import ProxyConnection

        proxy_conn = ProxyConnection(
            connection_id=connection_id,
            config=config,
            client_ws=client_ws_adapter,  # 使用适配器
            config_manager=self.config_manager,
            database_manager=self.database_manager,
            logger=self.logger
        )

        self.active_connections[connection_id] = proxy_conn

        try:
            # 启动代理（使用原有的 ProxyConnection 逻辑）
            await proxy_conn.start_proxy()
        except Exception as e:
            self.logger.ws.error(f"[{connection_id}] WebSocket 代理错误: {e}")
        finally:
            if connection_id in self.active_connections:
                del self.active_connections[connection_id]

        return ws

    async def start(self):
        """启动统一服务器"""
        self.running = True
        self.logger.info(f"启动统一 HTTP/WebSocket 服务器在端口 {self.port}...")

        # 构建 WebSocket 路由映射
        connections_config = self.config_manager.get_connections_config()
        self._build_route_map(connections_config)

        # 创建 aiohttp 应用
        self.app = web.Application()

        # 注册 WebSocket 路由
        for port, route_info in self.route_map.items():
            for path in route_info['paths'].keys():
                self.app.router.add_get(path, self.websocket_handler)
                self.logger.ws.info(f"注册 WebSocket 路由: {path}")

        # 注册 HTTP 路由（所有其他请求转发给 Flask）
        # 使用 WSGIHandler 包装 Flask 应用
        wsgi_handler = WSGIHandler(self.flask_app)

        # 添加通配符路由处理所有 HTTP 请求
        self.app.router.add_route('*', '/{path_info:.*}', wsgi_handler)

        # 启动服务器
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()

        self.logger.info(f"✅ 统一服务器已启动在 http://0.0.0.0:{self.port}")
        self.logger.info(f"   - Web 管理界面: http://0.0.0.0:{self.port}")

        if self.route_map:
            paths = []
            for port, route_info in self.route_map.items():
                paths.extend(route_info['paths'].keys())
            self.logger.info(f"   - WebSocket 路由: {', '.join(paths)}")

        # 保持运行
        while self.running:
            await asyncio.sleep(1)

    async def stop(self):
        """停止服务器"""
        self.running = False
        self.logger.info("正在停止统一服务器...")

        # 关闭所有活跃的 WebSocket 连接
        for connection_id, proxy_conn in list(self.active_connections.items()):
            try:
                await proxy_conn.stop()
            except Exception as e:
                self.logger.ws.error(f"关闭连接 {connection_id} 失败: {e}")

        # 停止 aiohttp 服务器
        if self.site:
            await self.site.stop()

        if self.runner:
            await self.runner.cleanup()

        self.logger.info("统一服务器已停止")

