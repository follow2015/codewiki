#!/usr/bin/env python3
"""
scan-secrets.py — 机密信息扫描器（移植自 ipip 同名脚本，按 codewiki 裁剪）

扫描文件内容，发现疑似机密则报告并退出非零码。
纯正则实现，无外部依赖，可在本地和 CI 中运行。

规则覆盖：
  1. 公网 IP（RFC1918 / 回环 / 掩码 / 文档网段放行）
  2. 私钥（-----BEGIN ... PRIVATE KEY-----）
  3. 高熵 token（gho_/ghp_/sk-/AKIA 等已知前缀）
  4. 密码赋值（明文即可，带不带引号都算；占位符与变量插值放行）
  5. 连接字符串含密码（mysql://<user>:<pwd>@host）

与 ipip 版的差异：去掉 MySQL DEFINER、语言包（locales）、OID 上下文豁免
（codewiki 无此三类场景）；豁免判据（整值插值 / 占位符 / $( 命令替换 /
命名空间键名）逐条保留 —— 那是实测换来的防漏报口径。

用法：
  python3 scripts/scan-secrets.py <file1> [file2 ...]

退出码：0 通过 | 1 发现机密 | 2 用法错误
"""
import re
import sys
from pathlib import Path

# ── 扫描规则 ────────────────────────────────────────────────────────────────
RULES: list[tuple[str, re.Pattern, str]] = [
    (
        "IP_ADDR",
        # 八位组必须 ≤255：非法字面量（999.1.1.1 这类举例值）不按 IP 报。
        re.compile(r"\b(?!0\.0\.0\.0|127\.0\.0\.1|255\.255\.255\.255|localhost)"
                   r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                   r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"),
        "IP 地址（可能是内网/公网 IP）",
    ),
    (
        "PRIVATE_KEY",
        re.compile(r"-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE KEY-----"),
        "私钥",
    ),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(gho_|ghp_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36,}\b"),
        "GitHub Token",
    ),
    (
        "AWS_KEY",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "AWS Access Key ID",
    ),
    (
        "SLACK_TOKEN",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
        "Slack Token",
    ),
    (
        "SK_PREFIX",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        "sk- 前缀密钥（OpenAI/Stripe 等）",
    ),
    (
        "PASSWORD_ASSIGN",
        # 键名允许任意 \w 前缀与可选引号：\b 在 `_` 后不成立（_ 属 \w），
        # 旧写法对带下划线的键名（`ssh_password`）100% 漏检。
        # 值允许**不带引号**：裸写或冒号分隔的明文都算，此前一律漏检。
        # 裸值取到空白为止，占位符/变量名/透传（值等于键名）在后面统一豁免。
        re.compile(
            r"(?i)['\"]?\w*(passwd|password|pwd|secret|api[_-]?key|apikey)['\"]?\s*[=:]\s*"
            r"(?:['\"]([^'\"]{4,})['\"]|(\S{4,}))"
        ),
        "明文密码赋值",
    ),
    (
        "CONN_STRING",
        re.compile(r"(?i)(mysql|redis|postgresql|mongodb)://\w+:[^@\s]+@"),
        "连接字符串含密码",
    ),
]

# ── 变量插值识别：值**整体**是变量引用才放行 ──────────────────────────────
# 判据必须锚定"整个值"，不能只判"值里出现过花括号"——字面量 `Xy{Ab}9`
# 只因含一对花括号就被豁免是静默漏报，比没有闸门更危险。
_VALUE_TAIL = re.compile(r"['\"]([^'\"]*)['\"]\s*$")

#: 占位符 / 间接取值形态（裸值没有引号可依，靠这些形状排除误报）
_PLACEHOLDER_VALUE = re.compile(
    r"^(?:<.*|\$\{.*|\$\(.*|\{.*|os\..*|getenv.*|environ.*|None|null|\*+|x+|\.+)$",
    re.IGNORECASE,
)
_INTERP_SEGMENT = (
    r"(?:\$\{[^{}]*\}"                        # ${VAR} / ${VAR:-default}
    r"|\$[A-Za-z_][A-Za-z0-9_]*"              # $VAR
    r"|\{[A-Za-z_][A-Za-z0-9_.\[\]'\"]*\})"   # {VAR} / {cfg['k']} / {obj.attr}
)
_INTERP_ONLY = re.compile(rf"^{_INTERP_SEGMENT}+$")
_BRACE_SEGMENT = re.compile(r"(?<!\$)\{[A-Za-z_]")
_FSTRING_PREFIX = re.compile(r"(?<![\w])[rb]{0,2}f[rb]{0,2}['\"]", re.IGNORECASE)

#: 值整体是**命名空间化的键名**（末段与机密字段同名）——"字段名 → key"映射形态：
#:     password: 'auth.password'
#: 刻意要求末段同名，"任意点分标识符都放行"会把真密码一并漏掉。
_NAMESPACED_KEY_ONLY = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)+(?:passwd|password|pwd|secret|api[_-]?key)$",
    re.IGNORECASE,
)

# ── 白名单：已知安全的 IP/模式（避免误报）──────────────────────────────────
SAFE_IPS = {
    "0.0.0.0", "127.0.0.1", "255.255.255.255",
    "1.1.1.1", "8.8.8.8", "8.8.4.4",  # 公共 DNS
    "1.2.3.4",  # 文档惯用占位 IP
    "239.0.0.1",  # 多播
    "255.255.255.0", "255.255.0.0", "255.0.0.0",
    "255.255.252.0", "255.255.248.0", "255.255.240.0",
}


