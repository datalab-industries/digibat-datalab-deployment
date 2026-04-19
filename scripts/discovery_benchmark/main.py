"""Idempotent ingestion of the DIGIBAT Discovery Benchmark into datalab."""

from __future__ import annotations

import argparse
import contextvars
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from datalab_api import DatalabClient

from parse_plan import (
    PlanData,
    map_dirs_by_id,
    map_files_by_id,
    parse_plan,
)

logger = logging.getLogger("discovery_benchmark")

_current_cell: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_cell", default=None
)


class _CellIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        cid = _current_cell.get()
        record.cell_id = cid or "-"
        return True


class _ColorFormatter(logging.Formatter):
    RESET = "\033[0m"
    LEVEL_COLORS = {
        logging.DEBUG: "\033[2;37m",
        logging.INFO: "\033[36m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    NAME_COLOR = "\033[1;35m"
    CELL_COLOR = "\033[1;32m"

    def __init__(self, use_color: bool):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        level = record.levelname
        name = record.name
        cell = getattr(record, "cell_id", "-")
        msg = record.getMessage()
        if self.use_color:
            level_c = self.LEVEL_COLORS.get(record.levelno, "")
            r = self.RESET
            head = f"{ts} {level_c}{level:<8}{r} {self.NAME_COLOR}{name}{r} [{self.CELL_COLOR}{cell}{r}]"
        else:
            head = f"{ts} {level:<8} {name} [{cell}]"
        if record.exc_info:
            msg = msg + "\n" + self.formatException(record.exc_info)
        return f"{head} {msg}"


def _configure_logging(verbose: bool) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ColorFormatter(use_color=sys.stderr.isatty()))
    handler.addFilter(_CellIdFilter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


DATA_DIR = Path("data")
PLAN_PATH = DATA_DIR / "CoinCellAssemble_250Plan_20260410.xlsx"
NEWARE_DIR = DATA_DIR / "Neware"
CELLERATE_DIR = DATA_DIR / "Cellerate" / "Labelled"
COLLECTION_ID = "Discovery-Benchmark"

ECHEM_BLOCK = "cycle"
MEDIA_BLOCK = "media"
XRD_BLOCK = "xrd"
XPS_BLOCK = "xps"

# Per-cell data: filename prefix matches cell ID.
CELL_FILE_SOURCES: list[tuple[Path, set[str] | None, str | None]] = [
    (DATA_DIR / "CV", {".mpr"}, ECHEM_BLOCK),
    (DATA_DIR / "EIS", {".mpr"}, ECHEM_BLOCK),
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


@dataclass
class TimedOp:
    label: str
    start_ns: int = 0

    def __enter__(self):
        self.start_ns = time.monotonic_ns()
        return self

    def __exit__(self, *exc):
        elapsed = (time.monotonic_ns() - self.start_ns) / 1e9
        logger.info("%s done", self.label, extra={"elapsed_s": round(elapsed, 3)})


# ---------- idempotent primitives ----------

def upsert_item(
    client: DatalabClient,
    item_id: str | int,
    item_type: str,
    item_data: dict,
    collection_id: str | None = None,
) -> dict:
    try:
        with TimedOp(f"create_item {item_id}"):
            return client.create_item(
                item_id=item_id,
                item_type=item_type,
                item_data=item_data,
                collection_id=collection_id,
            )
    except Exception as e:
        logger.info("create_item %s failed (%s); falling back to update", item_id, e)
    try:
        existing = client.get_item(item_id=item_id)
    except Exception as e:
        logger.error("get_item %s failed: %s", item_id, e)
        raise
    with TimedOp(f"update_item {item_id}"):
        client.update_item(item_id=item_id, item_data=item_data)
    return existing


def _existing_file_names(item: dict) -> set[str]:
    return {str(f["name"]) for f in (item.get("files") or [])}


def _existing_block_titles(item: dict) -> set[str]:
    """Return the set of titles already present on the item's blocks, for idempotency."""
    titles: set[str] = set()
    for block in (item.get("blocks_obj") or {}).values() if isinstance(item.get("blocks_obj"), dict) else []:
        t = block.get("title")
        if t:
            titles.add(str(t))
    # Some API variants return blocks as a list.
    if isinstance(item.get("blocks"), list):
        for b in item["blocks"]:
            if isinstance(b, dict) and b.get("title"):
                titles.add(str(b["title"]))
    return titles


def _try_rename_block(
    client: DatalabClient,
    item_id: str,
    block_type: str,
    block_id: str,
    title: str,
) -> None:
    try:
        client.update_data_block(
            item_id=str(item_id),
            block_id=block_id,
            block_type=block_type,
            block_data={"title": title},
        )
    except Exception as e:
        logger.debug("rename block %s failed: %s", block_id, e)


def upload_if_new(
    client: DatalabClient,
    item_id: str | int,
    path: Path,
    existing_names: set[str],
    block_type: str | None,
    block_title: str | None = None,
) -> bool:
    canonical = path.name.replace(" ", "_")
    if canonical in existing_names or path.name in existing_names:
        logger.debug("skip existing file %s on %s", path.name, item_id)
        return False
    with TimedOp(f"upload {path.name} -> {item_id}"):
        uploaded = client.upload_file(item_id, str(path))
    existing_names.add(canonical)
    if block_type:
        with TimedOp(f"create_data_block {block_type} on {item_id}"):
            block = client.create_data_block(
                str(item_id), block_type, file_ids=uploaded["file_id"]
            )
        if block_title:
            _try_rename_block(
                client, str(item_id), block_type, block.get("block_id", ""), block_title
            )
    return True


def ensure_block(
    client: DatalabClient,
    item_id: str,
    block_type: str,
    title: str,
    existing_titles: set[str],
) -> None:
    """Create a data block (no attached file) with a specific title if none exists."""
    if title in existing_titles:
        logger.debug("block %r already present on %s", title, item_id)
        return
    try:
        with TimedOp(f"create_data_block {block_type} on {item_id}"):
            block = client.create_data_block(str(item_id), block_type)
        _try_rename_block(client, item_id, block_type, block.get("block_id", ""), title)
        existing_titles.add(title)
    except Exception as e:
        logger.error("create %s block on %s failed: %s", block_type, item_id, e)


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
) -> dict:
    cell_id = int(row["ID_No"])
    name = _nan_to_none(row["Cell_ID"])

    cell: dict = {
        "item_id": str(cell_id),
        "name": name if isinstance(name, str) else str(cell_id),
        "description": _plan_metadata_html(raw_row),
        "cell_format": "coin",
    }
    if cell_id in cellerate_cells:
        cell["cell_format_description"] = "cellerate"

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
        token = _current_cell.set(item_id)
        try:
            logger.info("upsert precursor (%s)", key[0])
            upsert_item(
                client,
                item_id=item_id,
                item_type="starting_materials",
                item_data=payload,
                collection_id=COLLECTION_ID,
            )
            index[key] = item_id
        finally:
            _current_cell.reset(token)
    return index


def upsert_cells(
    client: DatalabClient,
    plan: PlanData,
    precursor_index: dict[tuple[str, str], str],
    echem_files: dict[int, list[Path]],
    cell_characterisation: dict[int, list[tuple[Path, str | None]]],
    cellerate_files: dict[int, list[Path]],
    active_proportions: dict[str, float],
) -> None:
    cellerate_cells = set(cellerate_files.keys())
    for i, (_, row) in enumerate(plan.cells.iterrows()):
        raw_row = plan.cells_raw.iloc[i]
        try:
            cell = row_to_cell(row, raw_row, precursor_index, active_proportions, cellerate_cells)
        except Exception as e:
            logger.exception("skipping bad row %s: %s", row.get("ID_No"), e)
            continue

        cell_id = cell["item_id"]
        token = _current_cell.set(cell_id)
        try:
            logger.info("ingesting cell")
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

            existing_files = _existing_file_names(item)
            existing_titles = _existing_block_titles(item)

            numeric_id = int(row["ID_No"])

            # Cycling — each file gets its own cycle block (historic behaviour).
            for f in echem_files.get(numeric_id, []):
                try:
                    upload_if_new(client, cell_id, f, existing_files, ECHEM_BLOCK)
                except Exception as e:
                    logger.error("upload %s failed: %s", f.name, e)

            # CV/EIS/SWingXL etc.
            for f, block in cell_characterisation.get(numeric_id, []):
                try:
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
            _current_cell.reset(token)


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
            token = _current_cell.set(item_id)
            try:
                item = client.get_item(item_id=item_id)
                existing = _existing_file_names(item)
                # Only make a media block for TIFs — .dm4 doesn't render.
                block = MEDIA_BLOCK if f.suffix.lower() == ".tif" else None
                title = _media_title(technique, name, f) if block else None
                upload_if_new(client, item_id, f, existing, block, block_title=title)
            except Exception as e:
                logger.error("attach %s failed: %s", f.name, e)
            finally:
                _current_cell.reset(token)


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
        token = _current_cell.set(item_id)
        try:
            item = client.get_item(item_id=item_id)
            existing = _existing_file_names(item)
            upload_if_new(client, item_id, f, existing, block_type=None)
            touched_precursors[item_id] = name
        except Exception as e:
            logger.error("attach %s failed: %s", f.name, e)
        finally:
            _current_cell.reset(token)
    # One XRD block per precursor with any XRD data — no file attached so it renders all.
    for item_id, name in touched_precursors.items():
        token = _current_cell.set(item_id)
        try:
            item = client.get_item(item_id=item_id)
            titles = _existing_block_titles(item)
            ensure_block(client, item_id, XRD_BLOCK, f"XRD {name}", titles)
        finally:
            _current_cell.reset(token)


def attach_xps(
    client: DatalabClient,
    precursors: list[tuple[str, str]],
) -> None:
    root = DATA_DIR / "XPS"
    if not root.exists():
        return
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
        token = _current_cell.set(item_id)
        try:
            item = client.get_item(item_id=item_id)
            existing = _existing_file_names(item)
            # Build a descriptive filename to avoid collisions across directories.
            scan = f.stem
            uniq_name = f"{name}_{f.parent.name}_{scan}{f.suffix}".replace(" ", "_")
            target = f
            if uniq_name not in existing and f.name not in existing:
                with TimedOp(f"upload {uniq_name} -> {item_id}"):
                    uploaded = client.upload_file(item_id, str(target))
                existing.add(uniq_name)
                existing.add(f.name)
                try:
                    with TimedOp(f"create_data_block xps on {item_id}"):
                        block = client.create_data_block(
                            str(item_id), XPS_BLOCK, file_ids=uploaded["file_id"]
                        )
                    _try_rename_block(
                        client, item_id, XPS_BLOCK, block.get("block_id", ""),
                        f"XPS {name} {scan}",
                    )
                except Exception as e:
                    # xps block may not exist on this server — uploading is still useful.
                    logger.warning("xps block for %s failed: %s", f.name, e)
        except Exception as e:
            logger.error("attach %s failed: %s", f.name, e)
        finally:
            _current_cell.reset(token)


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
    for root, exts, block in CELL_FILE_SOURCES:
        files = map_files_by_id(root, known_ids, extensions=exts)
        for cid, paths in files.items():
            result.setdefault(cid, []).extend((p, block) for p in paths)
    return result


def collect_cellerate(known_ids: set[int]) -> dict[int, list[Path]]:
    return map_dirs_by_id(CELLERATE_DIR, known_ids)


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
    _configure_logging(verbose=args.verbose)

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

    precursor_index = upsert_precursors(client, plan)
    upsert_cells(
        client,
        plan,
        precursor_index,
        plan.echem_file_map,
        cell_char,
        cellerate_files,
        active_props,
    )
    attach_precursor_characterisation(client, precursor_index)

    logger.info("tasks: %s", client.check_tasks())


if __name__ == "__main__":
    main()
