# agent-term web · 前端

**纯静态三件**（index.html / app.js / style.css）—— 零框架、零构建、零 CDN、零
字体外链（CDN 在国内会被墙）。三种用法都不需要任何环境:

1. **后端同源托管（推荐）**: 后端 `python server.py` 默认就挂本目录, 浏览器开
   `http://后端地址:8787` 即用, 什么都不用配。
2. **双击直开**: 直接双击 `index.html`（file:// 打开）, 点「⚙ 连接」填后端地址
   （如 `http://192.168.x.x:8787`）+ 令牌, 存浏览器 localStorage。
3. **静态托管**: 整个目录丢到任何静态托管（Gitee Pages / Cloudflare Pages /
   GitHub Pages / nginx）, 打开后同样在「⚙ 连接」里填后端 API 地址。

## 功能（v1 = 聊天 MVP）

- 多会话（新建/切换, 断线按 seq 续传不丢事件）
- 聊天流: 用户/助手气泡、流式逐字、工具行（点开看参数与结果）、⤷ 分身缩进行、
  todo 清单、轮末 tokens 统计
- **权限三键卡**（写/执行类工具真弹: 拒绝/允许/本会话总允许）
- **ask_user 选项卡**（选项/自填/跳过）
- 模型配置: 填 base_url/key/model 存成服务器自增档（key 回显恒打码）
- 中断按钮; Enter 发送 / Shift+Enter 换行

## 后端接口

见 `../backend/server.py` 头注释（REST + SSE, 全 JSON, CORS 全开, 支持
`AT_WEB_TOKEN` Bearer 或 `?token=`）。
