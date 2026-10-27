"""应用入口：通过包的重导出名使用服务。"""

from pkg import Svc


def main() -> int:
    return Svc().run()
