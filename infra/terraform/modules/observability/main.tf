resource "azurerm_log_analytics_workspace" "this" {
  count               = var.enabled ? 1 : 0
  name                = "${var.name}-logs"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.1
  tags                = var.tags
}
resource "azurerm_monitor_diagnostic_setting" "metrics" {
  for_each                   = var.enabled ? { mysql = var.mysql_id, lb = var.lb_id } : {}
  name                       = "${var.name}-${each.key}-metrics"
  target_resource_id         = each.value
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this[0].id
  enabled_metric { category = "AllMetrics" }
}
