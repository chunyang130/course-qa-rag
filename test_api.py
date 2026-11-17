#!/usr/bin/env python3
"""api.py 的自動測試：以示範模式啟動 API，實際送出 HTTP 請求並檢查回應與資料庫紀錄。

用法：python test_api.py
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
failed = 0


def check(label, cond, detail=""):
    global failed
    print(("✓ " if cond else "✗ ") + label + ("" if cond else f"  {detail}"))
    failed += not cond


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def call(method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


with tempfile.TemporaryDirectory() as d:
    port = free_port()
    BASE = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "api.py"), "--port", str(port),
                             "--db", os.path.join(d, "qa.db"), "--mock", os.path.join(HERE, "eval", "mock_responses.json")],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(50):   # 等伺服器啟動
            try:
                urllib.request.urlopen(BASE + "/health", timeout=1)
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)

        code, r = call("GET", "/health")
        check("GET /health → 200，載入 26 個段落", code == 200 and r["chunks"] == 26, r)

        code, r = call("GET", "/search?" + urllib.parse.urlencode({"q": "錯過一堂課怎麼補？", "k": 2}))
        check("GET /search：同義詞擴充找到 class_modes#1", code == 200 and r["results"][0]["chunk_id"] == "class_modes#1", r)

        code, r = call("POST", "/ask", {"question": "多益衝刺班一期多少錢？"})
        check("POST /ask：回答含正確金額 18,000", code == 200 and r["answerable"] and "18,000" in r["answer"], r)
        check("POST /ask：附引用段落 pricing#1", any(c["chunk_id"] == "pricing#1" for c in r["citations"]), r["citations"])
        check("POST /ask：第一次金額錯誤被驗證擋下並重試", r["retried"] and r["validation_errors"] == [], r)

        code, r = call("POST", "/ask", {"question": "有沒有日文課？"})
        check("POST /ask：知識庫沒有的內容 → 拒答", code == 200 and not r["answerable"] and "找不到" in r["answer"], r)
        call("POST", "/ask", {"question": "多益衝刺班一期多少錢？"})

        code, r = call("POST", "/ask", {})
        check("缺少 question → 400", code == 400, r)
        code, r = call("POST", "/ask", {"question": "多" * 301})
        check("問題太長 → 400", code == 400, r)
        code, r = call("POST", "/ask", raw=b"not json")
        check("不是 JSON → 400", code == 400, r)
        code, r = call("GET", "/ask")
        check("GET /ask → 405（要用 POST）", code == 405, r)
        code, r = call("GET", "/nope")
        check("不存在的路徑 → 404", code == 404, r)
        code, r = call("GET", "/search?" + urllib.parse.urlencode({"q": "退費", "k": 99}))
        check("k 超出範圍 → 400", code == 400, r)

        code, r = call("GET", "/stats")
        total = r["總覽"][0]
        check("GET /stats：記錄 3 次問答、1 次拒答、2 次重試（多益題問了兩次，每次都先被擋下）",
              (total["題數"], total["拒答數"], total["觸發重試"]) == (3, 1, 2), total)
        top = r["最常被問的問題"][0]
        check("最常被問的問題：多益衝刺班（2 次）", top["問題"].startswith("多益") and top["次數"] == 2, top)
        check("拒答的問題清單：日文課", [x["問題"] for x in r["拒答的問題（知識庫可能要補資料）"]] == ["有沒有日文課？"])
    finally:
        proc.terminate()
        proc.wait(timeout=5)

print(f"\n{'全部通過' if not failed else f'{failed} 項失敗'}")
sys.exit(1 if failed else 0)
