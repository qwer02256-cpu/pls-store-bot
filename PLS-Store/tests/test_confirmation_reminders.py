import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

PLUGIN=Path(__file__).resolve().parents[1]/'plugins/ConfirmOrderReminder.py'


class ConfirmationReminderTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('confirmation_test',PLUGIN)
        self.p=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.p)
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p.DB_PATH=Path(self.tmp.name)/'state.db'
        self.p._init_db()
        self.order=SimpleNamespace(id='AB12CD34',seller_id=10,buyer_username='Buyer',status=SimpleNamespace(name='PAID'))
        self.account=SimpleNamespace(id=10,get_order=Mock(return_value=self.order),
            get_chat_by_name=Mock(return_value=SimpleNamespace(id=123,name='Buyer')),
            chat_id_private=Mock(return_value=True),send_message=Mock(return_value=object()))
        self.card=SimpleNamespace(account=self.account)
        self.event=SimpleNamespace(order=self.order)
        self.p.time=SimpleNamespace(time=lambda:1000)

    def schedule(self):
        self.p.new_order_handler(self.card,self.event)

    def row(self):
        with self.p._db() as conn:
            return conn.execute('SELECT * FROM reminders').fetchone()

    def due(self):
        self.p.time.time=lambda:19000

    def test_timer_starts_at_payment_and_sends_one_conditional_reminder(self):
        self.schedule()
        self.assertEqual(self.row()['due'],19000)
        self.assertFalse(self.p._process_one(self.card))
        self.p.time.time=lambda:18999
        self.assertFalse(self.p._process_one(self.card))
        self.due()
        self.assertTrue(self.p._process_one(self.card))
        self.account.send_message.assert_called_once()
        text=self.account.send_message.call_args.args[1]
        self.assertIn('https://funpay.com/orders/AB12CD34/',text)
        self.assertIn('Если вы уже получили',text)
        self.assertNotIn('Вижу, что заказ выполнен',text)
        self.assertFalse(self.p._process_one(self.card))

    def test_duplicate_payment_does_not_reset_timer_or_duplicate(self):
        self.schedule()
        self.p.time.time=lambda:2000
        self.schedule()
        self.assertEqual(self.row()['due'],19000)
        self.due()
        self.p._process_one(self.card)
        self.schedule()
        self.assertEqual(self.row()['state'],'sent')
        self.account.send_message.assert_called_once()

    def test_closed_and_refunded_are_rechecked_before_send(self):
        for status in ('CLOSED','REFUNDED','UNKNOWN'):
            with self.subTest(status=status):
                with self.p._db() as conn:
                    conn.execute('DELETE FROM reminders')
                self.order.status.name='PAID'
                self.p.time.time=lambda:1000
                self.schedule()
                self.due()
                self.order.status.name=status
                # move beyond the due time of this iteration's payment
                self.p.time.time=lambda:100000
                self.p._process_one(self.card)
                self.account.send_message.assert_not_called()
                self.assertNotEqual(self.row()['state'],'pending')

    def test_status_event_cancels_waiting_reminder(self):
        self.schedule()
        self.order.status.name='CLOSED'
        self.p.order_status_handler(self.card,self.event)
        self.assertEqual(self.row()['state'],'closed')
        self.due()
        self.assertFalse(self.p._process_one(self.card))

    def test_restart_keeps_due_time_and_does_not_repeat_uncertain_send(self):
        self.schedule()
        self.p._init_db()
        self.assertEqual(self.row()['due'],19000)
        with self.p._db() as conn:
            conn.execute("UPDATE reminders SET state='sending'")
        self.p._init_db()
        self.assertEqual(self.row()['state'],'uncertain')
        self.due()
        self.assertFalse(self.p._process_one(self.card))

    def test_foreign_sale_or_wrong_chat_never_gets_message(self):
        self.schedule()
        self.due()
        self.order.seller_id=99
        self.p._process_one(self.card)
        self.account.send_message.assert_not_called()
        self.assertEqual(self.row()['state'],'not_our_sale')

    def test_chat_mismatch_retries_verification_not_send(self):
        self.schedule()
        self.due()
        self.account.get_chat_by_name.return_value.name='AnotherBuyer'
        self.p._process_one(self.card)
        self.account.send_message.assert_not_called()
        self.assertEqual(self.row()['attempts'],1)
        self.assertEqual(self.row()['state'],'pending')

    def test_network_verification_failure_is_not_treated_as_paid(self):
        self.schedule()
        self.due()
        self.account.get_order.side_effect=TimeoutError()
        self.p._process_one(self.card)
        self.account.send_message.assert_not_called()
        self.assertEqual(self.row()['attempts'],1)

    def test_send_timeout_or_none_result_has_no_automatic_retry(self):
        self.schedule()
        self.due()
        self.account.send_message.return_value=None
        self.p._process_one(self.card)
        self.assertEqual(self.row()['state'],'uncertain')
        self.p._process_one(self.card)
        self.account.send_message.assert_called_once()

    def test_disabled_does_not_schedule_and_stop_does_not_send(self):
        with self.p._db() as conn:
            conn.execute("UPDATE settings SET value='0' WHERE key='enabled'")
        self.schedule()
        self.assertIsNone(self.row())
        with self.p._db() as conn:
            conn.execute("UPDATE settings SET value='1' WHERE key='enabled'")
        self.schedule()
        self.due()
        self.p.stop_plugin()
        self.assertFalse(self.p._process_one(self.card))
        self.account.send_message.assert_not_called()
