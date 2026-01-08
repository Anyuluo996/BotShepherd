"""
鉴权指令
处理密钥鉴权功能
"""

from typing import Dict, Any, List
from ...onebotv11.models import Event
from ..base_command import BaseCommand, CommandResponse, CommandResult, command_registry
from ..permission_manager import PermissionLevel


class AuthCommand(BaseCommand):
    """鉴权指令 - 密钥验证"""

    def __init__(self):
        super().__init__()
        self.name = "鉴权"
        self.description = "密钥验证（启用安全鉴权后必须先执行此指令）"
        self.usage = "鉴权 [密钥]"
        self.example = """
    bs鉴权 (生成新的临时密钥)
    bs鉴权 ABC123DEF456 (验证密钥)"""
        self.aliases = ["auth", "authenticate"]
        self.required_permission = PermissionLevel.MEMBER  # 所有用户都可以使用

        # 设置为始终允许执行（在启用鉴权时这是唯一可用的指令）
        self.always_allow = True

    def _setup_parser(self):
        """设置参数解析器"""
        super()._setup_parser()
        self.parser.add_argument("key", nargs="?", help="要验证的密钥（可选）")

    async def execute(self, event: Event, args: List[str], context: Dict[str, Any]) -> CommandResponse:
        """执行鉴权指令"""
        try:
            # 获取鉴权管理器
            auth_manager = context.get("auth_manager")
            if not auth_manager:
                return self.format_error("鉴权管理器未初始化")

            # 检查是否启用了密钥鉴权
            if not auth_manager.is_auth_enabled():
                return self.format_info("未启用密钥鉴权功能，无需验证")

            bot_id = str(event.self_id)
            parsed_args = self.parse_args(args)

            if isinstance(parsed_args, str):
                # 解析失败，当作没有参数处理
                parsed_args = type('obj', (object,), {'key': None})()

            # 如果没有提供密钥，生成新的临时密钥
            if not parsed_args.key:
                temp_key, expires_at = auth_manager.generate_temp_key(bot_id)

                message = f"""已为Bot {bot_id} 生成临时验证密钥

✅ 密钥有效期3分钟
📱 请在WebUI系统设置页面查看密钥

请使用以下指令验证：
{context.get("config_manager").get_global_config().get("command_prefix", "bs")}{self.name} <密钥>"""

                return self.format_success(message, use_forward=False)

            # 验证密钥
            key = parsed_args.key.strip().upper()
            success, message = await auth_manager.verify_key(bot_id, key)

            if success:
                return self.format_success(message, use_forward=False)
            else:
                return self.format_error(message, CommandResult.PERMISSION_DENIED, use_forward=False)

        except Exception as e:
            return self.format_error(f"鉴权操作失败: {e}")


def register_auth_command():
    """注册鉴权指令"""
    command_registry.register(AuthCommand())


# 自动注册
register_auth_command()
