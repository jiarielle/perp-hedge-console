#!/usr/bin/env python3
"""每小时风控检查(纯maker对冲)。
数据源:优先控制台API(127.0.0.1:8788),失败则读机器人自写的状态文件 BOT_STATE_FILE。
输出: OK行报告 / STOP_NOW+数据(并自动停机,SIGTERM触发优雅平仓) / ERROR(绝不触发停止)。
"""
import json, os, subprocess, sys, time

B = "http://127.0.0.1:8788"
STATE_FILE = os.path.expanduser(
    os.environ.get("BOT_STATE_FILE", "~/bot_state.json"))
# 基线从环境/.env 读取(私密数据不入库);未配置时退化为纯状态上报(不做磨损判定)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass
BASE_EQ = float(os.environ.get("HEDGE_BASE_EQ", "0") or 0)
BASE_HEDGED = float(os.environ.get("HEDGE_BASE_HEDGED", "0") or 0)

s = None
try:
    raw = subprocess.run(["curl", "-s", "--max-time", "5", f"{B}/api/status"],
                         capture_output=True, text=True, timeout=8).stdout
    s = json.loads(raw)
except Exception:
    s = None
if not isinstance(s, dict) or not s.get("stats"):
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception as e:
        print(f"ERROR 无法获取状态(API与{STATE_FILE}都失败): {e}")
        sys.exit(0)   # 失败开放:不停止

if not s.get("running"):
    print(f"机器人未运行 stop_reason={s.get('stop_reason','')!r}")
    sys.exit(0)

def _stop_bot(line: str) -> None:
    print(f"STOP_NOW {line}")
    print("自动停机中(SIGTERM→优雅撤单平仓)...")
    # stop.flag:看门狗1小时内不得拉起,尊重熔断(人工确认后再清)
    try:
        with open(os.path.expanduser("~/stop.flag"), "w") as f:
            f.write(f"{time.strftime('%F %T')} {line}\n")
    except Exception:
        pass
    subprocess.run(["pkill", "-f", "hedge_bot.py"], capture_output=True)

try:
    b = s.get("balances") or {}
    p = s.get("prices") or {}
    total = float(b.get("popdex_equity") or 0) + float(b.get("lighter_equity") or 0)
    if total <= 0:
        print("ERROR 权益数据缺失(不停止)")
        sys.exit(0)
    wear = BASE_EQ - total
    vol = (float(s["stats"]["hedged_qty"]) - BASE_HEDGED) * float(p.get("popdex_bid") or 77500) * 2
    rate = wear / vol * 10000 if (vol > 2000 and int(s['stats']['rounds']) >= 5) else None
    net = float(s.get("positions", {}).get("net") or 0)
    errs = int(s.get("errors") or 0)
    line = (f"{time.strftime('%H:%M')} 轮数={s['stats']['rounds']} 单边增量=${vol:,.0f} "
            f"磨损=${wear:.2f} 磨损率={(f'{rate:.2f}bp' if rate is not None else '样本不足')} "
            f"敞口={net} errors={errs}")

    if BASE_EQ <= 0:
        print(f"OK(未设基线,仅上报) 轮数={s['stats']['rounds']} 敞口={net} errors={errs}"); sys.exit(0)
    # 冲刺$1M:绝对磨损上限$250(全程预算),比率上限3.5bp不变
    stop = (wear > 250) or (rate is not None and rate > 3.5) or errs >= 3 or abs(net) > 0.006
    if stop:
        _stop_bot(line)
    else:
        print(f"OK {line}")
except Exception as e:
    print(f"ERROR 计算失败: {e}(不停止)")
