"""
Adapted from test_new_patterns.py (new_query_optimizer/query_optimizer),
covering two patterns:
  (8) a query with no LIMIT or WHERE that fetches and loops over an
      entire table
  (9) a function containing a query, called from several places

Five scenarios, --func-targetable individually:
  - huge_table_query: no LIMIT or WHERE
  - huge_table_query_with_limit / huge_table_query_with_where: the same
    query bounded by a LIMIT or a WHERE clause
  - reused_function_query: a function containing a query, called three times
  - single_use_function_query: the same shape, called once
"""
import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()


def huge_table_query():
    cursor.execute("SELECT * FROM huge_logs")
    logs = cursor.fetchall()
    for log in logs:
        print(log)


def huge_table_query_with_limit():
    cursor.execute("SELECT * FROM huge_logs LIMIT 100")
    logs = cursor.fetchall()
    for log in logs:
        print(log)


def huge_table_query_with_where():
    cursor.execute("SELECT * FROM huge_logs WHERE level = 'ERROR'")
    logs = cursor.fetchall()
    for log in logs:
        print(log)


def get_user(uid):
    cursor.execute(f"SELECT * FROM users WHERE id = {uid}")
    return cursor.fetchone()


def reused_function_query():
    a = get_user(1)
    b = get_user(2)
    c = get_user(3)
    return a, b, c


def get_config():
    cursor.execute("SELECT * FROM config WHERE id = 1")
    return cursor.fetchone()


def single_use_function_query():
    cfg = get_config()
    return cfg
