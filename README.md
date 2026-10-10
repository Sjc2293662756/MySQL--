# MySQL Multi-Server Traffic Generator

Python 命令行工具，用于随机访问多个 MySQL 服务器上账号可见的数据库和普通表，并生成可控查询流量。它不是压测工具。

## 安全范围

- 普通业务表的数据访问始终只执行 `SELECT`；启用事务批次时只额外发送 `START TRANSACTION`、`COMMIT` 或 `ROLLBACK`。表结构和库表清单也通过 `information_schema` 的 `SELECT` 获取，不会对业务库执行 DML 或 DDL。
- 默认模式不会创建数据库或修改任何数据。
- 只有显式运行 `--init-test-db` 或 `--test-crud` 才会创建专用测试库、测试表和合成数据。
- 测试模式中的 `SELECT`、`INSERT`、`UPDATE`、`DELETE` 只作用于带有本工具所有权标记的测试库内固定测试表。遇到同名但没有匹配标记的现有库时，程序会拒绝写入。
- 程序不会自动删除测试库。需要清理时，请由数据库管理员确认目标库后在工具外处理。
- 建议使用专门账号：业务库只授予所需 `SELECT` 权限；测试模式还需要创建专用数据库和在该库内创建表、读写数据的权限。权限由 DBA 在项目外配置，程序不会执行 `GRANT`。

## 安装

需要 Python 3.10+、PyMySQL 和 PyYAML：

```powershell
py -3 -m venv .venv
\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

## 配置多台服务器

在 `config.yaml` 的 `servers` 列表中为每台服务器添加一项。程序会随机选服务器、可见数据库和其中的普通表。账号只能发现其权限允许查看的数据库与表。

```yaml
servers:
  - name: mysql-238
    host: 101.254.114.238
    port: 3306
    user: traffic_user
    password: "替换为密码"
  - name: mysql-secondary
    host: 192.0.2.20
    port: 3306
    user: traffic_user
    password: "替换为密码"
```

旧版单服务器 `mysql:` 配置仍可使用；它会被转换成一台服务器配置。不要把真实密码提交到版本库，`config.yaml` 已在 `.gitignore` 中。

## 命令

在项目目录运行：

```powershell
\.venv\Scripts\python.exe traffic.py --check
\.venv\Scripts\python.exe traffic.py --list-tables
\.venv\Scripts\python.exe traffic.py
```

- `--check`：对每台服务器执行 `SELECT 1` 后退出。
- `--list-tables`：列出每台服务器上账号可见的数据库和普通表后退出。
- 默认运行：持续随机访问这些业务表，只执行 SELECT。按 `Ctrl+C` 停止。
- `--inspect`：读取并显示所有可见普通表的列信息。

## 测试库 CRUD

先只创建并初始化测试库：

```powershell
\.venv\Scripts\python.exe traffic.py --init-test-db
```

需要在持续流量中加入随机 CRUD 时，显式运行：

```powershell
\.venv\Scripts\python.exe traffic.py --test-crud
```

每台服务器会使用 `test_sandbox.database_prefix` 配置生成一个独立库名，并创建所有权标记表和 `traffic_test_data`。如果是首次创建，会插入 `seed_rows` 条合成测试数据。业务库的查询流量仍然只执行 SELECT。测试模式每个 Worker 以 `traffic_probability` 的概率选择测试库操作，并按 `operation_weights` 在 SELECT、INSERT、UPDATE、DELETE 中随机选择；空表时会自动选择 INSERT。

程序不会删除测试库或表。已有同名前缀数据库只有在所有权标记与当前服务器匹配时才会复用；否则初始化停止，不会对该库写入。

## 日志

默认写入控制台、`logs/traffic.log` 和 `logs/traffic.jsonl`。事件包含服务器名、数据库、表、SQL、参数、耗时、行数和结果；测试库初始化及 CRUD 语句也会记录。日志可能包含业务表抽样得到的参数值，请按数据敏感级别保护日志。数据库密码不会被记录。

更完整的配置和排障步骤见 [USAGE.md](USAGE.md)。
