# 实验样本流转并发台

青屿检测样本中心（24 人）的样本移交复核工作台。原生 HTML/CSS/JavaScript 前端，Python FastAPI 后端，真实 PostgreSQL 两连接事务与行锁演练。

## 场景和模块

团队原先通过纸质交接单、SQL 截图和 Excel 交接。三处痛点：样本版本难统一、并发移交容易重复、超时后无法确认回执是否回滚。上游为样本接收记录，本站承接样本登记、移交申请、并发复核和证据导出，下游为理化检测室与复核员。

- 样本台账：批次、可用份数、保管位置，可录入。
- 流转申请：关联样本、指定数量和接收实验室。
- 并发执行：Redis 队列、独立 Python worker、可设锁等待上限。
- 冲突证据：Backend PID、SQLSTATE、回执回滚、数量守恒与 JSON 导出。
- 备份恢复：单事务同提交时点快照、隔离库断点续跑、三级对账通过才可用。
- 管理员审计：登录、写入和执行动作。

## 启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18242 ，后端 http://localhost:19242/docs ，数据库 localhost:17742。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@qingyu.test|Demo2026!|全业务写入、执行、审计|
|operator|operator@qingyu.test|Demo2026!|创建样本、申请、执行、导出|
|viewer|viewer@qingyu.test|Demo2026!|只读、导出；写 API 返回403|

## 一天剧本

09:00 operator 登录；09:05 复核 `QY-261117-A` 近海水质留样 120 份，位置冷藏间 A-03；09:10 查看申请 `MOVE-1117-01`，接收理化检测室 30 份；09:15 新建 `shift-20261117` 流转申请；09:20 以该申请发起 250ms 并发演练；09:25 查看 A 提交 30、B SQLSTATE 55P03 回滚，90+30=120；09:30 导出 `trial-1.json`；09:40 viewer 复核证据；10:00 admin 查看审计。

## 算法与业务边界

这是**真实数据库行锁的受控演练**。每次以样本可用量建立独立 `trial_balances` 镜像行，不扣减原台账。A 使用 `SELECT FOR UPDATE` 持锁并写未提交回执；Python threading.Event 确保 B 在 A 持锁期间运行。B 先写自己的回执，再以指定 `lock_timeout` 请求同一行锁；PostgreSQL 返回 SQLSTATE 55P03 后完整 ROLLBACK。B 结束事件释放 A，A 扣减 remaining、增加 received 并 COMMIT。没有随机结果、sleep 假等待或假 SQL 计划。

输入：流转申请、50–2000ms 锁等待上限。输出：实际 PID、错误码、等待耗时、剩余量、接收量、回执数和 SHA256。口径为 `remaining + received == initial`、remaining非负、仅A回执一条；B回执必须为零。等待耗时受主机负载影响，SHA包含PID和实际耗时因此每次不同。算法输入和最终不变量可复现。失败态包括申请量超限、数据库错误、线程协调超时、队列不可用；页面显示错误并允许创建新演练。

单 worker 顺序运行任务，无生产重试/崩溃恢复调度；worker中断时 running 任务需要人工重建，不能用作生产样本库存扣减。

## 备份与恢复（可恢复性验证）

备份页面（仅 admin，导航 05）用来定期确认样本库坏了还能恢复，规则如下：

- **同一提交时点**：一次备份在单个快照事务内导出全部 7 张业务表（PostgreSQL 用 `REPEATABLE READ` 固定快照）。备份期间样本登记、申请和演练照常并发，导出内容始终取自同一提交点，不会读到备份中途的新提交。
- **产物与校验**：每张表一个 JSONL 文件加 `manifest.json`，manifest 记录每表行数、列、SHA-256 与总校验和；备份任务状态为 running/completed/failed，失败留存原因。
- **隔离恢复库**：恢复目标永远是独立数据库（PG 默认同实例 `locklab_restore` 库，可用 `RESTORE_DATABASE_URL` 覆盖），主库不被改动。
- **演练处置口径**：快照时点已完成的演练保留全部回执（仅 A 一条）与镜像账；未完成（queued/running）的演练在恢复库改写为 `interrupted`、写入中断原因且**不再入队执行**。
- **断点续跑、幂等发布**：每表一个载入事务，提交后才把检查点写入主库 `restore_jobs.progress`；进程在中途重启后，对同一备份再次发起恢复即从断点续跑，已提交但未记检查点的表整体重载（先 DELETE 再 INSERT），不产生重复行。对外通知走 `restore_events` outbox（按恢复任务+事件键唯一），只有对账通过后才放行，重试不重复发布。
- **三级对账与可用性**：页面可对恢复任务发起验证；一级对备份文件 SHA-256，二级对各表行数与内容哈希，三级对业务不变量（数量守恒、完成演练回执齐全、中断演练无回执且有原因、无残留 queued/running）。全部通过恢复任务才显示 `available`，任一失败给出具体原因并保持 `failed`。
- 定时备份：设置 `BACKUP_INTERVAL_MINUTES`（分钟）后由后端守护线程按周期发起，0 表示仅手动触发。

无 Docker/PG 环境下，测试用文件 SQLite（WAL 模式提供同样的快照隔离语义）加假 Redis 跑通完整链路：`cd backend && python -m pytest tests/`。

## 工程说明

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|4业务页+登录，反向代理 /api/|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、持久化|
|worker|Python3.11/psycopg|真实双连接锁竞争|
|db|PostgreSQL16|业务数据、隔离镜像账本|
|redis|Redis7|持久队列|

目录：`frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}`；`backend/src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config}`；`backend/scripts/seed_demo.py`；`backend/migrations/001_initial.sql`；备份恢复测试位于 `backend/tests/`。
核心实体 Sample、Transfer、Trial 各有模型、schema、服务、路由、API、store与页面；共享 table/badge/frame 组件。共享枚举 roles/run states 位于后端 `src/constants/enums.py`、模型/权限引用和前端 `src/constants/enums.js`、frame/badge；演练状态枚举 queued/running/completed/failed/interrupted（interrupted 仅用于恢复库）。备份恢复实体 BackupJob、RestoreJob、RestoreEvent（outbox）由 SQLAlchemy metadata 启动时建表，服务在 `src/services/backup.py` 与 `src/services/restore.py`，隔离库配置在 `src/config/restore_db.py`，路由在 `src/routes/backups.py`（admin 限定）。两个后端中间件：request_id/errors。

环境变量见 `.env.example`：COMPOSE_PROJECT_NAME、三端口、DB_PASSWORD、JWT_SECRET、SEED_DEMO、BACKUP_INTERVAL_MINUTES（定时备份周期，0 为手动）。容器内另设 BACKUP_DIR=/artifacts/backups 与 RESTORE_DATABASE_URL（指向同实例 locklab_restore 库）。默认密码仅用于本地演示；JWT8小时有效。seed按自然键仅插入缺失行，不覆盖修改。所有5服务有健康检查，backend/worker依赖健康数据库/Redis，frontend依赖backend；db/redis/artifacts使用命名卷（备份产物在 artifacts 卷内）。停止及清空本项目数据：`docker compose down -v --remove-orphans`。

本地开发：Python3.11安装 backend/requirements.txt；设置 DATABASE_URL/REDIS_URL 后 `uvicorn src.main:app --reload`（backend目录）；worker `python -m src.worker`；前端用Nginx同源代理后端。前端双字体经Google Fonts加载，离线使用浏览器字体回退；业务不依赖外部字体。

## License
MIT，见 LICENSE。测试脚本、截图与实际结果位于源码外 `../../test-artifacts/gb-4103/`。
