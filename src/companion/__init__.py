"""长期记忆 AI 伙伴：本地记忆核心 + 可替换的模型与身体。"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("companion")
except PackageNotFoundError:
    __version__ = "0.0.0"
