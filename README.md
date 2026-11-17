# HIGHWAY29 Billing

An offline-first restaurant billing app for Windows. The billing computer stores menu data, tables, reports, and bills locally in SQLite, and hosts a private Waiter screen on the restaurant Wi-Fi for tablets.

## Features

- Casual mode with menu search, category filters, photo dish cards, prominent add buttons, quantity controls, order totals, and a 5% GST estimate.
- Waiter orders for multiple tables, saved to a shared open-order list. Waiters can reopen and edit orders, then send them to the Windows Biller queue. Each send starts a fresh order for the next table.
- The Windows Biller screen groups orders by waiter. Select a waiter to review prices, add or edit dishes, cancel an individual table order, mark food ready, and print its GST receipt. Tablets cannot create or print bills.
- The Windows Biller screen refreshes its waiter list every 30 seconds. The tablet Waiter route shows only the waiter interface, uses mobile and tablet layouts, displays each dish quantity on its menu button, and disables double-tap zoom.
- The waiter QR and tablet link appear in the Windows app’s Waiter screen. The mobile/tablet Waiter page hides the QR and link.
- Dine-in and delivery orders, with the dine-in table selector synchronized with Admin mode.
- Printable GST bill preview and save/print action. The sample bill does not include a restaurant name.
- Admin mode for creating, renaming, and removing categories, dishes, and tables. Dish records include name, price, optional description, and an uploaded photo.
- Sales reports with a date range or all-time view, completed order count, GST collected, best-selling dishes, and recent orders.
- Export date filtered order lists and GST summaries to Excel, or save the report as CSV. Resetting sales history deletes completed bills and archived waiter orders while keeping unbilled waiter orders.
- Light and dark themes.
- Orders entered on a tablet during a temporary connection loss are kept in that browser and sync when the billing computer is reachable again. Bills can be finalized after reconnecting.
- Local storage keeps the billing computer working during an internet outage. Waiter tablets still need the restaurant Wi-Fi connection to reach the billing computer.

## Run from source

Python 3.10 or newer is required. The desktop window uses pywebview and the WebView2 runtime on Windows. The restaurant computer hosts the app on its local network at port `8765`.

```bash
python -m pip install -r requirements.txt
python restaurant_billing/app.py
```

## Build the Windows executable

On Windows, install Python 3.12 and the Microsoft Edge WebView2 Runtime, then run:

```powershell
python -m pip install -r requirements.txt
pyinstaller --noconfirm --clean --onefile --windowed --name HIGHWAY29Billing --collect-all webview --collect-all flask --collect-all waitress --collect-all qrcode --collect-all PIL --collect-all openpyxl --add-data "restaurant_billing/ui;restaurant_billing/ui" restaurant_billing/app.py
```

The standalone app is created under `dist/HIGHWAY29Billing.exe`. Start it on the restaurant computer, keep the devices on the same Wi-Fi network, then open the Tablet link shown in the app on each waiter tablet. If Windows Firewall asks, allow access on private networks. The app remains usable on the billing computer when the internet is unavailable; waiter tablets still need a working local Wi-Fi connection to reach it.

The GitHub Actions workflow `.github/workflows/windows-release.yml` builds the Windows executable when a `v*.*.*` tag is pushed and attaches it to the GitHub Release.

## Data location

- Windows: `%APPDATA%\TableAndThymeBilling\`
- macOS/Linux: `~/.local/share/TableAndThymeBilling/` (or `$XDG_DATA_HOME` when set)

The folder contains `restaurant.sqlite3` and uploaded dish photos. Back up this folder to preserve the restaurant's records.

## Release

Tag a version such as `v1.7.0` and push the tag. GitHub Actions builds and publishes `HIGHWAY29Billing.exe` to that release.
