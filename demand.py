"""
demand.py

Demand calculation and reorder reporting.

Key behaviors:
- Daily demand uses MAX-per-line, then SUM across lines.
- Waste % is applied from Kits.csv (per kit / per line).
- Lead time is taken from INV-01.csv column: "LeadTimeDays"
- Low Stock Report prints ONLY: Component, On hand, ROP (below-ROP items only).
- Full Reorder Report is color-coded:
    - RED if On hand < ROP
    - GREEN if On hand >= ROP
  and includes a per-line demand explanation + ROP math.

Compatibility:
- Provides load_line_capacity() and edit_line_capacity() for main.py / health_check.py.
"""

import pandas as pd

from file_utils import load_csv_strip, save_csv
from helpers import Color, menu_title

INVENTORY_FILE = "INV-01.csv"
KITS_FILE = "Kits.csv"
LINE_CAPACITY_FILE = "LineCapacity.csv"
LINE_SETTINGS_FILE = "LineSettings.csv"


# ------------------------------------------------------------------
# Compatibility helpers (used by health_check.py / main.py)
# ------------------------------------------------------------------
def load_line_capacity() -> pd.DataFrame:
    """Load LineCapacity.csv as a DataFrame (compat for health_check)."""
    try:
        return load_csv_strip(LINE_CAPACITY_FILE)
    except Exception:
        return pd.DataFrame()


def load_line_settings() -> dict:
    """
    Returns dict mapping line number -> line name, using LineSettings.csv if present.
    Falls back to empty dict.
    """
    try:
        df = load_csv_strip(LINE_SETTINGS_FILE)
        if df.empty:
            return {}
        # Support either schema:
        #   Line,LineName
        # or legacy:
        #   Line,PalletsPerDay (ignore)
        if "Line" in df.columns and "LineName" in df.columns:
            out = {}
            for _, r in df.iterrows():
                k = str(r["Line"]).strip()
                v = str(r["LineName"]).strip()
                if k and v:
                    out[k] = v
            return out
        return {}
    except Exception:
        return {}


def edit_line_capacity() -> None:
    """
    Simple editor for LineCapacity.csv (MaxPalletsPerDay per line).
    Keeps schema: Line,MaxPalletsPerDay
    """
    menu_title("Edit Line Capacity")
    df = load_line_capacity()

    # Ensure schema
    if df.empty:
        df = pd.DataFrame(
            {"Line": ["1", "2", "3", "4"], "MaxPalletsPerDay": [0.0, 0.0, 0.0, 0.0]}
        )
    else:
        if "Line" not in df.columns:
            df["Line"] = ""
        if "MaxPalletsPerDay" not in df.columns:
            df["MaxPalletsPerDay"] = 0.0

    # Normalize
    df["Line"] = df["Line"].astype(str).str.strip()
    df["MaxPalletsPerDay"] = pd.to_numeric(
        df["MaxPalletsPerDay"], errors="coerce"
    ).fillna(0.0)

    line_names = load_line_settings()

    print("\nCurrent capacities:")
    for _, r in df.iterrows():
        line = str(r["Line"])
        name = line_names.get(line, "")
        label = f"Line {line}" + (f" – {name}" if name else "")
        print(f"  {label}: {float(r['MaxPalletsPerDay']):.2f} pallets/day")

    print("\nEnter updates (press ENTER to skip a line).")
    for i in range(len(df)):
        line = str(df.loc[i, "Line"])
        name = line_names.get(line, "")
        label = f"Line {line}" + (f" – {name}" if name else "")
        new_val = input(f"New MaxPalletsPerDay for {label}: ").strip()
        if new_val == "":
            continue
        try:
            df.loc[i, "MaxPalletsPerDay"] = float(new_val)
        except ValueError:
            print(Color.RED + "Invalid number — skipped.\n" + Color.RESET)

    save_csv(df, LINE_CAPACITY_FILE)
    print(Color.GREEN + "\n✔ Line capacity updated.\n" + Color.RESET)


