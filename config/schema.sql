-- ============================================================
-- DSH 插件独立库 schema（2026-11-26 创建）
-- 用法：mysql -u root < config/schema.sql
-- ============================================================


/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

CREATE DATABASE /*!32312 IF NOT EXISTS*/ `dsh_test` /*!40100 DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci */ /*!80016 DEFAULT ENCRYPTION='N' */;

USE `dsh_test`;
DROP TABLE IF EXISTS `dsh_case_results`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `dsh_case_results` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `run_id` varchar(64) NOT NULL,
  `case_id` varchar(64) NOT NULL,
  `case_type` varchar(16) DEFAULT NULL,
  `attempt` int DEFAULT '1',
  `status` varchar(16) DEFAULT NULL COMMENT 'passed/failed/errors/skipped',
  `start_time` datetime DEFAULT NULL,
  `end_time` datetime DEFAULT NULL,
  `cost_ms` int DEFAULT NULL,
  `message` text,
  `req_content` longtext,
  `resp_content` longtext,
  `assert_detail` json DEFAULT NULL,
  `screenshot_path` varchar(512) DEFAULT NULL,
  `ext_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_run_case_attempt` (`run_id`,`case_id`,`attempt`),
  KEY `idx_run_id` (`run_id`)
) ENGINE=InnoDB AUTO_INCREMENT=906 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用例执行结果表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `dsh_test_bugs`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `dsh_test_bugs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `run_id` varchar(64) NOT NULL,
  `bug_id` varchar(64) NOT NULL,
  `title` varchar(512) DEFAULT NULL,
  `severity` varchar(16) DEFAULT NULL COMMENT 'blocker/critical/major/minor/trivial',
  `priority` varchar(8) DEFAULT NULL,
  `module` varchar(128) DEFAULT NULL,
  `status` varchar(16) DEFAULT NULL COMMENT 'new/confirmed/fixing/verifying/closed/reopened/rejected',
  `case_id` varchar(64) DEFAULT NULL,
  `case_type` varchar(16) DEFAULT NULL,
  `description` text,
  `steps_json` json DEFAULT NULL,
  `expected` text,
  `actual` text,
  `req_content` longtext,
  `resp_content` longtext,
  `assert_detail` json DEFAULT NULL,
  `screenshot_path` varchar(512) DEFAULT NULL,
  `reporter` varchar(64) DEFAULT NULL,
  `assignee` varchar(64) DEFAULT NULL,
  `occurrences` int DEFAULT '1',
  `first_run_id` varchar(64) DEFAULT NULL,
  `found_at` datetime DEFAULT NULL,
  `confirmed_at` datetime DEFAULT NULL,
  `fixed_at` datetime DEFAULT NULL,
  `closed_at` datetime DEFAULT NULL,
  `ext_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_run_bug` (`run_id`,`bug_id`),
  KEY `idx_run_id` (`run_id`),
  KEY `idx_status` (`status`),
  KEY `idx_severity` (`severity`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='测试缺陷台账表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `dsh_test_cases`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `dsh_test_cases` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `run_id` varchar(64) NOT NULL,
  `case_id` varchar(64) NOT NULL,
  `case_type` varchar(16) NOT NULL COMMENT 'functional/api/ui',
  `title` varchar(512) DEFAULT NULL,
  `module` varchar(128) DEFAULT NULL,
  `priority` varchar(8) DEFAULT NULL,
  `precondition` text,
  `related_req` varchar(64) DEFAULT NULL,
  `related_func` varchar(64) DEFAULT NULL,
  `steps_json` json DEFAULT NULL,
  `request_json` json DEFAULT NULL,
  `assert_json` json DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `ext_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_run_case` (`run_id`,`case_id`),
  KEY `idx_run_id` (`run_id`)
) ENGINE=InnoDB AUTO_INCREMENT=949 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用例表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `dsh_test_reports`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `dsh_test_reports` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `run_id` varchar(64) NOT NULL,
  `total` int DEFAULT NULL,
  `passed` int DEFAULT NULL,
  `failed` int DEFAULT NULL,
  `skipped` int DEFAULT NULL,
  `pass_rate` decimal(5,2) DEFAULT NULL,
  `report_md_path` varchar(512) DEFAULT NULL,
  `report_json` longtext,
  `report_html_path` varchar(512) DEFAULT NULL,
  `conclusion` varchar(1024) DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `ext_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `run_id` (`run_id`)
) ENGINE=InnoDB AUTO_INCREMENT=13 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='测试报告表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `dsh_test_runs`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `dsh_test_runs` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `run_id` varchar(64) NOT NULL,
  `run_name` varchar(255) DEFAULT NULL,
  `demand_hash` varchar(64) DEFAULT NULL,
  `workspace_hash` varchar(64) DEFAULT NULL,
  `start_time` datetime DEFAULT NULL,
  `end_time` datetime DEFAULT NULL,
  `total` int DEFAULT '0',
  `passed` int DEFAULT '0',
  `failed` int DEFAULT '0',
  `errors` int DEFAULT '0',
  `skipped` int DEFAULT '0',
  `pass_rate` decimal(5,2) DEFAULT NULL,
  `duration_sec` decimal(10,2) DEFAULT NULL,
  `status` tinyint DEFAULT NULL COMMENT '0运行中 1完成 2中断',
  `report_path` varchar(512) DEFAULT NULL,
  `ext_json` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `run_id` (`run_id`),
  KEY `idx_start_time` (`start_time`)
) ENGINE=InnoDB AUTO_INCREMENT=13 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='测试批次表';
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

