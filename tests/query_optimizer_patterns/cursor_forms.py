"""
Adapted from test_cursor_forms.py (new_query_optimizer/query_optimizer).

Four functions build the same result: two tables fetched up front, then
joined by hand in Python via a nested `for` + `if`. Each names its DB
cursor a different common way real code does: module-level `cursor`, a
custom name (`cur`), `self.cursor`, and a nested-attribute `self.db.cursor`.

Target one function at a time with --func, e.g.:
    python cli.py tests/query_optimizer_patterns/cursor_forms.py --func module_level_cursor
"""
import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()
cur = conn.cursor()


def module_level_cursor():
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    r = []
    for order in orders:
        for customer in customers:
            if order["customer_id"] == customer["customer_id"]:
                r.append((order, customer))
    return r


def custom_cursor_name():
    cur.execute("SELECT * FROM orders")
    orders = cur.fetchall()
    cur.execute("SELECT * FROM customers")
    customers = cur.fetchall()
    r = []
    for order in orders:
        for customer in customers:
            if order["customer_id"] == customer["customer_id"]:
                r.append((order, customer))
    return r


class Repository:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def run(self):
        self.cursor.execute("SELECT * FROM orders")
        orders = self.cursor.fetchall()
        self.cursor.execute("SELECT * FROM customers")
        customers = self.cursor.fetchall()
        r = []
        for order in orders:
            for customer in customers:
                if order["customer_id"] == customer["customer_id"]:
                    r.append((order, customer))
        return r


class Database:
    def __init__(self, connection):
        self.cursor = connection.cursor()


class RepositoryWithNestedAttr:
    def __init__(self, connection):
        self.db = Database(connection)

    def run(self):
        self.db.cursor.execute("SELECT * FROM orders")
        orders = self.db.cursor.fetchall()
        self.db.cursor.execute("SELECT * FROM customers")
        customers = self.db.cursor.fetchall()
        r = []
        for order in orders:
            for customer in customers:
                if order["customer_id"] == customer["customer_id"]:
                    r.append((order, customer))
        return r
