"""Offline learning lifecycle tests; no downloads or expensive training."""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from webui.learning import BASE_MODEL, Store
from webui.learning_worker import prepare, run_market, worker_lock, passes


class LearningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        log_patch = patch('webui.forecast_api.LOG_PATH', Path(self.tmp.name) / 'api.log')
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.addCleanup(self.close_log)
        self.close_log()
        self.store = Store(self.tmp.name)
        self.stamps = pd.Series(pd.date_range('2025-01-01', periods=2048, freq='15min', tz='UTC'))
        self.history = pd.DataFrame({'open': 100., 'high': 110., 'low': 90., 'close': 101.,
                                     'volume': 10.}, index=range(2048))

    def close_log(self):
        logger = logging.getLogger('kronos.forecast.access')
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()

    def seed(self):
        return self.store.collect('BTC', 'M15', self.history, self.stamps, 900)

    def trainer(self, trainval, folder, device):
        self.assertEqual(len(trainval), 1792)
        self.assertLess(trainval.timestamps.iloc[-1], self.stamps.iloc[-256])
        checkpoint = folder / 'model'
        checkpoint.mkdir()
        (checkpoint / 'config.json').write_text('{}')
        (checkpoint / 'model.safetensors').write_bytes(b'test only')
        return str(checkpoint)

    def evaluator(self, model, frame, start, device):
        self.assertEqual(start, 128)
        self.assertEqual(frame.timestamps.iloc[start], self.stamps.iloc[-256])
        return {'mae': 2. if model == BASE_MODEL else 1., 'persistence_mae': 3., 'points': 240}

    def test_collect_only_closed_immutable_deduplicated_isolated(self):
        now = self.stamps.iloc[1]
        n = self.store.collect('BTC', 'M15', self.history.iloc[:3], self.stamps.iloc[:3], 900, now)
        self.assertEqual(n, 1)
        changed = self.history.iloc[:3].copy()
        changed['close'] = 102
        self.store.collect('BTC', 'M15', changed, self.stamps.iloc[:3], 900, now)
        self.assertEqual(self.store.snapshot('BTC', 'M15').close.tolist(), [101.])
        self.assertTrue(self.store.snapshot('GOLD', 'M15').empty)

    def test_promote_then_rollback_and_no_repeat_job(self):
        self.seed()
        result = run_market(self.store, 'BTC', 'M15', 900, trainer=self.trainer, evaluator=self.evaluator)
        self.assertEqual(result['status'], 'promoted')
        self.assertEqual(self.store.active('BTC', 'M15'), result['candidate'])
        self.assertEqual(self.store.active('GOLD', 'M15'), BASE_MODEL)
        self.assertEqual(run_market(self.store, 'BTC', 'M15', 900)['status'], 'waiting_for_data')
        self.assertEqual(self.store.rollback('BTC', 'M15'), BASE_MODEL)
        self.assertEqual(self.store.active('BTC', 'M15'), BASE_MODEL)

    def test_failed_training_keeps_registry_and_is_retryable(self):
        self.seed()
        def fail(*args):
            raise RuntimeError('training failed')
        with self.assertRaises(RuntimeError):
            run_market(self.store, 'BTC', 'M15', 900, trainer=fail)
        self.assertEqual(self.store.active('BTC', 'M15'), BASE_MODEL)
        self.assertEqual(self.store.cutoff('BTC', 'M15'), 0)
        report = next((self.store.root / 'versions').glob('*/report.json'))
        self.assertEqual(json.loads(report.read_text())['status'], 'failed')

    def test_rejected_model_never_activated(self):
        self.seed()
        def bad(*args):
            return {'mae': 5., 'persistence_mae': 3., 'points': 240}
        result = run_market(self.store, 'BTC', 'M15', 900, trainer=self.trainer, evaluator=bad)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.store.active('BTC', 'M15'), BASE_MODEL)
        self.assertGreater(self.store.cutoff('BTC', 'M15'), 0)

    def test_nonfinite_and_equal_metrics_fail_gate(self):
        incumbent = {'mae': 1., 'points': 240}
        for value in (float('nan'), float('inf'), 1., .99):
            self.assertFalse(passes({'mae': value, 'points': 240, 'persistence_mae': 2.}, incumbent, .02))

    def test_holdout_is_fresh_and_btc_gaps_do_not_join(self):
        self.seed()
        frame = self.store.snapshot('BTC', 'M15')
        self.assertIsNone(prepare(frame, 900, 'BTC', self.stamps.iloc[-250].value))
        frame.loc[1000:, 'timestamps'] += pd.Timedelta(minutes=15)
        self.assertIsNone(prepare(frame, 900, 'BTC', 0))
        self.assertIsNotNone(prepare(frame, 900, 'GOLD', 0))

    def test_worker_lock(self):
        with worker_lock(self.store.root):
            with self.assertRaises(RuntimeError):
                with worker_lock(self.store.root):
                    pass

    def test_promotion_cannot_overwrite_changed_registry(self):
        self.store.finish('BTC', 'M15', 1, {}, candidate='another-model', expected=BASE_MODEL)
        with self.assertRaises(RuntimeError):
            self.store.finish('BTC', 'M15', 2, {}, candidate='stale-job', expected=BASE_MODEL)
        self.assertEqual(self.store.active('BTC', 'M15'), 'another-model')
        self.assertEqual(self.store.cutoff('BTC', 'M15'), 1)

    def test_api_collection_opt_in_and_model_switch(self):
        from flask import Flask
        from webui.forecast_api import create_forecast_blueprint
        class Predictor:
            def predict(self, **kwargs):
                return pd.DataFrame({k: [100.] for k in ('open', 'high', 'low', 'close')})
        with patch.dict('os.environ', {'KRONOS_LEARNING_ENABLED': '1',
                                      'KRONOS_LEARNING_DIR': self.tmp.name,
                                      'KRONOS_API_TOKEN': 'test-secret'}):
            loads = []
            def factory():
                loads.append(1)
                return Predictor()
            app = Flask(__name__)
            app.register_blueprint(create_forecast_blueprint(factory))
            client = app.test_client()
            body = {'symbol': 'BTC', 'timeframe': 'M15', 'pred_len': 1,
                    'candles': [dict(row, timestamp=stamp.isoformat()) for row, stamp in
                                zip(self.history.head(2).to_dict('records'), self.stamps.head(2))]}
            def post(auth=True):
                return client.post('/api/forecast', json=body,
                                   headers={'Authorization': 'Bearer test-secret'} if auth else {})
            self.assertEqual(post().status_code, 200)
            self.assertTrue(self.store.snapshot('BTC', 'M15').empty)
            body['learn'] = True
            self.assertEqual(post(False).status_code, 401)
            self.assertTrue(self.store.snapshot('BTC', 'M15').empty)
            self.assertEqual(post().status_code, 200)
            self.assertEqual(len(self.store.snapshot('BTC', 'M15')), 2)
            self.store.finish('BTC', 'M15', 1, {}, candidate='test-model', expected=BASE_MODEL)
            self.assertEqual(post().get_json()['model'], 'test-model')
            self.assertEqual(len(loads), 2)
            self.store.rollback('BTC', 'M15')
            self.assertEqual(post().get_json()['model'], BASE_MODEL)
            self.assertEqual(len(loads), 3)

    def test_bad_checkpoint_falls_back_to_base_not_another_market(self):
        from flask import Flask
        from webui.forecast_api import create_forecast_blueprint
        class Predictor:
            def predict(self, **kwargs):
                return pd.DataFrame({k: [100.] for k in ('open', 'high', 'low', 'close')})
        def load(path):
            if path == 'broken-gold':
                raise RuntimeError('broken checkpoint')
            return Predictor()
        self.store.finish('BTC', 'M15', 1, {}, candidate='btc-model', expected=BASE_MODEL)
        self.store.finish('GOLD', 'M15', 1, {}, candidate='broken-gold', expected=BASE_MODEL)
        with patch.dict('os.environ', {'KRONOS_LEARNING_ENABLED': '1',
                                      'KRONOS_LEARNING_DIR': self.tmp.name,
                                      'KRONOS_API_TOKEN': 'test-secret'}), \
                patch('webui.forecast_api.load_predictor', side_effect=load) as loader:
            app = Flask(__name__)
            app.register_blueprint(create_forecast_blueprint(loader))
            client = app.test_client()
            body = {'timeframe': 'M15', 'pred_len': 1,
                    'candles': [dict(row, timestamp=stamp.isoformat()) for row, stamp in
                                zip(self.history.head(2).to_dict('records'), self.stamps.head(2))]}
            for symbol, expected in [('BTC', 'btc-model'), ('GOLD', BASE_MODEL)]:
                response = client.post('/api/forecast', json=dict(body, symbol=symbol),
                                       headers={'Authorization': 'Bearer test-secret'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()['model'], expected)


if __name__ == '__main__':
    unittest.main()
