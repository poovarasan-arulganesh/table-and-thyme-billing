"""Offline-first restaurant POS and sales reporting app."""
from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageTk
except ImportError:  # Optional on developer machines; installed by the Windows build.
    Image = ImageTk = None

APP_NAME = "Table & Thyme Billing"
GST_RATE = 0.05


def data_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    target = base / "TableAndThymeBilling"
    target.mkdir(parents=True, exist_ok=True)
    (target / "photos").mkdir(exist_ok=True)
    return target


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.initialize()

    def initialize(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS categories(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS dishes(
          id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
          name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', price REAL NOT NULL CHECK(price >= 0), image_path TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS dining_tables(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, service_type TEXT NOT NULL,
          table_name TEXT NOT NULL DEFAULT '', subtotal REAL NOT NULL, gst REAL NOT NULL, total REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS order_items(
          id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
          dish_name TEXT NOT NULL, unit_price REAL NOT NULL, quantity INTEGER NOT NULL, line_total REAL NOT NULL);
        """)
        if self.db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            categories = [("Starters", "Small plates to begin"), ("Main course", "Freshly prepared mains"),
                          ("Juices & drinks", "Fresh drinks and coolers"), ("Desserts", "Something sweet")]
            ids = {}
            for index, (name, description) in enumerate(categories):
                cur = self.db.execute("INSERT INTO categories(name,description,sort_order) VALUES(?,?,?)", (name, description, index))
                ids[name] = cur.lastrowid
            dishes = [("Starters", "Crispy Samosa", "", 90), ("Starters", "Paneer Tikka", "", 240),
                      ("Main course", "Butter Chicken", "", 320), ("Main course", "Hyderabadi Biryani", "", 280),
                      ("Main course", "Dal Makhani", "", 220), ("Juices & drinks", "Mango Cooler", "", 120),
                      ("Desserts", "Gulab Jamun", "", 100)]
            self.db.executemany("INSERT INTO dishes(category_id,name,description,price) VALUES(?,?,?,?)",
                                [(ids[c], n, d, p) for c, n, d, p in dishes])
        if self.db.execute("SELECT COUNT(*) FROM dining_tables").fetchone()[0] == 0:
            self.db.executemany("INSERT INTO dining_tables(name) VALUES(?)", [(f"Table {i:02d}",) for i in range(1, 7)])
        self.db.commit()

    def categories(self):
        return self.db.execute("SELECT * FROM categories ORDER BY sort_order,name").fetchall()

    def dishes(self, category_id=None, search=""):
        sql = "SELECT d.*,c.name category_name FROM dishes d JOIN categories c ON c.id=d.category_id WHERE 1=1"
        args = []
        if category_id is not None:
            sql += " AND d.category_id=?"; args.append(category_id)
        if search:
            sql += " AND d.name LIKE ?"; args.append(f"%{search}%")
        return self.db.execute(sql + " ORDER BY d.name", args).fetchall()

    def tables(self):
        return self.db.execute("SELECT * FROM dining_tables ORDER BY name").fetchall()

    def save_order(self, cart, service, table_name):
        if not cart:
            raise ValueError("Add at least one dish before creating a bill.")
        subtotal = round(sum(item["price"] * item["quantity"] for item in cart.values()), 2)
        tax = round(subtotal * GST_RATE, 2)
        total = round(subtotal + tax, 2)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.db:
            cur = self.db.execute("INSERT INTO orders(created_at,service_type,table_name,subtotal,gst,total) VALUES(?,?,?,?,?,?)",
                                  (now, service, table_name if service == "Dine in" else "", subtotal, tax, total))
            order_id = cur.lastrowid
            self.db.executemany("INSERT INTO order_items(order_id,dish_name,unit_price,quantity,line_total) VALUES(?,?,?,?,?)",
                                [(order_id, item["name"], item["price"], item["quantity"], round(item["price"] * item["quantity"], 2)) for item in cart.values()])
        return order_id, now, subtotal, tax, total

    def report_summary(self, start="", end=""):
        where, args = [], []
        if start:
            where.append("date(created_at)>=date(?)"); args.append(start)
        if end:
            where.append("date(created_at)<=date(?)"); args.append(end)
        clause = " WHERE " + " AND ".join(where) if where else ""
        orders = self.db.execute("SELECT COUNT(*) n,COALESCE(SUM(subtotal),0) sales,COALESCE(SUM(gst),0) gst FROM orders" + clause, args).fetchone()
        item_conditions = []
        if start: item_conditions.append("date(o.created_at)>=date(?)")
        if end: item_conditions.append("date(o.created_at)<=date(?)")
        item_where, item_args = (" WHERE " + " AND ".join(item_conditions) if item_conditions else ""), args
        sold = self.db.execute("SELECT i.dish_name,SUM(i.quantity) quantity,SUM(i.line_total) sales FROM order_items i JOIN orders o ON o.id=i.order_id" + item_where + " GROUP BY i.dish_name ORDER BY quantity DESC,sales DESC", item_args).fetchall()
        recent = self.db.execute("SELECT * FROM orders" + clause + " ORDER BY id DESC LIMIT 100", args).fetchall()
        return orders, sold, recent

    def close(self):
        self.db.close()


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1360x860")
        self.root.minsize(980, 680)
        self.store = Store(data_root() / "restaurant.sqlite3")
        self.cart = {}
        self.theme = "light"
        self.mode = "casual"
        self.selected_category = None
        self.photos = []
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.set_theme()
        self.build_shell()
        self.show_casual()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def set_theme(self):
        dark = self.theme == "dark"
        bg, card, fg, muted, border = ("#191e1b", "#242b26", "#f4f2ea", "#a4a69c", "#39423b") if dark else ("#fbfaf7", "#ffffff", "#24251f", "#85877e", "#e8e8e1")
        self.colors = dict(bg=bg, card=card, fg=fg, muted=muted, border=border, green="#285a43", soft="#eef3e7")
        self.root.configure(bg=bg)
        self.style.configure("TFrame", background=bg)
        self.style.configure("Card.TFrame", background=card, relief="solid", borderwidth=1)
        self.style.configure("TLabel", background=bg, foreground=fg, font=("Segoe UI", 10))
        self.style.configure("Muted.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        self.style.configure("Card.TLabel", background=card, foreground=fg, font=("Segoe UI", 10))
        self.style.configure("Title.TLabel", background=bg, foreground=fg, font=("Georgia", 24, "bold"))
        self.style.configure("Header.TLabel", background=card, foreground=fg, font=("Georgia", 16, "bold"))
        self.style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7))
        self.style.configure("Primary.TButton", background=self.colors["green"], foreground="#ffffff", font=("Segoe UI", 10, "bold"), padding=(12, 9))
        self.style.map("Primary.TButton", background=[("active", "#1e4834")])
        self.style.configure("Treeview", background=card, fieldbackground=card, foreground=fg, rowheight=29, bordercolor=border)
        self.style.configure("Treeview.Heading", background=bg, foreground=fg, font=("Segoe UI", 9, "bold"))
        self.style.configure("TNotebook", background=bg, borderwidth=0)
        self.style.configure("TNotebook.Tab", padding=(15, 9), font=("Segoe UI", 10, "bold"))
        self.style.configure("TEntry", fieldbackground=card, foreground=fg)
        self.style.configure("TCombobox", fieldbackground=card, foreground=fg)

    def build_shell(self):
        top = ttk.Frame(self.root, padding=(22, 12)); top.pack(fill="x")
        brand = ttk.Frame(top); brand.pack(side="left")
        ttk.Label(brand, text="✳", foreground=self.colors["green"], font=("Segoe UI", 19, "bold")).pack(side="left", padx=(0, 10))
        ttk.Label(brand, text="Table & Thyme", font=("Georgia", 17, "bold")).pack(anchor="w")
        ttk.Label(brand, text="RESTAURANT BILLING · OFFLINE", style="Muted.TLabel").pack(anchor="w")
        nav = ttk.Frame(top); nav.pack(side="left", expand=True)
        self.nav = nav
        self.casual_btn = ttk.Button(nav, text="◉  Casual mode", command=self.show_casual); self.casual_btn.pack(side="left", padx=4)
        self.admin_btn = ttk.Button(nav, text="⚙  Admin mode", command=self.show_admin); self.admin_btn.pack(side="left", padx=4)
        self.theme_btn = ttk.Button(top, text="☾  Dark mode", command=self.toggle_theme); self.theme_btn.pack(side="right")
        self.content = ttk.Frame(self.root, padding=(22, 12, 22, 22)); self.content.pack(fill="both", expand=True)
        self.show_casual()

    def clear_content(self):
        for child in self.content.winfo_children(): child.destroy()
        self.photos.clear()

    def toggle_theme(self):
        self.theme = "dark" if self.theme == "light" else "light"
        self.set_theme(); self.theme_btn.configure(text="☀  Light mode" if self.theme == "dark" else "☾  Dark mode")
        if self.mode == "admin": self.show_admin()
        else: self.show_casual()

    def show_casual(self):
        self.mode = "casual"
        self.clear_content()
        heading = ttk.Frame(self.content); heading.pack(fill="x", pady=(0, 14))
        ttk.Label(heading, text="Build an order", style="Title.TLabel").pack(side="left")
        ttk.Label(heading, text="Choose dishes, then create a GST bill.", style="Muted.TLabel").pack(side="left", padx=15, pady=(9, 0))
        layout = ttk.Frame(self.content); layout.pack(fill="both", expand=True)
        menu = ttk.Frame(layout); menu.pack(side="left", fill="both", expand=True, padx=(0, 16))
        self.category_box = ttk.Frame(menu); self.category_box.pack(fill="x", pady=(0, 10))
        tools = ttk.Frame(menu); tools.pack(fill="x", pady=(0, 12))
        self.search_var = tk.StringVar(); search = ttk.Entry(tools, textvariable=self.search_var); search.pack(side="left", fill="x", expand=True, ipady=5)
        search.insert(0, "Search dishes"); self.search_var.trace_add("write", lambda *_: self.render_dishes())
        self.dish_canvas = tk.Canvas(menu, highlightthickness=0, bg=self.colors["bg"]); self.dish_canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(menu, orient="vertical", command=self.dish_canvas.yview); scroll.pack(side="right", fill="y")
        self.dish_canvas.configure(yscrollcommand=scroll.set)
        self.dish_frame = ttk.Frame(self.dish_canvas)
        self.dish_window = self.dish_canvas.create_window((0, 0), window=self.dish_frame, anchor="nw")
        self.dish_frame.bind("<Configure>", lambda e: self.dish_canvas.configure(scrollregion=self.dish_canvas.bbox("all")))
        self.dish_canvas.bind("<Configure>", lambda e: self.dish_canvas.itemconfigure(self.dish_window, width=e.width))
        order = ttk.Frame(layout, style="Card.TFrame", padding=16); order.pack(side="right", fill="y"); order.configure(width=360); order.pack_propagate(False)
        toporder = ttk.Frame(order); toporder.pack(fill="x")
        ttk.Label(toporder, text="Current order", style="Header.TLabel").pack(side="left")
        ttk.Button(toporder, text="Clear", command=self.clear_cart).pack(side="right")
        row = ttk.Frame(order); row.pack(fill="x", pady=(14, 9))
        ttk.Label(row, text="Service").pack(side="left")
        self.service_var = tk.StringVar(value="Dine in")
        service = ttk.Combobox(row, textvariable=self.service_var, values=["Dine in", "Delivery"], state="readonly", width=13); service.pack(side="right")
        service.bind("<<ComboboxSelected>>", lambda e: self.refresh_tables())
        tablerow = ttk.Frame(order); tablerow.pack(fill="x", pady=(0, 10))
        ttk.Label(tablerow, text="Table").pack(side="left")
        self.table_var = tk.StringVar(); self.table_select = ttk.Combobox(tablerow, textvariable=self.table_var, state="readonly", width=13); self.table_select.pack(side="right")
        self.cart_frame = ttk.Frame(order); self.cart_frame.pack(fill="both", expand=True)
        self.total_frame = ttk.Frame(order); self.total_frame.pack(fill="x", pady=(8, 8))
        ttk.Label(order, text="GST calculated at 5% · Stored on this computer", style="Muted.TLabel").pack(anchor="w", pady=(2, 9))
        ttk.Button(order, text="Create bill  →", style="Primary.TButton", command=self.checkout).pack(fill="x")
        self.render_categories(); self.render_dishes(); self.refresh_tables(); self.render_cart()

    def render_categories(self):
        for child in self.category_box.winfo_children(): child.destroy()
        ttk.Button(self.category_box, text="All items", command=lambda: self.choose_category(None)).pack(side="left", padx=(0, 6))
        for category in self.store.categories():
            ttk.Button(self.category_box, text=category["name"], command=lambda cid=category["id"]: self.choose_category(cid)).pack(side="left", padx=3)

    def choose_category(self, category_id):
        self.selected_category = category_id; self.render_dishes()

    def render_dishes(self):
        if not hasattr(self, "dish_frame") or not self.dish_frame.winfo_exists(): return
        for child in self.dish_frame.winfo_children(): child.destroy()
        self.photos.clear()
        query = self.search_var.get().strip()
        if query == "Search dishes": query = ""
        rows = self.store.dishes(self.selected_category, query)
        width = max(1, self.dish_canvas.winfo_width())
        columns = 3 if width > 780 else (2 if width > 500 else 1)
        for i, dish in enumerate(rows):
            card = ttk.Frame(self.dish_frame, style="Card.TFrame", padding=12); card.grid(row=i // columns, column=i % columns, sticky="nsew", padx=6, pady=6)
            self.draw_photo(card, dish["image_path"], dish["name"])
            ttk.Label(card, text=dish["name"], style="Card.TLabel", font=("Segoe UI", 13, "bold"), wraplength=210).pack(anchor="w", pady=(8, 5))
            bottom = ttk.Frame(card); bottom.pack(fill="x", pady=(2, 0))
            ttk.Label(bottom, text=f"₹ {dish['price']:,.2f}", style="Card.TLabel", font=("Segoe UI", 13, "bold")).pack(side="left")
            ttk.Button(bottom, text="＋ Add", style="Primary.TButton", command=lambda d=dish: self.add_to_cart(d)).pack(side="right")
        for col in range(columns): self.dish_frame.columnconfigure(col, weight=1)

    def draw_photo(self, parent, path, name, size=(210, 105)):
        if path and Path(path).exists() and Image is not None:
            try:
                image = Image.open(path).convert("RGB"); image.thumbnail(size)
                photo = ImageTk.PhotoImage(image); self.photos.append(photo)
                ttk.Label(parent, image=photo, background=self.colors["card"]).pack(fill="x")
                return
            except Exception: pass
        emoji_map = [("Chicken", "🍛"), ("Paneer", "🧀"), ("Biryani", "🍚"), ("Samosa", "🥟"), ("Mango", "🥭"), ("Jamun", "🍮"), ("Dal", "🍲")]
        emoji = next((e for key, e in emoji_map if key.lower() in name.lower()), "🍽️")
        tile = tk.Label(parent, text=emoji, font=("Segoe UI Emoji", 37), bg="#eef3e7", fg="#285a43", height=2)
        tile.pack(fill="x")

    def add_to_cart(self, dish):
        item = self.cart.setdefault(dish["id"], {"name": dish["name"], "price": dish["price"], "quantity": 0})
        item["quantity"] += 1; self.render_cart()

    def clear_cart(self):
        if self.cart and messagebox.askyesno("Clear order", "Remove all dishes from the current order?"):
            self.cart.clear(); self.render_cart()

    def refresh_tables(self):
        names = [row["name"] for row in self.store.tables()]
        self.table_select.configure(values=names)
        if names and self.table_var.get() not in names: self.table_var.set(names[0])
        if self.service_var.get() == "Delivery": self.table_select.configure(state="disabled")
        else: self.table_select.configure(state="readonly")

    def render_cart(self):
        if not hasattr(self, "cart_frame") or not self.cart_frame.winfo_exists(): return
        for child in self.cart_frame.winfo_children(): child.destroy()
        if not self.cart:
            ttk.Label(self.cart_frame, text="Your order is empty.\nAdd dishes from the menu.", style="Muted.TLabel", justify="center").pack(expand=True)
        else:
            for dish_id, item in list(self.cart.items()):
                row = ttk.Frame(self.cart_frame); row.pack(fill="x", pady=7)
                text = ttk.Frame(row); text.pack(side="left", fill="x", expand=True)
                ttk.Label(text, text=item["name"], font=("Segoe UI", 10, "bold")).pack(anchor="w")
                ttk.Label(text, text=f"₹ {item['price']:,.2f} each", style="Muted.TLabel").pack(anchor="w")
                controls = ttk.Frame(row); controls.pack(side="right")
                ttk.Button(controls, text="−", width=2, command=lambda did=dish_id: self.change_quantity(did, -1)).pack(side="left")
                ttk.Label(controls, text=str(item["quantity"]), width=3, anchor="center").pack(side="left")
                ttk.Button(controls, text="+", width=2, command=lambda did=dish_id: self.change_quantity(did, 1)).pack(side="left")
                ttk.Label(row, text=f"₹ {item['price']*item['quantity']:,.2f}", font=("Segoe UI", 10, "bold")).pack(side="right", padx=(8, 0))
        for child in self.total_frame.winfo_children(): child.destroy()
        subtotal = sum(x["price"] * x["quantity"] for x in self.cart.values()); gst = subtotal * GST_RATE
        for label, amount, bold in [("Subtotal", subtotal, False), ("GST (5%)", gst, False), ("Estimated total", subtotal + gst, True)]:
            line = ttk.Frame(self.total_frame); line.pack(fill="x", pady=3)
            ttk.Label(line, text=label, font=("Segoe UI", 10, "bold" if bold else "normal")).pack(side="left")
            ttk.Label(line, text=f"₹ {amount:,.2f}", font=("Segoe UI", 10, "bold" if bold else "normal")).pack(side="right")

    def change_quantity(self, dish_id, amount):
        if dish_id in self.cart:
            self.cart[dish_id]["quantity"] += amount
            if self.cart[dish_id]["quantity"] <= 0: del self.cart[dish_id]
            self.render_cart()

    def checkout(self):
        if not self.cart:
            messagebox.showinfo("No dishes", "Add at least one dish to the order."); return
        service = self.service_var.get(); table = self.table_var.get()
        if service == "Dine in" and not table:
            messagebox.showwarning("No tables", "Add a table in Admin mode before creating a dine-in bill."); return
        try:
            order_id, created_at, subtotal, tax, total = self.store.save_order(self.cart, service, table)
        except ValueError as error:
            messagebox.showerror("Cannot create bill", str(error)); return
        receipt = self.make_receipt(order_id, created_at, service, table, subtotal, tax, total)
        self.show_receipt(receipt, order_id)
        self.cart.clear(); self.render_cart()

    def make_receipt(self, order_id, created_at, service, table, subtotal, tax, total):
        lines = ["                 GST TAX INVOICE", "=" * 38,
                 f"Order: #{order_id:05d}", f"Date: {datetime.fromisoformat(created_at).strftime('%d %b %Y %I:%M %p')}",
                 f"Service: {service}" + (f" · {table}" if service == "Dine in" else ""), "-" * 38,
                 f"{'Item':20} {'Qty':>4} {'Price':>10}", "-" * 38]
        for item in self.cart.values():
            lines.append(f"{item['name'][:20]:20} {item['quantity']:>4} {item['price']*item['quantity']:>10.2f}")
        lines += ["-" * 38, f"{'Subtotal':>26} {subtotal:>10.2f}", f"{'GST (5%)':>26} {tax:>10.2f}", "=" * 38, f"{'TOTAL':>26} {total:>10.2f}", "", "           Thank you for dining with us!"]
        return "\n".join(lines)

    def show_receipt(self, receipt, order_id):
        win = tk.Toplevel(self.root); win.title(f"Bill #{order_id:05d}"); win.geometry("440x600"); win.transient(self.root)
        box = tk.Text(win, font=("Courier New", 11), padx=18, pady=15, wrap="none", height=27)
        box.pack(fill="both", expand=True, padx=16, pady=16); box.insert("1.0", receipt); box.configure(state="disabled")
        actions = ttk.Frame(win, padding=(16, 0, 16, 16)); actions.pack(fill="x")
        ttk.Button(actions, text="Save / Print…", style="Primary.TButton", command=lambda: self.print_receipt(receipt, order_id)).pack(side="right")
        ttk.Button(actions, text="Close", command=win.destroy).pack(side="right", padx=7)

    def print_receipt(self, receipt, order_id):
        path = filedialog.asksaveasfilename(title="Save GST bill", initialfile=f"bill-{order_id:05d}.txt", defaultextension=".txt", filetypes=[("Text bill", "*.txt"), ("All files", "*.*")])
        if not path: return
        Path(path).write_text(receipt, encoding="utf-8")
        try:
            if sys.platform == "win32": os.startfile(path, "print")
            elif sys.platform == "darwin": subprocess.Popen(["open", "-a", "Preview", path])
            else: subprocess.Popen(["lp", path])
            messagebox.showinfo("Bill saved", "The bill was saved and sent to the system print dialog.")
        except Exception:
            messagebox.showinfo("Bill saved", f"Saved bill to:\n{path}")

    def show_admin(self):
        self.mode = "admin"
        self.clear_content()
        ttk.Label(self.content, text="Administration", style="Title.TLabel").pack(anchor="w", pady=(0, 14))
        tabs = ttk.Notebook(self.content); tabs.pack(fill="both", expand=True)
        menu_tab, seating_tab, reports_tab = ttk.Frame(tabs, padding=14), ttk.Frame(tabs, padding=14), ttk.Frame(tabs, padding=14)
        tabs.add(menu_tab, text="Menu management"); tabs.add(seating_tab, text="Seating overview"); tabs.add(reports_tab, text="Sales reports")
        self.build_menu_admin(menu_tab); self.build_seating_admin(seating_tab); self.build_reports(reports_tab)

    def build_menu_admin(self, parent):
        split = ttk.Panedwindow(parent, orient="horizontal"); split.pack(fill="both", expand=True)
        left = ttk.Frame(split, padding=8); right = ttk.Frame(split, padding=8); split.add(left, weight=1); split.add(right, weight=3)
        ttk.Label(left, text="Categories", style="Header.TLabel").pack(anchor="w", pady=(0, 10))
        self.category_tree = ttk.Treeview(left, columns=("count",), show="tree headings", selectmode="browse")
        self.category_tree.heading("#0", text="Category"); self.category_tree.heading("count", text="Dishes"); self.category_tree.column("#0", width=180); self.category_tree.column("count", width=60, anchor="center")
        self.category_tree.pack(fill="both", expand=True); self.category_tree.bind("<<TreeviewSelect>>", self.on_category_select)
        actions = ttk.Frame(left); actions.pack(fill="x", pady=8)
        ttk.Button(actions, text="＋ Add", command=self.add_category).pack(side="left", padx=2)
        ttk.Button(actions, text="Rename", command=self.edit_category).pack(side="left", padx=2)
        ttk.Button(actions, text="Remove", command=self.delete_category).pack(side="left", padx=2)
        ttk.Label(right, text="Dishes", style="Header.TLabel").pack(anchor="w", pady=(0, 10))
        cols = ("name", "price", "photo")
        self.dish_tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="browse")
        for col, label in [("name", "Dish name"), ("price", "Price (₹)"), ("photo", "Photo")]: self.dish_tree.heading(col, text=label)
        self.dish_tree.column("name", width=250); self.dish_tree.column("price", width=110, anchor="e"); self.dish_tree.column("photo", width=220)
        self.dish_tree.pack(fill="both", expand=True)
        controls = ttk.Frame(right); controls.pack(fill="x", pady=8)
        ttk.Button(controls, text="＋ Add dish", style="Primary.TButton", command=self.add_dish).pack(side="left", padx=3)
        ttk.Button(controls, text="Edit selected", command=self.edit_dish).pack(side="left", padx=3)
        ttk.Button(controls, text="Remove selected", command=self.delete_dish).pack(side="left", padx=3)
        self.refresh_admin_menu()

    def refresh_admin_menu(self, select_id=None):
        if not hasattr(self, "category_tree") or not self.category_tree.winfo_exists(): return
        for iid in self.category_tree.get_children(): self.category_tree.delete(iid)
        for c in self.store.categories():
            count = self.store.db.execute("SELECT COUNT(*) FROM dishes WHERE category_id=?", (c["id"],)).fetchone()[0]
            self.category_tree.insert("", "end", iid=str(c["id"]), text=c["name"], values=(count,))
        if select_id is not None and self.category_tree.exists(str(select_id)): self.category_tree.selection_set(str(select_id))
        elif self.category_tree.get_children(): self.category_tree.selection_set(self.category_tree.get_children()[0])
        self.refresh_admin_dishes()

    def selected_category_id(self):
        selected = self.category_tree.selection() if hasattr(self, "category_tree") else ()
        return int(selected[0]) if selected else None

    def on_category_select(self, _event=None): self.refresh_admin_dishes()

    def refresh_admin_dishes(self):
        if not hasattr(self, "dish_tree") or not self.dish_tree.winfo_exists(): return
        for iid in self.dish_tree.get_children(): self.dish_tree.delete(iid)
        cid = self.selected_category_id()
        if cid:
            for d in self.store.dishes(cid): self.dish_tree.insert("", "end", iid=str(d["id"]), values=(d["name"], f"₹ {d['price']:,.2f}", "✓" if d["image_path"] else "Emoji placeholder"))

    def add_category(self):
        dialog = CategoryDialog(self.root, "Add category")
        if dialog.result:
            name, description = dialog.result
            try:
                cur = self.store.db.execute("INSERT INTO categories(name,description,sort_order) VALUES(?,?,?)", (name, description, len(self.store.categories()))); self.store.db.commit()
                self.refresh_admin_menu(cur.lastrowid); self.refresh_casual_categories()
            except sqlite3.IntegrityError: messagebox.showerror("Category exists", "Choose a different category name.")

    def edit_category(self):
        cid = self.selected_category_id()
        if not cid: return
        row = self.store.db.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
        dialog = CategoryDialog(self.root, "Edit category", row["name"], row["description"])
        if dialog.result:
            try:
                self.store.db.execute("UPDATE categories SET name=?,description=? WHERE id=?", (*dialog.result, cid)); self.store.db.commit()
                self.refresh_admin_menu(cid); self.refresh_casual_categories()
            except sqlite3.IntegrityError: messagebox.showerror("Category exists", "Choose a different category name.")

    def delete_category(self):
        cid = self.selected_category_id()
        if not cid: return
        name = self.store.db.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()[0]
        if messagebox.askyesno("Remove category", f"Remove {name} and all dishes in it?"):
            self.store.db.execute("DELETE FROM categories WHERE id=?", (cid,)); self.store.db.commit(); self.refresh_admin_menu(); self.refresh_casual_categories()

    def add_dish(self): self.open_dish_dialog()

    def selected_dish_id(self):
        selected = self.dish_tree.selection() if hasattr(self, "dish_tree") else ()
        return int(selected[0]) if selected else None

    def edit_dish(self):
        did = self.selected_dish_id()
        if not did: messagebox.showinfo("Select a dish", "Choose a dish to edit."); return
        self.open_dish_dialog(self.store.db.execute("SELECT * FROM dishes WHERE id=?", (did,)).fetchone())

    def open_dish_dialog(self, dish=None):
        categories = self.store.categories()
        if not categories: messagebox.showinfo("Create a category", "Add a category before adding dishes."); return
        selected_id = self.selected_category_id() or categories[0]["id"]
        dialog = DishDialog(self.root, categories, selected_id, dish)
        if not dialog.result: return
        category_id, name, description, price, photo = dialog.result
        if photo:
            source = Path(photo); dest = data_root() / "photos" / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}{source.suffix.lower()}"
            shutil.copy2(source, dest); photo = str(dest)
        if dish:
            self.store.db.execute("UPDATE dishes SET category_id=?,name=?,description=?,price=?,image_path=? WHERE id=?", (category_id, name, description, price, photo or dish["image_path"], dish["id"]))
        else: self.store.db.execute("INSERT INTO dishes(category_id,name,description,price,image_path) VALUES(?,?,?,?,?)", (category_id, name, description, price, photo))
        self.store.db.commit(); self.refresh_admin_menu(category_id); self.refresh_casual_menu()

    def delete_dish(self):
        did = self.selected_dish_id()
        if did and messagebox.askyesno("Remove dish", "Remove this dish from the menu?"):
            self.store.db.execute("DELETE FROM dishes WHERE id=?", (did,)); self.store.db.commit(); self.refresh_admin_dishes(); self.refresh_casual_menu()

    def build_seating_admin(self, parent):
        top = ttk.Frame(parent); top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="Dining tables", style="Header.TLabel").pack(side="left")
        ttk.Button(top, text="＋ Add table", style="Primary.TButton", command=self.add_table).pack(side="right")
        ttk.Label(parent, text="These tables appear in the Dine in selector in Casual mode.", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        self.table_tree = ttk.Treeview(parent, columns=("status",), show="tree headings", selectmode="browse")
        self.table_tree.heading("#0", text="Table name"); self.table_tree.heading("status", text="Status"); self.table_tree.column("#0", width=260); self.table_tree.column("status", width=160)
        self.table_tree.pack(fill="both", expand=True)
        actions = ttk.Frame(parent); actions.pack(fill="x", pady=10)
        ttk.Button(actions, text="Rename selected", command=self.edit_table).pack(side="left", padx=3)
        ttk.Button(actions, text="Remove selected", command=self.delete_table).pack(side="left", padx=3)
        self.refresh_table_admin()

    def refresh_table_admin(self):
        if not hasattr(self, "table_tree") or not self.table_tree.winfo_exists(): return
        for iid in self.table_tree.get_children(): self.table_tree.delete(iid)
        for t in self.store.tables(): self.table_tree.insert("", "end", iid=str(t["id"]), text=t["name"], values=("Available",))

    def add_table(self):
        name = simpledialog.askstring("Add table", "Table name or number (for example, Table 07):", parent=self.root)
        if name and name.strip():
            try: self.store.db.execute("INSERT INTO dining_tables(name) VALUES(?)", (name.strip(),)); self.store.db.commit(); self.refresh_table_admin(); self.refresh_tables()
            except sqlite3.IntegrityError: messagebox.showerror("Table exists", "That table name is already in use.")

    def edit_table(self):
        selected = self.table_tree.selection()
        if not selected: return
        table = self.store.db.execute("SELECT * FROM dining_tables WHERE id=?", (int(selected[0]),)).fetchone()
        name = simpledialog.askstring("Rename table", "Table name:", initialvalue=table["name"], parent=self.root)
        if name and name.strip():
            try: self.store.db.execute("UPDATE dining_tables SET name=? WHERE id=?", (name.strip(), table["id"])); self.store.db.commit(); self.refresh_table_admin(); self.refresh_tables()
            except sqlite3.IntegrityError: messagebox.showerror("Table exists", "That table name is already in use.")

    def delete_table(self):
        selected = self.table_tree.selection()
        if not selected: return
        name = self.store.db.execute("SELECT name FROM dining_tables WHERE id=?", (int(selected[0]),)).fetchone()[0]
        if messagebox.askyesno("Remove table", f"Remove {name} from the available tables?"):
            self.store.db.execute("DELETE FROM dining_tables WHERE id=?", (int(selected[0]),)); self.store.db.commit(); self.refresh_table_admin(); self.refresh_tables()

    def build_reports(self, parent):
        bar = ttk.Frame(parent); bar.pack(fill="x", pady=(0, 12))
        ttk.Label(bar, text="From").pack(side="left")
        self.report_start = tk.StringVar(value=date.today().replace(day=1).isoformat())
        ttk.Entry(bar, textvariable=self.report_start, width=13).pack(side="left", padx=(6, 14))
        ttk.Label(bar, text="To").pack(side="left")
        self.report_end = tk.StringVar(value=date.today().isoformat())
        ttk.Entry(bar, textvariable=self.report_end, width=13).pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh report", style="Primary.TButton", command=self.refresh_reports).pack(side="left", padx=8)
        ttk.Button(bar, text="All time", command=self.show_all_time_report).pack(side="left", padx=2)
        ttk.Button(bar, text="Export CSV…", command=self.export_report).pack(side="right")
        self.report_metrics = ttk.Frame(parent); self.report_metrics.pack(fill="x", pady=(0, 12))
        split = ttk.Panedwindow(parent, orient="horizontal"); split.pack(fill="both", expand=True)
        sold_frame = ttk.Frame(split, padding=8); orders_frame = ttk.Frame(split, padding=8); split.add(sold_frame, weight=2); split.add(orders_frame, weight=3)
        ttk.Label(sold_frame, text="Best-selling dishes", style="Header.TLabel").pack(anchor="w", pady=(0, 8))
        self.sold_tree = ttk.Treeview(sold_frame, columns=("qty", "sales"), show="headings")
        for col, label in [("name", "Dish"), ("qty", "Qty sold"), ("sales", "Sales")]: self.sold_tree.heading(col, text=label)
        self.sold_tree.column("name", width=200); self.sold_tree.column("qty", width=75, anchor="center"); self.sold_tree.column("sales", width=105, anchor="e")
        self.sold_tree.pack(fill="both", expand=True)
        ttk.Label(orders_frame, text="Recent orders", style="Header.TLabel").pack(anchor="w", pady=(0, 8))
        self.orders_tree = ttk.Treeview(orders_frame, columns=("when", "type", "total"), show="headings")
        for col, label in [("when", "Date / time"), ("type", "Service / table"), ("total", "Total")]: self.orders_tree.heading(col, text=label)
        self.orders_tree.column("when", width=150); self.orders_tree.column("type", width=150); self.orders_tree.column("total", width=100, anchor="e")
        self.orders_tree.pack(fill="both", expand=True)
        self.refresh_reports()

    def show_all_time_report(self):
        self.report_start.set(""); self.report_end.set(""); self.refresh_reports()

    def refresh_reports(self):
        if not hasattr(self, "report_metrics") or not self.report_metrics.winfo_exists(): return
        start, end = self.report_start.get().strip(), self.report_end.get().strip()
        try:
            for value in (start, end):
                if value: date.fromisoformat(value)
            summary, sold, recent = self.store.report_summary(start, end)
        except ValueError:
            messagebox.showerror("Date format", "Enter dates as YYYY-MM-DD."); return
        for child in self.report_metrics.winfo_children(): child.destroy()
        for label, value in [("Completed orders", str(summary["n"])), ("Food sales", f"₹ {summary['sales']:,.2f}"), ("GST collected", f"₹ {summary['gst']:,.2f}")]:
            card = ttk.Frame(self.report_metrics, style="Card.TFrame", padding=13); card.pack(side="left", fill="x", expand=True, padx=(0, 10))
            ttk.Label(card, text=label, style="Muted.TLabel").pack(anchor="w"); ttk.Label(card, text=value, style="Card.TLabel", font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(5, 0))
        for tree in (self.sold_tree, self.orders_tree):
            for iid in tree.get_children(): tree.delete(iid)
        for row in sold: self.sold_tree.insert("", "end", values=(row["dish_name"], row["quantity"], f"₹ {row['sales']:,.2f}"))
        for row in recent:
            when = datetime.fromisoformat(row["created_at"]).astimezone().strftime("%d %b %Y %H:%M")
            service = row["service_type"] + (f" · {row['table_name']}" if row["table_name"] else "")
            self.orders_tree.insert("", "end", values=(when, service, f"₹ {row['total']:,.2f}"))

    def export_report(self):
        self.refresh_reports()
        path = filedialog.asksaveasfilename(title="Export sales report", initialfile=f"sales-report-{date.today().isoformat()}.csv", defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not path: return
        summary, sold, recent = self.store.report_summary(self.report_start.get().strip(), self.report_end.get().strip())
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f); writer.writerow(["Sales report", self.report_start.get(), self.report_end.get()]); writer.writerow(["Orders", summary["n"], "Sales", summary["sales"], "GST", summary["gst"]]); writer.writerow([]); writer.writerow(["Dish", "Quantity sold", "Sales"])
            for row in sold: writer.writerow([row["dish_name"], row["quantity"], row["sales"]])
            writer.writerow([]); writer.writerow(["Order ID", "Date", "Service", "Table", "Subtotal", "GST", "Total"])
            for row in recent: writer.writerow([row["id"], row["created_at"], row["service_type"], row["table_name"], row["subtotal"], row["gst"], row["total"]])
        messagebox.showinfo("Report exported", f"Saved report to:\n{path}")

    def refresh_casual_categories(self):
        if hasattr(self, "category_box") and self.category_box.winfo_exists(): self.render_categories()

    def refresh_casual_menu(self):
        if hasattr(self, "dish_frame") and self.dish_frame.winfo_exists(): self.render_dishes()

    def on_close(self):
        self.store.close(); self.root.destroy()


class CategoryDialog(simpledialog.Dialog):
    def __init__(self, parent, title, name="", description=""):
        self.initial = (name, description); self.result = None
        super().__init__(parent, title)

    def body(self, master):
        ttk.Label(master, text="Category name").grid(row=0, column=0, sticky="w", pady=5)
        self.name = ttk.Entry(master, width=40); self.name.grid(row=0, column=1, pady=5); self.name.insert(0, self.initial[0])
        ttk.Label(master, text="Description").grid(row=1, column=0, sticky="w", pady=5)
        self.description = ttk.Entry(master, width=40); self.description.grid(row=1, column=1, pady=5); self.description.insert(0, self.initial[1])
        return self.name

    def validate(self):
        if not self.name.get().strip():
            messagebox.showerror("Missing name", "Enter a category name.", parent=self); return False
        return True

    def apply(self):
        self.result = (self.name.get().strip(), self.description.get().strip())


class DishDialog(simpledialog.Dialog):
    def __init__(self, parent, categories, selected_category_id, dish=None):
        self.categories = categories; self.selected_category_id = selected_category_id; self.dish = dish; self.result = None
        super().__init__(parent, "Edit dish" if dish else "Add dish")

    def body(self, master):
        names = [c["name"] for c in self.categories]
        existing = next((c["name"] for c in self.categories if c["id"] == (self.dish["category_id"] if self.dish else self.selected_category_id)), names[0])
        ttk.Label(master, text="Category").grid(row=0, column=0, sticky="w", pady=6)
        self.category = tk.StringVar(value=existing); ttk.Combobox(master, textvariable=self.category, values=names, state="readonly", width=32).grid(row=0, column=1, pady=6)
        ttk.Label(master, text="Dish name").grid(row=1, column=0, sticky="w", pady=6)
        self.name = ttk.Entry(master, width=35); self.name.grid(row=1, column=1, pady=6); self.name.insert(0, self.dish["name"] if self.dish else "")
        ttk.Label(master, text="Price (₹)").grid(row=2, column=0, sticky="w", pady=6)
        self.price = ttk.Entry(master, width=35); self.price.grid(row=2, column=1, pady=6); self.price.insert(0, str(self.dish["price"]) if self.dish else "")
        ttk.Label(master, text="Description (optional)").grid(row=3, column=0, sticky="w", pady=6)
        self.description = ttk.Entry(master, width=35); self.description.grid(row=3, column=1, pady=6); self.description.insert(0, self.dish["description"] if self.dish else "")
        ttk.Label(master, text="Dish photo").grid(row=4, column=0, sticky="w", pady=6)
        self.photo = tk.StringVar(value=self.dish["image_path"] if self.dish else "")
        photo_row = ttk.Frame(master); photo_row.grid(row=4, column=1, sticky="ew", pady=6)
        ttk.Entry(photo_row, textvariable=self.photo, width=25).pack(side="left", fill="x", expand=True)
        ttk.Button(photo_row, text="Browse…", command=self.browse).pack(side="left", padx=(5, 0))
        return self.name

    def browse(self):
        path = filedialog.askopenfilename(parent=self, title="Choose dish photo", filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("All files", "*.*")])
        if path: self.photo.set(path)

    def validate(self):
        try: price = float(self.price.get().strip())
        except ValueError: messagebox.showerror("Invalid price", "Enter a valid price.", parent=self); return False
        if not self.name.get().strip() or price < 0:
            messagebox.showerror("Invalid dish", "Enter a dish name and a non-negative price.", parent=self); return False
        return True

    def apply(self):
        name = self.name.get().strip()
        price = float(self.price.get().strip())
        category_id = next(c["id"] for c in self.categories if c["name"] == self.category.get())
        self.result = (category_id, name, self.description.get().strip(), price, self.photo.get().strip())


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
