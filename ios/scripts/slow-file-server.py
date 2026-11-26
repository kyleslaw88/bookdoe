#!/usr/bin/env python3
"""A deliberately slow file server for download tests: GET /big.bin streams SIZE_MB megabytes at RATE_MB megabytes
per second, with a Content-Length and a filename, like a book's audio file.
  python3 slow-file-server.py [port=8099] [size_mb=40] [rate_mb=4]
"""
import http.server, sys, time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
SIZE = int(sys.argv[2]) * 1024 * 1024 if len(sys.argv) > 2 else 40 * 1024 * 1024
RATE = float(sys.argv[3]) * 1024 * 1024 if len(sys.argv) > 3 else 4 * 1024 * 1024
CHUNK = 256 * 1024


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mp4")
        self.send_header("Content-Length", str(SIZE))
        self.send_header("Content-Disposition", 'attachment; filename="big.m4b"')
        self.end_headers()
        sent = 0
        try:
            while sent < SIZE:
                n = min(CHUNK, SIZE - sent)
                self.wfile.write(b"\0" * n)
                sent += n
                time.sleep(n / RATE)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
