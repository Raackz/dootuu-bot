from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from telethon.tl.functions.messages import ForwardMessagesRequest, GetForumTopicsRequest

from mailer.services.telethon_manager import TelethonManager
from mailer.db import MailerDB
from mailer.services.mailer_engine import MailerEngine
from mailer.handlers.admin import _reject_unresolvable_forward


class ForwardingTests(IsolatedAsyncioTestCase):
    def manager(self, client):
        manager = object.__new__(TelethonManager)
        manager.get_client = AsyncMock(return_value=client)
        return manager

    async def test_regular_forward_uses_resolved_source_peer(self):
        client = SimpleNamespace(
            forward_messages=AsyncMock(return_value=SimpleNamespace(id=42)),
            send_message=AsyncMock(),
        )
        manager = self.manager(client)
        manager._get_entity_for_chat = AsyncMock(side_effect=["destination", "source-peer"])

        result = await manager.send_to_group(1, -10020, "caption", -10010, 7)

        self.assertTrue(result["ok"])
        client.forward_messages.assert_awaited_once_with(
            "destination", 7, from_peer="source-peer"
        )
        client.send_message.assert_not_awaited()

    async def test_unavailable_media_source_does_not_send_empty_message(self):
        client = SimpleNamespace(
            forward_messages=AsyncMock(),
            send_message=AsyncMock(),
        )
        manager = self.manager(client)
        manager._get_entity_for_chat = AsyncMock(
            side_effect=["destination", ValueError("unknown peer")]
        )

        result = await manager.send_to_group(1, -10020, "", -10010, 7)

        self.assertFalse(result["ok"])
        self.assertTrue(result["source_unavailable"])
        client.forward_messages.assert_not_awaited()
        client.send_message.assert_not_awaited()

    async def test_unavailable_source_with_caption_falls_back_to_text(self):
        sent = SimpleNamespace(id=43)
        client = SimpleNamespace(
            forward_messages=AsyncMock(),
            send_message=AsyncMock(return_value=sent),
        )
        manager = self.manager(client)
        manager._get_entity_for_chat = AsyncMock(
            side_effect=["destination", ValueError("unknown peer")]
        )

        result = await manager.send_to_group(1, -10020, "caption", -10010, 7)

        self.assertTrue(result["ok"])
        client.send_message.assert_awaited_once_with(
            "destination", "caption", parse_mode=None
        )

    async def test_forum_forward_keeps_resolved_peer_and_random_id(self):
        class FakeClient:
            def __init__(self):
                self.forward_request = None

            async def __call__(self, request):
                if isinstance(request, GetForumTopicsRequest):
                    return SimpleNamespace(
                        topics=[SimpleNamespace(id=99, closed=False, hidden=False)]
                    )
                if isinstance(request, ForwardMessagesRequest):
                    self.forward_request = request
                    return SimpleNamespace(
                        updates=[SimpleNamespace(message=SimpleNamespace(id=44))]
                    )
                raise AssertionError(type(request))

        client = FakeClient()
        manager = object.__new__(TelethonManager)

        result = await manager._send_in_forum_topic(
            client, "destination", "", "resolved-source", 7
        )

        self.assertEqual(result.id, 44)
        self.assertEqual(client.forward_request.from_peer, "resolved-source")
        self.assertEqual(client.forward_request.id, [7])
        self.assertEqual(client.forward_request.top_msg_id, 99)
        self.assertEqual(len(client.forward_request.random_id), 1)

    async def test_login_code_separators_are_removed(self):
        client = SimpleNamespace(
            sign_in=AsyncMock(return_value=None),
            get_me=AsyncMock(return_value=SimpleNamespace(
                first_name="Test", last_name=None, username=None
            )),
            disconnect=AsyncMock(),
        )
        manager = object.__new__(TelethonManager)
        manager._pending = {
            1: SimpleNamespace(
                client=client, phone="+15550000001", phone_code_hash="hash"
            )
        }
        manager._clients = {}
        manager.db = SimpleNamespace(
            get_account_by_phone=AsyncMock(return_value=None),
            add_account=AsyncMock(return_value=1),
        )
        manager.get_client = AsyncMock(return_value=client)

        await manager.confirm_code(1, "1.a-2 / 3_4:5")

        client.sign_in.assert_awaited_once_with(
            phone="+15550000001", code="12345", phone_code_hash="hash"
        )

    async def test_active_account_is_not_opened_twice(self):
        manager = object.__new__(TelethonManager)
        manager.db = SimpleNamespace(
            get_account_by_phone=AsyncMock(
                return_value={"id": 1, "status": "active"}
            )
        )
        manager.cancel_pending = AsyncMock()
        manager.disconnect_account = AsyncMock()
        manager._load_api = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "уже добавлен"):
            await manager.start_login(10, "+15550000001")

        manager._load_api.assert_not_awaited()
        manager.disconnect_account.assert_not_awaited()

    async def test_forward_without_source_id_is_rejected(self):
        message = SimpleNamespace(forward_origin=object(), answer=AsyncMock())

        rejected = await _reject_unresolvable_forward(message, None)

        self.assertTrue(rejected)
        message.answer.assert_awaited_once()


class MailingIntegrationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db = MailerDB(Path(self.temp_dir.name) / "mailer.db")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_account_group_and_forward_reach_mailer_engine(self):
        account_id = await self.db.add_account(
            "+15550000001", "acc_15550000001", "Test account", added_by=1
        )
        group_id = await self.db.add_group(-10020, "Test group")
        await self.db.link_account_group(account_id, group_id)
        await self.db.set_account_message(account_id, "caption", -10010, 7)

        telethon = SimpleNamespace(
            send_to_group=AsyncMock(
                return_value={"ok": True, "message_id": 44, "title": "Test group"}
            )
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        engine = MailerEngine(self.db, telethon, bot)

        did_work, _ = await engine._tick()

        self.assertTrue(did_work)
        telethon.send_to_group.assert_awaited_once_with(
            account_id,
            -10020,
            "caption",
            source_chat_id=-10010,
            source_message_id=7,
        )
        logs = await self.db.recent_logs()
        self.assertEqual(logs[0]["status"], "ok")
        self.assertEqual((await self.db.get_account(account_id))["sent_in_cycle"], 1)

    async def test_target_block_survives_database_restart(self):
        account_id = await self.db.add_account(
            "+15550000002", "acc_15550000002", "Blocked test", added_by=1
        )
        first = await self.db.add_group(-10021, "Blocked group")
        second = await self.db.add_group(-10022, "Allowed group")
        await self.db.link_account_group(account_id, first)
        await self.db.link_account_group(account_id, second)
        await self.db.block_account_group_send(account_id, first, "write forbidden")

        before = await self.db.list_account_sendable_groups(account_id)
        self.assertEqual([group["id"] for group in before], [second])

        db_path = self.db.path
        await self.db.close()
        self.db = MailerDB(db_path)
        await self.db.connect()

        after = await self.db.list_account_sendable_groups(account_id)
        self.assertEqual([group["id"] for group in after], [second])
        self.assertEqual(await self.db.count_account_send_blocks(account_id), 1)

        await self.db.db.execute(
            "UPDATE accounts SET status='cooldown', next_cycle_at=0 WHERE id=?",
            (account_id,),
        )
        await self.db.db.commit()
        self.assertTrue(await self.db.maybe_end_cooldown(account_id))
        after_cooldown = await self.db.list_account_sendable_groups(account_id)
        self.assertEqual([group["id"] for group in after_cooldown], [second])

        self.assertEqual(await self.db.clear_account_send_blocks(account_id), 1)
        self.assertEqual(len(await self.db.list_account_sendable_groups(account_id)), 2)

    async def test_repeated_user_bans_stop_account_without_duplicate_alerts(self):
        account_id = await self.db.add_account(
            "+15550000003", "acc_15550000003", "Restricted test", added_by=1
        )
        await self.db.set_account_message(account_id, "forward caption", -10010, 7)
        for index in range(3):
            group_id = await self.db.add_group(-10030 - index, f"Group {index}")
            await self.db.link_account_group(account_id, group_id)
        log_group_id = await self.db.add_log_group(-10099, "Test log")
        await self.db.link_account_log_group(account_id, log_group_id)

        telethon = SimpleNamespace(
            send_to_group=AsyncMock(return_value={
                "ok": False,
                "error": "You're banned from sending messages in supergroups/channels",
                "write_forbidden": True,
                "user_banned": True,
            })
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        engine = MailerEngine(self.db, telethon, bot)

        for _ in range(3):
            did_work, _ = await engine._tick()
            self.assertTrue(did_work)

        account = await self.db.get_account(account_id)
        self.assertEqual(account["status"], "error")
        self.assertEqual(await self.db.count_account_send_blocks(account_id), 3)
        self.assertEqual(len(await self.db.list_account_sendable_groups(account_id)), 0)
        self.assertEqual(bot.send_message.await_count, 3)
        self.assertEqual(telethon.send_to_group.await_count, 3)


if __name__ == "__main__":
    import unittest

    unittest.main()
