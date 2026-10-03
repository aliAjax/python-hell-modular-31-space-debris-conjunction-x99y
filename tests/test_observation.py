import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError


def _item_payload():
    return {
        "primary_object_id": "SAT-1",
        "secondary_object_id": "DEB-9",
        "tca": "2026-09-28T12:00:00+00:00",
        "miss_distance_m": 120,
        "covariance_m": 100,
        "fuel_budget_m_s": 5,
        "track_age_hours": 1,
        "operating_organizations": ["Org-A"],
    }


def _source_payload(external_id, observed_at="2026-09-27T12:00:00+00:00"):
    return {
        "source_type": "radar",
        "external_id": external_id,
        "observed_at": observed_at,
        "miss_distance_m": 130,
        "covariance_m": 110,
    }


class ObservationUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _seed_approved(self):
        item = self.service.create_item(_item_payload(), "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        item = self.service.act(
            item["id"], "approve",
            {"fuel_cost_m_s": 2.5, "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z"},
            "coordinator-1", "coordinator", item["version"],
        )
        return item

    def test_revision_invalidates_assessment_plan_and_signatures(self):
        item = self._seed_approved()
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(item["payload"]["observation_version"], 1)

        # 运营方签字
        item = self.service.act(
            item["id"], "record_opinion",
            {"operator": "Org-A", "opinion": "approve"},
            "operator-1", "operator", item["version"],
        )
        self.assertEqual(len(item["payload"]["opinions"]), 1)

        # 轨道观测更新
        item = self.service.act(
            item["id"], "report_revision",
            {"observed_at": "2026-09-27T12:00:00+00:00", "miss_distance_m": 200, "covariance_m": 150, "source": "radar-2"},
            "analyst-1", "analyst", item["version"],
        )
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["payload"]["observation_version"], 2)
        self.assertNotIn("assessment", item["payload"])
        self.assertNotIn("approved_maneuver", item["payload"])
        self.assertEqual(item["payload"]["opinions"], [])
        self.assertFalse(item["payload"]["conflict"])

        # 旧评估已失效，不能直接批准
        with self.assertRaises(DomainError) as context:
            self.service.act(
                item["id"], "approve",
                {"fuel_cost_m_s": 2.5, "maneuver_window": "w"},
                "coordinator-1", "coordinator", item["version"],
            )
        self.assertEqual(context.exception.code, "invalid_state")

        # 重走审批：重新评估 -> 批准
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        self.assertEqual(item["status"], "assessed")
        self.assertEqual(item["payload"]["assessment"]["observation_version"], 2)
        item = self.service.act(
            item["id"], "approve",
            {"fuel_cost_m_s": 2.5, "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z"},
            "coordinator-1", "coordinator", item["version"],
        )
        self.assertEqual(item["status"], "coordinating")
        self.assertEqual(item["payload"]["approved_maneuver"]["observation_version"], 2)

    def test_source_submission_requires_version_and_conflicts_on_stale(self):
        item = self._seed_approved()
        v3 = item["version"]
        self.assertEqual(v3, 3)

        # 分析员 A 提交观测
        result_a = self.service.add_source(item["id"], _source_payload("RAD-A"), "analyst-a", "analyst", expected_version=v3)
        self.assertEqual(result_a["version"], 4)
        self.assertEqual(result_a["observation_version"], 2)

        # 分析员 B 用旧版本提交 -> 版本冲突
        with self.assertRaises(ConflictError) as context:
            self.service.add_source(item["id"], _source_payload("RAD-B"), "analyst-b", "analyst", expected_version=v3)
        self.assertEqual(context.exception.code, "version_conflict")

        # B 重新读取，基于最新结果重算后再提交，不能覆盖 A 的结果
        item = self.service.get_item(item["id"])
        self.assertEqual(item["version"], 4)
        self.assertEqual(item["status"], "pending")
        result_b = self.service.add_source(item["id"], _source_payload("RAD-B"), "analyst-b", "analyst", expected_version=item["version"])
        self.assertEqual(result_b["version"], 5)
        self.assertEqual(result_b["observation_version"], 3)

        sources = self.service.repository.list_sources(item["id"])
        external_ids = {s["external_id"] for s in sources}
        self.assertIn("RAD-A", external_ids)
        self.assertIn("RAD-B", external_ids)

    def test_source_submission_without_version_is_rejected(self):
        item = self.service.create_item(_item_payload(), "analyst-1", "analyst")
        with self.assertRaises(DomainError) as context:
            self.service.add_source(item["id"], _source_payload("RAD-X"), "analyst-1", "analyst")
        self.assertEqual(context.exception.code, "expected_version_required")


class ExecutionFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _seed_approved(self):
        item = self.service.create_item(_item_payload(), "analyst-1", "analyst")
        item = self.service.act(item["id"], "assess", {"hours_to_tca": 18}, "analyst-1", "analyst", item["version"])
        item = self.service.act(
            item["id"], "approve",
            {"fuel_cost_m_s": 2.5, "maneuver_window": "2026-09-28T08:00:00Z/2026-09-28T09:00:00Z"},
            "coordinator-1", "coordinator", item["version"],
        )
        return item

    def test_failure_keeps_window_and_retries_unfinished_action(self):
        item = self._seed_approved()
        window = item["payload"]["approved_maneuver"]["maneuver_window"]

        # 第一次执行失败
        item = self.service.act(
            item["id"], "report_execution_failure",
            {"reason": "地面站上行中断"},
            "operator-1", "operator", item["version"],
        )
        self.assertEqual(item["status"], "coordinating")
        # 机动窗口保留
        self.assertEqual(item["payload"]["approved_maneuver"]["maneuver_window"], window)
        self.assertEqual(item["payload"]["execution"]["status"], "failed")
        self.assertEqual(len(item["payload"]["execution"]["attempts"]), 1)
        self.assertEqual(item["payload"]["execution"]["attempts"][0]["status"], "failed")

        # 只重试未完成的 execute，不重走审批
        item = self.service.act(
            item["id"], "execute",
            {"command_ref": "CMD-RETRY"},
            "operator-1", "operator", item["version"],
        )
        self.assertEqual(item["status"], "executing")
        self.assertEqual(item["payload"]["execution"]["status"], "succeeded")
        attempts = item["payload"]["execution"]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status"], "failed")
        self.assertEqual(attempts[1]["status"], "succeeded")
        self.assertEqual(attempts[1]["command_ref"], "CMD-RETRY")
        self.assertEqual(item["payload"]["approved_maneuver"]["maneuver_window"], window)

        # 结束
        item = self.service.act(
            item["id"], "resolve",
            {"report_ref": "RPT-7"},
            "coordinator-1", "coordinator", item["version"],
        )
        self.assertEqual(item["status"], "resolved")

    def test_resolve_requires_successful_execution(self):
        item = self._seed_approved()
        item = self.service.act(
            item["id"], "report_execution_failure",
            {"reason": "上行中断"},
            "operator-1", "operator", item["version"],
        )
        with self.assertRaises(DomainError) as context:
            self.service.act(
                item["id"], "resolve",
                {"report_ref": "RPT-X"},
                "coordinator-1", "coordinator", item["version"],
            )
        self.assertEqual(context.exception.code, "invalid_state")


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _insert_legacy_item(self):
        conn = self.repo.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            payload = {
                "primary_object_id": "SAT-OLD",
                "secondary_object_id": "DEB-OLD",
                "tca": "2026-09-28T12:00:00+00:00",
                "miss_distance_m": 100,
                "covariance_m": 100,
                "fuel_budget_m_s": 5,
                "track_age_hours": 1,
                "operating_organizations": [],
                "revisions": [],
                "opinions": [],
                "conflict": False,
            }
            conn.execute(
                "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "space_conjunction",
                    "SAT-OLD|DEB-OLD|2026-09-28T12:00:00+00:00",
                    "pending",
                    1,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "a",
                    "analyst",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.repo.append_audit(conn, item_id, "created", "a", "analyst", {"stable_key": "SAT-OLD|DEB-OLD"})
            conn.execute("COMMIT")
            return item_id
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def test_migration_backfills_observation_version_and_keeps_audit(self):
        item_id = self._insert_legacy_item()

        # 升级前：缺少 observation_version
        conn = self.repo.connect()
        try:
            raw = conn.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
            self.assertNotIn("observation_version", json.loads(raw["payload"]))
        finally:
            conn.close()

        # 执行升级
        self.repo.initialize()

        item = self.repo.get_item(item_id)
        self.assertEqual(item["payload"]["observation_version"], 1)

        # 原审计记录继续可查
        trail = self.repo.audit_trail(item_id)
        self.assertGreaterEqual(len(trail), 1)
        self.assertEqual(trail[0]["event_type"], "created")

        # 幂等：再次升级不变
        self.repo.initialize()
        item = self.repo.get_item(item_id)
        self.assertEqual(item["payload"]["observation_version"], 1)


if __name__ == "__main__":
    unittest.main()
