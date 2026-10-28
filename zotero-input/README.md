# Zotero Input

版本：**v0.1.0**

一个可独立使用的文献导入与引文核查 skill。支持中英文文献、指定 Zotero
collection 导入、为无引用文本补充参考文献、引用对应关系笔记与本地快照，
以及正文引文语义核查汇总表。只需安装本 skill，无需另装搜索 skill 或
`zotero-output`。

## 功能

| 功能 | 行为 |
| --- | --- |
| 独立导入 | 从 DOCX、PDF、Markdown、Excel/CSV、RIS、文本识别文献，核实元数据并验证目标 collection 成员关系 |
| 中英文识别 | 优先 DOI，其次 PMID、题名，结合作者和年份检查冲突；支持已核实的中文来源补充元数据 |
| 更新核对 | 仅在用户明确要求时对比来源和目标；移除操作需要授权，默认不删除库中条目 |
| 无引用文本补引用 | 检索并审阅实际证据，生成带引用文本、参考文献及对应关系；询问是否导入 Zotero |
| 引用溯源 | 单独征得同意并提醒较高 token 消耗后，准备或写入引用对应关系子笔记；可导出本地 Markdown 快照 |
| 引文核查 | 核查文献身份、正文语义与原文证据，定位章节、页码和段落，生成 Markdown、CSV 和 JSON 汇总 |
| 可选结构化接口 | 保留每条来源映射及不透明位置标识，供未来调用方使用，不要求后续调用其他 skill |

**确保文献进入指定 collection 是必须满足的目标，减少库内重复是优化。**

1. 目标内已有明确匹配：复用并验证，不重新导入。
2. 仅在选定库其他位置已有：优先将同一 item 加入目标，保留原 collection 成员关系。
3. 库中没有：核实元数据后导入目标。
4. 复用能力不可用或失败：先重新读取目标；确认尚未进入且写入权限允许时，
   保底导入新条目并报告可能重复及原因。

库内存在或接口返回成功不能替代成员关系验证。多个候选、标识冲突、权限拒绝
或无法判断的写入状态不会被当成成功，不会无限重试或连续制造副本。

## 环境要求

- Python 3.10+；主要脚本使用标准库。
- Zotero 导入需要运行中的 Zotero Desktop、本地 API 和 Connector 服务。
- 纯文本检索、证据审核和本地报告无需 Zotero；在线检索需要相应网络访问权限。
- PDF 文本抽取使用已有的 `pdftotext`、`pypdf` 或 `PyPDF2`；没有可用读取器时
  明确报告，不自动安装。扫描件需要可用的 OCR 或已核实文本。
- 不强制安装第三方 Zotero 插件、配置账号或 API key。可选插件不可用时，
  按支持的 Connector 或 Zotero 原生操作路径处理；缺少必要执行能力时报告待完成。

本地 API 按只读使用，绝不直接修改 `zotero.sqlite`。Connector 导入目标是
Zotero 界面当前选中的 collection；`--collection` 只是目标约束，不会替用户
选择文件夹。导入前检查选择，导入后重新读取并验证。

## 安装与使用

将完整 `zotero-input` 目录放入支持本地 skill 的客户端技能目录；不要只复制
`SKILL.md` 或某一个脚本。当前电脑的 Codex 技能目录使用 Junction 指向本目录，
因此只维护一份原文件。Git 仓库位于 skill 目录内，不包含上级项目中的实验数据
和回滚备份；本次版本仅保存在本地，未上传远程。

可直接用自然语言请求，例如：

> 使用 zotero-input，将这个表格中的文献导入“毕设系统综述 → 全文筛”。

> 为这段没有参考文献的文字检索证据并添加引用，完成后询问我是否导入 Zotero。

> 核查这篇文稿的正文引用是否得到被引原文支持，定位页码和段落并制作汇总表。

普通导入不要求正文引用位置、合并顺序、输出样式或映射参数。首次导入不会
默认执行同步删除，也不会默认生成笔记、附件或 PDF。

