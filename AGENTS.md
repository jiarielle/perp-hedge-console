# AGENTS.md — AI Agent 部署运行手册

> 本文件面向 AI 编程代理(Claude Code / ZCode / Cursor 等)。你的任务是把这个交易机器人在**用户本机**部署起来并跑通网页控制台。请严格按本文档执行,不要自行改动交易逻辑代码。

## 这个项目是什么

Popdex ↔ Lighter 双所永续套利/对冲/刷量机器人,含本地 Web 控制台(深色/浅色双主题)。

- 技术栈:Python 3.9+(纯标准库+5个pip依赖)、aiohttp、web3.py、lighter-sdk
- 架构:`web_console.py`(HTTP服务,127.0.0.1:8788)→ `hedge_bot.py`(策略循环)→ `popdex_client.py`(链上下单)+ `lighter_client.py`(官方SDK)
- 三种策略模式(运行页可切换):`arb` 套利(价差达标才出手)/ `hedge` 跨所对冲刷量 / `volume` Popdex单所taker秒开秒平

## 部署五步

```bash
# 1. 进入项目目录
cd popdex-lighter-hedge

# 2. 虚拟环境
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. 依赖(国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple)
pip install -r requirements.txt

# 4. 配置模板(可以留空,用户稍后在网页向导里填)
cp .env.example .env

# 5. 启动控制台
python web_console.py
```

成功标志:终端打印 `对冲机器人控制台: http://127.0.0.1:8788`。
然后帮用户打开浏览器到 `http://127.0.0.1:8788`(macOS: `open http://127.0.0.1:8788`)。

## 首次设置(引导用户在网页完成,你不要代填密钥)

控制台「设置」页是四步向导,**让用户本人操作**,你在旁边解释:

1. **Lighter 凭据**:用户在 app.lighter.xyz → 设置/API 生成 API Key,把 Account Index / Key Index / 私钥填入,点「验证并保存」。注意:Lighter 账户档位必须是 **Standard(免费)**,Premium 档每笔 taker 收 2.8bps
2. **Popdex Agent 授权**:点「生成新 Agent 钱包」→ 用户粘贴 Popdex 主钱包私钥(仅内存签名一次授权交易,不落盘)→ 授权上链。已授权过的用户用折叠项直接填
3. 可选:点「发送测试交易」(noop 空交易)验证下单链路
4. 回「运行监控」页:顶部选币种(下拉只显示两所都有的交易对)→ 选模式 → 点「▶ 启动」

**默认实盘模式**。想先模拟就把 `.env` 里 `DRY_RUN=true` 再重启控制台。

## 交易模式说明(用户问起时)

| 模式 | 行为 | 实测成本(BTC,VIP0) |
|---|---|---|
| 💎 套利(推荐默认) | 两所价差 ≥7bps 才出手,每轮锁定正收益,平时等待 | 每轮 **+7bps** |
| 🔀 对冲 | Popdex挂maker单,成交秒级在Lighter对冲,双边刷量 | 每轮约 -6.6bps |
| 🔄 刷量 | Popdex单所市价秒开秒平(taker双腿),最快刷量 | 每轮约 -8.5bps ≈ **$180/时**($300/轮,5秒间隔) |

关键参数在「设置→策略参数」,改完自动体检联动关系(如熔断线须≥3轮),有黄色警告条可一键采纳。

## 已内置的坑(agent 不要重复排查)

以下问题代码已处理,直接引用即可:

1. **Agent 交易 nonce**:Popdex 对 Agent 账户拒绝 `getTransactionCount`,已改用官方"时间戳nonce"(毫秒时间戳)
2. **价格/数量精度**:BTC tickSize=1(价格必须整数)、lotSize=0.0001,`load_symbol_config()` 自动拉取并对齐,不对齐链上直接 revert
3. **cancelOrder 签名**:正确签名是 `cancelOrder(address,uint128,bytes32)` 三参数
4. **Lighter 持仓符号**:返回 `sign(±1)` + `position(绝对值)`,必须相乘
5. **跨币种熔断线**:熔断线是 base 币单位,切币种时自动按 3 轮缓冲重算
6. **Popdex tickers 的 category 是大写 "FUTURES"**(与 orderbook 参数 "Futures" 不同)

## 故障排查表

| 症状 | 处置 |
|---|---|
| `pip install` 编译失败 | 确认 Python ≥3.9;macOS 需 Xcode Command Line Tools(`xcode-select --install`) |
| 控制台启动后 `/api/status` 一直 `initialized:false` | 首次 build 要连两所(约10秒),等一会再点启动;仍失败看终端日志是否 RPC 超时 |
| `Agent accounts do not support getTransactionCount` | 旧版本问题,当前代码已用时间戳nonce;确认用的是本仓库最新版 |
| placeOrder 上链成功但状态显示被拒 | 大概率 PostOnly 挂价穿越盘口(价差1个tick时常见),属正常,机器人自动重挂 |
| Lighter 凭据验证失败 | 检查账户档位是否 Standard;API key 是否过期;账户号是否正确 |
| 端口占用 | `python web_console.py --port 9000` |
| REST 403 | 短时限流,轮询间隔别低于0.5秒;IP 必须在两所允许地区(中国大陆/美国等不可用) |

## 安全禁区(必须遵守)

1. **绝不**把 `.env`、`agent.env` 提交进 git 或打包(已在 .gitignore)
2. **绝不**在日志/终端打印任何私钥明文
3. **绝不**把控制台端口暴露到公网(仅 127.0.0.1)
4. **绝不**代替用户生成/粘贴密钥;主钱包私钥只在用户本人操作授权时瞬时使用
5. 用户停止机器人时它会自动撤单+双侧平仓——这是设计行为,不要"优化"掉
6. 修改交易逻辑前必须向用户确认;本项目自带风控(熔断线/连续错误停机/停机全平),不要绕过

## 目录结构

```
popdex-lighter-hedge/
├── AGENTS.md            # 本文件
├── README.md            # 人类小白教程
├── requirements.txt     # pip 依赖
├── .env.example         # 配置模板(可留空,网页向导会写)
├── web_console.py       # 入口:本地Web控制台
├── hedge_bot.py         # 策略循环(三模式+风控)
├── popdex_client.py     # Popdex:REST行情+链上下单
├── lighter_client.py    # Lighter:官方SDK
├── setup_store.py       # .env 安全读写(600权限/脱敏)
├── authorize_agent.py   # Popdex Agent授权CLI(网页复用)
└── static/              # 前端(运行页/设置页/样式/组件)
```

MIT License. 交易有风险,责任由使用者自负。
