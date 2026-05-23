"""Idempotent ingestion of the DIGIBAT Discovery Benchmark into datalab."""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
from datalab_api import DatalabClient

from .parse_plan import (
    ConsumableRow,
    ElectrodeRow,
    ElectrolyteRow,
    PlanData,
    map_dirs_by_legacy_id,
    map_files_by_eld,
    map_files_by_legacy_id,
    map_files_by_number,
    map_subdirs_by_eld,
    parse_plan,
)
from .utils import (
    TimedOp,
    configure_logging,
    ensure_block,
    ensure_collection,
    existing_block_titles,
    existing_file_names,
    reconcile_block_type,
    reset_current_cell,
    set_current_cell,
    try_rename_block,
    upload_file_only,
    upload_if_new,
    upsert_item,
)

logger = logging.getLogger("discovery_benchmark")


# ---------- configuration ----------

PROJECT = "P025"

# Date used when neither Cellerate nor Neware mtime supply one.
FALLBACK_CELL_DATE = datetime(2026, 4, 20)

DATA_DIR = Path("data")
PLAN_PATH = DATA_DIR / "CoinCellAssemble_250Plan_20260506.xlsx"

NEWARE_DIR = DATA_DIR / "Neware"
EIS_DIR = DATA_DIR / "EIS"
CV_DIR = DATA_DIR / "CV"
CELLERATE_DIR = DATA_DIR / "Cellerate" / "Labelled"
SEM_DIR = DATA_DIR / "SEM"
TEM_DIR = DATA_DIR / "TEM"
XRD_DIR = DATA_DIR / "XRD"
XPS_DIR = DATA_DIR / "XPS"
XPS_SUBDIR = XPS_DIR / "Digibat"  # ELD-XXX subdirectories with VGD scans

CELL_COLLECTION = "Discovery-Benchmark"
MATERIALS_COLLECTION = "Discovery-Benchmark-Materials"
ELECTRODES_COLLECTION = "Discovery-Benchmark-Electrodes"
INKS_COLLECTION = "Discovery-Benchmark-Inks"
REPO_URL = "https://github.com/datalab-industries/digibat-datalab-deployment"
INGESTION_SUBPATH = "tree/main/scripts/discovery_benchmark"

FUNDING_NOTE = (
    "<p><i>This work was supported by the "
    '<a href="https://www.royce.ac.uk/">Henry Royce Institute</a>.</i></p>'
)

CELL_COLLECTION_DESCRIPTION = f"""\
<h2>DIGIBAT Discovery Benchmark</h2>
<p>
  A reference collection of ~250 lithium-ion coin cells assembled to
  systematically span common cathode/anode/electrolyte chemistries,
  produced by the DIGIBAT project as a shared benchmark for
  battery-informatics tooling. Each cell is characterised both at the
  electrode (precursor) level and at the cell level.
</p>

<h3>Mass-correction note (May 2026 revision)</h3>
<p>
  The original charge/discharge capacities recorded in cell-side files
  used the total electrode disc mass (e.g. 23 mg) rather than the active
  material mass after subtracting current collector foil (e.g. 20 mg).
  Capacities have been recalculated; the corrected nominal capacity is
  shown on each cell record as <code>characteristic_mass</code> /
  <code>nominal_voltage</code>. Some cells turn out to be limited on
  the carbon side after recalculation — for those the nominal capacity
  is taken as <code>min(cathode, anode)</code>. The per-file raw
  capacity values in the cycling exports are therefore <i>stale</i>;
  the cell-record value is the source of truth.
</p>

<h3>Characterisation</h3>
<ul>
  <li>Cycling — Neware (<code>.xlsx</code>) and BioLogic CV/EIS
      (<code>.mpr</code>) attach to the <i>cell</i>.</li>
  <li>Cellerate assembly photos attach to the cell as raw files.</li>
  <li>SEM/TEM micrographs, XRD patterns, XPS scans attach to the
      <i>electrode</i> precursors (P025-ELD-…), not to individual
      cells.</li>
</ul>

<p>
  The ingestion code that produced this collection lives at
  <a href="{REPO_URL}/{INGESTION_SUBPATH}">{REPO_URL}/{INGESTION_SUBPATH}</a>.
</p>

{FUNDING_NOTE}
"""

MATERIALS_COLLECTION_DESCRIPTION = f"""\
<h2>Discovery Benchmark — Materials</h2>
<p>Electrolytes and consumables (separators, spacers, springs) used in
the Discovery Benchmark cells.</p>
{FUNDING_NOTE}
"""

