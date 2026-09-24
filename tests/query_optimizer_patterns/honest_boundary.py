"""
Adapted from test_honest_boundary.py (new_query_optimizer/query_optimizer).

Six variations on joining two (or three) prefetched tables by hand in
Python, ranging from a plain join to ones that mix in extra conditions
beyond the join itself:
  - clean_two_table_join / three_table_join / composite_key_join: the
    join condition is the only thing being checked
  - join_plus_filter: the join condition is combined with an extra filter
    in the same `if`
  - prefilter_join: a filter on the outer row guards the whole inner
    loop, before the join condition is checked
  - three_table_with_residual_filter: three tables, with an extra filter
    combined into the innermost join condition
"""
import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()


def clean_two_table_join():
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    cursor.execute("SELECT * FROM order_items")
    items = cursor.fetchall()
    results = []
    for order in orders:
        for item in items:
            if order["order_id"] == item["order_id"]:
                results.append((order, item))
    return results


def three_table_join():
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    cursor.execute("SELECT * FROM order_items")
    items = cursor.fetchall()
    results = []
    for order in orders:
        for customer in customers:
            if order["customer_id"] == customer["id"]:
                for item in items:
                    if item["order_id"] == order["id"]:
                        results.append((order, customer, item))
    return results


def composite_key_join():
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    cursor.execute("SELECT * FROM order_items")
    items = cursor.fetchall()
    results = []
    for order in orders:
        for item in items:
            if order["order_id"] == item["order_id"] and order["seller_id"] == item["seller_id"]:
                results.append((order, item))
    return results


def join_plus_filter():
    # The join condition is combined with an extra filter
    # (item["price"] > 100) in the same `if`.
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    cursor.execute("SELECT * FROM order_items")
    items = cursor.fetchall()
    results = []
    for order in orders:
        for item in items:
            if order["order_id"] == item["order_id"] and item["price"] > 100:
                results.append((order, item))
    return results


def prefilter_join():
    # A filter on the outer row (order["status"]) guards the whole inner
    # loop, before the join condition is checked.
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    cursor.execute("SELECT * FROM order_items")
    items = cursor.fetchall()
    results = []
    for order in orders:
        if order["status"] == "delivered":
            for item in items:
                if order["order_id"] == item["order_id"]:
                    results.append((order, item))
    return results


def three_table_with_residual_filter():
    # Three tables, with an extra filter (item["price"] > 100) combined
    # into the innermost join condition.
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    cursor.execute("SELECT * FROM order_items")
    items = cursor.fetchall()
    results = []
    for order in orders:
        for customer in customers:
            if order["customer_id"] == customer["customer_id"]:
                for item in items:
                    if item["order_id"] == order["order_id"] and item["price"] > 100:
                        results.append((order, customer, item))
    return results
