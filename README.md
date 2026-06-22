# CodePilot

> 轻量级终端编码助手 — 基于 GenericAgent 架构，融合 MiniCode 设计理念

## 特性

- **Agent循环恢复**: 自动处理空响应/截断/限速，保障对话稳定性
- **AutoCompact**: 长对话自动压缩上下文，突破token窗口限制
- **权限系统**: 路径/命令/编辑三级权限控制，安全执行代码
- **命令白名单**: 读写命令分类，危险命令需确认
- **Session JSONL**: 完整会话持久化，支持历史回溯
- **文件工具**: `grep_files`(rg优先) + `list_files`(树形遍历)

## 快速开始

```bash
# 安装
pip install -r requirements.txt

# 配置API密钥 (复制模板后填入)
cp mykey_template.py mykey.py

# 运行
python main.py
```

## 架构

```
CodePilot/
├── main.py              # CLI入口
├── agentmain.py         # Agent生命周期管理
├── agent_loop.py        # 核心循环 (LLM调用+工具分发+恢复)
├── ga.py                # 工具Handler (grep/list/files/shell)
├── llmcore.py           # LLM客户端抽象层
├── compact.py           # 上下文压缩引擎
├── permissions.py       # 权限管理器
├── session_manager.py   # Session JSONL持久化
├── simphtml.py          # HTML解析工具
├── plugins/             # 插件系统 (hooks/tracing)
└── assets/              # 系统提示词+工具schema
```

## 技术栈

- Python 3.10+
- 多LLM后端支持 (OpenAI/Claude/本地模型)
- ripgrep (可选, 用于高速代码搜索)
- JSONL会话持久化

## License

MIT
