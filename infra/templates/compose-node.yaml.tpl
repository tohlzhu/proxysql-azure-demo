services:
  proxysql:
    image: proxysql/proxysql:2.7.3
    container_name: proxysql
    restart: unless-stopped
    ports:
      - "6033:6033"
    volumes:
      - ./proxysql.cnf:/etc/proxysql.cnf:ro
      - ./certs:/certs:ro
      - proxysql-data:/var/lib/proxysql
      - ./certs/proxysql-ca.pem:/var/lib/proxysql/proxysql-ca.pem:ro
      - ./certs/proxysql-cert.pem:/var/lib/proxysql/proxysql-cert.pem:ro
      - ./certs/proxysql-key.pem:/var/lib/proxysql/proxysql-key.pem:ro
    command: ["proxysql", "-f", "--initial", "-c", "/etc/proxysql.cnf"]
  app:
    image: proxysql-demo-app:azure
    container_name: limiter
    restart: unless-stopped
    env_file: runtime.env
    ports:
      - "8080:8080"
    volumes:
      - ./certs:/certs:ro
    init: true
volumes:
  proxysql-data:
