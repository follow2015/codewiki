"""核心服务。"""


class Base:
    """基类。"""

    def tag(self) -> str:
        return "base"


class Service(Base):
    """业务服务。"""

    def run(self) -> int:
        return helper()


def helper() -> int:
    """被内部调用的工具函数。"""
    return 42


MAX_RETRY = 3
