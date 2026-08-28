#!/usr/bin/env bash
# Ubuntu's iptables-nft frontend expects the legacy xt_owner kernel module for
# `-m owner --uid-owner`. E2B exposes native nftables skuid matching but not that
# compatibility module. Translate the one owner-isolation chain used by
# OSWorld-V2's protected task runtime; pass every other invocation unchanged.
set -uo pipefail

STATE_DIR=/run/osworld-iptables-owner

if [ "$#" -eq 8 ] \
    && [ "$1" = "-A" ] \
    && [ "$3" = "-m" ] \
    && [ "$4" = "owner" ] \
    && [ "$5" = "--uid-owner" ] \
    && [ "$7" = "-j" ] \
    && [ "$8" = "RETURN" ]; then
    chain="$2"
    owner="$6"
    case "$chain" in TASK[0-9][0-9][0-9]_HTTP) ;; *) exec /usr/sbin/iptables "$@" ;; esac
    uid="$(id -u "$owner")" || exit 2
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$uid" > "$STATE_DIR/$chain.uid"
    exit 0
fi

if [ "$#" -eq 4 ] \
    && [ "$1" = "-A" ] \
    && [ "$3" = "-j" ] \
    && [ "$4" = "REJECT" ] \
    && [ -f "$STATE_DIR/$2.uid" ]; then
    chain="$2"
    uid="$(sed -n '1p' "$STATE_DIR/$chain.uid")"
    port="$(/usr/sbin/iptables -S OUTPUT | awk -v chain="$chain" '
        $0 ~ ("-j " chain "$") {
            for (i = 1; i <= NF; i++) if ($i == "--dport") print $(i + 1)
        }
    ' | tail -1)"
    case "$uid:$port" in *[!0-9:]*|:|*:) exit 2 ;; esac

    while /usr/sbin/iptables -C OUTPUT -o lo -p tcp --dport "$port" \
        -j "$chain" 2>/dev/null; do
        /usr/sbin/iptables -D OUTPUT -o lo -p tcp --dport "$port" -j "$chain"
    done
    /usr/sbin/iptables -F "$chain" 2>/dev/null || true
    /usr/sbin/iptables -X "$chain" 2>/dev/null || true

    /usr/sbin/nft delete table inet osworld_owner_compat 2>/dev/null || true
    /usr/sbin/nft add table inet osworld_owner_compat
    /usr/sbin/nft 'add chain inet osworld_owner_compat output { type filter hook output priority 0; policy accept; }'
    /usr/sbin/nft add rule inet osworld_owner_compat output \
        oifname lo tcp dport "$port" meta skuid != "$uid" reject
    exit 0
fi

exec /usr/sbin/iptables "$@"
