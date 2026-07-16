# Ubuntu 从零部署 ContentAI V0.4.2

本文适用于全新 Ubuntu LTS 服务器和全新数据库。部署完成后，会在数据库中创建管理员 `1848714681@qq.cpm`，初始密码为 `WnaFan2026`。

> 注意：邮箱地址按提供内容使用 `.cpm`。如这是笔误，请在部署前把本文和 `.env` 中的地址一并改为正确邮箱。初始密码仅用于首次登录；在服务器对外开放前，请登录后立即修改密码。

## 1. 准备服务器

需要一台可通过 SSH 登录的 Ubuntu LTS 服务器、一个已经解析到该服务器的域名，以及可使用 `sudo` 的账户。防火墙只开放 SSH、HTTP 和 HTTPS：

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

## 2. 安装 Docker Engine 与 Compose

以下命令使用 Docker 官方 Ubuntu APT 仓库。官方安装说明见 <https://docs.docker.com/engine/install/ubuntu/>。

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo \"${UBUNTU_CODENAME:-$VERSION_CODENAME}\") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo usermod -aG docker "$USER"
```

重新登录 SSH（或新开一个终端会话）后，确认当前用户可以直接执行 Docker 命令；后续部署命令均以此用户执行：

```bash
docker version
docker compose version
```

## 3. 下载并校验发布包

```bash
cd /tmp
curl -fL -O https://github.com/whynotsprout-alt/ContentAI/releases/download/V0.4.2/contentai-0.4.2-ubuntu.tar.gz

echo '8D6961B5801162C7937BF6BFBB6438C1B6C3376F30D1672BBE8D5BB92E85F0EF  contentai-0.4.2-ubuntu.tar.gz' | sha256sum -c -

sudo install -d -m 0755 /opt/contentai
sudo tar -xzf contentai-0.4.2-ubuntu.tar.gz -C /opt/contentai
sudo chown -R "$USER":"$USER" /opt/contentai/contentai-0.4.2-ubuntu
cd /opt/contentai/contentai-0.4.2-ubuntu
```

也可从源码部署：

```bash
git clone --branch codex/release-v0.4.2 --single-branch https://github.com/whynotsprout-alt/ContentAI.git /opt/contentai/contentai-0.4.2
cd /opt/contentai/contentai-0.4.2
```

## 4. 配置生产环境

复制模板并限制权限：

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少替换以下值。`POSTGRES_PASSWORD` 与 `CONTENTAI_DATABASE__URL` 中的密码必须完全一致；域名必须使用实际的 HTTPS 地址。

```dotenv
CONTENTAI_ENV=production
POSTGRES_DB=contentai
POSTGRES_USER=contentai
POSTGRES_PASSWORD=请替换为高强度数据库密码
CONTENTAI_DATABASE__URL=postgresql+psycopg://contentai:请替换为同一高强度数据库密码@postgres:5432/contentai

WEB_PORT=5180
CONTENTAI_SERVER__FRONTEND_ORIGINS=https://content.example.com
CONTENTAI_AUTH__PUBLIC_BASE_URL=https://content.example.com
CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAILS=["1848714681@qq.cpm"]
CONTENTAI_AUTH__REQUIRE_EMAIL_VERIFICATION=false

CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY=请填写实际密钥
CONTENTAI_SEARCH__TIKHUB_API_KEY=请填写实际密钥
CONTENTAI_SEARCH__METASO_API_KEY=请填写实际密钥
CONTENTAI_SEARCH__ANSPIRE_API_KEY=请填写实际密钥
```

其余模型、SMTP 和限流配置按 `.env.example` 的说明补全。不要提交 `.env`。

## 5. 配置 Nginx 与 HTTPS

应用容器只监听 `127.0.0.1:5180`，通过宿主机 Nginx 对外提供 HTTPS。将 `content.example.com` 改为实际域名：

```bash
sudo apt-get install -y nginx certbot python3-certbot-nginx

sudo tee /etc/nginx/sites-available/contentai > /dev/null <<'EOF'
server {
    listen 80;
    server_name content.example.com;

    location / {
        proxy_pass http://127.0.0.1:5180;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/contentai /etc/nginx/sites-enabled/contentai
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d content.example.com
```

## 6. 启动服务

```bash
docker compose --env-file .env config --quiet
bash infra/ubuntu/deploy.sh
bash infra/ubuntu/health.sh
```

`migration` 显示为 `Exited (0)` 属于正常现象；其余服务应为 `healthy` 或 `Up`。

## 7. 创建初始管理员

确认 API 健康后，在服务器本机执行下列命令。应用会将用户写入数据库；由于邮箱已出现在 `CONTENTAI_AUTH__BOOTSTRAP_ADMIN_EMAILS` 中，该用户会以 `admin` 角色创建。

```bash
curl --fail --show-error --silent \
  -X POST http://127.0.0.1:5180/api/auth/register \
  -H 'Content-Type: application/json' \
  --data '{"email":"1848714681@qq.cpm","password":"WnaFan2026"}'

curl --fail --show-error --silent \
  -c /tmp/contentai-admin.cookies \
  -X POST http://127.0.0.1:5180/api/auth/login \
  -H 'Content-Type: application/json' \
  --data '{"email":"1848714681@qq.cpm","password":"WnaFan2026"}' | tee /tmp/contentai-admin.json

grep -q '"role":"admin"' /tmp/contentai-admin.json && echo '管理员创建成功'
rm -f /tmp/contentai-admin.cookies /tmp/contentai-admin.json
```

若注册返回 `409`，说明该邮箱已经存在。本文只面向全新数据库；不要直接修改 `password_hash`。请先确认是否部署到了已有数据，再按既有管理员流程处理。

## 8. 首次登录与日常检查

浏览器打开 `https://content.example.com`，使用上述账号登录，并立即修改初始密码。日常健康检查和日志命令：

```bash
cd /opt/contentai/contentai-0.4.2-ubuntu
bash infra/ubuntu/health.sh
docker compose --env-file .env logs --tail 200 api
docker compose --env-file .env logs --follow agent-worker
```

备份、恢复与升级说明见 [OPERATIONS.md](OPERATIONS.md)。
