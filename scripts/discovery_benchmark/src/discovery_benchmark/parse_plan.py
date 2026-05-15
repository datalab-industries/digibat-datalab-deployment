"""Parse the CoinCellAssemble_250Plan spreadsheet (2026-05 revision).

The new layout has three sheets — ``Automated cells`` (cell metadata),
``Inventory`` (consumables/electrolytes/electrodes stacked vertically in
one sheet) and ``Equipment``. Old ``Manual Cells`` / ``Chemical
information`` sheets are gone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


# Cell columns we hand off to the ingestion side. Pandas inference handles the
# types; this dict is informational + a list of names that must exist on the
# aliased DataFrame.
CELL_COLUMNS: dict[str, type] = {
    "Item_ID": str,
    "Project": str,
    "Project_Number": int,
    "Category": str,
    "Cell_Name": str,
    "Cell_No": int,
    "Batch": int,
    "Testing_Procedure": str,
    "Assembler": str,
    "Cathode": str,
    "Cathode_Diameter_mm": float,
    "Cathode_Mass_mg": float,
    "Cathode_Mass_no_foil_mg": float,
    "Cathode_Active_Mass_mg": float,
    "Cathode_Capacity_mAh": float,
    "Cathode_Supplier": str,
    "Anode": str,
    "Anode_Diameter_mm": float,
    "Anode_Mass_mg": float,
    "Anode_Mass_no_foil_mg": float,
    "Anode_Active_Mass_mg": float,
    "Anode_Capacity_mAh": float,
    "Anode_Supplier": str,
    "NP_Ratio": float,
    "Separator_Type": str,
    "Separator_Diameter_mm": float,
    "Electrolyte": str,
    "Electrolyte_Supplier": str,
    "Electrolyte_Volume_uL": float,
    "Spacer_mm": float,
    "Repeat": int,
    "Cycler_Position": str,
    "Notes": str,
}

CELL_COLUMN_ALIASES: dict[str, str] = {
    "ID": "Item_ID",
    "Project": "Project",
    "Number": "Project_Number",
    "Category": "Category",
    "Cell name": "Cell_Name",
    "Cell no.": "Cell_No",
    "Batch": "Batch",
    "Testing procedure": "Testing_Procedure",
    "Assembler": "Assembler",
    "Cathode": "Cathode",
    "Cathode diameter (mm)": "Cathode_Diameter_mm",
    "Cathode weight (mg)": "Cathode_Mass_mg",
    "cathode weight no foil(mg)": "Cathode_Mass_no_foil_mg",
    "Active material": "Cathode_Active_Mass_mg",
    "Capacity (mAh)": "Cathode_Capacity_mAh",
    "Cathode supplier": "Cathode_Supplier",
    "Anode": "Anode",
    "Anode diameter (mm)": "Anode_Diameter_mm",
    "Anode weight (mg)": "Anode_Mass_mg",
    "No foil": "Anode_Mass_no_foil_mg",
    "Active material.1": "Anode_Active_Mass_mg",
    "Capacity (mAh).1": "Anode_Capacity_mAh",
    "Anode supplier": "Anode_Supplier",
    "N/P Ratio": "NP_Ratio",
    "Separator_Type": "Separator_Type",
    "Separator_Diameter_mm": "Separator_Diameter_mm",
    "Electrolyte": "Electrolyte",
    "Electrolyte supplier": "Electrolyte_Supplier",
    "Electrolyte_Volume_uL": "Electrolyte_Volume_uL",
    "Spacer_mm": "Spacer_mm",
    "Repeat": "Repeat",
    "Cycler position": "Cycler_Position",
    "Notes": "Notes",
}


_CELL_ID_RE = re.compile(r"^([pP]\d+)-([A-Za-z]+)-(\d+)$")


def _normalise_cell_id(s: object) -> str | None:
    """Upper-case the project segment and zero-pad the numeric suffix to 3
    digits, so ``p025-CEL-2`` → ``P025-CEL-002``."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    raw = str(s).strip()
    if not raw:
        return None
    m = _CELL_ID_RE.match(raw)
    if m:
        return f"{m.group(1).upper()}-{m.group(2).upper()}-{int(m.group(3)):03d}"
    return raw


@dataclass
class ConsumableRow:
    id: str
    name: str | None = None


@dataclass
class ElectrolyteRow:
    id: str
    product_code: str | None = None
    amount: str | None = None
    description: str | None = None
    supplier: str | None = None
    link: str | None = None


