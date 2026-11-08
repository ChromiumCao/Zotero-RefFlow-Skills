# Zotero-RefFlow-Skills

两项面向 Zotero 文献管理和论文写作的 Codex 技能。**Input 把文献整理进 Zotero，Output 把文稿中的引用整理成统一格式。** 两项技能可以分别下载、安装、运行和更新。Output 需要补录库中缺失的条目时，可以搭配 Input。

| 技能 | 适合什么任务 | 独立安装包 |
| --- | --- | --- |
| [Zotero Input](zotero-input/README.md) · v0.1.0 | 从文档或表格识别参考文献，核验并导入指定 Zotero 分类（collection）。也可为段落查找文献，或核查现有引文是否得到原文支持。 | [Input v0.1.0 ZIP](packages/zotero-input-v0.1.0.zip) |
| [Zotero Output](zotero-output/README.md) · v0.1.1 | 合并带参考文献的文稿，或整理单篇文稿的引文。核对引用位置、去重并统一样式，生成可编辑的 Word 文档或 Markdown。 | [Output v0.1.1 ZIP](packages/zotero-output-v0.1.1.zip) |

## 安装

1. 打开上表中所需的 ZIP，在 GitHub 文件页下载原文件并解压。
2. 将解压出的完整 `zotero-input` 或 `zotero-output` 目录放进 Codex 技能目录。默认位置为 `~/.codex/skills/`，自定义位置为 `$CODEX_HOME/skills/`。
3. 准备 Python 3.10 或更高版本。Input 的主要脚本使用 Python 标准库。使用 Output 时，进入 `zotero-output/` 安装依赖并检查环境。

```sh
python -m pip install -r requirements.txt
python -X utf8 -B scripts/zotero_output.py doctor
```

Input 导入文献需要运行中的 Zotero Desktop、已启用的本地 API 和 Connector 服务。Output 生成动态引用时通过本地 API 读取文献库，并使用 Microsoft Word 和 Zotero 的 Word 插件完成刷新验证。Output 的静态编号模式可脱离 Zotero 运行。PDF 文本提取和扫描件 OCR 的要求见各技能文档。

## 使用示例

### Zotero Input

> 用 zotero-input 读取附件里的参考文献，核验后导入 Zotero 的「项目文献」分类。优先复用库中已有的条目。

Input 会检查条目是否实际进入目标分类。引用证据核查可以独立于导入执行。详细用法见 [Zotero Input 文档](zotero-input/README.md)。

### Zotero Output

> 用 zotero-output 按附件顺序合并这两篇文稿。引用统一为 GB/T 7714—2015 顺序编码，生成可以用 Zotero 刷新的 Word 文档。不使用 collection，直接复用我库里已有的条目。

动态 DOCX 使用 Zotero 原生引用域，可在 Word 中通过 Zotero 插件添加、编辑和刷新引文。无法可靠匹配到库内条目的引用会列为待解决项。只需普通编号时，可明确要求静态 DOCX 或 Markdown，后续编号由用户维护。详细用法和验证范围见 [Zotero Output 文档](zotero-output/README.md)。

## 开发与打包

两项技能的代码、说明和测试分别位于 [zotero-input/](zotero-input/) 和 [zotero-output/](zotero-output/)。[versions.json](versions.json) 记录各自的版本和最初导入的 Git 快照。公开分发包在这些快照上补充了许可证、署名和发布文档。更新某项技能时，同步调整该技能的版本与下载链接，再从仓库根目录单独打包。

```sh
python tools/package_skills.py --skill zotero-input
python tools/package_skills.py --skill zotero-output
```

从仓库根目录运行相应的隔离测试。真实 Word 刷新和 Zotero 集成需在相应桌面环境中验证。

```sh
python -X utf8 -B -m unittest discover -s zotero-input/scripts -p "test_*.py" -v
python -X utf8 -B -m unittest discover -s zotero-output/scripts -p "test_*.py" -v
```

## 代码来源与许可

本项目采用 [Apache License, Version 2.0](LICENSE)。两项独立 ZIP 各自包含许可证副本。

感谢 [Yuan1z0825](https://github.com/Yuan1z0825) 和 [nature-skills](https://github.com/Yuan1z0825/nature-skills) 的贡献者。Zotero Input 内置的文献检索与导出脚本改编自该项目的 `nature-citation`，上游同样采用 Apache-2.0。来源提交、修改说明和署名随 Input 一同分发。参见 [NOTICE](NOTICE) 和 [Input 上游来源记录](zotero-input/references/bundled-search.md)。
