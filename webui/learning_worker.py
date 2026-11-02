"""Scheduled offline fine-tuning. Run with python -m webui.learning_worker run."""
import argparse
import fcntl
import gc
import json
import math
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from webui.learning import BASE_MODEL, TOKENIZER, Store

PROJECT = Path(__file__).resolve().parents[1]


@contextmanager
def worker_lock(root):
    with (root / 'worker.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another training/rollback job is running') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prepare(frame, seconds, symbol, cutoff, min_rows=2048, min_new=512, holdout=256):
    if frame.empty:
        return None
    # Missing API calls must not silently join disjoint BTC sequences for training.
    deltas = frame.timestamps.diff().dt.total_seconds()
    broken = (deltas != seconds) if symbol == 'BTC' else ((deltas <= 0) | (deltas % seconds != 0))
    broken.iloc[0] = False
    if broken.any():
        frame = frame.iloc[int(broken[broken].index[-1]):].reset_index(drop=True)
    if len(frame) < min_rows or int((frame.timestamps.astype('int64') > cutoff).sum()) < min_new:
        return None
    trainval, test = frame.iloc[:-holdout].copy(), frame.iloc[-holdout:].copy()
    if int(test.timestamps.iloc[0].value) <= cutoff:
        return None  # Never select a new version using the previous job's test set.
    return trainval, test


def train_candidate(trainval, folder, device):
    import yaml
    frame = trainval.copy()
    frame['amount'] = frame[['open', 'high', 'low', 'close']].mean(axis=1) * frame.volume
    frame['timestamps'] = frame.timestamps.dt.tz_localize(None)
    csv = folder / 'train-validation.csv'
    frame.to_csv(csv, index=False)
    config = {
        'data': {'data_path': str(csv), 'lookback_window': 128, 'predict_window': 24,
                 'max_context': 512, 'train_ratio': .8, 'val_ratio': .2, 'test_ratio': 0.0},
        'training': {'basemodel_epochs': 2, 'batch_size': 4, 'num_workers': 0,
                     'seed': 42, 'predictor_learning_rate': 1e-6},
        'model_paths': {'base_save_path': str(folder), 'pretrained_predictor': BASE_MODEL,
                        'pretrained_tokenizer': TOKENIZER, 'finetuned_tokenizer': TOKENIZER},
        'experiment': {'use_comet': False},
    }
    path = folder / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    env = dict(os.environ, KRONOS_TRAIN_DEVICE=device)
    # Dedicated subprocess frees all training memory before evaluation.
    with (folder / 'training.log').open('w') as log:
        subprocess.run([sys.executable, 'finetune_base_model.py', '--config', str(path)],
                       cwd=PROJECT / 'finetune_csv', env=env, stdout=log,
                       stderr=subprocess.STDOUT, check=True,
                       timeout=int(os.getenv('KRONOS_TRAIN_TIMEOUT', '21600')))
    return str(folder / 'basemodel' / 'best_model')


def evaluate(model_path, frame, test_start, device):
    import numpy as np
    import torch
    from model import Kronos, KronosTokenizer, KronosPredictor
    tokenizer = KronosTokenizer.from_pretrained(TOKENIZER).eval()
    model = Kronos.from_pretrained(model_path).eval()
    predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
    errors, naive = [], []
    try:
        # Non-overlapping target windows; each prediction sees only past real candles.
        for start in range(test_start, len(frame) - 24 + 1, 24):
            history = frame.iloc[start - 128:start]
            target = frame.iloc[start:start + 24]
            torch.manual_seed(42 + start)
            np.random.seed(42 + start)
            result = predictor.predict(
                df=history[['open', 'high', 'low', 'close', 'volume']].copy(),
                x_timestamp=history.timestamps.dt.tz_localize(None).reset_index(drop=True),
                y_timestamp=target.timestamps.dt.tz_localize(None).reset_index(drop=True),
                pred_len=24, T=1.0, top_p=.9, sample_count=1, verbose=False)
            predicted = result.close.to_numpy()
            actual = target.close.to_numpy()
            if len(predicted) != len(actual) or not np.isfinite(predicted).all():
                raise ValueError('Invalid evaluation forecast')
            errors.extend(np.abs(predicted - actual).tolist())
            naive.extend(np.abs(history.close.iloc[-1] - actual).tolist())
        if not errors:
            raise ValueError('Empty evaluation set')
        return {'mae': float(np.mean(errors)), 'persistence_mae': float(np.mean(naive)),
                'points': len(errors)}
    finally:
        del predictor, model, tokenizer
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def passes(candidate, incumbent, improvement):
    values = [candidate['mae'], incumbent['mae'], candidate['persistence_mae']]
    return (candidate['points'] > 0 and candidate['points'] == incumbent['points']
            and all(math.isfinite(v) and v >= 0 for v in values)
            and candidate['mae'] < incumbent['mae'] * (1 - improvement)
            and candidate['mae'] < candidate['persistence_mae'])


