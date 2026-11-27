#!/usr/bin/env bash
# Post-apply step for the test VM: create, show or remove the Azure Bot and its
# Teams channel. They live in their own resource group, outside Terraform state,
# so the module's resource group and its tags are never touched.
#
# Usage: scripts/bot-channel.sh up|status|down
# Needs: az (signed in to the target subscription), terraform, jq.
# Env for "up" (runtime only; never written to disk):
#   BOT_APP_ID     app (client) ID of the bot's Entra app registration
#   BOT_TENANT_ID  tenant ID of that app
# Optional: TF_STATE (path to a local state file), BOT_NAME.
set -euo pipefail

usage() { echo "usage: $0 up|status|down" >&2; exit 2; }
[[ $# -eq 1 ]] || usage
action=$1
module_dir=$(cd "$(dirname "$0")/.." && pwd)
tf_out() { terraform -chdir="$module_dir" output -raw ${TF_STATE:+"-state=$TF_STATE"} "$1"; }

vm_rg=$(tf_out resource_group_name)
bot_rg=$(tf_out bot_resource_group_name)
endpoint=$(tf_out messages_endpoint)
bot_name=${BOT_NAME:-${bot_rg%-rg}}
[[ $bot_rg != "$vm_rg" ]] || { echo "bot resource group must differ from $vm_rg" >&2; exit 1; }
api=2022-09-15

channel_url() {
  local sub
  sub=$(az account show --query id -o tsv)
  echo "https://management.azure.com/subscriptions/$sub/resourceGroups/$bot_rg/providers/Microsoft.BotService/botServices/$bot_name/channels/MsTeamsChannel?api-version=$api"
}

show() {
  if [[ $(az group exists -n "$bot_rg") != true ]]; then
    echo "bot resource group $bot_rg: absent"
    return 0
  fi
  echo "bot resource group $bot_rg: present"
  az bot show -g "$bot_rg" -n "$bot_name" \
    --query "{name:name, state:properties.provisioningState, endpoint:properties.endpoint, channels:properties.enabledChannels}" -o json
}

up() {
  local guid='^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'
  [[ ${BOT_APP_ID:-} =~ $guid ]] || { echo "BOT_APP_ID must be set to the app (client) ID" >&2; exit 1; }
  [[ ${BOT_TENANT_ID:-} =~ $guid ]] || { echo "BOT_TENANT_ID must be set to the tenant ID" >&2; exit 1; }

  if [[ $(az group exists -n "$bot_rg") != true ]]; then
    local location tag tags=()
    location=$(az group show -n "$vm_rg" --query location -o tsv)
    while IFS= read -r tag; do tags+=("$tag"); done < <(az group show -n "$vm_rg" --query tags -o json | jq -r 'to_entries[] | "\(.key)=\(.value)"')
    az group create -n "$bot_rg" -l "$location" --tags "${tags[@]}" -o none
  fi

  local existing
  existing=$(az bot show -g "$bot_rg" -n "$bot_name" --query properties.msaAppId -o tsv 2>/dev/null || true)
  if [[ -z $existing ]]; then
    az bot create -g "$bot_rg" -n "$bot_name" --app-type SingleTenant \
      --appid "$BOT_APP_ID" --tenant-id "$BOT_TENANT_ID" --endpoint "$endpoint" --sku F0 -o none
  elif [[ $existing != "$BOT_APP_ID" ]]; then
    echo "bot $bot_name exists with a different app ID; not changing it" >&2
    exit 1
  else
    az bot update -g "$bot_rg" -n "$bot_name" --endpoint "$endpoint" -o none
  fi

  # az bot msteams create sends a deployment environment the service rejects,
  # so the channel is written directly through the ARM API.
  local url enabled
  url=$(channel_url)
  enabled=$(az rest --method get --url "$url" --query properties.properties.isEnabled -o tsv 2>/dev/null || true)
  if [[ $enabled != true ]]; then
    az rest --method put --url "$url" --headers Content-Type=application/json -o none --body \
      '{"location":"global","properties":{"channelName":"MsTeamsChannel","properties":{"isEnabled":true,"acceptedTerms":true,"enableCalling":false,"deploymentEnvironment":"CommercialDeployment"}}}'
  fi
  show
}

down() {
  if [[ $(az group exists -n "$bot_rg") != true ]]; then
    echo "bot resource group $bot_rg: absent"
    return 0
  fi
  local other
  other=$(az resource list -g "$bot_rg" --query "[?type!='Microsoft.BotService/botServices'] | length(@)" -o tsv)
  [[ $other == 0 ]] || { echo "$bot_rg holds resources other than the bot; not deleting" >&2; exit 1; }
  az group delete -n "$bot_rg" --yes
  [[ $(az group exists -n "$bot_rg") == false ]] || { echo "$bot_rg still exists" >&2; exit 1; }
  echo "bot resource group $bot_rg: deleted"
}

case $action in
  up) up ;;
  status) show ;;
  down) down ;;
  *) usage ;;
esac
