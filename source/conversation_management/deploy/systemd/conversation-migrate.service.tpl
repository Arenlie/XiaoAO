[Unit]
Description=Conversation Management Database Migration
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=__APP_USER__
Group=__APP_GROUP__
WorkingDirectory=__APP_DIR__
EnvironmentFile=__APP_DIR__/.env
Environment=PYTHONUNBUFFERED=1
Environment=CONVERSATION_PROCESS_ROLE=migrate
ExecStart=__PYTHON__ -m scripts.run_migrate
RemainAfterExit=yes
TimeoutStartSec=180

[Install]
WantedBy=multi-user.target
