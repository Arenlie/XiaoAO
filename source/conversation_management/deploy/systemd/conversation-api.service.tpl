[Unit]
Description=Conversation Management FastAPI
Wants=network-online.target
Requires=conversation-migrate.service
After=network-online.target conversation-migrate.service

[Service]
Type=simple
User=__APP_USER__
Group=__APP_GROUP__
WorkingDirectory=__APP_DIR__
EnvironmentFile=__APP_DIR__/.env
Environment=PYTHONUNBUFFERED=1
Environment=CONVERSATION_PROCESS_ROLE=api
ExecStart=__PYTHON__ -m scripts.run_api
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
