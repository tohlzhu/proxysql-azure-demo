output "project_id" { value = var.project_id }
output "resource_group_name" { value = data.azurerm_resource_group.shared.name }
output "location" { value = var.location }
output "node_vm_names" { value = module.compute.node_vm_names }
output "redis_vm_name" { value = module.compute.redis_vm_name }
output "node_private_ips" { value = module.compute.node_private_ips }
output "redis_private_ip" { value = module.compute.redis_private_ip }
output "mysql_host" { value = module.database.fqdn }
output "mysql_admin_user" { value = var.mysql_admin_user }
output "mysql_database" { value = "ratelimitdemo" }
output "private_mysql_endpoint" { value = "${module.lb.private_ip}:6033" }
output "private_http_url" { value = "http://${module.lb.private_ip}:8080" }
output "http_url" { value = "http://${var.public_http_enabled ? module.lb.public_ip : module.lb.private_ip}:8080" }
output "public_http_enabled" { value = var.public_http_enabled }
output "egress_public_ip" { value = module.lb.public_ip }
output "placement" {
  value = length(var.node_zones) == 2 ? "Separate availability zones: ${join(", ", var.node_zones)}" : "Regional placement: SKU validation did not select two zones; zonal HA not guaranteed."
}
output "cost_profile" {
  value = {
    node_count       = 2
    node_sku         = var.node_vm_size
    redis_sku        = var.redis_vm_size
    mysql_sku        = var.mysql_sku
    mysql_ha         = var.mysql_ha_enabled
    observability    = var.observability_enabled
    egress           = "Standard public LB explicit outbound rule + one public IPv4; no NAT Gateway."
    database_storage = "20 GiB; auto-grow disabled; backup retention 7 days"
    caution          = "3 VMs + disks, MySQL, 2 Standard LBs, IPv4, bandwidth/backup/optional logs are billable. Redis is a single point of failure. Stop/deallocate alone does not remove all charges."
  }
}
