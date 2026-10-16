#!/usr/bin/env bash
# Root-only systemd installation; no secrets in unit files or arguments.
set -euo pipefail
deploy_path=${1:-/opt/catchhole}
deploy_user=${2:-ubuntu}
if [[ ! "$deploy_path" =~ ^/[A-Za-z0-9_./-]+$ ]] || [[ ! "$deploy_user" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
  echo 'Invalid deployment path or user.' >&2
  exit 2
fi
test "$(id -u)" -eq 0
test -f "$deploy_path/current-chat-release/manage-ai-runtime.py"
cat > /etc/systemd/system/catchhole-retrieval-backfill.service <<EOF
[Unit]
Description=CatchHole bounded retrieval backfill and incremental indexing
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
ConditionPathExists=$deploy_path/retrieval.env

[Service]
Type=oneshot
User=$deploy_user
WorkingDirectory=$deploy_path
ExecStart=/usr/bin/python3 $deploy_path/current-chat-release/manage-ai-runtime.py run --directory $deploy_path
# The application owns a 300s work deadline. These outer bounds also cover
# Docker startup, final persistence/reporting and shutdown; an active run skips ticks.
TimeoutStartSec=900
TimeoutStopSec=45
UMask=0077
StandardOutput=journal
StandardError=journal
EOF
cat > /etc/systemd/system/catchhole-retrieval-backfill.timer <<'EOF'
[Unit]
Description=Check CatchHole missing retrieval embeddings every five minutes

[Timer]
OnCalendar=*:0/5
RandomizedDelaySec=20
Persistent=true
Unit=catchhole-retrieval-backfill.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now catchhole-retrieval-backfill.timer
# Non-blocking; systemd never starts a second instance of the same running oneshot.
systemctl start --no-block catchhole-retrieval-backfill.service
