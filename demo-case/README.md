# Reproduce the Zotero RefFlow demo / 复现本次测试

This folder contains the **original example passages** used to prepare the three mixed-format files demonstrated in the README. All source citations are plain text.

本目录包含 README 中三文件测试的**原始测试文本**，引用均为纯文本，没有预先插入 Zotero 引用域。

## Prepare inputs / 制作输入文件

| Text source | Save as | Test feature |
| --- | --- | --- |
| `inputs/01_Introduction_PASTE_INTO_WORD.txt` | `1.docx` | Author–year citations; out-of-order reference list |
| `inputs/02_Crossover_Evidence_PASTE_INTO_PDF.txt` | `2.pdf` (export a text PDF) | Local numeric citations with restarted numbering |
| `inputs/03_Clinical_Implications_PASTE_INTO_TXT.txt` | `3.txt` (UTF-8) | Mixed author strings, duplicate sources, 2016/2017 online-vs-print dates |

第四份 `inputs/04_Optional_Stress_Test_PASTE_INTO_MD.txt` 是另外准备的可选测试材料，**不属于 README 展示的三文件实测结果**。

## Demo task / 测试任务

> Use Zotero RefFlow to merge `1.docx`, `2.pdf`, and `3.txt` in that order. Resolve existing citations, unify the manuscript to Vancouver numbering, identify duplicate works, save the references in a Zotero collection, and deliver a dynamic Zotero-linked Word manuscript. Also produce an evidence review sheet containing each manuscript claim, the verbatim original-source passage, its location, and a support judgment.

本次演示使用另一套 Agent Harness 中的 **DeepSeek V4.1 Flash（High）**，以便展示从已有引用的三个独立文件到 Zotero 可编辑手稿的流程。

## Main test outcomes / 本次展示的结果

- Three numbered sections combined into one Word document; one global numeric bibliography containing **five distinct references**.
- Zotero library view displayed the five works; Word **Add/Edit Bibliography** displayed the same five references.
- The source-evidence review sheet contained manuscript sentences, original quotations, original-source locations, reference identifiers, and support labels. The ten visible rows included **9 Direct + 1 Partial**.

## Ground truth: five intended works / 五篇原始测试文献

| Reference | DOI | PMID |
| --- | --- | --- |
| Dunstan et al., 2012, *Diabetes Care* | `10.2337/dc11-1931` | 22374636 |
| DiPietro et al., 2013, *Diabetes Care* | `10.2337/dc13-0084` | 23761134 |
| Colberg et al., 2016, *Diabetes Care* | `10.2337/dc16-1728` | 27926890 |
| Peddie et al., 2013, *American Journal of Clinical Nutrition* | `10.3945/ajcn.112.051763` | 23803893 |
| Duvivier et al., online 2016 / print 2017, *Diabetologia* | `10.1007/s00125-016-4161-7` | 27904925 |

The test texts deliberately repeat Dunstan and DiPietro, reference Peddie and DiPietro in the same year, and cite Duvivier once as 2016 (online) and once as 2017 (print).

## Images / 配图

Images embedded in the README are in [`../assets/demo/`](../assets/demo/). The merged Word screenshot, bibliography editor and evidence review table are the three key results shown prominently in the README. The original input screenshots and citation style choice are collapsed below them.
