"""Offline-first restaurant billing desktop app with the mockup's HTML interface."""
from __future__ import annotations

import base64
import csv
import os
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import webview

APP_NAME = "Table & Thyme Billing"
GST_RATE = 0.05


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "restaurant_billing" / "ui"
    return Path(__file__).resolve().parent / "ui"


def data_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    target = base / "TableAndThymeBilling"
    (target / "photos").mkdir(parents=True, exist_ok=True)
    (target / "reports").mkdir(parents=True, exist_ok=True)
    return target


def as_dicts(rows):
    return [dict(row) for row in rows]


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.initialize()

    def initialize(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS categories(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          description TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS dishes(
          id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
          name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          price REAL NOT NULL CHECK(price >= 0), image_path TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS dining_tables(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
          service_type TEXT NOT NULL, table_name TEXT NOT NULL DEFAULT '',
          subtotal REAL NOT NULL, gst REAL NOT NULL, total REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS order_items(
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
          dish_name TEXT NOT NULL, unit_price REAL NOT NULL, quantity INTEGER NOT NULL,
          line_total REAL NOT NULL);
        """)
        if self.db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            categories = [
                ("Starters", "Small plates to begin", 0),
                ("Main course", "Freshly prepared mains", 1),
                ("Juices & drinks", "Fresh drinks and coolers", 2),
                ("Desserts", "Something sweet", 3),
            ]
            self.db.executemany("INSERT INTO categories(name,description,sort_order) VALUES(?,?,?)", categories)
            ids = {row["name"]: row["id"] for row in self.db.execute("SELECT id,name FROM categories")}
            dishes = [
                ("Starters", "Crispy Samosa", "", 90),
                ("Starters", "Paneer Tikka", "Chargrilled · Vegetarian", 240),
                ("Main course", "Butter Chicken", "Creamy tomato gravy · Mild", 320),
                ("Main course", "Hyderabadi Biryani", "Dum cooked · Aromatic", 280),
                ("Main course", "Dal Makhani", "Slow cooked · Vegetarian", 220),
                ("Main course", "Garden Fresh Salad", "Seasonal greens · Vegan", 180),
                ("Juices & drinks", "Mango Cooler", "Freshly blended", 120),
                ("Desserts", "Gulab Jamun", "Warm and sweet", 100),
            ]
            self.db.executemany("INSERT INTO dishes(category_id,name,description,price) VALUES(?,?,?,?)",
                                [(ids[c], n, d, p) for c, n, d, p in dishes])
        if self.db.execute("SELECT COUNT(*) FROM dining_tables").fetchone()[0] == 0:
            self.db.executemany("INSERT INTO dining_tables(name) VALUES(?)", [(f"Table {i:02d}",) for i in range(1, 7)])
        self.db.commit()

    def dishes(self, category_id=None, search=""):
        sql = "SELECT d.*,c.name AS category_name FROM dishes d JOIN categories c ON c.id=d.category_id WHERE 1=1"
        args = []
        if category_id is not None:
            sql += " AND d.category_id=?"; args.append(category_id)
        if search:
            sql += " AND d.name LIKE ?"; args.append(f"%{search}%")
        return self.db.execute(sql + " ORDER BY d.name", args).fetchall()

    def report(self, start="", end=""):
        clauses, args = [], []
        if start:
            clauses.append("date(created_at)>=date(?)"); args.append(start)
        if end:
            clauses.append("date(created_at)<=date(?)"); args.append(end)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        summary = self.db.execute("SELECT COUNT(*) AS orders, COALESCE(SUM(subtotal),0) AS sales, COALESCE(SUM(gst),0) AS gst FROM orders" + where, args).fetchone()
        item_clauses, item_args = [], []
        if start:
            item_clauses.append("date(o.created_at)>=date(?)"); item_args.append(start)
        if end:
            item_clauses.append("date(o.created_at)<=date(?)"); item_args.append(end)
        item_where = " WHERE " + " AND ".join(item_clauses) if item_clauses else ""
        sold = self.db.execute("SELECT i.dish_name, SUM(i.quantity) AS quantity, SUM(i.line_total) AS sales FROM order_items i JOIN orders o ON o.id=i.order_id" + item_where + " GROUP BY i.dish_name ORDER BY quantity DESC,sales DESC", item_args).fetchall()
        recent = self.db.execute("SELECT * FROM orders" + where + " ORDER BY id DESC LIMIT 200", args).fetchall()
        return dict(summary=dict(summary), sold=as_dicts(sold), orders=as_dicts(recent))


class Api:
    def __init__(self, root: Path):
        self.root = root
        self.data = data_root()
        self.store = Store(self.data / "restaurant.sqlite3")

    def _photo_url(self, path):
        if path and Path(path).is_file():
            return Path(path).resolve().as_uri()
        return ""

    def get_state(self):
        categories = as_dicts(self.store.db.execute("SELECT * FROM categories ORDER BY sort_order,name").fetchall())
        dishes = []
        for dish in self.store.dishes():
            row = dict(dish)
            row["photo_url"] = self._photo_url(row.get("image_path", ""))
            dishes.append(row)
        tables = as_dicts(self.store.db.execute("SELECT * FROM dining_tables ORDER BY name").fetchall())
        return {"categories": categories, "dishes": dishes, "tables": tables,
                "stock_sheet": (self.root / "assets" / "menu-food-sheet.jpg").resolve().as_uri(),
                "date_label": date.today().strftime("%A, %d %B")}

    def save_category(self, category_id, name, description):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Enter a category name."}
        try:
            if category_id:
                self.store.db.execute("UPDATE categories SET name=?,description=? WHERE id=?", (name, (description or "").strip(), int(category_id)))
            else:
                order = self.store.db.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM categories").fetchone()[0]
                self.store.db.execute("INSERT INTO categories(name,description,sort_order) VALUES(?,?,?)", (name, (description or "").strip(), order))
            self.store.db.commit(); return {"ok": True}
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "That category name is already in use."}

    def delete_category(self, category_id):
        self.store.db.execute("DELETE FROM categories WHERE id=?", (int(category_id),)); self.store.db.commit()
        return {"ok": True}

    def _save_photo(self, image_data, filename):
        if not image_data:
            return ""
        match = re.match(r"^data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=]+)$", image_data)
        if not match:
            raise ValueError("Choose a PNG, JPG, WEBP, or GIF image.")
        content = base64.b64decode(match.group(2), validate=True)
        if len(content) > 8 * 1024 * 1024:
            raise ValueError("Dish photos must be smaller than 8 MB.")
        suffix = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}[match.group(1)]
        safe_name = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(filename or "dish").stem).strip("-") or "dish"
        target = self.data / "photos" / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{safe_name}{suffix}"
        target.write_bytes(content)
        return str(target)

    def save_dish(self, dish_id, category_id, name, price, description, image_data="", image_name=""):
        name = (name or "").strip()
        try:
            price = float(price)
            category_id = int(category_id)
            if not name or price < 0:
                return {"ok": False, "error": "Enter a dish name and a non-negative price."}
            photo = self._save_photo(image_data, image_name) if image_data else ""
            if dish_id:
                old = self.store.db.execute("SELECT image_path FROM dishes WHERE id=?", (int(dish_id),)).fetchone()
                if not old:
                    return {"ok": False, "error": "This dish is no longer in the menu."}
                self.store.db.execute("UPDATE dishes SET category_id=?,name=?,description=?,price=?,image_path=? WHERE id=?",
                                      (category_id, name, (description or "").strip(), price, photo or old["image_path"], int(dish_id)))
            else:
                self.store.db.execute("INSERT INTO dishes(category_id,name,description,price,image_path) VALUES(?,?,?,?,?)",
                                      (category_id, name, (description or "").strip(), price, photo))
            self.store.db.commit(); return {"ok": True}
        except (ValueError, sqlite3.IntegrityError) as error:
            return {"ok": False, "error": str(error) or "Enter valid dish details."}

    def delete_dish(self, dish_id):
        self.store.db.execute("DELETE FROM dishes WHERE id=?", (int(dish_id),)); self.store.db.commit()
        return {"ok": True}

    def add_table(self, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Enter a table name."}
        try:
            self.store.db.execute("INSERT INTO dining_tables(name) VALUES(?)", (name,)); self.store.db.commit()
            return {"ok": True}
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "That table name already exists."}

    def rename_table(self, table_id, name):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Enter a table name."}
        try:
            self.store.db.execute("UPDATE dining_tables SET name=? WHERE id=?", (name, int(table_id))); self.store.db.commit()
            return {"ok": True}
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "That table name already exists."}

    def delete_table(self, table_id):
        self.store.db.execute("DELETE FROM dining_tables WHERE id=?", (int(table_id),)); self.store.db.commit()
        return {"ok": True}

    def save_order(self, items, service, table_name):
        if not items:
            return {"ok": False, "error": "Add at least one dish before creating a bill."}
        if service == "Dine in" and not table_name:
            return {"ok": False, "error": "Choose a table for this dine-in order."}
        cart, subtotal = [], 0.0
        for item in items:
            dish = self.store.db.execute("SELECT id,name,price FROM dishes WHERE id=?", (int(item.get("dish_id", 0)),)).fetchone()
            quantity = int(item.get("quantity", 0))
            if not dish or quantity < 1:
                continue
            line_total = round(float(dish["price"]) * quantity, 2)
            cart.append({"dish_id": dish["id"], "name": dish["name"], "price": float(dish["price"]), "quantity": quantity, "line_total": line_total})
            subtotal += line_total
        if not cart:
            return {"ok": False, "error": "The selected dishes are no longer available."}
        subtotal = round(subtotal, 2); gst = round(subtotal * GST_RATE, 2); total = round(subtotal + gst, 2)
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.store.db:
            cursor = self.store.db.execute("INSERT INTO orders(created_at,service_type,table_name,subtotal,gst,total) VALUES(?,?,?,?,?,?)",
                                           (created_at, service, table_name if service == "Dine in" else "", subtotal, gst, total))
            order_id = cursor.lastrowid
            self.store.db.executemany("INSERT INTO order_items(order_id,dish_name,unit_price,quantity,line_total) VALUES(?,?,?,?,?)",
                                      [(order_id, x["name"], x["price"], x["quantity"], x["line_total"]) for x in cart])
        return {"ok": True, "order_id": order_id, "created_at": created_at, "service": service,
                "table": table_name if service == "Dine in" else "", "subtotal": subtotal, "gst": gst, "total": total, "items": cart}

    def get_report(self, start="", end=""):
        for value in (start, end):
            if value:
                try: date.fromisoformat(value)
                except ValueError: return {"ok": False, "error": "Use dates in YYYY-MM-DD format."}
        return {"ok": True, **self.store.report(start, end)}

    def export_report(self, start="", end=""):
        try:
            import webview as webview_module
            selected = webview_module.windows[0].create_file_dialog(
                webview_module.SAVE_DIALOG, save_filename=f"sales-report-{date.today().isoformat()}.csv",
                file_types=("CSV files (*.csv)",))
        except Exception as error:
            return {"ok": False, "error": f"Could not open the save dialog: {error}"}
        if not selected:
            return {"ok": False, "cancelled": True}
        path = Path(selected[0] if isinstance(selected, (list, tuple)) else selected)
        report = self.store.report(start, end)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Sales report", start or "All time", end or "All time"])
            writer.writerow(["Orders", report["summary"]["orders"], "Food sales", report["summary"]["sales"], "GST", report["summary"]["gst"]])
            writer.writerow([]); writer.writerow(["Dish", "Quantity sold", "Sales"])
            for row in report["sold"]: writer.writerow([row["dish_name"], row["quantity"], row["sales"]])
            writer.writerow([]); writer.writerow(["Order ID", "Date", "Service", "Table", "Subtotal", "GST", "Total"])
            for row in report["orders"]: writer.writerow([row["id"], row["created_at"], row["service_type"], row["table_name"], row["subtotal"], row["gst"], row["total"]])
        return {"ok": True, "path": str(path)}


def main():
    root = resource_root()
    api = Api(root)
    page = root / "index.html"
    webview.create_window(APP_NAME, page.resolve().as_uri(), width=1440, height=960,
                          min_size=(1024, 720), js_api=api, background_color="#fbfaf7")
    webview.start(debug=False)


if __name__ == "__main__":
    main()
