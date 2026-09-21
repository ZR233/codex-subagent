#!/usr/bin/env python3
"""子代理启动器（主代理侧使用）。

主代理把自包含任务交给子代理工人，底层固定调用：
  codex exec --profile <zai|deepseek> --disable multi_agent --disable multi_agent_v2
    -c agents.enabled=false
    --json --color never --skip-git-repo-check
    --sandbox [explore:read-only|implement:workspace-write] -C <cwd>
    --output-last-message <out>/attempt-N/final.txt -
两个 --disable 之外还按每次调用传入 `-c agents.enabled=false`：配置里
features.MultiAgentV2=false 与 agents.enabled=false 同时成立才会强制
MultiAgentVersion::Disabled，只关一项时模型元数据仍可能启用 V2；因此不依赖全局配置。
用法: python3 run_subagent.py --mode explore|implement --cwd DIR --prompt-file FILE \
      --output-dir DIR [--timeout SECONDS] [--resume-from 上一轮证据目录] \
      [--provider zai|deepseek] [--peak-window auto|on|off]
默认路由：官方每日峰值 14:00-18:00（UTC+8）内用 deepseek，其余时段优先 zai；
--provider 显式覆盖提供方，--peak-window on/off 强制或禁用峰值判定。
ZAI 在尚未开始任何工具调用/命令之前遇到明确的提供方配额耗尽、限流或服务过载时，
只用 DeepSeek 重试一次（仅限新任务）；两次尝试的证据分别保存在 attempt-1/attempt-2，
顶层 status.json 描述最终选中的尝试，顶层 final.txt 为选中尝试的最终答复。
新任务会保存会话；续跑用 --resume-from 指向含 status.json 的上一轮证据目录，
并以其中的实际 thread_id 与提供方执行 resume；旧 status.json 缺少 provider 时按
deepseek 处理。
子代理工人自身（CODEX_SUBAGENT_WORKER=1 或旧的 DS_SUBAGENT_WORKER=1）会在创建任何
子进程前被直接拒绝。安全边界是「能力关闭 + 行为规则 + 事件审计」，不是完全隔离或
预执行拦截；审计只解析真实 command_execution 与原生 collab 事件，异常/未知 shell
形式可能漏报，这是明确限制。

注入的任务规则按并行协作分工：子代理工人负责自包含任务的探索与编码，只可对改动文件
做简单 fmt；全部编译、lint、测试与运行验证（含启动器开发时的自测）由主代理执行。
主代理按目录/文件/功能给每个工人分配互不冲突的所有权并决定实际并发数（最多 30，
不设默认值），脚本本身不内置调度器或并发度参数。提供方凭据与本地模型配置不进仓库，
只通过本机 `codex exec --profile zai|deepseek` 的既有配置生效。
"""
from __future__ import annotations

import argparse, datetime, getpass, hashlib, json, math, ntpath, os, re, shlex, signal
import subprocess, sys
import tempfile
import threading, time, uuid
from pathlib import Path

if os.name == 'nt':
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl

WORKER_ENV_FLAG = "CODEX_SUBAGENT_WORKER"
LEGACY_WORKER_ENV_FLAG = "DS_SUBAGENT_WORKER"
# 同时识别新旧的工人标记：旧标记用于兼容尚未更新的技能文档与旧启动器注入的环境
WORKER_ENV_FLAGS = (WORKER_ENV_FLAG, LEGACY_WORKER_ENV_FLAG)
WORKER_ENV_VALUE = "1"
DEFAULT_TIMEOUT = 900.0
KILL_GRACE_SECONDS = 3.0
POLL_INTERVAL = 0.2
PROGRESS_EVERY = 20
STDERR_TAIL_LINES = 60
DELEGATION_REASON = "delegation_violation"
PROVIDER_FAILURE_REASON = "provider_error"
COPY_FINAL_REASON = "evidence_write_failed"
LOCK_NAME = ".run_subagent.lock"
SESSION_LOCK_PREFIX = "run_subagent-session-locks"
ATTEMPT_DIR_TEMPLATE = "attempt-{index}"

# 提供方路由：非峰值优先 zai，官方每日峰值 14:00-18:00（UTC+8）用 deepseek
PROVIDER_ZAI = "zai"
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_CHOICES = (PROVIDER_ZAI, PROVIDER_DEEPSEEK)
DEFAULT_PROVIDER = PROVIDER_ZAI
PEAK_PROVIDER = PROVIDER_DEEPSEEK
FALLBACK_PROVIDER = PROVIDER_DEEPSEEK
PEAK_WINDOW_MODES = ("auto", "on", "off")
PEAK_UTC_OFFSET_HOURS = 8
PEAK_START_HOUR = 14
PEAK_END_HOUR = 18
PROVIDER_SCAN_BYTES = 256 * 1024

RULES_HEADER = (
    "【子代理工人硬性规则】\n"
    "1. 你就是执行者：不派子代理、不调用任何 codex 或其它代理 CLI、"
    "不运行 run_subagent.py（含旧的 run_ds.py）启动器，"
    "不以联调/自测名义间接启动代理；执行者不负责派发其它工人。\n"
    "2. 主代理按目录/文件/功能给每个工人分配互不冲突的所有权：你只改自己范围内的文件，"
    "包括主代理明确分配给你负责的共享文件；不碰其他任务的文件、未分配给你的共享文件；"
    "实际并发数由主代理决定（最多 30，不设默认值）。\n"
    "3. 允许探索与编码：先按项目技能与仓库约定只读探索（证据带 file:line）再改动；"
    "可以读写文件，也可以编写或修改测试代码，但不得运行它。\n"
    "4. 允许编写测试代码、断言和样例程序，仅禁止执行测试、编译、静态检查或运行验证："
    "不编译、不 lint、不跑测试、不 py_compile、不做启动器 self-test；只读探索与代码审查不属于"
    "禁止的验证；只允许在改动文件范围内做简单 fmt。\n"
    "5. 保护 dirty 工作树，不撤销协作者改动，不做未经授权的破坏性操作。\n"
    "6. 未真正执行的验证不得写成通过；不要用超时/重试/弱断言冒充成功。"
    "全部编译、lint、测试与运行验证（含启动器自测）由主代理执行。\n\n"
    "7. 不得以检查环境、模块或工具可用性、烟测、自测为由执行任何程序（含解释器 -m、"
    "单行样例、断言脚本、探测脚本）；环境事实靠读取文件与文档获得，不确定就把缺口交回主代理。"
    "仅为必要文件编辑运行解释器脚本不算测试，但不得借文件编辑夹带导入探测、断言或验证。\n\n"
)
RULES_FOOTER = (
    "【结尾再次提醒】禁止再委派：不得运行任何 codex 或其它代理 CLI、代理启动器、子代理或后台"
    "间接派发；讨论、阅读或生成相关脚本文本可以，但不要实际运行。禁止执行测试、编译、静态检查"
    "或运行验证：不编译、不 lint、"
    "不跑测试、不 py_compile、不启动器 self-test、不做运行验证，只可对所改文件做简单 fmt；"
    "全部验证由主代理执行。也不得以检查环境、模块或工具可用性、烟测、自测为由执行程序："
    "环境事实靠读文件与文档，不确定就交回主代理；仅为必要文件编辑运行解释器脚本不算测试，"
    "但不得借文件编辑夹带导入探测、断言或验证。超出授权或信息不足时把缺口返回主代理。\n"
)
EXPLORE_OUTPUT = ("\n【输出要求 · 探索】结论用 file:line 定位并附关键原文；列出不确定项与未覆盖范围；"
                  "只读探索，不修改任何文件、不做 fmt、不运行任何验证。\n")
IMPLEMENT_OUTPUT = ("\n【输出要求 · 实现】只报告：任务标识、改动文件、行为/接口变化、fmt 命令及结果"
                    "（未做就如实说明未 fmt）、风险与待验收项，并明确写出“未运行测试、编译、"
                    "静态检查或运行验证”。不要输出大段源码或探索过程；改动完成立即返回待主代理"
                    "验收，不宣称功能已完整通过；结果不足时把缺口交回主代理，不要反复打磨或自行"
                    "增加能力。\n")

# 命令监控：只对真实命令做结构化判断
SEPARATORS = {"&", "&&", "|", "||", ";", "(", ")"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "ash"}
WRAPPERS = {"env", "sudo", "doas", "nice", "ionice", "nohup", "setsid", "stdbuf", "time", "command", "exec", "timeout"}
WRAPPER_VALUE_OPTS = {
    "env": {"-u", "--unset", "-S", "-C", "--chdir"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "sudo": {"-u", "--user", "-g", "--group", "-p", "--prompt", "-C", "--close-from",
             "-h", "--host", "-r", "--role", "-t", "--type", "-R", "--chroot"},
    "doas": {"-u", "-C"}, "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"}, "stdbuf": {"-i", "-o", "-e"},
}
PYTHON_PROGRAMS = {"python", "python2", "python3", "pypy", "pypy3",
                   "python3.9", "python3.10", "python3.11", "python3.12", "python3.13"}
