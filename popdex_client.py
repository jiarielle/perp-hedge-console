"""
Popdex 客户端:REST 行情 + 持仓/挂单查询 + 链上下单(web3 签名)。

所有接口与合约信息来自 Popdex 官方文档(https://popdex.xyz/docs),关键端点已于
2026-08 在主网实测验证:

  行情  GET /api/v1/public/market/tickers
  盘口  GET /api/v1/public/market/orderbook?category=Futures&symbol=X&levels=N (levels ∈ 1/5/25/50/100/200/max)
  时间  GET /api/v1/public/time
  持仓  GET /api/v1/account/{walletId}/positions
  挂单  GET /api/v1/account/{walletId}/orders
  单查  GET /api/v1/account/{walletId}/orders/client-oid/{clientOid}
  历史  GET /api/v1/account/{walletId}/history/orders
  RPC   POST /api/v1/web3/rpc   (JSON-RPC 网关,eth_sendRawTransaction 权重 10,约 120 tx/min/IP)

链: Morph Tachyon,主网 chainId 2184,测试网 34952。
下单合约(Order 预编译) 0x...1000:
    placeOrder(address account, bytes32 clientOrderId, uint16 symbolId,
               bytes32 orderParams, uint256 price, uint256 qty,
               uint256 slippage, address builder, uint256 builderFeeRate)
               external returns (bool success)
    cancelOrder(uint128 orderId)
orderParams(bytes32)逐字节布局:
    [0]=category   2=Futures
    [1]=orderType  0=Limit 1=Market 2=Plan 3=Tpsl
    [2]=side       0=Buy 1=Sell
    [3]=timeInForce 0=Default 1=GTC 2=IOC 3=FOK 4=PostOnly
    [4]=marketUnit 0=BaseToken 1=QuoteToken
    [5]=bbo        0=None 1=对手一档 2=对手五档 3=同侧一档 4=同侧五档
    [6]=isReduceOnly 0/1
    [7]=positionSide 0=None 1=Long 2=Short
    [8..31]=0(placeOrder 要求)
price/qty/slippage 均为 18 位小数定点:price=0 表示市价单;qty 单位由 marketUnit 决定;
slippage 为 1e18 比例(如 0.5% = 5e15),0 表示不限制。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("popdex")

ONE_18 = Decimal(10) ** 18

# orderParams 字节位取值(见模块 docstring)
CATEGORY_FUTURES = 2
ORDER_TYPE_LIMIT = 0
ORDER_TYPE_MARKET = 1
SIDE_BUY = 0
SIDE_SELL = 1
TIF_GTC = 1
TIF_IOC = 2
TIF_POST_ONLY = 4
MARKET_UNIT_BASE = 0
BBO_NONE = 0

ORDER_ABI = [
    {
        "type": "function",
        "name": "placeOrder",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "clientOrderId", "type": "bytes32"},
            {"name": "symbolId", "type": "uint16"},
            {"name": "orderParams", "type": "bytes32"},
            {"name": "price", "type": "uint256"},
            {"name": "qty", "type": "uint256"},
            {"name": "slippage", "type": "uint256"},
            {"name": "builder", "type": "address"},
            {"name": "builderFeeRate", "type": "uint256"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
    },
    {
        # 官方文档实测签名:cancelOrder(address, uint128, bytes32) —— 首版漏了 account/clientOrderId 被链拒
        "type": "function",
        "name": "cancelOrder",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "orderId", "type": "uint128"},
            {"name": "clientOrderId", "type": "bytes32"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
    },
    {
        # 官方提供的空操作:用于验证 Agent 交易链路 / 同步 nonce,链上无任何效果
        "type": "function",
        "name": "noop",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [],
    },
]

# Account 预编译合约(0x...1008):Agent 授权与查询
ACCOUNT_CONTRACT = "0x0000000000000000000000000000000000001008"
ACCOUNT_ABI = [
    {
        "type": "function",
        "name": "getAgentInfo",
        "stateMutability": "view",
        "inputs": [{"name": "agent", "type": "address"}],
        "outputs": [
            {"name": "exists", "type": "bool"},
            {"name": "expiresAt", "type": "uint64"},
            {"name": "isExpired", "type": "bool"},
            {"name": "delegator", "type": "address"},
            {"name": "name", "type": "bytes32"},
            {"name": "isGlobal", "type": "bool"},
            {"name": "agentType", "type": "uint8"},
            {"name": "scope", "type": "uint8"},
            {"name": "allowedRecipients", "type": "address[]"},
        ],
    },
]


def build_order_params(
    *,
    order_type: int = ORDER_TYPE_LIMIT,
    side: int = SIDE_BUY,
    time_in_force: int = TIF_POST_ONLY,
    market_unit: int = MARKET_UNIT_BASE,
    bbo: int = BBO_NONE,
    reduce_only: bool = False,
    position_side: int = 0,
) -> bytes:
    """把下单标志打包成 placeOrder 的 orderParams(bytes32)。"""
    b = bytearray(32)
    b[0] = CATEGORY_FUTURES
    b[1] = order_type
    b[2] = side
    b[3] = time_in_force
    b[4] = market_unit
    b[5] = bbo
    b[6] = 1 if reduce_only else 0
    b[7] = position_side
    return bytes(b)


def to_18_decimals(value: Decimal) -> int:
    return int((value * ONE_18).quantize(Decimal("1")))


@dataclass
class PopdexConfig:
    api_url: str
    chain_id: int
    account_address: str          # 主账户(资金所在)
    signer_private_key: str       # Agent 钱包私钥(经 approveAgent 授权)
    order_contract: str = "0x0000000000000000000000000000000000001000"
    builder_address: str = "0x0000000000000000000000000000000000000000"
    builder_fee_rate: int = 0
    dry_run: bool = True


class PopdexClient:
    def __init__(self, cfg: PopdexConfig):
        self.cfg = cfg
        self._http = httpx.AsyncClient(base_url=cfg.api_url, timeout=10)
        self._w3 = None
        self._signer = None
        self._order_contract = None
        self._client_oid_counter = itertools.count(1)
        self.symbol_cfg: Dict[str, Decimal] = {}   # tick/lot/min 规则(load_symbol_config 填充)

    # ---------------- 基础设施 ----------------

    async def connect(self) -> None:
        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(f"{self.cfg.api_url}/api/v1/web3/rpc",
                                          request_kwargs={"timeout": 15}))
        if not await asyncio.to_thread(self._w3.is_connected):
            raise RuntimeError("无法连接 Popdex RPC 网关")
        chain_id = await asyncio.to_thread(lambda: self._w3.eth.chain_id)
        if chain_id != self.cfg.chain_id:
            raise RuntimeError(f"chainId 不符:期望 {self.cfg.chain_id},实际 {chain_id}")

        if self.cfg.signer_private_key:
            from eth_account import Account

            self._signer = Account.from_key(self.cfg.signer_private_key)
            log.info("Popdex Agent 签名钱包: %s (主账户 %s)",
                     self._signer.address, self.cfg.account_address)

        checksum = Web3.to_checksum_address(self.cfg.order_contract)
        self._order_contract = self._w3.eth.contract(address=checksum, abi=ORDER_ABI)

    async def close(self) -> None:
        await self._http.aclose()

    def _require_signer(self) -> None:
        if self._signer is None:
            raise RuntimeError("未配置 POPDEX_SIGNER_PRIVATE_KEY,无法签名交易")

    # ---------------- REST:公共行情 ----------------

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = await self._http.get(path, params=params)
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("code")) != "200":
            raise RuntimeError(f"Popdex API 错误 {body.get('code')}: {body.get('msg')} ({path})")
        return body.get("data")

    async def get_server_time_ms(self) -> int:
        # /public/time 返回 data{blockNumber, blockTs, systemTs}
        data = await self._get("/api/v1/public/time")
        return int(data["systemTs"])

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """返回该 symbol 的 ticker dict(lastPrice/bid1Price/ask1Price/markPrice...)。"""
        tickers = await self._get("/api/v1/public/market/tickers")
        for t in tickers or []:
            if t.get("symbol") == symbol:
                return t
        raise RuntimeError(f"Popdex 无此交易对: {symbol}")

    async def get_futures_symbols(self) -> List[Dict[str, Any]]:
        """全部可交易的 Futures 交易对:[{symbol, symbol_id, last_price, change_pct}]。
        (用于两所交集匹配 + 币种选择器的行情展示)

        注意 tickers 返回的 category 是大写 "FUTURES",与 orderbook 查询参数
        的 "Futures" 不同,这里做大小写无关匹配。"""
        tickers = await self._get("/api/v1/public/market/tickers")
        out = []
        for t in tickers or []:
            if str(t.get("category", "")).upper() == "FUTURES" and t.get("status") == "Trading":
                out.append({
                    "symbol": t["symbol"],
                    "symbol_id": int(t["symbolId"]),
                    "last_price": str(t.get("lastPrice", "")),
                    "change_pct": str(t.get("price24hPcnt", "")),
                })
        return out

    async def resolve_symbol_id(self, symbol: str) -> int:
        """按 symbol 名自动解析 Popdex symbolId(替代手填 .env,消除出错点)。"""
        for m in await self.get_futures_symbols():
            if m["symbol"] == symbol:
                return m["symbol_id"]
        raise RuntimeError(f"Popdex 无此可交易对: {symbol}")

    async def load_symbol_config(self, symbol: str) -> Dict[str, Decimal]:
        """加载交易对精度规则(tickSize/lotSize/minQty/minNotional),下单前自动对齐。

        实测 BTCUSDT:tickSize=1(价格必须整数)、lotSize=0.0001(数量步进),
        不对齐会被链上合约直接 revert——这是实盘首单失败的根因。"""
        d = await self._get("/api/v1/config/symbol",
                            {"symbol": symbol, "category": "Futures"})
        cfg = {
            "tick_size": Decimal(str(d.get("tickSize", "1"))),
            "lot_size": Decimal(str(d.get("lotSize", "0.0001"))),
            "min_qty": Decimal(str(d.get("minQty", "0"))),
            "min_notional": Decimal(str(d.get("minNotional", "0"))),
        }
        self.symbol_cfg = cfg
        log.info("Popdex %s 精度: tick=%s lot=%s minQty=%s minNotional=%s",
                 symbol, cfg["tick_size"], cfg["lot_size"], cfg["min_qty"], cfg["min_notional"])
        return cfg

    async def get_agent_info(self, agent_address: str) -> Dict[str, Any]:
        """链上查询 Agent 授权状态(Account 合约 getAgentInfo)。"""
        from web3 import Web3

        contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(ACCOUNT_CONTRACT), abi=ACCOUNT_ABI)
        info = await asyncio.to_thread(
            lambda: contract.functions.getAgentInfo(
                Web3.to_checksum_address(agent_address)).call())
        return {
            "exists": bool(info[0]),
            "expires_at": int(info[1]),
            "is_expired": bool(info[2]),
            "delegator": info[3],
            "name": bytes(info[4]).rstrip(b"\0").decode("utf-8", "replace"),
            "is_global": bool(info[5]),
        }

    async def get_bbo(self, symbol: str) -> Tuple[Decimal, Decimal]:
        """返回 (best_bid, best_ask)。REST 轮询,盘口变化不快时够用;
        需要更低延迟可换 WS books1 频道(见 README)。"""
        data = await self._get("/api/v1/public/market/orderbook",
                               {"category": "Futures", "symbol": symbol, "levels": "1"})
        asks = data.get("asks") or []
        bids = data.get("bids") or []
        if not asks or not bids:
            raise RuntimeError(f"Popdex 盘口为空: {symbol}")
        # 每档为 [price, size] 字符串数组,价格降序 asks[0] 最优
        return Decimal(str(bids[0][0])), Decimal(str(asks[0][0]))

    # ---------------- REST:私有查询(路径实测验证) ----------------

    async def get_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.cfg.account_address:
            return []  # 演示模式:未配置账户地址,不发起私有查询
        wallet = self.cfg.account_address.lower()
        params: Dict[str, Any] = {"category": "Futures"}
        if symbol:
            params["symbol"] = symbol
        return (await self._get(f"/api/v1/account/{wallet}/positions", params)) or []

    async def get_position_size(self, symbol: str) -> Decimal:
        """该 symbol 的净持仓(base 币,正=多、负=空)。

        实测持仓字段:holdQty(数量,恒正)+ positionSide("Long"/"Short"),
        二者相乘才是带符号净持仓——首版误读 position/size 字段导致恒为 0,
        成交检测失效,这是实盘最关键的 bug。"""
        total = Decimal(0)
        for p in await self.get_positions(symbol):
            raw = p.get("holdQty", p.get("position", p.get("size", p.get("qty", 0))))
            val = abs(Decimal(str(raw or 0)))
            side = str(p.get("positionSide", p.get("side", ""))).lower()
            if side in ("short", "sell", "2"):
                val = -val
            total += val
        return total

    async def get_pending_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        wallet = self.cfg.account_address.lower()
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return (await self._get(f"/api/v1/account/{wallet}/orders", params)) or []

    async def get_order_by_client_oid(self, client_oid: str) -> Optional[Dict[str, Any]]:
        """clientOid 用 bytes32 的 hex(0x 开头 64 位)查询。"""
        wallet = self.cfg.account_address.lower()
        data = await self._get(f"/api/v1/account/{wallet}/orders/client-oid/{client_oid}")
        return data

    async def get_equity(self) -> Dict[str, str]:
        """账户权益概况(overview,实测字段):equity/available/初始保证金占用。"""
        if not self.cfg.account_address:
            return {}
        wallet = self.cfg.account_address.lower()
        d = await self._get(f"/api/v1/account/{wallet}/overview")
        return {
            "equity": str(d.get("accountEquity", "")),
            "available": str(d.get("availableMargin", "")),
            "imr": str(d.get("imr", d.get("initialMargin", ""))),
        }

    async def get_leverage(self, symbol: str) -> str:
        """当前交易对配置的杠杆倍数(只读;改杠杆去 app.popdex.xyz 网页上设置)。"""
        if not self.cfg.account_address:
            return ""
        wallet = self.cfg.account_address.lower()
        d = await self._get(f"/api/v1/account/{wallet}/config/futures-leverage",
                            {"symbol": symbol})
        # 字段名做兼容
        for k in ("leverage", "leverageRate", "value"):
            if isinstance(d, dict) and d.get(k):
                return str(d[k])
        if isinstance(d, list) and d:
            return str(d[0].get("leverage", ""))
        return ""

    async def send_noop(self) -> Optional[str]:
        """发送一笔 noop 空交易:验证 Agent 签名/nonce/RPC 全链路,无任何业务效果。"""
        call = self._order_contract.functions.noop()
        return await self._send_contract_tx(call, "noop测试")

    # ---------------- 链上下单 ----------------

    def _next_client_oid(self) -> bytes:
        """bytes32 clientOrderId:8 字节毫秒时间戳 + 8 字节计数器,右侧补零。"""
        ms = int(time.time() * 1000)
        return ms.to_bytes(8, "big") + next(self._client_oid_counter).to_bytes(8, "big") + bytes(16)

    async def _send_contract_tx(self, contract_call, label: str) -> Optional[str]:
        """签名并发送 EIP-1559 交易,返回 tx hash。DRY_RUN 只打印,不需要私钥。

        Agent 账户没有标准的 EVM nonce(RPC 会拒绝 getTransactionCount),
        Popdex 对 Agent 使用「时间戳 nonce」:当前毫秒时间戳,允许并行发单。
        官方规则:窗口 (now-1h, now+1d),大于最近100个最小值,不可复用。
        """
        from web3 import Web3

        if self.cfg.dry_run:
            calldata = contract_call._encode_transaction_data()
            log.info("[DRY_RUN] %s 已构造未发送: to=%s calldata=%s",
                     label, self.cfg.order_contract, calldata[:138])
            return None

        self._require_signer()
        w3 = self._w3
        is_agent = self._signer.address.lower() != (self.cfg.account_address or "").lower()

        def build_and_send():
            signer = self._signer
            if is_agent:
                nonce = int(time.time() * 1000)          # Timestamp Nonce
            else:
                nonce = w3.eth.get_transaction_count(signer.address, "pending")
            gas = int(contract_call.estimate_gas({"from": signer.address}) * 1.2)
            block = w3.eth.get_block("latest")
            base_fee = block.get("baseFeePerGas") or Web3.to_wei(1, "gwei")
            tx = contract_call.build_transaction({
                "from": signer.address,
                "chainId": self.cfg.chain_id,
                "nonce": nonce,
                "gas": gas,
                "type": 2,
                "maxFeePerGas": int(base_fee * 2),
                "maxPriorityFeePerGas": 0,   # Tachyon 上 priority fee 为 0
                "value": 0,
            })
            signed = signer.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status != 1:
                raise RuntimeError(f"{label} 交易上链失败: {tx_hash.hex()}")
            return tx_hash.hex()

        tx_hash = await asyncio.to_thread(build_and_send)
        log.info("%s 交易已上链: %s", label, tx_hash)
        return tx_hash

    async def place_order(
        self,
        *,
        symbol_id: int,
        side: str,                      # 'buy' | 'sell'
        qty: Decimal,                   # base 币数量
        order_type: int = ORDER_TYPE_LIMIT,
        time_in_force: int = TIF_POST_ONLY,
        price: Optional[Decimal] = None,    # None/0 = 市价
        slippage_ratio: Optional[Decimal] = None,  # 如 Decimal('0.005') = 0.5%
        reduce_only: bool = False,
    ) -> str:
        """下单。返回 clientOid hex(用于 REST 查单/对账)。价格/数量自动按
        tickSize/lotSize 对齐,并校验 minQty/minNotional(不对齐链上会 revert)。"""
        params = build_order_params(
            order_type=order_type,
            side=SIDE_BUY if side == "buy" else SIDE_SELL,
            time_in_force=time_in_force,
            reduce_only=reduce_only,
        )
        if self.symbol_cfg:
            tick = self.symbol_cfg["tick_size"]
            lot = self.symbol_cfg["lot_size"]
            if tick > 0 and price:
                # 买单向下取整(更保守),卖单向上取整(更保守),都更远离盘口
                rounding = "ROUND_DOWN" if side == "buy" else "ROUND_UP"
                price = (price / tick).to_integral_value(rounding=rounding) * tick
            if lot > 0:
                qty = (qty / lot).to_integral_value(rounding="ROUND_DOWN") * lot
            min_qty = self.symbol_cfg.get("min_qty", Decimal(0))
            min_notional = self.symbol_cfg.get("min_notional", Decimal(0))
            if qty < min_qty or (price and qty * price < min_notional):
                raise RuntimeError(
                    f"数量 {qty} 低于最小限制(minQty={min_qty}, minNotional={min_notional})")
        client_oid = self._next_client_oid()
        price_scaled = to_18_decimals(price) if price else 0
        qty_scaled = to_18_decimals(qty)
        slip_scaled = to_18_decimals(slippage_ratio) if slippage_ratio else 0

        call = self._order_contract.functions.placeOrder(
            self._w3.to_checksum_address(self.cfg.account_address),
            client_oid,
            symbol_id,
            params,
            price_scaled,
            qty_scaled,
            slip_scaled,
            self._w3.to_checksum_address(self.cfg.builder_address),
            self.cfg.builder_fee_rate,
        )
        log.info(
            "Popdex 下单: %s %s qty=%s price=%s tif=%s reduce_only=%s symbolId=%s clientOid=%s",
            "LIMIT" if order_type == ORDER_TYPE_LIMIT else "MARKET",
            side.upper(), qty, price or "(market)", time_in_force,
            reduce_only, symbol_id, client_oid.hex(),
        )
        await self._send_contract_tx(call, "placeOrder")
        return client_oid.hex()

    async def cancel_order(self, order_id: int, client_oid_hex: Optional[str] = None) -> Optional[str]:
        """链上撤单。client_oid_hex 传下单时返回的 bytes32 hex(用于链上对账),没有就传零。"""
        client_oid = (bytes.fromhex(client_oid_hex[2:]) if client_oid_hex and client_oid_hex.startswith("0x")
                      else bytes.fromhex(client_oid_hex) if client_oid_hex
                      else bytes(32))
        client_oid = (client_oid + bytes(32))[:32]
        call = self._order_contract.functions.cancelOrder(
            self._w3.to_checksum_address(self.cfg.account_address),
            int(order_id),
            client_oid,
        )
        log.info("Popdex 撤单: orderId=%s", order_id)
        return await self._send_contract_tx(call, "cancelOrder")
