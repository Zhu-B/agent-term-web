---
name: regression-three-piece
description: 改动后必验的三件套回归(回放/冒烟/布局), 按此顺序跑, 全绿才算改完
---

# 三件套回归 —— agent-term 改动后的标准验证流程

改完代码(尤其动了 frontend.py / tools.py / prompts.py / agent.py 或加了新模块)后, 按顺序跑下面三件, 全绿才算改完。工作目录 = agent-term 仓库根。

## 命令

```bash
cd /c/Users/10601/agent-term
PYTHONIOENCODING=utf-8 python _verify.py        # ① 回放回归 + 各子系统自检
PYTHONIOENCODING=utf-8 python panel_smoke.py    # ② 面板行为冒烟(需显示环境)
PYTHONIOENCODING=utf-8 python panel_layout_test.py  # ③ 布局回归(缩窗/切换/滚动)
```

Windows 控制台一律加 `PYTHONIOENCODING=utf-8` —— 输出里有 ✓/⚠ 等非 GBK 字符, 不设会崩在打印上(不是代码坏了)。

## 各自看什么

- `_verify.py`: 全场景回放 + 各子系统自检, **零联网零花费**。过 = 末尾打印 `VERIFY ALL PASS`; 有场景 diff 会报"首个差异@事件N"。**刻意改了行为**之后: 先逐字段审计 diff(确认只有预期变化), 再 `python _verify.py --update-golden` 重铸, 然后**连跑两次**都 PASS 才收(golden 必须确定性)。
- `panel_smoke.py`: 面板行为冒烟(询问流/排队/chips/多key槽/ask_user/platform/记忆治理/组装厂/客制化/分身/自动继续…)。过 = 单行 `PANEL SMOKE PASS (...)`; 挂在哪一节会打 `PANEL SMOKE FAIL(n) ...`。
- `panel_layout_test.py`: 布局回归(缩窗/切换/滚动, 需显示环境)。过 = 末尾 OK 且无 FAIL 行。

## 规矩

- 一件没绿就修那件, 修完**从头重跑**受影响的套件, 不要带着红往下一件走。
- 回归 golden 以 Windows 输出为基线; Mac 上跑 _verify 有平台差异属预期。
- 跑完确认工作区改动都在(不 commit —— commit 由用户自己做)。
