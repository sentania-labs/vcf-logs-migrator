#!/bin/bash
# Copy Operations for Logs buckets from an 8.x source to a 9.x target (copy mode, source kept).
# Runs ON THE TARGET as root. Same mechanism as the stock bucket-tools-importer, minus the
# source-side `bucket-index delete`, and the password via SSHPASS env, not argv.
# usage: SSHPASS=<source root pw> bucket-copy.sh <source ip> <bucket id> [<bucket id> ...]
set -euo pipefail
SRC=$1; shift
STORE=/storage/core/loginsight/cidata/store
BI=/usr/lib/loginsight/application/sbin/bucket-index
ssh_src() { sshpass -e ssh -o StrictHostKeyChecking=accept-new root@"$SRC" "$@"; }
for id in "$@"; do
  used=$(df --output=pcent /storage/core | tail -1 | tr -dc 0-9)
  [ "$used" -gt 90 ] && { echo "stop: /storage/core at ${used}%"; exit 2; }
  [ -e "$STORE/$id" ] && { echo "skip $id: already on target"; continue; }
  t0=$(date +%s)
  ssh_src "tar -C $STORE -cf - $id" | tar -C "$STORE" -xf -
  t1=$(date +%s)
  echo y | $BI add "$id" --statuses archived >/dev/null
  t2=$(date +%s)
  echo "$(date +%T) $id copied $(du -sh $STORE/$id | cut -f1) in $((t1-t0))s, registered in $((t2-t1))s"
done
