import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

# Anti Join with NULL
# Find orders that do not have a corresponding review
cursor.execute("""
    SELECT o.order_id
    FROM olist_orders_dataset o
    WHERE o.order_id NOT IN (
        SELECT r.order_id
        FROM olist_order_reviews_dataset r
    )
""")

orders_without_reviews = cursor.fetchall()

# Print orders without reviews
for order in orders_without_reviews:
    print(order[0])

conn.close()