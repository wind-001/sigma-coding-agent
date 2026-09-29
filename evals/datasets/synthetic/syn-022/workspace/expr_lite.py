"""简易整数表达式求值器:四则运算 + 括号 + 优先级。

约定:
- ``tokenize(text)`` 把表达式切成记号:非负整数(十进制)与 ``+ - * / ( )``,
  记号之间允许任意空白;出现其他字符抛 ``ValueError``;
- ``to_rpn(tokens)`` 用调度场算法把中缀记号转成后缀记号(RPN):
  - 优先级:``*`` ``/`` 高于 ``+`` ``-``;
  - 同优先级**左结合**(``8-3-2`` 按 ``(8-3)-2`` 求值);
  - 括号只改变结合顺序,本身不进输出;
- ``evaluate_rpn(rpn)`` 对后缀记号求值,返回整数;除法**向零截断**
  (``9/2`` 是 ``4``,``(2-9)/2`` 是 ``-3``);除数为 0 时自然抛 ``ZeroDivisionError``;
- ``evaluate(expr)`` 是三者的组合:tokenize → to_rpn → evaluate_rpn。
"""

from __future__ import annotations

#: 运算符的优先级:数字越大越先算。
_PRECEDENCE: dict[str, int] = {"+": 1, "-": 1, "*": 1, "/": 1}


def tokenize(text: str) -> list[str]:
    """把表达式字符串切成记号列表。"""
    tokens: list[str] = []
    digits: list[str] = []

    def flush() -> None:
        if digits:
            tokens.append("".join(digits))
            digits.clear()

    for ch in text:
        if ch.isdigit():
            digits.append(ch)
            continue
        flush()
        if ch.isspace():
            continue
        if ch in "+-*/()":
            tokens.append(ch)
        else:
            raise ValueError(f"非法字符:{ch!r}")
    flush()
    return tokens


def to_rpn(tokens: list[str]) -> list[str]:
    """调度场算法:中缀记号 → 后缀记号。"""
    output: list[str] = []
    stack: list[str] = []
    for token in tokens:
        if token.isdigit():
            output.append(token)
        elif token in _PRECEDENCE:
            while (
                stack
                and stack[-1] in _PRECEDENCE
                and _PRECEDENCE[stack[-1]] > _PRECEDENCE[token]
            ):
                output.append(stack.pop())
            stack.append(token)
        elif token == "(":
            stack.append(token)
        elif token == ")":
            while stack and stack[-1] != "(":
                output.append(stack.pop())
            if not stack:
                raise ValueError("括号不匹配:缺少左括号")
        else:
            raise ValueError(f"非法记号:{token!r}")
    while stack:
        output.append(stack.pop())
    return output


def evaluate_rpn(rpn: list[str]) -> int:
    """对后缀记号求值。"""
    values: list[int] = []
    for token in rpn:
        if token.isdigit():
            values.append(int(token))
            continue
        if token not in "+-*/":
            raise ValueError(f"非法记号:{token!r}")
        left = values.pop()
        right = values.pop()
        if token == "+":
            values.append(left + right)
        elif token == "-":
            values.append(left - right)
        elif token == "*":
            values.append(left * right)
        else:
            values.append(int(left / right))
    if len(values) != 1:
        raise ValueError("表达式不完整")
    return values[0]


def evaluate(expr: str) -> int:
    """tokenize → to_rpn → evaluate_rpn 的组合入口。"""
    return evaluate_rpn(to_rpn(tokenize(expr)))
