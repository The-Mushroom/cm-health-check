#!/usr/bin/env python3
"""
Runs the health check for one form entry and emails the PDF for review.

Used by the GitHub Actions workflow. Everything comes in through environment
variables (see .github/workflows/health-check.yml). The report always goes to
REPORT_TO (Creative Mushroom), never to the person who filled in the form.
"""

import ipaddress
import json
import os
import re
import shlex
import smtplib
import ssl
import subprocess
import sys
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports"
SUBJECT = "A Site Health report for you to read before sending"


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def clean(text, limit):
    """Single line, no control characters, sensible length."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text or "").strip()
    return text[:limit]


def check_url(raw):
    """Accept an ordinary public website address only."""
    raw = clean(raw, 300)
    if not raw:
        raise ValueError("No website address was given.")
    probe = raw if re.match(r"^https?://", raw, re.I) else "https://" + raw
    u = urlparse(probe)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host or "." not in host:
        raise ValueError(f"'{raw}' does not look like a website address.")
    if u.username or u.password or host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError(f"'{raw}' is not a public website address.")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError(f"'{raw}' is not a public website address.")
    except ValueError as e:
        if "public" in str(e):
            raise
    return raw


def send(subject, body, attachments=()):
    host, user, password = env("SMTP_HOST"), env("SMTP_USER"), env("SMTP_PASSWORD")
    to = env("REPORT_TO", "hello@creativemushroom.co.uk")
    if not (host and user and password):
        print("Email settings are missing (SMTP_HOST, SMTP_USER, SMTP_PASSWORD), so nothing was sent.")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Creative Mushroom Health Check", user))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain=user.split("@")[-1])
    msg.set_content(body)
    for path in attachments:
        p = Path(path)
        subtype = "pdf" if p.suffix == ".pdf" else "json"
        msg.add_attachment(p.read_bytes(), maintype="application", subtype=subtype, filename=p.name)
    port = int(env("SMTP_PORT", "465"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)
    print(f"Report emailed to {to}.")
    return True


def contact_block():
    rows = [("Name", clean(env("CONTACT_NAME"), 120)), ("Email", clean(env("CONTACT_EMAIL"), 200)),
            ("Phone", clean(env("CONTACT_PHONE"), 60)), ("Form entry", clean(env("ENTRY_ID"), 20))]
    lines = [f"  {k}: {v}" for k, v in rows if v]
    msg = (os.environ.get("CONTACT_MESSAGE") or "").strip()[:2000]
    if msg:
        lines.append("  Their message:")
        lines += ["    " + ln for ln in msg.splitlines()]
    return "\n".join(lines) if lines else "  (started by hand, no form details)"


def main():
    name = clean(env("BUSINESS_NAME"), 80)
    btype = env("BUSINESS_TYPE", "other").lower()
    btype = btype if btype in ("hospitality", "other") else "other"
    gbp = env("GBP").lower()
    rank = clean(env("RANK"), 300)
    run_url = env("RUN_URL")

    try:
        url = env("SITE_URL") if env("CM_TEST_ALLOW_LOCAL") == "1" else check_url(env("SITE_URL"))
    except ValueError as e:
        print(e)
        send(f"Health check could not run: {name or 'unknown site'}",
             f"The health check did not run because: {e}\n\nWho asked:\n{contact_block()}\n\nRun log: {run_url}\n")
        sys.exit(1)

    cmd = [sys.executable, str(ROOT / "health_check.py"), url, "--business-type", btype, "--out", str(OUT)]
    if name:
        cmd += ["--name", name]
    if gbp in ("match", "mismatch", "none", "na"):
        cmd += ["--gbp", gbp]
    if rank:
        cmd += ["--rank", rank]

    print("Running:", " ".join(shlex.quote(c) for c in cmd))
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=25 * 60)
    print(proc.stdout[-6000:])
    if proc.stderr:
        print(proc.stderr[-6000:], file=sys.stderr)

    pdfs = sorted(OUT.glob("*.pdf"), key=lambda p: p.stat().st_mtime)
    if proc.returncode != 0 or not pdfs:
        tail = (proc.stdout + "\n" + proc.stderr).strip()[-2500:]
        blocked = "bot protection" in proc.stdout + proc.stderr
        send(f"Health check {'blocked by bot protection' if blocked else 'failed'}: {name or url}",
             f"The health check for {url} did not finish, so there is no report yet.\n\n"
             f"Who asked:\n{contact_block()}\n\nLast lines of the log:\n{tail}\n\nFull run log: {run_url}\n")
        sys.exit(0 if blocked else 1)  # a blocked site is a known outcome, not a broken run

    pdf = pdfs[-1]
    data_file = pdf.with_suffix(".json")
    data = json.loads(data_file.read_text(encoding="utf-8")) if data_file.exists() else {}
    cats = data.get("categories", [])
    labels = {"speed": "Speed", "mobile": "Mobile", "findability": "Findability", "trust": "Trust and security",
              "housekeeping": "Housekeeping"}
    score_lines = "\n".join(f"  {labels.get(c['key'], c['key']):<20} {c['score']:>3}/{c['max']}"
                            + (f"   (not measured: {', '.join(c['unmeasured'])})" if c.get("unmeasured") else "")
                            for c in cats)

    gbp_checked = gbp in ("match", "mismatch", "none", "na")
    google_note = ""
    if not gbp_checked and run_url:
        workflow_page = run_url.rsplit("/runs/", 1)[0] + "/workflows/health-check.yml"
        google_note = ("Google Business Profile and Google position are not in this report.\n"
                       f"To add them, use Run workflow on GitHub: {workflow_page}\n\n")
    body = f"""Hello Aaron,

A new Website Health Check is ready for you to read before sending.

Site:     {url}
Business: {name or '(not given)'}{'  (hospitality)' if btype == 'hospitality' else ''}
Score:    {data.get('total', '?')}/100, {data.get('band', '')}

{score_lines}

Who asked:
{contact_block()}

{google_note}The PDF is attached, along with the data file showing every measurement.
When you are happy with it, forward the PDF to them with your own note.

Creative Mushroom Health Check
"""
    subject = f"{SUBJECT}: {name or urlparse(url if '://' in url else 'https://' + url).hostname}"
    attachments = [pdf] + ([data_file] if data_file.exists() else [])
    send(subject, body, attachments)


if __name__ == "__main__":
    main()
