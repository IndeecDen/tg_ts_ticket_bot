"""Tests for the mass broadcast feature: access, formatting, queue and recovery."""
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import (
    Audio,
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    ChatMemberUpdated,
    Document,
    Message,
    MessageEntity,
    MessageOriginUser,
    PhotoSize,
    Sticker,
    Update,
    User,
    Video,
)

from config import Config
from db.async_database import AsyncDatabase
from db.database import Database
from handlers import broadcast as broadcasts
from handlers import commands, common, keyboards
from main import polling_updates
from services.broadcasts import (
    BroadcastService,
    BroadcastValidationError,
    build_entities,
    extract_content,
    parse_schedule,
)

ROOT = Path(__file__).resolve().parents[1]

WORK_CHAT = -1001111111
ADMIN = 42
SPECIALIST = 43
CLIENT = 7
GROUP_A = -1002222222
GROUP_B = -1003333333
OLD_GROUP = -1004444444
NEW_GROUP = -1005555555
CHANNEL = -1006666666


class RecordingSession(BaseSession):
    """Offline Telegram transport: every API call is recorded, nothing leaves the process."""

    def __init__(self):
        super().__init__()
        self.methods = []
        self.errors = {}
        self.chats = {}
        self._message_id = 500

    def sent(self, api_name):
        return [method for method in self.methods if method.__api_method__ == api_name]

    def texts(self, api_name='sendMessage'):
        return [method.text or '' for method in self.sent(api_name)]

    def _error_for(self, api_name):
        error = self.errors.get(api_name)
        if isinstance(error, list):
            return error.pop(0) if error else None
        return error

    @staticmethod
    def _member(user_id, status):
        user = User(id=user_id, is_bot=True, first_name='Bot')
        if status == 'creator':
            return ChatMemberOwner(user=user, status='creator', is_anonymous=False)
        if status in ('member', 'administrator'):
            return ChatMemberMember(user=user, status='member')
        return ChatMemberLeft(user=user, status='left')

    def _result(self, api_name, method):
        chat_id = getattr(method, 'chat_id', 0)
        chat = Chat(id=chat_id, type='private' if chat_id > 0 else 'supergroup')
        payload = {'message_id': self._message_id, 'date': datetime.now(), 'chat': chat}
        if api_name == 'sendMessage':
            payload['text'] = method.text
            payload['entities'] = method.entities
        elif api_name == 'sendPhoto':
            payload['photo'] = [PhotoSize(file_id=method.photo, file_unique_id='unique', width=1, height=1)]
            payload['caption'] = method.caption
            payload['caption_entities'] = method.caption_entities
        elif api_name == 'sendDocument':
            payload['document'] = Document(file_id=method.document, file_unique_id='unique')
            payload['caption'] = method.caption
            payload['caption_entities'] = method.caption_entities
        elif api_name == 'sendVideo':
            payload['video'] = Video(file_id=method.video, file_unique_id='unique', width=1, height=1,
                                     duration=1)
            payload['caption'] = method.caption
            payload['caption_entities'] = method.caption_entities
        elif api_name == 'sendAudio':
            payload['audio'] = Audio(file_id=method.audio, file_unique_id='unique', duration=1)
            payload['caption'] = method.caption
            payload['caption_entities'] = method.caption_entities
        return Message(**payload)

    async def make_request(self, bot, method, timeout=None):
        api_name = method.__api_method__
        self.methods.append(method)
        error = self._error_for(api_name)
        if error is not None:
            raise error
        if api_name == 'getChatMember':
            return self._member(method.user_id, self._member_status(method.chat_id, method.user_id))
        if api_name == 'getChat':
            return self.chats.get(method.chat_id,
                                  Chat(id=method.chat_id, type='supergroup', title='Группа'))
        if api_name == 'getMe':
            return User(id=bot.id, is_bot=True, first_name='Bot', username='test_bot')
        if api_name.startswith('send') or api_name.startswith('edit'):
            self._message_id += 1
            return self._result(api_name, method)
        return True

    def _member_status(self, chat_id, user_id):
        return 'member' if user_id == 123456 else 'left'

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b''


def handler_callbacks(observer):
    return [getattr(handler, 'callback', handler) for handler in observer.handlers]


class BroadcastFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        base = ROOT / '.test-data'
        base.mkdir(exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(dir=base))
        self.path = self.directory / 'test.db'
        self.db = Database(str(self.path))
        self.adb = AsyncDatabase(self.db)
        self.config = Config(bot_token='123456:offline-test-token', work_chat_id=WORK_CHAT,
                             database_path=str(self.path), timezone='Europe/Moscow',
                             broadcast_send_interval=0.05)
        common.database = self.adb
        common.config = self.config
        self.session = RecordingSession()
        self.bot = Bot('123456:offline-test-token', session=self.session)
        self.service = BroadcastService(self.bot, self.adb, self.config)
        self.db.add_botadmin(ADMIN, 'admin')
        self.db.add_specialist(SPECIALIST, 'specialist')
        self.state = self.fsm(ADMIN, ADMIN)

    async def asyncTearDown(self):
        await self.bot.session.close()
        shutil.rmtree(self.directory)

    # ── helpers ──────────────────────────────────────────────────────────

    def fsm(self, user_id=ADMIN, chat_id=ADMIN):
        return FSMContext(storage=MemoryStorage(),
                          key=StorageKey(bot_id=self.bot.id, chat_id=chat_id, user_id=user_id))

    def message(self, text=None, entities=None, user_id=ADMIN, chat_id=ADMIN,
                chat_type='private', **extra):
        payload = {'message_id': 1000 + len(self.session.methods),
                   'date': datetime.now(),
                   'chat': {'id': chat_id, 'type': chat_type,
                            'title': 'Группа' if chat_type != 'private' else None},
                   'from_user': {'id': user_id, 'is_bot': False, 'first_name': 'Admin'},
                   'text': text, 'entities': entities, **extra}
        return Message.model_validate(payload, context={'bot': self.bot})

    def callback(self, data, user_id=ADMIN, message_id=900, chat_id=ADMIN):
        payload = {'id': str(len(self.session.methods) + 1),
                   'from_user': {'id': user_id, 'is_bot': False, 'first_name': 'Admin'},
                   'chat_instance': 'instance', 'data': data,
                    'message': {'message_id': message_id, 'date': datetime.now(),
                                'chat': {'id': chat_id, 'type': 'private'},
                                'from_user': {'id': self.bot.id, 'is_bot': True, 'first_name': 'Bot'},
                               'text': 'кнопки'}}
        return CallbackQuery.model_validate(payload, context={'bot': self.bot})

    async def open_section(self):
        message = self.message(text=keyboards.BTN_BROADCAST)
        await keyboards.btn_broadcast(message)
        return message

    async def new_draft(self, user_id=ADMIN, state=None):
        await self.open_section()
        await keyboards.btn_broadcast_new(self.message(text=keyboards.BTN_BC_NEW, user_id=user_id),
                                          state or self.state)

    def latest_broadcast(self):
        rows = self.db.list_broadcasts(10, 0)
        return rows[0] if rows else None

    def button_labels(self, markup):
        return [button.text for row in markup.inline_keyboard for button in row]


