#!/bin/bash
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
export HOME="${HOME:-/root}"
cd /root/trader || exit 1
set -a; . ./.env 2>/dev/null; set +a
exec 9>/tmp/mean_rev_ft.lock
flock -n 9 || exit 0
echo "==== $(date -u +%FT%TZ) mean_rev_ft run ====" >> /root/trader/loop_trader_data_meanrev/cron.log
/root/trader/.venv/bin/python3 /root/trader/mean_rev_ft.py >> /root/trader/loop_trader_data_meanrev/cron.log 2>&1
