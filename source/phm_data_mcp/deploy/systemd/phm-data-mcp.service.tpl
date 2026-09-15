[Unit]
Description=PHM Data MCP
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=__APP_USER__
Group=__APP_GROUP__
WorkingDirectory=__APP_DIR__
EnvironmentFile=__APP_DIR__/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=__PYTHON__ __APP_DIR__/main.py
Restart=always
RestartSec=3
TimeoutStartSec=60
TimeoutStopSec=45
KillSignal=SIGTERM
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
UMask=0027

[Install]
WantedBy=multi-user.target
