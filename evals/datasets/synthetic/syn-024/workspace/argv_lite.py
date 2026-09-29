"""迷你命令行参数解析器(为假想 CLI 工具 ``toolkit`` 解析 argv)。

约定的命令行界面:
- 开关:``-v`` / ``--verbose``、``-n`` / ``--dry-run``;短开关可组合,
  ``-vn`` 等价于 ``-v -n``;组合串里只允许开关字符;
- 值选项:``-o`` / ``--output``、``-j`` / ``--jobs``;值是紧随的下一个参数
  (即使它以 ``-`` 开头);``--output=x`` 的 ``=`` 写法只支持长选项,且
  ``=`` 之后的内容**原样**作为值(值本身可以再含 ``=``);
- ``--`` 之后的所有参数一律是位置参数,哪怕长得像选项;
- 单独的 ``-`` 与不以 ``-`` 开头的参数都是位置参数;
- 结果里的选项名一律用**长名**(``-o`` 记成 ``output``);开关按首次出现
  顺序去重;
- 未知选项抛 ``UnknownOptionError``;值选项缺值抛 ``MissingValueError``;
  两者都是 ``ArgvError``(``ValueError`` 子类)。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ArgvError(ValueError):
    """参数解析失败的基类。"""


class UnknownOptionError(ArgvError):
    """出现了约定之外的选项。"""


class MissingValueError(ArgvError):
    """值选项缺少值。"""


@dataclass
class ParseResult:
    """解析结果:开关(去重、有序)、选项值、位置参数。"""

    flags: tuple[str, ...] = ()
    options: dict[str, str] = field(default_factory=dict)
    positionals: tuple[str, ...] = ()


_SHORT_FLAGS: dict[str, str] = {"v": "verbose", "n": "dry-run"}
_LONG_FLAGS: frozenset[str] = frozenset({"verbose", "dry-run"})
_SHORT_VALUES: dict[str, str] = {"o": "output", "j": "jobs"}
_LONG_VALUES: frozenset[str] = frozenset({"output", "jobs"})


def parse(argv: list[str]) -> ParseResult:
    """按上述约定解析参数列表。"""
    flags: list[str] = []
    options: dict[str, str] = {}
    positionals: list[str] = []
    only_positional = False

    def add_flag(canonical: str) -> None:
        if canonical not in flags:
            flags.append(canonical)

    i = 0
    while i < len(argv):
        arg = argv[i]
        i += 1
        if only_positional:
            positionals.append(arg)
            continue
        if arg.startswith("--"):
            body = arg[2:]
            name, sep, _value = body.partition("=")
            if name in _LONG_FLAGS:
                add_flag(name)
            elif name in _LONG_VALUES:
                if sep:
                    parts = body.split("=")
                    options[name] = parts[1]
                elif i < len(argv):
                    options[name] = argv[i]
                    i += 1
                else:
                    options[name] = None
            else:
                raise UnknownOptionError(arg)
            continue
        if arg == "--":
            only_positional = True
            continue
        if arg.startswith("-") and arg != "-":
            body = arg[1:]
            if len(body) == 1 and body in _SHORT_VALUES:
                if i < len(argv):
                    options[_SHORT_VALUES[body]] = argv[i]
                    i += 1
                else:
                    options[_SHORT_VALUES[body]] = None
                continue
            if body in _SHORT_FLAGS:
                add_flag(_SHORT_FLAGS[body])
                continue
            raise UnknownOptionError(arg)
        positionals.append(arg)
    return ParseResult(flags=tuple(flags), options=options, positionals=tuple(positionals))
