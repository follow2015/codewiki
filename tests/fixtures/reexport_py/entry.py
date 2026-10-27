"""入口：全部通过包的重导出名使用。"""

from pkg import A, AliasB, C


def use() -> None:
    A()
    AliasB()
    C()
