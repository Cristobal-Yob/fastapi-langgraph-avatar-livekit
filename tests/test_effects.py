import os
import unittest
from unittest.mock import AsyncMock, patch
from cloud.mail import MailRuntime


class EffectsTests(unittest.IsolatedAsyncioTestCase):
    async def test_mail_disabled_before_configuration(self):
        with patch.dict(os.environ, {'EMAIL_ENABLE_REAL_SEND':'false'}):
            result = await MailRuntime(allowed=True).dispatch(recipient='test@example.invalid',subject='Test',
                body='Synthetic',conversation_id='test',action_key='test',order_digest='test',authorized=True)
        self.assertEqual(result['status'],'disabled')

    async def test_unauthorized_mail_is_not_attempted(self):
        with patch.dict(os.environ, {'EMAIL_ENABLE_REAL_SEND':'true'}):
            result = await MailRuntime(allowed=True).dispatch(recipient='test@example.invalid',subject='Test',
                body='Synthetic',conversation_id='test',action_key='test',order_digest='test',authorized=False)
        self.assertEqual(result['status'],'authorization_required')

    async def test_base_never_enables_mail_from_environment_alone(self):
        with patch.dict(os.environ, {'EMAIL_ENABLE_REAL_SEND':'true'}):
            self.assertFalse(MailRuntime(allowed=False).enabled)


@unittest.skipUnless(os.environ.get('RUN_DATABASE_TESTS') == 'true', 'requires isolated synthetic PostgreSQL')
class DatabaseTests(unittest.TestCase):
    def test_claims_encryption_and_single_owner_survive_connection(self):
        from cloud.store import DurableStore
        from uuid import uuid4
        namespace = 'offline-test-'+uuid4().hex
        store = DurableStore(namespace,singleton=True)
        try:
            self.assertEqual(store.claim('turn','digest'),'new')
            self.assertEqual(store.claim('turn','digest'),'uncertain')
            self.assertEqual(store.claim('turn','changed'),'conflict')
            store.write('synthetic',{'value':'not-a-real-secret'})
            self.assertEqual(store.read('synthetic'),{'value':'not-a-real-secret'})
            raw = store.connection.execute('SELECT encrypted FROM cloud_records WHERE namespace=%s',(namespace,)).fetchone()[0]
            self.assertNotIn(b'not-a-real-secret',bytes(raw))
            with self.assertRaises(ValueError): DurableStore(namespace,singleton=True)
        finally: store.close()
        other = DurableStore(namespace,singleton=True)
        try: self.assertEqual(other.claim('turn','digest'),'uncertain')
        finally:
            other.connection.execute('DELETE FROM cloud_claims WHERE namespace=%s',(namespace,))
            other.connection.execute('DELETE FROM cloud_records WHERE namespace=%s',(namespace,))
            other.close()
