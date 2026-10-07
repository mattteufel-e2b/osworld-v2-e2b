#!/usr/bin/env bash
# Tasks 103/104 invoke `freecad`; their evaluators invoke `freecadcmd`. The
# official AppImage bundles its own OpenCASCADE (7.8.1) so it cannot conflict
# with the KiCad PPA's libocct 7.6; extracted at build time because the guest
# has no FUSE. AppRun execs usr/bin/$1 when that name exists there, so passing
# the invoked name through lets /usr/local/bin/freecadcmd (a symlink to this
# file) reach the AppImage's freecadcmd.
exec /opt/freecad/squashfs-root/AppRun "$(basename "$0")" "$@"