def run_market(store, symbol, timeframe, seconds, device='cpu', trainer=train_candidate,
               evaluator=evaluate, min_rows=2048, min_new=512, improvement=.02):
    split = prepare(store.snapshot(symbol, timeframe), seconds, symbol,
                    store.cutoff(symbol, timeframe), min_rows, min_new)
    if split is None:
        return {'status': 'waiting_for_data', 'symbol': symbol, 'timeframe': timeframe}
    trainval, test = split
    incumbent = store.active(symbol, timeframe)
    folder = store.root / 'versions' / f'{symbol}-{timeframe}-{uuid.uuid4().hex}'
    folder.mkdir(parents=True)
    report = {'symbol': symbol, 'timeframe': timeframe, 'incumbent': incumbent,
              'train_validation_end': trainval.timestamps.iloc[-1].isoformat(),
              'test_start': test.timestamps.iloc[0].isoformat(),
              'test_end': test.timestamps.iloc[-1].isoformat(), 'improvement_required': improvement}
    try:
        test.to_csv(folder / 'test.csv', index=False)
        candidate = trainer(trainval, folder, device)
        if not (Path(candidate) / 'config.json').is_file() or not (Path(candidate) / 'model.safetensors').is_file():
            raise ValueError('Candidate checkpoint is incomplete')
        evaluation = pd.concat([trainval.tail(128), test], ignore_index=True)
        report['candidate_metrics'] = evaluator(candidate, evaluation, 128, device)
        report['incumbent_metrics'] = evaluator(incumbent, evaluation, 128, device)
        accepted = passes(report['candidate_metrics'], report['incumbent_metrics'], improvement)
        report.update(status='promoted' if accepted else 'rejected', candidate=candidate)
        # Commit registry only after candidate reload and inference succeeded in evaluation.
        store.finish(symbol, timeframe, int(test.timestamps.iloc[-1].value), report,
                     candidate=candidate if accepted else None, expected=incumbent)
    except Exception as exc:
        report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        (folder / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    return report


def main():
    from webui.forecast_api import TIMEFRAMES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run', 'status', 'rollback'])
    parser.add_argument('--symbol', choices=['BTC', 'GOLD'])
    parser.add_argument('--timeframe', choices=list(TIMEFRAMES))
    args = parser.parse_args()
    store = Store()
    if args.command == 'status':
        for symbol, timeframe in store.markets():
            print(json.dumps({'symbol': symbol, 'timeframe': timeframe,
                              'model': store.active(symbol, timeframe),
                              'last_evaluated_timestamp_ns': store.cutoff(symbol, timeframe)}))
        return
    with worker_lock(store.root):
        if args.command == 'rollback':
            if not args.symbol or not args.timeframe:
                parser.error('rollback requires --symbol and --timeframe')
            print(store.rollback(args.symbol, args.timeframe))
        else:
            failures = 0
            for symbol, timeframe in store.markets():
                if args.symbol and symbol != args.symbol or args.timeframe and timeframe != args.timeframe:
                    continue
                try:
                    print(json.dumps(run_market(store, symbol, timeframe, TIMEFRAMES[timeframe],
                                                device=os.getenv('KRONOS_TRAIN_DEVICE', 'cpu'))), flush=True)
                except Exception as exc:
                    failures += 1
                    print(f'{symbol}/{timeframe}: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            if failures:
                raise SystemExit(1)


if __name__ == '__main__':
    main()
