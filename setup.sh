#!/usr/bin/env bash
# MSC v2 — One-step setup script
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== MSC v2 Setup ==="

# 1. Create .env if not exists
if [ ! -f .env ]; then
    echo "[1/5] Creating .env from template..."
    cp .env.example .env

    # Generate a random admin key if not set
    if ! grep -q "MSC_INITIAL_ADMIN_KEY=" .env || [ -z "$(grep 'MSC_INITIAL_ADMIN_KEY=' .env | cut -d= -f2)" ]; then
        ADMIN_KEY=$(openssl rand -hex 16)
        echo "MSC_INITIAL_ADMIN_KEY=$ADMIN_KEY" >> .env
        echo "  Generated admin key: $ADMIN_KEY"
    fi

    # Generate a random secret key
    if ! grep -q "MSC_SECRET_KEY=" .env || [ "$(grep 'MSC_SECRET_KEY=' .env | cut -d= -f2)" = "change-me-to-random-string" ]; then
        SECRET_KEY=$(openssl rand -hex 32)
        sed -i "s/MSC_SECRET_KEY=change-me-to-random-string/MSC_SECRET_KEY=$SECRET_KEY/" .env
    fi
else
    echo "[1/5] .env already exists, skipping..."
fi

# 2. Run migration
echo "[2/5] Running migration..."
cd "$SCRIPT_DIR"
python3 migrations/migrate_from_markdown.py --memory-dir /home/astro/.msc/memory

# 3. Start Docker
echo "[3/5] Starting Docker service..."
docker compose up -d

# 4. Install the msc CLI on the host (optional, never fatal)
echo "[4/5] Installing the msc CLI..."
if pip install --user --break-system-packages -q -e "$SCRIPT_DIR/cli" 2>/dev/null; then
    echo "  CLI installed: $(command -v msc || echo '~/.local/bin/msc')"
else
    echo "  CLI install failed — install later with: make cli"
fi

# 4. Wait for health check
echo "[5/5] Waiting for service..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8000/api/v1/health > /dev/null 2>&1; then
        echo ""
        echo "=== MSC v2 is running! ==="
        echo ""
        echo "  Web UI:   http://localhost:8000"
        echo "  API:      http://localhost:8000/api/v1"
        echo "  Swagger:  http://localhost:8000/docs"
        echo ""

        # LAN mode by default: nothing to log in with
        if [ "$(grep '^MSC_AUTH_ENABLED=' .env | cut -d= -f2)" = "true" ]; then
            ADMIN_KEY=$(grep '^MSC_INITIAL_ADMIN_KEY=' .env | cut -d= -f2)
            if [ -n "$ADMIN_KEY" ]; then
                echo "  Admin key: api-999-$ADMIN_KEY"
                echo ""
            fi
        else
            echo "  访问模式: 局域网免登录（无需 token，不会过期）"
            echo "  恢复鉴权: .env 里设 MSC_AUTH_ENABLED=true 后 docker compose up -d"
            echo ""
        fi

        echo "  Quick start:"
        echo "    msc list                    # List memories"
        echo "    msc add --name 'test'       # Add a memory"
        echo "    msc search 'keyword'        # Search"
        echo "    msc sync                    # Sync filesystem"
        echo ""
        exit 0
    fi
    sleep 1
done

echo "Warning: Service did not become healthy in time."
echo "Check logs: docker compose logs msc-api"
exit 1
