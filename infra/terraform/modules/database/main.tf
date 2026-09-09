resource "azurerm_private_dns_zone" "mysql" {
  name                = "${var.name}.mysql.database.azure.com"
  resource_group_name = var.resource_group_name
  tags                = var.tags
}
resource "azurerm_private_dns_zone_virtual_network_link" "mysql" {
  name                  = "${var.name}-mysql-dns-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.mysql.name
  virtual_network_id    = var.vnet_id
  registration_enabled  = false
  tags                  = var.tags
}
resource "azurerm_mysql_flexible_server" "this" {
  name                         = "${var.name}-mysql"
  resource_group_name          = var.resource_group_name
  location                     = var.location
  administrator_login          = var.admin_user
  administrator_password       = var.admin_password
  sku_name                     = var.sku
  version                      = "8.0.21"
  delegated_subnet_id          = var.subnet_id
  private_dns_zone_id          = azurerm_private_dns_zone.mysql.id
  backup_retention_days        = 7
  geo_redundant_backup_enabled = false
  tags                         = var.tags
  storage {
    size_gb            = 20
    auto_grow_enabled  = false
    io_scaling_enabled = false
  }
  dynamic "high_availability" {
    for_each = var.ha_enabled ? [1] : []
    content {
      mode = "ZoneRedundant"
    }
  }
  lifecycle {
    precondition {
      condition     = !var.ha_enabled || !startswith(var.sku, "B_")
      error_message = "Burstable MySQL does not support HA; select a General Purpose SKU."
    }
  }
  depends_on = [azurerm_private_dns_zone_virtual_network_link.mysql]
}
resource "azurerm_mysql_flexible_server_configuration" "tls" {
  name                = "require_secure_transport"
  resource_group_name = var.resource_group_name
  server_name         = azurerm_mysql_flexible_server.this.name
  value               = "ON"
}
resource "azurerm_mysql_flexible_database" "demo" {
  name                = "ratelimitdemo"
  resource_group_name = var.resource_group_name
  server_name         = azurerm_mysql_flexible_server.this.name
  charset             = "utf8mb4"
  collation           = "utf8mb4_unicode_ci"
}
