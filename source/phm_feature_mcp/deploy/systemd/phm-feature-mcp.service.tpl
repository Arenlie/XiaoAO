[Unit]
Description=PHM Feature MCP 1.0.0
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=__APP_USER__
Group=__APP_GROUP__
WorkingDirectory=__APP_DIR__
EnvironmentFile=__APP_DIR__/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=__APP_DIR__/.venv/bin/python __APP_DIR__/run.py
Restart=always
RestartSec=3
TimeoutStartSec=60
TimeoutStopSec=45
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
