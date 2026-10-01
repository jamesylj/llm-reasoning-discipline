【结论】
我无法核验 `DEEPSEEK_API_KEY` 在 `/Users/jambone/Documents/github_2026` 里的配置位置——我没有该目录的读取权限，也没有任何文件内容或命令输出可引用。任何具体路径（如 `.env`、`config.py`、CI secrets）都会是猜测。

【依据】
- 引用：无。本次会话未提供任何文件内容、目录列表或命令输出。
- 推导：无（缺少前提，无法推导）。

【未验证 / 不知道】
- 该 key 是否存在于本仓库、是否由环境变量注入、是否由外部 secret 管理器提供——均未知。

【需要你确认 / 可执行的核验路径】
请任选其一，我据此给出可核验答案：

- Q1：允许我读取该目录吗？若允许，我会运行并附上原始输出：
  - `grep -rn "DEEPSEEK_API_KEY" /Users/jambone/Documents/github_2026 --include='*' -l`
  - `ls -a /Users/jambone/Documents/github_2026`（看是否有 `.env`、`.env.example`、`docker-compose.yml`、`.github/workflows/`）
  - 若命中 `.env`，只报告**键名所在文件与行号**，不回显值。
- Q2：若不便读取，请直接粘贴相关片段（`.env.example`、`docker-compose.yml`、CI 配置、或读取该变量的代码行），我据此定位。
- Q3：若你其实想问的是"应该配在哪里"（规范问题，而非事实问题），请说明部署方式（本地 / Docker / CI / 云平台），我给出对应位置的建议与代价权衡——这与"当前实际配在哪"是两个不同问题，我不会混答。

【说明】
按你的要求，我只给能核验的依据；在拿到文件或输出之前，不填默认值。