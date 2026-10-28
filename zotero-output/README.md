# Zotero Output — v0.1.1

将多份文档合并，或整理单份文档中的混乱引用，生成与 Zotero 文献库关联、可继续编辑的 Word 文档。

支持 DOCX、TXT、Markdown 和有可提取文本的 PDF。保留正文与文献之间的对应关系，去重后统一引用格式与编号。DOCX 使用原生 Zotero 引用域；后续在 Word 中通过 Zotero **添加/编辑引文**及**刷新**管理引用。

## 功能与触发

- 合并多篇文稿，或重新整理一个文件内的引用。
- 识别 Zotero 引用域、支持的 EndNote/Mendeley 引用域及纯文本引用；不明确的对应关系需要审查。
- 可独立通过只读 Zotero API 建立原生引用或库内条目映射；只有需要导入、核验新文献或整理 collection 时，才选择可选的 `zotero-input` 协作路径。
- 使用一个 CSL 样式统一排序、格式和编号，例如 GB/T 7714—2015 顺序编码。
- 默认交付最终 DOCX，可附一份简短 Markdown 说明；验证后清理临时文件。
- 保留真实作者、编辑者、单位及人工批注，只清理明确的自动生成标记。作者字段中独立出现的 Codex、GPT6/GPT-6 等 agent／模型名也会清理；参考文献作者和 Zotero 数据保留。

触发按语义判断，不限语言。明确要求“合并两篇文献”“合并带参考文献的文本”或“重新整理引用”时直接启用；普通“合并文本”先轻量检查原文，确认含参考文献后再启用。单纯导入文献使用 `zotero-input`。

启动后先询问是否采用已有 collection。未指定目标或 60 秒未答复时默认新建；明确选择已有 collection 后，其身份仍不清楚时会继续确认。原库条目优先复用并加入目标 collection；无法复用时沿用 `zotero-input` 的保底导入与成员验证规则。输出去重不会删除或合并库中的记录。

**上述默认规则保持不变。唯一新增例外：用户明确选择“不使用 collection”时，跳过询问、创建和超时默认流程，直接验证库内条目身份。** 不会因为未安装 Input 就自动切换到无 collection。Output 的独立映射器只有只读能力；需要新增成员或创建 collection 时可使用可选 Input 或已授权的 Zotero 界面操作，缺少相应能力会如实报告。

| 模式 | Zotero Input | Zotero Desktop | 引用性质 |
| --- | --- | --- | --- |
| 独立动态引用 | 不需要 | 需要可访问的本地 API | 原生 Word 引用域，须真实 Refresh 验证 |
| Input 协作 | 可选增强 | 需要 | 沿用已有导入及成员验证规则 |
| 静态 source-text 格式 | 不需要 | 格式化不需要 | 普通数字文本，不可自动刷新 |

## 安装与依赖

将完整的 `zotero-output` 技能目录放入 Codex 的技能目录即可独立安装；`zotero-input` 不是安装前提。默认位置为 `~/.codex/skills/`；使用自定义 `CODEX_HOME` 时放入其 `skills/` 子目录。复制发布文件时排除 `.git/`、缓存和实验文件。

运行环境：

- Python 3.10+；Word 处理依赖 `lxml`、`python-docx` 和 `docxcompose`。
- 动态映射需要 Zotero Desktop 正在运行并开启本地 API；可选导入协作才需要 Connector。静态格式化本身不读取文献库。
- 最终动态引用验证需要 Microsoft Word 及 Zotero 的 Word 插件。
- PDF 提取可选用 PyMuPDF 或 `pdftotext`；扫描件需要另行完成 OCR。
- Markdown 引用源的排版需要 Pandoc/citeproc；它本身不具备 Word 的实时引用集成。

在技能目录安装依赖并检查环境：

```text
python -m pip install -r requirements.txt
python -X utf8 -B scripts/zotero_output.py doctor
```

如需 PyMuPDF 提取 PDF 文本，可另行安装 `PyMuPDF`。只读映射不要求 Zotero 云端账号或第三方桌面扩展；可选导入和 collection 成员变更能否执行，取决于 Input 或可用界面的写入能力。本版本没有新增外部依赖。

## 使用示例

可以直接向智能体提出请求，并提供源文件：

> 用 zotero-output 按文件1、文件2、文件3的顺序合并，输出 Word，引用统一为 GB/T 7714—2015 顺序编码格式。

