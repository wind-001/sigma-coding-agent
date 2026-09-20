# extensions —— 扩展样例

> 状态：P4 起填充。此处只定义约定，尚无实现。

扩展是运行时加载的 Python 模块，与内置工具**走同一套注册表接口**。
因此「用扩展替换内置工具」是免费的，`--no-builtin-tools` + 只加载扩展可直接演示。

## 边界（P4 实现时严格遵守）

扩展能做的事：

- 注册工具
- 注册钩子（`before_tool_call` / `after_tool_call` / `transform_context` 等）
- 注册 slash 命令

扩展**不能**做的事（刻意的收窄，理由见 `docs/decisions/D3-扩展层形态.md`）：

- 任意改写 harness 自身代码
- 静默覆盖内置工具（重名直接报 `DuplicateToolError`，要替换必须显式
  `--no-builtin-tools`）

## 热重载的硬约束

**调用方只能通过 `registry.get(name)` 取工具，不得缓存 `ToolDefinition` 实例。**

违反此约束的症状是「改了扩展代码但行为没变」，且极难排查。
这条约束会做成 CI 检查（见 `docs/architecture.md` 第 8 节）。

## 模块加载方式

必须每次生成新的 module 对象，**不要用 `importlib.reload()`**——
它保留同一个 module 对象，其它模块以 `from x import y` 形式持有的旧引用不会被更新。

三个必须处理的失败场景，各配一个单测：

1. 新模块导入时抛异常 → 保留旧注册表，返回失败报告，不进入半更新状态
2. 新模块没有注册任何工具 → 视为扩展被移除，清理旧注册项
3. 与内置工具重名 → 拒绝，保留旧状态
