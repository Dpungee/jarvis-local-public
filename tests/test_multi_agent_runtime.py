from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from jarvis.multi_agent_runtime import (
    AgentAuthority,
    AgentAutonomy,
    AgentLifecycle,
    CapabilityRequestPolicy,
    CapabilityRequestStatus,
    CollaborationKind,
    CollaborationStatus,
    MultiAgentRuntimeStore,
    RoomAccess,
    RuntimeConflictError,
    RuntimeNotFoundError,
    RuntimePermissionError,
    RuntimeScope,
    RuntimeStoreError,
    TaskStatus,
)


class MultiAgentRuntimeAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "multi-agent-runtime.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _create_two_agents(store: MultiAgentRuntimeStore):
        scout = store.create_agent(
            display_name="Scout",
            role="Evidence researcher",
            purpose="Find and verify primary evidence.",
            personality="Precise and curious",
            specialties=("research", "evidence"),
            model_provider="provider-a",
            model_name="research-model",
            project_id="project-aurora",
            idempotency_key="acceptance-create-scout",
        )
        builder = store.create_agent(
            display_name="Builder",
            role="Implementation engineer",
            purpose="Turn verified plans into tested code.",
            personality="Practical and explicit",
            specialties=("implementation", "testing"),
            model_provider="provider-b",
            model_name="coding-model",
            project_id="project-aurora",
            idempotency_key="acceptance-create-builder",
        )
        scout_context = store.bind_agent(scout.agent_id)
        builder_context = store.bind_agent(builder.agent_id)
        scout_context.start(idempotency_key="acceptance-start-scout")
        builder_context.start(idempotency_key="acceptance-start-builder")
        return scout, builder, scout_context, builder_context

    def test_two_agents_discover_message_room_delegate_and_resume_after_restart(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, builder, scout_context, builder_context = self._create_two_agents(
                store
            )

            discovered = scout_context.find_agents(specialty="implementation")
            self.assertEqual(
                [builder.agent_id], [agent.agent_id for agent in discovered]
            )

            request = scout_context.send_message(
                builder.agent_id,
                "Can you implement the parser after I verify the fixture?",
                idempotency_key="acceptance-direct-request",
            )
            response = builder_context.reply(
                request.message_id,
                "Yes. Send the evidence in our room and delegate the task.",
                idempotency_key="acceptance-direct-response",
            )

            room = scout_context.create_room(
                "Parser collaboration",
                kind="task",
                access=RoomAccess.INVITE_ONLY,
                idempotency_key="acceptance-create-room",
            )
            scout_context.invite(
                room.room_id,
                builder.agent_id,
                idempotency_key="acceptance-invite-builder",
            )
            builder_context.join_room(
                room.room_id, idempotency_key="acceptance-join-builder"
            )
            room_message = scout_context.send_room_message(
                room.room_id,
                "The fixture is verified; use boundary case 17.",
                idempotency_key="acceptance-room-message",
            )

            task = scout_context.create_task(
                "Implement the parser",
                "Implement the verified parser and its boundary-case test.",
                idempotency_key="acceptance-create-task",
            )
            delegation = scout_context.delegate_task(
                task.task_id,
                builder.agent_id,
                "Builder has the implementation specialty.",
                idempotency_key="acceptance-delegate-task",
            )
            event_ids = [event.event_id for event in store.events()]
            event_sequences = [event.sequence for event in store.events()]
            self.assertEqual(sorted(event_sequences), event_sequences)
            self.assertEqual(len(event_ids), len(set(event_ids)))

        with MultiAgentRuntimeStore(self.path) as restarted:
            resumed_scout = restarted.bind_agent(scout.agent_id)
            resumed_builder = restarted.bind_agent(builder.agent_id)

            self.assertEqual(AgentLifecycle.RUNNING, resumed_scout.profile().lifecycle)
            self.assertEqual("coding-model", resumed_builder.profile().model_name)
            self.assertEqual(
                [request.message_id],
                [message.message_id for message in resumed_builder.mailbox()],
            )
            self.assertEqual(
                [request.message_id, response.message_id],
                [
                    message.message_id
                    for message in resumed_scout.mailbox(include_sent=True)
                ],
            )
            self.assertEqual(
                {scout.agent_id, builder.agent_id},
                {
                    agent.agent_id
                    for agent in resumed_builder.room_members(room.room_id)
                },
            )
            self.assertEqual(
                [room_message.message_id],
                [
                    message.message_id
                    for message in resumed_builder.room_messages(room.room_id)
                ],
            )
            owned = resumed_builder.tasks(statuses=(TaskStatus.ASSIGNED,))
            self.assertEqual([task.task_id], [item.task_id for item in owned])
            self.assertEqual(
                builder.agent_id, restarted.get_task(task.task_id).owner_id
            )
            self.assertEqual(
                task.task_id, restarted.get_delegation(delegation.delegation_id).task_id
            )

            events = restarted.events()
            self.assertEqual(event_ids, [event.event_id for event in events])
            self.assertNotIn("jarvis", {event.actor_id.casefold() for event in events})
            self.assertEqual(
                scout.agent_id,
                next(
                    event.actor_id
                    for event in events
                    if event.event_type == "task.delegated"
                ),
            )

    def test_agent_creation_and_send_are_retry_safe_but_key_reuse_conflicts(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, builder, scout_context, _builder_context = self._create_two_agents(
                store
            )
            replayed = store.create_agent(
                display_name="Scout",
                role="Evidence researcher",
                purpose="Find and verify primary evidence.",
                personality="Precise and curious",
                specialties=("evidence", "research"),
                model_provider="provider-a",
                model_name="research-model",
                project_id="project-aurora",
                idempotency_key="acceptance-create-scout",
            )
            self.assertEqual(scout.agent_id, replayed.agent_id)

            first = scout_context.send_message(
                builder.agent_id,
                "One durable command",
                idempotency_key="retry-safe-message",
            )
            second = scout_context.send_message(
                builder.agent_id,
                "One durable command",
                idempotency_key="retry-safe-message",
            )
            self.assertEqual(first.message_id, second.message_id)
            self.assertEqual(
                1,
                len(
                    [
                        event
                        for event in store.events()
                        if event.idempotency_key == "retry-safe-message"
                    ]
                ),
            )
            with self.assertRaisesRegex(RuntimeConflictError, "different command"):
                scout_context.send_message(
                    builder.agent_id,
                    "A different body",
                    idempotency_key="retry-safe-message",
                )

    def test_room_and_task_authority_are_owned_by_peer_state(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, _builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            outsider = store.create_agent(
                display_name="Outsider",
                role="Independent reviewer",
                project_id="project-aurora",
                idempotency_key="authority-create-outsider",
            )
            outsider_context = store.bind_agent(outsider.agent_id)
            outsider_context.start(idempotency_key="authority-start-outsider")

            room = scout_context.create_room(
                "Private work",
                idempotency_key="authority-create-room",
            )
            with self.assertRaises(RuntimePermissionError):
                outsider_context.join_room(
                    room.room_id, idempotency_key="authority-uninvited-join"
                )
            with self.assertRaises(RuntimePermissionError):
                outsider_context.send_room_message(
                    room.room_id,
                    "I should not be able to write this.",
                    idempotency_key="authority-unjoined-send",
                )
            with self.assertRaises(RuntimePermissionError):
                outsider_context.room_messages(room.room_id)

            task = scout_context.create_task(
                "Owned work",
                "The current owner alone may delegate.",
                idempotency_key="authority-create-task",
            )
            with self.assertRaisesRegex(RuntimePermissionError, "current task owner"):
                builder_context.delegate_task(
                    task.task_id,
                    outsider.agent_id,
                    "Attempt to steal ownership",
                    idempotency_key="authority-invalid-delegation",
                )
            self.assertEqual(scout.agent_id, store.get_task(task.task_id).owner_id)

    def test_paused_agent_cannot_send_but_can_resume_with_same_identity(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, builder, scout_context, _builder_context = self._create_two_agents(
                store
            )
            paused = scout_context.pause(idempotency_key="lifecycle-pause")
            self.assertEqual(AgentLifecycle.PAUSED, paused.lifecycle)
            with self.assertRaisesRegex(RuntimePermissionError, "RUNNING"):
                scout_context.send_message(
                    builder.agent_id,
                    "This must not send while paused.",
                    idempotency_key="lifecycle-paused-send",
                )
            resumed = scout_context.start(idempotency_key="lifecycle-resume")
            self.assertEqual(scout.agent_id, resumed.agent_id)
            sent = scout_context.send_message(
                builder.agent_id,
                "The same agent resumed.",
                idempotency_key="lifecycle-resumed-send",
            )
            self.assertEqual(scout.agent_id, sent.sender_id)

    def test_discovery_is_project_filtered_and_never_selects_a_peer(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, builder, scout_context, _builder_context = self._create_two_agents(
                store
            )
            other = store.create_agent(
                display_name="Other project agent",
                role="Implementation engineer",
                specialties=("implementation",),
                project_id="project-other",
                idempotency_key="discovery-create-other",
            )
            store.bind_agent(other.agent_id).start(
                idempotency_key="discovery-start-other"
            )

            visible = scout_context.find_agents(specialty="implementation")
            self.assertEqual([builder.agent_id], [agent.agent_id for agent in visible])
            self.assertNotEqual(builder.agent_id, scout.agent_id)

    def test_model_can_change_without_changing_agent_identity(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, _builder, scout_context, _builder_context = self._create_two_agents(
                store
            )
            changed = scout_context.change_model(
                "provider-c",
                "replacement-model",
                idempotency_key="model-change",
            )
            self.assertEqual(scout.agent_id, changed.agent_id)
            self.assertEqual("provider-c", changed.model_provider)
            self.assertEqual("replacement-model", changed.model_name)
            self.assertEqual(
                ["agent.created", "agent.running", "agent.model_changed"],
                [
                    event.event_type
                    for event in store.events(
                        subject_kind="agent", subject_id=scout.agent_id
                    )
                ],
            )

    def test_delegated_owner_runs_and_completes_task_and_room_member_can_leave(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _scout, builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            room = scout_context.create_room(
                "Finite collaboration", idempotency_key="finite-room"
            )
            scout_context.invite(
                room.room_id,
                builder.agent_id,
                idempotency_key="finite-invite",
            )
            builder_context.join_room(room.room_id, idempotency_key="finite-join")
            task = scout_context.create_task(
                "Finish a bounded task",
                "Return a durable result.",
                idempotency_key="finite-task",
            )
            scout_context.delegate_task(
                task.task_id,
                builder.agent_id,
                "The selected peer will execute it.",
                idempotency_key="finite-delegate",
            )
            running = builder_context.update_task(
                task.task_id,
                TaskStatus.RUNNING,
                idempotency_key="finite-running",
            )
            self.assertEqual(TaskStatus.RUNNING, running.status)
            completed = builder_context.update_task(
                task.task_id,
                TaskStatus.COMPLETED,
                result="Verified parser and boundary test.",
                idempotency_key="finite-completed",
            )
            self.assertEqual(TaskStatus.COMPLETED, completed.status)
            self.assertEqual("Verified parser and boundary test.", completed.result)

            builder_context.leave_room(room.room_id, idempotency_key="finite-leave")
            with self.assertRaises(RuntimePermissionError):
                builder_context.room_messages(room.room_id)

            report = store.verify_integrity()
            self.assertEqual(2, report.agent_count)
            self.assertEqual(1, report.room_count)
            self.assertEqual(1, report.task_count)
            self.assertEqual(1, report.delegation_count)

    def test_failed_causation_check_rolls_back_projection_and_event(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _scout, builder, scout_context, _builder_context = self._create_two_agents(
                store
            )
            before = store.verify_integrity()
            with self.assertRaisesRegex(RuntimeNotFoundError, "causation event"):
                store.send_direct_message(
                    sender_id=scout_context.agent_id,
                    recipient_id=builder.agent_id,
                    body="This projection must roll back.",
                    idempotency_key="invalid-causation",
                    causation_id="evt_00000000000000000000000000000000",
                )
            after = store.verify_integrity()
            self.assertEqual(before, after)
            self.assertEqual((), store.mailbox(builder.agent_id))
            self.assertFalse(
                any(
                    event.idempotency_key == "invalid-causation"
                    for event in store.events()
                )
            )

    def test_concurrent_retry_of_one_send_commits_one_message(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, builder, _scout_context, _builder_context = self._create_two_agents(
                store
            )

        barrier = Barrier(2)

        def send_once() -> str:
            with MultiAgentRuntimeStore(self.path) as worker_store:
                barrier.wait(timeout=5)
                message = worker_store.send_direct_message(
                    sender_id=scout.agent_id,
                    recipient_id=builder.agent_id,
                    body="One command retried by concurrent workers.",
                    idempotency_key="concurrent-send",
                )
                return message.message_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            message_ids = list(executor.map(lambda _value: send_once(), range(2)))

        self.assertEqual(1, len(set(message_ids)))
        with MultiAgentRuntimeStore(self.path) as restarted:
            self.assertEqual(1, len(restarted.mailbox(builder.agent_id)))
            self.assertEqual(
                1,
                len(
                    [
                        event
                        for event in restarted.events()
                        if event.idempotency_key == "concurrent-send"
                    ]
                ),
            )
            restarted.verify_integrity()

    def test_scoped_capability_grant_expires_revokes_and_survives_restart(self):
        future = time.time() + 3600
        with MultiAgentRuntimeStore(self.path) as store:
            scout, _builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            room = scout_context.create_room(
                "Capability scope", idempotency_key="capability-room"
            )
            scout_context.invite(
                room.room_id,
                builder_context.agent_id,
                idempotency_key="capability-invite",
            )
            builder_context.join_room(room.room_id, idempotency_key="capability-join")
            request = builder_context.request_capability(
                "artifact.publish",
                scope=RuntimeScope.ROOM,
                scope_id=room.room_id,
                reason="Publish the room's reviewed artifact.",
                idempotency_key="capability-request",
            )
            self.assertEqual(CapabilityRequestStatus.PENDING, request.status)
            with self.assertRaisesRegex(RuntimePermissionError, "owner authority"):
                store.decide_capability_request(
                    request.request_id,
                    actor_id=scout.agent_id,
                    grant=True,
                    decision_reason="A standard agent cannot grant this.",
                    expires_at=future,
                    idempotency_key="capability-unauthorized-decision",
                )
            grant = store.decide_capability_request(
                request.request_id,
                actor_id="owner",
                grant=True,
                decision_reason="Bounded to one room and one hour.",
                expires_at=future,
                idempotency_key="capability-grant",
            )
            replay = store.decide_capability_request(
                request.request_id,
                actor_id="owner",
                grant=True,
                decision_reason="Bounded to one room and one hour.",
                expires_at=future,
                idempotency_key="capability-grant",
            )
            self.assertEqual(grant.grant_id, replay.grant_id)
            self.assertTrue(
                builder_context.has_capability(
                    "artifact.publish",
                    scope=RuntimeScope.ROOM,
                    scope_id=room.room_id,
                )
            )
            self.assertFalse(
                builder_context.has_capability(
                    "artifact.publish",
                    scope=RuntimeScope.PROJECT,
                    scope_id="project-aurora",
                )
            )
            self.assertFalse(
                store.has_capability(
                    builder_context.agent_id,
                    "artifact.publish",
                    scope=RuntimeScope.ROOM,
                    scope_id=room.room_id,
                    at=future + 1,
                )
            )
            store.verify_integrity()

        with MultiAgentRuntimeStore(self.path) as restarted:
            self.assertTrue(
                restarted.has_capability(
                    builder_context.agent_id,
                    "artifact.publish",
                    scope=RuntimeScope.ROOM,
                    scope_id=room.room_id,
                )
            )
            revoked = restarted.revoke_capability(
                grant.grant_id,
                actor_id="owner",
                idempotency_key="capability-revoke",
            )
            self.assertIsNotNone(revoked.revoked_at)
            self.assertFalse(
                restarted.has_capability(
                    builder_context.agent_id,
                    "artifact.publish",
                    scope=RuntimeScope.ROOM,
                    scope_id=room.room_id,
                )
            )
            restarted.verify_integrity()

    def test_deny_request_policy_fails_closed_without_pending_request(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, _builder, scout_context, _builder_context = self._create_two_agents(
                store
            )
            changed = store.update_agent_policy(
                scout.agent_id,
                actor_id="owner",
                autonomy=AgentAutonomy.LOW,
                authority=AgentAuthority.SANDBOX,
                request_policy=CapabilityRequestPolicy.DENY,
                idempotency_key="policy-deny",
            )
            self.assertEqual(CapabilityRequestPolicy.DENY, changed.request_policy)
            request = scout_context.request_capability(
                "network.external",
                scope=RuntimeScope.PROJECT,
                scope_id="project-aurora",
                reason="Attempt a denied capability request.",
                idempotency_key="policy-denied-request",
            )
            self.assertEqual(CapabilityRequestStatus.DENIED, request.status)
            self.assertIn("request policy", request.decision_reason or "")
            with self.assertRaisesRegex(RuntimeConflictError, "already decided"):
                store.decide_capability_request(
                    request.request_id,
                    actor_id="owner",
                    grant=True,
                    decision_reason="Must remain denied.",
                    idempotency_key="policy-late-grant",
                )
            store.verify_integrity()

    def test_auto_project_policy_grants_only_matching_project_scope(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, _builder, scout_context, _builder_context = self._create_two_agents(
                store
            )
            store.update_agent_policy(
                scout.agent_id,
                actor_id="owner",
                autonomy=AgentAutonomy.HIGH,
                authority=AgentAuthority.STANDARD,
                request_policy=CapabilityRequestPolicy.AUTO_PROJECT,
                idempotency_key="policy-auto-project",
            )
            request = scout_context.request_capability(
                "artifact.share",
                scope=RuntimeScope.PROJECT,
                scope_id="project-aurora",
                reason="Share an artifact inside the current project.",
                idempotency_key="policy-auto-project-request",
            )
            self.assertEqual(CapabilityRequestStatus.GRANTED, request.status)
            self.assertTrue(
                scout_context.has_capability(
                    "artifact.share",
                    scope=RuntimeScope.PROJECT,
                    scope_id="project-aurora",
                )
            )
            capability_events = [
                event
                for event in store.events()
                if event.subject_id == request.request_id
            ]
            self.assertEqual(
                ["capability.requested", "capability.granted"],
                [event.event_type for event in capability_events],
            )
            self.assertEqual(
                capability_events[0].event_id, capability_events[1].causation_id
            )
            with self.assertRaisesRegex(RuntimePermissionError, "own project"):
                scout_context.request_capability(
                    "artifact.share",
                    scope=RuntimeScope.PROJECT,
                    scope_id="project-other",
                    reason="Must not cross project scope.",
                    idempotency_key="policy-auto-project-cross-scope",
                )
            store.verify_integrity()

    def test_direct_delivery_and_read_receipts_resume_after_restart(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _scout, _builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            message = scout_context.send_message(
                builder_context.agent_id,
                "Please acknowledge receipt before starting.",
                idempotency_key="receipt-send",
            )
            self.assertEqual(
                [message.message_id],
                [item.message_id for item in builder_context.unread_mailbox()],
            )
            with self.assertRaisesRegex(RuntimePermissionError, "recipient"):
                scout_context.acknowledge_message(
                    message.message_id,
                    read=False,
                    idempotency_key="receipt-wrong-agent",
                )
            delivered = builder_context.acknowledge_message(
                message.message_id,
                read=False,
                idempotency_key="receipt-delivered",
            )
            replayed = builder_context.acknowledge_message(
                message.message_id,
                read=False,
                idempotency_key="receipt-delivered",
            )
            self.assertEqual(delivered.delivered_at, replayed.delivered_at)
            self.assertIsNone(delivered.read_at)
            self.assertEqual(1, len(builder_context.unread_mailbox()))
            read = builder_context.acknowledge_message(
                message.message_id,
                read=True,
                idempotency_key="receipt-read",
            )
            self.assertIsNotNone(read.read_at)
            self.assertEqual((), builder_context.unread_mailbox())
            report = store.verify_integrity()
            self.assertEqual(1, report.direct_message_receipt_count)

        with MultiAgentRuntimeStore(self.path) as restarted:
            self.assertIsNotNone(
                restarted.get_direct_message_receipt(message.message_id).read_at
            )
            self.assertEqual(
                (), restarted.bind_agent(builder_context.agent_id).unread_mailbox()
            )
            restarted.verify_integrity()

    def test_room_cursor_advances_monotonically_and_resumes_after_restart(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _scout, builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            room = scout_context.create_room(
                "Cursor room", idempotency_key="cursor-room"
            )
            scout_context.invite(
                room.room_id,
                builder.agent_id,
                idempotency_key="cursor-invite",
            )
            builder_context.join_room(room.room_id, idempotency_key="cursor-join")
            messages = tuple(
                scout_context.send_room_message(
                    room.room_id,
                    f"Message {number}",
                    idempotency_key=f"cursor-message-{number}",
                )
                for number in range(1, 4)
            )
            self.assertEqual(
                [message.message_id for message in messages],
                [
                    message.message_id
                    for message in builder_context.unread_room_messages(room.room_id)
                ],
            )
            first_cursor = builder_context.advance_room_cursor(
                room.room_id,
                messages[0].message_id,
                idempotency_key="cursor-first",
            )
            self.assertEqual(
                [message.message_id for message in messages[1:]],
                [
                    message.message_id
                    for message in builder_context.unread_room_messages(room.room_id)
                ],
            )
            with self.assertRaisesRegex(RuntimeConflictError, "cannot move backward"):
                builder_context.advance_room_cursor(
                    room.room_id,
                    messages[0].message_id,
                    idempotency_key="cursor-backward",
                )
            final_cursor = builder_context.advance_room_cursor(
                room.room_id,
                messages[-1].message_id,
                idempotency_key="cursor-final",
            )
            self.assertEqual(first_cursor.cursor_id, final_cursor.cursor_id)
            self.assertEqual((), builder_context.unread_room_messages(room.room_id))
            report = store.verify_integrity()
            self.assertEqual(1, report.room_message_cursor_count)

        with MultiAgentRuntimeStore(self.path) as restarted:
            cursor = restarted.get_room_message_cursor(final_cursor.cursor_id)
            self.assertEqual(messages[-1].message_id, cursor.last_message_id)
            self.assertEqual(
                (),
                restarted.bind_agent(builder.agent_id).unread_room_messages(
                    room.room_id
                ),
            )
            restarted.verify_integrity()

    def test_running_task_cannot_gain_a_dependency(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _, _, context, _ = self._create_two_agents(store)
            task = context.create_task(
                "Running", "Original work", idempotency_key="running-task"
            )
            prerequisite = context.create_task(
                "Prerequisite", "Pending", idempotency_key="running-prerequisite"
            )
            context.update_task(
                task.task_id, TaskStatus.RUNNING, idempotency_key="running-start"
            )
            before = store.events()
            with self.assertRaisesRegex(RuntimeConflictError, "running task"):
                context.add_task_dependency(
                    task.task_id, prerequisite.task_id, idempotency_key="running-add"
                )
            self.assertEqual(before, store.events())
            self.assertEqual((), context.task_dependencies(task.task_id))
            self.assertEqual(TaskStatus.RUNNING, store.get_task(task.task_id).status)
            store.verify_integrity()

    def test_events_retain_historical_content(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _, builder, context, _ = self._create_two_agents(store)
            message = context.send_message(
                builder.agent_id,
                "Historical message",
                idempotency_key="history-message",
            )
            task = context.create_task(
                "Historical task",
                "Historical description",
                idempotency_key="history-task",
            )
            context.update_task(
                task.task_id, TaskStatus.RUNNING, idempotency_key="history-start"
            )
            context.update_task(
                task.task_id,
                TaskStatus.COMPLETED,
                result="Historical result",
                idempotency_key="history-complete",
            )
            events = {event.idempotency_key: event for event in store.events()}
            self.assertEqual(
                "Historical message",
                events["history-message"].payload["record"]["body"],
            )
            self.assertEqual(
                "Historical description",
                events["history-task"].payload["record"]["description"],
            )
            self.assertIsNone(events["history-task"].payload["record"]["result"])
            self.assertEqual(
                "Historical result",
                events["history-complete"].payload["record"]["result"],
            )
            self.assertEqual(message.message_id, events["history-message"].result_id)
            room = context.create_room("History room", idempotency_key="history-room")
            context.send_room_message(
                room.room_id,
                "Historical room message",
                idempotency_key="history-room-message",
            )
            context.change_model(
                "replacement-provider",
                "replacement-model",
                idempotency_key="history-model",
            )
            expected = store.replay_events()
            # Projections can be rebuilt, migrated, or damaged. They must not be
            # used to invent old event content. Deliberately damage this fixture.
            store.db.execute("UPDATE runtime_direct_messages SET body='changed'")
            store.db.execute("UPDATE runtime_room_messages SET body='changed'")
            store.db.execute(
                "UPDATE runtime_tasks SET description='changed', result='changed'"
            )
            store.db.commit()

        with MultiAgentRuntimeStore(self.path) as store:
            # Deny every projection read, independently proving event-only IO.
            store.db.set_authorizer(
                lambda action, table, *_: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and table != "runtime_events"
                    else sqlite3.SQLITE_OK
                )
            )
            actual = store.replay_events()
            self.assertEqual(expected, actual)
            restored = {
                event.idempotency_key: event.payload["record"] for event in actual
            }
            self.assertEqual(
                "Historical room message", restored["history-room-message"]["body"]
            )
            self.assertEqual(
                "research-model", restored["acceptance-create-scout"]["model_name"]
            )
            self.assertEqual(
                "replacement-model", restored["history-model"]["model_name"]
            )
            self.assertEqual(
                "Historical description", restored["history-task"]["description"]
            )
            self.assertIsNone(restored["history-task"]["result"])
            self.assertEqual(
                "Historical result", restored["history-complete"]["result"]
            )

    def test_block_before_adding_prerequisite_then_resume_after_completion(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _, _, context, _ = self._create_two_agents(store)
            task = context.create_task("Work", "Work", idempotency_key="block-work")
            prerequisite = context.create_task(
                "Prerequisite", "Pending", idempotency_key="block-prerequisite"
            )
            context.update_task(
                task.task_id, TaskStatus.RUNNING, idempotency_key="block-start"
            )
            context.update_task(
                task.task_id, TaskStatus.BLOCKED, idempotency_key="block-pause"
            )
            context.add_task_dependency(
                task.task_id, prerequisite.task_id, idempotency_key="block-add"
            )
            with self.assertRaisesRegex(RuntimeConflictError, "unfinished dependency"):
                context.update_task(
                    task.task_id, TaskStatus.RUNNING, idempotency_key="block-too-soon"
                )
            context.update_task(
                prerequisite.task_id,
                TaskStatus.RUNNING,
                idempotency_key="block-prerequisite-start",
            )
            context.update_task(
                prerequisite.task_id,
                TaskStatus.COMPLETED,
                result="Verified",
                idempotency_key="block-prerequisite-done",
            )
            context.update_task(
                task.task_id, TaskStatus.RUNNING, idempotency_key="block-resume"
            )
            # A retry of an already committed edge is not a new prerequisite.
            context.add_task_dependency(
                task.task_id, prerequisite.task_id, idempotency_key="block-add"
            )
            another = context.create_task(
                "Done prerequisite", "Done", idempotency_key="block-another"
            )
            context.update_task(
                another.task_id,
                TaskStatus.RUNNING,
                idempotency_key="block-another-start",
            )
            context.update_task(
                another.task_id,
                TaskStatus.COMPLETED,
                result="Done",
                idempotency_key="block-another-done",
            )
            with self.assertRaisesRegex(RuntimeConflictError, "running task"):
                context.add_task_dependency(
                    task.task_id, another.task_id, idempotency_key="block-add-completed"
                )
            store.verify_integrity()

    def test_integrity_rejects_legacy_running_or_completed_unfinished_dependency(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _, _, context, _ = self._create_two_agents(store)
            task = context.create_task("Work", "Work", idempotency_key="legacy-work")
            prerequisite = context.create_task(
                "Prerequisite", "Pending", idempotency_key="legacy-prerequisite"
            )
            context.add_task_dependency(
                task.task_id, prerequisite.task_id, idempotency_key="legacy-add"
            )
            for status in ("RUNNING", "COMPLETED"):
                with self.subTest(status=status):
                    store.db.execute(
                        "UPDATE runtime_tasks SET status=? WHERE task_id=?",
                        (status, task.task_id),
                    )
                    with self.assertRaisesRegex(
                        RuntimeStoreError, "unfinished dependency"
                    ):
                        store.verify_integrity()

    def test_racing_start_and_dependency_add_cannot_commit_inconsistent_state(self):
        with MultiAgentRuntimeStore(self.path) as store:
            scout, _, context, _ = self._create_two_agents(store)
            task = context.create_task("Race", "Work", idempotency_key="race-work")
            prerequisite = context.create_task(
                "Prerequisite", "Pending", idempotency_key="race-prerequisite"
            )
        barrier = Barrier(2)

        def mutate(start):
            with MultiAgentRuntimeStore(self.path) as store:
                context = store.bind_agent(scout.agent_id)
                barrier.wait(timeout=10)
                try:
                    if start:
                        context.update_task(
                            task.task_id,
                            TaskStatus.RUNNING,
                            idempotency_key="race-start",
                        )
                    else:
                        context.add_task_dependency(
                            task.task_id,
                            prerequisite.task_id,
                            idempotency_key="race-add",
                        )
                    return True
                except RuntimeConflictError:
                    return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([False, True], sorted(pool.map(mutate, (True, False))))
        with MultiAgentRuntimeStore(self.path) as store:
            store.verify_integrity()

    def test_replay_cursor_binds_history_and_preserves_batch_order(self):
        with MultiAgentRuntimeStore(self.path) as store:
            self._create_two_agents(store)
            first = store.replay_events(limit=2)
            second = store.replay_events(
                after_sequence=first[-1].sequence,
                after_event_id=first[-1].event_id,
                limit=2,
            )
            self.assertEqual(store.events(), first + second)
            self.assertEqual(
                (),
                store.replay_events(
                    after_sequence=second[-1].sequence,
                    after_event_id=second[-1].event_id,
                ),
            )
            for sequence, event_id in (
                (1, None),
                (1, second[-1].event_id),
                (999, first[-1].event_id),
                (0, first[-1].event_id),
            ):
                with (
                    self.subTest(sequence=sequence, event_id=event_id),
                    self.assertRaises(RuntimeConflictError),
                ):
                    store.replay_events(
                        after_sequence=sequence, after_event_id=event_id
                    )

    def test_events_reject_sql_updates_and_deletes(self):
        with MultiAgentRuntimeStore(self.path) as store:
            self._create_two_agents(store)
            before = store.events()
            for statement in (
                "UPDATE runtime_events SET payload_json='{}'",
                "DELETE FROM runtime_events",
                "INSERT OR REPLACE INTO runtime_events SELECT * FROM runtime_events WHERE sequence=1",
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    store.db.execute(statement)
            self.assertEqual(before, store.replay_events())
            store.verify_integrity()

    def test_legacy_events_remain_inspectable_but_not_content_replayable(self):
        with MultiAgentRuntimeStore(self.path) as store:
            self._create_two_agents(store)
            store.db.execute("DROP TRIGGER runtime_events_no_update")
            for event in store.events():
                payload = dict(event.payload)
                del payload["record"]
                del payload["record_sha256"]
                store.db.execute(
                    "UPDATE runtime_events SET schema_version=1, payload_json=? WHERE event_id=?",
                    (
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                        event.event_id,
                    ),
                )
            legacy = store.events()
            store.db.commit()
        with MultiAgentRuntimeStore(self.path) as store:
            self.assertEqual(legacy, store.events())
            store.verify_integrity()
            with self.assertRaisesRegex(RuntimeStoreError, "legacy history"):
                store.replay_events()
            store.bind_agent(legacy[0].result_id).pause(
                idempotency_key="legacy-new-pause"
            )
            self.assertEqual(2, store.events()[-1].schema_version)
            store.verify_integrity()
            with self.assertRaisesRegex(RuntimeStoreError, "legacy history"):
                store.replay_events()

    def test_replay_rejects_corrupt_or_unknown_event_content(self):
        with MultiAgentRuntimeStore(self.path) as store:
            self._create_two_agents(store)
            event = store.events()[0]
            store.db.execute("DROP TRIGGER runtime_events_no_update")
            for defect in ("digest", "identity", "fields", "version", "vocabulary"):
                with self.subTest(defect=defect):
                    payload = json.loads(json.dumps(event.payload))
                    if defect == "digest":
                        payload["record"]["role"] = "corrupt"
                    elif defect == "identity":
                        payload["record"]["agent_id"] = "agt_" + "0" * 32
                        payload["record_sha256"] = hashlib.sha256(
                            json.dumps(
                                payload["record"],
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ).encode()
                        ).hexdigest()
                    elif defect == "fields":
                        payload["record"]["unknown"] = "future field"
                    store.db.execute(
                        "UPDATE runtime_events SET payload_json=?, schema_version=?, event_type=? WHERE event_id=?",
                        (
                            json.dumps(
                                payload,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            99 if defect == "version" else 2,
                            "future.event"
                            if defect == "vocabulary"
                            else event.event_type,
                            event.event_id,
                        ),
                    )
                    with self.assertRaises(RuntimeStoreError):
                        store.replay_events()
                    with self.assertRaises(RuntimeStoreError):
                        store.verify_integrity()

    def test_replay_rejects_sequence_gap(self):
        with MultiAgentRuntimeStore(self.path) as store:
            self._create_two_agents(store)
            store.db.execute("DROP TRIGGER runtime_events_no_delete")
            store.db.execute("DELETE FROM runtime_events WHERE sequence=2")
            with self.assertRaisesRegex(RuntimeStoreError, "gap"):
                store.replay_events()
            with self.assertRaisesRegex(RuntimeStoreError, "not contiguous"):
                store.verify_integrity()

    def test_task_dependencies_block_execution_reject_cycles_and_resume(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _scout, builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            prerequisite = scout_context.create_task(
                "Verify the fixture",
                "Establish the parser boundary before implementation.",
                idempotency_key="dependency-prerequisite",
            )
            implementation = scout_context.create_task(
                "Implement the parser",
                "Implement only after the fixture is verified.",
                idempotency_key="dependency-implementation",
            )
            scout_context.add_task_dependency(
                implementation.task_id,
                prerequisite.task_id,
                idempotency_key="dependency-add",
            )
            with self.assertRaisesRegex(RuntimeConflictError, "unfinished dependency"):
                scout_context.update_task(
                    implementation.task_id,
                    TaskStatus.RUNNING,
                    idempotency_key="dependency-start-too-soon",
                )
            with self.assertRaisesRegex(RuntimeConflictError, "cycle"):
                scout_context.add_task_dependency(
                    prerequisite.task_id,
                    implementation.task_id,
                    idempotency_key="dependency-cycle",
                )
            scout_context.update_task(
                prerequisite.task_id,
                TaskStatus.RUNNING,
                idempotency_key="dependency-prerequisite-running",
            )
            scout_context.update_task(
                prerequisite.task_id,
                TaskStatus.COMPLETED,
                result="Boundary fixture verified.",
                idempotency_key="dependency-prerequisite-complete",
            )
            scout_context.delegate_task(
                implementation.task_id,
                builder.agent_id,
                "The prerequisite is complete.",
                idempotency_key="dependency-delegate",
            )
            running = builder_context.update_task(
                implementation.task_id,
                TaskStatus.RUNNING,
                idempotency_key="dependency-implementation-running",
            )
            self.assertEqual(TaskStatus.RUNNING, running.status)
            report = store.verify_integrity()
            self.assertEqual(1, report.task_dependency_count)

        with MultiAgentRuntimeStore(self.path) as restarted:
            dependencies = restarted.task_dependencies(
                builder.agent_id, implementation.task_id
            )
            self.assertEqual(
                [prerequisite.task_id], [task.task_id for task in dependencies]
            )
            restarted.verify_integrity()

    def test_task_owner_shares_content_addressed_artifact_with_room(self):
        digest = "a" * 64
        with MultiAgentRuntimeStore(self.path) as store:
            _scout, builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            outsider = store.create_agent(
                display_name="Same-project outsider",
                role="Observer",
                project_id="project-aurora",
                idempotency_key="artifact-outsider",
            )
            outsider_context = store.bind_agent(outsider.agent_id)
            outsider_context.start(idempotency_key="artifact-outsider-start")
            room = scout_context.create_room(
                "Artifact exchange", idempotency_key="artifact-room"
            )
            scout_context.invite(
                room.room_id,
                builder.agent_id,
                idempotency_key="artifact-invite",
            )
            builder_context.join_room(room.room_id, idempotency_key="artifact-join")
            task = scout_context.create_task(
                "Produce parser patch",
                "Create and share the verified patch.",
                idempotency_key="artifact-task",
            )
            scout_context.delegate_task(
                task.task_id,
                builder.agent_id,
                "Builder owns the output artifact.",
                idempotency_key="artifact-delegate",
            )
            with self.assertRaisesRegex(RuntimePermissionError, "current task owner"):
                scout_context.share_artifact(
                    "stale-owner.patch",
                    media_type="text/x-diff",
                    uri="artifact://stale-owner.patch",
                    sha256=digest,
                    task_id=task.task_id,
                    room_id=room.room_id,
                    idempotency_key="artifact-stale-owner-share",
                )
            artifact = builder_context.share_artifact(
                "parser.patch",
                media_type="text/x-diff",
                uri="artifact://sha256/" + digest,
                sha256=digest,
                size_bytes=418,
                task_id=task.task_id,
                room_id=room.room_id,
                idempotency_key="artifact-share",
            )
            self.assertEqual(builder.agent_id, artifact.owner_id)
            self.assertEqual(
                [artifact.artifact_id],
                [
                    item.artifact_id
                    for item in scout_context.room_artifacts(room.room_id)
                ],
            )
            self.assertEqual(
                [artifact.artifact_id],
                [
                    item.artifact_id
                    for item in scout_context.task_artifacts(task.task_id)
                ],
            )
            with self.assertRaises(RuntimePermissionError):
                outsider_context.room_artifacts(room.room_id)
            report = store.verify_integrity()
            self.assertEqual(1, report.artifact_count)

        with MultiAgentRuntimeStore(self.path) as restarted:
            self.assertEqual(
                digest, restarted.get_artifact(artifact.artifact_id).sha256
            )
            restarted.verify_integrity()

    def test_targeted_review_opinion_vote_and_evidence_protocols_resume(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _scout, builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            proposal = scout_context.share_artifact(
                "proposal.md",
                media_type="text/markdown",
                uri="artifact://sha256/" + "b" * 64,
                sha256="b" * 64,
                idempotency_key="protocol-proposal",
            )
            evidence = builder_context.share_artifact(
                "evidence.json",
                media_type="application/json",
                uri="artifact://sha256/" + "c" * 64,
                sha256="c" * 64,
                idempotency_key="protocol-evidence",
            )

            review = scout_context.request_review(
                builder.agent_id,
                "Review the proposal against the acceptance boundary.",
                artifact_id=proposal.artifact_id,
                idempotency_key="protocol-review",
            )
            self.assertEqual(
                [review.request_id],
                [item.request_id for item in builder_context.collaboration_inbox()],
            )
            review_response = builder_context.respond_to_collaboration(
                review.request_id,
                "The proposal satisfies the stated boundary.",
                idempotency_key="protocol-review-response",
            )
            replayed_response = builder_context.respond_to_collaboration(
                review.request_id,
                "The proposal satisfies the stated boundary.",
                idempotency_key="protocol-review-response",
            )
            self.assertEqual(review_response.response_id, replayed_response.response_id)
            self.assertEqual(
                CollaborationStatus.CLOSED,
                store.get_collaboration_request(review.request_id).status,
            )

            opinion = scout_context.request_opinion(
                builder.agent_id,
                "Should the parser remain fail-closed?",
                idempotency_key="protocol-opinion",
            )
            builder_context.respond_to_collaboration(
                opinion.request_id,
                "Yes; malformed inputs must not cross the boundary.",
                idempotency_key="protocol-opinion-response",
            )

            vote = scout_context.request_vote(
                builder.agent_id,
                "Choose the public encoding.",
                ("JSON", "CBOR"),
                idempotency_key="protocol-vote",
            )
            self.assertEqual(CollaborationKind.VOTE, vote.kind)
            with self.assertRaisesRegex(
                RuntimeConflictError, "one of the request options"
            ):
                builder_context.respond_to_collaboration(
                    vote.request_id,
                    "I prefer YAML.",
                    selected_option="YAML",
                    idempotency_key="protocol-invalid-vote-response",
                )
            vote_response = builder_context.respond_to_collaboration(
                vote.request_id,
                "JSON is already used by the event envelope.",
                selected_option="JSON",
                idempotency_key="protocol-vote-response",
            )
            self.assertEqual("JSON", vote_response.selected_option)

            evidence_request = scout_context.request_evidence(
                builder.agent_id,
                "Provide the verification evidence for the decision.",
                artifact_id=proposal.artifact_id,
                idempotency_key="protocol-evidence-request",
            )
            builder_context.respond_to_collaboration(
                evidence_request.request_id,
                "The attached result contains the verification record.",
                evidence_artifact_id=evidence.artifact_id,
                idempotency_key="protocol-evidence-response",
            )
            self.assertEqual(
                evidence.artifact_id,
                scout_context.collaboration_responses(evidence_request.request_id)[
                    0
                ].evidence_artifact_id,
            )
            report = store.verify_integrity()
            self.assertEqual(4, report.collaboration_request_count)
            self.assertEqual(4, report.collaboration_response_count)

        with MultiAgentRuntimeStore(self.path) as restarted:
            resumed = restarted.bind_agent(builder.agent_id)
            closed = resumed.collaboration_inbox(include_closed=True)
            self.assertEqual(4, len(closed))
            restarted.verify_integrity()

    def test_room_help_broadcast_allows_opt_in_response_and_owner_close(self):
        with MultiAgentRuntimeStore(self.path) as store:
            _scout, builder, scout_context, builder_context = self._create_two_agents(
                store
            )
            outsider = store.create_agent(
                display_name="Outside the room",
                role="Observer",
                project_id="project-aurora",
                idempotency_key="help-outsider",
            )
            outsider_context = store.bind_agent(outsider.agent_id)
            outsider_context.start(idempotency_key="help-outsider-start")
            volunteer = store.create_agent(
                display_name="Second volunteer",
                role="Independent reproducer",
                project_id="project-aurora",
                idempotency_key="help-volunteer",
            )
            volunteer_context = store.bind_agent(volunteer.agent_id)
            volunteer_context.start(idempotency_key="help-volunteer-start")
            room = scout_context.create_room("Help room", idempotency_key="help-room")
            scout_context.invite(
                room.room_id,
                builder.agent_id,
                idempotency_key="help-room-invite",
            )
            builder_context.join_room(room.room_id, idempotency_key="help-room-join")
            scout_context.invite(
                room.room_id,
                volunteer.agent_id,
                idempotency_key="help-volunteer-invite",
            )
            volunteer_context.join_room(
                room.room_id, idempotency_key="help-volunteer-join"
            )
            request = scout_context.broadcast_help(
                "Who can independently reproduce the parser failure?",
                room_id=room.room_id,
                idempotency_key="help-broadcast",
            )
            self.assertEqual(CollaborationKind.HELP, request.kind)
            self.assertEqual((), scout_context.collaboration_inbox())
            self.assertEqual(
                [request.request_id],
                [item.request_id for item in builder_context.collaboration_inbox()],
            )
            self.assertEqual((), outsider_context.collaboration_inbox())
            with self.assertRaisesRegex(RuntimePermissionError, "not eligible"):
                outsider_context.respond_to_collaboration(
                    request.request_id,
                    "I should not see this room request.",
                    idempotency_key="help-outsider-response",
                )
            builder_context.respond_to_collaboration(
                request.request_id,
                "I can reproduce it independently.",
                idempotency_key="help-builder-response",
            )
            volunteer_context.respond_to_collaboration(
                request.request_id,
                "I can run a second independent reproduction.",
                idempotency_key="help-volunteer-response",
            )
            self.assertEqual(
                CollaborationStatus.OPEN,
                store.get_collaboration_request(request.request_id).status,
            )
            closed = scout_context.close_collaboration(
                request.request_id, idempotency_key="help-close"
            )
            self.assertEqual(CollaborationStatus.CLOSED, closed.status)
            report = store.verify_integrity()
            self.assertEqual(1, report.collaboration_request_count)
            self.assertEqual(2, report.collaboration_response_count)

        with MultiAgentRuntimeStore(self.path) as restarted:
            responses = restarted.collaboration_responses(
                builder.agent_id, request.request_id
            )
            self.assertEqual(
                {builder.agent_id, volunteer.agent_id},
                {response.responder_id for response in responses},
            )
            restarted.verify_integrity()


class MultiAgentRuntimeSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "runtime.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rejects_unmarked_foreign_database_without_modifying_it(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("CREATE TABLE foreign_data(value TEXT)")
            db.execute("INSERT INTO foreign_data(value) VALUES ('preserve-me')")
            db.commit()
        finally:
            db.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeStoreError, "unmarked"):
            MultiAgentRuntimeStore(self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_rejects_newer_runtime_schema(self):
        with MultiAgentRuntimeStore(self.path):
            pass
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA user_version=999")
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(RuntimeStoreError, "newer"):
            MultiAgentRuntimeStore(self.path)


if __name__ == "__main__":
    unittest.main()
