import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT order_id
    FROM olist_orders_dataset
    WHERE order_status = 'delivered'
""")

orders = cursor.fetchall()
order_totals = []

for order in orders:
    order_id = order[0]

    cursor.execute(f"""
        SELECT price
        FROM olist_order_items_dataset
        WHERE order_id = '{order_id}'
    """)

    items = cursor.fetchall()
    total = 0

    for item in items:
        price = item[0]
        total = total + price

    order_totals.append({
        "order_id": order_id,
        "total": total
    })

for result in order_totals[:20]:
    print(result)

conn.close()
