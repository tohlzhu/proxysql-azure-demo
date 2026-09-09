datadir="/var/lib/proxysql"
admin_variables =
{
    admin_credentials=${ADMIN_CREDENTIALS}
    mysql_ifaces="127.0.0.1:6032"
}
mysql_variables =
{
    threads=2
    max_connections=256
    interfaces="0.0.0.0:6033"
    default_schema=${MYSQL_DATABASE}
    server_version="8.0.41"
    monitor_username=${MYSQL_MONITOR_USER}
    monitor_password=${MYSQL_MONITOR_PASSWORD}
    monitor_connect_interval=2000
    monitor_ping_interval=2000
    connect_timeout_server=2000
    connect_timeout_server_max=3000
    default_query_timeout=4000
    have_ssl=true
    ssl_p2s_ca=${MYSQL_CA_FILE}
}
mysql_servers =
(
    { address=${MYSQL_HOST}; port=${MYSQL_PORT}; hostgroup=10; max_connections=64; use_ssl=${PROXYSQL_BACKEND_TLS}; }
)
mysql_users =
(
    { username=${MYSQL_APP_USER}; password=${MYSQL_APP_PASSWORD}; default_hostgroup=10; active=1; transaction_persistent=1; }
)
mysql_query_rules =
(
    { rule_id=1; active=1; match_digest="^(DELETE|DROP|TRUNCATE|ALTER)"; error_msg="Statement blocked by demo protection rule"; apply=1; },
    { rule_id=10; active=1; match_digest="^SELECT"; destination_hostgroup=10; timeout=3000; apply=1; }
)
