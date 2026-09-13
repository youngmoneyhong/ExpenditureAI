"""Apply Summary formatting only, preserving every workbook cell and formula."""

from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from financial_statement import statement_matrix, style_statement
from sheets import SheetClient, _remove_statement_deficit_rules


def main():
    load_dotenv(ROOT / ".env")
    year = sys.argv[1]
    client = SheetClient.from_env()
    identifier = client._find_spreadsheet_in_folder(year)
    if not identifier:
        raise ValueError("Existing annual workbook not found")
    spreadsheet = client.client.open_by_key(identifier)
    worksheets = spreadsheet.worksheets()
    before = {ws.title: ws.get_all_values(value_render_option="FORMULA") for ws in worksheets}
    backup = ROOT / "backups" / f"summary-style-{year}-{datetime.now():%Y%m%d-%H%M%S}.json"
    backup.parent.mkdir(exist_ok=True)
    backup.write_text(json.dumps({"cells": before, "metadata": spreadsheet.fetch_sheet_metadata()}, ensure_ascii=True), encoding="utf-8")
    summary = next(ws for ws in worksheets if ws.title == "Summary")
    matrix, layout = statement_matrix(client._month_worksheets(spreadsheet), year)
    if [row[0] for row in before["Summary"][:len(matrix)]] != [row[0] for row in matrix]:
        raise ValueError("Summary layout differs; no formatting was applied")
    _remove_statement_deficit_rules(summary, spreadsheet, layout)
    style_statement(summary, spreadsheet, matrix, layout)
    for ws in worksheets:
        assert ws.get_all_values(value_render_option="FORMULA") == before[ws.title], f"Cell contents changed: {ws.title}"
    print(f"Formatting applied. Every cell and formula unchanged across {len(worksheets)} tabs.")
    print(f"Backup: {backup.name}")
    print(f"https://docs.google.com/spreadsheets/d/{identifier}/edit#gid={summary.id}")


if __name__ == "__main__":
    main()