> 这个文件中两段话的参考文献编号是乱的，请重新整理，使用我已有的“项目文献”collection。

> Merge these two manuscripts and rebuild their references as live Zotero citations in Word.

> 不使用 collection，也不调用 Zotero Input。直接复用我库里的条目，合并这些已有 Zotero 引用的文档。

> 只输出普通编号的 DOCX，不需要 Zotero 动态引用，也不使用 collection。

动态处理流程为：扫描与审查 → 独立只读映射，或可选 Input 映射 → 审查引用计划 → 从 Zotero 读取条目 → 生成工作副本 → Word 刷新与验证 → 精简交付。中间 JSON、预览及工作副本放在临时工作目录。无法可靠确定身份时会保留未解决列表，不会伪造引用域或把超链接当作动态引用。

独立映射仍复用现有 plan、items、build、verify 命令：

```text
python -X utf8 -B scripts/zotero_output.py resolve --scan scan.json --no-collection --out mapping.json
python -X utf8 -B scripts/zotero_output.py plan --scan scan.json --mapping mapping.json --style vancouver --out plan.json
```

`--no-collection` 只用于明确的无 collection 选择；否则使用已解析的 `--collection KEY`。计划仍需按工作流审查，再进入 items/build/verify。缺少库内文献时，可选择 Input 协作，或先在 Zotero 中人工导入后重试；不会暗中新建中转 collection。

明确选择普通格式化时可运行 `plan --scan scan.json --static --style source-text --out plan.json`，审查后用相同 build/verify 命令，无需 `--items`。DOCX 和 MD 都是不可刷新的普通编号文本。该基本模式保留来源文献文本／内嵌元数据，按首次引用编号，**不冒充 GB/T、APA 等 CSL 样式**；其他静态样式需要可用的排版引擎。

底层命令供调试或集成使用：

```text
python -X utf8 -B scripts/zotero_output.py --help
```

逐步操作见 [workflow.md](references/workflow.md)，数据结构与调用接口见 [interfaces.md](references/interfaces.md)。低层生成命令会保留验证用的 sidecar；skill 完成验收后才清理。最终 DOCX 的原生引用与偏好保存在文档内部，不依赖这些外部 JSON。

Word 保存后会再次检查文档属性，定向清理生成标记，保留真实编辑者；以后也可显式执行：

```text
python -X utf8 -B scripts/zotero_output.py clean-metadata --document merged.docx
```

此命令只清理可明确识别的工具标记，重复执行不会继续损失正常身份信息。未知姓名、真实单位、学术披露和人工批注／修订均保留；普通正文中的工具名不会被扫描删除。单源 DOCX 继承其真实作者和编辑者；多源作者按来源顺序去重合并，最后编辑者不一致时留空而不猜测。生成标记不会写入附带的 Markdown 说明。

## 验证与边界

运行隔离测试：

```text
python -X utf8 -B -m unittest discover -s scripts -p "test_*.py" -v
```

结构验证会核对引用域、条目绑定、正文及引用位置，但不能代替真实 Word 刷新和文献库链接检查。历史环境中的实际 Word 刷新、增删引用后重新编号及测试覆盖记录见 [verification.md](references/verification.md)；字段约定见 [word-fields.md](references/word-fields.md)。

已知边界：

- PDF 输出是可编辑重建，不能保证原版式与图像无损恢复。
- 未支持的外来引用域、歧义作者年份引用、修订、文本框等需要修复或替代流程。
- 多篇富文本 DOCX 的差异页眉页脚、批注、嵌入对象及尾注等可能被合并器拒绝，不能为完成合并而丢弃内容。
- 零正文引用的动态参考文献表尚未完成实际 Word 验证。
- 手动输入编号、编辑文末文字或改变 collection 成员，不会自动成为 Zotero 管理的正文引用。

## 仓库结构与版本

```text
SKILL.md           技能规则；metadata.version 为 0.1.1
README.md          项目说明
agents/            技能界面配置
scripts/           扫描、映射、生成、验证与隔离测试
references/        操作流程、接口和验证记录
requirements.txt   Word 处理依赖
```

本地发布版本标签为 **v0.1.1**。新版的真实 Word 刷新、跨库集成和交互式 60 秒计时验证与隔离测试分开记录，详见 verification.md。
