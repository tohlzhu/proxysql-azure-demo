variable "name" { type = string }
variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "enabled" { type = bool }
variable "mysql_id" { type = string }
variable "lb_id" { type = string }
