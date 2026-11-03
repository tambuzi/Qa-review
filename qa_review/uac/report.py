"""Acceptance report with evidence for every UAC. One self-contained HTML (images embedded) is the
source; the PDF is printed from it with Playwright's Chromium or an installed Chrome/Chromium/Edge."""

from __future__ import annotations

import base64
import html
import json
import os
import shutil
import subprocess
from pathlib import Path

from .. import __version__
from . import store

LABELS = {
    "es": {"title": "Informe de aceptación", "project": "Proyecto", "task": "Tarea", "host": "Host",
           "branch": "Rama / commit", "date": "Fecha", "run": "Ejecución", "duration": "Duración",
           "summary": "Resumen", "criteria": "Criterios de aceptación", "status": "Estado", "type": "Tipo",
           "source": "Origen", "ref": "Referencia", "tests": "Tests", "evidence": "Evidencias",
           "no_evidence": "Sin evidencias adjuntas.", "no_tests": "Ningún test cubre este criterio.",
           "error": "Error", "orphans": "Tests sin criterio asociado", "appendix": "Detalles de la ejecución",
           "command": "Comando", "output": "Salida", "files": "Otros archivos",
           "verdict": {"pass": "ACEPTADO", "fail": "NO ACEPTADO", "incomplete": "INCOMPLETO"},
           "st": {"passed": "Superado", "failed": "Fallido", "flaky": "Inestable", "skipped": "Omitido",
                  "not_covered": "Sin cubrir"},
           "src": {"task": "tarea", "code": "código"}},
    "en": {"title": "Acceptance report", "project": "Project", "task": "Task", "host": "Host",
           "branch": "Branch / commit", "date": "Date", "run": "Run", "duration": "Duration",
           "summary": "Summary", "criteria": "Acceptance criteria", "status": "Status", "type": "Type",
           "source": "Source", "ref": "Reference", "tests": "Tests", "evidence": "Evidence",
           "no_evidence": "No evidence attached.", "no_tests": "No test covers this criterion.",
           "error": "Error", "orphans": "Tests without a criterion", "appendix": "Run details",
           "command": "Command", "output": "Output", "files": "Other files",
           "verdict": {"pass": "ACCEPTED", "fail": "NOT ACCEPTED", "incomplete": "INCOMPLETE"},
           "st": {"passed": "Passed", "failed": "Failed", "flaky": "Flaky", "skipped": "Skipped",
                  "not_covered": "Not covered"},
           "src": {"task": "task", "code": "code"}},
}

