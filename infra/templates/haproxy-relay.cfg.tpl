global
    maxconn 256

defaults
    mode http
    timeout connect 3s
    timeout client 10s
    timeout server 10s

frontend query_relay
    bind :8081
    acl query_method method POST
    acl query_path url -m str /query
    http-request deny deny_status 403 unless query_method query_path
    default_backend fixed_internal_lb

backend fixed_internal_lb
    http-request set-header Host ${LB_ENDPOINT}
    server internal_lb ${LB_ENDPOINT}
