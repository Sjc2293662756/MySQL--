# 237 服务器 Docker 使用说明

这份文档用于管理已经部署在 **101.254.114.237** 上的 MySQL 流量脚本。以下 Docker 命令都在登录服务器后执行。执行 `docker compose` 命令前，先进入本服务目录。

## 1. 当前部署

| 项目 | 当前设置 |
| --- | --- |
| 登录账号 | `netinside` |
| Compose 服务名 | `traffic` |
| 容器名称 | `mysql-traffic-generator` |
| 镜像版本 | `mysql-traffic-generator:1.0.4` |
| 服务目录 | `/home/netinside/docker-images/mysql-traffic-generator/service` |
| 运行方式 | Docker Compose 管理的后台容器，关闭 SSH 窗口后继续运行 |
| 访问目标 | 当前配置中的 mysql-238、mysql-239、mysql-240 |
| 连接方式 | 三台均使用明文 MySQL，`ssl_disabled: true` |
| 运行模式 | 业务库 SELECT + 专用测试库 CRUD，启动参数 `--test-crud` |
| 流量设置 | 3 个 Worker，每次查询后随机等待 1～3 秒 |
| 日志时间 | 北京时间，UTC+8 |
| 自动重启 | `unless-stopped`；手动停止后需要手动启动 |

服务器的 Docker 服务已启用开机启动。保持本容器运行时，Docker 服务恢复后会按重启策略恢复容器。本次部署已启用测试库 CRUD 模式。

每个 Worker 默认以 25% 的概率选择测试库操作；测试库内 SELECT、INSERT、UPDATE、DELETE 的权重相同。业务库继续保持只读。测试库第一次启用时会创建固定测试表并写入 20 条合成数据，之后持续随机执行 CRUD。

| 服务器 | 专用测试库 | 数据表 |
| --- | --- | --- |
| 238 | `codex_traffic_test_mysql_238_20d15c4e22` | `traffic_test_data` |
| 239 | `codex_traffic_test_mysql_239_19993cad5f` | `traffic_test_data` |
| 240 | `codex_traffic_test_mysql_240_d52e3af9dc` | `traffic_test_data` |

在监控产品中可按以上数据库名筛选，查看 CREATE DATABASE、CREATE TABLE 以及测试表的增删改查。建库、建表只在首次初始化时执行；已初始化的库在重启时会验证并复用，不会反复创建或自动删除。

先记住三个名字：**镜像**是打包好的程序，**容器**是正在运行这份程序的实例，**配置文件**决定连接哪些数据库、使用多少 Worker 和查询频率。

本部署已安装 Docker Compose v2，并通过 `compose.yaml` 管理。命令中的 `traffic` 是服务名，实际容器名称仍是 `mysql-traffic-generator`。

使用文档也已只读挂载到容器内的 `/app/DOCKER_USAGE.md`。登录服务器并进入服务目录后，可在 Docker 中查看：

```bash
docker compose exec -T traffic cat /app/DOCKER_USAGE.md | less
```

按 **q** 退出文档查看。

## 2. 登录服务器

在 Windows PowerShell 中执行：

```powershell
ssh netinside@101.254.114.237
```

如提示输入密码，输入服务器账号密码；终端输入密码时通常不显示字符。登录成功后，进入服务目录：

```bash
cd /home/netinside/docker-images/mysql-traffic-generator/service
```

## 3. 最常用的命令

| 想做什么 | 命令 |
| --- | --- |
| 查看运行状态 | `docker compose ps -a` |
| 查看最近 50 行日志 | `docker compose logs --tail 50 traffic` |
| 实时跟踪日志 | `docker compose logs -f --tail 50 traffic` |
| 停止脚本 | `docker compose stop -t 30 traffic` |
| 恢复运行 | `docker compose up -d --no-build` |
| 重启脚本 | `docker compose restart -t 30 traffic` |
| 查看 CPU 和内存占用 | `docker stats --no-stream mysql-traffic-generator` |
| 检查三台数据库连接 | `docker compose exec -T traffic python traffic.py --check` |

