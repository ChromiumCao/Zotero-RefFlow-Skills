# Zotero-RefFlow-Skills

两个可独立安装、运行和维护的 Zotero 工作流技能，放在同一个项目中管理。

| Skill | 当前版本 | 主要用途 | 单独下载 |
| --- | --- | --- | --- |
| [Zotero Input](zotero-input/README.md) | v0.1.0 | 文献识别、检索核验、导入、引用补充及核查 | [zotero-input-v0.1.0.zip](packages/zotero-input-v0.1.0.zip) |
| [Zotero Output](zotero-output/README.md) | v0.1.1 | 文稿合并、引用映射与排序、动态或静态文档生成 | [zotero-output-v0.1.1.zip](packages/zotero-output-v0.1.1.zip) |

在 GitHub 中打开 ZIP 文件后，点击 **Download raw file** 下载；私有仓库需要先登录有访问权限的账号。两个 ZIP 均包含完整的技能目录，可只下载、安装其中一个。

## 目录与独立性

```text
Zotero-RefFlow-Skills/
├── README.md
├── versions.json
├── zotero-input/        # Input 的完整源码、说明与测试
├── zotero-output/       # Output 的完整源码、说明与测试
├── packages/           # 各 skill 的独立下载包
└── tools/
    └── package_skills.py
```

- 每个 skill 的运行资源都在自己的目录中，没有共用的运行时代码、子模块或指向另一台电脑的链接。
- Input 不依赖 Output；Output 可独立做库内只读映射和明确选择的静态格式化，Input 只是其可选导入增强路径。
- 可只修改其中一个目录，另一个 skill 的版本不必一起变更。一个 Git 仓库统一保存历史，修改记录仍可按目录分别查看和提交。
- 独立安装不等于没有环境依赖。动态 Zotero 操作、Word Refresh、PDF 读取等环境要求以各 skill 的说明为准。

本次初始源码分别导出自 Input 的 `v0.1.0`、Output 的 `v0.1.1` 标签。原始提交及文件数量记录在 [versions.json](versions.json)。子目录中的说明可能包含旧机器的历史部署描述；新机器应安装完整目录，不依赖旧机器的 Junction 或本地路径。

## 在另一台计算机上测试

只想使用某一个 skill：登录 GitHub，下载表格中对应的独立 ZIP，解压，将完整的 `zotero-input` 或 `zotero-output` 目录安装到所用客户端的技能目录。不要只复制 SKILL.md 或单个脚本；Input 的 `scripts/_vendor` 也需要保留。

需要测试、修改并同步回 GitHub：建议用 GitHub Desktop 登录同一账号，选择 **File → Clone repository**，克隆本仓库。之后修改所需的 skill 目录，通过提交和推送保存改动。在另一台电脑开始修改前先拉取最新版本，避免两台电脑互相覆盖。

Output 的依赖和环境检查：

```text
python -m pip install -r zotero-output/requirements.txt
python -X utf8 -B zotero-output/scripts/zotero_output.py doctor
```

Input 的主要脚本使用 Python 标准库；可先查看命令帮助：

```text
python -X utf8 -B zotero-input/scripts/zotero.py --help
```

两个 skill 的隔离测试可分开运行：

```text
python -X utf8 -B -m unittest discover -s zotero-input/scripts -p "test_*.py" -v
python -X utf8 -B -m unittest discover -s zotero-output/scripts -p "test_*.py" -v
```

若执行环境的默认临时目录不可写，可将该测试进程的 TEMP/TMP 指向一个可写临时目录。隔离测试不代表实际 Word、Zotero、界面选择或计时集成均已通过；各 skill 的 README 和 references 文档保留了具体边界。

## 分别修改、版本与打包

例如，只升级 Output 时，修改 `zotero-output/` 中的代码、测试和说明，更新其 SKILL.md 的版本及 README；Input 的内容和版本保持不变。同步更新 `versions.json` 对应条目的 `version`，再生成 Output 下载包：

```text
python tools/package_skills.py --skill zotero-output
```

只打包 Input：

```text
python tools/package_skills.py --skill zotero-input
```

Input 的版本标记在其 VERSION 文件中。脚本会核对各 skill 自己的版本与 versions.json 一致，然后更新 packages 中相应的 ZIP；不会因为修改了一个 skill 而要求重打另一个。历史版本包不会自动删除；旧包与新版包的保留由维护者决定。修改并打包后，也应更新本 README 的当前版本与下载链接。

`origin_tag`、`origin_commit` 和 `snapshot_files` 只记录导入这个组合项目时的原始快照，不表示后续修改后的当前 Git 提交。新仓库的后续版本建议用 `zotero-input-v0.1.0`、`zotero-output-v0.1.1` 这类带 skill 名称的标签分别管理。

## 首次网页上传

1. 新建 GitHub 仓库，名称为 `Zotero-RefFlow-Skills`，可见性选择 **Private**。创建页面的 README、.gitignore 和 License 暂不自动添加，本目录已备好项目说明和忽略规则。
2. 创建成功后，点击 **uploading an existing file**；若已显示文件列表，选择 **Add file → Upload files**。
3. 打开本地 `Zotero-RefFlow-Skills` 目录，将其中的文件与子目录拖入上传区域。应让 README.md、zotero-input、zotero-output 等直接位于仓库根目录，不多套一层同名项目目录。
4. 等待上传完成，填写 `Initial import: Input v0.1.0 and Output v0.1.1`，在自己的空仓库中提交到 main。按钮名称可能显示为 Commit changes；若界面要求分支或 PR，则按界面完成对应提交。
5. 检查仓库名称旁显示 **Private**，源码目录和两个独立 ZIP 均可打开。

这里应上传解压后的项目内容，而不是只上传整个项目的大 ZIP。仓库只包含技能代码、测试、说明及版本包，不包含 Zotero 数据库、实验文档或运行缓存。本准备目录不含原来两个仓库的 Git 历史；原仓库仍在原位置保留。

官方操作说明：[创建仓库](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)、[上传文件](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)、[GitHub Desktop 克隆](https://docs.github.com/en/desktop/adding-and-cloning-repositories/cloning-and-forking-repositories-from-github-desktop)。
