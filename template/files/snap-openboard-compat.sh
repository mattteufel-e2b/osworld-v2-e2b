#!/bin/sh
set -eu

# OSWorld task 093 was authored against the upstream image's OpenBoard snap.
# Ubuntu 22.04 provides the same application as a distro package, while E2B's
# container-native guest intentionally has no snapd. Expose only the two snap
# operations that task setup probes; every other command fails closed.
if [ "$1" = "list" ] && [ "${2:-}" = "openboard" ] && [ "$#" -eq 2 ]; then
  test -x /snap/bin/openboard
  printf '%s\n' 'Name       Version  Rev  Tracking       Publisher  Notes'
  printf '%s\n' 'openboard  1.6.1    e2b  latest/stable  ubuntu    -'
  exit 0
fi

if [ "$1" = "install" ] && [ "${2:-}" = "openboard" ]; then
  test -x /snap/bin/openboard
  case " ${3:-} " in
    '  '|*' --channel=latest/stable '*) exit 0 ;;
  esac
fi

printf 'unsupported snap command: %s\n' "$*" >&2
exit 64
