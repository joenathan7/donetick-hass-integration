"""Per-member Donetick sensors: points plus a summary of their chores."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .model import DonetickMember

_LOGGER = logging.getLogger(__name__)


def user_chore_summary(user_id, tasks, completed, now: datetime) -> dict[str, Any]:
    """Summarize one member's pending and recently completed chores.

    Pending chores are active tasks currently assigned to the member, matching the
    per-assignee todo lists. Completed chores are history entries the member
    performed, from the coordinator's seven-day completion history. Dates are
    bucketed in the time zone of ``now``.
    """
    today = now.date()
    pending = sorted(
        (task for task in tasks or [] if task.is_active and task.assigned_to == user_id),
        # Undated tasks sort last; ties keep a stable order by name.
        key=lambda task: (task.next_due_date is None, task.next_due_date or now, task.name),
    )
    pending_items = []
    overdue = due_today = 0
    for task in pending:
        due = task.next_due_date.astimezone(now.tzinfo) if task.next_due_date else None
        is_overdue = due is not None and due < now
        is_due_today = due is not None and due.date() == today
        overdue += is_overdue
        due_today += is_due_today and not is_overdue
        pending_items.append({
            "task_id": task.id,
            "name": task.name,
            "next_due_date": task.next_due_date.isoformat() if task.next_due_date else None,
            "points": task.points,
            "overdue": is_overdue,
        })

    done = [item for item in completed or [] if item.get("completed_by") == user_id]
    done_today = sum(
        datetime.fromisoformat(item["completed_at"]).astimezone(now.tzinfo).date() == today
        for item in done
    )
    return {
        "pending_count": len(pending_items),
        "overdue_count": overdue,
        "due_today_count": due_today,
        "completed_today_count": done_today,
        "completed_last_7_days_count": len(done),
        "pending_tasks": pending_items,
        "completed_tasks": [
            {"task_id": item["task_id"], "name": item["name"], "completed_at": item["completed_at"]}
            for item in done
        ],
    }


async def async_setup_user_sensors(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a sensor for each active circle member, and for members who join later."""
    config = hass.data[DOMAIN][config_entry.entry_id]
    coordinator = config["coordinator"]
    known_ids: set[int] = set()

    @callback
    def _add_new_members() -> None:
        members: list[DonetickMember] = config.get("circle_members") or []
        new = [m for m in members if m.is_active and m.user_id not in known_ids]
        if new:
            known_ids.update(m.user_id for m in new)
            _LOGGER.debug("Creating %d Donetick user sensors", len(new))
            async_add_entities([DonetickUserSensor(coordinator, config_entry, m) for m in new])

    _add_new_members()
    config_entry.async_on_unload(coordinator.async_add_listener(_add_new_members))


class DonetickUserSensor(CoordinatorEntity, SensorEntity):
    """A circle member's points, with their chore summary as attributes."""

    _attr_has_entity_name = True
    _attr_name = "Points"
    _attr_icon = "mdi:star-circle"
    _attr_native_unit_of_measurement = "points"
    _attr_state_class = SensorStateClass.TOTAL
    # Task lists change often and can be long; keep them out of the recorder.
    _unrecorded_attributes = frozenset({"pending_tasks", "completed_tasks", "image"})

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry: ConfigEntry,
        member: DonetickMember,
    ) -> None:
        """Initialize the user sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._user_id = member.user_id
        self._fallback_name = member.display_name or member.username
        self._attr_unique_id = f"dt_user_{config_entry.entry_id}_{member.user_id}"

    @property
    def _member(self) -> DonetickMember | None:
        """Return the latest copy of this member, refreshed on every poll."""
        config = self.hass.data[DOMAIN].get(self._config_entry.entry_id, {})
        return next((m for m in config.get("circle_members") or [] if m.user_id == self._user_id), None)

    @property
    def available(self) -> bool:
        """Return False once the member leaves the circle or is deactivated."""
        member = self._member
        return member is not None and member.is_active and super().available

    @property
    def native_value(self) -> int | None:
        """Return the member's points as reported by Donetick."""
        member = self._member
        return member.points if member else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the member and their chore summary."""
        member = self._member
        if member is None:
            return {}
        return {
            "user_id": member.user_id,
            "display_name": member.display_name,
            "username": member.username,
            "role": member.role,
            "image": member.image,
            "points_redeemed": member.points_redeemed,
            "points_available": member.points - member.points_redeemed,
            **user_chore_summary(
                member.user_id,
                self.coordinator.data,
                getattr(self.coordinator, "completed_tasks", []),
                dt_util.now(),
            ),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        """Group each member's entities under their own device."""
        return {
            "identifiers": {(DOMAIN, f"user_{self._config_entry.entry_id}_{self._user_id}")},
            "name": self._fallback_name,
            "manufacturer": "Donetick",
            "model": "Circle member",
        }
