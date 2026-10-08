"""Real dispatcher scenarios and durable queue race/restart regressions; offline API only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aiogram import Router
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import SendMessage
from aiogram.types import Animation, Document, PhotoSize, Update

import main
from db.async_database import AsyncDatabase
from db.database import Database
from handlers import broadcast, common
from services.broadcasts import BroadcastService, BroadcastValidationError, extract_content, parse_schedule
from services.requests import RequestService
from tests.test_broadcasts import BroadcastFixture, ADMIN, CLIENT, GROUP_A, GROUP_B, OLD_GROUP, NEW_GROUP, WORK_CHAT
from utils.process_lock import bot_process_lock


def clone_router(source):
    """Fresh routing tree with the production filters/handlers (aiogram routers attach once)."""
    target = Router()
    for name, observer in source.observers.items():
        target.observers[name].handlers.extend(observer.handlers)
        for middleware in observer.outer_middleware:
            target.observers[name].outer_middleware(middleware)
        for middleware in observer.middleware:
            target.observers[name].middleware(middleware)
    for child in source.sub_routers:
        target.include_router(clone_router(child))
    return target


class DispatcherTests(BroadcastFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        with ExitStack() as patches:
            for module in (main.keyboards, main.broadcast, main.commands, main.callbacks):
                patches.enter_context(patch.object(module, 'router', clone_router(module.router)))
            self.dp = main.build_dispatcher(self.adb, self.config,
                                           RequestService(self.bot, self.adb, self.config), self.service)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.dp.fsm.events_isolation.close()
        await super().asyncTearDown()

    async def feed(self, message=None, callback=None, member=None):
        self.update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, message=message,
                                                  callback_query=callback, my_chat_member=member))

    async def draft(self):
        await self.feed(callback=self.callback('bc:new'))
        await self.feed(message=self.message(text='😀 <b>буквально</b>',
                        entities=[{'type': 'bold', 'offset': 0, 'length': 2}]))
        return self.latest_broadcast()['id']

    async def test_real_callback_author_preview_cancel_and_reopen_after_restart(self):
        bid = await self.draft()
        calls = self.session.sent('sendMessage')
        self.assertEqual(calls[-2].text, '😀 <b>буквально</b>')
        self.assertIsNone(calls[-2].parse_mode)
        self.assertEqual(len(self.button_labels(calls[-1].reply_markup)), 3)
        self.assertFalse(any(call.chat_id < 0 for call in calls))
        await self.dp.storage.close()  # Memory FSM is empty after a restart.
        state = self.dp.fsm.get_context(bot=self.bot, chat_id=ADMIN, user_id=ADMIN)
        await state.clear()
        await self.feed(callback=self.callback(f'bc:preview:{bid}'))
        self.assertEqual((await state.get_data())['broadcast_id'], bid)
        await self.feed(message=self.message(text='/cancel'))
        self.assertEqual(self.db.get_broadcast(bid)['status'], 'cancelled')
        self.assertIsNone(await state.get_state())

    async def test_real_cancel_before_content_and_after_invalid_timer(self):
        await self.feed(message=self.message(text=broadcast.BTN_BC_NEW))
        await self.feed(message=self.message(text='/cancel@test_bot'))
        self.assertEqual(self.db.count_broadcasts(), 0)
        bid = await self.draft()
        await self.feed(callback=self.callback(f'bc:sched:{bid}'))
        await self.feed(message=self.message(text='не дата'))
        self.assertIn('✖ Отменить', self.button_labels(self.session.sent('sendMessage')[-1].reply_markup))
        await self.feed(message=self.message(text='/cancel'))
        self.assertEqual(self.db.get_broadcast(bid)['status'], 'cancelled')

    async def test_real_send_double_callback_and_role_revocation(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup')
        bid = await self.draft()
        await self.feed(callback=self.callback(f'bc:send:{bid}'))
        await self.feed(callback=self.callback(f'bc:send:{bid}'))
        self.assertEqual(self.db.broadcast_counts(bid)['total'], 1)
        self.assertFalse(any(m.chat_id == GROUP_A for m in self.session.sent('sendMessage')))
        await self.service.tick()
        self.assertEqual(self.db.broadcast_counts(bid)['sent'], 1)
        next_id = await self.draft()
        self.db.delete_botadmin(ADMIN)
        await self.feed(callback=self.callback(f'bc:cancel:{next_id}'))
        self.assertEqual(self.db.get_broadcast(next_id)['status'], 'draft')
        await self.feed(message=self.message(text='/cancel'))
        self.assertEqual(self.db.get_broadcast(next_id)['status'], 'draft')

    async def test_group_keyboard_interception_whoami_and_both_migration_fields(self):
        await self.feed(message=self.message(text='/start', chat_id=OLD_GROUP, chat_type='group'))
        self.assertIn(OLD_GROUP, [r[0] for r in self.db.get_broadcast_chats()])
        request_id = self.db.create_request(CLIENT, 'client', 'problem', OLD_GROUP, 123)
        await self.feed(message=self.message(chat_id=OLD_GROUP, chat_type='group', migrate_to_chat_id=NEW_GROUP))
        await self.feed(message=self.message(chat_id=NEW_GROUP, chat_type='supergroup', migrate_from_chat_id=OLD_GROUP))
        self.assertEqual([r[0] for r in self.db.get_broadcast_chats()], [NEW_GROUP])
        self.assertEqual(self.db.request_original_chat(request_id), OLD_GROUP)
        self.assertEqual(self.db.get_request_by_id(request_id)[8], NEW_GROUP)
        await self.feed(message=self.message(text='/whoami@test_bot', chat_id=GROUP_B, chat_type='supergroup'))
        self.assertIn(GROUP_B, [r[0] for r in self.db.get_broadcast_chats()])

    async def test_album_all_elements_rejected_and_private_client_denied(self):
        await self.feed(callback=self.callback('bc:new', user_id=CLIENT, chat_id=CLIENT))
        self.assertEqual(self.db.count_broadcasts(), 0)
        await self.feed(callback=self.callback('bc:new'))
        for file_id in ('one', 'two', 'three'):
            await self.feed(message=self.message(media_group_id='album',
                        photo=[PhotoSize(file_id=file_id, file_unique_id=file_id, width=10, height=10)]))
        self.assertEqual(self.db.count_broadcasts(), 0)
        await self.feed(callback=self.callback('bc:abort'))
        self.assertIsNone(await self.dp.fsm.get_context(bot=self.bot, chat_id=ADMIN, user_id=ADMIN).get_state())

    async def test_timer_back_navigation_and_draft_persists(self):
        bid = await self.draft()
        await self.feed(callback=self.callback(f'bc:sched:{bid}'))
        await self.feed(message=self.message(text='30'))
        self.assertEqual(self.db.get_broadcast(bid)['status'], 'scheduled')
        self.assertEqual(self.db.broadcast_counts(bid)['total'], 0)
        await self.feed(callback=self.callback('bc:noop'))
        self.assertIn('Главное меню', self.session.texts()[-1])


class DurabilityTests(BroadcastFixture):
    def test_competing_launches_and_claims_use_exclusive_transactions(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup')
        bid = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='text')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.db.start_broadcast(bid), range(16)))
            claims = list(pool.map(lambda _: self.db.claim_broadcast_job(), range(16)))
        self.assertEqual(results.count('running'), 1)
        self.assertEqual(sum(job is not None for job in claims), 1)
        self.assertEqual(self.db.broadcast_counts(bid)['total'], 1)

    def test_duplicate_source_update_does_not_create_two_drafts(self):
        first = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t', source_message_id=9)
        second = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t', source_message_id=9)
        self.assertEqual(first, second)
        self.assertEqual(self.db.count_broadcasts(), 1)

    async def test_429_global_pause_survives_restart(self):
        for cid in (GROUP_A, GROUP_B):
            self.db.register_broadcast_chat(cid, 'supergroup')
        bid = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t')
        self.db.start_broadcast(bid)
        self.session.errors['sendMessage'] = [TelegramRetryAfter(method=SendMessage(chat_id=GROUP_A, text='t'),
                                                               message='SECRET raw', retry_after=60)]
        await self.service.tick()
        restarted = BroadcastService(self.bot, AsyncDatabase(Database(str(self.path))), self.config)
        await restarted.tick()
        self.assertEqual(len(self.session.sent('sendMessage')), 1)
        self.assertEqual(self.db.broadcast_counts(bid)['pending'], 2)

    async def test_membership_check_429_keeps_registry_and_persists_delay(self):
        self.db.apply_broadcast_chat_member(GROUP_A, 'supergroup', 'member')
        self.session.errors['getChatMember'] = [TelegramRetryAfter(
            method=SendMessage(chat_id=1, text='t'), message='rate', retry_after=70)]
        self.assertIsNone(await self.service.verify_chat(GROUP_A))
        self.assertEqual([r[0] for r in self.db.get_broadcast_chats()], [GROUP_A])
        self.assertTrue(Database(str(self.path)).broadcast_delivery_paused())

    async def test_changed_work_chat_is_excluded_from_queued_delivery(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup')
        bid = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t')
        self.db.start_broadcast(bid)
        self.config.work_chat_id = GROUP_A
        await self.service.tick()
        self.assertFalse(self.session.sent('sendMessage'))
        self.assertEqual(self.db.broadcast_counts(bid)['blocked'], 1)

    def test_per_group_rate_limit_spans_broadcasts_and_restart(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup')
        for _ in range(2):
            bid = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t')
            self.db.start_broadcast(bid)
        now = datetime.now(timezone.utc)
        job = self.db.claim_broadcast_job(now=now.isoformat())
        self.db.set_broadcast_job_result(job['job_id'], 'sent')
        restarted = Database(str(self.path))
        self.assertIsNone(restarted.claim_broadcast_job(now=(now + timedelta(seconds=1)).isoformat()))
        self.assertIsNotNone(restarted.claim_broadcast_job(now=(now + timedelta(seconds=4)).isoformat()))

    async def test_temporary_role_failure_preserves_schedule_and_rechecks_without_cache(self):
        self.db.delete_botadmin(ADMIN)
        bid = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t')
        self.db.schedule_broadcast(bid, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), 'UTC')
        self.session.errors['getChatMember'] = [TelegramNetworkError(method=SendMessage(chat_id=1, text='t'), message='timeout')]
        await self.service.tick()
        self.assertEqual(self.db.get_broadcast(bid)['status'], 'scheduled')
        self.session._member_status = lambda chat_id, user_id: 'creator'
        self.assertTrue(await self.service.author_is_admin(ADMIN))
        self.session._member_status = lambda chat_id, user_id: 'left'
        await self.service.tick()
        self.assertEqual(self.db.get_broadcast(bid)['status'], 'cancelled')

    async def test_reschedule_during_role_check_does_not_launch_early(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup')
        bid = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t')
        self.db.schedule_broadcast(bid, (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), 'UTC')
        async def changed(*args):
            self.db.schedule_broadcast(bid, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 'UTC')
            return True
        with patch.object(self.service, 'author_is_admin', changed):
            await self.service.tick()
        self.assertEqual(self.db.get_broadcast(bid)['status'], 'scheduled')
        self.assertEqual(self.db.broadcast_counts(bid)['total'], 0)

    async def test_server_failure_is_uncertain_and_safe_error_details(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup')
        bid = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t')
        self.db.start_broadcast(bid)
        self.session.errors['sendMessage'] = [TelegramServerError(method=SendMessage(chat_id=1, text='t'), message='SECRET')]
        await self.service.tick()
        await self.service.tick()
        self.assertEqual(len(self.session.sent('sendMessage')), 1)
        self.assertEqual(self.db.broadcast_counts(bid)['uncertain'], 1)
        await broadcast.broadcast_callback(self.callback(f'bc:errors:{bid}:0'), self.state)
        self.assertNotIn('SECRET', self.session.texts()[-1])
        self.assertIn(str(GROUP_A), self.session.texts()[-1])

    async def test_stale_group_message_does_not_reactivate_removed_bot(self):
        self.db.apply_broadcast_chat_member(GROUP_A, 'supergroup', 'left')
        async def done(event, data):
            pass
        await broadcast.remember_group_messages(done, self.message(text='stale', chat_id=GROUP_A, chat_type='supergroup'), {})
        self.assertEqual(self.db.get_broadcast_chats(), [])

    def test_migration_preserves_terminal_results_and_pending_ticket_payloads(self):
        for cid in (OLD_GROUP, NEW_GROUP):
            self.db.register_broadcast_chat(cid, 'supergroup')
            self.db.collect_message(cid, CLIENT, 'c', {'message_id': abs(cid), 'text': str(cid)}, 300)
        bid = self.db.create_broadcast(ADMIN, 'a', ADMIN, text='t')
        self.db.start_broadcast(bid)
        with self.db.connect() as conn:
            conn.execute("UPDATE broadcast_jobs SET status='sent', message_id=777 WHERE chat_id=?", (OLD_GROUP,))
        self.db.migrate_broadcast_chat(OLD_GROUP, NEW_GROUP, 'supergroup')
        self.assertEqual(self.db.broadcast_counts(bid)['sent'], 1)
        with self.db.connect() as conn:
            text = conn.execute('SELECT messages FROM pending_requests WHERE chat_id=?', (NEW_GROUP,)).fetchone()[0]
        self.assertIn(str(OLD_GROUP), text)
        self.assertIn(str(NEW_GROUP), text)

    def test_os_lock_prevents_second_bot_and_releases(self):
        with bot_process_lock(self.path):
            with self.assertRaises(RuntimeError), bot_process_lock(self.path):
                pass
        with bot_process_lock(self.path):
            pass

    def test_elapsed_minutes_across_dst_and_invalid_timezone(self):
        now = datetime(2030, 3, 31, 0, 45, tzinfo=timezone.utc)
        self.assertEqual(parse_schedule('30', 'Europe/Berlin', now=now), now + timedelta(minutes=30))
        with self.assertRaises(ValueError):
            self.config.__class__(bot_token='123:abc', work_chat_id=WORK_CHAT, timezone='Not/AZone')

    def test_animation_document_is_rejected_and_presentation_persists(self):
        with self.assertRaises(BroadcastValidationError):
            extract_content(self.message(animation=Animation(file_id='gif', file_unique_id='g', width=1, height=1, duration=1),
                                         document=Document(file_id='gif', file_unique_id='g')))
        content = extract_content(self.message(photo=[PhotoSize(file_id='p', file_unique_id='p', width=1, height=1)],
                                              caption='x', has_media_spoiler=True, show_caption_above_media=True))
        bid = self.db.create_broadcast(ADMIN, 'a', ADMIN, **content)
        self.assertEqual(Database(str(self.path)).get_broadcast(bid)['options'],
                         {'has_spoiler': True, 'show_caption_above_media': True})
