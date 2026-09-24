import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

cursor.execute("""
    SELECT customer_id, customer_unique_id
    FROM olist_customers_dataset
""")

customers = cursor.fetchall()
final_report = []

for customer in customers:
    customer_id = customer[0]
    customer_unique_id = customer[1]

    cursor.execute(f"""
        SELECT order_id
        FROM olist_orders_dataset
        WHERE customer_id = '{customer_id}'
    """)

    orders = cursor.fetchall()

    order_count = 0
    total_price = 0
    total_freight = 0
    total_payment = 0
    review_total = 0
    review_count = 0

    for order in orders:
        order_id = order[0]
        order_count += 1

        cursor.execute(f"""
            SELECT price, freight_value
            FROM olist_order_items_dataset
            WHERE order_id = '{order_id}'
        """)

        items = cursor.fetchall()

        for item in items:
            price = item[0]
            freight = item[1]

            if price is not None:
                total_price += price

            if freight is not None:
                total_freight += freight

        cursor.execute(f"""
            SELECT payment_value
            FROM olist_order_payments_dataset
            WHERE order_id = '{order_id}'
        """)

        payments = cursor.fetchall()

        for payment in payments:
            if payment[0] is not None:
                total_payment += payment[0]

        cursor.execute(f"""
            SELECT review_score
            FROM olist_order_reviews_dataset
            WHERE order_id = '{order_id}'
        """)

        reviews = cursor.fetchall()

        for review in reviews:
            if review[0] is not None:
                review_total += review[0]
                review_count += 1

    if review_count > 0:
        average_review = review_total / review_count
    else:
        average_review = 0

    if order_count > 0:
        final_report.append({
            "customer_unique_id": customer_unique_id,
            "orders": order_count,
            "total_price": total_price,
            "total_freight": total_freight,
            "total_payment": total_payment,
            "average_review": average_review
        })

for row in final_report:
    if row["total_price"] > 500:
        if row["average_review"] < 3:
            print(
                row["customer_unique_id"],
                row["orders"],
                row["total_price"],
                row["total_freight"],
                row["total_payment"],
                row["average_review"]
            )

conn.close()
