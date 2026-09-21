<#
    sigma 启动器（PowerShell）

    用法：
      .\sigma.ps1              -> 交互模式（工作区 = 当前目录）
      .\sigma.ps1 "某个任务"    -> 一次性模式
      $env:SIGMA_NO_PAUSE=1    -> 结束时不等待按键（供脚本调用）

    设计
        本文件**只负责挑解释器与转发参数**，全部逻辑都在 ``sigma.cli:main``。
        入口可以有多个（bat / ps1 / python -m / sigma.exe），**组装只有一个**——
        否则每加一个入口就多一份会漂移的初始化代码。

    为什么要 ``Read-Host`` 收尾
        双击运行的窗口，脚本一结束就关，人什么也看不到。
        这是 Windows 上最经典的那个坑，不修它就等于"点了没反应"。
#>

$ErrorActionPreference = "Stop"

$here = $PSScriptRoot
$py = Join-Path $here ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "[harness 错误] 找不到虚拟环境的 python：$py"
    Write-Host '请先执行：python -m venv .venv  然后  python -m pip install -e ".[dev]"'
    if ($env:SIGMA_NO_PAUSE -ne "1") { Read-Host "按回车键退出" | Out-Null }
    exit 2
}

if ($args.Count -eq 0) {
    # 无参：交互模式（工作区 = 当前目录）
    & $py -m sigma
} elseif ($args[0] -like "-*") {
    # 第一个参数像开关 -> 全部原样转发给 CLI（如 --workspace D:\code）
    & $py -m sigma @args
} else {
    # 否则它就是任务本身
    & $py -m sigma -p ($args -join " ")
}

# 先把退出码存下来：Q4 规定非 0 = harness 失败。
# 包装脚本若总是返回 0，就把"sigma 崩了"悄悄改成"一切正常"。
$rc = $LASTEXITCODE
if ($env:SIGMA_NO_PAUSE -ne "1") {
    Read-Host "按回车键关闭" | Out-Null
}
exit $rc
