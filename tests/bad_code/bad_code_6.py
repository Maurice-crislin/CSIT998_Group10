import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT customer_unique_id
    FROM olist_customers_dataset
""")

customers = cursor.fetchall()
results = []

for customer in customers:
    customer_unique_id = customer[0]

    cursor.execute(f"""
        SELECT COUNT(*)
        FROM olist_customers_dataset c
        JOIN olist_orders_dataset o
            ON c.customer_id = o.customer_id
        WHERE c.customer_unique_id = '{customer_unique_id}'
    """)

    order_count = cursor.fetchone()[0]

    if order_count > 2:
        results.append({
            "customer_unique_id": customer_unique_id,
            "order_count": order_count
        })

print(results)
conn.close()
