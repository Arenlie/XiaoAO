[Unit]
Description=Conversation Management Data and Attachment Cleanup
After=network-online.target conversation-migrate.service
Requires=conversation-migrate.service

[Service]
Type=oneshot
User=__APP_USER__
Group=__APP_GROUP__
WorkingDirectory=__APP_DIR__
EnvironmentFile=__APP_DIR__/.env
Environment=PYTHONUNBUFFERED=1
Environment=CONVERSATION_PROCESS_ROLE=data-cleanup
ExecStart=__PYTHON__ -m scripts.cleanup
