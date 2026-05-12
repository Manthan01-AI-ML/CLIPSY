#!/usr/bin/env bash
# One-time setup helper. Run after scaffold.
echo "Setting up ClipWise environment..."
docker-compose up -d db redis
sleep 5
echo "DB and Redis are up. Now run: docker-compose up backend"