CSS = """
:root{--ink:#1d2433;--muted:#5d6779;--line:#dfe3ea;--soft:#f5f7fa;--pass:#1f7a4d;--pass-bg:#e6f4ec;
--fail:#b42318;--fail-bg:#fdecea;--warn:#9a6700;--warn-bg:#fff4d6;--skip:#5d6779;--skip-bg:#eef1f5;--accent:#2f5bd3}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color:var(--ink);background:#fff}
main{max-width:980px;margin:0 auto;padding:32px 24px 48px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 12px}h3{font-size:15px;margin:18px 0 8px}
.sub{color:var(--muted);margin:0 0 20px}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px 20px;padding:14px 16px;background:var(--soft);border-radius:8px}
.meta div span{display:block;color:var(--muted);font-size:12px}
.meta div b{font-weight:600;word-break:break-all}
.verdict{display:flex;flex-wrap:wrap;align-items:center;gap:16px;margin:20px 0;padding:16px 18px;border-radius:8px;border:1px solid var(--line)}
.verdict .big{font-size:20px;font-weight:700;letter-spacing:.02em}
.v-pass{background:var(--pass-bg);border-color:#b7e1c8}.v-pass .big{color:var(--pass)}
.v-fail{background:var(--fail-bg);border-color:#f5c2bd}.v-fail .big{color:var(--fail)}
.v-incomplete{background:var(--warn-bg);border-color:#f0d58a}.v-incomplete .big{color:var(--warn)}
.counts{display:flex;flex-wrap:wrap;gap:8px}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.passed{background:var(--pass-bg);color:var(--pass)}.failed{background:var(--fail-bg);color:var(--fail)}
.flaky{background:var(--warn-bg);color:var(--warn)}.skipped,.not_covered{background:var(--skip-bg);color:var(--skip)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600;background:var(--soft)}
td.id{font-weight:700;white-space:nowrap}
.uac{border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:18px 0;break-inside:auto}
.uac header{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
.uac header h3{margin:0;flex:1 1 300px}
.tags{color:var(--muted);font-size:12px}
pre{background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:10px 12px;overflow-x:auto;
font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
pre.gherkin .kw{color:var(--accent);font-weight:700}pre.gherkin .tag{color:var(--muted)}
pre.err{background:var(--fail-bg);border-color:#f5c2bd;color:#7a1a12}
.test{margin:14px 0 6px;padding-top:10px;border-top:1px dashed var(--line)}
.test .t{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-weight:600}
.test .d{color:var(--muted);font-weight:400;font-size:12px}
figure{margin:10px 0;break-inside:avoid}
figure img{max-width:100%;border:1px solid var(--line);border-radius:6px}
figcaption{color:var(--muted);font-size:12px;margin-top:4px}
.muted{color:var(--muted)}
footer{margin-top:40px;color:var(--muted);font-size:12px}
@page{size:A4;margin:14mm 12mm}
@media print{main{padding:0;max-width:none}.uac{break-before:page}.uac:first-of-type{break-before:auto}
pre{white-space:pre-wrap}a{color:inherit;text-decoration:none}}
"""

_KW = ("Feature", "Scenario Outline", "Scenario", "Examples", "Background", "Given", "When", "Then", "And",
       "But", "Característica", "Escenario", "Esquema del escenario", "Ejemplos", "Antecedentes", "Dado", "Dada",
       "Cuando", "Entonces", "Y", "Pero")


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _gherkin(text: str) -> str:
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        esc = _e(line)
        if s.startswith("@"):
            out.append(f'<span class="tag">{esc}</span>')
            continue
        for kw in _KW:
            if s.startswith((kw + " ", kw + ":")):
                i = line.index(kw)
                out.append(_e(line[:i]) + f'<span class="kw">{_e(kw)}</span>' + _e(line[i + len(kw):]))
                break
        else:
            out.append(esc)
    return "\n".join(out)


