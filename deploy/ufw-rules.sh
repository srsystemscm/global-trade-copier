#!/usr/bin/env bash
#
# Firewall rules for the trade copier VPS: SSH (rate-limited), HTTP/HTTPS
# for the dashboard, and the ZMQ signal ports restricted to wherever your
# MT4 terminal actually connects from -- NOT open to the world.
#
# ZMQ PUB/SUB has no built-in authentication or encryption: anyone who can
# reach the port can publish fake trade signals or read real ones in
# plaintext. IP-allowlisting via this script is the practical mitigation
# for this phase. If your MT4 box doesn't have a static IP (most home
# connections don't), the stronger option is a VPN (WireGuard is the usual
# choice) between the MT4 machine and the VPS, with ZMQ only reachable over
# the VPN interface -- that's future hardening, not implemented here.
#
# Usage:
#   bash deploy/ufw-rules.sh <MT4_SOURCE_IP>
# (files transferred via git/rsync won't have the executable bit set,
# hence `bash script.sh` rather than `./script.sh`)
#
# NOT executed against a real VPS anywhere in this project's history.
# Review the rules below before running -- a wrong rule here can lock you
# out of SSH.

set -euo pipefail

MT4_SOURCE_IP="${1:-}"
if [ -z "${MT4_SOURCE_IP}" ]; then
    echo "Usage: $0 <MT4_SOURCE_IP>" >&2
    echo "  MT4_SOURCE_IP: the public IP your MT4 terminal(s) connect from." >&2
    echo "  Find it by browsing https://ifconfig.me from that machine." >&2
    exit 1
fi

echo "==> Default deny incoming, allow outgoing"
sudo ufw default deny incoming
sudo ufw default allow outgoing

echo "==> SSH (rate-limited against brute force)"
sudo ufw limit ssh

echo "==> HTTP/HTTPS for the dashboard"
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

echo "==> ZMQ signal ports, restricted to ${MT4_SOURCE_IP}"
sudo ufw allow from "${MT4_SOURCE_IP}" to any port 5555 proto tcp
sudo ufw allow from "${MT4_SOURCE_IP}" to any port 5557 proto tcp

echo "==> Enabling ufw"
sudo ufw --force enable
sudo ufw status verbose

cat <<EOF

==> Firewall enabled. Sanity-check before disconnecting this SSH session:
    open a SECOND terminal and confirm you can still SSH in.

If MT4 runs from more than one IP (e.g. a dynamic home IP that changes),
re-run this script with the new IP -- it's additive, old rules for a
stale IP aren't removed automatically. Check with 'sudo ufw status numbered'
and 'sudo ufw delete <n>' to clean up rules for IPs you no longer use.
EOF
