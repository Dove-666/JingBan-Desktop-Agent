# 可选受控 Python 后台：离线原型

## 当前状态

**不可用，未接入 Agent、工具目录、桌面控制器或 Android。**

本轮只实现宿主编排、固定容器 helper 和离线 fake Docker 测试。本模块及这些测试未启动 Docker Desktop，未调用真实 `docker info`、`inspect`、`create` 等命令，未下载镜像，也没有在宿主执行模型 Python。宿主可在另一个验证阶段显式启动自己安装的 Docker Desktop；那不等于此能力已经可用。

`ContainerPythonRunner.capability()` 固定返回 `available=false`、`runtime_verified=false`、`file_exports=false`，错误码为 `CONTAINER_RUNTIME_UNVERIFIED`。默认 `DockerCLITransport(live_enabled=False)` 在创建临时目录和发送任何 Docker 命令前拒绝执行。`live_enabled=True` 仅为后续宿主隔离验收提供显式入口，不能据此把能力标为可用。

当前只规划返回有界 stdout/stderr。容器内文件放在有硬大小上限的 tmpfs 中，**不导出文件，不提供宿主可写挂载，不自动发布、不打开或执行输出**。`publish()` 无条件返回 `CONTAINER_FILE_EXPORT_UNAVAILABLE`。

## 与现有 JavaScript VM 的区别

现有 QuickJS 工具只计算 JSON 数据，不绑定 Python、文件系统、进程或网络对象。它是一个语言运行时边界，不代表宿主操作系统隔离。

此模块规划另一种执行环境：Windows 上 Docker Desktop 的 Linux engine。Python 可以使用镜像内标准库、读取明确交给它的副本，并在内存文件系统中计算；隔离依赖 Docker Desktop/VM、Linux 内核、容器运行时和实际生效的资源约束。fake 测试不能证明这些条件成立。

DeepSeek Harness 的 `SandboxProvider` 对每次调用携带文件策略、在不能执行约束时拒绝降级；其源码明确把同宿主文件约束与容器执行环境分开。这里采用这两个原则，没有复制其宿主 shell/ACL 执行链，也不把 Windows 文件 ACL 或 QuickJS 宣称为 Python OS sandbox。[服务源码](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox/src/index.ts)、[物理路径规则](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sandbox/sandbox/src/roots.ts)。

## 宿主 API 与权限

核心代码：`agent_backend/nanobot/container_execution.py`；固定入口：`container_entry.py`。

1. 宿主从配置提供 `ContainerPolicy.image_digest`，必须是仓库名加 `@sha256:` 的完整 digest；没有默认镜像，也没有模型参数可以替换镜像。
2. 宿主创建 `WorkspaceFiles`，取得明确授权输入的当前 SHA-256。
3. 宿主创建 `HostExecutionScope(conversation_id, generation, request_id, files, grant_revision=..., is_current=...)`。三个 ID 都须为非零规范 UUID，grant revision 为 SHA-256。scope 冻结并带随机宿主 nonce，不接受模型提供的 scope 对象。
4. `is_current` 必须读取宿主当前的会话、generation、request 和权限版本，使用线程安全的快照；不能根据模型的文本回答返回授权。
5. `execute(code, input_json, scope=..., authorized_inputs=[AuthorizedInput(...)], cancel_event=...)` 只接收代码、JSON 和宿主列出的普通文件。代码上限 64 KiB，stdin 上限 2 MiB；序列化前检查 JSON 类型、深度、节点和大小。

输入通过 `WorkspaceFiles.read_bytes` 读取；读取前拒绝命名路径中的 symlink/reparse point，拒绝硬链接、重复的大小写别名，并核对预期 hash。只把已验证字节复制到本次独占临时目录。**不挂载整个工作区、宿主 profile、Docker socket 或凭据目录。**副本与原文件之后的更改分离。

每次执行都创建新 scope/job/container，不复用上一次的 Python 解释器、挂载、工作目录或输出。模型 stdout 是不可信的引用数据，不能变成批准、工具参数授权或任务验收证据。

