# Disposable test VM
Ubuntu VM, public IP, and edge network for Hermes-A365. Caddy forwards only `/api/messages` to loopback. Needs Terraform 1.11 or later, Azure access, and B-series quota.

```bash
terraform init
terraform plan -var-file=local.auto.tfvars
terraform apply -var-file=local.auto.tfvars
```
Untracked tfvars: `name_prefix`, `dns_label`, `admin_ssh_public_key`, `hermes_a365_ref`, `hermes_agent_ref`, plus optional sizing pins. Inbound 80/443 only; no public health route (502 on `/api/messages` while gateway is stopped).

Smoke (port 22 is closed, so use `az vm run-command invoke --command-id RunShellScript`): `cloud-init status --long` must show `status: done` and `errors: []`; any failed bootstrap step, including the venv import check, makes it `status: error`. Then confirm the venv:

```bash
t=$(mktemp -d); HOME=$t HERMES_HOME=$t/.hermes /opt/hermes/venv/bin/python -c "import hermes_cli.main, gateway.run, hermes_a365, hermes_a365.plugin" && echo venv-ok; rm -rf "$t"
```
hermes-agent is installed editable from a pinned checkout in `/opt/hermes/hermes-agent` (its build refuses wheels), then `hermes-a365[bridge]` in a second step, which raises PyJWT to the bridge floor.

## After apply
Bot and Teams channel: one repeatable command, run from this directory (needs `az`, `terraform`, and `jq`). It creates a separate resource group (`bot_resource_group_name` output, tagged like the VM's group), an F0 single-tenant Azure Bot pointing at `messages_endpoint`, and the Teams channel. It never touches the VM's resource group, so its tags stay as Terraform set them. Nothing it uses is written to tfvars, state, outputs or the repo.

```bash
BOT_APP_ID=... BOT_TENANT_ID=... scripts/bot-channel.sh up   # also: status, down
```
Set `TF_STATE=path` if you keep state outside this directory. Re-running `up` is safe: an existing group is never re-created, the endpoint is re-applied, and an enabled channel is left alone.

Still manual and never in Terraform: the Entra app registrations (agent blueprint and bot app) and their secrets, the Teams app package, secrets injected on the VM, starting `hermes-gateway`, and the model login (`xai-oauth` device-code flow on the VM).

## Teardown
1. Stop the gateway, sign out the model login, remove the manual Entra and Teams items.
2. `scripts/bot-channel.sh down` deletes the bot group (bot and channel go with it).
3. Record `terraform output -raw os_disk_name`, then `terraform destroy`. The provider deletes the OS disk with the VM.
4. `scripts/check-leftovers.sh <name_prefix> <os_disk_name>` (read-only; set `LEFTOVER_TAG=key=value` to also check by tag). OS disk names vary between builds (`<vm>_OsDisk_1_<id>` or `<vm>_disk1_<id>`), so match the recorded name or the `<vm>_` prefix, not a fixed word. Deletion of the OS disk with the VM is confirmed by step 4; the terraform test only guards the provider setting in versions.tf.

A refresh after create shows only values Azure fills in (NIC MAC and DNS servers, empty public IP zones and IP tags, empty subnet endpoint lists, the VNet's inline subnet view). That is not drift.

Network Watcher: Azure creates `NetworkWatcherRG` with `NetworkWatcher_<region>` the first time a VNet is deployed in a region. It is per subscription, not in this module's state, and is left in place on teardown.
