# MySQL 多服务器流量工具使用手册

## 1. 行为和安全边界

- 普通业务表的数据访问、库表发现和列信息读取均使用 `SELECT`；启用查询事务时额外使用 `START TRANSACTION`、`COMMIT` 或 `ROLLBACK`。不对业务库执行 INSERT、UPDATE、DELETE 或 DDL。
- 默认启动：随机选择配置中的服务器、账号可见数据库和普通表，持续执行 SELECT。系统库 `mysql`、`information_schema`、`performance_schema`、`sys` 不作为业务流量目标；视图也不作为目标。
- 测试库写入：只有运行 `--init-test-db` 或 `--test-crud` 时才会创建测试库、表并写入合成数据。`--test-crud` 还会随机执行测试表 SELECT、INSERT、UPDATE、DELETE。
- 测试数据写入仅使用生成的测试库名和固定表 `traffic_test_data`。该库必须有本工具匹配的所有权标记，名称相同但标记缺失或不匹配时拒绝写入。
- 工具不会自动删除测试库。清理需由管理员核对库名后，在工具外完成。
- 测试库的创建和写入需要账号有相应权限。不要为了方便授予账号业务库的写权限；应让 DBA 单独配置需要的权限范围。

## 2. 安装

Windows PowerShell：

```powershell
cd G:\my_file\项目测试\MySQL-流量工具\mysql-traffic-generator
py -3 -m venv .venv
\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

已有虚拟环境时，跳过建环境和复制配置步骤。Linux/macOS 使用 `python3 -m venv .venv`、`.venv/bin/python` 和 `cp config.example.yaml config.yaml`。

## 3. 配置多台服务器

编辑 `config.yaml` 的 `servers`，每个服务器都要有唯一名称、地址、端口和登录账号：

```yaml
servers:
  - name: mysql-238
    host: 101.254.114.238
    port: 3306
    user: traffic_user
    password: "替换为该服务器的密码"
    charset: utf8mb4
    connect_timeout: 5
    reconnect:
      enabled: true
      interval_seconds: 5
      max_retries: 0
  - name: mysql-secondary
    host: 192.0.2.20
    port: 3306
    user: traffic_user
    password: "替换为该服务器的密码"
```

每个服务器可以使用不同账号和密码。账号能发现哪些库表，取决于 MySQL 授权和 `information_schema` 对该账号的可见范围。发现结果不等于账号一定对每张表有 SELECT 权限；运行时的权限错误会记录下来。

旧配置中单个 `mysql:` 区域仍兼容，会自动作为一台服务器使用。如果要增加服务器，建议改用 `servers:` 列表。不要提交真实密码；`config.yaml` 已被 Git 忽略。

## 4. 连通性和库表清单

所有命令都在项目目录运行：

```powershell
\.venv\Scripts\python.exe traffic.py --check
\.venv\Scripts\python.exe traffic.py --list-tables
```

- `--check` 对每台配置服务器分别执行 `SELECT 1` 并退出。
- `--list-tables` 查询每台服务器上账号可见的数据库及普通表，并退出；不会启动 Worker，也不会创建测试库。
- `--inspect` 读取并显示所有可见普通表的列信息和行数估算。表很多时会输出较多内容。

## 5. 只读流量

```powershell
\.venv\Scripts\python.exe traffic.py
```

程序随机选择服务器，再从该服务器上随机选择数据库和普通表，按配置产生 SELECT。普通查询带 LIMIT；COUNT 受 `count_max_estimated_rows` 限制。数据库和表名来自元数据并经过反引号转义，用户数据值使用参数绑定。每个 Worker 独立连接，运行期间按 `Ctrl+C` 停止。

流量参数示例：

```yaml
traffic:
  workers: 3
  mode: interval       # interval 或 qps
  interval:
    min: 1
    max: 3
  qps: 5

query:
  limit_values: [1, 5, 10, 20, 50]
  enable_count: true
  count_max_estimated_rows: 100000
  max_execution_time_ms: 3000

transactions:
  enabled: true
  min_queries: 2
  max_queries: 3
```

事务批次中只对业务库执行 SELECT。MySQL 5.1 不支持 `START TRANSACTION READ ONLY`，因此建议业务账号本身只拥有 SELECT 权限。

## 6. 创建测试库和随机 CRUD

测试库使用独立配置，不会使用现有业务库名：

```yaml
test_sandbox:
  database_prefix: codex_traffic_test_
  seed_rows: 20
  traffic_probability: 0.25
  operation_weights:
    select: 25
    insert: 25
    update: 25
    delete: 25
```

生成的库名由此前缀、服务器名称和服务器地址摘要组合而成；每台服务器各自使用一个库。工具创建所有权标记表及 `traffic_test_data` 表，并在空表中放入 `seed_rows` 条合成记录。业务库依然只执行 SELECT。

只初始化并退出：

```powershell
\.venv\Scripts\python.exe traffic.py --init-test-db
```

初始化测试库并开始混合流量：

```powershell
\.venv\Scripts\python.exe traffic.py --test-crud
```

混合运行时，每次选中服务器后以 `traffic_probability` 的概率进入该服务器的测试库；再根据 `operation_weights` 随机选测试表 SELECT、INSERT、UPDATE 或 DELETE。其余轮次随机查询该服务器上的业务库表。如果该服务器没有可见业务表，则可用测试库继续产生流量。

账号需要能够连接服务器、读取可见业务库以及创建专用测试数据库；在测试库内需要建表、读写权限。如果权限不足，初始化会报错并停止。工具不会尝试执行 GRANT，也不会通过别的库名回退写入。

## 7. 日志与统计

默认写入：

- `logs/traffic.log`
- `logs/traffic.jsonl`
- 控制台

日志包含服务器名、数据库、表、实际 SQL、参数、耗时和执行结果。初始化测试库时执行的 CREATE、标记写入和种子数据写入也会记录。测试 CRUD 的事务控制语句和增删改语句会被记录。密码不会写入日志；业务查询的参数可能来自真实字段值，需保护日志文件。

可以在配置中控制日志：

```yaml
logging:
  level: INFO
  console: true
  file:
    enabled: true
    path: logs/traffic.log
  jsonl:
    enabled: true
    path: logs/traffic.jsonl
```

统计摘要输出运行时间、QPS、成功/失败数、延迟、主要表和查询类型。测试 CRUD 事件以 `sandbox_select`、`sandbox_insert`、`sandbox_update`、`sandbox_delete` 分类。

## 8. 常见问题

- **只显示一台服务器**：检查 `servers` 列表是否包括所有地址，名称不能重复。
- **只发现少量数据库**：程序只列出账号有权查看的 schema。请 DBA 确认 SELECT 元数据/表权限，而不是改程序绕过权限。
- **库表能列出但 SELECT 报权限错误**：账号可能只能查看 schema 元数据，没有对应表的 SELECT 权限；程序会在日志记录错误并继续其他目标。
- **测试库初始化报 CREATE DATABASE 权限错误**：该账号没有建库权限。请由 DBA 单独配置创建专用测试库及其内部读写所需权限；程序不会自动申请或授予权限，也不会接管无所有权标记的同名空库。
- **同名前缀数据库未被接受**：为了避免误写已有库，只有包含匹配所有权标记的测试库才可复用。不要手动创建一个同名空库期待程序自动接管。
- **连接中断或超时**：检查对应服务器网络、端口、负载和账号连接限制。每个服务器都有独立重连配置；设置 `max_retries: 0` 表示持续重试。

