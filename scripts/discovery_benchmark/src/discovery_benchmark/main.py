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
    PlanData,
    map_dirs_by_id,
    map_files_by_id,
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
    upload_if_new,
    upsert_item,
)

logger = logging.getLogger("discovery_benchmark")


DATA_DIR = Path("data")
PLAN_PATH = DATA_DIR / "CoinCellAssemble_250Plan_20260410.xlsx"
NEWARE_DIR = DATA_DIR / "Neware"
CELLERATE_DIR = DATA_DIR / "Cellerate" / "Labelled"
COLLECTION_ID = "Discovery-Benchmark"
COLLECTION_TITLE = "DIGIBAT Discovery Benchmark"
REPO_URL = "https://github.com/datalab-industries/digibat-datalab-deployment"
INGESTION_SUBPATH = "tree/main/scripts/discovery_benchmark"

# Funding acknowledgement appended to the collection description and every cell.
# Update with a funding/grant code when available.
FUNDING_NOTE = (
    "<p><i>This work was supported by the "
    '<a href="https://www.royce.ac.uk/">Henry Royce Institute</a>.</i></p>'
)

COLLECTION_DESCRIPTION = f"""\
<h2>DIGIBAT Discovery Benchmark</h2>
<p>
  A reference collection of ~250 lithium-ion coin cells assembled to
  systematically span common cathode/anode/electrolyte chemistries,
  produced by the DIGIBAT project as a shared benchmark for
  battery-informatics tooling. Each cell is characterised both at the
  component (precursor) level and at the cell level.
</p>

<h3>Cell chemistries covered</h3>
<ul>
  <li><b>Cathodes:</b> NMC811, NMC622, LFP, LCO, LMFP</li>
  <li><b>Anodes:</b> Graphite, Li (counter electrode)</li>
  <li><b>Electrolytes:</b> several LiPF<sub>6</sub> formulations
      (EC/DEC, EC/DMC, EC/DMC/DEC, EC/EMC)</li>
  <li><b>Separators:</b> Celgard, glass fibre</li>
</ul>

<h3>Characterisation</h3>
<ul>
  <li>Cycling — Neware (<code>.ndax</code>/<code>.nda</code>/<code>.xlsx</code>)
      and BioLogic CV/EIS (<code>.mpr</code>)</li>
  <li>SWingXL cycler reports (<code>.xlsx</code>)</li>
  <li>Cellerate assembly photos per cell</li>
  <li>SEM/TEM micrographs, XRD patterns, XPS scans, BET surface area —
      per precursor material</li>
</ul>

<p>
  The ingestion code that produced this collection lives at
  <a href="{REPO_URL}/{INGESTION_SUBPATH}">{REPO_URL}/{INGESTION_SUBPATH}</a>.
</p>

{FUNDING_NOTE}
"""

ECHEM_BLOCK = "cycle"
EIS_BLOCK = "eis"
CV_BLOCK = "cv"
MEDIA_BLOCK = "media"
XRD_BLOCK = "xrd"
XPS_BLOCK = "xps"

# Per-cell data: filename prefix matches cell ID. The optional name-substring
# filter (case-insensitive) restricts which files in the dir get picked up.
CELL_FILE_SOURCES: list[tuple[Path, set[str] | None, str | None, str | None]] = [
    (DATA_DIR / "CV", {".mpr"}, CV_BLOCK, "CV"),
    (DATA_DIR / "EIS", {".mpr"}, EIS_BLOCK, None),
]


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


def _parse_active_proportion(val) -> float | None:
    """Parse the Active_Material_Proportion cell, either ``"95% (...)"`` or ``"0.96"``."""
    s = str(_nan_to_none(val) or "").strip()
    if not s:
        return None
    m = re.match(r"\s*([\d.]+)\s*%", s)
    if m:
        return float(m.group(1)) / 100
    try:
        v = float(s)
    except ValueError:
        return None
    if 0 < v <= 1:
        return v
    if 1 < v <= 100:
        return v / 100
    return None