def _attachment(att: dict, run_dir: Path, L: dict) -> str:
    name, ctype, rel = att.get("name", ""), att.get("contentType", ""), att.get("path", "")
    p = Path(rel) if Path(rel).is_absolute() else run_dir / rel
    label = name.split(":", 1)[1].strip() if ":" in name else name
    if ctype.startswith("image/") and p.exists():
        b64 = base64.b64encode(p.read_bytes()).decode()
        return (f'<figure><img alt="{_e(label)}" src="data:{_e(ctype)};base64,{b64}">'
                f"<figcaption>{_e(label)}</figcaption></figure>")
    if ctype in ("application/json", "text/plain") and p.exists():
        body = p.read_text(encoding="utf-8", errors="replace")
        if ctype == "application/json":
            try:
                body = json.dumps(json.loads(body), indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        if len(body) > 6000:
            body = body[:6000] + "\n… (truncated)"
        return f"<figure><figcaption>{_e(name)}</figcaption><pre>{_e(body)}</pre></figure>"
    return ""


def _files(atts: list[dict]) -> list[str]:
    return [a.get("path", "") for a in atts if not (a.get("contentType", "").startswith("image/")
                                                     or a.get("contentType") in ("application/json", "text/plain"))]


def _test(t: dict, run_dir: Path, L: dict) -> str:
    st = t["status"] if t["status"] in L["st"] else "failed"
    head = (f'<div class="test"><div class="t"><span class="badge {st}">{_e(L["st"][st])}</span>{_e(t["title"])}'
            f' <span class="d">{_e(t.get("project") or "")} · {t.get("duration_ms", 0) / 1000:.1f}s · '
            f'{_e(t.get("file") or "")}:{_e(t.get("line") or "")}</span></div>')
    parts = [head]
    if t.get("error"):
        parts.append(f'<pre class="err">{_e(t["error"])}</pre>')
    atts = t.get("attachments", [])
    if any(a.get("name", "").startswith("shot:") for a in atts):   # the test took its own: skip the automatic one
        atts = [a for a in atts if a.get("name") != "screenshot"]
    shown = [_attachment(a, run_dir, L) for a in atts]
    shown = [s for s in shown if s]
    parts += shown or [f'<p class="muted">{_e(L["no_evidence"])}</p>']
    files = _files(t.get("attachments", []))
    if files:
        parts.append(f'<p class="muted">{_e(L["files"])}: ' + ", ".join(f"<code>{_e(f)}</code>" for f in files) + "</p>")
    parts.append("</div>")
    return "".join(parts)


def html_report(run: dict, language: str = "es") -> str:
    L = LABELS.get(language, LABELS["en"])
    run_dir = Path(run["run_dir"])
    c = run["counts"]
    v = run["verdict"]
    counts = " ".join(f'<span class="badge {s}">{c.get(s, 0)} {_e(L["st"][s])}</span>'
                      for s in ("passed", "failed", "flaky", "skipped", "not_covered") if c.get(s))
    meta = [(L["project"], run.get("project")), (L["task"], run.get("task_ref") or run["slug"]),
            (L["host"], run.get("host")), (L["branch"], f'{run.get("branch", "")} · {run.get("commit", "")}'),
            (L["date"], (run.get("created") or "").replace("T", " ")), (L["run"], run["id"]),
            (L["duration"], f'{run.get("duration_sec", 0)} s')]
    rows = []
    for u in run["uacs"]:
        src = L["src"].get(u.get("source") or "", u.get("source") or "")
        rows.append(f'<tr><td class="id"><a href="#{_e(u["id"])}">{_e(u["id"])}</a></td><td>{_e(u["name"])}</td>'
                    f'<td>{_e(", ".join(u["kinds"]))}</td><td>{_e(src)}</td><td>{_e(u.get("ref") or "")}</td>'
                    f'<td>{len(u["tests"])}</td><td><span class="badge {u["status"]}">{_e(L["st"][u["status"]])}</span></td></tr>')
    sections = []
    for u in run["uacs"]:
        body = "".join(_test(t, run_dir, L) for t in u["tests"]) or f'<p class="muted">{_e(L["no_tests"])}</p>'
        sections.append(
            f'<section class="uac" id="{_e(u["id"])}"><header><h3>{_e(u["id"])} · {_e(u["name"])}</h3>'
            f'<span class="badge {u["status"]}">{_e(L["st"][u["status"]])}</span></header>'
            f'<pre class="gherkin">{_gherkin(u["text"])}</pre><h3>{_e(L["evidence"])}</h3>{body}</section>')
    orphans = ""
    if run.get("orphans"):
        orphans = (f'<h2>{_e(L["orphans"])}</h2>' + "".join(_test(t, run_dir, L) for t in run["orphans"]))
    return f"""<!doctype html>
<html lang="{_e(language)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(L["title"])} · {_e(run["feature"])}</title><style>{CSS}</style></head>
<body><main>
<h1>{_e(L["title"])}</h1><p class="sub">{_e(run["feature"])}</p>
<div class="meta">{"".join(f"<div><span>{_e(k)}</span><b>{_e(v_)}</b></div>" for k, v_ in meta)}</div>
<div class="verdict v-{_e(v)}"><span class="big">{_e(L["verdict"][v])}</span>
<span class="counts">{counts}</span><span class="muted">{c.get("total", 0)} UAC</span></div>
{f'<p>{_e(run["description"])}</p>' if run.get("description") else ""}
<h2>{_e(L["summary"])}</h2>
<table><thead><tr><th>UAC</th><th>{_e(L["criteria"])}</th><th>{_e(L["type"])}</th><th>{_e(L["source"])}</th>
<th>{_e(L["ref"])}</th><th>{_e(L["tests"])}</th><th>{_e(L["status"])}</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<h2>{_e(L["criteria"])}</h2>{"".join(sections)}{orphans}
<h2>{_e(L["appendix"])}</h2><p><b>{_e(L["command"])}:</b> <code>{_e(run.get("command"))}</code></p>
<pre>{_e(chr(10).join((run.get("output_tail") or "").splitlines()[-30:]))}</pre>
<footer>qa-review {__version__}</footer>
</main></body></html>
"""


# ------------------------------------------------------------------ PDF
_CHROME_PATHS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Chromium.app/Contents/MacOS/Chromium",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
_CHROME_NAMES = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge", "chrome"]

PW_PDF = """const path = require('path');
let pw; for (const m of ['@playwright/test', 'playwright', 'playwright-core']) { try { pw = require(m); break; } catch (e) {} }
if (!pw) { console.error('playwright not found'); process.exit(3); }
(async () => {
  const browser = await pw.chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + path.resolve(process.argv[2]), { waitUntil: 'load' });
  await page.pdf({ path: process.argv[3], format: 'A4', printBackground: true, preferCSSPageSize: true });
  await browser.close();
})().catch(e => { console.error(String(e)); process.exit(1); });
"""


def find_chrome() -> str | None:
    if env := os.environ.get("QA_REVIEW_CHROME"):
        return env
    for p in _CHROME_PATHS:
        if Path(p).exists():
            return p
    for n in _CHROME_NAMES:
        if exe := shutil.which(n):
            return exe
    return None


def to_pdf(html_path: Path, pdf_path: Path, project_root: Path) -> str:
    """Print the HTML to PDF. Returns the method used; raises RuntimeError with every attempt's reason."""
    reasons = []
    if (project_root / "node_modules").is_dir() and shutil.which("node"):
        script = html_path.parent / "_pdf.cjs"
        script.write_text(PW_PDF, encoding="utf-8")
        r = subprocess.run(["node", str(script), str(html_path), str(pdf_path)], cwd=project_root,
                           capture_output=True, text=True, timeout=180, check=False)
        script.unlink(missing_ok=True)
        if r.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0:
            return "playwright"
        reasons.append(f"playwright: {(r.stderr or r.stdout).strip()[-300:]}")
    chrome = find_chrome()
    if chrome:
        args = [chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--print-to-pdf-no-header",
                f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=180, check=False)
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                return "chrome"
            reasons.append(f"chrome: {(r.stderr or r.stdout).strip()[-300:]}")
        except OSError as e:
            reasons.append(f"chrome ({chrome}): {e.strerror or e}")
    else:
        reasons.append("no Chrome/Chromium/Edge found (set QA_REVIEW_CHROME to its path)")
    raise RuntimeError("could not print the PDF: " + " | ".join(reasons))


def write(root: Path, run: dict, formats: list[str] | None = None) -> dict:
    """Write the report next to the run in the requested formats. Returns {format: path} and errors."""
    st = store.settings(root)
    formats = store.parse_formats(formats) if formats else st.report
    run_dir = Path(run["run_dir"])
    html_path = run_dir / f"uac-report-{run['slug']}.html"
    html_path.write_text(html_report(run, st.language), encoding="utf-8")
    out, errors = {}, []
    if "html" in formats:
        out["html"] = str(html_path)
    if "pdf" in formats:
        pdf_path = run_dir / f"uac-report-{run['slug']}.pdf"
        try:
            out["pdf_method"] = to_pdf(html_path, pdf_path, Path(root))
            out["pdf"] = str(pdf_path)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
            errors.append(str(e))
            out.setdefault("html", str(html_path))      # never leave the user without a report
    if "html" not in formats and "pdf" in out:
        html_path.unlink(missing_ok=True)
    return {"files": out, "errors": errors}
