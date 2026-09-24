def get_customers(db):
    rows = db.execute("SELECT * FROM customers")
    out = []
    for r in rows:
        out.append({"id": r.customer_id, "city": r.customer_city})
    return out
