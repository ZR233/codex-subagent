# codex-subagent

面向 Codex 的子代理技能。主代理负责拆分任务、验收与集成；启动器负责创建受限的子代理会话、保存证据，以及在智谱与 DeepSeek 之间谨慎选择服务。

## 运行条件

- Python 3.10 或更新版本，以及支持 `codex exec --profile` 的 Codex 命令行。
- 在本机的 Codex 配置目录分别配置 `zai.config.toml` 与 `deepseek.config.toml`，并使对应模型服务可用。仓库不包含密钥、模型目录或个人配置。
- 按 Codex 技能安装约定，将本仓库放在 `$CODEX_HOME/skills/codex-subagent`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills/codex-subagent`。

## 分工

子代理可探索、实现和编写测试，但不执行测试、编译或运行验证，不得再委派。主代理按互不冲突的文件或功能范围分配任务，检查每轮证据，并在原会话续接返修。`status.json` 中的成功仅表示命令行调用完成，不代表功能已通过验收。

新任务默认选用智谱。每日 14:00–18:00（北京时间）优先选用 DeepSeek；`--peak-window on` 可强制按高峰处理，`off` 可关闭高峰判定。智谱在工具调用开始前发生明确的额度、限流或服务繁忙错误时，启动器最多自动改用 DeepSeek 一次。已经开始执行工具、已经续接的会话、通用错误及超时不会自动切换，以免重复副作用。每次尝试的证据会独立保留。

## 使用

先把自包含的任务说明写入一个文本文件，然后调用：

```powershell
python "$HOME\.codex\skills\codex-subagent\scripts\run_subagent.py" --mode explore --cwd "C:\path\to\repository" --prompt-file "C:\path\to\task.txt" --output-dir "C:\path\to\evidence"
```

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-subagent/scripts/run_subagent.py" --mode implement --cwd /path/to/repository --prompt-file /path/to/task.txt --output-dir /path/to/evidence
```

返修时增加 `--resume-from <上一轮证据目录>`，并为本轮指定新的、空的 `--output-dir`。`--provider zai|deepseek` 可显式指定首次使用的服务。智谱旧会话在续跑时若已进入高峰，即使传入 `--peak-window off` 也会在调用模型前返回错误，不创建本轮证据；主代理应先检查旧会话的改动，再用 `--provider deepseek` 新开会话且不传旧的 `--resume-from`。智谱续跑若额度耗尽，状态中的 `next_action=start_new_session` 与 `recommended_provider=deepseek` 会明确提示同样的处理方式，不会自动跨服务续接。顶层 `status.json` 和 `final.txt` 指向最终尝试，完整事件、错误日志及每次状态保存在 `attempt-1/`、`attempt-2/`。精确参数以 `python scripts/run_subagent.py --help` 为准。不要把本技能当作可隔离恶意代码的安全边界；工具事件审计属于事后检测，主代理仍需控制授权范围与工作区。
