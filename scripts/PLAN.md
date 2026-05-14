# DIGIBAT datalab Plugin — Plan

## Background

The DIGIBAT lab at Imperial College London studies lithium-ion battery materials,
fabricating coin cells with various cathode chemistries (NMC811, NMC622, LFP),
graphite anodes, and different separator/electrolyte combinations. They run a
[datalab](https://github.com/datalab-industries/datalab-server) instance for
research data management and need to ingest an initial dataset of ~250 coin
cells along with their associated characterisation data.

This plugin repository (`datalab-app-plugin-digibat`) will host:

1. **Ingestion scripts** to bulk-load the coin cell dataset into the DIGIBAT
   datalab instance via the
   [datalab Python API](https://github.com/datalab-industries/datalab-api).
   This work may also drive upstream improvements to `datalab-api` itself to
   support more expressive item creation and relationship management.
2. **DataBlock implementations** for characterisation techniques not yet
   supported upstream, prototyped here before upstreaming where appropriate.

## The Dataset

The dataset lives in `data/coin_cell_dataset/` and contains:

| Directory | Technique | Files | Formats | Notes |
|-----------|-----------|-------|---------|-------|
| — | Master plan | 1 | `.xlsx` | `CoinCellAssemble_250Plan.xlsx` — 10 sheets covering cell definitions, masses, capacity calculations, chemical info, and test plans |
| `Neware/` | Electrochemical cycling | 120 | `.xlsx`, `.ndax` | Rate testing, formation cycling; already supported by the upstream `CycleBlock` |
| `TEM/` | Transmission electron microscopy | 48 | `.dm4`, `.tif` | Paired DM4 (Gatan) + TIFF images at 5k–200k× for graphite, NMC622, NMC811 |
| `XPS/` | X-ray photoelectron spectroscopy | 45 | `.VGD`, `.VGX`, `.xlsx` | Thermo Scientific format; survey + elemental scans (C1s, N1s, O1s) for all electrode materials |
| `XRD/` | X-ray diffraction | 9 | `.xrdml`, `.xy` | PANalytical data for LFP and NMC811; already supported by the upstream `XRDBlock` |
| `BET/` | Surface area analysis | 0 | — | Placeholder, no data yet |

### File naming conventions

- **Neware**: `{CellID}_{Anode}_{AnodeDiam}_{Cathode}_{CathodeDiam}_{Repeat}_{TestType}.xlsx`
- **TEM**: `{Material}_{Magnification}.dm4` / `.tif`
- **XPS**: Organised in subdirectories by scan type (Initial Scans / Single Scans)
  and then by material, with files named by scan region (e.g., `C1s Scan.VGD`).
- **XRD**: `{Material}_{2theta_range}_{duration}[_bkg_subtracted].{ext}`

## Work Items

### 1. Ingestion Scripts

**Goal**: Use the `datalab-api` Python package to create items and upload files
into the DIGIBAT datalab instance. Where the API is insufficient, contribute
improvements upstream to `datalab-api`.

Tasks:

- [ ] Parse `CoinCellAssemble_250Plan.xlsx` to extract cell definitions
  (cell ID, cathode/anode type, separator, electrolyte, masses, N/P ratio,
  test plan flags).
- [ ] Handle the multi-sheet structure of the spreadsheet — reconcile data across
  sheets (e.g., `masses` sheet supplements the main plan sheet; `Chemical
  information` sheet has vendor/batch details).
- [ ] Create `starting_material` items — both abstract materials (e.g., "NMC811")
  and batch-specific instances where vendor/batch information is available.
  The exact hierarchy needs clarification from the lab (same batch? same
  vendor across cells?).
- [ ] Create `cell` items for each coin cell with appropriate metadata fields.
- [ ] Create relationships between cells and their constituent starting materials
  (e.g., cathode, anode, separator, electrolyte).
- [ ] Upload and attach characterisation files to the correct items, matching by
  cell ID (Neware) or material name (TEM, XPS, XRD) from filenames.

### 2. XPS DataBlock

**Goal**: Parse and store XPS data in a machine-accessible format. Analysis
features (peak fitting, quantification, reference line overlays) are deferred
— the priority is reliable parsing and structured data extraction.

The `.VGD` files are Thermo Scientific proprietary (OLE2-based). It is not yet
clear whether they follow standard VAMAS (ISO 14976) or require a custom parser
— this needs investigation.

Tasks:

- [ ] Investigate `.VGD` file format — try parsing with `vamas` or similar
  libraries; if that fails, explore the OLE2 structure directly.
- [ ] Implement `XPSBlock(DataBlock)` with:
  - `accepted_file_extensions = (".vgd",)`
  - File parser returning a DataFrame of binding energy vs. intensity
  - Metadata extraction (scan region, pass energy, dwell time, etc.)
  - Basic plot function for visual verification of parsed data
- [ ] Add tests with sample VGD files.

### 3. TEM / Microscopy DataBlock

**Goal**: Display TEM micrographs with extracted metadata. This block is intended
as a prototype for broader microscopy/imaging support in datalab (covering TEM,
SEM, and general electrode imaging). It will be developed here first and
upstreamed to datalab-server once mature.

Both image display and metadata extraction (magnification, scale, instrument
settings) are important.

Tasks:

- [ ] Evaluate `ncempy` vs `hyperspy` for `.dm4` parsing — `ncempy` is lighter
  weight; `hyperspy` is more feature-complete but heavy.
- [ ] Implement `TEMBlock(DataBlock)` (or more general `MicroscopyBlock`) with:
  - `accepted_file_extensions = (".dm4", ".tif")`
  - Image display (convert to base64 PNG or use bokeh image glyph)
  - Metadata extraction from dm4 (magnification, scale bar, acquisition date,
    instrument parameters)
- [ ] Add tests with sample images.
- [ ] Consider generalisation path: SEM images and electrode photos will use
  similar display logic — design with extensibility in mind.

### 4. Future Data Types (deferred)

Additional characterisation data is expected from the DIGIBAT lab. Some already
have upstream block support; others will need new implementations.

**Already supported upstream** (ingestion only needed):

- **CV** (cyclic voltammetry) — handled by the existing `CycleBlock`
- **EIS** (electrochemical impedance spectroscopy) — handled by `EISBlock`
- **Rate testing** — handled by `CycleBlock`

**Will need new blocks or ingestion support**:

- **BET** — adsorption/desorption isotherms and surface area. No data yet.
- **SEM** — scanning electron microscopy. Expected as TIFFs; can share the
  microscopy block developed for TEM (see §3).
- **HPLC** — high-performance liquid chromatography. Will need a new parser
  and block.
- **Electrode images** — optical/photo images of electrodes. Qualitative only;
  handled as standard image display (no specialised block needed).

These will be scoped as data arrives.

## Existing Upstream Support

The following blocks **already exist** in
[datalab-server](https://github.com/datalab-industries/datalab-server) and do
not need reimplementing:

- **`CycleBlock`** — handles `.ndax` (Neware) and `.xlsx` echem files via the
  `navani` library. Also covers CV and rate testing data.
- **`XRDBlock`** — handles `.xrdml` and `.xy` files.
- **`EISBlock`** — handles impedance spectroscopy data.

The ingestion scripts should upload these files so that users can attach them to
the existing upstream blocks.

## Design Consideration: Offline Processing vs JIT Parsing

Current datalab blocks follow a just-in-time (JIT) approach: files are re-parsed
every time a user opens the sample page. This is workable for small files but
becomes impractical at scale (e.g., 3.4 GB of Neware cycling data) and wastes
compute on repeated parsing of static data.

The blocks developed in this plugin should be designed with **offline/batch
processing** in mind:

- **Parse once, cache structured results.** When a file is first attached (or
  via a batch ingestion step), parse it fully and cache the extracted data
  (DataFrames, metadata) on the filesystem alongside the source file — not
  just file-level metadata.
- **Serve from cache on page load.** The block's plot functions should read
  pre-processed data from the cache rather than re-parsing the source file.
- **Invalidate on file change.** If the underlying file is updated (e.g.,
  re-uploaded), the cached data should be regenerated.

This extends the pattern already used by the `CycleBlock`, which caches parsed
data as pickle files (`.RAW_PARSED.pkl`) alongside source files. Upstream work
on asynchronous block processing is underway and expected to land imminently
(days/weeks). The blocks developed here should be designed to slot into that
async processing infrastructure once available.

## Beyond Batch Ingestion: Real-time Data Capture

The ingestion scripts (§1) address the initial backlog of ~250 coin cells. Going
forward, data should flow into datalab automatically as it is acquired. The
planned `datalab-beholder-plugin` will monitor folders on instrument PCs and
upload new files to datalab in real time. This means the blocks developed here
need to handle files appearing incrementally (not just as a one-off batch), and
the async processing infrastructure becomes essential for processing uploads as
they arrive.

## Dependencies

The plugin currently has no runtime dependencies. New dependencies to add:

- `datalab-api` — for ingestion scripts (can be an optional/dev dependency)
- `openpyxl` — for parsing the master spreadsheet
- TBD: XPS parser library (e.g., `vamas`)
- TBD: TEM/microscopy image library (e.g., `ncempy`) for dm4 support

## Repository Structure (planned)

```
src/datalab_app_plugin_digibat/
├── __init__.py
├── _version.py
├── blocks.py          → replace ExampleDataBlock with real blocks
├── xps.py             → XPS block implementation
└── tem.py             → TEM / microscopy block implementation

scripts/
├── ingest_coin_cells.py    → main ingestion script
└── parse_spreadsheet.py    → spreadsheet parsing utilities
```
