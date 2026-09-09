resource "azurerm_lb" "private" {
  name                = "${var.name}-private-lb"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "Standard"
  tags                = var.tags
  frontend_ip_configuration {
    name                          = "private"
    subnet_id                     = var.subnet_id
    private_ip_address_allocation = "Static"
    private_ip_address            = "10.42.1.10"
  }
}
resource "azurerm_lb_backend_address_pool" "private" {
  name            = "nodes"
  loadbalancer_id = azurerm_lb.private.id
}
resource "azurerm_network_interface_backend_address_pool_association" "private" {
  for_each                = var.node_nic_ids
  network_interface_id    = each.value
  ip_configuration_name   = "private"
  backend_address_pool_id = azurerm_lb_backend_address_pool.private.id
}
resource "azurerm_lb_probe" "private" {
  for_each = {
    mysql = { port = 6033, protocol = "Tcp", path = null }
    http  = { port = 8080, protocol = "Http", path = "/readyz" }
  }
  name                = each.key
  loadbalancer_id     = azurerm_lb.private.id
  protocol            = each.value.protocol
  port                = each.value.port
  request_path        = each.value.path
  interval_in_seconds = 5
  number_of_probes    = 2
}
resource "azurerm_lb_rule" "private" {
  for_each                       = { mysql = 6033, http = 8080 }
  name                           = each.key
  loadbalancer_id                = azurerm_lb.private.id
  protocol                       = "Tcp"
  frontend_port                  = each.value
  backend_port                   = each.value
  frontend_ip_configuration_name = "private"
  backend_address_pool_ids       = [azurerm_lb_backend_address_pool.private.id]
  probe_id                       = azurerm_lb_probe.private[each.key].id
  disable_outbound_snat          = true
  tcp_reset_enabled              = true
  idle_timeout_in_minutes        = 4
}
# Explicit SNAT is necessary even with an internal LB on modern private subnets.
# Sharing this public LB for optional HTTP avoids the higher NAT Gateway baseline.
resource "azurerm_public_ip" "egress" {
  name                = "${var.name}-egress-ip"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}
resource "azurerm_lb" "public" {
  name                = "${var.name}-egress-lb"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku                 = "Standard"
  tags                = var.tags
  frontend_ip_configuration {
    name                 = "public"
    public_ip_address_id = azurerm_public_ip.egress.id
  }
}
resource "azurerm_lb_backend_address_pool" "outbound" {
  name            = "outbound"
  loadbalancer_id = azurerm_lb.public.id
}
resource "azurerm_network_interface_backend_address_pool_association" "outbound" {
  for_each                = merge(var.node_nic_ids, { redis = var.redis_nic_id })
  network_interface_id    = each.value
  ip_configuration_name   = "private"
  backend_address_pool_id = azurerm_lb_backend_address_pool.outbound.id
}
resource "azurerm_lb_outbound_rule" "explicit" {
  name                     = "explicit-egress"
  loadbalancer_id          = azurerm_lb.public.id
  protocol                 = "All"
  backend_address_pool_id  = azurerm_lb_backend_address_pool.outbound.id
  allocated_outbound_ports = 1024
  idle_timeout_in_minutes  = 4
  tcp_reset_enabled        = true
  frontend_ip_configuration { name = "public" }
}
resource "azurerm_lb_backend_address_pool" "http" {
  count           = var.public_http_enabled ? 1 : 0
  name            = "http-nodes-only"
  loadbalancer_id = azurerm_lb.public.id
}
resource "azurerm_network_interface_backend_address_pool_association" "http" {
  for_each                = var.public_http_enabled ? var.node_nic_ids : {}
  network_interface_id    = each.value
  ip_configuration_name   = "private"
  backend_address_pool_id = azurerm_lb_backend_address_pool.http[0].id
}
resource "azurerm_lb_probe" "http" {
  count               = var.public_http_enabled ? 1 : 0
  name                = "http-ready"
  loadbalancer_id     = azurerm_lb.public.id
  protocol            = "Http"
  port                = 8080
  request_path        = "/readyz"
  interval_in_seconds = 5
  number_of_probes    = 2
}
resource "azurerm_lb_rule" "http" {
  count                          = var.public_http_enabled ? 1 : 0
  name                           = "restricted-http"
  loadbalancer_id                = azurerm_lb.public.id
  protocol                       = "Tcp"
  frontend_port                  = 8080
  backend_port                   = 8080
  frontend_ip_configuration_name = "public"
  backend_address_pool_ids       = [azurerm_lb_backend_address_pool.http[0].id]
  probe_id                       = azurerm_lb_probe.http[0].id
  disable_outbound_snat          = true
  tcp_reset_enabled              = true
}
