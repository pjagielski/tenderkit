#!/usr/bin/env bash
# Shared read-only tenderkit MCP server behind nginx, on an Ubuntu host that already runs nginx and certbot.
#
#   sudo DOMAIN=mcp.example.org bash setup.sh
#
# Creates the unprivileged `tenderkit` user and /srv/tenderkit, installs tenderkit in a venv, adds systemd units
# (MCP server, fetch every 8 hours, one-off 90-day backfill), an nginx site with per-IP rate limits and a
# Let's Encrypt certificate (webroot). Safe to run again: it rewrites its own files and reloads.
set -euo pipefail

DOMAIN=${DOMAIN:?set DOMAIN, for example DOMAIN=mcp.example.org}
REF=${REF:-master}                       # tenderkit git ref to install
PORT=${PORT:-8810}                       # local port, never exposed
BACKFILL_DAYS=${BACKFILL_DAYS:-90}
WEBROOT=${WEBROOT:-/var/www/html}        # certbot webroot already served on port 80
SSH_KEYS_FROM=${SSH_KEYS_FROM:-}         # optional: copy authorized_keys from this user for maintenance
APP=/srv/tenderkit
DB=$APP/data/notices.sqlite3

[ "$(id -u)" = 0 ] || { echo 'Run as root (sudo).' >&2; exit 1; }

id -u tenderkit >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /bin/bash tenderkit
install -d -o tenderkit -g tenderkit -m 755 "$APP"
install -d -o tenderkit -g tenderkit -m 750 "$APP/data" "$APP/bin"
if [ -n "$SSH_KEYS_FROM" ]; then
  install -d -o tenderkit -g tenderkit -m 700 "$APP/.ssh"
  install -o tenderkit -g tenderkit -m 600 "/home/$SSH_KEYS_FROM/.ssh/authorized_keys" "$APP/.ssh/authorized_keys"
fi

sudo -u tenderkit python3 -m venv "$APP/venv"
sudo -u tenderkit "$APP/venv/bin/pip" install -q --upgrade pip
sudo -u tenderkit "$APP/venv/bin/pip" install -q --upgrade "tenderkit[mcp] @ git+https://github.com/pjagielski/tenderkit@$REF"
# Unreleased commits keep the version number, so --upgrade alone leaves the previous code in place.
sudo -u tenderkit "$APP/venv/bin/pip" install -q --force-reinstall --no-deps "tenderkit @ git+https://github.com/pjagielski/tenderkit@$REF"

echo 'name = "all"' > "$APP/all.toml"
chown tenderkit:tenderkit "$APP/all.toml"

cat > "$APP/bin/fetch.sh" <<EOF
#!/usr/bin/env bash
# fetch.sh [FROM [TO] [--source NAME]...]: notices with results, compact records. Default: the last 3 days, all sources.
set -euo pipefail
FROM=\${1:-\$(date -u -d '3 days ago' +%F)}
TO=\${2:-\$(date -u +%F)}
exec $APP/venv/bin/tenderkit --db $DB fetch --profile $APP/all.toml --results --no-raw --from "\$FROM" --to "\$TO" "\${@:3}"
EOF
cat > "$APP/bin/backfill.sh" <<EOF
#!/usr/bin/env bash
# Newest window first, so the server is useful after the first one. Window sizes stay under source limits:
# BZP takes 14 days; TED reads award XML for at most 1000 result notices per window (about 760 a week).
set -uo pipefail
backfill() {
  local source=\$1 days=\$2 end start stop
  end=\$(date -u +%F)
  stop=\$(date -u -d "$BACKFILL_DAYS days ago" +%F)
  while [[ "\$end" > "\$stop" ]]; do
    start=\$(date -u -d "\$end -\$((days - 1)) days" +%F)
    [[ "\$start" < "\$stop" ]] && start=\$stop
    echo "backfill \$source \$start..\$end"
    $APP/bin/fetch.sh "\$start" "\$end" --source "\$source" || echo "backfill \$source \$start..\$end failed"
    end=\$(date -u -d "\$start -1 day" +%F)
  done
}
backfill BZP 14
backfill TED 5
EOF
chown tenderkit:tenderkit "$APP/bin/fetch.sh" "$APP/bin/backfill.sh"
chmod 750 "$APP/bin/fetch.sh" "$APP/bin/backfill.sh"

SANDBOX="NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes"

cat > /etc/systemd/system/tenderkit-mcp.service <<EOF
[Unit]
Description=tenderkit read-only MCP server ($DOMAIN)
After=network-online.target

[Service]
User=tenderkit
Group=tenderkit
ExecStart=$APP/venv/bin/tenderkit --db $DB mcp --read-only --http --port $PORT --allowed-host $DOMAIN
# The database appears with the first backfill window; until then the server exits and is restarted.
Restart=always
RestartSec=15
MemoryMax=512M
CPUQuota=60%
$SANDBOX
ReadOnlyPaths=$APP

[Install]
WantedBy=multi-user.target
EOF

for unit in fetch backfill; do
  script=$([ $unit = fetch ] && echo fetch.sh || echo backfill.sh)
  cat > /etc/systemd/system/tenderkit-$unit.service <<EOF
[Unit]
Description=tenderkit $unit of BZP and TED notices for $DOMAIN
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=tenderkit
Group=tenderkit
ExecStart=$APP/bin/$script
TimeoutStartSec=$([ $unit = fetch ] && echo 2h || echo infinity)
Nice=10
IOSchedulingClass=idle
MemoryMax=768M
$SANDBOX
ReadWritePaths=$APP/data
EOF
done

cat > /etc/systemd/system/tenderkit-fetch.timer <<EOF
[Unit]
Description=Refresh tenderkit notices every 8 hours

[Timer]
OnCalendar=*-*-* 03,11,19:40:00 UTC
RandomizedDelaySec=10m
Persistent=true

[Install]
WantedBy=timers.target
EOF

cat > /etc/nginx/conf.d/tenderkit-mcp-limits.conf <<EOF
limit_req_zone \$binary_remote_addr zone=tenderkit_mcp:10m rate=60r/m;
limit_conn_zone \$binary_remote_addr zone=tenderkit_mcp_conn:10m;
EOF

SITE=/etc/nginx/sites-available/tenderkit-mcp
HTTP_BLOCK="server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $WEBROOT; }
    location / { return 301 https://$DOMAIN\$request_uri; }
}"
CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
if [ ! -f "$CERT" ]; then
  echo "$HTTP_BLOCK" > "$SITE"
  ln -sf "$SITE" /etc/nginx/sites-enabled/tenderkit-mcp
  nginx -t && systemctl reload nginx
  certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --non-interactive --keep-until-expiring
fi
cat > "$SITE" <<EOF
$HTTP_BLOCK
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name $DOMAIN;
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 64k;

    location = /mcp {
        limit_req zone=tenderkit_mcp burst=30 nodelay;
        limit_conn tenderkit_mcp_conn 5;
        limit_req_status 429;
        limit_conn_status 429;
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        proxy_buffering off;
    }
    location / { return 302 https://tenderkit.pl/; }
}
EOF
ln -sf "$SITE" /etc/nginx/sites-enabled/tenderkit-mcp
nginx -t && systemctl reload nginx

systemctl daemon-reload
systemctl enable --now tenderkit-mcp.service tenderkit-fetch.timer
[ -f "$DB" ] || systemctl start --no-block tenderkit-backfill.service

echo
echo "Done. Backfill runs in the background: journalctl -u tenderkit-backfill -f"
echo "Server: https://$DOMAIN/mcp  (status: systemctl status tenderkit-mcp)"