def is_private_ip(ip: str) -> bool:
    """RFC1918 私有地址 + 回环 + 链路本地 + CGNAT + 子网掩码 + 文档网段。"""
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    o = [int(p) for p in parts]
    if o[0] in (255, 10, 127):
        return True
    if o[0] == 172 and 16 <= o[1] <= 31:
        return True
    if o[0] == 192 and o[1] == 168:
        return True
    if o[0] == 169 and o[1] == 254:
        return True
    if o[0] == 100 and 64 <= o[1] <= 127:
        return True
    # 文档网段 RFC5737
    if o[0] == 192 and o[1] == 0 and o[2] == 2:
        return True
    if o[0] == 198 and o[1] == 51 and o[2] == 100:
        return True
    return bool(o[0] == 203 and o[1] == 0 and o[2] == 113)


# 文件中允许出现 IP 的路径前缀（示例配置 / 文档）
SAFE_PATH_PREFIXES = (".example", "README", "docs/")


def scan_file(path: Path, text: str | None = None) -> list[tuple[int, str, str, str]]:
    """扫描单个文件，返回 [(行号, 规则ID, 匹配内容, 描述)] 列表。"""
    findings: list[tuple[int, str, str, str]] = []

    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return findings

    # 跳过二进制文件
    if "\x00" in text[:8192]:
        return findings

    full = str(path).replace("\\", "/")
    # 示例文件/文档按文件名或路径片段放行 IP 规则
    is_safe_path = any(path.name.startswith(p) or "/docs/" in full for p in SAFE_PATH_PREFIXES)

    for lineno, line in enumerate(text.splitlines(), 1):
        for rule_id, pattern, desc in RULES:
            for m in pattern.finditer(line):
                matched = m.group(0)

                if rule_id == "IP_ADDR":
                    if matched in SAFE_IPS or is_private_ip(matched):
                        continue
                    if is_safe_path:
                        continue
                    # 版本号形如 x.y.z.w（四段 <100 且行内含 version/==）
                    parts = matched.split(".")
                    if (
                        len(parts) == 4
                        and all(p.isdigit() and int(p) < 100 for p in parts)
                        and ("version" in line.lower() or "==" in line)
                    ):
                        continue

                if rule_id == "PASSWORD_ASSIGN":
                    placeholder = m.group(0).lower()
                    if any(p in placeholder for p in ("your", "xxx", "change", "example", "<", "placeholder")):
                        continue
                    tail = _VALUE_TAIL.search(m.group(0))
                    value = tail.group(1).strip() if tail else ""
                    # 裸值（不带引号）：命中后还要排除占位符与间接取值，
                    # 否则 `password = os.environ[...]` / `password = ${PW}`
                    # 会把正常写法刷成告警。组序：1=键名 2=引号值 3=裸值。
                    bare = (m.group(3) or "").strip().rstrip(",")
                    if bare and m.group(2) is None:
                        if _PLACEHOLDER_VALUE.match(bare) or re.fullmatch(r"[A-Z][A-Z0-9_]*", bare):
                            continue
                        # 值与键名同名（参数透传）不是机密
                        if bare == m.group(1):
                            continue
                        value, tail = bare, None
                    # 值整体是插值（且裸花括号只在 f-string 里才放行）
                    if tail and _INTERP_ONLY.match(value) and (
                        not _BRACE_SEGMENT.search(value)
                        or _FSTRING_PREFIX.search(line)
                    ):
                        continue
                    # 值是命令替换（以 $( 开头）——运行时计算，非明文
                    if tail and value.startswith("$("):
                        continue
                    # 值在指代键本身（字段名 → key 的映射）
                    if tail and _NAMESPACED_KEY_ONLY.match(value):
                        continue
                    # 值整体是 UPPER_SNAKE 的**环境变量名**（指向取值来源，如
                    # ENV_API_KEY = "OPENAI_API_KEY"）—— 不是明文。真实密码
                    # 几乎不会是纯大写下划线形态（实测误报据此放宽）。
                    if tail and re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
                        continue

                if rule_id == "CONN_STRING" and any(
                    p in m.group(0).lower() for p in ("password", "secret", "your", "xxx", "<")
                ):
                    continue

                findings.append((lineno, rule_id, matched, desc))

    return findings


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: scan-secrets.py <file1> [file2 ...]", file=sys.stderr)
        return 2

    all_findings: list[tuple[str, int, str, str, str]] = []
    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists():
            continue
        for lineno, rule_id, matched, desc in scan_file(path):
            all_findings.append((arg, lineno, rule_id, matched, desc))

    if not all_findings:
        print("✅ 未发现机密")
        return 0

    print(f"❌ 发现 {len(all_findings)} 处疑似机密：\n", file=sys.stderr)
    for path, lineno, rule_id, matched, desc in all_findings:
        shown = matched[:80] + "..." if len(matched) > 80 else matched
        print(f"  [{rule_id}] {path}:{lineno}", file=sys.stderr)
        print(f"    {desc}", file=sys.stderr)
        print(f"    匹配: {shown}\n", file=sys.stderr)

    print("⚠️  请确认这些内容是否为机密。如是，请从文件中移除后再同步。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