ELECTRODES_COLLECTION_DESCRIPTION = f"""\
<h2>Discovery Benchmark — Electrodes</h2>
<p>Pre-coated electrode laminates from which Discovery Benchmark cells
were punched. SEM/TEM/XRD/XPS characterisation attaches here.</p>
{FUNDING_NOTE}
"""

INKS_COLLECTION_DESCRIPTION = f"""\
<h2>Discovery Benchmark — Inks</h2>
<p>Slurry / ink formulations associated with the Discovery Benchmark
project. Populated separately.</p>
{FUNDING_NOTE}
"""

ECHEM_BLOCK = "cycle"
EIS_BLOCK = "eis"
CV_BLOCK = "cv"
XRD_BLOCK = "xrd"
XPS_BLOCK = "xps"
MEDIA_BLOCK = "media"


# ---------- helpers ----------

def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def _nan_to_none(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, str) and not v.strip():
        return None
    return v


def project_id(raw_id: str) -> str:
    """Prefix an inventory/equipment id with the project namespace."""
    return f"{PROJECT}-{raw_id}"


def _html_table(items, caption: str | None = None) -> str:
    rows_html = []
    for col, val in items:
        v = _nan_to_none(val)
        if v is None:
            continue
        rows_html.append(f'    <tr><th align="left">{col}</th><td>{v}</td></tr>')
    if not rows_html:
        return ""
    parts = ["<table>"]
    if caption:
        parts.append(f"  <caption>{caption}</caption>")
    parts.append("  <tbody>")
    parts.extend(rows_html)
    parts.append("  </tbody>")
    parts.append("</table>")
    return "\n".join(parts)


# ---------- precursor matching ----------

# Material-name → tokens that may appear in ELD.material/product_code.
_MATERIAL_SYNONYMS: dict[str, list[str]] = {
    "nmc811": ["nmc811", "ncm811"],
    "nmc622": ["nmc622", "ncm622"],
    "lfp": ["lfp", "lifepo4"],
    "lco": ["lco", "licoo2"],
    "lmfp": ["lmfp", "limn0.6fe0.4po4"],
    "graphite": ["graphite"],
    "hard carbon": ["hard carbon", "hardcarbon"],
}


def _eld_haystack(eld: ElectrodeRow) -> str:
    return " ".join(filter(None, [eld.material, eld.product_code])).lower()


def find_eld(
    name: str,
    supplier: str | None,
    electrodes: list[ElectrodeRow],
) -> str | None:
    """Return the ELD raw id matching (name, supplier), or None."""
    name_lc = name.lower().strip()
    needles = _MATERIAL_SYNONYMS.get(name_lc, [name_lc])
    candidates: list[ElectrodeRow] = []
    for e in electrodes:
        hay = _eld_haystack(e)
        if any(n in hay for n in needles):
            candidates.append(e)
    if not candidates:
        return None
    if supplier:
        sup_lc = supplier.lower()
        narrowed = [e for e in candidates if (e.supplier or "").lower() == sup_lc]
        if narrowed:
            candidates = narrowed
    return candidates[0].id


def find_electrolyte(
    name: str,
    supplier: str | None,
    electrolytes: list[ElectrolyteRow],
) -> str | None:
    """Loose token-match against ELY description."""
    name_lc = name.lower()
    candidates: list[ElectrolyteRow] = []
    # exact-ish description match first
    for e in electrolytes:
        if e.description and name_lc.replace(" ", "") in e.description.lower().replace(" ", ""):
            candidates.append(e)
    if not candidates:
        # fall back to token overlap
        name_tokens = {t for t in re.findall(r"[a-z0-9]+", name_lc) if len(t) > 1}
        best = None
        best_score = 0
        for e in electrolytes:
            desc_lc = (e.description or "").lower()
            score = sum(1 for t in name_tokens if t in desc_lc)
            if score > best_score:
                best, best_score = e, score
        if best is not None:
            candidates = [best]
    if not candidates:
        return None
    if supplier:
        sup_lc = supplier.lower()
        narrowed = [e for e in candidates if (e.supplier or "").lower() == sup_lc]
        if narrowed:
            candidates = narrowed
    return candidates[0].id


_CONSUMABLE_NAME_TO_ID: dict[str, str] = {
    # Hand-curated for separator/spacer lookups by spreadsheet value.
    "celgard": "INV-CLG",
    "glassfiber": "INV-GFA",
    "glassfibre": "INV-GFA",
    "glass fiber": "INV-GFA",
    "glass fibre": "INV-GFA",
}


