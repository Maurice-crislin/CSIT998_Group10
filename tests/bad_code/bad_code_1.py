import sqlite3

conn = sqlite3.connect("olist.db")
cursor = conn.cursor()

customer_ids = [
    "06b8999e2fba1a1fbc88172c00ba8bc7",
    "18955e83d337fd6b2def6b18a428ac77",
    "4e7b3e00288586ebd08712fdd0374a03"
]

customers = []

for customer_id in customer_ids:
    cursor.execute(
        f"""
        SELECT *
        FROM olist_customers_dataset
        WHERE customer_id = '{customer_id}'
        """
    )

    row = cursor.fetchone()

    if row:
        customers.append({
            "customer_id": row[0],
            "customer_city": row[3]
        })

print(customers)

conn.close()
