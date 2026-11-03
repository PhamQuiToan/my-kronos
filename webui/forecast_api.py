"""Authenticated client-candle forecast API, with bounded per-process inference."""
import hashlib
import hmac
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pandas as pd
from flask import Blueprint, Flask, current_app, g, jsonify, request
from werkzeug.exceptions import HTTPException

from webui.api_token import read_token
from webui.learning import BASE_MODEL, Store, enabled as learning_enabled

TIMEFRAMES = {**{f'M{n}': n * 60 for n in (1, 5, 15, 30)},
              **{f'H{n}': n * 3600 for n in (1, 4)}, 'D1': 86400}
FIELDS = ['open', 'high', 'low', 'close', 'volume']
MAX_BYTES = 512 * 1024
FORECAST_CACHE_MAX_ENTRIES = 128
LOG_PATH = Path(__file__).resolve().parent / 'logs' / 'forecast_api.log'
_log_lock = threading.Lock()


def gold_future_timestamps(stamps, interval, count):
    """Skip intraday slots missing on multiple dates, unless observed trading there.

    This is an input-derived heuristic, not an exchange holiday/weekend calendar.
    """
    day = pd.Timedelta(days=1)
    missing_dates = {}
    observed = {stamp - stamp.normalize() for stamp in stamps}
    for a, b in zip(stamps, stamps[1:]):
        if interval < b - a < day:
            candidate = a + interval
            while candidate < b:
                slot = candidate - candidate.normalize()
                missing_dates.setdefault(slot, set()).add(candidate.date())
                candidate += interval
    closed = {slot for slot, dates in missing_dates.items()
              if len(dates) >= 2 and slot not in observed}
    points = []
    candidate = stamps[-1]
    while len(points) < count:
        candidate += interval
        if candidate - candidate.normalize() not in closed:
            points.append(candidate)
    return pd.Series(points)


def forecast_logger():
    logger = logging.getLogger('kronos.forecast.access')
    with _log_lock:
        if not logger.handlers:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024,
                                          backupCount=3, encoding='utf-8', delay=True)
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
    return logger


def load_predictor(model_path=BASE_MODEL):
    # Lazy import keeps API validation and tests independent of model installation.
    from model import Kronos, KronosPredictor, KronosTokenizer
    tokenizer = KronosTokenizer.from_pretrained('NeoQuasar/Kronos-Tokenizer-base')
    model = Kronos.from_pretrained(model_path)
    tokenizer.eval()
    model.eval()
    return KronosPredictor(model, tokenizer, device=os.getenv('KRONOS_API_DEVICE', 'cpu'), max_context=512)


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('JSON body must be an object')
    if set(data) - {'symbol', 'timeframe', 'candles', 'pred_len', 'temperature', 'top_p', 'sample_count', 'learn'}:
        raise ValueError('Unknown request fields')
    if data.get('symbol') not in ('BTC', 'GOLD'):
        raise ValueError('symbol must be BTC or GOLD')
    if not isinstance(data.get('timeframe'), str) or data['timeframe'] not in TIMEFRAMES:
        raise ValueError('timeframe must be M1, M5, M15, M30, H1, H4 or D1')
    candles = data.get('candles')
    if not isinstance(candles, list) or not 2 <= len(candles) <= 512:
        raise ValueError('Provide 2 to 512 candles; all supplied candles are used')
    params = {}
    for name, default, minimum, maximum in [('pred_len', 24, 1, 120), ('sample_count', 1, 1, 5),
                                           ('temperature', 1.0, 0.01, 2), ('top_p', 0.9, 0.01, 1)]:
        value = data.get(name, default)
        if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
            raise ValueError(f'Invalid {name}: expected {minimum}..{maximum}')
        if name in ('pred_len', 'sample_count') and type(value) is not int:
            raise ValueError(f'{name} must be an integer')
        params[name] = value
    stamps, rows = [], []
    for candle in candles:
        if not isinstance(candle, dict) or set(candle) != {'timestamp', *FIELDS}:
            raise ValueError('Each candle requires timestamp and exactly OHLCV fields')
        if not isinstance(candle['timestamp'], str):
            raise ValueError('timestamp must be an ISO 8601 string with timezone')
        try:
            stamp = pd.Timestamp(candle['timestamp'])
            if pd.isna(stamp) or stamp.tzinfo is None:
                raise ValueError()
            stamp = stamp.tz_convert('UTC')
            # Validate representability by the model timestamp pipeline.
            _ = stamp.value
        except (ValueError, TypeError, OverflowError):
            raise ValueError('timestamp must be a valid ISO 8601 string with timezone') from None
        values = [candle[key] for key in FIELDS]
        if any(type(v) not in (int, float) or abs(v) > 1e15 or not math.isfinite(v) for v in values):
            raise ValueError('OHLCV must contain finite JSON numbers with magnitude at most 1e15')
        o, h, l, c, v = values
        if min(o, h, l, c) <= 0 or v < 0 or l > min(o, c) or h < max(o, c) or l > h:
            raise ValueError('Invalid OHLC bounds, price or volume')
        stamps.append(stamp)
        rows.append(values)
    interval = pd.Timedelta(seconds=TIMEFRAMES[data['timeframe']])
    if data['symbol'] == 'GOLD':
        if any(b <= a or (b - a) % interval != pd.Timedelta(0)
               for a, b in zip(stamps, stamps[1:])):
            raise ValueError('GOLD candles must be strictly increasing with gaps that are multiples of the timeframe')
    elif any(b - a != interval for a, b in zip(stamps, stamps[1:])):
        raise ValueError('Candles must be strictly increasing with exactly one timeframe between candles')
    try:
        if data['symbol'] == 'GOLD':
            future = gold_future_timestamps(stamps, interval, params['pred_len'])
        else:
            future = pd.Series(pd.date_range(stamps[-1] + interval, periods=params['pred_len'], freq=interval))
    except (ValueError, OverflowError):
        raise ValueError('Forecast timestamps are out of range') from None
    return pd.DataFrame(rows, columns=FIELDS), pd.Series(stamps), future, params


