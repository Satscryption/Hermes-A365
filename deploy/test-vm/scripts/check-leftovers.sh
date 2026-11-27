#!/usr/bin/env bash
# Read-only leftover check after terraform destroy. Never changes anything.
#
# Usage: scripts/check-leftovers.sh NAME_PREFIX [OS_DISK_NAME]
# OS_DISK_NAME is the value of `terraform output -raw os_disk_name`, recorded
# before destroy. Azure names OS disks differently between builds, so disks are
# matched on that recorded name or on the VM name prefix, never on a fixed word.
# Optional env LEFTOVER_TAG=key=value also checks resources and groups by tag.
# Exit 0 when clean, 1 when anything is left, 2 on bad input.
set -euo pipefail

[[ $# -ge 1 && $# -le 2 ]] || { echo "usage: $0 NAME_PREFIX [OS_DISK_NAME]" >&2; exit 2; }
prefix=$1
disk=${2:-}
[[ $prefix =~ ^[a-z0-9-]{3,20}$ ]] || { echo "NAME_PREFIX must match ^[a-z0-9-]{3,20}$" >&2; exit 2; }
[[ -z $disk || $disk =~ ^[A-Za-z0-9_.-]{1,80}$ ]] || { echo "OS_DISK_NAME has unexpected characters" >&2; exit 2; }
left=0

found() {
  local what=$1 n=$2
  if [[ $n != 0 ]]; then
    echo "LEFTOVER $what: $n"
    left=1
  else
    echo "ok $what: 0"
  fi
}

for rg in "$prefix-rg" "$prefix-bot-rg"; do
  if [[ $(az group exists -n "$rg") == false ]]; then
    echo "ok resource group $rg: absent"
  else
    echo "LEFTOVER resource group $rg"
    left=1
  fi
done
found "resources named $prefix*" "$(az resource list --query "length([?starts_with(name, '$prefix')])" -o tsv)"
disk_q="starts_with(name, '$prefix-vm_')"
[[ -n $disk ]] && disk_q="$disk_q || name == '$disk'"
found "OS disks" "$(az resource list --resource-type Microsoft.Compute/disks --query "length([?$disk_q])" -o tsv)"
if [[ -n ${LEFTOVER_TAG:-} ]]; then
  found "resources tagged $LEFTOVER_TAG" "$(az resource list --tag "$LEFTOVER_TAG" --query 'length(@)' -o tsv)"
  found "resource groups tagged $LEFTOVER_TAG" "$(az group list --tag "$LEFTOVER_TAG" --query 'length(@)' -o tsv)"
fi
echo "NetworkWatcherRG is created by Azure per subscription and is not a leftover of this module."
exit "$left"