# ------------------------------------------------------------------
# Core demand logic
# ------------------------------------------------------------------
def calculate_daily_usage():
    """
    Calculates daily usage per component using:
    - MAX demand per line
    - SUM across lines

    Returns:
        final_usage: dict[str, float]
        usage_explain: dict[str, dict[str, dict]]
    """
    kits = load_csv_strip(KITS_FILE)
    caps = load_csv_strip(LINE_CAPACITY_FILE)

    final_usage: dict[str, float] = {}
    usage_explain: dict[str, dict[str, dict]] = {}

    if kits.empty or caps.empty:
        return final_usage, usage_explain

    # Normalize caps
    caps = caps.copy()
    caps["Line"] = caps["Line"].astype(str).str.strip()
    caps["MaxPalletsPerDay"] = pd.to_numeric(
        caps["MaxPalletsPerDay"], errors="coerce"
    ).fillna(0.0)

    for _, kit in kits.iterrows():
        component = str(kit.get("Component", "")).strip()
        line = str(kit.get("Line", "")).strip()
        product = str(kit.get("Finished Product", "")).strip()

        if not component or not line:
            continue

        cap_row = caps[caps["Line"] == line]
        if cap_row.empty:
            continue

        pallets_day = float(cap_row.iloc[0]["MaxPalletsPerDay"])
        if pallets_day <= 0:
            # Inactive line contributes no demand and therefore will not appear as "used by".
            continue

        units_per_pallet = float(kit.get("UnitsPerPallet", 1) or 1)
        qty_per_unit = float(kit.get("Qty Per Production Unit", 1) or 1)
        waste = float(kit.get("Waste %", 0) or 0) / 100.0

        daily = pallets_day * units_per_pallet * qty_per_unit * (1 + waste)

        usage_explain.setdefault(component, {})
        prev = float(usage_explain[component].get(line, {}).get("daily", 0) or 0)

        # MAX per line
        if daily > prev:
            usage_explain[component][line] = {
                "product": product,
                "daily": daily,
                "pallets": pallets_day,
                "units_per_pallet": units_per_pallet,
                "qty_per_unit": qty_per_unit,
                "waste": waste,
            }

    # SUM across lines
    for comp, lines in usage_explain.items():
        final_usage[comp] = sum(v["daily"] for v in lines.values())

    return final_usage, usage_explain


# ------------------------------------------------------------------
# Reorder / Low Stock Reports
# ------------------------------------------------------------------
def reorder_report(low_stock_only: bool = False):
    menu_title("Low Stock Report" if low_stock_only else "Full Reorder Report")

    inv = load_csv_strip(INVENTORY_FILE)
    daily_usage, explain = calculate_daily_usage()

    if inv.empty:
        print(Color.YELLOW + "\nNo inventory data.\n" + Color.RESET)
        return

    for _, row in inv.iterrows():
        component = str(row.get("Component", "")).strip()
        if not component:
            continue

        on_hand = float(row.get("Quantity", 0) or 0)

        # IMPORTANT: exact column name
        lead_time = float(row.get("LeadTimeDays", 0) or 0)

        daily = float(daily_usage.get(component, 0) or 0)
        rop = daily * lead_time

        below = on_hand < rop

        # Low Stock Report shows ONLY below-ROP items
        if low_stock_only and not below:
            continue

        color = Color.RED if below else Color.GREEN

        print("\n" + "=" * 45)

        # Header
        if low_stock_only:
            print(f"Component: {component}")
        else:
            print(color + f"Component: {component}" + Color.RESET)

        # Minimal low-stock output (your requested final format)
        if low_stock_only:
            print(f"On hand: {on_hand:.2f}")
            print(f"ROP: {rop:.2f}")
            continue

        # Full report (colored values)
        print(color + f"On hand: {on_hand:.2f}" + Color.RESET)
        print(color + f"ROP: {rop:.2f}" + Color.RESET)

        # Full explanation (kept for the full report)
        if component in explain:
            print("\nDemand drivers:")
            for line in sorted(explain[component].keys(), key=lambda x: (len(x), x)):
                d = explain[component][line]
                waste_pct = int(round(float(d["waste"]) * 100))
                formula = (
                    f"{d['pallets']:.0f} × {d['units_per_pallet']:.0f} × "
                    f"{d['qty_per_unit']:.0f} × (1 + {waste_pct}%) = {d['daily']:.2f}"
                )
                print(f"  Line {line} – {d['product']}")
                print(f"    {formula}")

            print("\nROP calculation:")
            print(f"  {daily:.2f} × {lead_time:.0f} = {rop:.2f}")

    print("\nEnd of report.\n")
