locals {
  machines = {
    node-1 = { size = var.node_vm_size, ip = "10.42.1.11", subnet = var.subnet_id, zone = try(var.node_zones[0], null) }
    node-2 = { size = var.node_vm_size, ip = "10.42.1.12", subnet = var.subnet_id, zone = try(var.node_zones[1], null) }
    redis  = { size = var.redis_vm_size, ip = "10.42.3.10", subnet = var.redis_subnet_id, zone = null }
  }
}
resource "azurerm_network_interface" "this" {
  for_each            = local.machines
  name                = "${var.name}-${each.key}-nic"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
  ip_configuration {
    name                          = "private"
    subnet_id                     = each.value.subnet
    private_ip_address_allocation = "Static"
    private_ip_address            = each.value.ip
  }
}
resource "azurerm_linux_virtual_machine" "this" {
  for_each                        = local.machines
  name                            = "${var.name}-${each.key}"
  resource_group_name             = var.resource_group_name
  location                        = var.location
  size                            = each.value.size
  zone                            = each.value.zone
  admin_username                  = "demoadmin"
  disable_password_authentication = true
  network_interface_ids           = [azurerm_network_interface.this[each.key].id]
  custom_data                     = filebase64("${path.module}/../../../templates/cloud-init.yaml")
  tags                            = var.tags
  admin_ssh_key {
    username   = "demoadmin"
    public_key = var.ssh_public_key
  }
  os_disk {
    name                 = "${var.name}-${each.key}-osdisk"
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 30
  }
  source_image_reference {
    publisher = "Canonical"
    offer     = "ubuntu-24_04-lts"
    sku       = "server"
    version   = "latest"
  }
  boot_diagnostics {}
}
