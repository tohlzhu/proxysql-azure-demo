output "node_nic_ids" { value = { for key in ["node-1", "node-2"] : key => azurerm_network_interface.this[key].id } }
output "redis_nic_id" { value = azurerm_network_interface.this["redis"].id }
output "node_vm_names" { value = { for key in ["node-1", "node-2"] : key => azurerm_linux_virtual_machine.this[key].name } }
output "redis_vm_name" { value = azurerm_linux_virtual_machine.this["redis"].name }
output "node_private_ips" { value = { for key in ["node-1", "node-2"] : key => azurerm_network_interface.this[key].private_ip_address } }
output "redis_private_ip" { value = azurerm_network_interface.this["redis"].private_ip_address }
