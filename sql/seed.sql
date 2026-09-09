INSERT INTO customers (customer_id, name) VALUES
    (1, 'Demo customer A'), (2, 'Demo customer B'), (3, 'Demo customer C')
ON DUPLICATE KEY UPDATE name = VALUES(name);
INSERT INTO orders (order_id, customer_id, amount) VALUES
    (1, 1, 12.50), (2, 1, 25.00), (3, 2, 18.75), (4, 3, 42.00)
ON DUPLICATE KEY UPDATE amount = VALUES(amount);
