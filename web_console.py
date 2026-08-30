"""
本地 Web 控制台:后台跑对冲机器人,浏览器里完成首次设置、启停、改参、一键熔断。

用法:
    python web_console.py            # 默认 http://127.0.0.1:8788
    python web_console.py --port 9000

只监听 127.0.0.1,不要暴露到公网(接口无鉴权,仅限本机使用)。

设置向导安全边界:
  - Lighter API 私钥 / Popdex Agent 私钥:验证通过后写入本地 .env(600 权限)
  - Popdex 主钱包私钥:只用于签发一次 approveAgent 授权交易,仅存在于单次请求
    内存中,授权完成立即丢弃,绝不写入文件或日志
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from aiohttp import web

import setup_store
from hedge_bot import HedgeBot, build

log = logging.getLogger("web_console")

# 控制台日志面板只看业务日志,过滤 HTTP 访问/请求噪音
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).parent / "static"

STATUS_KEYS = [
    "LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX", "LIGHTER_API_PRIVATE_KEY",
    "POPDEX_ACCOUNT_ADDRESS", "POPDEX_SIGNER_PRIVATE_KEY",
    "LIGHTER_SYMBOL", "POPDEX_SYMBOL", "DRY_RUN",
]


class Console:
    def __init__(self) -> None:
        self.bot: HedgeBot | None = None
        self.bot_task: asyncio.Task | None = None
        self.start_error: str = ""
        # 已通过验证的凭据(内存标记;控制台重启后需重新验证再开实盘)
        self.verified = {"lighter": False, "popdex": False}
        # 网页生成、等待授权的 Agent 钱包(仅内存)
        self.pending_agent: dict | None = None

    # ---------------- 机器人生命周期 ----------------

    async def start_bot(self) -> None:
        if self.bot_task and not self.bot_task.done():
            return
        if self.bot is None:
            self.bot = await build()
        self.start_error = ""
        self.bot_task = asyncio.create_task(self.bot.run(install_signals=False))

    async def stop_bot(self, reason: str = "控制台手动停止") -> None:
        if self.bot:
            self.bot.stop(reason=reason)
        if self.bot_task:
            try:
                await asyncio.wait_for(self.bot_task, timeout=15)
            except asyncio.TimeoutError:
                self.bot_task.cancel()
            except Exception:  # noqa: BLE001 run() 内部已处理
                pass

    async def rebuild(self) -> None:
        """设置变更后:停机 → 断开旧客户端 → 按 .env 重建(不自动启动)。"""
        was_running = bool(self.bot and self.bot.state.get("running"))
        await self.stop_bot(reason="配置变更,重建")
        if self.bot:
            try:
                await self.bot.popdex.close()
                await self.bot.lighter.close()
            except Exception:  # noqa: BLE001
                pass
        self.bot = None
        self.bot = await build()
        if was_running:
            await self.start_bot()
        return None

    async def panic(self) -> None:
        """一键熔断:停机 + 双侧 reduce-only 平仓。"""
        if self.bot:
            await self.bot._emergency("控制台一键熔断")

    # ---------------- 验证 ----------------

    async def verify_lighter(self) -> bool:
        """实测当前 .env 里的 Lighter 凭据。"""
        from lighter_client import verify_credentials

        env = setup_store.read_raw()
        key = env.get("LIGHTER_API_PRIVATE_KEY", "")
        if not key:
            self.verified["lighter"] = False
            return False
        try:
            info = await verify_credentials(
                base_url=env.get("LIGHTER_BASE_URL") or "https://mainnet.zklighter.elliot.ai",
                account_index=int(env.get("LIGHTER_ACCOUNT_INDEX") or "0"),
                api_key_index=int(env.get("LIGHTER_API_KEY_INDEX") or "0"),
                api_private_key=key,
            )
            log.info("Lighter 凭据验证通过: account_index=%s equity=%s",
                     info["account_index"], info["equity"])
            self.verified["lighter"] = True
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Lighter 凭据验证失败: %s", exc)
            self.verified["lighter"] = False
            return False

    async def verify_popdex(self) -> bool:
        """实测 Popdex 主地址 + Agent 授权(getAgentInfo)。"""
        from popdex_client import PopdexClient, PopdexConfig

        env = setup_store.read_raw()
        account = env.get("POPDEX_ACCOUNT_ADDRESS", "")
        agent_key = env.get("POPDEX_SIGNER_PRIVATE_KEY", "")
        if not account or not agent_key:
            self.verified["popdex"] = False
            return False

        from eth_account import Account

        agent_address = Account.from_key(agent_key).address
        client = PopdexClient(PopdexConfig(
            api_url=env.get("POPDEX_API_URL") or "https://api.popdex.xyz",
            chain_id=int(env.get("POPDEX_CHAIN_ID") or "2184"),
            account_address=account, signer_private_key=""))
        try:
            await client.connect()
            info = await client.get_agent_info(agent_address)
        except Exception as exc:  # noqa: BLE001
            log.warning("Popdex 验证失败: %s", exc)
            self.verified["popdex"] = False
            return False
        finally:
            await client.close()

        ok = (info["exists"] and not info["is_expired"]
              and info["delegator"].lower() == account.lower())
        if ok:
            log.info("Popdex Agent 验证通过: agent=%s delegator=%s 有效期至 %s",
                     agent_address, info["delegator"],
                     time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(info["expires_at"] / 1000)))
        else:
            log.warning("Popdex Agent 验证失败: exists=%s expired=%s delegator=%s(期望主账户 %s)",
                        info["exists"], info["is_expired"], info["delegator"], account)
        self.verified["popdex"] = ok
        return ok


console = Console()


def _need_bot() -> HedgeBot:
    if console.bot is None:
        raise web.HTTPBadRequest(text=json.dumps({"error": "机器人尚未初始化,先点启动"},
                                                 ensure_ascii=False),
                                 content_type="application/json")
    return console.bot


async def _json_error(exc: Exception) -> web.Response:
    return web.json_response({"ok": False, "error": str(exc)}, status=500)


# ---------------- 页面与状态 ----------------

async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def setup_page(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "setup.html")


async def api_status(_request: web.Request) -> web.Response:
    if console.bot is None:
        return web.json_response({
            "initialized": False,
            "starting": bool(console.bot_task and not console.bot_task.done()),
            "start_error": console.start_error,
            "setup": _setup_snapshot(),
        })
    snap = console.bot.snapshot()
    snap["initialized"] = True
    snap["starting"] = bool(console.bot_task and not console.bot_task.done())
    snap["start_error"] = console.start_error
    snap["logs"] = list(console.bot.ring_log.buf)
    snap["trades"] = list(reversed(console.bot.trades))
    snap["setup"] = _setup_snapshot()
    return web.json_response(snap)


def _setup_snapshot() -> dict:
    masked = setup_store.get_masked(STATUS_KEYS)
    env = setup_store.read_raw()
    return {
        "fields": masked,
        "verified": dict(console.verified),
        "live_ready": console.verified["lighter"] and console.verified["popdex"],
        "dry_run": (env.get("DRY_RUN", "true").lower() != "false"),
    }


# ---------------- 两所交集交易对 ----------------

async def api_markets(_request: web.Request) -> web.Response:
    """两所都在交易的交易对交集(Popdex XXXUSDT ↔ Lighter XXX)。"""
    try:
        async with _ClientPool() as pool:
            markets = await pool.markets()
    except Exception as exc:  # noqa: BLE001
        return await _json_error(exc)
    current = setup_store.read_raw().get("POPDEX_SYMBOL", "")
    return web.json_response({"markets": markets, "current": current})


# ---------------- 设置向导 ----------------

async def api_setup_get(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, **_setup_snapshot()})


async def api_setup_lighter(request: web.Request) -> web.Response:
    """表单直填 Lighter 凭据:先实测,通过才写 .env。"""
    from lighter_client import verify_credentials

    try:
        form = await request.json()
        env = setup_store.read_raw()
        info = await verify_credentials(
            base_url=env.get("LIGHTER_BASE_URL") or "https://mainnet.zklighter.elliot.ai",
            account_index=int(form["account_index"]),
            api_key_index=int(form.get("api_key_index") or 0),
            api_private_key=form["api_private_key"],
        )
    except Exception as exc:  # noqa: BLE001
        console.verified["lighter"] = False
        return await _json_error(exc)
    setup_store.update({
        "LIGHTER_ACCOUNT_INDEX": str(form["account_index"]),
        "LIGHTER_API_KEY_INDEX": str(form.get("api_key_index") or 0),
        "LIGHTER_API_PRIVATE_KEY": form["api_private_key"],
    })
    console.verified["lighter"] = True
    log.info("Lighter 凭据已保存并验证(equity=%s)", info["equity"])
    await _safe_rebuild()
    return web.json_response({"ok": True, "equity": info["equity"]})


async def api_setup_popdex(request: web.Request) -> web.Response:
    """表单直填 Popdex 主地址 + Agent 私钥:链上验证授权关系后写 .env。"""
    try:
        form = await request.json()
        account = str(form["account_address"]).strip()
        agent_key = str(form["agent_private_key"]).strip()
        from eth_account import Account

        agent_address = Account.from_key(agent_key).address
        from popdex_client import PopdexClient, PopdexConfig

        env = setup_store.read_raw()
        client = PopdexClient(PopdexConfig(
            api_url=env.get("POPDEX_API_URL") or "https://api.popdex.xyz",
            chain_id=int(env.get("POPDEX_CHAIN_ID") or "2184"),
            account_address=account, signer_private_key=""))
        await client.connect()
        try:
            info = await client.get_agent_info(agent_address)
        finally:
            await client.close()
        if not info["exists"]:
            raise RuntimeError("该 Agent 未被任何主账户授权,请先用向导完成授权")
        if info["is_expired"]:
            raise RuntimeError("该 Agent 授权已过期,请重新授权")
        if info["delegator"].lower() != account.lower():
            raise RuntimeError(f"该 Agent 属于主账户 {info['delegator']},与填写的地址不符")
    except Exception as exc:  # noqa: BLE001
        console.verified["popdex"] = False
        return await _json_error(exc)
    setup_store.update({
        "POPDEX_ACCOUNT_ADDRESS": account,
        "POPDEX_SIGNER_PRIVATE_KEY": agent_key,
    })
    console.verified["popdex"] = True
    log.info("Popdex 主账户与 Agent 已保存并验证(agent=%s)", agent_address)
    await _safe_rebuild()
    return web.json_response({"ok": True, "agent": agent_address,
                              "expires_at": info["expires_at"]})


async def api_setup_agent_create(_request: web.Request) -> web.Response:
    """生成新 Agent 钱包(私钥只留在服务端内存,等授权后一并保存)。"""
    from eth_account import Account

    acct = Account.create()
    console.pending_agent = {"address": acct.address, "private_key": "0x" + acct.key.hex()}
    log.info("已生成待授权 Agent 钱包: %s", acct.address)
    return web.json_response({"ok": True, "agent_address": acct.address})


async def api_setup_agent_authorize(request: web.Request) -> web.Response:
    """粘贴主钱包私钥完成 approveAgent 授权(仅内存使用,立即丢弃)。"""
    from authorize_agent import authorize_agent_onchain

    if not console.pending_agent:
        return web.json_response({"ok": False, "error": "请先生成 Agent 钱包"},
                                 status=400)
    try:
        form = await request.json()
        env = setup_store.read_raw()
        result = await asyncio.to_thread(
            authorize_agent_onchain,
            rpc_url=(env.get("POPDEX_API_URL") or "https://api.popdex.xyz") + "/api/v1/web3/rpc",
            chain_id=int(env.get("POPDEX_CHAIN_ID") or "2184"),
            agent=console.pending_agent["address"],
            main_private_key=str(form["main_private_key"]).strip(),
            days=int(form.get("days") or 30),
        )
    except Exception as exc:  # noqa: BLE001
        return await _json_error(exc)
    # 授权成功:主地址(从私钥推出)与 Agent 私钥一起落盘
    setup_store.update({
        "POPDEX_ACCOUNT_ADDRESS": result["main_address"],
        "POPDEX_SIGNER_PRIVATE_KEY": console.pending_agent["private_key"],
    })
    console.pending_agent = None
    console.verified["popdex"] = True
    log.info("Agent 授权完成: main=%s agent=%s tx=%s",
             result["main_address"], result["agent"], result["tx_hash"])
    await _safe_rebuild()
    result.pop("main_address", None)  # 主地址回传无妨,但保持返回精简
    return web.json_response({"ok": True, **result})


async def api_setup_agent_wallet_confirm(request: web.Request) -> web.Response:
    """钱包弹窗授权的收尾:用户提供钱包广播的 approveAgent 交易哈希,
    后端等回执→链上验证 getAgentInfo→保存主地址(取交易发起者)+Agent私钥。
    全程不接触任何私钥。"""
    from popdex_client import PopdexClient, PopdexConfig

    if not console.pending_agent:
        return web.json_response({"ok": False, "error": "请先生成 Agent 钱包"}, status=400)
    try:
        form = await request.json()
        tx_hash = str(form["tx_hash"]).strip()
        if not tx_hash.startswith("0x") or len(tx_hash) != 66:
            raise ValueError("交易哈希格式不对(应为66位0x开头)")
        env = setup_store.read_raw()
        client = PopdexClient(PopdexConfig(
            api_url=env.get("POPDEX_API_URL") or "https://api.popdex.xyz",
            chain_id=int(env.get("POPDEX_CHAIN_ID") or "2184"),
            account_address="", signer_private_key=""))
        await client.connect()
        try:
            receipt = await asyncio.to_thread(
                client._w3.eth.wait_for_transaction_receipt, tx_hash, 180)
            if receipt.status != 1:
                raise RuntimeError("授权交易上链失败(reverted)")
            signer_addr = receipt["from"]
            info = await client.get_agent_info(console.pending_agent["address"])
            if not info["exists"] or info["isExpired"]:
                raise RuntimeError("链上未查到有效授权")
            if info["delegator"].lower() != signer_addr.lower():
                raise RuntimeError(f"授权归属 {info['delegator']},与签名钱包不符")
        finally:
            await client.close()
        setup_store.update({
            "POPDEX_ACCOUNT_ADDRESS": signer_addr,
            "POPDEX_SIGNER_PRIVATE_KEY": console.pending_agent["private_key"],
        })
        agent_addr = console.pending_agent["address"]
        console.pending_agent = None
        console.verified["popdex"] = True
        log.info("钱包授权完成: main=%s agent=%s tx=%s", signer_addr, agent_addr, tx_hash)
        await _safe_rebuild()
        return web.json_response({"ok": True, "main_address": signer_addr,
                                  "agent": agent_addr, "tx_hash": tx_hash,
                                  "expires_at": info["expires_at"]})
    except Exception as exc:  # noqa: BLE001
        return await _json_error(exc)


async def api_setup_agent_test(_request: web.Request) -> web.Response:
    """发送一笔 noop 空交易:端到端验证 Agent 签名/时间戳nonce/RPC 链路,无业务效果。"""
    from popdex_client import PopdexClient, PopdexConfig

    env = setup_store.read_raw()
    if not (env.get("POPDEX_SIGNER_PRIVATE_KEY") and env.get("POPDEX_ACCOUNT_ADDRESS")):
        return web.json_response({"ok": False, "error": "未配置 Popdex 凭据"}, status=400)
    client = PopdexClient(PopdexConfig(
        api_url=env.get("POPDEX_API_URL") or "https://api.popdex.xyz",
        chain_id=int(env.get("POPDEX_CHAIN_ID") or "2184"),
        account_address=env["POPDEX_ACCOUNT_ADDRESS"],
        signer_private_key=env["POPDEX_SIGNER_PRIVATE_KEY"],
        dry_run=False,
    ))
    try:
        await client.connect()
        tx_hash = await client.send_noop()
    except Exception as exc:  # noqa: BLE001
        return await _json_error(exc)
    finally:
        await client.close()
    log.info("noop 测试交易成功: %s", tx_hash)
    return web.json_response({"ok": True, "tx_hash": tx_hash})


async def api_setup_symbol(request: web.Request) -> web.Response:
    """选择交易对(仅限两所交集),写入 .env 并重建。"""
    try:
        form = await request.json()
        base = str(form["base"]).upper()
        # 实时校验交集
        async with _ClientPool() as pool:
            markets = await pool.markets()
        match = next((m for m in markets if m["base"] == base), None)
        if not match:
            raise RuntimeError(f"{base} 不在两所共同支持的交易对里")
    except Exception as exc:  # noqa: BLE001
        return await _json_error(exc)
    setup_store.update({
        "POPDEX_SYMBOL": match["popdex_symbol"],
        "LIGHTER_SYMBOL": match["lighter_symbol"],
        "POPDEX_SYMBOL_ID": str(match["popdex_symbol_id"]),
    })
    log.info("交易对已切换: %s (Popdex %s / Lighter %s)",
             base, match["popdex_symbol"], match["lighter_symbol"])
    await _safe_rebuild()
    # 等首个 tick 填充行情(体检需要当前价换算轮数量)
    if console.bot:
        for _ in range(20):
            if (console.bot.state.get("prices") or {}).get("popdex_bid"):
                break
            await asyncio.sleep(0.5)
    # 跨币种纠偏:熔断线是 base 币单位,换币后旧值语义失效(如 BTC 的 0.012 在 AAVE 上
    # 一轮都兜不住,首次成交即熔断)。若缓冲不足自动调到 3 轮并返回提示。
    auto_fixed = None
    if console.bot:
        for w in console.bot.check_config():
            if w["key"] == "max_net_position":
                console.bot.apply_config({"max_net_position": w["suggest"]})
                setup_store.update({"MAX_NET_POSITION": w["suggest"]})
                auto_fixed = w["suggest"]
                log.warning("熔断线已按 %s 自动调整: %s(原值对新币种缓冲不足)", base, w["suggest"])
    return web.json_response({"ok": True, "market": match, "auto_fixed_cap": auto_fixed})


async def api_setup_mode(request: web.Request) -> web.Response:
    """切换 DRY_RUN / 实盘。实盘要求两项凭据都已验证 + 前端输入确认词。"""
    try:
        form = await request.json()
        live = bool(form.get("live"))
        if live:
            if form.get("confirm") != "实盘":
                raise RuntimeError("需要输入确认词「实盘」")
            if not (console.verified["lighter"] and console.verified["popdex"]):
                await console.verify_lighter()
                await console.verify_popdex()
            if not (console.verified["lighter"] and console.verified["popdex"]):
                raise RuntimeError("凭据未验证通过,不能开实盘(请在向导里逐项验证)")
        setup_store.update({
            "DRY_RUN": "false" if live else "true",
            "LIVE_TRADING_ACK": "true" if live else "false",
        })
    except Exception as exc:  # noqa: BLE001
        return await _json_error(exc)
    log.warning("模式切换: %s", "实盘!" if live else "DRY_RUN")
    await _safe_rebuild()
    return web.json_response({"ok": True, "live": live})


async def api_setup_verify(_request: web.Request) -> web.Response:
    """对 .env 里已保存的凭据重新跑一遍验证。"""
    lighter_ok = await console.verify_lighter()
    popdex_ok = await console.verify_popdex()
    return web.json_response({"ok": lighter_ok and popdex_ok,
                              "lighter": lighter_ok, "popdex": popdex_ok})


async def api_setup_clear(_request: web.Request) -> web.Response:
    setup_store.clear_secrets()
    console.verified = {"lighter": False, "popdex": False}
    log.warning("已清除全部保存的密钥")
    await _safe_rebuild()
    return web.json_response({"ok": True})


class _ClientPool:
    """复用 markets 查询的临时连接。"""

    async def __aenter__(self):
        from lighter import ApiClient, Configuration
        import lighter as _lighter
        from popdex_client import PopdexClient, PopdexConfig

        env = setup_store.read_raw()
        self._popdex = PopdexClient(PopdexConfig(
            api_url=env.get("POPDEX_API_URL") or "https://api.popdex.xyz",
            chain_id=int(env.get("POPDEX_CHAIN_ID") or "2184"),
            account_address="", signer_private_key=""))
        await self._popdex.connect()
        self._api = ApiClient(configuration=Configuration(
            host=env.get("LIGHTER_BASE_URL") or "https://mainnet.zklighter.elliot.ai"))
        self._lighter = _lighter
        return self

    async def markets(self) -> list[dict]:
        popdex_symbols = await self._popdex.get_futures_symbols()
        books = await self._lighter.OrderApi(self._api).order_books()
        lighter_bases = {m.symbol.upper(): m.market_id for m in books.order_books}
        out = []
        for m in popdex_symbols:
            base = m["symbol"].upper().removesuffix("USDT")
            if base in lighter_bases:
                out.append({"base": base, "popdex_symbol": m["symbol"],
                            "popdex_symbol_id": m["symbol_id"], "lighter_symbol": base,
                            "last_price": m.get("last_price", ""),
                            "change_pct": m.get("change_pct", "")})
        return sorted(out, key=lambda x: x["base"])

    async def __aexit__(self, *exc):
        await self._popdex.close()
        await self._api.close()
        return False


async def _safe_rebuild() -> None:
    try:
        await console.rebuild()
    except Exception as exc:  # noqa: BLE001
        log.warning("重建机器人失败(配置可能不完整): %s", exc)


# ---------------- 机器人操作 ----------------

async def api_start(_request: web.Request) -> web.Response:
    try:
        await console.start_bot()
        return web.json_response({"ok": True})
    except Exception as exc:  # noqa: BLE001
        console.start_error = str(exc)
        log.exception("启动失败")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def api_stop(_request: web.Request) -> web.Response:
    await console.stop_bot()
    return web.json_response({"ok": True})


async def api_panic(_request: web.Request) -> web.Response:
    await console.panic()
    return web.json_response({"ok": True})


async def api_config(request: web.Request) -> web.Response:
    bot = _need_bot()
    try:
        updates = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "请求体不是合法 JSON"}, status=400)
    updates = dict(updates or {})
    errors: Dict[str, str] = {}

    # 策略模式:特殊处理(切换时撤单重置),通过校验才持久化
    mode_req = updates.pop("strategy_mode", None)
    if mode_req is not None:
        try:
            await bot.set_strategy_mode(str(mode_req))
            setup_store.update({"STRATEGY_MODE": str(mode_req)})
        except Exception as exc:  # noqa: BLE001
            errors["strategy_mode"] = str(exc)

    errs2 = bot.apply_config(updates)
    errors.update(errs2)
    # 持久化到 .env(重启不丢);只写通过校验的键
    if not errors:
        persisted = {str(k).upper(): str(v) for k, v in updates.items()}
        if persisted:
            setup_store.update(persisted)
    return web.json_response({"ok": not errors, "errors": errors,
                              "config": bot.snapshot()["config"],
                              "warnings": bot.check_config()})


async def on_startup(app: web.Application) -> None:
    """控制台启动即后台验证 .env 里已保存的凭据,重启后向导徽章自动变绿。"""
    async def _verify():
        try:
            await console.verify_lighter()
            await console.verify_popdex()
        except Exception as exc:  # noqa: BLE001
            log.debug("启动验证失败: %s", exc)
    asyncio.create_task(_verify())


async def on_shutdown(app: web.Application) -> None:
    if console.bot_task and not console.bot_task.done():
        console.bot and console.bot.stop(reason="控制台进程退出")
        try:
            await asyncio.wait_for(console.bot_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            console.bot_task.cancel()
    if console.bot:
        await console.bot.popdex.close()
        await console.bot.lighter.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Popdex↔Lighter 对冲机器人 Web 控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/setup", setup_page)
    app.router.add_static("/static/", STATIC_DIR)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/markets", api_markets)
    app.router.add_get("/api/setup", api_setup_get)
    app.router.add_post("/api/setup/lighter", api_setup_lighter)
    app.router.add_post("/api/setup/popdex", api_setup_popdex)
    app.router.add_post("/api/setup/agent/create", api_setup_agent_create)
    app.router.add_post("/api/setup/agent/authorize", api_setup_agent_authorize)
    app.router.add_post("/api/setup/agent/test", api_setup_agent_test)
    app.router.add_post("/api/setup/agent/wallet_confirm", api_setup_agent_wallet_confirm)
    app.router.add_post("/api/setup/symbol", api_setup_symbol)
    app.router.add_post("/api/setup/mode", api_setup_mode)
    app.router.add_post("/api/setup/verify", api_setup_verify)
    app.router.add_post("/api/setup/clear", api_setup_clear)
    app.router.add_post("/api/start", api_start)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_post("/api/panic", api_panic)
    app.router.add_post("/api/config", api_config)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    print(f"\n  对冲机器人控制台: http://{args.host}:{args.port}\n"
          f"  (仅本机访问;首次使用请先在页面完成设置向导)\n")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