@dataclass
class ElectrodeRow:
    id: str
    product_code: str | None = None
    material: str | None = None
    coating: str | None = None
    current_collector: str | None = None
    dimensions: str | None = None
    supplier: str | None = None
    active_loading_mg_cm2: float | None = None
    coating_area_density_mg_cm2: float | None = None
    cc_area_density_mg_cm2: float | None = None
    compaction_density_g_cm3: float | None = None
    specific_capacity_mAh_g: float | None = None
    coated_area: str | None = None
    coating_thickness_um: float | None = None
    total_thickness_um: float | None = None
    active_material_weight_g: float | None = None
    active_material_proportion: float | None = None
    area_capacity_mAh_cm2: float | None = None
    note: str | None = None
    link: str | None = None


@dataclass
class Inventory:
    consumables: list[ConsumableRow] = field(default_factory=list)
    electrolytes: list[ElectrolyteRow] = field(default_factory=list)
    electrodes: list[ElectrodeRow] = field(default_factory=list)


@dataclass
class EquipmentRow:
    id: str
    status: str | None = None
    name: str | None = None
    date: str | None = None
    location: str | None = None
    manufacturer: str | None = None
    maintainer: str | None = None


@dataclass
class PlanData:
    cells: pd.DataFrame                # aliased columns
    cells_raw: pd.DataFrame            # original spreadsheet columns (for description rendering)
    inventory: Inventory
    equipment: list[EquipmentRow] = field(default_factory=list)


# ---------- parsing ----------