def find_consumable(name: str, consumables: list[ConsumableRow]) -> str | None:
    name_lc = name.lower().strip()
    if name_lc in _CONSUMABLE_NAME_TO_ID:
        return _CONSUMABLE_NAME_TO_ID[name_lc]
    for c in consumables:
        if (c.name or "").lower().startswith(name_lc):
            return c.id
    return None


# ---------- precursor / equipment upsert ----------

def _electrode_payload(eld: ElectrodeRow) -> dict:
    rows: list[tuple[str, object]] = [
        ("Product code", eld.product_code),
        ("Material", eld.material),
        ("Coating", eld.coating),
        ("Current collector", eld.current_collector),
        ("Dimensions", eld.dimensions),
        ("Supplier", eld.supplier),
        ("Active material loading (mg/cm²)", eld.active_loading_mg_cm2),
        ("Coating area density (mg/cm²)", eld.coating_area_density_mg_cm2),
        ("Current collector area density (mg/cm²)", eld.cc_area_density_mg_cm2),
        ("Compaction density (g/cm³)", eld.compaction_density_g_cm3),
        ("Specific capacity (mAh/g)", eld.specific_capacity_mAh_g),
        ("Coated area", eld.coated_area),
        ("Coating thickness (μm)", eld.coating_thickness_um),
        ("Total thickness (μm)", eld.total_thickness_um),
        ("Active material weight (g)", eld.active_material_weight_g),
        ("Active material proportion", eld.active_material_proportion),
        ("Area capacity (mAh/cm²)", eld.area_capacity_mAh_cm2),
        ("Note", eld.note),
        ("Link", f'<a href="{eld.link}">{eld.link}</a>' if eld.link else None),
    ]
    name = eld.material or eld.id
    desc = (
        f"<h2>{name}</h2>\n"
        f"<p>Electrode laminate <code>{eld.id}</code>"
        + (f" from {eld.supplier}" if eld.supplier else "")
        + ".</p>\n"
        + _html_table(rows, caption="Inventory entry")
    )
    short_name = eld.material or eld.id
    if eld.supplier:
        short_name = f"{short_name} ({eld.supplier})"
    return {
        "item_id": project_id(eld.id),
        "name": short_name,
        "chemform": eld.material,
        "supplier": eld.supplier,
        "description": desc,
    }


def _electrolyte_payload(ely: ElectrolyteRow) -> dict:
    rows: list[tuple[str, object]] = [
        ("Product code", ely.product_code),
        ("Amount", ely.amount),
        ("Description", ely.description),
        ("Supplier", ely.supplier),
        ("Link", f'<a href="{ely.link}">{ely.link}</a>' if ely.link else None),
    ]
    name = ely.description or ely.id
    desc = (
        f"<h2>Electrolyte {ely.id}</h2>\n"
        f"<p><code>{ely.product_code or ely.id}</code></p>\n"
        + _html_table(rows, caption="Inventory entry")
    )
    return {
        "item_id": project_id(ely.id),
        "name": name,
        "supplier": ely.supplier,
        "description": desc,
    }


def _consumable_payload(c: ConsumableRow) -> dict:
    return {
        "item_id": project_id(c.id),
        "name": c.name or c.id,
        "description": f"<h2>{c.name or c.id}</h2>\n<p>Consumable <code>{c.id}</code>.</p>",
    }


def upsert_precursors(client: DatalabClient, plan: PlanData) -> None:
    for eld in plan.inventory.electrodes:
        payload = _electrode_payload(eld)
        token = set_current_cell(payload["item_id"])
        try:
            upsert_item(
                client,
                item_id=payload["item_id"],
                item_type="starting_materials",
                item_data=payload,
                collection_ids=[ELECTRODES_COLLECTION, CELL_COLLECTION],
            )
        except Exception as e:
            logger.error("upsert electrode %s failed: %s", payload["item_id"], e)
        finally:
            reset_current_cell(token)

    for ely in plan.inventory.electrolytes:
        payload = _electrolyte_payload(ely)
        token = set_current_cell(payload["item_id"])
        try:
            upsert_item(
                client,
                item_id=payload["item_id"],
                item_type="starting_materials",
                item_data=payload,
                collection_ids=[MATERIALS_COLLECTION, CELL_COLLECTION],
            )
        except Exception as e:
            logger.error("upsert electrolyte %s failed: %s", payload["item_id"], e)
        finally:
            reset_current_cell(token)

    for c in plan.inventory.consumables:
        payload = _consumable_payload(c)
        token = set_current_cell(payload["item_id"])
        try:
            upsert_item(
                client,
                item_id=payload["item_id"],
                item_type="starting_materials",
                item_data=payload,
                collection_ids=[MATERIALS_COLLECTION, CELL_COLLECTION],
            )
        except Exception as e:
            logger.error("upsert consumable %s failed: %s", payload["item_id"], e)
        finally:
            reset_current_cell(token)


