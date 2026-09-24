import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT order_id,
           customer_id,
           order_status,
           order_purchase_timestamp
    FROM olist_orders_dataset
""")

orders = cursor.fetchall()

delivered_orders = []

for order in orders:
    order_id = order[0]
    customer_id = order[1]
    status = order[2]
    purchase_date = order[3]

    if status == "delivered":
        delivered_orders.append({
            "order_id": order_id,
            "customer_id": customer_id,
            "purchase_date": purchase_date
        })

print(len(delivered_orders))
conn.close()
