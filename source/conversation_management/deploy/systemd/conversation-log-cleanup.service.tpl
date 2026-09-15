[Unit]
Description=Conversation Management Old Log Cleanup
After=network-online.target

[Service]
Type=oneshot
User=__APP_USER__
Group=__APP_GROUP__
WorkingDirectory=__APP_DIR__
EnvironmentFile=__APP_DIR__/.env
Environment=PYTHONUNBUFFERED=1
Environment=CONVERSATION_PROCESS_ROLE=log-cleanup
ExecStart=__PYTHON__ -m scripts.cleanup_logs
