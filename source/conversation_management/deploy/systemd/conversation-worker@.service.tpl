[Unit]
Description=Conversation Management Worker %i
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
Environment=WORKER_INSTANCE=%i
Environment=CONVERSATION_PROCESS_ROLE=worker-%i
ExecStart=__PYTHON__ -m scripts.run_worker
Restart=always
RestartSec=3
TimeoutStartSec=60
TimeoutStopSec=60
KillSignal=SIGTERM
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
UMask=0027

[Install]
WantedBy=multi-user.target
