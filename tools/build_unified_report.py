import argparse
from pathlib import Path

from tools.report_data import load_report_paths, write_tool_report


def collect_report_paths(input_dir, patterns):
    input_dir = Path(input_dir)
    paths = []
    for pattern in patterns:
        paths.extend(input_dir.glob(pattern))
    return sorted({
        path.resolve()
        for path in paths
        if path.is_file() and path.stat().st_size > 0
    })

EXCLUDED_REPORTS = {
    "minor_sense_report.tsv",
    "minor_sense_autofix_report.tsv",
}


def main():
    parser = argparse.ArgumentParser(description="Build one report for DigitizationTools.")
    parser.add_argument("input_dir", help="Directory containing source reports.")
    parser.add_argument("output", help="Output JSON path.")
    parser.add_argument(
        "--pattern",
        action="append",
        dest="patterns",
        default=None,
        help="Input glob; repeat to add patterns. Default: *report*.tsv and *report*.csv",
    )
    args = parser.parse_args()

    patterns = args.patterns or [
        "*report*.tsv",
        "*report*.csv",
        "minor_sense_manual_review.tsv",
    ]
    paths = collect_report_paths(args.input_dir, patterns)
    output = Path(args.output).resolve()
    paths = [
        path for path in paths
        if path != output and path.name not in EXCLUDED_REPORTS
    ]
    rows = load_report_paths(paths)
    write_tool_report(output, rows, [path.name for path in paths])
    print(f"Wrote {len(rows)} issues from {len(paths)} reports to {output}")


if __name__ == "__main__":
    main()
