"""
安全工具模块
提供 API 密钥生成、验证等功能
"""

import secrets
import string
from typing import List


def generate_api_key(length: int = 32) -> str:
    """
    生成安全的 API 密钥

    使用密码学安全的随机数生成器，确保密钥的安全性

    Args:
        length: 密钥长度，默认 32 个字符（最少 16 个）

    Returns:
        生成的 API 密钥字符串

    Raises:
        ValueError: 如果长度小于 16
    """
    if length < 16:
        raise ValueError("API 密钥长度不能小于 16 个字符")

    # 使用字母、数字和部分特殊字符生成密钥
    # 避免使用容易混淆的字符（如 0/O, 1/l/I）
    alphabet = string.ascii_letters + string.digits
    api_key = ''.join(secrets.choice(alphabet) for _ in range(length))

    return api_key


def generate_multiple_api_keys(count: int, length: int = 32) -> List[str]:
    """
    生成多个 API 密钥

    Args:
        count: 要生成的密钥数量
        length: 每个密钥的长度，默认 32 个字符

    Returns:
        API 密钥列表

    Raises:
        ValueError: 如果 count 小于 1 或 length 小于 16
    """
    if count < 1:
        raise ValueError("密钥数量必须大于 0")

    return [generate_api_key(length) for _ in range(count)]


def validate_api_key(api_key: str, min_length: int = 16) -> bool:
    """
    验证 API 密钥格式

    Args:
        api_key: 要验证的 API 密钥
        min_length: 最小长度要求，默认 16

    Returns:
        是否有效
    """
    if not isinstance(api_key, str):
        return False

    if len(api_key) < min_length:
        return False

    # 检查是否只包含允许的字符
    allowed_chars = set(string.ascii_letters + string.digits)
    return all(c in allowed_chars for c in api_key)


def generate_secure_token(length: int = 32) -> str:
    """
    生成安全令牌（URL 安全）

    使用 base64 编码的随机字节，适合生成 URL 安全的令牌

    Args:
        length: 令牌长度（字节），默认 32

    Returns:
        URL 安全的令牌字符串
    """
    return secrets.token_urlsafe(length)
