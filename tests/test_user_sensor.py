"""Check per-member points sensors and their chore summary without an HA installation."""
import ast
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
import unittest


ROOT = Path(__file__).resolve().parents[1] / "custom_components/donetick_dev"
LOCAL = timezone(timedelta(hours=1))
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=LOCAL)


def load_definitions(filename, names, namespace):
    source = ROOT / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    definitions = [node for node in tree.body if getattr(node, "name", None) in names]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *definitions], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)


class CoordinatorStub:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return True


class UserSensorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = dict(dataclass=dataclass, datetime=datetime, Any=Any, List=List,
                         Optional=Optional, _LOGGER=logging.getLogger(__name__),
                         CoordinatorEntity=CoordinatorStub, SensorEntity=type("SensorStub", (), {}),
                         SensorStateClass=SimpleNamespace(TOTAL="total"), DOMAIN="donetick",
                         dt_util=SimpleNamespace(now=lambda: NOW))
        load_definitions("model.py", {"_parse_datetime", "DonetickTask", "DonetickMember"}, namespace)
        load_definitions("user_sensor.py", {"user_chore_summary", "DonetickUserSensor"}, namespace)
        cls.ns = namespace

    def task(self, task_id, assigned_to=7, due=None, **fields):
        data = {"id": task_id, "name": f"Task {task_id}", "assignedTo": assigned_to, **fields}
        if due is not None:
            data["nextDueDate"] = due.isoformat()
        return self.ns["DonetickTask"].from_json(data)

    def member(self, user_id=7, **fields):
        return self.ns["DonetickMember"].from_json({
            "id": 1, "userId": user_id, "circleId": 1, "role": "member", "isActive": True,
            "username": "sam", "displayName": "Sam", "points": 40, "pointsRedeemed": 15, **fields})

    def done(self, user_id, when, task_id=1):
        return {"task_id": task_id, "name": f"Task {task_id}", "completed_by": user_id,
                "completed_at": when.isoformat()}

    def summary(self, tasks=(), completed=()):
        return self.ns["user_chore_summary"](7, list(tasks), list(completed), NOW)

    def test_pending_counts_only_active_tasks_assigned_to_the_member(self):
        tasks = [
            self.task(1, due=NOW - timedelta(days=1)),                 # overdue
            self.task(2, due=NOW + timedelta(hours=3)),                # due later today
            self.task(3, due=NOW + timedelta(days=3)),                 # upcoming
            self.task(4),                                              # no due date
            self.task(5, assigned_to=8, due=NOW),                      # someone else
            self.task(6, assigned_to=None, due=NOW),                   # Anyone
            self.task(7, due=NOW, isActive=False),                     # archived
        ]
        result = self.summary(tasks)
        self.assertEqual(result["pending_count"], 4)
        self.assertEqual(result["overdue_count"], 1)
        self.assertEqual(result["due_today_count"], 1)
        self.assertEqual([t["task_id"] for t in result["pending_tasks"]], [1, 2, 3, 4])
        self.assertEqual([t["overdue"] for t in result["pending_tasks"]], [True, False, False, False])

    def test_overdue_task_earlier_today_is_not_also_due_today(self):
        result = self.summary([self.task(1, due=NOW - timedelta(hours=1))])
        self.assertEqual((result["overdue_count"], result["due_today_count"]), (1, 0))

    def test_due_today_uses_local_calendar_day(self):
        # 23:30 UTC on the 10th is 00:30 local on the 11th.
        due = datetime(2026, 3, 10, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(self.summary([self.task(1, due=due)])["due_today_count"], 0)

    def test_completed_counts_by_performer_and_local_day(self):
        completed = [
            self.done(7, NOW - timedelta(hours=2), task_id=1),
            self.done(7, NOW - timedelta(days=2), task_id=2),
            self.done(8, NOW - timedelta(hours=1), task_id=3),
            # 23:30 UTC on the 9th is 00:30 local on the 10th, so it counts as today.
            self.done(7, datetime(2026, 3, 9, 23, 30, tzinfo=timezone.utc), task_id=4),
        ]
        result = self.summary(completed=completed)
        self.assertEqual(result["completed_today_count"], 2)
        self.assertEqual(result["completed_last_7_days_count"], 3)
        self.assertEqual([t["task_id"] for t in result["completed_tasks"]], [1, 2, 4])

    def test_empty_data(self):
        result = self.ns["user_chore_summary"](7, None, None, NOW)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["completed_tasks"], [])

    def sensor(self, members, tasks=(), completed=()):
        coordinator = SimpleNamespace(data=list(tasks), completed_tasks=list(completed))
        entry = SimpleNamespace(entry_id="entry")
        sensor = self.ns["DonetickUserSensor"](coordinator, entry, members[0])
        sensor.hass = SimpleNamespace(data={"donetick": {"entry": {"circle_members": members}}})
        return sensor

    def test_state_is_points_and_attributes_include_summary(self):
        sensor = self.sensor([self.member()], tasks=[self.task(1, due=NOW)])
        self.assertEqual(sensor.native_value, 40)
        attrs = sensor.extra_state_attributes
        self.assertEqual(attrs["points_available"], 25)
        self.assertEqual(attrs["points_redeemed"], 15)
        self.assertEqual(attrs["display_name"], "Sam")
        self.assertEqual(attrs["pending_count"], 1)
        self.assertEqual(sensor._attr_unique_id, "dt_user_entry_7")

    def test_points_follow_refreshed_members(self):
        sensor = self.sensor([self.member()])
        sensor.hass.data["donetick"]["entry"]["circle_members"] = [self.member(points=55)]
        self.assertEqual(sensor.native_value, 55)

    def test_unavailable_when_member_leaves_or_is_deactivated(self):
        sensor = self.sensor([self.member()])
        self.assertTrue(sensor.available)
        sensor.hass.data["donetick"]["entry"]["circle_members"] = [self.member(isActive=False)]
        self.assertFalse(sensor.available)
        sensor.hass.data["donetick"]["entry"]["circle_members"] = []
        self.assertFalse(sensor.available)
        self.assertIsNone(sensor.native_value)
        self.assertEqual(sensor.extra_state_attributes, {})


if __name__ == "__main__":
    unittest.main()
