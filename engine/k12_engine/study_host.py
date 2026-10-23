"""Owner-only bridge operations. Deployment configuration never comes from a workspace."""
import json
from pathlib import Path
from . import study_transport as transport


def approved_config():
    path = transport._store().with_name('study-service.json')
    if not path.is_file():
        raise transport.TransportError('blocked', 'study service is not configured by the local administrator')
    value = json.loads(path.read_text(encoding='utf-8'))
    if set(value) != {'endpoint', 'command'}:
        raise transport.TransportError('blocked', 'invalid administrator study service configuration')
    return value


def binding_context(args):
    binding = transport.load_binding(args['cwd'])
    return {'credentialRef': binding['credentialRef'] if binding else None,
            'bindingVersion': binding['bindingVersion'] if binding else None,
            'enabled': bool(binding and binding.get('uiAuthorized'))}


def open_task(args):
    from . import study_practice
    binding = transport.load_binding(args['cwd'])
    if not binding or binding['endpoint'] != approved_config()['endpoint'].rstrip('/'):
        raise transport.TransportError('blocked', 'study origin is not approved')
    return study_practice.open_task(args)


def watch_list(args):
    from . import study_practice
    if args:
        raise transport.TransportError('blocked', 'watch enumeration accepts no overrides')
    endpoint = approved_config()['endpoint'].rstrip('/')
    items = []
    for binding in transport.known_bindings():
        if binding['endpoint'] == endpoint:
            items.extend(study_practice.armed_tasks(binding, limit=30 - len(items)))
        if len(items) >= 30:
            break
    return {'items': items, 'limit': 30}


def notification_ids(args):
    from .session import Workspace
    ws = Workspace(args.get('cwd'), args.get('session'))
    return {'requestIds': transport.notification_ids(str(ws.root.parent), ws.session)}


def connect(args):
    private = transport.request_credentials.get()
    if not private or not private.get('token') or not private.get('credentialRef'):
        raise transport.TransportError('blocked', 'credential unavailable')
    approved = approved_config()
    probe = transport._config(args['cwd'], {**approved, 'studentId': 'probe', 'credentialRef': private['credentialRef']})
    identity = transport.Transport(probe)._invoke(['whoami'])
    if (not isinstance(identity, dict) or not isinstance(identity.get('student'), dict)
            or identity.get('capabilities', {}).get('learningRecordVersion') != 'learning-v1'):
        raise transport.TransportError('blocked', 'student identity or server capability mismatch')
    student = identity['student']
    transport.bind(args['cwd'], {**approved, 'studentId': student['id'], 'credentialRef': private['credentialRef']},
                   ui_authorized=True, display_name=student.get('displayName') or student.get('name'))
    return status(args)


def status(args):
    from . import study_practice
    binding = transport.load_binding(args['cwd'])
    try:
        endpoint = approved_config()['endpoint']
    except transport.TransportError:
        endpoint = None
    return {**study_practice.status(args), 'sessionReady': True,
            'connected': binding is not None, 'enabled': bool(binding and binding.get('uiAuthorized')),
            'student': {'id': binding['studentId'], 'displayName': binding.get('displayName', binding['studentId'])} if binding else None,
            'siteUrl': endpoint, 'historyUrl': f"{binding['endpoint']}/s/{binding['studentId']}/history" if binding else None,
            'settingsUrl': f'{endpoint}/settings' if endpoint else None}
