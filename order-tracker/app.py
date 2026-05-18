from flask import Flask, render_template, request, redirect, url_for, jsonify
import sqlite3
import os
from datetime import datetime

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "orders.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                order_number TEXT NOT NULL UNIQUE,
                tracking_number TEXT,
                arrived_warehouse TEXT,
                shipped_to_belize TEXT,
                arrived_belize TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


@app.route("/")
def index():
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "")
    with get_db() as conn:
        query = "SELECT * FROM orders"
        params = []
        conditions = []
        if search:
            conditions.append(
                "(customer LIKE ? OR order_number LIKE ? OR tracking_number LIKE ?)"
            )
            like = f"%{search}%"
            params += [like, like, like]
        if status_filter == "pending_warehouse":
            conditions.append("arrived_warehouse IS NULL")
        elif status_filter == "at_warehouse":
            conditions.append("arrived_warehouse IS NOT NULL AND shipped_to_belize IS NULL")
        elif status_filter == "shipped":
            conditions.append("shipped_to_belize IS NOT NULL AND arrived_belize IS NULL")
        elif status_filter == "delivered":
            conditions.append("arrived_belize IS NOT NULL")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        orders = conn.execute(query, params).fetchall()

    stats = _get_stats()
    return render_template(
        "index.html",
        orders=orders,
        search=search,
        status_filter=status_filter,
        stats=stats,
    )


@app.route("/add", methods=["POST"])
def add_order():
    customer = request.form["customer"].strip()
    order_number = request.form["order_number"].strip()
    tracking_number = request.form.get("tracking_number", "").strip()
    notes = request.form.get("notes", "").strip()

    if not customer or not order_number:
        return redirect(url_for("index"))

    with get_db() as conn:
        try:
            conn.execute(
                """INSERT INTO orders
                   (customer, order_number, tracking_number, notes, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (customer, order_number, tracking_number or None, notes or None,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # duplicate order_number — silently skip
    return redirect(url_for("index"))


@app.route("/update/<int:order_id>", methods=["POST"])
def update_order(order_id):
    field = request.form.get("field")
    value = request.form.get("value", "").strip() or None
    allowed = {"tracking_number", "arrived_warehouse", "shipped_to_belize", "arrived_belize", "notes"}
    if field not in allowed:
        return jsonify({"error": "invalid field"}), 400
    with get_db() as conn:
        conn.execute(f"UPDATE orders SET {field} = ? WHERE id = ?", (value, order_id))
        conn.commit()
    return redirect(url_for("index"))


@app.route("/delete/<int:order_id>", methods=["POST"])
def delete_order(order_id):
    with get_db() as conn:
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()
    return redirect(url_for("index"))


def _get_stats():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM orders WHERE arrived_warehouse IS NULL").fetchone()[0]
        at_wh = conn.execute("SELECT COUNT(*) FROM orders WHERE arrived_warehouse IS NOT NULL AND shipped_to_belize IS NULL").fetchone()[0]
        shipped = conn.execute("SELECT COUNT(*) FROM orders WHERE shipped_to_belize IS NOT NULL AND arrived_belize IS NULL").fetchone()[0]
        delivered = conn.execute("SELECT COUNT(*) FROM orders WHERE arrived_belize IS NOT NULL").fetchone()[0]
    return {"total": total, "pending": pending, "at_warehouse": at_wh, "shipped": shipped, "delivered": delivered}


if __name__ == "__main__":
    init_db()
    print("Order Tracker running at http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
