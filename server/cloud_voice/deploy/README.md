# 可选云配音服务部署

本目录提供 Linux 上独立部署账号、字数、兑换码、配音任务及经营管理服务的参考文件。桌面软件和系统配音可以免费独立使用，不依赖这些服务器服务。云配音需要部署者自行配置供应商账号、计费保护和 HTTPS 入口。

这些文件是部署模板和隔离验收工具，不表示任何目标服务器已完成部署或验收。部署前应根据自己的系统、目录、证书和网络环境核对配置，不覆盖已有服务或数据库。

## 文件与运行限制

| 文件 | 用途 |
| --- | --- |
| `production.py` | 客户端 API 的单进程 Waitress 入口，无 fork、预加载或热更新 |
| `operator_production.py` | 独立经营管理进程；核对受限配置、数据库和预期 HTTPS 地址 |
| `requirements.txt` | 客户端 API 所需的固定 Waitress 版本 |
| `requirements-operator.txt` | 独立经营管理和只读余额查询所需依赖 |
| `yijianjifa-cloud-voice.service` | 客户端 API 的 systemd 模板 |
| `yijianjifa-cloud-voice-operator.service` | 经营管理的独立 systemd 模板 |
| `service.env.example` | 不含真实密钥的环境变量示例 |
| `qa_http.py` | 仅使用回环端口和临时数据库的隔离 HTTP 验收 |
| `qa_stop.py` | 在途请求停止探针，检查排空和数据库锁 |

入口支持 Python 3.12/3.13，要求 Waitress 3.0.2。客户端 API 默认仅监听 `127.0.0.1:18769`；隔离验收使用 `127.0.0.1:18770`。经营管理默认使用回环端口 `18771`，与客户端 API 分开运行。不要将这些进程直接暴露到公网，也不要启动多个工作进程或针对同一数据库启动第二个 API 实例。

客户端 API 配置为 8 个 HTTP 线程、32 个连接上限、32 个 backlog、1 MiB 请求体、16 KiB 请求头及 90 秒通道空闲限制。应用另有来源、账号限速和配音并发控制；HTTP 线程数不等于配音并发量。默认不记录访问日志、请求体、令牌或异常详情。

systemd 模板中的内存、CPU、启动重试及目录防护限制必须在目标系统上验证。`CPUQuota=50%` 表示半个 CPU 的运行时间配额。不能仅凭模板宣称资源限制已经生效或承诺用户容量。停止窗口为 120 秒，用于结束请求及释放数据库锁。

## 目录与权限

参考布局如下；安装前确认路径没有被其他服务使用：

```text
/opt/yijianjifa-cloud-voice/runtime/          # 独立且已校验来源的 CPython
/opt/yijianjifa-cloud-voice/releases/<id>/    # server/cloud_voice 代码
/opt/yijianjifa-cloud-voice/current           # 管理员维护的版本软链接
/opt/yijianjifa-cloud-voice/venv/             # 客户端 API 的独立虚拟环境
/opt/yijianjifa-cloud-voice/operator-venv/    # 经营管理的独立虚拟环境
/etc/yijianjifa-cloud-voice/service.env      # root:root 0600
/etc/yijianjifa-cloud-voice-operator/         # 经营配置及只读余额查询配置
/var/lib/yijianjifa-cloud-voice/              # 服务专用 SQLite、WAL、SHM 和进程锁
/var/backups/yijianjifa-cloud-voice/          # 受限备份目录；异地备份另行配置
```

创建禁止交互登录的专用系统用户和组 `yjjfvoice`。代码、运行时和虚拟环境由管理员维护，服务用户只读，不以 root 或其他业务的账号运行。数据目录由 `yjjfvoice` 拥有并设置为 `0700`，不能放入网站目录；单元使用 `UMask=0077`。

`service.env` 由 systemd 读取，保持 `root:root 0600`。其父目录可设为 `root:yjjfvoice 0750`。需要由服务读取的音色清单可设为 `root:yjjfvoice 0640`，不要把供应商密钥放在音色清单中。经营配置的所有者、父目录权限和文件权限必须满足 `operator_production.py` 的校验要求。

所有真实密钥、口令、令牌及余额查询凭证均保存在仓库外的受限配置中，不进入 Git、网页目录、命令参数或终端输出。不要从其他服务复制凭证。供应商配音凭证与只读费用查询凭证分开管理。

`current` 内应含 `pyproject.toml`、`yijianjifa_cloud_voice/` 和 `deploy/`。服务器不需要安装桌面端的 Qt、浏览器或 FFmpeg 依赖。

## 安装与启动

1. 核对目标系统架构、glibc、磁盘、内存和端口占用。使用独立 Python 运行时或目标系统支持的独立虚拟环境，确认 Python 版本、SSL/CA、SQLite WAL/UPSERT 和 `fcntl` 可用，不替换系统 Python。
2. 创建专用虚拟环境，仅安装本服务需要的依赖。客户端 API 通过 `requirements.txt` 安装锁定的 Waitress；经营管理使用自己的依赖清单和虚拟环境。核对所用软件包的来源与完整性。
3. 把服务源码放入新的版本目录，核对所有者和权限后设置 `current`。systemd 模板通过明确的 `PYTHONPATH` 加载源码；路径与实际目录必须一致。所有运维命令使用明确的解释器路径。
4. 在仓库外创建受限 `service.env`，配置独立数据库和适合目标系统的 CA 文件。若只验收账号与字数功能，供应商相关选项全部留空；配音请求将返回 `service_not_configured`，这是未配置供应商的正常状态。供应商配置必须完整，见下文。
5. 使用独立服务用户和解释器运行 `qa_http.py`，再运行 `qa_stop.py`。确认验收端口已释放，不把临时测试数据库、账号或兑换码迁入正式数据目录。
6. 根据目标系统核对两个 systemd 模板。使用 `systemd-analyze verify` 检查语法、目录、解释器和用户；只加载本次新增的单元。先启动需要的服务，观察运行情况后再决定是否启用开机启动。不要启用多 worker 或热重载。
7. 使用目标系统的端口及进程工具确认只监听回环地址。客户端 API 的 `/missing` 应返回 404 和 `no-store`；无令牌请求 `/v1/me` 应返回 401。健康检查不需要创建账号或提交付费配音。
8. 核对实际用户、单进程状态、数据库锁、cgroup 资源限制和重试限制；同时确认其他服务保持正常。记录峰值资源消耗，用实际负载验证容量，不简单累计 VSZ/RSS 作为物理内存占用。

