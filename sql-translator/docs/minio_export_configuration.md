# SQL 大结果导出的配置接入

## 配置来源

`data_exporter.py` 使用进程环境中的 `MINIO_*` 配置。
`runtime_config.load_workspace_env()` 仅尝试补充 **SQL 源码目录上一级的 `.env`**，不会覆盖已有进程环境。
它不会扫描其他服务目录，也不会把 `DATA_AGENT_MINIO_*` 自动转换成 SQL 使用的键名。

本地源码放在共享工作区时，上级 `.env` 可能已经包含 `MINIO_*`，因此可以直接导出。
把 SQL 服务迁移到独立目录后，如果上级 `.env` 不存在、启动配置又只包含数据库和 Redis，
导出器就会落到默认本机地址和空凭据。数据库查询及健康检查仍可能成功，但附件上传失败。
因此“健康接口正常”不能替代附件导出验收。

## 配置键映射

如果权威共享配置只有 Agent 前缀，部署时应将同一组已确认有效的配置接入 SQL 的 EnvironmentFile：

| 共享配置 | SQL 服务配置 |
| --- | --- |
| `DATA_AGENT_MINIO_ENDPOINT` | `MINIO_ENDPOINT` |
| `DATA_AGENT_MINIO_ACCESS_KEY` | `MINIO_ACCESS_KEY` |
| `DATA_AGENT_MINIO_SECRET_KEY` | `MINIO_SECRET_KEY` |
| `DATA_AGENT_MINIO_BUCKET` | `SQL_EXPORT_MINIO_BUCKET` |
| `DATA_AGENT_MINIO_SECURE` | `MINIO_SECURE` |
| 已有公网/用户可访问下载地址 | `MINIO_PUBLIC_ENDPOINT` |

没有独立下载地址时，仅在验证访问可达后使用存储 endpoint 对应的 HTTP(S) 地址。
不能使用默认本机地址作为用户下载地址，也不要为修复配置接入问题将存储桶改成公开写入。
通用 `MINIO_*` 与 Agent 配置可能属于不同存储，不能未经核对混用地址、账号和密钥。

## 发布与验收

1. 使用 `systemctl show sql-translator.service -p EnvironmentFiles -p WorkingDirectory` 核对真实配置入口。
2. 核对进程是否加载必要键，只输出是否有值，不输出密码、密钥或完整环境。
3. 修改前备份原文件；配置与备份均只允许服务管理者读取。保留非 MinIO 配置，逐项比较，不覆盖整个共享环境。
4. 重启 SQL 服务以更新导出器的模块级配置和缓存客户端；修改文件而不重启不会改变已运行服务。
5. 用临时合成数据测试超过 200 行的导出：预览应为 20 行、总数保持真实条数、Excel 应包含完整行、下载应成功。
6. 验证后仅删除本次测试创建的对象，不删除业务导出文件。不要为了验证导出而执行真实业务 SQL。
7. EnvironmentFile、密钥和备份不提交 Git、不随源码整体覆盖。后续部署保留这些受限运行配置。

## 2026-09-24 修复记录

- PROVEN：当前 SQL 运行环境没有 MinIO 键，独立目录的上级 `.env` 不存在；SQL 运行配置仅有数据库和 Redis 键。
- PROVEN：共享配置中的 Agent MinIO 配置仍存在，带凭据访问存储桶成功，现有文件的下载检查为 HTTP 200。
- 未发现“本次推送删除了配置文件”的证据；已确认的是部署目录/键名与配置接入不一致。
- 将六个 SQL MinIO 键接入现有受限运行配置，备份原配置。原数据库和 Redis 配置逐项保持不变，没有改存储桶 ACL/策略，也没有创建或提升账号权限。
- 仅重启 SQL 服务：启动时间 2026-09-24 01:15:09 CST；PID 从 2051085 变为 560012，健康检查通过，进程环境确认加载了预期配置。
- DataAnalysis 和 Oagnet 的 PID、启动时间及状态保持不变。本次没有部署上一轮的结果预览容错代码。
- 重启前后健康接口的 SQL 核心指纹发生变化：翻译器从 `b4236aed7dd69f03` 变为 `d50038738e0ea822`，Scope 从 `ac3106d79b1af9ab` 变为 `cdaf1d58118c9b74`；API 指纹保持 `e9f46282dbb52f50`。这些指纹在进程启动时计算，说明重启加载了服务器上已有的文件版本；新指纹与本地通过 463 项测试的对应文件一致。本次没有上传或修改这些 Python 文件，不能将“仅修改配置”误解为进程继续持有旧代码。
- 真实导出冒烟：使用新 SQL 进程环境、线上导出模块和 201 行合成数据，返回 20 行预览；下载 HTTP 200；读取 Excel 验证完整 201 行；临时对象已删除并确认不存在。没有执行真实业务 SQL。
- 离线 SQL 全量：463 passed；网络隔离入口 `scripts/run_offline_tests.py` 也全部通过，无业务代码变更。
- Current Stage：SQL 附件配置接入恢复；当前目标无剩余配置阻塞。Catalog/Evaluation/Shadow 门禁与 V1 Replacement Readiness 不变，无生产路由切换，无自动合并。
- 下一步：以新请求确认页面的真实业务附件；上一轮尚未部署的容错代码保持独立发布，不与此次配置修复混淆。
