#!/bin/bash
# psn-watch installer / updater for a fresh Ubuntu VM. Safe to re-run.
#
# First install (on the VM):
#   curl -fsSL https://raw.githubusercontent.com/Achyuth762/psn-watch/main/deploy/install.sh | sudo bash -s -- YOUR_NTFY_TOPIC
# Update later:
#   sudo psn-update
set -euo pipefail
REPO="${PSN_REPO:-https://github.com/Achyuth762/psn-watch.git}"
BRANCH="${PSN_BRANCH:-main}"
TOPIC="${1:-}"
APP=/opt/psn-watch
export DEBIAN_FRONTEND=noninteractive PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

echo "==> system packages"
command -v git >/dev/null && dpkg -s python3-venv >/dev/null 2>&1 || { apt-get update -q && apt-get install -yq git python3-venv python3-pip; }

echo "==> 2 GB swap (headless Chrome on a 1 GB VM needs it)"
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> code from $REPO ($BRANCH)"
id psnwatch >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin psnwatch
git config --global --add safe.directory "$APP"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch -q origin "$BRANCH" && git -C "$APP" reset -q --hard "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$APP"
fi
echo "    at commit $(git -C "$APP" log -1 --format='%h %s')"

echo "==> alert settings (/etc/psn-watch.env)"
if [ ! -f /etc/psn-watch.env ]; then
  printf 'NTFY_TOPIC=%s\n# TELEGRAM_BOT_TOKEN=\n# TELEGRAM_CHAT_ID=\n# HEALTHCHECK_URL=\n' "$TOPIC" > /etc/psn-watch.env
elif [ -n "$TOPIC" ]; then
  grep -q '^NTFY_TOPIC=' /etc/psn-watch.env && sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=$TOPIC|" /etc/psn-watch.env \
    || echo "NTFY_TOPIC=$TOPIC" >> /etc/psn-watch.env
fi
chgrp psnwatch /etc/psn-watch.env && chmod 640 /etc/psn-watch.env
grep -q '^NTFY_TOPIC=.' /etc/psn-watch.env || echo "    WARNING: no NTFY_TOPIC set — alerts will only go to the log"

echo "==> python packages"
cd "$APP"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> headless Chromium (first time only, ~3 min)"
mkdir -p /opt/pw-browsers /var/lib/psn-watch
if [ ! -f /opt/pw-browsers/.installed ]; then
  .venv/bin/python -m playwright install --with-deps chromium && touch /opt/pw-browsers/.installed
fi

echo "==> helper commands: psn-status, psn-update"
cat > /usr/local/bin/psn-status <<'S'
#!/bin/bash
# What the watcher sees right now + recent log.  Usage: psn-status [log-lines]
cd /opt/psn-watch && sudo -u psnwatch PSN_DATA=/var/lib/psn-watch .venv/bin/python watch.py --status
echo; systemctl --no-pager --lines=0 status psn-watch | head -4
echo; journalctl -u psn-watch --no-pager -n "${1:-25}" -o cat
S
cat > /usr/local/sbin/psn-update <<S
#!/bin/bash
# Pull the latest code from GitHub and restart.
git -C $APP fetch -q origin $BRANCH && git -C $APP reset -q --hard origin/$BRANCH && exec bash $APP/deploy/install.sh
S
chmod 755 /usr/local/bin/psn-status /usr/local/sbin/psn-update

echo "==> service"
chown -R psnwatch:psnwatch "$APP" /var/lib/psn-watch /opt/pw-browsers
cp "$APP/deploy/psn-watch.service" /etc/systemd/system/psn-watch.service
systemctl daemon-reload
systemctl enable -q psn-watch
systemctl restart psn-watch
echo "==> started. First readings in ~30 s:"
sleep 30
/usr/local/bin/psn-status 20
