# Docker 打包与运行

项目入口是 `traffic.py`，依赖来自 `requirements.txt`。镜像使用 Python 3.12 的 Debian Bookworm 精简版，包含代码、依赖和示例配置。实际数据库配置通过文件挂载提供，日志写入挂载目录。SPAN 明文测试需要在对应 `servers` 项设置 `ssl_disabled: true`；未设置时保留连接库的 TLS 协商行为。

## 1. 准备环境与配置

Windows 上启动 Docker Desktop，使用 **Linux containers**。Linux 服务器上需要运行中的 Docker Engine，以及 Docker Compose v2（使用 Compose 部署时）。确认引擎正常：

```bash
docker version
docker compose version
```

`docker version` 应同时显示 Client 和 Server。只有 Client，或出现无法连接 Docker API 的提示时，先启动 Docker 引擎。

以下命令均在包含 `traffic.py`、`Dockerfile` 和 `compose.yaml` 的 `mysql-traffic-generator` 目录执行。

项目已有 `config.yaml` 时直接编辑该文件；只有缺少配置时才从示例复制，以免覆盖现有连接信息。

Windows PowerShell：

```powershell
if (-not (Test-Path -LiteralPath ./config.yaml)) {
    Copy-Item -LiteralPath ./config.example.yaml -Destination ./config.yaml
}
New-Item -ItemType Directory -Path ./logs -Force | Out-Null
```

Linux：

```bash
test -f config.yaml || cp config.example.yaml config.yaml
mkdir -p logs
```

在 `config.yaml` 的 `servers` 列表中填写所有 MySQL 服务器。容器中的 `127.0.0.1` 指向容器自身，数据库地址应根据部署方式填写：

| MySQL 所在位置 | 对应服务器项中 `host` 的设置 |
| --- | --- |
| 其他服务器 | 容器可以访问的数据库 IP 或域名 |
| 当前 Windows/macOS Docker Desktop 宿主机 | `host.docker.internal` |
| 当前 Linux Docker Engine 宿主机 | `host.docker.internal`，本项目 Compose 已配置 `host-gateway` 映射；MySQL 需监听容器可以访问的地址 |
| 同一个 Docker 自定义网络中的其他容器 | MySQL 的服务名或网络别名；流量容器也必须加入该网络 |

数据库端的监听地址、防火墙及账号 Host 授权需要允许来自容器的连接。默认模式只执行业务库 SELECT。需要测试库 CRUD 时，请单独运行 `--init-test-db` 或 `--test-crud`，并为账号配置创建测试库及该测试库内读写所需权限。

日志默认配置保持为 `logs/traffic.log`、`logs/traffic.jsonl`。程序以配置文件所在目录解析相对日志路径，挂载到 `/app/config.yaml` 后日志正好落在 `/app/logs`。自定义日志路径时，也要调整对应挂载。

镜像以 UID/GID `10001:10001` 运行。Linux 上为该用户准备配置读取权限和日志写入权限：

```bash
sudo chown 10001:10001 config.yaml logs
sudo chmod 640 config.yaml
sudo chmod 750 logs
```

如果 `logs` 已有文件，已有文件也需要允许 UID 10001 写入，或使用一个新的空日志目录。Windows Docker Desktop 通常无需上述 Linux 权限命令。

## 2. 构建镜像

```bash
docker build -t mysql-traffic-generator:1.0.4 .
```

也可以通过 Compose 构建同一个镜像：

```bash
docker compose build
```

`.dockerignore` 仅放行构建必需文件，排除实际 `config.yaml`、`.venv`、日志、缓存及其他文件。数据库密码无需写入 Dockerfile，也无需在构建时传递。首次构建需要下载基础镜像和 Python 依赖。

镜像架构默认与构建环境一致。需要明确构建普通 x86_64 Linux 服务器使用的镜像时：

```bash
docker build --platform linux/amd64 -t mysql-traffic-generator:1.0.4 .
```

ARM64 服务器使用 `--platform linux/arm64`；跨架构构建需要构建器提供对应支持。

