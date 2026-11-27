import os
import sys
import asyncio
from loguru import logger
from config.log_safety import (sanitize_record, safe_filter, safe_format,
    private_opener, configure_standard_logging)
from config.config_loader import load_config
from config.settings import check_config_file
from core.utils.cache.manager import cache_manager, CacheType

SERVER_VERSION = "0.9.7"
_logger_initialized = False


def get_module_abbreviation(module_name, module_dict):
    """获取模块名称的缩写，如果为空则返回00
    如果名称中包含下划线，则返回下划线后面的前两个字符
    """
    module_value = module_dict.get(module_name, "")
    if not module_value:
        return "00"
    if "_" in module_value:
        parts = module_value.split("_")
        return parts[-1][:2] if parts[-1] else "00"
    return module_value[:2]


def build_module_string(selected_module):
    """构建模块字符串"""
    return (
        get_module_abbreviation("VAD", selected_module)
        + get_module_abbreviation("ASR", selected_module)
        + get_module_abbreviation("LLM", selected_module)
        + get_module_abbreviation("TTS", selected_module)
        + get_module_abbreviation("Memory", selected_module)
        + get_module_abbreviation("Intent", selected_module)
        + get_module_abbreviation("VLLM", selected_module)
    )


def formatter(record):
    """Compatibility filter: no caller-provided fields or free text leave sinks."""
    return safe_filter(record)


def setup_logging(config=None):
    """Use fixed JSON vocabulary in console/file sinks; preserve config loading."""
    if config is None:
        check_config_file()
        config = cache_manager.get(CacheType.CONFIG, "main_config")
        if config is None:
            config = asyncio.run(load_config())
    log_config = config["log"]
    global _logger_initialized
    if not _logger_initialized:
        log_level = log_config.get("log_level", "INFO")
        log_dir = log_config.get("log_dir", "tmp")
        log_file = log_config.get("log_file", "server.log")
        data_dir = log_config.get("data_dir", "data")
        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)
        logger.remove()
        logger.configure(extra={}, patcher=sanitize_record)
        common = dict(format=safe_format, level=log_level, filter=formatter,
                      backtrace=False, diagnose=False, colorize=False)
        logger.add(sys.stdout, **common)
        logger.add(os.path.join(log_dir, log_file), **common,
                   rotation="10 MB", retention="30 days", compression=None,
                   encoding="utf-8", enqueue=True, opener=private_opener)
        configure_standard_logging()
        _logger_initialized = True
    return logger


def create_connection_logger(selected_module_str):
    """Keep caller API; bound module labels are stripped by the sink policy."""
    return logger.bind(selected_module=selected_module_str)
