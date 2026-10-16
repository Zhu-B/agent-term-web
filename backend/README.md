# agent-term web · 后端

agent-term 的**无头核心**（`core/` 下的 20 个模块快照, 全部纯标准库、零 tkinter/零
win32, Linux 直接跑）外面的一层 HTTP/SSE 壳。**零 pip 依赖** —— 任何能跑 Python
3.9+ 的机器 `python server.py` 直接起。

> 快照派生自 agent-term 主仓工作区（2026-10-16, 含 M32 识图委派/M33）, 与主仓
> 后续各自演化; `kb/`（问题库）与 `skills/`（预置技能）随核心一起带走。

## 本地跑

```bash
cd backend
python server.py                 # 默认 8787, 同时托管 ../frontend(同源)
# 浏览器开 http://127.0.0.1:8787
```

自检（不联网, 剧本 provider 全链路: SSE/权限卡/选项卡/中断/鉴权/掩码）:

```bash
python server.py --check
```

## 环境变量 / 参数

| 变量/参数 | 说明 |
|---|---|
| `PORT` / `--port` | 监听端口（默认 8787; 免费托管平台一般会给 PORT） |
| `AT_WEB_TOKEN` | **公网部署必设**的访问令牌（前端「⚙ 连接」里填同一个） |
| `AT_DEMO=1` | 演示模式: 没 key 也能跑（剧本 provider 演示工具调用回路） |
| `AT_WEB_STATIC` / `--static` | 前端目录（默认 `../frontend`, 存在才挂） |
| `AT_PROVIDERS` 等 | 核心原有重定向口子照旧可用（keys/models/记忆/账本…） |

## 数据落在哪

- `backend/run/`（会话与 trace, 相对本目录）
- `~/.agent-term/`（models.json 自增档 / 记忆 / 账本 / 定时）

**免费托管平台普遍无持久盘** —— 重新部署/休眠重启后这两处会清空, 模型配置要重填
（会话历史本版不回放）。要持久就自己机器跑（见 DEPLOY.md 的隧道方案）。

## 安全注意

- **公网必设 `AT_WEB_TOKEN`**, 否则任何拿到地址的人都能花你 models.json 里的 key。
- key 只落服务器文件, 接口回显永远打码; 但 agent 的 bash 工具跑在服务器上 ——
  对外开放时建议在 `~/.agent-term/settings.json` 的 perm 节加 deny 规则收紧。

## 排障

- **curl 探针空回 / 测试脚本连不上本机**: 机器配了系统/环境代理（`http_proxy`
  等）会把 127.0.0.1 也递给代理 —— curl 加 `--noproxy "*"`, Python 用
  `ProxyHandler({})` 的 opener（内置 selfcheck 已这么干）。浏览器访问 localhost
  默认绕代理, 网页端不受影响。
- 端口被占: `--port` 换一个。