ACTIVE_PROPORTION_FALLBACK: dict[str, float] = {"NMC622": 0.95}


def _active_proportions(plan: PlanData) -> dict[str, float]:
    props: dict[str, float] = {}
    for name in list(plan.cells["Cathode"].dropna().unique()) + list(
        plan.cells["Anode"].dropna().unique()
    ):
        row = _match_electrode_row(name, plan.electrodes)
        prop = _parse_active_proportion(row["Active_Material_Proportion"]) if row is not None else None
        props[name] = prop or ACTIVE_PROPORTION_FALLBACK.get(name, 1.0)
    return props


# Preferred order and display name for description table columns.
DESCRIPTION_COLUMN_ORDER: list[tuple[str, str]] = [
    ("Cell_ID", "Cell ID"),
    ("ID no.", "ID no."),
    ("Batch", "Batch"),
    ("Category", "Category"),
    ("Cathode", "Cathode"),
    ("Cathode diameter (mm)", "Cathode diameter (mm)"),
    ("cathode mass", "Cathode mass (mg)"),
    ("Anode", "Anode"),
    ("Anode diameter (mm)", "Anode diameter (mm)"),
    ("anode mass", "Anode mass (mg)"),
    ("N/P ratio", "N/P ratio"),
    ("Separator_Type", "Separator"),
    ("Separator_Diameter_mm", "Separator diameter (mm)"),
    ("Electrolyte", "Electrolyte"),
    ("Electrolyte_Volume_uL", "Electrolyte volume (μL)"),
    ("Spacer_mm", "Spacer (mm)"),
    ("Repeat", "Repeat"),
    ("Cycler position", "Cycler position"),
    ("capacity", "Nominal capacity"),
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


def _plan_metadata_html(raw_row: pd.Series) -> str:
    """Reorder and relabel the spreadsheet row, then render as an HTML table."""
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
    # Append anything else we didn't account for.
    for col in raw_row.index:
        if col in seen:
            continue
        v = _format_cell_value(col, raw_row[col])
        if v is None:
            continue
        ordered.append((col, v))
    return "<h2>Plan metadata</h2>\n" + _html_table(ordered)


# ---------- precursor matching ----------

def _match_electrode_row(cathode_name: str, electrodes: pd.DataFrame) -> pd.Series | None:
    base = cathode_name.lower().replace(" ", "")
    needles = {base}
    if "nmc" in base:
        needles.add(base.replace("nmc", "ncm"))
    if base == "lfp":
        needles.update({"lpf", "lifepo4"})
    for _, row in electrodes.iterrows():
        hay = " ".join(
            str(row.get(c) or "") for c in ("Product_Code", "Material")
        ).lower().replace(" ", "")
        if any(n and n in hay for n in needles):
            return row
    return None


def _match_electrolyte_row(name: str, electrolytes: pd.DataFrame) -> pd.Series | None:
    key_tokens = {t for t in re.findall(r"[a-zA-Z]+", name.lower()) if len(t) > 2}
    best, best_score = None, 0
    for _, row in electrolytes.iterrows():
        desc = str(row.get("Description") or "").lower()
        score = sum(1 for t in key_tokens if t in desc)
        if score > best_score:
            best, best_score = row, score
    return best if best_score else None


# Supplier product codes are reused as datalab item_ids where possible.
PRECURSOR_ITEM_IDS: dict[tuple[str, str], str] = {
    ("cathode", "LCO"): "bcaf-241co-ss",
    ("cathode", "LFP"): "bcaf-lpfss",
    ("cathode", "LMFP"): "bcaf-lmfpss",
    ("cathode", "NMC811"): "bcaf-ncm811ss",
    ("cathode", "NMC622"): "nmc622",
    ("anode", "Graphite"): "bccf-ss",
    ("separator", "Celgard"): "celgard",
    ("separator", "GlassFiber"): "glassfiber",
    ("electrolyte", "1.0 M LiPF6\xa0in EC/DEC=50/50 (v/v)"): "746746",
    ("electrolyte", "1.0 M LiPF6 in EC/DMC/DEC=1:1:1 (v/v/v)"): "901685",
    ("electrolyte", "1.0 M LiPF6 in EC/DMC=50/50 (v/v)"): "746711",
    ("electrolyte", "1M LiPF6 EC:EMC=3:7wt%"): "lp57",
}

CHEMFORM_FALLBACK = {
    "NMC622": "LiNi0.6Mn0.2Co0.2O2",
}


def _precursor_id(kind: str, name: str) -> str:
    try:
        return PRECURSOR_ITEM_IDS[(kind, name)]
    except KeyError:
        raise KeyError(
            f"no hardcoded precursor item_id for ({kind!r}, {name!r}); "
            f"add it to PRECURSOR_ITEM_IDS"
        )


def build_precursors(plan: PlanData) -> dict[tuple[str, str], dict]:
    precursors: dict[tuple[str, str], dict] = {}

    def _electrode_precursor(kind: str, name: str, blurb: str) -> dict:
        row = _match_electrode_row(name, plan.electrodes)
        desc = [f"<h2>{name}</h2>", f"<p>{blurb}</p>"]
        if row is not None:
            desc.append(_html_table(row.items(), caption="Supplier specification"))
        data = {
            "item_id": _precursor_id(kind, name),
            "name": name,
            "chemform": (
                str(row["Material"]) if row is not None else CHEMFORM_FALLBACK.get(name)
            ),
            "supplier": _nan_to_none(row["Supplier"]) if row is not None else None,
            "description": "\n".join(p for p in desc if p),
        }
        return {k: v for k, v in data.items() if v is not None}

    for name in sorted(plan.cells["Cathode"].dropna().unique()):
        precursors[("cathode", name)] = _electrode_precursor(
            "cathode", name, "Cathode material used in the Discovery Benchmark dataset."
        )

    for name in sorted(plan.cells["Anode"].dropna().unique()):
        precursors[("anode", name)] = _electrode_precursor(
            "anode", name, "Anode material used in the Discovery Benchmark dataset."
        )

    for name in sorted(plan.cells["Separator_Type"].dropna().unique()):
        precursors[("separator", name)] = {
            "item_id": _precursor_id("separator", name),
            "name": f"{name} separator",
            "description": f"<h2>{name}</h2>\n<p>Separator material.</p>",
        }

    for name in sorted(plan.cells["Electrolyte"].dropna().unique()):
        row = _match_electrolyte_row(name, plan.electrolytes)
        desc = ["<h2>Electrolyte</h2>", f"<p><code>{name}</code></p>"]
        if row is not None:
            desc.append(_html_table(row.items(), caption="Supplier specification"))
        data = {
            "item_id": _precursor_id("electrolyte", name),
            "name": name,
            "supplier": _nan_to_none(row["Supplier"]) if row is not None else None,
            "description": "\n".join(p for p in desc if p),
        }
        precursors[("electrolyte", name)] = {k: v for k, v in data.items() if v is not None}

    return precursors


# ---------- cell build ----------

def _constituent(item_id: str, mass_mg, unit: str, proportion: float | None = None) -> dict:
    """Return a constituent dict; quantity is None when the spreadsheet mass is missing."""
    q = _nan_to_none(mass_mg)
    if q is not None and proportion is not None:
        q = float(q) * proportion
    return {
        "item": {"item_id": item_id, "type": "starting_materials"},
        "quantity": q,
        "unit": unit,
    }


def row_to_cell(
    row: pd.Series,
    raw_row: pd.Series,
    precursor_index: dict[tuple[str, str], str],
    active_proportions: dict[str, float],
    cellerate_cells: set[int],
    cell_dates: dict[int, datetime],
) -> dict:
    cell_id = int(row["ID_No"])
    name = _nan_to_none(row["Cell_ID"])

    cell: dict = {
        "item_id": str(cell_id),
        "name": name if isinstance(name, str) else str(cell_id),
        "description": _plan_metadata_html(raw_row) + "\n\n" + FUNDING_NOTE,
        "cell_format": "coin",
    }
    if cell_id in cellerate_cells:
        cell["cell_format_description"] = "cellerate"
    if cell_id in cell_dates:
        cell["date"] = cell_dates[cell_id].isoformat()

    cathode_active_mg = None
    cathode = _nan_to_none(row["Cathode"])
    if cathode and ("cathode", cathode) in precursor_index:
        prop = active_proportions.get(cathode, 1.0)
        c = _constituent(
            precursor_index[("cathode", cathode)], row["Cathode_Mass_mg"], "mg", proportion=prop
        )
        cell["positive_electrode"] = [c]
        cathode_active_mg = c["quantity"]

    anode = _nan_to_none(row["Anode"])
    if anode and ("anode", anode) in precursor_index:
        prop = active_proportions.get(anode, 1.0)
        cell["negative_electrode"] = [
            _constituent(
                precursor_index[("anode", anode)], row["Anode_Mass_mg"], "mg", proportion=prop
            )
        ]

    electrolyte = _nan_to_none(row["Electrolyte"])
    if electrolyte and ("electrolyte", electrolyte) in precursor_index:
        cell["electrolyte"] = [
            _constituent(
                precursor_index[("electrolyte", electrolyte)],
                row["Electrolyte_Volume_uL"],
                "μL",
            )
        ]

    # N/P > 1 throughout the dataset, so cathode is limiting → characteristic mass in grams.
    if cathode_active_mg is not None:
        cell["characteristic_mass"] = cathode_active_mg

    return cell


# ---------- orchestration ----------

def upsert_precursors(
    client: DatalabClient, plan: PlanData
) -> dict[tuple[str, str], str]:
    payloads = build_precursors(plan)
    index: dict[tuple[str, str], str] = {}
    for key, payload in payloads.items():
        item_id = payload["item_id"]
        token = set_current_cell(item_id)
        try:
            upsert_item(
                client,
                item_id=item_id,
                item_type="starting_materials",
                item_data=payload,
                collection_id=COLLECTION_ID,
            )
            index[key] = item_id
        finally:
            reset_current_cell(token)
    return index


def upsert_cells(
    client: DatalabClient,
    plan: PlanData,
    precursor_index: dict[tuple[str, str], str],
    echem_files: dict[int, list[Path]],
    cell_characterisation: dict[int, list[tuple[Path, str | None]]],
    cellerate_files: dict[int, list[Path]],
    active_proportions: dict[str, float],
    cell_dates: dict[int, datetime],
) -> None:
    cellerate_cells = set(cellerate_files.keys())
    for i, (_, row) in enumerate(plan.cells.iterrows()):
        raw_row = plan.cells_raw.iloc[i]
        try:
            cell = row_to_cell(
                row, raw_row, precursor_index, active_proportions, cellerate_cells, cell_dates
            )
        except Exception as e:
            logger.exception("skipping bad row %s: %s", row.get("ID_No"), e)
            continue

        cell_id = cell["item_id"]
        token = set_current_cell(cell_id)
        try:
            try:
                item = upsert_item(
                    client,
                    item_id=cell_id,
                    item_type="cells",
                    item_data=cell,
                    collection_id=COLLECTION_ID,
                )
            except Exception as e:
                logger.error("upsert cell failed: %s", e)
                continue

            existing_files = existing_file_names(item)
            existing_titles = existing_block_titles(item)

            numeric_id = int(row["ID_No"])

            # Cycling — each file gets its own cycle block (historic behaviour).
            for f in echem_files.get(numeric_id, []):
                try:
                    reconcile_block_type(client, cell_id, item, f.name, ECHEM_BLOCK)
                    upload_if_new(client, cell_id, f, existing_files, ECHEM_BLOCK)
                except Exception as e:
                    logger.error("upload %s failed: %s", f.name, e)

            # CV/EIS/SWingXL etc. Block-type conventions have changed between
            # runs (EIS used to go into cycle blocks) — reconcile first so stale
            # cycle blocks get rebuilt as eis/cv blocks.
            for f, block in cell_characterisation.get(numeric_id, []):
                try:
                    if block:
                        reconcile_block_type(client, cell_id, item, f.name, block)
                    upload_if_new(client, cell_id, f, existing_files, block)
                except Exception as e:
                    logger.error("upload %s failed: %s", f.name, e)

            # Cellerate: upload files but don't make a block per file. Make a single
            # media block for the cell once any are attached.
            cellerate_paths = cellerate_files.get(numeric_id, [])
            for f in cellerate_paths:
                try:
                    upload_if_new(client, cell_id, f, existing_files, block_type=None)
                except Exception as e:
                    logger.error("upload %s failed: %s", f.name, e)
            if cellerate_paths:
                ensure_block(
                    client, cell_id, MEDIA_BLOCK, "Cellerate images", existing_titles
                )
        finally:
            reset_current_cell(token)


# ---------- precursor characterisation ----------

_TEM_MAG_RE = re.compile(r"x(\d+k)", re.IGNORECASE)
_SEM_MAG_RE = re.compile(r"_(\d+k)(?:[._(]|$)", re.IGNORECASE)


def _infer_mag(technique: str, path: Path) -> str | None:
    stem = path.stem
    rx = _TEM_MAG_RE if technique == "TEM" else _SEM_MAG_RE
    m = rx.search(stem)
    if m:
        return f"×{m.group(1).lower()}"
    return None


def _media_title(technique: str, precursor_name: str, path: Path) -> str:
    mag = _infer_mag(technique, path)
    parts = [technique, precursor_name]
    if mag:
        parts.append(mag)
    return " ".join(parts)


def _best_precursor_match(
    filename: str, precursors: list[tuple[str, str]]
) -> tuple[str, str] | None:
    """Longest-material-name-first match against filename (case-insensitive)."""
    lower = filename.lower()
    for name, item_id in precursors:
        if name.lower() in lower:
            return (name, item_id)
    return None


def attach_sem_tem(
    client: DatalabClient,
    precursors: list[tuple[str, str]],
) -> None:
    for technique, root, exts in [
        ("SEM", DATA_DIR / "SEM", {".tif", ".dm4"}),
        ("TEM", DATA_DIR / "TEM", {".tif", ".dm4"}),
    ]:
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in exts:
                continue
            matched = _best_precursor_match(f.name, precursors)
            if matched is None:
                logger.debug("no precursor match for %s", f)
                continue
            name, item_id = matched
            token = set_current_cell(item_id)
            try:
                item = client.get_item(item_id=item_id)
                existing = existing_file_names(item)
                # Only make a media block for TIFs — .dm4 doesn't render.
                block = MEDIA_BLOCK if f.suffix.lower() == ".tif" else None
                title = _media_title(technique, name, f) if block else None
                if block:
                    reconcile_block_type(
                        client, item_id, item, f.name, block, desired_title=title
                    )
                upload_if_new(client, item_id, f, existing, block, block_title=title)
            except Exception as e:
                logger.error("attach %s failed: %s", f.name, e)
            finally:
                reset_current_cell(token)


def attach_xrd(
    client: DatalabClient,
    precursors: list[tuple[str, str]],
) -> None:
    root = DATA_DIR / "XRD"
    if not root.exists():
        return
    exts = {".xrdml", ".xy"}
    touched_precursors: dict[str, str] = {}  # item_id -> precursor name
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in exts:
            continue
        matched = _best_precursor_match(f.name, precursors)
        if matched is None:
            logger.debug("no precursor match for %s", f)
            continue
        name, item_id = matched
        token = set_current_cell(item_id)
        try:
            item = client.get_item(item_id=item_id)
            existing = existing_file_names(item)
            upload_if_new(client, item_id, f, existing, block_type=None)
            touched_precursors[item_id] = name
        except Exception as e:
            logger.error("attach %s failed: %s", f.name, e)
        finally:
            reset_current_cell(token)
    # One XRD block per precursor with any XRD data — no file attached so it renders all.
    for item_id, name in touched_precursors.items():
        token = set_current_cell(item_id)
        try:
            item = client.get_item(item_id=item_id)
            titles = existing_block_titles(item)
            ensure_block(client, item_id, XRD_BLOCK, f"XRD {name}", titles)
        finally:
            reset_current_cell(token)


def attach_xps(
    client: DatalabClient,
    precursors: list[tuple[str, str]],
) -> None:
    root = DATA_DIR / "XPS"
    if not root.exists():
        return
    # Some precursors have multiple copies of the same scan (e.g. LCO appears
    # under LCO/Group and LCO_repeat/LCO/Group with identical C1s/Co2p/... files).
    # Only upload the first occurrence of each (precursor, scan_stem) pair.
    seen_scans: set[tuple[str, str]] = set()
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() != ".vgd":
            continue
        # VGD filenames are often generic ("C1s Scan.VGD") so we also inspect
        # parent directories when searching for a precursor match.
        hay = " / ".join([*(p.name for p in f.parents), f.name])
        matched = _best_precursor_match(hay, precursors)
        if matched is None:
            logger.debug("no precursor match for %s", f)
            continue
        name, item_id = matched
        scan = f.stem
        scan_key = (item_id, scan.lower().replace(" ", ""))
        if scan_key in seen_scans:
            logger.debug("skip duplicate %s scan for %s (%s)", scan, name, f)
            continue
        seen_scans.add(scan_key)
        token = set_current_cell(item_id)
        try:
            item = client.get_item(item_id=item_id)
            existing = existing_file_names(item)
            # Build a descriptive filename. datalab stores files under
            # ``file_path.name``, so to actually persist this name (and dedupe
            # on rerun) we copy the file to a tempdir with the unique name
            # and upload from there.
            uniq_name = f"{name}_{scan}{f.suffix}".replace(" ", "_")
            if uniq_name in existing:
                # File already uploaded on a prior run — heal any damaged XPS
                # block (wrong type or missing file_id) before skipping.
                reconcile_block_type(
                    client, item_id, item, uniq_name, XPS_BLOCK,
                    desired_title=f"XPS {name} {scan}",
                )
                logger.debug("skip existing XPS file %s on %s", uniq_name, item_id)
                continue
            with tempfile.TemporaryDirectory() as tmp:
                staged = Path(tmp) / uniq_name
                shutil.copy2(f, staged)
                with TimedOp(f"upload {uniq_name} -> {item_id}"):
                    uploaded = client.upload_file(item_id, str(staged))
            existing.add(uniq_name)
            try:
                with TimedOp(f"create_data_block xps on {item_id}"):
                    block = client.create_data_block(
                        str(item_id), XPS_BLOCK, file_ids=uploaded["file_id"]
                    )
                try_rename_block(
                    client, item_id, XPS_BLOCK, block.get("block_id", ""),
                    f"XPS {name} {scan}",
                    existing_block_data=block,
                    file_id=uploaded["file_id"],
                )
            except Exception as e:
                # xps block may not exist on this server — the file upload is still useful.
                logger.warning("xps block for %s failed: %s", f.name, e)
        except Exception as e:
            logger.error("attach %s failed: %s", f.name, e)
        finally:
            reset_current_cell(token)


def attach_precursor_characterisation(
    client: DatalabClient,
    precursor_index: dict[tuple[str, str], str],
) -> None:
    precursors: list[tuple[str, str]] = sorted(
        [
            (name, item_id)
            for (kind, name), item_id in precursor_index.items()
            if kind in ("cathode", "anode")
        ],
        key=lambda t: -len(t[0]),
    )
    attach_sem_tem(client, precursors)
    attach_xrd(client, precursors)
    attach_xps(client, precursors)


# ---------- file collection ----------

def collect_cell_characterisation(
    known_ids: set[int],
) -> dict[int, list[tuple[Path, str | None]]]:
    """Per-cell files from CV/EIS/SWingXL (filename prefix = cell ID)."""
    result: dict[int, list[tuple[Path, str | None]]] = {}
    for root, exts, block, name_token in CELL_FILE_SOURCES:
        files = map_files_by_id(root, known_ids, extensions=exts)
        for cid, paths in files.items():
            for p in paths:
                if name_token and name_token.lower() not in p.name.lower():
                    continue
                result.setdefault(cid, []).append((p, block))
    return result


def collect_cellerate(known_ids: set[int]) -> dict[int, list[Path]]:
    return map_dirs_by_id(CELLERATE_DIR, known_ids)


# ---------- assembly dates ----------

# Cellerate saves each cell into a subdir named cell_x-YYYYMMDD-HHMM — that
# timestamp is the assembly run.
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
    """Upper bound on assembly: the earliest file mtime associated with the cell.

    SharePoint often flattens mtimes on sync, but Neware cycling exports tend to
    retain their original mtime. Cycling postdates assembly, so this is a
    conservative upper bound rather than the true assembly date.
    """
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
    known_ids: set[int],
) -> dict[int, datetime]:
    dates: dict[int, datetime] = {}
    for cid in known_ids:
        d = _cellerate_date(cellerate_files.get(cid, []))
        if d is None:
            d = _earliest_mtime(neware_files.get(cid, []))
        if d is not None:
            dates[cid] = d
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
    plan = parse_plan(PLAN_PATH, neware_dir=NEWARE_DIR)
    logger.info(
        "parsed: %d cells, %d cathodes, %d anodes, %d electrolytes, %d separators",
        len(plan.cells),
        plan.cells["Cathode"].nunique(),
        plan.cells["Anode"].nunique(),
        plan.cells["Electrolyte"].nunique(),
        plan.cells["Separator_Type"].nunique(),
    )
    known_ids = set(
        pd.to_numeric(plan.cells["ID_No"], errors="coerce").dropna().astype(int)
    )
    cell_char = collect_cell_characterisation(known_ids)
    cellerate_files = collect_cellerate(known_ids)
    cell_dates = collect_cell_dates(cellerate_files, plan.echem_file_map, known_ids)
    logger.info(
        "assembly dates resolved for %d/%d cells (%d from Cellerate, rest from echem mtime)",
        len(cell_dates),
        len(known_ids),
        sum(1 for cid in cell_dates if _cellerate_date(cellerate_files.get(cid, []))),
    )
    active_props = _active_proportions(plan)
    logger.info(
        "active proportions: %s",
        {k: round(v, 3) for k, v in active_props.items()},
    )
    logger.info(
        "cell-level extra files: %d files across %d cells",
        sum(len(v) for v in cell_char.values()),
        len(cell_char),
    )
    logger.info(
        "cellerate files: %d files across %d cells",
        sum(len(v) for v in cellerate_files.values()),
        len(cellerate_files),
    )
    logger.info(
        "neware files: %d files across %d cells",
        sum(len(v) for v in plan.echem_file_map.values()),
        len(plan.echem_file_map),
    )

    logger.info("connecting to %s", args.url)
    client = DatalabClient(args.url)
    client.authenticate()

    ensure_collection(client, COLLECTION_ID, COLLECTION_TITLE, COLLECTION_DESCRIPTION)
    precursor_index = upsert_precursors(client, plan)
    upsert_cells(
        client,
        plan,
        precursor_index,
        plan.echem_file_map,
        cell_char,
        cellerate_files,
        active_props,
        cell_dates,
    )
    attach_precursor_characterisation(client, precursor_index)

    logger.info("tasks: %s", client.check_tasks())


if __name__ == "__main__":
    main()
