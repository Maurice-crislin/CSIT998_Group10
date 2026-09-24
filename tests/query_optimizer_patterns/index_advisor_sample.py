"""
Adapted from test_index_advisor.py (new_query_optimizer/query_optimizer) --
specifically its SAMPLE_SRC fixture.

Two independent patterns, both over the `customers` table: a join against
`orders` on customer_id/id in the first, and an unfiltered `email`
equality check in the second.
"""
import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()

cursor.execute("SELECT * FROM orders")
orders = cursor.fetchall()
cursor.execute("SELECT * FROM customers")
customers = cursor.fetchall()
results = []
for order in orders:
    for customer in customers:
        if order["customer_id"] == customer["id"]:
            results.append((order, customer))

cursor.execute("SELECT * FROM customers")
all_customers = cursor.fetchall()
found = False
for customer in all_customers:
    if customer["email"] == "a@b.com":
        found = True
