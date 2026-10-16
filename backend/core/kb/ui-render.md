# A 界面与渲染(tkinter/GUI)

「控件在代码里好好的, 用户就是看不见/点不到/样子不对」这一类.
排查顺序建议: 先 A1(widget 到底有没有进布局) → 再 A2(被谁盖住) → 再 A3(是不是被挤没).

---

### A1 控件从未 pack/grid — 整行空框的"隐形输入框"

- 现象: 行 Frame 在、字段标签在, 但输入区域空白; 点不动; 连写在 Entry 里的占位提示也看不见.
- 根因: 造了 Entry 但**从未调用任何几何管理器** → 尺寸 1×1, 永不显示. 表单行自身 pack 了,
  所以"行是可见的", 极具迷惑性.
- 判据: `w.winfo_manager() == ""`(且 `winfo_ismapped() == 0`).
- 对策: 创建即 pack(写个工厂函数统一做, 别在散点漏); **测试断言必须查 winfo_manager/
  winfo_ismapped** —— 只查 `.get()` 值与父行的 manager 会完美漏掉(冒烟和人工复现都这么漏过).
- 出处: frontend.py VoicePanel._mk_ent(2026-10-13, 用户报"自定义接口没有输入方式").

### A2 层叠序: 控件被后建的兄弟 Frame 盖住(几何全对, 渲染 0 像素)

- 现象: "下拉框/控件消失". winfo 几何、mapped、req 全对, 就是看不见.
- 根因: Tk 同层兄弟**按创建顺序叠放**: 控件先于所在行 Frame 创建(父容器是窗口不是行),
  后建的不透明行 Frame 把它整个盖住. `pack(in_=行)` 只改布局归属, **不改层叠序**.
- 判据: 把行 Frame 背景色改成鲜艳色 —— 框还在原位被盖着; 或 `winfo_containing(x,y)` 看是谁.
- 对策: 铁律 = **先建 Frame, 再以该 Frame 为父容器创建其中所有控件**(别用 `in_=` 事后补救).
  验证必须采样真实屏幕像素(winfo 全对也白搭; winfo_ismapped 盖住时仍可能为 1, 别全信它).
- 出处: desktop-workbench 设置页下拉框(2026-10-08, 曾误诊为对比度问题反而改恶化).

### A3 空 Canvas 默认请求高度 ~265px, 把后面 pack 的兄弟挤成 1×1

- 现象: "某个控件消失/没有输入空间", 消失的总是**最后 pack 的那个**.
- 根因: 空 `tk.Canvas` 默认请求高度 ~265px; 放进固定高容器后总请求超宽超深,
  pack 从后往前挤, 最后 pack 的兄弟被挤成 1×1 不渲染.
- 判据: 看该控件 `winfo_reqwidth/reqheight` 是不是异常大/为 1.
- 对策: Canvas 永远显式给 `height=N`(小请求), 别靠默认.
- 出处: desktop-workbench 待办输入框(2026-10-08).

### A4 裸 `pack()` 进的是 widget 的父容器, 不是"当前行"

- 现象: 标签和控件不在同一行(控件跑到窗口某一排).
- 根因: `外部创建的控件(parent=win)` 在行内裸 pack → 挂到窗级.
- 对策: `w.pack(side="left", in_=row)`(注意 in_= 只改布局不改 winfo 父子, 断言用
  rootx/rooty 几何而不是 winfo_children); 根治同 A2 = 以行 Frame 为父创建.
- 出处: desktop-workbench(2026-10-08).

### A5 macOS Aqua 主题无视 tk.Button 的 bg/fg/activebackground

- 现象: Mac 上按钮配色/选中态/悬停全变形("风格对不上 Windows").
- 根因: Aqua 原生绘制按钮, 平涂设计依赖的 bg/fg 全部被忽略.
- 对策: `_FlatLabelBtn` = Label 画按钮(自绑点击/悬停/禁用态, 覆盖 invoke/configure/cget
  常用子集) + `FlatBtn` 开关: Darwin=shim, 其余平台=tk.Button 别名(零行为变化).