def create_forecast_blueprint(predictor_factory=load_predictor):
    bp = Blueprint('forecast_api', __name__)
    access_log = forecast_logger()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='kronos-forecast')
    slot = threading.Lock()
    rate_lock = threading.Lock()
    arrivals = deque()
    forecast_cache = OrderedDict()
    cache_lock = threading.Lock()
    cached = None
    cached_model = None
    learning_store = Store() if learning_enabled() else None

    def fail(code, message, status):
        return jsonify(error={'code': code, 'message': message}, request_id=g.forecast_request_id), status

    @bp.before_request
    def protect():
        g.forecast_request_id = uuid.uuid4().hex
        g.forecast_started = time.monotonic()
        access_log.info('request_id=%s event=started method=%s path=/api/forecast',
                        g.forecast_request_id, request.method)
        token = read_token()
        if not token.strip():
            return fail('unavailable', 'Forecast authentication is not configured', 503)
        auth = request.headers.get('Authorization', '').split()
        supplied = auth[1] if len(auth) == 2 and auth[0].lower() == 'bearer' else ''
        if not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(token.encode()).digest()):
            response, status = fail('unauthorized', 'Valid Bearer token required', 401)
            response.headers['WWW-Authenticate'] = 'Bearer'
            return response, status
        with rate_lock:
            now = time.monotonic()
            while arrivals and arrivals[0] <= now - 60:
                arrivals.popleft()
            if len(arrivals) >= 10:
                response, status = fail('rate_limited', 'Limit is 10 authenticated requests per minute', 429)
                response.headers['Retry-After'] = '60'
                return response, status
            arrivals.append(now)

    @bp.after_request
    def headers(response):
        response.headers['X-Request-ID'] = g.forecast_request_id
        response.headers['Cache-Control'] = 'no-store'
        error = (response.get_json(silent=True) or {}).get('error', {})
        access_log.log(logging.WARNING if response.status_code >= 400 else logging.INFO,
                       'request_id=%s event=completed status=%s duration_ms=%.1f code=%s reason=%s',
                       g.forecast_request_id, response.status_code,
                       (time.monotonic() - g.forecast_started) * 1000,
                       error.get('code', '-'), json.dumps(error.get('message', '-'), ensure_ascii=False))
        return response

    @bp.errorhandler(HTTPException)
    def http_error(exc):
        return fail('http_error', exc.name, exc.code)

    @bp.errorhandler(Exception)
    def unexpected_error(exc):
        current_app.logger.error('Forecast request failed request_id=%s', g.forecast_request_id)
        return fail('internal_error', 'Forecast request failed', 500)

    def get_cached_forecast(key):
        now = time.monotonic()
        with cache_lock:
            entry = forecast_cache.get(key)
            if entry is None:
                return None
            expires, points, model_path = entry
            if expires <= now:
                del forecast_cache[key]
                return None
            forecast_cache.move_to_end(key)
            return points, model_path

    def cache_forecast(key, points, model_path, timeframe):
        with cache_lock:
            forecast_cache[key] = (
                time.monotonic() + TIMEFRAMES[timeframe], points, model_path)
            forecast_cache.move_to_end(key)
            while len(forecast_cache) > FORECAST_CACHE_MAX_ENTRIES:
                forecast_cache.popitem(last=False)

    def run(history, stamps, future, params, model_path, symbol, timeframe):
        nonlocal cached, cached_model
        try:
            if cached is None or cached_model != model_path:
                try:
                    replacement = (load_predictor(model_path) if predictor_factory is load_predictor
                                   else predictor_factory())
                except Exception:
                    # Never serve a model cached for another market as fallback.
                    if cached is None or cached_model != BASE_MODEL:
                        cached = None
                        cached = (load_predictor(BASE_MODEL) if predictor_factory is load_predictor
                                  else predictor_factory())
                    model_path = BASE_MODEL
                    access_log.warning('event=model_load_fallback symbol=%s timeframe=%s', symbol, timeframe)
                else:
                    cached = replacement
                cached_model = model_path
            result = cached.predict(df=history, x_timestamp=stamps.dt.tz_localize(None),
                                    y_timestamp=future.dt.tz_localize(None), pred_len=params['pred_len'],
                                    T=params['temperature'], top_p=params['top_p'],
                                    sample_count=params['sample_count'], verbose=False)
            columns = [key for key in [*FIELDS, 'amount'] if key in result.columns]
            if len(result) != params['pred_len'] or not set(FIELDS[:4]).issubset(columns):
                raise RuntimeError('Invalid model result shape')
            points = []
            for stamp, (_, row) in zip(future, result.iterrows()):
                values = {key: float(row[key]) for key in columns}
                if not all(math.isfinite(v) for v in values.values()):
                    raise RuntimeError('Nonfinite model output')
                points.append({'timestamp': stamp.isoformat(), **values})
            return points, cached_model
        finally:
            slot.release()

    @bp.route('/api/forecast', methods=['POST'])
    def forecast():
        if request.content_length is not None and request.content_length > MAX_BYTES:
            return fail('payload_too_large', 'Request limit is 512 KiB', 413)
        if not request.is_json:
            return fail('unsupported_media_type', 'Content-Type application/json required', 415)
        # Bound reads even for chunked requests without Content-Length.
        raw = request.stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return fail('payload_too_large', 'Request limit is 512 KiB', 413)
        try:
            data = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            message = 'Request body must contain valid JSON'
            current_app.logger.warning('Forecast rejected request_id=%s reason=%s',
                                       g.forecast_request_id, message)
            return fail('invalid_request', message, 400)
        try:
            history, stamps, future, params = validate(data)
        except ValueError as exc:
            message = str(exc)
            current_app.logger.warning('Forecast rejected request_id=%s reason=%s',
                                       g.forecast_request_id, message)
            return fail('invalid_request', message, 400)
        model_path = BASE_MODEL
        if learning_store is not None:
            try:
                inserted = learning_store.collect(data['symbol'], data['timeframe'], history, stamps,
                                                  TIMEFRAMES[data['timeframe']])
                model_path = learning_store.active(data['symbol'], data['timeframe'])
                access_log.info('request_id=%s event=learning_collected inserted=%s',
                                g.forecast_request_id, inserted)
            except Exception:
                access_log.error('request_id=%s event=learning_store_failed', g.forecast_request_id)
                return fail('learning_unavailable', 'Could not persist training candles; retry later', 503)
        cache_input = {key: value for key, value in data.items() if key != 'learn'}
        cache_digest = hashlib.sha256(
            (model_path + '\0' + json.dumps(cache_input, sort_keys=True, separators=(',', ':'))).encode()
        ).hexdigest()
        cached_result = get_cached_forecast(cache_digest)
        if cached_result is None:
            access_log.info('request_id=%s event=forecast_cache_miss', g.forecast_request_id)
        else:
            access_log.info('request_id=%s event=forecast_cache_hit', g.forecast_request_id)
            points, model_path = cached_result
        if cached_result is None:
            if not slot.acquire(blocking=False):
                return fail('busy', 'Forecast worker is busy; retry later', 503)
            try:
                job = executor.submit(
                    run, history, stamps, future, params, model_path, data['symbol'], data['timeframe'])
            except Exception:
                slot.release()
                return fail('unavailable', 'Forecast worker is unavailable', 503)
            try:
                points, model_path = job.result(timeout=current_app.config.get('FORECAST_TIMEOUT_SECONDS', 120))
            except TimeoutError:
                # Keep the slot until inference actually finishes; never queue more work.
                return fail('timeout', 'Forecast deadline exceeded', 504)
            except Exception:
                current_app.logger.error('Forecast failed request_id=%s', g.forecast_request_id)
                return fail('forecast_failed', 'Model loading or forecast failed', 503)
            cache_forecast(cache_digest, points, model_path, data['timeframe'])
        return jsonify(request_id=g.forecast_request_id, symbol=data['symbol'], timeframe=data['timeframe'],
                       generated_at=pd.Timestamp.now(tz='UTC').isoformat(),
                       last_input_timestamp=stamps.iloc[-1].isoformat(),
                       model=model_path, parameters={**params, 'lookback': len(history)},
                       forecast=points)

    return bp


def create_app():
    """Dedicated API server exposing only the authenticated forecast endpoint."""
    app = Flask(__name__)
    app.register_blueprint(create_forecast_blueprint())
    return app
