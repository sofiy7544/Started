-- Периодическое удаление старой переписки всех пользователей (в т. ч. неактивных).
CREATE INDEX messages_created_at ON messages (created_at);