## 固定 Docker 控制面

CLI 路径来自宿主显式配置：`DockerCLITransport(docker_cli_path=绝对本机DockerDesktop路径)`；默认仅保留标准 Program Files 安装位置，不做 PATH、注册表、profile 或 Docker context 发现。只接受本机驱动器上的绝对 `docker.exe` 路径，拒绝 UNC、相对路径和 `..`。这不是模型参数；产品接入时必须由受信的宿主配置提供。endpoint 固定为：

```text
npipe:////./pipe/dockerDesktopLinuxEngine
```

不读取 `DOCKER_HOST`、`DOCKER_CONTEXT` 或当前 Docker context。每个 job 使用自己新建的空 client config；不加载个人 Docker 配置、认证 header、credential helper 或代理配置。子进程环境只保留必要 Windows 系统变量，并把 HOME/profile/temp 指向本次受控 client 目录。模型代码和 JSON 通过 stdin 传递，不放在 CLI 参数、环境、标签或日志中。[Docker CLI 环境与 context 规则](https://docs.docker.com/reference/cli/docker/)。

镜像只允许本地 `image inspect`，并检查 digest、Linux 类型、无镜像声明卷及已允许的静态环境变量。创建使用 `--pull=never`，不会为了缺失镜像连接 registry。镜像须由宿主之后单独构建/取得、检查和配置。

## 每个容器的约束

| 项目 | 配置与复核 |
| --- | --- |
| 网络 | `--network none`，不发布端口，不连接其他容器 |
| 根文件系统 | `--read-only` |
| Linux 权限 | `--cap-drop ALL`、`no-new-privileges=true`、非 root `10001:10001` |
| 进程/CPU/内存 | 有限 PID、CPU、memory 与相同 memory-swap 上限；CPU 时间、文件大小、文件描述符 ulimit |
| namespace | 默认私有 PID namespace，IPC none，cgroup private，拒绝 host/container PID mode |
| 宿主文件 | 唯一 bind 为本次 input 副本，read-only、rprivate |
| 临时目录 | `/tmp` 为 16 MiB tmpfs，noexec/nosuid/nodev |
| 输出目录 | `/aura/output` 为有限大小、有限 inode 的 tmpfs，noexec/nosuid/nodev；没有宿主 RW bind |
| 入口 | `/usr/local/bin/python -I -B /aura/input/.aura_container_entry.py` |
| 日志与常驻行为 | log-driver none、restart no、禁用镜像 healthcheck |

`create` 不启动代码。创建后由宿主独立 `inspect` 核对实际 ID、随机名字、owner/scope 标签，以及上表的关键字段，之后才允许 `start --attach --interactive`。tmpfs 选项使用闭合键值表，拒绝重复或冲突选项，不能通过追加 `exec`、更大的 size 或 root uid 改写前面的安全选项。字段不符就停止，不自动删去限制或回退宿主 Python。[Docker create](https://docs.docker.com/reference/cli/docker/container/create/)、[none 网络](https://docs.docker.com/engine/network/drivers/none/)、[资源上限](https://docs.docker.com/engine/containers/resource_constraints/)。

stdout/stderr 分别限长，返回前再次脱敏和限长；不返回 Docker 原始错误、宿主路径或镜像配置。程序退出只代表进程结束；`task_verified` 始终为 false。生成的 CSV/计算结果仍需要独立校验，不能把用户代码打印的“成功”当作验收。

## 停止、超时与清理

停止事件、总时间预算、stdout/stderr 超限或 scope 撤销都会中止 attached client，然后进入独立的清理流程。**杀掉 CLI 不等于停止容器。**

清理在已有可信 CID 时按 CID 查找该实例，不把它被改名当作消失；未知 CID 时才先用宿主随机名字发现实例。两种情况都核对 owner/scope nonce。删除使用这次 `inspect` 已验证的不可变 CID，避免外部管理员并发替换同名实例时误删；不会使用模型提供或未经核对的 create 响应 CID。随后分别按完整 CID 和精确随机名字成功查询并确认缺席，还必须确认 CLI 已 reap，才返回 `container_removed=true`。[Docker force removal](https://docs.docker.com/reference/cli/docker/container/rm/)。

以下情况返回 `CONTAINER_CLEANUP_UNCONFIRMED`，不发布任何数据，不把任务算作已停止完成：

- Docker daemon 不可达、`rm` 或确认查询失败；
- 同名对象不属于本次 nonce；
- create 请求中断后对象尚未出现，无法排除 daemon 稍后才完成创建；
- CLI 无法被确认 reap。

此时保留随机 owned 目录及最小 owner marker，供宿主之后 `retry_cleanup`。权限撤销仍允许宿主停止自己已拥有的容器，但禁止继续读取/发布任务数据。确认清理后，调用 `dispose` 才能删除经过物理路径、目录 identity 和 owner marker 再核对的独占目录。没有 prune、通配符删除或删除其他容器的路径。

## 文件导出为何禁用

宿主 RW quarantine bind 无硬磁盘总配额：轮询检查文件大小/数量不能阻止程序在下一次检查前写满宿主磁盘。因此本轮不采用这一方案。

未来只有在独立实测完成后，才考虑：将所有文件留在有限 tmpfs；成功后 pause 容器冻结所有子进程；以总字节数、文件数、深度和单文件大小都有硬边界的 TAR 流导入新的宿主 quarantine；拒绝绝对路径、`..`、symlink、hardlink、设备、FIFO、socket、稀疏膨胀及重复名称；再次核对 scope；最后通过 `WorkspaceFiles.write_bytes` 的 scope/hash/原子写路径发布。任何导出文件都不执行或自动打开。

**尚未验证 Docker cp/API 对 paused tmpfs 的可读性。**如果不能证明暂停期间能可靠读取并绑定快照，就继续 stdout-only，不能改回宿主 RW bind 来交付文件。[bind mount 的宿主写权限风险](https://docs.docker.com/engine/storage/bind-mounts/)。

## 真实启用前还缺的证据

- 固定本机 pipe 的 engine 身份与 Linux 类型，实际版本、运行时、seccomp/cgroup 支持；
- 固定 digest 镜像的内容、依赖、环境、入口和非 root 文件权限；
- Docker Desktop 返回的 Windows mount Source spelling、tmpfs/ulimit/namespace 字段与实际效果；
- 真实隔离下拒绝网络、宿主 profile/工作区/socket、root 写入和输入写入；
- PID/CPU/内存/tmpfs 限额、fork/输出洪泛、超时和用户停止；
- CLI 退出后容器仍运行、daemon 断连、迟到的 create、清理重试及宿主进程崩溃后的 orphan recovery；
- 并发请求、scope 撤销，以及同名/错误 CID 的独立负向验证。

Docker Desktop/VM/内核漏洞、拥有 Docker 管理权限的宿主软件、操作系统故障和供给链风险不由 Python wrapper 消除。一次 `inspect` 也不能证明内核真正执行了资源约束。这些证据未完成前，不能把该后台作为可用 Agent Python 能力，也不能宣称达到商业 Agent 的隔离水平。

## 本轮验证

运行 `python -m unittest agent_backend.nanobot.tests.test_container_execution -v`。测试使用内存 fake Docker 或被 mock 的 Popen，从未联系真实 daemon；覆盖固定 argv/env、hash-copy 输入、目录别名与硬链接、scope 隔离、inspect 约束漂移、tmpfs 冲突、错误 CID、foreign owner、取消/超时、迟到 create、客户端 reap、输出限长及禁止发布。

模块同时支持 package 导入和 worker 的 `-I` + HERE 显式路径导入。独立后端 Python 子进程只验证 unqualified `container_execution` 与 worker 的 `safe_files.WorkspaceFiles` 类型一致，默认 native gate 仍关闭；这一导入检查不是 Docker 或隔离运行验证。
