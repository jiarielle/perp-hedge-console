"""
Popdex ↔ Lighter 对冲机器人(主循环)。

策略(与 perp-dex-tools 的 hedge_mode 同型,把主所换成 Popdex):
  1. 在 Popdex 挂 post-only maker 限价单(买挂 bid+offset,卖挂 ask-offset,多空交替);
  2. 轮询 Popdex 持仓变化检测成交(REST,已验证端点);
  3. 一旦 Popdex 成交数量 Q,立刻在 Lighter 以 IOC 限价单反向对冲 Q;
  4. 两边同时产生成交量(maker + taker),净敞口维持在 MAX_NET_POSITION 内;
  5. 净敞口超限或连续出错时,用 reduce-only 市价单把两边仓位打回安全区并停机。

风控:
  - DRY_RUN 默认开启:只看行情、打印订单参数,不签任何交易
  - 实盘需 .env 同时设 DRY_RUN=false 和 LIVE_TRADING_ACK=true
  - 单腿对冲失败 → 重试 → 仍失败则 reduce-only 强平 Popdex 侧并熔断
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import setup_store
from dotenv import load_dotenv

from lighter_client import LighterClient, LighterConfig
from popdex_client import (
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    PopdexClient,
    PopdexConfig,
    TIF_IOC,
    TIF_POST_ONLY,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("lighter").setLevel(logging.WARNING)
log = logging.getLogger("hedge_bot")


@dataclass
class BotConfig:
    lighter_symbol: str
    popdex_symbol: str
    popdex_symbol_id: int
    order_qty: Decimal
    order_notional_usdt: Decimal        # >0 时按目标USDT金额换算数量,覆盖 order_qty
    max_net_position: Decimal
    maker_offset: Decimal
    requote_threshold: Decimal
    fill_timeout_sec: float
    hedge_max_slippage_bps: int
    poll_interval_sec: float
    position_check_sec: float
    max_consecutive_errors: int
    dry_run: bool
    min_edge_bps: int = 0          # 套利模式价差门槛(bps);对冲/刷量模式忽略
    maker_fee_bps: int = 1         # Popdex maker 费率(bps):VIP0=1,VIP3=0;套利门槛计算用
    strategy_mode: str = 'arb'     # arb=套利(赚钱才出手) hedge=跨所对冲刷量 volume=Popdex单所刷量
    volume_round_interval: float = 5.0   # 刷量模式:两轮之间的间隔秒(链上限速,建议≥3)
    hedge_deadline_taker: int = 1        # 对冲模式:maker超时后升级市价吃单(保证成交,提速用;0=纯maker)
    hedge_edge_gate_bp: int = 2          # 对冲模式基差门控:最优侧基差<-N bp时暂停挂单(0=关)
    demo: bool = False   # 未配置凭据:只看行情,不挂单不查持仓

    @staticmethod
    def from_env() -> "BotConfig":
        return BotConfig(
            lighter_symbol=os.environ.get("LIGHTER_SYMBOL") or "BTC",
            popdex_symbol=os.environ.get("POPDEX_SYMBOL") or "BTCUSDT",
            popdex_symbol_id=int(os.environ.get("POPDEX_SYMBOL_ID") or "0"),  # 0 = 待解析
            order_qty=Decimal(os.environ.get("ORDER_QTY", "0.01")),
            order_notional_usdt=Decimal(os.environ.get("ORDER_NOTIONAL_USDT", "0")),
            max_net_position=Decimal(os.environ.get("MAX_NET_POSITION", "0.03")),
            maker_offset=Decimal(os.environ.get("MAKER_OFFSET", "0.05")),
            requote_threshold=Decimal(os.environ.get("REQUOTE_THRESHOLD", "0.5")),
            fill_timeout_sec=float(os.environ.get("FILL_TIMEOUT_SEC", "120")),
            hedge_max_slippage_bps=int(os.environ.get("HEDGE_MAX_SLIPPAGE_BPS", "30")),
            min_edge_bps=int(os.environ.get("MIN_EDGE_BPS", "0")),
            maker_fee_bps=int(os.environ.get("MAKER_FEE_BPS", "1")),
            strategy_mode=os.environ.get("STRATEGY_MODE") or "arb",
            volume_round_interval=float(os.environ.get("VOLUME_ROUND_INTERVAL", "5")),
            hedge_deadline_taker=int(os.environ.get("HEDGE_DEADLINE_TAKER", "1")),
            hedge_edge_gate_bp=int(os.environ.get("HEDGE_EDGE_GATE_BP", "2")),
            poll_interval_sec=float(os.environ.get("POLL_INTERVAL_SEC", "0.6")),
            position_check_sec=float(os.environ.get("POSITION_CHECK_SEC", "2.0")),
            max_consecutive_errors=int(os.environ.get("MAX_CONSECUTIVE_ERRORS", "5")),
            dry_run=os.environ.get("DRY_RUN", "true").lower() != "false",
        )


HEDGE_COOLDOWN_SEC = 6.0   # 对冲冷却:等成交回报+仓位刷新,防锤击循环


class RingLogHandler(logging.Handler):
    """把日志收进环形缓冲,供 Web 控制台读取。"""

    def __init__(self, capacity: int = 400):
        super().__init__()
        self.buf: deque = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                                            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buf.append(self.format(record))
        except Exception:
            pass


class HedgeBot:
    def __init__(self, cfg: BotConfig, popdex: PopdexClient, lighter: LighterClient):
        self.cfg = cfg
        self.popdex = popdex
        self.lighter = lighter
        self._running = False
        self._next_maker_side: str = "buy"          # 多空交替
        self._active_oid: Optional[str] = None       # 当前 Popdex maker 单 clientOid
        self._active_side: str = ""
        self._active_price: Optional[Decimal] = None
        self._active_qty = Decimal(0)
        self._placed_at = 0.0
        self._last_popdex_pos = Decimal(0)           # 上次观测的 Popdex 净持仓
        self._last_hedge_at = 0.0                    # 上次对冲时间戳(冷却用)
        self._net_errors = 0                         # 连续网络错误计数(软处理)
        self._vol_prev_pos = Decimal(0)              # 刷量模式:上次观测持仓
        self._last_vol_round = 0.0                   # 刷量模式:上轮开仓时间戳
        self._errors = 0
        self._stop_reason = ""

        # Web 控制台用的状态与历史
        self.ring_log = RingLogHandler()
        logging.getLogger().addHandler(self.ring_log)
        self.trades: deque = deque(maxlen=300)       # 成交/对冲事件
        self.state: Dict[str, Any] = {
            "running": False,
            "mode": "DEMO" if cfg.demo else ("DRY_RUN" if cfg.dry_run else "LIVE"),
            "popdex_symbol": cfg.popdex_symbol,
            "lighter_symbol": cfg.lighter_symbol,
            "prices": {},          # {popdex_bid, popdex_ask, lighter_bid, lighter_ask, spread_bps, updated}
            "positions": {},       # {popdex, lighter, net, cap}
            "balances": {},        # {popdex_equity, popdex_available, lighter_equity, ...}
            "leverage": "",
            "edge": {},            # 套利模式:双向净价差 {sell_bps, buy_bps, threshold}
            "waiting_edge": False,  # 套利模式:价差不足,持币等待中
            "order": None,         # {side, price, qty, oid, placed_at}
            "errors": 0,
            "last_error": "",
            "stop_reason": "",
            "started_at": None,
            "stats": {"rounds": 0, "hedged_qty": "0"},
        }

    def stop(self, *_args, reason: str = "手动停止") -> None:
        log.warning("停止机器人:%s(退出前将自动撤掉全部挂单并市价平掉两侧仓位)", reason)
        self._running = False
        self._stop_reason = reason

    def apply_config(self, updates: Dict[str, Any]) -> Dict[str, str]:
        """运行时修改策略参数(只允许白名单内的数值字段)。返回错误信息 dict。"""
        zero_ok = {"order_notional_usdt", "min_edge_bps", "maker_fee_bps", "hedge_deadline_taker", "maker_offset", "hedge_edge_gate_bp"}
        errors: Dict[str, str] = {}
        for key, raw in (updates or {}).items():
            if not hasattr(self.cfg, key):
                errors[key] = "未知参数"
                continue
            cur = getattr(self.cfg, key)
            try:
                if isinstance(cur, Decimal):
                    val: Any = Decimal(str(raw))
                    if val < 0 or (key not in zero_ok and val <= 0):
                        raise ValueError
                elif isinstance(cur, int):
                    val = int(raw)
                    if val < 0 or (key not in zero_ok and val <= 0):
                        raise ValueError
                else:
                    val = float(raw)
                    if val <= 0:
                        raise ValueError
            except (ValueError, TypeError, InvalidOperation):
                errors[key] = "必须是有效数值"
                continue
            setattr(self.cfg, key, val)
        return errors

    # ---------------- 主循环 ----------------

    async def run(self, install_signals: bool = True) -> None:
        if install_signals:
            loop = asyncio.get_event_loop()
            for s in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(s, self.stop)
                except NotImplementedError:
                    pass

        # 启动时核对两侧持仓:非零则拒绝启动,避免在未知敞口上继续叠加
        p_pos = await self.popdex.get_position_size(self.cfg.popdex_symbol)
        l_pos = await self.lighter.get_position_size()
        net = p_pos + l_pos
        log.info("启动持仓核对: Popdex %s %s | Lighter %s %s | 净敞口 %s",
                 p_pos, self.cfg.popdex_symbol, l_pos, self.cfg.lighter_symbol, net)
        if net != 0:
            log.warning("⚠ 启动时净敞口非零(%s):机器人只对冲新成交,不会自动回平存量敞口,"
                        "请自行决定对冲或平仓", net)
        if abs(p_pos) > self.cfg.max_net_position or abs(l_pos) > self.cfg.max_net_position:
            log.error("启动时已存在超过 MAX_NET_POSITION 的单边持仓,请先手动处理后再运行")
            self._stop_reason = "启动时持仓超限"
            return

        self._running = True
        self._stop_reason = ""
        self.state.update(running=True, started_at=time.time(), stop_reason="",
                          errors=0, last_error="")
        self._errors = 0
        self._last_popdex_pos = p_pos
        last_pos_check = time.monotonic()

        while self._running:
            try:
                await self._tick()
                last_pos_check = await self._maybe_check_positions(last_pos_check)
                self._errors = 0
                self.state["errors"] = 0
            except Exception as exc:  # noqa: BLE001 主循环兜底,任何异常计数并可能熔断
                msg = str(exc)
                is_net = ("Cannot connect" in msg or "SSLError" in msg
                          or "ClientConnectorError" in msg or "Max retries" in msg)
                if is_net:
                    # ★ 网络瞬断软处理:代理抖动很常见,不值得立即熔断。
                    # 等20秒重试;连续2分钟(6次)都连不上才算真故障熔断。
                    self._net_errors += 1
                    log.warning("网络瞬断(%d/6,等20s重试): %s", self._net_errors, msg[:80])
                    if self._net_errors >= 6:
                        await self._emergency(f"网络持续中断2分钟: {msg[:60]}")
                        return
                    await asyncio.sleep(20)
                    continue
                self._errors += 1
                self.state["errors"] = self._errors
                self.state["last_error"] = msg
                log.exception("tick 异常 (%d/%d): %s", self._errors,
                              self.cfg.max_consecutive_errors, exc)
                if self._errors >= self.cfg.max_consecutive_errors:
                    await self._emergency(f"连续错误达到上限")
                    return
            # 本tick正常走完 → 清零网络错误计数
            self._net_errors = 0
            await asyncio.sleep(self.cfg.poll_interval_sec)

        # 主循环退出:撤全部挂单 + 双侧仓位平到零(任何模式一致)
        if not self.cfg.demo:
            try:
                await self._flatten_all()
            except Exception:
                log.exception("停机收尾失败,请立即手动检查挂单与持仓!")
        self.state.update(running=False, stop_reason=self._stop_reason,
                          order=None)

    async def _tick(self) -> None:
        pop_bid, pop_ask = await self.popdex.get_bbo(self.cfg.popdex_symbol)
        lig_bid, lig_ask = await self.lighter.get_bbo()

        spread_bps = (pop_ask - lig_bid) / lig_bid * 10000 if lig_bid else Decimal(0)
        self.state["prices"] = {
            "popdex_bid": str(pop_bid), "popdex_ask": str(pop_ask),
            "lighter_bid": str(lig_bid), "lighter_ask": str(lig_ask),
            "spread_bps": f"{spread_bps:.1f}",
            "updated": time.time(),
        }

        # 演示模式:只更新行情,不挂单不查持仓
        if self.cfg.demo:
            return

        # 刷量模式:Popdex 单所自我成交,不碰 Lighter
        if self.cfg.strategy_mode == "volume":
            await self._tick_volume(pop_bid, pop_ask)
            return

        # 1) 净敞口对齐:任何来源的失衡(成交/部分成交/遗漏)都直接对冲到零。
        #    比逐笔 delta 检测健壮——不存在检测窗口漏洞。
        #    ★ 冷却期修复:对冲有 ~1.5s 成交回报延迟,若每 0.5s tick 都允许
        #    再对冲,会叠加多笔在途订单(实测90秒内锤了9笔)。冷却 6 秒,
        #    期间仓位读数追上来,净敞口计算自然收敛。
        popdex_pos = await self.popdex.get_position_size(self.cfg.popdex_symbol)
        lighter_pos = await self.lighter.get_position_size()
        net = popdex_pos + lighter_pos
        mid = (pop_bid + pop_ask) / 2
        now_mono = time.monotonic()
        if now_mono - self._last_hedge_at < HEDGE_COOLDOWN_SEC:
            net = Decimal(0)     # 冷却期内不再触发对冲
        if net != 0 and abs(net) * mid >= Decimal("10"):
            # Lighter BTC 最小名义 10 USDT,不足则并入下轮
            hedge_side = "sell" if net > 0 else "buy"
            log.info("检测到未对冲敞口 net=%s → Lighter %s %s", net, hedge_side, abs(net))
            self._last_hedge_at = now_mono
            await self._hedge(hedge_side, abs(net), (lig_bid, lig_ask))
            self.trades.append({
                "time": time.strftime("%m-%d %H:%M:%S"),
                "popdex_fill": str(popdex_pos),
                "hedge": f"{hedge_side.upper()} {abs(net)}",
                "hedge_price": str(lig_ask if hedge_side == "buy" else lig_bid),
            })
            self.state["stats"]["rounds"] += 1
            self.state["stats"]["hedged_qty"] = str(
                Decimal(self.state["stats"]["hedged_qty"]) + abs(net))
            self._active_oid = None
            self._active_side = ""
            self._active_price = None
            self._next_maker_side = "buy" if self._next_maker_side == "sell" else "sell"
        self._last_popdex_pos = popdex_pos
        self.state["positions"]["popdex"] = str(popdex_pos)

        # 2) 无活动 maker 单 → 选方向挂新单
        if self._active_oid is None:
            # ★ 基差门控(对冲模式):双边基差都太差时暂停挂单(磨损主因是逆基差成交)。
            # 只挑基差较优的一侧挂;最优侧 < -gate 时宁可等待(等待=零成本)。
            if self.cfg.strategy_mode == "hedge" and self.cfg.hedge_edge_gate_bp > 0:
                e_sell = (pop_ask - lig_ask) / lig_ask * 10000   # 卖Pdex+买Lt锁定的基差
                e_buy = (lig_bid - pop_bid) / pop_bid * 10000    # 买Pdex+卖Lt
                best_side = "sell" if e_sell >= e_buy else "buy"
                best_edge = max(e_sell, e_buy)
                self.state["edge"] = {"sell_bps": f"{e_sell:.1f}", "buy_bps": f"{e_buy:.1f}",
                                      "threshold": -self.cfg.hedge_edge_gate_bp, "updated": time.time()}
                if best_edge < -self.cfg.hedge_edge_gate_bp:
                    self.state["waiting_edge"] = True
                    return
                self.state["waiting_edge"] = False
                self._next_maker_side = best_side   # 跟随较优基差侧
            # 双向净价差(bps,扣 Popdex maker 手续费,按当前VIP档位):
            # sell = Popdex卖maker@pop_ask + Lighter买taker@lig_ask 每轮锁定的收益
            # buy  = Popdex买maker@pop_bid + Lighter卖taker@lig_bid
            fee_bps = Decimal(str(self.cfg.maker_fee_bps))
            edge_sell = (pop_ask - lig_ask) / lig_ask * 10000 - fee_bps
            edge_buy = (lig_bid - pop_bid) / pop_bid * 10000 - fee_bps
            arb = self.cfg.strategy_mode == "arb"
            self.state["edge"] = {"sell_bps": f"{edge_sell:.1f}", "buy_bps": f"{edge_buy:.1f}",
                                  "threshold": self.cfg.min_edge_bps if arb else 0,
                                  "updated": time.time()}
            if arb and self.cfg.min_edge_bps > 0:
                # 套利模式:只在价差达标的一侧挂单;都不达标就持币等待
                if edge_sell >= self.cfg.min_edge_bps:
                    self._next_maker_side = "sell"
                    log.info("价差套利: sell侧价差 %.1fbps ≥ %d,挂卖单", edge_sell, self.cfg.min_edge_bps)
                elif edge_buy >= self.cfg.min_edge_bps:
                    self._next_maker_side = "buy"
                    log.info("价差套利: buy侧价差 %.1fbps ≥ %d,挂买单", edge_buy, self.cfg.min_edge_bps)
                else:
                    self.state["waiting_edge"] = True
                    return
            self.state["waiting_edge"] = False
            await self._place_maker(pop_bid, pop_ask)
            return

        # 3) 已挂单:盘口漂移/超时/价差消失 → 撤单重挂
        target = (pop_bid + self.cfg.maker_offset) if self._active_side == "buy" \
            else (pop_ask - self.cfg.maker_offset)
        drifted = abs(target - (self._active_price or target))
        timed_out = (time.monotonic() - self._placed_at) > self.cfg.fill_timeout_sec
        edge_gone = False
        if self.cfg.strategy_mode == "arb" and self.state.get("edge"):
            e = Decimal(self.state["edge"]["sell_bps" if self._active_side == "sell" else "buy_bps"])
            edge_gone = e < 0   # 挂单方向价差转负,成交反而亏 → 立即撤
        if drifted > self.cfg.requote_threshold or timed_out or edge_gone:
            reason = "价差消失" if edge_gone else ("漂移" if drifted > self.cfg.requote_threshold else "超时")
            # 对冲模式提速:超时(而非漂移)时升级为市价吃单,本轮保底成交;
            # maker白嫖与taker付费混合,平均成本≈ taker占比×2.4bp
            if (timed_out and not drifted > self.cfg.requote_threshold and not edge_gone
                    and self.cfg.strategy_mode == "hedge" and self.cfg.hedge_deadline_taker):
                side = self._active_side
                qty = self._active_qty
                await self._cancel_active("超时升级taker")
                slip = Decimal(self.cfg.hedge_max_slippage_bps) / 10000
                log.info("对冲提速: %s 市价吃单 %s(5秒maker无人吃)", side.upper(), qty)
                await self.popdex.place_order(
                    symbol_id=self.cfg.popdex_symbol_id, side=side, qty=qty,
                    order_type=ORDER_TYPE_MARKET, time_in_force=TIF_IOC,
                    slippage_ratio=slip)
                return   # 成交后由净敞口对齐自动在 Lighter 对冲
            await self._cancel_active(reason)

        self.state["order"] = {
            "side": self._active_side,
            "price": str(self._active_price),
            "qty": str(self._active_qty),
            "oid": self._active_oid,
            "age": round(time.monotonic() - self._placed_at, 1),
        } if self._active_oid else None

    async def _tick_volume(self, pop_bid: Decimal, pop_ask: Decimal) -> None:
        """刷量模式(taker):Popdex 单所市价秒开 → 确认成交 → 市价秒平 → 循环。

        与油猴脚本同款节奏:不挂单干等,双腿 taker 立即成交;
        两腿都带滑点保护(hedge_max_slippage_bps),轮间隔 volume_round_interval 控制节奏
        (Popdex RPC 限速 ~120笔交易/分钟,每轮2笔,间隔别低于3秒)。
        不使用 Lighter;仓位即净敞口,超过 MAX_NET_POSITION 由常规核对熔断。
        """
        pos = await self.popdex.get_position_size(self.cfg.popdex_symbol)
        slip = Decimal(self.cfg.hedge_max_slippage_bps) / 10000
        mid = (pop_bid + pop_ask) / 2

        if pos != 0:
            # 有仓 → 立即市价平掉(reduce-only)
            side = "sell" if pos > 0 else "buy"
            log.info("刷量平仓: MARKET %s %s (reduce-only)", side.upper(), abs(pos))
            await self.popdex.place_order(
                symbol_id=self.cfg.popdex_symbol_id, side=side, qty=abs(pos),
                order_type=ORDER_TYPE_MARKET, time_in_force=TIF_IOC,
                slippage_ratio=slip, reduce_only=True)
            await asyncio.sleep(1.5)   # 等成交回报,下轮核对
            self.state["order"] = None
            return

        # 空仓:上一轮刚平完则计入统计
        delta = self._vol_prev_pos  # 上次观测还持仓,现在归零 → 平仓完成
        if delta != 0:
            self.state["stats"]["rounds"] += 1
            self.state["stats"]["hedged_qty"] = str(
                Decimal(self.state["stats"]["hedged_qty"]) + abs(delta))
            self.trades.append({
                "time": time.strftime("%m-%d %H:%M:%S"),
                "popdex_fill": "0",
                "hedge": f"本所秒平 {abs(delta)}",
                "hedge_price": str(mid),
            })
        self._vol_prev_pos = Decimal(0)

        # 轮间隔控节奏
        if time.monotonic() - self._last_vol_round < self.cfg.volume_round_interval:
            self.state["order"] = None
            return

        # 市价开仓,多空交替
        side = self._next_maker_side
        qty = (self.cfg.order_notional_usdt / mid).quantize(
            Decimal("0.000001"), rounding="ROUND_DOWN") \
            if self.cfg.order_notional_usdt > 0 else self.cfg.order_qty
        if qty <= 0:
            return
        log.info("刷量开仓: MARKET %s %s", side.upper(), qty)
        await self.popdex.place_order(
            symbol_id=self.cfg.popdex_symbol_id, side=side, qty=qty,
            order_type=ORDER_TYPE_MARKET, time_in_force=TIF_IOC,
            slippage_ratio=slip)
        self._last_vol_round = time.monotonic()
        self._vol_prev_pos = await self.popdex.get_position_size(self.cfg.popdex_symbol)
        self._next_maker_side = "buy" if side == "sell" else "sell"
        self.state["order"] = None

    async def set_strategy_mode(self, mode: str) -> None:
        """切换策略模式:撤掉当前挂单,重置基准持仓。"""
        if mode not in ("volume", "hedge", "arb"):
            raise ValueError("模式必须是 volume/hedge/arb")
        self.cfg.strategy_mode = mode
        if self._active_oid:
            await self._cancel_active("切换模式")
        self._vol_prev_pos = Decimal(0)
        self.state["waiting_edge"] = False
        self.state["order"] = None
        log.info("策略模式切换为: %s", mode)

    # ---------------- 动作 ----------------

    async def _place_maker(self, pop_bid: Decimal, pop_ask: Decimal, side: Optional[str] = None) -> None:
        # ★ 库存感知修复:原版盲目多空交替,单边行情下仓位堆积(实测积到空8轮)。
        # 现在:若 Popdex 库存超过 2 轮的量,强制挂反方向单(把库存吃回来)。
        if side is None:
            side = self._next_maker_side
            inv = self._last_popdex_pos
            two_rounds = self.cfg.order_qty * 2 if self.cfg.order_notional_usdt <= 0 else                 (self.cfg.order_notional_usdt / ((pop_bid + pop_ask) / 2)) * 2
            if inv > two_rounds:
                side = "sell"      # 多头库存过重 → 强制挂卖单减仓
                log.info("库存感知: 多头库存 %s 超2轮,强制挂卖", inv)
            elif inv < -two_rounds:
                side = "buy"
                log.info("库存感知: 空头库存 %s 超2轮,强制挂买", inv)
        price = (pop_bid + self.cfg.maker_offset) if side == "buy" \
            else (pop_ask - self.cfg.maker_offset)
        # 防穿越钳制(参考 perp-dex-tools 快速模式:任何价差状态都保持在场内):
        # 买单必须 < 对手价,卖单必须 > 对手价,否则 PostOnly 在区块执行时 revert;
        # 点差仅1个tick无处更进取时,排同侧最优价队列(靠短超时换边提高成交率)
        tick = self.popdex.symbol_cfg.get("tick_size", Decimal(0))
        if tick > 0:
            if side == "buy":
                price = min(price, pop_ask - tick)
                if price <= pop_bid:
                    price = pop_bid
            else:
                price = max(price, pop_bid + tick)
                if price >= pop_ask:
                    price = pop_ask
        # 目标量:优先按USDT名义金额换算(小白友好),否则用 base 币数量
        if self.cfg.order_notional_usdt > 0:
            mid = (pop_bid + pop_ask) / 2
            qty = (self.cfg.order_notional_usdt / mid).quantize(
                Decimal("0.000001"), rounding="ROUND_DOWN")
        else:
            qty = self.cfg.order_qty
        if qty <= 0:
            log.warning("计算出的下单数量为 0(名义金额太小?),跳过本轮")
            return
        # 清场式撤单:快速模式下撤单/重挂频繁,偶发匹配失误或上链延迟会留下旧单
        # 叠加堆积。挂新单前把本交易对全部挂单撤光,保证任意时刻最多1张在场内。
        try:
            strays = await self.popdex.get_pending_orders(self.cfg.popdex_symbol)
            for o in strays:
                await self.popdex.cancel_order(int(o["orderId"]))
            if strays:
                log.info("清场撤单 %d 张(防堆积)", len(strays))
        except Exception as exc:
            log.warning("清场撤单失败(继续挂单): %s", exc)
        self._active_oid = await self.popdex.place_order(
            symbol_id=self.cfg.popdex_symbol_id,
            side=side,
            qty=qty,
            order_type=ORDER_TYPE_LIMIT,
            time_in_force=TIF_POST_ONLY,
            price=price,
        )
        self._active_side = side
        self._active_price = price
        self._active_qty = qty
        self._placed_at = time.monotonic()
        self.state["order"] = {"side": side, "price": str(price), "qty": str(qty),
                               "oid": self._active_oid, "age": 0}
        log.info("已挂 maker 单: %s %s @ %s (oid=%s)", side, qty, price, self._active_oid)

    async def _cancel_active(self, reason: str) -> None:
        """撤掉当前 maker 单:REST 挂单列表按 方向+价格+数量 匹配出链上 orderId 再撤。

        实测 REST 返回的 clientOid 为 null,不能按 clientOid 查;按字段匹配。
        若匹配到多张(历史堆积),全部撤掉。"""
        log.info("撤单(%s): oid=%s price=%s", reason, self._active_oid, self._active_price)
        side_cn = {"buy": "Buy", "sell": "Sell"}.get(self._active_side, "")
        cancelled = 0
        try:
            orders = await self.popdex.get_pending_orders(self.cfg.popdex_symbol)
            for o in orders:
                if (str(o.get("side")) == side_cn
                        and self._active_price is not None
                        and Decimal(str(o.get("price", "0"))) == self._active_price
                        and Decimal(str(o.get("remainingQty", o.get("qty", "0")))) > 0):
                    order_id = int(o["orderId"])
                    await self.popdex.cancel_order(order_id, self._active_oid)
                    cancelled += 1
            if cancelled == 0:
                log.warning("未匹配到挂单(可能已成交/已撤销)")
        except Exception as exc:
            log.warning("撤单查询/执行失败: %s", exc)
        finally:
            self._active_oid = None
            self._active_side = ""
            self._active_price = None

    async def _flatten_all(self) -> None:
        """停机/熔断收尾:撤掉本交易对全部挂单,并把两侧仓位 reduce-only 平到零。

        任何模式通用;带重试与最终复核,平不掉会打出 CRITICAL 级警报。
        """
        slip = Decimal(self.cfg.hedge_max_slippage_bps) / 10000

        # 1) 撤掉 Popdex 本交易对全部挂单(不只当前活动单,防异常残留)
        try:
            orders = await self.popdex.get_pending_orders(self.cfg.popdex_symbol)
            for o in orders:
                try:
                    await self.popdex.cancel_order(int(o["orderId"]))
                except Exception:
                    log.exception("停机撤单失败 orderId=%s", o.get("orderId"))
        except Exception:
            log.exception("停机查询挂单失败,请手动检查 Popdex 挂单!")

        # 2) 两侧仓位平到零(重试 + 平后复核)
        for venue in ("popdex", "lighter"):
            for attempt in (1, 2, 3):
                try:
                    if venue == "popdex":
                        pos = await self.popdex.get_position_size(self.cfg.popdex_symbol)
                        if pos == 0:
                            break
                        side = "sell" if pos > 0 else "buy"
                        log.error("停机平 Popdex 侧: reduce-only %s %s", side, abs(pos))
                        await self.popdex.place_order(
                            symbol_id=self.cfg.popdex_symbol_id,
                            side=side,
                            qty=abs(pos),
                            order_type=ORDER_TYPE_MARKET,
                            time_in_force=TIF_IOC,
                            reduce_only=True,
                        )
                    else:
                        pos = await self.lighter.get_position_size()
                        if pos == 0:
                            break
                        log.error("停机平 Lighter 侧: reduce-only %s", pos)
                        await self.lighter.reduce_only_close(pos, slip)
                    await asyncio.sleep(1.5)   # 等成交回报/持仓刷新,下一轮复核
                except Exception:
                    log.exception("%s 停机平仓第 %d 次失败", venue, attempt)
                    if attempt == 3:
                        log.critical("!!! %s 平仓三次全部失败,请立即手动处理!!!", venue)
                    await asyncio.sleep(1)

        # 3) 最终复核
        try:
            p = await self.popdex.get_position_size(self.cfg.popdex_symbol)
            l = await self.lighter.get_position_size()
            if p != 0 or l != 0:
                log.critical("!!! 停机平仓后仍有持仓: Popdex=%s Lighter=%s,请立即手动处理!!!", p, l)
            else:
                log.info("停机收尾完成:挂单已清,两侧仓位为零")
        except Exception:
            log.exception("停机复核失败,请手动确认两侧持仓!")

    async def _hedge(self, side: str, qty: Decimal, bbo) -> None:
        slippage = Decimal(self.cfg.hedge_max_slippage_bps) / 10000
        last_err: Optional[Exception] = None
        for attempt in (1, 2, 3):
            try:
                fresh_bbo = bbo if attempt == 1 else await self.lighter.get_bbo()
                await self.lighter.hedge_ioc(side, qty, fresh_bbo, slippage)
                return
            except Exception as exc:  # noqa: PERF203
                last_err = exc
                log.warning("对冲第 %d 次失败: %s", attempt, exc)
                await asyncio.sleep(0.5)
        await self._emergency(f"对冲连续失败: {last_err}")

    async def _maybe_check_positions(self, last: float) -> float:
        if self.cfg.demo:
            return last  # 演示模式不查持仓
        now = time.monotonic()
        if now - last < self.cfg.position_check_sec:
            return last
        p = await self.popdex.get_position_size(self.cfg.popdex_symbol)
        l = await self.lighter.get_position_size()
        net = p + l
        self.state["positions"] = {
            "popdex": str(p), "lighter": str(l),
            "net": str(net), "cap": str(self.cfg.max_net_position),
            "updated": time.time(),
        }
        # 权益与杠杆(每轮核对时顺带刷新)
        try:
            pe = await self.popdex.get_equity()
            le = await self.lighter.get_equity()
            self.state["balances"] = {
                "popdex_equity": pe.get("equity", ""),
                "popdex_available": pe.get("available", ""),
                "lighter_equity": le.get("equity", ""),
                "lighter_available": le.get("available", ""),
                "updated": time.time(),
            }
        except Exception as exc:  # noqa: BLE001 权益读取失败不阻断主循环
            log.debug("权益读取失败: %s", exc)
        try:
            self.state["leverage"] = await self.popdex.get_leverage(self.cfg.popdex_symbol)
        except Exception:  # noqa: BLE001
            self.state["leverage"] = ""
        log.info("持仓核对: Popdex=%s Lighter=%s 净敞口=%s (上限 %s)",
                 p, l, net, self.cfg.max_net_position)
        if abs(net) > self.cfg.max_net_position:
            await self._emergency(f"净敞口 {net} 超过上限 {self.cfg.max_net_position}")
        return now

    async def _emergency(self, reason: str) -> None:
        """熔断:停机 + 撤全部挂单 + 双侧 reduce-only 平到零(人工接管)。"""
        log.error("!!! 熔断:%s", reason)
        self._running = False
        self._stop_reason = f"熔断: {reason}"
        self.state.update(running=False, stop_reason=self._stop_reason)
        await self._flatten_all()

    def snapshot(self) -> Dict[str, Any]:
        """Web 控制台状态快照(含当前配置)。"""
        st = dict(self.state)
        st["config"] = {
            "strategy_mode": self.cfg.strategy_mode,
            "volume_round_interval": self.cfg.volume_round_interval,
            "hedge_deadline_taker": self.cfg.hedge_deadline_taker,
            "hedge_edge_gate_bp": self.cfg.hedge_edge_gate_bp,
            "order_qty": str(self.cfg.order_qty),
            "order_notional_usdt": str(self.cfg.order_notional_usdt),
            "max_net_position": str(self.cfg.max_net_position),
            "maker_offset": str(self.cfg.maker_offset),
            "requote_threshold": str(self.cfg.requote_threshold),
            "fill_timeout_sec": self.cfg.fill_timeout_sec,
            "hedge_max_slippage_bps": self.cfg.hedge_max_slippage_bps,
            "min_edge_bps": self.cfg.min_edge_bps,
            "maker_fee_bps": self.cfg.maker_fee_bps,
            "poll_interval_sec": self.cfg.poll_interval_sec,
            "position_check_sec": self.cfg.position_check_sec,
            "max_consecutive_errors": self.cfg.max_consecutive_errors,
        }
        return st

    def check_config(self) -> List[Dict[str, str]]:
        """参数联动体检:检查隐藏的参数耦合关系,返回警告列表。

        每条:{level, key(建议修改的字段), suggest(建议值), msg(人话说明)}。
        只警告不拦截;前端提供一键采纳。核心联动:熔断线应 ≥ 单轮数量×3。"""
        cfg = self.cfg
        warnings: List[Dict[str, str]] = []
        prices = self.state.get("prices") or {}
        mid: Optional[Decimal] = None
        if prices.get("popdex_bid"):
            mid = (Decimal(prices["popdex_bid"]) + Decimal(prices["popdex_ask"])) / 2

        # 单轮数量(base 币):目标金额优先,按当前价换算
        qty = cfg.order_qty
        if cfg.order_notional_usdt > 0:
            if mid:
                qty = cfg.order_notional_usdt / mid
            else:
                qty = Decimal(0)

        # 1) 熔断线缓冲不足(<2轮):对冲失败一次就熔断停机
        if qty > 0 and cfg.max_net_position < qty * 2:
            rounds = float(cfg.max_net_position / qty)
            suggest = (qty * 3).quantize(Decimal("0.000001")).normalize()
            warnings.append({
                "level": "high",
                "key": "max_net_position",
                "suggest": f"{suggest}",
                "msg": f"净敞口熔断线 {cfg.max_net_position} 只够 {rounds:.1f} 轮缓冲(建议≥3轮):"
                       f"一次对冲失败就可能触发熔断停机",
            })

        # 2) 单轮名义低于 Lighter 最小下单额(10 USDT,对冲单会被拒)
        notional = cfg.order_notional_usdt if cfg.order_notional_usdt > 0 else (
            qty * mid if (qty and mid) else Decimal(0))
        if 0 < notional < 10:
            warnings.append({
                "level": "high", "key": "order_notional_usdt", "suggest": "24",
                "msg": f"每轮名义 {notional:.2f} USDT 低于 Lighter 最小下单额(10 USDT),对冲单会被拒",
            })

        # 3) 保证金占用 vs 两所可用权益(Popdex 按当前杠杆,Lighter 按 2% IMR)
        bal = self.state.get("balances") or {}
        try:
            pd_eq = Decimal(bal.get("popdex_equity") or "0")
            lt_eq = Decimal(bal.get("lighter_equity") or "0")
        except Exception:
            pd_eq = lt_eq = Decimal(0)
        if qty > 0 and mid and pd_eq > 0 and lt_eq > 0:
            lev = Decimal(self.state.get("leverage") or "10") or Decimal(10)
            need_pd = qty * mid / lev
            need_lt = qty * mid * Decimal("0.02")
            if need_pd > pd_eq * Decimal("0.8") or need_lt > lt_eq * Decimal("0.8"):
                # 给出两侧都能承受的 80% 上限
                cap_pd = pd_eq * lev * Decimal("0.8")
                cap_lt = lt_eq / Decimal("0.02") * Decimal("0.8")
                affordable = min(cap_pd, cap_lt)
                suggest = int(affordable / mid) if mid else 0
                warnings.append({
                    "level": "high", "key": "order_notional_usdt", "suggest": f"{max(suggest, 24)}",
                    "msg": f"单轮保证金占用 Popdex≈{need_pd:.1f}U + Lighter≈{need_lt:.1f}U,"
                           f"接近可用权益(Popdex {pd_eq:.0f}U / Lighter {lt_eq:.0f}U)",
                })

        # 4) 挂单偏移 ≥ 重挂阈值:挂出即触发撤单重挂循环
        if cfg.maker_offset >= cfg.requote_threshold:
            warnings.append({
                "level": "mid", "key": "requote_threshold", "suggest": f"{cfg.maker_offset * 5}",
                "msg": f"挂单偏移({cfg.maker_offset})≥ 重挂阈值({cfg.requote_threshold}):"
                       f"单子挂出就会立刻被撤掉重挂,白烧链上交易",
            })

        # 5) 超时太短
        if cfg.fill_timeout_sec < cfg.poll_interval_sec * 5:
            warnings.append({
                "level": "mid", "key": "fill_timeout_sec", "suggest": "30",
                "msg": f"挂单超时({cfg.fill_timeout_sec}s)短于轮询间隔×5,行为异常",
            })

        # 6) 价差门槛低于成本线(手续费≈1bp + 对冲滑点)
        if 0 < cfg.min_edge_bps < 4:
            warnings.append({
                "level": "mid", "key": "min_edge_bps", "suggest": "8",
                "msg": f"价差门槛 {cfg.min_edge_bps}bps 低于成本线(手续费1bp+滑点),成交仍可能亏,建议≥8",
            })

        # 7) 刷量轮间隔低于链上限速安全值(每轮2笔,RPC ~120笔/分钟)
        if cfg.strategy_mode == "volume" and cfg.volume_round_interval < 3:
            warnings.append({
                "level": "mid", "key": "volume_round_interval", "suggest": "3",
                "msg": f"刷量轮间隔 {cfg.volume_round_interval}s 过短:链上RPC约120笔/分钟"
                       f"(每轮2笔交易),建议≥3秒,否则可能被限流",
            })
        return warnings


async def build(dry_run_override: Optional[bool] = None) -> HedgeBot:
    """按 .env 构建 Popdex/Lighter 客户端与机器人。CLI 与 Web 控制台共用。"""
    load_dotenv(override=True)   # 设置向导会改写 .env,这里强制重读
    bot_cfg = BotConfig.from_env()
    if dry_run_override is not None:
        bot_cfg.dry_run = dry_run_override
    live_ack = os.environ.get("LIVE_TRADING_ACK", "").lower() == "true"

    popdex_key = os.environ.get("POPDEX_SIGNER_PRIVATE_KEY") or ""
    popdex_addr = os.environ.get("POPDEX_ACCOUNT_ADDRESS") or ""
    lighter_key = os.environ.get("LIGHTER_API_PRIVATE_KEY") or ""
    bot_cfg.demo = not (popdex_key and popdex_addr and lighter_key)

    if not bot_cfg.dry_run and not live_ack:
        raise RuntimeError("实盘需要在 .env 同时设置 DRY_RUN=false 和 LIVE_TRADING_ACK=true(请通过网页设置向导切换)")

    mode = ("演示(未配置凭据,只看行情)" if bot_cfg.demo
            else "DRY_RUN(已配置,只读不下单)" if bot_cfg.dry_run else "实盘!")
    log.info("=== Popdex↔Lighter 对冲机器人 [%s] %s/%s 每轮 %s ===",
             mode, bot_cfg.popdex_symbol, bot_cfg.lighter_symbol, bot_cfg.order_qty)

    popdex = PopdexClient(PopdexConfig(
        api_url=os.environ.get("POPDEX_API_URL") or "https://api.popdex.xyz",
        chain_id=int(os.environ.get("POPDEX_CHAIN_ID") or "2184"),
        account_address=popdex_addr,
        signer_private_key=popdex_key,
        order_contract=os.environ.get(
            "POPDEX_ORDER_CONTRACT") or "0x0000000000000000000000000000000000001000",
        builder_address=os.environ.get(
            "POPDEX_BUILDER_ADDRESS") or "0x0000000000000000000000000000000000000000",
        builder_fee_rate=int(os.environ.get("POPDEX_BUILDER_FEE_RATE") or "0"),
        dry_run=bot_cfg.dry_run,
    ))
    lighter = LighterClient(LighterConfig(
        base_url=os.environ.get("LIGHTER_BASE_URL") or "https://mainnet.zklighter.elliot.ai",
        account_index=int(os.environ.get("LIGHTER_ACCOUNT_INDEX") or "0"),
        api_key_index=int(os.environ.get("LIGHTER_API_KEY_INDEX") or "0"),
        api_private_key=lighter_key,
        dry_run=bot_cfg.dry_run,
    ))

    await popdex.connect()
    await lighter.connect(bot_cfg.lighter_symbol)
    # symbolId 每次启动强制实时解析(陈旧ID会把A价格打到B币种合约,严重bug);
    # 同时加载精度规则(tickSize/lotSize),下单自动对齐,避免链上 revert
    bot_cfg.popdex_symbol_id = await popdex.resolve_symbol_id(bot_cfg.popdex_symbol)
    setup_store.update({"POPDEX_SYMBOL_ID": str(bot_cfg.popdex_symbol_id)})
    await popdex.load_symbol_config(bot_cfg.popdex_symbol)
    ticker = await popdex.get_ticker(bot_cfg.popdex_symbol)
    log.info("Popdex %s (id=%s) 行情: last=%s bid=%s ask=%s", bot_cfg.popdex_symbol,
             bot_cfg.popdex_symbol_id, ticker.get("lastPrice"),
             ticker.get("bid1Price"), ticker.get("ask1Price"))

    return HedgeBot(bot_cfg, popdex, lighter)


async def main() -> int:
    try:
        bot = await build()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    try:
        await bot.run()
    finally:
        await bot.popdex.close()
        await bot.lighter.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
