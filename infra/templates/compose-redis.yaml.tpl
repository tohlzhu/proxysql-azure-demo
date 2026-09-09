services:
  redis:
    image: redis:7.4.2-alpine
    container_name: redis
    restart: unless-stopped
    ports:
      - "6379:6379"
    volumes:
      - ./redis.conf:/usr/local/etc/redis/redis.conf:ro
      - redis-data:/data
    command: ["redis-server", "/usr/local/etc/redis/redis.conf"]
  load-relay:
    image: haproxy:3.0.8
    container_name: load-relay
    restart: unless-stopped
    user: "99:99"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    ports:
      - "8081:8081"
    volumes:
      - ./haproxy-relay.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro
volumes:
  redis-data:
