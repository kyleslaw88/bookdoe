"""A tiny SMTP sink for the browser e2e: accepts one connection at a time, speaks just
enough SMTP (EHLO/HELO, MAIL, RCPT, DATA, QUIT; no auth, no STARTTLS) and appends each
accepted message as one JSON line {from, to, size, subject, attachment} to --out.
Standard library only; never used in production. Usage:
    python scripts/ui-smtp-sink.py --port 2525 --out /tmp/mail.jsonl
"""
import argparse
import asyncio
import email
import json
import sys


async def handle(reader, writer, out):
    def send(line):
        writer.write((line + "\r\n").encode()); return writer.drain()

    await send("220 sink ready")
    sender, rcpts, data = None, [], None
    while True:
        line = await reader.readline()
        if not line:
            break
        cmd = line.decode(errors="replace").strip()
        up = cmd.upper()
        if up.startswith(("EHLO", "HELO")):
            await send("250-sink"); await send("250 SIZE 52428800")
        elif up.startswith("MAIL FROM:"):
            sender = cmd[10:].split(">", 1)[0].strip().lstrip("<"); await send("250 OK")  # drop SIZE= and other params
        elif up.startswith("RCPT TO:"):
            rcpts.append(cmd[8:].split(">", 1)[0].strip().lstrip("<")); await send("250 OK")
        elif up == "DATA":
            await send("354 End data with <CR><LF>.<CR><LF>")
            buf = []
            while True:
                l = await reader.readline()
                if not l or l in (b".\r\n", b".\n"):
                    break
                buf.append(l[1:] if l.startswith(b"..") else l)
            data = b"".join(buf)
            msg = email.message_from_bytes(data)
            attachment = next((p.get_filename() for p in msg.walk() if p.get_filename()), None)
            with open(out, "a") as f:
                f.write(json.dumps({"from": sender, "to": rcpts, "size": len(data), "subject": msg.get("Subject", ""), "attachment": attachment}) + "\n")
            await send("250 OK queued")
            sender, rcpts = None, []
        elif up == "QUIT":
            await send("221 bye"); break
        elif up in ("NOOP", "RSET"):
            await send("250 OK")
        else:
            await send("502 not implemented")
    writer.close()


async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    open(a.out, "a").close()
    server = await asyncio.start_server(lambda r, w: handle(r, w, a.out), "127.0.0.1", a.port)
    print(f"SINK {a.port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
