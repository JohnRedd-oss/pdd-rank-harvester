"""gouvip-picker：基于「狗宝助手 / gouvip 网页版」行业榜单的选品工具。"""
from .api import ApiError, GouVip, sign  # noqa: F401
from .store import Store  # noqa: F401
from .scoring import DEFAULT_CONFIG, merge_config, rank  # noqa: F401

__all__ = ["ApiError", "GouVip", "Store", "sign", "DEFAULT_CONFIG", "merge_config", "rank"]
__version__ = "0.1.0"
