"""
鉴权管理器
处理密钥鉴权、Bot封禁等功能（支持数据库持久化）
"""

import time
import hashlib
import secrets
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone


class AuthManager:
    """鉴权管理器 - 处理临时密钥生成和Bot验证"""

    def __init__(self, config_manager, logger, database_manager=None):
        """
        初始化鉴权管理器

        Args:
            config_manager: 配置管理器
            logger: 日志记录器
            database_manager: 数据库管理器（用于持久化）
        """
        self.config_manager = config_manager
        self.logger = logger
        self.database_manager = database_manager

        # 当前有效的临时密钥: {key: {'bot_id': xxx, 'expires_at': timestamp}}
        self._valid_keys: Dict[str, dict] = {}

        # 内存模式下的失败尝试记录（无数据库时使用）
        self._failed_attempts: Dict[str, dict] = {}

    async def initialize(self):
        """初始化时从数据库加载鉴权状态"""
        if not self.database_manager:
            self.logger.command.warning("数据库管理器未初始化，鉴权状态将不会持久化")
            return

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import text

                # 清理过期的封禁
                await session.execute(
                    text("UPDATE auth_status SET is_banned = 0, banned_until = NULL WHERE banned_until < :now"),
                    {"now": datetime.now(timezone.utc)}
                )
                await session.commit()

                self.logger.command.info("鉴权管理器已初始化，从数据库加载状态")
        except Exception as e:
            self.logger.command.error(f"初始化鉴权管理器失败: {e}")

    def is_auth_enabled(self) -> bool:
        """检查是否启用了密钥鉴权"""
        global_config = self.config_manager.get_global_config()
        security = global_config.get("security", {})
        return security.get("auth_enabled", False)

    def get_max_attempts(self) -> int:
        """获取最大尝试次数"""
        global_config = self.config_manager.get_global_config()
        security = global_config.get("security", {})
        return security.get("max_attempts", 3)

    def get_ban_duration(self) -> int:
        """获取封禁时长（分钟）"""
        global_config = self.config_manager.get_global_config()
        security = global_config.get("security", {})
        return security.get("ban_duration", 30)

    def generate_temp_key(self, bot_id: str) -> Tuple[str, int]:
        """
        生成临时鉴权密钥

        Args:
            bot_id: Bot账号ID

        Returns:
            (密钥, 过期时间戳)
        """
        # 使用时间戳、随机数和BotID生成密钥
        timestamp = int(time.time())
        random_str = secrets.token_hex(16)
        data = f"{bot_id}:{timestamp}:{random_str}"

        # 使用 SHA256 哈希生成密钥
        key_hash = hashlib.sha256(data.encode()).hexdigest()

        # 取前20位作为最终密钥
        temp_key = key_hash[:20].upper()

        # 设置过期时间（3分钟后）
        expires_at = timestamp + 180

        # 保存密钥
        self._valid_keys[temp_key] = {
            'bot_id': bot_id,
            'expires_at': expires_at
        }

        # 清理过期的密钥
        self._cleanup_expired_keys()

        self.logger.command.info(f"[鉴权] 为Bot {bot_id} 生成临时密钥，有效期至 {datetime.fromtimestamp(expires_at).strftime('%H:%M:%S')}")

        return temp_key, expires_at

    async def verify_key(self, bot_id: str, key: str) -> Tuple[bool, str]:
        """
        验证临时密钥

        Args:
            bot_id: Bot账号ID
            key: 用户提供的密钥

        Returns:
            (是否成功, 消息)
        """
        # 清理过期的密钥和会话
        self._cleanup_expired_keys()
        await self._cleanup_expired_bans()

        # 检查Bot是否被封禁
        if await self._is_bot_banned(bot_id):
            remaining_minutes = await self._get_ban_remaining_minutes(bot_id)
            return False, f"验证失败次数过多，已被封禁 {remaining_minutes} 分钟"

        # 查找密钥
        key_data = self._valid_keys.get(key.upper())

        if not key_data:
            return await self._record_failed_attempt(bot_id), "密钥无效或已过期"

        # 检查密钥是否属于该Bot
        if key_data['bot_id'] != bot_id:
            return await self._record_failed_attempt(bot_id), "密钥不属于当前Bot"

        # 检查是否过期
        if time.time() > key_data['expires_at']:
            del self._valid_keys[key.upper()]
            return await self._record_failed_attempt(bot_id), "密钥已过期"

        # 验证成功！
        await self._set_bot_authenticated(bot_id)

        # 删除已使用的密钥
        del self._valid_keys[key.upper()]

        self.logger.command.info(f"[鉴权] Bot {bot_id} 验证成功")
        return True, "验证成功！该Bot已获得访问权限"

    async def is_bot_authenticated(self, bot_id: str) -> bool:
        """
        检查Bot是否已通过鉴权

        Args:
            bot_id: Bot账号ID

        Returns:
            是否已鉴权
        """
        # 如果未启用鉴权，直接返回 True
        if not self.is_auth_enabled():
            return True

        # 从数据库检查
        if not self.database_manager:
            return False

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import select

                result = await session.execute(
                    select(AuthStatus).where(AuthStatus.bot_id == bot_id)
                )
                auth_status = result.scalar_one_or_none()

                if auth_status and auth_status.is_authenticated:
                    return True
                return False
        except Exception as e:
            self.logger.command.error(f"检查Bot鉴权状态失败: {e}")
            return False

    async def _set_bot_authenticated(self, bot_id: str):
        """设置Bot为已鉴权状态"""
        if not self.database_manager:
            return

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import select

                result = await session.execute(
                    select(AuthStatus).where(AuthStatus.bot_id == bot_id)
                )
                auth_status = result.scalar_one_or_none()

                if auth_status:
                    auth_status.is_authenticated = True
                    auth_status.authenticated_at = datetime.now(timezone.utc)
                    auth_status.failed_attempts = 0  # 重置失败次数
                    auth_status.is_banned = False  # 解除封禁
                    auth_status.banned_until = None
                else:
                    auth_status = AuthStatus(
                        bot_id=bot_id,
                        is_authenticated=True,
                        authenticated_at=datetime.now(timezone.utc)
                    )
                    session.add(auth_status)

                await session.commit()
        except Exception as e:
            self.logger.command.error(f"保存Bot鉴权状态失败: {e}")

    async def _record_failed_attempt(self, bot_id: str) -> Tuple[bool, str]:
        """
        记录失败尝试并检查是否需要封禁

        Args:
            bot_id: Bot账号ID

        Returns:
            (是否成功, 消息)
        """
        if not self.database_manager:
            # 如果没有数据库，使用内存模式（重启后丢失）
            if bot_id not in self._failed_attempts:
                self._failed_attempts[bot_id] = {'attempts': 0}

            self._failed_attempts[bot_id]['attempts'] += 1
            max_attempts = self.get_max_attempts()

            if self._failed_attempts[bot_id]['attempts'] >= max_attempts:
                return False, f"验证失败次数过多，已被封禁 {self.get_ban_duration()} 分钟"

            remaining_attempts = max_attempts - self._failed_attempts[bot_id]['attempts']
            return False, f"密钥无效，还剩 {remaining_attempts} 次尝试机会"

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import select

                result = await session.execute(
                    select(AuthStatus).where(AuthStatus.bot_id == bot_id)
                )
                auth_status = result.scalar_one_or_none()

                current_time = datetime.now(timezone.utc)

                if auth_status:
                    auth_status.failed_attempts += 1
                    auth_status.last_attempt_at = current_time
                else:
                    auth_status = AuthStatus(
                        bot_id=bot_id,
                        failed_attempts=1,
                        last_attempt_at=current_time
                    )
                    session.add(auth_status)

                # 检查是否需要封禁
                max_attempts = self.get_max_attempts()
                if auth_status.failed_attempts >= max_attempts:
                    # 封禁Bot
                    ban_duration_minutes = self.get_ban_duration()
                    from datetime import timedelta
                    auth_status.is_banned = True
                    auth_status.banned_until = current_time + timedelta(minutes=ban_duration_minutes)

                    await session.commit()
                    self.logger.command.warning(f"[鉴权] Bot {bot_id} 验证失败次数过多，已被封禁 {ban_duration_minutes} 分钟")
                    return False, f"验证失败次数过多，已被封禁 {ban_duration_minutes} 分钟"

                await session.commit()

                remaining_attempts = max_attempts - auth_status.failed_attempts
                return False, f"密钥无效，还剩 {remaining_attempts} 次尝试机会"
        except Exception as e:
            self.logger.command.error(f"记录失败尝试时出错: {e}")
            return False, "验证失败，请稍后重试"

    async def _is_bot_banned(self, bot_id: str) -> bool:
        """检查Bot是否被封禁"""
        if not self.database_manager:
            # 内存模式
            if bot_id not in self._failed_attempts:
                return False
            return 'ban_until' in self._failed_attempts[bot_id]

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import select

                result = await session.execute(
                    select(AuthStatus).where(AuthStatus.bot_id == bot_id)
                )
                auth_status = result.scalar_one_or_none()

                if not auth_status or not auth_status.is_banned:
                    return False

                # 检查封禁是否已过期
                if auth_status.banned_until and auth_status.banned_until < datetime.now(timezone.utc):
                    # 封禁已过期，自动解除
                    auth_status.is_banned = False
                    auth_status.banned_until = None
                    auth_status.failed_attempts = 0
                    await session.commit()
                    return False

                return True
        except Exception as e:
            self.logger.command.error(f"检查Bot封禁状态失败: {e}")
            return False

    async def _get_ban_remaining_minutes(self, bot_id: str) -> int:
        """获取封禁剩余分钟数"""
        if not self.database_manager:
            return 0

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import select

                result = await session.execute(
                    select(AuthStatus).where(AuthStatus.bot_id == bot_id)
                )
                auth_status = result.scalar_one_or_none()

                if auth_status and auth_status.banned_until:
                    remaining = auth_status.banned_until - datetime.now(timezone.utc)
                    return max(0, int(remaining.total_seconds() / 60))
                return 0
        except Exception:
            return 0

    async def _cleanup_expired_bans(self):
        """清理过期的封禁"""
        if not self.database_manager:
            return

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import update, text

                # 使用原生SQL更新，更高效
                await session.execute(
                    text("UPDATE auth_status SET is_banned = 0, banned_until = NULL, failed_attempts = 0 WHERE banned_until < :now"),
                    {"now": datetime.now(timezone.utc)}
                )
                await session.commit()
        except Exception as e:
            self.logger.command.error(f"清理过期封禁失败: {e}")

    def _cleanup_expired_keys(self):
        """清理过期的密钥"""
        current_time = time.time()
        expired_keys = [
            key for key, data in self._valid_keys.items()
            if current_time > data['expires_at']
        ]

        for key in expired_keys:
            del self._valid_keys[key]

    async def get_auth_status(self, bot_id: str) -> Optional[dict]:
        """
        获取Bot的鉴权状态（用于账户列表显示）

        Returns:
            {
                'is_authenticated': bool,
                'authenticated_at': str (ISO格式),
                'failed_attempts': int,
                'is_banned': bool,
                'banned_until': str (ISO格式)
            }
        """
        if not self.database_manager:
            return None

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import select

                result = await session.execute(
                    select(AuthStatus).where(AuthStatus.bot_id == bot_id)
                )
                auth_status = result.scalar_one_or_none()

                if not auth_status:
                    return None

                # 转换datetime为ISO格式字符串以便JSON序列化
                authenticated_at = auth_status.authenticated_at.isoformat() if auth_status.authenticated_at else None
                banned_until = auth_status.banned_until.isoformat() if auth_status.banned_until else None

                return {
                    'is_authenticated': auth_status.is_authenticated,
                    'authenticated_at': authenticated_at,
                    'failed_attempts': auth_status.failed_attempts,
                    'is_banned': auth_status.is_banned,
                    'banned_until': banned_until
                }
        except Exception as e:
            self.logger.command.error(f"获取Bot鉴权状态失败: {e}")
            return None

    async def clear_bot_session(self, bot_id: str):
        """
        清除Bot会话（登出）

        Args:
            bot_id: Bot账号ID
        """
        if not self.database_manager:
            return

        try:
            async with self.database_manager.session_factory() as session:
                from app.database.models import AuthStatus
                from sqlalchemy import delete

                await session.execute(
                    delete(AuthStatus).where(AuthStatus.bot_id == bot_id)
                )
                await session.commit()

            self.logger.command.info(f"[鉴权] Bot {bot_id} 已登出")
        except Exception as e:
            self.logger.command.error(f"清除Bot会话失败: {e}")

    def get_all_valid_keys(self) -> list:
        """
        获取所有有效密钥（用于WebUI显示）

        Returns:
            有效密钥列表
        """
        self._cleanup_expired_keys()

        keys = []
        for key, data in self._valid_keys.items():
            keys.append({
                'key': key,
                'bot_id': data['bot_id'],
                'expires_at': data['expires_at']
            })

        return keys
