mock_provider "azurerm" {}

variables {
  name_prefix          = "example-walk"
  dns_label            = "example-walk"
  admin_ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIORxucrWMA7qXjLipQktiRvQyFszhXiTMU/Q+HKfamXU"
  hermes_a365_ref      = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  hermes_agent_ref     = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}

run "plan_network" {
  command = plan

  override_resource {
    target          = azurerm_public_ip.main
    override_during = plan
    values = {
      fqdn = "example-walk.centralindia.cloudapp.azure.com"
    }
  }

  assert {
    condition     = azurerm_resource_group.main.location == "centralindia"
    error_message = "Default location must be centralindia."
  }

  assert {
    condition     = azurerm_public_ip.main.allocation_method == "Static" && azurerm_public_ip.main.sku == "Standard"
    error_message = "Public IP must be Static allocation with Standard SKU."
  }

  assert {
    condition = length([
      for r in azurerm_network_security_group.main.security_rule : r
      if r.access == "Allow" && r.direction == "Inbound" && r.destination_port_range == "443" && r.source_address_prefix == "Internet"
    ]) == 1
    error_message = "NSG must allow inbound TCP 443 from Internet."
  }

  assert {
    condition = length([
      for r in azurerm_network_security_group.main.security_rule : r
      if r.access == "Allow" && r.direction == "Inbound" && r.destination_port_range == "80" && r.source_address_prefix == "Internet"
    ]) == 1
    error_message = "NSG must allow inbound TCP 80 from Internet."
  }

  assert {
    condition = length([
      for r in azurerm_network_security_group.main.security_rule : r
      if r.access == "Deny" && r.direction == "Inbound" && r.priority == 4096
    ]) == 1
    error_message = "NSG must deny all inbound traffic at priority 4096."
  }

  assert {
    condition = length([
      for r in azurerm_network_security_group.main.security_rule : r
      if r.access == "Allow" && r.destination_port_range == "22"
    ]) == 0
    error_message = "NSG must not allow inbound TCP 22."
  }

  assert {
    condition = alltrue([
      for key in ["purpose", "owner", "deadline"] :
      contains(keys(azurerm_resource_group.main.tags), key) &&
      contains(keys(azurerm_virtual_network.main.tags), key) &&
      contains(keys(azurerm_network_security_group.main.tags), key) &&
      contains(keys(azurerm_public_ip.main.tags), key) &&
      contains(keys(azurerm_network_interface.main.tags), key) &&
      contains(keys(azurerm_linux_virtual_machine.main.tags), key) &&
      contains(keys(azurerm_dev_test_global_vm_shutdown_schedule.main.tags), key)
    ])
    error_message = "Every taggable resource must include purpose, owner, and deadline tags."
  }

  assert {
    condition     = azurerm_public_ip.main.domain_name_label == var.dns_label
    error_message = "Public IP domain_name_label must match dns_label."
  }

  assert {
    condition     = output.messages_endpoint == "https://${var.dns_label}.centralindia.cloudapp.azure.com/api/messages"
    error_message = "messages_endpoint must be the FQDN-derived Bot Framework URL."
  }
}

run "reject_invalid_name_prefix" {
  command = plan

  variables {
    name_prefix = "AB"
  }

  expect_failures = [
    var.name_prefix,
  ]
}

run "reject_invalid_dns_label" {
  command = plan

  variables {
    dns_label = "1bad-label"
  }

  expect_failures = [
    var.dns_label,
  ]
}