这些模板保留对较旧 systemd 的兼容写法，包括 `[Service]` 中的 `StartLimitInterval` / `StartLimitBurst` 和 `MemoryLimit`。不同系统支持的指令和 cgroup 行为可能不同；出现不兼容时应明确调整并重新验收，不默默移除限制或关闭整机防护。

经营管理入口还会校验预期数据库、来源和固定端口。自行部署时应先核对 `operator_production.py` 的部署默认值与受限配置是否匹配，不能只复制一个 unit 就认为管理页可以使用。

## 隔离 HTTP 验收

以下示例假定采用上面的目录布局，以专用服务用户运行：

```sh
runuser -u yjjfvoice -- env PYTHONPATH=/opt/yijianjifa-cloud-voice/current /opt/yijianjifa-cloud-voice/venv/bin/python /opt/yijianjifa-cloud-voice/current/deploy/qa_http.py
```

脚本不接受自选地址或生产数据库参数。它临时移除供应商配置，仅绑定 `127.0.0.1:18770` 并创建新临时数据库。密码、令牌和兑换码在内存中生成，不输出；端口被占用时直接退出，不停止占用进程。

验收覆盖注册、登录、测试兑换、重复请求与跨账号保护、账本、供应商未配置提示、Origin 拒绝、请求体上限、并发只读请求、同库第二实例拒绝、一致性备份及恢复读回。测试额度不代表实际收款。脚本不验证真实供应商、真实支付或公网 HTTPS，也不应为通过测试而放宽正式认证限速。

`qa_http.py` 结束并释放端口后，以相同解释器和 `PYTHONPATH` 运行 `qa_stop.py`。它仅启动自己的临时子进程并发送延时只读请求，检查停止时在途工作排空和数据库锁释放。停止事件循环后不能保证客户端收到完整响应；业务操作遇到结果不明确时应先核对原结果，避免重复提交。

## 供应商与 HTTPS 配置

供应商鉴权二选一：新版只设置 `YJJF_VOLC_API_KEY`；旧版同时设置 `YJJF_VOLC_APP_ID` 和 `YJJF_VOLC_ACCESS_KEY`（Access Token）。混填或缺项会拒绝启动。两种方式均还需要 `YJJF_CLOUD_VOICES_FILE` 和 `YJJF_CLOUD_MAX_DAILY_WORDS`，用于明确核准音色和每日提交上限。

公网接入需要由部署者配置受信证书、HTTPS 反向代理、可信代理范围和请求来源校验。仅在反向代理明确覆盖 `X-Real-IP` 等来源字段后配置可信代理，不能信任公网请求自行提交的转发头。供应商余额告警、费用上限、少量真实配音及账单核对均应单独验收。

源码桌面端默认不连接收费云服务。自行部署或官方构建需要显式指定服务地址，详见[配置说明](../../../docs/CONFIGURATION.md)。配置入口、服务运行、客户端接入、供应商计费与最终用户体验是不同验收项；部署模板不代表它们已经通过。

## 一致性备份与恢复

备份包括账号资料、凭证哈希、字数账本和配音文本，必须按敏感资料保护。不要仅复制运行中的 `voice.db`；使用 SQLite backup API 生成新的受限文件，或停止本服务后复制完整数据库。备份不可放入公开站点、Git 或普通下载目录。

下面示例要求先核对路径，提前创建权限为 `0700` 的备份目录，并使用一个尚不存在的输出文件：

```sh
/opt/yijianjifa-cloud-voice/venv/bin/python - <<'PY'
import os, sqlite3
from pathlib import Path
source = Path('/var/lib/yijianjifa-cloud-voice/voice.db')
backup = Path('/var/backups/yijianjifa-cloud-voice/voice-YYYYMMDD-HHMMSS.db')
if not source.is_file():
    raise SystemExit('source database is missing')
fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('backup integrity failed; do not overwrite or retry blindly')
print('backup complete; account data was not printed')
PY
```

备份完成后核对文件哈希、权限和恢复结果，另行配置受限异地副本。恢复前停止使用该数据库的服务，保留原数据库、WAL、SHM 及私有目录作为回滚副本，确认没有持锁进程。将已验证备份放入新的私有数据目录，通过受限配置指向新库；不要在运行时覆盖主库或删除锁文件。核对余额、已用及预留字数和成功任务后，再恢复业务访问。

## 回滚

- 出现启动失败、资源限制或影响其他服务时，只停止本次部署的云配音或经营管理服务，确认对应监听已消失，保留数据和受限配置。
- 回退代码前先停相关服务，恢复 `current` 到已验证版本。数据库迁移不兼容时，应从一致性备份恢复到新目录，不能让旧代码直接写入不兼容的新库。
- 不删除系统用户、运行时、数据库、兑换记录或账本，也不为回滚本服务而重启其他服务或整机。
- 单元变更后重新加载对应配置，恢复后核对只读路由和数据。对于结果不明确的发码、配音或扣费操作，先查询原结果，不盲目重试或重置额度。
