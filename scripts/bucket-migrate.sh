#!/bin/bash
# Prototype: stage live, commit with the target stopped. Runs ON THE TARGET as root.
#   SSHPASS=<src root pw> bucket-migrate.sh stage <source ip> <bucket id> [...]   target keeps running
#   bucket-migrate.sh commit                                                       stops target, moves + registers every staged bucket, starts target
#   bucket-migrate.sh status
# env: FAULT=transfer|staged|moved (testing: hard exit at that point), RESERVE_PCT (default 10)
set -uo pipefail
CORE=/storage/core; STORE=$CORE/loginsight/cidata/store; STAGE=$CORE/migrate-staging; J=$STAGE/journal
BI=/usr/lib/loginsight/application/sbin/bucket-index; RESERVE_PCT=${RESERVE_PCT:-10}
mkdir -p "$STAGE"
log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$J"; }
fault() { [ "${FAULT:-}" = "$1" ] && { log "FAULT injected at $1 for $2"; kill -9 $$; }; }
registered() { $BI show 2>/dev/null | grep -q "id=$1, status="; }
manifest_loc() { (cd "$1" && find . -type f -printf '%P %s\n' | LC_ALL=C sort); }
cmd=$1; shift
case $cmd in
stage)
  SRC=$1; shift
  src() { sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 root@"$SRC" "$@"; }
  manifest_src() { src "cd $STORE/$1 && find . -type f -printf '%P %s\n' | LC_ALL=C sort"; }
  src_active=$(src "systemctl is-active loginsight" 2>/dev/null)
  for id in "$@"; do
    if registered "$id"; then
      if [ -d "$STORE/$id" ] && [ "$(manifest_loc "$STORE/$id")" = "$(manifest_src "$id")" ]; then log "skip $id: registered, files match source"; continue; fi
      log "STOP $id: id registered on target with DIFFERENT content"; exit 8; fi
    if [ -d "$STORE/$id" ]; then log "note $id: in store but unregistered (crash after move); commit will verify and register"; manifest_src "$id" > "$STAGE/$id.manifest"; continue; fi
    if [ -d "$STAGE/$id" ] && [ -f "$STAGE/$id.manifest" ] && [ "$(manifest_loc "$STAGE/$id")" = "$(cat "$STAGE/$id.manifest")" ]; then log "skip $id: already staged and verified"; continue; fi
    st=$(src "$BI show 2>/dev/null | grep -o 'id=$id, status=[a-z]*'" | sed 's/.*status=//')
    [ -z "$st" ] && { log "STOP $id: not on source"; exit 4; }
    [ "$st" = active ] && [ "$src_active" = active ] && { log "STOP $id: active on source and source running"; exit 5; }
    need=$(src "du -sB1 $STORE/$id | cut -f1"); read total avail < <(df -B1 --output=size,avail $CORE | tail -1)
    part=$(du -sB1 "$STAGE/$id.partial" 2>/dev/null | cut -f1); part=${part:-0}
    after=$(( (total - avail + need - part) * 100 / total ))
    [ "$after" -gt $((100 - RESERVE_PCT)) ] && { log "STOP $id: would leave /storage/core at ${after}% (reserve ${RESERVE_PCT}%)"; exit 2; }
    rm -rf "$STAGE/$id.partial" "$STAGE/$id"; mkdir -p "$STAGE/$id.partial"; t0=$(date +%s)
    if [ "${FAULT:-}" = transfer ]; then src "tar -C $STORE/$id -cf - ." | head -c 8000000 | tar -C "$STAGE/$id.partial" -xf - 2>/dev/null; fault transfer "$id"; fi
    src "tar -C $STORE/$id -cf - ." | tar -C "$STAGE/$id.partial" -xf -
    manifest_src "$id" > "$STAGE/$id.manifest"
    [ "$(manifest_loc "$STAGE/$id.partial")" = "$(cat "$STAGE/$id.manifest")" ] || { log "STOP $id: staged copy does not match source manifest"; exit 6; }
    mv "$STAGE/$id.partial" "$STAGE/$id"; log "staged $id $(numfmt --to=iec $need) in $(( $(date +%s) - t0 ))s"; fault staged "$id" || true
  done ;;
commit)
  ids=$(cd "$STAGE" && ls *.manifest 2>/dev/null | sed 's/\.manifest$//')
  [ -z "$ids" ] && { log "commit: nothing staged"; exit 0; }
  log "commit: stopping target loginsight"; systemctl stop loginsight
  trap 'systemctl start loginsight; log "commit: target loginsight started (trap)"' EXIT
  for id in $ids; do
    if registered "$id"; then log "skip $id: already registered"; rm -f "$STAGE/$id.manifest"; continue; fi
    if [ ! -d "$STORE/$id" ]; then
      [ -d "$STAGE/$id" ] || { log "STOP $id: manifest but no staged dir"; exit 9; }
      mv "$STAGE/$id" "$STORE/$id"; fault moved "$id"; fi
    [ "$(manifest_loc "$STORE/$id")" = "$(cat "$STAGE/$id.manifest")" ] || { log "STOP $id: store copy does not match manifest"; exit 6; }
    echo y | $BI add "$id" --statuses archived >/dev/null
    registered "$id" || { log "STOP $id: add did not register"; exit 7; }
    rm -f "$STAGE/$id.manifest"; log "committed $id"
  done ;;
status)
  echo "staged:"; (cd "$STAGE" && ls -d *-*-*-*-* 2>/dev/null); echo "manifests:"; (cd "$STAGE" && ls *.manifest 2>/dev/null)
  echo "store dirs not in index:"; for d in $(cd $STORE && ls -d *-*-*-*-*); do registered "$d" || echo "  $d"; done
  echo "index entries without dir:"; $BI show 2>/dev/null | grep -o 'id=[0-9a-f-]*' | cut -d= -f2 | while read i; do [ -d "$STORE/$i" ] || echo "  $i"; done ;;
esac