def _cell(v):
    """NaN/empty → None; pass everything else through unchanged."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, str) and not v.strip():
        return None
    return v


def _str(v) -> str | None:
    v = _cell(v)
    return None if v is None else str(v).strip()


def _float(v) -> float | None:
    v = _cell(v)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_cells(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = pd.read_excel(path, sheet_name="Automated cells", header=0, keep_default_na=True)
    raw.columns = raw.columns.astype(str).str.strip()
    raw = raw.dropna(subset=["ID"]).reset_index(drop=True)
    raw["ID"] = raw["ID"].map(_normalise_cell_id)
    if "Project" in raw.columns:
        raw["Project"] = raw["Project"].astype(str).str.upper().str.strip()

    aliased = raw.rename(columns=CELL_COLUMN_ALIASES).copy()
    return aliased, raw


def _parse_inventory(path: Path) -> Inventory:
    raw = pd.read_excel(path, sheet_name="Inventory", header=None)
    inv = Inventory()

    section_rows: dict[str, int] = {}
    for i, v in enumerate(raw.iloc[:, 0]):
        s = _str(v)
        if s in ("Consumables", "Electrolytes", "Electrode"):
            section_rows[s] = i

    def section_slice(name: str) -> pd.DataFrame:
        start = section_rows.get(name)
        if start is None:
            return raw.iloc[0:0]
        ends = [r for r in section_rows.values() if r > start]
        stop = min(ends) if ends else len(raw)
        # The row after the section header is column labels; data starts +2.
        return raw.iloc[start + 2 : stop].reset_index(drop=True)

    for _, r in section_slice("Consumables").iterrows():
        rid = _str(r.iloc[0])
        if not rid:
            continue
        inv.consumables.append(ConsumableRow(id=rid, name=_str(r.iloc[1])))

    for _, r in section_slice("Electrolytes").iterrows():
        rid = _str(r.iloc[0])
        if not rid:
            continue
        inv.electrolytes.append(
            ElectrolyteRow(
                id=rid,
                product_code=_str(r.iloc[1]),
                amount=_str(r.iloc[2]),
                description=_str(r.iloc[3]),
                supplier=_str(r.iloc[5]),
                link=_str(r.iloc[6]),
            )
        )

    for _, r in section_slice("Electrode").iterrows():
        rid = _str(r.iloc[0])
        if not rid:
            continue
        inv.electrodes.append(
            ElectrodeRow(
                id=rid,
                product_code=_str(r.iloc[1]),
                material=_str(r.iloc[2]),
                coating=_str(r.iloc[3]),
                current_collector=_str(r.iloc[4]),
                dimensions=_str(r.iloc[5]),
                supplier=_str(r.iloc[6]),
                active_loading_mg_cm2=_float(r.iloc[7]),
                coating_area_density_mg_cm2=_float(r.iloc[8]),
                cc_area_density_mg_cm2=_float(r.iloc[9]),
                compaction_density_g_cm3=_float(r.iloc[10]),
                specific_capacity_mAh_g=_float(r.iloc[11]),
                coated_area=_str(r.iloc[13]),
                coating_thickness_um=_float(r.iloc[14]),
                total_thickness_um=_float(r.iloc[15]),
                active_material_weight_g=_float(r.iloc[16]),
                active_material_proportion=_float(r.iloc[17]),
                area_capacity_mAh_cm2=_float(r.iloc[19]),
                note=_str(r.iloc[20]),
                link=_str(r.iloc[21]),
            )
        )

    return inv


def _parse_equipment(path: Path) -> list[EquipmentRow]:
    df = pd.read_excel(path, sheet_name="Equipment", header=0)
    df.columns = df.columns.astype(str).str.strip()
    out: list[EquipmentRow] = []
    for _, r in df.iterrows():
        rid = _str(r.get("ID"))
        if not rid:
            continue
        date_val = r.get("Date")
        date_str: str | None
        if isinstance(date_val, pd.Timestamp):
            date_str = date_val.date().isoformat()
        else:
            date_str = _str(date_val)
        out.append(
            EquipmentRow(
                id=rid,
                status=_str(r.get("Status")),
                name=_str(r.get("Name")),
                date=date_str,
                location=_str(r.get("Location")),
                manufacturer=_str(r.get("Manufacturer")),
                maintainer=_str(r.get("Maintainer")),
            )
        )
    return out


def parse_plan(path: Path) -> PlanData:
    path = Path(path)
    cells, cells_raw = _parse_cells(path)
    inventory = _parse_inventory(path)
    equipment = _parse_equipment(path)
    return PlanData(cells=cells, cells_raw=cells_raw, inventory=inventory, equipment=equipment)


# ---------- file matchers ----------

_HIDDEN = {"desktop.ini", ".DS_Store"}


def _real_files(root: Path):
    if not root.exists():
        return
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if f.name in _HIDDEN or f.name.startswith("."):
            continue
        yield f


def map_files_by_number(
    root: Path,
    numbers: set[int],
    prefix: str,
    extensions: set[str] | None = None,
) -> dict[int, list[Path]]:
    """Match files like ``{prefix}-{N}-...`` where N is in *numbers*.

    Used for Neware (``CEL-100-...``) and EIS (``P025-CEL-100-...``).
    """
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)(?=[-_. ])", re.IGNORECASE)
    out: dict[int, list[Path]] = {}
    for f in _real_files(root):
        if extensions and f.suffix.lower() not in extensions:
            continue
        m = pat.match(f.name)
        if not m:
            continue
        n = int(m.group(1))
        if n in numbers:
            out.setdefault(n, []).append(f)
    return out


def map_files_by_legacy_id(
    root: Path,
    cell_nos: set[int],
    extensions: set[str] | None = None,
) -> dict[int, list[Path]]:
    """Match files whose basename starts with ``{Cell_No}_...`` (CV)."""
    pat = re.compile(r"^(\d+)(?=[_. -])")
    out: dict[int, list[Path]] = {}
    for f in _real_files(root):
        if extensions and f.suffix.lower() not in extensions:
            continue
        m = pat.match(f.name)
        if not m:
            continue
        n = int(m.group(1))
        if n in cell_nos:
            out.setdefault(n, []).append(f)
    return out


def map_dirs_by_legacy_id(root: Path, cell_nos: set[int]) -> dict[int, list[Path]]:
    """Map cell_nos to files under ``root/<cell_no>`` or ``root/<cell_no>_<n>``."""
    pat = re.compile(r"^(\d+)(?:_\d+)?$")
    out: dict[int, list[Path]] = {}
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        m = pat.match(d.name)
        if not m:
            continue
        n = int(m.group(1))
        if n not in cell_nos:
            continue
        for f in _real_files(d):
            out.setdefault(n, []).append(f)
    return out


def map_files_by_eld(
    root: Path,
    eld_ids: set[str],
    extensions: set[str] | None = None,
    recursive: bool = False,
) -> dict[str, list[Path]]:
    """Match files like ``ELD-NNN-...`` against the supplied ELD ids.

    *eld_ids* must be uppercased (e.g. ``{"ELD-001", "ELD-010"}``).
    """
    pat = re.compile(r"^(ELD-\d+)(?=[-_. ])", re.IGNORECASE)
    out: dict[str, list[Path]] = {}
    if not root.exists():
        return out
    iterator = root.rglob("*") if recursive else root.iterdir()
    for f in sorted(iterator):
        if not f.is_file():
            continue
        if f.name in _HIDDEN or f.name.startswith("."):
            continue
        if extensions and f.suffix.lower() not in extensions:
            continue
        m = pat.match(f.name)
        if not m:
            continue
        key = m.group(1).upper()
        if key in eld_ids:
            out.setdefault(key, []).append(f)
    return out


def map_subdirs_by_eld(root: Path, eld_ids: set[str]) -> dict[str, list[Path]]:
    """Walk ``root/ELD-NNN-{name}/...`` and group files under their ELD id."""
    pat = re.compile(r"^(ELD-\d+)\b", re.IGNORECASE)
    out: dict[str, list[Path]] = {}
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        m = pat.match(d.name)
        if not m:
            continue
        key = m.group(1).upper()
        if key not in eld_ids:
            continue
        for f in _real_files(d):
            out.setdefault(key, []).append(f)
    return out
