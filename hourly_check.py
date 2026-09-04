#!/usr/bin/env python3
"""风控巡检 + 智能提醒(每10分钟cron)。
数据源:优先控制台API(127.0.0.1:8788),失败则读机器人自写的状态文件 BOT_STATE_FILE。
功能:
  1. 硬熔断(自动停机+stop.flag): 累计磨损>WEAR_ABS_STOP / 10分钟边际>$5每万 / 连续错误 / 敞口异常(对冲模式)
  2. 建议提醒(只推送不停机): 对冲模式边际磨损>刷量地板(建议切刷量) / 基差空转没量
  3. 推送: 飞书webhook(FEISHU_WEBHOOK_URL) / Telegram(TELEGRAM_BOT_TOKEN+CHAT_ID) / 本地alerts.log
失败开放: 任何自身错误绝不触发停止。
"""
import json, os, subprocess, sys, time, urllib.request, urllib.parse

B = "http://127.0.0.1:8788"
STATE_FILE = os.path.expanduser(os.environ.get("BOT_STATE_FILE", "~/bot_state.json"))
LAST_FILE = os.path.expanduser("~/check_last.json")
ALERTS_LOG = os.path.expanduser("~/alerts.log")
WEAR_ABS_STOP = 250.0      # 累计磨损绝对上限(全程预算)
RATE_HARD_STOP = 5.0       # 10分钟边际磨损>该值(每万Popdex量) → 熔断
FLOOR_ADVICE = 3.0         # 对冲模式边际>该值(比刷量地板$2.5贵) → 建议切刷量
STARVE_USD = 2000.0        # 一个巡检周期增量低于该值视为空转
ADV_SUPPRESS_SEC = 1800    # 同类建议30分钟内不重复推送

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass
BASE_EQ = float(os.environ.get("HEDGE_BASE_EQ", "0") or 0)
BASE_HEDGED = float(os.environ.get("HEDGE_BASE_HEDGED", "0") or 0)


