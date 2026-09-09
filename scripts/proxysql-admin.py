#!/usr/bin/env python3
"""Apply template configuration or read a fixed set of nonsecret ProxySQL statistics."""

import argparse
import json
import os

import pymysql

QUERIES = {
    "runtime_mysql_servers": (
        "SELECT hostgroup_id,hostname,port,status,use_ssl FROM runtime_mysql_servers"
    ),
    "runtime_mysql_query_rules": (
        "SELECT rule_id,active,match_digest,destination_hostgroup,timeout,error_msg "
        "FROM runtime_mysql_query_rules"
    ),
    "disk_mysql_query_rules": (
        "SELECT rule_id,active,match_digest,destination_hostgroup,timeout,error_msg "
        "FROM disk.mysql_query_rules"
    ),
    "stats_mysql_connection_pool": (
        "SELECT hostgroup,srv_host,srv_port,status,ConnUsed,ConnFree,Queries "
        "FROM stats_mysql_connection_pool"
    ),
    "stats_mysql_query_digest": (
        "SELECT hostgroup,digest,count_star,sum_time,min_time,max_time "
        "FROM stats_mysql_query_digest"
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stats-only", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    connection = pymysql.connect(
        host=args.host,
        port=6032,
        user="admin",
        password=os.environ["PROXYSQL_ADMIN_PASSWORD"],
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        autocommit=True,
    )
    result = {}
    with connection, connection.cursor() as cursor:
        if not args.stats_only:
            for module in ("MYSQL VARIABLES", "MYSQL SERVERS", "MYSQL USERS", "MYSQL QUERY RULES"):
                cursor.execute(f"LOAD {module} FROM CONFIG")
                cursor.execute(f"LOAD {module} TO RUNTIME")
                cursor.execute(f"SAVE {module} TO DISK")
        for name, query in QUERIES.items():
            cursor.execute(query)
            result[name] = cursor.fetchall()
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
