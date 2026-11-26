mock_provider "azurerm" {}

variables {
  name_prefix          = "example-walk"
  dns_label            = "example-walk"
  admin_ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIORxucrWMA7qXjLipQktiRvQyFszhXiTMU/Q+HKfamXU"
  hermes_a365_ref      = "cccccccccccccccccccccccccccccccccccccccc"
  hermes_agent_ref     = "dddddddddddddddddddddddddddddddddddddddd"
}

run "plan_vm_defaults" {
  command = plan

  override_resource {
    target          = azurerm_public_ip.main
    override_during = plan
    values = {
      fqdn = "example-walk.centralindia.cloudapp.azure.com"
    }
  }

  assert {
    condition     = azurerm_linux_virtual_machine.main.size == "Standard_B2s_v2" && azurerm_linux_virtual_machine.main.location == "centralindia"
    error_message = "VM must use default size and region."
  }

  assert {
    condition     = azurerm_linux_virtual_machine.main.disable_password_authentication
    error_message = "Password authentication must be disabled."
  }

  assert {
    condition     = azurerm_linux_virtual_machine.main.source_image_reference[0].version != "latest" && var.image_version == "24.04.202611170"
    error_message = "Image version must not be latest."
  }

  assert {
    condition     = azurerm_linux_virtual_machine.main.os_disk[0].disk_size_gb == 128 && azurerm_linux_virtual_machine.main.os_disk[0].storage_account_type == "StandardSSD_LRS"
    error_message = "OS disk must be 128 GiB StandardSSD_LRS."
  }

  assert {
    condition     = azurerm_dev_test_global_vm_shutdown_schedule.main.enabled && azurerm_dev_test_global_vm_shutdown_schedule.main.timezone == "UTC" && azurerm_dev_test_global_vm_shutdown_schedule.main.daily_recurrence_time == "1900"
    error_message = "Shutdown schedule must be enabled at 1900 UTC by default."
  }

  assert {
    condition = alltrue([
      strcontains(base64decode(azurerm_linux_virtual_machine.main.custom_data), "/api/messages"),
      strcontains(base64decode(azurerm_linux_virtual_machine.main.custom_data), "127.0.0.1:3978"),
      strcontains(base64decode(azurerm_linux_virtual_machine.main.custom_data), "admin off"),
      !strcontains(base64decode(azurerm_linux_virtual_machine.main.custom_data), "enable hermes-gateway"),
      !strcontains(base64decode(azurerm_linux_virtual_machine.main.custom_data), "start hermes-gateway"),
    ])
    error_message = "cloud-init must proxy /api/messages with admin off and must not auto-start hermes-gateway."
  }
}

run "reject_invalid_hermes_a365_ref" {
  command = plan

  variables {
    hermes_a365_ref = "not-a-commit"
  }

  expect_failures = [
    var.hermes_a365_ref,
  ]
}

run "reject_invalid_shutdown_time" {
  command = plan

  variables {
    shutdown_time_utc = "2500"
  }

  expect_failures = [
    var.shutdown_time_utc,
  ]
}

run "reject_latest_image" {
  command = plan

  variables {
    image_version = "latest"
  }

  expect_failures = [
    var.image_version,
  ]
}