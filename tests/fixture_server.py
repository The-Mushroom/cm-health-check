#!/usr/bin/env python3
"""A deliberately broken little 'pub' website for testing the health check.

Known faults (the expected score effects are listed in tests/EXPECTED.md):
  duplicate titles, missing meta descriptions, a page without an H1, images without alt,
  a 2.9 MB image, a broken internal link, a 2-hop redirect chain, an old WordPress version,
  an outdated plugin version string, an unprotected contact form, phone numbers not linked,
  a page wider than a phone, a missing viewport on one page, an old copyright year,
  'lorem ipsum' leftover, a legacy Google Maps embed, a PDF menu, no booking link,
  no newsletter sign-up, a bare 404 page, no sitemap, no schema, no privacy policy.
Run:  python3 fixture_server.py 8765
"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent

HEAD = """<!doctype html><html lang="en-GB"><head><meta charset="utf-8">{viewport}
<title>{title}</title>{meta}
<meta name="generator" content="WordPress 6.2.1">
<link rel="stylesheet" href="/wp-content/plugins/contact-form-7/includes/css/styles.css?ver=5.7.2">
<link rel="stylesheet" href="/wp-content/plugins/wordpress-seo/css/x.css?ver=20.1">
<style>body{{font-family:Arial;font-size:13px;margin:0}} .wide{{width:900px;background:#eee}} nav a{{font-size:11px;margin:0 2px}}</style>
</head><body>
<nav><a href="/">Home</a><a href="/menu/">Menu</a><a href="/rooms/">Rooms</a><a href="/contact/">Contact</a><a href="/old-offers/">Offers</a><a href="/about/">About</a></nav>
"""
FOOT = """<footer><p>Call us on 01326 555 123</p><p>&copy; 2021 The Salty Gull</p></footer></body></html>"""
VP = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def page(title, body, meta="", viewport=VP):
    m = f'<meta name="description" content="{meta}">' if meta else ""
    return (HEAD.format(title=title, meta=m, viewport=viewport) + body + FOOT).encode()


PAGES = {
    "/": page("The Salty Gull", """<h1>The Salty Gull, Falmouth</h1>
<p>A harbourside pub with rooms. Call 01326 555 123 to book a table.</p>
<img src="/img/big.jpg" width="1200" height="800">
<img src="/img/small.jpg" alt="Our bar">
<p><a href="/menu.pdf">Download our menu (PDF)</a></p>
<p><a href="/missing-page/">Our history</a></p>""", meta="Harbourside pub with rooms in Falmouth, Cornwall."),
    "/menu/": page("The Salty Gull", """<h1>Menu</h1><p>See our <a href="/menu.pdf">food menu</a> and
<a href="/wine-list.pdf">wine list</a>.</p><img src="/img/small.jpg">"""),
    "/rooms/": page("Rooms | The Salty Gull", """<h2>Rooms</h2><div class="wide">Our rooms have sea views.</div>
<p>Lorem ipsum dolor sit amet.</p><img src="/img/small.jpg">
<div style="font-size:11px;line-height:13px"><a href="/rooms/#a">Room 1</a><br><a href="/rooms/#b">Room 2</a><br><a href="/rooms/#c">Room 3</a><br><a href="/rooms/#d">Room 4</a></div>"""),
    "/contact/": page("Contact | The Salty Gull", """<h1>Contact</h1>
<p>Telephone: <a href="tel:+441326555123">01326 555 123</a></p>
<form method="post" action="/contact/"><input name="your-name"><input name="your-email" type="email"><textarea name="msg"></textarea><button>Send</button></form>
<iframe src="https://maps.google.com/maps?q=falmouth&output=embed" width="600" height="300"></iframe>""",
                      meta="Get in touch with The Salty Gull."),
    "/about/": page("About", """<h1>About us</h1><h1>Our story</h1><p>Family run since 1998.</p>""", viewport=""),
    "/robots.txt": None,
}
REDIRECTS = {"/old-offers/": "/offers-2/", "/offers-2/": "/about/"}
FILES = {"/img/big.jpg": ("big.jpg", "image/jpeg"), "/img/small.jpg": ("small.jpg", "image/jpeg")}
PDFS = {"/menu.pdf", "/wine-list.pdf"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/robots.txt":
            return self._send(200, b"User-agent: *\nDisallow: /wp-admin/\n", "text/plain")
        if p in REDIRECTS:
            return self._send(301, b"", extra={"Location": REDIRECTS[p]})
        if p in PAGES and PAGES[p]:
            return self._send(200, PAGES[p], extra={"X-Content-Type-Options": "nosniff"})
        if p in FILES:
            f, t = FILES[p]
            return self._send(200, (HERE / f).read_bytes(), t)
        if p in PDFS:
            return self._send(200, b"%PDF-1.4\n%fake\n", "application/pdf")
        if p.startswith("/wp-content/"):
            return self._send(200, b"/* css */", "text/css")
        return self._send(404, b"<html><body>Not Found</body></html>")

    do_HEAD = do_GET

    def do_POST(self):
        self._send(200, b"ok")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
