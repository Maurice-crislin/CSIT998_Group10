import sqlite3

conn = sqlite3.connect("shop.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT customer_id
    FROM orders
""")

customers = cursor.fetchall()

customer_max_prices = []

for customer in customers:
    customer_id = customer[0]

    cursor.execute(f"""
        SELECT price
        FROM order_items
        WHERE customer_id = '{customer_id}'
    """)

    items = cursor.fetchall()

    max_price = None

    for item in items:
        price = item[0]

        if max_price is None or price > max_price:
            max_price = price

    customer_max_prices.append({
        "customer_id": customer_id,
        "max_price": max_price
    })

for result in customer_max_prices:
    print(result)

conn.close()