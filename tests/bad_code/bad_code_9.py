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

cursor.execute("""
    SELECT order_id, product_id, price
    FROM olist_order_items_dataset
""")

order_items = cursor.fetchall()

cursor.execute("""
    SELECT product_id, product_category_name
    FROM olist_products_dataset
""")

products = cursor.fetchall()

results = []

for customer in customers:
    customer_id = customer[0]
    customer_unique_id = customer[1]
    total_spending = 0

    for order in orders:
        order_id = order[0]
        order_customer_id = order[1]

        if order_customer_id != customer_id:
            continue

        for item in order_items:
            item_order_id = item[0]
            product_id = item[1]
            price = item[2]

            if item_order_id != order_id:
                continue

            for product in products:
                product_id_db = product[0]
                category = product[1]

                if product_id_db != product_id:
                    continue

                if category == "beleza_saude":
                    if price is not None:
                        total_spending += price

    if total_spending > 100:
        results.append({
            "customer_unique_id": customer_unique_id,
            "total_spending": total_spending
        })

print(results[:20])
conn.close()
