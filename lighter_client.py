"""
Lighter 客户端:官方 lighter-sdk 封装(行情 + IOC 对冲下单 + 持仓)。

依赖 PyPI `lighter-sdk`(import 名为 lighter)。连接方式与参数编码参考
your-quantguy/perp-dex-tools 的 exchanges/lighter.py,在其基础上精简:
  - SignerClient 负责签名下单(API key 模式,账户级,非钱包私钥)
  - ApiClient + OrderApi 负责 REST 行情/订单查询
  - 数量/价格按 market 的 supported_size_decimals / supported_price_decimals 放大为整数

对冲单用 LIMIT + IMMEDIATE_OR_CANCEL(IOC):价格按盘口加滑点保护编码,
未成交部分立即作废,不会留单,适合单腿对冲。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple

log = logging.getLogger("lighter")

# lighter-sdk 常量(SignerClient 类属性,实测 1.0.4 存在)
ORDER_TYPE_LIMIT = 0
TIF_IMMEDIATE_OR_CANCEL = 1


async def verify_credentials(base_url: str, account_index: int, api_key_index: int,
                             api_private_key: str) -> Dict[str, str]:
    """设置向导用:用表单里的凭据临时连接 Lighter 并实测,成功返回账户概况。"""
    from lighter import ApiClient, Configuration, SignerClient
    import lighter as _lighter

    signer = SignerClient(
        url=base_url,
        account_index=int(account_index),
        api_private_keys={int(api_key_index): api_private_key},
    )
    err = signer.check_client()
    if err is not None:
        raise RuntimeError(f"Lighter 签名客户端校验失败: {err}")

    async with ApiClient(configuration=Configuration(host=base_url)) as api:
        account_api = _lighter.AccountApi(api)
        data = await account_api.account(by="index", value=str(int(account_index)))
        if not data or not data.accounts:
            raise RuntimeError(f"查不到 Lighter 账户 index={account_index}")
        acct = data.accounts[0]
        equity = getattr(acct, "collateral", None)
        return {
            "account_index": str(account_index),
            "equity": str(equity) if equity is not None else "未知",
        }


@dataclass
class LighterConfig:
    base_url: str
    account_index: int
    api_key_index: int
    api_private_key: str
    dry_run: bool = True


class LighterClient:
    def __init__(self, cfg: LighterConfig):
        self.cfg = cfg
        self._signer = None
        self._api = None
        self._order_api = None
        self._market: Optional[object] = None
        self.market_index: Optional[int] = None
        self._size_multiplier: Optional[int] = None
        self._price_multiplier: Optional[int] = None
        self._client_order_index = itertools.count(int(time.time()) % 1_000_000)

    async def connect(self, symbol: str) -> None:
        import lighter
        from lighter import ApiClient, Configuration, SignerClient

        if self.cfg.api_private_key:
            self._signer = SignerClient(
                url=self.cfg.base_url,
                account_index=self.cfg.account_index,
                api_private_keys={self.cfg.api_key_index: self.cfg.api_private_key},
            )
            err = self._signer.check_client()
            if err is not None:
                raise RuntimeError(f"Lighter SignerClient 校验失败: {err}")

        self._api = ApiClient(configuration=Configuration(host=self.cfg.base_url))
        self._lighter = lighter
        self._order_api = lighter.OrderApi(self._api)

        # 用 symbol 反查 market 配置(market_index + 精度)
        books = await self._order_api.order_books()
        for m in books.order_books:
            if m.symbol == symbol:
                self._market = m
                self.market_index = m.market_id
                self._size_multiplier = 10 ** m.supported_size_decimals
                self._price_multiplier = 10 ** m.supported_price_decimals
                break
        else:
            raise RuntimeError(f"Lighter 无此交易对: {symbol}")

        d = await self._order_api.order_book_details(market_id=self.market_index)
        det = d.order_book_details[0]
        log.info("Lighter market %s: id=%s size_dec=%s price_dec=%s maker_fee=%s taker_fee=%s",
                 symbol, self.market_index, self._market.supported_size_decimals,
                 self._market.supported_price_decimals,
                 getattr(det, "maker_fee", "?"), getattr(det, "taker_fee", "?"))

    async def close(self) -> None:
        if self._api:
            await self._api.close()

    # ---------------- 行情 ----------------

    async def get_bbo(self) -> Tuple[Decimal, Decimal]:
        """返回 (best_bid, best_ask)。order_book_orders(limit=1) 即两档各一条。"""
        ob = await self._order_api.order_book_orders(market_id=self.market_index, limit=1)
        if not ob.asks or not ob.bids:
            raise RuntimeError("Lighter 盘口为空")
        return Decimal(str(ob.bids[0].price)), Decimal(str(ob.asks[0].price))

    # ---------------- 持仓 ----------------

    async def get_position_size(self) -> Decimal:
        """净持仓(base 币,带方向:正=多、负=空)。

        Lighter 返回两个字段:sign(1/-1) + position(绝对数量,恒为正),
        实测空头是 sign=-1、position=0.0008,必须相乘才是带符号净持仓。
        演示模式(未配置账户)直接返回 0。"""
        if not self.cfg.api_private_key and not self.cfg.account_index:
            return Decimal(0)
        account_api = self._lighter.AccountApi(self._api)
        data = await account_api.account(by="index", value=str(self.cfg.account_index))
        if not data or not data.accounts:
            return Decimal(0)
        for pos in data.accounts[0].positions:
            if pos.market_id == self.market_index:
                sign = getattr(pos, "sign", 1)
                return Decimal(str(sign)) * Decimal(str(pos.position))
        return Decimal(0)

    async def get_equity(self) -> Dict[str, str]:
        """账户权益概况:collateral(保证金总额)/ available(可用)。未配置返回空。"""
        if not self.cfg.api_private_key and not self.cfg.account_index:
            return {}
        account_api = self._lighter.AccountApi(self._api)
        data = await account_api.account(by="index", value=str(self.cfg.account_index))
        if not data or not data.accounts:
            return {}
        acct = data.accounts[0]
        return {
            "equity": str(getattr(acct, "collateral", "") or ""),
            "available": str(getattr(acct, "available_balance", "") or ""),
        }

    # ---------------- 下单 ----------------

    async def hedge_ioc(
        self,
        side: str,                  # 'buy' | 'sell'(对冲方向,与成交腿相反)
        qty: Decimal,
        bbo: Tuple[Decimal, Decimal],
        max_slippage_ratio: Decimal,
        reduce_only: bool = False,
    ) -> None:
        """IOC 限价对冲单:价格=对侧一档再放宽 max_slippage_ratio,吃不满就作废。"""
        best_bid, best_ask = bbo
        if side == "buy":
            price = best_ask * (1 + max_slippage_ratio)
        else:
            price = best_bid * (1 - max_slippage_ratio)

        base_amount = int((qty * self._size_multiplier).quantize(Decimal("1")))
        price_scaled = int((price * self._price_multiplier).quantize(Decimal("1")))
        client_order_index = next(self._client_order_index)

        log.info("Lighter 对冲: %s IOC qty=%s 限价=%s (盘口 %s/%s)",
                 side.upper(), qty, price, best_bid, best_ask)

        if self.cfg.dry_run:
            log.info("[DRY_RUN] Lighter create_order 参数: market_index=%s coi=%s "
                     "base_amount=%s price=%s is_ask=%s",
                     self.market_index, client_order_index, base_amount,
                     price_scaled, side == "sell")
            return

        if self._signer is None:
            raise RuntimeError("未配置 LIGHTER_API_PRIVATE_KEY,无法下单")

        tx, resp, err = await self._signer.create_order(
            market_index=self.market_index,
            client_order_index=client_order_index,
            base_amount=base_amount,
            price=price_scaled,
            is_ask=(side == "sell"),
            order_type=ORDER_TYPE_LIMIT,
            time_in_force=TIF_IMMEDIATE_OR_CANCEL,
            reduce_only=reduce_only,
            trigger_price=0,
        )
        if err is not None:
            raise RuntimeError(f"Lighter 下单失败: {err}")
        log.info("Lighter 对冲单已提交: tx=%s", getattr(tx, "tx_hash", None) or resp)

    async def reduce_only_close(self, position: Decimal, max_slippage_ratio: Decimal) -> None:
        """用 reduce-only IOC 单平掉指定净持仓(正=多→卖出,负=空→买入)。"""
        if position == 0:
            return
        side = "sell" if position > 0 else "buy"
        bbo = await self.get_bbo()
        await self.hedge_ioc(side, abs(position), bbo, max_slippage_ratio, reduce_only=True)
