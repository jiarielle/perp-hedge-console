"""
Popdex Agent 钱包生成 + 授权工具。

Popdex 没有传统 API key:程序化下单需要一个"Agent 钱包",由主账户在链上
Account 合约(0x...1008)调用 approveAgent 授权。Agent 只有交易权,
不能提现、不能转走资产,可设置有效期,可随时撤销。参考实现来自
lihanyu81/PopDEX---Lighter-Arbitrage-Tool-From-PandaZhai 的 MIT 开源模块。

用法:
  # 1) 生成 Agent 钱包(本地随机,写入 agent.env,权限 600)
  python authorize_agent.py create

  # 2) 授权(默认只预演 + Gas 估算,不广播)
  python authorize_agent.py authorize
  #    确认预演输出无误后,显式广播:
  python authorize_agent.py authorize --send

  # 3) 查询授权状态
  python authorize_agent.py info --agent 0x...

授权后,把 agent.env 里的地址对应私钥填入 .env 的 POPDEX_SIGNER_PRIVATE_KEY,
POPDEX_ACCOUNT_ADDRESS 填主账户地址。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
import time
from pathlib import Path

ACCOUNT_CONTRACT = "0x0000000000000000000000000000000000001008"
POPDEX_TIME_URL = "https://api.popdex.xyz/api/v1/public/time"

ACCOUNT_ABI = [
    {
        "type": "function",
        "name": "approveAgent",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "agent", "type": "address"},
            {"name": "delegator", "type": "address"},
            {"name": "name", "type": "bytes32"},
            {"name": "expiresAt", "type": "uint64"},
            {"name": "initialNonce", "type": "uint64"},
            {"name": "isGlobal", "type": "bool"},
        ],
        "outputs": [],
    },
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


def agent_env_path() -> Path:
    return Path(os.environ.get("AGENT_ENV", "agent.env")).resolve()


def cmd_create(args: argparse.Namespace) -> None:
    from eth_account import Account

    path = agent_env_path()
    if path.exists():
        raise SystemExit(f"{path} 已存在,拒绝覆盖。请先备份/删除或用 AGENT_ENV 指定其他路径")

    acct = Account.create()
    path.write_text(
        f"POPDEX_AGENT_ADDRESS={acct.address}\n"
        f"POPDEX_AGENT_PRIVATE_KEY=0x{acct.key.hex()}\n"
        f"CREATED_AT={int(time.time())}\n",
        encoding="utf-8",
    )
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
    print(f"Agent 钱包已生成并写入 {path}(权限 600)")
    print(f"  地址  : {acct.address}")
    print(f"下一步 : python {sys.argv[0]} authorize")


def _fetch_time_ms(rpc_base: str = "https://api.popdex.xyz") -> int:
    import httpx

    resp = httpx.get(f"{rpc_base}/api/v1/public/time", timeout=15)
    resp.raise_for_status()
    return int(resp.json()["data"]["systemTs"])


def authorize_agent_onchain(
    *,
    rpc_url: str,
    chain_id: int,
    agent: str,
    main_private_key: str,
    delegator: str | None = None,
    name: str | None = None,
    days: int = 30,
) -> dict:
    """用主账户私钥签发 approveAgent 交易并等待上链。

    供 CLI 与 Web 控制台共用。main_private_key 只在本函数栈内使用,
    返回前即丢弃,不写文件、不打日志。返回 getAgentInfo 结果。
    """
    from eth_account import Account
    from web3 import Web3

    agent = Web3.to_checksum_address(agent)
    signer = Account.from_key(main_private_key)
    delegator = Web3.to_checksum_address(delegator or signer.address)

    name_bytes = (name or f"hedge-{agent[-6:].lower()}").encode("utf-8")
    if not 0 < len(name_bytes) <= 32:
        raise ValueError("Agent 名称 UTF-8 长度须为 1-32 字节")
    name_bytes32 = name_bytes.ljust(32, b"\0")

    rpc_base = rpc_url.rsplit("/api/v1/web3/rpc", 1)[0]
    initial_nonce = _fetch_time_ms(rpc_base)
    expires_at = initial_nonce + days * 24 * 60 * 60 * 1000

    web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
    if not web3.is_connected():
        raise RuntimeError("无法连接 Popdex RPC")
    if web3.eth.chain_id != chain_id:
        raise RuntimeError(f"chainId 不符:期望 {chain_id},实际 {web3.eth.chain_id}")

    contract = web3.eth.contract(address=Web3.to_checksum_address(ACCOUNT_CONTRACT), abi=ACCOUNT_ABI)
    approval = contract.functions.approveAgent(
        agent, delegator, name_bytes32, expires_at, initial_nonce, False
    )
    gas = approval.estimate_gas({"from": signer.address})
    block = web3.eth.get_block("latest")
    base_fee = block.get("baseFeePerGas") or web3.to_wei(1, "gwei")
    tx = approval.build_transaction({
        "from": signer.address,
        "chainId": chain_id,
        "nonce": web3.eth.get_transaction_count(signer.address, "pending"),
        "gas": int(gas * 1.2),
        "type": 2,
        "maxFeePerGas": int(base_fee * 2),
        "maxPriorityFeePerGas": 0,
        "value": 0,
    })
    signed = signer.sign_transaction(tx)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise RuntimeError(f"授权交易执行失败: {tx_hash.hex()}")

    info = contract.functions.getAgentInfo(agent).call()
    return {
        "main_address": signer.address,
        "agent": agent,
        "tx_hash": tx_hash.hex(),
        "exists": bool(info[0]),
        "expires_at": int(info[1]),
        "is_expired": bool(info[2]),
        "delegator": info[3],
    }


def cmd_authorize(args: argparse.Namespace) -> None:
    from eth_account import Account
    from web3 import Web3

    path = agent_env_path()
    if not path.exists():
        raise SystemExit(f"找不到 {path},先运行: python {sys.argv[0]} create")

    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key.strip()] = value.strip()
    agent = Web3.to_checksum_address(env["POPDEX_AGENT_ADDRESS"])

    private_key = getpass.getpass("请输入 Popdex 主账户私钥(输入不显示):").strip()
    try:
        signer = Account.from_key(private_key)
    finally:
        private_key = ""
    delegator = Web3.to_checksum_address(args.delegator or signer.address)

    name = (args.name or f"hedge-{agent[-6:].lower()}").encode("utf-8")
    if not 0 < len(name) <= 32:
        raise SystemExit("Agent 名称 UTF-8 长度须为 1-32 字节")
    name_bytes32 = name.ljust(32, b"\0")

    initial_nonce = _fetch_time_ms()
    expires_at = initial_nonce + args.days * 24 * 60 * 60 * 1000

    web3 = Web3(Web3.HTTPProvider(args.rpc_url, request_kwargs={"timeout": 15}))
    if not web3.is_connected():
        raise SystemExit("无法连接 Popdex RPC")
    if web3.eth.chain_id != args.chain_id:
        raise SystemExit(f"chainId 不符:期望 {args.chain_id},实际 {web3.eth.chain_id}")

    contract = web3.eth.contract(address=Web3.to_checksum_address(ACCOUNT_CONTRACT), abi=ACCOUNT_ABI)
    approval = contract.functions.approveAgent(
        agent, delegator, name_bytes32, expires_at, initial_nonce, False
    )

    print("\n授权预览")
    print(f"  Chain ID    : {args.chain_id}")
    print(f"  签署主账户  : {signer.address}")
    print(f"  Agent      : {agent}")
    print(f"  delegator  : {delegator}")
    print(f"  名称       : {name.decode()}")
    print(f"  有效期     : {args.days} 天(至 {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expires_at / 1000))})")
    print(f"  isGlobal   : False")
    print(f"  合约       : {ACCOUNT_CONTRACT}")

    gas = approval.estimate_gas({"from": signer.address})
    print(f"  Gas 估算   : {gas}")

    if not args.send:
        print("\n预演成功,未发送交易。确认无误后运行:")
        print(f"  python {sys.argv[0]} authorize --send")
        return

    confirm = input("\n输入大写 AUTHORIZE 才会广播交易:").strip()
    if confirm != "AUTHORIZE":
        print("已取消,未发送任何交易")
        return

    block = web3.eth.get_block("latest")
    base_fee = block.get("baseFeePerGas") or web3.to_wei(1, "gwei")
    tx = approval.build_transaction({
        "from": signer.address,
        "chainId": args.chain_id,
        "nonce": web3.eth.get_transaction_count(signer.address, "pending"),
        "gas": int(gas * 1.2),
        "type": 2,
        "maxFeePerGas": int(base_fee * 2),
        "maxPriorityFeePerGas": 0,
        "value": 0,
    })
    signed = signer.sign_transaction(tx)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"\n交易已发送: {tx_hash.hex()}")
    receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1:
        raise SystemExit(f"授权交易执行失败: {tx_hash.hex()}")
    print(f"授权成功,区块 {receipt.blockNumber}")

    info = contract.functions.getAgentInfo(agent).call()
    print("\n授权验证")
    print(f"  exists    : {info[0]}")
    print(f"  expiresAt : {info[1]}")
    print(f"  isExpired : {info[2]}")
    print(f"  delegator : {info[3]}")
    print(f"\n把 agent.env 中的 POPDEX_AGENT_PRIVATE_KEY 填入 .env 的 "
          f"POPDEX_SIGNER_PRIVATE_KEY,POPDEX_ACCOUNT_ADDRESS 填主账户地址即可开始 DRY_RUN。")


def cmd_info(args: argparse.Namespace) -> None:
    from web3 import Web3

    web3 = Web3(Web3.HTTPProvider(args.rpc_url, request_kwargs={"timeout": 15}))
    contract = web3.eth.contract(address=Web3.to_checksum_address(ACCOUNT_CONTRACT), abi=ACCOUNT_ABI)
    info = contract.functions.getAgentInfo(Web3.to_checksum_address(args.agent)).call()
    print(json.dumps({
        "exists": info[0],
        "expiresAt": info[1],
        "isExpired": info[2],
        "delegator": info[3],
        "name": bytes(info[4]).rstrip(b"\0").decode("utf-8", "replace"),
        "isGlobal": info[5],
    }, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Popdex Agent 钱包生成/授权工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("create", help="生成本地 Agent 钱包(agent.env)")

    p_auth = sub.add_parser("authorize", help="授权 Agent(默认只预演)")
    p_auth.add_argument("--delegator", help="委托账户,默认签署主账户本身")
    p_auth.add_argument("--name", help="Agent 名称(≤32字节),默认自动生成")
    p_auth.add_argument("--days", type=int, default=30, help="授权有效天数,默认 30")
    p_auth.add_argument("--rpc-url", default="https://api.popdex.xyz/api/v1/web3/rpc")
    p_auth.add_argument("--chain-id", type=int, default=2184)
    p_auth.add_argument("--send", action="store_true", help="预演成功并输入 AUTHORIZE 后才真正广播")

    p_info = sub.add_parser("info", help="查询 Agent 授权状态")
    p_info.add_argument("--agent", required=True, help="Agent 钱包地址")
    p_info.add_argument("--rpc-url", default="https://api.popdex.xyz/api/v1/web3/rpc")
    p_info.add_argument("--chain-id", type=int, default=2184)

    args = parser.parse_args()
    if args.cmd == "create":
        cmd_create(args)
    elif args.cmd == "authorize":
        cmd_authorize(args)
    elif args.cmd == "info":
        cmd_info(args)


if __name__ == "__main__":
    main()
