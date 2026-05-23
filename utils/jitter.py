"""
抖动工具模块
用于防风控的随机间隔抖动
"""
import random
import config


def apply_jitter(base_interval: int) -> int:
    """
    对基础间隔应用随机抖动

    Args:
        base_interval: 基础间隔时间（秒）

    Returns:
        应用抖动后的间隔时间（秒），确保最小为1秒

    示例:
        如果基础间隔为60秒，抖动范围为5秒
        则返回值在55-65秒之间随机
    """
    if not config.ANTI_DETECTION_JITTER_ENABLED or not config.ANTI_DETECTION_ENABLED:
        return base_interval

    jitter = random.randint(-config.ANTI_DETECTION_JITTER_RANGE, config.ANTI_DETECTION_JITTER_RANGE)
    return max(1, base_interval + jitter)
