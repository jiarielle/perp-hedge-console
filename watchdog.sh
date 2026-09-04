#!/bin/bash
# 机器人看门狗(每10分钟cron):进程死了自动拉起。
# 巡检 STOP_NOW 熔断后 1 小时内不拉起(stop.flag),尊重风控;人工清掉 flag 即恢复。
LOG="$HOME/watchdog.log"
if pgrep -f "[h]edge_bot.py" > /dev/null; then
    exit 0
fi
if [ -n "$(find "$HOME/stop.flag" -mmin -60 2>/dev/null)" ]; then
    echo "$(date '+%F %T') 熔断窗口内(stop.flag<60min),不拉起" >> "$LOG"
    exit 0
fi
cd "$HOME/perp-hedge-console" || exit 1
BOT_STATE_FILE="$HOME/bot_state.json" nohup .venv/bin/python hedge_bot.py >> "$HOME/bot.log" 2>&1 < /dev/null &
echo "$(date '+%F %T') 看门狗拉起机器人" >> "$LOG"
