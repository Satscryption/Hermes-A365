# Disposable test VM
Ubuntu VM, public IP, and edge network for Hermes-A365. Caddy forwards only `/api/messages` to loopback. Needs Terraform 1.11 or later, Azure access, and B-series quota.

```bash
terraform init
terraform plan -var-file=local.auto.tfvars
terraform apply -var-file=local.auto.tfvars
```
Untracked tfvars: `name_prefix`, `dns_label`, `admin_ssh_public_key`, `hermes_a365_ref`, `hermes_agent_ref`, plus optional sizing pins. Inbound 80/443 only; no public health route (502 on `/api/messages` while gateway is stopped).

After apply: inject secrets on the VM, start `hermes-gateway`, create bot/channel/identity, model login—never in tfvars/state/repo. Teardown: remove manual bot/identity, then `terraform destroy`. Network Watcher RG may appear unmanaged.
