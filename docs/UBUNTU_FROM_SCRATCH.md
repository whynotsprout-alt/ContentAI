# Ubuntu 部署指南（ContentAI V0.7.0）

本文用于在全新的 Ubuntu 22.04 或 24.04 LTS 服务器上部署 ContentAI。生产环境使用 Docker Compose，应用只在本机回环地址暴露 Web 服务，由 Nginx 或 Caddy 负责 HTTPS。

> 本指南仅适用于全新数据库。不要将其直接用于旧版本数据库的原地升级；先完成备份和恢复演练。

## 1. 部署前准备

准备以下资源：

- 可通过 SSH 登录、拥有 `sudo` 权限的 Ubuntu LTS 服务器；
- 已解析至服务器公网 IP 的域名；
- 用于 OpenAI-compatible 模型、Fernet 主密钥与所选搜索提供方的密钥；
- 一个强密码的默认管理员账号。

开放 SSH、HTTP、HTTPS，其他端口不对公网开放：

~~~bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
~~~

## 2. 安装 Docker Engine 与 Compose

以下命令使用 Docker 官方 APT 仓库。若已有 Docker Compose v2，可跳到下一节。

~~~bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
~~~

重新登录 SSH 后确认：

~~~bash
docker version
docker compose version
~~~

## 3. 下载并校验发布包

~~~bash
cd /tmp
curl -fL -O https://github.com/whynotsprout-alt/ContentAI/releases/download/v0.7.0/contentai-0.7.0-ubuntu.tar.gz
curl -fL -O https://github.com/whynotsprout-alt/ContentAI/releases/download/v0.7.0/contentai-0.7.0-ubuntu.tar.gz.sha256

sha256sum -c contentai-0.7.0-ubuntu.tar.gz.sha256

sudo install -d -m 0755 /opt/contentai
sudo tar -xzf contentai-0.7.0-ubuntu.tar.gz -C /opt/contentai
sudo chown -R "$USER":"$USER" /opt/contentai/contentai-0.7.0-ubuntu
cd /opt/contentai/contentai-0.7.0-ubuntu
~~~

也可从源码安装：

~~~bash
git clone --branch v0.7.0 --single-branch \
  https://github.com/whynotsprout-alt/ContentAI.git /opt/contentai/contentai-0.7.0
cd /opt/contentai/contentai-0.7.0
~~~

## 4. 配置生产环境

复制模板并限制其读取权限：

~~~bash
cp .env.example .env
chmod 600 .env
~~~

编辑 `.env`。下列是生产启动所需的最小配置；数据库 URL 中的密码必须与 `POSTGRES_PASSWORD` 完全一致。

~~~dotenv
# Docker 与数据库
CONTENTAI_ENV=production
POSTGRES_DB=contentai
POSTGRES_USER=contentai
POSTGRES_PASSWORD=replace-with-a-long-url-safe-password
CONTENTAI_DATABASE__URL=postgresql+psycopg://contentai:replace-with-a-long-url-safe-password@postgres:5432/contentai
WEB_PORT=5180

# 浏览器访问域名（HTTPS 由反向代理终止）
CONTENTAI_SERVER__FRONTEND_ORIGINS=https://content.example.com

# 模型凭据的 Fernet 主密钥：生成一次、保管并安全备份；不可在线轮换
CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY=replace-with-a-generated-fernet-key

# 仅填写实际启用的搜索提供方
# Traffic Relay 仅用于搜索，不能作为模型 API Key
CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY=replace-with-search-relay-key
CONTENTAI_SEARCH__TIKHUB_API_KEY=
CONTENTAI_SEARCH__METASO_API_KEY=
CONTENTAI_SEARCH__ANSPIRE_API_KEY=

# 首次启动时自动创建；已有同邮箱账号不会被覆盖
CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL=admin@example.com
CONTENTAI_AUTH__BOOTSTRAP_ADMIN_PASSWORD=replace-with-a-long-unique-password
~~~

先在受控的终端生成 Fernet key，写入 `.env` 后立即清理终端记录：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。该 key 必须与数据库备份一同安全备份，且所有应用服务必须共用；丢失、格式错误或替换它会使应用拒绝启动或无法解密历史模型凭据。不要配置已下线的邮件验证、SMTP、`PUBLIC_BASE_URL` 或邮件密码重置变量。所有搜索密钥必须是对应服务提供的原始值；Metaso 密钥仅支持 ASCII 字符。

