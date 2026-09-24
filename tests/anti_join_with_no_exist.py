import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()


# anti_join_empty_check: check orders once for each customer within the loop 
# use `if not orders`
def anti_join_empty_check(): 
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    results = []
    for customer in customers:
        cursor.execute(f"SELECT * FROM orders WHERE customer_id = {customer['customer_id']}")
        orders = cursor.fetchall()
        if not orders:
            results.append(customer)
    return results


# anti_join_fetchone_none: check orders once for each customer within the loop
# use `fetchone() is None`
def anti_join_fetchone_none():
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    results = []
    for customer in customers:
        cursor.execute(f"SELECT * FROM orders WHERE customer_id = {customer['customer_id']}")
        first_order = cursor.fetchone()
        if first_order is None:
            results.append(customer)
    return results


# anti_join_count_zero: Check COUNT(*) once per customer
# keep only if equal to 0
def anti_join_count_zero():
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    results = []
    for customer in customers:
        cursor.execute(f"SELECT COUNT(*) FROM orders WHERE customer_id = {customer['customer_id']}")
        order_count = cursor.fetchone()[0]
        if order_count == 0:
            results.append(customer)
    return results


# anti_join_nested_loop_flag: Fetch both tables first, 
# use nested loops with a found flag
def anti_join_nested_loop_flag():
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    results = []
    for customer in customers:
        found = False
        for order in orders:
            if order["customer_id"] == customer["customer_id"]:
                found = True
                break
        if not found:
            results.append(customer)
    return results

# anti_join_for_else:  Fetch both tables first,
# use Python's `for ... else`
def anti_join_for_else():
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    results = []
    for customer in customers:
        for order in orders:
            if order["customer_id"] == customer["customer_id"]:
                break
        else:
            results.append(customer)
    return results


# anti_join_set_membership: Put the order's Customer ID into a set and 
# compare using 'not in'
def anti_join_set_membership():
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    cursor.execute("SELECT * FROM orders")
    orders = cursor.fetchall()
    ordered_ids = set()
    for order in orders:
        ordered_ids.add(order["customer_id"])
    results = []
    for customer in customers:
        if customer["customer_id"] not in ordered_ids:
            results.append(customer)
    return results

# anti_join_with_child_filter: N+1, 
# subquery has an additional status = 'delivered' condition
def anti_join_with_child_filter():
    # "Customers with no DELIVERED orders" -- the status condition belongs
    # inside the NOT EXISTS subquery, not in the outer WHERE.
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    results = []
    for customer in customers:
        cursor.execute(
            f"SELECT * FROM orders WHERE customer_id = {customer['customer_id']} AND status = 'delivered'"
        )
        delivered_orders = cursor.fetchall()
        if not delivered_orders:
            results.append(customer)
    return results

# semi_join_control: Opposite condition ( if orders )
# should be rewritten as EXISTS
def semi_join_control():
    # Negative control: keeps customers who DO have orders (EXISTS).
    # Same loop shape as anti_join_empty_check, opposite condition.
    cursor.execute("SELECT * FROM customers")
    customers = cursor.fetchall()
    results = []
    for customer in customers:
        cursor.execute(f"SELECT * FROM orders WHERE customer_id = {customer['customer_id']}")
        orders = cursor.fetchall()
        if orders:
            results.append(customer)
    return results
