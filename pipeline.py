#!/usr/bin/env python3
"""知識庫更新管線：資料檢查 → 變更比對 → 評估閘門。全部通過才可以部署。

知識庫是 AI 回答的依據，資料一改，答案就跟著變。這支程式在上線前把關：
  1. 資料檢查：價目表欄位、價格格式與範圍、重複課程、計價方式
  2. 文件檢查：每份文件要有標題、每段要有內容、段落不能太長（太長會降低檢索準確度）
  3. 變更比對：和上一版比較，列出價格變動與新增／修改／刪除的段落；
              價格變動超過 30% 會擋下，除非提交說明裡寫了「確認價格」（避免打錯的價格直接上線）
  4. 評估閘門：評估題的預期答案必須還在資料裡（資料改了、評估題沒跟著改會被擋下），
              檢索命中率不能低於門檻
只用 Python 標準函式庫，不呼叫 AI 模型，不花錢。

用法：
  python pipeline.py check                              # 檢查目前的資料
  python pipeline.py check --baseline 上一版的資料夾      # 加上和上一版的比較
  python pipeline.py check --baseline ... --price-confirmed  # 已確認大幅調價是正確的
  python pipeline.py preview                            # 看價目表 CSV 產生出來的文件
結束代碼：通過 0、未通過 1。報告同時寫到 output/pipeline_report.md；
在 GitHub Actions 裡執行時，報告會顯示在執行結果的摘要頁。
"""
import argparse, json, os, sys

import kb_sources, rag

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_SECTION_CHARS = 500     # 單一段落上限
PRICE_ALERT = 0.30          # 價格變動超過這個比例要人確認
DEFAULT_MIN_HIT = 1.0       # 檢索命中率門檻


def plain(s):
    return s.replace(",", "").replace("，", "")


class Report:
    def __init__(self):
        self.sections, self.errors, self.warnings = [], 0, 0

    def add(self, title, lines):
        self.sections.append((title, lines))

    def error(self, msg):
        self.errors += 1
        return f"- ❌ {msg}"

    def warn(self, msg):
        self.warnings += 1
        return f"- ⚠️ {msg}"

    @staticmethod
    def ok(msg):
        return f"- ✅ {msg}"

    def markdown(self):
        verdict = ("## ❌ 未通過：不可部署，請依下方說明修正" if self.errors else
                   "## ✅ 通過：可以部署" + (f"（有 {self.warnings} 項需要人確認）" if self.warnings else ""))
        out = ["# 知識庫更新檢查報告", "", verdict, ""]
        for title, lines in self.sections:
            out += [f"### {title}", ""] + (lines or ["- （無）"]) + [""]
        return "\n".join(out)


def check_sources(src_dir, rep):
    rows, errs = kb_sources.read_pricing(os.path.join(src_dir, "pricing.csv"))
    _, gen_errs = kb_sources.generated_docs(src_dir)
    errs += [e for e in gen_errs if e not in errs]
    lines = [rep.error(e) for e in errs] or [rep.ok(f"價目表 {len(rows)} 門課，欄位、價格與計價方式都正確")]
    rep.add("1. 資料檢查（sources/pricing.csv）", lines)
    return rows, not errs


def check_docs(root, rep, sources_ok=True):
    if not sources_ok:
        rep.add("2. 文件檢查", ["- 資料檢查未通過，先修正上面的問題"])
        return None
    try:
        docs = rag.load_docs(os.path.join(root, "knowledge_base"), os.path.join(root, "sources"))
        chunks = rag.load_chunks(os.path.join(root, "knowledge_base"), os.path.join(root, "sources"))
    except SystemExit as e:
        rep.add("2. 文件檢查", [rep.error(str(e))])
        return None
    lines = []
    for stem, text in docs.items():
        if not text.lstrip().startswith("# "):
            lines.append(rep.error(f"{stem}：第一行要是「# 標題」"))
        if "\n## " not in text:
            lines.append(rep.error(f"{stem}：至少要有一個「## 小標」段落"))
    for c in chunks:
        body = c["text"].partition("\n")[2].strip()
        if not body:
            lines.append(rep.error(f"{c['id']}（{c['heading']}）：段落沒有內容"))
        elif len(body) > MAX_SECTION_CHARS:
            lines.append(rep.error(f"{c['id']}（{c['heading']}）：{len(body)} 字，超過 {MAX_SECTION_CHARS} 字，請拆成多個小標"))
    if not lines:
        lines.append(rep.ok(f"{len(docs)} 份文件、{len(chunks)} 個段落，結構正確"))
    rep.add("2. 文件檢查", lines)
    return chunks


