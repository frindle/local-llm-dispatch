#!/bin/bash
# Remount the Unraid `data` SMB share whenever storagedemon is reachable again.
#
# WHY: the share drops whenever Unraid reboots or the SMB session dies, and
# smb-keepalive-data.sh explicitly cannot remount -- it only touches a mount
# that is already up. After the 2026-08-23 reboot the share stayed gone until
# someone noticed.
#
# DELIBERATELY does NOT touch OLLAMA_MODELS or restart Ollama. mount-ollama-models.sh
# does, and that restart once killed an in-flight dispatch and cost six models in a
# bake-off. Mounting is safe; restarting a busy inference server is not.
MOUNT_POINT="/Volumes/data"
SMB_HOST="storagedemon.local"
SMB_USER="penndalton"

mount | grep -q "on $MOUNT_POINT " && exit 0          # already mounted, nothing to do
nc -z -G3 "$SMB_HOST" 445 2>/dev/null || exit 0        # host not back yet, stay quiet

[ -d "$MOUNT_POINT" ] || mkdir -p "$MOUNT_POINT" 2>/dev/null
PASS=$(security find-internet-password -a "$SMB_USER" -s "$SMB_HOST" -w 2>/dev/null)
[ -n "$PASS" ] || { echo "$(date '+%F %T') no keychain credential"; exit 1; }
if mount_smbfs "//${SMB_USER}:${PASS}@${SMB_HOST}/data" "$MOUNT_POINT" 2>/dev/null; then
  echo "$(date '+%F %T') remounted $MOUNT_POINT"
else
  echo "$(date '+%F %T') mount attempt failed"
fi
