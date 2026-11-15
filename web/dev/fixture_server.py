"""前端联调用的本机服务：真实的 HTTP、SSE、显示回执和续读逻辑，模型输出换成固定内容。

不调用任何模型，不联网，数据放在临时目录里，退出时删除，不碰真实的阅读记录。
它只用来检查页面和后端的配合，不能说明真实模型的讲解质量。

    python3 web/dev/fixture_server.py --port 8766

然后打开：
    http://127.0.0.1:8766/?paper=2610.27436v1   HTML 论文：导读 → 两轮讲解 → 一次模型失败 → 主线结束
    http://127.0.0.1:8766/?paper=2601.00002v1   只有 PDF 的论文：开场失败一次 → 导读 → 一轮讲解
"""
import argparse
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HTML_PAPER = "2610.27436v1"
PDF_PAPER = "2601.00002v1"
PDF_PAGES = [
    "Fixture paper, page one. A toy chain has two sites per unit cell.",
    "Fixture paper, page two. The winding number counts loops around the origin.",
    "Fixture paper, page three. The count changes only when the gap closes.",
]


def html_paper_json(core):
    """把 web/mock 里的正文还原成后端缓存的 paper.json 结构。"""
    mock = json.loads((ROOT / "web/mock" / f"paper-{HTML_PAPER}.json").read_text(encoding="utf-8"))
    data = {"arxiv_id": "2610.27436", "version": "v1", "title": mock["title"], "authors": mock["authors"],
            "source": "arxiv_html", "source_url": mock["source_url"], "parser_version": core.PARSER_VERSION,
            "sections": [], "paragraphs": [], "equations": [], "figures": [], "references": []}
    order = 0
    for block in mock["blocks"]:
        order += 10
        if block["type"] == "section":
            data["sections"].append({"id": block["id"], "title": block["title"], "level": block["level"],
                                     "order": order, "end_order": order + 9})
        elif block["type"] == "paragraph":
            inside = [eq for eq in mock["equations"] if f"⟨{eq}⟩" in block["text"]]
            data["paragraphs"].append({"id": block["id"], "section": block["section"], "text": block["text"],
                                       "sentences": [block["text"]], "equations": inside, "notes": [],
                                       "order": order, "end_order": order + 9})
            for offset, eq in enumerate(inside, 1):
                data["equations"].append({"id": eq, "latex": mock["equations"][eq], "paragraph": block["id"],
                                          "section": block["section"], "order": order + offset})
        elif block["type"] == "figure":
            data["figures"].append({"id": block["id"], "caption": block["caption"], "subcaptions": [],
                                    "section": block["section"], "order": order})
    # 样例讲解引用的外部来源要出现在参考文献里，后端才接受。
    data["references"].append({"id": "bib.fixture1", "label": "[1]", "text": "固定样例的参考文献条目。",
                               "links": ["https://arxiv.org/html/1509.02295v1"]})
    return data


def html_script(backend_error):
    """HTML 论文的固定输出：导读、两轮讲解、一次失败、主线结束。"""
    mock = json.loads((ROOT / "web/mock" / f"explanations-{HTML_PAPER}.json").read_text(encoding="utf-8"))
    overview, first, second = (copy.deepcopy(x) for x in mock["explanations"])
    # 步骤名里放进引号和尖括号，检查页面只把它当文字显示。
    overview["route"]["steps"][2]["name"] = '单杂质基线 <b>"x"</b>'
    # 后端要求每一轮讲的就是上一轮说的“接下来”的位置。
    overview["next"]["where"] = copy.deepcopy(first["where"])
    overview["next"]["where"]["quote"] = None
    # 读者档案是空的，除了上一轮实际讲过的概念，其余都只能标成“这一轮补”。
    for assumption in second["assumes"]:
        if assumption["status"] == "unsure":
            assumption["status"] = "new"
    last = copy.deepcopy(second)
    last.update(point="式 (4) 的谱选择规则", where={"ids": ["S2.E4"], "page": None, "quote": None},
                intro="固定样例的最后一轮：用来检查主线结束时页面的表现。", assumes=[], unexpanded=[],
                together="样例文字：只有一个概念，没有需要合起来说的。", back_to_text="样例到这里结束。",
                highlights=[{"ids": ["S2.E4"], "page": None, "quote": None, "label": "式 (4)"}])
    last["concepts"] = last["concepts"][:1]
    last["concepts"][0].update(name="谱选择", in_this_paper="样例文字：式 (4) 按本征值实部的符号给每个分量配上正负号。",
                               in_this_paper_where=["S2.E4"], itself="样例文字，不是真实讲解。", sources=[])
    last["progress"] = {"step": 2, "steps_total": 6, "text": "固定样例的主线到这里结束。", "next": None}
    return [overview, first, second, backend_error("固定样例：模拟一次模型失败。"), last]