状态中出现 **Up** 表示容器正在运行，**Exited** 表示已经停止，**Restarting** 表示正在反复重启，需要查看日志中的启动错误。

执行本表的 Compose 命令前，请先 `cd /home/netinside/docker-images/mysql-traffic-generator/service`。

实时查看日志时，按 **Ctrl+C** 结束日志查看，后台服务会继续运行。要停止脚本，请使用表中的 `docker compose stop` 命令。

日志里的 `Queries: total=... success=... failed=...` 表示累计查询数、成功数和失败数。`success` 持续增长说明脚本正在产生查询流量。启动日志中的 `server=... discovered ...` 用于确认每台服务器的库表发现结果；容器显示 Up 时，也应检查这些日志。

停止命令会给脚本发送停止信号，最多等待 30 秒，让 Worker 结束查询并关闭连接。

## 4. 启动与恢复

启动、恢复运行或重新创建容器，执行：

```bash
docker compose up -d --no-build
```

也可以执行快捷启动脚本，它会先校验 Compose 文件和应用配置，再启动后台容器：

```bash
cd /home/netinside/docker-images/mysql-traffic-generator/service
bash start.sh
```

如果部署设置没有变化，重复执行启动命令会复用现有容器。镜像已经导入服务器，日常启动和重启无需再次上传或导入镜像。

如果需要停止并移除当前 Compose 容器，可以执行：

```bash
docker compose down
```

服务器上的 `config.yaml` 和 `logs/` 会保留；再执行 `docker compose up -d --no-build` 可重新创建并启动容器。

## 5. 查看日志文件

除了 `docker compose logs`，日志也直接保存在服务器目录：

```text
/home/netinside/docker-images/mysql-traffic-generator/service/logs/traffic.log
/home/netinside/docker-images/mysql-traffic-generator/service/logs/traffic.jsonl
```

在服务目录查看最近的文本日志：

```bash
tail -n 50 logs/traffic.log
```

查看目录占用空间：

```bash
du -sh logs
```

`traffic.log` 适合直接阅读，`traffic.jsonl` 是每行一条 JSON 的查询记录，适合后续分析。两类文件按配置自动轮转，当前每个文件上限 100 MB，最多保留 5 个历史文件；Docker 控制台日志也配置了轮转。

日志可能包含查询采样得到的业务字段值，请按业务数据要求保管。配置和日志挂载在服务器上，容器停止、重启或重新创建后仍会保留。

## 6. 修改配置

配置文件位置：

```text
/home/netinside/docker-images/mysql-traffic-generator/service/config.yaml
```

用 `netinside` 账号编辑。先备份，再修改：

```bash
cd /home/netinside/docker-images/mysql-traffic-generator/service
mkdir -p backups
chmod 700 backups
backup_file="backups/config-$(date +%Y%m%d-%H%M%S).yaml"
cp config.yaml "$backup_file"
chmod 600 "$backup_file"
nano config.yaml
```

在 nano 中，按 **Ctrl+O**、**Enter** 保存，再按 **Ctrl+X** 退出。YAML 使用空格缩进，保留原有层级。

例如，降低查询频率，可把 `traffic` 下的 Worker 数和等待时间改为：

```yaml
traffic:
  workers: 2
  mode: interval
  interval:
    min: 3
    max: 5
```

这里只展示需要调整的字段，请在原文件中修改对应字段，保留数据库连接信息及其他配置。

修改后先校验配置；出现“配置校验通过”后再重启：

```bash
bash start.sh check-config
docker compose restart -t 30 traffic
docker compose logs --tail 80 traffic
```

`check-config` 只校验 YAML 和配置参数。重启后如需验证数据库连接，再执行：

```bash
docker compose exec -T traffic python traffic.py --check
```

增加服务器时，在 `servers` 列表中增加一项；修改数据库密码也在这里进行。密码保存在配置文件中，请勿把密码写入使用文档或公开日志。

配置已通过只读挂载提供给容器。日常修改服务器上的 `config.yaml`，然后重启服务即可；不需要进入容器编辑代码，也不需要重新构建镜像。

