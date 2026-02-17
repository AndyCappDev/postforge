#!/usr/bin/env python3
# PostForge - A PostScript Interpreter
# Copyright (c) 2025-2026 Scott Bowman
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Visual regression testing for PostForge sample PostScript files.

Usage:
    # Generate baseline reference images (all devices)
    ./visual_test.py --baseline

    # Compare current output against baseline (all devices)
    ./visual_test.py

    # Test specific device(s)
    ./visual_test.py -d png
    ./visual_test.py -d pdf svg --baseline

    # Compare with custom threshold (default 0.1% pixel difference)
    ./visual_test.py --threshold 0.5

    # Test specific samples only
    ./visual_test.py --samples tiger.ps escher.ps

    # Pass extra flags to postforge
    ./visual_test.py --baseline --flags --glyph-cache
    ./visual_test.py --flags --glyph-cache --some-other-flag
"""

import argparse
import concurrent.futures
import html as html_mod
import os
import re
import subprocess
import sys
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import pymupdf
except ImportError:
    pymupdf = None


PROJECT_ROOT = Path(__file__).resolve().parent
SAMPLES_DIR = PROJECT_ROOT / "samples"
LAUNCHER = str(PROJECT_ROOT / ("postforge.sh" if os.name != "nt" else "postforge.bat"))
RASTERIZE_DPI = 300


def get_dirs(device):
    """Get directory paths for a specific device."""
    base = PROJECT_ROOT / f"visual_tests_{device}"
    return SimpleNamespace(
        base=base,
        baseline=base / "baseline",
        current=base / "current",
        diff=base / "diff",
        timings=base / "baseline_timings.txt",
        report=base / "report.html",
        config=PROJECT_ROOT / f"visual_tests_{device}.conf",
    )


def load_config(config_file):
    """Load per-sample threshold overrides from a config file."""
    overrides = {}
    if config_file.exists():
        for line in config_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    overrides[parts[0]] = float(parts[1])
                except ValueError:
                    pass
    return overrides


def format_duration(seconds):
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m {s:.1f}s"


def get_sample_files(specific=None, exclude=None):
    if specific:
        samples = [SAMPLES_DIR / s for s in specific if (SAMPLES_DIR / s).exists()]
    else:
        samples = sorted(
            list(SAMPLES_DIR.glob("*.ps")) + list(SAMPLES_DIR.glob("*.eps"))
        )
    if exclude:
        exclude_set = {s for s in exclude}
        samples = [s for s in samples if s.name not in exclude_set]
    return samples


PS_ERROR_RE = re.compile(r"%%\[\s*Error:.*?\]%%")


def check_stderr_for_errors(stderr):
    """Check stderr for PostScript errors or Python tracebacks."""
    errors = []
    for match in PS_ERROR_RE.finditer(stderr):
        errors.append(match.group(0))
    if "Traceback (most recent call last)" in stderr:
        errors.append(stderr[stderr.index("Traceback"):].strip())
    return errors


def _rasterize_pdf(pdf_path, output_dir):
    """Rasterize all pages of a PDF to PNG at RASTERIZE_DPI using pymupdf."""
    doc = pymupdf.open(str(pdf_path))
    pngs = []
    matrix = pymupdf.Matrix(RASTERIZE_DPI / 72, RASTERIZE_DPI / 72)
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=matrix)
        png_path = output_dir / f"{pdf_path.stem}-{i + 1:04d}.png"
        pix.save(str(png_path))
        pngs.append(png_path)
    doc.close()
    return pngs


def _rasterize_svg(svg_path, output_dir):
    """Rasterize an SVG to PNG at RASTERIZE_DPI using pymupdf."""
    doc = pymupdf.open(str(svg_path))
    pngs = []
    matrix = pymupdf.Matrix(RASTERIZE_DPI / 72, RASTERIZE_DPI / 72)
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=matrix)
        png_path = output_dir / f"{svg_path.stem}-{i + 1:04d}.png"
        pix.save(str(png_path))
        pngs.append(png_path)
    doc.close()
    return pngs


def _render_one(ps_file, output_dir, timeout, extra_flags, device):
    """Render a single sample file. Designed to run in a subprocess pool."""
    name = ps_file.stem
    sample_out = output_dir / name
    sample_out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    try:
        cmd = [LAUNCHER, "-d", device, "--output-dir", str(sample_out)]
        if extra_flags:
            cmd.extend(extra_flags)
        cmd.append(str(ps_file))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.monotonic() - start
        stderr_errors = check_stderr_for_errors(result.stderr)

        if device == "pdf":
            pdfs = sorted(sample_out.glob("*.pdf"))
            pngs = []
            for pdf_path in pdfs:
                pngs.extend(_rasterize_pdf(pdf_path, sample_out))
        elif device == "svg":
            svgs = sorted(sample_out.glob("*.svg"))
            pngs = []
            for svg_path in svgs:
                pngs.extend(_rasterize_svg(svg_path, sample_out))
        else:
            pngs = sorted(sample_out.glob("*.png"))

        if pngs:
            return name, ps_file.name, pngs, elapsed, stderr_errors
        elif stderr_errors:
            return name, ps_file.name, None, elapsed, stderr_errors
        else:
            return name, ps_file.name, "no_pages", elapsed, []
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return name, ps_file.name, "timeout", elapsed, []
    except Exception as e:
        elapsed = time.monotonic() - start
        return name, ps_file.name, ("exception", str(e)), elapsed, []


def _sort_for_render(samples):
    """Sort samples smallest file first for baseline/render phase."""
    return sorted(samples, key=lambda s: s.stat().st_size)


def _sort_for_compare(items, output_dir):
    """Sort comparison items by number of rendered PNGs descending (most pages first)."""
    def _page_count(item):
        name = item[0]
        sample_dir = output_dir / name
        if sample_dir.is_dir():
            return -len(list(sample_dir.glob("*.png")))
        return 0
    return sorted(items, key=_page_count)


def render_samples(samples, output_dir, timeout=60, extra_flags=None, jobs=1,
                   device="png"):
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    timings = {}
    errors = {}
    total_start = time.monotonic()

    if jobs == 1:
        # Serial path — preserves original output ordering
        for ps_file in samples:
            print(f"  Rendering {ps_file.name}...", end=" ", flush=True)
            name, fname, pngs, elapsed, stderr_errors = _render_one(
                ps_file, output_dir, timeout, extra_flags, device)
            _report_render_result(name, fname, pngs, elapsed, stderr_errors,
                                  results, timings, errors)
    else:
        ordered = _sort_for_render(samples)
        with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as pool:
            future_to_file = {
                pool.submit(_render_one, ps_file, output_dir, timeout,
                            extra_flags, device): ps_file
                for ps_file in ordered
            }
            for future in concurrent.futures.as_completed(future_to_file):
                name, fname, pngs, elapsed, stderr_errors = future.result()
                print(f"  Rendered {fname}...", end=" ", flush=True)
                _report_render_result(name, fname, pngs, elapsed, stderr_errors,
                                      results, timings, errors)

    wall_clock = time.monotonic() - total_start
    summed = sum(timings.values())
    print(f"\n  Wall-clock render time: {format_duration(wall_clock)}")
    print(f"  Summed render time: {format_duration(summed)}")
    return results, timings, wall_clock, errors


def _report_render_result(name, fname, pngs, elapsed, stderr_errors,
                          results, timings, errors):
    """Print status and record results for a single render."""
    if isinstance(pngs, list):
        if stderr_errors:
            print(f"OK ({len(pngs)} page(s), {format_duration(elapsed)}) [with errors]")
            errors[name] = stderr_errors
        else:
            print(f"OK ({len(pngs)} page(s), {format_duration(elapsed)})")
        results[name] = pngs
    elif pngs == "no_pages":
        print(f"OK (no pages, {format_duration(elapsed)})")
        results[name] = "no_pages"
    elif pngs == "timeout":
        print(f"TIMEOUT ({format_duration(elapsed)})")
        results[name] = None
    elif isinstance(pngs, tuple) and pngs[0] == "exception":
        print(f"ERROR ({pngs[1]}, {format_duration(elapsed)})")
        results[name] = None
    elif pngs is None:
        print(f"ERROR ({format_duration(elapsed)})")
        errors[name] = stderr_errors
        results[name] = None
    timings[name] = elapsed


def compare_images(baseline_path, current_path):
    img_base = Image.open(baseline_path).convert("RGB")
    img_curr = Image.open(current_path).convert("RGB")

    if img_base.size != img_curr.size:
        return 100.0, None

    pixels_base = img_base.load()
    pixels_curr = img_curr.load()
    w, h = img_base.size
    total = w * h
    diff_count = 0
    diff_img = Image.new("RGB", (w, h), (0, 0, 0))
    diff_pixels = diff_img.load()

    for y in range(h):
        for x in range(w):
            rb, gb, bb = pixels_base[x, y]
            rc, gc, bc = pixels_curr[x, y]
            if rb != rc or gb != gc or bb != bc:
                diff_count += 1
                dr = min(255, abs(rc - rb) * 4)
                dg = min(255, abs(gc - gb) * 4)
                db = min(255, abs(bc - bb) * 4)
                diff_pixels[x, y] = (dr, dg, db)

    pct = (diff_count / total) * 100.0 if total > 0 else 0.0
    return pct, diff_img


def _compare_one(name, ext, current_pngs, render_errs, default_threshold,
                 config_overrides, baseline_dir, diff_dir):
    """Compare one sample against baseline. Returns (name, status, pct, page_data, errs, msg)."""
    if current_pngs == "no_pages":
        if render_errs:
            return (name, "error", 0, None, render_errs, "ERROR (no pages, has errors)")
        else:
            return (name, "skip", 0, None, None, "SKIP (no pages)")

    if current_pngs is None:
        return (name, "error", 0, None, render_errs, "ERROR (render failed)")

    baseline_sample_dir = baseline_dir / name
    if not baseline_sample_dir.exists():
        return (name, "new", 0, None, None, "NEW (no baseline)")

    baseline_pngs = sorted(baseline_sample_dir.glob("*.png"))
    if not baseline_pngs:
        return (name, "missing", 0, None, None, "MISSING (baseline empty)")

    max_pct = 0.0
    page_data = []
    page_count = max(len(baseline_pngs), len(current_pngs))

    for i in range(page_count):
        bp = baseline_pngs[i] if i < len(baseline_pngs) else None
        cp = current_pngs[i] if i < len(current_pngs) else None

        if bp and cp:
            pct, diff_img = compare_images(bp, cp)
            max_pct = max(max_pct, pct)
            diff_path = None
            if diff_img and pct > 0:
                diff_sample = diff_dir / name
                diff_sample.mkdir(parents=True, exist_ok=True)
                diff_path = diff_sample / f"diff-{i:04d}.png"
                diff_img.save(diff_path)
            page_data.append((bp, cp, diff_path))
        else:
            max_pct = 100.0
            page_data.append((bp, cp, None))

    sample_threshold = config_overrides.get(f"{name}{ext}", default_threshold)
    if max_pct <= sample_threshold:
        if sample_threshold != default_threshold:
            msg = f"PASS ({max_pct:.3f}%, threshold {sample_threshold}%)"
        else:
            msg = f"PASS ({max_pct:.3f}%)"
        return (name, "pass", max_pct, page_data, render_errs, msg)
    else:
        msg = f"FAIL ({max_pct:.6f}% difference)"
        return (name, "fail", max_pct, page_data, render_errs, msg)


def _report_compare_result(result):
    """Print the status message for a comparison result."""
    print(result[5])


def _build_report_rows(report_data, html_path, baseline_timings, current_timings,
                       config_overrides, default_threshold):
    """Build HTML table rows for a set of report data. Returns list of row strings."""
    rows = []
    for name, status, pct, pages, errs in sorted(report_data, key=lambda r: (r[1] != "fail", r[0])):
        if status == "pass":
            badge = '<span style="color:green">PASS</span>'
        elif status == "fail":
            badge = f'<span style="color:red">FAIL ({pct:.6f}%)</span>'
        elif status == "new":
            badge = '<span style="color:blue">NEW</span>'
        elif status == "missing":
            badge = '<span style="color:orange">MISSING</span>'
        elif status == "skip":
            badge = '<span style="color:gray">SKIP (no pages)</span>'
        elif status == "error":
            badge = '<span style="color:red">ERROR</span>'
        else:
            badge = f'<span style="color:gray">{status.upper()}</span>'

        bt = baseline_timings.get(name)
        ct = current_timings.get(name)
        time_baseline = format_duration(bt) if bt is not None else "-"
        time_current = format_duration(ct) if ct is not None else "-"
        # Try both .ps and .eps extensions for config lookup
        threshold = (config_overrides or {}).get(f"{name}.ps",
                     (config_overrides or {}).get(f"{name}.eps", default_threshold))
        threshold_str = f"{threshold:g}%"

        imgs = ""
        if pages:
            failed_indices = [i for i, (_, _, diff_p) in enumerate(pages) if diff_p is not None]
            if status == "fail" and failed_indices:
                show_indices = failed_indices
            else:
                show_indices = [0]
            for i in show_indices:
                base_p, curr_p, diff_p = pages[i]
                page_label = f"Page {i + 1}" if len(pages) > 1 else ""
                base_rel = os.path.relpath(base_p, html_path.parent) if base_p else ""
                curr_rel = os.path.relpath(curr_p, html_path.parent) if curr_p else ""
                diff_rel = os.path.relpath(diff_p, html_path.parent) if diff_p else ""
                if page_label:
                    imgs += f'<div style="margin:5px 0"><strong>{page_label}</strong></div>'
                imgs += '<div style="display:flex;gap:10px;margin:5px 0">'
                if base_rel:
                    imgs += f'<div><div>Baseline</div><img src="{base_rel}" style="max-width:300px" onclick="lb(this.src)"></div>'
                if curr_rel:
                    imgs += f'<div><div>Current</div><img src="{curr_rel}" style="max-width:300px" onclick="lb(this.src)"></div>'
                if diff_rel:
                    imgs += f'<div><div>Diff</div><img src="{diff_rel}" style="max-width:300px" onclick="lb(this.src)"></div>'
                imgs += "</div>"

        error_html = ""
        if errs:
            error_html = '<div style="margin-top:5px">'
            for e in errs:
                error_html += f'<pre style="color:red;margin:2px 0;white-space:pre-wrap">{html_mod.escape(e)}</pre>'
            error_html += "</div>"

        page_count = len(pages) if pages else 0
        threshold_display = f'<span style="color:blue">{threshold_str}</span>' if threshold > 0 else threshold_str
        if pct is not None:
            diff_color = "green" if pct <= threshold else "red"
            diff_display = f'Diff: <span style="color:{diff_color}">{pct:.2f}%</span>'
        else:
            diff_display = ""
        if page_count:
            name_cell = f"{name}<br><br>Pages: {page_count}<br>Threshold: {threshold_display}"
        else:
            name_cell = f"{name}<br><br>Threshold: {threshold_display}"
        if diff_display:
            name_cell += f"<br>{diff_display}"

        rows.append(
            f"<tr><td>{name_cell}</td><td>{badge}</td>"
            f"<td>{time_baseline}</td><td>{time_current}</td>"
            f"<td>{imgs}{error_html}</td></tr>"
        )
    return rows


def _build_summary_stats(report_data):
    """Compute summary statistics from report data. Returns dict of counts."""
    counts = {}
    for _, status, _, _, _ in report_data:
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(report_data),
        "pass": counts.get("pass", 0),
        "fail": counts.get("fail", 0),
        "error": counts.get("error", 0),
        "new": counts.get("new", 0),
        "missing": counts.get("missing", 0),
        "skip": counts.get("skip", 0),
    }


REPORT_CSS = """\
body { font-family: sans-serif; margin: 20px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }
th { background: #f0f0f0; }
tfoot td { font-weight: bold; background: #f8f8f8; }
.summary { margin-bottom: 20px; padding: 15px; background: #f8f8f8; border: 1px solid #ddd; border-radius: 4px; }
.summary span { margin-right: 20px; }
.device-nav { margin-bottom: 20px; padding: 10px; background: #e8e8e8; border-radius: 4px; }
.device-nav a { margin-right: 15px; text-decoration: none; font-weight: bold; }
.device-section { margin-bottom: 40px; }
.lightbox { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.9); z-index:1000; cursor:pointer; justify-content:center; align-items:center; }
.lightbox.active { display:flex; }
.lightbox img { max-width:95%; max-height:95%; object-fit:contain; }
td img { cursor: pointer; }"""

REPORT_JS = """\
<div class="lightbox" onclick="this.classList.remove('active')"><img id="lb-img"></div>
<script>
function lb(src){var el=document.querySelector('.lightbox');document.getElementById('lb-img').src=src;el.classList.add('active');}
document.addEventListener('keydown',function(e){if(e.key==='Escape')document.querySelector('.lightbox').classList.remove('active');});
</script>"""


def _build_summary_html(stats, baseline_timings, current_timings,
                        baseline_wall_clock, current_wall_clock, compare_time):
    """Build the summary div HTML."""
    total_bt = sum(v for v in baseline_timings.values()) if baseline_timings else 0
    total_ct = sum(v for v in current_timings.values()) if current_timings else 0
    return f"""<div class="summary">
<div><strong>Total samples:</strong> {stats["total"]}</div>
<div style="margin-top:8px">
<span style="color:green"><strong>{stats["pass"]}</strong> passed</span>
<span style="color:red"><strong>{stats["fail"]}</strong> failed</span>
<span style="color:gray"><strong>{stats["error"]}</strong> errors</span>
<span style="color:blue"><strong>{stats["new"]}</strong> new</span>
<span style="color:orange"><strong>{stats["missing"]}</strong> missing</span>
<span style="color:gray"><strong>{stats["skip"]}</strong> skipped</span>
</div>
<div style="margin-top:8px">
<strong>Baseline total:</strong> {format_duration(total_bt)} &nbsp;|&nbsp;
<strong>Current total:</strong> {format_duration(total_ct)}
</div>
<div style="margin-top:4px">
<strong>Baseline wall-clock:</strong> {format_duration(baseline_wall_clock) if baseline_wall_clock else "-"} &nbsp;|&nbsp;
<strong>Current wall-clock:</strong> {format_duration(current_wall_clock) if current_wall_clock else "-"} &nbsp;|&nbsp;
<strong>Comparison time:</strong> {format_duration(compare_time) if compare_time else "-"}
</div>
</div>"""


def _build_table_html(rows, baseline_timings, current_timings):
    """Build the full table HTML including header and footer."""
    total_bt = sum(v for v in baseline_timings.values()) if baseline_timings else 0
    total_ct = sum(v for v in current_timings.values()) if current_timings else 0
    return f"""<table>
<thead><tr><th>Sample</th><th>Status</th><th>Baseline Time</th><th>Current Time</th><th>Images</th></tr></thead>
<tbody>
{"".join(rows)}
</tbody>
<tfoot><tr><td>Total</td><td></td><td>{format_duration(total_bt)}</td><td>{format_duration(total_ct)}</td><td></td></tr></tfoot>
</table>"""


def generate_html_report(report_data, html_path, baseline_timings, current_timings,
                         config_overrides=None, default_threshold=0,
                         baseline_wall_clock=None, current_wall_clock=None,
                         compare_time=None, device="png"):
    rows = _build_report_rows(report_data, html_path, baseline_timings,
                              current_timings, config_overrides, default_threshold)
    stats = _build_summary_stats(report_data)
    summary_html = _build_summary_html(stats, baseline_timings, current_timings,
                                       baseline_wall_clock, current_wall_clock,
                                       compare_time)
    table_html = _build_table_html(rows, baseline_timings, current_timings)

    html = f"""<!DOCTYPE html>
<html><head><title>PostForge Visual Regression Report - {device.upper()}</title>
<style>
{REPORT_CSS}
</style></head><body>
<h1>PostForge Visual Regression Report - {device.upper()}</h1>
{summary_html}
{table_html}
{REPORT_JS}
</body></html>"""

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html)
    print(f"HTML report written to {html_path}")


def generate_combined_report(device_results, html_path):
    """Generate a combined HTML report spanning multiple devices."""
    # Build navigation bar
    nav_parts = []
    for dr in device_results:
        device = dr["device"]
        stats = _build_summary_stats(dr["report_data"])
        label = f'{device.upper()} ({stats["pass"]}P/{stats["fail"]}F/{stats["error"]}E)'
        nav_parts.append(f'<a href="#{device}">{label}</a>')
    nav_html = '<div class="device-nav">' + " | ".join(nav_parts) + "</div>"

    # Build per-device sections
    sections = []
    for dr in device_results:
        device = dr["device"]
        stats = _build_summary_stats(dr["report_data"])
        summary_html = _build_summary_html(
            stats, dr["baseline_timings"], dr["current_timings"],
            dr["baseline_wall_clock"], dr["current_wall_clock"],
            dr["compare_elapsed"])
        rows = _build_report_rows(
            dr["report_data"], html_path, dr["baseline_timings"],
            dr["current_timings"], dr["config_overrides"],
            dr["default_threshold"])
        table_html = _build_table_html(rows, dr["baseline_timings"],
                                       dr["current_timings"])
        sections.append(f"""<div id="{device}" class="device-section">
<h2>{device.upper()}</h2>
{summary_html}
{table_html}
</div>""")

    html = f"""<!DOCTYPE html>
<html><head><title>PostForge Visual Regression Report - Combined</title>
<style>
{REPORT_CSS}
</style></head><body>
<h1>PostForge Visual Regression Report - Combined</h1>
{nav_html}
{"".join(sections)}
{REPORT_JS}
</body></html>"""

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html)
    print(f"Combined HTML report written to {html_path}")


def save_timings(timings, wall_clock, timings_file):
    timings_file.parent.mkdir(parents=True, exist_ok=True)
    with open(timings_file, "w") as f:
        f.write(f"__wall_clock__\t{wall_clock}\n")
        for name, elapsed in sorted(timings.items()):
            f.write(f"{name}\t{elapsed}\n")


def load_timings(timings_file):
    timings = {}
    wall_clock = None
    if timings_file.exists():
        for line in timings_file.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                if parts[0] == "__wall_clock__":
                    wall_clock = float(parts[1])
                else:
                    timings[parts[0]] = float(parts[1])
    return timings, wall_clock


def cmd_baseline(args, device, dirs):
    samples = get_sample_files(args.samples, args.exclude)
    if not samples:
        print("No sample files found.")
        return 1

    if dirs.baseline.exists():
        shutil.rmtree(dirs.baseline)

    print(f"Generating baseline for {len(samples)} samples...")
    results, timings, wall_clock, errors = render_samples(
        samples, dirs.baseline, timeout=args.timeout, extra_flags=args.flags,
        jobs=args.jobs, device=device)
    save_timings(timings, wall_clock, dirs.timings)

    ok = sum(1 for v in results.values() if v is not None)
    fail = sum(1 for v in results.values() if v is None)
    print(f"Baseline complete: {ok} succeeded, {fail} failed")
    if errors:
        print(f"  {len(errors)} sample(s) had errors:")
        for name, errs in sorted(errors.items()):
            for e in errs:
                print(f"    {name}: {e}")
    return 0


def cmd_compare(args, device, dirs):
    """Run comparison for a single device. Returns a result dict."""
    if Image is None:
        print("Error: Pillow is required for comparison. Install with: pip install Pillow")
        return {"exit_code": 1}

    if not dirs.baseline.exists():
        print(f"No baseline found for {device}. Run with --baseline first.")
        return {"exit_code": 1}

    samples = get_sample_files(args.samples, args.exclude)
    if not samples:
        print("No sample files found.")
        return {"exit_code": 1}

    if dirs.current.exists():
        shutil.rmtree(dirs.current)
    if dirs.diff.exists():
        shutil.rmtree(dirs.diff)

    print(f"Rendering {len(samples)} samples...")
    results, current_timings, current_wall_clock, render_errors = render_samples(
        samples, dirs.current, timeout=args.timeout, extra_flags=args.flags,
        jobs=args.jobs, device=device)
    baseline_timings, baseline_wall_clock = load_timings(dirs.timings)

    print("Comparing against baseline...")
    compare_start = time.monotonic()
    config_overrides = load_config(dirs.config)
    report_data = []
    pass_count = 0
    fail_count = 0
    new_count = 0
    missing_count = 0
    error_count = 0
    skip_count = 0

    # Build comparison work items
    compare_items = []
    for name, current_pngs in sorted(results.items()):
        matching = [s for s in samples if s.stem == name]
        ext = matching[0].suffix if matching else ".ps"
        compare_items.append((name, ext, current_pngs, render_errors.get(name)))

    if args.jobs == 1:
        compare_results = []
        for name, ext, current_pngs, errs in compare_items:
            print(f"  Comparing {name}{ext}...", end=" ", flush=True)
            r = _compare_one(name, ext, current_pngs, errs, args.threshold,
                             config_overrides, dirs.baseline, dirs.diff)
            compare_results.append(r)
            _report_compare_result(r)
    else:
        ordered_items = _sort_for_compare(compare_items, dirs.current)
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {
                pool.submit(_compare_one, name, ext, current_pngs, errs,
                            args.threshold, config_overrides,
                            dirs.baseline, dirs.diff): (name, ext)
                for name, ext, current_pngs, errs in ordered_items
            }
            compare_results = []
            for future in concurrent.futures.as_completed(futures):
                r = future.result()
                name, ext = futures[future]
                print(f"  Compared {name}{ext}...", end=" ", flush=True)
                compare_results.append(r)
                _report_compare_result(r)

    for name, status, max_pct, page_data, errs, _ in compare_results:
        report_data.append((name, status, max_pct, page_data, errs))
        if status == "pass":
            pass_count += 1
        elif status == "fail":
            fail_count += 1
        elif status == "new":
            new_count += 1
        elif status == "missing":
            missing_count += 1
        elif status == "error":
            error_count += 1
        elif status == "skip":
            skip_count += 1

    compare_elapsed = time.monotonic() - compare_start
    print(f"\n  Comparison time: {format_duration(compare_elapsed)}")
    if error_count > 0:
        print(f"ERRORS: {error_count} (render failed)")
    print(f"Results: {pass_count} passed, {fail_count} failed, "
          f"{skip_count} skipped (no pages), {new_count} new (no baseline), "
          f"{missing_count} missing (empty baseline)")

    report_path = Path(args.html) if (args.html and len(args.device) == 1) else dirs.report
    generate_html_report(report_data, report_path, baseline_timings, current_timings,
                         config_overrides, args.threshold,
                         baseline_wall_clock, current_wall_clock, compare_elapsed,
                         device)

    return {
        "exit_code": 1 if fail_count > 0 else 0,
        "device": device,
        "report_data": report_data,
        "baseline_timings": baseline_timings,
        "current_timings": current_timings,
        "config_overrides": config_overrides,
        "default_threshold": args.threshold,
        "baseline_wall_clock": baseline_wall_clock,
        "current_wall_clock": current_wall_clock,
        "compare_elapsed": compare_elapsed,
    }


def check_device_deps(devices):
    """Check that required dependencies are installed for the requested devices."""
    missing = []
    if "pdf" in devices and pymupdf is None:
        missing.append("PyMuPDF (pip install pymupdf) — required for PDF testing")
    if "svg" in devices and pymupdf is None:
        missing.append("PyMuPDF (pip install pymupdf) — required for SVG testing")
    if missing:
        print("Missing dependencies:")
        for m in missing:
            print(f"  - {m}")
        print("\nInstall with: pip install 'postforge[visual-test]'")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Visual regression testing for PostForge")
    parser.add_argument("--baseline", action="store_true", help="Generate baseline images")
    parser.add_argument("--threshold", type=float, default=0,
                        help="Max allowed pixel difference %% (default: 0)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Per-sample render timeout in seconds (default: 600)")
    parser.add_argument("--samples", nargs="*", help="Specific sample filenames to test")
    parser.add_argument("--html", type=str,
                        help="Path for HTML report (default: visual_tests_{device}/report.html)")
    parser.add_argument("--exclude", nargs="*", default=None,
                        help="Sample filenames to exclude (e.g. --exclude EazyBBS.ps JavaPlatform.ps)")
    parser.add_argument("-j", "--jobs", type=int, default=4,
                        help="Number of parallel render/compare workers (default: 4)")
    parser.add_argument("-d", "--device", nargs="+",
                        choices=["png", "pdf", "svg", "all"],
                        default=["png", "pdf", "svg"],
                        help="Device(s) to test (default: all three)")
    parser.add_argument("--flags", nargs=argparse.REMAINDER, default=None,
                        help="Extra flags to pass to postforge (e.g. --flags --glyph-cache). Must be last argument.")
    args = parser.parse_args()

    # Expand "all" and deduplicate
    if "all" in args.device:
        args.device = ["png", "pdf", "svg"]
    else:
        seen = set()
        args.device = [d for d in args.device if d not in seen and not seen.add(d)]

    if not check_device_deps(args.device):
        return 1

    exit_code = 0
    device_compare_results = []

    for device in args.device:
        print(f"\n{'=' * 60}")
        print(f"  Device: {device.upper()}")
        print(f"{'=' * 60}\n")
        dirs = get_dirs(device)

        if args.baseline:
            rc = cmd_baseline(args, device, dirs)
            if rc != 0:
                exit_code = rc
        else:
            result = cmd_compare(args, device, dirs)
            if result["exit_code"] != 0:
                exit_code = result["exit_code"]
            if "report_data" in result:
                device_compare_results.append(result)

    # Generate combined report when multiple devices were compared
    if len(device_compare_results) > 1:
        combined_path = (Path(args.html) if args.html
                         else PROJECT_ROOT / "visual_tests_report.html")
        generate_combined_report(device_compare_results, combined_path)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
