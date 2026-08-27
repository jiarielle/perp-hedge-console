# Popdex ⇄ Lighter 对冲交易机器人

在 [Popdex](https://app.popdex.xyz) 和 [Lighter](https://app.lighter.xyz) 两个永续交易所之间做**套利 / 对冲 / 刷量**的自动化机器人,自带本地网页控制台(黑白双主题、手机适配)。

> ⚠️ **风险声明**:本软件仅供学习研究,不构成投资建议。合约交易有爆仓风险;刷量/对冲模式每轮支付点差与手续费(实测约 6~9bps/轮);两所均限制中国大陆、美国等地区 IP,请自行确认合规。使用本软件产生的一切盈亏由使用者自行承担。

## 三种模式(网页一键切换)

| 模式 | 干什么 | 适合 |
|---|---|---|
| 💎 **套利**(默认) | 两所价差 ≥ 7bps 才出手:一边 maker 一边对冲,每轮锁正收益,平时等待 | 想赚钱 |
| 🔀 **对冲** | Popdex 挂单成交后秒级在 Lighter 对冲,双边同时刷交易量 | 两边都要积分 |
| 🔄 **刷量** | Popdex 单所市价秒开秒平,速度最快 | 纯刷 Popdex 量(烧钱,见成本表) |

**实测成本参考**(BTC,VIP0 费率,Lighter 免费档):套利每轮 **+7bps**;对冲 -6.6bps;刷量 -8.5bps ≈ $0.26/轮($300/轮、5秒间隔时约 $180/小时)。

## 快速开始(3 条命令)

需要:Python 3.9+、能访问两所的网络的电脑。

```bash
git clone https://github.com/<用户名>/popdex-lighter-hedge.git
cd popdex-lighter-hedge
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python web_console.py
```

浏览器打开 **http://127.0.0.1:8788**(Windows 激活 venv 用 `.venv\Scripts\activate`)。

> 💡 不会用终端?把仓库链接发给你的 AI 编程助手(Claude Code / ZCode / Cursor),对它说:**"按仓库里的 AGENTS.md 部署运行"**,它会替你完成全部步骤。

## 首次设置(网页向导,约 5 分钟)

打开「设置」页,三步:

1. **Lighter**:在 app.lighter.xyz → 设置/API 生成 API Key(账户档位选 **Standard 免费**),把 Account Index、Key Index、私钥填入 → 验证并保存
2. **Popdex**:点「生成新 Agent 钱包」→ 粘贴你 Popdex 主钱包私钥完成链上授权(私钥只在内存里签一次授权交易,立即丢弃,不写任何文件)。Agent 只有交易权、不能提现、30天有效、可撤销
3. 回「运行监控」页:选币种(下拉只显示两所都有的)→ 选模式 → **▶ 启动**

**默认实盘**。想先空跑:`.env` 里改 `DRY_RUN=true` 重启。

## 控制台功能

- **运行监控页**:权益/价差走势图/净敞口/挂单/成交记录/日志,启动·停止·熔断,币种切换(Popdex 同款下拉,带实时价格)
- **设置页**:凭据向导、策略参数(带联动体检+一键档位+3个自定义槽位)、黑白主题切换
- **停止即全平**:任何模式点停止都会撤掉全部挂单并把两侧仓位市价平到零(带重试与复核)
- 参数保存即持久化,重启不丢;密钥只存本地 `.env`(600 权限),页面永远只显示脱敏

## 常见问题

**Q: Popdex 不是没有 API 吗?**
没有传统 API key,但下单走官方链上合约(Order 预编译 `0x...1000`),凭 Agent 钱包授权(只交易、不提现)。本项目已处理好时间戳 nonce、价格步进对齐、三参数撤单签名等全部链上细节。

**Q: 为什么我一直在亏?**
如果你开着对冲/刷量模式,每轮支付两所点差是结构性成本(费率降到0也省不掉);想每轮赚点请切**套利模式**。另外确认:①Lighter 账户是 Standard(免费),Premium 每笔多付 2.8bps;② Popdex VIP 等级越高 maker 费越低(VIP3 = 0)。

**Q: 资金费算谁的?**
两边持仓互相对冲,资金费一付一收,净额很小;长时间持仓时可关注两所费率差。

**Q: 支持哪些币?**
币种下拉实时拉取两所交集(BTC/ETH/SOL/DOGE/HYPE/SUI/SNDK 等约 18 个),单边没有的不会出现。

## 开发者

```
popdex-lighter-hedge/
├── web_console.py    # 入口:aiohttp 本地控制台(127.0.0.1:8788)
├── hedge_bot.py      # 三模式策略循环 + 风控(熔断线/连续错误停机/停机全平)
├── popdex_client.py  # Popdex REST 行情 + 链上 placeOrder/cancelOrder(web3 签名)
├── lighter_client.py # Lighter 官方 SDK(lighter-sdk)
├── setup_store.py    # .env 安全读写
├── authorize_agent.py# Agent 授权 CLI
└── static/           # 前端(运行页/设置页/主题/组件)
```

AI agent 部署指引见 `AGENTS.md`。MIT License。
