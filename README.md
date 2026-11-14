# session-baton

Cross-harness relay: pack handoff batons from Claude/Codex/Pi logs + git, then phase-fold review the work.

一个会话额度用完，另一端（Codex、Antigravity、另一个 Claude）接手；额度重置后，原会话回来审接手方干了什么。`baton` 管这件事的两头：

- `baton pack`：从当前项目最近一次会话记录和 git 状态，生成一份交接包（markdown + json）。接手端读完就知道目标、边界、文件范围、未完成事项和当前 git 锚点。
- `baton review`：拿交接包和接手方的会话记录做比对，把连续命令按阶段折叠成时间线，并标出范围外的改动、可能触碰边界的命令、没处理的待办，以及改了代码却没跑测试的情况。

## 安装

```bash
pip install -e .          # 得到 baton 命令
# 或不安装：python3 -m session_baton ...
```

依赖 Python 3.9+ 和 `rich`（没装 rich 会退回纯文本输出）。

## 用法

```bash
cd your-project

baton list                     # 当前目录对应的会话记录，新的在前
baton pack                     # 用最近一次会话打交接包，写入 .baton/
baton pack --scope tests/ --scope "docs/*.md"   # 额外放行的文件范围
baton pack --stdout | pbcopy   # 直接复制 markdown，粘给接手端

# ……接手端干完活之后……
baton review                   # 自动找交接之后同目录的新会话
baton review --receiver path/to/rollout.jsonl --expand   # 指定会话，展开全部命令
baton review --json            # 机器可读
```

`review` 发现范围外改动或触碰边界时退出码为 1，可以挂进脚本里。

## 交接包里有什么

| 字段 | 来源 |
| --- | --- |
| 目标 | 原会话第一条真实用户消息（系统注入的 reminder、环境上下文会被过滤） |
| 后续指令 | 之后的用户消息，保留最近 6 条 |
| 边界 | 用户原话里的作用域限定（如「我说的最高权限是类似并发这种的，但是托管还是和其他账号一样」）和禁令（「不要改 billing.py」「never touch prod」） |
| 文件范围 | 原会话实际写过的文件，加上 `--scope` 追加的范围 |
| 待办 | 原会话最后几段回复里的 `- [ ]`、「下一步」「还差」等 |
| git 锚点 | HEAD、分支、未提交文件，回审时据此计算 diff |

## 回审怎么判

- **阶段折叠**：接手方每说一句话，就开始一个新阶段；工作类型变了（摸底、改代码、跑测试、提交推送）也会切开。同类调用合并，超过 3 步的折叠成「另 N 步」。前面没有说明的阶段会标黄。
- **范围外改动**：git 自锚点以来的 diff、未跟踪文件、接手方工具调用里写过的文件，三者合起来，减去交接包里的范围。
- **边界**：禁令里出现的文件名或标识符，如果出现在接手方的命令或改动文件里就会标出来。另外内置一组高危命令：强推、`reset --hard`、`rm -rf`、整体丢弃工作区、全盘扫描主目录、`curl | sh`。作用域类边界（「X 指的是……但是……一样」）没法靠机器判定，会单独列出来让你人工确认。
- **待办**：接手方的回复里没提到的待办算未处理。

## 会话从哪里找

只扫这三个目录，有深度上限、文件数上限，默认 5 秒超时。不会遍历整个主目录。

| 端 | 目录 | 匹配当前项目的方式 |
| --- | --- | --- |
| Claude Code | `~/.claude/projects/<路径编码>/*.jsonl` | 目录名 |
| Codex CLI | `~/.codex/sessions/YYYY/MM/DD/*.jsonl` | 首行 `session_meta.cwd` |
| Pi | `~/.pi/agent/sessions/<路径编码>/*.jsonl` | 目录名 |

设置 `BATON_HOME` 可以改根目录（测试就是这么做的）。Antigravity 这类没有本地 jsonl 的端，可以用它产出的 git 提交做回审，或者用 `--receiver` 指定导出的记录。

## 开发

```bash
python3 -m pytest -q
```

测试里所有会话样本都是虚构的，跑在临时 git 仓库里。