- 出处: frontend.py FlatBtn(2026-10-13).

### A6 Windows vista 主题忽略 ttk 配色 configure

- 现象: ttk 控件 fieldbackground 等设了无效, 实际系统白底黑字.
- 根因: vsapi 原生绘制.
- 对策: 与 A5 同思路 —— 别跟主题较劲, 用不依赖主题的 Widget(Label/自绘)画想要的样式.
- 出处: desktop-workbench(2026-10-08).

### A7 直接 configure 的状态文字被周期 tick 冲掉

- 现象: 状态栏刚写"等你确认", 120ms 后闪回默认文案.
- 根因: 周期性刷新(tick)每次用状态变量重写文案, 你的一次性 configure 被覆盖.
- 对策: 不直接写控件 —— 更新**状态属性**, 让 tick 按属性统一渲染(如 `_awaiting_card` +
  `_tick_status`).
- 出处: frontend.py SessionPanel(2026-10-13).

### A8 Toplevel 里先 destroy 再读 Entry → TclError invalid command name

- 现象: `TclError: invalid command name ".!frame2.!frame5.!toplevel2.!entry"`(Mac/Win 全平台).
- 根因: save() 先 `box.destroy()` 再 `ent.get()` —— 控件没了才取值.
- 对策: **先取所有值, 再 destroy**; 冒烟测试要点真对话框的按钮(直调内部函数会绕过此路径).
- 出处: version.py 建版对话框(2026-10-13, Mac 同事实测).

### A9 窗口销毁后 after 回调仍触发 → Tcl 噪声, 不代表失败

- 现象: 退出/关闭窗后打印 `invalid command name "…_drain"` 之类.
- 根因: `after(200, self._drain)` 链在窗口 destroy 后还排着队.
- 对策: 出现它先**确认对应窗口确实该销毁了**(是无害收尾噪声); 要根治就在 drain 里
  先判 `winfo_exists()`. 测试脚本别把它当 fail.
- 出处: panel_smoke 退出噪声(2026-10-13).

### A10 画布拖拽/连线坐标缺 canvasx/canvasy 换算

- 现象: 滚动过画布后, 拖出来的线/拖动的东西不跟鼠标, 偏移越滚越大(实测 (299,1691)px).
- 根因: 用了窗口坐标, 没换算成画布坐标.
- 对策: 一切画布内坐标经 `canvas.canvasx/canvasy` 换算; 端口等小命中区 z 序置顶防被既有
  连线抢(第二根拖拽起手被抢的经典).
- 出处: factory.py 组装厂(2026-10-13).

### A11 切页重建导致状态丢失 → 常驻不重建

- 现象: 切走再切回, 画布/正在编辑的东西没了.
- 根因: 每次显示页面 destroy 重建.
- 对策: 有状态的页面改**常驻**(首次建好后只 raise/hide 切换).
- 出处: factory.py(2026-10-13).

### A12 Text 内嵌图片两个坑: image_create 不吃 -tags; PhotoImage 被 GC

- 现象: `Text.image_create(..., tags=...)` 抛 TclError; 缩略图显示一下变空白.
- 根因: ① image_create 不接受 -tags 参数; ② PhotoImage 没有 Python 引用被垃圾回收.
- 对策: Label 装图 + `text.window_create(...)`; 面板持 `_img_refs` 引用防 GC.
- 出处: frontend.py _insert_thumb(2026-10-13, M15 聊天流缩略图).

### A13 pythonw 无 console, 异常全静默 → report_callback_exception 兜底

- 现象: 点了按钮没反应, 没有任何报错(比如 `tk.Combobox` 写错 —— 它在 ttk 不在 tk).
- 根因: pythonw 无控制台, 回调异常被吞.
- 对策: 任何 GUI 必挂 `Tk.report_callback_exception` 弹窗兜底; 记牢 Combobox 在 ttk.
- 出处: desktop-workbench(2026-10-08).