# ---------- cell description ----------

# Preferred order and display name for description table columns. Keys are
# original (un-aliased) spreadsheet column names.
DESCRIPTION_COLUMN_ORDER: list[tuple[str, str]] = [
    ("ID", "Item ID"),
    ("Cell name", "Cell name"),
    ("Cell no.", "Cell no. (legacy)"),
    ("Batch", "Batch"),
    ("Category", "Category"),
    ("Testing procedure", "Testing procedure"),
    ("Assembler", "Assembler"),
    ("Cathode", "Cathode"),
    ("Cathode supplier", "Cathode supplier"),
    ("Cathode diameter (mm)", "Cathode diameter (mm)"),
    ("Cathode weight (mg)", "Cathode weight, total (mg)"),
    ("cathode weight no foil(mg)", "Cathode weight, no foil (mg)"),
    ("Active material", "Cathode active material (mg)"),
    ("Capacity (mAh)", "Cathode capacity (mAh)"),
    ("Anode", "Anode"),
    ("Anode supplier", "Anode supplier"),
    ("Anode diameter (mm)", "Anode diameter (mm)"),
    ("Anode weight (mg)", "Anode weight, total (mg)"),
    ("No foil", "Anode weight, no foil (mg)"),
    ("Active material.1", "Anode active material (mg)"),
    ("Capacity (mAh).1", "Anode capacity (mAh)"),
    ("N/P Ratio", "N/P ratio"),
    ("Separator_Type", "Separator"),
    ("Separator_Diameter_mm", "Separator diameter (mm)"),
    ("Electrolyte", "Electrolyte"),
    ("Electrolyte supplier", "Electrolyte supplier"),
    ("Electrolyte_Volume_uL", "Electrolyte volume (μL)"),
    ("Spacer_mm", "Spacer (mm)"),
    ("Repeat", "Repeat"),
    ("Cycler position", "Cycler position"),
    ("Notes", "Notes"),
]


def _format_cell_value(col: str, val) -> object | None:
    v = _nan_to_none(val)
    if v is None:
        return None
    if col == "Batch":
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return v
    return v


def _plan_metadata_html(raw_row: pd.Series) -> str:
    seen: set[str] = set()
    ordered: list[tuple[str, object]] = []
    for src, label in DESCRIPTION_COLUMN_ORDER:
        if src not in raw_row.index:
            continue
        seen.add(src)
        v = _format_cell_value(src, raw_row[src])
        if v is None:
            continue
        ordered.append((label, v))
    for col in raw_row.index:
        if col in seen:
            continue
        v = _format_cell_value(col, raw_row[col])
        if v is None:
            continue
        ordered.append((col, v))
    return "<h2>Plan metadata</h2>\n" + _html_table(ordered)


# ---------- cell build ----------

def _constituent(item_id: str, quantity, unit: str) -> dict:
    return {
        "item": {"item_id": item_id, "type": "starting_materials"},
        "quantity": _nan_to_none(quantity),
        "unit": unit,
    }


