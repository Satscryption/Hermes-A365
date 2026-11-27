mock_provider "azurerm" {}

variables {
  name_prefix          = "example-walk"
  dns_label            = "example-walk"
  admin_ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIORxucrWMA7qXjLipQktiRvQyFszhXiTMU/Q+HKfamXU"
  hermes_a365_ref      = "cccccccccccccccccccccccccccccccccccccccc"
  hermes_agent_ref     = "dddddddddddddddddddddddddddddddddddddddd"
}

run "os_disk_deleted_with_vm" {
  command = plan
  assert {
    condition     = can(regex("delete_os_disk_on_deletion\\s*=\\s*true", file("${path.module}/versions.tf")))
    error_message = "The provider must delete the OS disk with the VM."
  }
  assert {
    condition     = output.bot_resource_group_name == "example-walk-bot-rg" && output.bot_resource_group_name != azurerm_resource_group.main.name
    error_message = "bot rg separate"
  }
}

run "os_disk_name_output" {
  command = plan
  override_resource {
    target          = azurerm_public_ip.main
    override_during = plan
    values = {
      fqdn = "example-walk.centralindia.cloudapp.azure.com"
    }
  }
  override_resource {
    target          = azurerm_linux_virtual_machine.main
    override_during = plan
    values = {
      id      = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/example-walk-rg/providers/Microsoft.Compute/virtualMachines/example-walk-vm"
      os_disk = { name = "example-walk-vm_disk1_0123abcd" }
    }
  }
  assert {
    condition     = startswith(output.os_disk_name, "${azurerm_linux_virtual_machine.main.name}_")
    error_message = "os_disk_name output must be the VM's OS disk name."
  }
}
