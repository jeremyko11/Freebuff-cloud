# 贡献指南

感谢关注本项目！

## 开发流程

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/你的特性`
3. 提交更改：`git commit -m "feat: 简要描述"`
4. 推送分支：`git push origin feature/你的特性`
5. 发起 [Pull Request](https://github.com/jeremyko11/Freebuff-cloud/pulls)

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

| 前缀 | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修复缺陷 |
| `docs` | 文档变更 |
| `refactor` | 重构（无功能变化） |
| `test` | 测试相关 |
| `chore` | 构建/工具链 |

## 代码要求

- 新代码需附带测试（`tests/`）
- 保持风格一致（`.editorconfig`）
- 不要提交密钥、凭据或大型生成文件（见 `.gitignore`）

## 报告问题

提 Issue 时请包含：环境信息、复现步骤、期望与实际结果。