def row_to_cell(
    row: pd.Series,
    raw_row: pd.Series,
    plan: PlanData,
    cell_dates: dict[int, datetime],
    cellerate_present: set[int],
) -> dict:
    item_id = row["Item_ID"]
    cell_name = _nan_to_none(row.get("Cell_Name")) or item_id
    cell_no = int(row["Cell_No"]) if pd.notna(row.get("Cell_No")) else None

    cell: dict = {
        "item_id": item_id,
        "name": cell_name,
        "description": _plan_metadata_html(raw_row) + "\n\n" + FUNDING_NOTE,
        "cell_format": "coin",
    }
    if cell_no is not None and cell_no in cellerate_present:
        cell["cell_format_description"] = "cellerate"
    date = cell_dates.get(cell_no) if cell_no is not None else None
    cell["date"] = (date or FALLBACK_CELL_DATE).isoformat()

    # Cathode
    cathode = _nan_to_none(row.get("Cathode"))
    cathode_active_mg = _nan_to_none(row.get("Cathode_Active_Mass_mg"))
    if cathode:
        eld = find_eld(
            str(cathode),
            _nan_to_none(row.get("Cathode_Supplier")),
            plan.inventory.electrodes,
        )
        if eld:
            cell["positive_electrode"] = [
                _constituent(project_id(eld), cathode_active_mg, "mg")
            ]
        else:
            logger.warning("no ELD match for cathode %s (supplier=%s)", cathode, row.get("Cathode_Supplier"))

    # Anode
    anode = _nan_to_none(row.get("Anode"))
    anode_active_mg = _nan_to_none(row.get("Anode_Active_Mass_mg"))
    if anode:
        # Li counter-electrode has no ELD entry — skip silently.
        if str(anode).lower() in {"li", "lithium"}:
            pass
        else:
            eld = find_eld(
                str(anode),
                _nan_to_none(row.get("Anode_Supplier")),
                plan.inventory.electrodes,
            )
            if eld:
                cell["negative_electrode"] = [
                    _constituent(project_id(eld), anode_active_mg, "mg")
                ]
            else:
                logger.warning("no ELD match for anode %s (supplier=%s)", anode, row.get("Anode_Supplier"))

    # Electrolyte
    electrolyte = _nan_to_none(row.get("Electrolyte"))
    if electrolyte:
        ely = find_electrolyte(
            str(electrolyte),
            _nan_to_none(row.get("Electrolyte_Supplier")),
            plan.inventory.electrolytes,
        )
        if ely:
            cell["electrolyte"] = [
                _constituent(
                    project_id(ely),
                    _nan_to_none(row.get("Electrolyte_Volume_uL")),
                    "μL",
                )
            ]
        else:
            logger.warning("no ELY match for electrolyte %r", electrolyte)

    # Characteristic mass + nominal capacity
    if cathode_active_mg is not None:
        cell["characteristic_mass"] = float(cathode_active_mg)
    cc = _nan_to_none(row.get("Cathode_Capacity_mAh"))
    ac = _nan_to_none(row.get("Anode_Capacity_mAh"))
    caps = [c for c in (cc, ac) if c is not None]
    if caps:
        cell["nominal_capacity"] = min(float(c) for c in caps)

    return cell


# ---------- per-cell file attachment ----------

def upsert_cells(
    client: DatalabClient,
    plan: PlanData,
    neware_files: dict[int, list[Path]],
    eis_files: dict[int, list[Path]],
    cv_files: dict[int, list[Path]],
    cellerate_files: dict[int, list[Path]],
    cell_dates: dict[int, datetime],
) -> None:
    cellerate_present = set(cellerate_files.keys())
    for i, (_, row) in enumerate(plan.cells.iterrows()):
        raw_row = plan.cells_raw.iloc[i]
        item_id = row.get("Item_ID")
        if not item_id:
            logger.warning("row %d has no Item_ID; skipping", i)
            continue
        try:
            cell = row_to_cell(row, raw_row, plan, cell_dates, cellerate_present)
        except Exception as e:
            logger.exception("skipping bad row %s: %s", item_id, e)
            continue

        token = set_current_cell(item_id)
        try:
            try:
                item = upsert_item(
                    client,
                    item_id=item_id,
                    item_type="cells",
                    item_data=cell,
                    collection_id=CELL_COLLECTION,
                )
            except Exception as e:
                logger.error("upsert cell failed: %s", e)
                continue

            existing_files = existing_file_names(item)

            number = int(row["Project_Number"]) if pd.notna(row.get("Project_Number")) else None
            cell_no = int(row["Cell_No"]) if pd.notna(row.get("Cell_No")) else None

            # Neware (keyed on Project_Number)
            for f in neware_files.get(number, []) if number is not None else []:
                try:
                    reconcile_block_type(client, item_id, item, f.name, ECHEM_BLOCK)
                    upload_if_new(client, item_id, f, existing_files, ECHEM_BLOCK)
                except Exception as e:
                    logger.error("upload neware %s failed: %s", f.name, e)

            # EIS (keyed on Project_Number)
            for f in eis_files.get(number, []) if number is not None else []:
                try:
                    block = EIS_BLOCK if f.suffix.lower() == ".mpr" else None
                    if block:
                        reconcile_block_type(client, item_id, item, f.name, block)
                    upload_if_new(client, item_id, f, existing_files, block)
                except Exception as e:
                    logger.error("upload eis %s failed: %s", f.name, e)

            # CV (keyed on legacy Cell_No)
            for f in cv_files.get(cell_no, []) if cell_no is not None else []:
                try:
                    block = CV_BLOCK if f.suffix.lower() == ".mpr" else None
                    if block:
                        reconcile_block_type(client, item_id, item, f.name, block)
                    upload_if_new(client, item_id, f, existing_files, block)
                except Exception as e:
                    logger.error("upload cv %s failed: %s", f.name, e)

            # Cellerate (keyed on legacy Cell_No) — upload files + a media
            # block titled "Cellerate images" with the first file attached.
            cellerate_paths = cellerate_files.get(cell_no, []) if cell_no is not None else []
            first_cellerate_id: str | None = None
            for f in cellerate_paths:
                try:
                    fid = upload_file_only(client, item_id, f, existing_files)
                    if fid and first_cellerate_id is None:
                        first_cellerate_id = fid
                except Exception as e:
                    logger.error("upload cellerate %s failed: %s", f.name, e)
            if cellerate_paths:
                if first_cellerate_id is None:
                    # All uploads were skipped — re-fetch item to find an
                    # existing cellerate file_id.
                    try:
                        item = client.get_item(item_id=item_id)
                    except Exception as e:
                        logger.warning("re-fetch %s failed: %s", item_id, e)
                    name_to_id = _name_to_file_id(item)
                    for f in cellerate_paths:
                        cand = f.name.replace(" ", "_")
                        if cand in name_to_id:
                            first_cellerate_id = name_to_id[cand]
                            break
                        if f.name in name_to_id:
                            first_cellerate_id = name_to_id[f.name]
                            break
                ensure_block(
                    client, item_id, MEDIA_BLOCK,
                    "Cellerate images", existing_block_titles(item),
                    file_id=first_cellerate_id,
                    item=item,
                )
        finally:
            reset_current_cell(token)