class AccessTests(BroadcastFixture):
    async def test_section_is_closed_for_non_admins(self):
        await self.open_section()
        self.assertTrue(any('📣' in text for text in self.session.texts()))
        await keyboards.btn_broadcast(self.message(text=keyboards.BTN_BROADCAST, user_id=SPECIALIST))
        await keyboards.btn_broadcast(self.message(text=keyboards.BTN_BROADCAST, user_id=CLIENT))
        denied = [text for text in self.session.texts() if text.startswith('⛔')]
        self.assertEqual(len(denied), 2)
        self.assertTrue(all('администраторам' in text for text in denied))

    async def test_group_chat_cannot_open_the_section(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'Группа')
        group_message = self.message(text=keyboards.BTN_BROADCAST, chat_id=GROUP_A,
                                     chat_type='supergroup')
        # The menu handlers are private-only, and the guard refuses group chats as well.
        self.assertFalse(await broadcasts._require_admin(group_message))
        await broadcasts.broadcast_callback(self.callback('bc:new', chat_id=GROUP_A), self.state)
        self.assertFalse(any('Отправка' in text for text in self.session.texts()))
        self.assertIsNone(self.latest_broadcast())

    async def test_role_is_rechecked_when_content_arrives(self):
        await self.new_draft()
        self.db.delete_botadmin(ADMIN)
        await broadcasts.take_broadcast_content(self.message(text='Текст рассылки'), self.state)
        self.assertIsNone(self.latest_broadcast())
        self.assertTrue(any(text.startswith('⛔') for text in self.session.texts()))

    async def test_role_is_rechecked_before_launch(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'Группа')
        await broadcasts.take_broadcast_content(self.message(text='Текст'), self.state)
        broadcast_id = self.latest_broadcast()['id']
        self.db.delete_botadmin(ADMIN)
        await broadcasts.start_now_callback(self.callback(f'bc:send:{broadcast_id}'), self.service)
        # The button press is refused and nothing is queued for a former administrator.
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'draft')
        self.assertEqual(self.db.broadcast_counts(broadcast_id)['total'], 0)
        self.assertTrue(any(text.startswith('⛔')
                            for text in self.session.texts('answerCallbackQuery')))

    async def test_launch_of_a_scheduled_broadcast_requires_the_author_role(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.delete_botadmin(ADMIN)
        self.assertEqual(await self.service.start_broadcast(broadcast_id, admin_id=ADMIN), 'not_admin')
        broadcast = self.db.get_broadcast(broadcast_id)
        self.assertEqual((broadcast['status'], broadcast['reason']), ('cancelled', 'author'))
        self.assertTrue(any('больше не администратор' in text for text in self.session.texts()))

    async def test_callback_access_is_checked(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        await broadcasts.start_now_callback(self.callback(f'bc:send:{broadcast_id}', user_id=CLIENT),
                                            self.service)
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'draft')

    async def test_foreign_administrator_cannot_touch_someone_elses_draft(self):
        other = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.add_botadmin(99, 'other')
        await broadcasts.start_now_callback(self.callback(f'bc:send:{other}', user_id=99), self.service)
        self.assertEqual(self.db.get_broadcast(other)['status'], 'draft')


class ContentTests(BroadcastFixture):
    def test_entities_keep_utf16_offsets_with_emoji(self):
        text = '😀Привет мир'
        entities = [MessageEntity(type='bold', offset=0, length=2),
                    MessageEntity(type='italic', offset=2, length=6)]
        content = extract_content(self.message(text=text, entities=entities))
        # The Python index of "Привет" is 1, but Telegram counts the emoji as two units.
        self.assertEqual(text.index('Привет'), 1)
        self.assertEqual(content['entities'][1]['offset'], 2)
        self.assertEqual(content['entities'][1]['length'], 6)
        restored = build_entities(content['entities'])
        self.assertEqual([(item.type, item.offset, item.length) for item in restored],
                         [('bold', 0, 2), ('italic', 2, 6)])

    def test_entity_cutting_an_emoji_pair_is_rejected(self):
        text = 'A😀B'
        for offset, length in ((2, 1), (1, 1), (2, 2)):
            with self.subTest(offset=offset), self.assertRaises(BroadcastValidationError):
                extract_content(self.message(
                    text=text, entities=[MessageEntity(type='bold', offset=offset, length=length)]))
        # The whole emoji pair is a valid entity.
        extract_content(self.message(
            text=text, entities=[MessageEntity(type='bold', offset=1, length=2)]))

    def test_text_limits_count_characters_but_entities_use_utf16(self):
        extract_content(self.message(text='я' * 4096))
        with self.assertRaises(BroadcastValidationError) as overflow:
            extract_content(self.message(text='я' * 4097))
        self.assertIn('4096', overflow.exception.hint)
        photo = [PhotoSize(file_id='BIG', file_unique_id='u', width=10, height=10)]
        extract_content(self.message(caption='я' * 1024, photo=photo))
        with self.assertRaises(BroadcastValidationError) as caption:
            extract_content(self.message(caption='😀' * 1025, photo=photo))
        self.assertIn('1024', caption.exception.hint)
        with self.assertRaises(BroadcastValidationError):
            extract_content(self.message(text='😀' * 4097))
        extract_content(self.message(text='😀' * 4096,
                        entities=[MessageEntity(type='bold', offset=0, length=8192)]))

    def test_empty_text_is_rejected(self):
        with self.assertRaises(BroadcastValidationError):
            extract_content(self.message(text='   '))

    async def test_album_is_rejected_without_losing_the_message(self):
        await self.new_draft()
        photo = [PhotoSize(file_id='BIG', file_unique_id='u', width=10, height=10)]
        await broadcasts.take_broadcast_content(
            self.message(photo=photo, caption='Альбом', media_group_id='album-1'), self.state)
        self.assertTrue(any('альбом' in text.lower() for text in self.session.texts()))
        self.assertIsNone(self.latest_broadcast())
        # The dialogue stays usable and the next single attachment is accepted.
        await broadcasts.take_broadcast_content(self.message(text='Обычный текст'), self.state)
        self.assertEqual(self.latest_broadcast()['text'], 'Обычный текст')

    async def test_unsupported_attachment_is_never_silently_dropped(self):
        await self.new_draft()
        sticker = Sticker(file_id='S', file_unique_id='u', width=10, height=10, type='regular',
                 is_animated=False, is_video=False)
        await broadcasts.take_broadcast_content(self.message(sticker=sticker), self.state)
        self.assertTrue(any('не поддерживает' in text for text in self.session.texts()))
        self.assertIsNone(self.latest_broadcast())
        await broadcasts.take_broadcast_content(
            self.message(document=Document(file_id='DOC', file_unique_id='u')), self.state)
        self.assertEqual(self.latest_broadcast()['media_type'], 'document')

    async def test_supported_attachments_keep_caption_and_formatting(self):
        cases = [
            ('photo', {'photo': [PhotoSize(file_id='SMALL', file_unique_id='u', width=1, height=1),
                                 PhotoSize(file_id='LARGE', file_unique_id='u', width=90, height=90)]},
             'LARGE', 'sendPhoto', 'photo'),
            ('document', {'document': Document(file_id='DOC', file_unique_id='u')},
             'DOC', 'sendDocument', 'document'),
            ('video', {'video': Video(file_id='VID', file_unique_id='u', width=1, height=1, duration=1)},
             'VID', 'sendVideo', 'video'),
            ('audio', {'audio': Audio(file_id='AUD', file_unique_id='u', duration=1)},
             'AUD', 'sendAudio', 'audio'),
        ]
        for label, extra, expected_id, api_name, attribute in cases:
            with self.subTest(label):
                await self.new_draft()
                entities = [MessageEntity(type='bold', offset=0, length=2)]
                await broadcasts.take_broadcast_content(
                    self.message(caption='Подпись', caption_entities=entities, **extra), self.state)
                broadcast = self.latest_broadcast()
                self.assertEqual(broadcast['media_type'], label)
                self.assertEqual(broadcast['media_file_id'], expected_id)
                self.assertEqual(broadcast['caption'], 'Подпись')
                self.assertEqual(broadcast['caption_entities'][0]['offset'], 0)
                call = self.session.sent(api_name)[-1]
                self.assertEqual(getattr(call, attribute), expected_id)
                self.assertEqual(call.caption, 'Подпись')
                self.assertEqual(call.caption_entities[0].type, 'bold')
                self.assertIsNone(call.parse_mode)

    async def test_preview_is_an_exact_copy_and_buttons_come_in_a_new_message(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'Группа')
        await self.new_draft()
        entities = [MessageEntity(type='bold', offset=0, length=6),
                    MessageEntity(type='text_link', offset=7, length=5, url='https://e.org')]
        await broadcasts.take_broadcast_content(
            self.message(text='Привет ссылка', entities=entities), self.state)
        broadcast = self.latest_broadcast()
        sent = self.session.sent('sendMessage')
        preview, explanation = sent[-2], sent[-1]
        self.assertEqual(preview.text, 'Привет ссылка')
        self.assertEqual([item.type for item in preview.entities], ['bold', 'text_link'])
        self.assertIsNone(preview.parse_mode)
        # Explanations and buttons arrive in a separate message, the preview is never edited.
        self.assertFalse(self.session.sent('editMessageText'))
        self.assertIn(f'#{broadcast["id"]}', explanation.text)
        self.assertIn('Известных групп-получателей: <b>1</b>', explanation.text)
        self.assertEqual(self.button_labels(explanation.reply_markup),
                          ['🚀 Отправить сейчас', '🕒 По таймеру', '✖ Отменить'])

    async def test_failed_preview_is_reported_without_a_false_confirmation(self):
        self.session.errors['sendPhoto'] = TelegramNetworkError(method=SendMessage(chat_id=1, text='x'),
                                                                message='timeout')
        await self.new_draft()
        await broadcasts.take_broadcast_content(
            self.message(photo=[PhotoSize(file_id='P', file_unique_id='u', width=1, height=1)],
                         caption='Подпись'), self.state)
        broadcast = self.latest_broadcast()
        self.assertEqual(broadcast['status'], 'draft')
        self.assertIsNone(broadcast['preview_message_id'])
        self.assertTrue(any('Не удалось отправить предпросмотр' in text
                            for text in self.session.texts()))
        self.assertFalse(any('Черновик рассылки' in text for text in self.session.texts()))


class RecipientRegistryTests(BroadcastFixture):
    def test_registry_keeps_only_sendable_groups(self):
        self.assertTrue(self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'A'))
        self.assertTrue(self.db.register_broadcast_chat(GROUP_B, 'group', 'B'))
        self.assertFalse(self.db.register_broadcast_chat(5, 'private'))
        self.assertFalse(self.db.apply_broadcast_chat_member(CHANNEL, 'channel', 'administrator'))
        self.assertTrue(self.db.apply_broadcast_chat_member(WORK_CHAT, 'supergroup', 'administrator'))
        self.assertTrue(self.db.apply_broadcast_chat_member(OLD_GROUP, 'supergroup', 'left'))
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats()],
                         sorted([GROUP_A, GROUP_B, WORK_CHAT]))
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats(exclude_chat_ids=(WORK_CHAT,))],
                         sorted([GROUP_A, GROUP_B]))
        self.assertEqual(self.db.count_broadcast_chats(exclude_chat_ids=(WORK_CHAT,)), 2)

    async def test_my_chat_member_tracks_adding_and_removal(self):
        await broadcasts.track_chat_member(self._member_event(GROUP_A, 'administrator', 'left'))
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats()], [GROUP_A])
        await broadcasts.track_chat_member(self._member_event(GROUP_A, 'left', 'administrator'))
        self.assertEqual(self.db.get_broadcast_chats(), [])

    async def test_restricted_member_without_send_rights_is_skipped(self):
        bot_user = User(id=self.bot.id, is_bot=True, first_name='Bot')
        event = ChatMemberUpdated(chat=Chat(id=GROUP_B, type='supergroup', title='B'),
                                  from_user=User(id=ADMIN, is_bot=False, first_name='Admin'),
                                  date=datetime.now(),
                                  old_chat_member=ChatMemberMember(user=bot_user, status='member'),
                                  new_chat_member=ChatMemberRestricted(
                                      user=bot_user, status='restricted', is_member=True,
                                      can_send_messages=False, can_send_audios=True,
                                      can_send_documents=True, can_send_photos=True,
                                      can_send_videos=True, can_send_video_notes=True,
                                      can_send_voice_notes=True, can_send_polls=True,
                                      can_send_other_messages=True, can_add_web_page_previews=True,
                                      can_change_info=True, can_invite_users=True,
                                      can_pin_messages=True, can_manage_topics=True,
                                      until_date=0))
        await broadcasts.track_chat_member(event)
        self.assertEqual(self.db.get_broadcast_chats(), [])
        with self.db.connect() as conn:
            row = conn.execute('SELECT active, can_send FROM broadcast_chats WHERE chat_id=?',
                               (GROUP_B,)).fetchone()
        self.assertEqual(row, (1, 0))

    async def test_work_chat_member_event_does_not_register_the_work_chat(self):
        await broadcasts.track_chat_member(self._member_event(WORK_CHAT, 'administrator', 'left'))
        self.assertEqual(self.db.get_broadcast_chats(), [])

    async def test_group_messages_register_the_chat(self):
        async def handler(event, data):
            return 'handled'
        result = await broadcasts.remember_group_messages(
            handler, self.message(text='обычное сообщение', chat_id=GROUP_A, chat_type='supergroup'), {})
        self.assertEqual(result, 'handled')
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats()], [GROUP_A])

    async def test_work_chat_is_never_remembered_as_a_recipient(self):
        async def handler(event, data):
            return 'handled'
        await broadcasts.remember_group_messages(
            handler, self.message(text='рабочий чат', chat_id=WORK_CHAT, chat_type='supergroup'), {})
        self.assertEqual(self.db.get_broadcast_chats(), [])

    async def test_forward_origin_is_never_registered(self):
        origin = MessageOriginUser(user=User(id=CLIENT, is_bot=False, first_name='Client'),
                                   sender_user=User(id=CLIENT, is_bot=False, first_name='Client'),
                                   date=datetime.now())

        async def handler(event, data):
            return 'handled'
        await broadcasts.remember_group_messages(handler, self.message(text='переслано',
                                                                       forward_origin=origin), {})
        self.assertEqual(self.db.get_broadcast_chats(), [])

    async def test_whoami_command_registers_an_old_group(self):
        await broadcasts.whoami_command(
            self.message(text='/whoami', chat_id=GROUP_B, chat_type='supergroup'))
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats()], [GROUP_B])
        self.assertTrue(any('зарегистрирована' in text for text in self.session.texts()))

    def test_work_chat_and_private_chats_are_excluded_from_recipients(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'A')
        self.db.apply_broadcast_chat_member(WORK_CHAT, 'supergroup', 'administrator')
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.assertEqual(self.db.start_broadcast(broadcast_id, exclude_chat_ids=(WORK_CHAT,)), 'running')
        with self.db.connect() as conn:
            targets = conn.execute('SELECT chat_id FROM broadcast_jobs WHERE broadcast_id=?',
                                   (broadcast_id,)).fetchall()
        self.assertEqual(targets, [(GROUP_A,)])

    def test_migration_updates_a_chat_without_duplicating_targets(self):
        with self.db.connect() as conn:
            conn.execute('DELETE FROM broadcast_chats')
        self.db.apply_broadcast_chat_member(OLD_GROUP, 'supergroup', 'member', True, 'Старая')
        self.db.create_request(CLIENT, 'client', 'текст', OLD_GROUP, 7)
        first = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='раз')
        self.db.start_broadcast(first, (WORK_CHAT,))
        second = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='два')
        self.db.migrate_broadcast_chat(OLD_GROUP, NEW_GROUP, 'supergroup', 'Новая')
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats()], [NEW_GROUP])
        self.assertEqual(self.db.start_broadcast(second, (WORK_CHAT,)), 'running')
        with self.db.connect() as conn:
            rows = conn.execute('SELECT chat_id FROM broadcast_jobs WHERE broadcast_id=?',
                                (second,)).fetchall()
            request_chat = conn.execute('SELECT chat_id FROM requests').fetchone()[0]
        self.assertEqual(rows, [(NEW_GROUP,)])
        self.assertEqual(request_chat, NEW_GROUP)

    async def test_candidate_chats_are_verified_before_they_become_recipients(self):
        self.db.create_request(CLIENT, 'client', 'текст', GROUP_A, 5)
        self.db.create_request(CLIENT, 'client', 'текст', CHANNEL, 6)
        self.db = Database(str(self.path))  # a restart backfills candidates from known requests
        self.adb = AsyncDatabase(self.db)
        common.database = self.adb
        self.service = BroadcastService(self.bot, self.adb, self.config)
        self.session.chats[GROUP_A] = Chat(id=GROUP_A, type='supergroup', title='A')
        self.session.chats[CHANNEL] = Chat(id=CHANNEL, type='channel', title='Канал')
        await self.service.tick()
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats()], [GROUP_A])

    def _member_event(self, chat_id, new_status, old_status):
        bot_user = User(id=self.bot.id, is_bot=True, first_name='Bot')
        return ChatMemberUpdated(chat=Chat(id=chat_id, type='supergroup', title='Группа'),
                                 from_user=User(id=ADMIN, is_bot=False, first_name='Admin'),
                                 date=datetime.now(),
                                 old_chat_member=self.session._member(bot_user.id, old_status),
                                  new_chat_member=self.session._member(bot_user.id, new_status)).as_(self.bot)


