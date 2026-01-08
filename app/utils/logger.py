import logging
import logging.handlers
from pathlib import Path
from datetime import datetime
import sys
import re

# ANSI 颜色代码
class ANSIColors:
    """ANSI 颜色代码"""
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    ITALIC = '\033[3m'
    UNDERLINE = '\033[4m'

    # 前景色
    BLACK = '\033[30m'
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    BLUE = '\033[34m'
    MAGENTA = '\033[35m'
    CYAN = '\033[36m'
    WHITE = '\033[37m'

    # 明亮的前景色
    BRIGHT_BLACK = '\033[90m'
    BRIGHT_RED = '\033[91m'
    BRIGHT_GREEN = '\033[92m'
    BRIGHT_YELLOW = '\033[93m'
    BRIGHT_BLUE = '\033[94m'
    BRIGHT_MAGENTA = '\033[95m'
    BRIGHT_CYAN = '\033[96m'
    BRIGHT_WHITE = '\033[97m'

    # 背景色
    BG_BLACK = '\033[40m'
    BG_RED = '\033[41m'
    BG_GREEN = '\033[42m'
    BG_YELLOW = '\033[43m'
    BG_BLUE = '\033[44m'
    BG_MAGENTA = '\033[45m'
    BG_CYAN = '\033[46m'
    BG_WHITE = '\033[47m'


class ColoredFormatter(logging.Formatter):
    """带颜色的日志格式化器"""

    # 日志级别对应的颜色
    LOG_COLORS = {
        logging.DEBUG: ANSIColors.DIM + ANSIColors.CYAN,
        logging.INFO: ANSIColors.GREEN,
        logging.WARNING: ANSIColors.YELLOW + ANSIColors.BOLD,
        logging.ERROR: ANSIColors.RED + ANSIColors.BOLD,
        logging.CRITICAL: ANSIColors.BRIGHT_RED + ANSIColors.BOLD + ANSIColors.UNDERLINE,
    }

    # 组件名称的颜色
    COMPONENT_COLORS = {
        'BotShepherd': ANSIColors.BOLD + ANSIColors.BRIGHT_BLUE,
        'WebSocket': ANSIColors.CYAN,
        'Message': ANSIColors.MAGENTA,
        'Web': ANSIColors.BLUE,
        'Command': ANSIColors.BRIGHT_YELLOW,
        'Operation': ANSIColors.BRIGHT_MAGENTA,
    }

    # 高亮关键词
    HIGHLIGHT_PATTERNS = {
        '成功': ANSIColors.GREEN + ANSIColors.BOLD,
        'SUCCESS': ANSIColors.GREEN + ANSIColors.BOLD,
        '✅': ANSIColors.GREEN + ANSIColors.BOLD,
        '完成': ANSIColors.GREEN + ANSIColors.BOLD,
        '启动': ANSIColors.BRIGHT_GREEN,
        '已连接': ANSIColors.GREEN,

        '错误': ANSIColors.RED + ANSIColors.BOLD,
        'ERROR': ANSIColors.RED + ANSIColors.BOLD,
        '失败': ANSIColors.RED + ANSIColors.BOLD,
        '❌': ANSIColors.RED + ANSIColors.BOLD,
        '异常': ANSIColors.RED,
        'Exception': ANSIColors.RED,

        '警告': ANSIColors.YELLOW + ANSIColors.BOLD,
        'WARNING': ANSIColors.YELLOW + ANSIColors.BOLD,
        '⚠': ANSIColors.YELLOW + ANSIColors.BOLD,
        '注意': ANSIColors.YELLOW,

        '信息': ANSIColors.CYAN,
        'INFO': ANSIColors.CYAN,
        'ℹ': ANSIColors.CYAN,

        '发送': ANSIColors.BLUE,
        'SENT': ANSIColors.BLUE,
        '➡': ANSIColors.BLUE,

        '接收': ANSIColors.MAGENTA,
        'RECV': ANSIColors.MAGENTA,
        '⬅': ANSIColors.MAGENTA,
    }

    def __init__(self, fmt=None, datefmt=None, style='%'):
        super().__init__(fmt, datefmt, style)
        self.use_colors = self._supports_color()
        # URL 匹配正则表达式（匹配 http、https、ws、wss 协议）
        self.url_pattern = re.compile(
            r'(\b(?:https?|wss?)://[-\w.]+(?:[:/][-\w./?%&=+#]*)?)',
            re.IGNORECASE
        )

    def _highlight_urls(self, text):
        """高亮文本中的 URL 链接"""
        if not self.use_colors:
            return text

        def replace_url(match):
            url = match.group(1)
            # 使用蓝色和下划线高亮 URL
            return ANSIColors.BLUE + ANSIColors.UNDERLINE + url + ANSIColors.RESET

        return self.url_pattern.sub(replace_url, text)

    def _supports_color(self):
        """检测终端是否支持颜色"""
        # Windows 检查
        if sys.platform == 'win32':
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
                return True
            except:
                # 检查是否在支持 ANSI 的终端中运行
                return ('TERM' in os.environ or
                        'COLORTERM' in os.environ or
                        'ANSICON' in os.environ)

        # Unix/Linux/Mac 检查
        if hasattr(sys.stdout, 'isatty') and sys.stdout.isatty():
            return True

        # 检查环境变量
        if os.environ.get('TERM', '') != '':
            return True

        return False

    def format(self, record):
        # 如果不支持颜色，使用原始格式化
        if not self.use_colors:
            return super().format(record)

        # 获取原始格式化结果
        formatted = super().format(record)

        # 添加级别颜色
        levelname = record.levelname
        level_color = self.LOG_COLORS.get(record.levelno, '')
        if level_color:
            formatted = formatted.replace(levelname, level_color + levelname + ANSIColors.RESET)

        # 添加组件名称颜色
        if hasattr(record, 'name'):
            for comp, color in self.COMPONENT_COLORS.items():
                if comp in record.name:
                    formatted = formatted.replace(comp, color + comp + ANSIColors.RESET)
                    break

        # 高亮关键词
        for pattern, color in self.HIGHLIGHT_PATTERNS.items():
            if pattern in formatted:
                # 只高亮第一个匹配，避免重复替换
                parts = formatted.split(pattern)
                if len(parts) > 1:
                    result = []
                    result.append(parts[0])
                    for i, part in enumerate(parts[1:], 1):
                        # 确保不在已着色的文本中添加颜色
                        if not result[-1].endswith(ANSIColors.RESET):
                            result.append(color + pattern + ANSIColors.RESET)
                        else:
                            result.append(pattern + ANSIColors.RESET)
                        result.append(part)
                    formatted = ''.join(result)

        # 高亮 URL 链接
        formatted = self._highlight_urls(formatted)

        return formatted


