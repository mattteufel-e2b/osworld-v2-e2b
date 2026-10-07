#!/usr/bin/env bash
# Task 067/071 invoke `musescore`. The AppImage is extracted at build time
# (the guest has no FUSE); AppRun sets up the bundled Qt and launches
# bin/mscore4portable with the given score.
exec /opt/musescore/squashfs-root/AppRun "$@"
