"""
Fake Jev server for trying the extension without a TypeSafe key.
Flags posts by keyword only. Use it to check the extension works, not to judge accuracy.

    python mock_jev.py            (serves http://127.0.0.1:8787)

Then in the extension's Settings: API key = sk-test, API base URL = http://127.0.0.1:8787, Save.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEYWORDS = {
    "ragebait": ["idiots", "disgusting", "destroy", "every single one of them"],
    "slop": ["here are 7", "let that sink in", "read that again", "game changer"],
    "bait": ["like and repost", "comment yes", "follow for more", "bookmark this"],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer sk-test":
            self.send_response(401)
            self.cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        text = json.dumps(body["state"], ensure_ascii=False).lower()
        answers = {}
        for qid, q in body["questions"].items():
            hit = any(w in text for w in KEYWORDS.get(qid, []))
            answers[qid] = {"type": "noul", "noul": 0.93 if hit else 0.07}
        print("%-40s %s" % (body["state"].get("post_text", "")[:40].replace("\n", " "),
                            {k: v["noul"] for k, v in answers.items()}))
        out = json.dumps({"model": body.get("model"), "answers": answers,
                          "usage": {"input_tokens": 300, "output_tokens": 3}}).encode()
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print("fake Jev on http://127.0.0.1:%d  (key: sk-test)" % port)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