### A14 grab_set 在窗口未 viewable 时抛错, 且半残 grab 冻结全应用

- 现象: "所有按钮都没用"—— 半残窗冻住输入.
- 根因: grab 在崩溃前已生效且没释放.
- 对策: 自家对话框**别 grab**; 需要模式感用 topmost + 独立 Toplevel.
- 出处: desktop-workbench(2026-10-08).

### A15 overrideredirect 窗口拿不到系统键盘焦点(Entry 打不了字)

- 对策: ctypes `SetForegroundWindow(GetAncestor(winfo_id(), GA_ROOT))`.
- 出处: desktop-workbench(2026-10-08).

### A16 非置顶无边框窗口会被前台窗口盖回 → topmost-flip

- 现象: 展开瞬间可见随即被盖.
- 对策: `SetWindowPos(HWND_TOPMOST)` → `SetWindowPos(HWND_NOTOPMOST)` 落回常规层最顶;
  短命弹窗干脆真 `-topmost`.
- 出处: desktop-workbench(2026-10-08).

### A17 bind_all 全局滚轮: 不带 add 的二次注册会把别家的绑定静默顶掉

- 现象: 组装厂画布滚轮"有点失灵" —— 刚打开好用, 进过模型页/语音页再回来就死了
  (页面常驻不重建, 不会再绑).
- 根因: 两处 `bind_all("<MouseWheel>")` —— 组装厂带 `add="+"` 追加, ScrollFrame
  **不带 add**; Tk 的绑定表里同一 tag+序列只存一份脚本列表, 不带 add 的 bind
  整条替换 —— 模型页每次进都重建 ScrollFrame, 重建即把组装厂的滚轮抹掉.
- 判据: `root.tk.eval("bind all <MouseWheel>")` 的行数比预期少(每行一条脚本).
- 对策: 全局序列(bind_all)一律 `add="+"`, 且**一个类只注册一条分发**(类级
  dispatch + 活实例表, 重建不累积, 分发时惰性清死件); 旗标挂各自根(多 Tk 根
  =多解释器, 各绑各的). 冒烟锚: 页面重建后数全局绑定条数不变.
- 出处: agent-term M32.1(2026-10-16), 设置页加滚动条时顺藤摸出组装厂失灵根.

### A18 Tk 窗口上子类化截"投递型"消息的 ctypes 回调必崩(GIL); 正解=专用消息线程

- 现象: 托盘(Shell_NotifyIcon)/PostMessage 类**投递型**消息, 用 dropfiles.py 同款
  SetWindowLongPtrW 子类化 Tk 窗口去截, 回调一进来就
  `PyEval_RestoreThread: GIL released`(fatal, 进程死), update() 与 mainloop()
  双双实锤; 而 dropfiles 截 WM_KEYDOWN/WM_DROPFILES(输入型)同一手法活得好好的.
- 根因: 投递型消息在 Tcl 事件分发的 **GIL 释放相位**进来, ctypes 蹦床拿不到
  线程状态; 输入型消息走另一条分发路(线程状态在) —— **同一手法对不同消息
  类型结论不同, dropfiles 先例不能类推**.
- 对策: 专用 `threading.Thread`(Python 线程=永远有线程状态)里
  RegisterClassW+CreateWindowExW(message-only, HWND_MESSAGE) + GetMessage 循环;
  wndproc 里只 `queue.put`(纯 Python 零 Tcl), Tk 侧 `root.after` 轮询消费(控件
  操作全回主线程). 坑位: 同名类二次 Register 必失败(幂等忽略, 类进程级复用,
  **但类 proc 是谁的? 建窗后按实例子类化**, 不然第二只实例的事件进第一只的
  蹦床); GetDC 在 user32 不在 gdi32.
- 出处: agent-term M33 托盘(2026-10-16), 路线史全文在 tray.py 模块头.
