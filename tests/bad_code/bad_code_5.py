import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT customer_id, customer_unique_id
    FROM olist_customers_dataset
""")

customers = cursor.fetchall()

cursor.execute("""
    SELECT order_id, customer_id
    FROM olist_orders_dataset
""")

orders = cursor.fetchall()

customer_spending = []

for customer in customers:
    customer_id = customer[0]
    customer_unique_id = customer[1]
    total = 0

    for order in orders:
        order_id = order[0]
        order_customer_id = order[1]

        if order_customer_id == customer_id:
            cursor.execute(f"""
                SELECT price
                FROM olist_order_items_dataset
                WHERE order_id = '{order_id}'
            """)

            items = cursor.fetchall()

            for item in items:
                total += item[0]

    customer_spending.append({
        "customer_unique_id": customer_unique_id,
        "total_spending": total
    })

print(customer_spending[:20])
conn.close()
