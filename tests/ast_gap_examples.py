import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()

# --- Gap (A): parameterized query via execute(sql, params) -- the *safe*,
# recommended way to avoid SQL injection. The placeholder ('?') carries no
# textual trace of which variable fills it -- that link only exists in the
# second execute() argument, which the AST builder never looks at.
cursor.execute("SELECT * FROM orders")
orders_a = cursor.fetchall()

items_a = []
for order in orders_a:
    cursor.execute("SELECT * FROM order_items WHERE order_id = ?", (order["id"],))
    items_a.extend(cursor.fetchall())


# --- Gap (B): same idiom, DB-API %s paramstyle (e.g. psycopg2/MySQLdb style)
cursor.execute("SELECT * FROM customers")
customers_b = cursor.fetchall()

orders_b = []
for customer in customers_b:
    cursor.execute("SELECT * FROM orders WHERE customer_id = %s", (customer["id"],))
    orders_b.extend(cursor.fetchall())


# --- Gap (C): SQL built via string concatenation (+), not f-string/%/```.format()```
cursor.execute("SELECT * FROM orders")
orders_c = cursor.fetchall()

items_c = []
for order in orders_c:
    cursor.execute("SELECT * FROM order_items WHERE order_id = " + str(order["id"]))
    items_c.extend(cursor.fetchall())


# --- Gap (D): looping over .items() (a call, not a bare Name) as the
# iteration source -- the outer query's result variable is never recognized
# as the thing being iterated.
cursor.execute("SELECT * FROM customers")
customers_d = {row["id"]: row for row in cursor.fetchall()}

orders_d = []
for cid, customer in customers_d.items():
    cursor.execute(f"SELECT * FROM orders WHERE customer_id = {cid}")
    orders_d.extend(cursor.fetchall())
