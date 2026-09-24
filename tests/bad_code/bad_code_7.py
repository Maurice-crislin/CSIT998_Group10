import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT product_id, product_category_name
    FROM olist_products_dataset
    WHERE product_category_name IS NOT NULL
""")

products = cursor.fetchall()
results = []

for product in products:
    product_id = product[0]
    category = product[1]

    cursor.execute(f"""
        SELECT price
        FROM olist_order_items_dataset
        WHERE product_id = '{product_id}'
    """)

    items = cursor.fetchall()

    total_sales = 0
    item_count = 0

    for item in items:
        price = item[0]

        if price is not None:
            total_sales += price
            item_count += 1

    if total_sales > 5000:
        results.append({
            "product_id": product_id,
            "category": category,
            "total_sales": total_sales,
            "items_sold": item_count
        })

results.sort(key=lambda x: x["total_sales"], reverse=True)

print(results[:20])
conn.close()
