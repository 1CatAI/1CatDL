# 1CatDL

面向单台 Gaudi2 服务器的轻量自助实例平台：客户注册、余额与账单、管理员充值及一次性充值码、GPU 直通、无头实例、SSH 公网入口和共享模型目录。

## 启动模式

| 模式 | vCPU | 内存 | 加速器 | 计算计费 | 同客户并发 |
| --- | ---: | ---: | --- | --- | --- |
| GPU | 16 | 62.5 GB | 独占一张 Gaudi2 | 管理员运营价，启动成功后锁价 | 最多 1 台 |
| 无头 | 2 | 4 GB | 无 | 固定 0.08 元/小时 | 可多台，受主机总资源限制 |

系统盘50 GiB，默认不带数据盘。可选0–200 GiB数据盘保持独立存储计费，关机未释放仍收费。关机后可切换启动模式，磁盘与环境保留。余额用尽自动关机；关机48小时未使用按平台规则释放。

内存使用十进制GB。无头模式不占GPU池，但仍占CPU、内存、磁盘和独立SSH入口；并非无限超售。当前每宿主8张GPU、默认16个无头入口。详见[无头模式](rental-platform/HEADLESS.md)。

## 目录

- `rental-platform/`：Python标准库HTTP服务、SQLite账本/调度、libvirt适配、测试和部署工具。
- `gaudi-status-panel/`：React/Vinext前端、管理界面、静态导出工具。
- `.github/workflows/check.yml`：隔离后端回归与前端类型/构建检查。

## 本地检查

需要Python 3.12+、Linux Bash（完整部署测试）和Node.js 22.13+。

```sh
cd rental-platform
python3 -m unittest discover -v
cd ../gaudi-status-panel
npm ci
npx tsc --noEmit
npm run lint
npm run build
```

生产静态文件不提交Git。构建后在另一个终端运行`npm run start -- --port 3100`，再执行`npm run export:static`，将`gaudi-status-panel/deploy/public`复制到`rental-platform/public`，与后端一起打发行包。

## 部署边界

现有部署脚本针对已经验收的Ubuntu/libvirt/Gaudi2宿主机，包含明确的挂载点、网络、预算和8卡约束。它不是通用的一键装机工具，也不包含可再分发的Gaudi镜像或厂商驱动。

1. 准备经过独立验收的只读母盘与对应`validated=true`的manifest；不要仅改标志绕过验收。
2. 以`rental.env.example`为模板配置宿主路径、资源预算和公网SSH映射。不要提交真实环境文件。
3. 使用`configure_headless_tunnels.py`追加无头入口，默认先dry-run；重启隧道后把9–24入口实际公网端口填写到环境配置。
4. 先运行`deploy-production.sh check`，再执行带备份的`install`。首次管理员通过`bootstrap-admin`的隐藏提示设置密码；升级不重设钱包或运营价格。
5. 现有virtio-fs共享存储配置由独立systemd drop-in加载，升级保留该配置。共享内容必须允许所有客户读取；客户私有内容不得放共享目录。

计费使用事务和幂等键；不足一分钱的用量跨开关机累积。生产回滚不得用旧数据库覆盖新余额与消费。已有无头实例时禁止回退到不支持无头的旧后端，可单独回退UI。

## 安全

仓库不包含管理员密码、SSH密码、隧道令牌、客户账本、虚拟磁盘或运行日志。测试中的固定字符串仅用于临时隔离数据库。对外正式服务应部署HTTPS、管理端访问限制、备份与审计；HTTP测试部署不提供传输机密性。

## 注册赠送额度

管理员登录 `/rental?view=admin`，在管理中心顶部的“注册赠送额度”填写每个新账户的赠送金额并确认保存。金额支持 0.01 元精度、上限 10000 元，设为 **0** 关闭；新安装或未配置时默认为 0，不擅自开启营销赠送。

配置持久化到 SQLite `settings.registration_bonus_cents`，修改记录审计。仅保存后新注册的客户按注册完成时的规则领取一次；旧账户不补发、不扣回，管理员账户不领取。创建账户、赠金入账与账单在同一事务完成，重复注册或重新登录不会重复发放。赠金账单类型为 `registration_bonus`，与实收充值区分。

注册页通过只读 `/api/auth/registration-policy` 显示当前赠送政策；管理员专用 `/api/admin/registration-bonus` 支持 GET 和 POST，POST 使用整数分 `cents` 和读取时的 `expectedCents`，拒绝非管理员、非法金额与过期覆盖请求。测试见 `rental-platform/test_registration_bonus.py`。

## 客户账户删除与恢复

管理员在管理中心的“客户账户管理”搜索账户，点击“删除账户”，输入完整账户名确认。仅客户账户可删除，管理员（含自己）不能删除；所有实例须先释放并等待删除完成，包括关机保留的实例。此操作不会自动关机、删除磁盘、退款或清零余额。

账户采用可恢复删除：立即撤销所有登录会话，禁止登录、下单、充值及兑换；历史账单、余额、已释放实例记录和账户名保留，不能同名重注册领赠金。在“已删除”列表可恢复，客户需重新登录；不重复赠送额度，也不恢复已释放的磁盘。绑定该账户的未兑换码保留，恢复后仍可使用；不自动解除绑定。

接口仅管理员可用：`GET /api/admin/customers?status=active|deleted|all`（默认正常账户，每类最近 500 个）；`POST /api/admin/customers/{name}/delete`，JSON `{ "confirmName": "完整账户名" }`；`POST /api/admin/customers/{name}/restore`。删除、恢复均记审计；事务保护删除与下单、登录、充值并发。数据库为增量字段迁移，不重建或清空用户表。

存在已删除账户时，部署脚本禁止回退到不支持账户删除的旧后端，避免重新开放登录；仍可单独回滚 UI。禁止绕过脚本直接换回旧控制器。测试见 `rental-platform/test_account_deletion.py` 和部署故障注入测试。

现有充值码代表平台余额入账机制，不是支付通道或自动退款系统。生产客户余额不能用于测试。发布前必须完成宿主机和隔离真实VM验收。

本仓库未授予额外开源许可证；第三方依赖遵循各自许可证。
