#!/usr/bin/env python3
"""課程問答 HTTP API：把 RAG 包成服務，其他系統送問題過來就能拿到有引用的答案；每次問答記錄到 SQLite。

只用 Python 標準函式庫（http.server、sqlite3）。

啟動：
  python api.py --mock eval/mock_responses.json      # 免費示範，不呼叫 Claude
  python api.py                                      # 實際呼叫 Claude（需 ANTHROPIC_API_KEY）

容器與雲端（Docker／Cloud Run）用環境變數設定，參數沒指定時才讀：
  PORT、HOST、DB_PATH      監聽的埠號、位址、問答紀錄資料庫位置
  ASK_DAILY_LIMIT          /ask 每天最多幾題（0 = 不限制），公開網址避免被大量呼叫、產生 API 費用

端點：
  GET  /                      問答網頁（輸入問題、看答案與引用）
  GET  /health                健康檢查
  GET  /examples              網頁上的範例問題
  GET  /search?q=退費&k=4      只做檢索，回傳相關段落（不呼叫模型）
  POST /ask   {"question": "..."}   檢索＋生成＋驗證，回傳答案與引用
  GET  /stats                 用 SQL 統計問答紀錄：題數、拒答率、驗證問題、最常被問的問題
"""
import argparse, json, os, sqlite3, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import rag

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_QUESTION = 300
WEB_PAGE = os.path.join(HERE, "web", "index.html")
# 網頁上的範例問題：取自評估題組，最後一題是知識庫沒有的內容，用來示範拒答
EXAMPLE_IDS = ["q02", "q06", "q12", "q14", "q01", "q15"]

LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS qa_log (
    id            INTEGER PRIMARY KEY,
    asked_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    question      TEXT NOT NULL,
    answerable    INTEGER,
    answer        TEXT,
    citations     TEXT,      -- JSON：引用的段落編號
    retrieved     TEXT,      -- JSON：檢索到的段落編號
    retried       INTEGER,
    errors        TEXT,      -- JSON：驗證問題
    latency_ms    INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER
);
"""

STATS_SQL = {
    "總覽": """
        SELECT COUNT(*) AS 題數,
               SUM(answerable = 0) AS 拒答數,
               ROUND(100.0 * SUM(answerable = 0) / MAX(COUNT(*), 1), 1) AS 拒答率,
               SUM(retried) AS 觸發重試,
               SUM(errors != '[]') AS 驗證未通過,
               ROUND(AVG(latency_ms)) AS 平均毫秒,
               SUM(input_tokens) AS 輸入token, SUM(output_tokens) AS 輸出token
        FROM qa_log""",
    "最常被問的問題": """
        SELECT question AS 問題, COUNT(*) AS 次數, MAX(asked_at) AS 最近一次
        FROM qa_log GROUP BY question ORDER BY 次數 DESC, 最近一次 DESC LIMIT 10""",
    "拒答的問題（知識庫可能要補資料）": """
        SELECT question AS 問題, COUNT(*) AS 次數
        FROM qa_log WHERE answerable = 0 GROUP BY question ORDER BY 次數 DESC LIMIT 10""",
}


class App:
    def __init__(self, db_path, model, api_key, mock, daily_limit=0):
        self.daily_limit = daily_limit
        self.chunks = rag.load_chunks()
        self.index = rag.BM25([c["text"] for c in self.chunks])
        self.model, self.api_key, self.mock = model, api_key, mock
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with sqlite3.connect(db_path) as con:
            con.executescript(LOG_SCHEMA)
        qs = json.load(open(os.path.join(HERE, "eval", "questions.json"), encoding="utf-8"))
        by_id = {q["id"]: q["question"] for q in qs}
        self.examples = [by_id[i] for i in EXAMPLE_IDS if i in by_id]
        if mock:
            self.mock_by_q = {q["question"]: mock.get(q["id"]) for q in qs}

    def asked_today(self):
        with sqlite3.connect(self.db_path) as con:
            return con.execute("SELECT COUNT(*) FROM qa_log WHERE date(asked_at) = date('now', 'localtime')").fetchone()[0]

    def ask(self, question):
        t0 = time.time()
        if self.mock:
            m = self.mock_by_q.get(question) or {"answerable": False, "answer": rag.NOT_FOUND, "citations": []}
            resp, retrieved, errs, usage, retried = rag.answer(question, self.chunks, self.index, mock=m)
        else:
            resp, retrieved, errs, usage, retried = rag.answer(question, self.chunks, self.index, self.model, self.api_key)
        ms = int((time.time() - t0) * 1000)
        out = {
            "question": question,
            "answerable": bool(resp.get("answerable")),
            "answer": resp.get("answer", ""),
            "citations": [{"chunk_id": c.get("chunk_id"), "quote": c.get("quote")} for c in resp.get("citations", [])],
            "retrieved": [{"chunk_id": c["id"], "score": c["score"]} for c in retrieved],
            "validation_errors": errs,
            "retried": retried,
            "latency_ms": ms,
        }
        with sqlite3.connect(self.db_path) as con:
            con.execute("""INSERT INTO qa_log (question, answerable, answer, citations, retrieved, retried, errors,
                           latency_ms, input_tokens, output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (question, int(out["answerable"]), out["answer"],
                         json.dumps([c["chunk_id"] for c in out["citations"]]),
                         json.dumps([c["chunk_id"] for c in out["retrieved"]]),
                         int(retried), json.dumps(errs, ensure_ascii=False), ms,
                         usage.get("input_tokens", 0), usage.get("output_tokens", 0)))
        return out

    def search(self, q, k):
        return [{"chunk_id": c["id"], "score": c["score"], "text": c["text"]}
                for c in rag.retrieve(q, self.chunks, self.index, k)]

    def stats(self):
        with sqlite3.connect(self.db_path) as con:
            out = {}
            for title, sql in STATS_SQL.items():
                cur = con.execute(sql)
                cols = [d[0] for d in cur.description]
                out[title] = [dict(zip(cols, r)) for r in cur.fetchall()]
            return out


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def send_json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):   # 紀錄寫到 stderr，格式簡化
            sys.stderr.write("[api] %s\n" % (fmt % args))

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                body = open(WEB_PAGE, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if url.path == "/examples":
                return self.send_json(200, {"examples": app.examples})
            if url.path == "/health":
                return self.send_json(200, {"status": "ok", "chunks": len(app.chunks),
                                            "mode": "mock" if app.mock else app.model,
                                            "daily_limit": app.daily_limit or None})
            if url.path == "/search":
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    return self.send_json(400, {"error": "缺少參數 q"})
                try:
                    k = int((qs.get("k") or ["4"])[0])
                except ValueError:
                    return self.send_json(400, {"error": "k 必須是整數"})
                if not 1 <= k <= 10:
                    return self.send_json(400, {"error": "k 要介於 1 到 10"})
                return self.send_json(200, {"query": q, "results": app.search(q, k)})
            if url.path == "/stats":
                return self.send_json(200, app.stats())
            if url.path == "/ask":
                return self.send_json(405, {"error": "/ask 請用 POST"})
            return self.send_json(404, {"error": f"找不到 {url.path}"})

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/ask":
                return self.send_json(404, {"error": "找不到這個端點"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self.send_json(400, {"error": "請送出 JSON，例如 {\"question\": \"多益班多少錢？\"}"})
            q = str(data.get("question", "")).strip() if isinstance(data, dict) else ""
            if not q:
                return self.send_json(400, {"error": "缺少 question"})
            if len(q) > MAX_QUESTION:
                return self.send_json(400, {"error": f"問題太長，上限 {MAX_QUESTION} 字"})
            if app.daily_limit and app.asked_today() >= app.daily_limit:
                return self.send_json(429, {"error": f"今天的問答次數已達上限（{app.daily_limit} 題），請明天再試；/search 不受限制"})
            try:
                return self.send_json(200, app.ask(q))
            except SystemExit as e:   # rag.py 在 API 錯誤時以 SystemExit 回報
                return self.send_json(502, {"error": f"模型服務錯誤：{e}"})

    return Handler


def main():
    ap = argparse.ArgumentParser()
    env = os.environ.get
    ap.add_argument("--port", type=int, default=int(env("PORT", "8000")))
    ap.add_argument("--host", default=env("HOST", "127.0.0.1"))
    ap.add_argument("--db", default=env("DB_PATH", os.path.join("output", "qa_log.db")))
    ap.add_argument("--model", default=rag.DEFAULT_MODEL)
    ap.add_argument("--mock", help="用預先錄好的模型回覆（免費示範）")
    ap.add_argument("--mock-if-no-key", action="store_true",
                    help="沒有 ANTHROPIC_API_KEY 時自動改用示範模式（容器預設，方便免費試跑）")
    ap.add_argument("--daily-limit", type=int, default=int(env("ASK_DAILY_LIMIT", "0")),
                    help="/ask 每天最多幾題，0 = 不限制")
    a = ap.parse_args()
    key = env("ANTHROPIC_API_KEY")
    if not a.mock and not key and a.mock_if_no_key:
        a.mock = os.path.join(HERE, "eval", "mock_responses.json")
        print("未設定 ANTHROPIC_API_KEY，改用示範模式", flush=True)
    mock = json.load(open(a.mock, encoding="utf-8")) if a.mock else None
    if not mock and not key:
        raise SystemExit("找不到 ANTHROPIC_API_KEY。免費示範請加 --mock eval/mock_responses.json")
    app = App(a.db, a.model, key, mock, a.daily_limit)
    server = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    print(f"課程問答 API 已啟動：http://{a.host}:{a.port}（{'示範模式' if mock else a.model}）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
