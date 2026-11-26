locals {
  cloud_init = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    fqdn             = azurerm_public_ip.main.fqdn
    hermes_a365_ref  = var.hermes_a365_ref
    hermes_agent_ref = var.hermes_agent_ref
    uv_version       = var.uv_version
  })
}
resource "azurerm_linux_virtual_machine" "main" {
  name                            = "${var.name_prefix}-vm"
  location                        = var.location
  resource_group_name             = azurerm_resource_group.main.name
  size                            = var.vm_size
  admin_username                  = var.admin_username
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.main.id]
  tags                            = var.tags
  admin_ssh_key {
    username   = var.admin_username
    public_key = var.admin_ssh_public_key
  }
  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "StandardSSD_LRS"
    disk_size_gb         = 128
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = var.image_version
  }
  custom_data = base64encode(local.cloud_init)
}
resource "azurerm_dev_test_global_vm_shutdown_schedule" "main" {
  virtual_machine_id    = azurerm_linux_virtual_machine.main.id
  location              = var.location
  enabled               = true
  daily_recurrence_time = var.shutdown_time_utc
  timezone              = "UTC"
  tags                  = var.tags
  notification_settings { enabled = false }
}
