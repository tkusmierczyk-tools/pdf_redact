#!/usr/bin/env python3
"""Run pdf_redact.py over every PDF in a directory."""
import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("pdf_redact.py")

p = argparse.ArgumentParser()
p.add_argument("directory", type=Path)
p.add_argument("terms")
p.add_argument("-o", "--outdir", type=Path, default=None,
               help="output directory (default: <directory>/redacted)")
p.add_argument("-r", "--recursive", action="store_true")
p.add_argument("-j", "--jobs", type=int, default=1)
# everything after -- goes to pdf_redact.py verbatim, e.g. -s '#' --fixed
p.add_argument("extra", nargs="*", help="extra args for pdf_redact.py")
args = p.parse_args()

outdir = args.outdir or args.directory / "redacted"
outdir.mkdir(parents=True, exist_ok=True)

pattern = "**/*.pdf" if args.recursive else "*.pdf"
pdfs = [f for f in sorted(args.directory.glob(pattern))
        if outdir not in f.parents]          # don't re-process our own output

def run(pdf):
    dst = outdir / pdf.name
    cmd = [sys.executable, str(SCRIPT), "-o", str(dst),
           *args.extra, str(pdf), args.terms]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return pdf, r.returncode, r.stderr

if args.jobs > 1:
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(args.jobs) as ex:
        results = list(ex.map(run, pdfs))
else:
    results = [run(f) for f in pdfs]

failed = 0
for pdf, code, err in results:
    if code:
        failed += 1
        print(f"FAIL {pdf.name}: {err.strip().splitlines()[-1:]}", file=sys.stderr)
    else:
        print(f"ok   {pdf.name}")

print(f"\n{len(pdfs) - failed}/{len(pdfs)} redacted -> {outdir}", file=sys.stderr)
sys.exit(1 if failed else 0)