class QueueTests(BroadcastFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'A')
        self.db.register_broadcast_chat(GROUP_B, 'supergroup', 'B')

    def test_start_is_atomic_and_creates_one_job_per_chat(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.assertEqual(self.db.start_broadcast(broadcast_id, (WORK_CHAT,)), 'running')
        self.assertIsNone(self.db.start_broadcast(broadcast_id, (WORK_CHAT,)))
        self.assertEqual(self.db.broadcast_counts(broadcast_id)['total'], 2)
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'running')
        self.assertEqual(self.db.get_broadcast(broadcast_id)['recipients'], 2)

    def test_broadcast_without_recipients_is_marked(self):
        empty = Database(str(self.directory / 'empty.db'))
        broadcast_id = empty.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.assertEqual(empty.start_broadcast(broadcast_id, ()), 'no_recipients')
        self.assertEqual(empty.get_broadcast(broadcast_id)['status'], 'no_recipients')

    async def test_send_now_and_repeated_button_do_not_start_twice(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        await broadcasts.start_now_callback(self.callback(f'bc:send:{broadcast_id}'), self.service)
        await broadcasts.start_now_callback(self.callback(f'bc:send:{broadcast_id}'), self.service)
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'running')
        self.assertEqual(self.db.broadcast_counts(broadcast_id)['total'], 2)
        self.assertTrue(any('повторное нажатие' in text for text in self.session.texts()))

    async def test_no_recipients_is_reported_to_the_admin(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        with self.db.connect() as conn:
            conn.execute('DELETE FROM broadcast_chats')
        await broadcasts.start_now_callback(self.callback(f'bc:send:{broadcast_id}'), self.service)
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'no_recipients')
        self.assertTrue(any('нет получателей' in text for text in self.session.texts()))

    async def test_delivery_matches_the_preview_byte_for_byte(self):
        entities = [MessageEntity(type='bold', offset=0, length=6)]
        await self.new_draft()
        await broadcasts.take_broadcast_content(
            self.message(caption='Подпись 😀', caption_entities=entities,
                         photo=[PhotoSize(file_id='LARGE', file_unique_id='u', width=9, height=9)]),
            self.state)
        broadcast_id = self.latest_broadcast()['id']
        preview = self.session.sent('sendPhoto')[0]
        await broadcasts.start_now_callback(self.callback(f'bc:send:{broadcast_id}'), self.service)
        await self.service.tick()
        delivery = self.session.sent('sendPhoto')[-1]
        self.assertNotEqual(preview.chat_id, delivery.chat_id)
        self.assertEqual(delivery.chat_id, GROUP_A)
        for field in ('photo', 'caption', 'caption_entities', 'parse_mode'):
            self.assertEqual(getattr(delivery, field), getattr(preview, field), field)

    async def test_counters_and_final_status_come_from_real_jobs(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.start_broadcast(broadcast_id, (WORK_CHAT,))
        await self.service.tick()
        counts = self.db.broadcast_counts(broadcast_id)
        self.assertEqual((counts['sent'], counts['total']), (2, 2))
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'completed')

    async def test_failure_in_one_chat_does_not_stop_the_others(self):
        self.session.errors['sendMessage'] = [
            TelegramForbiddenError(method=SendMessage(chat_id=1, text='x'), message='Forbidden')]
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.start_broadcast(broadcast_id, (WORK_CHAT,))
        await self.service.tick()
        counts = self.db.broadcast_counts(broadcast_id)
        self.assertEqual(counts['sent'], 1)
        self.assertEqual(counts['blocked'], 1)
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'completed_with_errors')

    async def test_retry_after_pauses_the_queue_and_requeues_the_job(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.start_broadcast(broadcast_id, (WORK_CHAT,))
        self.session.errors['sendMessage'] = [
            TelegramRetryAfter(method=SendMessage(chat_id=1, text='x'),
                               message='Too Many Requests', retry_after=42)]
        await self.service.tick()
        counts = self.db.broadcast_counts(broadcast_id)
        self.assertEqual(counts['pending'], 2)
        self.assertGreater(self.service.retry_until, 0)
        with self.db.connect() as conn:
            error = conn.execute('SELECT last_error FROM broadcast_jobs LIMIT 1').fetchone()[0]
        self.assertEqual(error, 'retry_after')

    async def test_forbidden_chat_is_blocked_but_stays_registered(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.start_broadcast(broadcast_id, (WORK_CHAT,))
        self.session.errors['sendMessage'] = [
            TelegramForbiddenError(method=SendMessage(chat_id=1, text='x'),
                                   message='Forbidden: bot was blocked')]
        await self.service.tick()
        counts = self.db.broadcast_counts(broadcast_id)
        self.assertEqual(counts['blocked'], 1)
        self.assertEqual(counts['sent'], 1)
        with self.db.connect() as conn:
            rows = conn.execute('SELECT chat_id, active, can_send FROM broadcast_chats').fetchall()
        blocked = [chat_id for chat_id, _, can_send in rows if can_send == 0]
        self.assertEqual(len(blocked), 1)
        # A temporary access problem never deactivates the chat for good.
        self.assertTrue(all(active == 1 for _, active, _ in rows))
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats()],
                         [chat_id for chat_id, active, can_send in rows if active and can_send])

    async def test_network_error_is_uncertain_and_is_not_retried(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.start_broadcast(broadcast_id, (WORK_CHAT,))
        self.session.errors['sendMessage'] = [
            TelegramNetworkError(method=SendMessage(chat_id=1, text='x'), message='connection lost'),
            TelegramNetworkError(method=SendMessage(chat_id=1, text='x'), message='connection lost')]
        await self.service.tick()
        counts = self.db.broadcast_counts(broadcast_id)
        self.assertEqual(counts['uncertain'], 2)
        self.assertEqual(counts['sent'], 0)
        await self.service.tick()
        self.assertEqual(self.db.broadcast_counts(broadcast_id)['uncertain'], 2)
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'completed_with_errors')

    async def test_chat_not_found_and_too_long_are_recorded_separately(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.start_broadcast(broadcast_id, (WORK_CHAT,))
        self.session.errors['sendMessage'] = [
            TelegramBadRequest(method=SendMessage(chat_id=1, text='x'),
                               message='Bad Request: chat not found'),
            TelegramBadRequest(method=SendMessage(chat_id=1, text='x'),
                               message='Bad Request: message is too long'),
        ]
        await self.service.tick()
        counts = self.db.broadcast_counts(broadcast_id)
        self.assertEqual(counts['blocked'], 1)
        self.assertEqual(counts['failed'], 1)

    async def test_migrated_chat_changes_the_address_without_duplicating_the_target(self):
        with self.db.connect() as conn:
            conn.execute('DELETE FROM broadcast_chats')
        self.db.apply_broadcast_chat_member(OLD_GROUP, 'supergroup', 'member', True, 'Старая')
        self.db.create_request(CLIENT, 'client', 'текст', OLD_GROUP, 7)
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.start_broadcast(broadcast_id, (WORK_CHAT,))
        self.session.errors['sendMessage'] = [
            TelegramMigrateToChat(method=SendMessage(chat_id=1, text='x'),
                                 message='Bad Request: group chat was upgraded',
                                 migrate_to_chat_id=NEW_GROUP)]
        await self.service.tick()
        self.assertEqual([row[0] for row in self.db.get_broadcast_chats()], [NEW_GROUP])
        with self.db.connect() as conn:
            request_chat = conn.execute('SELECT chat_id FROM requests').fetchone()[0]
            conn.execute("UPDATE broadcast_jobs SET next_attempt='' WHERE broadcast_id=?",
                         (broadcast_id,))
        self.assertEqual(request_chat, NEW_GROUP)
        await self.service.tick()
        self.assertEqual(self.session.sent('sendMessage')[-1].chat_id, NEW_GROUP)
        counts = self.db.broadcast_counts(broadcast_id)
        self.assertEqual((counts['sent'], counts['total']), (1, 1))

    async def test_interrupted_send_is_uncertain_after_a_restart(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.start_broadcast(broadcast_id, (WORK_CHAT,))
        self.assertIsNotNone(self.db.claim_broadcast_job())
        restarted = Database(str(self.path))
        self.assertEqual(restarted.restore_broadcasts_on_start(), 1)
        counts = restarted.broadcast_counts(broadcast_id)
        self.assertEqual(counts['uncertain'], 1)
        self.assertEqual(counts['pending'], 1)

    async def test_scheduled_broadcast_runs_after_a_restart(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        moment = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.assertTrue(self.db.schedule_broadcast(broadcast_id, moment, 'Europe/Moscow'))
        restarted_db = Database(str(self.path))
        service = BroadcastService(self.bot, AsyncDatabase(restarted_db), self.config)
        await service.tick()
        self.assertEqual(restarted_db.get_broadcast(broadcast_id)['status'], 'completed')
        self.assertEqual(restarted_db.broadcast_counts(broadcast_id)['sent'], 2)

    async def test_scheduled_broadcast_is_cancelled_when_the_author_loses_the_role(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.schedule_broadcast(broadcast_id,
                                   (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                                   'Europe/Moscow')
        self.db.delete_botadmin(ADMIN)
        await self.service.tick()
        broadcast = self.db.get_broadcast(broadcast_id)
        self.assertEqual(broadcast['status'], 'cancelled')
        self.assertEqual(broadcast['reason'], 'author')
        self.assertEqual(self.db.broadcast_counts(broadcast_id)['total'], 0)
        self.assertTrue(any('больше не администратор' in text for text in self.session.texts()))


class ScheduleTests(BroadcastFixture):
    def test_relative_and_absolute_times_are_parsed(self):
        moment = parse_schedule('30', 'Europe/Moscow')
        self.assertEqual(moment.tzinfo, timezone.utc)
        minutes = (moment - datetime.now(timezone.utc)).total_seconds() / 60
        self.assertTrue(29 <= minutes <= 31)
        self.assertEqual(parse_schedule('2030-01-02 10:00', 'Europe/Moscow')
                         .astimezone(timezone.utc).hour, 7)

    def test_invalid_input_is_explained(self):
        for raw in ('0', '-5', '999999999', '02.01.2030 10:00', '2030-02-30 10:00',
                    '2030-01-02 25:00', 'завтра'):
            with self.subTest(raw=raw), self.assertRaises(BroadcastValidationError):
                parse_schedule(raw, 'Europe/Moscow')

    def test_past_and_ambiguous_local_times_are_rejected(self):
        past = (datetime.now() - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M')
        with self.assertRaises(BroadcastValidationError):
            parse_schedule(past, 'Europe/Moscow')
        # Moscow's final backward transition: 2014-10-26 01:30 repeated.
        with self.assertRaises(BroadcastValidationError) as ambiguous:
            parse_schedule('2014-10-26 01:30', 'Europe/Moscow')
        self.assertIn('повторяется', ambiguous.exception.hint)
        with self.assertRaises(BroadcastValidationError) as missing:
            parse_schedule('2011-03-27 02:30', 'Europe/Moscow')
        self.assertIn('не существует', missing.exception.hint)

    async def test_timer_flow_keeps_the_draft_on_a_wrong_time(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'A')
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        await broadcasts.broadcast_callback(self.callback(f'bc:sched:{broadcast_id}'), self.state)
        self.assertIn('Europe/Moscow', self.session.texts()[-1])
        await broadcasts.take_broadcast_schedule(self.message(text='когда-нибудь'), self.state)
        self.assertTrue(any('Формат не распознан' in text for text in self.session.texts()))
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'draft')
        self.assertIn('✖ Отменить', self.button_labels(self.session.sent('sendMessage')[-1].reply_markup))
        await broadcasts.take_broadcast_schedule(self.message(text='45'), self.state)
        broadcast = self.db.get_broadcast(broadcast_id)
        self.assertEqual(broadcast['status'], 'scheduled')
        self.assertIsNotNone(broadcast['scheduled_at'])
        self.assertEqual(broadcast['timezone'], 'Europe/Moscow')
        self.assertEqual(self.db.broadcast_counts(broadcast_id)['total'], 0)

    async def test_recipients_are_snapshotted_only_at_launch(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'A')
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        self.db.schedule_broadcast(broadcast_id,
                                   (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(),
                                   'Europe/Moscow')
        await broadcasts.track_chat_member(self._member_event(GROUP_B, 'administrator', 'left'))
        await self.service.tick()
        self.assertEqual(self.db.broadcast_counts(broadcast_id)['total'], 2)

    def _member_event(self, chat_id, new_status, old_status):
        bot_user = User(id=self.bot.id, is_bot=True, first_name='Bot')
        return ChatMemberUpdated(chat=Chat(id=chat_id, type='supergroup', title='Группа'),
                                 from_user=User(id=ADMIN, is_bot=False, first_name='Admin'),
                                 date=datetime.now(),
                                 old_chat_member=self.session._member(bot_user.id, old_status),
                                  new_chat_member=self.session._member(bot_user.id, new_status)).as_(self.bot)


class CancellationTests(BroadcastFixture):
    async def test_slash_cancel_stops_the_content_dialogue(self):
        await self.new_draft()
        await commands.cancel_cmd(self.message(text='/cancel'), self.state)
        self.assertTrue(any('Подготовка рассылки отменена' in text for text in self.session.texts()))
        self.assertIsNone(self.latest_broadcast())
        self.assertIsNone(await self.state.get_state())

    async def test_slash_cancel_still_serves_other_dialogues(self):
        await commands.cancel_cmd(self.message(text='/cancel'), self.fsm(CLIENT, CLIENT))
        self.assertFalse(any('Подготовка рассылки' in text for text in self.session.texts()))

    async def test_cancel_button_and_repeat_are_safe(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        await broadcasts.broadcast_callback(self.callback(f'bc:cancel:{broadcast_id}'), self.state)
        await broadcasts.broadcast_callback(self.callback(f'bc:cancel:{broadcast_id}'), self.state)
        broadcast = self.db.get_broadcast(broadcast_id)
        self.assertEqual(broadcast['status'], 'cancelled')
        self.assertIsNotNone(broadcast['finished_at'])
        self.assertIn('cancelled', [row[2] for row in self.db.get_broadcast_audit(10)])

    async def test_cancel_during_schedule_input(self):
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        await broadcasts.broadcast_callback(self.callback(f'bc:sched:{broadcast_id}'), self.state)
        await commands.cancel_cmd(self.message(text='/cancel'), self.state)
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'cancelled')

    async def test_cancel_after_the_launch_reports_the_running_state(self):
        self.db.register_broadcast_chat(GROUP_A, 'supergroup', 'A')
        broadcast_id = self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        await broadcasts.start_now_callback(self.callback(f'bc:send:{broadcast_id}'), self.service)
        await broadcasts.broadcast_callback(self.callback(f'bc:cancel:{broadcast_id}'), self.state)
        self.assertEqual(self.db.get_broadcast(broadcast_id)['status'], 'running')
        self.assertTrue(any('уже выполняется' in text for text in self.session.texts()))

    async def test_list_paginates_and_shows_every_counter(self):
        for _ in range(7):
            self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет')
        await keyboards.btn_broadcast_list(self.message(text=keyboards.BTN_BC_LIST))
        first_page = self.session.texts()[-1]
        self.assertIn('страница 1 из 2', first_page)
        for label in ('Получателей: 0', 'Отправлено: 0', 'Ожидают: 0', 'Отправляется: 0',
                      'Ошибки: 0', 'Неопределённо: 0'):
            self.assertIn(label, first_page)
        await broadcasts.broadcast_callback(self.callback('bc:list:1'), self.state)
        self.assertIn('страница 2 из 2', self.session.sent('editMessageText')[-1].text)

    async def test_list_never_shows_internal_identifiers(self):
        self.db.create_broadcast(ADMIN, 'admin', ADMIN, text='привет', media_type='photo',
                                 media_file_id='SECRET-FILE-ID', caption='подпись')
        await keyboards.btn_broadcast_list(self.message(text=keyboards.BTN_BC_LIST))
        listing = self.session.texts()[-1]
        self.assertNotIn('SECRET-FILE-ID', listing)
        self.assertIn('фото', listing)


class WiringTests(unittest.TestCase):
    def test_broadcast_handlers_are_registered(self):
        message_handlers = handler_callbacks(broadcasts.router.message)
        self.assertIn(broadcasts.take_broadcast_content, message_handlers)
        self.assertIn(broadcasts.take_broadcast_schedule, message_handlers)
        self.assertIn(broadcasts.whoami_command, message_handlers)
        self.assertIn(broadcasts.broadcast_callback, handler_callbacks(broadcasts.router.callback_query))
        self.assertIn(broadcasts.start_now_callback, handler_callbacks(broadcasts.router.callback_query))
        self.assertIn(broadcasts.track_chat_member, handler_callbacks(broadcasts.router.my_chat_member))
        self.assertEqual(len(broadcasts.router.message.outer_middleware), 0)

    def test_menu_buttons_are_registered(self):
        menu_handlers = handler_callbacks(keyboards.router.message)
        self.assertIn(keyboards.btn_broadcast, menu_handlers)
        self.assertIn(keyboards.btn_broadcast_new, menu_handlers)
        self.assertIn(keyboards.btn_broadcast_list, menu_handlers)
        labels = [button.text for row in keyboards.kb_admin().keyboard for button in row]
        self.assertIn(keyboards.BTN_BROADCAST, labels)
        self.assertIn(keyboards.BTN_BROADCAST,
                      [button.text for row in keyboards.kb_settings().keyboard for button in row])

    def test_existing_menu_entries_are_preserved(self):
        labels = [button.text for row in keyboards.kb_admin().keyboard for button in row]
        for label in (keyboards.BTN_MY_REQUESTS, keyboards.BTN_MY_STATS, keyboards.BTN_STATS,
                      keyboards.BTN_EXPORT, keyboards.BTN_SETTINGS, keyboards.BTN_HELP):
            self.assertIn(label, labels)
        settings = [button.text for row in keyboards.kb_settings().keyboard for button in row]
        for label in (keyboards.BTN_OPEN_REQUESTS, keyboards.BTN_UNASSIGNED, keyboards.BTN_SPECIALISTS,
                      keyboards.BTN_BOTADMINS, keyboards.BTN_TIMEOUT, keyboards.BTN_NOTICES,
                      keyboards.BTN_DAILY, keyboards.BTN_UNASSIGNED_D, keyboards.BTN_STOPWORDS,
                      keyboards.BTN_AUTOCLEAN, keyboards.BTN_BACK):
            self.assertIn(label, settings)

    def test_dispatcher_registers_the_broadcast_router_and_my_chat_member(self):
        import inspect

        import main
        self.assertIn('broadcast.router', inspect.getsource(main.build_dispatcher))
        from aiogram import Dispatcher
        dp = Dispatcher()
        dp.message.register(lambda: None)
        updates = polling_updates(dp)
        self.assertIn('my_chat_member', updates)
        self.assertIn('message', updates)
        self.assertEqual(updates.count('my_chat_member'), 1)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.test-data'
        base.mkdir(exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(dir=base))
        self.path = self.directory / 'legacy.db'

    def tearDown(self):
        self.assertTrue(self.directory.resolve().is_relative_to((ROOT / '.test-data').resolve()))
        shutil.rmtree(self.directory)

    def test_existing_data_survives_the_broadcast_migration(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('CREATE TABLE botadmins (user_id INTEGER PRIMARY KEY, username TEXT)')
            conn.execute('INSERT INTO botadmins VALUES (42, "admin")')
            conn.execute("""CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                username TEXT, description TEXT, status TEXT, specialist TEXT, start_time TEXT,
                end_time TEXT, chat_id INTEGER, message_id INTEGER, bot_message_id INTEGER,
                work_message_id INTEGER, created_time TEXT)""")
            conn.execute("INSERT INTO requests (user_id, description, status, chat_id) "
                         "VALUES (5, 'старая заявка', 'closed', -100999)")
            conn.commit()
        upgraded = Database(str(self.path))
        with upgraded.connect() as conn:
            description, chat_id = conn.execute('SELECT description, chat_id FROM requests').fetchone()
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        self.assertEqual(description, 'старая заявка')
        self.assertEqual(upgraded.is_botadmin(42), True)
        self.assertEqual(version, 2)
        self.assertTrue({'broadcasts', 'broadcast_jobs', 'broadcast_chats', 'broadcast_audit'} <= tables)
        self.assertEqual(len(list(self.directory.glob('legacy.db.backup-*'))), 1)
        Database(str(self.path))
        self.assertEqual(len(list(self.directory.glob('legacy.db.backup-*'))), 1)
        # Known client chats are only candidates until the worker verifies them.
        self.assertEqual(upgraded.get_broadcast_chat_candidates(), [-100999])
        self.assertEqual(upgraded.get_broadcast_chats(), [])
        self.assertEqual(chat_id, -100999)

    def test_existing_tables_and_indexes_are_untouched(self):
        Database(str(self.path))
        with Database(str(self.path)).connect() as conn:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({'requests', 'specialists', 'botadmins', 'announcements', 'delivery_queue',
                         'pending_requests'} <= names)
        self.assertIn('idx_requests_user_chat_status', indexes)
        self.assertIn('idx_broadcast_jobs_due', indexes)


if __name__ == '__main__':
    unittest.main()
