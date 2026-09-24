import sqlite3
from datetime import datetime

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT order_id,
           order_purchase_timestamp,
           order_delivered_customer_date,
           order_estimated_delivery_date
    FROM olist_orders_dataset
""")

orders = cursor.fetchall()
results = []

for order in orders:
    order_id = order[0]
    purchase_date = order[1]
    delivered_date = order[2]
    estimated_date = order[3]

    if purchase_date is None:
        continue

    purchase = datetime.strptime(
        purchase_date,
        "%Y-%m-%d %H:%M:%S"
    )

    if delivered_date is not None:
        delivered = datetime.strptime(
            delivered_date,
            "%Y-%m-%d %H:%M:%S"
        )
        delivery_days = (delivered - purchase).days
    else:
        delivery_days = 0

    if estimated_date is not None and delivered_date is not None:
        estimated = datetime.strptime(
            estimated_date,
            "%Y-%m-%d %H:%M:%S"
        )
        late = delivered > estimated
    else:
        late = False

    results.append({
        "order_id": order_id,
        "delivery_days": delivery_days,
        "late": late
    })

late_orders = []

for result in results:
    if result["late"]:
        late_orders.append(result)

print("Total late orders:", len(late_orders))
conn.close()
