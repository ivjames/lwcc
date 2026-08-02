#!/usr/bin/env python3
"""lwcc.lab980.com — worship guide site server.

Serves the published worship guides from public/ (self-contained HTML, one
directory per Sunday plus the current guide at /). Stdlib only, run under pm2
behind the site's nginx vhost per lab980 conventions.
"""
import argparse
import http.server
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, 'public')


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC, **kwargs)

    def do_GET(self):
        if self.path == '/healthz':
            body = b'ok\n'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        # Guides are replaced in place when re-rendered; keep caching short.
        self.send_header('Cache-Control', 'public, max-age=300')
        super().end_headers()

    def list_directory(self, path):  # no directory listings
        self.send_error(404, 'Not Found')
        return None

    def log_message(self, fmt, *args):
        sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8061)))
    ap.add_argument('--host', default='127.0.0.1')
    args = ap.parse_args()
    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'lwcc serving {PUBLIC} on http://{args.host}:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
