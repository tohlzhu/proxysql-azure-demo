variable "project_id" {
  type        = string
  description = "Unique lowercase project suffix; also the exact cleanup confirmation."
  default     = "proxysql-demo"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{3,23}$", var.project_id))
    error_message = "Use 4-24 lowercase letters, digits or hyphens, starting with a letter."
  }
}
variable "task_id" {
  type    = string
  default = "mysql-proxysql-demo"
}
variable "location" {
  type    = string
  default = "japaneast"
  validation {
    condition     = var.location == "japaneast"
    error_message = "This deployment is authorized only in Japan East."
  }
}
variable "resource_group_name" {
  type    = string
  default = "rg-dev2"
  validation {
    condition     = var.resource_group_name == "rg-dev2"
    error_message = "Only the shared authorized rg-dev2 may be used."
  }
}
variable "node_vm_size" {
  type    = string
  default = "Standard_B2ls_v2"
}
variable "redis_vm_size" {
  type    = string
  default = "Standard_B2ats_v2"
}
variable "node_zones" {
  type        = list(string)
  default     = []
  description = "plan/deploy scripts select two unrestricted zones; empty means regional placement."
  validation {
    condition     = length(var.node_zones) == 0 || (length(var.node_zones) == 2 && length(distinct(var.node_zones)) == 2 && alltrue([for z in var.node_zones : contains(["1", "2", "3"], z)]))
    error_message = "Supply no zones or two different zones from 1, 2, 3."
  }
}
variable "ssh_public_key" {
  type        = string
  description = "Public key only. No inbound SSH is permitted; management uses Azure Run Command."
}
variable "mysql_admin_user" {
  type    = string
  default = "demoadmin"
}
variable "mysql_admin_password" {
  type      = string
  sensitive = true
}
variable "mysql_app_password" {
  type        = string
  sensitive   = true
  default     = null
  description = "Runtime-only contract. Not consumed by resources; use MYSQL_APP_PASSWORD when publishing."
}
variable "mysql_monitor_password" {
  type      = string
  sensitive = true
  default   = null
}
variable "redis_password" {
  type      = string
  sensitive = true
  default   = null
}
variable "proxysql_admin_password" {
  type      = string
  sensitive = true
  default   = null
}
variable "mysql_sku" {
  type    = string
  default = "B_Standard_B1ms"
}
variable "mysql_ha_enabled" {
  type    = bool
  default = false
}
variable "public_http_enabled" {
  type    = bool
  default = false
}
variable "allowed_http_cidrs" {
  type    = list(string)
  default = []
  validation {
    condition = alltrue([for cidr in var.allowed_http_cidrs :
      can(cidrnetmask(cidr)) && try(tonumber(split("/", cidr)[1]) >= 24, false)
    ])
    error_message = "Public demo access requires explicit IPv4 /24 or narrower CIDRs; no Internet-wide access."
  }
}
variable "observability_enabled" {
  type    = bool
  default = false
}
