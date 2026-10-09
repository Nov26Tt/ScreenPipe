# 贡献指南

感谢你愿意参与。这个项目不大，希望改动保持聚焦。

## 开发环境

```bash
git clone https://github.com/Nov26Tt/ScreenPipe.git
cd ScreenPipe

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

pip install -e ".[dev]"
pytest
```

## 提交前请确认

- [ ] `pytest` 全部通过
- [ ] 新增逻辑有对应测试
- [ ] **没有硬编码任何 API Key**（CI 会拦截）
- [ ] 涉及配置默认值时，`config.py` 的 `DEFAULTS` 与 `config.example.yaml` **两处同步改**
- [ ] 单文件不超过 300 行

## 代码风格

- 注释解释**为什么**，不解释**做了什么**
- 不引入新的运行时依赖，除非它显著降低使用门槛
- 前端是单文件 HTML，不引入构建步骤

## 提交信息

用祈使句写清做了什么，一行以内：

```text
新增有界历史淘汰，避免长时间运行内存增长
修复掩码回显会覆盖真密钥的问题
```

## 认领任务

先在 Issues 里留言认领，避免重复劳动。

## 换模型名时请务必核实

模型名随厂商快速迭代。**提交模型名变更前请到厂商官方文档确认**，
并同步更新 README 的「选择视觉模型」表格——一个写错的默认模型名
会让所有新用户开箱即错。