# ---------- electrode characterisation ----------

def attach_electrode_characterisation(
    client: DatalabClient,
    plan: PlanData,
) -> None:
    eld_ids = {e.id.upper() for e in plan.inventory.electrodes}

    sem = map_files_by_eld(SEM_DIR, eld_ids, extensions={".tif", ".dm4", ".jpg", ".png", ".docx"})
    tem = map_files_by_eld(TEM_DIR, eld_ids, extensions={".tif", ".dm4", ".jpg", ".png"})
    xrd = map_files_by_eld(XRD_DIR, eld_ids, extensions={".xrdml", ".xy", ".csv"})
    xps_top = map_files_by_eld(XPS_DIR, eld_ids, extensions={".xlsx"})
    xps_dirs = map_subdirs_by_eld(XPS_SUBDIR, eld_ids)

    all_eld_keys = set(sem) | set(tem) | set(xrd) | set(xps_top) | set(xps_dirs)
    if not all_eld_keys:
        logger.info("no electrode characterisation files found")
        return

    for eld_id in sorted(all_eld_keys):
        item_id = project_id(eld_id)
        token = set_current_cell(item_id)
        try:
            try:
                item = client.get_item(item_id=item_id)
            except Exception as e:
                logger.error("get_item %s failed; skipping characterisation: %s", item_id, e)
                continue
            existing = existing_file_names(item)
            existing_titles = existing_block_titles(item)

            # SEM / TEM: upload files + one combined SEM/TEM media block per
            # electrode with the first uploaded file attached.
            sem_tem_files = sem.get(eld_id, []) + tem.get(eld_id, [])
            if sem_tem_files:
                first_id: str | None = None
                for f in sem_tem_files:
                    try:
                        fid = upload_file_only(client, item_id, f, existing)
                        if fid and first_id is None:
                            first_id = fid
                    except Exception as e:
                        logger.error("upload %s failed: %s", f.name, e)
                if first_id is None:
                    name_to_id = _name_to_file_id(item)
                    for f in sem_tem_files:
                        cand = f.name.replace(" ", "_")
                        if cand in name_to_id:
                            first_id = name_to_id[cand]
                            break
                        if f.name in name_to_id:
                            first_id = name_to_id[f.name]
                            break
                ensure_block(
                    client, item_id, MEDIA_BLOCK,
                    f"SEM/TEM {eld_id}", existing_titles,
                    file_id=first_id, item=item,
                )

            # XRD: upload all patterns + one xrd block per electrode with
            # file_ids = [every .xrdml/.xy file]. .csv files still upload but
            # aren't included in the block.
            xrd_all = xrd.get(eld_id, [])
            xrd_pattern_files = [f for f in xrd_all if f.suffix.lower() in {".xrdml", ".xy"}]
            xrd_other = [f for f in xrd_all if f not in xrd_pattern_files]
            xrd_file_ids = _upload_scans(client, item_id, xrd_pattern_files, existing)
            for f in xrd_other:
                try:
                    upload_file_only(client, item_id, f, existing)
                except Exception as e:
                    logger.error("upload xrd %s failed: %s", f.name, e)
            if xrd_file_ids:
                _ensure_multi_file_block(
                    client, item_id, item, XRD_BLOCK,
                    f"XRD {eld_id}", xrd_file_ids, existing_titles,
                )

            # XPS: upload all scans + one xps block per electrode with
            # file_ids = [every uploaded XPS file].
            xps_files = xps_top.get(eld_id, []) + xps_dirs.get(eld_id, [])
            xps_file_ids = _upload_scans(client, item_id, xps_files, existing, name_with_parent=True)
            if xps_file_ids:
                _ensure_multi_file_block(
                    client, item_id, item, XPS_BLOCK,
                    f"XPS {eld_id}", xps_file_ids, existing_titles,
                )
        finally:
            reset_current_cell(token)


