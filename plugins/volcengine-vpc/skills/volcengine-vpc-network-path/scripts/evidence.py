"""Offline evidence references and structured answer checks. No cloud access."""
import hashlib
import json
from datetime import datetime


def pointer(value, path):
    if not isinstance(path, str) or (path and not path.startswith('/')):
        raise ValueError('Expected JSON pointer')
    for part in path.split('/')[1:] if path else []:
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list):
            if not part.isdigit():
                raise ValueError('Invalid array pointer')
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError('Pointer does not address a container')
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp required')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return stamp


class Evidence:
    def __init__(self, bundle, context, window):
        if not isinstance(bundle, dict) or not isinstance(context, dict):
            raise ValueError('Evidence bundle and context required')
        if not context.get('account') or not context.get('region'):
            raise ValueError('Account and region context required')
        self.bundle, self.context = bundle, context
        self.start, self.end = timestamp(window['start']), timestamp(window['end'])
        if self.start > self.end:
            raise ValueError('Reversed target window')
        self.used = {}

    def read(self, ref):
        if not isinstance(ref, dict):
            raise ValueError('Evidence reference must be an object')
        source = self.bundle[ref['source']]
        if not isinstance(source, dict):
            raise ValueError('Capture must be an object')
        if source.get('context') != self.context:
            raise ValueError('Evidence account/region context mismatch')
        # A current capture cannot be relabelled by changing gate.observed_at.
        # Historical data must carry its independently saved effective interval.
        if source.get('kind') == 'historical_configuration':
            start = timestamp(pointer(source['response'], source['valid_from_pointer']))
            end = timestamp(pointer(source['response'], source['valid_to_pointer']))
            if not start <= self.start <= self.end <= end:
                raise ValueError('Historical configuration does not cover target window')
        elif source.get('kind') == 'current_configuration':
            if not self.start <= timestamp(source['observed_at']) <= self.end:
                raise ValueError('Current observation is outside target window')
        else:
            raise ValueError('Unsupported evidence kind for configuration proof')
        raw = source['response']
        metadata = raw.get('ResponseMetadata', {}) if isinstance(raw, dict) else {}
        if not isinstance(metadata, dict) or not metadata.get('RequestId') or metadata.get('Error') or not source.get('request'):
            raise ValueError('Successful response and original request required')
        request = source['request']
        if not isinstance(request, dict) or request.get('Region') != self.context['region']:
            raise ValueError('Original request region mismatch or missing')
        if metadata.get('Region', self.context['region']) != self.context['region']:
            raise ValueError('Response region mismatch')
        if metadata.get('Action', request.get('Action')) != request.get('Action'):
            raise ValueError('Request/response action mismatch')
        if 'AccountId' in request and str(request['AccountId']) != str(self.context['account']):
            raise ValueError('Original request account mismatch')
        digest = hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':')).encode()).hexdigest()
        self.used[ref['source']] = {'sha256': digest, 'request_id': metadata['RequestId'],
                                    'observed_at': source.get('observed_at')}
        return pointer(raw, ref['pointer'])


def verify_claims(result, claims):
    """Check declared structured claims, not arbitrary prose or product semantics."""
    if not isinstance(claims, list):
        raise ValueError('answer_claims must be a list')
    errors = []
    for i, claim in enumerate(claims):
        try:
            expected = pointer(result, claim['field'])
            actual = claim['value']
            # bool is not a numeric count; preserve exact structured values.
            matches = (type(expected) is type(actual) and expected == actual)
            if not matches:
                errors.append({'index': i, 'field': claim['field'], 'expected': expected})
        except (KeyError, TypeError, ValueError, IndexError):
            errors.append({'index': i, 'reason': 'Unknown or invalid answer field'})
    return {'status': 'consistent' if claims and not errors else ('mismatch' if errors else 'not_checked'),
            'checked': len(claims), 'errors': errors,
            'boundary': 'Declared fields only; unlisted prose is not verified'}