def notify(text: str) -> None:
    line = f"[Opodex巡检] {text}"
    print(line)
    try:
        with open(ALERTS_LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} {line}\n")
    except Exception:
        pass
    # 飞书群机器人 webhook(在群里建"自定义机器人"拿到 URL 填进 .env 即可)
    fw = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if fw:
        try:
            req = urllib.request.Request(
                fw, data=json.dumps({"msg_type": "text", "content": {"text": line}}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    cid = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tok and cid:
        try:
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{tok}/sendMessage"
                f"?chat_id={cid}&text={urllib.parse.quote(line)}", timeout=10).read()
        except Exception:
            pass


def _stop_bot(reason: str) -> None:
    notify(f"⛔ 熔断停机: {reason}")
    try:
        with open(os.path.expanduser("~/stop.flag"), "w") as f:
            f.write(f"{time.strftime('%F %T')} {reason}\n")
    except Exception:
        pass
    subprocess.run(["pkill", "-f", "hedge_bot.py"], capture_output=True)


def _save_exit(last: dict, wear: float, vol_popdex: float) -> None:
    """保存巡检快照(供下次算边际)后退出。"""
    try:
        with open(LAST_FILE, "w") as f:
            json.dump({"ts": time.time(), "wear": wear, "vol": vol_popdex,
                       "adv_ts": last.get("adv_ts") or {}}, f)
    except Exception:
        pass
    sys.exit(0)


# ---------- 1) 取机器人状态 ----------
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

# ---------- 2) 核心指标 ----------
try:
    b = s.get("balances") or {}
    p = s.get("prices") or {}
    total = float(b.get("popdex_equity") or 0) + float(b.get("lighter_equity") or 0)
    if total <= 0:
        print("ERROR 权益数据缺失(不停止)"); sys.exit(0)
    wear = BASE_EQ - total
    bid = float(p.get("popdex_bid") or 77500)
    vol_popdex = (float(s["stats"]["hedged_qty"]) - BASE_HEDGED) * bid   # Popdex单所量
    vol_double = vol_popdex * 2
    rate_cum = wear / vol_popdex * 10000 if vol_popdex > 5000 else None
    net = float(s.get("positions", {}).get("net") or 0)
    errs = int(s.get("errors") or 0)
    mode = str(s.get("mode") or os.environ.get("STRATEGY_MODE", "hedge"))
    rounds = int(s["stats"].get("rounds") or 0)

    # 边际(与上次巡检比): Δ磨损/Δ量,10分钟窗口的即时速率
    last = {}
    try:
        with open(LAST_FILE) as f:
            last = json.load(f)
    except Exception:
        pass
    d_wear = d_vol = None
    if last.get("vol") is not None:
        d_wear = wear - float(last["wear"])
        d_vol = vol_popdex - float(last["vol"])
    m_rate = (d_wear / d_vol * 10000) if (d_vol is not None and d_vol > 2000) else None

    summary = (f"{time.strftime('%m-%d %H:%M')} [{mode}] 轮数={rounds} "
               f"Popdex量=${vol_popdex:,.0f} 磨损=${wear:.2f} "
               f"累计={(f'{rate_cum:.2f}/万' if rate_cum is not None else '样本不足')} "
               f"10分钟边际={(f'{m_rate:.2f}/万' if m_rate is not None else '-')} "
               f"敞口={net} errors={errs}")

    if BASE_EQ <= 0:
        notify(f"OK(未设基线,仅上报) {summary}"); _save_exit(last, wear, vol_popdex)

    # ---------- 3) 硬熔断 ----------
    vol_mode = mode == "volume"
    if wear > WEAR_ABS_STOP:
        _stop_bot(f"累计磨损${wear:.2f}超预算${WEAR_ABS_STOP:.0f} | {summary}")
        _save_exit(last, wear, vol_popdex)
    if m_rate is not None and m_rate > RATE_HARD_STOP:
        _stop_bot(f"10分钟边际磨损{m_rate:.2f}/万超上限{RATE_HARD_STOP}/万 | {summary}")
        _save_exit(last, wear, vol_popdex)
    if errs >= 3:
        _stop_bot(f"连续错误{errs}次 | {summary}")
        _save_exit(last, wear, vol_popdex)
    if (not vol_mode) and abs(net) > 0.006:
        _stop_bot(f"净敞口{net}异常(对冲模式) | {summary}")
        _save_exit(last, wear, vol_popdex)

    # ---------- 4) 建议提醒(30分钟同类去重) ----------
    now = time.time()
    adv_ts = last.get("adv_ts") or {}

    def advise(key: str, text: str) -> None:
        if now - float(adv_ts.get(key) or 0) > ADV_SUPPRESS_SEC:
            adv_ts[key] = now
            notify(text)

    if not vol_mode and m_rate is not None and m_rate > FLOOR_ADVICE:
        advise("floor", f"⚠️ 对冲模式边际磨损 {m_rate:.2f}/万 已超过刷量模式地板($2.5/万),"
                        f"当前基差环境刷对冲不划算,建议切刷量模式(改.env: STRATEGY_MODE=volume) | {summary}")
    if not vol_mode and d_vol is not None and d_vol < STARVE_USD:
        advise("starve", f"⚠️ 对冲模式空转:10分钟只刷了${d_vol:,.0f}量(基差门控卡住),"
                         f"如急需量建议切刷量模式 | {summary}")
    if vol_mode and m_rate is not None and m_rate > FLOOR_ADVICE:
        advise("vexp", f"⚠️ 刷量模式边际磨损 {m_rate:.2f}/万 高于常规地板$2.5/万,"
                       f"检查滑点设置/VIP费率 | {summary}")

    notify(f"✅ {summary}")
    with open(LAST_FILE, "w") as f:
        json.dump({"ts": now, "wear": wear, "vol": vol_popdex, "adv_ts": adv_ts}, f)
except Exception as e:
    print(f"ERROR 计算失败: {e}(不停止)")
