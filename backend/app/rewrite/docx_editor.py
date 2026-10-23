"""DOCX 编辑引擎：按段落 id 定位并改文字，样式保留。

设计要点
    1. **不重建文档，只改 run 的文字**。段落里第一个 run 保留全部格式（字号/加粗/颜色），
       其余 run 清空 —— 这样改完看起来和原文一模一样，不会变成"另一个模板"。
    2. **每个段落有稳定 id**（p12 / t0-r1-c1）。模型只能引用已存在的 id，
       不存在就拒绝执行，避免它到处乱改。
    3. **改动可回滚**。所有操作记在 changes 里，保存前可以整体放弃；
       前端拿到的 diff 就是这份记录。

为什么不让模型直接写 python 改文件
    那样不可复现、无法校验、出错就毁原文件。这里的形态是受限工具：
    模型只输出「改哪个 id、改成什么」，执行由本模块做。
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

logger = logging.getLogger(__name__)

# 单段允许的最大膨胀倍数：模型偶尔会把一句话扩成三行，那是改写不是润色
MAX_GROWTH = 3.0
# 单段改动的最小长度：防它把整段删成一个字
MIN_CHARS = 8
# 改写后最短保留比例。模型看到的是完整段落文本，正常情况下不该砍掉一半；
# 一旦砍了，多半是从别处（如截断过的清单）抄了半句，等于把后半段经历删掉
MIN_KEEP_RATIO = 0.6


@dataclass(slots=True)
class Change:
    op: str            # replace / insert
    pid: str
    before: str
    after: str
    reason: str = ""
    evidence: str = ""
    accepted: bool = True
    rejected_why: str = ""


@dataclass(slots=True)
class OutlineItem:
    pid: str
    text: str
    style: str
    kind: str          # para / table
    editable: bool     # 表格单元格允许改文字，但不允许插入


class DocxEditor:
    """打开一份 DOCX，按段落 id 编辑，另存为新文件（原文件不动）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.doc = Document(str(self.path))
        self.changes: list[Change] = []
        self._paras: dict[str, Paragraph] = {}
        self._outline: list[OutlineItem] = []
        self._index()

    # ---------- 索引 ----------

    def _index(self) -> None:
        body = self.doc.element.body
        p_no = 0
        t_no = 0
        for child in body.iterchildren():
            if child.tag.endswith("}p"):
                para = Paragraph(child, self.doc)
                pid = f"p{p_no}"
                p_no += 1
                text = (para.text or "").strip()
                if not text:
                    continue  # 空段落不参与编辑，避免把间距删掉
                self._paras[pid] = para
                self._outline.append(
                    OutlineItem(
                        pid=pid,
                        text=text,
                        style=self._style_name(para),
                        kind="para",
                        editable=True,
                    )
                )
            elif child.tag.endswith("}tbl"):
                table = Table(child, self.doc)
                # 合并单元格会被 row.cells 重复返回同一个 tc：
                # pdf2docx 转出来的表头常是一整行合并，简历上的姓名会以
                # t0-r0-c0 ~ t0-r0-c4 五个 id 出现在清单里。
                # 不去重的话模型可能对同一个段落改五次，也会白白撑大上下文
                seen_cells: set[int] = set()
                for r_idx, row in enumerate(table.rows):
                    for c_idx, cell in enumerate(row.cells):
                        key = id(cell._tc)
                        if key in seen_cells:
                            continue
                        seen_cells.add(key)
                        for p_idx, para in enumerate(cell.paragraphs):
                            text = (para.text or "").strip()
                            if not text:
                                continue
                            pid = f"t{t_no}-r{r_idx}-c{c_idx}-p{p_idx}"
                            self._paras[pid] = para
                            self._outline.append(
                                OutlineItem(
                                    pid=pid,
                                    text=text,
                                    style=self._style_name(para),
                                    kind="table",
                                    editable=True,
                                )
                            )
                t_no += 1

    @staticmethod
    def _style_name(para: Paragraph) -> str:
        try:
            return para.style.name if para.style is not None else ""
        except Exception:  # noqa: BLE001 - 样式缺失的文档不该让整体失败
            return ""

    # ---------- 读取 ----------

    def outline(self, max_chars: int = 120) -> list[dict]:
        """给模型看的结构清单。文本截断，避免整份简历把上下文撑满。"""
        out = []
        for item in self._outline:
            out.append(
                {
                    "id": item.pid,
                    "style": item.style,
                    "kind": item.kind,
                    "text": item.text if len(item.text) <= max_chars else item.text[:max_chars] + "…",
                }
            )
        return out

    def full_text(self) -> str:
        return "\n".join(item.text for item in self._outline)

    def exists(self, pid: str) -> bool:
        return pid in self._paras

    def get_text(self, pid: str) -> str:
        para = self._paras.get(pid)
        return (para.text or "") if para is not None else ""

    # ---------- 编辑 ----------

    @staticmethod
    def _set_text(para: Paragraph, text: str) -> None:
        """只改文字不改格式：第一个 run 承载全部样式，其余清空。"""
        runs = para.runs
        if not runs:
            para.add_run(text)
            return
        runs[0].text = text
        for run in runs[1:]:
            run.text = ""

    def replace(self, pid: str, new_text: str, reason: str = "", evidence: str = "") -> Change:
        para = self._paras.get(pid)
        before = (para.text or "") if para is not None else ""
        change = Change("replace", pid, before, new_text, reason, evidence)

        if para is None:
            change.accepted, change.rejected_why = False, "段落不存在"
            self.changes.append(change)
            return change

        problem = self._validate(before, new_text)
        if problem:
            change.accepted, change.rejected_why = False, problem
            self.changes.append(change)
            return change

        self._set_text(para, new_text)
        self.changes.append(change)
        return change

    def insert_after(self, pid: str, text: str, reason: str = "", evidence: str = "") -> Change:
        """复制同段落的格式，插一条新要点（用于补一条不存在的经历/技能）。"""
        para = self._paras.get(pid)
        change = Change("insert", pid, "", text, reason, evidence)

        if para is None:
            change.accepted, change.rejected_why = False, "锚点段落不存在"
            self.changes.append(change)
            return change
        if not text.strip():
            change.accepted, change.rejected_why = False, "内容为空"
            self.changes.append(change)
            return change

        # 深拷贝 p 元素再插到锚点之后：样式（含项目符号）随之复制
        new_element = copy.deepcopy(para._p)
        para._p.addnext(new_element)
        new_para = Paragraph(new_element, para._parent)
        self._set_text(new_para, text)

        # 新段落也要能继续被编辑，补进索引
        new_pid = f"{pid}+i{sum(1 for c in self.changes if c.op == 'insert' and c.pid == pid)}"
        self._paras[new_pid] = new_para
        self.changes.append(change)
        change.pid = new_pid  # diff 里记的是新段落 id，便于定位
        return change

    def _validate(self, before: str, after: str) -> str:
        after = (after or "").strip()
        if not after:
            return "改写后为空"
        if len(after) < MIN_CHARS:
            return f"改写后过短（{len(after)} 字），可能是误删"
        if before and len(after) > len(before) * MAX_GROWTH:
            return f"膨胀过度（{len(before)} → {len(after)} 字），超出润色范围"
        # 缩水检查：实测踩过一次 —— 给模型的段落清单被截断到 120 字，
        # 它照着截断版改写，写回时整段就被砍掉了后半部分，
        # 简历上直接出现「具备基础的 …」这种断句
        if before and len(after) < len(before) * MIN_KEEP_RATIO:
            return (
                f"改写后只剩原文的 {len(after) / len(before):.0%}"
                f"（{len(before)} → {len(after)} 字），可能丢掉后半段内容"
            )
        return ""

    # ---------- 输出 ----------

    def save(self, out_path: Path) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(out_path))
        return out_path

    def diff(self) -> list[dict]:
        return [
            {
                "op": c.op,
                "pid": c.pid,
                "before": c.before,
                "after": c.after,
                "reason": c.reason,
                "evidence": c.evidence,
                "accepted": c.accepted,
                "rejected_why": c.rejected_why,
            }
            for c in self.changes
        ]