def pdf_location(page):
    return {"ids": [], "page": page, "quote": PDF_PAGES[page - 1][:40]}


def pdf_script(backend_error):
    """PDF 论文的固定输出：开场先失败一次，然后是导读和一轮讲解。"""
    overview = {
        "type": "overview", "title": "Fixture PDF paper",
        "question": {"text": "样例 PDF：一条玩具链的绕数怎样定义？", "where": pdf_location(1)},
        "model": {"summary": "样例文字，不是真实讲解。", "items": []},
        "conclusions": {"text": "样例结论。", "where": pdf_location(2)},
        "foundations": [{"name": "绕数", "note": "样例里提到，还没有展开。"}], "previously": [],
        "route": {"steps": [{"step": 1, "name": "模型", "range": "PDF 第 1 页"},
                            {"step": 2, "name": "绕数", "range": "PDF 第 2 页"},
                            {"step": 3, "name": "能隙关闭", "range": "PDF 第 3 页"}], "skim": None},
        "next": {"text": "从第 2 页的绕数讲起。", "where": pdf_location(2)},
        "highlights": [dict(pdf_location(1), label="全文的问题")],
    }
    point = {
        "type": "point", "point": "绕数数的是什么", "section": None, "where": pdf_location(2),
        "intro": "样例文字：这一页给出绕数的说法。",
        "assumes": [{"concept": "绕数", "status": "new", "explained_at": None, "note": "这一轮补。"}],
        "concepts": [{"name": "绕数", "in_this_paper": "样例文字：第 2 页用它数闭合曲线绕原点的圈数。",
                      "in_this_paper_where": ["PDF 第 2 页"], "itself": "样例文字，不是真实讲解。", "sources": []}],
        "together": "样例文字：只有一个概念，没有需要合起来说的。", "back_to_text": "对应第 2 页的那句话。", "unexpanded": [],
        "progress": {"step": 2, "steps_total": 3, "text": "样例到这里结束。", "next": None},
        "highlights": [dict(pdf_location(2), label="这一点")],
    }
    return [backend_error("固定样例：开场的模型失败。"), overview, point]


def pdf_bytes(pages):
    """手写一份最小的 PDF，每页一行文字。"""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages))), len(pages)),
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for index, text in enumerate(pages):
        stream = "BT /F1 14 Tf 72 720 Td (%s) Tj ET" % text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        objects.append("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>" % (5 + 2 * index))
        objects.append("<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1")
    out += b"".join(f"{offset:010d} 00000 n \n".encode("latin-1") for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("latin-1")
    return out


class ScriptedBackend:
    """按论文依次给出固定输出；输出是异常时就抛出它。"""

    def __init__(self, scripts):
        self.scripts = scripts

    def generate(self, prompt, schema):
        # 只认“本轮材料”开头的那一个论文编号：阅读记录里也会出现别的论文的编号。
        current = re.search(r'本轮材料[^\n]*\n\{"paper": "([^"]+)"', prompt)
        for paper, queue in self.scripts.items():
            if current and current.group(1) == paper:
                if not queue:
                    raise RuntimeError("固定样例已经用完。")
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return copy.deepcopy(item)
        raise RuntimeError("固定样例里没有这篇论文。")


class NoNetwork:
    def get(self, url):
        raise OSError("联调服务不联网。")


def main():
    parser = argparse.ArgumentParser(description="前端联调用的固定输出服务")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="paperread-fixture-") as home:
        os.environ["PAPERREAD_HOME"] = home
        from server.backends import BackendError
        from server.http import create_server
        from server.service import Application, core

        papers = Path(home) / "papers"
        core.write_json(papers / HTML_PAPER / "paper.json", html_paper_json(core))
        core.write_json(papers / PDF_PAPER / "paper.json", {
            "arxiv_id": "2601.00002", "version": "v1", "title": "Fixture PDF paper", "authors": ["Synthetic Author"],
            "source": "pdf_only", "source_url": None, "parser_version": core.PARSER_VERSION,
            "sections": [], "paragraphs": [], "equations": [], "figures": [], "references": []})
        (papers / PDF_PAPER / "paper.pdf").write_bytes(pdf_bytes(PDF_PAGES))

        backend = ScriptedBackend({HTML_PAPER: html_script(BackendError), PDF_PAPER: pdf_script(BackendError)})
        app = Application(core.Store(client=NoNetwork()), backend,
                          backend_info={"backend": "fixture", "backends": []})
        server = create_server(app, port=args.port)
        print(f"联调服务已启动：http://127.0.0.1:{server.server_port}/?paper={HTML_PAPER}（固定输出，不调用模型）", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            app.close()


if __name__ == "__main__":
    main()
