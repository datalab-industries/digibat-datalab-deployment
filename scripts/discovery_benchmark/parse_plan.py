"""Parse the CoinCellAssemble_250Plan.xlsx spreadsheet into structured DataFrames."""

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
    "Do_Formation": str,
    "Do_RateTest": str,
    "Do_EIS": str,
    "Anode_Mass_mg": float,
    "Cathode_Mass_mg": float,
    "ID_No": int,
    "Notes": str,
}

COLUMN_ALIASES = {
    "Cell_ID": "Cell_ID",
    "Batch": "Batch",
    "Category": "Category",
    "Cathode": "Cathode",
    "Cathode diameter (mm)": "Cathode_Diameter_mm",
    "Anode": "Anode",
    "Anode diameter (mm)": "Anode_Diameter_mm",
    "N/P ratio": "NP_Ratio",
    "Separator_Type": "Separator_Type",
    "Separator_Diameter_mm": "Separator_Diameter_mm",
    "Electrolyte": "Electrolyte",
    "Electrolyte_Volume_uL": "Electrolyte_Volume_uL",
    "Anode mass": "Anode_Mass_mg",
    "Cathode mass": "Cathode_Mass_mg",
    "ID no.": "ID_No",
}

BATCH2_EXTRA_COLUMNS = ["Channel", "Nominal_Capacity"]

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
    electrolytes: pd.DataFrame
    electrodes: pd.DataFrame
    echem_file_map: dict[int, list[Path]] = field(default_factory=dict)


def _parse_batches(path: Path, sheet_name: str) -> pd.DataFrame:
    df = pd.read_excel(
        path,
        sheet_name=sheet_name,
        keep_default_na=False,
        header=0,
    )
    df.rename(columns=COLUMN_ALIASES, inplace=True)
    return df.dropna(subset=["ID_No"]).reset_index(drop=True)


def _parse_chemicals(path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = pd.read_excel(path, sheet_name="Chemical information", header=None)

    electrolytes = raw.iloc[3:6, 0:6].copy()
    electrolytes.columns = ELECTROLYTE_COLUMNS
    electrolytes = electrolytes.reset_index(drop=True)

    electrodes = raw.iloc[15:23, 0:19].copy()
    electrodes.columns = ELECTRODE_COLUMNS
    electrodes = electrodes.reset_index(drop=True)

    return electrolytes, electrodes


_ID_PREFIX_RE = re.compile(r"^(\d+)_")

NEWARE_EXTENSIONS = {".xlsx"}


def map_echem_files(
    neware_dir: Path,
    known_ids: set[int],
) -> dict[int, list[Path]]:
    """Map cell ID numbers to their Neware echem data files.

    Walks *neware_dir* recursively and matches files whose name starts with
    a numeric ID present in *known_ids*.  Returns ``{id_no: [path, ...]}``.
    """
    result: dict[int, list[Path]] = {}
    for f in sorted(neware_dir.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in NEWARE_EXTENSIONS:
            continue
        m = _ID_PREFIX_RE.match(f.name)
        if m is None:
            continue
        cell_id = int(m.group(1))
        if cell_id in known_ids:
            result.setdefault(cell_id, []).append(f)
    return result


def parse_plan(path: Path, sheet_names: list[str], neware_dir: Path | None = None) -> PlanData:
    """Parse the full assembly plan spreadsheet.

    Returns a PlanData with:
    - cells: both batches concatenated on the shared CELL_COLUMNS schema
    - electrolytes: electrolyte product information
    - electrodes: electrode specification and supplier data
    - echem_file_map: {ID_No: [paths...]} mapping cells to Neware data files
    """
    batches = [_parse_batches(path, sheet_name) for sheet_name in sheet_names]

    shared = list(CELL_COLUMNS.keys())
    cells = pd.concat([b[shared] for b in batches], ignore_index=True)

    electrolytes, electrodes = _parse_chemicals(path)

    known_ids = set(pd.to_numeric(cells["ID_No"], errors="coerce").dropna().astype(int))
    echem_file_map: dict[int, list[Path]] = {}
    echem_file_map = map_echem_files(Path("data/Neware"), known_ids)

    return PlanData(
        cells=cells,
        electrolytes=electrolytes,
        electrodes=electrodes,
        echem_file_map=echem_file_map,
    )
