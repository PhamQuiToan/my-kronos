"""API contract tests; no weights or network required."""
import copy
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from flask import Flask

from webui.forecast_api import MAX_BYTES, TIMEFRAMES, create_forecast_blueprint, validate


class ForecastAPITest(unittest.TestCase):
    def setUp(self):
        log_dir = tempfile.TemporaryDirectory()
        self.addCleanup(log_dir.cleanup)
        self.log_path = Path(log_dir.name) / 'forecast_api.log'
        log_patch = patch('webui.forecast_api.LOG_PATH', self.log_path)
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.addCleanup(self.close_log)
        self.close_log()
        self.env = patch.dict('os.environ', {'KRONOS_API_TOKEN': 'test-only-secret',
                                            'KRONOS_LEARNING_ENABLED': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []
        self.loads = 0
        self.error = False
        self.gate = None
        self.done = threading.Event()
        self.app = Flask(__name__)
        self.app.register_blueprint(create_forecast_blueprint(self.factory))
        self.client = self.app.test_client()
        self.body = {'symbol': 'BTC', 'timeframe': 'M15', 'pred_len': 2, 'candles': [
            {'timestamp': '2026-10-30T00:00:00Z', 'open': 100, 'high': 110, 'low': 90, 'close': 101, 'volume': 10},
            {'timestamp': '2026-10-30T00:15:00Z', 'open': 101, 'high': 111, 'low': 91, 'close': 102, 'volume': 11}]}

    def close_log(self):
        logger = logging.getLogger('kronos.forecast.access')
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()

    def test_file_logs_success_validation_and_auth_failure(self):
        responses = [self.post(), self.post(body={}),
                     self.client.post('/api/forecast', json=self.body)]
        self.assertEqual([r.status_code for r in responses], [200, 400, 401])
        contents = self.log_path.read_text()
        for response in responses:
            expected_lines = {200: 4, 400: 3, 401: 2}[response.status_code]
            self.assertEqual(contents.count(response.get_json()['request_id']), expected_lines)
            self.assertIn(f'status={response.status_code}', contents)
        self.assertIn('symbol must be BTC or GOLD', contents)
        self.assertIn('Valid Bearer token required', contents)
        self.assertNotIn('test-only-secret', contents)
        self.assertNotIn('Authorization', contents)

    def test_file_logs_invalid_candle_input(self):
        import json
        self.body['candles'].reverse()
        response = self.post()
        self.assertEqual(response.status_code, 400)
        lines = self.log_path.read_text().splitlines()
        inputs = [line for line in lines if 'event=input body=' in line]
        self.assertEqual(len(inputs), 1)
        self.assertIn(response.get_json()['request_id'], inputs[0])
        raw = json.loads(inputs[0].split('event=input body=', 1)[1])
        self.assertEqual(json.loads(raw), self.body)

    def factory(self):
        self.loads += 1
        return self

    def predict(self, **kwargs):
        self.calls.append(kwargs)
        if self.gate:
            self.gate.wait(2)
        self.done.set()
        if self.error:
            raise RuntimeError('sensitive internal details')
        return pd.DataFrame({'open': [101., 102.], 'high': [110., 111.], 'low': [90., 91.], 'close': [102., 103.]})

    def post(self, body=None, **kwargs):
        return self.client.post('/api/forecast', json=self.body if body is None else body,
                                headers={'Authorization': 'Bearer test-only-secret'}, **kwargs)

    def test_success_and_cache(self):
        for symbol in ('BTC', 'GOLD'):
            self.body['symbol'] = symbol
            response = self.post()
            self.assertEqual(response.status_code, 200)
            result = response.get_json()
            self.assertEqual(result['forecast'][0]['timestamp'], '2026-10-30T00:30:00+00:00')
            self.assertNotIn('volume', result['forecast'][0])
            self.assertNotIn('confidence', result['forecast'][0])
            self.assertEqual(response.headers['X-Request-ID'], result['request_id'])
            self.assertEqual(self.calls[-1]['df']['close'].tolist(), [101, 102])
        self.assertEqual(self.loads, 1)

    def test_forecast_result_cache_ignores_learn_and_refreshes_after_expiry(self):
        first = self.post()
        self.assertEqual(first.status_code, 200)
        call_count = len(self.calls)

        self.body['learn'] = True
        cached = self.post()
        self.assertEqual(cached.status_code, 200)
        self.assertNotEqual(first.get_json()['request_id'], cached.get_json()['request_id'])
        self.assertEqual(len(self.calls), call_count)

        self.body['candles'][0]['close'] = 100.5
        with patch('webui.forecast_api.FORECAST_CACHE_TTL_SECONDS', 0):
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.post().status_code, 200)
        self.assertEqual(len(self.calls), call_count + 2)

    def test_authentication(self):
        for auth in ('', 'Bearer wrong', 'Basic test-only-secret', 'Bearer ü'):
            response = self.client.post('/api/forecast', json=self.body, headers={'Authorization': auth})
            self.assertEqual(response.status_code, 401)
            self.assertIn('request_id', response.get_json())
        with patch.dict('os.environ', {'KRONOS_API_TOKEN': ''}):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.loads, 0)

    def test_invalid_data(self):
        mutations = [lambda b: b.update(symbol='ETH'), lambda b: b.update(timeframe='M7'),
                     lambda b: b.update(candles=[]), lambda b: b.update(pred_len=True),
                     lambda b: b.update(sample_count=1.5), lambda b: b.update(top_p=float('nan')),
                     lambda b: b['candles'][0].update(high=99),
                     lambda b: b['candles'][0].update(volume=-1),
                     lambda b: b['candles'][0].update(close='100'),
                     lambda b: b['candles'][0].update(timestamp='2026-10-30'),
                     lambda b: b['candles'][1].update(timestamp='2026-10-30T00:00:00Z'),
                     lambda b: b['candles'].reverse(),
                     lambda b: b['candles'][1].update(timestamp='2026-10-30T00:45:00Z'),
                     lambda b: b.update(candles=b['candles'] * 257)]
        # Use independent blueprints to avoid the deliberate rate limit in this matrix.
        for mutate in mutations:
            app = Flask(__name__)
            app.register_blueprint(create_forecast_blueprint(self.factory))
            body = copy.deepcopy(self.body)
            mutate(body)
            with self.subTest(body=body):
                response = app.test_client().post('/api/forecast', json=body,
                                                  headers={'Authorization': 'Bearer test-only-secret'})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.loads, 0)

    def test_debug_logs_authenticated_request_body(self):
        body = copy.deepcopy(self.body)
        body['timeframe'] = 'M7'
        with self.assertLogs(self.app.logger, level='DEBUG') as captured:
            response = self.post(body=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn(response.get_json()['request_id'], captured.output[0])
        self.assertIn('"timeframe": "M7"', captured.output[0])

    def test_model_failure(self):
        self.error = True
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('sensitive', response.get_data(as_text=True))
        self.error = False
        self.assertEqual(self.post().status_code, 200)

    def test_validation_reason_in_response_and_log(self):
        self.body['timeframe'] = ['M15']
        with self.assertLogs(self.app.logger, level='WARNING') as captured:
            response = self.post()
        self.assertEqual(response.status_code, 400)
        result = response.get_json()
        self.assertEqual(result['error']['code'], 'invalid_request')
        self.assertIn('timeframe must be', result['error']['message'])
        self.assertIn(result['error']['message'], captured.output[0])
        self.assertIn(result['request_id'], captured.output[0])
        self.assertNotIn('test-only-secret', captured.output[0])
        self.assertEqual(self.loads, 0)

    def test_malformed_json_has_specific_message(self):
        response = self.client.post('/api/forecast', data='{', content_type='application/json',
                                    headers={'Authorization': 'Bearer test-only-secret'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error']['message'],
                         'Request body must contain valid JSON')

    def test_payload_and_json(self):
        headers = {'Authorization': 'Bearer test-only-secret'}
        for raw, content_type, expected in [('{}', 'text/plain', 415), ('{', 'application/json', 400),
                                             ('[]', 'application/json', 400),
                                             (' ' * (MAX_BYTES + 1), 'application/json', 413)]:
            response = self.client.post('/api/forecast', data=raw, content_type=content_type, headers=headers)
            self.assertEqual(response.status_code, expected)
            self.assertIn('request_id', response.get_json())

    def test_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.post(body={}).status_code, 400)
        response = self.post()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '60')

    def test_timeout_does_not_queue_more_work(self):
        self.gate = threading.Event()
        self.app.config['FORECAST_TIMEOUT_SECONDS'] = 0.01
        try:
            self.assertEqual(self.post().status_code, 504)
            self.assertEqual(self.post().status_code, 503)
            self.assertEqual(self.loads, 1)
        finally:
            self.gate.set()
            self.done.wait(2)

    def test_defaults_and_timeframes(self):
        for timeframe, seconds in TIMEFRAMES.items():
            body = copy.deepcopy(self.body)
            body.pop('pred_len')
            body['timeframe'] = timeframe
            body['candles'][1]['timestamp'] = (pd.Timestamp(body['candles'][0]['timestamp'])
                                                 + pd.Timedelta(seconds=seconds)).isoformat()
            history, stamps, future, params = validate(body)
            self.assertEqual(len(future), 24)
            self.assertEqual(params, {'pred_len': 24, 'sample_count': 1, 'temperature': 1.0, 'top_p': 0.9})
            self.assertEqual(future.iloc[0] - stamps.iloc[-1], pd.Timedelta(seconds=seconds))

    def test_gold_session_gap_and_invalid_order(self):
        self.body['symbol'] = 'GOLD'
        self.body['candles'][0]['timestamp'] = '2026-10-26T20:45:00Z'
        self.body['candles'][1]['timestamp'] = '2026-10-26T22:00:00Z'
        self.assertEqual(self.post().status_code, 200)
        for timestamp in ('2026-10-26T20:45:00Z', '2026-10-26T20:30:00Z',
                          '2026-10-26T22:01:00Z'):
            self.body['candles'][1]['timestamp'] = timestamp
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.post().status_code, 400)

    def test_gold_forecast_skips_recurring_intraday_break(self):
        self.body['symbol'] = 'GOLD'
        stamps = pd.date_range('2026-10-26T20:45:00Z', '2026-10-28T20:45:00Z', freq='15min')
        self.body['candles'] = [dict(self.body['candles'][0], timestamp=t.isoformat())
                                for t in stamps if t.hour != 21]
        history, _, future, _ = validate(self.body)
        self.assertEqual(len(history), len(self.body['candles']))
        self.assertEqual(future.iloc[0], pd.Timestamp('2026-10-28T22:00:00Z'))
        self.assertEqual(future.iloc[1], pd.Timestamp('2026-10-28T22:15:00Z'))
        self.body['symbol'] = 'BTC'
        with self.assertRaises(ValueError):
            validate(self.body)

    def test_chunked_payload_limit(self):
        from io import BytesIO
        response = self.client.post('/api/forecast', content_type='application/json',
                                    headers={'Authorization': 'Bearer test-only-secret'},
                                    environ_overrides={'wsgi.input': BytesIO(b' ' * (MAX_BYTES + 1)),
                                                       'wsgi.input_terminated': True, 'CONTENT_LENGTH': ''})
        self.assertEqual(response.status_code, 413)

    def test_extreme_numbers(self):
        self.body['pred_len'] = 10 ** 400
        self.assertEqual(self.post().status_code, 400)
        self.body['pred_len'] = 2
        self.body['candles'][0]['high'] = 10 ** 400
        self.assertEqual(self.post().status_code, 400)

    def test_invalid_model_output(self):
        with patch.object(self, 'predict', return_value=pd.DataFrame({'close': [1, 2]})):
            self.assertEqual(self.post().status_code, 503)
        with patch.object(self, 'predict', return_value=pd.DataFrame(
                {key: [float('nan'), 2] for key in ('open', 'high', 'low', 'close')})):
            self.assertEqual(self.post().status_code, 503)


if __name__ == '__main__':
    unittest.main()
