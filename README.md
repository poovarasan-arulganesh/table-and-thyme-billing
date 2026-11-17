# Table & Thyme Billing

An offline-first restaurant billing desktop app for Windows. Its screens use the HTML/CSS layout from the Table & Thyme mockup inside a desktop window, while orders, menu data, table settings, photos, and reports are stored locally in SQLite. No account or network connection is needed after installation.

## Features

- Casual mode with menu search, category filters, photo dish cards, prominent add buttons, quantity controls, order totals, and a 5% GST estimate.
- Dine-in and delivery orders, with the dine-in table selector synchronized with Admin mode.
- Printable GST bill preview and save/print action. The sample bill does not include a restaurant name.
- Admin mode for creating, renaming, and removing categories, dishes, and tables. Dish records include name, price, optional description, and an uploaded photo.
- Sales reports with a date range or all-time view, completed order count, food sales, GST collected, best-selling dishes, and recent orders.
- Export reports to CSV.
- Light and dark themes.
- Local-only storage so the app works without an internet connection after installation.

## Run from source

Python 3.10 or newer is required. The desktop window uses pywebview and the WebView2 runtime on Windows.

```bash
python -m pip install -r requirements.txt
python restaurant_billing/app.py
```

## Build the Windows executable

On Windows, install Python 3.12 and the Microsoft Edge WebView2 Runtime, then run:

```powershell
python -m pip install -r requirements.txt
pyinstaller --noconfirm --clean --onefile --windowed --name TableAndThymeBilling --collect-all webview --add-data "restaurant_billing/ui;restaurant_billing/ui" restaurant_billing/app.py
```

The standalone app is created under `dist/TableAndThymeBilling.exe`. The GitHub Actions workflow `.github/workflows/windows-release.yml` builds the Windows executable when a `v*.*.*` tag is pushed and attaches it to the GitHub Release.

## Data location

- Windows: `%APPDATA%\TableAndThymeBilling\`
- macOS/Linux: `~/.local/share/TableAndThymeBilling/` (or `$XDG_DATA_HOME` when set)

The folder contains `restaurant.sqlite3` and uploaded dish photos. Back up this folder to preserve the restaurant's records.

## Release

Tag a version such as `v1.1.1` and push the tag. GitHub Actions builds and publishes `TableAndThymeBilling.exe` to that release.
