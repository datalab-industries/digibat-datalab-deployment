"""Parse the CoinCellAssemble_250Plan spreadsheet into structured DataFrames."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

CELL_COLUMNS: dict[str, type] = {
    "Cell_ID": str,
    "Batch": int,
    "Category": str,
    "Cathode": str,
    "Cathode_Diameter_mm": float,
    "Anode": str,
    "Anode_Diameter_mm": float,
    "NP_Ratio": float,
    "Separator_Type": str,
    "Separator_Diameter_mm": float,
    "Electrolyte": str,
    "Electrolyte_Volume_uL": float,
    "Spacer_mm": float,
    "Repeat": int,
    "Anode_Mass_mg": float,
    "Cathode_Mass_mg": float,
    "Cycler_Position": str,
    "Notes": str,
    "Nominal_Capacity_mAh": float,
    "ID_No": int,
}

COLUMN_ALIASES = {
    "Cathode diameter (mm)": "Cathode_Diameter_mm",
    "Anode diameter (mm)": "Anode_Diameter_mm",
    "N/P ratio": "NP_Ratio",
    "anode mass": "Anode_Mass_mg",
    "cathode mass": "Cathode_Mass_mg",
    "Cycler position": "Cycler_Position",
    "capacity": "Nominal_Capacity_mAh",
    "ID no.": "ID_No",
}

ELECTROLYTE_COLUMNS = ["Name", "Product_No", "Amount", "Description", "Supplier", "Link"]

ELECTRODE_COLUMNS = [
    "Name",
    "Product_Code",
    "Material",
    "Coating",
    "Current_Collector",
    "Dimensions",
    "Supplier",
    "Areal_Density_mg_cm2",
    "Compaction_Density_g_cm3",
    "Collector_Areal_Density_mg_cm2",
    "Coated_Area",
    "Coating_Thickness_um",
    "Total_Thickness_um",
    "Active_Material_Weight_g",
    "Active_Material_Proportion",
    "Specific_Capacity_mAh_g",
    "Areal_Capacity_mAh_cm2",
    "Link",
    "Note",
]


@dataclass
class PlanData:
    """All parsed data from the coin cell assembly plan spreadsheet."""

    cells: pd.DataFrame
    cells_raw: pd.DataFrame
    electrolytes: pd.DataFrame
    electrodes: pd.DataFrame
    echem_file_map: dict[int, list[Path]] = field(default_factory=dict)


def _parse_cells(path: Path, sheet_name: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (aliased, raw) dataframes. Rows without an ID no. are dropped."""
    raw = pd.read_excel(path, sheet_name=sheet_name, header=0, keep_default_na=True)
    raw.columns = raw.columns.astype(str).str.strip()
    raw = raw.dropna(subset=["ID no."]).reset_index(drop=True)
    aliased = raw.rename(columns=COLUMN_ALIASES).copy()
    return aliased, raw


def _parse_chemicals(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = pd.read_excel(path, sheet_name="Chemical information", header=None)

    electrolytes = raw.iloc[3:6, 0:6].copy()
    electrolytes.columns = ELECTROLYTE_COLUMNS
    electrolytes = electrolytes.reset_index(drop=True)

    electrodes = raw.iloc[15:23, 0:19].copy()
    electrodes.columns = ELECTRODE_COLUMNS
    electrodes = electrodes.reset_index(drop=True)

    return electrolytes, electrodes


_ID_PREFIX_RE = re.compile(r"^(\d+)(?:[_.-]|$)")
_ID_DIR_RE = re.compile(r"^(\d+)(?:_\d+)?$")


def map_files_by_id(
    root: Path,
    known_ids: set[int],
    extensions: set[str] | None = None,
) -> dict[int, list[Path]]:
    """Map numeric cell IDs to files whose basename starts with that ID.

    Walks *root* recursively. If *extensions* is provided, files are filtered
    by lowercase suffix (e.g. ``{".mpr", ".ndax"}``). A file matches an ID
    when its basename starts with ``<digits>`` followed by ``_``, ``.``,
    ``-`` or end-of-name, and those digits are in *known_ids*.
    """
    result: dict[int, list[Path]] = {}
    if not root.exists():
        return result
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        if extensions is not None and f.suffix.lower() not in extensions:
            continue
        m = _ID_PREFIX_RE.match(f.name)
        if m is None:
            continue
        cell_id = int(m.group(1))
        if cell_id in known_ids:
            result.setdefault(cell_id, []).append(f)
    return result


def map_dirs_by_id(root: Path, known_ids: set[int]) -> dict[int, list[Path]]:
    """Map cell IDs to all files under ``root/<id>`` or ``root/<id>_<n>``."""
    result: dict[int, list[Path]] = {}
    if not root.exists():
        return result
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        m = _ID_DIR_RE.match(d.name)
        if m is None:
            continue
        cell_id = int(m.group(1))
        if cell_id not in known_ids:
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file():
                result.setdefault(cell_id, []).append(f)
    return result


def parse_plan(
    path: Path,
    sheet_name: str = "Automated cells",
    neware_dir: Path | None = None,
) -> PlanData:
    """Parse the full assembly plan spreadsheet."""
    cells, cells_raw = _parse_cells(path, sheet_name)
    electrolytes, electrodes = _parse_chemicals(path)

    known_ids = set(
        pd.to_numeric(cells["ID_No"], errors="coerce").dropna().astype(int)
    )
    echem_file_map: dict[int, list[Path]] = {}
    if neware_dir is not None:
        echem_file_map = map_files_by_id(
            neware_dir, known_ids, extensions={".xlsx", ".nda", ".ndax"}
        )

    return PlanData(
        cells=cells,
        cells_raw=cells_raw,
        electrolytes=electrolytes,
        electrodes=electrodes,
        echem_file_map=echem_file_map,
    )
