-- KnowHub_AI MySQL initialization script.
-- Run this with a TencentDB/CynosDB account that has CREATE DATABASE permission.
-- This file intentionally contains no password or connection string.

CREATE DATABASE IF NOT EXISTS `knowhub_ai`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

ALTER DATABASE `knowhub_ai`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

SELECT 'knowhub_ai database is ready' AS result;
