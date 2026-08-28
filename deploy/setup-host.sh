#!/usr/bin/env bash
# One-time EC2 host setup: chrony (AWS Time Sync) + DNS caching.
# Idempotent. Run:  sudo bash deploy/setup-host.sh
# Works on Ubuntu/Debian and Amazon Linux 2023.
set -euo pipefail

say() { printf '\n== %s\n' "$*"; }

# ---------- 1. chrony -> Amazon Time Sync (169.254.169.123) ----------
say "installing chrony"
if command -v apt-get >/dev/null; then
  apt-get update -qq && apt-get install -y -qq chrony >/dev/null
  CONF_DIR=/etc/chrony
  SVC=chrony
else
  dnf install -y -q chrony >/dev/null || yum install -y -q chrony >/dev/null
  CONF_DIR=/etc
  SVC=chronyd
fi

say "writing aggressive sync config"
# sourcedir may not exist on all distros; append to main conf idempotently.
CONF="$CONF_DIR/chrony.conf"
if ! grep -q '169.254.169.123' "$CONF"; then
  cat >> "$CONF" <<'EOF'

# --- kalshi-bot: aggressive sync against Amazon Time Sync ---
# Link-local, no network egress, stratum-1 fenced to AWS's atomic/GPS fleet.
server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4
# Step the clock instead of slewing when offset > 0.1s (first 3 updates),
# so a bad boot clock is fixed in seconds, not hours.
makestep 0.1 3
# Discipline the RTC too, so reboots start close to true.
rtcsync
EOF
fi

systemctl enable --now "$SVC" >/dev/null
systemctl restart "$SVC"
sleep 3

say "clock status"
chronyc tracking | grep -E 'Reference|System time|RMS offset|Stratum'

# ---------- 2. DNS caching on the host ----------
# Under network_mode: host the container uses the host resolver directly.
# systemd-resolved gives a local cache at 127.0.0.53 so repeated lookups of
# external-api.kalshi.com never leave the box.
say "enabling DNS cache (systemd-resolved)"
if systemctl list-unit-files | grep -q systemd-resolved; then
  mkdir -p /etc/systemd/resolved.conf.d
  cat > /etc/systemd/resolved.conf.d/cache.conf <<'EOF'
[Resolve]
Cache=yes
DNSStubListener=yes
EOF
  systemctl enable --now systemd-resolved >/dev/null
  systemctl restart systemd-resolved
  resolvectl status 2>/dev/null | grep -m1 'resolv.conf mode' || true
else
  echo "systemd-resolved not present; skipping (VPC resolver is ~0.5ms anyway)"
fi

say "done"
