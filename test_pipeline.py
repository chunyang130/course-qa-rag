#!/usr/bin/env python3
"""pipeline.py 的自動測試：模擬業務改價目表的各種情況，確認管線該擋的有擋、該放的有放。

用法：python test_pipeline.py
"""
import os, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ["rag.py", "kb_sources.py", "pipeline.py", "knowledge_base", "sources", "eval"]
failed = 0


def check(label, cond, detail=""):
    global failed
    print(("✓ " if cond else "✗ ") + label + ("" if cond else f"\n    {detail[:600]}"))
    failed += not cond


def copy_project(dst):
    os.makedirs(dst)
    for f in FILES:
        src = os.path.join(HERE, f)
        (shutil.copytree if os.path.isdir(src) else shutil.copy)(src, os.path.join(dst, f))
    return dst


def edit_csv(root, old, new):
    path = os.path.join(root, "sources", "pricing.csv")
    text = open(path, encoding="utf-8").read()
    assert old in text, old
    open(path, "w", encoding="utf-8").write(text.replace(old, new))


def run(root, *extra):
    p = subprocess.run([sys.executable, os.path.join(root, "pipeline.py"), "check", "--root", root,
                        "--report", os.path.join(root, "report.md"), *extra],
                       capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout


with tempfile.TemporaryDirectory() as d:
    base = copy_project(os.path.join(d, "base"))

    code, out = run(base)
    check("目前的資料：全部通過", code == 0 and "✅ 通過" in out, out)

    w = copy_project(os.path.join(d, "small"))
    edit_csv(w, "英文會話班,12000", "英文會話班,12500")
    code, out = run(w, "--baseline", base)
    check("小幅調價（+4%）且不影響評估題：通過，並列出變動",
          code == 0 and "英文會話班 NT$12,000 → NT$12,500（+4%）" in out and "pricing#1" in out, out)

    w = copy_project(os.path.join(d, "eval"))
    edit_csv(w, "多益衝刺班,18000", "多益衝刺班,19000")
    code, out = run(w, "--baseline", base)
    check("改了多益價格、評估題沒同步：擋下，並指出 q01 要更新", code == 1 and "q01" in out and "同步更新" in out, out)

    w = copy_project(os.path.join(d, "big"))
    edit_csv(w, "托福全修班,38000", "托福全修班,52000")
    code, out = run(w, "--baseline", base)
    check("大幅調價（+37%）未確認：擋下", code == 1 and "+37%" in out and "確認價格" in out, out)
    code, out = run(w, "--baseline", base, "--price-confirmed")
    check("同樣的調價、已在提交說明確認：通過但標示需要人確認", code == 0 and "需要人確認" in out, out)

    w = copy_project(os.path.join(d, "typo"))
    edit_csv(w, "雅思保證班,36000,每期", "雅思保證班,3600,每月")
    open(os.path.join(w, "sources", "pricing.csv"), "a", encoding="utf-8").write("多益衝刺班,20000,每期,\n")
    code, out = run(w)
    check("打錯價格、計價方式錯誤、課程重複：三項都被找出來",
          code == 1 and "3,600 超出合理範圍" in out and "「每月」" in out and "「多益衝刺班」重複" in out, out)

    w = copy_project(os.path.join(d, "cols"))
    edit_csv(w, "課程,價格,計價方式,備註", "課程,金額,計價方式,備註")
    code, out = run(w)
    check("欄位名稱被改掉：擋下", code == 1 and "欄位必須是" in out, out)

    w = copy_project(os.path.join(d, "long"))
    with open(os.path.join(w, "knowledge_base", "faq.md"), "a", encoding="utf-8") as f:
        f.write("\n## 超長段落\n" + "很長的說明。" * 120 + "\n\n## 空段落\n")
    code, out = run(w, "--baseline", base)
    check("新增超長段落與空段落：擋下，並列出新增段落",
          code == 1 and "超過 500 字" in out and "段落沒有內容" in out and "新增段落" in out, out)

    w = copy_project(os.path.join(d, "retrieval"))
    path = os.path.join(w, "knowledge_base", "class_modes.md")
    open(path, "w", encoding="utf-8").write(open(path, encoding="utf-8").read().replace("缺課可預約該堂錄影補課", "未出席者可預約該堂錄影"))
    code, out = run(w)
    check("改寫文件導致檢索找不到（命中率下降）：擋下並列出沒找到的題目",
          code == 1 and "低於門檻" in out and "沒找到" in out, out)

print(f"\n{'全部通過' if not failed else f'{failed} 項失敗'}")
sys.exit(1 if failed else 0)
