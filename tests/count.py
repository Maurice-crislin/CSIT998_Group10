import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT order_id
    FROM orders
""")

orders = cursor.fetchall()

order_count = 0

for order in orders:
    order_count += 1

print(f"Total orders: {order_count}")

conn.close()