def compare(base_root, rows, chunks, rep, price_confirmed=False):
    if not base_root:
        rep.add("3. 和上一版比較", ["- 未提供上一版，略過比較"])
        return
    lines = []
    base_rows, base_errs = kb_sources.read_pricing(os.path.join(base_root, "sources", "pricing.csv"))
    if rows and base_rows and not base_errs:
        old = {r["課程"]: r["價格"] for r in base_rows}
        new = {r["課程"]: r["價格"] for r in rows}
        for name in new.keys() - old.keys():
            lines.append(f"- 新增課程：{name}（NT${new[name]:,}）")
        for name in old.keys() - new.keys():
            lines.append(f"- 刪除課程：{name}（原 NT${old[name]:,}）")
        for name in sorted(new.keys() & old.keys()):
            if new[name] != old[name]:
                pct = (new[name] - old[name]) / old[name]
                msg = f"價格變動：{name} NT${old[name]:,} → NT${new[name]:,}（{pct:+.0%}）"
                if abs(pct) <= PRICE_ALERT:
                    lines.append(f"- {msg}")
                elif price_confirmed:
                    lines.append(rep.warn(msg + "，變動超過 30%，提交說明已註明「確認價格」"))
                else:
                    lines.append(rep.error(msg + "，變動超過 30%，可能是打錯；確認無誤請在提交說明寫「確認價格」"))
    try:
        base_chunks = rag.load_chunks(os.path.join(base_root, "knowledge_base"), os.path.join(base_root, "sources"))
    except SystemExit:
        base_chunks = []
    if chunks is not None and base_chunks:
        old = {c["id"]: c["text"] for c in base_chunks}
        new = {c["id"]: c["text"] for c in chunks}
        for cid in sorted(new.keys() - old.keys()):
            lines.append(f"- 新增段落：`{cid}`")
        for cid in sorted(old.keys() - new.keys()):
            lines.append(f"- 刪除段落：`{cid}`")
        changed = sorted(cid for cid in new.keys() & old.keys() if new[cid] != old[cid])
        if changed:
            lines.append("- 內容有修改的段落：" + "、".join(f"`{c}`" for c in changed))
    rep.add("3. 和上一版比較", lines or ["- 資料沒有變動"])


def check_eval(chunks, eval_path, min_hit, rep):
    if chunks is None:
        rep.add("4. 評估閘門", ["- 前面的檢查未通過，略過評估"])
        return
    qs = json.load(open(eval_path, encoding="utf-8"))
    by_id = {c["id"]: c for c in chunks}
    lines, answerable, hits = [], [q for q in qs if q.get("expected_chunk")], 0
    errors_before = rep.errors
    for q in answerable:
        chunk = by_id.get(q["expected_chunk"])
        if not chunk:
            lines.append(rep.error(f"{q['id']}「{q['question']}」的預期段落 {q['expected_chunk']} 不存在了；"
                                   "段落被刪除或順序改變，評估題要同步更新"))
            continue
        if not any(plain(k) in plain(chunk["text"]) for k in q["keywords_any"]):
            lines.append(rep.error(f"{q['id']}「{q['question']}」的預期答案（{'／'.join(q['keywords_any'])}）"
                                   f"已不在 {q['expected_chunk']}；資料改了，評估題與示範回覆要同步更新"))
    if rep.errors == errors_before:
        lines.append(rep.ok(f"{len(answerable)} 題評估題的預期答案都還在資料裡"))
    index = rag.BM25([c["text"] for c in chunks])
    missed = []
    for q in answerable:
        ids = [c["id"] for c in rag.retrieve(q["question"], chunks, index)]
        if q["expected_chunk"] in ids:
            hits += 1
        else:
            missed.append(q)
    rate = hits / len(answerable) if answerable else 1.0
    msg = f"檢索命中率 {hits}/{len(answerable)}（門檻 {min_hit:.0%}）"
    if rate >= min_hit:
        lines.append(rep.ok(msg))
    else:
        lines.append(rep.error(msg + "，低於門檻"))
        lines += [f"  - 沒找到：{q['id']}「{q['question']}」→ 應該要找到 {q['expected_chunk']}" for q in missed]
    rep.add("4. 評估閘門", lines)


def run_check(args):
    rep = Report()
    root = os.path.abspath(args.root)
    rows, sources_ok = check_sources(os.path.join(root, "sources"), rep)
    chunks = check_docs(root, rep, sources_ok)
    compare(os.path.abspath(args.baseline) if args.baseline else None, rows, chunks, rep, args.price_confirmed)
    check_eval(chunks, os.path.join(root, "eval", "questions.json"), args.min_hit, rep)

    md = rep.markdown()
    print(md)
    os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
    open(args.report, "w", encoding="utf-8").write(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):                 # GitHub Actions 的摘要頁
        open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8").write(md + "\n")
    return 1 if rep.errors else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="檢查資料、比較變更、執行評估閘門")
    c.add_argument("--root", default=HERE, help="要檢查的專案資料夾（預設：這個專案）")
    c.add_argument("--baseline", help="上一版的專案資料夾，用來比較變更")
    c.add_argument("--price-confirmed", action="store_true", help="已確認超過 30% 的價格變動是正確的")
    c.add_argument("--min-hit", type=float, default=DEFAULT_MIN_HIT, help="檢索命中率門檻（0–1）")
    c.add_argument("--report", default=os.path.join("output", "pipeline_report.md"))
    p = sub.add_parser("preview", help="顯示由價目表 CSV 產生的文件")
    p.add_argument("--root", default=HERE)
    args = ap.parse_args()
    if args.cmd == "preview":
        docs, errors = kb_sources.generated_docs(os.path.join(args.root, "sources"))
        for e in errors:
            print("❌", e)
        for stem, text in docs.items():
            print(f"===== {stem}（自動產生）=====\n{text}")
        sys.exit(1 if errors else 0)
    sys.exit(run_check(args))


if __name__ == "__main__":
    main()
