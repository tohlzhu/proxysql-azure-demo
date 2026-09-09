output "vnet_id" { value = azurerm_virtual_network.this.id }
output "database_subnet_id" { value = azurerm_subnet.database.id }
output "nodes_subnet_id" { value = azurerm_subnet.nodes.id }
output "redis_subnet_id" { value = azurerm_subnet.redis.id }