# 导入 os 用于检测颜色支持
import os

class BSLogger:

    def __init__(self, global_config=None):
        # 1. 设置和解析配置
        self._setup_config(global_config)

        # 2. 创建根日志目录
        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)

        # 3. 配置主日志记录器 ("BotShepherd")
        self._setup_main_logger()

        # 4. 配置专用的子日志记录器
        self.ws = self._setup_special_logger("WebSocket", "websocket", use_timed_rotation=True)
        self.message = self._setup_special_logger("Message", "message", use_timed_rotation=True, formatter=logging.Formatter('%(message)s'))
        self.web = self._setup_special_logger("Web", "web", use_timed_rotation=True)
        self.command = self._setup_special_logger("Command", "command", use_timed_rotation=True)
        self.op = self._setup_special_logger("Operation", "operation", rotate=False) # 操作日志不轮转

    def __getattr__(self, name):
        if hasattr(self.main_logger, name):
            return getattr(self.main_logger, name)
        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    def _setup_config(self, global_config):
        self.config = {
            "level": "INFO",
            "file_rotation": True,
            "keep_days": 7,
            "max_file_size": "10MB"
        }
        if global_config and "logging" in global_config:
            self.config.update(global_config["logging"])
        
        self.log_level = getattr(logging, self.config["level"].upper())
        self.keep_days = int(self.config["keep_days"])

    def _setup_main_logger(self):
        self.main_logger = logging.getLogger("BotShepherd")
        self.main_logger.setLevel(self.log_level)
        self.main_logger.handlers.clear()
        self.main_logger.propagate = False

        # 控制台处理器 - 使用带颜色的格式化器
        self.console_handler = logging.StreamHandler()
        console_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        self.console_handler.setFormatter(ColoredFormatter(console_format))
        self.main_logger.addHandler(self.console_handler)

        # 主日志文件处理器（带轮转）
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s')

        if self.config["file_rotation"]:
            # 按时间轮转
            handler = logging.handlers.TimedRotatingFileHandler(
                filename=self.log_dir / "botshepherd.log",
                when="midnight",
                interval=1,
                backupCount=self.keep_days,
                encoding="utf-8"
            )
        else:
            # 按大小轮转
            max_bytes = self._parse_size(self.config["max_file_size"])
            handler = logging.handlers.RotatingFileHandler(
                filename=self.log_dir / "botshepherd.log",
                maxBytes=max_bytes,
                backupCount=self.keep_days, # 使用 keep_days 作为备份数量
                encoding="utf-8"
            )

        handler.setFormatter(file_formatter)
        self.main_logger.addHandler(handler)

    def _setup_special_logger(self, name, sub_dir, rotate=True, use_timed_rotation=True, formatter=None, console_formatter=None, use_colors=True):
        """
        创建一个专用的子日志记录器。

        :param name: 日志记录器名称 (e.g., "WebSocket")
        :param sub_dir: 日志存放的子目录 (e.g., "websocket")
        :param rotate: 是否需要轮转。如果为 False，则使用 FileHandler。
        :param use_timed_rotation: 如果 rotate=True，此参数决定是按时间还是大小轮转。
        :param formatter: 自定义日志格式。如果为 None，使用默认格式。
        :param console_formatter: 自定义控制台格式。如果为 None，使用带颜色的格式。
        :param use_colors: 是否在控制台使用颜色。默认为 True。
        :return: 配置好的 logging.Logger 实例。
        """
        logger_name = f"BotShepherd.{name}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(self.log_level)
        logger.handlers.clear()
        logger.propagate = False  # 防止日志向上传播到主日志记录器

        # 控制台处理器
        if console_formatter:
            # 复制一份，避免修改共享的 handler
            console_handler_copy = logging.StreamHandler()
            console_handler_copy.setFormatter(console_formatter)
            logger.addHandler(console_handler_copy)
        else:
            # 为专用日志创建带颜色的控制台格式化器
            if use_colors:
                # 创建更简洁的控制台格式
                console_format = '%(asctime)s - %(levelname)s - %(message)s'
                colored_formatter = ColoredFormatter(console_format)
                console_handler_copy = logging.StreamHandler()
                console_handler_copy.setFormatter(colored_formatter)
                logger.addHandler(console_handler_copy)
            else:
                logger.addHandler(self.console_handler)

        # 创建专用日志目录
        special_log_dir = self.log_dir / sub_dir
        special_log_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = special_log_dir / f"{sub_dir}.log"

        # 设置处理器
        if not rotate:
            # 不轮转的日志
            handler = logging.FileHandler(log_file_path, encoding="utf-8")
        elif use_timed_rotation:
            # 按时间轮转 (每日)
            handler = logging.handlers.TimedRotatingFileHandler(
                filename=log_file_path,
                when="midnight",
                interval=1,
                backupCount=self.keep_days,
                encoding="utf-8"
            )
        else:
            # 按大小轮转
            max_bytes = self._parse_size(self.config["max_file_size"])
            handler = logging.handlers.RotatingFileHandler(
                filename=log_file_path,
                maxBytes=max_bytes,
                backupCount=self.keep_days,
                encoding="utf-8"
            )

        # 设置格式化器
        if formatter:
            handler.setFormatter(formatter)
        else:
            default_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            handler.setFormatter(default_formatter)

        logger.addHandler(handler)
        return logger

    def log_message(self, direction, message_type, content_summary, extra_info=None, level="info"):
        """
        记录一条格式化的、扁平的消息日志。
        这是一个便捷方法，底层调用 self.message.info()。
        
        :param direction: 消息方向 (e.g., "SENT", "RECV")
        :param message_type: 消息类型 (e.g., "TEXT", "IMAGE")
        :param content_summary: 内容摘要
        :param extra_info: 额外信息 (e.g., user_id, chat_id)
        """
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        log_line = f"{timestamp} {direction} {message_type} {content_summary}"
        if extra_info:
            log_line += f" | {extra_info}"
        
        # 使用配置好的 message 日志记录器
        if level == "info":
            self.message.info(log_line)
        elif level == "debug":
            self.message.debug(log_line)
        elif level == "warning":
            self.message.warning(log_line)
        elif level == "error":
            self.message.error(log_line)
        else:
            raise ValueError(f"Invalid log level: {level}")

    @staticmethod
    def _parse_size(size_str):
        """解析大小字符串，如 '10MB' -> 10485760"""
        size_str = str(size_str).upper()
        if size_str.endswith('KB'):
            return int(size_str[:-2]) * 1024
        elif size_str.endswith('MB'):
            return int(size_str[:-2]) * 1024 * 1024
        elif size_str.endswith('GB'):
            return int(size_str[:-2]) * 1024 * 1024 * 1024
        else:
            return int(size_str)
