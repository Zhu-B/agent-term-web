# 免费部署攻略（国内可达优先）

> **2026-10-16 更新: 决定自租服务器** —— 见文末「自租服务器（推荐路线）」,
> 免费方案留在下面备查。

> 免费档先说结论: **个人自用 → 自己电脑/WSL 跑 + Cloudflare Tunnel**（免费、数据不丢、
> key 不出门、不休眠）; **要公网演示 → 后端丢 Zeabur/Koyeb + 同源托管前端**。
> 最省心形态永远是「只部署后端」—— `python server.py` 默认把 frontend/ 一起挂
> 出去, 一个域名全搞定, 不用分开托管。

## 方案一（推荐）: 自己机器 + Cloudflare Tunnel

1. 有一个域名托管在 Cloudflare（域名本身要花钱, 没域名可用下面的方案二）。
2. `cloudflared tunnel` 免费把本机 8787 映射成 `https://xx.你的域名`。
3. 数据全在本地（会话/记忆/key 都不丢）; 机器开着才在线。

国内替代: 花生壳/natapp 免费档（带宽小、需实名）; Tailscale（只有自己设备能访问,
**最安全**, 不算公网）。

## 方案二: 免费托管平台（公网可达, 无需自己机器）

| 平台 | 免费额度 | 国内直连 | 备注 |
|---|---|---|---|
| **Zeabur** | 免费档有限额 | ✅ 较好 | 对国内最友好; 建议同源托管前端 |
| **Koyeb** | 一个常驻小实例 | ⚠️ 一般 |不休眠; 部署选 "Build from GitHub / python", start `python server.py` |
| **Render** | 免费 web service | ⚠️ 一般 | 15 分钟无访问休眠, 冷启动 ~30s |
| **Hugging Face Spaces** | 免费 CPU | ❌ 时好时坏 | 选 Docker 方式, ENTRYPOINT `python server.py` |
| 腾讯云轻量/阿里云 ECS | 新用户免费试用 1–3 个月 | ✅ 最快 | 需实名; 到期要钱 |
| Gitee Pages（仅前端） | 静态免费 | ✅ 快 | 需实名+审核; 只能放前端, API 另找家 |

要点:

- 启动命令一律 `python server.py`（零依赖, 不用 requirements.txt）; 平台会注入
  `PORT`, 服务自动读。
- **环境变量 `AT_WEB_TOKEN` 必设**, 前端「⚙ 连接」填同一个。
- **免费档没有持久盘**: 重新部署后 models.json/会话/记忆全部清空, 模型要重配;
  休眠唤醒一般不丢（只重启进程的会）。
- agent 回合一跑几分钟 + SSE 长连接 —— 函数计算类 serverless（腾讯 SCF/阿里 FC/
  Vercel functions）**不适合**, 要常驻进程的平台。
- bash 工具跑在服务器上: 对外开放前在 `~/.agent-term/settings.json` perm 节加
  deny 规则（如 `"deny": ["bash:rm *"]`）收紧。

## 演示模式

`AT_DEMO=1 python server.py` —— 没有任何 key 也能演示完整的
「工具调用→结果→结论」回路（剧本 provider）, 适合先部署个壳给人看。

## 检查清单

```bash
curl http://你的地址/api/health        # {"ok": true, ...}
# 浏览器开首页 → ⚙连接(如跨源) → 新会话 → 发一句话 → 看见工具行与统计行
```

## 自租服务器（推荐路线, 2026-10-16 行情）

agent-term web 这点负载（LLM 调用都发生在模型厂商侧, 服务器只跑壳+工具）,
最便宜的 **2核2G 轻量应用服务器** 就够, 别买贵了。

**选型口径: 盯"续费同价", 别只看首购价。**

| 选择 | 行情 | 适合 |
|---|---|---|
| **腾讯云轻量 99元/年 新老同享**（2C2G 档） | 续费同价 | **默认推荐**: 长期用总价最低最省心 |
| 腾讯云/阿里云 新用户秒杀 28–38元/年（2C2G） | 限量抢, 续费恢复原价 | 能抢到就先占一年, 到期再换/再谈 |
| 京东云轻量 392元/3年（≈130元/年, 2C2G） | 一次锁三年 | 不想每年折腾的 |
| 阿里云轻量 68元/年（2C2G, 200M峰值带宽） | 抢不到 38 档的次选 | 对带宽峰值有执念的 |

买的时候: 选**轻量应用服务器**（不是 CVM）, 地域挑**离你近的**（华北/华东）,
镜像 **Ubuntu 24.04**, 需实名; **IP 直连端口不需要备案**（备案只发生在
绑域名走 80/443 时）。小厂（雨云之类）更便宜但跑路/波动风险自己掂量, 长期
自用不建议。

### 买完三步上线

```bash
# 1. 把 agent-term-web 传上去(本机 WSL 里):
scp -r ~/agent-term-web root@服务器IP:~

# 2. 服务器上装成系统服务(开机自启+崩了自动拉起), 令牌自己起一个:
ssh root@服务器IP
cd ~/agent-term-web && sudo sh setup-server.sh 你的访问令牌

# 3. 云控制台防火墙放行 8787/TCP —— 浏览器开 http://服务器IP:8787,
#    ⚙连接里填同一个令牌(同源部署不用填地址)
```

安全三件: `AT_WEB_TOKEN` 必设（脚本已带）; 服务器上给 agent 的模型 key 建议
**单独申请一把小额的**（别把主力 key 放公网机器）; ssh 改密钥登录。
模型页存的 key 落在服务器 `~/.agent-term/models.json`——轻量服务器盘是持久的,
**重启不丢, 重装系统才丢**（和免费托管平台的根本区别）。

## E2 版本系统上线(切版本要能自动重启)

```sh
# ① 快照目录(atweb 用户可写, 树外, 不进 git)
sudo mkdir -p /opt/agent-term-web/versions
sudo chown atweb:atweb /opt/agent-term-web/versions

# ② 让服务进程能自己重启(窄 sudoers, 只放行这一条命令)
echo 'atweb ALL=(root) NOPASSWD: /usr/bin/systemctl restart atweb' \
  | sudo tee /etc/sudoers.d/atweb-restart
sudo chmod 440 /etc/sudoers.d/atweb-restart

# ③ unit 加 env(改 /etc/systemd/system/atweb.service 的 [Service] 段)
#   Environment=AT_RESTART_CMD=sudo -n systemctl restart atweb
sudo systemctl daemon-reload && sudo systemctl restart atweb

# ④ 建纪元基线 v0001(超管令牌在页面上点也行)
curl -X POST http://127.0.0.1:8787/api/versions \
  -H "Authorization: Bearer <令牌>" -d '{"label":"E2 基线","note":"上线首版"}'
```

不配 ②③ 也能用: 切版本会落盘+写 RESTART.marker, 但不自动重启,
手动 `sudo systemctl restart atweb` 后生效。备份的 pg_backup.sh 不受影响。