def _upload_scans(
    client: DatalabClient,
    item_id: str,
    files: list[Path],
    existing: set[str],
    name_with_parent: bool = False,
) -> list[str]:
    """Upload each file with a deduplicated filename and return *all* file_ids
    (newly-uploaded + previously-uploaded by name), so a multi-file block can
    list every scan.

    *name_with_parent* prefixes the parent directory to the stored filename —
    needed for XPS where subdir contents have generic names like
    ``C1s Scan.VGD``.
    """
    file_ids: list[str] = []
    name_to_existing_id = _name_to_file_id_from_item(item_id, client)

    for f in files:
        if name_with_parent and f.parent.name and f.parent.name.upper().startswith("ELD-"):
            uniq_name = f"{f.parent.name}__{f.name}".replace(" ", "_")
        else:
            uniq_name = f.name.replace(" ", "_")

        if uniq_name in existing:
            fid = name_to_existing_id.get(uniq_name)
            if fid:
                file_ids.append(fid)
            continue

        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / uniq_name
            shutil.copy2(f, staged)
            with TimedOp(f"upload {uniq_name} -> {item_id}"):
                uploaded = client.upload_file(item_id, str(staged))
        existing.add(uniq_name)
        fid = uploaded.get("file_id")
        if fid:
            file_ids.append(str(fid))
    return file_ids


