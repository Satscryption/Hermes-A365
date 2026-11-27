output "resource_group_name" {
  description = "Deployed resource group name."
  value       = azurerm_resource_group.main.name
}

output "public_ip_address" {
  description = "Static public IPv4 on the gateway NIC."
  value       = azurerm_public_ip.main.ip_address
}

output "fqdn" {
  description = "FQDN for the public IP."
  value       = azurerm_public_ip.main.fqdn
}

output "messages_endpoint" {
  description = "Bot Framework messaging endpoint URL."
  value       = "https://${azurerm_public_ip.main.fqdn}/api/messages"
}

output "vm_name" {
  description = "Linux VM resource name."
  value       = azurerm_linux_virtual_machine.main.name
}

output "os_disk_name" {
  description = "OS disk name Azure assigned; record it before destroy for the leftover check."
  value       = azurerm_linux_virtual_machine.main.os_disk[0].name
}

output "bot_resource_group_name" {
  description = "Resource group the post-apply bot step creates outside Terraform."
  value       = "${var.name_prefix}-bot-rg"
}
