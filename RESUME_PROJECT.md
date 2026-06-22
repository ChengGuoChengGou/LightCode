# CodePilot 简历项目经历

---

## 项目简介

**CodePilot** — 轻量级终端编码助手，基于自主开发的 Agent 框架，融合多款主流 Coding Agent（Claude Code / MiniCode / Cursor）的设计理念，实现了完整的工具调用循环、上下文压缩、权限控制和会话持久化能力。

---

## 技术栈

- **语言**: Python 3.10+
- **LLM 抽象层**: 多后端支持（OpenAI / Claude / 本地模型），统一 API 封装
- **Agent 循环**: 自研 agent_loop，支持空响应重试、截断恢复、限速退避、工具分发
- **上下文管理**: AutoCompact 引擎，自动检测 token 超限并触发智能压缩（摘要+关键信息保留）
- **权限系统**: 路径/命令/编辑三级权限模型，支持持久化授权和会话级临时授权
- **会话持久化**: JSONL 格式记录完整对话树（uuid/parentUuid），支持历史回溯和标题提取
- **工具系统**: 可扩展工具 schema（JSON Schema），内置代码执行、文件操作、代码搜索（grep/list）
- **插件架构**: hooks + tracing 插件系统，支持 Langfuse 可观测性接入
- **CLI**: 交互式终端界面，支持命令行参数（--task/--model/--reflect）

---

## 技术亮点

### 1. Agent 循环恢复机制
自研的 `recovery.py` 模块，在 Agent 循环中自动处理三类异常：
- **空响应重试**: LLM 返回空内容时自动重试，最多 3 次
- **截断恢复**: 检测 JSON 工具调用被截断时，自动拼接恢复
- **限速退避**: 检测 429/rate-limit 错误时指数退避，保障长任务稳定性

### 2. 智能上下文压缩
借鉴 MiniCode 的 compact 设计，实现了两级压缩策略：
- **边界标记**: 在对话中插入 `compact_boundary`/`snip_boundary` 标记，标记压缩位置
- **自动触发**: 当 token 总量超过窗口 80% 时自动触发压缩，保留系统提示+关键上下文+最近对话
- **可逆恢复**: snip 模式保留被移除消息的 ID，支持按需恢复原始内容

### 3. 三级权限模型
参考 Claude Code 的权限设计，实现了细粒度的安全控制：
- **路径权限**: 文件读写操作前校验目标路径是否在允许范围内
- **命令权限**: 代码执行前分类命令为 READONLY/DEVELOPMENT/DANGEROUS，危险命令需确认
- **编辑权限**: 文件修改前生成 unified diff 并可选触发用户审批

### 4. 会话树形历史
采用 JSONL 格式实现完整会话持久化：
- 每条记录包含 uuid/parentUuid，支持树形对话历史
- 事件类型覆盖：user/assistant/tool_call/tool_result/compact_boundary/snip_boundary
- 标题自动提取：优先使用 rename 事件，fallback 到首条用户消息

---

## 项目结构

```
codepilot/
├── main.py              # CLI 入口
├── agentmain.py         # Agent 生命周期管理
├── agent_loop.py        # 核心循环引擎（恢复+分发+压缩）
├── ga.py                # 工具 Handler（命令白名单+grep+list）
├── llmcore.py           # LLM 客户端抽象层
├── compact.py           # 上下文压缩引擎
├── permissions.py       # 权限管理器
├── session_manager.py   # Session JSONL 持久化
├── simphtml.py          # HTML 解析工具
└── plugins/             # 插件系统（hooks/tracing）
```

---

## 面试话术参考

**Q: 这个项目解决什么问题？**
> 现有 Agent 框架在长对话场景下容易出现上下文溢出、工具调用失败、权限失控等问题。CodePilot 通过自研的循环恢复机制和智能压缩引擎，保障了 Agent 在复杂编码任务中的稳定性和安全性。

**Q: 技术难点是什么？**
> 主要有三个：1）如何在 token 窗口受限的情况下保留关键上下文，我们设计了两级压缩策略；2）如何处理 LLM 的不稳定输出（空响应、截断、限速），我们实现了自动恢复机制；3）如何在不牺牲灵活性的前提下保证安全性，我们设计了三级权限模型。

**Q: 和 Claude Code / MiniCode 的区别？**
> 我们参考了它们的设计理念，但做了差异化：1）上下文压缩支持可逆恢复，不是简单丢弃；2）权限模型支持会话级临时授权，更灵活；3）会话历史用树形结构存储，支持分支对话回溯。
