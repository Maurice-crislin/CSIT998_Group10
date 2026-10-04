import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT customer_id
    FROM orders
""")

customers = cursor.fetchall()

customer_totals = []

for customer in customers:
    customer_id = customer[0]
    total = 0

    cursor.execute(f"""
        SELECT price
        FROM order_items
        WHERE customer_id = '{customer_id}'
    """)

    items = cursor.fetchall()

    for item in items:
        total += item[0]

    customer_totals.append({
        "customer_id": customer_id,
        "total": total
    })

for result in customer_totals:
    print(result)

conn.close()