"""
Adapted from test_interproc.py (new_query_optimizer/query_optimizer).

Query results are fetched in one function and passed as arguments into a
second function that does the actual work, rather than everything living
in a single function body.

Four scenarios, each isolated in its own pair of functions so they can be
targeted individually with --func:
  - true_positive_match / true_positive_main: query results are fetched,
    then passed into a function that joins them with a nested loop
  - non_db_args_*: plain lists passed in, not DB results
  - db_not_passed_to_loop_*: DB results are fetched but never passed into
    the function that loops
  - cross_func_load_all / cross_func_match: the same variable names are
    reused across two unrelated functions
"""
import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()


def true_positive_match(orders, customers):
    results = []
    for order in orders:
        for customer in customers:
            if order["customer_id"] == customer["customer_id"]:
                results.append((order, customer))
    return results


def true_positive_main():
    cursor.execute("SELECT * FROM orders")
    all_orders = cursor.fetchall()
    cursor.execute("SELECT * FROM customers")
    all_customers = cursor.fetchall()
    return true_positive_match(all_orders, all_customers)


def non_db_args_match(a, b):
    for x in a:
        for y in b:
            if x["k"] == y["k"]:
                pass


def non_db_args_caller():
    list1 = [1, 2, 3]
    list2 = [4, 5, 6]
    non_db_args_match(list1, list2)


def db_not_passed_to_loop_helper(x):
    return x


def db_not_passed_to_loop_main():
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    return db_not_passed_to_loop_helper(orders)


def cross_func_load_all():
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    return orders, customers


def cross_func_match(orders, customers):
    results = []
    for order in orders:
        for customer in customers:
            if order["customer_id"] == customer["customer_id"]:
                results.append((order, customer))
    return results
