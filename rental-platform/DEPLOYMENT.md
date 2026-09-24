# 1Cat Gaudi 单卡出租平台交付说明

## 已实现的业务边界

| 需求 | 实现与验收依据 |
| --- | --- |
| 客户自行注册 | `/api/auth/register`，密码使用 scrypt 派生，前端提供注册页 |
| 注册余额 | 新客户注册初始余额为 0 元，需由管理员充值或兑换充值码后使用 |
| 独立管理员账户 | `deploy-production.sh bootstrap-admin`，管理员权限由数据库角色控制 |
| 管理员充值 | `/api/admin/recharge`，带幂等键，重复请求不会重复充值 |
| 固定计算规格 | 每台 1 张 Gaudi2、16 vCPU、62.5 GB（62,500 MB）内存、50 GiB 系统盘；客户只选择 0–200 GiB 数据盘 |
| 开机计费 | 开机成功后创建 usage ledger，按服务器费率扣余额 |
| 余额为零关机 | 后台结算将实例置为 stopping，后端确认关机并释放 GPU |
| 多客户/多实例 | 停止实例不占 GPU；每客户最多 1 个运行实例，平台最多 8 个活动槽位 |
| 资源不足 | SQLite 事务分配 8 个槽位，核算 CPU/内存与统一存储预留容量；另检查物理磁盘安全余量，失败返回 409 |
| SSH 入口 | 2221–2228 本机转发端口接入现有 1CatTunnel 公网端口 |
| 磁盘落盘 | 迁移完成后系统盘位于 Intel 的 `/mnt/nvme1/1cat-rental-system/instances`，数据盘位于 `/mnt/nvme1/1cat-rental-data/instances`；小体积控制状态、cloud-init 和 NVRAM 留原位 |
| 48 小时释放 | 无 GPU 占用的 stopped/error 实例超过 48 小时进入 deleting，确认虚机关闭后删除磁盘；UI 显示截止时间 |
| 控制服务重启 | 恢复本机转发，定期核对 VM 是否仍运行；检测到真实关机关闭算力账期，SSH 暂不可达不直接杀计算任务 |
| 账单和审计 | 客户只读自己的按日汇总费用，管理员读全平台账单、实例和操作审计；管理员列表不下发客户 SSH 密码 |

## 计费口径

固定包含 1 张 Gaudi2、16 vCPU、62.5 GB（十进制 62,500 MB）内存和 50 GiB 系统盘。8 台合计分配 500,000 MB，约 465.7 GiB，给 503 GiB 宿主机保留约 37 GiB。历史用户定价为 `4.00 元/小时`，但实际新开机价以数据库设置为准：2026-09-20 本次维护时后台为 `0.25 元/小时`，运行账期仍有 `1.00` / `4.00` 元锁定价。本次不重置运营价格或余额；页面逐实例显示实际费率，改价只作用于下次开机并记审计。

实例默认不带数据盘。客户选择的数据盘容量全部按 AutoDL 本地数据盘公开参考价 `0.0066 元/GB/天` 的七折，即 `0.00462 元/GiB/天` 计费。该存储费用从实例创建后开始累计，关机但未释放时仍计费，释放后停止。小于 1 分钱的部分累计到足够金额后扣除。

参考：<https://www.autodl.com/docs/local_disk/>。AutoDL 页面同时说明实际价格以平台页面为准，因此本项目把这个值作为当前测试平台的固定运营口径。

## 首次上线

### 充值 SDK / 一次性兑换码（2026-09-20）

这里的 SDK 是用户约定的“充值兑换码”，不是客户端开发包。管理员在管理中心的“充值 SDK 生成器”选择面额、数量、已收款/赠送类型、可选绑定客户，填写核验备注并确认后生成；客户在“充值与账单”输入兑换。

- 每张 0.01–10000 元，每批 1–100 张、总额不超过 100000 元。现金类必须填写收款编号或核验备注；生成不增加客户余额，也不是渠道收款证明。
- 码使用 128 位随机数；数据库只存 SHA-256 摘要、尾号与元数据。完整码仅首次响应返回，界面支持复制和下载，明文不存浏览器本地存储。
- 生成请求幂等；网络中断后同请求重试返回原批次元数据，不重复生成，也不会重新暴露明文。没有保存的码可按批次作废未兑换项后重新生成。
- 兑换、余额更新、流水与审计在同一写事务中完成。同客户重复提交返回已兑换结果，不重复充值；其他客户、绑定不符、作废等统一拒绝。仅客户能兑换。
- 每客户 10 分钟内最多 10 次无效尝试，限流状态写入数据库，进程重启不会清空。有效码按账户绑定或持码权限兑换。
- 未用码支持单张或整批作废；已兑换的码不能作废回收余额。无码自动失效期限，未用码在主动撤销前一直有效。
- 新流水分别为 `redeem_cash:<id>` 和 `redeem_gift:<id>`，保留已收款/赠送来源。余额仍用于统一算力/存储消费，本版没有自动退款，也没有将历史混合余额改成“可退现金”。后续退款必须增加消费分摊和现金冻结核算，不能直接退总余额。
- 列表每页 50 条，支持状态/客户/批次/备注筛选，汇总只代表充值码面额及兑换情况，不代表银行到账或财务收入。管理员历史人工充值入口保留。
- 生产验收只读，不发行或兑换真实可消费码。完整生成、兑换、重复与失败测试均使用隔离数据库；代码回滚保留新增表和现有账本，不恢复旧余额。
- 当前 HTTP 是既有测试部署选择。充值码是持有即能兑换的凭证，不可公开截图/群发，建议绑定账户和私密发放；正式对外收费应切换全站 HTTPS。

