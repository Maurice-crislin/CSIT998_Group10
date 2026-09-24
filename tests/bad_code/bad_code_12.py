import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("SELECT order_id, customer_id, order_status FROM olist_orders_dataset")
orders = cursor.fetchall()
results = []
for order in orders:
    cursor.execute(f"SELECT * FROM olist_order_items_dataset WHERE order_id = '{order[0]}'")
    items = cursor.fetchall()
    if not items:
        results.append(order)

print(len(results))
print(results[:5])
conn.close()
