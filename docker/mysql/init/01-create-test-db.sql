-- README 截图 / pytest 用的独立测试库。
-- MySQL 容器首次启动会按字母序自动执行 /docker-entrypoint-initdb.d/*.sql。
-- 现有容器已初始化，需要手动执行一次：
--   docker exec -i douyin-live-mysql mysql -uroot -proot123 < docker/mysql/init/01-create-test-db.sql

CREATE DATABASE IF NOT EXISTS douyin_live_test
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON douyin_live_test.* TO 'douyin'@'%';
FLUSH PRIVILEGES;