接口：`GET/POST /api/admin/recharge-codes`、`POST /api/admin/recharge-codes/<id>/revoke`、`POST /api/admin/recharge-code-batches/<id>/revoke`、`POST /api/rental/redeem-code`。生成需要 `X-Idempotency-Key`，全部接口遵守现有登录/同源保护；响应禁止缓存。包中必须包含 `recharge_codes.py`。

以下命令在服务器 root shell 执行。管理员密码只在隐藏提示中输入，不要写进环境文件、命令行、日志或聊天。

1. 将本发行目录放在服务器临时目录，例如 `/opt/1cat-rental-next-<版本>`。
2. 复制并编辑环境文件：

   ```bash
   install -d -m 700 /etc/1cat-rental
   install -m 600 rental.env.example /etc/1cat-rental/rental.env
   editor /etc/1cat-rental/rental.env
   ```

   必须确认 `RENTAL_IMAGE_ENABLED=1`、`RENTAL_RATE_CENTS_PER_HOUR` 为正数、镜像 manifest 存在；`RENTAL_DATA_ROOT=/mnt/nvme1/1cat-rental-data` 确实位于 `/dev/nvme1n1p1`；并填入八个 1CatTunnel 公网端口。

3. 检查发行包和配置：

   ```bash
   bash deploy-production.sh check
   ```

4. 执行带备份的安装：

   ```bash
   bash deploy-production.sh install
   ```

5. 初始化管理员并设置数据库费率：

   ```bash
   bash deploy-production.sh bootstrap-admin opsadmin
   bash deploy-production.sh set-price opsadmin 5.00
   ```

   不要在更新发布时重复设置价格；已上线平台以当前数据库运营价为准。仅在明确需要改价时使用管理中心确认设置。

6. 查看状态：

   ```bash
   bash deploy-production.sh status
   systemctl is-active 1cat-rental 1cattunnel libvirtd cloudstack-agent
   ```

## 验收顺序

使用隔离测试环境验证注册/赠金/充值/欠费关机，不把生产客户余额改成测试值。真实单卡验收用 `acceptance_slot8_real.py`，先确认第 8 卡空闲：新实例系统盘和数据盘均在 Intel，普通 gpu 用户可计算，/data 正常挂载，关机释放后 GPU 恢复主机。默认 0 GiB 不创建数据盘文件，但仍创建系统增量盘。测试后删除的仅为唯一命名的验收 VM 和对应临时路径。

## Intel 共享存储（2026-09-20）

- 已验收只读模板：`/mnt/nvme1/1cat-rental-images/gaudi-ubuntu24.04-repaired-20260919.qcow2`。
- 每个系统盘为 50 GiB 虚拟容量的稀疏 qcow2，只保存差异块；共享模板约 3 GiB，不为每实例复制一份完整模板。模板共享是原有能力，本次将模板/增量盘迁入 Intel 并统一容量管理。
- 统一规格预留上限 `RENTAL_STORAGE_POOL_BUDGET_GIB=3000`，系统+数据盘合计核算；创建与开机检查物理剩余至少 128 GiB。不是无限超卖；原先独立 700 GiB 系统盘预算不再限制约 14 台保留实例。
- 新建文件不立即占满其虚拟容量；已有 `discard=unmap`，宿主定期 TRIM 已启用。未给客户加入“在线缩小文件系统”这类风险功能。
- `storage_migrate.py` 默认只读 dry-run，应用前必须设置 `state/storage-maintenance`；在线采用浅层块复制/pivot，关机盘复制后逐块比较。`finalize_storage.py` 只在全部 live/inactive XML 路径一致后启用新环境。
- 维护门禁期间阻止客户新建和开关机/删除，余额结算、欠费关机仍生效；暂停 48 小时自动清理，避免搬盘与删除竞争。
- 迁移审计、环境备份和旧增量快照放在 `/mnt/nvme1/1cat-rental-migration-20260920`。旧快照只代表切换前时点，不包含切换后的新写入，不可直接当成实时回滚盘。