## 授权与证据边界

- 为文本添加引用不代表同意导入；导入也不代表同意写笔记。
- 深入引用溯源和全文语义核查会先提醒 token 消耗较高，再取得当前明确同意。
- “支持”“部分支持”“已审阅范围内未找到支持”“相矛盾”“无法核实”分别保留。
  原文无法访问不等于引用错误，关键词相似不等于语义支持。
- PDF 物理页序、印刷页码和章节分别记录；自动提取文本块不是已核实自然段。
- 语义判断由实际阅读证据的模型或人工审核者完成，脚本校验结构与来源一致性，
  不独立证明语义正确。不编造摘要、引用原句、页码、段落或缺失身份。
- 本地 Markdown 和报告使用已审阅缓存，记录当时的对应关系，不自动跟踪后来
  修改的正文，也不代表重新验证了当前 Zotero 或 PDF 状态。
- 本 skill 不合并文稿、不生成 Word 动态引用字段，也不自动绕过全文访问限制。

## 脚本入口

以下命令在本仓库根目录运行；路径和 collection key 应替换为实际值。
`--yes` 和各审批标志只记录已取得的用户授权，不能代替授权。

```powershell
python -X utf8 -B scripts/zotero.py status --json
python -X utf8 -B scripts/zotero.py import-references --source references.xlsx --collection TARGETKEY --yes
python -X utf8 -B scripts/zotero.py import-references --source references.docx --collection TARGETKEY --yes --mapping-out mapping.json
python -X utf8 -B scripts/citation_handoff.py --help
python -X utf8 -B scripts/citation_audit.py --help
```

引文核查的审核 JSON 由模型结合原始文稿和实际原文整理，用户不必自己编写：

```powershell
python -X utf8 -B scripts/citation_audit.py report --review audit-review.json --outdir audit-results --approve-audit --acknowledge-token-cost
```

输出 `citation-audit.md`、Excel 可打开的 UTF-8 BOM `citation-audit.csv` 和版本化
`citation-audit.json`。未核实结果仍会输出；退出码 `2` 可能表示未完成或输入错误，
须阅读结果说明，不能一概视为成功。

## 详细说明

- [技能入口与完整流程](SKILL.md)
- [实际读写能力与保底路径](references/write-paths.md)
- [中文文献与元数据补充](references/chinese-references.md)
- [为无引用文本添加引用](references/citation-workflow.md)
- [溯源笔记与本地 Markdown](references/provenance-notes.md)
- [正文引文核查与汇总表](references/citation-audit.md)
- [结构化来源映射接口](references/mapping-api.md)
- [授权后的 collection 成员移除](references/collection-sync.md)
- [内置检索代码来源说明](references/bundled-search.md)

## 验证

在仓库根目录运行隔离单元测试：

```powershell
python -X utf8 -B -m unittest discover -s scripts -p "test_*.py" -v
```

v0.1.0 封版前的已记录验证：232 项技能单元测试、299 项扩展隔离回归和
27 项浏览器模拟断言通过；299 项包含 232 项，不应相加。扩展回归及合成 PDF
烟雾样本位于上级实验目录，不属于本 skill 仓库，仅上述单元测试随仓库提供。

已有个人库导入与可选插件子笔记曾完成实际写入回读验证；新引文核查使用合成
审核数据和实际 PDF 读取器验证，尚未评测真实论文语义判断准确率。不要将模拟
测试通过视为任意环境中的真实 Zotero 集成成功；无插件原生写入、群组库写入等
未实际验证的边界以读写说明为准。

## 代码来源与版本

内置检索代码来自本地 `nature-citation` skill，保留来源和修改说明。
上游许可未确认，本版本不擅自添加开源许可证或宣称可自由再分发；对外发布前
应核实相关代码的授权条件。

版本文件为 [VERSION](VERSION)，对应本地 Git 标签 `v0.1.0`。本次只建立本地
版本记录，不配置远程仓库、不执行推送。