本次服务用于 SPAN 镜像下的数据库 SQL 解析，三个服务器项均显式设置了：

```yaml
ssl_disabled: true
```

该选项关闭流量生成器连接的 TLS。SQL 查询和返回数据以明文传输，监控端可直接解析。添加新的测试服务器时，也应在对应 `servers` 项下设置该字段；未设置或设置为 `false` 时，连接库可能自动协商 TLS。

## 7. 常见问题

### SSH 断开了，脚本还在运行吗？

会继续运行。重新登录后执行 `docker compose ps -a` 查看状态。

### 停止后会自动又启动吗？

当前策略为 `unless-stopped`。通过 `docker compose stop` 手动停止后，使用 `docker compose up -d --no-build` 恢复。

### 容器运行，但某台数据库没有流量？

先执行 `--check` 检查连接，再查看启动日志中该服务器是否出现 `discovered`。连接成功后，还需要成功发现可访问的普通表，才会产生业务查询。

238 的库表数量较多，之前的 8 秒读取超时曾导致启动时跳过该服务器。当前版本为启动库表发现保留 60 秒读取预算，并只枚举库表名称；行数统计在选中表时按需读取，避免同时读取数千张表的统计而阻塞启动。普通查询保留原有的限时策略。若再次出现 `2013`、`timed out`，请先检查该数据库和网络响应，再重启服务观察库表发现结果；持续超时需要进一步排查。

### 修改配置后启动失败？

先执行 `bash start.sh check-config`，根据提示修正 YAML 缩进或参数，再启动/重启容器。配置文件应保持由 `netinside` 账号维护，启动脚本会设置配置读取权限。

### 如何查看数据库表？

服务运行时可以执行：

```bash
docker compose exec -T traffic python traffic.py --list-tables | less
```

按 **q** 退出表清单查看。表较多时，使用这个命令会再次读取库表元数据。

### 是否会修改业务库数据？

当前持续服务启用了 `--test-crud`：业务数据查询只执行 SELECT；INSERT、UPDATE、DELETE 只作用于上表的专用测试库，并在每次操作前校验所有权标记。事务控制会发送 START TRANSACTION、COMMIT 或 ROLLBACK。

新增服务器时，CRUD 模式会为该服务器初始化专用测试库，需要账号具有相应建库、建表及测试库读写权限。

### 如何调整测试库 CRUD 的比例？

编辑 `config.yaml` 的 `test_sandbox.traffic_probability`，例如 `0.5` 表示每个 Worker 以 50% 概率选择测试库操作，`1.0` 表示只生成测试库操作。当前值为 `0.25`。修改后按文档步骤校验配置并重启。

### 如何恢复为只读模式？

编辑 `compose.yaml`，将 `command` 改为：

```yaml
command: ["--config", "/app/config.yaml"]
```

然后执行 `docker compose up -d --no-build`。变更启动参数需要重新创建容器；这个操作会保留现有配置、日志与测试数据库。

## 8. 当前文件位置

| 文件 | 用途 |
| --- | --- |
| `service/compose.yaml` | Compose 服务设置 |
| `service/start.sh` | 创建或恢复后台容器，也可校验配置 |
| `service/config.yaml` | 实际运行配置 |
| `service/logs/` | 持久化查询日志 |
| `service/DOCKER_USAGE.md` | 本使用文档的服务器副本 |
| `1.0.4/mysql-traffic-generator-1.0.4.tar` | 已上传的 Docker 镜像包 |

上表中的相对路径均以 `/home/netinside/docker-images/mysql-traffic-generator` 为起点。

迁移前的容器以 `mysql-traffic-generator-before-compose-...` 命名，已经停止并关闭自动重启，作为迁移备份保留。日常操作使用服务名 `traffic`。

## 参考

- [Docker Compose 命令说明](https://docs.docker.com/reference/cli/docker/compose/)
- [Docker 自动重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)
- [Docker 日志查看](https://docs.docker.com/reference/cli/docker/container/logs/)
- [Docker 容器重启](https://docs.docker.com/reference/cli/docker/container/restart/)