## 回滚

每次 `install` 会在 `/var/lib/1cat-rental/backups/<UTC时间>` 保存程序、环境文件、systemd unit 和 SQLite 状态快照。健康检查失败时脚本会自动恢复程序；手动回滚使用：

```bash
bash deploy-production.sh rollback /var/lib/1cat-rental/backups/<UTC时间>
```

代码回滚会重启控制服务，SSH 需重连，但不主动关闭 VM；刻意不恢复 SQLite，避免倒退余额、账本和实例历史。迁移后若备份后端/环境不认识 Intel 系统盘路径，脚本拒绝回滚；必须先用经过验证的反向块复制迁移实时磁盘，不能启用陈旧的源盘。

如仅需换回旧 UI，可以在 root shell 执行：

```bash
bash /opt/1cat-rental/deploy-production.sh ui-rollback /var/lib/1cat-rental/backups/20260920-041125-FxllCW
```

该操作只替换静态页面并保留被替换版本，不改后端、磁盘、环境、钱包，也无需重启 VM。刷新浏览器使旧界面生效。此恢复分支已在隔离部署故障测试中验证，不在生产为演示来回切换。

## 安全边界

### SATA 公共只读盘（2026-09-20）

- 技术：virtio-fs，经虚拟设备在母机内部共享，不经过 TCP/IP、公网或 1CatTunnel。
- 发布源：`/mnt/sata-raid0-3/public`；管理员在此维护 `models/`、`datasets/`，客户看到同一份文件。仅放可公开分发的资料，不放客户私有数据或凭据。
- 母机出口：`/srv/1cat-public-ro`，绑定源目录并强制 `ro,nosuid,nodev`。`1cat-shared-export.service` 在开机时验证 RAID UUID 后挂载；不对外开放新网络端口。
- 虚拟机：`/shared`，由镜像内 `1cat-shared-storage.service` 自动挂载；代码在旧实例下次开机后幂等补齐相同的托管文件，不重新执行 cloud-init 或磁盘格式化。
- 新默认镜像已切换到 `gaudi-ubuntu24.04-shared-20260920-v2.qcow2`，独立真实验收成功后置 validated=true。该镜像引用原 repaired-20260919 母盘，**不可删除旧母盘**，现有客户的底层引用也不改。
- 生产配置通过 `/etc/systemd/system/1cat-rental.service.d/20-shared-storage.conf` 加载单独的 `/etc/1cat-rental/shared-storage.env`；原 rental.env 的计费、隧道及凭据不改。
- 已运行实例不热改 RAM backing、不强制重启，显示待接入；正常关机并通过平台再次开机后生效。仅在虚拟机内 reboot 不能增加此前缺失的设备。
- 2026-09-20 14:40 已按用户授权将所有租赁实例正常关机，6 个现存域（12/14/20/21/25/31）的 inactive XML 全部补齐共享设备；32 尚未创建磁盘，沿用新模板。原配置备份 `/var/lib/1cat-rental/backups/shutdown-shared-20260920-1430`。客户随后自行启动 21，实测 `/shared` 为 `onecat-public virtiofs ro,nosuid,nodev`；其余实例下次面板开机自动安装挂载服务，不离线改写客户磁盘。
- `/shared` 与付费私有 `/data` 独立；模型 cache/lock/checkpoint 写在私有目录。遇到已有非空 `/shared` 或非托管同名配置，拒绝覆盖，先人工处理冲突。
- 发布采用不可变版本目录：在共享目录外暂存、校验后再发布，不覆盖正在读取的模型文件。RAID0 无冗余，公共资料应可重建/另有备份。
- 回滚使用本次专用 activation 脚本的 rollback 分支，仅恢复 backend/server 和服务配置；不恢复数据库、不关闭客户 VM、不删除任何镜像或公共文件。配置/代码备份在 `/var/lib/1cat-rental/backups/virtiofs-20260920`。回滚控制服务会使 SSH 转发连接需要重连。
- 本次切换命令：`sudo python3 /home/xinyi/1cat-virtiofs-20260920/activate-shared.py`；回滚在同一命令末尾加 `rollback`，先确认客户 SSH 会话已安排重连。回滚备份的 server 已更新为本次实施期间并发上线的充值码版本，不会撤掉充值功能。
- 实机验收：普通 gpu 用户64MiB mmap/哈希读取；root 写入/创建/删除/重命名/chmod/xattr 及 rw remount 后写入均 EROFS；16×16 HPU矩阵计算、重启后挂载和再次计算、无托管配置的存量 VM 补齐均通过。不是8卡满载或磁盘冷读带宽验收。

当前按用户要求使用 HTTP 测试；客户密码和 SSH 密码会通过 HTTP 传输，不适合直接面向公网正式商业运营。正式开放前应在 1CatTunnel 或反向代理层加 HTTPS，并限制管理端访问来源。
