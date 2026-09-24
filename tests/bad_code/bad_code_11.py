import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

# Four separate queries, each fetched in full up front -- no query ever runs
# inside a loop, so this is NOT the N+1 shape (nothing for pattern_detector
# to correlate a loop variable against).
cursor.execute("SELECT customer_id, customer_unique_id FROM olist_customers_dataset")
customers = cursor.fetchall()

cursor.execute("SELECT order_id, customer_id, order_status FROM olist_orders_dataset")
orders = cursor.fetchall()

cursor.execute("SELECT order_id, product_id, price FROM olist_order_items_dataset")
order_items = cursor.fetchall()

cursor.execute("SELECT product_id, product_category_name FROM olist_products_dataset")
products = cursor.fetchall()

# Instead of one SQL query with three JOINs, the code manually re-implements
# a 3-level join in Python using nested dict lookups.
orders_by_customer = {}
for order in orders:
    order_id, customer_id, status = order
    orders_by_customer.setdefault(customer_id, []).append((order_id, status))

items_by_order = {}
for item in order_items:
    order_id, product_id, price = item
    items_by_order.setdefault(order_id, []).append((product_id, price))

category_by_product = {}
for product in products:
    product_id, category = product
    category_by_product[product_id] = category

report = []
for customer_id, customer_unique_id in customers:
    customer_orders = orders_by_customer.get(customer_id, [])
    total_spent = 0
    categories_bought = set()

    for order_id, status in customer_orders:
        if status != "delivered":
            continue

        for product_id, price in items_by_order.get(order_id, []):
            total_spent += price
            categories_bought.add(category_by_product.get(product_id))

    if total_spent > 0:
        report.append({
            "customer_unique_id": customer_unique_id,
            "total_spent": total_spent,
            "categories": list(categories_bought)
        })

conn.close()