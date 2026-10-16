# B 编码与字符(中文 Windows 为主)

中文 Windows 下 python 的 stdio 默认不是 UTF-8 —— 这是本平台一半诡异故障的源头.
一句话判据: `python -c "import sys; print(sys.stdin.encoding, sys.stdin.errors, sys.stdout.encoding)"`.

---

### B1 子进程 stdout/stderr 默认 ANSI 码页(中文机=GBK): 打印非 GBK 字符即崩

- 现象: 脚本直接跑好好的, 输出重定向到文件/管道, 打印 `⚠ ✓ −` 这类字符时
  `UnicodeEncodeError` 崩掉(GBK 里没有这些码位).
- 根因: stdout 是控制台时 Python 走 UTF-8(WriteConsoleW)没事, 一旦重定向就按 cp936 严格编码.
- 判据: 同脚本控制台 OK、重定向崩, 就是它.
- 对策: 脚本开头 `sys.stdout.reconfigure(encoding="utf-8")`(stderr 同), 或环境变量
  `PYTHONIOENCODING=utf-8`(bat 启动器里 set). 新写的 CLI 一律先 reconfigure 再输出.
- 出处: 多起; windows-bat-gotchas(2026-09-24), version.py CLI(2026-10-12).

### B2 ★子进程 **stdin** 同坑 + surrogateescape: 中文参数崩掉整条通道

- 现象: 通过 stdio 通道(如 MCP)传**非 ASCII 参数**, server 要么回乱码, 要么直接崩掉
  (退出码 1/2), 客户端报 "server exited" / write failed Errno 22.
- 根因: 中文机 python 子进程 stdin 默认 **GBK + surrogateescape**; 客户端按协议写的
  UTF-8 字节被解成**半截代理字符**(如 `\udcb2`), server 再 `json.dumps(ensure_ascii=False)`
  往 UTF-8 严格 stdout 回写时 `UnicodeEncodeError: surrogates not allowed` → 整个 server 退出.
- 判据: 报错里见 `surrogates not allowed` / `\udcXX`; 非 ASCII 参数必现、纯 ASCII 全过.
- 对策(三层, 一起上):
  ① 写端线材用 **`ensure_ascii=True`**(非 ASCII 全转 \uXXXX 转义, 纯 ASCII 字节在
     任何编码假定的对端都解出同一结果 —— 免疫第三方 server 同款毛病);
  ② server 端 `sys.stdin.reconfigure(encoding="utf-8", errors="replace")`(**只 reconfigure
     stdout 不够, stdin 必须同款**; replace 让垃圾字节退化成 U+FFFD, 坏行跳过后 server 不死);
  ③ 回归测试在**干净环境**跑(见 C1).
- 出处: agent-term mcpclient + req/fake_mcp_server.py + wechat-eye/server.py(2026-10-13;
  旧版 wechat-eye 实证收中文参数退 2, 修后正常回 JSON-RPC 退 0).

### B3 WinRT/存储 API 路径只认反斜杠 —— 与 bash 正斜杠家法互为反例

- 现象: `GetFileFromPathAsync`(WinRT StorageFile)对 `C:/Users/...` 正斜杠路径抛
  AggregateException; 而 Test-Path 吃正斜杠照放行 → 前置检查全过、调用必炸, 模型
  连环重试"没通过".
- 根因: WinRT 只认规范反斜杠路径; 同参不同命(screenshot 走 .NET 就不敏感).
- 对策: 进门一行 `$Image = [System.IO.Path]::GetFullPath($Image)` 归一(正斜杠/相对全救回).
- 口诀: **bash argv 必须正斜杠; WinRT/存储 API 恰好相反** —— 别拿一条家法套所有层.
- 出处: wechat-eye ocr.ps1(2026-10-10, 七连炸实弹).

### B4 heredoc 生成代码/传脚本会吃掉转义

- 现象: 用 heredoc 生成 python 源码, `\n` 变成真换行、生成的字符串字面量全乱;
  给 WSL 传脚本, heredoc 里 `$( )` 被提前展开成空.
- 根因: heredoc 里 shell 先做一层转义/展开, 与想写的源码转义相互打架(写 py 源码尤其毒).
- 对策: **多行 Python/脚本别用 heredoc 生成** —— 用 Write 工具直接落盘;
  给 WSL 传脚本先落盘再 `cp`. 一次性短命令才用 heredoc.
- 出处: agent-term 开发(2026-10-13, `_maybe_auto_continue` 生成物被毁); WSL 笔记(2026-09-25).

### B5 行尾 CRLF/LF 混淆让 git diff 整个文件变红

- 现象: 只改了几行, `git diff` 整个文件全红/全绿; git 警告 "LF will be replaced by CRLF".
- 根因: 不同工具/平台写文件行尾不同(Windows 记事本 CRLF, 生成器可能平台默认).
- 判据: `git diff -w` 或看 `file`/十六进制里的 ^M; 内容一样就是行尾差.
- 对策: 程序生成文本文件显式写 `\n`(open(..., newline="") 或 write 明确换行);
  看 diff 先排除行尾因素, 别急着以为内容全改.
- 出处: agent-term 多处(2026-10-13 前后).
