"""Equal timestamps must not make random identity dictate conversation order."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import multi_agent_runtime as runtime


class MessageOrderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.db"
        self.ids = iter(f"msg_{number:032x}" for number in range(100, 0, -1))
        original = runtime._new_id
        self.id_patch = patch.object(runtime, "_new_id", side_effect=lambda prefix: next(self.ids) if prefix == "msg" else original(prefix))
        self.id_patch.start()
        self.addCleanup(self.id_patch.stop)
        self.clock_patch = patch.object(runtime.time, "time", return_value=1000.0)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def agents(self, store):
        agents = []
        for name in ("First", "Second", "Outside"):
            agent = store.create_agent(display_name=name, role="fixture", idempotency_key="create-" + name)
            bound = store.bind_agent(agent.agent_id)
            bound.start(idempotency_key="start-" + name)
            agents.append(bound)
        return agents

    @staticmethod
    def message_ids(messages):
        return [message.message_id for message in messages]

    def test_direct_ties_preserve_send_order_filters_receipts_limits_and_restart(self):
        with runtime.MultiAgentRuntimeStore(self.path) as store:
            first, second, outside = self.agents(store)
            first_id, second_id = first.profile().agent_id, second.profile().agent_id
            request = first.send_message(second_id, "request", idempotency_key="request")
            response = second.reply(request.message_id, "response", idempotency_key="response")
            followup = first.send_message(second_id, "followup", idempotency_key="followup")
            fourth = store.create_agent(display_name="Fourth", role="fixture", idempotency_key="fourth")
            outside.send_message(fourth.agent_id, "unrelated", idempotency_key="unrelated")
            retry = first.send_message(second_id, "request", idempotency_key="request")
            self.assertEqual(retry.message_id, request.message_id)
            self.assertGreater(request.message_id, response.message_id)
            self.assertGreater(response.message_id, followup.message_id)
            self.assertEqual(request.created_at, followup.created_at)
            expected = [request.message_id, response.message_id, followup.message_id]
            self.assertEqual(self.message_ids(store.mailbox(first_id, include_sent=True)), expected)
            self.assertEqual(self.message_ids(store.mailbox(first_id, include_sent=True, limit=2)), expected[:2])
            store.acknowledge_direct_message(recipient_id=second_id, message_id=request.message_id, read=False, idempotency_key="delivered")
            self.assertEqual(self.message_ids(store.unread_direct_messages(second_id)), [request.message_id, followup.message_id])
        with runtime.MultiAgentRuntimeStore(self.path) as store:
            self.assertEqual(self.message_ids(store.mailbox(first_id, include_sent=True)), expected)
            self.assertEqual(self.message_ids(store.mailbox(second_id)), [request.message_id, followup.message_id])
            self.assertEqual(self.message_ids(store.mailbox(second_id, limit=1)), [request.message_id])
            self.assertEqual(self.message_ids(store.unread_direct_messages(second_id, limit=1)), [request.message_id])
            store.acknowledge_direct_message(recipient_id=second_id, message_id=request.message_id, read=True, idempotency_key="read")
            self.assertEqual(self.message_ids(store.unread_direct_messages(second_id)), [followup.message_id])

    def test_room_ties_preserve_order_and_cursor_after_restart(self):
        with runtime.MultiAgentRuntimeStore(self.path) as store:
            first, second, outside = self.agents(store)
            first_id, second_id = first.profile().agent_id, second.profile().agent_id
            room = first.create_room("Fixture", idempotency_key="room")
            first.invite(room.room_id, second_id, idempotency_key="invite")
            second.join_room(room.room_id, idempotency_key="join")
            one = first.send_room_message(room.room_id, "one", idempotency_key="one")
            two = second.send_room_message(room.room_id, "two", idempotency_key="two")
            three = first.send_room_message(room.room_id, "three", idempotency_key="three")
            other_room = outside.create_room("Other", idempotency_key="other-room")
            outside.send_room_message(other_room.room_id, "excluded", idempotency_key="excluded")
            expected = [one.message_id, two.message_id, three.message_id]
            self.assertEqual(one.created_at, three.created_at)
            self.assertEqual(sorted(expected, reverse=True), expected)
            self.assertEqual(self.message_ids(store.room_messages(first_id, room.room_id)), expected)
            self.assertEqual(self.message_ids(store.room_messages(second_id, room.room_id, limit=2)), expected[:2])
            self.assertEqual(self.message_ids(store.unread_room_messages(second_id, room.room_id)), expected)
            store.advance_room_message_cursor(agent_id=second_id, room_id=room.room_id, through_message_id=one.message_id, idempotency_key="cursor")
            with self.assertRaises(runtime.RuntimePermissionError):
                store.room_messages(outside.profile().agent_id, room.room_id)
        with runtime.MultiAgentRuntimeStore(self.path) as store:
            self.assertEqual(self.message_ids(store.room_messages(second_id, room.room_id)), expected)
            self.assertEqual(self.message_ids(store.unread_room_messages(second_id, room.room_id, limit=1)), [two.message_id])
            self.assertEqual(self.message_ids(store.unread_room_messages(first_id, room.room_id)), expected)
            store.advance_room_message_cursor(agent_id=second_id, room_id=room.room_id, through_message_id=two.message_id, idempotency_key="cursor-two")
            self.assertEqual(self.message_ids(store.unread_room_messages(second_id, room.room_id)), [three.message_id])


if __name__ == "__main__":
    unittest.main()