def _name_to_file_id(item: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in item.get("files") or []:
        name = f.get("name")
        fid = f.get("immutable_id") or f.get("_id") or f.get("file_id")
        if name and fid:
            out[str(name)] = str(fid)
    return out


def _name_to_file_id_from_item(item_id: str, client: DatalabClient) -> dict[str, str]:
    try:
        item = client.get_item(item_id=item_id)
    except Exception:
        return {}
    return _name_to_file_id(item)


def _ensure_multi_file_block(
    client: DatalabClient,
    item_id: str,
    item: dict,
    block_type: str,
    title: str,
    file_ids: list[str],
    existing_titles: set[str],
) -> None:
    """Ensure a single multi-file block of *block_type* with *title* exists on
    *item_id* with ``file_ids`` set to the full list."""
    # Find an existing block of this type+title.
    block_id: str | None = None
    blocks_obj = item.get("blocks_obj") or {}
    if isinstance(blocks_obj, dict):
        for bid, b in blocks_obj.items():
            btype = b.get("blocktype") or b.get("block_type")
            if btype == block_type and b.get("title") == title:
                block_id = bid
                break
    if block_id is None:
        try:
            with TimedOp(f"create_data_block {block_type} on {item_id}"):
                block = client.create_data_block(str(item_id), block_type, file_ids=file_ids)
            block_id = block.get("block_id")
            existing_titles.add(title)
            try_rename_block(
                client, item_id, block_type, block_id or "", title,
                existing_block_data=block,
            )
        except Exception as e:
            logger.warning("create %s block for %s failed: %s", block_type, item_id, e)
        return

    try:
        client.update_data_block(
            item_id=str(item_id),
            block_id=block_id,
            block_type=block_type,
            block_data={"title": title, "file_ids": file_ids},
        )
        logger.info("updated %s block %s on %s (%d files)", block_type, block_id, item_id, len(file_ids))
    except Exception as e:
        logger.warning("update %s block %s on %s failed: %s", block_type, block_id, item_id, e)


# ---------- assembly dates ----------

_CELLERATE_TS_RE = re.compile(r"cell_x-(\d{8})-(\d{4})")


def _cellerate_date(paths: list[Path]) -> datetime | None:
    for p in paths:
        for part in p.parts:
            m = _CELLERATE_TS_RE.match(part)
            if m:
                try:
                    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
                except ValueError:
                    continue
    return None


def _earliest_mtime(paths: list[Path]) -> datetime | None:
    mtimes: list[float] = []
    for p in paths:
        try:
            mtimes.append(p.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    return datetime.fromtimestamp(min(mtimes))


def collect_cell_dates(
    cellerate_files: dict[int, list[Path]],
    neware_files: dict[int, list[Path]],
    number_to_cell_no: dict[int, int],
) -> dict[int, datetime]:
    """Returns mapping from Cell_No → datetime."""
    dates: dict[int, datetime] = {}
    for cell_no, paths in cellerate_files.items():
        d = _cellerate_date(paths)
        if d:
            dates[cell_no] = d
    for number, paths in neware_files.items():
        cell_no = number_to_cell_no.get(number)
        if cell_no is None or cell_no in dates:
            continue
        d = _earliest_mtime(paths)
        if d:
            dates[cell_no] = d
    return dates


# ---------- entry ----------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="http://localhost:5001",
        help="datalab base URL (default: localhost dev server)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    configure_logging(verbose=args.verbose)

    logger.info("parsing plan %s", PLAN_PATH)
    plan = parse_plan(PLAN_PATH)
    logger.info(
        "parsed: %d cells, %d electrodes, %d electrolytes, %d consumables, %d equipment",
        len(plan.cells),
        len(plan.inventory.electrodes),
        len(plan.inventory.electrolytes),
        len(plan.inventory.consumables),
        len(plan.equipment),
    )

    # File matching keysets
    numbers = set(
        pd.to_numeric(plan.cells["Project_Number"], errors="coerce").dropna().astype(int)
    )
    cell_nos = set(
        pd.to_numeric(plan.cells["Cell_No"], errors="coerce").dropna().astype(int)
    )
    number_to_cell_no: dict[int, int] = {}
    for _, r in plan.cells.iterrows():
        if pd.notna(r.get("Project_Number")) and pd.notna(r.get("Cell_No")):
            number_to_cell_no[int(r["Project_Number"])] = int(r["Cell_No"])

    neware_files = map_files_by_number(
        NEWARE_DIR, numbers, prefix="CEL", extensions={".xlsx", ".ndax", ".nda"}
    )
    eis_files = map_files_by_number(
        EIS_DIR, numbers, prefix=f"{PROJECT}-CEL", extensions={".mpr", ".mgr"}
    )
    cv_files = map_files_by_legacy_id(CV_DIR, cell_nos, extensions={".mpr", ".mgr"})
    cellerate_files = map_dirs_by_legacy_id(CELLERATE_DIR, cell_nos)
    cell_dates = collect_cell_dates(cellerate_files, neware_files, number_to_cell_no)

    logger.info(
        "files: neware=%d (cells=%d), eis=%d, cv=%d, cellerate=%d, dates resolved=%d",
        sum(len(v) for v in neware_files.values()), len(neware_files),
        sum(len(v) for v in eis_files.values()),
        sum(len(v) for v in cv_files.values()),
        sum(len(v) for v in cellerate_files.values()),
        len(cell_dates),
    )

    logger.info("connecting to %s", args.url)
    client = DatalabClient(args.url)
    client.authenticate()

    for cid, title, desc in [
        (CELL_COLLECTION, "DIGIBAT Discovery Benchmark", CELL_COLLECTION_DESCRIPTION),
        (MATERIALS_COLLECTION, "Discovery Benchmark — Materials", MATERIALS_COLLECTION_DESCRIPTION),
        (ELECTRODES_COLLECTION, "Discovery Benchmark — Electrodes", ELECTRODES_COLLECTION_DESCRIPTION),
        (INKS_COLLECTION, "Discovery Benchmark — Inks", INKS_COLLECTION_DESCRIPTION),
    ]:
        ensure_collection(client, cid, title, desc)

    upsert_precursors(client, plan)
    upsert_cells(client, plan, neware_files, eis_files, cv_files, cellerate_files, cell_dates)
    attach_electrode_characterisation(client, plan)

    logger.info("tasks: %s", client.check_tasks())


if __name__ == "__main__":
    main()
