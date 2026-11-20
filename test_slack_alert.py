import json, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock
import slack_alert

class SlackAlertTests(unittest.TestCase):
    def test_posts_to_fyi_and_returns_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'webhooks.env'
            path.write_text('SLACK_WEBHOOK_ALERTS=https://hooks.slack.com/services/T0/B0/a\nSLACK_WEBHOOK_FYI=https://hooks.slack.com/services/T0/B0/f\n')
            response=MagicMock();response.read.return_value=b'ok'
            context=MagicMock();context.__enter__.return_value=response
            opener=MagicMock(return_value=context)
            receipt=slack_alert.post('Price watch: MacBook','M5 Pro <64GB> & more','fyi',path,opener)
            request=opener.call_args.args[0]
            self.assertEqual(request.full_url,'https://hooks.slack.com/services/T0/B0/f')
            self.assertTrue(json.loads(request.data)['text'].startswith('*Price watch: MacBook*\nM5 Pro &lt;64GB&gt; &amp; more'))
            self.assertTrue(receipt.startswith('slack-fyi:'))

    def test_missing_webhook_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):slack_alert.webhook('fyi',Path(tmp)/'none.env')

if __name__=='__main__':unittest.main()
