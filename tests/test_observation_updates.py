import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


BASE_PAYLOAD = {
    "primary_object_id": "SAT-9",
    "secondary_object_id": "DEB-4",
    "tca": "2026-10-01T12:00:00+00:00",
    "miss_distance_m": 80,
    "covariance_m": 100,
    "fuel_budget_m_s": 5,
    "track_age_hours": 1,
    "operating_organizations": ["Org-A"],
}

WINDOW = "2026-10-01T08:00:00Z/2026-10-01T09:00:00Z"


class ObservationUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp.name + suffix)
            except OSError:
                pass

    def _assessed_item(self):
        item = self.service.create_item(dict(BASE_PAYLOAD), "analyst-1", "analyst")
        return self.service.act(item["id"], "assess", {"hours_to_tca": 12}, "analyst-1", "analyst", item["version"])

    def _revision(self, source, distance):
        return {
            "observed_at": "2026-10-01T01:00:00Z",
            "miss_distance_m": distance,
            "covariance_m": 100,
            "source": source,
        }

    def test_observation_update_invalidates_plan_and_signoff(self):
        item = self._assessed_item()
        self.assertEqual(item["payload"]["observation_version"], 1)
        item = self.service.act(item["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"}, "op-1", "operator", item["version"])
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2.0, "maneuver_window": WINDOW}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(item["payload"]["approved_maneuver"]["observation_version"], 1)

        item = self.service.act(item["id"], "report_revision", self._revision("radar-2", 300), "analyst-2", "analyst", item["version"])

        # 旧评估重算、规避方案和运营方签字作废，状态退回待审批
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(item["payload"]["observation_version"], 2)
        self.assertNotIn("approved_maneuver", item["payload"])
        self.assertEqual(item["payload"]["opinions"], [])
        self.assertFalse(item["payload"]["conflict"])
        self.assertEqual(item["payload"]["assessment"], item["assessment"])
        # 作废内容留在审计事件中
        revision_event = [e for e in item["audit"] if e["event_type"] == "report_revision"][-1]
        self.assertEqual(revision_event["payload"]["invalidated"]["approved_maneuver"]["maneuver_window"], WINDOW)
        self.assertEqual(len(revision_event["payload"]["invalidated"]["opinions"]), 1)
        # 未重新批准前不能执行
        with self.assertRaises(DomainError):
            self.service.act(item["id"], "execute", {"command_ref": "CMD-1"}, "op-1", "operator", item["version"])
        # 运营方重新签字、协调员重新批准后流程继续
        item = self.service.act(item["id"], "record_opinion", {"operator": "Org-A", "opinion": "approve"}, "op-1", "operator", item["version"])
        item = self.service.act(item["id"], "approve", {"fuel_cost_m_s": 1.5, "maneuver_window": WINDOW}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(item["payload"]["approved_maneuver"]["observation_version"], 2)

    def test_revision_requires_expected_version(self):
        item = self._assessed_item()
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "report_revision", self._revision("radar-2", 90), "analyst-1", "analyst")
        self.assertEqual(context.exception.code, "expected_version_required")

    def test_concurrent_observation_conflict_recompute_no_overwrite(self):
        item = self._assessed_item()
        stale_version = item["version"]
        # 两个分析员基于同一版本同时提交观测
        item = self.service.act(item["id"], "report_revision", self._revision("radar-a", 90), "analyst-1", "analyst", stale_version)
        with self.assertRaises(ConflictError):
            self.service.act(item["id"], "report_revision", self._revision("radar-b", 110), "analyst-2", "analyst", stale_version)
        # 冲突方的写入没有覆盖前一个结果
        latest = self.service.get_item(item["id"])
        self.assertEqual([r["source"] for r in latest["payload"]["revisions"]], ["radar-a"])
        self.assertEqual(latest["payload"]["miss_distance_m"], 90)
        # 后到的一方重新读取、基于最新结果重算后再提交，两次观测都保留
        item = self.service.act(item["id"], "report_revision", self._revision("radar-b", 110), "analyst-2", "analyst", latest["version"])
        self.assertEqual([r["source"] for r in item["payload"]["revisions"]], ["radar-a", "radar-b"])
        self.assertEqual(item["payload"]["observation_version"], 3)


class ExecutionRetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmp.name + suffix)
            except OSError:
                pass

    def _approved_item(self):
        item = self.service.create_item(dict(BASE_PAYLOAD), "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 12}, "analyst-1", "analyst", item["version"])
        return self.service.act(item["id"], "approve", {"fuel_cost_m_s": 2.0, "maneuver_window": WINDOW}, "coord-1", "coordinator", item["version"])

    def test_execution_failure_keeps_window_and_retries_incomplete_only(self):
        item = self._approved_item()
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-1", "steps": ["burn-1", "burn-2", "burn-3"]}, "op-1", "operator", item["version"])
        self.assertEqual(item["status"], "executing")

        # 执行失败：burn-1 完成，burn-2 失败
        item = self.service.act(item["id"], "execution_update", {"completed": ["burn-1"], "failed": ["burn-2"]}, "op-1", "operator", item["version"])
        self.assertEqual(item["status"], "coordinating")
        # 机动窗口保留，无需重新批准
        self.assertEqual(item["payload"]["approved_maneuver"]["maneuver_window"], WINDOW)
        self.assertEqual(item["payload"]["execution"]["steps"]["burn-1"], "done")
        self.assertEqual(item["payload"]["execution"]["steps"]["burn-2"], "failed")

        # 重试时不得包含已完成的动作
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "execute", {"command_ref": "CMD-2", "steps": ["burn-1", "burn-2"]}, "op-1", "operator", item["version"])
        self.assertEqual(context.exception.code, "steps_already_completed")

        # 只重试未完成的动作
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-2", "steps": ["burn-2", "burn-3"]}, "op-1", "operator", item["version"])
        retry_event = [e for e in item["audit"] if e["event_type"] == "execute"][-1]
        self.assertEqual(sorted(retry_event["payload"]["retry_steps"]), ["burn-2", "burn-3"])
        self.assertEqual(item["payload"]["execution"]["attempts"], 2)
        self.assertEqual(item["payload"]["execution"]["steps"]["burn-1"], "done")

        # 完成剩余动作后解决
        item = self.service.act(item["id"], "execution_update", {"completed": ["burn-2", "burn-3"]}, "op-1", "operator", item["version"])
        item = self.service.act(item["id"], "resolve", {"report_ref": "RPT-1"}, "coord-1", "coordinator", item["version"])
        self.assertEqual(item["status"], "resolved")

    def test_resolve_blocked_until_steps_complete(self):
        item = self._approved_item()
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-1", "steps": ["burn-1"]}, "op-1", "operator", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.act(item["id"], "resolve", {"report_ref": "RPT-1"}, "coord-1", "coordinator", item["version"])
        self.assertEqual(context.exception.code, "execution_incomplete")

    def test_observation_update_during_execution_restarts_approval(self):
        item = self._approved_item()
        item = self.service.act(item["id"], "execute", {"command_ref": "CMD-1", "steps": ["burn-1"]}, "op-1", "operator", item["version"])
        item = self.service.act(item["id"], "report_revision", {
            "observed_at": "2026-10-01T02:00:00Z",
            "miss_distance_m": 500,
            "covariance_m": 150,
            "source": "radar-3",
        }, "analyst-2", "analyst", item["version"])
        # 执行中的方案同样作废，执行进度清空，必须重走审批
        self.assertEqual(item["status"], "assessed")
        self.assertNotIn("execution", item["payload"])
        self.assertNotIn("approved_maneuver", item["payload"])


class LegacyMigrationTest(unittest.TestCase):
    def test_backfill_observation_version_and_keep_audit(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            repo = Repository(tmp.name)
            repo.initialize()
            # 模拟旧数据：缺少观测版本等字段的接近事件及其审计记录
            conn = repo.connect()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "space_conjunction",
                    "SAT-X|DEB-Y|2026-09-01",
                    "assessed",
                    4,
                    json.dumps({"primary_object_id": "SAT-X", "miss_distance_m": 50, "covariance_m": 80}),
                    "analyst-old",
                    "analyst",
                    "2026-09-01T00:00:00+00:00",
                    "2026-09-01T00:00:00+00:00",
                ),
            )
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            repo.append_audit(conn, item_id, "created", "analyst-old", "analyst", {"stable_key": "legacy"})
            conn.execute("COMMIT")
            conn.close()

            # 升级：补成初始观测版本
            repo.initialize()
            item = repo.get_item(item_id)
            self.assertEqual(item["payload"]["observation_version"], 1)
            self.assertEqual(item["payload"]["revisions"], [])
            self.assertEqual(item["payload"]["opinions"], [])
            self.assertFalse(item["payload"]["conflict"])
            # 原审计记录继续可查，迁移本身也有审计事件
            trail = repo.audit_trail(item_id)
            self.assertEqual(trail[0]["event_type"], "created")
            self.assertEqual(trail[0]["actor"], "analyst-old")
            self.assertEqual(trail[-1]["event_type"], "migrated")
            self.assertIn("observation_version", trail[-1]["payload"]["backfilled"])
            # 迁移是幂等的，不会重复补写
            repo.initialize()
            self.assertEqual(len(repo.audit_trail(item_id)), 2)
            # 迁移后的事件可以走新流程
            service = Service(repo)
            item = service.act(item_id, "report_revision", {
                "observed_at": "2026-10-01T03:00:00Z",
                "miss_distance_m": 60,
                "covariance_m": 80,
                "source": "radar-legacy",
            }, "analyst-1", "analyst", item["version"])
            self.assertEqual(item["payload"]["observation_version"], 2)
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(tmp.name + suffix)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