# 旧的 run_ds.py 与新的 run_subagent.py 都视为禁止嵌套调用的启动器
LAUNCHER_SCRIPT_NAMES = {"run_ds", "run_ds.py", "run_subagent", "run_subagent.py"}
AGENT_CLIS = {"claude", "claude-code", "gemini", "gemini-cli", "aider", "opencode",
              "qwen", "qwen-code", "goose", "crush", "cursor-agent", "amp"}
COLLAB_ITEM_TYPES = {"spawn_agent", "followup_task", "interrupt_agent", "list_agents", "send_message"}
COLLAB_HINTS = ("collab", "spawn_agent", "spawn-agent", "subagent", "sub-agent", "followup_task",
                "followup-task", "interrupt_agent", "send_message", "list_agents", "multi_agent", "multi-agent")
TOOL_ITEM_TYPES = {"mcp_tool_call", "function_call", "custom_tool_call", "tool_call"}
# 真实工具/命令活动：出现任意一条即认为已开始调用，禁止提供方回退以避免重复副作用。
# 覆盖常见的命令、补丁、检索、协作与 MCP 事件名，未知事件名可能漏判，这是明确限制。
TOOL_ACTIVITY_TYPES = (set(TOOL_ITEM_TYPES) | set(COLLAB_ITEM_TYPES)
                       | {"command_execution", "exec_command", "exec_command_begin",
                          "exec_command_end", "apply_patch", "patch_apply", "patch_apply_begin",
                          "patch_apply_end", "file_change", "file_write", "file_edit",
                          "web_search", "computer_call", "local_shell", "local_shell_call",
                          "image_generation_call", "tool_call_started", "tool_call_completed",
                          "mcp_tool_call_started", "mcp_tool_call_completed",
                          "custom_tool_call_started", "custom_tool_call_completed",
                          "function_call_started", "function_call_completed"})
# 只从真实错误节点取文本；正常答复、命令输出与提示词一律不参与提供方故障匹配
ERROR_NODE_TYPES = {"error", "error_event", "turn.failed"}
ERROR_TEXT_KEYS = ("message", "code", "type", "status", "detail", "reason", "error", "errors",
                   "error_type")
PROVIDER_FAILURE_PATTERNS = (
    ("rate_limit", re.compile(
        r"(?i)rate[ _-]?limit(?:ed|_error)?|too many requests|请求过于频繁|触发限流"
        r"|(?<!\w)(?:429)(?!\w)")),
    ("quota_exhausted", re.compile(
        r"(?i)insufficient[ _-]?(?:quota|balance|credits?)|quota exceeded"
        r"|exceeded your current quota|余额不足|额度不足|配额(?:已)?(?:用尽|耗尽|不足)")),
    ("service_overload", re.compile(
        r"(?i)overloaded(?:_error)?|service unavailable|server busy|bad gateway"
        r"|(?<!\w)(?:502|503|529)(?!\w)|过载|服务繁忙")),
)
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="run_subagent.py",
        description="用子代理工人（codex exec --profile zai|deepseek）执行自包含的探索/实现任务；"
                    "默认非峰值用 zai、峰值（UTC+8 14:00-18:00）用 deepseek。")
    parser.add_argument("--mode", required=True, choices=("explore", "implement"))
    parser.add_argument("--cwd", required=True, help="任务工作目录（必须已存在）")
    parser.add_argument("--prompt-file", required=True, help="自包含任务提示文件")
    parser.add_argument("--output-dir", required=True, help="证据目录：全新或空目录")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"单次尝试超时秒数（默认 {int(DEFAULT_TIMEOUT)}，需为有限正数；"
                             "提供方回退时第二次重新计时）")
    parser.add_argument("--resume-from", default=None,
                        help="上一轮证据目录（含 status.json）；用于续跑已持久化会话，"
                             "提供方与 thread_id 沿用上一轮")
    parser.add_argument("--provider", choices=PROVIDER_CHOICES, default=None,
                        help="显式指定提供方，覆盖峰值时段路由；续跑时必须与上一轮一致")
    parser.add_argument("--peak-window", choices=PEAK_WINDOW_MODES, default="auto",
                        help="峰值窗口判定：auto 按 UTC+8 时钟；on 强制视为峰值（deepseek）；"
                             "off 禁用峰值判定（优先 zai）")
    return parser.parse_args(argv)

def abort(message):
    print(f"run_subagent: {message}", file=sys.stderr)
    return 2

# 提供方路由：纯函数，便于主代理用 --self-test 独立核验时段边界
def peak_window_active(now_utc):
    """返回给定时刻是否落在官方每日峰值 14:00-18:00（UTC+8）内；18:00 整点起算非峰值。"""
    if not isinstance(now_utc, datetime.datetime):
        raise TypeError("now_utc 需为 datetime.datetime")
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=datetime.timezone.utc)
    local = (now_utc.astimezone(datetime.timezone.utc)
             + datetime.timedelta(hours=PEAK_UTC_OFFSET_HOURS))
    return PEAK_START_HOUR <= local.hour < PEAK_END_HOUR

def route_provider(provider_arg, peak_mode, now_utc):
    """返回 (提供方, 峰值判定结果, 路由原因)；显式 --provider 优先于峰值判定。

    峰值判定结果为 None 表示显式指定提供方、未参与判定，其余情况为布尔值。
    """
    if provider_arg:
        return provider_arg, None, f"显式指定 --provider={provider_arg}，跳过峰值判定"
    if peak_mode == "off":
        return DEFAULT_PROVIDER, False, "峰值判定已禁用（--peak-window off），按非峰值优先 zai"
    if peak_mode == "on":
        return PEAK_PROVIDER, True, "强制按峰值处理（--peak-window on），路由到 deepseek"
    if peak_window_active(now_utc):
        return PEAK_PROVIDER, True, "当前处于官方每日峰值 14:00-18:00（UTC+8），路由到 deepseek"
    return DEFAULT_PROVIDER, False, "当前不在官方每日峰值 14:00-18:00（UTC+8），优先 zai"

def current_utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

def acquire_output_dir(raw_path):
    """独占取得输出目录，返回 (Path, None) 或 (None, 错误信息)。

    非空目录/文件/symlink 一律拒绝；空目录允许复用但文件创建走排他模式；不存在时用
    exist_ok=False 的 mkdir 独占创建，避免 symlink 与并发覆盖。
    """
    if raw_path.is_symlink():
        return None, f"拒绝 symlink 输出目录：{raw_path}"
    out = raw_path.resolve()
    if out.exists():
        if not out.is_dir():
            return None, f"--output-dir 已存在且不是目录：{out}"
        try:
            if any(out.iterdir()):
                return None, f"--output-dir 已存在且非空，拒绝覆盖：{out}"
        except OSError as exc:
            return None, f"无法读取输出目录 {out}：{exc}"
        return out, None
    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return None, f"--output-dir 被并发创建，拒绝复用：{out}"
    except OSError as exc:
        return None, f"无法创建输出目录 {out}：{exc}"
    return out, None

def acquire_run_lock(output_dir):
    """在同一输出目录上取得独占运行锁，返回 (锁句柄, None) 或 (None, 错误信息)。

    只允许一个调用在某空目录上落地证据：抢不到锁的一方直接失败返回，不写任何文件，
    避免两个并发调用互相干扰 status.json。已有证据目录因非空会被 acquire_output_dir 拒绝。
    """
    lock_path = output_dir / LOCK_NAME
    try:
        return open(lock_path, "x", encoding="utf-8"), None
    except FileExistsError:
        return None, f"输出目录已被另一运行独占（{LOCK_NAME} 已存在），拒绝写入：{output_dir}"
    except OSError as exc:
        return None, f"无法创建运行锁 {lock_path}：{exc}"

