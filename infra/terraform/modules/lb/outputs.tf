output "private_ip" { value = azurerm_lb.private.frontend_ip_configuration[0].private_ip_address }
output "public_ip" { value = azurerm_public_ip.egress.ip_address }
output "private_lb_id" { value = azurerm_lb.private.id }
