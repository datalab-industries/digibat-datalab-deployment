# DIGIBAT Discovery Benchmark

This directory contains code for ingesting the DIGIBAT Discovery Benchmark
dataset into *datalab*.

Authenticated users can access the dataset directly on the DIGIBAT *datalab*
at <https://digibat.dept.ic.ac.uk/collections/Discovery-Benchmark>.

## The dataset

The Discovery Benchmark is a reference collection of ~250 lithium-ion coin
cells assembled to systematically span common cathode/anode/electrolyte
chemistries, produced by the DIGIBAT project as a shared benchmark for
battery-informatics tooling. Each cell is characterised both at the
component (precursor) level and at the cell level.

**Cell chemistries covered:**

- Cathodes: NMC811, NMC622, LFP, LCO, LMFP
- Anodes: Graphite, Li (counter electrode)
- Electrolytes: several LiPF6 formulations (EC/DEC, EC/DMC, EC/DMC/DEC, EC/EMC)
- Separators: Celgard, glass fibre

**Layout of `./data`:**

| Path | Contents |
|---|---|
| `CoinCellAssemble_250Plan_*.xlsx` | Master plan spreadsheet: per-cell build metadata, chemical inventory, N/P ratios |
| `Neware/` | Cycling data (Neware `.ndax` / `.nda` / `.xlsx`) |
| `CV/`, `EIS/` | BioLogic echem (`.mpr`) |
| `SWingXL/` | SWingXL cycler reports (`.xlsx`) |
| `Cellerate/Labelled/<id>/` | Cellerate assembly photos, per cell |
| `SEM/`, `TEM/` | Electron micrographs (`.tif`, `.dm4`) per precursor |
| `XRD/` | Diffraction patterns (`.xrdml`, `.xy`) per precursor |
| `XPS/` | XPS scans (`.VGD`) per precursor |
| `BET/` | BET surface-area data per precursor |

Per-cell files are matched to cells by filename prefix (`<cell_id>_...`).
Per-precursor files are matched by case-insensitive substring of the
material name in the filename/path.

## Usage

The ingestion script is packaged; install and run via `uv`:

```bash
uv run discovery-benchmark --url <DATALAB_URL>
```

The script expects the dataset at `./data` (see layout above) and runs
from the repository root. It is **idempotent**: rerunning picks up new
files/cells without duplicating items or re-uploading existing files,
so it is safe to re-execute after the dataset is updated.

## What the script does

1. Parses the master spreadsheet (`Automated cells` + `Chemical
   information` sheets).
2. Creates/updates precursor inventory items (`starting_materials`) for
   each distinct cathode, anode, separator and electrolyte.
3. Creates/updates a `cells` item per row, with constituents linked to
   the precursor items, `cell_format="coin"`, `characteristic_mass`
   derived from the active cathode mass, and an HTML metadata table in
   `description`.
4. Uploads characterisation files and creates the appropriate data
   blocks (`cycle` for echem, `media` for SEM/TEM/Cellerate, `xrd`,
   `xps`).
