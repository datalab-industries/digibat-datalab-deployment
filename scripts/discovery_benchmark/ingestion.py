import marimo

__generated_with = "0.21.1"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Coin Cell Assembly Plan Ingestion

    1. Read Excel plan and create entries for each cell
    2. Loop through spreadsheet and find attached characterisation data
    """)
    return


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
def _():
    from pathlib import Path

    from parse_plan import parse_plan

    data = parse_plan(
        Path("data/CoinCellAssemble_250Plan_20260325.xlsx"), ["Intial 40 cells", "Batch 2 cells cellerate", "Batch 3 cells cellerate"],
    )
    return Path, data


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## All cells (combined)
    """)
    return


@app.cell
def _(data):
    data.cells
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Chemical Information
    """)
    return


@app.cell
def _(data, mo):
    mo.md("### Electrolytes")
    mo.output.append(data.electrolytes)
    return


@app.cell
def _(data, mo):
    mo.md("### Electrodes")
    mo.output.append(data.electrodes)
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Echem File Mapping
    """)
    return


@app.cell
def _(data, pd):
    _rows = [
        {"ID_No": id_no, "File": f.name, "Path": str(f)}
        for id_no, files in sorted(data.echem_file_map.items())
        for f in files
    ]
    echem_files = pd.DataFrame(_rows)
    echem_files
    return (echem_files,)


@app.cell
def _():
    import pandas as pd

    return (pd,)


@app.cell(hide_code=True)
def _(data, mo):
    _cells = data.cells
    _matched = set(data.echem_file_map.keys())
    _all_ids = set(_cells["ID_No"].dropna().astype(str))
    _unmatched = sorted(_all_ids - _matched)
    _n_files = sum(len(fs) for fs in data.echem_file_map.values())
    mo.md(f"""
    ## Summary

    **Total cells:** {len(_cells)}

    **Unique cathodes:** {', '.join(_cells['Cathode'].dropna().unique())}

    **Unique anodes:** {', '.join(_cells['Anode'].dropna().unique())}

    **Unique electrolytes:** {', '.join(_cells['Electrolyte'].dropna().unique())}

    **Unique separators:** {', '.join(_cells['Separator_Type'].dropna().unique())}

    ---

    **Echem files found:** {_n_files} files across {len(_matched)} cells

    **Cells without echem data ({len(_unmatched)}):** {', '.join(str(i) for i in _unmatched)}
    """)
    return


@app.cell
def _():
    from datalab_api import DatalabClient
    import os

    client = DatalabClient("https://digibat.dept.ic.ac.uk")
    return (client,)


@app.cell
def _(client):
    client.authenticate()
    return


@app.cell
def _(Path, client, data, echem_files, mo):
    from pprint import pprint
    import traceback
    import time

    def row_to_cell(row):
        cell_id = int(row["ID_No"])

        cell = {}
        cell["item_id"] = cell_id
        cell["name"] = row["Cell_ID"]
        if not isinstance(cell["name"], str):
            cell["name"] = None
        cell["positive_electrode"] = [{"item": {"name": row["Cathode"]}, "unit": "mg", "quantity": row["Cathode_Mass_mg"]}]
        cell["negative_electrode"] = [{"item": {"name": row["Anode"]}, "unit": "mg", "quantity": row["Anode_Mass_mg"]}]
        cell["electrolyte"] = [{"item": {"name": row["Electrolyte"]}, "unit": "μL", "quantity": row["Electrolyte_Volume_uL"]}]

        return cell

    for index, row in mo.status.progress_bar(data.cells.iterrows(), total=300):

        try:
            cell = row_to_cell(row)
        except Exception as e:
            print("Skipping", row["ID_No"], e)
            traceback.print_exc()

        uploaded_files = []

        print(f"=============== {cell['item_id']} =============")

        start = time.monotonic_ns()

        try:
            item = client.create_item(item_id=cell["item_id"], item_type="cells", item_data=cell, collection_id="Discovery-Benchmark")
        except:
            try:
                item = client.get_item(item_id=cell["item_id"])
            except:
                print(f"Skipping bad entry {cell}")
                continue

            uploaded_files = [str(f["name"]) for f in item["files"]]

        print(f"Item creation took {(time.monotonic_ns() - start) / 1e9} s")

        files = echem_files[echem_files["ID_No"] == cell["item_id"]]
        for i, f in files.iterrows():

            if str(Path(f["Path"]).name).replace(" ", "_") in uploaded_files:
                continue

            start = time.monotonic_ns()

            file = client.upload_file(cell["item_id"], f["Path"])

            print(f"File upload {Path(f['Path']).name} took {(time.monotonic_ns() - start) / 1e9} s")

            file_id = file["file_id"]
            start = time.monotonic_ns()

            client.create_data_block(str(cell["item_id"]), "cycle", file_ids=file_id)
            print(f"Block creation took {(time.monotonic_ns() - start) / 1e9} s")

        print(f"Tasks completed/errored: {client.check_tasks()}")
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
