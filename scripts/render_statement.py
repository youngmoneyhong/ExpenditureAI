"""Export the actual Summary for visual QA; requires optional pymupdf locally."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
import pymupdf
from sheets import SheetClient


def main():
    load_dotenv(ROOT / ".env")
    client = SheetClient.from_env()
    year = sys.argv[1]
    identifier = client._find_spreadsheet_in_folder(year)
    if not identifier:
        raise ValueError("Annual workbook not found")
    spreadsheet = client.client.open_by_key(identifier)
    summary = spreadsheet.worksheet("Summary")
    response = client.client.http_client.request(
        "get", f"https://docs.google.com/spreadsheets/d/{identifier}/export",
        params={"format": "pdf", "gid": str(summary.id), "size": "A3", "portrait": "false",
                "scale": "4", "gridlines": "false", "sheetnames": "false", "printtitle": "false",
                "top_margin": ".25", "bottom_margin": ".25", "left_margin": ".25", "right_margin": ".25",
                "r1": "0", "r2": "46", "c1": "0", "c2": str(len(summary.row_values(1)))},
    )
    output = ROOT / ".statement-test"
    output.mkdir(exist_ok=True)
    pdf = output / "summary.pdf"
    pdf.write_bytes(response.content)
    document = pymupdf.open(pdf)
    for index, page in enumerate(document):
        page.get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6)).save(output / f"summary-{index+1}.png")
    print(f"Rendered {len(document)} page(s) to {output}")


if __name__ == "__main__":
    main()
