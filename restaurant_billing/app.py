"""Offline-first restaurant billing desktop app with the mockup's HTML interface."""
from __future__ import annotations

import base64
import csv
from functools import wraps
from io import BytesIO
import os
import re
import sqlite3
import socket
import sys
from threading import Event, RLock, Thread
from datetime import date, datetime
from pathlib import Path

import webview
import qrcode
from flask import Flask, jsonify, make_response, redirect, request, send_from_directory
from waitress import create_server

APP_NAME = "HIGHWAY29 Billing"
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
        CREATE TABLE IF NOT EXISTS waiter_orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, table_id INTEGER NOT NULL,
          table_name TEXT NOT NULL, waiter_name TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL, bill_order_id INTEGER REFERENCES orders(id));
        CREATE TABLE IF NOT EXISTS waiter_order_items(
          id INTEGER PRIMARY KEY, waiter_order_id INTEGER NOT NULL REFERENCES waiter_orders(id) ON DELETE CASCADE,
          dish_id INTEGER, dish_name TEXT NOT NULL, unit_price REAL NOT NULL,
          quantity INTEGER NOT NULL, line_total REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS waiter_print_requests(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          waiter_order_id INTEGER NOT NULL REFERENCES waiter_orders(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT '');
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
        self._lock = RLock()
        self.root = root
        self.data = data_root()
        self.store = Store(self.data / "restaurant.sqlite3")

    def _photo_url(self, path):
        if path and Path(path).is_file():
            return "/photos/" + Path(path).name
        return ""

    def toggle_fullscreen(self):
        if not webview.windows:
            return {"ok": False, "error": "The desktop window is not ready."}
        webview.windows[0].toggle_fullscreen()
        return {"ok": True}

    def list_printers(self):
        """Return installed Windows printers and the current system default."""
        if sys.platform != "win32":
            return {"ok": False, "printers": [], "default": "", "error": "Printer selection is available in the Windows billing app."}
        try:
            from System.Drawing.Printing import PrinterSettings
            printers = [str(name) for name in PrinterSettings.InstalledPrinters]
            default = str(PrinterSettings().PrinterName or "")
            preferred = next((name for name in printers if "billquick" in name.lower() or "helet" in name.lower()), default)
            return {"ok": True, "printers": printers, "default": default, "preferred": preferred}
        except Exception as error:
            return {"ok": False, "printers": [], "default": "", "error": f"Could not list installed printers: {error}"}

    def print_receipt(self, requested_printer=""):
        """Print the prepared receipt silently to the selected installed printer."""
        if sys.platform != "win32":
            return {"ok": False, "error": "Direct receipt printing is available in the Windows billing app."}
        if not webview.windows:
            return {"ok": False, "error": "The billing window is not ready for printing."}

        window = webview.windows[0]
        requested_printer = str(requested_printer or "").strip()
        completed = Event()
        result = {}

        def finish_print(task):
            try:
                status = str(task.GetAwaiter().GetResult())
                if "succeeded" in status.lower():
                    result.update(ok=True, printer=result.get("printer", "Windows printer"))
                elif "unavailable" in status.lower():
                    result.update(ok=False, error="The receipt printer is offline or unavailable.")
                else:
                    result.update(ok=False, error=f"Receipt printing failed ({status}).")
            except Exception as error:
                result.update(ok=False, error=f"Receipt printing failed: {error}")
            finally:
                completed.set()

        def begin_print():
            try:
                from System import Action
                from System.Drawing.Printing import PrinterSettings

                printers = [str(name) for name in PrinterSettings.InstalledPrinters]
                if requested_printer and requested_printer not in printers:
                    result.update(ok=False, error="That printer is no longer installed. Choose an available printer in Admin mode.")
                    completed.set()
                    return
                printer = requested_printer or next((name for name in printers
                                if "billquick" in name.lower() or "helet" in name.lower()), None)
                if printer is None:
                    printer = str(PrinterSettings().PrinterName or "")
                if not printer:
                    result.update(ok=False, error="No Windows printer is installed.")
                    completed.set()
                    return

                webview2 = window.native.webview
                core = webview2.CoreWebView2
                settings = core.Environment.CreatePrintSettings()
                settings.PrinterName = printer
                settings.ShouldPrintBackgrounds = True
                settings.ShouldPrintHeaderAndFooter = False
                result["printer"] = printer
                task = core.PrintAsync(settings)
                task.GetAwaiter().OnCompleted(Action(lambda: finish_print(task)))
            except Exception as error:
                result.update(ok=False, error=f"Could not send the receipt to the printer: {error}")
                completed.set()

        try:
            from System import Action
            window.native.BeginInvoke(Action(begin_print))
        except Exception as error:
            return {"ok": False, "error": f"Could not start direct printing: {error}"}

        if not completed.wait(60):
            return {"ok": False, "error": "The printer did not finish within 60 seconds. Check its status and paper."}
        return result

    def get_state(self):
        categories = as_dicts(self.store.db.execute("SELECT * FROM categories ORDER BY sort_order,name").fetchall())
        dishes = []
        for dish in self.store.dishes():
            row = dict(dish)
            row["photo_url"] = self._photo_url(row.get("image_path", ""))
            dishes.append(row)
        tables = as_dicts(self.store.db.execute("SELECT * FROM dining_tables ORDER BY name").fetchall())
        lan_ip = "127.0.0.1"
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("192.0.2.1", 80))
            lan_ip = probe.getsockname()[0]
            probe.close()
        except OSError:
            pass
        waiter_url = f"http://{lan_ip}:8765/waiter"
        qr_buffer = BytesIO()
        qrcode.make(waiter_url).save(qr_buffer, format="PNG")
        return {"categories": categories, "dishes": dishes, "tables": tables,
                "stock_sheet": "/assets/menu-food-sheet.jpg",
                "waiter_url": waiter_url,
                "waiter_qr_data": "data:image/png;base64," + base64.b64encode(qr_buffer.getvalue()).decode("ascii"),
                "date_label": date.today().strftime("%A, %d %B")}

    def list_waiter_orders(self):
        rows = self.store.db.execute(
            "SELECT w.*,(SELECT status FROM waiter_print_requests p WHERE p.waiter_order_id=w.id ORDER BY p.id DESC LIMIT 1) AS print_status,"
            "(SELECT error FROM waiter_print_requests p WHERE p.waiter_order_id=w.id ORDER BY p.id DESC LIMIT 1) AS print_error "
            "FROM waiter_orders w WHERE (w.status NOT IN ('billed','cancelled') OR "
            "(w.status='billed' AND (SELECT status FROM waiter_print_requests p WHERE p.waiter_order_id=w.id ORDER BY p.id DESC LIMIT 1)='failed')) "
            "ORDER BY w.updated_at DESC,w.id DESC"
        ).fetchall()
        orders = []
        for row in rows:
            order = dict(row)
            order["items"] = as_dicts(self.store.db.execute(
                "SELECT dish_id,dish_name AS name,unit_price AS price,quantity,line_total "
                "FROM waiter_order_items WHERE waiter_order_id=? ORDER BY id", (row["id"],)
            ).fetchall())
            orders.append(order)
        return {"ok": True, "orders": orders}

    def save_waiter_order(self, order_id, table_id, waiter_name, items):
        waiter_name = (waiter_name or "").strip()
        table = self.store.db.execute("SELECT id,name FROM dining_tables WHERE id=?", (int(table_id or 0),)).fetchone()
        if not table:
            return {"ok": False, "error": "Choose an available table."}
        if not waiter_name:
            return {"ok": False, "error": "Enter the waiter's name."}
        normalized, subtotal = [], 0.0
        for item in items or []:
            dish = self.store.db.execute("SELECT id,name,price FROM dishes WHERE id=?", (int(item.get("dish_id", 0)),)).fetchone()
            quantity = int(item.get("quantity", 0))
            if dish and quantity > 0:
                line_total = round(float(dish["price"]) * quantity, 2)
                normalized.append({"dish_id": dish["id"], "name": dish["name"], "price": float(dish["price"]),
                                   "quantity": quantity, "line_total": line_total})
                subtotal += line_total
        if not normalized:
            return {"ok": False, "error": "Add at least one dish to the order."}
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            with self.store.db:
                if order_id:
                    existing = self.store.db.execute("SELECT status FROM waiter_orders WHERE id=?", (int(order_id),)).fetchone()
                    if not existing or existing["status"] in ("ready", "billed"):
                        return {"ok": False, "error": "This order is ready for billing or no longer available."}
                    self.store.db.execute(
                        "UPDATE waiter_orders SET table_id=?,table_name=?,waiter_name=?,status='sent',updated_at=? WHERE id=?",
                        (table["id"], table["name"], waiter_name, now, int(order_id)))
                    self.store.db.execute("DELETE FROM waiter_order_items WHERE waiter_order_id=?", (int(order_id),))
                    saved_id = int(order_id)
                else:
                    cursor = self.store.db.execute(
                        "INSERT INTO waiter_orders(table_id,table_name,waiter_name,status,created_at,updated_at) VALUES(?,?,?,'sent',?,?)",
                        (table["id"], table["name"], waiter_name, now, now))
                    saved_id = cursor.lastrowid
                self.store.db.executemany(
                    "INSERT INTO waiter_order_items(waiter_order_id,dish_id,dish_name,unit_price,quantity,line_total) VALUES(?,?,?,?,?,?)",
                    [(saved_id, x["dish_id"], x["name"], x["price"], x["quantity"], x["line_total"]) for x in normalized])
            return {"ok": True, "order_id": saved_id, "table_id": table["id"], "table_name": table["name"],
                    "waiter_name": waiter_name, "items": normalized, "subtotal": round(subtotal, 2)}
        except sqlite3.Error as error:
            return {"ok": False, "error": f"Could not save waiter order: {error}"}

    def mark_waiter_ready(self, order_id):
        with self.store.db:
            result = self.store.db.execute("UPDATE waiter_orders SET status='ready',updated_at=? WHERE id=? AND status IN ('open','sent')",
                                           (datetime.now().astimezone().isoformat(timespec="seconds"), int(order_id)))
        return {"ok": result.rowcount == 1, "error": "Order not found or already billed." if result.rowcount != 1 else ""}

    def request_waiter_print(self, order_id):
        """Queue a ready waiter order for printing from the Windows billing station."""
        order = self.store.db.execute("SELECT status FROM waiter_orders WHERE id=?", (int(order_id),)).fetchone()
        if not order or order["status"] not in ("ready", "billed"):
            return {"ok": False, "error": "The Windows biller must mark this order ready before it can be printed."}
        pending = self.store.db.execute(
            "SELECT id FROM waiter_print_requests WHERE waiter_order_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (int(order_id),)).fetchone()
        if pending:
            return {"ok": True, "request_id": pending["id"], "message": "This receipt is already queued at the Windows billing computer."}
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.store.db:
            cursor = self.store.db.execute(
                "INSERT INTO waiter_print_requests(waiter_order_id,status,created_at,updated_at) VALUES(?,'pending',?,?)",
                (int(order_id), now, now))
        return {"ok": True, "request_id": cursor.lastrowid, "message": "Receipt sent to the Windows billing computer."}

    def list_waiter_print_requests(self):
        rows = self.store.db.execute(
            "SELECT p.id,p.waiter_order_id,w.bill_order_id FROM waiter_print_requests p "
            "JOIN waiter_orders w ON w.id=p.waiter_order_id WHERE p.status='pending' ORDER BY p.id"
        ).fetchall()
        return {"ok": True, "requests": as_dicts(rows)}

    def finish_waiter_print_request(self, request_id, success, error=""):
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        status = "completed" if success else "failed"
        with self.store.db:
            result = self.store.db.execute(
                "UPDATE waiter_print_requests SET status=?,error=?,updated_at=? WHERE id=? AND status='pending'",
                (status, "" if success else str(error or "Could not print the receipt."), now, int(request_id)))
        return {"ok": result.rowcount == 1}

    def get_waiter_bill(self, waiter_order_id):
        row = self.store.db.execute(
            "SELECT w.bill_order_id,o.created_at,o.service_type,o.table_name,o.subtotal,o.gst,o.total "
            "FROM waiter_orders w JOIN orders o ON o.id=w.bill_order_id WHERE w.id=? AND w.status='billed'",
            (int(waiter_order_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "A bill has not been created for this waiter order."}
        items = as_dicts(self.store.db.execute(
            "SELECT dish_name AS name,unit_price AS price,quantity,line_total FROM order_items WHERE order_id=? ORDER BY id",
            (row["bill_order_id"],)).fetchall())
        return {"ok": True, "order_id": row["bill_order_id"], "waiter_order_id": int(waiter_order_id),
                "created_at": row["created_at"], "service": row["service_type"], "table": row["table_name"],
                "subtotal": row["subtotal"], "gst": row["gst"], "total": row["total"], "items": items}

    def update_waiter_order_from_biller(self, order_id, items):
        order = self.store.db.execute("SELECT status FROM waiter_orders WHERE id=?", (int(order_id),)).fetchone()
        if not order or order["status"] not in ("sent", "ready"):
            return {"ok": False, "error": "This order can no longer be edited."}
        updated, now = [], datetime.now().astimezone().isoformat(timespec="seconds")
        for item in items or []:
            dish = self.store.db.execute("SELECT id,name,price FROM dishes WHERE id=?", (int(item.get("dish_id", 0)),)).fetchone()
            quantity = int(item.get("quantity", 0))
            if dish and quantity > 0:
                line_total = round(float(dish["price"]) * quantity, 2)
                updated.append((int(order_id), dish["id"], dish["name"], float(dish["price"]), quantity, line_total))
        if not updated:
            return {"ok": False, "error": "An order must contain at least one dish. Cancel it if the order is no longer needed."}
        with self.store.db:
            self.store.db.execute("DELETE FROM waiter_order_items WHERE waiter_order_id=?", (int(order_id),))
            self.store.db.executemany(
                "INSERT INTO waiter_order_items(waiter_order_id,dish_id,dish_name,unit_price,quantity,line_total) VALUES(?,?,?,?,?,?)",
                updated)
            self.store.db.execute("UPDATE waiter_orders SET updated_at=? WHERE id=?", (now, int(order_id)))
        return {"ok": True}

    def cancel_waiter_order(self, order_id):
        with self.store.db:
            result = self.store.db.execute(
                "UPDATE waiter_orders SET status='cancelled',updated_at=? WHERE id=? AND status IN ('open','sent','ready')",
                (datetime.now().astimezone().isoformat(timespec="seconds"), int(order_id)))
        return {"ok": result.rowcount == 1,
                "error": "This order can no longer be cancelled." if result.rowcount != 1 else ""}

    def bill_waiter_order(self, order_id):
        try:
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            with self.store.db:
                order = self.store.db.execute("SELECT * FROM waiter_orders WHERE id=? AND status='ready'", (int(order_id),)).fetchone()
                if not order:
                    return {"ok": False, "error": "Mark the food ready in the Windows Biller screen before printing its bill."}
                items = as_dicts(self.store.db.execute(
                    "SELECT dish_name AS name,unit_price AS price,quantity,line_total FROM waiter_order_items WHERE waiter_order_id=? ORDER BY id",
                    (int(order_id),)).fetchall())
                if not items:
                    return {"ok": False, "error": "This waiter order has no dishes."}
                subtotal = round(sum(float(i["line_total"]) for i in items), 2)
                gst = round(subtotal * GST_RATE, 2)
                total = round(subtotal + gst, 2)
                cursor = self.store.db.execute(
                    "INSERT INTO orders(created_at,service_type,table_name,subtotal,gst,total) VALUES(?,?,?,?,?,?)",
                    (now, "Dine in", order["table_name"], subtotal, gst, total))
                bill_id = cursor.lastrowid
                self.store.db.executemany(
                    "INSERT INTO order_items(order_id,dish_name,unit_price,quantity,line_total) VALUES(?,?,?,?,?)",
                    [(bill_id, i["name"], i["price"], i["quantity"], i["line_total"]) for i in items])
                self.store.db.execute("UPDATE waiter_orders SET status='billed',updated_at=?,bill_order_id=? WHERE id=?",
                                      (now, bill_id, int(order_id)))
            return {"ok": True, "order_id": bill_id, "waiter_order_id": int(order_id), "created_at": now,
                    "service": "Dine in", "table": order["table_name"], "waiter": order["waiter_name"],
                    "subtotal": subtotal, "gst": gst, "total": total,
                    "items": [{**i, "dish_id": None} for i in items]}
        except (ValueError, sqlite3.Error) as error:
            return {"ok": False, "error": f"Could not create bill: {error}"}

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

    def reset_sales_history(self):
        """Remove completed bill history while keeping active waiter orders."""
        db = self.store.db
        bills_deleted = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        archived_waiter_orders_deleted = db.execute(
            "SELECT COUNT(*) FROM waiter_orders WHERE status IN ('billed','cancelled')"
        ).fetchone()[0]
        with db:
            db.execute("DELETE FROM waiter_orders WHERE status IN ('billed','cancelled')")
            db.execute("UPDATE waiter_orders SET bill_order_id=NULL WHERE bill_order_id IS NOT NULL")
            db.execute("DELETE FROM orders")
        return {"ok": True, "bills_deleted": bills_deleted,
                "archived_waiter_orders_deleted": archived_waiter_orders_deleted}

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
            writer.writerow(["Orders", report["summary"]["orders"], "GST collected", report["summary"]["gst"]])
            writer.writerow([]); writer.writerow(["Dish", "Quantity sold", "Sales"])
            for row in report["sold"]: writer.writerow([row["dish_name"], row["quantity"], row["sales"]])
            writer.writerow([]); writer.writerow(["Order ID", "Date", "Service", "Table", "Subtotal", "GST", "Total"])
            for row in report["orders"]: writer.writerow([row["id"], row["created_at"], row["service_type"], row["table_name"], row["subtotal"], row["gst"], row["total"]])
        return {"ok": True, "path": str(path)}

    def export_report_excel(self, start="", end=""):
        validation = self.get_report(start, end)
        if not validation.get("ok"):
            return validation
        try:
            import webview as webview_module
            selected = webview_module.windows[0].create_file_dialog(
                webview_module.SAVE_DIALOG,
                save_filename=f"highway29-orders-{start or 'all'}-to-{end or 'all'}.xlsx",
                file_types=("Excel workbook (*.xlsx)",))
        except Exception as error:
            return {"ok": False, "error": f"Could not open the save dialog: {error}"}
        if not selected:
            return {"ok": False, "cancelled": True}
        path = Path(selected[0] if isinstance(selected, (list, tuple)) else selected)
        if path.suffix.lower() != ".xlsx":
            path = path.with_suffix(".xlsx")
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill

            report = self.store.report(start, end)
            workbook = Workbook()
            orders_sheet = workbook.active
            orders_sheet.title = "Orders"
            orders_sheet.append(["HIGHWAY29 · Order report", f"From {start or 'all time'} to {end or 'all time'}"])
            orders_sheet.append(["Order #", "Date & time", "Service", "Table", "Items", "Subtotal", "GST", "Total"])
            for order in report["orders"]:
                items = as_dicts(self.store.db.execute(
                    "SELECT dish_name,quantity FROM order_items WHERE order_id=? ORDER BY id", (order["id"],)
                ).fetchall())
                item_summary = ", ".join(f"{item['dish_name']} × {item['quantity']}" for item in items)
                orders_sheet.append([
                    order["id"], order["created_at"], order["service_type"], order["table_name"],
                    item_summary, order["subtotal"], order["gst"], order["total"]])

            summary = workbook.create_sheet("Summary")
            summary.append(["HIGHWAY29 · Report summary"])
            summary.append(["From", start or "All time"])
            summary.append(["To", end or "All time"])
            summary.append(["Completed orders", report["summary"]["orders"]])
            summary.append(["GST collected", report["summary"]["gst"]])
            for sheet in workbook.worksheets:
                sheet.freeze_panes = "A3" if sheet.title == "Orders" else "A2"
                sheet.auto_filter.ref = f"A2:H{max(2, sheet.max_row)}" if sheet.title == "Orders" else "A1:B5"
                for cell in sheet[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="285A43")
                if sheet.title == "Orders":
                    for cell in sheet[2]:
                        cell.font = Font(bold=True, color="FFFFFF")
                        cell.fill = PatternFill("solid", fgColor="285A43")
                    for row in sheet.iter_rows(min_row=3, min_col=6, max_col=8):
                        for cell in row:
                            cell.number_format = '₹#,##0.00'
                    for column, width in {"A": 12, "B": 24, "C": 16, "D": 18, "E": 50, "F": 16, "G": 14, "H": 16}.items():
                        sheet.column_dimensions[column].width = width
                else:
                    sheet.column_dimensions["A"].width = 24
                    sheet.column_dimensions["B"].width = 28
                    sheet[5][1].number_format = '₹#,##0.00'
            workbook.save(path)
            return {"ok": True, "path": str(path)}
        except Exception as error:
            return {"ok": False, "error": f"Could not export the Excel report: {error}"}


def synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


for _api_method in (name for name in dir(Api) if not name.startswith("_") and callable(getattr(Api, name))):
    setattr(Api, _api_method, synchronized(getattr(Api, _api_method)))


def main():
    root = resource_root()
    api = Api(root)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024
    exposed = {name for name in dir(api) if not name.startswith("_") and callable(getattr(api, name))}

    @app.get("/")
    def index():
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return redirect("/waiter")
        return send_from_directory(root, "index.html")

    @app.get("/waiter")
    def waiter_index():
        html = (root / "index.html").read_text(encoding="utf-8")
        html = html.replace(
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">',
            '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">',
            1,
        )
        response = make_response(html)
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie("client_mode", "waiter", httponly=True, samesite="Lax")
        return response

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(root / "assets", name)

    @app.get("/photos/<path:name>")
    def photos(name):
        return send_from_directory(api.data / "photos", name)

    @app.post("/api/<method>")
    def api_call(method):
        if method not in exposed:
            return jsonify({"ok": False, "error": "Unknown operation."}), 404
        waiter_methods = {"get_state", "list_waiter_orders", "save_waiter_order", "request_waiter_print"}
        if request.remote_addr not in ("127.0.0.1", "::1"):
            if request.cookies.get("client_mode") != "waiter" or method not in waiter_methods:
                return jsonify({"ok": False, "error": "This operation is only available in the Waiter screen."}), 403
        try:
            payload = request.get_json(silent=True) or {}
            with api._lock:
                result = getattr(api, method)(*(payload.get("args") or []))
            return jsonify(result)
        except Exception as error:
            app.logger.exception("API call failed: %s", method)
            return jsonify({"ok": False, "error": str(error)}), 400

    server = create_server(app, host="0.0.0.0", port=8765, threads=16)
    Thread(target=server.run, daemon=True).start()
    webview.create_window(APP_NAME, "http://127.0.0.1:8765/", width=1440, height=960, fullscreen=True,
                          min_size=(1024, 720), js_api=api, background_color="#fbfaf7")
    webview.start(debug=False)


if __name__ == "__main__":
    main()
