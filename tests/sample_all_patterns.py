import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()

# --- Anti-pattern (1): nested-loop manual JOIN ---
cursor.execute("SELECT * FROM orders")
orders = cursor.fetchall()

cursor.execute("SELECT * FROM customers")
customers = cursor.fetchall()

results = []
for order in orders:
    for customer in customers:
        if order['customer_id'] == customer['id']:
            results.append((order, customer))


# --- Anti-pattern (2): N+1 query (case A: flatten-merge extend, auto-rewritable) ---
cursor.execute("SELECT * FROM orders")
all_orders = cursor.fetchall()

all_items = []
for order in all_orders:
    cursor.execute(f"SELECT * FROM order_items WHERE order_id = {order['id']}")
    items = cursor.fetchall()
    all_items.extend(items)


# --- Anti-pattern (2b): N+1 query (case B: group into dict, auto-rewritable) ---
cursor.execute("SELECT * FROM orders")
orders_for_grouping = cursor.fetchall()

items_by_order = {}
for order in orders_for_grouping:
    cursor.execute(f"SELECT * FROM order_items WHERE order_id = {order['id']}")
    items = cursor.fetchall()
    items_by_order[order['id']] = items


# --- Anti-pattern (2c): N+1 query (case C: ordered append, auto-rewritable) ---
cursor.execute("SELECT * FROM orders")
orders_for_append = cursor.fetchall()

items_list = []
for order in orders_for_append:
    cursor.execute(f"SELECT * FROM order_items WHERE order_id = {order['id']}")
    items = cursor.fetchall()
    items_list.append(items)


# --- Anti-pattern (3): manual aggregation in loop (SUM) ---
cursor.execute("SELECT * FROM orders")
orders_for_sum = cursor.fetchall()

total = 0
for order in orders_for_sum:
    total += order['amount']


# --- Anti-pattern (3b): manual aggregation in loop (conditional COUNT) ---
cursor.execute("SELECT * FROM orders")
orders_for_count = cursor.fetchall()

shipped_count = 0
for order in orders_for_count:
    if order['status'] == 'shipped':
        shipped_count += 1


# --- Anti-pattern (5): filter parent rows by child existence (N+1 + existence check combo) ---
cursor.execute("SELECT * FROM customers")
customers_for_filter = cursor.fetchall()

active_customers = []
for customer in customers_for_filter:
    cursor.execute(f"SELECT * FROM orders WHERE customer_id = {customer['id']} AND status = 'shipped'")
    shipped_orders = cursor.fetchall()
    if shipped_orders:
        active_customers.append(customer)


# --- Anti-pattern (6): manual group-by accumulation in loop (should use GROUP BY) ---
cursor.execute("SELECT * FROM orders")
orders_for_groupby = cursor.fetchall()

totals_by_customer = {}
for order in orders_for_groupby:
    cid = order['customer_id']
    totals_by_customer[cid] = totals_by_customer.get(cid, 0) + order['amount']


# --- Anti-pattern (2d): N+1 query (%-formatting style, auto-rewritable) ---
cursor.execute("SELECT * FROM orders")
orders_percent = cursor.fetchall()

items_percent = []
for order in orders_percent:
    cursor.execute("SELECT * FROM order_items WHERE order_id = %d" % order['id'])
    items = cursor.fetchall()
    items_percent.extend(items)


# --- Anti-pattern (2e): N+1 query (.format() style, auto-rewritable) ---
cursor.execute("SELECT * FROM orders")
orders_format = cursor.fetchall()

items_format = []
for order in orders_format:
    cursor.execute("SELECT * FROM order_items WHERE order_id = {}".format(order['id']))
    items = cursor.fetchall()
    items_format.extend(items)


# --- Anti-pattern (7): row-by-row INSERT in loop (should use executemany) ---
new_customers = [
    {"id": 100, "email": "x@example.com"},
    {"id": 101, "email": "y@example.com"},
]
for c in new_customers:
    cursor.execute("INSERT INTO customers (id, email) VALUES (?, ?)", (c["id"], c["email"]))
cursor.execute("SELECT * FROM customers")
all_customers = cursor.fetchall()

target_email = "alice@example.com"
found = False
for customer in all_customers:
    if customer['email'] == target_email:
        found = True


# --- Anti-pattern (8): unbounded full-table query (no LIMIT) ---
# Pulls an entire table into memory just to loop over it; may be too large for big tables.
cursor.execute("SELECT * FROM audit_logs")
audit_logs = cursor.fetchall()
for entry in audit_logs:
    print(entry)


# --- Anti-pattern (9): query inside a reused/hot function ---
# get_customer() contains a query and is called from several places; it runs the query
# once per call, so if invoked frequently it consumes a lot of resources.
def get_customer(cid):
    cursor.execute(f"SELECT * FROM customers WHERE id = {cid}")
    return cursor.fetchone()

def build_report():
    a = get_customer(1)
    b = get_customer(2)
    c = get_customer(3)
