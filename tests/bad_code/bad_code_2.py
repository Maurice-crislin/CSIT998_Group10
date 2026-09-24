import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT customer_id, customer_unique_id
    FROM olist_customers_dataset
""")

customers = cursor.fetchall()

results = []

for customer in customers:
    customer_id = customer[0]
    customer_unique_id = customer[1]

    cursor.execute(f"""
        SELECT order_id, order_status
        FROM olist_orders_dataset
        WHERE customer_id = '{customer_id}'
    """)

    orders = cursor.fetchall()

    for order in orders:
        results.append({
            "customer_unique_id": customer_unique_id,
            "order_id": order[0],
            "status": order[1]
        })

print(len(results))
conn.close()
