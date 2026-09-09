resource "azurerm_virtual_network" "this" {
  name                = "${var.name}-vnet"
  resource_group_name = var.resource_group_name
  location            = var.location
  address_space       = ["10.42.0.0/16"]
  tags                = var.tags
}
resource "azurerm_subnet" "database" {
  name                            = "${var.name}-database"
  resource_group_name             = var.resource_group_name
  virtual_network_name            = azurerm_virtual_network.this.name
  address_prefixes                = ["10.42.2.0/24"]
  default_outbound_access_enabled = false
  delegation {
    name = "mysql"
    service_delegation {
      name    = "Microsoft.DBforMySQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}
resource "azurerm_subnet" "nodes" {
  name                            = "${var.name}-nodes"
  resource_group_name             = var.resource_group_name
  virtual_network_name            = azurerm_virtual_network.this.name
  address_prefixes                = ["10.42.1.0/24"]
  default_outbound_access_enabled = false
}
resource "azurerm_subnet" "redis" {
  name                            = "${var.name}-redis"
  resource_group_name             = var.resource_group_name
  virtual_network_name            = azurerm_virtual_network.this.name
  address_prefixes                = ["10.42.3.0/24"]
  default_outbound_access_enabled = false
}
resource "azurerm_network_security_group" "nodes" {
  name                = "${var.name}-nodes-nsg"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
  security_rule {
    name                       = "private-demo"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["6033", "8080"]
    source_address_prefix      = "10.42.0.0/16"
    destination_address_prefix = "10.42.1.0/24"
  }
  security_rule {
    name                       = "lb-probes"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["6033", "8080"]
    source_address_prefix      = "AzureLoadBalancer"
    destination_address_prefix = "10.42.1.0/24"
  }
  dynamic "security_rule" {
    for_each = var.public_http_enabled ? [1] : []
    content {
      name                       = "restricted-public-http"
      priority                   = 120
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = "Tcp"
      source_port_range          = "*"
      destination_port_range     = "8080"
      source_address_prefixes    = var.allowed_http_cidrs
      destination_address_prefix = "10.42.1.0/24"
    }
  }
  security_rule {
    name                       = "deny-other-inbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
  lifecycle {
    precondition {
      condition     = !var.public_http_enabled || length(var.allowed_http_cidrs) > 0
      error_message = "Public HTTP requires explicit restricted allowed_http_cidrs."
    }
  }
}
resource "azurerm_network_security_group" "redis" {
  name                = "${var.name}-redis-nsg"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
  security_rule {
    name                       = "nodes-only-redis-and-load-relay"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["6379", "8081"]
    source_address_prefix      = "10.42.1.0/24"
    destination_address_prefix = "10.42.3.0/24"
  }
  security_rule {
    name                       = "deny-other-inbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}
resource "azurerm_subnet_network_security_group_association" "nodes" {
  subnet_id                 = azurerm_subnet.nodes.id
  network_security_group_id = azurerm_network_security_group.nodes.id
}
resource "azurerm_subnet_network_security_group_association" "redis" {
  subnet_id                 = azurerm_subnet.redis.id
  network_security_group_id = azurerm_network_security_group.redis.id
}
resource "azurerm_subnet_network_security_group_association" "database" {
  subnet_id                 = azurerm_subnet.database.id
  network_security_group_id = azurerm_network_security_group.database.id
}
resource "azurerm_network_security_group" "database" {
  name                = "${var.name}-database-nsg"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = var.tags
  security_rule {
    name                       = "nodes-and-mysql-ha"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "3306"
    source_address_prefixes    = ["10.42.1.0/24", "10.42.2.0/24"]
    destination_address_prefix = "10.42.2.0/24"
  }
  security_rule {
    name                       = "mysql-platform-probes"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "3306"
    source_address_prefix      = "AzureLoadBalancer"
    destination_address_prefix = "10.42.2.0/24"
  }
  security_rule {
    name                       = "deny-other-inbound"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}