def parse_thread_id(value):
    """返回规范 UUID 字符串；缺失、类型错误或非法 UUID 返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        return None

def session_lock_dir():
    """返回按用户和 CODEX_HOME 隔离的会话锁目录。"""
    uid = os.getuid() if os.name != "nt" else getpass.getuser()
    # 未显式设置时与 Codex 默认目录 ~/.codex 取同一实际路径；
    # 显式值支持 ~ 与相对路径，并解析符号链接，使指向同一实际
    # Codex 目录的不同写法共享锁命名空间。
    raw_home = os.environ.get("CODEX_HOME") or "~/.codex"
    try:
        normalized_home = str(Path(raw_home).expanduser().resolve())
    except (OSError, RuntimeError):
        normalized_home = os.path.abspath(os.path.expanduser(raw_home))
    identity = f"{uid}\0{normalized_home}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    base = Path(tempfile.gettempdir()) if os.name == "nt" else Path("/tmp")
    return base / f"{SESSION_LOCK_PREFIX}-{uid}-{digest}"

def acquire_session_lock(thread_id):
    """按会话 UUID 取得非阻塞独占锁，返回 (锁句柄, None) 或 (None, 错误信息)。"""
    canonical = parse_thread_id(thread_id)
    if canonical is None:
        return None, f"续跑会话 ID 非法：{thread_id!r}"
    lock_dir = session_lock_dir()
    try:
        lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"无法创建会话锁目录 {lock_dir}：{exc}"
    lock_path = lock_dir / f"{canonical}.lock"
    try:
        handle = open(lock_path, "a+b")
    except OSError as exc:
        return None, f"无法打开会话锁 {lock_path}：{exc}"
    try:
        os.set_inheritable(handle.fileno(), False)
        if os.name == "nt":
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"1")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        close_quietly(handle)
        return None, f"同一会话已有续跑在执行，拒绝并发：{canonical}"
    except OSError as exc:
        close_quietly(handle)
        if os.name == "nt" and getattr(exc, "winerror", None) in (32, 33):
            return None, f"same session is already running: {canonical}"
        return None, f"无法获取会话锁 {lock_path}：{exc}"
    return handle, None

def release_session_lock(handle):
    # 保留锁文件：删除会造成不同 inode 上的锁并行，flock 关闭后即不再活动。
    if handle is None:
        return
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    close_quietly(handle)

def load_resume_context(raw_resume_from, mode, cwd):
    """读取并校验上一轮证据，返回 (上下文, None) 或 (None, 错误信息)。

    上下文包含 thread_id、resumed_from 与提供方 provider：续跑必须沿用上一轮提供方，
    且只支持 zai/deepseek 两个提供方，其它取值一律拒绝续跑；旧的 status.json 缺少
    provider 时按 deepseek 处理（旧的唯一提供方）。
    """
    try:
        resume_dir = Path(raw_resume_from).resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"无法解析 --resume-from {raw_resume_from!r}：{exc}"
    if not resume_dir.is_dir():
        return None, f"--resume-from 不是已存在目录：{raw_resume_from}"
    status_path = resume_dir / "status.json"
    if not status_path.is_file():
        return None, f"--resume-from 目录缺少 status.json：{status_path}"
    try:
        previous = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"读取上一轮 status.json 失败 {status_path}：{exc}"
    if not isinstance(previous, dict):
        return None, f"上一轮 status.json 不是 JSON 对象：{status_path}"
    if previous.get("session_persisted") is not True:
        return None, f"上一轮证据未持久化会话（session_persisted!=true），拒绝续跑：{resume_dir}"
    requested_thread_id = parse_thread_id(previous.get("thread_id"))
    if requested_thread_id is None:
        return None, f"上一轮 status.json 缺少合法 thread_id，拒绝续跑：{status_path}"
    if previous.get("mode") != mode:
        return None, (f"--resume-from 模式不符：上一轮 {previous.get('mode')!r}，"
                      f"当前 {mode!r}")
    previous_cwd = previous.get("cwd")
    if not isinstance(previous_cwd, str) or not previous_cwd:
        return None, f"上一轮 status.json 缺少 cwd，拒绝续跑：{status_path}"
    try:
        previous_cwd_path = Path(previous_cwd).resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"无法解析上一轮 cwd {previous_cwd!r}：{exc}"
    if previous_cwd_path != cwd:
        return None, (f"--resume-from cwd 不符：上一轮 {previous_cwd_path}，"
                      f"当前 {cwd}")
    raw_provider = previous.get("provider")
    if raw_provider is None or raw_provider == "":
        provider, provider_source = PROVIDER_DEEPSEEK, "旧 status.json 缺少 provider，按 deepseek 处理"
    elif not isinstance(raw_provider, str) or raw_provider not in PROVIDER_CHOICES:
        return None, (f"上一轮 status.json 的 provider 不受支持（仅支持 "
                      f"{'/'.join(PROVIDER_CHOICES)}），拒绝续跑：{raw_provider!r}")
    else:
        provider, provider_source = raw_provider, "沿用上一轮 status.json 的 provider"
    return {"thread_id": requested_thread_id, "resumed_from": str(resume_dir),
            "provider": provider, "provider_source": provider_source}, None

# 再委派监控：只解析真实命令，不把源码/散文里的字符串当作调用
def tokenize(text):
    # shlex 迭代到 EOF 会正常 StopIteration；不要再手写 get_token 取词循环。
    if not isinstance(text, str):
        text = str(text)
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        if os.name == "nt":
            lexer.escape = ""
        return [tok for tok in lexer if tok]
    except ValueError:
        return text.split()

def _parse_heredoc_delim(text, start):
    # 解析 `<<`/`<<-` 后的 heredoc delimiter；返回 (delimiter, 结束下标) 或 (None, start)
    i, n = start + 2, len(text)
    if i < n and text[i] == "<":  # here-string `<<<` 不是 heredoc
        return None, start
    if i < n and text[i] == "-":
        i += 1
    while i < n and text[i] in " \t":
        i += 1
    if i >= n:
        return None, start
    if text[i] in "'\"":
        end = text.find(text[i], i + 1)
        if end < 0:
            return None, start
        return text[i + 1:end], end + 1
    j = i
    while j < n and text[j] not in " \t\n;&|()<>":
        j += 1
    return (text[i:j], j) if j > i else (None, start)

def _skip_heredoc_bodies(text, start, delimiters):
    # 从 start 起逐个跳过 heredoc 正文，直到每个 delimiter 的结束行被消费
    pos, n = start, len(text)
    for delim in delimiters:
        while pos < n:
            line_end = text.find("\n", pos)
            if line_end < 0:
                line, pos = text[pos:], n
            else:
                line, pos = text[pos:line_end], line_end + 1
            if line.rstrip("\r").lstrip("\t") == delim:  # lstrip("\t") 兼容 `<<-`
                break
    return pos

def scan_command_pieces(text):
    # 把 shell 文本切成「单条命令」片段：按未加引号的分隔符（换行、;、&、|、(、)）切分；
    # 引号内的换行与分隔符保留为参数；heredoc 正文整段跳过，结束 delimiter 之后的命令继续
    # 参与判定。这是针对常见换行/heredoc 形式的定向处理，不追求完整 shell 语义。
    pieces, buf, pending, quote = [], [], [], None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(text[i + 1])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "<" and text[i + 1:i + 2] == "<":
            delim, end = _parse_heredoc_delim(text, i)
            if delim is not None:
                pending.append(delim)
                buf.append(text[i:end])
                i = end
                continue
        if ch in "\n;&|()":
            piece = "".join(buf).strip()
            if piece:
                pieces.append(piece)
            buf = []
            i += 1
            if ch == "\n" and pending:
                i = _skip_heredoc_bodies(text, i, pending)
                pending = []
            continue
        buf.append(ch)
        i += 1
    piece = "".join(buf).strip()
    if piece:
        pieces.append(piece)
    return pieces

def split_segments(command):
    # 按 shell 分隔符切成若干「单条命令」token 段（含换行分隔与 heredoc 正文跳过）
    if isinstance(command, (list, tuple)):
        return [list(command)]
    if not isinstance(command, str):
        command = str(command)
    segments = []
    for piece in scan_command_pieces(command):
        tokens = tokenize(piece)
        if tokens:
            segments.append(tokens)
    return segments

def base_name(tok):
    return ntpath.basename(tok)

def shell_command_string(args):
    # 从 `bash -lc '<cmd>'` 这类调用里取出 -c 的脚本文本
    for i, tok in enumerate(args):
        if tok == "--":
            return None
        if tok in ("-c", "/c", "-Command") or (tok.startswith("-") and not tok.startswith("--") and "c" in tok[1:]):
            return args[i + 1] if i + 1 < len(args) else None
    return None

def strip_wrapper_options(program, args):
    # 跳过包装命令自身的选项（含取值选项），返回其后的真实命令
    value_opts = WRAPPER_VALUE_OPTS.get(program, frozenset())
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return args[i + 1:]
        if tok.startswith("-") and tok != "-":
            i += 1
            if "=" not in tok and tok in value_opts and i < len(args):
                i += 1
            continue
        break
    return args[i:]

def python_runs_launcher(args):
    # python3 <path>/run_subagent.py 或旧的 run_ds.py 判定；`-m py_compile`、`-c` 等不算
    for i, tok in enumerate(args):
        if tok == "-m":
            return i + 1 < len(args) and base_name(args[i + 1]).lower() in LAUNCHER_SCRIPT_NAMES
        if tok == "-c":
            return False
        if tok.startswith("-") and tok != "-":
            continue
        return base_name(tok).lower() in LAUNCHER_SCRIPT_NAMES
    return False

def program_delegation(program, args):
    if program in AGENT_CLIS or program.startswith("codex"):
        return f"调用代理 CLI：{program}"
    if program in PYTHON_PROGRAMS:
        return "python 运行子代理启动器 run_subagent.py" if python_runs_launcher(args) else None
    if program in LAUNCHER_SCRIPT_NAMES:
        return "运行子代理启动器 run_subagent.py（含旧的 run_ds.py）"
    return None

def segment_delegation(tokens):
    # 判断单条命令段是否属于再委派；命中返回说明，否则 None
    i = 0
    while i < len(tokens) and tokens[i] in SEPARATORS:
        i += 1
    while i < len(tokens) and _ENV_ASSIGN.match(tokens[i]):
        i += 1
    if i >= len(tokens):
        return None
    program, args = base_name(tokens[i]).lower(), tokens[i + 1:]
    if program.endswith((".exe", ".cmd", ".bat")):
        program = program.rsplit(".", 1)[0]
    if program in ("powershell", "pwsh", "cmd"):
        content = shell_command_string(args)
        return command_delegation(content) if content else None
    if program in SHELLS:
        content = shell_command_string(args)
        return command_delegation(content) if content else None
    if program in WRAPPERS:
        rest = strip_wrapper_options(program, args)
        if program == "timeout" and rest:
            rest = rest[1:]  # 丢掉时长参数
        return segment_delegation(rest) if rest else None
    return program_delegation(program, args)

def command_delegation(command):
    # 判断整条命令（可能含分隔符/包装）是否属于再委派
    if isinstance(command, dict):
        command = command.get("command") or command.get("cmd") or ""
    for segment in split_segments(command):
        detail = segment_delegation(segment)
        if detail:
            return detail
    return None

def walk_events(node):
    """深度遍历事件中的字典/列表节点，产出所有字典节点。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk_events(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_events(value)

def detect_tool_activity(event):
    """事件中是否出现真实工具调用或命令执行；用于判断能否安全回退提供方。"""
    for node in walk_events(event):
        node_type = node.get("type")
        if isinstance(node_type, str) and node_type.lower() in TOOL_ACTIVITY_TYPES:
            return True
    return False

def _collect_error_strings(node, out):
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in ERROR_TEXT_KEYS:
                _collect_error_strings(value, out)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect_error_strings(value, out)

def collect_error_texts(event):
    """只从真实错误节点（type=error/turn.failed 等）提取文本。

    提示词、正常答复与命令输出都不在提取范围内，避免把散文里的字样当成提供方故障。
    """
    texts = []
    for node in walk_events(event):
        node_type = node.get("type")
        if isinstance(node_type, str) and node_type.lower() in ERROR_NODE_TYPES:
            _collect_error_strings(node, texts)
    return " ".join(text for text in texts if text)

def match_provider_failure(text):
    """在错误文本中匹配明确的提供方故障，返回标签或 None。"""
    if not text:
        return None
    for label, pattern in PROVIDER_FAILURE_PATTERNS:
        if pattern.search(text):
            return label
    return None

def read_text_head_tail(path, limit):
    """读取文件头部与尾部各 limit 字节并解码，避免大文件全量载入。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            head = handle.read(limit)
            tail = b""
            if size > limit:
                handle.seek(max(limit, size - limit))
                tail = handle.read(limit)
    except OSError:
        return ""
    return (head + b"\n" + tail).decode("utf-8", "replace")

def detect_provider_failure(state, stderr_path, stderr_tail):
    """只在错误事件文本与 stderr 中匹配提供方故障，返回标签或 None。

    匹配来源仅限结构化错误节点与 stderr（含其头部/尾部片段），不读取提示词、正常
    答复或 events.jsonl 中的非错误事件。
    """
    sources = [state.get("error_text") or "", "\n".join(stderr_tail or [])]
    file_text = read_text_head_tail(Path(stderr_path), PROVIDER_SCAN_BYTES)
    if file_text:
        sources.append(file_text)
    for source in sources:
        label = match_provider_failure(source)
        if label:
            return label
    return None

def detect_delegation(event):
    # 只按真实事件结构识别；不递归把任意 name/正文文字当成工具调用
    def walk(node):
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
    for node in walk(event):
        node_type = node.get("type")
        if not isinstance(node_type, str):
            continue
        low = node_type.lower()
        if "collab" in low or "subagent" in low or "multi_agent" in low or low in COLLAB_ITEM_TYPES:
            return f"collab 工具事件：{node_type}"
        if low in TOOL_ITEM_TYPES:
            name = str(node.get("name") or node.get("tool") or "").lower()
            if "collab" in str(node.get("server") or "").lower() or any(h in name for h in COLLAB_HINTS):
                return f"collab 工具调用：{node.get('name') or node.get('tool')}"
        if low == "command_execution":
            command = node.get("command")
            if isinstance(command, (str, list, tuple, dict)):
                detail = command_delegation(command)
                if detail:
                    return detail
    return None

# 子进程管理：独立进程组，超时/取消/违规一律清理整组并回收 reader
def build_command(mode, cwd, output_last_message, provider, resume_thread_id=None):
    sandbox = "read-only" if mode == "explore" else "workspace-write"
    marker = f'shell_environment_policy.set.{WORKER_ENV_FLAG}="{WORKER_ENV_VALUE}"'
    command = ["codex", "exec", "--profile", provider,
               "--disable", "multi_agent", "--disable", "multi_agent_v2",
               "-c", "agents.enabled=false",
               "--json", "--color", "never", "--skip-git-repo-check",
               "--sandbox", sandbox, "-C", str(cwd),
               "--output-last-message", str(output_last_message), "-c", marker]
    if resume_thread_id is None:
        command.append("-")
    else:
        # resume 子命令本身不接受 --profile/-C/--sandbox/--color，公共 exec 参数必须前置。
        command.extend(["resume", resume_thread_id, "-"])
    return command

def build_child_env():
    env = dict(os.environ)
    for flag in WORKER_ENV_FLAGS:  # 新旧标记同时注入，避免旧技能文档下漏判工人身份
        env[flag] = WORKER_ENV_VALUE
    return env

def worker_marker_present(env=None):
    """返回命中的工人标记名（新旧任一），未命中返回 None。"""
    env = os.environ if env is None else env
    for flag in WORKER_ENV_FLAGS:
        if env.get(flag) == WORKER_ENV_VALUE:
            return flag
    return None

def build_prompt(mode, task_body):
    trailer = EXPLORE_OUTPUT if mode == "explore" else IMPLEMENT_OUTPUT
    return RULES_HEADER + trailer + "\n===== 任务开始 =====\n\n" + task_body.strip() + "\n\n" + RULES_FOOTER

def close_quietly(handle):
    try:
        if handle is not None:
            handle.close()
    except OSError:
        pass

if os.name == "nt":
    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimitInformation),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_job = _kernel32.CreateJobObjectW
    _create_job.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _create_job.restype = wintypes.HANDLE
    _set_job_info = _kernel32.SetInformationJobObject
    _set_job_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _set_job_info.restype = wintypes.BOOL
    _assign_job = _kernel32.AssignProcessToJobObject
    _assign_job.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _assign_job.restype = wintypes.BOOL
    _terminate_job = _kernel32.TerminateJobObject
    _terminate_job.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _terminate_job.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL

def create_process_job(proc):
    job = _create_job(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _set_job_info(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _assign_job(job, wintypes.HANDLE(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        return job
    except OSError:
        _close_handle(job)
        raise

def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True

def kill_group(pgid):
    # 先 TERM 再 KILL 整个进程组；leader 是否已退出都不影响清理其余后代
    if pgid is None:
        return
    if os.name == "nt":
        _terminate_job(pgid, 1)
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline and group_alive(pgid):
        time.sleep(0.05)
    if group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass

def new_state():
    return {"event_count": 0, "bad_json": 0, "bad_json_sample": None, "saw_completed": False,
            "saw_failed": False, "saw_error": False, "violation": None, "reader_error": None,
            "encoding_error": None, "thread_id": None, "requested_thread_id": None,
            "session_mismatch": None, "tool_started": False, "error_text": ""}

def audit_event(event, state):
    etype = str(event.get("type", ""))
    if not state["tool_started"] and detect_tool_activity(event):
        state["tool_started"] = True
    error_text = collect_error_texts(event)
    if error_text:
        state["error_text"] = f"{state['error_text']} {error_text}".strip()
    if etype == "turn.completed":
        state["saw_completed"] = True
    elif etype == "turn.failed":
        state["saw_failed"] = True
    elif etype == "error":
        state["saw_error"] = True
    elif etype == "thread.started":
        raw_thread_id = event.get("thread_id")
        parsed_thread_id = parse_thread_id(raw_thread_id)
        state["thread_id"] = parsed_thread_id if parsed_thread_id is not None else raw_thread_id
        requested = state.get("requested_thread_id")
        if requested is not None and parsed_thread_id != requested:
            detail = (f"thread.started 返回的 thread_id 与请求不符："
                      f"请求 {requested}，实际 {raw_thread_id!r}")
            state["session_mismatch"] = state["session_mismatch"] or detail
    if state["violation"] is None:
        state["violation"] = detect_delegation(event)

def audit_line(raw, state):
    state["event_count"] += 1
    try:  # stdout 必须是合法 UTF-8：解码失败按协议失败，不悄悄替换后当成功
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        state["encoding_error"] = state["encoding_error"] or f"stdout 不是合法 UTF-8：{exc}"
        return
    text = text.strip()
    if text:
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict):
            audit_event(event, state)
        else:  # 坏 JSON 不能被忽略：留下样本并按协议失败
            state["bad_json"] += 1
            state["bad_json_sample"] = state["bad_json_sample"] or text[:400]
    if state["event_count"] % PROGRESS_EVERY == 0:
        print(f"run_subagent: 事件 {state['event_count']}", file=sys.stderr)


def read_stream(stream, sink, on_line, state, tag):
    """把一路管道原样落盘，并把每行交给 on_line；异常必须回传，不能假成功。"""
    try:
        for raw in stream:
            sink.write(raw)
            sink.flush()
            on_line(raw)
    except Exception as exc:
        state["reader_error"] = state["reader_error"] or f"{tag} reader 异常：{exc!r}"
    finally:
        close_quietly(stream)

def feed_stdin(proc, data):
    try:
        if proc.stdin is not None:
            proc.stdin.write(data)
            proc.stdin.flush()
    except (BrokenPipeError, ValueError, OSError):
        pass
    finally:
        close_quietly(proc.stdin)

def monitor(proc, pgid, state, prompt, events_handle, stderr_handle, stderr_tail, timeout, stop, started):
    def append_tail(raw):
        stderr_tail.append(raw.decode("utf-8", "replace").rstrip("\n"))
        del stderr_tail[:-STDERR_TAIL_LINES]
    readers = [threading.Thread(target=read_stream,
                                args=(proc.stdout, events_handle, lambda r: audit_line(r, state), state, "stdout"),
                                daemon=True),
               threading.Thread(target=read_stream,
                                args=(proc.stderr, stderr_handle, append_tail, state, "stderr"),
                                daemon=True)]
    writer = threading.Thread(target=feed_stdin, args=(proc, prompt), daemon=True)
    for thread in readers + [writer]:
        thread.start()
    deadline = started + timeout
    while True:
        if state["reader_error"]:
            outcome = "internal_error"
            break
        if state["violation"]:
            outcome = DELEGATION_REASON
            break
        if state.get("session_mismatch"):
            outcome = "session_mismatch"
            break
        if stop["reason"]:
            outcome = "cancelled"
            break
        if proc.poll() is not None and not any(t.is_alive() for t in readers):
            outcome = "exited"
            break
        if time.monotonic() >= deadline:
            outcome = "timeout"  # leader 已退出但后代仍持有管道 → 一样会走到这里
            break
        time.sleep(POLL_INTERVAL)
    if os.name == "nt" or outcome != "exited" or group_alive(pgid):
        kill_group(pgid)  # 超时/取消/违规，或 leader 退出后仍有后代：清理整组
    drain_deadline = time.monotonic() + KILL_GRACE_SECONDS
    for thread in readers:
        thread.join(timeout=max(0.0, drain_deadline - time.monotonic()))
    if any(t.is_alive() for t in readers):  # 杀组后再强制关管道，确保 reader 退出
        close_quietly(proc.stdout)
        close_quietly(proc.stderr)
        for thread in readers:
            thread.join(timeout=1.0)
    close_quietly(proc.stdin)
    writer.join(timeout=1.0)
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        kill_group(pgid)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
    if state["violation"] and outcome != DELEGATION_REASON:
        outcome = DELEGATION_REASON  # 最后排空阶段才发现的违规不能被 exited 覆盖
    return outcome

def write_status(status_path, status):
    try:
        payload = json.dumps(status, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        payload = json.dumps({k: str(v) for k, v in status.items()}, ensure_ascii=False, indent=2)
    try:
        with open(status_path, "x", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError as exc:
        return False, f"无法写入 {status_path}：{exc}"
    return True, None

def classify(state, outcome, exit_code, final_size, start_error=None, internal_detail=None,
             provider_failure=None):
    # 优先级：启动失败 > 编码错误 > 内部错误 > 违规 > 会话不匹配 > 提供方故障 > 协议错误
    #         > 超时/取消 > 退出码 > 完成信号 > 缺会话 ID > 空答复
    # 提供方故障只在子进程正常退出（exited）时归因：超时/取消必须如实保留原原因，
    # 且不得据此触发提供方回退；正常退出时维持协议/错误分支原有的先后顺序。
    if start_error:
        return "failed", "start_failed", start_error  # 保留真实 OSError 信息，不被「内部错误」覆盖
    if state["encoding_error"]:
        return "failed", "encoding_error", state["encoding_error"]
    if state["reader_error"] or outcome == "internal_error":
        return "failed", "internal_error", internal_detail or state["reader_error"] or "内部错误"
    if state["violation"]:
        return "failed", DELEGATION_REASON, "检测到实际再委派调用，已终止整个进程组"
    if state.get("session_mismatch"):
        return "failed", "session_mismatch", state["session_mismatch"]
    if (provider_failure and outcome == "exited" and not state["saw_completed"]
            and not state["bad_json"]):
        return "failed", PROVIDER_FAILURE_REASON, (
            f"提供方故障（{provider_failure}）：错误事件或 stderr 明确报告配额耗尽、限流或服务过载")
    if state["bad_json"] or state["saw_failed"] or state["saw_error"]:
        return "failed", "error_protocol", "事件流出现坏 JSON、turn.failed 或 error"
    if outcome == "timeout":
        return "failed", "timeout", "超时未完成，已终止整个进程组"
    if outcome == "cancelled":
        return "failed", "cancelled", "收到 SIGINT/SIGTERM，已终止整个进程组"
    if exit_code != 0:
        return "failed", "child_failed", f"子进程退出码 {exit_code}"
    if not state["saw_completed"]:
        return "failed", "no_completion_signal", "未观测到 turn.completed"
    if parse_thread_id(state.get("thread_id")) is None:
        return "failed", "session_missing", "未在 thread.started 中获得合法 thread_id"
    if final_size <= 0:
        return "failed", "final_message_missing", "缺少或为空的最终答复文件 final.txt"
    return "succeeded", "ok", None

def run(argv):
    args = parse_args(argv)
    marker = worker_marker_present()
    if marker is not None:
        return abort(f"当前执行者已是子代理工人（{marker}={WORKER_ENV_VALUE}），拒绝启动子进程；"
                     "请直接完成本任务，或把缺口返回主代理。")
    if not (isinstance(args.timeout, float) and math.isfinite(args.timeout) and args.timeout > 0):
        return abort(f"--timeout 需为有限正数，收到 {args.timeout!r}")
    cwd = Path(args.cwd)
    if not cwd.is_dir():
        return abort(f"--cwd 不是已存在目录：{args.cwd}")
    prompt_path = Path(args.prompt_file)
    if not prompt_path.is_file():
        return abort(f"--prompt-file 不存在或不是文件：{args.prompt_file}")
    try:
        task_body = prompt_path.resolve().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return abort(f"读取 --prompt-file 失败：{exc}")
    if not task_body.strip():
        return abort("--prompt-file 内容为空")
    cwd = cwd.resolve()

    resume_context = None
    requested_thread_id = None
    if args.resume_from is not None:
        resume_context, error = load_resume_context(args.resume_from, args.mode, cwd)
        if resume_context is None:
            return abort(error)
        requested_thread_id = resume_context["thread_id"]
        provider = resume_context["provider"]
        if args.provider is not None and args.provider != provider:
            return abort(f"--provider={args.provider} 与上一轮提供方 {provider} 不一致：续跑必须沿用"
                         f"上一轮提供方（{resume_context['provider_source']}）")
        peak_active = None
        routing_reason = (f"续跑沿用上一轮提供方 {provider}（{resume_context['provider_source']}）；"
                          "续跑不参与峰值判定与提供方回退")
    else:
        # 时钟固定用 UTC 再换算 UTC+8，不依赖本机时区设置
        provider, peak_active, routing_reason = route_provider(
            args.provider, args.peak_window, current_utc_now())

    session_lock = None
    if requested_thread_id is not None:
        session_lock, lock_error = acquire_session_lock(requested_thread_id)
        if session_lock is None:
            return abort(lock_error)
    try:
        return run_session(args, cwd, task_body, prompt_path, resume_context, requested_thread_id,
                           provider, peak_active, routing_reason)
    finally:
        release_session_lock(session_lock)

def run_session(args, cwd, task_body, prompt_path, resume_context, requested_thread_id,
                provider, peak_active, routing_reason):
    resumed_from = resume_context["resumed_from"] if resume_context is not None else None
    # 只有独占取得输出目录与运行锁后才允许落任何证据；未取得则只报 stderr 并返回非零。
    output_dir, error = acquire_output_dir(Path(args.output_dir))
    if output_dir is None:
        return abort(error)
    lock_handle, lock_error = acquire_run_lock(output_dir)
    if lock_handle is None:
        return abort(lock_error)
    try:
        return run_attempts(args, cwd, task_body, prompt_path, output_dir, requested_thread_id,
                            resumed_from, provider, peak_active, routing_reason)
    finally:
        close_quietly(lock_handle)

def base_attempt_status(args, cwd, prompt_path, output_dir, requested_thread_id, resumed_from,
                        provider, routing_reason, peak_active, index):
    """单次尝试的 status 初值：沿用顶层 schema，另加尝试序号与提供方信息。"""
    return {"mode": args.mode, "cwd": str(cwd), "prompt_file": str(prompt_path.resolve()),
            "output_dir": str(output_dir), "result": "failed", "reason": "internal_error",
            "timeout_seconds": args.timeout, "duration_seconds": None, "child_exit_code": None,
            "event_count": 0, "saw_turn_completed": False, "bad_json_lines": 0,
            "final_message_present": False, "final_message_bytes": 0, "stderr_tail": [],
            "outcome": None, "error": None, "thread_id": None,
            "requested_thread_id": requested_thread_id, "session_persisted": False,
            "resumed_from": resumed_from, "attempt_index": index, "provider": provider,
            "routing_reason": routing_reason, "peak_window_active": peak_active,
            "tool_started": False, "provider_failure": None,
            "attempt_dir": ATTEMPT_DIR_TEMPLATE.format(index=index)}

def attempt_record(index, provider, attempt_dir, status, status_written, raised):
    return {"index": index, "provider": provider, "attempt_dir": str(attempt_dir),
            "dir_name": attempt_dir.name, "status": status, "status_written": status_written,
            "raised": raised}

def run_attempts(args, cwd, task_body, prompt_path, output_dir, requested_thread_id, resumed_from,
                 provider, peak_active, routing_reason):
    """按提供方路由执行尝试：zai 明确提供方故障且未开始工具调用时用 deepseek 重试一次。"""
    started = time.monotonic()
    stop = {"reason": None}
    attempts = []
    fallback_reason = None
    fallback_note = None
    raised = None

    def handle_signal(signum, _frame):
        stop["reason"] = stop["reason"] or signal.Signals(signum).name

    previous = {sig: signal.signal(sig, handle_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for index in (1, 2):
            attempt_provider = provider if index == 1 else FALLBACK_PROVIDER
            attempt_reason = routing_reason if index == 1 else (
                f"首次尝试 {PROVIDER_ZAI} 提供方故障（{fallback_reason}），"
                f"改用 {FALLBACK_PROVIDER} 重试一次")
            record = run_attempt(index, attempt_provider, output_dir, args, cwd, task_body,
                                 prompt_path, requested_thread_id, resumed_from, stop,
                                 attempt_reason, peak_active)
            attempts.append(record)
            if record["raised"] is not None:
                raised = record["raised"]
                break
            if index == 1:
                allowed, fallback_note = fallback_allowed(record, resumed_from, stop)
                if allowed:
                    fallback_reason = fallback_note
                    continue
            break
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return finish_attempts(args, cwd, prompt_path, output_dir, attempts, provider, peak_active,
                           routing_reason, requested_thread_id, resumed_from, started,
                           fallback_reason, fallback_note, raised)

def fallback_allowed(record, resumed_from, stop):
    """判断首次尝试能否回退到 DeepSeek，返回 (是否允许, 说明)。

    只在明确提供方故障、正常退出、尚未出现任何工具调用/命令、且不是续跑时允许，
    其余情况一律不回退，避免重复副作用或掩盖真实失败。
    """
    status = record.get("status") or {}
    if resumed_from is not None:
        return False, "续跑任务不使用提供方回退"
    if record.get("provider") != PROVIDER_ZAI:
        return False, f"首次尝试提供方是 {record.get('provider')}，没有回退目标"
    if status.get("reason") != PROVIDER_FAILURE_REASON:
        return False, f"首次尝试的失败原因不是提供方故障（{status.get('reason')}），不触发回退"
    if status.get("result") != "failed":
        return False, "首次尝试未失败，不触发回退"
    if status.get("outcome") != "exited":
        return False, f"首次尝试未正常退出（{status.get('outcome')}），不触发回退"
    if status.get("tool_started"):
        return False, "首次尝试已开始工具调用或命令，禁止回退以避免重复副作用"
    if status.get("saw_turn_completed"):
        return False, "首次尝试已出现 turn.completed，不触发回退"
    if stop["reason"]:
        return False, f"已收到取消信号（{stop['reason']}），不触发回退"
    if record.get("raised") is not None:
        return False, "首次尝试出现意外异常，不触发回退"
    if not record.get("status_written", False):
        return False, "首次尝试的证据未完整落盘，不触发回退"
    return True, (f"{PROVIDER_ZAI} 提供方故障（{status.get('provider_failure')}），"
                  "尚未开始任何工具调用")

def run_attempt(index, provider, output_dir, args, cwd, task_body, prompt_path,
                requested_thread_id, resumed_from, stop, routing_reason, peak_active):
    """执行一次子代理调用，证据落在 attempt-N 子目录，返回尝试记录。"""
    started = time.monotonic()
    attempt_dir = output_dir / ATTEMPT_DIR_TEMPLATE.format(index=index)
    status = base_attempt_status(args, cwd, prompt_path, output_dir, requested_thread_id,
                                 resumed_from, provider, routing_reason, peak_active, index)
    try:
        attempt_dir.mkdir(exist_ok=False)
    except OSError as exc:
        status.update(error=f"无法创建证据子目录：{exc}",
                      duration_seconds=round(time.monotonic() - started, 3))
        print(f"run_subagent: 无法创建证据子目录：{exc}", file=sys.stderr)
        return attempt_record(index, provider, attempt_dir, status, False, None)
    final_path = attempt_dir / "final.txt"
    stderr_path = attempt_dir / "stderr.log"
    state, stderr_tail = new_state(), []
    state["requested_thread_id"] = requested_thread_id
    proc = pgid = events_handle = stderr_handle = None
    outcome = "internal_error"
    start_error = None
    unexpected = None
    try:
        try:
            events_handle = open(attempt_dir / "events.jsonl", "xb")
            stderr_handle = open(stderr_path, "xb")
        except OSError as exc:
            start_error = f"无法创建证据文件：{exc}"
        else:
            try:
                proc = subprocess.Popen(
                    build_command(args.mode, cwd, final_path, provider,
                                  resume_thread_id=requested_thread_id),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(cwd),
                    env=build_child_env(), start_new_session=os.name != "nt", close_fds=True,
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
                if os.name == "nt":
                    try:
                        pgid = create_process_job(proc)
                    except OSError:
                        proc.kill()
                        proc.wait()
                        raise
                else:
                    pgid = proc.pid
            except OSError as exc:
                start_error = f"无法启动子进程：{exc}"
            else:
                outcome = monitor(proc, pgid, state, build_prompt(args.mode, task_body).encode("utf-8"),
                                  events_handle, stderr_handle, stderr_tail, args.timeout, stop, started)
    except BaseException as exc:  # 意外异常/取消：也终止整个进程组，稍后尽量记录 status
        unexpected = exc
        if pgid is not None:
            kill_group(pgid)
    finally:
        close_quietly(events_handle)
        close_quietly(stderr_handle)
        if os.name == "nt" and pgid is not None:
            _close_handle(pgid)

    final_size = final_path.stat().st_size if final_path.is_file() else 0
    exit_code = proc.returncode if proc is not None else None
    provider_failure = detect_provider_failure(state, stderr_path, stderr_tail)
    internal_detail = f"意外中断：{unexpected!r}" if unexpected is not None else None
    result, reason, message = classify(state, outcome, exit_code, final_size,
                                       start_error=start_error, internal_detail=internal_detail,
                                       provider_failure=provider_failure)
    status.update(result=result, reason=reason, error=message, outcome=outcome,
                  duration_seconds=round(time.monotonic() - started, 3), child_exit_code=exit_code,
                  event_count=state["event_count"], saw_turn_completed=state["saw_completed"],
                  bad_json_lines=state["bad_json"], final_message_present=final_size > 0,
                  final_message_bytes=final_size, stderr_tail=stderr_tail[-STDERR_TAIL_LINES:],
                  thread_id=state.get("thread_id"),
                  requested_thread_id=requested_thread_id,
                  session_persisted=(parse_thread_id(state.get("thread_id")) is not None
                                     and not state.get("session_mismatch")),
                  resumed_from=resumed_from, tool_started=state["tool_started"],
                  provider_failure=provider_failure)
    if state["violation"]:
        status["violation_event"] = state["violation"]
    if state["session_mismatch"]:
        status["session_mismatch"] = state["session_mismatch"]
    if state["bad_json_sample"]:
        status["bad_json_sample"] = state["bad_json_sample"]
    if state["encoding_error"]:
        status["encoding_error"] = state["encoding_error"]

    status_written, write_error = write_status(attempt_dir / "status.json", status)
    if not status_written:  # 单次尝试证据不完整：如实记录，顶层按失败处理
        status["attempt_status_write_error"] = write_error
        print(f"run_subagent: 失败：{write_error}", file=sys.stderr)
    raised = unexpected if isinstance(unexpected, (KeyboardInterrupt, SystemExit)) else None
    return attempt_record(index, provider, attempt_dir, status, status_written, raised)

def summarize_attempt(record):
    status = record.get("status") or {}
    return {"index": record.get("index"), "provider": record.get("provider"),
            "attempt_dir": record.get("dir_name"),
            "status_file": f"{record.get('dir_name')}/status.json",
            "result": status.get("result", "failed"), "reason": status.get("reason", "internal_error"),
            "error": status.get("error"), "outcome": status.get("outcome"),
            "child_exit_code": status.get("child_exit_code"),
            "thread_id": status.get("thread_id"),
            "session_persisted": status.get("session_persisted", False),
            "event_count": status.get("event_count", 0),
            "duration_seconds": status.get("duration_seconds"),
            "tool_started": status.get("tool_started", False),
            "provider_failure": status.get("provider_failure"),
            "routing_reason": status.get("routing_reason"),
            "status_written": record.get("status_written", False)}

def describe_fallback(attempts, fallback_reason, fallback_note):
    first = attempts[0]
    if len(attempts) > 1 and fallback_reason:
        return {"attempted": True, "from": first.get("provider"),
                "to": attempts[1].get("provider"),
                "trigger": (first.get("status") or {}).get("provider_failure"),
                "reason": fallback_reason}
    return {"attempted": False, "from": first.get("provider"),
            "reason": fallback_note or "未触发提供方回退"}

def copy_selected_final(record, dest_path):
    """把选中尝试的 final.txt 复制到顶层；保持不覆盖语义，源不存在视为无最终答复。"""
    source = Path(record["attempt_dir"]) / "final.txt"
    if not source.is_file():
        return True, None
    created = False
    try:
        with open(source, "rb") as reader:
            with open(dest_path, "xb") as writer:
                created = True
                for chunk in iter(lambda: reader.read(65536), b""):
                    writer.write(chunk)
    except OSError as exc:
        if created:  # 只清理本次新建的目标文件，绝不触碰既有文件
            try:
                dest_path.unlink()
            except OSError:
                pass
        return False, f"复制选中尝试的 final.txt 到 {dest_path} 失败：{exc}"
    return True, None

def finish_attempts(args, cwd, prompt_path, output_dir, attempts, provider, peak_active,
                    routing_reason, requested_thread_id, resumed_from, started, fallback_reason,
                    fallback_note, raised):
    if not attempts:  # 信号处理器安装失败等极端情况：仍留下可读的失败证据
        status = base_attempt_status(args, cwd, prompt_path, output_dir, requested_thread_id,
                                     resumed_from, provider, routing_reason, peak_active, 1)
        status.update(error="未执行任何尝试", duration_seconds=round(time.monotonic() - started, 3))
        attempts = [attempt_record(1, provider,
                                   output_dir / ATTEMPT_DIR_TEMPLATE.format(index=1),
                                   status, False, raised)]
    selected = attempts[-1]
    attempt = selected["status"]
    # 顶层 status.json 描述最终选中的尝试，并保留两次尝试的完整摘要供人工核查
    status = {"mode": args.mode, "cwd": str(cwd), "prompt_file": str(prompt_path.resolve()),
              "output_dir": str(output_dir), "result": attempt.get("result", "failed"),
              "reason": attempt.get("reason", "internal_error"), "error": attempt.get("error"),
              "timeout_seconds": args.timeout,
              "duration_seconds": round(time.monotonic() - started, 3),
              "child_exit_code": attempt.get("child_exit_code"),
              "event_count": attempt.get("event_count", 0),
              "saw_turn_completed": attempt.get("saw_turn_completed", False),
              "bad_json_lines": attempt.get("bad_json_lines", 0),
              "final_message_present": attempt.get("final_message_present", False),
              "final_message_bytes": attempt.get("final_message_bytes", 0),
              "stderr_tail": attempt.get("stderr_tail", []),
              "outcome": attempt.get("outcome"), "thread_id": attempt.get("thread_id"),
              "requested_thread_id": requested_thread_id,
              "session_persisted": attempt.get("session_persisted", False),
              "resumed_from": resumed_from, "provider": selected.get("provider"),
              "requested_provider": args.provider, "peak_window": args.peak_window,
              "peak_window_active": peak_active,
              "routing_reason": attempt.get("routing_reason"),
              "attempt_count": len(attempts), "selected_attempt": selected.get("index"),
              "attempt_dir": selected.get("dir_name"),
              "tool_started": attempt.get("tool_started", False),
              "provider_failure": attempt.get("provider_failure"),
              "attempts": [summarize_attempt(record) for record in attempts],
              "fallback": describe_fallback(attempts, fallback_reason, fallback_note)}
    for key in ("violation_event", "session_mismatch", "bad_json_sample", "encoding_error"):
        if key in attempt:
            status[key] = attempt[key]
    if attempt.get("attempt_status_write_error"):
        # 选中尝试的证据文件没写全，顶层不能宣告成功
        status["attempt_status_write_error"] = attempt["attempt_status_write_error"]
        status["result"] = "failed"
        status["reason"] = COPY_FINAL_REASON
        status["error"] = attempt["attempt_status_write_error"]
    copy_ok, copy_error = copy_selected_final(selected, output_dir / "final.txt")
    if not copy_ok:
        status["final_copy_error"] = copy_error
        status["final_message_present"] = False
        status["final_message_bytes"] = 0
        status["result"] = "failed"
        status["reason"] = COPY_FINAL_REASON
        status["error"] = copy_error
        print(f"run_subagent: {copy_error}", file=sys.stderr)

    write_ok, write_error = write_status(output_dir / "status.json", status)
    if not write_ok:  # status 落盘失败绝不宣告成功：打印原因并返回非零
        print(f"run_subagent: 失败：{write_error}", file=sys.stderr)
        print("run_subagent: status.json 未落盘，按失败返回", file=sys.stderr)
        if raised is not None:
            raise raised
        return 1
    print(f"run_subagent: {status['result']} ({status['reason']}), 提供方 {status['provider']}, "
          f"尝试 {status['attempt_count']} 次, 事件 {status['event_count']}, "
          f"退出码 {status['child_exit_code']}, 耗时 {status['duration_seconds']}s", file=sys.stderr)
    if fallback_reason:
        print(f"run_subagent: 提供方回退：{fallback_reason}", file=sys.stderr)
    if status.get("error") and status["result"] != "succeeded":
        print(f"run_subagent: {status['error']}", file=sys.stderr)
    if raised is not None:
        raise raised
    return 0 if status["result"] == "succeeded" else 1

# 纯函数自测：只断言再委派判定、提供方路由与故障匹配，不启动任何子进程/代理
SELF_TEST_CASES = (
    ("codex exec x", True),
    ("pwd\ncodex exec x", True),
    ("codex exec x\npwd", True),
    ("bash -lc 'codex exec x'", True),
    ("bash -c 'pwd; codex exec x'", True),
    ("env FOO=1 timeout 5 codex exec x", True),
    ("timeout 5 codex exec x", True),
    ("sudo codex exec x", True),
    ("python3 scripts/run_ds.py --mode explore", True),
    ("python3 scripts/run_subagent.py --mode explore", True),
    ("python3 -m run_subagent --mode explore", True),
    ("run_subagent.py --mode implement", True),
    ("bash -lc 'python3 run_subagent.py --mode explore'", True),
    ("RUN_SUBAGENT.PY --mode explore", True),
    (r"C:\Tools\codex.exe exec x", True),
    (r"python.exe C:\Tools\run_ds.py --mode explore", True),
    (r"python.exe C:\Tools\run_subagent.py --mode explore", True),
    ("powershell.exe -NoProfile -Command 'codex.exe exec x'", True),
    ("cmd.exe /c 'codex.exe exec x'", True),
    ("cat <<'EOF'\nbody line\nEOF\ncodex exec y", True),
    ("pwd", False),
    ("cat file | rg codex", False),
    ("rg -n 'codex exec' .", False),
    ('echo "run codex exec later"', False),
    ("rg -n 'run_subagent.py' .", False),
    ('echo "稍后运行 run_subagent.py"', False),
    ("python3 -m py_compile scripts/run_ds.py", False),
    ("python3 -m py_compile scripts/run_subagent.py", False),
    (r"C:\Tools\python.exe -m py_compile C:\Tools\run_ds.py", False),
    ("cat <<'EOF'\ntext; codex exec fake\nEOF", False),
    ("cat <<'EOF'\nbody\nEOF", False),
    ("python3 -c 'print(1)'", False),
)

ROUTING_TEST_CASES = (
    # (显式 provider, 峰值模式, UTC 年, 月, 日, 时, 分, 期望 provider, 期望峰值判定)
    (None, "auto", 2026, 9, 21, 5, 59, PROVIDER_ZAI, False),        # UTC+8 13:59
    (None, "auto", 2026, 9, 21, 6, 0, PROVIDER_DEEPSEEK, True),     # UTC+8 14:00
    (None, "auto", 2026, 9, 21, 9, 59, PROVIDER_DEEPSEEK, True),    # UTC+8 17:59
    (None, "auto", 2026, 9, 21, 10, 0, PROVIDER_ZAI, False),        # UTC+8 18:00 起非峰值
    (None, "auto", 2026, 9, 21, 22, 30, PROVIDER_ZAI, False),
    (None, "on", 2026, 9, 21, 22, 30, PROVIDER_DEEPSEEK, True),
    (None, "off", 2026, 9, 21, 6, 0, PROVIDER_ZAI, False),
    (PROVIDER_DEEPSEEK, "auto", 2026, 9, 21, 22, 30, PROVIDER_DEEPSEEK, None),
    (PROVIDER_ZAI, "on", 2026, 9, 21, 6, 0, PROVIDER_ZAI, None),
)

PROVIDER_FAILURE_TEST_CASES = (
    ("HTTP 429 Too Many Requests", "rate_limit"),
    ("rate_limit_error: please retry later", "rate_limit"),
    ("触发限流，请稍后重试", "rate_limit"),
    ("You exceeded your current quota", "quota_exhausted"),
    ("insufficient balance：余额不足", "quota_exhausted"),
    ('{"type":"overloaded_error","message":"Overloaded"}', "service_overload"),
    ("503 Service Unavailable", "service_overload"),
    ("connection reset by peer", None),
    ("request timed out after 900s", None),
    ("permission denied", None),
    ("", None),
)

TOOL_ACTIVITY_TEST_CASES = (
    ('{"type":"item.completed","item":{"type":"command_execution","command":"pwd"}}', True),
    ('{"type":"item.completed","item":{"type":"mcp_tool_call","name":"read_file"}}', True),
    ('{"type":"item.completed","item":{"type":"file_change","path":"a.py"}}', True),
    ('{"type":"patch_apply_begin","path":"a.py"}', True),
    ('{"type":"item.completed","item":{"type":"agent_message","text":"已完成"}}', False),
    ('{"type":"item.completed","item":{"type":"reasoning","text":"先看仓库结构"}}', False),
    ('{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000000"}', False),
    ('{"type":"turn.completed","usage":{"input_tokens":1}}', False),
)

# 关键约束：提示词与正常答复里的字样不得被当成提供方故障
ERROR_TEXT_TEST_CASES = (
    ('{"type":"error","message":"429 Too Many Requests"}', "rate_limit"),
    ('{"type":"turn.failed","error":{"message":"Overloaded","type":"overloaded_error"}}',
     "service_overload"),
    ('{"type":"item.completed","item":{"type":"agent_message","text":"我遇到过 429 限流"}}', None),
    ('{"type":"item.completed","item":{"type":"command_execution","command":"curl x/429"}}', None),
    ('{"type":"thread.started","thread_id":"x","message":"rate limit"}', None),
)

def run_command_checks():
    """核验命令行形状：提供方 profile、沙箱、工人标记、resume 参数位置。"""
    failures = []
    marker = f'shell_environment_policy.set.{WORKER_ENV_FLAG}="{WORKER_ENV_VALUE}"'
    for mode, provider, sandbox in (("explore", PROVIDER_ZAI, "read-only"),
                                    ("implement", PROVIDER_DEEPSEEK, "workspace-write")):
        command = build_command(mode, Path("/tmp"), Path("/tmp/final.txt"), provider)
        problems = []
        if command[:2] != ["codex", "exec"]:
            problems.append(f"前缀异常：{command[:2]}")
        if command[2:4] != ["--profile", provider]:
            problems.append(f"未使用 --profile {provider}：{command[2:4]}")
        if "--sandbox" not in command or command[command.index("--sandbox") + 1] != sandbox:
            problems.append(f"sandbox 不是 {sandbox}")
        if marker not in command:
            problems.append("缺少工人环境标记")
        if command[-1] != "-":
            problems.append("新会话未以 stdin 提示结束")
        resume = build_command(mode, Path("/tmp"), Path("/tmp/final.txt"), provider,
                               resume_thread_id="session-id")
        if resume[-3:] != ["resume", "session-id", "-"]:
            problems.append(f"resume 参数异常：{resume[-3:]}")
        if problems:
            failures.append((mode, provider, problems))
    return failures

def run_routing_checks():
    failures = []
    for case in ROUTING_TEST_CASES:
        provider_arg, peak_mode, year, month, day, hour, minute, want_provider, want_peak = case
        now = datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.timezone.utc)
        got_provider, got_peak, reason = route_provider(provider_arg, peak_mode, now)
        stamp = f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}Z"
        if (got_provider != want_provider or got_peak != want_peak
                or not isinstance(reason, str) or not reason):
            failures.append((stamp, provider_arg, peak_mode, want_provider, want_peak,
                             got_provider, got_peak, reason))
    return failures

def run_provider_failure_checks():
    failures = []
    for text, want in PROVIDER_FAILURE_TEST_CASES:
        got = match_provider_failure(text)
        if got != want:
            failures.append((text, want, got))
    return failures

def run_tool_activity_checks():
    failures = []
    for text, want in TOOL_ACTIVITY_TEST_CASES:
        got = detect_tool_activity(json.loads(text))
        if got != want:
            failures.append((text, want, got))
    return failures

def run_error_text_checks():
    failures = []
    for text, want in ERROR_TEXT_TEST_CASES:
        got = match_provider_failure(collect_error_texts(json.loads(text)))
        if got != want:
            failures.append((text, want, got))
    return failures

def run_self_tests():
    failures = []
    for command, should_detect in SELF_TEST_CASES:
        detail = command_delegation(command)
        if (detail is not None) != should_detect:
            failures.append((command, should_detect, detail))
    for command, should_detect, detail in failures:
        print(f"selftest FAIL: {command!r} 期望命中={should_detect}，实际={detail!r}", file=sys.stderr)
    for mode, provider, problems in run_command_checks():
        print(f"selftest FAIL: 命令行 {mode}/{provider}：{problems}", file=sys.stderr)
        failures.append((mode, provider, problems))
    for stamp, provider_arg, peak_mode, want_provider, want_peak, got_provider, got_peak, reason in run_routing_checks():
        print(f"selftest FAIL: 路由 {stamp} provider={provider_arg} peak={peak_mode} "
              f"期望=({want_provider},{want_peak}) 实际=({got_provider},{got_peak}) 原因={reason!r}",
              file=sys.stderr)
        failures.append((stamp, provider_arg, peak_mode))
    for text, want, got in run_provider_failure_checks():
        print(f"selftest FAIL: 提供方故障匹配 {text!r} 期望={want!r} 实际={got!r}", file=sys.stderr)
        failures.append((text, want, got))
    for text, want, got in run_tool_activity_checks():
        print(f"selftest FAIL: 工具活动判定 {text!r} 期望={want} 实际={got}", file=sys.stderr)
        failures.append((text, want, got))
    for text, want, got in run_error_text_checks():
        print(f"selftest FAIL: 错误文本提取 {text!r} 期望={want!r} 实际={got!r}", file=sys.stderr)
        failures.append((text, want, got))
    if failures:
        print(f"run_subagent: 自测失败：{len(failures)} 项", file=sys.stderr)
        return 1
    total = (len(SELF_TEST_CASES) + len(ROUTING_TEST_CASES) + len(PROVIDER_FAILURE_TEST_CASES)
             + len(TOOL_ACTIVITY_TEST_CASES) + len(ERROR_TEXT_TEST_CASES) + 4)
    print(f"run_subagent: 自测通过（{total} 条纯函数断言：再委派判定、命令行形状、"
          "提供方路由、故障匹配、工具活动判定、错误文本提取）", file=sys.stderr)
    return 0

if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(run_self_tests())
    sys.exit(run(sys.argv[1:]))
