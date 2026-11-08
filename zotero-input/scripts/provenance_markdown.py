"""Local citation snapshots from reviewed evidence; no network or Zotero writes."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import citation_handoff as ch
import ingest as ing
import local_state

VERSION = "zotero-input.provenance-markdown/1"


def inline(value):
    if value is None or value == "":
        return "未取得"
    text = str(value).replace("\r", " ").replace("\n", " ")
    fence = "`"
    while fence in text:
        fence += "`"
    return fence + " " + text + " " + fence


def literal(value):
    if value is None or value == "":
        return "未取得\n"
    text = str(value)
    fence = "```"
    while fence in text:
        fence += "`"
    return fence + "text\n" + text + "\n" + fence + "\n"


def from_bundle(bundle, mapping=None):
    ch.check_bundle(bundle)
    adopted = {}
    if mapping is not None:
        if mapping.get("schema_version") != ing.SCHEMA_VERSION or mapping.get("citation_bundle_id") != bundle["bundle_id"]:
            raise ValueError("Mapping does not belong to this reviewed citation bundle")
        for row in mapping["mappings"]:
            key = (row["source"]["document_id"], row["source"]["reference_id"])
            if key in adopted:
                raise ValueError("Duplicate source mapping")
            adopted[key] = row
    usages = []
    for document in bundle["documents"]:
        for citation in document["citations"]:
            for support in citation["supports"]:
                reference_id = support["reference_id"]
                reference = bundle["references"][reference_id]
                source = reference["source_units"][support["source_id"]]
                row = adopted.get((document["document_id"], reference_id))
                verified = bool(row and row.get("outcome") in ing.SUCCESS and row.get("membership_verified") and row.get("item"))
                if verified and row["item"]["library"] != row["target_collection"]["library"]:
                    raise ValueError("Item/collection library mismatch")
                usages.append({"document_id": document["document_id"], "document_title": document.get("title"),
                               "document_path": document.get("source_path"), "document_hash": document["text_hash"],
                               "reference_id": reference_id, "citation_id": citation["citation_id"],
                               "sentence": citation["sentence"], "start": citation["start"], "end": citation["end"],
                               "marker": citation["marker"], "quote": support["quote"],
                               "source": deepcopy(source), "metadata": deepcopy(reference["metadata"]),
                               "raw_reference": reference.get("raw_text"),
                               "grade": support["grade"], "assessment": support["assessment"],
                               "item": deepcopy(row["item"]) if verified else None,
                               "target_collection": deepcopy(row.get("target_collection")) if row else None,
                               "membership_verified": verified, "outcome": row.get("outcome") if row else "not_linked",
                               "fallback_reason": row.get("fallback_reason") if row else None,
                               "known_duplicates": deepcopy(row.get("known_duplicates", [])) if row else []})
    unresolved = deepcopy(bundle["unresolved"])
    if mapping is not None:
        for row in mapping["mappings"]:
            if row.get("outcome") not in ing.SUCCESS or not row.get("membership_verified"):
                unresolved.append({"source": deepcopy(row["source"]), "outcome": row.get("outcome"),
                                   "reason": row.get("reason") or "Zotero membership not verified"})
    return render(bundle["bundle_id"], bundle["prepared_at"], usages, unresolved)


def from_job(job):
    from provenance_notes import check_job
    check_job(job)
    usages = []
    for note in job["notes"]:
        for usage in note["usages"]:
            usages.append({**deepcopy(usage), "document_id": note["document_id"],
                           "document_title": note["document_title"], "document_hash": note["document_hash"],
                           "metadata": deepcopy(note["parent_metadata"]), "item": deepcopy(note["item"]),
                           "target_collection": deepcopy(note["target_collection"]), "membership_verified": True,
                           "outcome": "verified_parent_in_note_plan"})
    recorded = job["notes"][0]["recorded_at"] if job["notes"] else None
    return render(job["citation_bundle_id"], recorded, usages, job["unresolved"])


def render(bundle_id, recorded_at, usages, unresolved):
    lines = ["# 文献引用映射快照", "", "- 格式版本：" + inline(VERSION),
             "- 引用包 ID：" + inline(bundle_id), "- 添加时快照时间：" + inline(recorded_at), "",
             "此文件保留添加引用时的状态，不自动跟踪后续正文修改。位置偏移基于原始正文，区间为 [start, end)。",
             "Zotero 身份和成员状态来自已有映射的验证快照，不代表导出时实时重查，也不表示 Zotero 子笔记已写入。",
             "原文上下文仅为此前实际取得的 source unit（摘要、段落或选定页面），不宣称是整篇全文。",
             "未取得的信息明确标记；此导出不访问网络、不读取新 PDF、不写入 Zotero。", ""]
    for number, usage in enumerate(usages, 1):
        metadata, source = usage["metadata"], usage["source"]
        lines += ["## 引用对应关系 " + str(number), "", "### 使用文档与位置", "",
                  "- 文档：" + inline(usage.get("document_title")),
                  "- 文档 ID：" + inline(usage["document_id"]),
                  "- 文档路径：" + inline(usage.get("document_path")),
                  "- 原始正文 SHA-256：" + inline(usage["document_hash"]),
                  "- 文献来源 ID：" + inline(usage.get("reference_id")),
                  "- 引用位置 ID：" + inline(usage["citation_id"]),
                  "- 正文字符区间：" + inline(str(usage["start"]) + ":" + str(usage["end"])),
                  "- 插入位置建议：原句末尾，偏移 " + inline(usage["end"]),
                  "- 引用编号或标签：" + inline(usage["marker"]), "", "#### 添加时的正文原句", "",
                  literal(usage["sentence"]), "### 文献信息", "",
                  "- 标题：" + inline(metadata.get("title")), "- DOI：" + inline(metadata.get("doi")),
                  "- PMID：" + inline(metadata.get("pmid")), "- 年份：" + inline(metadata.get("year")),
                  "- 生成的参考文献描述：" + inline(ch.reference_text(metadata)),
                  "- 原始参考文献文本：" + inline(usage.get("raw_reference")), "",
                  "### 证据原文与定位", "", "- 证据类型：" + inline(source.get("kind")),
                  "- 来源 ID：" + inline(source.get("source_id")), "- 来源网址：" + inline(source.get("url")),
                  "- 来源文件：" + inline(source.get("path")), "- 原文内容 SHA-256：" + inline(source.get("content_hash")),
                  "- 章节：" + inline(source.get("section")),
                  "- PDF 物理页序（从 1 起）：" + inline(source.get("pdf_page_index")),
                  "- 文献印刷页码或页标签：" + inline(source.get("printed_page_label")),
                  "- 已核实定位：" + inline(source.get("locator_verified")), "",
                  "#### 实际引用的原文句子或段落", "", literal(usage["quote"]),
                  "#### 已取得的原文上下文", "", literal(source.get("text")),
                  "- 支持程度：" + inline(usage.get("grade")), "- 核实说明：" + inline(usage.get("assessment")), "",
                  "### Zotero 关联快照", ""]
        item = usage.get("item")
        lines += ["- 状态：" + inline(usage.get("outcome")),
                  "- 文献库身份：" + inline(json.dumps(item["library"], ensure_ascii=False) if item else None),
                  "- 最终采用的 item key：" + inline(item.get("item_key") if item else None),
                  "- 可靠取得的 item URI：" + inline(item.get("uri") if item else None),
                  "- 目标 collection：" + inline(json.dumps(usage["target_collection"], ensure_ascii=False) if usage.get("target_collection") else None),
                  "- 此映射快照中的成员验证：" + inline(usage.get("membership_verified", False)),
                  "- 保底原因：" + inline(usage.get("fallback_reason")),
                  "- 已知重复关系：" + inline(json.dumps(usage.get("known_duplicates", []), ensure_ascii=False)), ""]
    lines += ["## 未完成与异常", ""]
    lines.append(literal(json.dumps(unresolved, ensure_ascii=False, indent=2)) if unresolved else "无已记录的未完成项。\n")
    return "\n".join(lines)


def write(bundle, path, mapping=None):
    content = from_bundle(bundle, mapping)
    local_state.preflight_output(path)
    local_state.atomic_text(path, content, replace=False)
    return Path(path)
