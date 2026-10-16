# D 子进程·管道·进程

「子进程吃掉通道/卡死/跑一半全丢/杀不干净」这一类. 配套: B1/B2(stdio 编码).

---

### D1 子进程继承的句柄会吃掉你的通道 → stdin=DEVNULL

- 现象: MCP 通道里待处理的请求行神秘消失(被别的子进程先读走了).
- 根因: 起的子进程(PowerShell 等)默认继承 stdin 句柄, 把协议行当自己的输入吃掉.
- 对策: 与协议无关的一切子进程显式 `stdin=subprocess.DEVNULL`(**连 taskkill 自己都要**).
  注意与 MCP server 不矛盾 —— 那是故意 PIPE 喂协议的.
- 出处: wechat-eye server.py(2026-09-30).

### D2 子进程 stderr 必须重定向到文件(防写满管道死锁)

- 现象: 子进程崩/挂起, 通道整体卡死.
- 根因: stderr 还是管道, 子进程写满管道缓冲(无人读) → 阻塞 → 整条通道死锁.
- 对策: stderr 一律重定向到**文件**(`run/mcp-<name>.log` 之类), 且文件用追加模式 ——
  它也是事后查子进程崩溃 traceback 的第一现场(见 B2 案例).
- 出处: mcpclient.py(起 server 铁律); 2026-10-13 靠 mcp-fake.log 破案.

### D3 长任务别把 agent 会话的管道当 stdout(EPIPE 全丢)

- 现象: 长实验/长脚本跑到一半崩, 结果全丢(脚本只在结尾 dump).
- 根因: 用会话管道当 stdout; 会话一死读端消失, 下次 print 即 BrokenPipeError 崩掉.
- 对策: 长跑一律 `nohup … > 项目/运行日志_日期.txt 2>&1 &` 落盘;
  **急救**: 进程还活着时接回读端 —— `wsl -u root` 后 `os.open("/proc/PID/fd/1",
  O_RDONLY|O_NONBLOCK)` 持有并循环 drain 到日志(重开 read 端后 write 从 EPIPE 变 OK).
- 出处: WSL 长实验(2026-09-21/25).

### D4 从 wsl.exe 起的后台进程, 会话一退即被收割

- 现象: `nohup ... &` 起的进程日志 0 字节, 早死了.
- 根因: wsl.exe 会话退出把子进程一起收割, nohup 不够.
- 对策: `setsid nohup … < /dev/null &` 彻底脱离会话; 起完另开独立 wsl 会话 `pgrep` 验证存活;
  完成通知用守望任务 `while kill -0 PID; do sleep 60; done`.
- 出处: WSL 长实验(2026-09-25).

### D5 bash heredoc 喂 stdin 只送达前几行就假 EOF

- 现象: 测 stdio server 时像"server 挂了只处理前几条"; 用 heredoc 当输入源.
- 根因: heredoc/管道喂多行给交互式 stdin 提前假 EOF.
- 对策: 测试喂 stdin 用**文件重定向**(`python server.py < req.jsonl`, req.jsonl 留作回归夹具).
- 出处: wechat-eye(2026-09-30).

### D6 Windows 杀进程树 + 防孤儿 python

- 对策: `taskkill /F /T /PID <pid>`(树杀); 换姿势前先 `proc.stdin.close()` + wait 给你机会
  自己退; atexit 兜底注册 shutdown. 起子进程加 `CREATE_NEW_PROCESS_GROUP`.
- 出处: mcpclient.py(2026-10-0x).

### D7 交付脚本的"真实路径"验证: 双击等价 ≠ bash 直跑

- 现象: 你测全过, 用户双击没反应.
- 根因: Git Bash 里 `cmd /c run.bat` 因 `NoDefaultCurrentDirectoryInExePath=1` 找不到 bat;
  真双击等价路径是 `.\run.bat`(配 `MSYS2_ARG_CONV_EXCL='*'`).
- 对策: bat/启动器**实测两条路**: 双击等价路径 + 输出重定向到文件; 测试目录带空格+中文
  最能暴露引号问题.
- 出处: windows-bat-gotchas(2026-09-24).