## 5. 配置 Nginx 与 HTTPS

应用只将 Web 服务绑定到 `127.0.0.1:${WEB_PORT}`。将 `content.example.com` 替换为实际域名：

~~~bash
sudo apt-get install -y nginx certbot python3-certbot-nginx

sudo tee /etc/nginx/sites-available/contentai > /dev/null <<'EOF'
server {
    listen 80;
    server_name content.example.com;

    location / {
        proxy_pass http://127.0.0.1:5180;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/contentai /etc/nginx/sites-enabled/contentai
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d content.example.com
~~~

完成后 Certbot 会续期证书；可用 `sudo certbot renew --dry-run` 验证续期。

## 6. 启动与验证

~~~bash
cd /opt/contentai/contentai-0.7.0-ubuntu
docker compose --env-file .env config --quiet
bash infra/ubuntu/deploy.sh
bash infra/ubuntu/health.sh
~~~

`migration` 是一次性容器，显示 `Exited (0)` 属于正常现象。其余服务应为 `healthy` 或 `Up`：

~~~bash
docker compose --env-file .env ps
curl --fail https://content.example.com/api/ready
~~~

首次 API 启动会自动创建由 `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAIL` 指定的已激活管理员。该邮箱若已存在，服务不会更改其密码、角色或状态；因此无需、也不应调用注册接口手工创建默认管理员。

使用该账号登录后立即修改初始密码。初始密码应保存在受控的密钥管理系统中，不应写入文档、Shell 历史或版本库。

然后访问 `/admin/models` 配置唯一全局 active OpenAI-compatible Base URL、API Key、模型名和 API 模式（Chat Completions 或 Responses）。可先 probe；保存时服务端会再次按所选模式探测对应 endpoint，运行时不会再根据模型名自动切换。首次配置必须输入 API Key，后续更新留空才表示继续使用当前 active Key。模型切换仅影响新 execution；queued、running、resume 与 retry 使用固化的历史版本。首个版本不提供旧环境变量自动导入、数据库回退、配置删除、回滚或在线 Fernet 主密钥轮换。

## 7. 日志与深度搜索排错

~~~bash
# API、鉴权、就绪检查与 HTTP 请求
docker compose --env-file .env logs --tail 200 api

# 深度搜索、工具调用、模型执行与队列任务
docker compose --env-file .env logs --follow agent-worker

# 调度或后台处理问题
docker compose --env-file .env logs --tail 200 dispatcher background-worker side-effect-worker
~~~

深度搜索的工具调用错误优先查看 `agent-worker`。若浏览器只显示请求失败或 SSE 中断，再查看 `api` 日志。

## 8. 备份、恢复与升级

~~~bash
# 备份 PostgreSQL，并生成同名 SHA-256 文件
bash infra/ubuntu/backup.sh /srv/contentai-backups

# 恢复会覆盖数据库，必须显式确认
CONTENTAI_CONFIRM_RESTORE=yes bash infra/ubuntu/restore.sh \
  /srv/contentai-backups/contentai-YYYYMMDDTHHMMSSZ.sql.gz

# 升级前自动备份、构建并重启
bash infra/ubuntu/upgrade.sh
~~~

在升级前先备份 `.env` 与数据库。将新发布包解压到新的版本目录，复制原有 `.env`，执行 `docker compose --env-file .env config --quiet` 后再运行升级脚本。

## 9. 常见故障

| 现象 | 首要检查 |
| --- | --- |
| `migration` 失败 | `.env` 中数据库密码与 URL 是否一致；`docker compose logs migration` |
| API 不健康 | `bash infra/ubuntu/health.sh` 与 `docker compose logs api` |
| 深度搜索失败 | `docker compose logs agent-worker`；搜索密钥与模型配置 |
| Web 可打开但 API 失败 | `https://域名/api/ready`、Nginx 配置和 `docker compose logs api` |
| 默认管理员无法登录 | 确认使用首次部署设置的邮箱；已有同邮箱用户不会被启动逻辑重置 |
| 事件流中断 | Nginx 的 `proxy_buffering off`、Redis 健康状态与 API 日志 |

更多运行维护说明见 [OPERATIONS.md](OPERATIONS.md)。
