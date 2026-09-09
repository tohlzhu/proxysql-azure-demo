data "azurerm_resource_group" "shared" {
  name = var.resource_group_name
}
locals {
  tags = {
    owner      = "agent"
    task       = var.task_id
    autodelete = "true"
    project    = var.project_id
  }
}
module "network" {
  source              = "./modules/network"
  name                = var.project_id
  resource_group_name = data.azurerm_resource_group.shared.name
  location            = var.location
  tags                = local.tags
  public_http_enabled = var.public_http_enabled
  allowed_http_cidrs  = var.allowed_http_cidrs
}
module "database" {
  source              = "./modules/database"
  name                = var.project_id
  resource_group_name = data.azurerm_resource_group.shared.name
  location            = var.location
  tags                = local.tags
  subnet_id           = module.network.database_subnet_id
  vnet_id             = module.network.vnet_id
  admin_user          = var.mysql_admin_user
  admin_password      = var.mysql_admin_password
  sku                 = var.mysql_sku
  ha_enabled          = var.mysql_ha_enabled
}
module "compute" {
  source              = "./modules/compute"
  name                = var.project_id
  resource_group_name = data.azurerm_resource_group.shared.name
  location            = var.location
  tags                = local.tags
  subnet_id           = module.network.nodes_subnet_id
  redis_subnet_id     = module.network.redis_subnet_id
  node_vm_size        = var.node_vm_size
  redis_vm_size       = var.redis_vm_size
  node_zones          = var.node_zones
  ssh_public_key      = var.ssh_public_key
}
module "lb" {
  source              = "./modules/lb"
  name                = var.project_id
  resource_group_name = data.azurerm_resource_group.shared.name
  location            = var.location
  tags                = local.tags
  subnet_id           = module.network.nodes_subnet_id
  node_nic_ids        = module.compute.node_nic_ids
  redis_nic_id        = module.compute.redis_nic_id
  public_http_enabled = var.public_http_enabled
}
module "observability" {
  source              = "./modules/observability"
  name                = var.project_id
  resource_group_name = data.azurerm_resource_group.shared.name
  location            = var.location
  tags                = local.tags
  enabled             = var.observability_enabled
  mysql_id            = module.database.id
  lb_id               = module.lb.private_lb_id
}