## 3. 检查连接并启动

先验证 Compose 配置和数据库连接：

```bash
docker compose config --quiet
docker compose run --rm traffic --check
```

出现 `Connection OK` 后，启动持续流量：

```bash
docker compose up -d
docker compose logs -f traffic
```

列出数据库表或查看表结构：

```bash
docker compose run --rm traffic --list-tables
docker compose run --rm traffic --inspect
```

仅初始化每台服务器上的独立测试库：

```bash
docker compose run --rm traffic --init-test-db
```

在持续流量中加入测试库 CRUD：

```bash
docker compose run --rm traffic --test-crud
```

停止并移除服务容器：

```bash
docker compose down
```

配置修改后使用 `docker compose restart traffic` 重新加载，不需要重新构建镜像。镜像设置 `STOPSIGNAL SIGINT`，Compose 保留 30 秒停止等待时间，复用脚本的 Ctrl+C 清理逻辑；如果增大了查询/连接超时，应相应增大停止等待时间。

宿主机 `logs/` 保留文本日志和 JSONL。Compose 也限制 Docker 控制台日志的滚动大小。流量工具只向数据库发起连接，不需要发布容器端口。

## 4. 不使用 Compose 时

Windows PowerShell：

```powershell
$projectDir = (Get-Location).Path
docker run --rm --init `
  --add-host=host.docker.internal:host-gateway `
  --mount "type=bind,source=$projectDir/config.yaml,target=/app/config.yaml,readonly" `
  --mount "type=bind,source=$projectDir/logs,target=/app/logs" `
  mysql-traffic-generator:1.0.4 --check

docker run -d --name mysql-traffic-generator --init --restart unless-stopped --stop-timeout 30 `
  --add-host=host.docker.internal:host-gateway `
  --mount "type=bind,source=$projectDir/config.yaml,target=/app/config.yaml,readonly" `
  --mount "type=bind,source=$projectDir/logs,target=/app/logs" `
  mysql-traffic-generator:1.0.4
```

Linux：

```bash
docker run --rm --init \
  --add-host=host.docker.internal:host-gateway \
  --mount "type=bind,source=$(pwd)/config.yaml,target=/app/config.yaml,readonly" \
  --mount "type=bind,source=$(pwd)/logs,target=/app/logs" \
  mysql-traffic-generator:1.0.4 --check

docker run -d --name mysql-traffic-generator --init --restart unless-stopped --stop-timeout 30 \
  --add-host=host.docker.internal:host-gateway \
  --mount "type=bind,source=$(pwd)/config.yaml,target=/app/config.yaml,readonly" \
  --mount "type=bind,source=$(pwd)/logs,target=/app/logs" \
  mysql-traffic-generator:1.0.4
```

查看与停止：

```bash
docker logs -f mysql-traffic-generator
docker stop -t 30 mysql-traffic-generator
```

## 5. 将镜像带到另一台服务器

本机导出：

```bash
docker save -o mysql-traffic-generator-1.0.4.tar mysql-traffic-generator:1.0.4
```

将 tar 文件、`compose.yaml` 和对应环境的 `config.yaml` 传到目标服务器，在部署目录创建可写的 `logs/`，按前述说明设置 Linux 权限，再导入和启动：

```bash
docker load -i mysql-traffic-generator-1.0.4.tar
docker compose run --rm --no-deps --pull never traffic --check
docker compose up -d --no-build --pull never
```

导出的是镜像，配置与日志需要单独管理。目标服务器运行已构建好的镜像时不需要安装 Python；镜像架构应与目标服务器匹配。

## 参考

- [Dockerfile：ENTRYPOINT、USER、STOPSIGNAL](https://docs.docker.com/reference/dockerfile/)
- [Docker 构建上下文与 .dockerignore](https://docs.docker.com/build/concepts/context/)
- [Docker Desktop：从容器连接宿主机](https://docs.docker.com/desktop/features/networking/networking-how-tos/)
- [Docker Compose 服务与挂载配置](https://docs.docker.com/reference/compose-file/services/)
