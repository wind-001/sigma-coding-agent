"""协作域子包:任务板状态机(board)+ 落盘(store)+ 信箱(mailbox)。

层位:import-linter 层表最底层——**零 sigma 内部依赖(纯 stdlib)**,
任何上层(tools/runtime/sdk)都可以向下引用它,它不引用任何上层。
结构见 docs/plans/P4-团队任务-详规.md §2.0(星辰拍板 2026-09-30)。
"""
