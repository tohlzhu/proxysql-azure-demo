variable "name" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "subnet_id" { type = string }
variable "vnet_id" { type = string }
variable "admin_user" { type = string }
variable "admin_password" {
  type      = string
  sensitive = true
}
variable "sku" { type = string }
variable "ha_enabled" { type = bool }
