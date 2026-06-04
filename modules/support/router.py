from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict

from modules.support.base_fsm import Message, MessageID
from modules.support.log_utils import get_logger

RouteCondition = Callable[[Message], bool]
RouteMessageFactory = Callable[[str, Message], Message]

SENSOR_MODULES = {
    "AHT10",
    "AIS",
    "MPU6050",
    "Windsonic",
    "XTRA2210",
}
AUDIO_MODULE = "AudioProc"
IRIDIUM_MODULE = "Iridium"


@dataclass
class RouteRule:
    origin: str
    message_id: MessageID
    target: str
    condition: RouteCondition = field(default=lambda _: True)
    make_message: RouteMessageFactory = field(default=lambda origin, message: message)


class Router:
    def __init__(self, logger_name: str = "router"):
        self.logger = get_logger(logger_name)
        self.queues: Dict[str, Any] = {}
        self.rules: list[RouteRule] = self._default_routes()
        self.latest_sensor_readings: Dict[str, dict[str, Any]] = {}
        self.latest_audio_summary: dict[str, Any] | None = None
        self.last_transmit_at: datetime | None = None

    def _default_routes(self) -> list[RouteRule]:
        return [
            RouteRule(
                origin="Behringer",
                message_id=MessageID.ACTION_RESULT,
                target=AUDIO_MODULE,
                condition=lambda message: bool(message.params.get("file")),
                make_message=lambda origin, message: Message(
                    MessageID.SIG_PROCESS,
                    {
                        "file": message.params["file"],
                        "origin": origin,
                    },
                ),
            ),
            RouteRule(
                origin="Behringer",
                message_id=MessageID.RECORDING_DONE,
                target=AUDIO_MODULE,
                condition=lambda message: bool(message.params.get("file")),
                make_message=lambda origin, message: Message(
                    MessageID.SIG_PROCESS,
                    {
                        "file": message.params["file"],
                        "origin": origin,
                    },
                ),
            ),
        ]

    def register(self, name: str, queue: Any) -> None:
        self.logger.info("Registering FSM queue: %s", name)
        self.queues[name] = queue

    def unregister(self, name: str) -> None:
        self.logger.info("Unregistering FSM queue: %s", name)
        self.queues.pop(name, None)

    def send(self, target: str, message: Message) -> None:
        if target not in self.queues:
            self.logger.warning("Cannot route to unknown target: %s", target)
            return
        self.logger.info("Routing message to %s: %s", target, message)
        self.queues[target].put(message)

    def route(self, origin: str, message: Message) -> bool:
        if origin in SENSOR_MODULES and message.id == MessageID.ACTION_RESULT:
            self._store_sensor_reading(origin, message)
            return False

        if origin == AUDIO_MODULE and message.id == MessageID.ACTION_RESULT:
            if message.params.get("output"):
                self.latest_audio_summary = self._compact_audio_message(message)
                return self._route_compact_telemetry_to_iridium(origin, message)

        for rule in self.rules:
            if rule.origin != origin or rule.message_id != message.id:
                continue
            if not rule.condition(message):
                continue

            if rule.target not in self.queues:
                self.logger.warning("Route target not registered: %s", rule.target)
                return False

            routed_message = rule.make_message(origin, message)
            self.send(rule.target, routed_message)
            return True

        self.logger.debug("No route matched for origin=%s message=%s", origin, message.id)
        return False

    def _store_sensor_reading(self, origin: str, message: Message) -> None:
        self.latest_sensor_readings[origin] = self._compact_sensor_message(origin, message)
        self.logger.info("Stored latest sensor reading for %s", origin)

    def _compact_sensor_message(self, origin: str, message: Message) -> dict[str, Any]:
        payload = message.params.get("data", {})
        if origin == "AHT10":
            return {
                "temperature_c": payload.get("temperature_c"),
                "humidity_rh": payload.get("humidity_rh"),
            }
        if origin == "AIS":
            return {
                "navigation": payload.get("navigation"),
                "fix": payload.get("navigation", {}).get("fix"),
            }
        if origin == "MPU6050":
            return {"motion": payload}
        if origin == "Windsonic":
            return {"wind": payload}
        if origin == "XTRA2210":
            return {"energy": payload}
        return {"data": payload}

    def _compact_audio_message(self, message: Message) -> dict[str, Any]:
        return {
            "input": message.params.get("input"),
            "output": message.params.get("output"),
            "details": message.params.get("details", {}),
        }

    def _route_compact_telemetry_to_iridium(self, origin: str, message: Message) -> bool:
        if IRIDIUM_MODULE not in self.queues:
            self.logger.warning("Iridium queue is not registered. Cannot send telemetry.")
            return False

        telemetry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "origin": origin,
            "audio": self.latest_audio_summary,
            "sensors": self.latest_sensor_readings,
        }

        payload_text = json.dumps(telemetry, separators=(",", ":"), ensure_ascii=False)
        transmit_message = Message(
            MessageID.SIG_TRANSMIT,
            {
                "mode": "text",
                "text": payload_text,
                "origin": "Router",
            },
        )
        self.send(IRIDIUM_MODULE, transmit_message)
        self.last_transmit_at = datetime.utcnow()
        self.logger.info("Sent compact telemetry to Iridium with %s sensor modules", len(self.latest_sensor_readings))
        return True
